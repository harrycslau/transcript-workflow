"""Read-only web keyword-search orchestration (Step 5A.4.2a/b) plus the
POST-only semantic/hybrid web orchestration (Step 5C).

The one home of the Library search flow: parse → validate → FULL
health gate EXACTLY once per submitted search (no cache — a cached
health could serve stale or deleted content and safe invalidation
across web and CLI mutations is not provable) → the reusable
``workflow.services.search_query`` engine with the normal Library's
LIBRARY-ITEM SCOPE (the exact one-column ``item_key`` UNION of
``workflow.query.library_item_key_queryset``; filters apply to the
candidate population BEFORE result limiting and pagination) →
deterministic sorting → pagination over the returned match set → one
bounded Library item hydration of the page window.

Library item scope and hydration (Step 6.3): search results mirror the
normal Library replacement — every winner is a Library ITEM identified
by the engine's additive ``item_key`` (``r:<recording_id>`` /
``s:<section_id>``), so a valid active split layout yields EXACTLY the
active Section items with the parent Recording SUPPRESSED (never parent
+ section duplicates) while unsplit/crop-only/historical/malformed
recordings answer their single Recording item. The page window is
hydrated by ``workflow.query.library_items_by_keys`` into the same
:class:`~workflow.query.LibraryItemCard` adapters the Library renders
(one bounded revalidation of every winner key through the exact
normal-Library identity semantics with the SAME scope filters the
engine ran with, then the shared batched hydration): a stale engine key
never resurrects a deleted, replaced or filtered-out item. The
templates render the SAME item-native presentation as the normal
Library (derived item title, item duration, item-scoped tags and
summary languages, Section links to the section detail plus the
parent/range context) with the search snippet/provenance riding on
top; Section links carry NO library-return token (search-origin
return is not supported).

Keyword search stays the historical GET flow (``run_web_search``).
Step 5C adds ``run_web_vector_search`` for the dedicated POST-only
endpoint: mode allowlist → cheap query validation → invalid scope
filters REJECT (never widened) → the same valid Library item scope →
``semantic_query.semantic_search`` / ``search_fusion.hybrid_search``
(each owns exactly one source health sweep, one integrity traversal and
at most one localhost embedding request; this layer adds none) → the
SAME shared sorting/pagination/bounded item hydration. Every service failure
is one stable ``unavailable`` outcome with a fixed sanitized message and
the query cleared.

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
winner set in the database before pagination through the EXACT normal
Library item sort (item display title under the Unicode collation,
parent effective date, unique ``item_key`` tie-break) — a Section
winner sorts by ITS OWN derived title, never by its parent only.
Truncation/more-match notes are scoped-honest: they describe only what
the engine can prove, and the beyond-window unit comes from the
engine's explicit ``item_mode`` flag (Library-item mode says "library
items" unconditionally, whether or not any visible or matched row is a
Section; a Recording-mode/hand-built payload without the flag keeps the
historical "recordings" wording) — never inferred from the rows visible
on the page.
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
    LibraryItemCard,
    ListFilters,
    RecordingCard,
    apply_item_sort,
    library_item_key_queryset,
    library_item_queryset,
    library_items_by_keys,
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
# Step 5C vector modes: every service failure (source index, embedding
# schema/generation, endpoint/timeout/http, concurrent change, generic)
# is ONE stable unavailable state with the service's fixed sanitized
# message — never a partial/fallback result and never an echoed query.
STATE_UNAVAILABLE = "unavailable"

# Search modes (Step 5C web). Keyword is the historical GET mode; the
# vector modes are POST-only (dedicated endpoint) and NEVER fall back to
# keyword.
MODE_KEYWORD = "keyword"
MODE_SEMANTIC = "semantic"
MODE_HYBRID = "hybrid"
VECTOR_MODES = (MODE_SEMANTIC, MODE_HYBRID)

# Fixed sanitized web messages — never interpolate query, filters, codes
# or underlying exception text.
INVALID_MODE_MESSAGE = "Choose a valid search mode: 'semantic' or 'hybrid'."
INVALID_VECTOR_FILTERS_MESSAGE = (
    "The submitted filters are invalid; adjust them and try again."
)

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
# Library-ITEM-mode variants (Step 6.3): chosen from the engine's
# explicit ``item_mode`` flag (Library-item mode matched LIBRARY ITEMS,
# so an item count NEVER claims "recordings" regardless of whether any
# visible row is a Section). A payload without the flag (recording mode
# or a hand-built legacy payload) keeps the historical Recording
# wordings verbatim.
NOTE_MORE_EXACT_ITEMS = (
    "{more} more library items also matched these filters (showing the "
    "{scan} most relevant)."
)
NOTE_MORE_UNKNOWN_ITEMS = (
    "More library items may match; the total is unknown because the "
    "candidate limit was reached."
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


def _winner_key(result: dict) -> str:
    """The stable winner identity for the maps/pagination of one payload.

    Every engine row carries the additive Library item identity
    ``item_key`` (``r:<recording_id>`` for a Recording item,
    ``s:<section_id>`` for a topic-Section item), so several Library
    items of ONE Recording are DISTINCT winners — never dict-collapsed
    by the parent Recording id. A row without the additive field (the
    defensive/legacy payload shape) falls back to the whole Recording's
    canonical ``r:`` key — a recording identity is never forged into a
    Section identity.
    """
    item_key = result.get("item_key")
    if isinstance(item_key, str) and item_key:
        return item_key
    return f"r:{_norm_id(result['recording_id'])}"


def _match_label(match: dict) -> str:
    """Plain provenance label for 5A.4.2a (links/highlights are 5A.4.2b)."""
    source = match.get("source")
    if source == "summary":
        language = match.get("output_language") or ""
        return f"summary · {language}" if language else "summary"
    if source == "segment":
        return f"segment · {mmss(match.get('start_ms'))}"
    return "recording metadata"


def _evidence_label(evidence) -> str:
    """Plain-text hybrid component-rank suffix (``kw #1 · sem #2``).

    Only exact integer ranks (never bools) are rendered; missing/absent
    component ranks are omitted. Returns an exact built-in ``str`` built
    from fixed text and integers — no raw scores, no vectors, no
    generated HTML. Non-dict evidence yields the empty string.
    """
    if not isinstance(evidence, dict):
        return ""
    parts: list[str] = []
    keyword_rank = evidence.get("keyword_rank")
    semantic_rank = evidence.get("semantic_rank")
    if isinstance(keyword_rank, int) and not isinstance(keyword_rank, bool):
        parts.append(f"kw #{keyword_rank}")
    if isinstance(semantic_rank, int) and not isinstance(semantic_rank, bool):
        parts.append(f"sem #{semantic_rank}")
    return " · ".join(parts)


def build_notes(payload: dict, sort: str, scan_limit: int = WEB_SCAN_LIMIT) -> list[str]:
    """Scope-honest notes derived ONLY from engine-proven facts.

    The beyond-window note is about SORTING a truncated/limited window:
    it applies exactly when the returned match set is known INCOMPLETE
    (``more_recordings_matched > 0`` or unknown ``None``) and the user
    asked for a non-relevance order. Pure candidate-bound truncation
    with the FULL winner set present (``more == 0``) does NOT make the
    sort window-limited — the truncation note says all there is to say.

    The UNIT of the beyond-window note is the engine's EXPLICIT
    ``item_mode`` flag, never the visible rows: Library-item mode
    matched LIBRARY ITEMS, so the note counts items even when every
    visible row is a Recording and the omitted matches are Sections. A
    payload without the flag (recording mode, or a legacy/hand-built
    payload) keeps the historical Recording wordings verbatim.
    """
    notes: list[str] = []
    more = payload.get("more_recordings_matched")
    item_units = bool(payload.get("item_mode"))
    if payload.get("truncated"):
        notes.append(NOTE_TRUNCATED)
    if more:
        exact = NOTE_MORE_EXACT_ITEMS if item_units else NOTE_MORE_EXACT
        notes.append(exact.format(more=more, scan=scan_limit))
    elif more is None:
        notes.append(
            NOTE_MORE_UNKNOWN_ITEMS if item_units else NOTE_MORE_UNKNOWN
        )
    incomplete_window = more is None or bool(more)
    if sort != SORT_RELEVANCE and incomplete_window:
        notes.append(NOTE_SORT_WINDOW)
    return notes


@dataclass(frozen=True)
class SearchRow:
    """One result row: the hydrated Library item card plus the engine's
    plain-text snippet fragments and provenance label/link primitives
    (already display-safe strings and ints; the template escapes them).

    Hydration always hands a :class:`LibraryItemCard` (a Section winner
    carries its own item identity plus the parent Recording's prefetched
    :class:`RecordingCard`); the templates render through the item-card
    contract exactly like the normal Library branches."""

    card: RecordingCard | LibraryItemCard
    snippet_text: str
    match_label: str
    match_source: str
    fragments: list[SnippetFragment] = field(default_factory=list)
    # Set together only when the segment provenance was batch-validated
    # against the DB (active transcript of THIS recording); otherwise
    # both stay None and the chip renders as a plain label.
    link_page: int | None = None
    link_anchor: str | None = None
    # Hybrid only: a plain-text component-rank suffix (empty otherwise).
    evidence_label: str = ""


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
    mode: str = MODE_KEYWORD

    @property
    def searching(self) -> bool:
        return True

    @property
    def echo_query(self) -> str | None:
        return self.query if self.echo_allowed else None

    @property
    def is_vector(self) -> bool:
        """Whether this outcome belongs to a POST-only vector mode."""
        return self.mode in VECTOR_MODES


def _ordered_winner_ids(
    winner_ids: list[str],
    sort: str,
    *,
    filters: ListFilters,
    timezone_name: str,
    using: str,
) -> list[str]:
    """Order the returned winner set BEFORE pagination.

    ``relevance`` keeps the engine order untouched (never a database
    ORDER BY); the Library sorts order the SAME winner keys through the
    EXACT normal-Library item sort (:func:`workflow.query.apply_item_sort`
    over ``library_item_queryset`` restricted to the winners): Title
    sorts fold the ITEM display title (a Section's own derived topic
    title, a Recording's fallback chain — never a parent-only order),
    date sorts use the parent effective date, and the unique
    ``item_key`` is the shared deterministic tie-breaker, so the search
    order can never diverge from the normal Library's ordering of the
    same items. One bounded UNION query at most (the per-branch IN
    predicate never exceeds the engine result cap); never applied
    page-by-page. A winner that no longer names a normal Library item
    (vanished, replaced, filtered out) drops here — the hydration
    revalidation would drop it anyway.
    """
    if sort == SORT_RELEVANCE:
        return winner_ids
    ordered = apply_item_sort(
        library_item_queryset(
            filters,
            timezone_name,
            using=using,
            item_keys=list(dict.fromkeys(winner_ids)),
        ),
        sort,
    )
    rank = {row["item_key"]: index for index, row in enumerate(ordered)}
    return sorted((key for key in winner_ids if key in rank), key=lambda key: rank[key])


def _resolve_segment_links(
    entries: list[tuple], segments_per_page: int
) -> dict[str, tuple[int, str]]:
    """ONE bounded SELECT validates EVERY segment provenance on the page.

    A chip becomes a link ONLY for a winner whose indexed transcript pk
    survives strict integer validation, is still ``is_active`` AND
    belongs to the SAME parent Recording as the hydrated item card (for
    a Section winner, the Section's own transcript belongs to that same
    parent) — the ``(transcript_id, recording_id)`` pair is validated
    together, so a forged or stale cross-recording provenance can never
    produce a link that jumps into another recording. Anything else
    keeps the plain chip. Segments are immutable per Transcript, so an
    active same-owner transcript guarantees the ordinal exists; the
    0-based ordinal maps deterministically onto the transcript page that
    renders it. Links are keyed by the WINNER key (Step 6.3): two
    Section winners of one Recording carry their own ordinals and can
    never overwrite each other.
    """
    validated: dict[str, tuple[int, int, str]] = {}
    for winner_key, card, result in entries:
        match = result["match"]
        if match.get("source") != "segment":
            continue
        transcript_pk = _clean_transcript_pk(match.get("transcript_id"))
        ordinal = _clean_ordinal(match.get("segment_ordinal"))
        if transcript_pk is None or ordinal is None:
            continue
        validated[winner_key] = (
            transcript_pk,
            ordinal,
            _norm_id(card.recording.pk),
        )
    if not validated:
        return {}
    transcript_pks = list({pk for pk, _ordinal, _rec in validated.values()})
    active_owner = dict(
        Transcript.objects.filter(pk__in=transcript_pks, is_active=True).values_list(
            "pk", "recording_id"
        )
    )
    links: dict[str, tuple[int, str]] = {}
    for winner_key, (transcript_pk, ordinal, rec_id) in validated.items():
        owner = active_owner.get(transcript_pk)
        if owner is None or _norm_id(owner) != rec_id:
            continue
        links[winner_key] = (
            ordinal // segments_per_page + 1,
            f"segment-{ordinal}",
        )
    return links


def _item_key_for_card(card: LibraryItemCard) -> str:
    """The canonical Library ``item_key`` of one hydrated card.

    Mirrors EXACTLY the two spellings the Library UNION projects
    (``s:<section pk>`` / ``r:<canonical recording id>``), rebuilt only
    from the card's own projected row identity — so a hydrated card maps
    back onto the winner key that requested it without ever touching
    ``library_items_by_keys``' internals.
    """
    if card.is_section:
        return f"s:{card.section_id}"
    return f"r:{_norm_id(card.recording_id)}"


def _build_rows(
    page_keys: list[str],
    result_by_id: dict[str, dict],
    segments_per_page: int,
    *,
    filters: ListFilters,
    timezone_name: str,
    using: str,
) -> list[SearchRow]:
    """Hydrate ONLY the page window through ``library_items_by_keys``
    (one bounded revalidation of every winner key against the EXACT
    normal-Library identity under the SAME scope filters the engine ran
    with, plus the shared batched hydration and the one bounded
    link-validation SELECT when any segment provenance is present);
    rows follow the pre-computed page order.

    A winner whose item vanished, was replaced by a valid active split
    layout, moved to a superseded layout or fell out of the filters
    between the engine and this read is skipped with its key — never a
    fake row and never a resurrected stale item. Card contract: the
    templates render through the card's parent-Recording accessors too,
    so a card whose parent Recording vanished between the revalidation
    and the hydration read is dropped with it.
    """
    if not page_keys:
        return []
    cards = library_items_by_keys(page_keys, filters, timezone_name, using=using)
    card_by_key = {_item_key_for_card(card): card for card in cards}
    entries: list[tuple] = []
    for winner_key in page_keys:
        card = card_by_key.get(winner_key)
        if card is None or card.recording is None:
            continue
        entries.append((winner_key, card, result_by_id[winner_key]))
    link_by_key = _resolve_segment_links(entries, segments_per_page)
    rows: list[SearchRow] = []
    for winner_key, card, result in entries:
        fragments = snippet_fragments(result.get("snippet"))
        link = link_by_key.get(winner_key)
        rows.append(
            SearchRow(
                card=card,
                snippet_text="".join(fragment.text for fragment in fragments),
                match_label=_match_label(result["match"]),
                match_source=result["match"].get("source") or "",
                fragments=fragments,
                link_page=None if link is None else link[0],
                link_anchor=None if link is None else link[1],
                evidence_label=_evidence_label(result.get("evidence")),
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
    engine -> search-aware sorting -> pagination -> bounded Library item
    hydration of the page window (with the one bounded segment-link
    validation SELECT).
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

    # 3. Scope: valid filters restrict the ENGINE candidate set itself —
    #    through the normal Library's ITEM scope (Step 6.3): a valid
    #    active split layout yields EXACTLY the Section items with the
    #    parent Recording suppressed, never parent + section duplicates.
    #    Invalid scope filters mirror the Library policy (show errors,
    #    ignore the filters) and run the search over the UNFILTERED
    #    canonical Library item scope — still ITEM mode (canonical parent
    #    replacement, Section items, per-item bounds and notes), never
    #    the historical whole-Recording engine mode — honestly labelled.
    #    The SAME filters drive the page-window item revalidation, so
    #    the fallback hydration never drops items the ignored filters
    #    would have excluded. An invalid sort never reaches here
    #    un-normalized.
    unscoped = not filters.scope_valid
    scope_filters = filters if not unscoped else ListFilters()
    item_scope = library_item_key_queryset(
        scope_filters, timezone_name, using=using
    )

    # 4. The engine (structural queryability errors land in the same
    #    friendly index state; neither message ever contains the query).
    try:
        payload = search_query.search_recordings(
            query, limit=WEB_SCAN_LIMIT, using=using, item_scope=item_scope
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
        key = _winner_key(result)
        result_by_id[key] = result
        winner_ids.append(key)

    # 5. Sorting across the returned match set, THEN pagination —
    #    never the other way around.
    rows, page, result_count = _finish_results(
        result_by_id,
        winner_ids,
        sort=sort,
        page_number=page_number,
        per_page=per_page,
        segments_per_page=segments_per_page,
        scope_filters=scope_filters,
        timezone_name=timezone_name,
        using=using,
    )

    return SearchOutcome(
        state=STATE_OK,
        query=query,
        echo_allowed=True,
        sort=sort,
        rows=rows,
        page=page,
        result_count=result_count,
        notes=build_notes(payload, sort, scan_limit=WEB_SCAN_LIMIT),
        unscoped_filters=unscoped,
        mode=MODE_KEYWORD,
    )


def _finish_results(
    result_by_id: dict[str, dict],
    winner_ids: list[str],
    *,
    sort: str,
    page_number,
    per_page: int,
    segments_per_page: int,
    scope_filters: ListFilters,
    timezone_name: str,
    using: str,
):
    """Shared tail for keyword and vector modes: order the returned winner
    set, paginate it, then hydrate ONLY the page window through
    ``library_items_by_keys`` — one bounded item revalidation with the
    EXACT scope filters the engine ran on plus the shared batched
    hydration (and the one bounded segment-link validation SELECT).
    Returns ``(rows, page, result_count)``. Relevance order is the
    engine's comparator output untouched; the Library sorts re-order the
    SAME returned winner set with the EXACT normal-Library item sort
    (item display title / parent effective date / ``item_key``
    tie-break) before pagination."""
    ordered_ids = _ordered_winner_ids(
        winner_ids,
        sort,
        filters=scope_filters,
        timezone_name=timezone_name,
        using=using,
    )
    paginator = Paginator(ordered_ids, max(1, int(per_page)))
    page = paginator.get_page(page_number)
    page_keys = list(page.object_list)
    rows = _build_rows(
        page_keys,
        result_by_id,
        segments_per_page,
        filters=scope_filters,
        timezone_name=timezone_name,
        using=using,
    )
    return rows, page, len(ordered_ids)


def run_web_vector_search(
    *,
    mode: str,
    raw_query: str | None,
    filters: ListFilters,
    timezone_name: str,
    page_number,
    per_page: int,
    config,
    segments_per_page: int = DEFAULT_SEGMENTS_PER_PAGE,
    using: str = "default",
    embedder=None,
) -> SearchOutcome:
    """One submitted POST-only Library semantic/hybrid search.

    Order (single source of truth): mode allowlist → cheap query
    validation → invalid-scope-filter REJECTION (never widened to an
    unscoped search) → the same valid Library item scope as keyword
    search → the mode's service (which owns EXACTLY one source health
    sweep, one integrity traversal and at most one localhost embedding
    request; this layer adds NO sweep, NO embedding and NO fallback) →
    the shared sorting/pagination/bounded item hydration.

    Every service failure (source index, embedding schema/generation,
    endpoint/timeout/http, concurrent change, generic) is ONE stable
    ``unavailable`` outcome carrying the service's fixed sanitized
    message with the query cleared (``echo_allowed=False``), so the
    rejected text appears NOWHERE in the response and never in a URL.
    Keyword :func:`run_web_search` is untouched.
    """
    sort = filters.sort if filters.sort else SORT_RELEVANCE
    try:
        segments_per_page = max(1, int(segments_per_page))
    except (TypeError, ValueError):
        segments_per_page = DEFAULT_SEGMENTS_PER_PAGE

    # 1. Mode allowlist (never silently keyword, never network).
    if mode not in VECTOR_MODES:
        return SearchOutcome(
            state=STATE_INVALID, query=None, echo_allowed=False,
            message=INVALID_MODE_MESSAGE, sort=sort, mode=MODE_KEYWORD,
        )

    # 2. Cheap input validation BEFORE health/network (mirrors the CLI).
    #    Imported lazily so the keyword GET path never touches the
    #    embedding services.
    from workflow.services import search_fusion, semantic_query

    try:
        if mode == MODE_SEMANTIC:
            query = semantic_query.validate_semantic_query(raw_query, WEB_SCAN_LIMIT)
        else:
            query = search_fusion.validate_hybrid_query(raw_query, WEB_SCAN_LIMIT)
    except (semantic_query.SemanticQueryInputError, search_fusion.HybridSearchInputError) as exc:
        return SearchOutcome(
            state=STATE_INVALID, query=None, echo_allowed=False,
            message=str(exc), sort=sort, mode=mode,
        )

    # 3. Invalid scope filters REJECT (never widen to an unscoped search)
    #    BEFORE any health/network work.
    if not filters.scope_valid:
        return SearchOutcome(
            state=STATE_INVALID, query=None, echo_allowed=False,
            message=INVALID_VECTOR_FILTERS_MESSAGE, sort=sort, mode=mode,
        )

    # 4. The same valid Library item scope the keyword engine uses: the
    #    exact one-column item_key UNION of the normal Library identity.
    #    Handed to the service UNCOMPILED — ``hybrid_search`` owns the
    #    EXACTLY-ONE compilation through the shared
    #    ``search_query.compile_item_scope`` and shares that immutable
    #    value with both components; a second compilation here would
    #    fork that contract.
    item_scope = library_item_key_queryset(filters, timezone_name, using=using)

    # 5. Delegate: the service owns the one-sweep/one-embed contract.
    try:
        if mode == MODE_SEMANTIC:
            payload = semantic_query.semantic_search(
                query,
                limit=WEB_SCAN_LIMIT,
                using=using,
                item_scope=item_scope,
                config=config,
                embedder=embedder,
            )
        else:
            payload = search_fusion.hybrid_search(
                query,
                limit=WEB_SCAN_LIMIT,
                using=using,
                item_scope=item_scope,
                config=config,
                embedder=embedder,
            )
    except ConfigError as exc:
        # Every service failure is already a fixed sanitized message
        # (SemanticQueryError / HybridSearchError / shared ConfigError);
        # never a raw code, path or traceback.
        return SearchOutcome(
            state=STATE_UNAVAILABLE, query=None, echo_allowed=False,
            message=str(exc), sort=sort, mode=mode,
        )

    results = payload.get("results", [])
    result_by_id: dict[str, dict] = {}
    winner_ids: list[str] = []
    for result in results:
        key = _winner_key(result)
        result_by_id[key] = result
        winner_ids.append(key)

    rows, page, result_count = _finish_results(
        result_by_id,
        winner_ids,
        sort=sort,
        page_number=page_number,
        per_page=per_page,
        segments_per_page=segments_per_page,
        # The filters are scope-valid here (invalid ones rejected above),
        # so the revalidation applies exactly the scope the service ran on.
        scope_filters=filters,
        timezone_name=timezone_name,
        using=using,
    )

    return SearchOutcome(
        state=STATE_OK,
        query=query,
        echo_allowed=True,
        sort=sort,
        rows=rows,
        page=page,
        result_count=result_count,
        notes=build_notes(payload, sort, scan_limit=WEB_SCAN_LIMIT),
        unscoped_filters=False,
        mode=mode,
    )
