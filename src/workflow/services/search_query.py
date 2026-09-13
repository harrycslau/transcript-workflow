"""Read-only keyword-search query service (Step 5A.4.1).

Builds on the Step 5A.2/5A.3 index (``workflow.services.search_index``
and ``workflow.services.search_sync``) and NEVER maintains it: this
module issues SELECTs only — no rebuild, no repair, no synchronization,
no writes, no pipeline lock.

Two explicitly separated health layers (so Step 5A.4.2 can later plug a
cached-health policy into the interactive path):

- :func:`preflight_full_health` — the FULL read-only integrity sweep
  (``build_status_report``), EXACTLY ONE call per ``brain search``
  command; any unhealthy category hard-fails with a stable, content-free
  message pointing at ``search-index status`` / ``rebuild``. The CLI
  calls this; the engine never does.
- :func:`search_recordings` — the reusable query engine. It only checks
  that the index is STRUCTURALLY queryable (registry table + FTS schema
  + window-function support) and maps query-time SQL failures to a
  stable error; it deliberately does not sweep for staleness, so an
  interactive caller can decide its own (possibly cached) freshness
  policy.

Query contract (plain-text users only, never raw FTS MATCH syntax):

- the query is NFC-normalized and outer-stripped, then split into
  whitespace-separated LITERAL terms (``"``/``%``/``_``/``\\``/``*``/
  ``AND``/``NEAR`` are user text, never syntax); terms combine with
  AND at document level;
- a term of 3+ Unicode codepoints runs as ONE safely quoted FTS
  phrase; a term of 1–2 codepoints (which the trigram tokenizer cannot
  match at all) runs as an escaped Unicode-aware ``LIKE`` fallback via
  the shared ``brain_fold`` contract (NFC + per-codepoint casefold);
  mixed short/long queries combine both predicate kinds with AND;
- selection is a deterministic, rebuild-stable ordering over the
  registry JOIN (never FTS rowid, never natural order): candidates are
  fetched in global document-key order, and the PER-RECORDING candidate
  bound keeps Summary first, then Recording metadata, then Segments
  (document-key order inside each class) — so a flood of matching
  segments can never evict the higher-priority candidates the
  comparator prefers;
- ``truncated`` is TRUE for ANY bound overflow: exceeded global
  candidate bound (exact ``COUNT(*) OVER ()``, never inferred from the
  kept-row count) or any single Recording exceeding its per-recording
  bound. When it is true, per-Recording winners are still the best
  among the bounded candidate set and the matched-Recording set stays
  complete under per-recording-only overflow (each matched Recording
  keeps at least its highest-priority candidate), but a truncated
  Recording's best document is APPROXIMATE: another candidate the
  comparator would have preferred may have been trimmed;
- ``more_recordings_matched`` is EXACT while the matched-Recording set
  is complete (the global fetch bound did not cut rows) and ``null``
  (unknown) otherwise — never guessed;
- every result additionally carries the additive Library item-identity
  fields ``item_key``, ``item_kind`` and nullable ``section_id``,
  mirroring the ``workflow.query`` Library item-union key contract
  (``r:<pk>`` / ``s:<pk>``). Without an item scope every result is the
  whole Recording (always ``r:<recording_id>`` / ``"recording"`` /
  ``None`` — purely additive, no existing field or value changes);
  with an item scope the identity is SQL-derived from the matched
  document (see the Library item-scope paragraph below);
- ranking is deterministic: (worst satisfied-column rank,
  document-type rank, total fold-occurrences desc, first match offset,
  document_key) with the Recording id as the final tie-break; the best
  document per Recording is returned EXACTLY ONCE per Recording;
- snippets are plain text (never HTML) with offset ranges relative to
  the returned snippet text, so the Step 5A.4.2 web layer can render
  highlights itself; highlight ranges map through EXACT
  start/end source-character spans (partial casefold expansions like
  ``ﬃ``→``ffi`` or ``ß``→``ss`` yield whole-source-character ranges,
  never zero-length ones); the window is anchored on the FIRST
  INDIVIDUAL mapped match (never on a globally merged range — repeated
  text chains into unbounded merges) and the content window is
  HARD-capped at ``SNIPPET_MAX_CODEPOINTS`` unconditionally (returned
  text at most that plus two ellipsis characters): the anchor is fully
  visible whenever it fits the cap, and the cap never yields to a
  pathological over-cap anchor; ranges are selected CLIPPED to the
  window and merged only among displayed ranges, so every returned
  range is non-empty, ordered, non-overlapping and inside the text.

Recording scope (Step 5A.4.2a, ``scope=`` parameter): an OPTIONAL
constrained ``Recording`` eligibility QuerySet (built by
``workflow.query.search_scope_queryset`` — never a SQL string) restricts
the candidate population BEFORE ranking: the engine validates the model,
clears ordering, forces a single-column PK selection and compiles it on
the SAME database alias; the predicate lands in the innermost matched-set
WHERE, which SQL evaluates before the window functions, so
``ROW_NUMBER``, both ``COUNT(*) OVER`` bounds, ``truncated`` and
``more_recordings_matched`` are all in-scope truths — out-of-scope
documents can neither evict in-scope candidates at the bounds nor trigger
truncation, and a lower-ranked in-scope match can never be starved by an
out-of-scope flood.

Library item scope (Step 6.3 groundwork): :func:`compile_item_scope`
compiles the one-column ``item_key`` UNION produced by
``workflow.query.library_item_key_queryset`` (the normal Library's
item-identity set, ``r:<pk>``/``s:<pk>``) into an immutable
:class:`CompiledItemScope` for consumption by :func:`search_recordings`.
The compiler validates the UNION exactly as the builder produces it (a
QuerySet, unsliced, union combinator, EXACTLY one ``item_key`` column on
the combined query and on every branch — a combined query can never be
re-projected or re-filtered, so the shape itself is the contract) and
compiles it once on the SAME database alias; a compiler-proven empty
UNION answers the same provably-empty subquery as the Recording scope,
and every other compilation failure is the same sanitized index failure
(the compiled SQL, parameters and the underlying exception text never
escape). ``search_recordings`` consumes the compiled value through the
optional ``compiled_item_scope=`` parameter (or compiles the raw UNION
itself for ``item_scope=``); the Recording-scope ``scope=`` /
:class:`CompiledScope` API and its SQL are unchanged, and the two
mechanisms are mutually exclusive (exactly one eligibility mechanism
per call).

Library item mode (Step 6.3): when an item scope is supplied the
innermost matched-set SQL derives every candidate's Library item
identity from the SHARED ``workflow.services.segmentation``
canonical-layout SQL (never a forked predicate, never a Python id set):
no canonical split ⇒ ``r:<recording_id>``; a canonical split ⇒ a valid
topic Summary maps to its own Section and a retained Segment ordinal
maps to its owning valid Section, while the parent metadata document,
the whole-recording fixed Summary and cropped-out Segments derive
``NULL`` and are excluded. The derived key is restricted to the
compiled item scope in the next subquery level — WHERE evaluates
before the window functions of the same SELECT — so the partition, the
per-item candidate bound, ``truncated``, the per-item dedup and
``more_items_matched`` are all item truths: a valid active split layout
yields EXACTLY the active Section items with the parent Recording
suppressed (never parent + section duplicates), while
unsplit/crop-only/historical/malformed recordings fail closed to their
single Recording item. Winners deduplicate and count per ``item_key``;
the historical ``more_recordings_matched`` stays as the SAME-VALUE
compatibility alias of ``more_items_matched``. Titles remain the parent
Recording's Library display title until the web slice renders section
titles. The item-mode payload additionally carries the simple flag
``item_mode: true`` (OMITTED entirely outside item mode, so the legacy
payload stays byte-identical): the matched unit IS a Library item, so
presentation counts Library items unconditionally and NEVER infers the
unit from the returned rows (they may all be Recordings while the
omitted matches include Sections).

Index/SQLite failures, malformed input and over-cap queries raise
``ConfigError`` subclasses with fixed sanitized messages: category and
command names only, never query text, indexed content, keys, paths or
SQL — including scope-related errors (the compiled scope SQL is never
echoed anywhere).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from django.db import connections

from brainlib.config import ConfigError
from workflow.services.search_index import (
    FTS_TABLE,
    INDEX_VERSION,
    SearchIndexError,
    _registry_schema_present,
    build_status_report,
    inspect_fts_schema,
)
# The SHARED canonical-layout SQL (the SAME predicate the Library item
# union uses; never a forked copy): import-safe one way only —
# segmentation imports neither search_index nor search_query.
from workflow.services.segmentation import (
    canonical_active_section_ids,
    canonical_hidden_recording_ids,
)
from workflow.sqlite_unicode import ensure_fold_function, fold_text

# ---------------------------------------------------------------------------
# Bounds (all fixed; CLI/echo limits are validated, never silently applied)
# ---------------------------------------------------------------------------

MAX_QUERY_CODEPOINTS = 256
MAX_QUERY_TERMS = 8
DEFAULT_RESULT_LIMIT = 50
MAX_RESULT_LIMIT = 200
# Global candidate bound; the selection fetches bound+1 rows to detect
# a fetch cut, and the exact candidate total (COUNT(*) OVER ()) decides
# `truncated` truthfully — never inferred from the kept-row count.
MAX_SCORED_DOCUMENTS = 2000
# Per-recording candidate bound: a recording with thousands of matching
# segments contributes at most this many candidates (Summary/recording
# metadata classes kept first), so it can never starve every other
# recording out of the global bound; exceeding the bound sets
# `truncated` instead of silently discarding candidates.
PER_RECORDING_CANDIDATES = 100
SNIPPET_RADIUS_CODEPOINTS = 80
SNIPPET_MAX_CODEPOINTS = 320
_TITLE_LOOKUP_CHUNK = 500

LIKE_ESCAPE = "\\"
ELLIPSIS = "\u2026"

# Stable error messages — fixed text only, never query text, indexed
# content, document keys, paths or SQL.
_EMPTY_QUERY_ERROR = "the search query must not be empty"
_TOO_LONG_ERROR = (
    f"the search query must be at most {MAX_QUERY_CODEPOINTS} characters"
)
_TOO_MANY_TERMS_ERROR = (
    f"the search query must contain at most {MAX_QUERY_TERMS} words"
)
_BAD_LIMIT_ERROR = (
    f"the result limit must be an integer between 1 and {MAX_RESULT_LIMIT}"
)
_SQLITE_ONLY_ERROR = "keyword search requires a SQLite database connection"
_REGISTRY_MISSING_ERROR = (
    "the search index registry is missing; apply pending migrations "
    "(uv run python src/manage.py migrate) and build the index with: "
    "brain search-index rebuild"
)
_FTS_MISSING_ERROR = (
    "the keyword-search index is missing; build it with: "
    "brain search-index rebuild"
)
_FTS_BROKEN_ERROR = (
    "the keyword-search index is broken; inspect with: brain search-index status"
    " and repair with: brain search-index rebuild"
)
_WINDOW_UNSUPPORTED_ERROR = (
    "keyword search requires SQLite window functions (SQLite 3.25+); "
    "this Python SQLite build does not provide them"
)
_SCOPE_TYPE_ERROR = (
    "the search scope must be an unsliced queryset of Recordings"
)
_COMPILED_SCOPE_TYPE_ERROR = (
    "the compiled search scope must be a CompiledScope value produced by compile_scope"
)
_SCOPE_AMBIGUOUS_ERROR = (
    "the search scope must be supplied either as a Recording queryset or "
    "as a precompiled scope, never both"
)
_SCOPE_ALIAS_ERROR = (
    "the compiled search scope was built for a different database connection"
)
_ITEM_SCOPE_TYPE_ERROR = (
    "the item scope must be the unsliced one-column item_key UNION from "
    "workflow.query.library_item_key_queryset"
)
_COMPILED_ITEM_SCOPE_TYPE_ERROR = (
    "the compiled item scope must be a CompiledItemScope value produced by "
    "compile_item_scope"
)
_ITEM_SCOPE_AMBIGUOUS_ERROR = (
    "the item scope must be supplied either as an item_key UNION queryset "
    "or as a precompiled item scope, never both"
)
_ITEM_SCOPE_ALIAS_ERROR = (
    "the compiled item scope was built for a different database connection"
)
_SCOPE_ITEM_CONFLICT_ERROR = (
    "the search scope must be either a Recording eligibility scope or a "
    "Library item scope, never both"
)
_QUERY_FAILED_ERROR = (
    "the keyword-search index could not be queried; inspect with: "
    "brain search-index status"
)
_FOLD_UNAVAILABLE_SEARCH_ERROR = (
    "keyword search cannot perform Unicode-folded short-term matching: the "
    "fold function is unavailable on this connection; inspect with: "
    "brain search-index status"
)


class SearchQueryInputError(ConfigError):
    """Malformed or over-cap search input (CLI: usage error, exit 2)."""


def normalize_query(raw: str | None) -> str:
    """NFC-normalize and outer-strip a user query, enforcing the stable
    input bounds. Returns the canonical query string or raises
    ``SearchQueryInputError`` (never echoes the offending text)."""
    if raw is None:
        raise SearchQueryInputError(_EMPTY_QUERY_ERROR)
    normalized = unicodedata.normalize("NFC", raw).strip()
    if not normalized:
        raise SearchQueryInputError(_EMPTY_QUERY_ERROR)
    if len(normalized) > MAX_QUERY_CODEPOINTS:
        raise SearchQueryInputError(_TOO_LONG_ERROR)
    return normalized


def _split_terms(normalized: str) -> list[str]:
    terms = normalized.split()
    if not terms:
        raise SearchQueryInputError(_EMPTY_QUERY_ERROR)
    if len(terms) > MAX_QUERY_TERMS:
        raise SearchQueryInputError(_TOO_MANY_TERMS_ERROR)
    return terms


def _validate_limit(limit) -> None:
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_RESULT_LIMIT
    ):
        raise SearchQueryInputError(_BAD_LIMIT_ERROR)


def validate_query(raw: str | None, limit: int = DEFAULT_RESULT_LIMIT) -> str:
    """ALL user-input validation in ONE cheap, sweep-free entry point
    (empty/over-long/over-many-terms/over-cap limit). The CLI calls this
    BEFORE the health gate so usage errors exit 2 without paying the
    integrity sweep; ``search_recordings`` validates again internally so
    no caller can skip the bounds."""
    normalized = normalize_query(raw)
    _validate_limit(limit)
    _split_terms(normalized)
    return normalized


# ---------------------------------------------------------------------------
# Health layers (FULL sweep = CLI preflight only; engine = structural)
# ---------------------------------------------------------------------------


def _unhealthy_message(report: dict) -> str:
    categories = report.get("categories", {})
    active = sorted(name for name, count in categories.items() if count)
    if "registry_schema_missing" in active:
        return _REGISTRY_MISSING_ERROR
    if "fts_missing" in active:
        return _FTS_MISSING_ERROR
    if "fts_broken" in active:
        sub = (report.get("fts") or {}).get("category") or "fts_broken"
        return (
            f"the keyword-search index is broken ({sub}); inspect with: "
            "brain search-index status and repair with: brain search-index rebuild"
        )
    listed = ", ".join(active) if active else "unknown"
    return (
        f"the keyword-search index is stale or inconsistent ({listed}); inspect "
        "with: brain search-index status and repair with: "
        "brain search-index rebuild"
    )


def preflight_full_health(*, using: str = "default") -> dict:
    """FULL read-only integrity preflight for ``brain search``.

    Runs ``build_status_report`` EXACTLY once and raises
    ``SearchIndexError`` (CLI exit 1, hard-fail — no results) on any
    unhealthy category. The query engine NEVER calls this; keeping the
    two layers separate is what lets Step 5A.4.2 choose a safe cached
    health policy for interactive web search.
    """
    report = build_status_report(using=using)
    if not report.get("healthy"):
        raise SearchIndexError(_unhealthy_message(report))
    return report


def _require_queryable_index(*, using: str = "default") -> None:
    """Cheap STRUCTURAL queryability checks only (registry table, FTS
    schema/tokenizer, window-function support). Detects missing or
    broken indexes cleanly; NEVER sweeps for staleness, NEVER creates,
    repairs or synchronizes anything."""
    connection = connections[using]
    if connection.vendor != "sqlite":
        raise SearchIndexError(_SQLITE_ONLY_ERROR)
    if not _registry_schema_present(using=using):
        raise SearchIndexError(_REGISTRY_MISSING_ERROR)
    state = inspect_fts_schema(using=using)
    if state["state"] == "missing":
        raise SearchIndexError(_FTS_MISSING_ERROR)
    if state["state"] != "ok":
        raise SearchIndexError(_FTS_BROKEN_ERROR)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT ROW_NUMBER() OVER (ORDER BY 1) WHERE 0")
    except Exception:
        raise SearchIndexError(_WINDOW_UNSUPPORTED_ERROR) from None


# ---------------------------------------------------------------------------
# Candidate selection (deterministic, rebuild-stable, bounded)
# ---------------------------------------------------------------------------


def _quote_phrase(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _escape_like(folded_term: str) -> str:
    return (
        folded_term.replace(LIKE_ESCAPE, LIKE_ESCAPE + LIKE_ESCAPE)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )


def _short_term_pattern(term: str) -> str:
    return "%" + _escape_like(fold_text(term)) + "%"


def _registry_table() -> str:
    from workflow.models import SearchDocument

    return SearchDocument._meta.db_table


# The provably-empty scope subquery both scope compilers answer for a
# compiler-proven empty scope (``EmptyResultSet``): it matches nothing on
# every connection, so an empty scope takes the normal zero-result path.
_EMPTY_SCOPE_SQL = "SELECT NULL WHERE 1 = 0"


def _compile_scope(scope, *, using: str) -> tuple[str, list]:
    """Validate and compile the constrained recording-scope QuerySet.

    The public contract (Step 5A.4.2a) is a QuerySet, never SQL text:
    callers pass a plain ``Recording`` eligibility queryset (see
    ``workflow.query.search_scope_queryset``). This is the ONLY place
    scope SQL is ever produced:

    - accepts ONLY an UNSLICED ``QuerySet`` whose model is EXACTLY
      ``Recording`` (anything else is a stable, content-free error);
    - clears any caller ordering and forces a single-column PK
      selection — never trusting the caller's shape;
    - compiles on the SAME database alias used for the search, so the
      subquery always agrees with the outer query dialectically.

    An EMPTY scope is VALID for EVERY empty-query form —
    ``Recording.objects.none()``, ``filter(pk__in=[])`` or any other
    range the compiler proves empty: Django refuses to emit SQL for
    such queries (``EmptyResultSet`` from ``as_sql``), which is caught
    and answered with the same provably-empty subquery, going through
    the normal zero-result path — never an error. The compiler is the
    single source of truth for emptiness (no reliance on internal
    query flags, which only cover some forms).

    Every OTHER compilation failure is the same sanitized index failure
    as any other query execution failure (``from None`` severs the
    context chain): compiled SQL, parameters, paths, indexed content,
    the query and the underlying exception text NEVER escape.
    """
    from django.core.exceptions import EmptyResultSet
    from django.db.models import QuerySet

    from workflow.models import Recording

    if not isinstance(scope, QuerySet) or scope.model is not Recording:
        raise SearchQueryInputError(_SCOPE_TYPE_ERROR)
    if scope.query.is_sliced:
        raise SearchQueryInputError(_SCOPE_TYPE_ERROR)
    scoped = scope.order_by().values_list("pk", flat=True)
    try:
        sql, params = scoped.query.get_compiler(using=using).as_sql()
    except EmptyResultSet:
        return _EMPTY_SCOPE_SQL, []
    except Exception:
        raise SearchIndexError(_QUERY_FAILED_ERROR) from None
    return sql, list(params)


@dataclass(frozen=True)
class CompiledScope:
    """Immutable compiled Recording-scope value for trusted orchestration.

    Produced ONCE by :func:`compile_scope` from the exact
    ``_compile_scope`` logic (same validation, same-DB alias, empty-scope
    behaviour, innermost-WHERE placement and sanitized SQL/params — never
    forked). A hybrid orchestrator compiles the QuerySet exactly once,
    stores this SAME value in its semantic snapshot and passes it to
    ``search_recordings(compiled_scope=...)`` so the keyword engine never
    recompiles. ``sql`` is the compiled subquery (never echoed anywhere),
    ``params`` the bound parameters and ``using`` the database alias the
    scope was compiled against. Frozen: fields are never mutated.
    """

    sql: str
    params: tuple
    using: str


def compile_scope(scope, *, using: str) -> CompiledScope:
    """Compile an unsliced ``Recording`` QuerySet into an immutable
    :class:`CompiledScope` via the EXACT ``_compile_scope`` logic.

    Validation, same-DB alias, empty-scope behaviour and sanitized
    failures are never forked: this is a thin wrapper that captures the
    compiled SQL + parameters in an immutable value so a trusted
    orchestrator can compile once and share the result between the
    keyword engine and the semantic snapshot. ``scope`` may never be
    ``None`` — use the value only when a scope is supplied.
    """
    sql, params = _compile_scope(scope, using=using)
    return CompiledScope(sql=sql, params=tuple(params), using=using)


def _item_projection_names(query) -> tuple[str, ...]:
    """The selected column names of one compiled SELECT branch, in the
    compiler's own select order (plain values, then unmasked annotations,
    then extra selects). ``()`` for a default-column (all-model-fields)
    selection, which is NEVER a valid item-scope projection."""
    return (
        tuple(query.values_select)
        + tuple(query.annotation_select)
        + tuple(query.extra_select)
    )


@dataclass(frozen=True)
class CompiledItemScope:
    """Immutable compiled Library-item-scope value for trusted orchestration.

    Produced ONCE by :func:`compile_item_scope` from the one-column
    ``item_key`` UNION returned by ``workflow.query.library_item_key_queryset``
    (the normal Library item-identity set). Same value shape as
    :class:`CompiledScope` — ``sql`` the compiled subquery (never echoed
    anywhere), ``params`` the bound parameters and ``using`` the database
    alias it was compiled against — but over the ``r:<pk>``/``s:<pk>``
    item keys rather than Recording pks. Frozen: fields are never mutated.
    Consumed by :func:`search_recordings` via ``compiled_item_scope=``.
    """

    sql: str
    params: tuple
    using: str


def compile_item_scope(scope, *, using: str) -> CompiledItemScope:
    """Compile the one-column ``item_key`` UNION from
    ``workflow.query.library_item_key_queryset`` into an immutable
    :class:`CompiledItemScope`.

    A combined (UNION) QuerySet can never be re-projected, re-annotated
    or re-filtered, so — unlike the Recording-scope ``_compile_scope``,
    which FORCES a single-column PK selection — this compiler VALIDATES
    the exact built shape instead of forcing one:

    - accepts ONLY an UNSLICED ``QuerySet`` whose query is a ``union``
      combinator and whose combined query AND every branch project
      EXACTLY one column named ``item_key`` (anything else — SQL text, a
      mapping, a plain Recording queryset, a different or multi-column
      projection, a slice — is the same stable, content-free error);
    - an EMPTY UNION is VALID in every form the compiler proves empty
      (``library_item_key_queryset(...).none()``): ``EmptyResultSet``
      from ``as_sql`` is caught and answered with the same
      provably-empty subquery the Recording scope uses — never an error;
    - compiles on the SAME database alias used by the consuming query
      (the alias is stored on the value for the consumer to enforce);
    - every OTHER compilation failure is the same sanitized index failure
      as any query execution failure (``from None`` severs the context
      chain): compiled SQL, parameters, paths, the underlying exception
      text and the connection alias NEVER escape.

    The QuerySet is captured, never executed: compilation is SQL
    generation only (no DB round trip, no reads).
    """
    from django.core.exceptions import EmptyResultSet
    from django.db.models import QuerySet

    if not isinstance(scope, QuerySet):
        raise SearchQueryInputError(_ITEM_SCOPE_TYPE_ERROR)
    query = scope.query
    if query.is_sliced:
        raise SearchQueryInputError(_ITEM_SCOPE_TYPE_ERROR)
    branches = tuple(query.combined_queries)
    if (
        query.combinator != "union"
        or len(branches) < 2
        or _item_projection_names(query) != ("item_key",)
        or any(_item_projection_names(branch) != ("item_key",) for branch in branches)
    ):
        raise SearchQueryInputError(_ITEM_SCOPE_TYPE_ERROR)
    try:
        sql, params = query.get_compiler(using=using).as_sql()
    except EmptyResultSet:
        return CompiledItemScope(sql=_EMPTY_SCOPE_SQL, params=(), using=using)
    except Exception:
        raise SearchIndexError(_QUERY_FAILED_ERROR) from None
    return CompiledItemScope(sql=sql, params=tuple(params), using=using)


# Mirrors _DOC_TYPE_RANKS: within one Recording the per-recording
# candidate bound keeps Summary first, then Recording metadata, then
# Segments (document_key order inside each class) — a flood of matching
# segments can never evict the higher-priority candidates the
# comparator would prefer.
_DOC_TYPE_PRIORITY_SQL = (
    "CASE d.doc_type WHEN 'summary' THEN 0"
    " WHEN 'recording' THEN 1 ELSE 2 END"
)

# Same priority over the item-mode middle-level alias (Step 6.3): within
# one LIBRARY ITEM the per-item candidate bound keeps Summary first,
# then Recording metadata (recording items only), then Segments.
_ITEM_DOC_TYPE_PRIORITY_SQL = (
    "CASE m.doc_type WHEN 'summary' THEN 0"
    " WHEN 'recording' THEN 1 ELSE 2 END"
)


def _item_key_case(table: str = "d") -> tuple[str, list]:
    """The SQL-derived Library ITEM identity of one registry row
    (Step 6.3), mirroring the ``workflow.query`` Library item-union
    contract over the SHARED ``segmentation`` canonical-layout SQL (the
    same predicate the union uses — never forked, never a Python id set):

    - the parent Recording has NO canonical split layout ⇒ the item is
      ``r:<recording_id>`` (every document type of the Recording);
    - the parent Recording IS hidden by a canonical split layout ⇒
      an ACTIVE Summary variant maps to its own valid topic Section
      (``s:<section_id>``), a Segment maps to the valid topic Section
      owning its ordinal range, and anything the layout cannot own
      (parent metadata document, the fixed whole-recording Summary,
      cropped-out Segments, rows of stale/malformed/cross-parent
      layouts) derives ``NULL``.

    ``table`` is the SQL reference for the OUTER registry row: the
    default alias ``d`` (the keyword selection's registry alias — the
    generated SQL is byte-identical to the historical default) or the
    quoted registry TABLE NAME for the semantic traversal, whose
    page SELECT has no ``d`` alias (SQLite resolves quoted
    table-qualified references to its single FROM table; the correlated
    subselects qualify their own tables, so no reference is shadowed).

    Returns ``(sql, params)`` in SQL text order (hidden-recording
    subquery, then the valid-Section subquery of each scalar branch)."""
    hidden_sql, hidden_params = canonical_hidden_recording_ids()
    valid_sql, valid_params = canonical_active_section_ids()
    ref = f"{table}."
    sql = (
        "CASE"
        " WHEN " + ref + "recording_id NOT IN (" + hidden_sql + ")"
        " THEN 'r:' || " + ref + "recording_id"
        " WHEN " + ref + "doc_type = 'summary' AND " + ref + "summary_id IS NOT NULL THEN ("
        " SELECT 's:' || s.id FROM workflow_summary su"
        " JOIN workflow_section s ON s.id = su.section_id"
        " WHERE su.id = " + ref + "summary_id AND su.is_active = 1"
        " AND su.transcript_id = " + ref + "transcript_id"
        " AND s.transcript_id = " + ref + "transcript_id"
        " AND s.segmented_version_id IS NOT NULL"
        " AND s.id IN (" + valid_sql + "))"
        " WHEN " + ref + "doc_type = 'segment' THEN ("
        " SELECT 's:' || s.id FROM workflow_section s"
        " WHERE s.transcript_id = " + ref + "transcript_id"
        " AND s.segmented_version_id IS NOT NULL"
        " AND " + ref + "segment_ordinal >= s.start_segment_ordinal"
        " AND " + ref + "segment_ordinal < s.end_segment_ordinal_exclusive"
        " AND s.id IN (" + valid_sql + ") LIMIT 1)"
        " ELSE NULL END"
    )
    return sql, [*hidden_params, *valid_params, *valid_params]


def _selection_sql(where: str) -> str:
    registry = _registry_table()
    return (
        "SELECT id, document_key, doc_type, recording_id, transcript_id, "
        "summary_id, segment_ordinal, start_ms, end_ms, output_language, "
        "title_text, body_text, aux_text, recording_matches, "
        "total_matches FROM ("
        " SELECT d.id AS id, d.document_key AS document_key,"
        " d.doc_type AS doc_type, d.recording_id AS recording_id,"
        " d.transcript_id AS transcript_id, d.summary_id AS summary_id,"
        " d.segment_ordinal AS segment_ordinal, d.start_ms AS start_ms,"
        " d.end_ms AS end_ms, d.output_language AS output_language,"
        " d.title_text AS title_text, d.body_text AS body_text,"
        " d.aux_text AS aux_text,"
        " ROW_NUMBER() OVER ("
        f"PARTITION BY d.recording_id ORDER BY {_DOC_TYPE_PRIORITY_SQL},"
        " d.document_key) AS rn,"
        " COUNT(*) OVER (PARTITION BY d.recording_id) AS recording_matches,"
        " COUNT(*) OVER () AS total_matches"
        f" FROM {FTS_TABLE} JOIN {registry} d"
        f" ON d.id = {FTS_TABLE}.rowid"
        f" WHERE {where}"
        ") WHERE rn <= %s ORDER BY document_key LIMIT %s"
    )

_LIKE_GROUP = (
    "(brain_fold(d.title_text) LIKE %s ESCAPE '\\'"
    " OR brain_fold(d.body_text) LIKE %s ESCAPE '\\'"
    " OR brain_fold(d.aux_text) LIKE %s ESCAPE '\\')"
)


def _item_selection_sql(where: str, item_case: str, item_scope_sql: str) -> str:
    """Item-mode selection (Step 6.3). Load-bearing layering:

    - INNERMOST: the matched-set WHERE (terms only) plus the derived
      ``item_key`` CASE projected as a column;
    - MIDDLE: ``WHERE m.item_key IN (scope)`` restricts to the compiled
      Library-item scope, and the window functions of the SAME SELECT
      therefore compute over the in-scope item population ONLY —
      ``ROW_NUMBER`` partitions by ``m.item_key`` (per-ITEM bound),
      ``recording_matches`` counts per item and ``total_matches`` counts
      the whole in-scope candidate set. ``NULL`` item keys (parent
      metadata, fixed whole-recording Summary, cropped-out or
      unmappable rows) never satisfy ``IN`` and are excluded;
    - OUTER: the per-item bound, deterministic ``document_key`` order
      and the bounded fetch (identical semantics to recording mode).
    """
    registry = _registry_table()
    return (
        "SELECT id, document_key, doc_type, recording_id, transcript_id, "
        "summary_id, segment_ordinal, start_ms, end_ms, output_language, "
        "title_text, body_text, aux_text, item_key, "
        "recording_matches, total_matches FROM ("
        " SELECT m.*,"
        " ROW_NUMBER() OVER ("
        f"PARTITION BY m.item_key ORDER BY {_ITEM_DOC_TYPE_PRIORITY_SQL},"
        " m.document_key) AS rn,"
        " COUNT(*) OVER (PARTITION BY m.item_key) AS recording_matches,"
        " COUNT(*) OVER () AS total_matches"
        " FROM ("
        " SELECT d.id AS id, d.document_key AS document_key,"
        " d.doc_type AS doc_type, d.recording_id AS recording_id,"
        " d.transcript_id AS transcript_id, d.summary_id AS summary_id,"
        " d.segment_ordinal AS segment_ordinal, d.start_ms AS start_ms,"
        " d.end_ms AS end_ms, d.output_language AS output_language,"
        " d.title_text AS title_text, d.body_text AS body_text,"
        " d.aux_text AS aux_text,"
        f" {item_case} AS item_key"
        f" FROM {FTS_TABLE} JOIN {registry} d"
        f" ON d.id = {FTS_TABLE}.rowid"
        f" WHERE {where}"
        f" ) m WHERE m.item_key IN ({item_scope_sql})"
        ") WHERE rn <= %s ORDER BY document_key LIMIT %s"
    )


def _term_predicates(terms: list[str]) -> tuple[list[str], list, bool]:
    """AND-combined matched-set predicates for the user terms: long
    terms become one quoted MATCH phrase each; short terms become
    escaped Unicode-aware LIKE groups. Returns
    ``(predicates, params, needs_fold)``."""
    predicates: list[str] = []
    params: list = []
    needs_fold = False
    for term in terms:
        if len(term) >= 3:
            predicates.append(f"{FTS_TABLE} MATCH %s")
            params.append(_quote_phrase(term))
        else:
            needs_fold = True
            predicates.append(_LIKE_GROUP)
            pattern = _short_term_pattern(term)
            params.extend([pattern, pattern, pattern])
    return predicates, params, needs_fold


def _build_selection(
    terms: list[str],
    scope_sql: str | None = None,
    scope_params: list | None = None,
) -> tuple[str, list]:
    """Deterministic AND-combined selection SQL over the registry JOIN
    (never raw FTS order/rowid): long terms become one quoted MATCH
    phrase each; short terms become escaped Unicode-aware LIKE groups;
    an optional engine-compiled scope restricts ``d.recording_id`` to
    the eligibility subquery.

    Placement is load-bearing (Step 5A.4.2a): every predicate —
    including the scope — lands in the INNERMOST matched-set WHERE.
    SQL evaluates WHERE before the window functions of the same SELECT,
    so ``ROW_NUMBER``, both ``COUNT(*) OVER`` bounds, ``truncated`` and
    ``more_recordings_matched`` are computed over the in-scope
    population only. Param order: terms → scope → window bounds.
    """
    where, params, needs_fold = _term_predicates(terms)
    if scope_sql is not None:
        where.append(f"d.recording_id IN ({scope_sql})")
        params.extend(scope_params or [])
    sql = _selection_sql(" AND ".join(where))
    return sql, params, needs_fold


def _build_item_selection(
    terms: list[str],
    item_scope_sql: str,
    item_scope_params: list | None,
) -> tuple[str, list, bool]:
    """Item-mode counterpart of :func:`_build_selection` (Step 6.3): the
    term predicates select the matched set, the innermost projection
    derives each row's Library ``item_key`` and the scope filter lands
    in the MIDDLE WHERE — still before the window functions — so the
    per-item bound and both counts are item truths. Param order:
    item-identity case → terms → item scope → window bounds. The
    compiled scope SQL/params are engine-internal and never echoed."""
    where, term_params, needs_fold = _term_predicates(terms)
    item_case, case_params = _item_key_case()
    sql = _item_selection_sql(" AND ".join(where), item_case, item_scope_sql)
    return sql, [*case_params, *term_params, *(item_scope_params or [])], needs_fold


_COLUMNS = (
    "id",
    "document_key",
    "doc_type",
    "recording_id",
    "transcript_id",
    "summary_id",
    "segment_ordinal",
    "start_ms",
    "end_ms",
    "output_language",
    "title_text",
    "body_text",
    "aux_text",
)

# Item-mode selection columns (Step 6.3): the same columns plus the
# SQL-derived Library item key (the per-item/global window counts stay
# the final two columns of both row shapes — the positional window-count
# reads in ``_select_candidates`` rely on that).
_ITEM_COLUMNS = _COLUMNS + ("item_key",)


def _select_candidates(
    terms: list[str],
    *,
    using: str,
    max_scored_documents: int,
    per_recording_candidates: int,
    scope_sql: str | None = None,
    scope_params: list | None = None,
    item_scope_sql: str | None = None,
    item_scope_params: list | None = None,
) -> tuple[list[dict], bool, bool]:
    """Returns ``(candidates, truncated, recordings_complete)``.

    ``truncated`` is TRUE for ANY overflow of either bound — the global
    candidate bound (``total_matches`` from ``COUNT(*) OVER ()`` counts
    ALL matching candidates, including rows the per-unit bound removed)
    or a per-unit bound exceeded by any single unit. Overflow is
    detected from window counts, never inferred from the kept row
    count.

    ``recordings_complete`` is False only when the GLOBAL fetch bound
    actually cut rows (bound+1 detection): then entire units may be
    missing and the matched count is unknowable. A per-unit overflow
    keeps every matched unit represented (the bound always keeps the
    highest-priority candidate of each unit) so the unit set stays
    complete while that unit's best document stays approximate.

    The unit is the Recording (recording mode) or the Library item
    (item mode, ``item_scope_sql`` supplied): the SAME contract one
    level down — the derived ``item_key`` is restricted to the compiled
    item scope BEFORE the windows, ``per_recording_candidates`` is then
    the PER-ITEM bound and ``recording_matches`` counts per item.
    """
    if item_scope_sql is not None:
        sql, params, needs_fold = _build_item_selection(
            terms, item_scope_sql, item_scope_params
        )
        columns = _ITEM_COLUMNS
    else:
        sql, params, needs_fold = _build_selection(
            terms, scope_sql=scope_sql, scope_params=scope_params
        )
        columns = _COLUMNS
    if needs_fold:
        try:
            ensure_fold_function(using)
        except RuntimeError:
            raise SearchIndexError(_FOLD_UNAVAILABLE_SEARCH_ERROR) from None
    connection = connections[using]
    params = params + [per_recording_candidates, max_scored_documents + 1]
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            fetched = cursor.fetchall()
    except Exception:
        raise SearchIndexError(_QUERY_FAILED_ERROR) from None
    if fetched:
        total_matches = fetched[0][-1]
        per_recording_overflow = any(
            row[-2] > per_recording_candidates for row in fetched
        )
    else:
        total_matches = 0
        per_recording_overflow = False
    truncated = total_matches > max_scored_documents or per_recording_overflow
    recordings_complete = len(fetched) <= max_scored_documents
    rows = fetched[:max_scored_documents]
    return [dict(zip(columns, row[: len(columns)])) for row in rows], truncated, (
        recordings_complete
    )


# ---------------------------------------------------------------------------
# Scoring (shared Python fold contract; deterministic comparator)
# ---------------------------------------------------------------------------

_FIELD_RANKS = (("title_text", 0), ("body_text", 1), ("aux_text", 2))
_FIELD_NAMES = tuple(name for name, _ in _FIELD_RANKS)
_DOC_TYPE_RANKS = {"summary": 0, "recording": 1, "segment": 2}
_ABSENT = 1 << 60


def _find_all(haystack: str, needle: str) -> list[tuple[int, int]]:
    """Non-overlapping occurrence ranges (folded coordinates)."""
    ranges: list[tuple[int, int]] = []
    if not needle:
        return ranges
    start = haystack.find(needle)
    while start != -1:
        ranges.append((start, start + len(needle)))
        start = haystack.find(needle, start + len(needle))
    return ranges


def _score_document(row: dict, folded_terms: list[str]) -> dict:
    """Score one candidate with the SHARED fold contract. Returns the
    comparator inputs, per-field occurrence ranges and the
    pathological-divergence flag (a row FTS/LIKE selected but the Python
    fold cannot locate — kept, ranked last, never highlighted wrongly)."""
    folded = {field: fold_text(row[field] or "") for field in _FIELD_NAMES}
    field_ranges: dict[str, list[tuple[int, int]]] = {f: [] for f in _FIELD_NAMES}
    worst_field_rank = 0
    occurrences = 0
    first_offset = _ABSENT
    divergent = False
    for term_folded in folded_terms:
        term_best_rank = 3
        term_first = _ABSENT
        for field, rank in _FIELD_RANKS:
            ranges = _find_all(folded[field], term_folded)
            if ranges:
                field_ranges[field].extend(ranges)
                occurrences += len(ranges)
                term_best_rank = min(term_best_rank, rank)
                term_first = min(term_first, ranges[0][0])
        if term_best_rank == 3:
            divergent = True
        worst_field_rank = max(worst_field_rank, min(term_best_rank, 3))
        first_offset = min(first_offset, term_first)
    if divergent:
        occurrences = 0
        first_offset = _ABSENT
    return {
        "field_ranges": {f: sorted(v) for f, v in field_ranges.items()},
        "worst_field_rank": worst_field_rank,
        "doc_type_rank": _DOC_TYPE_RANKS.get(row["doc_type"], 3),
        "occurrences": occurrences,
        "first_offset": first_offset,
        "divergent": divergent,
    }


def _comparator(row: dict, score: dict) -> tuple:
    return (
        score["worst_field_rank"],
        score["doc_type_rank"],
        -score["occurrences"],
        score["first_offset"],
        row["document_key"],
    )


# ---------------------------------------------------------------------------
# Snippets (plain text + offset ranges; never HTML)
# ---------------------------------------------------------------------------


def _fold_with_spans(text: str | None) -> tuple[str, str, list[int], list[int]]:
    """Return ``(folded, nfc_text, starts, ends)`` for the shared fold
    contract: ``starts[p]``/``ends[p]`` are the INCLUSIVE source-
    character START and EXCLUSIVE END of the NFC character that
    produced folded position ``p``. Mapping a folded match range
    ``[start, end)`` as ``(starts[start], ends[end - 1])`` covers whole
    source characters — a match that lands PARTLY inside a casefold
    EXPANSION (``ﬃ``→``ffi``, ``ß``→``ss``) yields the whole expanded
    character's range, never a zero-length or wrong slice."""
    nfc = unicodedata.normalize("NFC", text or "")
    folded_chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for index, character in enumerate(nfc):
        folded = character.casefold()
        starts.extend([index] * len(folded))
        ends.extend([index + 1] * len(folded))
        folded_chars.append(folded)
    return "".join(folded_chars), nfc, starts, ends


