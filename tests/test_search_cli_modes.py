"""CLI mode tests for ``brain search`` (Step 5C — Task 5).

Keyword stays the default with byte-for-byte output/JSON parity and
exactly one full health sweep; semantic/hybrid delegate to their
services (each owning exactly one source health sweep, one integrity
traversal and one embedding request). The CLI adds no second sweep,
never takes the pipeline lock, never writes, and maps usage errors to
exit 2 before any health/network work. All network is mocked; no real
HTTP.
"""

from __future__ import annotations

import builtins
import json
import sys

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from brainlib import cli
from brainlib.config import EmbeddingConfig
from factories import make_config, make_transcribed_recording
from workflow.models import (
    EmbeddingDocument,
    EmbeddingGeneration,
    EmbeddingGenerationState,
    Recording,
    TranscriptSegment,
)
from workflow.services import embedding_index as ei
from workflow.services import search_fusion as sf
from workflow.services import search_index as si
from workflow.services import search_query as sq
from workflow.services import semantic_query as sem
from workflow.services.embedding_client import EmbeddingBatch, EmbeddingHTTPError

# transaction=True: the semantic/hybrid engines refuse to run inside a
# SQLite transaction, and pytest-django's default outer transaction would
# trip that fixed precondition.
pytestmark = pytest.mark.django_db(transaction=True)

DIM = 4


# ---------------------------------------------------------------------------
# helpers (mirror the semantic engine's test setup)
# ---------------------------------------------------------------------------


def cli_config(tmp_path, monkeypatch, *, model="test-embed-model"):
    """Point ``brainlib.config.load_config`` at a config with an embedding
    model set (the CLI resolves it at call time inside the handler)."""
    config = make_config(
        tmp_path,
        embedding=EmbeddingConfig(
            base_url="http://127.0.0.1:1/v1",
            model=model,
            api_key_env="BRAIN_TEST_LLM_API_KEY",
            timeout_seconds=120,
            batch_size=32,
        ),
    )
    monkeypatch.setattr("brainlib.config.load_config", lambda: config)
    return config


def _unmatched_vector(dim=DIM):
    return [0.0] + [0.01] * (dim - 1)


def keyword_embedder(keywords, dim=DIM, *, tracker=None, fail_calls=(), guard_no_txn=False):
    """Deterministic fake embedder: a text containing keyword ``k`` gets
    1.0 on axis ``idx(k)``; the query text is dispatched identically."""
    state = {"calls": 0}

    def embed(config, texts):
        state["calls"] += 1
        if tracker is not None:
            tracker.append(list(texts))
        if guard_no_txn:
            assert connection.in_atomic_block is False, "network inside a transaction"
        if state["calls"] in fail_calls:
            raise EmbeddingHTTPError(503)
        out = []
        for text in texts:
            vec = _unmatched_vector(dim)
            for idx, keyword in enumerate(keywords):
                if keyword in text:
                    vec[idx] = 1.0
            out.append(EmbeddingBatch(text=text, embedding=tuple(vec)))
        return out

    embed.state = state
    return embed


def active_generation():
    return EmbeddingGeneration.objects.get(state=EmbeddingGenerationState.ACTIVE)


def seed_keyword():
    make_transcribed_recording(["quarterly budget review meeting"], sha="cli-kw-0")
    make_transcribed_recording(["garden planning discussion"], sha="cli-kw-1")
    si.rebuild_index()


def seed_healthy(tmp_path, monkeypatch, *, keywords=("alpha", "beta")):
    """Two transcribed recordings plus a healthy active embedding
    generation (dim 4, keyword-dispatch embedder)."""
    make_transcribed_recording(["alpha meeting discussion"], sha="cli-sem-0")
    make_transcribed_recording(["beta irrelevant chatter"], sha="cli-sem-1")
    si.rebuild_index()
    config = cli_config(tmp_path, monkeypatch)
    ei.rebuild_embedding_index(config, embedder=keyword_embedder(list(keywords)))
    return config


