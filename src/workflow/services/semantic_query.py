"""Semantic-query contracts, bounded grouped top-K and the scoped
read-only semantic engine (Step 5C).

The first half of this module is deliberately PURE (no database
traversal, no health sweep, no network/embedding call, no hybrid fusion,
no CLI/web wiring and no migration):

- query input validation (``normalize_semantic_query`` /
  ``validate_semantic_query``) with the SAME plain-text, NFC-normalized,
  outer-stripped contract as keyword search, but WITHOUT the keyword
  eight-term cap (a semantic query is one embedding payload, not a term
  list);
- ``prepare_query_text`` — the query-side text mapping for the query
  mapping version ``SEMANTIC_QUERY_VERSION``. For v1 it returns the
  exact normalized query unchanged (no prefix, no truncation, no
  coercion). The query mapping version is SEPARATE from the document
  mapping version ``embedding_index.EMBEDDING_VERSION``; changing either
  side's text contract never silently changes the other;
- ``cosine_similarity`` — a numerically safe (``math.hypot`` /
  ``math.fsum``) cosine over finite decoded vectors of exact matching
  dimensions. A zero query norm is a fixed ``invalid_query_vector``
  failure; a zero document norm is a fixed ``invalid_document_vector``
  failure (fail closed — a zero-norm vector is never skipped or
  approximated). For finite float32-decoded inputs a non-finite score is
  impossible (norms stay at most ~4.3e40 even at MAX_DIMENSION), but any
  pathological arithmetic failure or non-finite norm/score still maps to
  the role-appropriate fixed sanitized error — never a raw math
  exception and never a leaked value;
- ``select_semantic_winners`` — the exact bounded grouped per-recording
  top-K primitive. Input candidates MUST be deterministically ordered by
  ``(recording_id, document_key)``; the accumulator finishes ONE
  recording's best document (per-recording comparator) before inserting
  ONE winner into the global heap of at most ``limit`` (<= 200). It never
  uses a global stale-entry heap while documents for the same recording
  remain unfinished. Winners are provenance-metadata-only
  (:class:`SemanticMatch`): the heap and the returned results never
  retain vectors, so memory is bounded by the current input/group vector
  plus K metadata winners — never K vectors.

The second half is the PUBLIC semantic engine (read-only, strict
SELECT/PRAGMA + exactly one localhost embedding request, no lock, no
writes, no rebuild/repair/sync, no logs):

- ``semantic_search`` — the full pipeline: one source-health sweep
  (``search_index.build_status_report``) exactly once, schema/model/
  active-generation identity validation, ``PRAGMA data_version``
  concurrency guards before/after, exactly one query embedding request
  (only when at least one in-scope document exists), ONE complete
  global active-generation integrity traversal with in-scope brute-force
  scoring in deterministic ``(recording_id, document_key)`` order, and a
  final data_version + active-identity re-read. Empty valid scope →
  zero embedding calls and a successful zero-result payload (global
  integrity is still validated).
- ``semantic_rank`` — the reusable validated snapshot/result entry point
  for later hybrid orchestration: accepts an already-embedded normalized
  query vector and scope, never triggers the source health sweep itself,
  and returns the same payload plus complete active-generation identity
  metadata.

Integrity and scoring share ONE vector traversal (reusing the shared
5B.3 length-first decode helper ``embedding_index._classify_active_page``,
so a corpus is never blob-decoded twice): the traversal validates
missing/stale/orphan/malformed/zero vectors across the COMPLETE global
set (in-scope AND out-of-scope defects fail closed with a stable
sanitized error and no partial results) while only IN-SCOPE documents
are scored. Scope is the existing unsliced ``Recording`` QuerySet
contract compiled by ``search_query._compile_scope`` (semantics are
never forked); it applies before ranking and out-of-scope rows can never
win.

Everything here is read-only, deterministic and content-free in its
errors: query text, vector values, document keys, recording ids, SQL and
indexed content never appear in any message. Every public failure is a
:class:`SemanticQueryError` (a :class:`~brainlib.config.ConfigError`
subclass) carrying a stable ``code`` and a fixed static message.
"""

from __future__ import annotations

import heapq
import math
import unicodedata
from dataclasses import dataclass

from django.db import connections
from django.db.models import Q

from brainlib.config import ConfigError
from workflow.models import (
    EmbeddingDocument,
    EmbeddingGeneration,
    EmbeddingGenerationState,
    SearchDocument,
)
from workflow.services import search_index
from workflow.services.embedding_client import EmbeddingError
from workflow.services.embedding_index import (
    EMBEDDING_VERSION,
    _classify_active_page,
    _embedding_schema_present,
    _pragma_data_version,
    _request_error,
)

# Semantic policy values are NOT copied: the result cap, snippet cap,
# query codepoint cap and default limit are the keyword-search values
# (the semantic layer must stay compatible with them) and are imported
# from their ONE runtime home. The provenance shape and the scope
# compiler are also reused verbatim (never forked).
from workflow.services.search_query import (
    DEFAULT_RESULT_LIMIT,
    ELLIPSIS,
    MAX_QUERY_CODEPOINTS,
    MAX_RESULT_LIMIT,
    SNIPPET_MAX_CODEPOINTS,
    CompiledScope,
    _compile_scope,
    _lookup_titles,
    _provenance,
    compile_scope,
)

# The QUERY-side mapping contract version. It versions the query text
# preparation (``prepare_query_text``) and is intentionally SEPARATE from
# the DOCUMENT-side ``embedding_index.EMBEDDING_VERSION``: a change to
# one side never silently changes the other. For v1 the query payload is
# exactly the normalized query text.
SEMANTIC_QUERY_VERSION = "1"

# Semantic policy, named clearly, sourced from the keyword-search bounds.
SEMANTIC_RESULT_LIMIT_MAX = MAX_RESULT_LIMIT  # 200
SEMANTIC_DEFAULT_RESULT_LIMIT = DEFAULT_RESULT_LIMIT
SEMANTIC_MAX_QUERY_CODEPOINTS = MAX_QUERY_CODEPOINTS  # 256
SEMANTIC_SNIPPET_MAX_CODEPOINTS = SNIPPET_MAX_CODEPOINTS  # 320

# Stable sanitized error codes (never renamed silently).
INVALID_QUERY = "invalid_query"
INVALID_LIMIT = "invalid_limit"
INVALID_CANDIDATE = "invalid_candidate"
CANDIDATE_ORDER = "candidate_order"
INVALID_QUERY_VECTOR = "invalid_query_vector"
INVALID_DOCUMENT_VECTOR = "invalid_document_vector"
DIMENSION_MISMATCH = "dimension_mismatch"

# Fixed sanitized messages — never interpolate query text, vector values,
# document keys, recording ids, indexed content, SQL or paths.
_EMPTY_QUERY_ERROR = "the semantic query must not be empty"
_QUERY_TYPE_ERROR = "the semantic query must be a string"
_TOO_LONG_ERROR = (
    f"the semantic query must be at most {SEMANTIC_MAX_QUERY_CODEPOINTS} characters"
)
_QUERY_TEXT_ERROR = "the semantic query text is not a normalized semantic query"
_BAD_LIMIT_ERROR = (
    f"the result limit must be an integer between 1 and {SEMANTIC_RESULT_LIMIT_MAX}"
)
_CANDIDATE_ERROR = "a semantic candidate is malformed"
_CANDIDATE_ORDER_ERROR = (
    "semantic candidates must be ordered by (recording_id, document_key)"
)
_INVALID_QUERY_VECTOR_ERROR = "the semantic query vector is invalid"
_INVALID_DOCUMENT_VECTOR_ERROR = "a semantic document vector is invalid"
_DIMENSION_MISMATCH_ERROR = "semantic vector dimensions do not match"

