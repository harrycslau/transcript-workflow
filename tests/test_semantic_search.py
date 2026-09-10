"""Service tests for the Step 5C scoped read-only semantic engine.

All network is mocked (fake embedder functions replace ``embed_texts``);
no real HTTP, no oMLX, no user data. Covers the full engine contract:
complete healthy ranking/dedup/provenance/snippets, the deterministic
``(recording_id, document_key)`` traversal with the best-last top-K
regression, scope filtering (out-of-scope rows never win while global
integrity defects still fail closed), exactly one source health sweep /
one integrity traversal / one embedding request (zero for an empty
scope), strict read-only behavior (no writes/locks/sync/rebuild, no HTTP
inside a transaction), every integrity failure class (missing/stale/
orphan/wrong-length/nonfinite/zero) failing closed, sanitized
dimension/endpoint/hostile-code/concurrency failures, bounded query
count/page and winner-only bounded excerpt behavior, and the reusable
``semantic_rank`` snapshot entry point that never re-triggers keyword
health.
"""

from __future__ import annotations

import struct

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from brainlib.config import EmbeddingConfig
from factories import make_config, make_summary_version, make_transcribed_recording
from workflow.models import (
    EmbeddingDocument,
    EmbeddingGeneration,
    EmbeddingGenerationState,
    Recording,
    TranscriptSegment,
)
from workflow.services import embedding_index as ei
from workflow.services import search_index as si
from workflow.services import semantic_query as sq
from workflow.services.embedding_client import (
    EmbeddingBatch,
    EmbeddingError,
    EmbeddingHTTPError,
)
from workflow.services.vector_codec import encode_vector

# transaction=True: the engine refuses to run while the caller is inside
# a SQLite transaction, and pytest-django's default outer transaction
# would otherwise trip that fixed precondition.
pytestmark = pytest.mark.django_db(transaction=True)

DIM = 4


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def emb_config(tmp_path, **overrides):
    return make_config(
        tmp_path,
        embedding=EmbeddingConfig(
            base_url="http://127.0.0.1:1/v1",
            model=overrides.pop("model", "test-embed-model"),
            api_key_env="BRAIN_TEST_LLM_API_KEY",
            timeout_seconds=120,
            batch_size=overrides.pop("batch_size", 32),
        ),
    )


def active_generation():
    return EmbeddingGeneration.objects.get(state=EmbeddingGenerationState.ACTIVE)


def build_healthy(tmp_path, *, model="test-embed-model"):
    """Two transcribed recordings with distinct content plus a healthy
    active embedding generation (dim 4, keyword-dispatch embedder)."""
    make_transcribed_recording(["alpha meeting discussion"], sha="sem-h-0")
    make_transcribed_recording(["beta irrelevant chatter"], sha="sem-h-1")
    si.rebuild_index()
    config = emb_config(tmp_path, model=model)
    ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha", "beta"]))
    return config


def _unmatched_vector(dim=DIM):
    """Default vector for documents no rule/keyword matches: near-zero on
    the query axis so unmatched documents score low, never the zero
    vector (which would fail closed)."""
    return [0.0] + [0.01] * (dim - 1)


def keyword_embedder(
    keywords,
    dim=DIM,
    *,
    tracker=None,
    fail_calls=(),
    dims_by_call=None,
    zero_texts=(),
    mutate_call=None,
    guard_no_txn=False,
):
    """Deterministic fake embedder: a text containing keyword ``k`` gets
    1.0 on axis ``idx(k)`` (with the near-zero fill elsewhere); the
    query text is dispatched identically. Never the zero vector unless
    requested via ``zero_texts``."""
    state = {"calls": 0}

    def embed(config, texts):
        state["calls"] += 1
        if tracker is not None:
            tracker.append(list(texts))
        if guard_no_txn:
            assert connection.in_atomic_block is False, "network inside a transaction"
        if state["calls"] in fail_calls:
            raise EmbeddingHTTPError(503)
        if mutate_call is not None and state["calls"] in mutate_call:
            mutate_call[state["calls"]]()
        call_dim = dim
        if dims_by_call and state["calls"] in dims_by_call:
            call_dim = dims_by_call[state["calls"]]
        out = []
        for text in texts:
            vec = _unmatched_vector(call_dim)
            for idx, keyword in enumerate(keywords):
                if keyword in text:
                    vec[idx] = 1.0
            if any(z in text for z in zero_texts):
                vec = [0.0] * call_dim
            out.append(EmbeddingBatch(text=text, embedding=tuple(vec)))
        return out

    embed.state = state
    return embed


