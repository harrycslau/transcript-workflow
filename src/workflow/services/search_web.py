"""Read-only web keyword-search orchestration (Step 5A.4.2a/b).

The one home of the Library search flow: parse → validate → FULL
health gate EXACTLY once per submitted search (no cache — a cached
health could serve stale or deleted content and safe invalidation
across web and CLI mutations is not provable) → the reusable
``workflow.services.search_query`` engine with a constrained RECORDING
SCOPE (filters apply to the candidate population BEFORE result limiting
and pagination) → deterministic sorting → pagination over the returned
match set → one bounded prefetch-contracted card fetch for the page
window.

Step 5A.4.2b adds the display-safe rendering contract ON TOP of the
same flow — still zero new queries beyond ONE bounded batch validation
per page:

- snippets become ordered PLAIN-TEXT :class:`SnippetFragment` lists
  (the deterministic malformed-range policy below); the template wraps
  ``mark=True`` fragments in semantic ``<mark>`` elements. This module
  never builds HTML, never uses ``mark_safe``/``SafeString``; Django
  autoescaping on the exact ``str`` fragments is the whole safety story.
- segment provenance links to the ACTIVE transcript page carrying the
  indexed ordinal, through the ONE batch query in
  :func:`_resolve_segment_links` (active AND same-Recording pair
  validation). Any doubt — inactive, missing, foreign-owned transcript,
  malformed provenance — keeps the plain non-link chip, never a guessed
  link and never a 500.

Everything here stays strictly read-only: no writes, no pipeline lock,
no synchronization/rebuild, no network, no subprocess. Query/result
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

from django.core.exceptions import ValidationError
from django.core.paginator import Page, Paginator

from brainlib.config import ConfigError
from workflow.models import Transcript
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

# Mirrors the config default so the service never needs the config layer
# to compute a segment page; callers always pass the configured value.
DEFAULT_SEGMENTS_PER_PAGE = 200


# ---------------------------------------------------------------------------
# Snippet fragments (Step 5A.4.2b): plain text, never HTML
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SnippetFragment:
    """One plain-text piece of a snippet. ``mark=True`` pieces are the
    template's job to wrap in ``<mark>``; every ``text`` is an exact
    plain ``str`` — never a ``SafeString`` and never coerced input."""

    text: str
    mark: bool


def _clean_ranges(text: str, matches) -> list[tuple[int, int]]:
    """Deterministic malformed-range policy for ONE snippet.

    Individual malformed entries (non-dict, non-``int``/``bool``
    endpoints, inverted or out-of-text ranges after clamping) are
    ignored; surviving ranges are clamped, sorted, and touching or
    overlapping ranges are merged. Never raises.
    """
    if not isinstance(matches, list):
        return []
    length = len(text)
    cleaned: list[tuple[int, int]] = []
    for item in matches:
        if not isinstance(item, dict):
            continue
        start, end = item.get("start"), item.get("end")
        if isinstance(start, bool) or isinstance(end, bool):
            continue
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        start, end = max(0, start), min(length, end)
        if start < end:
            cleaned.append((start, end))
    cleaned.sort()
    merged: list[list[int]] = []
    for start, end in cleaned:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def snippet_fragments(snippet) -> list[SnippetFragment]:
    """Split one engine snippet into display-safe plain-text fragments.

    The ONE deterministic policy (never raises, never coerces types):

    - missing/non-dict snippet or non-``str``/empty text → NO fragments
      (the row renders no snippet at all);
    - unusable ``matches`` shape or no valid range surviving the
      clean-up → exactly ONE unmarked fragment with the full text;
    - otherwise the fragments partition the complete snippet text
      exactly, with the cleaned/clamped/merged ranges marked.

    Every fragment's ``text`` is an EXACT built-in ``str``: an accepted
    str SUBCLASS is rebuilt through the UNBOUND base-str conversion,
    which runs NO subclass hook — a subclass ``__str__`` could vouch
    for itself as trusted HTML and a hostile ``__iter__`` could raise —
    so template autoescaping can never be bypassed by the object a
    caller handed in, and no accepted input can make this function
    raise through a subclass override. Fragments are plain data; the
    templates alone decide markup.
    """
    if not isinstance(snippet, dict):
        return []
    text = snippet.get("text")
    if not isinstance(text, str) or text == "":
        return []
    # An EXACT built-in ``str`` is required for the autoescaping
    # contract, and the conversion may not run ANY subclass hook:
    # ``str(x)`` dispatches to the subclass ``__str__`` (Django's
    # trusted-string wrapper returns THE SAME OBJECT there — an
    # autoescaping bypass in the no-valid-range path) and an iterable
    # copy would call the subclass ``__iter__`` (hostile subclasses
    # raise). ``str.__str__(text)`` is the unbound base-str
    # conversion: no user code runs and a subclass becomes an exact
    # base-str copy with identical content. Non-str objects stay
    # rejected above (no coercion of foreign types).
    if type(text) is not str:
        text = str.__str__(text)
        if type(text) is not str:  # defensive belt-and-braces
            return []
    ranges = _clean_ranges(text, snippet.get("matches"))
    if not ranges:
        return [SnippetFragment(text, False)]
    fragments: list[SnippetFragment] = []
    cursor = 0
    for start, end in ranges:
        if start > cursor:
            fragments.append(SnippetFragment(text[cursor:start], False))
        fragments.append(SnippetFragment(text[start:end], True))
        cursor = end
    if cursor < len(text):
        fragments.append(SnippetFragment(text[cursor:], False))
    return fragments


def _clean_transcript_pk(value) -> int | None:
    """Strict positive-integer validation for Transcript integer pks.

    Accepts only what ``Transcript._meta.pk.to_python`` accepts as an
    integer AND the value itself is not a bool/float; rejects None,
    negatives, zero and malformed strings. Transcript pks are
    BigAutoField INTEGERS (Recording pks stay UUID strings — the two id
    spaces are validated differently on purpose).
    """
    if value is None or isinstance(value, bool) or isinstance(value, float):
        return None
    try:
        pk = Transcript._meta.pk.to_python(value)
    except (ValidationError, TypeError, ValueError):
        return None
    if isinstance(pk, bool) or not isinstance(pk, int):
        return None
    return pk if pk > 0 else None


def _clean_ordinal(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


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
    plain-text snippet fragments and provenance label/link primitives
    (already display-safe strings and ints; the template escapes them)."""

    card: RecordingCard
    snippet_text: str
    match_label: str
    match_source: str
    fragments: list[SnippetFragment] = field(default_factory=list)
    # Set together only when the segment provenance was batch-validated
    # against the DB (active transcript of THIS recording); otherwise
    # both stay None and the chip renders as a plain label.
    link_page: int | None = None
    link_anchor: str | None = None


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