# ---------------------------------------------------------------------------
# Engine-level stable codes (never renamed silently). These belong to the
# scoped semantic engine below; the pure primitives above keep the codes
# INVALID_QUERY / INVALID_LIMIT / INVALID_CANDIDATE / CANDIDATE_ORDER /
# INVALID_QUERY_VECTOR / INVALID_DOCUMENT_VECTOR / DIMENSION_MISMATCH.
# ---------------------------------------------------------------------------

SEMANTIC_SOURCE_UNHEALTHY = "source_index_unhealthy"
SEMANTIC_EMBEDDING_SCHEMA_MISSING = "embedding_schema_missing"
SEMANTIC_MODEL_NOT_CONFIGURED = "model_not_configured"
SEMANTIC_NO_ACTIVE_GENERATION = "no_active_generation"
SEMANTIC_INCOMPATIBLE_GENERATION = "incompatible_generation"
SEMANTIC_CONCURRENT_CHANGE = "concurrent_change"
SEMANTIC_INDEX_INTEGRITY = "embedding_index_integrity"
SEMANTIC_EMBEDDING_FAILED = "embedding_request_failed"
SEMANTIC_EMBEDDING_PAIRING = "embedding_response_invalid"
SEMANTIC_IN_TRANSACTION = "semantic_query_in_transaction"
SEMANTIC_UNEXPECTED = "semantic_query_failed"

# Engine policy: exhaustive scan (never truncated) with fixed pages and
# excerpt bounds. Excerpt content is capped at the keyword snippet cap;
# the SELECT window is cap+1 so truncation is detectable without
# loading unbounded TextFields.
SEMANTIC_PAGE_SIZE = 500
EXCERPT_MAX_CODEPOINTS = SNIPPET_MAX_CODEPOINTS  # 320
_EXCERPT_SELECT_CODEPOINTS = EXCERPT_MAX_CODEPOINTS + 1  # 321

# Fixed sanitized engine messages — never interpolate query text, vector
# values, document keys, recording ids, SQL, paths, indexed content or
# raw exception details.
_SOURCE_UNHEALTHY_ERROR = (
    "the search index is not healthy; run 'brain search-index status' "
    "and repair it with 'brain search-index rebuild' first"
)
_EMBEDDING_SCHEMA_MISSING_ERROR = (
    "the embedding index schema is missing; apply pending migrations with: "
    "uv run python src/manage.py migrate"
)
_MODEL_NOT_CONFIGURED_ERROR = (
    "no embedding model is configured; set the 'embedding.model' configuration value"
)
_NO_ACTIVE_GENERATION_ERROR = (
    "no active embedding generation exists; run 'brain embedding-index rebuild'"
)
_INCOMPATIBLE_GENERATION_ERROR = (
    "the active embedding generation is incompatible with the configured model or "
    "versions; run 'brain embedding-index rebuild'"
)
_CONCURRENT_CHANGE_ERROR = (
    "the embedding index changed during the semantic search; try again"
)
_MISSING_DOCUMENT_ERROR = (
    "the embedding index is missing documents; run 'brain embedding-index status' "
    "and repair with 'brain embedding-index repair'"
)
_STALE_CONTENT_ERROR = (
    "the embedding index has stale content; run 'brain embedding-index status' "
    "and repair with 'brain embedding-index repair'"
)
_ORPHAN_DOCUMENT_ERROR = (
    "the embedding index has orphan vectors; run 'brain embedding-index status' "
    "and repair with 'brain embedding-index repair'"
)
_INVALID_VECTOR_ERROR = (
    "the embedding index contains an invalid vector; run 'brain embedding-index status' "
    "and repair with 'brain embedding-index repair'"
)
_QUERY_PAIRING_ERROR = "the embedding response does not match the semantic query"
_IN_TRANSACTION_ERROR = (
    "semantic requests must not be made inside a database transaction"
)
_UNEXPECTED_ERROR = (
    "the semantic search could not be completed; inspect with: "
    "brain embedding-index status"
)


