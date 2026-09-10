"""Deterministic depth-200 hybrid keyword/semantic fusion (Step 5C).

One public hybrid entry point (:func:`hybrid_search`) plus PURE fusion
helpers (:func:`reciprocal_rank_fusion` / :func:`rrf_score`) with the
shared one-sweep orchestration contract:

- the hybrid query MUST satisfy the existing keyword validation (NFC/
  strip, <= 256 codepoints, <= 8 terms, limit 1..200) AND the semantic
  already-normalized preparation contract — cheap usage errors are
  fixed/sanitized and raised BEFORE any health/DB work;
- BOTH components run at the fixed depth :data:`HYBRID_DEPTH` (200 =
  ``search_query.MAX_RESULT_LIMIT``) regardless of the final requested
  limit (<= 200);
- exactly ONE complete source/keyword health sweep
  (``search_index.build_status_report``), exactly ONE global embedding
  integrity validation/decode traversal and exactly ONE query embedding
  request for a nonempty scoped corpus (ZERO for an empty scope);
- the keyword component calls ``search_query.search_recordings``
  DIRECTLY (never ``preflight_full_health``); the semantic component
  consumes the same validated ``data_version`` / active-generation /
  compiled-scope / query-vector snapshot (:class:`SemanticSnapshot`)
  and never re-triggers source health or the query embedding;
- sequence: cheap validation → reject caller transaction → capture
  ``PRAGMA data_version`` BEFORE health → one source full health sweep
  → validate exact active identity → re-check ``data_version``
  immediately after health → compile/validate the Recording scope
  EXACTLY ONCE into an immutable
  :class:`~workflow.services.search_query.CompiledScope`
  (``search_query.compile_scope`` wraps the exact keyword compiler) →
  determine empty scope → one query embed when needed → run the keyword
  engine without health (consuming that SAME compiled scope via
  ``compiled_scope=``, never the QuerySet, never a recompile) → run the
  semantic global integrity+scoring once over the snapshot holding the
  SAME compiled value → ONE final ``data_version`` + complete active
  identity check AFTER BOTH components. NO results on any failure or
  concurrent change.

Fusion is PURE reciprocal rank fusion over the RETURNED component lists
("absent" = absent from the returned depth, never a corpus nonmatch):
one-based component ranks, ``k=60``, score = sum ``1/(60+rank)`` over
present components; deterministic order RRF desc, component presence
count desc, minimum present rank, maximum present rank, canonical
recording id. No cosine/keyword raw-score normalization or weights.
Presentation prefers keyword evidence (title/match/snippet) whenever it
exists — highlights are preserved even when the semantic rank is
stronger; otherwise the semantic fields are used. Every result carries
an ``evidence`` block (keyword_rank / semantic_rank / semantic_cosine /
rrf_score) and the final ``rank`` is the FUSED rank.

Completeness is exact ONLY over the returned component lists:
``truncated`` is true if either component says truncated;
``more_recordings_matched`` is the exact ``len(fused) - final_count``
ONLY when BOTH component populations are proved complete
(``more_recordings_matched == 0`` and no unknown), otherwise ``null``.
Explicit per-component metadata keeps every corpus-wide claim honest.

Everything is strictly read-only: SELECT/PRAGMA plus exactly one
localhost embedding request outside any transaction — no writes, no
pipeline lock, no rebuild/repair/sync, no logs, no caches, no retries,
no keyword-only fallback. Every public failure is sanitized: query
text, vector values, document keys, recording ids, SQL, indexed content
and secrets never appear in any message.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from brainlib.config import ConfigError
from workflow.services import search_index
from workflow.services import semantic_query as sq
from workflow.services.embedding_client import EmbeddingError
from workflow.services.search_query import (
    DEFAULT_RESULT_LIMIT,
    MAX_RESULT_LIMIT,
    SearchQueryInputError,
    compile_scope,
    search_recordings,
    validate_query,
)
from workflow.services.semantic_query import (
    SEMANTIC_EMBEDDING_FAILED,
    SEMANTIC_QUERY_VERSION,
    SEMANTIC_SOURCE_UNHEALTHY,
    SEMANTIC_UNEXPECTED,
    SemanticQueryError,
    SemanticQueryInputError,
    SemanticSnapshot,
    _SOURCE_UNHEALTHY_ERROR,
    _UNEXPECTED_ERROR,
    _reject_in_atomic_block,
    _require_unchanged_data_version,
    _request_error,
    _validate_embedding_setup,
    _verify_final_state,
    embed_query_vector,
    prepare_query_text,
    run_semantic_snapshot,
)

# ---------------------------------------------------------------------------
# Constants + errors
# ---------------------------------------------------------------------------

# RRF parameter: score contribution of a component at one-based rank r is
# 1/(k+r). Deterministic and fixed — never configurable, never weighted.
RRF_K = 60

# Both components always run at the full result depth (200), regardless
# of the final requested limit (1..200): fusion is exact over these
# returned depth lists only.
HYBRID_DEPTH = MAX_RESULT_LIMIT  # 200


class HybridSearchError(ConfigError):
    """Sanitized hybrid-search failure (engine failures reuse the stable
    :class:`~workflow.services.semantic_query.SemanticQueryError`
    taxonomy and codes; this base exists for hybrid-specific classes)."""


class HybridSearchInputError(HybridSearchError):
    """Malformed or over-cap hybrid query input (usage error)."""


# ---------------------------------------------------------------------------
# Pure RRF fusion
# ---------------------------------------------------------------------------


def rrf_score(keyword_rank, semantic_rank, *, k: int = RRF_K) -> float:
    """Pure one-based reciprocal-rank-fusion score.

    ``1/(k+rank)`` summed over the PRESENT component ranks (``None``
    means absent from that component's returned depth — never a corpus
    nonmatch). ``math.fsum`` keeps the sum correctly rounded and
    order-independent, so equal rank sets always tie exactly.
    """
    terms = []
    if keyword_rank is not None:
        terms.append(1.0 / (k + keyword_rank))
    if semantic_rank is not None:
        terms.append(1.0 / (k + semantic_rank))
    return math.fsum(terms) if terms else 0.0


@dataclass(frozen=True)
class FusedResult:
    """One fused Recording row: rank evidence plus the component result
    dicts (the keyword dict is used for presentation whenever present)."""

    recording_id: str
    keyword_rank: int | None
    semantic_rank: int | None
    semantic_cosine: float | None
    rrf_score: float
    keyword: dict | None
    semantic: dict | None

    @property
    def presence(self) -> int:
        return int(self.keyword_rank is not None) + int(self.semantic_rank is not None)

    @property
    def min_present_rank(self) -> int:
        ranks = [r for r in (self.keyword_rank, self.semantic_rank) if r is not None]
        return min(ranks)

    @property
    def max_present_rank(self) -> int:
        ranks = [r for r in (self.keyword_rank, self.semantic_rank) if r is not None]
        return max(ranks)


def _index_components(results: Sequence[Mapping]) -> tuple[dict, dict]:
    """First-occurrence-wins index of a component result list: returns
    ``(result_by_recording_id, rank_by_recording_id)``. The 1-based rank
    is the row's own ``rank`` when present, else its list position
    (components always emit one-based ordered ranks)."""
    result_by_rid: dict = {}
    rank_by_rid: dict = {}
    for position, result in enumerate(results, start=1):
        recording_id = result.get("recording_id")
        if recording_id is None:
            continue
        if recording_id in result_by_rid:
            continue
        result_by_rid[recording_id] = result
        rank_by_rid[recording_id] = result.get("rank", position)
    return result_by_rid, rank_by_rid


def _semantic_cosine(result: Mapping | None):
    if result is None:
        return None
    return result.get("score")


def _fusion_key(item: FusedResult) -> tuple:
    """Deterministic fused order: RRF desc, presence count desc, minimum
    present rank, maximum present rank, canonical recording id."""
    return (
        -item.rrf_score,
        -item.presence,
        item.min_present_rank,
        item.max_present_rank,
        item.recording_id,
    )


def reciprocal_rank_fusion(
    keyword_results: Sequence[Mapping],
    semantic_results: Sequence[Mapping],
    *,
    k: int = RRF_K,
) -> list[FusedResult]:
    """Pure deterministic RRF fusion over two ordered component result
    lists.

    ``keyword_results`` / ``semantic_results`` are the component payload
    result rows (each carrying ``recording_id`` and its 1-based
    ``rank``); fusion is EXACT only over these returned lists — a
    Recording absent from a component list is treated as absent from
    that component's returned depth, never as a corpus nonmatch. One
    fused row per Recording. PURE: no DB, no network, no health, no
    writes. Deterministic order: RRF desc, component presence count
    desc, minimum present rank, maximum present rank, canonical
    recording id.
    """
    keyword_by_rid, keyword_ranks = _index_components(keyword_results)
    semantic_by_rid, semantic_ranks = _index_components(semantic_results)
    fused: list[FusedResult] = []
    for recording_id in sorted(set(keyword_ranks) | set(semantic_ranks)):
        krank = keyword_ranks.get(recording_id)
        srank = semantic_ranks.get(recording_id)
        fused.append(
            FusedResult(
                recording_id=recording_id,
                keyword_rank=krank,
                semantic_rank=srank,
                semantic_cosine=_semantic_cosine(semantic_by_rid.get(recording_id)),
                rrf_score=rrf_score(krank, srank, k=k),
                keyword=keyword_by_rid.get(recording_id),
                semantic=semantic_by_rid.get(recording_id),
            )
        )
    fused.sort(key=_fusion_key)
    return fused


# ---------------------------------------------------------------------------
# Presentation assembly
# ---------------------------------------------------------------------------


def _present_results(fused: Sequence[FusedResult], *, limit: int) -> list[dict]:
    """Keyword-first presentation: whenever keyword evidence exists use
    the keyword title/match/snippet (highlights preserved even when the
    semantic rank is stronger); otherwise the semantic fields. Every row
    carries the fused rank and the evidence block."""
    results = []
    for rank, item in enumerate(fused[:limit], start=1):
        if item.keyword is not None:
            title = item.keyword["title"]
            match = item.keyword["match"]
            snippet = item.keyword["snippet"]
        else:
            title = item.semantic["title"]
            match = item.semantic["match"]
            snippet = item.semantic["snippet"]
        results.append(
            {
                "rank": rank,
                "recording_id": item.recording_id,
                "title": title,
                "match": match,
                "snippet": snippet,
                "evidence": {
                    "keyword_rank": item.keyword_rank,
                    "semantic_rank": item.semantic_rank,
                    "semantic_cosine": item.semantic_cosine,
                    "rrf_score": item.rrf_score,
                },
            }
        )
    return results


def _component_metadata(payload: dict) -> dict:
    return {
        "depth": HYBRID_DEPTH,
        "result_count": payload["result_count"],
        "truncated": payload["truncated"],
        "more_recordings_matched": payload["more_recordings_matched"],
    }


def _assemble_payload(
    *,
    normalized: str,
    limit: int,
    active,
    keyword_payload: dict,
    semantic_payload: dict,
) -> dict:
    """Fuse the two depth-200 component payloads into the final hybrid
    payload. ``truncated`` is true if either component says truncated.
    ``more_recordings_matched`` is the exact ``len(fused) - final_count``
    ONLY when BOTH component populations are proved complete
    (``more_recordings_matched == 0`` and no unknown), otherwise null —
    the explicit component metadata never implies a corpus-wide claim."""
    fused = reciprocal_rank_fusion(
        keyword_payload["results"], semantic_payload["results"]
    )
    results = _present_results(fused, limit=limit)
    keyword_more = keyword_payload["more_recordings_matched"]
    semantic_more = semantic_payload["more_recordings_matched"]
    if keyword_more == 0 and semantic_more == 0:
        more_recordings_matched = max(len(fused) - len(results), 0)
    else:
        more_recordings_matched = None
    truncated = bool(keyword_payload["truncated"] or semantic_payload["truncated"])
    return {
        "query": normalized,
        "mode": "hybrid",
        "index_version": search_index.INDEX_VERSION,
        "semantic_query_version": SEMANTIC_QUERY_VERSION,
        "embedding_generation": {
            "id": active.pk,
            "model": active.model,
            "dimensions": active.dimensions,
            "embedding_version": active.embedding_version,
            "source_index_version": active.source_index_version,
        },
        "rrf_k": RRF_K,
        "depth": HYBRID_DEPTH,
        "limit": limit,
        "results": results,
        "result_count": len(results),
        "truncated": truncated,
        "more_recordings_matched": more_recordings_matched,
        "components": {
            "keyword": _component_metadata(keyword_payload),
            "semantic": _component_metadata(semantic_payload),
        },
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _validate_hybrid_input(raw, limit: int) -> str:
    """ALL cheap hybrid input validation in ONE sweep-free entry point.

    The hybrid query MUST satisfy the existing keyword validation
    (NFC/strip, <= 256 codepoints, <= 8 terms, limit 1..200) AND the
    semantic already-normalized preparation contract. Usage errors are
    fixed/sanitized (the offending text is never echoed) and raised
    BEFORE any health/DB work."""
    try:
        normalized = validate_query(raw, limit)
        prepare_query_text(normalized)
    except (SearchQueryInputError, SemanticQueryInputError) as exc:
        raise HybridSearchInputError(str(exc)) from None
    return normalized


def validate_hybrid_query(raw, limit: int = DEFAULT_RESULT_LIMIT) -> str:
    """Public cheap hybrid input validation (no health/DB/network).

    Exposes the EXACT hybrid input contract (keyword validation AND the
    semantic already-normalized preparation contract) as a sweep-free
    entry point, so the CLI can map usage errors to exit 2 BEFORE any
    health or network work without invoking the full hybrid search.
    Returns the normalized query; raises :class:`HybridSearchInputError`
    with a fixed sanitized message (the offending text is never echoed).
    """
    return _validate_hybrid_input(raw, limit)


def hybrid_search(
    raw,
    *,
    limit: int = DEFAULT_RESULT_LIMIT,
    using: str = "default",
    scope=None,
    config=None,
    embedder=None,
) -> dict:
    """One complete read-only hybrid (keyword + semantic) search.

    Sequence (single source of truth): cheap validation → reject caller
    transaction → capture ``PRAGMA data_version`` BEFORE health → EXACTLY
    ONE source full health sweep (``search_index.build_status_report``)
    → validate exact active identity → re-check ``data_version``
    immediately after health → compile/validate the Recording scope
    EXACTLY ONCE into an immutable
    :class:`~workflow.services.search_query.CompiledScope`
    (``search_query.compile_scope`` wraps the exact keyword compiler) →
    determine empty scope → EXACTLY ONE query embedding request (zero
    for an empty scope) → run the keyword engine directly
    (``search_query.search_recordings``, never ``preflight_full_health``)
    at the fixed depth 200, consuming that SAME compiled scope via
    ``compiled_scope=`` (never the QuerySet, never a recompile) → run the
    semantic global integrity+scoring once over the SHARED frozen
    snapshot (holding that same compiled scope; never re-sweeping health,
    never re-embedding) → ONE final ``data_version`` + complete active
    identity check AFTER BOTH components → deterministic RRF fusion.

    NO results on any failure or concurrent change; no keyword-only
    fallback (a zero query vector fails ``invalid_query_vector``, a zero
    stored document vector fails ``invalid_document_vector``). Strictly
    read-only: SELECT/PRAGMA plus one localhost embedding request
    outside any transaction; no lock, no writes, no rebuild/repair/sync,
    no logs, no caches, no retries.

    ``scope`` is the existing unsliced ``Recording`` QuerySet contract
    (never SQL; ``search_query.compile_scope`` compiles it EXACTLY ONCE
    through the exact keyword compiler) applied BEFORE both component
    rankings — out-of-scope documents can never win. ``config``/
    ``embedder`` mirror the semantic engine: the production
    ``embedding_client.embed_texts`` is resolved at call time when
    ``embedder`` is ``None``.
    """
    normalized = _validate_hybrid_input(raw, limit)
    try:
        _reject_in_atomic_block(using)
        if config is None:
            raise SemanticQueryError(SEMANTIC_UNEXPECTED, _UNEXPECTED_ERROR)
        if embedder is None:
            from workflow.services import embedding_client

            embedder = embedding_client.embed_texts
        data_version_before = sq._pragma_data_version(using)
        source_report = search_index.build_status_report(using=using)
        if not source_report.get("healthy"):
            raise SemanticQueryError(
                SEMANTIC_SOURCE_UNHEALTHY, _SOURCE_UNHEALTHY_ERROR
            )
        active = _validate_embedding_setup(config, using=using)
        _require_unchanged_data_version(data_version_before, using)
        compiled_scope = compile_scope(scope, using=using) if scope is not None else None
        scope_sql = compiled_scope.sql if compiled_scope is not None else None
        scope_params = list(compiled_scope.params) if compiled_scope is not None else None
        query_vector = embed_query_vector(
            normalized,
            active=active,
            scope_sql=scope_sql,
            scope_params=scope_params,
            using=using,
            config=config,
            embedder=embedder,
        )
        snapshot = SemanticSnapshot(
            normalized=normalized,
            active=active,
            scope=compiled_scope,
            query_vector=query_vector,
            data_version_before=data_version_before,
        )
        keyword_payload = search_recordings(
            normalized,
            limit=HYBRID_DEPTH,
            using=using,
            compiled_scope=compiled_scope,
        )
        semantic_payload = run_semantic_snapshot(
            snapshot, using=using, limit=HYBRID_DEPTH, verify_final=False
        )
        _verify_final_state(active, data_version_before, using)
        return _assemble_payload(
            normalized=normalized,
            limit=limit,
            active=active,
            keyword_payload=keyword_payload,
            semantic_payload=semantic_payload,
        )
    except SemanticQueryError:
        raise
    except ConfigError:
        # Already-sanitized shared errors (scope compiler usage/index
        # failures, keyword engine index failures) propagate unchanged —
        # never forked.
        raise
    except EmbeddingError as exc:
        raise SemanticQueryError(
            SEMANTIC_EMBEDDING_FAILED, _request_error(getattr(exc, "code", None))
        ) from None
    except Exception:
        # Unexpected operational failure — never KeyboardInterrupt/
        # SystemExit (BaseException) and never raw details.
        raise SemanticQueryError(SEMANTIC_UNEXPECTED, _UNEXPECTED_ERROR) from None