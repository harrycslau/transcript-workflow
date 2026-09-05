"""CLI tests for ``brain search-index status|rebuild`` (Step 5A.2).

Exit semantics (locked): healthy → 0; not built / stale / inconsistent /
missing-or-broken FTS → 1; usage → 2; rebuild lock contention → 3.
Status is read-only and NEVER takes the pipeline lock; rebuild is
mutating and DOES.
"""

from __future__ import annotations

import json

import pytest
from django.db import connection

from brainlib import cli
from factories import make_transcribed_recording
from workflow.services import search_index as si

pytestmark = pytest.mark.django_db


def _seed(sha: str, text: str = "cli segment content"):
    return make_transcribed_recording([text], sha=sha)


class TestStatus:
    def test_healthy_status_exits_zero(self, capsys):
        _seed("cli-ok")
        assert cli.main(["search-index", "rebuild"]) == 0
        capsys.readouterr()
        assert cli.main(["search-index", "status"]) == 0
        assert "healthy: True" in capsys.readouterr().out

    def test_never_built_corpus_exits_one(self, capsys):
        _seed("cli-stale")
        assert cli.main(["search-index", "status"]) == 1
        out = capsys.readouterr().out
        assert "healthy: False" in out

    def test_stale_index_exits_one(self, capsys):
        rec, transcript, section = _seed("cli-stale2")
        assert cli.main(["search-index", "rebuild"]) == 0
        capsys.readouterr()
        from workflow.models import TranscriptSegment

        TranscriptSegment.objects.filter(transcript=transcript).update(text="edited source")
        assert cli.main(["search-index", "status"]) == 1
        out = capsys.readouterr().out
        assert "stale_content" in out

    def test_missing_fts_table_exits_one(self, capsys):
        _seed("cli-missing")
        cli.main(["search-index", "rebuild"])
        capsys.readouterr()
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_search_fts")
        assert cli.main(["search-index", "status", "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["fts"] == {"state": "missing", "category": "fts_missing"}
        assert payload["categories"]["fts_missing"] == 1

    def test_json_report_has_categories_counts_keys_and_no_text(self, capsys):
        rec, transcript, _section = _seed("cli-json", text="super-private-content-XYZ")
        cli.main(["search-index", "rebuild"])
        capsys.readouterr()
        from workflow.models import TranscriptSegment

        TranscriptSegment.objects.filter(transcript=transcript).update(text="changed source")
        assert cli.main(["search-index", "status", "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert set(si.CATEGORIES).issubset(payload["categories"])
        assert payload["counts"]["source_documents"] >= 2
        assert "stale_content" in payload["keys"]
        assert payload["keys"]["stale_content"][0].startswith("segment:")
        assert "super-private-content-XYZ" not in json.dumps(payload)

    def test_status_never_acquires_the_pipeline_lock(self, monkeypatch, capsys):
        _seed("cli-nolock")
        si.rebuild_index()  # build the index without the CLI lock path

        def forbidden(*args, **kwargs):
            raise AssertionError("status must not touch the pipeline lock")

        monkeypatch.setattr("workflow.services.pipeline_lock.pipeline_lock", forbidden)
        monkeypatch.setattr("workflow.services.pipeline.pipeline_lock", forbidden)
        assert cli.main(["search-index", "status"]) == 0

    def test_status_read_only_after_content_changes(self):
        _seed("cli-ro")
        cli.main(["search-index", "rebuild"])
        before = si.build_status_report()
        assert cli.main(["search-index", "status"]) == 0
        assert si.build_status_report()["healthy"] == before["healthy"]


class TestRebuild:
    def test_rebuild_lock_contention_exits_3(self, capsys):
        from brainlib.config import load_config
        from workflow.services.pipeline import pipeline_lock

        _seed("cli-busy")
        config = load_config()
        with pipeline_lock(config):
            assert cli.main(["search-index", "rebuild"]) == 3
        assert "another pipeline process" in capsys.readouterr().err

    def test_rebuild_repairs_broken_fts_and_reports_counts(self, capsys):
        _seed("cli-repair")
        cli.main(["search-index", "rebuild"])
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_search_fts")
            cursor.execute(
                "CREATE VIRTUAL TABLE workflow_search_fts USING fts5(body_text, tokenize='porter')"
            )
        capsys.readouterr()
        assert cli.main(["search-index", "rebuild", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["result"] == "rebuilt"
        assert payload["documents"]["recordings"] >= 1
        assert cli.main(["search-index", "status", "--json"]) == 0

    def test_rebuild_sanitized_when_fts5_trigram_unavailable(self, monkeypatch, capsys):
        monkeypatch.setattr(si, "fts5_trigram_available", lambda: False)
        _seed("cli-nocap")
        assert cli.main(["search-index", "rebuild"]) == 1
        err = capsys.readouterr().err
        assert "trigram" in err
        assert "Traceback" not in err

    def test_rebuild_blocked_on_pending_migrations(self, monkeypatch, capsys):
        def pending():
            return ["workflow.0099_fake"]

        monkeypatch.setattr("brainlib.migrations.unapplied_migrations", pending)
        assert cli.main(["search-index", "rebuild"]) == 1
        assert "out of date" in capsys.readouterr().err


class TestUsage:
    def test_missing_action_exits_2(self):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["search-index"])
        assert excinfo.value.code == 2

    def test_unknown_action_exits_2(self):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["search-index", "defragment"])
        assert excinfo.value.code == 2