def rule_embedder(
    rules,
    query_vector,
    dim=DIM,
    *,
    tracker=None,
    fail_calls=(),
    dims_by_call=None,
    zero_queries=False,
    mutate_call=None,
    guard_no_txn=False,
):
    """Deterministic fake embedder dispatching on CONTENT: document texts
    (which carry the ``brain-embedding-v1`` marker) match the first rule
    substring; the query text (never the marker) gets ``query_vector``."""
    state = {"calls": 0}

    def embed(config, texts):
        state["calls"] += 1
        if tracker is not None:
            tracker.append(list(texts))
        if guard_no_txn:
            assert connection.in_atomic_block is False, "network inside a transaction"
        if state["calls"] in fail_calls:
            raise EmbeddingHTTPError(503)
        if mutate_call is not None and state["calls"] in mutate_call:
            mutate_call[state["calls"]]()
        call_dim = dim
        if dims_by_call and state["calls"] in dims_by_call:
            call_dim = dims_by_call[state["calls"]]
        out = []
        for text in texts:
            if text.startswith("brain-embedding-v1\n"):
                vec = _unmatched_vector(call_dim)
                for substring, vector in rules:
                    if substring in text:
                        vec = list(vector)
                        break
            else:
                if zero_queries:
                    vec = [0.0] * call_dim
                else:
                    vec = list(query_vector)
            out.append(EmbeddingBatch(text=text, embedding=tuple(vec)))
        return out

    embed.state = state
    return embed


# ---------------------------------------------------------------------------
# 1. Complete healthy semantic search: ranking, dedup, provenance, snippets
# ---------------------------------------------------------------------------


class TestHealthySearch:
    def test_ranks_dedups_and_provenance(self, tmp_path):
        config = build_healthy(tmp_path)
        payload = sq.semantic_search(
            "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
        )
        assert payload["mode"] == "semantic"
        assert payload["query"] == "alpha"
        assert payload["semantic_query_version"] == sq.SEMANTIC_QUERY_VERSION
        assert payload["truncated"] is False
        assert payload["result_count"] == 2  # one winner per Recording, never more
        assert payload["more_recordings_matched"] == 0

        first, second = payload["results"]
        # r0 (alpha) ranks before r1 (beta); per-Recording dedup: exactly
        # one result each even though r0 has a segment + a metadata doc.
        assert first["rank"] == 1
        assert second["rank"] == 2
        assert first["score"] > second["score"]
        assert first["recording_id"] != second["recording_id"]

        # Winner provenance: the alpha segment is r0's best document.
        alpha_rec = Recording.objects.get(sha256="sem-h-0")
        assert first["recording_id"] == alpha_rec.pk
        assert first["match"]["source"] == "segment"
        assert first["match"]["transcript_id"] is not None
        assert first["match"]["segment_ordinal"] == 0
        assert first["match"]["start_ms"] == 0
        assert first["match"]["end_ms"] == 1000
        assert first["match"]["document_key"] == (
            f"segment:{first['match']['transcript_id']}:0"
        )
        assert first["score"] == pytest.approx(1.0, abs=1e-3)

        # Snippet: winner-only bounded plain text from the segment body.
        snippet = first["snippet"]
        assert snippet is not None
        assert snippet["field"] == "body_text"
        assert "alpha meeting discussion" in snippet["text"]
        assert len(snippet["text"]) <= 321
        assert "matches" not in snippet  # no highlight marks for semantic

    def test_payload_contract(self, tmp_path):
        config = build_healthy(tmp_path)
        payload = sq.semantic_search(
            "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
        )
        assert set(payload) == {
            "query",
            "mode",
            "semantic_query_version",
            "index_version",
            "embedding_generation",
            "limit",
            "results",
            "result_count",
            "truncated",
            "more_recordings_matched",
        }
        gen = active_generation()
        assert payload["embedding_generation"] == {
            "id": gen.pk,
            "model": "test-embed-model",
            "dimensions": DIM,
            "embedding_version": ei.EMBEDDING_VERSION,
            "source_index_version": si.INDEX_VERSION,
        }
        assert payload["index_version"] == si.INDEX_VERSION
        assert payload["limit"] == sq.SEMANTIC_DEFAULT_RESULT_LIMIT

    def test_more_recordings_matched_exact_when_capped(self, tmp_path):
        build_healthy(tmp_path)
        for i in range(3):
            make_transcribed_recording([f"extra gamma {i}"], sha=f"sem-cap-{i}")
        si.rebuild_index()
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(
            config, embedder=keyword_embedder(["alpha", "beta", "gamma"])
        )
        payload = sq.semantic_search(
            "alpha",
            limit=2,
            config=config,
            embedder=keyword_embedder(["alpha", "beta", "gamma"]),
        )
        assert len(payload["results"]) == 2
        assert payload["result_count"] == 2
        # 5 recordings matched; the trailing 3 are exact, never guessed.
        assert payload["more_recordings_matched"] == 3
        assert payload["truncated"] is False

    def test_summary_snippet_policy_body_then_title(self, tmp_path):
        rec, transcript, section = make_transcribed_recording(
            ["plain segment"], sha="sem-sum-0"
        )
        summary = make_summary_version(
            rec,
            transcript,
            section,
            title="alpha title text",
            overview="",
            key_points=[],
            action_items=[],
            people=[],
            topics=[],
            organizations=[],
        )
        si.rebuild_index()
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(
            config, embedder=keyword_embedder(["alpha", "plain"])
        )
        payload = sq.semantic_search(
            "alpha", config=config, embedder=keyword_embedder(["alpha", "plain"])
        )
        assert len(payload["results"]) == 1
        winner = payload["results"][0]
        assert winner["match"]["source"] == "summary"
        # The real Summary PK (a canonical UUID string) is carried directly
        # through the pure match into the shared _provenance shape.
        assert winner["match"]["summary_id"] == summary.pk
        assert winner["match"]["transcript_id"] == transcript.pk
        # Summary policy: body (empty) then title then aux.
        assert winner["snippet"]["field"] == "title_text"
        assert winner["snippet"]["text"] == "alpha title text"
        assert winner["title"]  # canonical metadata title is populated


