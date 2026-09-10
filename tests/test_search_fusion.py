"""Hybrid keyword/semantic fusion tests (Step 5C — Task 4).

Proves: the PURE reciprocal-rank-fusion helper (math, deterministic
order, ties, presence, absence = returned-depth semantics), the shared
one-sweep orchestration (EXACTLY one source health sweep, one integrity
traversal, one query embedding — zero for an empty scope; keyword runs
``search_recordings`` directly and NEVER ``preflight_full_health``),
same-scope-for-both-components with out-of-scope rows never winning,
the fixed depth-200 component contract with a varying final limit,
keyword-first presentation (highlights preserved) with the evidence
block, the completeness/truncation/null-more truth table, fail-closed
failure/concurrency/zero-vector behavior with NO keyword-only fallback,
and strict read-only purity (no writes/locks/sync/rebuild/repair, no
HTTP inside a transaction, sanitized privacy).

All network is mocked (fake embedder functions replace ``embed_texts``);
no real HTTP, no oMLX, no user data.
"""

from __future__ import annotations

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from brainlib.config import EmbeddingConfig
from factories import make_config, make_transcribed_recording
from workflow.models import (
    EmbeddingDocument,
    EmbeddingGeneration,
    EmbeddingGenerationState,
    Recording,
)
from workflow.services import embedding_index as ei
from workflow.services import search_fusion as sf
from workflow.services import search_index as si
from workflow.services import search_query as kw
from workflow.services import semantic_query as sq
from workflow.services.embedding_client import (
    EmbeddingBatch,
    EmbeddingError,
    EmbeddingHTTPError,
)
from workflow.services.vector_codec import encode_vector

# transaction=True: the hybrid engine refuses to run while the caller is
# inside a SQLite transaction, and pytest-django's default outer
# transaction would otherwise trip that fixed precondition.
pytestmark = pytest.mark.django_db(transaction=True)

DIM = 4


# ---------------------------------------------------------------------------
# helpers (mirror the semantic engine's test setup)
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


# ---------------------------------------------------------------------------
# Crafted component payload helpers (for pure assembly-level tests)
# ---------------------------------------------------------------------------


def _kw_result(rid, rank, title="kw title", source="segment", snippet=None):
    return {
        "rank": rank,
        "recording_id": rid,
        "title": title,
        "match": {"source": source, "document_key": f"{rid}:kw"},
        "snippet": snippet
        if snippet is not None
        else {"field": "body_text", "text": "kw snippet", "matches": [{"start": 0, "end": 2}]},
    }


def _sem_result(rid, rank, score=0.5, title="sem title"):
    return {
        "rank": rank,
        "recording_id": rid,
        "title": title,
        "match": {"source": "segment", "document_key": f"{rid}:sem"},
        "snippet": {"field": "body_text", "text": "sem snippet"},
        "score": score,
    }


def _kw_payload(results, *, truncated=False, more=0):
    return {
        "query": "alpha",
        "result_count": len(results),
        "results": results,
        "truncated": truncated,
        "more_recordings_matched": more,
    }


def _sem_payload(results, *, truncated=False, more=0):
    return {
        "query": "alpha",
        "result_count": len(results),
        "results": results,
        "truncated": truncated,
        "more_recordings_matched": more,
    }


def _patch_components(monkeypatch, kw_payload, sem_payload):
    monkeypatch.setattr(sf, "search_recordings", lambda *a, **k: kw_payload)
    monkeypatch.setattr(sf, "run_semantic_snapshot", lambda *a, **k: sem_payload)


# ---------------------------------------------------------------------------
# 1. Pure RRF math / order / ties / presence / depth semantics
# ---------------------------------------------------------------------------