class SemanticQueryError(ConfigError):
    """Sanitized semantic-query failure (CLI exit 1).

    ``code`` is a stable category and the message is a fixed static
    string; neither ever contains query text, vector values, document
    keys, recording ids or indexed content.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class SemanticQueryInputError(SemanticQueryError):
    """Malformed or over-cap semantic query input."""


# ---------------------------------------------------------------------------
# Query input (same plain-text contract as keyword search, no term cap)
# ---------------------------------------------------------------------------


def normalize_semantic_query(raw: str | None) -> str:
    """NFC-normalize and outer-strip a user query, enforcing the stable
    semantic input bounds. ``None`` is the empty-query case (consistent
    with keyword search); any non-string is rejected. Returns the
    canonical query or raises ``SemanticQueryInputError`` (never echoes
    the offending text). The keyword eight-term cap is intentionally NOT
    applied: a semantic query is one embedding payload."""
    if raw is None:
        raise SemanticQueryInputError(INVALID_QUERY, _EMPTY_QUERY_ERROR)
    if type(raw) is not str:
        raise SemanticQueryInputError(INVALID_QUERY, _QUERY_TYPE_ERROR)
    normalized = unicodedata.normalize("NFC", raw).strip()
    if not normalized:
        raise SemanticQueryInputError(INVALID_QUERY, _EMPTY_QUERY_ERROR)
    if len(normalized) > SEMANTIC_MAX_QUERY_CODEPOINTS:
        raise SemanticQueryInputError(INVALID_QUERY, _TOO_LONG_ERROR)
    return normalized


def validate_semantic_limit(limit) -> None:
    """Validate the result limit: an exact ``int`` (``bool`` rejected)
    between 1 and :data:`SEMANTIC_RESULT_LIMIT_MAX` (200)."""
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= SEMANTIC_RESULT_LIMIT_MAX
    ):
        raise SemanticQueryInputError(INVALID_LIMIT, _BAD_LIMIT_ERROR)


def validate_semantic_query(
    raw: str | None, limit: int = SEMANTIC_DEFAULT_RESULT_LIMIT
) -> str:
    """ALL cheap semantic user-input validation in one entry point
    (empty/over-long/over-cap limit). Deliberately applies NO term-count
    cap, so any number of whitespace-separated words is accepted as one
    semantic query. Returns the normalized query."""
    normalized = normalize_semantic_query(raw)
    validate_semantic_limit(limit)
    return normalized


def prepare_query_text(normalized_query: str) -> str:
    """Return the exact query embedding payload for v1.

    Accepts ONLY an exact built-in ``str`` that is ALREADY a normalized
    query — i.e. exactly equal to :func:`normalize_semantic_query` of
    itself (NFC, outer-stripped, nonblank, within the codepoint cap).
    Anything else — leading/trailing whitespace, non-NFC text, an
    over-cap or blank string, or a non-``str`` — raises the fixed
    sanitized invalid-query error. This helper NEVER silently normalizes:
    the payload is returned UNCHANGED (no prefix, no truncation, no
    coercion). The payload is bound to :data:`SEMANTIC_QUERY_VERSION`,
    which is separate from the document ``EMBEDDING_VERSION``; any future
    change to the query mapping must bump ``SEMANTIC_QUERY_VERSION``.
    """
    if type(normalized_query) is not str:
        raise SemanticQueryInputError(INVALID_QUERY, _QUERY_TEXT_ERROR)
    if (
        not normalized_query.strip()
        or len(normalized_query) > SEMANTIC_MAX_QUERY_CODEPOINTS
        or normalized_query
        != unicodedata.normalize("NFC", normalized_query).strip()
    ):
        raise SemanticQueryInputError(INVALID_QUERY, _QUERY_TEXT_ERROR)
    return normalized_query


# ---------------------------------------------------------------------------
# Vector validation + cosine (pure, numerically safe)
# ---------------------------------------------------------------------------


def _validated_vector(values, *, code: str, message: str) -> tuple[float, ...]:
    """Validate a decoded vector: exact ``list``/``tuple`` of exact
    ``int``/``float`` (``bool`` and subclasses rejected), nonempty and
    finite. Returns a tuple of floats. Every failure is the fixed
    sanitized error for the caller's role; the offending value is never
    echoed."""
    if type(values) is not list and type(values) is not tuple:
        raise SemanticQueryError(code, message)
    if not values:
        raise SemanticQueryError(code, message)
    coerced: list[float] = []
    for value in values:
        if isinstance(value, bool) or type(value) not in (int, float):
            raise SemanticQueryError(code, message)
        if type(value) is int:
            try:
                value = float(value)
            except OverflowError:
                raise SemanticQueryError(code, message) from None
        if value != value or math.isinf(value):
            raise SemanticQueryError(code, message)
        coerced.append(value)
    return tuple(coerced)


def _prepare_query_vector(query_vector) -> tuple[tuple[float, ...], float]:
    """Validate the query vector and return ``(values, norm)``. A zero or
    non-finite query norm is the fixed ``invalid_query_vector`` failure
    (a pathological huge-but-finite input can overflow ``math.hypot``)."""
    values = _validated_vector(
        query_vector, code=INVALID_QUERY_VECTOR, message=_INVALID_QUERY_VECTOR_ERROR
    )
    norm = math.hypot(*values)
    if norm == 0.0 or not math.isfinite(norm):
        raise SemanticQueryError(INVALID_QUERY_VECTOR, _INVALID_QUERY_VECTOR_ERROR)
    return values, norm


def _cosine_against_query(
    query: tuple[float, ...], query_norm: float, document_vector
) -> float:
    document = _validated_vector(
        document_vector,
        code=INVALID_DOCUMENT_VECTOR,
        message=_INVALID_DOCUMENT_VECTOR_ERROR,
    )
    if len(query) != len(document):
        raise SemanticQueryError(DIMENSION_MISMATCH, _DIMENSION_MISMATCH_ERROR)
    document_norm = math.hypot(*document)
    if document_norm == 0.0 or not math.isfinite(document_norm):
        raise SemanticQueryError(
            INVALID_DOCUMENT_VECTOR, _INVALID_DOCUMENT_VECTOR_ERROR
        )
    # Cosine computed as the dot product of the UNIT vectors: every
    # product term has magnitude <= 1, so no intermediate can overflow or
    # underflow into a raw inf/nan for finite inputs — and for the actual
    # float32-decoded contract a non-finite score is impossible (norms
    # stay at most ~4.3e40 even at MAX_DIMENSION). ``math.fsum`` keeps the
    # sum exactly rounded; ``math.hypot`` avoids under/overflow computing
    # the norms. The final finiteness guard is belt-and-suspenders: any
    # arithmetic failure still maps to the fixed sanitized document-vector
    # error instead of leaking a raw math exception or a non-finite score.
    score = math.fsum(
        (a / query_norm) * (b / document_norm) for a, b in zip(query, document)
    )
    if not math.isfinite(score):
        raise SemanticQueryError(
            INVALID_DOCUMENT_VECTOR, _INVALID_DOCUMENT_VECTOR_ERROR
        )
    return score


def cosine_similarity(query_vector, document_vector) -> float:
    """Numerically safe cosine over finite decoded vectors.

    Both vectors must be exact ``list``/``tuple`` of finite exact
    ``int``/``float`` values (``bool`` rejected) of the SAME nonempty
    dimension. A zero query norm raises the fixed
    ``invalid_query_vector`` error; a zero document norm raises the fixed
    ``invalid_document_vector`` error (fail closed — never skipped or
    approximated); non-finite/type/cardinality failures raise the
    role-specific fixed sanitized error and a dimension mismatch raises
    ``dimension_mismatch``. No value is ever echoed.
    """
    query, query_norm = _prepare_query_vector(query_vector)
    return _cosine_against_query(query, query_norm, document_vector)


# ---------------------------------------------------------------------------
# Grouped per-recording top-K primitive
# ---------------------------------------------------------------------------

# Per-recording comparator doc-type rank: summary < recording < segment.
# These are the ONLY doc types the SearchDocument contract produces
# (``SearchDocType``), and the pure boundary rejects anything else.
_DOC_TYPE_RANKS = {"summary": 0, "recording": 1, "segment": 2}
_KNOWN_DOC_TYPES = frozenset(_DOC_TYPE_RANKS)
_UNKNOWN_DOC_TYPE_RANK = 3


@dataclass(frozen=True)
class SemanticCandidate:
    """One decoded semantic candidate, ordered by ``(recording_id,
    document_key)`` in the input stream.

    The INPUT carrier: the caller supplies the decoded ``vector`` plus
    the identity/provenance columns a later DB integration needs. It is
    never returned and never stored in the heap — winners are converted
    to the provenance-only :class:`SemanticMatch` when a Recording group
    finishes, so vectors go out of scope with the input stream.

    Provenance contracts match the repository models: ``recording_id``
    is the Recording UUID string, ``transcript_id`` the Transcript
    BigAutoField positive integer, and ``summary_id`` the Summary
    ``CharField(36)`` primary key (UUID-producing default, but manually
    supplied or legacy nonempty strings are valid and pass through
    unchanged). ``None`` means the column is absent for the doc type.
    """

    recording_id: str
    document_key: str
    doc_type: str
    vector: tuple
    transcript_id: int | None = None
    summary_id: str | None = None
    segment_ordinal: int | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    output_language: str = ""


@dataclass(frozen=True)
class SemanticMatch:
    """Provenance-only winner payload — NEVER carries the vector.

    Built from a finished :class:`SemanticCandidate` at the moment its
    Recording group ends; the global heap and the returned winners hold
    ONLY these metadata matches, so at most one input/group vector is
    ever live alongside the K metadata winners (never K vectors).
    """

    recording_id: str
    document_key: str
    doc_type: str
    transcript_id: int | None = None
    summary_id: str | None = None
    segment_ordinal: int | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    output_language: str = ""


@dataclass(frozen=True)
class SemanticWinner:
    """The best provenance match of one Recording, in global order."""

    rank: int
    score: float
    match: SemanticMatch


def _match_from_candidate(candidate: SemanticCandidate) -> SemanticMatch:
    """Drop the vector: convert a finished candidate to the
    provenance-only match the heap and the results retain."""
    return SemanticMatch(
        recording_id=candidate.recording_id,
        document_key=candidate.document_key,
        doc_type=candidate.doc_type,
        transcript_id=candidate.transcript_id,
        summary_id=candidate.summary_id,
        segment_ordinal=candidate.segment_ordinal,
        start_ms=candidate.start_ms,
        end_ms=candidate.end_ms,
        output_language=candidate.output_language,
    )


def _valid_transcript_id(value) -> bool:
    """Present Transcript PKs must be exact positive ints (never bools,
    never <= 0, never strings); ``None`` means the column is absent for
    this doc type and is always valid."""
    if value is None:
        return True
    return not isinstance(value, bool) and isinstance(value, int) and value >= 1


def _valid_summary_id(value) -> bool:
    """Present Summary PKs must be exact NONEMPTY built-in ``str`` values.

    The Summary model uses ``CharField(primary_key=True, max_length=36,
    default=_uuid)``, but the DB/application contract does NOT constrain
    manually supplied or legacy primary keys to canonical UUID syntax —
    so no UUID parse or canonical-form check is applied and the exact
    value passes through provenance unchanged (no coercion). ``None``
    means the column is absent and is always valid. The offending value
    is never echoed.
    """
    if value is None:
        return True
    return type(value) is str and bool(value)


def _valid_segment_ordinal(value) -> bool:
    """Present segment ordinals must be exact nonnegative ints (never
    bools, never negative, never strings); ``None`` is valid."""
    if value is None:
        return True
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _valid_int_provenance(value) -> bool:
    """Present integer provenance (``start_ms``/``end_ms``) must be exact
    ints (never bools, never strings); ``None`` is valid."""
    if value is None:
        return True
    return not isinstance(value, bool) and isinstance(value, int)


def _rank_key(score: float, candidate: SemanticCandidate) -> tuple:
    """The ONE comparator: cosine descending, doc-type rank
    summary<recording<segment, document_key, recording id. A smaller
    tuple is better."""
    return (
        -score,
        _DOC_TYPE_RANKS.get(candidate.doc_type, _UNKNOWN_DOC_TYPE_RANK),
        candidate.document_key,
        candidate.recording_id,
    )


class _WorstFirst:
    """Heap entry whose ordering is REVERSED, so the heap root is the
    WORST (largest) comparator key and can be evicted when a better
    recording winner arrives. Exact ties never occur (recording ids are
    unique), and equal keys compare as not-less. Holds a
    provenance-only :class:`SemanticMatch` — NEVER the vector."""

    __slots__ = ("key", "score", "match")

    def __init__(self, key: tuple, score: float, match: SemanticMatch) -> None:
        self.key = key
        self.score = score
        self.match = match

    def __lt__(self, other: "_WorstFirst") -> bool:
        return self.key > other.key


def _offer(heap: list, group_best, limit: int) -> None:
    """Insert ONE finished Recording winner into the global heap of max
    ``limit``. Called only after every document of that Recording has
    been seen (never a stale-entry heap). The winner is converted to a
    provenance-only :class:`SemanticMatch` here, so the input candidate's
    vector goes out of scope with the group."""
    if group_best is None:
        return
    key, score, candidate = group_best
    heapq.heappush(heap, _WorstFirst(key, score, _match_from_candidate(candidate)))
    if len(heap) > limit:
        heapq.heappop(heap)


def select_semantic_winners(
    query_vector,
    candidates,
    *,
    limit: int = SEMANTIC_DEFAULT_RESULT_LIMIT,
) -> list[SemanticWinner]:
    """Return the global top-``limit`` per-Recording semantic winners.

    ``candidates`` MUST be deterministically ordered by ``(recording_id,
    document_key)``: within one Recording, ``document_key`` must strictly
    increase, and a new Recording's ``recording_id`` must strictly exceed
    every previous one (a decrease or a reappearing Recording is a fixed
    sanitized ``candidate_order`` failure). Any malformed candidate is a
    fixed sanitized ``invalid_candidate`` failure. The pure boundary
    validates every provided field: ``recording_id``/``document_key``/
    ``doc_type``/``output_language`` must be exact built-in ``str``,
    ``doc_type`` must be one of the known SearchDocument types
    (``summary``/``recording``/``segment``), present ``transcript_id``
    must be an exact positive int, present ``summary_id`` an exact
    nonempty built-in string (no UUID-syntax check — legacy or custom
    values pass through unchanged), present ``segment_ordinal`` an exact
    nonnegative int, and present ``start_ms``/``end_ms`` exact ints
    (``bool`` and str-subclass values are always rejected); ``None`` is
    valid for every optional column. No value is ever echoed.

    For each contiguous Recording group the best document is selected
    with :func:`_rank_key`; only AFTER the group is finished is its single
    winner converted to a provenance-only :class:`SemanticMatch` and
    offered to the global heap of at most ``limit`` (1..200). Memory is
    bounded by the current input/group vector plus the K metadata
    winners — never K vectors. Results are returned best-first with
    deterministic 1-based ranks. A zero/non-finite query norm fails
    ``invalid_query_vector`` up front; a zero/non-finite document norm
    fails ``invalid_document_vector`` (fail closed).
    """
    validate_semantic_limit(limit)
    query, query_norm = _prepare_query_vector(query_vector)

    heap: list[_WorstFirst] = []
    last_recording: str | None = None
    last_document_key: str | None = None
    group_best: tuple | None = None

    for candidate in candidates:
        if type(candidate) is not SemanticCandidate:
            raise SemanticQueryError(INVALID_CANDIDATE, _CANDIDATE_ERROR)
        recording_id = candidate.recording_id
        document_key = candidate.document_key
        if (
            type(recording_id) is not str
            or type(document_key) is not str
            or type(candidate.doc_type) is not str
            or candidate.doc_type not in _KNOWN_DOC_TYPES
            or type(candidate.output_language) is not str
            or not _valid_transcript_id(candidate.transcript_id)
            or not _valid_summary_id(candidate.summary_id)
            or not _valid_segment_ordinal(candidate.segment_ordinal)
            or not _valid_int_provenance(candidate.start_ms)
            or not _valid_int_provenance(candidate.end_ms)
        ):
            raise SemanticQueryError(INVALID_CANDIDATE, _CANDIDATE_ERROR)

        if last_recording is not None:
            if recording_id == last_recording:
                if document_key <= last_document_key:
                    raise SemanticQueryError(
                        CANDIDATE_ORDER, _CANDIDATE_ORDER_ERROR
                    )
            else:
                # A recording may never reappear and ids never decrease.
                if recording_id < last_recording:
                    raise SemanticQueryError(
                        CANDIDATE_ORDER, _CANDIDATE_ORDER_ERROR
                    )
                _offer(heap, group_best, limit)
                group_best = None
                last_document_key = None

        last_recording = recording_id
        last_document_key = document_key

        score = _cosine_against_query(query, query_norm, candidate.vector)
        key = _rank_key(score, candidate)
        if group_best is None or key < group_best[0]:
            group_best = (key, score, candidate)

    _offer(heap, group_best, limit)

    ordered = sorted(heap, key=lambda entry: entry.key)
    return [
        SemanticWinner(rank=rank, score=entry.score, match=entry.match)
        for rank, entry in enumerate(ordered, start=1)
    ]


# ---------------------------------------------------------------------------
# Engine: scoped read-only semantic retrieval (Step 5C)
# ---------------------------------------------------------------------------
#
# Health/concurrency sequence (single source of truth):
#
# 1. capture ``PRAGMA data_version`` BEFORE any health work;
# 2. run the complete ``search_index.build_status_report`` EXACTLY once;
# 3. validate the embedding schema, the configured exact model and the
#    active generation's exact identity (id/state/model/dimensions/
#    EMBEDDING_VERSION/INDEX_VERSION);
# 4. re-check ``data_version`` immediately after health;
# 5. embed the query EXACTLY once (one request with
#    ``[prepare_query_text(query)]``) and only when at least one
#    in-scope document exists — an empty valid scope/corpus makes ZERO
#    embedding calls and still validates global integrity;
# 6. ONE complete active-generation integrity traversal with in-scope
#    scoring (deterministic ``(recording_id, document_key)`` order,
#    bounded memory, fail closed on any missing/stale/orphan/malformed/
#    zero vector anywhere);
# 7. re-read ``data_version`` AND the complete active identity at the
#    end. ANY mismatch is the fixed sanitized concurrent-change failure
#    with no results.
#
# The traversal is shared by the scoring path and the reusable
# ``semantic_rank`` snapshot entry point, and it reuses the shared 5B.3
# length-first decode helper (``embedding_index._classify_active_page``),
# so a corpus is never blob-decoded twice. A zero-norm STORED document
# vector maps to the role-appropriate ``invalid_document_vector`` code
# (it is a structurally-valid but unusable document vector); every other
# integrity defect (missing/stale/orphan/wrong-length/non-finite) uses
# the stable ``embedding_index_integrity`` category.


def _reject_in_atomic_block(using: str) -> None:
    """Fixed precondition: the engine must never run (and therefore never
    call the embedder) while already inside a caller SQLite transaction."""
    if connections[using].in_atomic_block:
        raise SemanticQueryError(SEMANTIC_IN_TRANSACTION, _IN_TRANSACTION_ERROR)


def _validate_embedding_setup(config, *, using: str) -> EmbeddingGeneration:
    """Validate schema presence, the configured EXACT model (blankness
    via ``.strip()`` only — never stripped/canonicalized) and the active
    generation's exact identity. Returns the active generation row."""
    if not _embedding_schema_present(using=using):
        raise SemanticQueryError(
            SEMANTIC_EMBEDDING_SCHEMA_MISSING, _EMBEDDING_SCHEMA_MISSING_ERROR
        )
    model = config.embedding.model
    if not (model or "").strip():
        raise SemanticQueryError(SEMANTIC_MODEL_NOT_CONFIGURED, _MODEL_NOT_CONFIGURED_ERROR)
    active = (
        EmbeddingGeneration.objects.using(using)
        .filter(state=EmbeddingGenerationState.ACTIVE)
        .order_by("pk")
        .first()
    )
    if active is None:
        raise SemanticQueryError(SEMANTIC_NO_ACTIVE_GENERATION, _NO_ACTIVE_GENERATION_ERROR)
    if (
        active.model != model
        or active.embedding_version != EMBEDDING_VERSION
        or active.source_index_version != search_index.INDEX_VERSION
    ):
        raise SemanticQueryError(
            SEMANTIC_INCOMPATIBLE_GENERATION, _INCOMPATIBLE_GENERATION_ERROR
        )
    return active


