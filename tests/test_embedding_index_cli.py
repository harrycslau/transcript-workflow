"""CLI tests for ``brain embedding-index status|rebuild|repair`` (Step 5B.3).

Exit semantics: healthy → 0; unhealthy / sanitized failure → 1; usage →
2; rebuild/repair lock contention → 3. status is read-only and NEVER
takes the pipeline lock or runs recovery; rebuild/repair go through the
shared pipeline runner (schema preflight BEFORE lock/recovery, exclusive
lock, recover_interruptions). All embedding calls are mocked; no real
network.
"""

from __future__ import annotations

import json

import pytest
from django.db import connection

from brainlib import cli
from brainlib.config import EmbeddingConfig
from factories import make_config, make_transcribed_recording
from workflow.models import EmbeddingGeneration, EmbeddingGenerationState, SearchDocument
from workflow.services import embedding_index as ei
from workflow.services import search_index as si
from workflow.services.embedding_client import EmbeddingBatch, EmbeddingHTTPError

# transaction=True: the CLI rebuild/repair paths run the service, whose
# fixed precondition refuses to run inside a SQLite transaction, and
# pytest-django's default outer transaction would trip it.
pytestmark = pytest.mark.django_db(transaction=True)


def cli_config(tmp_path, monkeypatch, **overrides):
    """Point ``brainlib.config.load_config`` at a config with an embedding
    model set (the CLI resolves it at call time inside each handler)."""
    config = make_config(
        tmp_path,
        embedding=EmbeddingConfig(
            base_url="http://127.0.0.1:1/v1",
            model=overrides.pop("model", "test-embed-model"),
            api_key_env="BRAIN_TEST_LLM_API_KEY",
            timeout_seconds=120,
            batch_size=overrides.pop("batch_size", 4),
        ),
        **overrides,
    )
    monkeypatch.setattr("brainlib.config.load_config", lambda: config)
    return config


def seed_healthy(tmp_path, monkeypatch):
    """Recording + rebuilt search index + healthy embedding generation."""
    make_transcribed_recording(["cli embed seed"], sha="cli-embed-1")
    si.rebuild_index()
    config = cli_config(tmp_path, monkeypatch)
    ei.rebuild_embedding_index(
        config,
        embedder=lambda c, texts: [
            EmbeddingBatch(text=t, embedding=(0.1, 0.1, 0.1, 0.1)) for t in texts
        ],
    )
    return config


def mock_embedder(dim=4):
    return lambda c, texts: [
        EmbeddingBatch(text=t, embedding=tuple([0.1] * dim)) for t in texts
    ]