# ---------------------------------------------------------------------------
# 2. Deterministic (recording_id, document_key) traversal + best-last top-K
# ---------------------------------------------------------------------------


class TestTraversalOrder:
    def test_best_document_last_determines_top_k_membership(self, tmp_path):
        # r1's best document (its summary, sorted LAST inside the r1
        # group) must decide the K=1 membership; a global document_key
        # order would interleave r2's segment BEFORE r1's summary and
        # either produce the wrong winner or a candidate-order failure.
        rec1, transcript1, section1 = make_transcribed_recording(
            ["r1 beta segment text"], sha="sem-reg-1"
        )
        make_summary_version(
            rec1, transcript1, section1, title="r1 alpha title", overview="r1 alpha summary text"
        )
        make_transcribed_recording(["r2 alpha segment text"], sha="sem-reg-2")
        si.rebuild_index()
        config = emb_config(tmp_path)
        rules = [
            ("r1 beta", (0.2, 0.98, 0.0, 0.0)),
            ("r1 alpha", (0.99, 0.141, 0.0, 0.0)),
            ("r2 alpha", (0.9, 0.436, 0.0, 0.0)),
        ]
        ei.rebuild_embedding_index(
            config, embedder=rule_embedder(rules, query_vector=(1.0, 0.0, 0.0, 0.0))
        )
        payload = sq.semantic_search(
            "alpha",
            limit=1,
            config=config,
            embedder=rule_embedder(rules, query_vector=(1.0, 0.0, 0.0, 0.0)),
        )
        assert len(payload["results"]) == 1
        winner = payload["results"][0]
        assert winner["recording_id"] == rec1.pk
        assert winner["match"]["source"] == "summary"
        assert winner["match"]["document_key"].startswith("summary:")
        assert winner["score"] == pytest.approx(0.99, abs=1e-3)
        assert payload["more_recordings_matched"] == 1

    def test_scores_descending_and_one_winner_per_recording(self, tmp_path):
        build_healthy(tmp_path)
        for i in range(3):
            make_transcribed_recording([f"delta {i}"], sha=f"sem-ord-{i}")
        si.rebuild_index()
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(
            config, embedder=keyword_embedder(["alpha", "beta", "delta"])
        )
        payload = sq.semantic_search(
            "alpha", config=config, embedder=keyword_embedder(["alpha", "beta", "delta"])
        )
        scores = [r["score"] for r in payload["results"]]
        assert scores == sorted(scores, reverse=True)
        ids = [r["recording_id"] for r in payload["results"]]
        assert len(ids) == len(set(ids))  # per-Recording dedup


# ---------------------------------------------------------------------------
# 3. Scope filters scoring; global integrity still fails closed
# ---------------------------------------------------------------------------