def _require_unchanged_data_version(data_version_before: int, using: str) -> None:
    if _pragma_data_version(using) != data_version_before:
        raise SemanticQueryError(SEMANTIC_CONCURRENT_CHANGE, _CONCURRENT_CHANGE_ERROR)


def _compile_scope_or_none(scope, *, using: str) -> tuple[str | None, list | None]:
    """Reuse the keyword engine's scope compiler verbatim (validation and
    compilation semantics are never forked). ``None`` scope stays
    ``(None, None)`` — the traversal then treats every row as in scope."""
    if scope is None:
        return None, None
    return _compile_scope(scope, using=using)


def _scope_has_documents(scope_sql, scope_params, *, using: str) -> bool:
    """Bounded existence check (SELECT ... LIMIT 1) over the scoped
    current SearchDocuments — decides whether a query vector is needed."""
    queryset = SearchDocument.objects.using(using)
    if scope_sql is not None:
        queryset = queryset.extra(
            where=[f"recording_id IN ({scope_sql})"],
            params=list(scope_params or []),
        )
    return queryset.exists()


def _validated_query_vector(embedded, normalized: str, dimensions: int) -> tuple[float, ...]:
    """Validate the ONE returned query embedding: exact cardinality and
    text pairing, dimension equal to the active generation's, and a
    finite NONZERO vector (zero/non-finite → ``invalid_query_vector``).
    Returns the validated values tuple."""
    if type(embedded) is not list or len(embedded) != 1:
        raise SemanticQueryError(SEMANTIC_EMBEDDING_PAIRING, _QUERY_PAIRING_ERROR)
    batch = embedded[0]
    text = getattr(batch, "text", None)
    embedding = getattr(batch, "embedding", None)
    if type(text) is not str or text != prepare_query_text(normalized):
        raise SemanticQueryError(SEMANTIC_EMBEDDING_PAIRING, _QUERY_PAIRING_ERROR)
    if type(embedding) is not tuple and type(embedding) is not list:
        raise SemanticQueryError(SEMANTIC_EMBEDDING_PAIRING, _QUERY_PAIRING_ERROR)
    if len(embedding) != dimensions:
        raise SemanticQueryError(DIMENSION_MISMATCH, _DIMENSION_MISMATCH_ERROR)
    return _prepare_query_vector(embedding)[0]