class TestPureRRF:
    def test_rrf_score_math(self):
        assert sf.rrf_score(1, None) == pytest.approx(1 / 61)
        assert sf.rrf_score(None, 1) == pytest.approx(1 / 61)
        assert sf.rrf_score(1, 1) == pytest.approx(2 / 61)
        assert sf.rrf_score(2, None) == pytest.approx(1 / 62)
        assert sf.rrf_score(60, 60) == pytest.approx(2 / 120)
        assert sf.rrf_score(None, None) == 0.0
        # k is a parameter, the production constant is 60
        assert sf.rrf_score(1, None, k=10) == pytest.approx(1 / 11)

    def test_depth_constant_is_keyword_max(self):
        assert sf.HYBRID_DEPTH == 200
        assert sf.HYBRID_DEPTH == kw.MAX_RESULT_LIMIT
        assert sf.RRF_K == 60

    def test_rrf_order_desc(self):
        kw_results = [
            {"recording_id": "r1", "rank": 1},
            {"recording_id": "r2", "rank": 2},
            {"recording_id": "r3", "rank": 3},
        ]
        sem_results = [
            {"recording_id": "r1", "rank": 1},
            {"recording_id": "r3", "rank": 2},
        ]
        fused = sf.reciprocal_rank_fusion(kw_results, sem_results)
        assert [f.recording_id for f in fused] == ["r1", "r3", "r2"]
        assert fused[0].rrf_score > fused[1].rrf_score > fused[2].rrf_score
        # r1 = 2/61 ; r3 = 1/63+1/62 ; r2 = 1/62
        assert fused[0].rrf_score == pytest.approx(2 / 61)
        assert fused[1].rrf_score == pytest.approx(1 / 63 + 1 / 62)
        assert fused[2].rrf_score == pytest.approx(1 / 62)

    def test_ties_break_by_recording_id(self):
        # (k1,s100) and (k100,s1): identical score/presence/min/max —
        # fsum is correctly rounded and order-independent, so the tie is
        # exact; the canonical recording id decides.
        kw_results = [
            {"recording_id": "b", "rank": 1},
            {"recording_id": "a", "rank": 100},
        ]
        sem_results = [
            {"recording_id": "b", "rank": 100},
            {"recording_id": "a", "rank": 1},
        ]
        fused = sf.reciprocal_rank_fusion(kw_results, sem_results)
        assert fused[0].rrf_score == fused[1].rrf_score
        assert [f.recording_id for f in fused] == ["a", "b"]

    def test_fusion_key_ordering_rrf_presence_min_max(self):
        # Direct proof of the deterministic key tuple: RRF desc, presence
        # desc, minimum present rank, maximum present rank, recording id.
        key = sf._fusion_key
        a = sf.FusedResult("a", 1, None, None, 1 / 61, {}, None)
        b = sf.FusedResult("b", 1, 1, 0.9, 1 / 61, {}, {})  # same score, presence 2
        c = sf.FusedResult("c", 1, 2, 0.8, 1 / 61 + 1 / 62, {}, {})  # higher score
        ordered = sorted([a, b, c], key=key)
        assert [i.recording_id for i in ordered] == ["c", "b", "a"]

        # equal score + presence, different min/max: min asc then max asc.
        x = sf.FusedResult("x", 1, 5, 0.5, 0.1, {}, {})  # min 1 max 5
        y = sf.FusedResult("y", 2, 2, 0.5, 0.1, {}, {})  # min 2 max 2
        ordered = sorted([x, y], key=key)
        assert [i.recording_id for i in ordered] == ["x", "y"]

    def test_presence_tie_break_in_key(self):
        # The presence-count-desc tie-break decides when rrf_score ties
        # exactly (as proven by the key test above); here it is exercised
        # through the key directly with equal scores and a difference in
        # presence.
        key = sf._fusion_key
        single = sf.FusedResult("single", 1, None, None, 2 / 61, {}, None)
        both = sf.FusedResult("both", 1, 1, 0.9, 2 / 61, {}, {})
        ordered = sorted([single, both], key=key)
        assert [i.recording_id for i in ordered] == ["both", "single"]

    def test_absence_is_returned_depth_not_corpus(self):
        # r2 exists in the corpus but is absent from the semantic
        # component's RETURNED list: fusion treats it as absent from the
        # returned depth — evidence null, never a corpus nonmatch.
        kw_results = [
            {"recording_id": "r1", "rank": 1},
            {"recording_id": "r2", "rank": 2},
        ]
        sem_results = [{"recording_id": "r1", "rank": 1}]
        fused = sf.reciprocal_rank_fusion(kw_results, sem_results)
        by_id = {f.recording_id: f for f in fused}
        assert set(by_id) == {"r1", "r2"}
        assert by_id["r2"].semantic_rank is None
        assert by_id["r2"].semantic is None
        assert by_id["r2"].semantic_cosine is None
        assert by_id["r2"].keyword_rank == 2
        assert by_id["r1"].semantic_rank == 1

    def test_duplicate_component_rows_first_occurrence_wins(self):
        kw_results = [
            {"recording_id": "r1", "rank": 1},
            {"recording_id": "r1", "rank": 99},
        ]
        fused = sf.reciprocal_rank_fusion(kw_results, [])
        assert len(fused) == 1
        assert fused[0].keyword_rank == 1


# ---------------------------------------------------------------------------
# 2. Overlap/dedup, keyword display preference, semantic-only rows, evidence
# ---------------------------------------------------------------------------