class TestScope:
    def test_out_of_scope_rows_cannot_win(self, tmp_path):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)
        alpha = Recording.objects.get(sha256="sem-h-0")
        scope = Recording.objects.filter(pk=alpha.pk)
        payload = sq.semantic_search(
            "alpha",
            config=config,
            scope=scope,
            embedder=keyword_embedder(["alpha", "beta"]),
        )
        assert len(payload["results"]) == 1
        assert payload["results"][0]["recording_id"] == alpha.pk
        assert payload["more_recordings_matched"] == 0
        # the out-of-scope recording's rows never appeared
        beta = Recording.objects.get(sha256="sem-h-1")
        assert beta.pk not in [r["recording_id"] for r in payload["results"]]

    def test_global_integrity_defect_out_of_scope_fails_closed(self, tmp_path):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)
        alpha = Recording.objects.get(sha256="sem-h-0")
        beta = Recording.objects.get(sha256="sem-h-1")
        scope = Recording.objects.filter(pk=alpha.pk)
        # Corrupt the OUT-OF-SCOPE recording's source (stale vector). The
        # semantic contract requires a globally healthy embedding index:
        # the defect must still fail the search closed.
        TranscriptSegment.objects.filter(transcript__recording=beta, ordinal=0).update(
            text="beta irrelevant chatter EDITED"
        )
        si.rebuild_index()
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.semantic_search(
                "alpha",
                config=config,
                scope=scope,
                embedder=keyword_embedder(["alpha", "beta"]),
            )
        assert excinfo.value.code == sq.SEMANTIC_INDEX_INTEGRITY
        assert "EDITED" not in str(excinfo.value)

    def test_scope_must_be_unsliced_recording_queryset(self, tmp_path):
        config = build_healthy(tmp_path)
        from workflow.models import SearchDocument

        with pytest.raises(Exception, match="unsliced queryset of Recordings"):
            sq.semantic_search(
                "alpha",
                config=config,
                scope=SearchDocument.objects.all(),
                embedder=keyword_embedder(["alpha", "beta"]),
            )
        with pytest.raises(Exception, match="unsliced queryset of Recordings"):
            sq.semantic_search(
                "alpha",
                config=config,
                scope=Recording.objects.all()[:1],
                embedder=keyword_embedder(["alpha", "beta"]),
            )


# ---------------------------------------------------------------------------
# 4. Exactly one health sweep / one traversal / one embedding request
# ---------------------------------------------------------------------------


class TestSingleSweep:
    def test_one_health_sweep_one_traversal_one_embedding_request(
        self, tmp_path, monkeypatch
    ):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)
        calls = {"health": 0, "decoded_rows": 0}
        real_status = si.build_status_report

        def spy_status(*args, **kwargs):
            calls["health"] += 1
            return real_status(*args, **kwargs)

        monkeypatch.setattr(si, "build_status_report", spy_status)
        real_decode = ei._classify_active_page

        def spy_decode(page, dimensions, using):
            calls["decoded_rows"] += len(page)
            return real_decode(page, dimensions, using)

        monkeypatch.setattr(sq, "_classify_active_page", spy_decode)
        tracker = []
        sq.semantic_search(
            "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"], tracker=tracker)
        )
        assert calls["health"] == 1  # EXACTLY one source health sweep
        assert len(tracker) == 1  # EXACTLY one embedding request
        assert tracker[0] == ["alpha"]  # one payload: the prepared query text
        gen = active_generation()
        active_rows = EmbeddingDocument.objects.filter(generation=gen).count()
        assert active_rows > 0
        # ONE integrity traversal: every active vector decoded exactly
        # once (never a second blob pass).
        assert calls["decoded_rows"] == active_rows

    def test_empty_scope_zero_embedding_requests_but_integrity_validated(
        self, tmp_path, monkeypatch
    ):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)
        tracker = []
        payload = sq.semantic_search(
            "alpha",
            config=config,
            scope=Recording.objects.none(),
            embedder=keyword_embedder(["alpha", "beta"], tracker=tracker),
        )
        assert tracker == []  # zero embedding requests for an empty scope
        assert payload["results"] == []
        assert payload["result_count"] == 0
        assert payload["more_recordings_matched"] == 0
        assert payload["truncated"] is False
        # global integrity is STILL validated once: a corrupted vector
        # anywhere fails the empty-scope search closed.
        gen = active_generation()
        victim = EmbeddingDocument.objects.filter(generation=gen).first()
        EmbeddingDocument.objects.filter(pk=victim.pk).update(vector_blob=b"\x00\x00")
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.semantic_search(
                "alpha",
                config=config,
                scope=Recording.objects.none(),
                embedder=keyword_embedder(["alpha", "beta"], tracker=tracker),
            )
        assert excinfo.value.code == sq.SEMANTIC_INDEX_INTEGRITY

    def test_semantic_rank_never_triggers_keyword_health(self, tmp_path, monkeypatch):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)
        calls = {"health": 0}
        real_status = si.build_status_report

        def spy_status(*args, **kwargs):
            calls["health"] += 1
            return real_status(*args, **kwargs)

        monkeypatch.setattr(si, "build_status_report", spy_status)
        payload = sq.semantic_rank(
            "alpha", (1.0, 0.0, 0.0, 0.0), config=config
        )
        assert calls["health"] == 0  # reusable entry point never re-sweeps
        assert payload["results"]
        assert payload["embedding_generation"]["dimensions"] == DIM

    def test_default_embedder_resolves_at_call_time(self, tmp_path, monkeypatch):
        # With embedder=None the production seam (embedding_client.
        # embed_texts) is resolved at call time, so tests/CLI can patch it
        # consistently with the embedding-index commands.
        build_healthy(tmp_path)
        config = emb_config(tmp_path)
        calls = {"n": 0}

        def fake(config, texts):
            calls["n"] += 1
            return [
                EmbeddingBatch(text=text, embedding=(1.0, 0.0, 0.0, 0.0))
                for text in texts
            ]

        monkeypatch.setattr("workflow.services.embedding_client.embed_texts", fake)
        payload = sq.semantic_search("alpha", config=config, embedder=None)
        assert calls["n"] == 1  # resolved production seam called exactly once
        assert payload["results"]

    def test_explicit_embedder_injection_still_exactly_once(self, tmp_path):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)
        tracker = []
        embedder = keyword_embedder(["alpha", "beta"], tracker=tracker)
        payload = sq.semantic_search("alpha", config=config, embedder=embedder)
        assert len(tracker) == 1
        assert payload["results"]