def _iter_current_document_pages(
    *, using: str, page_size: int, scope_sql, scope_params
):
    """ALL current SearchDocuments in deterministic
    ``(recording_id, document_key)`` order, keyset-paged with the exact
    composite predicate (a recording may never reappear and ids never
    decrease). Every row carries an ``in_scope`` attribute computed from
    the compiled scope subquery, so the SAME traversal validates GLOBAL
    integrity while scoring only in-scope rows. With ``scope=None``
    ``in_scope`` is ``1`` for every row (full-scope parity).

    Rows are loaded with an EXACT ``.only(...)`` projection — the fields
    needed for integrity (``document_key``/``content_hash``), ordering
    and scope (``recording_id``), and scoring provenance
    (``doc_type``/``transcript_id``/``summary_id``/``segment_ordinal``/
    ``start_ms``/``end_ms``/``output_language``). The unbounded
    ``title_text``/``body_text``/``aux_text`` TextFields are NEVER
    loaded during the traversal; winner excerpts are the only bounded
    text fetch (``_fetch_excerpts``).
    """
    _TRAVERSAL_ONLY_FIELDS = (
        "document_key",
        "doc_type",
        "recording_id",
        "transcript_id",
        "summary_id",
        "segment_ordinal",
        "start_ms",
        "end_ms",
        "output_language",
        "content_hash",
    )
    last_recording: str | None = None
    last_key: str | None = None
    while True:
        queryset = (
            SearchDocument.objects.using(using)
            .only(*_TRAVERSAL_ONLY_FIELDS)
            .order_by("recording_id", "document_key")
        )
        if scope_sql is not None:
            queryset = queryset.extra(
                select={"in_scope": f"recording_id IN ({scope_sql})"},
                select_params=list(scope_params or []),
            )
        else:
            queryset = queryset.extra(select={"in_scope": "1"})
        if last_recording is not None:
            queryset = queryset.filter(
                Q(recording_id__gt=last_recording)
                | Q(recording_id=last_recording, document_key__gt=last_key)
            )
        page = list(queryset[:page_size])
        if not page:
            return
        yield page
        last_recording = page[-1].recording_id
        last_key = page[-1].document_key