class TestStatus:
    def test_healthy_status_exits_zero(self, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)
        assert cli.main(["embedding-index", "status"]) == 0
        assert "healthy: True" in capsys.readouterr().out

    def test_unhealthy_status_exits_one(self, tmp_path, monkeypatch, capsys):
        make_transcribed_recording(["no embedding yet"], sha="cli-embed-2")
        si.rebuild_index()
        cli_config(tmp_path, monkeypatch)
        assert cli.main(["embedding-index", "status"]) == 1
        out = capsys.readouterr().out
        assert "healthy: False" in out
        assert "no_active_generation" in out

    def test_status_json_payload(self, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)
        assert cli.main(["embedding-index", "status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["healthy"] is True
        assert payload["active_generation"]["model"] == "test-embed-model"

    def test_status_never_locks_or_recovers(self, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)

        # Import the pipeline modules BEFORE patching: importing
        # workflow.services.pipeline while pipeline_lock.pipeline_lock is
        # already patched would bind the forbidden stub into the module
        # attribute (from pipeline_lock import pipeline_lock), and the
        # recorded "original" for the later patch would then be the stub
        # — leaking it into subsequent tests.
        from workflow.services import pipeline as _pipeline_mod  # noqa: F401
        from workflow.services import pipeline_lock as _pipeline_lock_mod  # noqa: F401

        def forbidden(*args, **kwargs):
            raise AssertionError("status must not lock or recover")

        monkeypatch.setattr("workflow.services.pipeline_lock.pipeline_lock", forbidden)
        monkeypatch.setattr("workflow.services.pipeline.pipeline_lock", forbidden)
        monkeypatch.setattr("workflow.services.pipeline.recover_interruptions", forbidden)
        assert cli.main(["embedding-index", "status"]) == 0

    def test_status_blocked_on_pending_migrations(self, monkeypatch, capsys):
        def pending():
            return ["workflow.0099_fake"]

        monkeypatch.setattr("brainlib.migrations.unapplied_migrations", pending)

        def forbidden(*args, **kwargs):
            raise AssertionError("no lock/recovery/network before the preflight")

        monkeypatch.setattr("workflow.services.pipeline.pipeline_lock", forbidden)
        assert cli.main(["embedding-index", "status"]) == 1
        assert "out of date" in capsys.readouterr().err


class TestRebuild:
    def test_rebuild_happy_path_json(self, tmp_path, monkeypatch, capsys):
        make_transcribed_recording(["cli rebuild"], sha="cli-rebuild-1")
        si.rebuild_index()
        cli_config(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", mock_embedder()
        )
        assert cli.main(["embedding-index", "rebuild", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["result"] == "rebuilt"
        assert payload["healthy"] is True
        assert payload["documents"] == SearchDocument.objects.count()
        assert cli.main(["embedding-index", "status", "--json"]) == 0

    def test_rebuild_lock_contention_exits_3(self, tmp_path, monkeypatch, capsys):
        from workflow.services.pipeline import pipeline_lock

        make_transcribed_recording(["cli busy"], sha="cli-busy-1")
        si.rebuild_index()
        config = cli_config(tmp_path, monkeypatch)
        with pipeline_lock(config):
            assert cli.main(["embedding-index", "rebuild"]) == 3
        assert "another pipeline process" in capsys.readouterr().err

    def test_rebuild_calls_recovery_under_lock(self, tmp_path, monkeypatch, capsys):
        from workflow.services.pipeline import recover_interruptions as real_recover

        make_transcribed_recording(["cli recover"], sha="cli-rec-1")
        si.rebuild_index()
        cli_config(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", mock_embedder()
        )
        seen = {"recovery": False}

        def spy(config):
            seen["recovery"] = True
            return real_recover(config)

        monkeypatch.setattr("workflow.services.pipeline.recover_interruptions", spy)
        assert cli.main(["embedding-index", "rebuild"]) == 0
        assert seen["recovery"] is True

    def test_rebuild_blocked_on_pending_migrations(self, monkeypatch, capsys):
        def pending():
            return ["workflow.0099_fake"]

        monkeypatch.setattr("brainlib.migrations.unapplied_migrations", pending)

        def forbidden(*args, **kwargs):
            raise AssertionError("lock must not be taken before the preflight")

        monkeypatch.setattr("workflow.services.pipeline.pipeline_lock", forbidden)
        assert cli.main(["embedding-index", "rebuild"]) == 1
        captured = capsys.readouterr()
        assert "out of date" in captured.err
        assert "Traceback" not in captured.err

    def test_rebuild_failure_is_sanitized_no_traceback(self, tmp_path, monkeypatch, capsys):
        make_transcribed_recording(["cli fail"], sha="cli-fail-1")
        si.rebuild_index()
        cli_config(tmp_path, monkeypatch)

        def failing(config, texts):
            raise EmbeddingHTTPError(500)

        monkeypatch.setattr("workflow.services.embedding_index.embed_texts", failing)
        assert cli.main(["embedding-index", "rebuild"]) == 1
        err = capsys.readouterr().err
        assert "embedding request failed" in err
        assert "Traceback" not in err
        assert EmbeddingGeneration.objects.count() == 0

    def test_rebuild_uses_mock_not_real_client(self, tmp_path, monkeypatch, capsys):
        make_transcribed_recording(["cli nomock"], sha="cli-nomock-1")
        si.rebuild_index()
        cli_config(tmp_path, monkeypatch)

        def no_http(*args, **kwargs):
            raise AssertionError("real HTTP must never be used")

        monkeypatch.setattr("httpx.Client", no_http)
        monkeypatch.setattr("httpx.get", no_http)
        monkeypatch.setattr("httpx.post", no_http)
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", mock_embedder()
        )
        assert cli.main(["embedding-index", "rebuild"]) == 0

    def test_rebuild_sanitized_when_source_unhealthy(self, tmp_path, monkeypatch, capsys):
        make_transcribed_recording(["cli unhealthy"], sha="cli-uh-1")
        si.rebuild_index()
        cli_config(tmp_path, monkeypatch)
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_search_fts")
        try:
            assert cli.main(["embedding-index", "rebuild"]) == 1
            err = capsys.readouterr().err
            assert "search-index status" in err
            assert "search-index rebuild" in err
            assert "Traceback" not in err
        finally:
            # transaction=True commits the DROP for real: restore the FTS
            # virtual table for subsequent tests.
            si.rebuild_index()

    def test_rebuild_unexpected_failure_sanitized_no_traceback(self, tmp_path, monkeypatch, capsys):
        make_transcribed_recording(["cli unexpected"], sha="cli-unexp-1")
        si.rebuild_index()
        cli_config(tmp_path, monkeypatch)

        def explode(config, texts):
            raise RuntimeError("CANARY-UNEXPECTED-CLI-SECRET")

        monkeypatch.setattr("workflow.services.embedding_index.embed_texts", explode)
        assert cli.main(["embedding-index", "rebuild"]) == 1
        err = capsys.readouterr().err
        assert "failed unexpectedly" in err
        assert "CANARY-UNEXPECTED-CLI-SECRET" not in err
        assert "Traceback" not in err

    def test_status_failure_sanitized_no_traceback(self, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)

        def boom(*args, **kwargs):
            raise RuntimeError("CANARY-STATUS-CLI-SECRET")

        monkeypatch.setattr("workflow.services.embedding_index._active_page_invalid", boom)
        assert cli.main(["embedding-index", "status"]) == 1
        err = capsys.readouterr().err
        assert "CANARY-STATUS-CLI-SECRET" not in err
        assert "Traceback" not in err

    def test_rebuild_preflight_failure_sanitized_no_traceback(self, tmp_path, monkeypatch, capsys):
        make_transcribed_recording(["cli pref canary"], sha="cli-pref-canary")
        si.rebuild_index()
        cli_config(tmp_path, monkeypatch)

        def boom(*args, **kwargs):
            raise RuntimeError("CANARY-CLI-PREFLIGHT-SECRET")

        monkeypatch.setattr(si, "build_status_report", boom)
        assert cli.main(["embedding-index", "rebuild"]) == 1
        err = capsys.readouterr().err
        assert "failed unexpectedly" in err
        assert "CANARY-CLI-PREFLIGHT-SECRET" not in err
        assert "Traceback" not in err

    def test_repair_preflight_failure_sanitized_no_traceback(self, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)

        def boom(*args, **kwargs):
            raise RuntimeError("CANARY-CLI-REPAIR-PREFLIGHT-SECRET")

        monkeypatch.setattr(si, "build_status_report", boom)
        assert cli.main(["embedding-index", "repair"]) == 1
        err = capsys.readouterr().err
        assert "failed unexpectedly" in err
        assert "CANARY-CLI-REPAIR-PREFLIGHT-SECRET" not in err
        assert "Traceback" not in err


class TestRepair:
    def test_repair_happy_path(self, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)
        # orphan the active generation
        gen = EmbeddingGeneration.objects.get(state=EmbeddingGenerationState.ACTIVE)
        from workflow.services.vector_codec import encode_vector

        from workflow.models import EmbeddingDocument

        EmbeddingDocument.objects.create(
            generation=gen, document_key="segment:cli-orphan:0",
            source_content_hash="a" * 64,
            vector_blob=encode_vector([0.1] * gen.dimensions, dimensions=gen.dimensions),
        )
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", mock_embedder()
        )
        assert cli.main(["embedding-index", "repair", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["result"] == "repaired"
        assert payload["healthy"] is True
        assert payload["deleted_orphans"] == 1

    def test_repair_lock_contention_exits_3(self, tmp_path, monkeypatch, capsys):
        from workflow.services.pipeline import pipeline_lock

        seed_healthy(tmp_path, monkeypatch)
        config = cli_config(tmp_path, monkeypatch)
        with pipeline_lock(config):
            assert cli.main(["embedding-index", "repair"]) == 3
        assert "another pipeline process" in capsys.readouterr().err

    def test_repair_incompatible_guidance(self, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)
        EmbeddingGeneration.objects.filter(
            state=EmbeddingGenerationState.ACTIVE
        ).update(embedding_version="2")
        assert cli.main(["embedding-index", "repair"]) == 1
        err = capsys.readouterr().err
        assert "embedding-index rebuild" in err
        assert "Traceback" not in err

    def test_repair_no_network_when_healthy(self, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)

        def forbidden(config, texts):
            raise AssertionError("no embedding work needed when healthy")

        monkeypatch.setattr("workflow.services.embedding_index.embed_texts", forbidden)
        assert cli.main(["embedding-index", "repair", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["healthy"] is True

    def test_repair_unexpected_failure_sanitized_no_traceback(self, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)
        # create stale work (source edited + search rebuilt) so the
        # embedder is actually invoked by the repair
        from workflow.models import TranscriptSegment

        TranscriptSegment.objects.filter(transcript__isnull=False).update(
            text="cli repair unexpected EDITED"
        )
        si.rebuild_index()

        def explode(config, texts):
            raise RuntimeError("CANARY-REPAIR-CLI-SECRET")

        monkeypatch.setattr("workflow.services.embedding_index.embed_texts", explode)
        assert cli.main(["embedding-index", "repair"]) == 1
        err = capsys.readouterr().err
        assert "failed unexpectedly" in err
        assert "CANARY-REPAIR-CLI-SECRET" not in err
        assert "Traceback" not in err


class TestUsage:
    def test_missing_action_exits_2(self):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["embedding-index"])
        assert excinfo.value.code == 2

    def test_unknown_action_exits_2(self):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["embedding-index", "probe"])
        assert excinfo.value.code == 2