class TestPresentation:
    def test_real_components_overlap_and_semantic_only(self, tmp_path):
        config = build_healthy(tmp_path)
        payload = sf.hybrid_search(
            "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
        )
        alpha = Recording.objects.get(sha256="sem-h-0")
        beta = Recording.objects.get(sha256="sem-h-1")
        assert payload["result_count"] == 2
        first, second = payload["results"]
        # overlap row: one fused row per Recording, keyword presentation
        # (highlight ranges preserved) even though both ranks are 1.
        assert first["recording_id"] == alpha.pk
        assert first["match"]["source"] == "segment"
        assert first["snippet"] is not None
        assert "matches" in first["snippet"]
        assert first["evidence"]["keyword_rank"] == 1
        assert first["evidence"]["semantic_rank"] == 1
        assert first["evidence"]["semantic_cosine"] == pytest.approx(1.0, abs=1e-3)
        assert first["evidence"]["rrf_score"] == pytest.approx(2 / 61)
        # semantic-only row: semantic fields, no keyword evidence.
        assert second["recording_id"] == beta.pk
        assert second["evidence"]["keyword_rank"] is None
        assert second["evidence"]["semantic_rank"] == 2
        assert second["evidence"]["semantic_cosine"] is not None
        assert second["evidence"]["rrf_score"] == pytest.approx(1 / 62)
        assert "matches" not in second["snippet"]
        assert payload["more_recordings_matched"] == 0
        assert payload["truncated"] is False

    def test_keyword_display_preference_even_when_semantic_stronger(self, tmp_path, monkeypatch):
        config = build_healthy(tmp_path)
        kw_results = [
            _kw_result("overlap", 5, title="kw overlap title"),
            _kw_result("kwonly", 2, title="kw only title"),
        ]
        sem_results = [
            _sem_result("semonly", 1, score=0.99, title="sem only title"),
            _sem_result("overlap", 1, score=0.98, title="sem overlap title"),
        ]
        _patch_components(monkeypatch, _kw_payload(kw_results), _sem_payload(sem_results))
        payload = sf.hybrid_search(
            "alpha", limit=10, config=config, embedder=keyword_embedder(["alpha", "beta"])
        )
        by_id = {r["recording_id"]: r for r in payload["results"]}
        # overlap: keyword rank 5 is WEAKER than semantic rank 1, but the
        # keyword title/match/snippet are still used (highlights win).
        overlap = by_id["overlap"]
        assert overlap["title"] == "kw overlap title"
        assert overlap["match"]["document_key"] == "overlap:kw"
        assert overlap["snippet"]["text"] == "kw snippet"
        assert overlap["evidence"]["keyword_rank"] == 5
        assert overlap["evidence"]["semantic_rank"] == 1
        assert overlap["evidence"]["semantic_cosine"] == pytest.approx(0.98)
        assert overlap["evidence"]["rrf_score"] == pytest.approx(1 / 65 + 1 / 61)
        # keyword-only row: keyword presentation, semantic evidence null.
        kwonly = by_id["kwonly"]
        assert kwonly["title"] == "kw only title"
        assert kwonly["evidence"]["keyword_rank"] == 2
        assert kwonly["evidence"]["semantic_rank"] is None
        assert kwonly["evidence"]["semantic_cosine"] is None
        # semantic-only row: semantic fields.
        semonly = by_id["semonly"]
        assert semonly["title"] == "sem only title"
        assert semonly["match"]["document_key"] == "semonly:sem"
        assert semonly["evidence"]["keyword_rank"] is None
        assert semonly["evidence"]["semantic_rank"] == 1
        assert semonly["evidence"]["semantic_cosine"] == pytest.approx(0.99)
        assert "matches" not in semonly["snippet"]
        # deterministic fused order: overlap > semonly > kwonly.
        assert [r["recording_id"] for r in payload["results"]] == [
            "overlap",
            "semonly",
            "kwonly",
        ]

    def test_payload_contract(self, tmp_path):
        config = build_healthy(tmp_path)
        payload = sf.hybrid_search(
            "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
        )
        assert set(payload) == {
            "query",
            "mode",
            "index_version",
            "semantic_query_version",
            "embedding_generation",
            "rrf_k",
            "depth",
            "limit",
            "results",
            "result_count",
            "truncated",
            "more_recordings_matched",
            "components",
        }
        assert payload["mode"] == "hybrid"
        assert payload["query"] == "alpha"
        assert payload["depth"] == 200
        assert payload["rrf_k"] == 60
        assert payload["index_version"] == si.INDEX_VERSION
        assert payload["semantic_query_version"] == sq.SEMANTIC_QUERY_VERSION
        gen = active_generation()
        assert payload["embedding_generation"] == {
            "id": gen.pk,
            "model": "test-embed-model",
            "dimensions": DIM,
            "embedding_version": ei.EMBEDDING_VERSION,
            "source_index_version": si.INDEX_VERSION,
        }
        for name in ("keyword", "semantic"):
            assert set(payload["components"][name]) == {
                "depth",
                "result_count",
                "truncated",
                "more_recordings_matched",
            }
            assert payload["components"][name]["depth"] == 200