def _iter_integrity_scored(
    *,
    using: str,
    active,
    dimensions: int,
    scope_sql,
    scope_params,
    query_vector,
    state: dict,
):
    """ONE complete active-generation integrity traversal.

    Yields a provenance-carrying :class:`SemanticCandidate` for every
    IN-SCOPE valid current document in deterministic
    ``(recording_id, document_key)`` order (the exact input contract of
    :func:`select_semantic_winners`, so one Recording finishes before its
    winner enters the global top-K). Missing/stale/invalid vectors are
    detected across the COMPLETE global set — in-scope AND out-of-scope
    defects fail closed with the fixed sanitized integrity error and no
    partial results. Orphans are derived by exact scalar arithmetic
    (active total minus matched active keys) after the stream ends — no
    second blob pass. ``query_vector`` may be ``None`` for an
    empty-scope/integrity-only traversal (no scoring, no candidates).

    ``state`` carries bounded scalar counters: ``current_documents``,
    ``in_scope_documents``, ``matched_recordings`` (distinct in-scope
    recordings, exact), ``matched_active_keys`` and the last seen
    in-scope recording id.
    """
    active_total = (
        EmbeddingDocument.objects.using(using).filter(generation=active).count()
    )
    for page in _iter_current_document_pages(
        using=using,
        page_size=SEMANTIC_PAGE_SIZE,
        scope_sql=scope_sql,
        scope_params=scope_params,
    ):
        keys = [row.document_key for row in page]
        active_rows = list(
            EmbeddingDocument.objects.using(using)
            .filter(generation=active, document_key__in=keys)
            .only("id", "document_key", "source_content_hash")
        )
        classified = _classify_active_page(active_rows, dimensions, using)
        by_key = {row.document_key: row for row in active_rows}
        for row in page:
            state["current_documents"] += 1
            arow = by_key.get(row.document_key)
            if arow is None:
                raise SemanticQueryError(
                    SEMANTIC_INDEX_INTEGRITY, _MISSING_DOCUMENT_ERROR
                )
            state["matched_active_keys"] += 1
            if arow.source_content_hash != row.content_hash:
                raise SemanticQueryError(
                    SEMANTIC_INDEX_INTEGRITY, _STALE_CONTENT_ERROR
                )
            kind, values = classified.get(arow.pk, ("invalid", None))
            if kind == "zero":
                # A structurally-valid but zero-norm stored document
                # vector is the pure-layer ``invalid_document_vector``
                # failure (never skipped, never approximated).
                raise SemanticQueryError(
                    INVALID_DOCUMENT_VECTOR, _INVALID_DOCUMENT_VECTOR_ERROR
                )
            if kind != "ok":
                raise SemanticQueryError(
                    SEMANTIC_INDEX_INTEGRITY, _INVALID_VECTOR_ERROR
                )
            if not bool(getattr(row, "in_scope", True)):
                continue
            state["in_scope_documents"] += 1
            if row.recording_id != state["last_in_scope_recording"]:
                state["matched_recordings"] += 1
                state["last_in_scope_recording"] = row.recording_id
            if query_vector is not None:
                yield SemanticCandidate(
                    recording_id=row.recording_id,
                    document_key=row.document_key,
                    doc_type=row.doc_type,
                    vector=values,
                    transcript_id=row.transcript_id,
                    summary_id=row.summary_id,
                    segment_ordinal=row.segment_ordinal,
                    start_ms=row.start_ms,
                    end_ms=row.end_ms,
                    output_language=row.output_language,
                )
    if state["matched_active_keys"] != active_total:
        raise SemanticQueryError(SEMANTIC_INDEX_INTEGRITY, _ORPHAN_DOCUMENT_ERROR)


def _same_active_identity(a, b) -> bool:
    """Complete active identity equality (id, state, model, dimensions,
    embedding version, source index version)."""
    return (
        a.pk == b.pk
        and a.state == b.state
        and a.model == b.model
        and a.dimensions == b.dimensions
        and a.embedding_version == b.embedding_version
        and a.source_index_version == b.source_index_version
    )


def _verify_final_state(active, data_version_before: int, using: str) -> None:
    """Re-read ``PRAGMA data_version`` and the complete active identity
    at the very end: ANY mismatch (another connection committed anywhere
    during the operation, or the active generation was superseded/
    replaced) is the fixed sanitized concurrent-change failure with no
    results."""
    if _pragma_data_version(using) != data_version_before:
        raise SemanticQueryError(SEMANTIC_CONCURRENT_CHANGE, _CONCURRENT_CHANGE_ERROR)
    current = EmbeddingGeneration.objects.using(using).filter(pk=active.pk).first()
    if current is None or not _same_active_identity(current, active):
        raise SemanticQueryError(SEMANTIC_CONCURRENT_CHANGE, _CONCURRENT_CHANGE_ERROR)


# Winner-only excerpt policy (plain text, no marks): segment uses the
# body; summary prefers body then title then aux; metadata (recording)
# prefers title then body then aux. Fields are SELECTed as bounded
# SUBSTR windows (max 321 codepoints), never loaded whole.
_EXCERPT_FIELD_ORDER = {
    "segment": ("body_text",),
    "summary": ("body_text", "title_text", "aux_text"),
    "recording": ("title_text", "body_text", "aux_text"),
}


def _fetch_excerpts(matches, *, using: str) -> dict:
    """Winner-only bounded excerpt fetch: ONE SELECT of bounded SUBSTR
    windows over the winner keys, then the per-doc-type policy in
    Python. Returns ``{document_key: {"field", "text"} | None}`` — text
    is at most 320 content codepoints plus an optional ellipsis."""
    if not matches:
        return {}
    keys = [match.document_key for match in matches]
    table = SearchDocument._meta.db_table
    placeholders = ", ".join(["%s"] * len(keys))
    rows: dict[str, dict[str, str]] = {}
    with connections[using].cursor() as cursor:
        cursor.execute(
            f"SELECT document_key, SUBSTR(title_text, 1, {_EXCERPT_SELECT_CODEPOINTS}), "
            f"SUBSTR(body_text, 1, {_EXCERPT_SELECT_CODEPOINTS}), "
            f"SUBSTR(aux_text, 1, {_EXCERPT_SELECT_CODEPOINTS}) "
            f"FROM {table} WHERE document_key IN ({placeholders})",
            keys,
        )
        for key, title, body, aux in cursor.fetchall():
            rows[key] = {"title_text": title, "body_text": body, "aux_text": aux}
    out: dict[str, dict | None] = {}
    for match in matches:
        fields = _EXCERPT_FIELD_ORDER.get(match.doc_type)
        if not fields:
            out[match.document_key] = None
            continue
        chosen_field: str | None = None
        chosen_value: str | None = None
        row = rows.get(match.document_key) or {}
        for field in fields:
            value = row.get(field)
            if value:
                chosen_field = field
                chosen_value = value
                break
        if chosen_value is None:
            out[match.document_key] = None
        elif len(chosen_value) > EXCERPT_MAX_CODEPOINTS:
            out[match.document_key] = {
                "field": chosen_field,
                "text": chosen_value[:EXCERPT_MAX_CODEPOINTS] + ELLIPSIS,
            }
        else:
            out[match.document_key] = {"field": chosen_field, "text": chosen_value}
    return out