def seed_empty_embedding(tmp_path, monkeypatch):
    """Healthy but EMPTY corpus + empty active embedding generation."""
    si.rebuild_index()
    config = cli_config(tmp_path, monkeypatch)
    ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha", "beta"]))
    return config


def _patch_query_embedder(monkeypatch, embedder):
    monkeypatch.setattr("workflow.services.embedding_client.embed_texts", embedder)


def _counting_health(monkeypatch, module):
    """Count ``build_status_report`` calls on the module the code path
    actually uses (``search_query`` for the keyword preflight, the
    ``search_index`` module for semantic/hybrid)."""
    calls = []
    real = module.build_status_report

    def counting(*args, **kwargs):
        calls.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "build_status_report", counting)
    return calls


def _counting_decode(monkeypatch):
    state = {"rows": 0}
    real = sem._classify_active_page

    def counting(page, dimensions, using):
        state["rows"] += len(page)
        return real(page, dimensions, using)

    monkeypatch.setattr(sem, "_classify_active_page", counting)
    return state


def _active_row_count():
    return EmbeddingDocument.objects.filter(generation=active_generation()).count()


# ---------------------------------------------------------------------------
# 1. Keyword default/explicit parity, exactly one health, no embedding
# ---------------------------------------------------------------------------


class TestKeywordParity:
    def test_default_and_explicit_keyword_human_parity(self, capsys):
        seed_keyword()
        capsys.readouterr()
        assert cli.main(["search", "budget"]) == 0
        default_out = capsys.readouterr().out
        assert cli.main(["search", "budget", "--mode", "keyword"]) == 0
        explicit_out = capsys.readouterr().out
        assert default_out == explicit_out
        assert '1 result(s) for "budget"' in default_out
        assert "\u00abbudget\u00bb" in default_out
        assert "[keyword]" not in default_out

    def test_default_and_explicit_keyword_json_parity(self, capsys):
        seed_keyword()
        capsys.readouterr()
        assert cli.main(["search", "budget", "--json"]) == 0
        default_payload = json.loads(capsys.readouterr().out)
        assert cli.main(["search", "budget", "--mode", "keyword", "--json"]) == 0
        explicit_payload = json.loads(capsys.readouterr().out)
        assert default_payload == explicit_payload
        # Keyword JSON is the engine payload unchanged: no synthetic mode.
        assert "mode" not in default_payload
        assert default_payload["query"] == "budget"
        assert default_payload["result_count"] == 1

    def test_exactly_one_keyword_health_sweep(self, monkeypatch, capsys):
        seed_keyword()
        capsys.readouterr()
        calls = _counting_health(monkeypatch, sq)
        assert cli.main(["search", "budget"]) == 0
        assert len(calls) == 1

    def test_keyword_never_calls_embedding(self, monkeypatch, capsys):
        seed_keyword()
        capsys.readouterr()

        def forbidden(*args, **kwargs):
            raise AssertionError("keyword search must not call the embedder")

        _patch_query_embedder(monkeypatch, forbidden)
        assert cli.main(["search", "budget"]) == 0

    def test_keyword_never_imports_embedding_services(self, monkeypatch, capsys):
        import workflow.services as services_pkg

        seed_keyword()
        capsys.readouterr()

        guarded = {
            "workflow.services.semantic_query",
            "workflow.services.search_fusion",
            "workflow.services.embedding_client",
        }
        for name in list(guarded):
            monkeypatch.delitem(sys.modules, name, raising=False)
        for attr in ("semantic_query", "search_fusion", "embedding_client"):
            monkeypatch.delattr(services_pkg, attr, raising=False)

        real_import = builtins.__import__

        def spy_import(name, *args, **kwargs):
            if name in guarded:
                raise AssertionError(f"keyword search imported {name}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", spy_import)
        assert cli.main(["search", "budget"]) == 0


# ---------------------------------------------------------------------------
# 2. Semantic mode: success / zero / error / usage / JSON / human
# ---------------------------------------------------------------------------


