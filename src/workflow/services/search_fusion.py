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

Library item mode (Step 6.3, ``item_scope``/``compiled_item_scope``):
the hybrid receives the unsliced one-column ``item_key`` UNION from
``workflow.query.library_item_key_queryset`` (or its precompiled
:class:`~workflow.services.search_query.CompiledItemScope`) INSTEAD of
the Recording ``scope`` (mutually exclusive, validated fail-closed
BEFORE any health/DB work with the engines' stable
``invalid_item_scope`` taxonomy). The UNION is compiled EXACTLY ONCE
through the SHARED ``search_query.compile_item_scope`` and that SAME
immutable value is handed to the keyword component
(``compiled_item_scope=``) AND stored verbatim on the semantic snapshot
(``item_scope=``) — never recompiled, never forked. Both components
then run their item-mode engines at the fixed depth 200, so every
result row already carries the additive
    ``item_key``/``item_kind``/``section_id`` identity. Fusion, dedup and
    the final tie-break key on the item identity (two Sections of one
    Recording are two fused rows; a merged row keeps the parent
     ``recording_id``), the fused ``more_recordings_matched`` counts fused
     ROWS (= items) and the payload additionally carries the SAME-VALUE
     ``more_items_matched`` beside the explicit ``item_mode: true`` flag
     (present ONLY in item mode, like ``more_items_matched``, so
     more-match wording counts Library items unconditionally — never
     derived from the presentation rows); presentation rows gain the
     additive identity fields. Component ``item_key`` values are STRICTLY canonicalized at
    the fusion boundary by the ONE helper :func:`_canonical_item_identity`
    (reused for grouping AND for the fused identity fields, so the two
    can never diverge): only the canonical ``r:<canonical UUID>`` and
    positive canonical ASCII-decimal ``s:<id>`` spellings are honoured,
    so a missing/blank/malformed/leading-zero/foreign-digit ``item_key``
    falls back to the canonical parent ``r:<recording_id>`` identity
    (never a crash, never a fabricated section id, never a trusted
    ad-hoc identity). Engine-produced keys are always canonical, so
    production fusion is byte-identical. The recording-mode sequence, payload,
one-sweep/one-traversal/one-embedding contract and the legacy per-
Recording fusion are unchanged; Ask (Step 5D) is untouched.