def _build_payload(
    *,
    normalized: str,
    limit: int,
    using: str,
    active,
    winners: list[SemanticWinner],
    matched_recordings: int,
) -> dict:
    """Assemble the ``search_recordings``-aligned semantic payload.
    Titles use canonical metadata (shared ``search_query._lookup_titles``,
    bounded chunked); the match shape is the shared ``_provenance``
    contract; snippets are winner-only bounded plain-text excerpts."""
    titles = _lookup_titles([w.match.recording_id for w in winners], using=using)
    excerpts = _fetch_excerpts([w.match for w in winners], using=using)
    results = []
    for winner in winners:
        match = winner.match
        provenance_row = {
            "doc_type": match.doc_type,
            "document_key": match.document_key,
            "output_language": match.output_language,
            "summary_id": match.summary_id,
            "transcript_id": match.transcript_id,
            "segment_ordinal": match.segment_ordinal,
            "start_ms": match.start_ms,
            "end_ms": match.end_ms,
        }
        results.append(
            {
                "rank": winner.rank,
                "recording_id": match.recording_id,
                "title": titles.get(match.recording_id, ""),
                "match": _provenance(provenance_row),
                "snippet": excerpts.get(match.document_key),
                "score": winner.score,
            }
        )
    return {
        "query": normalized,
        "mode": "semantic",
        "semantic_query_version": SEMANTIC_QUERY_VERSION,
        "index_version": search_index.INDEX_VERSION,
        "embedding_generation": {
            "id": active.pk,
            "model": active.model,
            "dimensions": active.dimensions,
            "embedding_version": active.embedding_version,
            "source_index_version": active.source_index_version,
        },
        "limit": limit,
        "results": results,
        "result_count": len(results),
        "truncated": False,  # exhaustive scan: truncation never applies
        # EXACT (derived with scalar counts during the grouped
        # traversal, never a COUNT DISTINCT extra sweep).
        "more_recordings_matched": max(matched_recordings - len(results), 0),
    }


def _traverse_and_finish(
    query_vector,
    *,
    normalized: str,
    limit: int,
    using: str,
    active,
    scope_sql,
    scope_params,
    data_version_before: int,
    verify_final: bool = True,
) -> dict:
    """Run the ONE integrity traversal (+ in-scope scoring), build the
    payload, then perform the final data_version + active-identity
    re-read. ``query_vector`` is ``None`` only for an empty scope (or
    empty in-scope set): the traversal then integrity-checks the
    complete global set and returns a zero-result payload.

    ``verify_final=False`` skips the final re-read so a hybrid
    orchestrator can run the keyword component and perform ONE final
    data_version + active-identity check AFTER BOTH components (no
    concurrent commit may slip between the two components and still
    report results)."""
    state = {
        "current_documents": 0,
        "in_scope_documents": 0,
        "matched_recordings": 0,
        "matched_active_keys": 0,
        "last_in_scope_recording": None,
    }
    stream = _iter_integrity_scored(
        using=using,
        active=active,
        dimensions=active.dimensions,
        scope_sql=scope_sql,
        scope_params=scope_params,
        query_vector=query_vector,
        state=state,
    )
    if query_vector is not None:
        winners = select_semantic_winners(query_vector, stream, limit=limit)
    else:
        for _candidate in stream:
            pass
        winners = []
    payload = _build_payload(
        normalized=normalized,
        limit=limit,
        using=using,
        active=active,
        winners=winners,
        matched_recordings=state["matched_recordings"],
    )
    if verify_final:
        _verify_final_state(active, data_version_before, using)
    return payload


@dataclass(frozen=True)
class SemanticSnapshot:
    """Frozen validated snapshot consumed by :func:`run_semantic_snapshot`.

    Carries everything the ONE integrity traversal + in-scope scoring
    needs: the exact normalized query, the validated active generation
    row, the compiled recording scope (an immutable
    :class:`~workflow.services.search_query.CompiledScope`, ``None`` for
    an unscoped run), the prepared query vector (``None`` for an empty
    scope) and the caller's ``data_version`` capture. A hybrid
    orchestrator compiles the Recording QuerySet EXACTLY ONCE via
    ``search_query.compile_scope``, places that SAME compiled value here
    and passes it to the keyword engine — the runner NEVER re-sweeps
    source health, NEVER re-embeds and NEVER recompiles the scope.
    """

    normalized: str
    active: EmbeddingGeneration
    scope: CompiledScope | None
    query_vector: tuple[float, ...] | None
    data_version_before: int


def embed_query_vector(
    normalized: str,
    *,
    active,
    scope_sql,
    scope_params,
    using: str,
    config,
    embedder,
) -> tuple[float, ...] | None:
    """Prepare the query vector for one already-validated snapshot.

    EXACTLY ONE embedding request for a nonempty scoped corpus and ZERO
    for an empty one (the empty decision uses the same scoped existence
    check as :func:`semantic_search`). The returned vector is validated
    against the active generation's dimensions and the prepared query
    text; failures raise the fixed sanitized semantic errors. The
    request is made outside any DB transaction (the caller already
    rejected atomic blocks at its boundary)."""
    if not _scope_has_documents(scope_sql, scope_params, using=using):
        return None
    embedded = embedder(config, [prepare_query_text(normalized)])
    return _validated_query_vector(embedded, normalized, active.dimensions)


def run_semantic_snapshot(
    snapshot: SemanticSnapshot,
    *,
    using: str,
    limit: int,
    verify_final: bool = True,
) -> dict:
    """Run the ONE complete integrity traversal (+ in-scope scoring) over
    a prevalidated :class:`SemanticSnapshot`.

    The snapshot's active generation, compiled scope (an immutable
    :class:`~workflow.services.search_query.CompiledScope`), prepared
    query vector and ``data_version`` capture were validated by the
    caller (a hybrid orchestrator that already ran the single
    source-health sweep, the single scope compilation and the query
    embedding itself), so this entry point NEVER sweeps source health,
    NEVER embeds and NEVER recompiles the scope — the snapshot is
    consumed as-is. With ``verify_final=True`` (the default) it ends with
    the data_version + active-identity re-read; a hybrid orchestrator
    passes ``verify_final=False`` and performs that final re-read itself
    AFTER its other component, so no concurrent commit can slip between
    the two components and still report results. Returns the standard
    semantic payload."""
    validate_semantic_limit(limit)
    scope_sql = snapshot.scope.sql if snapshot.scope is not None else None
    scope_params = (
        list(snapshot.scope.params) if snapshot.scope is not None else None
    )
    return _traverse_and_finish(
        snapshot.query_vector,
        normalized=snapshot.normalized,
        limit=limit,
        using=using,
        active=snapshot.active,
        scope_sql=scope_sql,
        scope_params=scope_params,
        data_version_before=snapshot.data_version_before,
        verify_final=verify_final,
    )