# ---------------------------------------------------------------------------
# 3. EXACT spies: one health / one integrity traversal / one embed
# ---------------------------------------------------------------------------


class TestSingleSweep:
    def _spies(self, monkeypatch):
        calls = {"health": 0, "decoded_rows": 0, "preflight": 0}
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

        def forbidden(*args, **kwargs):
            calls["preflight"] += 1
            raise AssertionError("preflight_full_health must never be called")

        monkeypatch.setattr("workflow.services.search_query.preflight_full_health", forbidden)
        return calls

    def test_exact_spies_one_health_one_integrity_one_embed(self, tmp_path, monkeypatch):
        config = build_healthy(tmp_path)
        calls = self._spies(monkeypatch)
        tracker = []
        payload = sf.hybrid_search(
            "alpha",
            config=config,
            embedder=keyword_embedder(["alpha", "beta"], tracker=tracker),
        )
        assert payload["results"]
        assert calls["health"] == 1  # EXACTLY one source health sweep
        assert calls["preflight"] == 0  # keyword preflight never called
        assert len(tracker) == 1  # EXACTLY one embedding request
        assert tracker[0] == ["alpha"]  # one payload: the prepared query text
        gen = active_generation()
        active_rows = EmbeddingDocument.objects.filter(generation=gen).count()
        assert active_rows > 0
        # ONE integrity traversal: every active vector decoded exactly once.
        assert calls["decoded_rows"] == active_rows

    def test_empty_scope_zero_embed_one_health_one_integrity(self, tmp_path, monkeypatch):
        config = build_healthy(tmp_path)
        calls = self._spies(monkeypatch)
        tracker = []
        payload = sf.hybrid_search(
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
        assert payload["components"]["keyword"]["result_count"] == 0
        assert payload["components"]["semantic"]["result_count"] == 0
        assert calls["health"] == 1  # source health still swept exactly once
        assert calls["preflight"] == 0
        gen = active_generation()
        active_rows = EmbeddingDocument.objects.filter(generation=gen).count()
        # global integrity is STILL validated exactly once on an empty scope.
        assert calls["decoded_rows"] == active_rows

    def test_default_embedder_resolves_at_call_time(self, tmp_path, monkeypatch):
        config = build_healthy(tmp_path)
        calls = {"n": 0}

        def fake(config, texts):
            calls["n"] += 1
            return [
                EmbeddingBatch(text=text, embedding=(1.0, 0.0, 0.0, 0.0))
                for text in texts
            ]

        monkeypatch.setattr("workflow.services.embedding_client.embed_texts", fake)
        payload = sf.hybrid_search("alpha", config=config, embedder=None)
        assert calls["n"] == 1
        assert payload["results"]


# ---------------------------------------------------------------------------
# 4. Both components receive the same scope; out-of-scope cannot win
# ---------------------------------------------------------------------------


class TestScope:
    def test_scope_compiled_exactly_once_and_shared_by_both_engines(
        self, tmp_path, monkeypatch
    ):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)
        alpha = Recording.objects.get(sha256="sem-h-0")
        beta = Recording.objects.get(sha256="sem-h-1")
        scope = Recording.objects.filter(pk=alpha.pk)
        captured = {"compiles": 0, "kw_compiled": None, "sem_scope": None}
        # The EXACT single source of compiled scope SQL: hybrid must go
        # through it exactly once in total.
        real_compile = kw._compile_scope

        def spy_compile(scope_qs, *, using):
            captured["compiles"] += 1
            captured["qs"] = scope_qs
            return real_compile(scope_qs, using=using)

        monkeypatch.setattr(kw, "_compile_scope", spy_compile)
        real_kw = sf.search_recordings

        def spy_kw(query, *, limit, using="default", scope=None, compiled_scope=None, **kwargs):
            captured["kw_scope_arg"] = scope
            captured["kw_compiled"] = compiled_scope
            captured["kw_limit"] = limit
            return real_kw(
                query, limit=limit, using=using, scope=scope,
                compiled_scope=compiled_scope, **kwargs,
            )

        monkeypatch.setattr(sf, "search_recordings", spy_kw)
        real_run = sf.run_semantic_snapshot

        def spy_run(snapshot, *, using, limit, verify_final=True):
            captured["sem_scope"] = snapshot.scope
            return real_run(snapshot, using=using, limit=limit, verify_final=verify_final)

        monkeypatch.setattr(sf, "run_semantic_snapshot", spy_run)
        payload = sf.hybrid_search(
            "alpha",
            config=config,
            scope=scope,
            embedder=keyword_embedder(["alpha", "beta"]),
        )
        # EXACTLY ONE compilation total; both engines consume the SAME
        # SQL + parameter snapshot.
        assert captured["compiles"] == 1
        assert captured["qs"] is scope
        assert captured["kw_scope_arg"] is None  # keyword got the compiled scope, never the QuerySet
        kw_compiled = captured["kw_compiled"]
        assert kw_compiled is not None
        assert kw_compiled is not None and captured["sem_scope"] is not None
        assert kw_compiled.sql == captured["sem_scope"].sql
        assert kw_compiled.params == captured["sem_scope"].params
        assert kw_compiled.using == "default"
        assert captured["kw_limit"] == 200
        # out-of-scope cannot win
        assert len(payload["results"]) == 1
        assert payload["results"][0]["recording_id"] == alpha.pk
        assert beta.pk not in [r["recording_id"] for r in payload["results"]]
        assert payload["more_recordings_matched"] == 0

    def test_unscoped_hybrid_never_compiles(self, tmp_path, monkeypatch):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)
        captured = {"compiles": 0}
        real_compile = kw._compile_scope

        def spy_compile(scope_qs, *, using):
            captured["compiles"] += 1
            return real_compile(scope_qs, using=using)

        monkeypatch.setattr(kw, "_compile_scope", spy_compile)
        payload = sf.hybrid_search(
            "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
        )
        assert captured["compiles"] == 0
        assert payload["results"]

    def test_compiled_scope_keyword_parity_with_queryset_scope(self, tmp_path):
        # The trusted ``compiled_scope=`` path must be byte-identical to
        # the historical QuerySet ``scope=`` path.
        build_healthy(tmp_path)
        alpha = Recording.objects.get(sha256="sem-h-0")
        scope = Recording.objects.filter(pk=alpha.pk)
        compiled = kw.compile_scope(scope, using="default")
        direct = kw.search_recordings("alpha", scope=scope)
        compiled_payload = kw.search_recordings(
            "alpha", compiled_scope=compiled
        )
        assert compiled_payload == direct

    def test_compiled_scope_rejects_ambiguous_or_malformed(self, tmp_path):
        build_healthy(tmp_path)
        scope = Recording.objects.filter(pk=Recording.objects.get(sha256="sem-h-0").pk)
        compiled = kw.compile_scope(scope, using="default")
        # both QuerySet AND compiled scope is ambiguous.
        with pytest.raises(kw.SearchQueryInputError, match="never both"):
            kw.search_recordings("alpha", scope=scope, compiled_scope=compiled)
        # a non-CompiledScope value is rejected.
        with pytest.raises(kw.SearchQueryInputError, match="must be a CompiledScope"):
            kw.search_recordings(
                "alpha", compiled_scope=("SELECT 1", ())  # type: ignore[arg-type]
            )
        # a compiled scope built for another alias is rejected.
        other = kw.CompiledScope(sql=compiled.sql, params=compiled.params, using="other")
        with pytest.raises(kw.SearchQueryInputError, match="different database connection"):
            kw.search_recordings("alpha", compiled_scope=other)

    def test_invalid_scope_is_fixed_sanitized(self, tmp_path):
        config = build_healthy(tmp_path)
        from workflow.models import SearchDocument

        with pytest.raises(Exception, match="unsliced queryset of Recordings"):
            sf.hybrid_search(
                "alpha",
                config=config,
                scope=SearchDocument.objects.all(),
                embedder=keyword_embedder(["alpha", "beta"]),
            )
        with pytest.raises(Exception, match="unsliced queryset of Recordings"):
            sf.hybrid_search(
                "alpha",
                config=config,
                scope=Recording.objects.all()[:1],
                embedder=keyword_embedder(["alpha", "beta"]),
            )


