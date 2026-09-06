"""Read-only web keyword-search orchestration (Step 5A.4.2a).

The one home of the Library search flow: parse → validate → FULL
health gate EXACTLY once per submitted search (no cache — a cached
health could serve stale or deleted content and safe invalidation
across web and CLI mutations is not provable) → the reusable
``workflow.services.search_query`` engine with a constrained RECORDING
SCOPE (filters apply to the candidate population BEFORE result limiting
and pagination) → deterministic sorting → pagination over the returned
match set → one bounded prefetch-contracted card fetch for the page
window.

Everything here is strictly read-only: no writes, no pipeline lock, no
synchronization/rebuild, no network, no subprocess. Query/result
rendering stays the view/template's job; this module never builds HTML.

Health/validation states (``SearchOutcome.state``):

- ``ok``            — engine answered (zero results included); the
  normalized query may be echoed (autoescaped rendering is the view's
  contract);
- ``invalid``       — malformed query (empty after strip never reaches
  here: the view treats it as the normal Library); fixed bound message;
- ``index``         — the full sweep (or the engine's structural check)
  reported a missing/broken/stale index; the sanitized stable message
  names ``search-index status`` / ``rebuild``.

Privacy (approved): invalid/index outcomes clear the query
(``echo_allowed=False``) so the rejected text appears NOWHERE in the
response; error messages are engine-provided fixed texts that never
contain the query.

Sorting contract (search mode): ``relevance`` preserves the engine's
comparator order over the returned winners and is never translated into
database ordering; the four Library sorts re-order the SAME returned
winner set in the database before pagination. Truncation/more-match
notes are scoped-honest: they describe only what the engine can prove.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

from django.core.paginator import Page, Paginator

from brainlib.config import ConfigError
from workflow.query import (
    ListFilters,
    RecordingCard,
    apply_sort,
    recording_list_queryset,
    search_scope_queryset,
)
from workflow.services import search_query
from workflow.templatetags.workflow_extras import mmss

logger = logging.getLogger(__name__)

# The web scans the engine's hard result cap (imported, never
# duplicated): filters already narrowed the population inside the
# engine, so this window bounds only the RETURNED per-page metadata
# payload; anything beyond it is reported honestly.
WEB_SCAN_LIMIT = search_query.MAX_RESULT_LIMIT

SORT_RELEVANCE = "relevance"

STATE_OK = "ok"
STATE_INVALID = "invalid"
STATE_INDEX = "index"

# Exact approved wordings — scoped-honest, never corpus-wide claims.
NOTE_TRUNCATED = (
    "Too many candidate matches to rank exhaustively; ranking may be "
    "approximate for some recordings."
)
NOTE_MORE_EXACT = (
    "{more} more recordings also matched these filters (showing the {scan} "
    "most relevant)."
)
NOTE_MORE_UNKNOWN = (
    "More recordings may match; the total is unknown because the candidate "
    "limit was reached."
)
NOTE_SORT_WINDOW = (
    "Sorting applies to the returned most-relevant matches, not to matches "
    "beyond this window."
)
NOTE_UNSCOPED_FILTERS = "Search ran without the invalid filters."

UNINDEXED_NOTE_HELP = (
    "Keyword search needs the search index; run it from a terminal."
)


def _norm_id(value) -> str:
    """Canonical lowercase-hyphenated UUID string for engine/DB joins.

    The raw engine selection returns the storage-format string while
    the ORM hands back ``uuid.UUID`` objects; every map in this module
    keys on ONE canonical form.
    """
    if isinstance(value, UUID):
        return str(value)
    return str(UUID(str(value)))


def _match_label(match: dict) -> str:
    """Plain provenance label for 5A.4.2a (links/highlights are 5A.4.2b)."""
    source = match.get("source")
    if source == "summary":
        language = match.get("output_language") or ""
        return f"summary · {language}" if language else "summary"
    if source == "segment":
        return f"segment · {mmss(match.get('start_ms'))}"
    return "recording metadata"


def build_notes(payload: dict, sort: str, scan_limit: int = WEB_SCAN_LIMIT) -> list[str]:
    """Scope-honest notes derived ONLY from engine-proven facts.

    The beyond-window note is about SORTING a truncated/limited window:
    it applies exactly when the returned match set is known INCOMPLETE
    (``more_recordings_matched > 0`` or unknown ``None``) and the user
    asked for a non-relevance order. Pure candidate-bound truncation
    with the FULL winner set present (``more == 0``) does NOT make the
    sort window-limited — the truncation note says all there is to say.
    """
    notes: list[str] = []
    more = payload.get("more_recordings_matched")
    if payload.get("truncated"):
        notes.append(NOTE_TRUNCATED)
    if more:
        notes.append(NOTE_MORE_EXACT.format(more=more, scan=scan_limit))
    elif more is None:
        notes.append(NOTE_MORE_UNKNOWN)
    incomplete_window = more is None or bool(more)
    if sort != SORT_RELEVANCE and incomplete_window:
        notes.append(NOTE_SORT_WINDOW)
    return notes


@dataclass(frozen=True)
class SearchRow:
    """One result row: the prefetch-contracted card plus the engine's
    plain-text snippet and provenance label (already display-safe
    strings; the template escapes them)."""

    card: RecordingCard
    snippet_text: str
    match_label: str
    match_source: str


@dataclass
class SearchOutcome:
    state: str
    query: str | None
    echo_allowed: bool
    message: str | None = None
    sort: str = SORT_RELEVANCE
    rows: list[SearchRow] = field(default_factory=list)
    page: Page | None = None
    result_count: int = 0
    notes: list[str] = field(default_factory=list)
    unscoped_filters: bool = False

    @property
    def searching(self) -> bool:
        return True

    @property
    def echo_query(self) -> str | None:
        return self.query if self.echo_allowed else None


def _ordered_winner_ids(winner_ids: list[str], sort: str) -> list[str]:
    """Order the returned winner set BEFORE pagination.

    ``relevance`` keeps the engine order untouched (never a database
    ORDER BY); the Library sorts order the SAME winner ids in the
    database (title sorts through the annotated display_title and the
    Unicode collation). One query at most; never applied page-by-page.
    """
    if sort == SORT_RELEVANCE:
        return winner_ids
    ordered = recording_list_queryset().filter(pk__in=winner_ids)
    ordered = apply_sort(ordered, sort).values_list("pk", flat=True)
    return [_norm_id(pk) for pk in ordered]


def _build_rows(
    page_ids: list[str], result_by_id: dict[str, dict]
) -> list[SearchRow]:
    """ONE prefetch-contracted fetch for the page window; rows follow the
    pre-computed page order. A row that vanished between the gate and
    this read (a racing deletion) is skipped — never a fake row."""
    if not page_ids:
        return []
    by_id = {}
    for recording in recording_list_queryset().filter(pk__in=page_ids):
        by_id[_norm_id(recording.pk)] = recording
    rows: list[SearchRow] = []
    for pk in page_ids:
        recording = by_id.get(pk)
        if recording is None:
            continue
        result = result_by_id[pk]
        snippet = result.get("snippet") or {}
        rows.append(
            SearchRow(
                card=RecordingCard(recording),
                snippet_text=snippet.get("text") or "",
                match_label=_match_label(result["match"]),
                match_source=result["match"].get("source") or "",
            )
        )
    return rows


def run_web_search(
    *,
    raw_query: str,
    filters: ListFilters,
    timezone_name: str,
    page_number,
    per_page: int,
    using: str = "default",
) -> SearchOutcome:
    """One submitted Library search, strictly read-only, in the approved
    order: cheap validation -> FULL health gate exactly once -> scoped
    engine -> search-aware sorting -> pagination -> bounded card fetch.
    """
    sort = filters.sort if filters.sort else SORT_RELEVANCE

    # 1. Cheap input validation BEFORE any sweep (mirrors the CLI).
    try:
        query = search_query.validate_query(raw_query, WEB_SCAN_LIMIT)
    except search_query.SearchQueryInputError as exc:
        return SearchOutcome(
            state=STATE_INVALID, query=None, echo_allowed=False,
            message=str(exc), sort=sort,
        )

    # 2. The FULL read-only integrity sweep, EXACTLY once per submitted
    #    search (no health cache). Stale/missing/broken ⇒ no results.
    try:
        search_query.preflight_full_health(using=using)
    except ConfigError as exc:
        return SearchOutcome(
            state=STATE_INDEX, query=None, echo_allowed=False,
            message=str(exc), sort=sort,
        )

    # 3. Scope: valid filters restrict the ENGINE candidate set itself.
    #    Invalid scope filters mirror the Library policy (show errors,
    #    ignore the filters) and run the search unscoped, honestly
    #    labelled. An invalid sort never reaches here un-normalized.
    unscoped = not filters.scope_valid
    scope = None if unscoped else search_scope_queryset(filters, timezone_name)

    # 4. The engine (structural queryability errors land in the same
    #    friendly index state; neither message ever contains the query).
    try:
        payload = search_query.search_recordings(
            query, limit=WEB_SCAN_LIMIT, using=using, scope=scope
        )
    except search_query.SearchQueryInputError as exc:
        return SearchOutcome(
            state=STATE_INVALID, query=None, echo_allowed=False,
            message=str(exc), sort=sort,
        )
    except ConfigError as exc:
        return SearchOutcome(
            state=STATE_INDEX, query=None, echo_allowed=False,
            message=str(exc), sort=sort,
        )

    results = payload.get("results", [])
    result_by_id: dict[str, dict] = {}
    winner_ids: list[str] = []
    for result in results:
        key = _norm_id(result["recording_id"])
        result_by_id[key] = result
        winner_ids.append(key)

    # 5. Sorting across the returned match set, THEN pagination —
    #    never the other way around.
    ordered_ids = _ordered_winner_ids(winner_ids, sort)
    paginator = Paginator(ordered_ids, max(1, int(per_page)))
    page = paginator.get_page(page_number)
    page_ids = list(page.object_list)

    rows = _build_rows(page_ids, result_by_id)

    return SearchOutcome(
        state=STATE_OK,
        query=query,
        echo_allowed=True,
        sort=sort,
        rows=rows,
        page=page,
        result_count=len(ordered_ids),
        notes=build_notes(payload, sort, scan_limit=WEB_SCAN_LIMIT),
        unscoped_filters=unscoped,
    )