def _build_snippet(row: dict, score: dict) -> dict | None:
    if score["divergent"]:
        return None
    chosen = None
    for field, _rank in _FIELD_RANKS:
        if score["field_ranges"][field]:
            chosen = field
            break
    if chosen is None:
        return None

    folded, nfc_text, starts, ends = _fold_with_spans(row[chosen])
    ranges = score["field_ranges"][chosen]
    # Folded coordinates -> INDIVIDUAL NFC ranges over WHOLE source
    # characters (exact spans; partial casefold expansions can never
    # produce a zero-length range). NO global merging here: repeated
    # text chains adjacent occurrences into one unbounded range, and
    # anchoring the window on that merged monster is exactly what
    # used to emit 1000-codepoint snippets.
    mapped = sorted((starts[s], ends[e - 1]) for s, e in ranges)

    anchor_start, anchor_end = mapped[0]
    # HARD-bounded window anchored on the FIRST INDIVIDUAL match: the
    # content window is ALWAYS at most SNIPPET_MAX_CODEPOINTS and,
    # whenever the anchor itself fits the cap (always true for a
    # single term match of up to SNIPPET_MAX_CODEPOINTS), it is fully
    # inside so the first match stays highlightable. The cap never
    # yields: a pathological single match longer than the cap (many
    # query codepoints folding into an expanded match, e.g. 200 ß ->
    # 400 folded positions) is shown from its start inside the capped
    # window, never emitted whole. The window clamps at both text
    # ends and shifts left near the end of the text.
    text_length = len(nfc_text)
    if text_length <= SNIPPET_MAX_CODEPOINTS:
        start, end = 0, text_length
    else:
        low = max(0, anchor_end - SNIPPET_MAX_CODEPOINTS)
        high = min(anchor_start, text_length - SNIPPET_MAX_CODEPOINTS)
        if low <= high:
            start = max(low, min(anchor_start - SNIPPET_RADIUS_CODEPOINTS, high))
        else:
            # Anchor longer than the cap: the bound wins, the match is
            # shown from its start (clipped) instead of oversized.
            start = max(0, min(anchor_start, text_length - SNIPPET_MAX_CODEPOINTS))
        end = start + SNIPPET_MAX_CODEPOINTS

    # Select the mapped ranges that intersect the window, clipped to
    # its bounds (every kept range stays non-empty, ordered and fully
    # within the returned text), and merge ONLY the displayed ranges.
    displayed: list[tuple[int, int]] = []
    for s, e in mapped:
        clipped = (max(s, start), min(e, end))
        if clipped[0] >= clipped[1]:
            continue
        if displayed and clipped[0] <= displayed[-1][1]:
            displayed[-1] = (displayed[-1][0], max(displayed[-1][1], clipped[1]))
        else:
            displayed.append(clipped)
    kept = displayed

    window = nfc_text[start:end]
    prefix = ELLIPSIS if start > 0 else ""
    suffix = ELLIPSIS if end < len(nfc_text) else ""
    shift = len(prefix)
    return {
        "field": chosen,
        "text": prefix + window + suffix,
        "matches": [
            {"start": s - start + shift, "end": e - start + shift} for s, e in kept
        ],
        "ellipsis_before": bool(prefix),
        "ellipsis_after": bool(suffix),
    }