# ---------------------------------------------------------------------------
# 5. Component depth is always 200 while the final limit varies
# ---------------------------------------------------------------------------


class TestDepth:
    def test_component_depth_always_200_while_final_limit_varies(self, tmp_path, monkeypatch):
        build_healthy(tmp_path)
        config = emb_config(tmp_path)
        real_kw = sf.search_recordings
        real_run = sf.run_semantic_snapshot
        captured = {}

        def spy_kw(query, *, limit, using="default", scope=None, **kwargs):
            captured["kw_limit"] = limit
            return real_kw(query, limit=limit, using=using, scope=scope, **kwargs)

        def spy_run(snapshot, *, using, limit, verify_final=True):
            captured["sem_limit"] = limit
            captured["sem_verify_final"] = verify_final
            return real_run(snapshot, using=using, limit=limit, verify_final=verify_final)

        monkeypatch.setattr(sf, "search_recordings", spy_kw)
        monkeypatch.setattr(sf, "run_semantic_snapshot", spy_run)
        embedder = keyword_embedder(["alpha", "beta"])
        for final_limit in (1, 7, 200):
            payload = sf.hybrid_search(
                "alpha", limit=final_limit, config=config, embedder=embedder
            )
            assert captured["kw_limit"] == 200
            assert captured["sem_limit"] == 200
            assert captured["sem_verify_final"] is False
            assert payload["limit"] == final_limit
            assert payload["result_count"] <= final_limit
            assert payload["depth"] == 200