# ---------------------------------------------------------------------------
# 4b. semantic_rank input contract (already-normalized exact-str query)
# ---------------------------------------------------------------------------


class TestSemanticRankInput:
    def test_requires_exact_normalized_query(self, tmp_path):
        config = build_healthy(tmp_path)
        # Over-cap / empty / whitespace / non-NFC / non-str are all the
        # same fixed sanitized input error, raised before any DB work.
        for bad in ("", "   ", "  alpha", "alpha  ", "a\u0308", "x" * 257, None, 5, b"alpha"):
            with pytest.raises(sq.SemanticQueryInputError) as excinfo:
                sq.semantic_rank(bad, (1.0, 0.0, 0.0, 0.0), config=config)
            assert excinfo.value.code == sq.INVALID_QUERY

    def test_valid_normalized_query_echoed_unchanged(self, tmp_path):
        config = build_healthy(tmp_path)
        payload = sq.semantic_rank("alpha", (1.0, 0.0, 0.0, 0.0), config=config)
        assert payload["query"] == "alpha"
        assert payload["results"]

    def test_rejects_before_any_health_work(self, tmp_path, monkeypatch):
        config = build_healthy(tmp_path)
        calls = {"health": 0}
        real_status = si.build_status_report

        def spy_status(*args, **kwargs):
            calls["health"] += 1
            return real_status(*args, **kwargs)

        monkeypatch.setattr(si, "build_status_report", spy_status)
        with pytest.raises(sq.SemanticQueryInputError):
            sq.semantic_rank("  bad query  ", (1.0, 0.0, 0.0, 0.0), config=config)
        assert calls["health"] == 0  # input rejected before any sweep

    def test_input_canary_never_echoed(self, tmp_path):
        config = build_healthy(tmp_path)
        canary = "SECRET-RANK-CANARY-" + "x" * 300  # over-cap
        with pytest.raises(sq.SemanticQueryInputError) as excinfo:
            sq.semantic_rank(canary, (1.0, 0.0, 0.0, 0.0), config=config)
        message = str(excinfo.value)
        assert canary not in message
        assert "SECRET-RANK-CANARY" not in message


# ---------------------------------------------------------------------------
# 5. Read-only purity: no writes/locks/sync/rebuild, no HTTP in a txn
# ---------------------------------------------------------------------------