class TestSemanticMode:
    def test_semantic_success_human_plain_snippet(self, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", "semantic"]) == 0
        out = capsys.readouterr().out
        assert '2 result(s) for "alpha" [semantic]' in out
        assert "segment 0 @ 0:00" in out
        # Semantic snippets are plain, unmarked text.
        assert "\u00ab" not in out
        assert "alpha meeting discussion" in out

    def test_semantic_json_has_mode_and_metadata(self, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", "semantic", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "semantic"
        assert payload["query"] == "alpha"
        assert payload["embedding_generation"]["model"] == "test-embed-model"
        assert payload["semantic_query_version"] == sem.SEMANTIC_QUERY_VERSION
        assert payload["result_count"] == 2
        assert payload["results"][0]["score"] >= payload["results"][-1]["score"]

    def test_semantic_zero_results_empty_corpus(self, tmp_path, monkeypatch, capsys):
        seed_empty_embedding(tmp_path, monkeypatch)

        def forbidden(*args, **kwargs):
            raise AssertionError("an empty corpus must not embed the query")

        _patch_query_embedder(monkeypatch, forbidden)
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", "semantic"]) == 0
        assert 'no results for "alpha" [semantic]' in capsys.readouterr().out

    def test_semantic_limit_note(self, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", "semantic", "--limit", "1"]) == 0
        out = capsys.readouterr().out
        assert "1 result(s)" in out
        assert "1 more matching recording(s)" in out

    def test_semantic_exactly_one_health_integrity_embed(
        self, tmp_path, monkeypatch, capsys
    ):
        seed_healthy(tmp_path, monkeypatch)
        health = _counting_health(monkeypatch, si)
        decoded = _counting_decode(monkeypatch)
        tracker = []
        _patch_query_embedder(
            monkeypatch, keyword_embedder(["alpha", "beta"], tracker=tracker)
        )
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", "semantic"]) == 0
        assert len(health) == 1
        assert len(tracker) == 1
        assert tracker[0] == ["alpha"]
        assert decoded["rows"] == _active_row_count()

    def test_semantic_never_calls_keyword_preflight(
        self, tmp_path, monkeypatch, capsys
    ):
        seed_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))

        def forbidden(*args, **kwargs):
            raise AssertionError("semantic must not run the keyword preflight")

        monkeypatch.setattr(sq, "preflight_full_health", forbidden)
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", "semantic"]) == 0


# ---------------------------------------------------------------------------
# 3. Hybrid mode: success / error / JSON / human and exact one/one/one
# ---------------------------------------------------------------------------


class TestHybridMode:
    def test_hybrid_success_human_with_evidence(self, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", "hybrid"]) == 0
        out = capsys.readouterr().out
        assert "[hybrid]" in out
        assert "segment 0 @ 0:00" in out
        # Compact evidence ranks are shown; the semantic-only result has an
        # unmarked plain-text snippet (``matches`` absent) rendered safely.
        assert "(kw #" in out
        assert "(sem #" in out
        assert "Untitled recording" in out

    def test_hybrid_json_has_evidence_and_components(
        self, tmp_path, monkeypatch, capsys
    ):
        seed_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", "hybrid", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "hybrid"
        assert payload["rrf_k"] == sf.RRF_K
        assert payload["components"]["keyword"]["depth"] == sf.HYBRID_DEPTH
        assert payload["components"]["semantic"]["depth"] == sf.HYBRID_DEPTH
        assert payload["results"]
        for result in payload["results"]:
            evidence = result["evidence"]
            assert set(evidence) == {
                "keyword_rank",
                "semantic_rank",
                "semantic_cosine",
                "rrf_score",
            }

    def test_hybrid_zero_results_empty_corpus(self, tmp_path, monkeypatch, capsys):
        seed_empty_embedding(tmp_path, monkeypatch)

        def forbidden(*args, **kwargs):
            raise AssertionError("an empty corpus must not embed the query")

        _patch_query_embedder(monkeypatch, forbidden)
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", "hybrid"]) == 0
        assert 'no results for "alpha" [hybrid]' in capsys.readouterr().out

    def test_hybrid_exactly_one_one_one_and_no_keyword_preflight(
        self, tmp_path, monkeypatch, capsys
    ):
        seed_healthy(tmp_path, monkeypatch)
        health = _counting_health(monkeypatch, si)
        decoded = _counting_decode(monkeypatch)

        keyword_calls = []
        real_search = sf.search_recordings

        def counting_search(*args, **kwargs):
            keyword_calls.append(True)
            return real_search(*args, **kwargs)

        monkeypatch.setattr(sf, "search_recordings", counting_search)

        def forbidden(*args, **kwargs):
            raise AssertionError("hybrid must not call the keyword preflight")

        monkeypatch.setattr(sq, "preflight_full_health", forbidden)

        tracker = []
        _patch_query_embedder(
            monkeypatch, keyword_embedder(["alpha", "beta"], tracker=tracker)
        )
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", "hybrid"]) == 0
        assert len(health) == 1
        assert len(tracker) == 1
        assert tracker[0] == ["alpha"]
        assert len(keyword_calls) == 1
        assert decoded["rows"] == _active_row_count()

    def test_hybrid_limit_note(self, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", "hybrid", "--limit", "1"]) == 0
        out = capsys.readouterr().out
        assert "1 result(s)" in out
        assert "1 more matching recording(s)" in out


# ---------------------------------------------------------------------------
# 4. Exit codes, sanitized failures, no lock/recovery/write
# ---------------------------------------------------------------------------


class TestExitCodesAndPurity:
    @pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
    def test_usage_errors_exit_2_before_health(self, mode, capsys):
        # No index is built on purpose: a usage error must still be 2.
        assert cli.main(["search", "   ", "--mode", mode]) == 2
        err = capsys.readouterr().err
        assert "empty" in err
        assert "Traceback" not in err

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_over_long_query_exit_2_without_echo(self, mode, capsys):
        canary = "SECRET-CLI-CANARY-" + "x" * 300
        assert cli.main(["search", canary, "--mode", mode]) == 2
        err = capsys.readouterr().err
        assert "256" in err
        assert canary not in err
        assert "SECRET-CLI-CANARY" not in err

    @pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
    @pytest.mark.parametrize("bad", ["0", "-3", "201"])
    def test_bad_limit_exit_2(self, mode, bad, capsys):
        assert cli.main(["search", "q", "--mode", mode, "--limit", bad]) == 2
        assert "limit" in capsys.readouterr().err

    def test_hybrid_term_cap_exit_2(self, capsys):
        query = " ".join(f"w{i}" for i in range(9))
        assert cli.main(["search", query, "--mode", "hybrid"]) == 2
        assert "8" in capsys.readouterr().err

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_stale_source_index_exits_1_sanitized(
        self, mode, tmp_path, monkeypatch, capsys
    ):
        seed_healthy(tmp_path, monkeypatch)
        recording = Recording.objects.get(sha256="cli-sem-0")
        TranscriptSegment.objects.filter(
            transcript__recording=recording, ordinal=0
        ).update(text="edited secret content")
        assert cli.main(["search", "alpha", "--mode", mode]) == 1
        captured = capsys.readouterr()
        assert "not healthy" in captured.err
        assert "edited secret content" not in captured.err
        assert "Traceback" not in captured.err
        assert captured.out == ""

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_no_active_generation_exits_1(self, mode, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)
        EmbeddingGeneration.objects.all().delete()
        assert cli.main(["search", "alpha", "--mode", mode]) == 1
        assert "no active embedding generation" in capsys.readouterr().err

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_endpoint_error_exits_1_sanitized(self, mode, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)
        canary = "SECRET-QUERY-CANARY-XYZ"

        def failing(config, texts):
            raise EmbeddingHTTPError(503)

        _patch_query_embedder(monkeypatch, failing)
        assert cli.main(["search", canary, "--mode", mode]) == 1
        captured = capsys.readouterr()
        assert "embedding request failed" in captured.err
        assert canary not in captured.err
        assert "SECRET-QUERY-CANARY" not in captured.err
        assert "Traceback" not in captured.err
        assert captured.out == ""

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_concurrent_change_exits_1(self, mode, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        calls = {"n": 0}
        real = sem._pragma_data_version

        def flaky(using):
            calls["n"] += 1
            if calls["n"] < 3:
                return real(using)
            return real(using) + 1

        monkeypatch.setattr(sem, "_pragma_data_version", flaky)
        assert cli.main(["search", "alpha", "--mode", mode]) == 1
        assert "changed during" in capsys.readouterr().err

    @pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
    def test_search_never_locks_or_recovers(self, mode, tmp_path, monkeypatch, capsys):
        # Import BEFORE patching so the patched stub is never bound into
        # the modules' own global names.
        from workflow.services import pipeline as pipeline_service
        from workflow.services import pipeline_lock as pipeline_lock_service

        if mode == "keyword":
            seed_keyword()
            query = "budget"
        else:
            seed_healthy(tmp_path, monkeypatch)
            _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
            query = "alpha"

        def forbidden(*args, **kwargs):
            raise AssertionError("search must not lock or recover")

        monkeypatch.setattr(pipeline_lock_service, "pipeline_lock", forbidden)
        monkeypatch.setattr(pipeline_service, "pipeline_lock", forbidden)
        monkeypatch.setattr(pipeline_service, "recover_interruptions", forbidden)
        capsys.readouterr()
        assert cli.main(["search", query, "--mode", mode]) == 0

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_search_never_writes(self, mode, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        capsys.readouterr()
        with CaptureQueriesContext(connection) as ctx:
            assert cli.main(["search", "alpha", "--mode", mode]) == 0
        writes = [
            query["sql"]
            for query in ctx.captured_queries
            if query["sql"].lstrip().upper().startswith(
                ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "REPLACE")
            )
        ]
        assert writes == []

    def test_semantic_never_rebuilds_or_repairs(self, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))

        def forbidden(*args, **kwargs):
            raise AssertionError("semantic search must stay read-only")

        monkeypatch.setattr(si, "rebuild_index", forbidden)
        monkeypatch.setattr(ei, "rebuild_embedding_index", forbidden)
        monkeypatch.setattr(ei, "repair_embedding_index", forbidden)
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", "semantic"]) == 0


# ---------------------------------------------------------------------------
# 5. Parser: mode choices/default, limit behavior, public validator
# ---------------------------------------------------------------------------


class TestParserAndValidator:
    def test_mode_choices_reject_unknown(self):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["search", "q", "--mode", "bogus"])
        assert excinfo.value.code == 2

    def test_mode_choices_accept_all_modes(self, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        for mode in ("keyword", "semantic", "hybrid"):
            capsys.readouterr()
            assert cli.main(["search", "alpha", "--mode", mode]) == 0
            assert capsys.readouterr().out

    def test_default_mode_is_keyword(self, capsys):
        seed_keyword()
        capsys.readouterr()
        assert cli.main(["search", "budget"]) == 0
        out = capsys.readouterr().out
        assert "[keyword]" not in out
        assert "[semantic]" not in out
        assert "[hybrid]" not in out

    def test_limit_bounds_across_modes(self, tmp_path, monkeypatch, capsys):
        seed_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        for mode in ("keyword", "semantic", "hybrid"):
            capsys.readouterr()
            assert cli.main(["search", "alpha", "--mode", mode, "--limit", "1"]) == 0
            assert "1 result(s)" in capsys.readouterr().out

    def test_validate_hybrid_query_is_public_and_sweep_free(self, monkeypatch):
        calls = []
        monkeypatch.setattr(si, "build_status_report", lambda **kwargs: calls.append(1))
        assert sf.validate_hybrid_query("alpha", 10) == "alpha"
        with pytest.raises(sf.HybridSearchInputError):
            sf.validate_hybrid_query("   ", 10)
        with pytest.raises(sf.HybridSearchInputError):
            sf.validate_hybrid_query(" ".join(f"w{i}" for i in range(9)), 10)
        assert calls == []