# ---------------------------------------------------------------------------
# 6. Completeness / truncation / null-more truth table
# ---------------------------------------------------------------------------


class TestCompleteness:
    def test_both_complete_exact_more(self, tmp_path, monkeypatch):
        config = build_healthy(tmp_path)
        kw_results = [_kw_result(f"r{i}", i) for i in range(1, 6)]
        sem_results = [_sem_result(f"r{i}", i) for i in range(1, 4)]
        _patch_components(
            monkeypatch, _kw_payload(kw_results, more=0), _sem_payload(sem_results, more=0)
        )
        payload = sf.hybrid_search(
            "alpha", limit=2, config=config, embedder=keyword_embedder(["alpha", "beta"])
        )
        assert payload["result_count"] == 2
        assert payload["more_recordings_matched"] == 3  # 5 fused - 2 final
        assert payload["truncated"] is False
        assert payload["components"]["keyword"]["result_count"] == 5
        assert payload["components"]["semantic"]["result_count"] == 3
        assert payload["components"]["keyword"]["more_recordings_matched"] == 0
        assert payload["components"]["semantic"]["more_recordings_matched"] == 0

    def test_keyword_incomplete_makes_more_null(self, tmp_path, monkeypatch):
        config = build_healthy(tmp_path)
        kw_results = [_kw_result(f"r{i}", i) for i in range(1, 6)]
        sem_results = [_sem_result(f"r{i}", i) for i in range(1, 3)]
        _patch_components(
            monkeypatch, _kw_payload(kw_results, more=3), _sem_payload(sem_results, more=0)
        )
        payload = sf.hybrid_search(
            "alpha", limit=10, config=config, embedder=keyword_embedder(["alpha", "beta"])
        )
        assert payload["more_recordings_matched"] is None
        assert payload["truncated"] is False

    def test_semantic_incomplete_makes_more_null(self, tmp_path, monkeypatch):
        config = build_healthy(tmp_path)
        kw_results = [_kw_result(f"r{i}", i) for i in range(1, 6)]
        sem_results = [_sem_result(f"r{i}", i) for i in range(1, 3)]
        _patch_components(
            monkeypatch, _kw_payload(kw_results, more=0), _sem_payload(sem_results, more=2)
        )
        payload = sf.hybrid_search(
            "alpha", limit=10, config=config, embedder=keyword_embedder(["alpha", "beta"])
        )
        assert payload["more_recordings_matched"] is None

    def test_keyword_unknown_more_makes_more_null(self, tmp_path, monkeypatch):
        config = build_healthy(tmp_path)
        kw_results = [_kw_result(f"r{i}", i) for i in range(1, 6)]
        _patch_components(
            monkeypatch,
            _kw_payload(kw_results, more=None),
            _sem_payload([_sem_result("r1", 1)], more=0),
        )
        payload = sf.hybrid_search(
            "alpha", limit=10, config=config, embedder=keyword_embedder(["alpha", "beta"])
        )
        assert payload["more_recordings_matched"] is None

    def test_either_component_truncated_sets_hybrid_truncated(self, tmp_path, monkeypatch):
        config = build_healthy(tmp_path)
        kw_results = [_kw_result(f"r{i}", i) for i in range(1, 6)]
        sem_results = [_sem_result(f"r{i}", i) for i in range(1, 4)]
        _patch_components(
            monkeypatch,
            _kw_payload(kw_results, truncated=True, more=0),
            _sem_payload(sem_results, more=0),
        )
        payload = sf.hybrid_search(
            "alpha", limit=2, config=config, embedder=keyword_embedder(["alpha", "beta"])
        )
        assert payload["truncated"] is True
        # exact more is still possible when both populations are complete.
        assert payload["more_recordings_matched"] == 3
        assert payload["components"]["keyword"]["truncated"] is True
        assert payload["components"]["semantic"]["truncated"] is False

        _patch_components(
            monkeypatch,
            _kw_payload(kw_results, more=0),
            _sem_payload(sem_results, truncated=True, more=0),
        )
        payload = sf.hybrid_search(
            "alpha", limit=2, config=config, embedder=keyword_embedder(["alpha", "beta"])
        )
        assert payload["truncated"] is True

    def test_both_empty_and_complete_zero_more(self, tmp_path, monkeypatch):
        config = build_healthy(tmp_path)
        _patch_components(monkeypatch, _kw_payload([], more=0), _sem_payload([], more=0))
        payload = sf.hybrid_search(
            "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
        )
        assert payload["result_count"] == 0
        assert payload["more_recordings_matched"] == 0
        assert payload["truncated"] is False

    def test_no_results_on_crafted_component_failure_never_partial(self, tmp_path, monkeypatch):
        # The truth table never fabricates partial fusion: a component
        # that would leave the population unproven still yields a full
        # payload (results over the returned depth), never a claim.
        config = build_healthy(tmp_path)
        kw_results = [_kw_result(f"r{i}", i) for i in range(1, 6)]
        _patch_components(
            monkeypatch, _kw_payload(kw_results, more=0), _sem_payload([], more=1)
        )
        payload = sf.hybrid_search(
            "alpha", limit=10, config=config, embedder=keyword_embedder(["alpha", "beta"])
        )
        assert payload["result_count"] == 5
        assert payload["more_recordings_matched"] is None