class TestPurity:
    def test_read_only_no_writes_no_lock_no_sync_no_rebuild(self, tmp_path, monkeypatch):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)

        def forbidden(*args, **kwargs):
            raise AssertionError("semantic search must stay read-only")

        monkeypatch.setattr("workflow.services.pipeline_lock.pipeline_lock", forbidden)
        monkeypatch.setattr(
            "workflow.services.search_sync.schedule_recording_sync", forbidden
        )
        monkeypatch.setattr("workflow.services.search_index.rebuild_index", forbidden)
        monkeypatch.setattr(
            "workflow.services.embedding_index.rebuild_embedding_index", forbidden
        )
        monkeypatch.setattr("workflow.services.embedding_index.repair_embedding_index", forbidden)
        with CaptureQueriesContext(connection) as ctx:
            sq.semantic_search(
                "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
            )
        writes = [
            q["sql"]
            for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith(
                ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "REPLACE")
            )
        ]
        assert writes == []

    def test_traversal_never_loads_searchable_text(self, tmp_path):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)
        with CaptureQueriesContext(connection) as ctx:
            sq.semantic_search(
                "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
            )
        # The candidate-traversal page SELECTs carry an exact .only(...)
        # projection: the unbounded title/body/aux TextFields are never
        # selected during the traversal (the winner excerpt is the ONLY
        # bounded text fetch, via SUBSTR windows). The traversal pages are
        # uniquely identified by the computed ``in_scope`` column.
        page_queries = [
            q["sql"]
            for q in ctx.captured_queries
            if "AS \"in_scope\"" in q["sql"] and q["sql"].lstrip().upper().startswith("SELECT")
        ]
        assert page_queries  # the traversal page SELECT actually ran
        for sql in page_queries:
            assert "title_text" not in sql
            assert "body_text" not in sql
            assert "aux_text" not in sql
        # The in-scope existence check stays existence-only (no columns).
        assert any(
            'SELECT 1 AS "a" FROM "workflow_search_document" LIMIT 1'
            in q["sql"]
            for q in ctx.captured_queries
        )

    def test_rejects_caller_transaction_zero_embedder_calls(self, tmp_path):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)

        def forbidden(config, texts):
            raise AssertionError("no embedder call inside a caller transaction")

        with transaction.atomic():
            with pytest.raises(sq.SemanticQueryError) as excinfo:
                sq.semantic_search("alpha", config=config, embedder=forbidden)
        assert excinfo.value.code == sq.SEMANTIC_IN_TRANSACTION


# ---------------------------------------------------------------------------
# 6. Integrity failure classes all fail closed with no partial results
# ---------------------------------------------------------------------------


class TestIntegrity:
    def _search(self, config, **kwargs):
        return sq.semantic_search(
            "alpha",
            config=config,
            embedder=keyword_embedder(["alpha", "beta"]),
            **kwargs,
        )

    def test_missing_document(self, tmp_path):
        config = build_healthy(tmp_path)
        make_transcribed_recording(["brand new recording"], sha="sem-miss-0")
        si.rebuild_index()  # new SearchDocuments, no active vectors
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            self._search(config)
        assert excinfo.value.code == sq.SEMANTIC_INDEX_INTEGRITY
        assert "missing" in str(excinfo.value)

    def test_stale_content(self, tmp_path):
        config = build_healthy(tmp_path)
        rec = Recording.objects.get(sha256="sem-h-0")
        TranscriptSegment.objects.filter(transcript__recording=rec, ordinal=0).update(
            text="alpha meeting discussion EDITED"
        )
        si.rebuild_index()
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            self._search(config)
        assert excinfo.value.code == sq.SEMANTIC_INDEX_INTEGRITY
        assert "EDITED" not in str(excinfo.value)

    def test_orphan_document(self, tmp_path):
        config = build_healthy(tmp_path)
        gen = active_generation()
        EmbeddingDocument.objects.create(
            generation=gen,
            document_key="segment:ghost:0",
            source_content_hash="a" * 64,
            vector_blob=encode_vector([0.1] * gen.dimensions, dimensions=gen.dimensions),
        )
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            self._search(config)
        assert excinfo.value.code == sq.SEMANTIC_INDEX_INTEGRITY
        assert "orphan" in str(excinfo.value)

    def test_invalid_vector_wrong_length(self, tmp_path):
        config = build_healthy(tmp_path)
        gen = active_generation()
        victim = EmbeddingDocument.objects.filter(generation=gen).first()
        EmbeddingDocument.objects.filter(pk=victim.pk).update(vector_blob=b"\x00\x00")
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            self._search(config)
        assert excinfo.value.code == sq.SEMANTIC_INDEX_INTEGRITY
        assert "invalid vector" in str(excinfo.value)

    def test_invalid_vector_nonfinite(self, tmp_path):
        config = build_healthy(tmp_path)
        gen = active_generation()
        victim = EmbeddingDocument.objects.filter(generation=gen).first()
        bad = struct.pack(
            f"<{gen.dimensions}f", *([1.0] * (gen.dimensions - 1) + [float("nan")])
        )
        EmbeddingDocument.objects.filter(pk=victim.pk).update(vector_blob=bad)
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            self._search(config)
        assert excinfo.value.code == sq.SEMANTIC_INDEX_INTEGRITY

    def test_invalid_vector_zero_norm(self, tmp_path):
        config = build_healthy(tmp_path)
        gen = active_generation()
        victim = EmbeddingDocument.objects.filter(generation=gen).first()
        EmbeddingDocument.objects.filter(pk=victim.pk).update(
            vector_blob=encode_vector([0.0] * gen.dimensions, dimensions=gen.dimensions)
        )
        # A zero stored document vector is the pure-layer
        # invalid_document_vector failure (never skipped, never scored).
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            self._search(config)
        assert excinfo.value.code == sq.INVALID_DOCUMENT_VECTOR

    def test_oversized_blob_classified_via_length(self, tmp_path):
        config = build_healthy(tmp_path)
        gen = active_generation()
        victim = EmbeddingDocument.objects.filter(generation=gen).first()
        EmbeddingDocument.objects.filter(pk=victim.pk).update(
            vector_blob=b"\x00" * (1024 * 1024)
        )
        with CaptureQueriesContext(connection) as ctx:
            with pytest.raises(sq.SemanticQueryError) as excinfo:
                self._search(config)
        assert excinfo.value.code == sq.SEMANTIC_INDEX_INTEGRITY
        # length-first: SQLite length() classifies the oversized blob.
        assert any("length(vector_blob)" in q["sql"] for q in ctx.captured_queries)
        assert "invalid vector" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 7. Sanitized dimension / endpoint / hostile-code / concurrency failures