Everything is strictly read-only: SELECT/PRAGMA plus exactly one
localhost embedding request outside any transaction — no writes, no
pipeline lock, no rebuild/repair/sync, no logs, no caches, no retries,
no keyword-only fallback. Every public failure is sanitized: query
text, vector values, document keys, recording ids, SQL, indexed content
and secrets never appear in any message.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from brainlib.config import ConfigError
from workflow.services import search_index
from workflow.services import semantic_query as sq
from workflow.services.embedding_client import EmbeddingError
from workflow.services.search_query import (
    DEFAULT_RESULT_LIMIT,
    MAX_RESULT_LIMIT,
    CompiledItemScope,
    SearchQueryInputError,
    compile_item_scope,
    compile_scope,
    search_recordings,
    validate_query,
)
from workflow.services.semantic_query import (
    INVALID_ITEM_SCOPE,
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


# Fixed sanitized item-scope usage messages (Step 6.3). The failures
# themselves use the engines' stable ``invalid_item_scope`` taxonomy
# (SemanticQueryInputError) — these messages only.
_HYBRID_ITEM_SCOPE_AMBIGUOUS_ERROR = (
    "the hybrid item scope must be supplied either as an item_key UNION "
    "queryset or as a precompiled item scope, never both"
)
_HYBRID_SCOPE_ITEM_CONFLICT_ERROR = (
    "the hybrid scope must be either a Recording eligibility scope or a "
    "Library item scope, never both"
)
_HYBRID_COMPILED_ITEM_SCOPE_TYPE_ERROR = (
    "the compiled item scope must be a CompiledItemScope value produced by "
    "compile_item_scope"
)
_HYBRID_ITEM_SCOPE_ALIAS_ERROR = (
    "the compiled item scope was built for a different database connection"
)


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
    """One fused row (a Recording, or a Library item in item mode):
    rank evidence, the component result dicts (the keyword dict is used
    for presentation whenever present) and the item-identity fields.

    ``item_key``/``item_kind``/``section_id`` are the Step 6.3 fused
    identity (``reciprocal_rank_fusion`` always normalizes them from the
    component rows; ``r:<recording_id>`` fallback included). ``recording_id``
    is ALWAYS the parent Recording (a Section item keeps its provenance).
    Manually constructed legacy rows leave ``item_key`` empty and the
    ``identity`` property derives the canonical Recording fallback."""

    recording_id: str
    keyword_rank: int | None
    semantic_rank: int | None
    semantic_cosine: float | None
    rrf_score: float
    keyword: dict | None
    semantic: dict | None
    item_key: str = ""
    item_kind: str = "recording"
    section_id: int | None = None

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

    @property
    def identity(self) -> str:
        """The ONE fusion identity: the normalized item key, with the
        canonical Recording key as the legacy/manual-construction
        fallback (identical values whenever the engine supplied it)."""
        return self.item_key or f"r:{self.recording_id}"


# The strict fusion-boundary canonical spellings for a component
# ``item_key``:
#
# - ``r:<UUID>``: the canonical lowercase hyphenated UUID form the
#   engines emit (``Recording.pk`` is UUID-produced); equivalent-but-
#   non-canonical spellings (uppercase, hyphen-less, braced, URN) are
#   NOT honoured here;
# - ``s:<id>``: a POSITIVE CANONICAL ASCII-decimal id — no sign, no
#   leading zero, ASCII digits only (``[0-9]`` never a foreign digit
#   such as Arabic-Indic or a superscript two), bounded by the decimal
#   length of a 64-bit SQLite INTEGER primary key (which also keeps the
#   ``int`` conversion below free of CPython's int-str conversion cap).
_CANONICAL_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_CANONICAL_SECTION_ID_RE = re.compile(r"[1-9][0-9]{0,18}")


def _canonical_item_identity(item_key, recording_id) -> str | None:
    """The ONE strict fusion-boundary canonicalizer of one component
    row's Library item identity — reused for the grouping AND for the
    fused identity fields, so the two can never diverge.

    Only the two canonical engine spellings are honoured: the canonical
    lowercase ``r:<UUID>`` naming the row's OWN parent, and the positive
    canonical ASCII-decimal ``s:<id>``. Everything else — missing,
    blank, non-``str``, malformed, leading-zero, foreign-digit or
    wrong-parent (or parent-less-unverifiable) recording keys — falls
    back to the canonical parent ``r:<recording_id>`` identity: never a
    crash, never a fabricated section id, never a trusted ad-hoc
    identity. Engine-produced keys are always canonical, so production
    fusion is byte-identical. ``None`` only when the row carries no
    usable identity at all (such rows are skipped)."""
    if type(item_key) is str and item_key:
        prefix, separator, remainder = item_key.partition(":")
        if (
            separator
            and prefix == "s"
            and _CANONICAL_SECTION_ID_RE.fullmatch(remainder)
        ):
            return item_key
        if (
            separator
            and prefix == "r"
            and recording_id is not None
            and _CANONICAL_UUID_RE.fullmatch(remainder)
            and remainder == str(recording_id)
        ):
            return f"r:{recording_id}"
    if recording_id is None:
        return None
    return f"r:{recording_id}"


def _index_components(results: Sequence[Mapping]) -> tuple[dict, dict]:
    """First-occurrence-wins index of a component result list keyed by
    the fused identity through the ONE strict helper
    :func:`_canonical_item_identity`: returns
    ``(result_by_identity, rank_by_identity)``.
    Recording mode keys every row as ``r:<recording_id>`` (byte-identical
    grouping to the historical recording id key); item mode groups per
    canonical ``item_key``, so two Sections of one Recording are distinct
    entries and a non-canonical key groups under its parent instead.
    The 1-based rank is the row's own ``rank`` when present, else its
    list position (components always emit one-based ordered ranks)."""
    result_by_identity: dict = {}
    rank_by_identity: dict = {}
    for position, result in enumerate(results, start=1):
        identity = _canonical_item_identity(
            result.get("item_key"), result.get("recording_id")
        )
        if identity is None:
            continue
        if identity in result_by_identity:
            continue
        result_by_identity[identity] = result
        rank_by_identity[identity] = result.get("rank", position)
    return result_by_identity, rank_by_identity


def _semantic_cosine(result: Mapping | None):
    if result is None:
        return None
    return result.get("score")


def _fusion_key(item: FusedResult) -> tuple:
    """Deterministic fused order: RRF desc, presence count desc, minimum
    present rank, maximum present rank, canonical fused identity (the
    item key; ``r:<recording_id>`` in recording mode, so the historical
    recording-id tie-break is byte-identical there)."""
    return (
        -item.rrf_score,
        -item.presence,
        item.min_present_rank,
        item.max_present_rank,
        item.identity,
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
    result rows (each carrying its 1-based ``rank``, the parent
    ``recording_id`` and the Step 6.3 additive ``item_key``); fusion,
    dedup and the final tie-break key on the item IDENTITY — the STRICTLY
    canonical item key through :func:`_canonical_item_identity` when
    present (two Sections of one Recording are two fused rows), else the
    canonical parent ``r:<recording_id>`` fallback. Recording mode
    emits ``r:<recording_id>`` on every row, so grouping and ordering
    there are exactly the historical per-Recording behavior. Fusion is
    EXACT only over these returned lists — a unit absent from a
    component list is treated as absent from that component's returned
    depth, never as a corpus nonmatch. One fused row per identity, with
    the parent ``recording_id`` and the identity fields derived from
    that SAME canonical identity (a non-canonical key never survives
    the boundary, so a malformed key can never fabricate a section).
    PURE: no DB, no network, no health, no writes. Deterministic order:
    RRF desc, component presence count desc, minimum present rank,
    maximum present rank, canonical fused identity.
    """
    keyword_by_id, keyword_ranks = _index_components(keyword_results)
    semantic_by_id, semantic_ranks = _index_components(semantic_results)
    fused: list[FusedResult] = []
    for identity in sorted(set(keyword_ranks) | set(semantic_ranks)):
        krank = keyword_ranks.get(identity)
        srank = semantic_ranks.get(identity)
        keyword_row = keyword_by_id.get(identity)
        semantic_row = semantic_by_id.get(identity)
        parent_row = keyword_row if keyword_row is not None else semantic_row
        # The identity is canonical BY CONSTRUCTION (the ONE strict
        # boundary helper above), so this is an exact mapping of the
        # canonical spelling, not a revalidation: a canonical ``s:``
        # key is a section with a round-tripping ASCII-decimal id;
        # every other canonical identity is a recording item.
        if identity.startswith("s:"):
            item_kind = "section"
            section_id: int | None = int(identity[2:])
        else:
            item_kind = "recording"
            section_id = None
        fused.append(
            FusedResult(
                recording_id=parent_row.get("recording_id"),
                keyword_rank=krank,
                semantic_rank=srank,
                semantic_cosine=_semantic_cosine(semantic_row),
                rrf_score=rrf_score(krank, srank, k=k),
                keyword=keyword_row,
                semantic=semantic_row,
                item_key=identity,
                item_kind=item_kind,
                section_id=section_id,
            )
        )
    fused.sort(key=_fusion_key)
    return fused


# ---------------------------------------------------------------------------
# Presentation assembly
# ---------------------------------------------------------------------------


def _present_results(
    fused: Sequence[FusedResult], *, limit: int, item_mode: bool = False
) -> list[dict]:
    """Keyword-first presentation: whenever keyword evidence exists use
    the keyword title/match/snippet (highlights preserved even when the
    semantic rank is stronger); otherwise the semantic fields. Every row
    carries the fused rank and the evidence block. Outside item mode the
    row shape is byte-identical to the historical hybrid contract; in
    item mode every row additionally carries the fused item identity
    ``item_key``/``item_kind``/``section_id`` next to the retained
    parent ``recording_id``."""
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
        row = {
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
        if item_mode:
            row["item_key"] = item.identity
            row["item_kind"] = item.item_kind
            row["section_id"] = item.section_id
        results.append(row)
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
    item_mode: bool = False,
) -> dict:
    """Fuse the two depth-200 component payloads into the final hybrid
    payload. ``truncated`` is true if either component says truncated.
    ``more_recordings_matched`` is the exact ``len(fused) - final_count``
    ONLY when BOTH component populations are proved complete
    (``more_recordings_matched == 0`` and no unknown), otherwise null —
    the explicit component metadata never implies a corpus-wide claim.

    ``item_mode`` (Step 6.3) marks the fused rows as LIBRARY items (the
    components' own same-value ``more_recordings_matched`` alias already
    carries the item truth, which the completeness gate reads unchanged):
    the payload then additionally carries the item-neutral
    ``more_items_matched`` with the SAME value (the historical
    Recording key stays as the compatibility alias), the simple
    ``item_mode: true`` flag (the engines' own contract: the fused unit
    IS a Library item in item mode, so presentation counts Library items
    unconditionally, never inferring the unit from the presentation
    rows), and the presentation rows gain the additive identity fields.
    Outside item mode the payload is byte-identical to the historical
    hybrid contract."""
    fused = reciprocal_rank_fusion(
        keyword_payload["results"], semantic_payload["results"]
    )
    results = _present_results(fused, limit=limit, item_mode=item_mode)
    keyword_more = keyword_payload["more_recordings_matched"]
    semantic_more = semantic_payload["more_recordings_matched"]
    if keyword_more == 0 and semantic_more == 0:
        more_recordings_matched = max(len(fused) - len(results), 0)
    else:
        more_recordings_matched = None
    truncated = bool(keyword_payload["truncated"] or semantic_payload["truncated"])
    payload = {
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
        # Fused-row (= unit) count: Recordings in recording mode,
        # Library items in item mode. Historical key unchanged; the
        # item-neutral alias is added ONLY in item mode with the SAME
        # value (the engines' own compatibility-alias contract).
        "more_recordings_matched": more_recordings_matched,
        "components": {
            "keyword": _component_metadata(keyword_payload),
            "semantic": _component_metadata(semantic_payload),
        },
    }
    if item_mode:
        payload["more_items_matched"] = more_recordings_matched
        # The explicit item-mode flag (the engines' own contract): the
        # fused unit IS a Library item, so presentation counts Library
        # items unconditionally.
        payload["item_mode"] = True
    return payload


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


def _validate_hybrid_item_usage(*, scope, item_scope, compiled_item_scope, using):
    """Cheap pure item-scope usage validation for :func:`hybrid_search`
    (Step 6.3), enforced BEFORE any health/DB/network work with the
    engines' stable ``invalid_item_scope`` taxonomy (fixed sanitized
    messages; never an implicit widening): the two item carriers are
    mutually exclusive with each other AND with the Recording
    ``scope``; a precompiled value must be EXACTLY a ``CompiledItemScope``
    built for the SAME database alias."""
    if item_scope is not None and compiled_item_scope is not None:
        raise SemanticQueryInputError(
            INVALID_ITEM_SCOPE, _HYBRID_ITEM_SCOPE_AMBIGUOUS_ERROR
        )
    if scope is not None and (
        item_scope is not None or compiled_item_scope is not None
    ):
        raise SemanticQueryInputError(
            INVALID_ITEM_SCOPE, _HYBRID_SCOPE_ITEM_CONFLICT_ERROR
        )
    if compiled_item_scope is not None:
        if type(compiled_item_scope) is not CompiledItemScope:
            raise SemanticQueryInputError(
                INVALID_ITEM_SCOPE, _HYBRID_COMPILED_ITEM_SCOPE_TYPE_ERROR
            )
        if compiled_item_scope.using != using:
            raise SemanticQueryInputError(
                INVALID_ITEM_SCOPE, _HYBRID_ITEM_SCOPE_ALIAS_ERROR
            )


def hybrid_search(
    raw,
    *,
    limit: int = DEFAULT_RESULT_LIMIT,
    using: str = "default",
    scope=None,
    config=None,
    embedder=None,
    item_scope=None,
    compiled_item_scope: CompiledItemScope | None = None,
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

    ``item_scope`` / ``compiled_item_scope`` (Step 6.3) are the
    LIBRARY-ITEM-mode alternative to ``scope``: the unsliced one-column
    ``item_key`` UNION from ``workflow.query.library_item_key_queryset``
    (or its precompiled
    :class:`~workflow.services.search_query.CompiledItemScope`, consumed
    VERBATIM — never recompiled). Usage is validated BEFORE any
    health/DB/network work (mutually exclusive with each other AND with
    ``scope``; fixed sanitized ``invalid_item_scope`` failures). The
    UNION is compiled EXACTLY ONCE via the SHARED
    ``search_query.compile_item_scope`` and that SAME immutable value is
    shared with BOTH components: passed to the keyword engine as
    ``compiled_item_scope=`` and stored on the semantic snapshot as
    ``item_scope=`` (never recompiled, never forked). Both engines then
    run their item mode at the fixed depth 200 and every component row
    carries the additive item identity; fusion, dedup and the fused
    tie-break key on the item key (a valid active split layout yields
    EXACTLY the active Section items with the parent Recording
    SUPPRESSED, never duplicated), every fused row retains the parent
    ``recording_id`` plus ``item_kind``/``section_id``, and the payload
    counts fused items with the SAME-VALUE ``more_items_matched`` alias
    beside the historical ``more_recordings_matched``. The one-sweep /
    one-integrity-traversal / one-embedding / bounded-page and empty-
    scope-zero-embed contracts are unchanged in item mode.
    """
    normalized = _validate_hybrid_input(raw, limit)
    _validate_hybrid_item_usage(
        scope=scope,
        item_scope=item_scope,
        compiled_item_scope=compiled_item_scope,
        using=using,
    )
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
        # EXACTLY ONE compilation of the Library item scope (skipped
        # entirely when a precompiled value is supplied); the SAME
        # immutable value feeds the query-embed emptiness check, the
        # keyword component and the semantic snapshot below.
        if compiled_item_scope is not None:
            compiled_items = compiled_item_scope
        elif item_scope is not None:
            compiled_items = compile_item_scope(item_scope, using=using)
        else:
            compiled_items = None
        item_mode = compiled_items is not None
        item_scope_sql = compiled_items.sql if item_mode else None
        item_scope_params = list(compiled_items.params) if item_mode else None
        query_vector = embed_query_vector(
            normalized,
            active=active,
            scope_sql=scope_sql,
            scope_params=scope_params,
            using=using,
            config=config,
            embedder=embedder,
            item_scope_sql=item_scope_sql,
            item_scope_params=item_scope_params,
        )
        snapshot = SemanticSnapshot(
            normalized=normalized,
            active=active,
            scope=compiled_scope,
            query_vector=query_vector,
            data_version_before=data_version_before,
            item_scope=compiled_items,
        )
        keyword_payload = search_recordings(
            normalized,
            limit=HYBRID_DEPTH,
            using=using,
            compiled_scope=compiled_scope,
            compiled_item_scope=compiled_items,
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
            item_mode=item_mode,
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