def _resolve_segment_links(
    entries: list[tuple], segments_per_page: int
) -> dict[str, tuple[int, str]]:
    """ONE bounded SELECT validates EVERY segment provenance on the page.

    A chip becomes a link ONLY for a row whose indexed transcript pk
    survives strict integer validation, is still ``is_active`` AND
    belongs to the SAME Recording as the result row — the
    ``(transcript_id, recording_id)`` pair is validated together, so a
    forged or stale cross-recording provenance can never produce a link
    that jumps into another recording. Anything else keeps the plain
    chip. Segments are immutable per Transcript, so an active same-owner
    transcript guarantees the ordinal exists; the 0-based ordinal maps
    deterministically onto the transcript page that renders it.
    """
    validated: dict[str, tuple[int, int]] = {}
    for recording, result in entries:
        match = result["match"]
        if match.get("source") != "segment":
            continue
        transcript_pk = _clean_transcript_pk(match.get("transcript_id"))
        ordinal = _clean_ordinal(match.get("segment_ordinal"))
        if transcript_pk is None or ordinal is None:
            continue
        validated[_norm_id(recording.pk)] = (transcript_pk, ordinal)
    if not validated:
        return {}
    transcript_pks = [pk for pk, _ordinal in validated.values()]
    active_owner = dict(
        Transcript.objects.filter(pk__in=transcript_pks, is_active=True).values_list(
            "pk", "recording_id"
        )
    )
    links: dict[str, tuple[int, str]] = {}
    for rec_id, (transcript_pk, ordinal) in validated.items():
        owner = active_owner.get(transcript_pk)
        if owner is None or _norm_id(owner) != rec_id:
            continue
        links[rec_id] = (ordinal // segments_per_page + 1, f"segment-{ordinal}")
    return links


def _build_rows(
    page_ids: list[str], result_by_id: dict[str, dict], segments_per_page: int
) -> list[SearchRow]:
    """ONE prefetch-contracted fetch for the page window (plus the one
    bounded link-validation SELECT when any segment provenance is
    present); rows follow the pre-computed page order. A row that
    vanished between the gate and this read (a racing deletion) is
    skipped — never a fake row."""
    if not page_ids:
        return []
    by_id = {}
    for recording in recording_list_queryset().filter(pk__in=page_ids):
        by_id[_norm_id(recording.pk)] = recording
    entries: list[tuple] = []
    for pk in page_ids:
        recording = by_id.get(pk)
        if recording is None:
            continue
        entries.append((recording, result_by_id[pk]))
    link_by_id = _resolve_segment_links(entries, segments_per_page)
    rows: list[SearchRow] = []
    for recording, result in entries:
        fragments = snippet_fragments(result.get("snippet"))
        link = link_by_id.get(_norm_id(recording.pk))
        rows.append(
            SearchRow(
                card=RecordingCard(recording),
                snippet_text="".join(fragment.text for fragment in fragments),
                match_label=_match_label(result["match"]),
                match_source=result["match"].get("source") or "",
                fragments=fragments,
                link_page=None if link is None else link[0],
                link_anchor=None if link is None else link[1],
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
    segments_per_page: int = DEFAULT_SEGMENTS_PER_PAGE,
    using: str = "default",
) -> SearchOutcome:
    """One submitted Library search, strictly read-only, in the approved
    order: cheap validation -> FULL health gate exactly once -> scoped
    engine -> search-aware sorting -> pagination -> bounded card fetch
    (with the one bounded segment-link validation SELECT).
    """
    sort = filters.sort if filters.sort else SORT_RELEVANCE
    try:
        segments_per_page = max(1, int(segments_per_page))
    except (TypeError, ValueError):
        segments_per_page = DEFAULT_SEGMENTS_PER_PAGE

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

    rows = _build_rows(page_ids, result_by_id, segments_per_page)

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