# ---------------------------------------------------------------------------


class TestFailures:
    def test_dimension_mismatch(self, tmp_path):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.semantic_search(
                "alpha",
                config=config,
                embedder=keyword_embedder(["alpha", "beta"], dims_by_call={1: DIM + 1}),
            )
        assert excinfo.value.code == sq.DIMENSION_MISMATCH

    def test_endpoint_error_sanitized(self, tmp_path):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.semantic_search(
                "alpha",
                config=config,
                embedder=keyword_embedder(["alpha", "beta"], fail_calls=(1,)),
            )
        assert excinfo.value.code == sq.SEMANTIC_EMBEDDING_FAILED
        assert "(http_error)" in str(excinfo.value)
        assert "alpha" not in str(excinfo.value)

    def test_hostile_embedding_code_allowlisted(self, tmp_path):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)

        class HostileError(EmbeddingError):
            code = "CANARY-HOSTILE-EMBEDDING-CODE"

        def hostile(config, texts):
            raise HostileError()

        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.semantic_search("alpha", config=config, embedder=hostile)
        message = str(excinfo.value)
        assert "CANARY-HOSTILE-EMBEDDING-CODE" not in message
        assert "(embedding_error)" in message  # fixed generic category

    def test_zero_query_vector(self, tmp_path):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.semantic_search(
                "alpha",
                config=config,
                embedder=keyword_embedder(["alpha", "beta"], zero_texts=("alpha",)),
            )
        assert excinfo.value.code == sq.INVALID_QUERY_VECTOR

    def test_concurrent_data_version_change(self, tmp_path, monkeypatch):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)
        calls = {"n": 0}
        real = sq._pragma_data_version

        def flaky(using):
            calls["n"] += 1
            if calls["n"] < 3:
                return real(using)
            return real(using) + 1  # another connection committed at the end

        monkeypatch.setattr(sq, "_pragma_data_version", flaky)
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.semantic_search(
                "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
            )
        assert excinfo.value.code == sq.SEMANTIC_CONCURRENT_CHANGE
        assert calls["n"] >= 3

    def test_concurrent_active_promotion(self, tmp_path):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)
        old = active_generation()

        def promote_while_query_embeds():
            # A concurrent writer promotes a NEW active generation while
            # the query embedding request is in flight.
            now = timezone.now()
            new = EmbeddingGeneration.objects.create(
                model="test-embed-model",
                dimensions=DIM,
                embedding_version=ei.EMBEDDING_VERSION,
                source_index_version=si.INDEX_VERSION,
            )
            EmbeddingGeneration.objects.filter(pk=old.pk).update(
                state=EmbeddingGenerationState.SUPERSEDED, superseded_at=now
            )
            EmbeddingGeneration.objects.filter(pk=new.pk).update(
                state=EmbeddingGenerationState.ACTIVE,
                completed_at=now,
                activated_at=now,
            )

        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.semantic_search(
                "alpha",
                config=config,
                embedder=keyword_embedder(
                    ["alpha", "beta"], mutate_call={1: promote_while_query_embeds}
                ),
            )
        assert excinfo.value.code == sq.SEMANTIC_CONCURRENT_CHANGE
        # the concurrent promotion really happened and superseded the old
        # active; the search never reported results against it.
        old.refresh_from_db()
        assert old.state == EmbeddingGenerationState.SUPERSEDED

    def test_source_index_unhealthy(self, tmp_path):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_search_fts")
        try:
            with pytest.raises(sq.SemanticQueryError) as excinfo:
                sq.semantic_search(
                    "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
                )
            assert excinfo.value.code == sq.SEMANTIC_SOURCE_UNHEALTHY
        finally:
            si.rebuild_index()

    def test_model_not_configured(self, tmp_path):
        build_healthy(tmp_path)
        config = emb_config(tmp_path, model="")
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.semantic_search(
                "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
            )
        assert excinfo.value.code == sq.SEMANTIC_MODEL_NOT_CONFIGURED

    def test_no_active_generation(self, tmp_path):
        config = build_healthy(tmp_path)
        EmbeddingGeneration.objects.all().delete()
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.semantic_search(
                "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
            )
        assert excinfo.value.code == sq.SEMANTIC_NO_ACTIVE_GENERATION

    def test_incompatible_generation(self, tmp_path):
        config = build_healthy(tmp_path)
        EmbeddingGeneration.objects.filter(state=EmbeddingGenerationState.ACTIVE).update(
            model="different-model"
        )
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.semantic_search(
                "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
            )
        assert excinfo.value.code == sq.SEMANTIC_INCOMPATIBLE_GENERATION

    def test_unexpected_failure_sanitized(self, tmp_path, monkeypatch):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError("CANARY-SEMANTIC-UNEXPECTED-SECRET")

        monkeypatch.setattr(sq, "_pragma_data_version", boom)
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.semantic_search(
                "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
            )
        assert excinfo.value.code == sq.SEMANTIC_UNEXPECTED
        assert "CANARY-SEMANTIC-UNEXPECTED-SECRET" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# 8. Bounded query/page behavior, length-first blobs, winner-only excerpts