# ---------------------------------------------------------------------------
# Result provenance + assembly
# ---------------------------------------------------------------------------

_SOURCE_NAMES = {"recording": "metadata", "summary": "summary", "segment": "segment"}


def _provenance(row: dict) -> dict:
    source = _SOURCE_NAMES.get(row["doc_type"], row["doc_type"])
    provenance = {"source": source, "document_key": row["document_key"]}
    if row["doc_type"] == "summary":
        provenance["output_language"] = row["output_language"]
        provenance["summary_id"] = row["summary_id"]
        provenance["transcript_id"] = row["transcript_id"]
    elif row["doc_type"] == "segment":
        provenance["transcript_id"] = row["transcript_id"]
        provenance["segment_ordinal"] = row["segment_ordinal"]
        provenance["start_ms"] = row["start_ms"]
        provenance["end_ms"] = row["end_ms"]
    return provenance


def _parse_item_key(item_key: str) -> tuple[str, str, int | None]:
    """Split one engine-derived item key into the result identity fields
    ``(item_key, item_kind, section_id)``. Only the two shapes the SQL
    derives are honoured (``r:<recording pk>`` / ``s:<section pk>``, the
    ``workflow.query`` Library item-union contract); anything else falls
    back DEFENSIVELY to a recording identity — the engine never
    fabricates a section id it cannot prove."""
    prefix, separator, remainder = (item_key or "").partition(":")
    if separator and prefix == "s" and remainder.isdigit():
        return item_key, "section", int(remainder)
    return item_key, "recording", None


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def search_recordings(
    query: str,
    *,
    limit: int = DEFAULT_RESULT_LIMIT,
    using: str = "default",
    max_scored_documents: int = MAX_SCORED_DOCUMENTS,
    per_recording_candidates: int = PER_RECORDING_CANDIDATES,
    scope=None,
    compiled_scope: CompiledScope | None = None,
    item_scope=None,
    compiled_item_scope: CompiledItemScope | None = None,
) -> dict:
    """Run one read-only keyword search against the existing index.

    NEVER runs the full health sweep (that is ``preflight_full_health``,
    the caller's policy), NEVER rebuilds, repairs, synchronizes, locks
    or writes. Returns one deduplicated result per Recording — per
    LIBRARY ITEM in item mode — with plain text snippets, structured
    highlight offsets and match provenance. Each result also carries
    the additive Library item-identity fields
    ``item_key``/``item_kind``/nullable ``section_id`` (see the module
    docstring): the whole Recording without an item scope, the
    SQL-derived owner of the matched document with one.

    ``scope`` (Step 5A.4.2a) is an optional constrained ``Recording``
    eligibility QuerySet (``workflow.query.search_scope_queryset``) —
    never SQL text. It restricts the candidate population INSIDE the
    innermost matched-set WHERE, i.e. BEFORE the window functions, so
    ranking, both candidate bounds, ``truncated`` and
    ``more_recordings_matched`` are in-scope truths and a lower-ranked
    in-scope Recording can never be evicted by out-of-scope matches.
    With ``scope=None`` the SQL and results are byte-identical to the
    unscoped engine (parity-tested).

    ``compiled_scope`` is the trusted-orchestration alternative: an
    immutable :class:`CompiledScope` produced ONCE by :func:`compile_scope`
    (which wraps the exact ``_compile_scope`` logic, so validation,
    same-DB alias, empty-scope behaviour and sanitized errors are never
    forked). When supplied, the engine uses it VERBATIM and never
    recompiles — the scope must have been compiled on the SAME database
    alias (``using``) or the call is rejected. Supplying BOTH ``scope``
    and ``compiled_scope`` is ambiguous and rejected with a fixed
    sanitized usage error. The QuerySet ``scope=`` path is unchanged and
    byte-identical to the historical behavior.

    ``item_scope`` / ``compiled_item_scope`` (Step 6.3) are the
    LIBRARY-ITEM-mode alternative to the Recording scope: the unsliced
    one-column ``item_key`` UNION from
    ``workflow.query.library_item_key_queryset`` (or its precompiled
    :class:`CompiledItemScope`, compiled ONCE on the SAME alias, which
    the engine uses VERBATIM and never recompiles). They are mutually
    exclusive with each other AND with ``scope``/``compiled_scope`` —
    exactly one eligibility mechanism per call, rejected otherwise with
    a fixed sanitized usage error. In item mode the innermost SQL
    derives every matched document's item identity from the SHARED
    segmentation canonical-layout SQL, the derived key is restricted to
    the compiled scope BEFORE the window functions, and the partition,
    per-item candidate bound, ``truncated``, the per-``item_key`` dedup
    and ``more_items_matched`` are all item truths; the historical
    ``more_recordings_matched`` stays as the SAME-VALUE compatibility
    alias, and the payload carries the explicit ``item_mode: true``
    flag (present ONLY in item mode, omitted outside it) so
    presentation counts Library items unconditionally — never from
    the returned rows. Titles remain the parent Recording's Library
    display title until the web slice renders section titles. Without
    an item scope the SQL and results are byte-identical to the
    unscoped engine.
    """
    normalized = normalize_query(query)
    _validate_limit(limit)
    if max_scored_documents < 1 or per_recording_candidates < 1:
        raise SearchQueryInputError(_BAD_LIMIT_ERROR)
    terms = _split_terms(normalized)
    folded_terms = [fold_text(term) for term in terms]

    _require_queryable_index(using=using)

    if scope is not None and compiled_scope is not None:
        raise SearchQueryInputError(_SCOPE_AMBIGUOUS_ERROR)
    if item_scope is not None and compiled_item_scope is not None:
        raise SearchQueryInputError(_ITEM_SCOPE_AMBIGUOUS_ERROR)
    if (item_scope is not None or compiled_item_scope is not None) and (
        scope is not None or compiled_scope is not None
    ):
        raise SearchQueryInputError(_SCOPE_ITEM_CONFLICT_ERROR)
    if compiled_scope is not None:
        if type(compiled_scope) is not CompiledScope:
            raise SearchQueryInputError(_COMPILED_SCOPE_TYPE_ERROR)
        if compiled_scope.using != using:
            raise SearchQueryInputError(_SCOPE_ALIAS_ERROR)
        scope_sql = compiled_scope.sql
        scope_params = list(compiled_scope.params)
    elif scope is not None:
        scope_sql, scope_params = _compile_scope(scope, using=using)
    else:
        scope_sql = scope_params = None

    if compiled_item_scope is not None:
        if type(compiled_item_scope) is not CompiledItemScope:
            raise SearchQueryInputError(_COMPILED_ITEM_SCOPE_TYPE_ERROR)
        if compiled_item_scope.using != using:
            raise SearchQueryInputError(_ITEM_SCOPE_ALIAS_ERROR)
        item_scope_sql = compiled_item_scope.sql
        item_scope_params = list(compiled_item_scope.params)
    elif item_scope is not None:
        compiled_items = compile_item_scope(item_scope, using=using)
        item_scope_sql = compiled_items.sql
        item_scope_params = list(compiled_items.params)
    else:
        item_scope_sql = item_scope_params = None
    item_mode = item_scope_sql is not None

    candidates, truncated, selection_complete = _select_candidates(
        terms,
        using=using,
        max_scored_documents=max_scored_documents,
        per_recording_candidates=per_recording_candidates,
        scope_sql=scope_sql,
        scope_params=scope_params,
        item_scope_sql=item_scope_sql,
        item_scope_params=item_scope_params,
    )

    # One winner per unit: the Recording (recording mode) or the
    # derived Library item (item mode).
    best: dict[str, tuple[tuple, dict, dict]] = {}
    for row in candidates:
        score = _score_document(row, folded_terms)
        key = _comparator(row, score)
        unit = row["item_key"] if item_mode else row["recording_id"]
        current = best.get(unit)
        if current is None or key < current[0]:
            best[unit] = (key, row, score)

    winners = sorted(
        best.values(),
        key=lambda item: (
            item[0],
            item[1]["item_key"] if item_mode else item[1]["recording_id"],
        ),
    )
    page = winners[:limit]
    # EXACT only while the matched-unit set is known complete (the
    # global fetch bound did not cut rows); null (unknown) otherwise —
    # never guessed. A per-unit-only overflow keeps every matched unit
    # present, so the count stays exact there while ``truncated`` still
    # honestly reports the approximate winner.
    more_items_matched: int | None = (
        max(len(winners) - len(page), 0) if selection_complete else None
    )

    titles = _lookup_titles([row["recording_id"] for _key, row, _s in page], using=using)

    results = []
    for rank, (_key, row, score) in enumerate(page, start=1):
        provenance = _provenance(row)
        provenance["fields_matched"] = [
            field for field in _FIELD_NAMES if score["field_ranges"][field]
        ]
        provenance["occurrences"] = score["occurrences"]
        if item_mode:
            item_key, item_kind, section_id = _parse_item_key(row["item_key"])
        else:
            item_key = f"r:{row['recording_id']}"
            item_kind = "recording"
            section_id = None
        results.append(
            {
                "rank": rank,
                "recording_id": row["recording_id"],
                # Additive Library item identity mirroring the
                # workflow.query Library item-union contract: the whole
                # Recording without an item scope; in item mode the
                # SQL-derived owner of the matched document.
                "item_key": item_key,
                "item_kind": item_kind,
                "section_id": section_id,
                # Titles stay the parent Recording's Library display
                # title until the web slice renders section titles.
                "title": titles.get(row["recording_id"], ""),
                "match": provenance,
                "snippet": _build_snippet(row, score),
            }
        )

    payload = {
        "query": normalized,
        "index_version": INDEX_VERSION,
        "limit": limit,
        "results": results,
        "result_count": len(results),
        "truncated": truncated,
        # Additive Step 6.3 item-level matched-unit count; the
        # historical Recording key stays as the SAME-VALUE
        # compatibility alias (item mode counts items, recording mode
        # counts Recordings — identical values either way).
        "more_items_matched": more_items_matched,
        "more_recordings_matched": more_items_matched,
    }
    if item_mode:
        # The EXPLICIT item-mode flag (see the module docstring): in
        # Library-item mode the matched unit IS a Library item, so
        # presentation counts Library items unconditionally — never
        # inferred from the returned rows (they may all be Recordings
        # while the omitted matches include Sections). Omitted entirely
        # outside item mode, so the legacy payload is byte-identical.
        payload["item_mode"] = True
    return payload


def _lookup_titles(recording_ids: list[str], *, using: str) -> dict[str, str]:
    """Library display titles from the canonical per-Recording metadata
    documents; bounded chunked lookups; missing rows fall back to ""."""
    from workflow.models import SearchDocType, SearchDocument

    titles: dict[str, str] = {}
    ids = sorted(set(recording_ids))
    for index in range(0, len(ids), _TITLE_LOOKUP_CHUNK):
        chunk = ids[index : index + _TITLE_LOOKUP_CHUNK]
        rows = (
            SearchDocument.objects.using(using)
            .filter(
                doc_type=SearchDocType.RECORDING,
                recording_id__in=chunk,
            )
            .values_list("recording_id", "title_text")
        )
        titles.update({recording_id: title for recording_id, title in rows})
    return titles