# ---------------------------------------------------------------------------
# 7. Failure / concurrency / zero-vector: fail closed, no keyword fallback
# ---------------------------------------------------------------------------


class TestFailures:
    def test_zero_query_vector_fails_closed(self, tmp_path):
        config = build_healthy(tmp_path)
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sf.hybrid_search(
                "alpha",
                config=config,
                embedder=keyword_embedder(["alpha", "beta"], zero_texts=("alpha",)),
            )
        assert excinfo.value.code == sq.INVALID_QUERY_VECTOR
        # No keyword-only fallback: keyword would have matched, but the
        # hybrid raises instead of returning partial keyword results.

    def test_zero_stored_vector_fails_closed_as_invalid_document_vector(self, tmp_path):
        config = build_healthy(tmp_path)
        gen = active_generation()
        victim = EmbeddingDocument.objects.filter(generation=gen).first()
        EmbeddingDocument.objects.filter(pk=victim.pk).update(
            vector_blob=encode_vector([0.0] * gen.dimensions, dimensions=gen.dimensions)
        )
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sf.hybrid_search(
                "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
            )
        assert excinfo.value.code == sq.INVALID_DOCUMENT_VECTOR

    def test_missing_document_fails_closed(self, tmp_path):
        config = build_healthy(tmp_path)
        make_transcribed_recording(["brand new recording"], sha="hyb-miss-0")
        si.rebuild_index()  # new SearchDocuments, no active vectors
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sf.hybrid_search(
                "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
            )
        assert excinfo.value.code == sq.SEMANTIC_INDEX_INTEGRITY

    def test_endpoint_error_sanitized(self, tmp_path):
        config = build_healthy(tmp_path)
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sf.hybrid_search(
                "alpha",
                config=config,
                embedder=keyword_embedder(["alpha", "beta"], fail_calls=(1,)),
            )
        assert excinfo.value.code == sq.SEMANTIC_EMBEDDING_FAILED
        assert "(http_error)" in str(excinfo.value)
        assert "alpha" not in str(excinfo.value)

    def test_hostile_embedding_code_allowlisted(self, tmp_path):
        config = build_healthy(tmp_path)

        class HostileError(EmbeddingError):
            code = "CANARY-HOSTILE-HYBRID-EMBEDDING-CODE"

        def hostile(config, texts):
            raise HostileError()

        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sf.hybrid_search("alpha", config=config, embedder=hostile)
        message = str(excinfo.value)
        assert "CANARY-HOSTILE-HYBRID-EMBEDDING-CODE" not in message
        assert "(embedding_error)" in message

    def test_concurrent_data_version_change(self, tmp_path, monkeypatch):
        config = build_healthy(tmp_path)
        calls = {"n": 0}
        real = sq._pragma_data_version

        def flaky(using):
            calls["n"] += 1
            if calls["n"] < 3:
                return real(using)
            return real(using) + 1  # another connection committed at the end

        monkeypatch.setattr(sq, "_pragma_data_version", flaky)
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sf.hybrid_search(
                "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
            )
        assert excinfo.value.code == sq.SEMANTIC_CONCURRENT_CHANGE
        assert calls["n"] >= 3

    def test_concurrent_active_promotion_during_embed(self, tmp_path):
        config = build_healthy(tmp_path)
        old = active_generation()

        def promote_while_query_embeds():
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
            sf.hybrid_search(
                "alpha",
                config=config,
                embedder=keyword_embedder(
                    ["alpha", "beta"], mutate_call={1: promote_while_query_embeds}
                ),
            )
        assert excinfo.value.code == sq.SEMANTIC_CONCURRENT_CHANGE
        old.refresh_from_db()
        assert old.state == EmbeddingGenerationState.SUPERSEDED

    def test_source_index_unhealthy(self, tmp_path):
        config = build_healthy(tmp_path)
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_search_fts")
        try:
            with pytest.raises(sq.SemanticQueryError) as excinfo:
                sf.hybrid_search(
                    "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
                )
            assert excinfo.value.code == sq.SEMANTIC_SOURCE_UNHEALTHY
        finally:
            si.rebuild_index()

    def test_unexpected_failure_sanitized(self, tmp_path, monkeypatch):
        config = build_healthy(tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError("CANARY-HYBRID-UNEXPECTED-SECRET")

        monkeypatch.setattr(sq, "_pragma_data_version", boom)
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sf.hybrid_search(
                "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
            )
        assert excinfo.value.code == sq.SEMANTIC_UNEXPECTED
        assert "CANARY-HYBRID-UNEXPECTED-SECRET" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# 8. Strict read-only / no lock / no HTTP in transaction / privacy
# ---------------------------------------------------------------------------


class TestPurity:
    def test_read_only_no_writes_no_lock_no_sync_no_rebuild(self, tmp_path, monkeypatch):
        config = build_healthy(tmp_path)

        def forbidden(*args, **kwargs):
            raise AssertionError("hybrid search must stay read-only")

        monkeypatch.setattr("workflow.services.pipeline_lock.pipeline_lock", forbidden)
        monkeypatch.setattr(
            "workflow.services.search_sync.schedule_recording_sync", forbidden
        )
        monkeypatch.setattr("workflow.services.search_index.rebuild_index", forbidden)
        monkeypatch.setattr(
            "workflow.services.embedding_index.rebuild_embedding_index", forbidden
        )
        monkeypatch.setattr(
            "workflow.services.embedding_index.repair_embedding_index", forbidden
        )
        with CaptureQueriesContext(connection) as ctx:
            sf.hybrid_search(
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

    def test_rejects_caller_transaction_zero_embedder_calls(self, tmp_path):
        config = build_healthy(tmp_path)

        def forbidden(config, texts):
            raise AssertionError("no embedder call inside a caller transaction")

        with transaction.atomic():
            with pytest.raises(sq.SemanticQueryError) as excinfo:
                sf.hybrid_search("alpha", config=config, embedder=forbidden)
        assert excinfo.value.code == sq.SEMANTIC_IN_TRANSACTION

    def test_embedding_request_outside_transaction(self, tmp_path):
        config = build_healthy(tmp_path)
        payload = sf.hybrid_search(
            "alpha",
            config=config,
            embedder=keyword_embedder(["alpha", "beta"], guard_no_txn=True),
        )
        assert payload["results"]

    def test_errors_never_echo_content_or_keys(self, tmp_path):
        canary = "SUPER-SECRET-HYBRID-CANARY"
        make_transcribed_recording([canary + " alpha"], sha="hyb-can-0")
        si.rebuild_index()
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha"]))
        gen = active_generation()
        victim = EmbeddingDocument.objects.filter(generation=gen).first()
        EmbeddingDocument.objects.filter(pk=victim.pk).update(vector_blob=b"\x00\x00")
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sf.hybrid_search(
                "alpha", config=config, embedder=keyword_embedder(["alpha"])
            )
        message = str(excinfo.value)
        assert canary not in message
        assert "Traceback" not in message
        assert victim.document_key not in message

    def test_input_validation_before_any_health_work(self, tmp_path, monkeypatch):
        config = build_healthy(tmp_path)
        calls = {"health": 0}
        real = si.build_status_report

        def spy(*args, **kwargs):
            calls["health"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(si, "build_status_report", spy)
        canary = "SECRET-" + "x" * 300
        with pytest.raises(sf.HybridSearchInputError) as excinfo:
            sf.hybrid_search(canary, config=config, embedder=keyword_embedder(["alpha"]))
        assert "SECRET" not in str(excinfo.value)
        assert calls["health"] == 0  # rejected before any sweep

        # keyword term cap applies to the hybrid query.
        with pytest.raises(sf.HybridSearchInputError):
            sf.hybrid_search(
                " ".join(f"t{i}" for i in range(9)),
                config=config,
                embedder=keyword_embedder(["alpha"]),
            )
        assert calls["health"] == 0

    @pytest.mark.parametrize("bad_limit", [0, -1, 201, True, "5", 1.5])
    def test_limit_bounds(self, bad_limit, tmp_path):
        config = build_healthy(tmp_path)
        with pytest.raises(sf.HybridSearchInputError):
            sf.hybrid_search(
                "alpha",
                limit=bad_limit,
                config=config,
                embedder=keyword_embedder(["alpha"]),
            )

    def test_empty_and_whitespace_query_rejected(self, tmp_path):
        config = build_healthy(tmp_path)
        for raw in (None, "", "   \t\n "):
            with pytest.raises(sf.HybridSearchInputError) as excinfo:
                sf.hybrid_search(raw, config=config, embedder=keyword_embedder(["alpha"]))
            assert "empty" in str(excinfo.value)