# ---------------------------------------------------------------------------


class TestBoundedness:
    def test_query_count_scales_with_pages(self, tmp_path, monkeypatch):
        build_healthy(tmp_path)
        for i in range(13):
            make_transcribed_recording([f"pad {i}"], sha=f"sem-page-{i}")
        si.rebuild_index()
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(
            config, embedder=keyword_embedder(["alpha", "beta", "pad"])
        )
        monkeypatch.setattr(sq, "SEMANTIC_PAGE_SIZE", 3)
        with CaptureQueriesContext(connection) as ctx:
            sq.semantic_rank("alpha", (1.0, 0.0, 0.0, 0.0), config=config)
        small = len(ctx.captured_queries)
        # Per page the traversal issues a fixed small number of queries
        # (page select, active lookup, length classification, blob
        # decode) — never a per-row query pattern.
        assert small < 60
        monkeypatch.setattr(sq, "SEMANTIC_PAGE_SIZE", 500)  # default page size
        with CaptureQueriesContext(connection) as ctx:
            sq.semantic_rank("alpha", (1.0, 0.0, 0.0, 0.0), config=config)
        large = len(ctx.captured_queries)
        assert small > large  # fewer pages -> fewer queries

    def test_winner_only_bounded_excerpt_fetch(self, tmp_path):
        long_text = "alpha " + "x" * 400
        make_transcribed_recording([long_text], sha="sem-long-0")
        si.rebuild_index()
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(
            config, embedder=keyword_embedder(["alpha", "x"])
        )
        with CaptureQueriesContext(connection) as ctx:
            payload = sq.semantic_search(
                "alpha", config=config, embedder=keyword_embedder(["alpha", "x"])
            )
        snippet = payload["results"][0]["snippet"]
        assert snippet["field"] == "body_text"
        assert snippet["text"].endswith("\u2026")
        assert len(snippet["text"]) <= 321
        assert "alpha " in snippet["text"]
        # the excerpt is a bounded SUBSTR SELECT, never a full-field load
        assert any("SUBSTR(body_text" in q["sql"] for q in ctx.captured_queries)

    def test_errors_never_echo_content_or_keys(self, tmp_path):
        canary = "SUPER-SECRET-SEMANTIC-CANARY"
        make_transcribed_recording([canary + " alpha"], sha="sem-can-0")
        si.rebuild_index()
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(
            config, embedder=keyword_embedder(["alpha"])
        )
        gen = active_generation()
        victim = EmbeddingDocument.objects.filter(generation=gen).first()
        EmbeddingDocument.objects.filter(pk=victim.pk).update(vector_blob=b"\x00\x00")
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.semantic_search("alpha", config=config, embedder=keyword_embedder(["alpha"]))
        message = str(excinfo.value)
        assert canary not in message
        assert "Traceback" not in message
        assert victim.document_key not in message