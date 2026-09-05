"""CLI tests for ``brain search "QUERY"`` (Step 5A.4.1).

Exit semantics (locked): search ran (even with zero results) → 0;
config/setup or MISSING/BROKEN/STALE index → 1 (concise stderr, never
rebuilds); malformed query or limit → 2. Search NEVER takes the
pipeline lock and NEVER rebuilds, repairs or synchronizes.
"""

from __future__ import annotations

import json

import pytest
from django.db import connection

from brainlib import cli
from factories import make_transcribed_recording
from workflow.services import search_index as si

pytestmark = pytest.mark.django_db


def _seed(texts, sha):
    return make_transcribed_recording(texts, sha=sha)


def _healthy():
    _seed(["quarterly budget review meeting"], "cli-ok")
    _seed(["garden planning discussion"], "cli-other")
    assert cli.main(["search-index", "rebuild"]) == 0


class TestSearch:
    def test_human_output_numbers_results_with_highlights(self, capsys):
        _healthy()
        capsys.readouterr()
        assert cli.main(["search", "budget"]) == 0
        out = capsys.readouterr().out
        assert '1 result(s) for "budget"' in out
        assert "«budget»" in out
        assert "1." in out

    def test_json_payload_shape(self, capsys):
        _healthy()
        capsys.readouterr()
        assert cli.main(["search", "budget", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["query"] == "budget"
        assert payload["result_count"] == 1
        assert payload["index_version"] == si.INDEX_VERSION
        assert payload["truncated"] is False
        assert payload["more_recordings_matched"] == 0
        first = payload["results"][0]
        assert first["rank"] == 1
        assert first["match"]["source"] == "segment"
        assert first["snippet"]["matches"]

    def test_zero_results_is_success(self, capsys):
        _healthy()
        capsys.readouterr()
        assert cli.main(["search", "elephant"]) == 0
        assert "no results" in capsys.readouterr().out

    def test_limit_note_and_bounds(self, capsys):
        _seed(["needle in this story"], "n1")
        _seed(["another needle tale"], "n2")
        si.rebuild_index()
        capsys.readouterr()
        assert cli.main(["search", "needle", "--limit", "1"]) == 0
        out = capsys.readouterr().out
        assert "1 result(s)" in out
        assert "more matching recording(s)" in out

    def test_segment_provenance_label(self, capsys):
        _healthy()
        capsys.readouterr()
        assert cli.main(["search", "garden"]) == 0
        assert "[segment 0 @ 0:00]" in capsys.readouterr().out

    def test_never_acquires_the_pipeline_lock(self, monkeypatch, capsys):
        _healthy()
        capsys.readouterr()

        def forbidden(*args, **kwargs):
            raise AssertionError("search must not touch the pipeline lock")

        # Explicit pre-import to avoid binding a patched attribute as the
        # freshly imported module's "original" value (see 5A.4.1 notes).
        import workflow.services.pipeline as pipeline_service
        import workflow.services.pipeline_lock as pipeline_lock_service

        monkeypatch.setattr(pipeline_lock_service, "pipeline_lock", forbidden)
        monkeypatch.setattr(pipeline_service, "pipeline_lock", forbidden)
        assert cli.main(["search", "budget"]) == 0

    def test_never_rebuilds_even_when_healthy(self, monkeypatch, capsys):
        _healthy()
        capsys.readouterr()

        def forbidden(*args, **kwargs):
            raise AssertionError("search must never rebuild")

        monkeypatch.setattr(si, "rebuild_index", forbidden)
        assert cli.main(["search", "budget"]) == 0

    def test_full_health_preflight_runs_exactly_once(self, monkeypatch, capsys):
        _healthy()
        capsys.readouterr()
        calls = []
        import workflow.services.search_query as search_query

        real = search_query.build_status_report

        def counting(**kwargs):
            calls.append(True)
            return real(**kwargs)

        monkeypatch.setattr(search_query, "build_status_report", counting)
        assert cli.main(["search", "budget"]) == 0
        assert len(calls) == 1

    def test_json_query_is_normalized_echo_only(self, capsys):
        _seed(["KÄYTÖSSÄ tapaaminen"], "cli-fi")
        si.rebuild_index()
        capsys.readouterr()
        assert cli.main(["search", "käytössä", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["query"] == "käytössä"
        assert payload["result_count"] == 1


class TestSearchErrors:
    def test_stale_index_hard_fails_exit_1_without_content_leak(self, capsys):
        rec, transcript, _section = _seed(["private super-secret-XYZ content"], "cli-stale")
        cli.main(["search-index", "rebuild"])
        capsys.readouterr()
        from workflow.models import TranscriptSegment

        TranscriptSegment.objects.filter(transcript=transcript).update(text="edited")
        assert cli.main(["search", "super-secret-XYZ"]) == 1
        captured = capsys.readouterr()
        assert "stale_content" in captured.err
        assert "brain search-index rebuild" in captured.err
        assert "super-secret-XYZ" not in captured.err
        assert captured.out == ""

    def test_missing_fts_exit_1(self, capsys):
        _healthy()
        capsys.readouterr()
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_search_fts")
        assert cli.main(["search", "budget"]) == 1
        assert "missing" in capsys.readouterr().err

    def test_broken_fts_exit_1(self, capsys):
        _healthy()
        capsys.readouterr()
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_search_fts")
            cursor.execute(
                "CREATE VIRTUAL TABLE workflow_search_fts USING fts5(body_text)"
            )
        assert cli.main(["search", "budget"]) == 1
        assert "broken" in capsys.readouterr().err

    @pytest.mark.parametrize("query", ["", "   ", "\t"])
    def test_empty_query_exit_2(self, query, capsys):
        _healthy()
        capsys.readouterr()
        assert cli.main(["search", query]) == 2
        assert "empty" in capsys.readouterr().err

    def test_over_long_query_exit_2(self, capsys):
        _healthy()
        capsys.readouterr()
        assert cli.main(["search", "x" * 300]) == 2
        assert "256" in capsys.readouterr().err

    def test_too_many_words_exit_2(self, capsys):
        _healthy()
        capsys.readouterr()
        assert cli.main(["search", " ".join(f"w{i}" for i in range(9))]) == 2

    @pytest.mark.parametrize("bad", ["0", "-3", "201"])
    def test_bad_limit_exit_2(self, bad, capsys):
        _healthy()
        capsys.readouterr()
        assert cli.main(["search", "budget", "--limit", bad]) == 2
        assert "limit" in capsys.readouterr().err

    def test_usage_errors_precede_health_gate(self, capsys):
        """A malformed query must fail with 2 WITHOUT paying the full
        integrity sweep (and before any index-state question)."""
        _seed(["usage order probe"], "cli-order")
        capsys.readouterr()  # never built on purpose
        assert cli.main(["search", "   "]) == 2  # usage 2, not stale-index 1


class TestHumanRenderer:
    def test_truncated_and_more_notes_render(self, capsys):
        payload = {
            "query": "q",
            "index_version": si.INDEX_VERSION,
            "limit": 1,
            "result_count": 1,
            "truncated": True,
            "more_recordings_matched": None,
            "results": [
                {
                    "rank": 1,
                    "recording_id": "r",
                    "title": "T",
                    "match": {"source": "metadata", "document_key": "recording:r"},
                    "snippet": None,
                }
            ],
        }
        cli._print_search_human(payload)
        out = capsys.readouterr().out
        assert "candidate scan reached its bound" in out
        assert "1. T  [metadata]" in out

    def test_summary_label_and_decorated_snippet(self):
        snippet = {
            "field": "body_text",
            "text": "…said hello now…",
            "matches": [{"start": 6, "end": 11}],
            "ellipsis_before": True,
            "ellipsis_after": True,
        }
        assert cli._decorate_snippet(snippet) == "…said «hello» now…"
        assert cli._search_source_label(
            {"source": "summary", "output_language": "en"}
        ) == "summary · en"
        assert cli._format_ms(75000) == "1:15"
        assert cli._format_ms(3723000) == "1:02:03"