def semantic_search(
    raw: str | None,
    *,
    limit: int = SEMANTIC_DEFAULT_RESULT_LIMIT,
    using: str = "default",
    scope=None,
    config=None,
    embedder=None,
) -> dict:
    """One complete read-only semantic search.

    Full pipeline: input validation → ``PRAGMA data_version`` capture →
    EXACTLY ONE source health sweep (``search_index.build_status_report``)
    → schema/model/active-generation identity validation → data_version
    re-check → scope compilation → exactly one query embedding request
    (only when at least one in-scope document exists; zero requests for
    an empty scope) → ONE complete global integrity traversal with
    in-scope scoring → final data_version + active-identity re-read.

    ``scope`` is the existing unsliced ``Recording`` QuerySet contract
    (never SQL; ``search_query._compile_scope`` compiles it). Out-of-scope
    documents can never win, but global integrity defects anywhere still
    fail closed. Strictly SELECT/PRAGMA plus one localhost embedding
    request; no lock, no writes, no rebuild/repair/sync, no logs.

    ``embedder`` is injectable for tests; when ``None`` the production
    ``embedding_client.embed_texts`` is resolved AT CALL TIME (a
    module-level lookup, matching the embedding-index commands), so the
    production seam can be patched consistently.
    """
    normalized = validate_semantic_query(raw, limit)
    try:
        _reject_in_atomic_block(using)
        if config is None:
            raise SemanticQueryError(SEMANTIC_UNEXPECTED, _UNEXPECTED_ERROR)
        if embedder is None:
            from workflow.services import embedding_client

            embedder = embedding_client.embed_texts
        data_version_before = _pragma_data_version(using)
        source_report = search_index.build_status_report(using=using)
        if not source_report.get("healthy"):
            raise SemanticQueryError(
                SEMANTIC_SOURCE_UNHEALTHY, _SOURCE_UNHEALTHY_ERROR
            )
        active = _validate_embedding_setup(config, using=using)
        _require_unchanged_data_version(data_version_before, using)
        scope_sql, scope_params = _compile_scope_or_none(scope, using=using)
        has_scope_documents = _scope_has_documents(
            scope_sql, scope_params, using=using
        )
        if has_scope_documents:
            embedded = embedder(config, [prepare_query_text(normalized)])
            query_vector = _validated_query_vector(
                embedded, normalized, active.dimensions
            )
        else:
            query_vector = None
        return _traverse_and_finish(
            query_vector,
            normalized=normalized,
            limit=limit,
            using=using,
            active=active,
            scope_sql=scope_sql,
            scope_params=scope_params,
            data_version_before=data_version_before,
        )
    except SemanticQueryError:
        raise
    except EmbeddingError as exc:
        raise SemanticQueryError(
            SEMANTIC_EMBEDDING_FAILED, _request_error(getattr(exc, "code", None))
        ) from None
    except ConfigError:
        # Already-sanitized shared errors (e.g. the scope compiler's
        # usage/index failures) propagate unchanged — never forked.
        raise
    except Exception:
        # Unexpected operational failure — never KeyboardInterrupt/
        # SystemExit (BaseException) and never raw details.
        raise SemanticQueryError(SEMANTIC_UNEXPECTED, _UNEXPECTED_ERROR) from None


def semantic_rank(
    query: str,
    query_vector,
    *,
    config,
    using: str = "default",
    scope=None,
    limit: int = SEMANTIC_DEFAULT_RESULT_LIMIT,
    data_version_before: int | None = None,
) -> dict:
    """Reusable validated snapshot/result entry point (for later hybrid
    orchestration).

    Accepts an ALREADY-EMBEDDED normalized query vector (``None`` for an
    empty-scope/integrity-only run) and the recording scope, and returns
    the semantic payload plus complete active-generation identity
    metadata. It NEVER triggers the source health sweep itself (the
    caller runs it exactly once) and never embeds: the traversal
    validates the COMPLETE global active-generation integrity while
    scoring only in-scope documents, re-reads data_version + active
    identity at the end, and fails closed on any defect or concurrent
    change.

    ``query`` must ALREADY be an exact normalized semantic query — the
    same already-normalized exact-str contract as
    :func:`prepare_query_text` (NFC, outer-stripped, nonblank, within
    the codepoint cap) — enforced at entry with a fixed sanitized input
    error, so a hybrid caller can never echo arbitrary/non-NFC/
    whitespace/over-cap content through this reusable API. It is echoed
    back in the payload unchanged.

    ``data_version_before`` is the value the caller captured BEFORE its
    own source health sweep (when omitted, one is captured at entry).
    """
    validate_semantic_limit(limit)
    # Enforce the same already-normalized exact-str contract as
    # prepare_query_text BEFORE any DB/health work (fixed sanitized
    # SemanticQueryInputError; the offending text is never echoed).
    query = prepare_query_text(query)
    try:
        _reject_in_atomic_block(using)
        if config is None:
            raise SemanticQueryError(SEMANTIC_UNEXPECTED, _UNEXPECTED_ERROR)
        if data_version_before is None:
            data_version_before = _pragma_data_version(using)
        active = _validate_embedding_setup(config, using=using)
        compiled_scope = compile_scope(scope, using=using) if scope is not None else None
        scope_sql = compiled_scope.sql if compiled_scope is not None else None
        scope_params = list(compiled_scope.params) if compiled_scope is not None else None
        has_scope_documents = _scope_has_documents(
            scope_sql, scope_params, using=using
        )
        if has_scope_documents:
            if query_vector is None:
                # A nonempty scope with no query vector is a caller bug:
                # fail closed as the fixed invalid-query-vector error.
                raise SemanticQueryError(
                    INVALID_QUERY_VECTOR, _INVALID_QUERY_VECTOR_ERROR
                )
            query_vector = _prepare_query_vector(query_vector)[0]
            if len(query_vector) != active.dimensions:
                raise SemanticQueryError(DIMENSION_MISMATCH, _DIMENSION_MISMATCH_ERROR)
        else:
            query_vector = None
        snapshot = SemanticSnapshot(
            normalized=query,
            active=active,
            scope=compiled_scope,
            query_vector=query_vector,
            data_version_before=data_version_before,
        )
        return run_semantic_snapshot(snapshot, using=using, limit=limit)
    except SemanticQueryError:
        raise
    except ConfigError:
        # Already-sanitized shared errors (e.g. the scope compiler's
        # usage/index failures) propagate unchanged — never forked.
        raise
    except Exception:
        # Unexpected operational failure — never KeyboardInterrupt/
        # SystemExit (BaseException) and never raw details.
        raise SemanticQueryError(SEMANTIC_UNEXPECTED, _UNEXPECTED_ERROR) from None
