"""Recording list/query helpers (Step 4 web interface).

This module owns the list-page query contract:

- ``recording_list_queryset()`` builds the base queryset with the
  ``effective_at`` annotation (Coalesce(recorded_at, discovered_at) —
  one effective timestamp used consistently for ordering, date
  filtering and display) and explicit ``Prefetch`` objects with
  ``to_attr`` lists. Row rendering MUST read only those ``to_attr``
  lists (via :class:`RecordingCard`) and never issue per-row queries
  such as ``Recording.current_summary()``.

- ``list_filters``/``apply_filters`` parse and validate the query
  string; invalid values become friendly error messages, never 500s.

- ``local_day_bounds`` computes timezone-aware local calendar-day
  boundaries (DST-correct) for date filters. Naive datetimes are never
  compared.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from django.db.models import (
    Case,
    CharField,
    Count,
    DateTimeField,
    Exists,
    F,
    FloatField,
    IntegerField,
    Max,
    Min,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Concat
from django.db.models.expressions import Func, RawSQL
from django.db.models.query import QuerySet
from django.utils import timezone as dj_tz

from brainlib.config import tag_name_key
from workflow.models import (
    AudioStatus,
    ProcessingStatus,
    Recording,
    RoutingDecision,
    AudioSource,
    Section,
    Summary,
    SummaryState,
    SummaryVariantState,
    Tag,
    TagAssignment,
    Transcript,
    TranscriptSegment,
)
from workflow.services.langresolve import default_output_language_expression
from workflow.services.library_metadata import (
    TITLE_PLACEHOLDER,
    display_title_from_recording,
)
from workflow.services.search_query import MAX_RESULT_LIMIT
from workflow.services.segmentation import (
    canonical_active_section_ids,
    canonical_hidden_recording_ids,
)
from workflow.sqlite_unicode import COLLATION_NAME, ensure_registered, folded_title_expression

MAX_TAG_FILTERS = 10

VALID_PROCESSING_STATUSES = {value for value, _ in ProcessingStatus.choices}
VALID_SUMMARY_STATUSES = {value for value, _ in SummaryState.choices}

SORT_CHOICES = ("newest", "oldest", "title_az", "title_za")


def effective_at_annotation() -> Coalesce:
    return Coalesce("recorded_at", "discovered_at", output_field=DateTimeField())


def current_summary_prefetch(to_attr: str = "current_summary_rows") -> Prefetch:
    return Prefetch(
        "summaries",
        queryset=Summary.objects.filter(
            is_active=True,
            transcript__is_active=True,
            section__ordinal=0,
            section__segmented_version__isnull=True,
        )
        # Multilingual: several variants can be active in scope. The
        # list card shows a DETERMINISTIC row (lowest ordinal); the
        # language-correct view lives in the recording detail/summary
        # pages via the variant view-model.
        .order_by("ordinal")
        .only(
            "id",
            "recording",
            "transcript",
            "section",
            "ordinal",
            "title",
            "overview",
            "language",
            "output_language",
            "is_active",
            "created_at",
            "model_id",
            "generation_mode",
        ),
        to_attr=to_attr,
    )


def _default_output_language_subquery() -> Subquery:
    """Default output language of a recording's ACTIVE transcript.

    Uses the ORM expression from ``langresolve`` (the language-policy
    layer) so the derived value always agrees with
    ``resolve_default_language`` — no policy duplication here.
    """
    return Subquery(
        Transcript.objects.filter(recording=OuterRef("pk"), is_active=True)
        .annotate(_default_output=default_output_language_expression())
        .values("_default_output")[:1],
        output_field=CharField(max_length=32),
    )


def _summary_title_subquery(*, default_language: bool) -> Subquery:
    """Deterministic whole-recording Summary title (active transcript,
    ordinal-0 section). With ``default_language=True`` the row is
    restricted to the recording's derived default output language."""
    queryset = Summary.objects.filter(
        transcript__recording=OuterRef("pk"),
        transcript__is_active=True,
        section__ordinal=0,
        section__segmented_version__isnull=True,
        is_active=True,
    )
    if default_language:
        queryset = queryset.filter(output_language=OuterRef("default_output_language"))
    return Subquery(
        queryset.order_by("ordinal").values("title")[:1],
        output_field=CharField(max_length=200),
    )


def _source_filename_subquery() -> Subquery:
    """Deterministic preferred AudioSource filename: canonical first,
    then earliest ``first_seen_at``, then PK."""
    return Subquery(
        AudioSource.objects.filter(recording=OuterRef("pk"))
        .order_by("-is_canonical", "first_seen_at", "pk")
        .values("original_filename")[:1],
        output_field=CharField(max_length=255),
    )


def _display_title_expression() -> Coalesce:
    """The single Library title source, used for BOTH rendering and
    Title A–Z/Z–A ordering.

    Fallback chain: active default-language Summary title → any active
    whole-recording Summary title (deterministic) → preferred AudioSource
    filename → ``Untitled recording``.
    """
    return Coalesce(
        _summary_title_subquery(default_language=True),
        _summary_title_subquery(default_language=False),
        _source_filename_subquery(),
        Value(TITLE_PLACEHOLDER),
    )


def recording_list_queryset(*, include_title: bool = True) -> QuerySet:
    """Base list queryset: effective ordering + the full prefetch contract.

    The contract: callers render rows exclusively through
    :class:`RecordingCard` over the ``to_attr`` lists populated here
    (``current_summary_rows``, ``active_tag_assignments``,
    ``active_routing_decisions``, ``presentation_sources``). No
    per-row queries are allowed in the loop.

    ``include_title=False`` (Library item hydration) drops ONLY the
    ``display_title`` annotation: ``RecordingCard.title`` then falls back
    to the pure Python reference implementation over the prefetched
    ``to_attr`` lists, so no summary-title subqueries are needed.
    """
    qs = Recording.objects.annotate(
        effective_at=effective_at_annotation(),
        default_output_language=_default_output_language_subquery(),
    )
    if include_title:
        qs = qs.annotate(display_title=_display_title_expression())
    return (
        qs.select_related("last_failed_attempt")
        .prefetch_related(
            current_summary_prefetch(),
            Prefetch(
                "tag_assignments",
                # Defense-in-depth: only recording-scoped assignments feed
                # the Library tag chips (section tags are a Step 6.2
                # per-section concern, not a recording-level one).
                queryset=TagAssignment.objects.filter(
                    is_active=True, section__isnull=True
                ).select_related("tag"),
                to_attr="active_tag_assignments",
            ),
            Prefetch(
                "routing_decisions",
                queryset=RoutingDecision.objects.filter(is_active=True).only(
                    "id",
                    "recording",
                    "ordinal",
                    "route_suggestion",
                    "profile_name",
                    "model_id",
                    "language_arg",
                    "method",
                    "confidence",
                    "reason_code",
                    "routing_verified",
                    "is_active",
                ),
                to_attr="active_routing_decisions",
            ),
            Prefetch(
                "sources",
                queryset=AudioSource.objects.only(
                    "id", "recording", "original_filename", "is_canonical", "presence", "first_seen_at"
                ),
                to_attr="presentation_sources",
            ),
        )
        .order_by("-effective_at", "pk")
    )


class RecordingCard:
    """Presentation adapter over the prefetched ``to_attr`` lists.

    Constructed once per row; every attribute access below is served
    from memory. The queryset contract guarantees each list has the
    expected cardinality (active routing ≤ 1; current summary rows are
    ordered deterministically by ordinal — with multilingual variants
    several rows can be active in scope, and the language-correct view
    lives on the detail/summary pages via the variant view-model).
    """

    def __init__(self, recording: Recording) -> None:
        self.recording = recording

    @property
    def current_summary(self) -> Summary | None:
        rows = getattr(self.recording, "current_summary_rows", [])
        return rows[0] if rows else None

    @property
    def display_summary(self) -> Summary | None:
        """The single Summary row driving all card Summary content.

        Selects the prefetched row matching the annotated default output
        language (the same language ``display_title`` uses), falling back
        to the deterministic lowest-ordinal active whole-recording
        Summary when that variant is absent. Never issues a query.
        """
        rows = getattr(self.recording, "current_summary_rows", [])
        if not rows:
            return None
        default_lang = getattr(self.recording, "default_output_language", None)
        if default_lang:
            for row in rows:
                if row.output_language == default_lang:
                    return row
        return rows[0]

    @property
    def title(self) -> str:
        """The Library title: the exact annotated ``display_title`` value
        used for rendering AND Title A–Z/Z–A ordering (fallback chain:
        default-language Summary → any active Summary → preferred source
        filename → placeholder)."""
        value = getattr(self.recording, "display_title", None)
        if value:
            return value
        # Single-object use without the annotation: the shared Python
        # reference implementation of the same chain (prefetch-only).
        return display_title_from_recording(self.recording)

    @property
    def available_languages(self) -> list[str]:
        """Sorted unique output languages already available (active
        whole-recording Summaries in scope). Read from the prefetch only —
        no related-manager access, constant query count."""
        return sorted(
            {
                summary.output_language
                for summary in getattr(self.recording, "current_summary_rows", [])
                if summary.output_language
            }
        )

    @property
    def month_label(self) -> str:
        """Local "Month YYYY" group label for chronological sorts."""
        return month_label_for(self.effective_at)

    @property
    def overview_excerpt(self) -> str:
        summary = self.display_summary
        if summary is None:
            return ""
        text = unicodedata.normalize("NFC", summary.overview).strip()
        if len(text) > 220:
            text = text[:220].rstrip() + "…"
        return text

    @property
    def active_tags(self) -> list[TagAssignment]:
        return list(getattr(self.recording, "active_tag_assignments", []))

    @property
    def active_route(self) -> RoutingDecision | None:
        rows = getattr(self.recording, "active_routing_decisions", [])
        return rows[0] if rows else None

    @property
    def display_source(self) -> AudioSource | None:
        sources = list(getattr(self.recording, "presentation_sources", []))
        if not sources:
            return None
        canonical = next((s for s in sources if s.is_canonical), None)
        return canonical or sources[0]

    @property
    def effective_at(self) -> datetime | None:
        # Annotated on the queryset; fall back for single-object use.
        value = getattr(self.recording, "effective_at", None)
        if value is not None:
            return value
        return self.recording.recorded_at or self.recording.discovered_at

    @property
    def effective_at_label(self) -> str:
        return "Recorded" if self.recording.recorded_at else "Discovered"

    @property
    def needs_attention(self) -> bool:
        r = self.recording
        decision = self.active_route
        return bool(
            r.processing_status == ProcessingStatus.NEEDS_REVIEW
            or r.processing_status == ProcessingStatus.FAILED
            or r.retranscription_failed
            or r.resummarization_failed
            or r.summary_status == SummaryState.FAILED
            or r.audio_status == AudioStatus.MISSING
            or (r.processing_status == ProcessingStatus.TRANSCRIBED and decision is not None and not decision.routing_verified)
        )


# ---------------------------------------------------------------------------
# Library item projection (Step 6.2)
#
# The Library overview unit is a DERIVED Library item, not necessarily a
# Recording:
#
# - any Recording with NO active topic Sections (unprocessed/no
#   transcript, unsplit active transcript, or a crop-only active layout
#   with zero Sections) yields exactly ONE recording-backed item;
# - an active transcript/layout with N canonical topic Sections (valid
#   N >= 2) yields exactly those N section-backed items and REPLACES its
#   recording-backed item in the normal Library overview;
# - historical layouts are absent; retranscription (no new segmented
#   version) naturally returns to one recording-backed item.
#
# There is deliberately NO persisted LibraryItem model: the projection is
# a read-only DB UNION of same-shaped recording-backed and active-topic
# branches (``library_item_queryset``), so count/pagination/order all
# happen BEFORE hydration and no Python code ever expands all recordings.
# Each page is then hydrated by ``hydrate_library_items`` in bounded
# batched queries (no N+1) into :class:`LibraryItemCard` adapters.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Canonical active-topic-layout state — LAZY database-side, never a Python
# expansion.
#
# The "which recordings are replaced by their topic Sections" and "which
# Section pks are valid topic items" questions are answered by the ONE
# shared parameterized read-only SQL predicate OWNED by
# :mod:`workflow.services.segmentation` (``canonical_layout_predicate`` —
# the SQL twin of the shared Python canonical validator, including the
# Step 6.2a temporary-title SHAPE rule). The Step 6.3 search index reuses
# the SAME predicate; it is never forked here. Both UNION branches embed
# it as a RawSQL subquery, so:
#
# - the number of active layouts/sections NEVER materializes in Python and
#   NEVER grows SQL parameters (the subquery carries exactly two fixed
#   parameters: MAX_TOPIC_SECTIONS and MAX_TOPIC_TITLE_LENGTH);
# - count/order/pagination stay exact database-side (the branches are the
#   same UNION; only the hidden/valid set has become a SQL predicate);
# - every canonical fail-closed rule lives in the ONE segmentation
#   predicate (see its section comment for the full rule list).
#
# ``_HIDDEN_RECORDINGS_SQL_RAW`` selects the parent Recording pks (the
# recording branch excludes them); ``_VALID_SECTIONS_SQL_RAW`` selects the
# valid topic Section pks (the Section branch filters on them). Both reuse
# the SAME segmentation predicate text and parameter list.
# ---------------------------------------------------------------------------


_HIDDEN_RECORDINGS_SQL_RAW = RawSQL(*canonical_hidden_recording_ids())
_VALID_SECTIONS_SQL_RAW = RawSQL(*canonical_active_section_ids())


# The one shared column list of the Library item UNION (identical names and
# order on BOTH branches so Django's ``union()`` aligns them positionally).
_ITEM_COLUMNS = (
    "item_key",
    "item_kind",
    "recording_id",
    "section_id",
    "display_title",
    "title_fold",
    "effective_at",
    "default_output_language",
    "processing_status",
    "summary_status",
    "audio_status",
    "retranscription_failed",
    "resummarization_failed",
    "has_route_unverified",
    "recorded_at",
    "range_start",
    "range_end",
    "duration_seconds",
)


def _fold_expression(title_expression):
    """``<title-expression> COLLATE unicode_fold`` as a SELECT expression.

    The collation must be applied INSIDE each UNION branch (the ORDER BY
    of a compound statement cannot reference a differently-collated
    column): the folded value is projected as the ``title_fold`` column
    and Title A–Z/Z–A sorts the UNION by that projected column.
    """
    return Func(
        title_expression,
        function=COLLATION_NAME,
        template="%(expressions)s COLLATE %(function)s",
    )


def _route_unverified_expression():
    """Exists: the parent recording has an ACTIVE unverified routing decision."""
    return Exists(
        RoutingDecision.objects.filter(
            recording=OuterRef("transcript__recording_id"),
            is_active=True,
            routing_verified=False,
        )
    )


def _recording_item_columns(
    filters: ListFilters, timezone_name: str, *, using: str = "default", item_keys=None
):
    """The recording branch of the Library item UNION.

    Every current Recording EXCEPT those replaced by active valid topic
    Sections. The exclusion is a lazy parameterized RawSQL subquery over
    the shared canonical-layout predicate (never a Python id set, never a
    growing ``IN (... )`` parameter list). Applies the EXISTING
    recording-scope filter predicates (:func:`filter_only` — recording
    tags/summary semantics unchanged) BEFORE projecting the shared union
    columns. ``item_keys`` (optional, bounded caller-provided list)
    restricts the branch to the projected ``item_key`` identity BEFORE
    the union — Django forbids ``filter()`` after ``union()``, so the
    bounded IN predicate must live inside each branch.
    """
    qs = Recording.objects.using(using).annotate(effective_at=effective_at_annotation())
    qs = filter_only(qs, filters, timezone_name)
    qs = qs.exclude(pk__in=_HIDDEN_RECORDINGS_SQL_RAW)
    annotated = _recording_item_annotation(qs)
    if item_keys is not None:
        annotated = annotated.filter(item_key__in=item_keys)
    return annotated.order_by().values(*_ITEM_COLUMNS)


def _recording_item_annotation(qs):
    """The shared recording-branch presentation annotations."""
    display_title = _display_title_expression()
    return qs.annotate(
        default_output_language=_default_output_language_subquery(),
        display_title=display_title,
        title_fold=_fold_expression(display_title),
        has_route_unverified=Exists(
            RoutingDecision.objects.filter(
                recording=OuterRef("pk"), is_active=True, routing_verified=False
            )
        ),
        item_kind=Value("recording", output_field=CharField(max_length=16)),
        item_key=Concat(Value("r:"), F("pk"), output_field=CharField()),
        recording_id=F("pk"),
        section_id=Value(None, output_field=CharField(max_length=36)),
        range_start=Value(None, output_field=IntegerField()),
        range_end=Value(None, output_field=IntegerField()),
        # Recording items retain the recording's OWN duration: it is a
        # real ``Recording`` field, so ``.values(*_ITEM_COLUMNS)``
        # projects it directly (never an annotation of the same name,
        # which Django forbids).
    )


def _section_default_language_expression() -> Subquery:
    """Default output language of a topic Section's ACTIVE transcript.

    Uses the same ``langresolve`` ORM expression as the recording branch
    (keyed on the Section's transcript), so the derived value always
    agrees with ``resolve_default_language``.
    """
    return Subquery(
        Transcript.objects.filter(pk=OuterRef("transcript_id"))
        .annotate(_default_output=default_output_language_expression())
        .values("_default_output")[:1],
        output_field=CharField(max_length=32),
    )


def _section_summary_state_expression() -> Subquery:
    """A Section's current DEFAULT-output-variant summary state.

    The ``SummaryVariantState.status`` value (missing/current/failed) or
    NULL when no variant-state row exists (never generated). Mirrors the
    recording-level ``summary_status`` semantics scoped to one Section.
    """
    return Subquery(
        SummaryVariantState.objects.filter(
            transcript=OuterRef("transcript_id"),
            section=OuterRef("pk"),
            output_language=OuterRef("default_output_language"),
        ).values("status")[:1],
        output_field=CharField(max_length=16),
    )


def _section_has_default_summary_expression() -> Exists:
    """Exists: an ACTIVE Summary for the Section in its DEFAULT output
    variant (the section-scoped analog of the recording ``has_summary``
    predicate)."""
    return Exists(
        Summary.objects.filter(
            transcript=OuterRef("transcript_id"),
            section=OuterRef("pk"),
            is_active=True,
            output_language=OuterRef("default_output_language"),
        )
    )


def _section_base_queryset(*, using: str = "default", include_archived: bool = False):
    """The Section branch base: topic Sections whose transcript is ACTIVE
    and whose SegmentedVersion is that transcript's ACTIVE revision (with
    the cross-parent ownership guard), limited to canonically valid
    Section pks via the shared parameterized RawSQL subquery (lazy —
    never a Python id set, never a growing ``IN (... )`` parameter
    list), annotated with the filter/order columns.

    ``include_archived=False`` (the normal Library/search/Ask scope)
    excludes Sections carrying their own ``archived_at`` marker.
    ``include_archived=True`` (the read-only combined archive listing
    only) keeps them; the SHARED canonical-layout predicate itself stays
    unchanged (archive is eligibility, not topology), so an archived
    Section still counts as structurally present and its parent stays
    suppressed.
    """
    qs = Section.objects.using(using).filter(
        pk__in=_VALID_SECTIONS_SQL_RAW,
        segmented_version__isnull=False,
        transcript__is_active=True,
        # Archived parents are ineligible everywhere user-facing:
        # their canonical topic Sections disappear with them.
        transcript__recording__archived_at__isnull=True,
        segmented_version__is_active=True,
        transcript=F("segmented_version__transcript"),
    )
    if not include_archived:
        qs = qs.filter(archived_at__isnull=True)
    return (
        qs.annotate(
            effective_at=Coalesce(
                "transcript__recording__recorded_at",
                "transcript__recording__discovered_at",
                output_field=DateTimeField(),
            ),
            default_output_language=_section_default_language_expression(),
            section_default_summary_state=_section_summary_state_expression(),
            section_has_default_summary=_section_has_default_summary_expression(),
        )
    )


def _section_filter_only(qs, filters: ListFilters, timezone_name: str):
    """Apply the parsed Library filters to the SECTION branch.

    Semantics per the approved product decisions: tag filters inspect the
    Section's OWN assignment rows only (any/all exact per item);
    ``has_summary`` inspects the Section's current DEFAULT output
    variant; status/review/language/date inherit the parent
    Recording/transcript as appropriate.
    """
    if filters.date:
        start, end = local_day_bounds(filters.date, timezone_name)
        qs = qs.filter(effective_at__gte=start, effective_at__lt=end)
    if filters.date_from:
        start, _ = local_day_bounds(filters.date_from, timezone_name)
        qs = qs.filter(effective_at__gte=start)
    if filters.date_to:
        _, end = local_day_bounds(filters.date_to, timezone_name)
        qs = qs.filter(effective_at__lt=end)

    if filters.tags:
        if filters.tag_match == "any":
            qs = qs.filter(
                tag_assignments__is_active=True,
                tag_assignments__tag__name_key__in=filters.tags,
            ).distinct()
        else:
            for key in filters.tags:
                qs = qs.filter(
                    tag_assignments__is_active=True,
                    tag_assignments__tag__name_key=key,
                )

    if filters.status:
        qs = qs.filter(transcript__recording__processing_status=filters.status)

    if filters.summary:
        if filters.summary == SummaryState.NOT_READY:
            # A topic Section only exists for an ACTIVE transcript, so
            # "not ready" can never match a Section item.
            qs = qs.none()
        elif filters.summary == SummaryState.MISSING:
            qs = qs.filter(
                Q(section_default_summary_state=SummaryVariantState.VariantStatus.MISSING)
                | Q(section_default_summary_state__isnull=True)
            )
        else:
            qs = qs.filter(section_default_summary_state=filters.summary)

    if filters.review:
        qs = qs.filter(
            Q(transcript__recording__processing_status=ProcessingStatus.NEEDS_REVIEW)
            | Q(transcript__recording__processing_status=ProcessingStatus.FAILED)
            | Q(transcript__recording__retranscription_failed=True)
            | Q(transcript__recording__resummarization_failed=True)
            | Q(section_default_summary_state=SummaryVariantState.VariantStatus.FAILED)
        ).distinct()

    if filters.audio:
        qs = qs.filter(transcript__recording__audio_status=filters.audio)

    if filters.has_summary is not None:
        if filters.has_summary:
            qs = qs.filter(section_has_default_summary=True).distinct()
        else:
            qs = qs.exclude(section_has_default_summary=True).distinct()

    return qs


def _section_display_title_expression():
    """The single derived Library title source for a topic Section (Step
    6.2a) — used for BOTH rendering and Title A–Z/Z–A ordering so the two
    can never diverge.

    The active DEFAULT-language section Summary's title is the Section's
    user-facing title whenever one exists: it supersedes BOTH a stored
    temporary title AND a manually entered custom title in presentation
    (a display override only). Without a default Summary the stored
    ``Section.title`` is used. ``Section.title`` is NEVER mutated during
    summary generation: the derived expression reads the Summary row
    without writing anything, so the custom title stays layout
    metadata/provenance.
    """
    default_summary_title = Subquery(
        Summary.objects.filter(
            transcript=OuterRef("transcript_id"),
            section=OuterRef("pk"),
            is_active=True,
            output_language=OuterRef("default_output_language"),
        )
        .order_by("ordinal")
        .values("title")[:1],
        output_field=CharField(max_length=200),
    )
    return Coalesce(
        default_summary_title,
        F("title"),
        output_field=CharField(max_length=255),
    )


def _section_duration_expression():
    """Approximate duration of a topic Section in seconds (Step 6.2a).

    ``(latest usable end_ms - earliest usable start_ms) / 1000`` where
    the two endpoints are selected INDEPENDENTLY across the canonical
    range: the earliest NON-NULL ``start_ms`` and the latest NON-NULL
    ``end_ms`` (a first segment with only a start and a last segment
    with only an end still yields a span). Returns NULL when no usable
    start OR no usable end exists in the range (rendered as "unknown");
    a nonpositive result is rejected by the card adapter. Bounded: one
    scalar subquery per projected row (the page row count is capped by
    ``per_page``), no N+1, no unbounded reads. The
    ``values("transcript_id")`` BEFORE the aggregate pins the GROUP BY to
    ONE group (every filtered row shares the outer transcript), so the
    MIN/MAX span the WHOLE canonical range — never per-segment.
    """
    return Subquery(
        TranscriptSegment.objects.filter(
            transcript=OuterRef("transcript_id"),
            ordinal__gte=OuterRef("start_segment_ordinal"),
            ordinal__lt=OuterRef("end_segment_ordinal_exclusive"),
        )
        .values("transcript_id")
        .annotate(
            _duration=(
                Max(Case(When(end_ms__isnull=False, then=F("end_ms")), default=Value(None)))
                - Min(Case(When(start_ms__isnull=False, then=F("start_ms")), default=Value(None)))
            )
            / 1000.0
        )
        .values("_duration")[:1],
        output_field=FloatField(),
    )


def section_duration_seconds(section, *, using: str = "default") -> float | None:
    """Approximate duration of ONE topic Section (pure bounded aggregate).

    Same semantics as :func:`_section_duration_expression`: the earliest
    NON-NULL ``start_ms`` to the latest NON-NULL ``end_ms`` across the
    Section's canonical range, selected independently, divided by 1000.
    Returns ``None`` ("unknown") when no usable start OR end exists or
    when the span is nonpositive. One SELECT; never per-row loops.
    """
    row = (
        TranscriptSegment.objects.using(using)
        .filter(
            transcript=section.transcript_id,
            ordinal__gte=section.start_segment_ordinal,
            ordinal__lt=section.end_segment_ordinal_exclusive,
        )
        .aggregate(
            start=Min(
                Case(When(start_ms__isnull=False, then=F("start_ms")), default=Value(None))
            ),
            end=Max(
                Case(When(end_ms__isnull=False, then=F("end_ms")), default=Value(None))
            ),
        )
    )
    if row["start"] is None or row["end"] is None:
        return None
    duration = (row["end"] - row["start"]) / 1000.0
    if duration <= 0:
        return None
    return duration


def _section_item_columns(
    filters: ListFilters, timezone_name: str, *, using: str = "default", item_keys=None
):
    """The Section branch of the Library item UNION.

    ``item_keys`` (optional, bounded caller-provided list) restricts the
    branch to the projected ``item_key`` identity BEFORE the union (the
    same per-branch bounded IN predicate as the recording branch).
    """
    qs = _section_base_queryset(using=using)
    qs = _section_filter_only(qs, filters, timezone_name)
    annotated = _section_item_annotation(qs)
    if item_keys is not None:
        annotated = annotated.filter(item_key__in=item_keys)
    return annotated.order_by().values(*_ITEM_COLUMNS)


def _section_item_annotation(qs):
    """The shared section-branch presentation annotations."""
    section_title = Coalesce(_section_display_title_expression(), Value(TITLE_PLACEHOLDER))
    return qs.annotate(**dict(
        item_kind=Value("section", output_field=CharField(max_length=16)),
        item_key=Concat(Value("s:"), F("pk"), output_field=CharField()),
        recording_id=F("transcript__recording_id"),
        section_id=F("pk"),
        display_title=section_title,
        title_fold=_fold_expression(section_title),
        processing_status=F("transcript__recording__processing_status"),
        summary_status=Coalesce(
            "section_default_summary_state", Value(SummaryState.MISSING)
        ),
        audio_status=F("transcript__recording__audio_status"),
        retranscription_failed=F("transcript__recording__retranscription_failed"),
        resummarization_failed=F("transcript__recording__resummarization_failed"),
        has_route_unverified=_route_unverified_expression(),
        recorded_at=F("transcript__recording__recorded_at"),
        range_start=F("start_segment_ordinal"),
        range_end=F("end_segment_ordinal_exclusive"),
        duration_seconds=_section_duration_expression(),
    ))


def library_item_queryset(
    filters: ListFilters,
    timezone_name: str,
    *,
    using: str = "default",
    item_keys=None,
) -> QuerySet:
    """The read-only Library item projection (Step 6.2).

    A Django UNION of same-shaped recording-backed and active-topic
    section-backed branches. Filters are applied PER BRANCH (before the
    union) so count/pagination/ordering all happen database-side over the
    union — never a Python expansion of all recordings. The "which
    recordings are replaced" / "which Sections are valid" state is a lazy
    parameterized RawSQL subquery shared by both branches — the number of
    active layouts/sections never materializes in Python and never grows
    SQL parameters. Callers order with :func:`apply_item_sort` and
    paginate with a ``Paginator``, then hydrate each page with
    :func:`hydrate_library_items`.

    ``item_keys`` (Step 6.3, optional bounded list) additionally
    restricts BOTH branches to the projected ``item_key`` identity with
    one bounded IN predicate PER branch (applied inside the branches —
    Django forbids ``filter()`` after ``union()``); the canonical
    replacement and filter semantics are untouched, so restricting can
    only REMOVE normal Library items, never add or alter one.
    """
    recording_qs = _recording_item_columns(
        filters, timezone_name, using=using, item_keys=item_keys
    )
    section_qs = _section_item_columns(
        filters, timezone_name, using=using, item_keys=item_keys
    )
    return recording_qs.union(section_qs)


def archived_item_queryset(*, using: str = "default") -> QuerySet:
    """The read-only combined Archived-items projection (Recording +
    Section archive).

    A database UNION of the same-shaped branches projecting exactly the
    shared :data:`_ITEM_COLUMNS` plus ``archived_at``:

    - archived Recordings (every archived Recording, regardless of any
      canonical split layout);
    - independently archived canonical ACTIVE topic Sections whose parent
      Recording is NOT archived (so a parent + child can never both
      appear as separate rows).

    The branches do NOT apply the Library ``ListFilters`` or the
    canonical parent-suppression exclusion — this is a bounded archive
    listing, not the Library — and the SHARED canonical-layout predicate
    is unchanged (an archived Section is structurally present, so its
    parent stays suppressed in the normal Library). Deliberately
    unsliced and unordered: the caller orders by ``-archived_at,
    item_key`` and applies its own hard limit+1 sentinel before
    hydration, exactly like the normal front-page listing.
    """
    recording_qs = (
        Recording.objects.using(using)
        .filter(archived_at__isnull=False)
        .annotate(effective_at=effective_at_annotation())
    )
    recording_qs = _recording_item_annotation(recording_qs)
    recording_qs = recording_qs.order_by().values(*_ITEM_COLUMNS, "archived_at")

    section_qs = _section_base_queryset(using=using, include_archived=True).filter(
        archived_at__isnull=False
    )
    section_qs = _section_item_annotation(section_qs)
    section_qs = section_qs.order_by().values(*_ITEM_COLUMNS, "archived_at")

    return recording_qs.union(section_qs)


def library_item_key_queryset(
    filters: ListFilters,
    timezone_name: str,
    *,
    using: str = "default",
    item_keys=None,
) -> QuerySet:
    """The unsliced, unordered one-column ``item_key`` UNION of the normal
    Library items (Step 6.2).

    This is the shared database-side identity set behind the projection:
    the EXACT per-branch ``ListFilters`` and the canonical parent-
    replacement semantics (a valid active split layout hides its recording
    and yields its topic Sections; crop-only/malformed/historical layouts
    never hide it) are applied with the SAME branch helpers as
    :func:`library_item_queryset` — ``filter_only`` + the ``_HIDDEN_RECORDINGS_SQL_RAW``
    exclusion on the recording branch and ``_section_base_queryset`` +
    ``_section_filter_only`` on the Section branch — so the key set can
    never diverge from the projected rows.

    Only the unique ``item_key`` column is projected (no presentation
    subqueries), the branches are unsliced and unordered, and the result is
    a database UNION: never a Python expansion of all recordings, never a
    Python id set. The canonical-layout state stays a bounded two-parameter
    RawSQL subquery shared by both branches (the layout/section counts
    never grow the SQL parameters). Honors ``using`` for the target
    database. Callers count it directly (``.count()``) or collect the keys;
    :func:`library_item_count` is the count entry point.

    ``item_keys`` (Step 6.3, optional bounded list) additionally restricts
    BOTH branches to the given ``item_key`` identity with one bounded IN
    predicate per branch (inside the branches: Django forbids
    ``filter()`` after ``union()``), so the restricted key set is always a
    subset of the unrestricted normal Library identity set.
    """
    recording_qs = Recording.objects.using(using).annotate(
        effective_at=effective_at_annotation()
    )
    recording_qs = filter_only(recording_qs, filters, timezone_name)
    recording_qs = recording_qs.exclude(pk__in=_HIDDEN_RECORDINGS_SQL_RAW)
    recording_qs = recording_qs.annotate(
        item_key=Concat(Value("r:"), F("pk"), output_field=CharField())
    )
    if item_keys is not None:
        recording_qs = recording_qs.filter(item_key__in=item_keys)
    recording_qs = recording_qs.order_by().values("item_key")

    section_qs = _section_base_queryset(using=using)
    section_qs = _section_filter_only(section_qs, filters, timezone_name)
    section_qs = section_qs.annotate(
        item_key=Concat(Value("s:"), F("pk"), output_field=CharField())
    )
    if item_keys is not None:
        section_qs = section_qs.filter(item_key__in=item_keys)
    section_qs = section_qs.order_by().values("item_key")

    return recording_qs.union(section_qs)


def library_item_count(
    filters: ListFilters,
    timezone_name: str,
    *,
    using: str = "default",
) -> int:
    """Database-side item count for the Library paginator.

    Counts the shared ``item_key`` identity UNION
    (:func:`library_item_key_queryset`), which projects ONLY the unique key
    per branch so the count is computed over a minimal UNION (same filters,
    same branches, same semantics — never a Python expansion, never a
    Python id set).
    """
    return library_item_key_queryset(filters, timezone_name, using=using).count()


def apply_item_sort(queryset, sort: str):
    """Order the Library item UNION by one of the Library sort choices.

    Date grouping/newest/oldest use the parent Recording effective date;
    Title sorts use the item display title (topic title for a Section,
    the existing fallback chain for a Recording) folded by the shared
    ``unicode_fold`` collation INSIDE each union branch and ordered by
    the projected ``title_fold`` column, with a stable deterministic
    tie-breaker (the unique ``item_key``).
    """
    if sort == "oldest":
        return queryset.order_by("effective_at", "item_key")
    if sort == "title_az":
        ensure_registered()
        return queryset.order_by("title_fold", "item_key")
    if sort == "title_za":
        ensure_registered()
        return queryset.order_by("-title_fold", "item_key")
    return queryset.order_by("-effective_at", "item_key")


def hydrate_library_items(rows, *, using: str = "default") -> list["LibraryItemCard"]:
    """Hydrate one page of projected item rows into card adapters.

    Bounded batched queries (never per-item): the parent Recordings come
    from ONE ``recording_list_queryset()`` batch (the existing prefetch
    contract), the Sections from ONE batch with their section-scoped
    active tags, and the section-scoped active Summaries/variant states
    from TWO bounded batches.
    """
    rows = list(rows)
    if not rows:
        return []
    recording_ids = {row["recording_id"] for row in rows}
    recordings = {
        r.pk: r
        for r in recording_list_queryset(include_title=False).using(using).filter(pk__in=recording_ids)
    }
    section_ids = [row["section_id"] for row in rows if row.get("section_id")]
    sections: dict[int, Section] = {}
    section_tags: dict[int, list] = {}
    section_summaries: dict[int, Summary] = {}
    section_languages: dict[int, list[str]] = {}
    section_variant_states: dict[int, SummaryVariantState] = {}
    if section_ids:
        sections = {
            s.pk: s
            for s in Section.objects.using(using)
            .filter(pk__in=section_ids)
            .select_related("transcript", "segmented_version")
        }
        for assignment in (
            TagAssignment.objects.using(using)
            .filter(section__in=section_ids, is_active=True)
            .select_related("tag")
            .order_by("tag__name")
        ):
            section_tags.setdefault(assignment.section_id, []).append(assignment)

        summaries_by_section: dict[int, dict[str, Summary]] = {}
        for summary in (
            Summary.objects.using(using)
            .filter(section__in=section_ids, is_active=True)
            .only(
                "id",
                "section",
                "transcript",
                "ordinal",
                "title",
                "overview",
                "language",
                "output_language",
                "is_active",
                "created_at",
                "model_id",
                "generation_mode",
            )
        ):
            summaries_by_section.setdefault(summary.section_id, {})[
                summary.output_language
            ] = summary

        states_by_section: dict[int, dict[str, SummaryVariantState]] = {}
        for state in SummaryVariantState.objects.using(using).filter(section__in=section_ids):
            states_by_section.setdefault(state.section_id, {})[state.output_language] = state

        for row in rows:
            sid = row.get("section_id")
            if not sid:
                continue
            by_lang = summaries_by_section.get(sid, {})
            default_lang = row.get("default_output_language") or ""
            section_summaries[sid] = by_lang.get(default_lang)
            section_languages[sid] = sorted(set(by_lang) | set(states_by_section.get(sid, {})))
            section_variant_states[sid] = states_by_section.get(sid, {}).get(default_lang)

    cards = []
    for row in rows:
        recording = recordings.get(row["recording_id"])
        sid = row.get("section_id")
        cards.append(
            LibraryItemCard(
                row,
                RecordingCard(recording) if recording is not None else None,
                section=sections.get(sid),
                section_tags=section_tags.get(sid, ()),
                section_summary=section_summaries.get(sid),
                section_languages=section_languages.get(sid, ()),
                section_variant_state=section_variant_states.get(sid),
            )
        )
    return cards


class LibraryItemCard:
    """Presentation adapter over ONE projected Library item.

    Wraps the item row (from the DB UNION), the hydrated parent
    Recording via the existing :class:`RecordingCard` contract, and — for
    a topic-Section item — the Section plus its section-scoped active
    tags, default-variant Summary, variant state and language set. Every
    attribute is served from memory; hydration happens once per page in
    bounded batched queries (never per-item).
    """

    def __init__(
        self,
        row,
        card: RecordingCard | None,
        *,
        section: Section | None = None,
        section_tags=(),
        section_summary: Summary | None = None,
        section_languages=(),
        section_variant_state: SummaryVariantState | None = None,
    ) -> None:
        self._row = row
        self._card = card
        self.section = section
        self._section_tags = list(section_tags)
        self._section_summary = section_summary
        self._section_languages = list(section_languages)
        self._section_variant_state = section_variant_state

    @property
    def is_section(self) -> bool:
        return self._row["item_kind"] == "section"

    @property
    def recording(self) -> Recording | None:
        return self._card.recording if self._card is not None else None

    @property
    def recording_id(self):
        return self._row.get("recording_id")

    @property
    def section_id(self):
        return self._row.get("section_id")

    @property
    def title(self) -> str:
        """The item display title: the exact projected value used for
        BOTH rendering and Title A–Z/Z–A ordering (topic title for a
        Section; the existing fallback chain for a Recording)."""
        value = self._row.get("display_title")
        if value:
            return value
        if self._card is not None:
            return self._card.title
        return TITLE_PLACEHOLDER

    @property
    def parent_title(self) -> str:
        """The parent Recording title (context/provenance for a Section
        item; identical to ``title`` for a Recording item)."""
        if self._card is not None:
            return self._card.title
        return TITLE_PLACEHOLDER

    @property
    def effective_at(self):
        value = self._row.get("effective_at")
        if value is not None:
            return value
        recording = self.recording
        if recording is None:
            return None
        return recording.recorded_at or recording.discovered_at

    @property
    def effective_at_label(self) -> str:
        return "Recorded" if self._row.get("recorded_at") else "Discovered"

    @property
    def month_label(self) -> str:
        return month_label_for(self.effective_at)

    @property
    def display_summary(self) -> Summary | None:
        """The single Summary row driving all card Summary content: the
        Section's DEFAULT-variant active Summary for a Section item, the
        default-language whole-recording Summary for a Recording item."""
        if self.is_section:
            return self._section_summary
        return self._card.display_summary if self._card is not None else None

    @property
    def overview_excerpt(self) -> str:
        summary = self.display_summary
        if summary is None:
            return ""
        text = unicodedata.normalize("NFC", summary.overview).strip()
        if len(text) > 220:
            text = text[:220].rstrip() + "…"
        return text

    @property
    def active_tags(self) -> list[TagAssignment]:
        if self.is_section:
            return list(self._section_tags)
        return self._card.active_tags if self._card is not None else []

    @property
    def available_languages(self) -> list[str]:
        if self.is_section:
            return list(self._section_languages)
        return self._card.available_languages if self._card is not None else []

    @property
    def display_source(self) -> AudioSource | None:
        return self._card.display_source if self._card is not None else None

    @property
    def active_route(self) -> RoutingDecision | None:
        return self._card.active_route if self._card is not None else None

    @property
    def needs_attention(self) -> bool:
        row = self._row
        return bool(
            row["processing_status"] in (ProcessingStatus.NEEDS_REVIEW, ProcessingStatus.FAILED)
            or row["retranscription_failed"]
            or row["resummarization_failed"]
            or row["summary_status"] == SummaryState.FAILED
            or row["audio_status"] == AudioStatus.MISSING
        )

    @property
    def range_label(self) -> str:
        if not self.is_section:
            return ""
        from workflow.services.segmentation import range_label

        return range_label(self._row.get("range_start"), self._row.get("range_end"))

    @property
    def summary_status(self):
        return self._row.get("summary_status")

    @property
    def archived_at(self):
        """The item's own archive marker (Recording or Section), or
        ``None`` for an item read from the normal Library projection."""
        if self.is_section:
            return self.section.archived_at if self.section is not None else None
        return self.recording.archived_at if self.recording is not None else None

    @property
    def duration_seconds(self) -> float | None:
        """Approximate item duration in seconds, or ``None`` ("unknown").

        A Section item uses its canonical-range span (earliest usable
        start_ms to latest usable end_ms, /1000); a Recording item keeps
        the recording's own duration. Unavailable or nonpositive values
        are safe ``None``. Served from the projected row — no per-item
        queries."""
        value = self._row.get("duration_seconds")
        if value is None:
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return value

    @property
    def default_language(self) -> str:
        return self._row.get("default_output_language") or ""

    @property
    def section_variant_state(self) -> SummaryVariantState | None:
        return self._section_variant_state


# ---------------------------------------------------------------------------
# Bounded search-result item hydration (Step 6.3)
#
# The search engines return winners identified by the Library ``item_key``
# (``r:<recording pk>`` / ``s:<section pk>``). ``library_items_by_keys`` is
# the ONE read-only entry point that turns such keys back into
# :class:`LibraryItemCard` adapters. Engine keys are trusted to come FROM
# the engine but are never trusted to still be CURRENT: every key is
# revalidated through the EXACT normal-Library semantics — the shared
# ``library_item_key_queryset`` identity UNION (per-branch ``ListFilters``
# plus the canonical parent-replacement rule) — so a stale engine result
# can never resurrect a deleted item, an item a valid active split layout
# has since replaced, a superseded-layout Section or an item the current
# filters exclude. Nothing is fabricated and no unbounded per-key work
# happens: malformed keys are skipped and two bounded reads — each a
# per-branch IN predicate, never a post-union filter (Django forbids
# filter() after union()) — plus the existing batched hydration answer
# the whole request.
# ---------------------------------------------------------------------------

# Hard input cap: the engine's search result cap (imported, never copied).
# One call carries at most the full winner set.
LIBRARY_ITEM_KEY_LIMIT = MAX_RESULT_LIMIT


def _canonical_item_key(item_key) -> str | None:
    """Canonicalize ONE requested item key, or ``None`` when malformed.

    Only the two shapes the Library UNION derives are honoured:
    ``r:<Recording pk>`` and ``s:<Section pk>``. Recording pks are
    canonicalized through ``uuid.UUID`` (the same normalization the
    search web layer applies), Section pks must be positive ASCII
    decimals; the canonical form is exactly what the UNION projects, so
    equivalent spellings resolve to the same identity and anything the
    UNION could never emit is rejected here — never passed to the
    database.
    """
    if not isinstance(item_key, str):
        return None
    prefix, separator, remainder = item_key.partition(":")
    if not separator:
        return None
    if prefix == "r":
        try:
            return f"r:{UUID(remainder)}"
        except ValueError:
            return None
    if prefix == "s":
        if not remainder.isascii() or not remainder.isdigit():
            return None
        value = int(remainder)
        if value <= 0:
            return None
        return f"s:{value}"
    return None


def library_items_by_keys(
    item_keys,
    filters: ListFilters,
    timezone_name: str,
    *,
    using: str = "default",
) -> list[LibraryItemCard]:
    """Hydrate bounded search-result item keys into Library item cards.

    Strictly read-only (SELECTs only) and fully bounded:

    - input: an ordered sequence of at most :data:`LIBRARY_ITEM_KEY_LIMIT`
      engine ``item_key`` strings; a larger sequence (or a bare
      ``str``/``bytes``) is a caller contract violation raising a fixed
      ``ValueError`` BEFORE any query;
    - revalidation: the canonicalized, de-duplicated keys (first
      occurrence wins, malformed keys skipped, ZERO queries for an empty
      or all-malformed request) are checked against the EXACT normal
      Library identity set via :func:`library_item_key_queryset` with the
      same per-branch ``ListFilters`` and canonical parent-replacement
      semantics — the key list lands as one bounded IN predicate inside
      EACH branch (at most the cap parameters per branch). Keys that no
      longer name a normal Library item (vanished, replaced by a split
      layout, superseded/historical layout, filtered out) are silently
      skipped, never fabricated;
    - fetch: the surviving keys drive ONE full-projection
      :func:`library_item_queryset` read (same bounded per-branch IN
      predicate); rows are re-ordered to the REQUESTED key order and
      hydrated by the existing batched :func:`hydrate_library_items`
      contract (constant query count, no N+1). A row vanishing between
      the two reads is skipped with its key.

    The result is one card per requested key that is still a normal
    Library item, in requested key order.
    """
    if item_keys is None or isinstance(item_keys, (str, bytes)):
        raise ValueError("item_keys must be a sequence of Library item key strings")
    requested = list(item_keys)
    if len(requested) > LIBRARY_ITEM_KEY_LIMIT:
        raise ValueError(
            f"item_keys must contain at most {LIBRARY_ITEM_KEY_LIMIT} keys"
        )

    order: list[str] = []
    seen: set[str] = set()
    for key in requested:
        canonical = _canonical_item_key(key)
        if canonical is None or canonical in seen:
            continue
        seen.add(canonical)
        order.append(canonical)
    if not order:
        return []

    survivors = set(
        library_item_key_queryset(
            filters, timezone_name, using=using, item_keys=order
        ).values_list("item_key", flat=True)
    )
    if not survivors:
        return []

    rows_by_key = {
        row["item_key"]: row
        for row in library_item_queryset(
            filters, timezone_name, using=using, item_keys=sorted(survivors)
        )
    }
    rows = [rows_by_key[key] for key in order if key in rows_by_key]
    return hydrate_library_items(rows, using=using)


def month_label_for(dt) -> str:
    """Local "Month YYYY" group label for chronological sorts."""
    if dt is None:
        return ""
    from brain import settings as django_settings

    tz_name = getattr(django_settings.BRAIN_CONFIG_OBJ, "timezone", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    local = dt.astimezone(tz) if dj_tz.is_aware(dt) else dt
    return local.strftime("%B %Y")


# ---------------------------------------------------------------------------
# Filter parsing / application
# ---------------------------------------------------------------------------


@dataclass
class ListFilters:
    date: date | None = None
    date_from: date | None = None
    date_to: date | None = None
    tags: list[str] = dc_field(default_factory=list)
    tag_match: str = "all"  # "all" (AND) | "any" (OR)
    status: str | None = None
    summary: str | None = None
    review: bool = False
    audio: str | None = None
    has_summary: bool | None = None
    sort: str = "newest"  # newest | oldest | title_az | title_za (+ relevance in search mode)
    # Stored sort mode (Step 5A.4.2a, explicit — never implicit): the
    # default the ``sort`` parameter is omitted against in
    # ``as_querystring()``. The Library keeps "newest"; search mode
    # (``allow_relevance=True``) stores "relevance".
    sort_default: str = "newest"
    # Search-mode-only structured channel (5A.4.2a): an invalid SORT is
    # never mixed into ``errors`` (which mean "the scope filters cannot
    # be applied"): it falls back to the mode default and reports here,
    # so a bad sort retains every valid filter.
    sort_error: str | None = None

    errors: list[str] = dc_field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def scope_valid(self) -> bool:
        """Whether the SCOPE FILTERS are safe to apply (search layer).

        Identical predicate to ``valid`` today, but semantically
        distinct: ``sort_error`` must never affect scope eligibility —
        an invalid sort keeps the valid filters and only changes the
        ordering.
        """
        return not self.errors

    def as_pairs(self) -> list[tuple[str, str]]:
        """Canonical ordered ``(name, value)`` pairs (without ``page``).

        The single serialization source: ``as_querystring()`` URL-encodes
        these pairs and the POST-only vector forms render them as hidden
        inputs, so GET links and POST bodies can never diverge. The
        ``sort`` pair is omitted iff it equals the mode default.
        """
        pairs: list[tuple[str, str]] = []
        if self.date:
            pairs.append(("date", self.date.isoformat()))
        if self.date_from:
            pairs.append(("from", self.date_from.isoformat()))
        if self.date_to:
            pairs.append(("to", self.date_to.isoformat()))
        for tag in self.tags:
            pairs.append(("tag", tag))
        if self.tag_match != "all":
            pairs.append(("tag_match", self.tag_match))
        if self.status:
            pairs.append(("status", self.status))
        if self.summary:
            pairs.append(("summary", self.summary))
        if self.review:
            pairs.append(("review", "1"))
        if self.audio:
            pairs.append(("audio", self.audio))
        if self.has_summary is not None:
            pairs.append(("has_summary", "1" if self.has_summary else "0"))
        if self.sort != self.sort_default:
            pairs.append(("sort", self.sort))
        return pairs

    def as_querystring(self) -> str:
        """Canonical query string (without the page parameter) so filters
        persist across pagination links."""
        import urllib.parse

        params: dict[str, list[str]] = {}
        for name, value in self.as_pairs():
            params.setdefault(name, []).append(value)
        return urllib.parse.urlencode(params, doseq=True)


def _parse_date(value: str) -> date | None:
    return date.fromisoformat(value)


def list_filters(
    GET, timezone_name: str = "Europe/Helsinki", *, allow_relevance: bool = False
) -> ListFilters:
    """Parse and validate the recording-list query string.

    Invalid values append friendly messages to ``errors`` (the affected
    filter is ignored) — an invalid filter is never a server error.

    ``allow_relevance=True`` (Step 5A.4.2a search mode) extends the sort
    contract: ``relevance`` is accepted and becomes BOTH the default and
    the deterministic fallback for an invalid sort, reported through the
    structured ``sort_error`` channel instead of ``errors`` — so an
    invalid sort never invalidates the valid scope filters. With the
    default (Library listing) the behavior is exactly the historical
    one: ``relevance`` is rejected into ``errors`` and the fallback is
    ``newest``.
    """
    filters = ListFilters(
        sort="relevance" if allow_relevance else "newest",
        sort_default="relevance" if allow_relevance else "newest",
    )

    def _date_param(name: str, target_attr: str, label: str) -> None:
        raw = (GET.get(name) or "").strip()
        if not raw:
            return
        try:
            setattr(filters, target_attr, _parse_date(raw))
        except ValueError:
            filters.errors.append(f"'{label}' must be a date in YYYY-MM-DD format.")

    _date_param("date", "date", "date")
    _date_param("from", "date_from", "from date")
    _date_param("to", "date_to", "to date")

    tags = [tag for tag in GET.getlist("tag") if tag.strip()]
    if len(tags) > MAX_TAG_FILTERS:
        filters.errors.append(f"Too many tag filters (maximum {MAX_TAG_FILTERS}).")
        tags = tags[:MAX_TAG_FILTERS]
    filters.tags = [tag_name_key(tag) for tag in tags]

    match = (GET.get("tag_match") or "all").strip().lower()
    if match not in ("all", "any"):
        filters.errors.append("'tag_match' must be 'all' or 'any'.")
        match = "all"
    filters.tag_match = match

    status = (GET.get("status") or "").strip()
    if status:
        if status not in VALID_PROCESSING_STATUSES:
            filters.errors.append(f"'{status}' is not a valid processing status.")
        else:
            filters.status = status

    summary = (GET.get("summary") or "").strip()
    if summary:
        if summary not in VALID_SUMMARY_STATUSES:
            filters.errors.append(f"'{summary}' is not a valid summary status.")
        else:
            filters.summary = summary

    filters.review = (GET.get("review") or "").strip() in ("1", "true")

    audio = (GET.get("audio") or "").strip()
    if audio:
        if audio not in (AudioStatus.PRESENT, AudioStatus.MISSING):
            filters.errors.append("'audio' must be 'present' or 'missing'.")
        else:
            filters.audio = audio

    valid_sorts = SORT_CHOICES + (("relevance",) if allow_relevance else ())
    sort = (GET.get("sort") or filters.sort_default).strip().lower()
    if sort not in valid_sorts:
        if allow_relevance:
            # Structured search-mode channel: the scope filters stay
            # valid; only the sort falls back (deterministically).
            filters.sort_error = (
                "'sort' must be one of 'relevance', 'newest', 'oldest', "
                "'title_az', 'title_za'."
            )
            sort = "relevance"
        else:
            filters.errors.append(
                "'sort' must be one of 'newest', 'oldest', 'title_az', 'title_za'."
            )
            sort = "newest"
    filters.sort = sort

    has_summary_raw = (GET.get("has_summary") or "").strip()
    if has_summary_raw in ("1", "true"):
        filters.has_summary = True
    elif has_summary_raw in ("0", "false"):
        filters.has_summary = False
    elif has_summary_raw:
        filters.errors.append("'has_summary' must be '1' or '0'.")

    if filters.date and (filters.date_from or filters.date_to):
        filters.errors.append("Use either a single 'date' or a 'from'/'to' range, not both.")

    # Cross-check the tz so a bad configuration surfaces as a filter error,
    # never a 500.
    try:
        ZoneInfo(timezone_name)
    except Exception:
        filters.errors.append("The configured timezone is invalid.")

    return filters


def local_day_bounds(day: date, timezone_name: str) -> tuple[datetime, datetime]:
    """Aware [start, end) boundaries of the LOCAL calendar day.

    Computed via ZoneInfo so DST transitions are handled correctly;
    naive datetimes never reach the database.
    """
    tz = ZoneInfo(timezone_name)
    start = datetime.combine(day, time.min, tzinfo=tz)
    next_day = day + timedelta(days=1)
    end = datetime.combine(next_day, time.min, tzinfo=tz)
    return start, end


def filter_only(queryset, filters: ListFilters, timezone_name: str):
    """Apply ONLY the relational filter predicates (Step 5A.4.2a split).

    No ordering — this is the scope-building half of ``apply_filters``
    (the search scope queryset reuses exactly these predicates, so
    filtering semantics can never diverge between the Library listing
    and a scoped keyword search).

    Archived Recordings are excluded here — the ONE central exclusion
    shared by the Library item projection/count/identity UNION, the
    recording-scope search queryset and every scoped keyword/semantic/
    hybrid engine — so archived rows can never reach a user-facing
    result. Detail/history/export reads never use this helper and stay
    available for restore/audit.
    """
    queryset = queryset.filter(archived_at__isnull=True)

    if filters.date:
        start, end = local_day_bounds(filters.date, timezone_name)
        queryset = queryset.filter(effective_at__gte=start, effective_at__lt=end)
    if filters.date_from:
        start, _ = local_day_bounds(filters.date_from, timezone_name)
        queryset = queryset.filter(effective_at__gte=start)
    if filters.date_to:
        _, end = local_day_bounds(filters.date_to, timezone_name)
        queryset = queryset.filter(effective_at__lt=end)

    if filters.tags:
        # Recording-scope only (defense-in-depth): Step 6.2 section-
        # scoped assignments are per-section concerns, never matched by
        # the recording Library/search tag filters until item projection
        # arrives.
        if filters.tag_match == "any":
            queryset = queryset.filter(
                tag_assignments__is_active=True,
                tag_assignments__section__isnull=True,
                tag_assignments__tag__name_key__in=filters.tags,
            ).distinct()
        else:
            # AND semantics: the recording must carry EVERY selected tag.
            for key in filters.tags:
                queryset = queryset.filter(
                    tag_assignments__is_active=True,
                    tag_assignments__section__isnull=True,
                    tag_assignments__tag__name_key=key,
                )

    if filters.status:
        queryset = queryset.filter(processing_status=filters.status)
    if filters.summary:
        queryset = queryset.filter(summary_status=filters.summary)

    if filters.review:
        queryset = queryset.filter(
            Q(processing_status=ProcessingStatus.NEEDS_REVIEW)
            | Q(processing_status=ProcessingStatus.FAILED)
            | Q(retranscription_failed=True)
            | Q(resummarization_failed=True)
            | Q(summary_status=SummaryState.FAILED)
        ).distinct()

    if filters.audio:
        queryset = queryset.filter(audio_status=filters.audio)

    if filters.has_summary is not None:
        current = Q(
            summaries__is_active=True,
            summaries__transcript__is_active=True,
            summaries__section__ordinal=0,
            summaries__section__segmented_version__isnull=True,
        )
        queryset = queryset.filter(current).distinct() if filters.has_summary else queryset.exclude(current).distinct()

    return queryset


def apply_filters(queryset, filters: ListFilters, timezone_name: str):
    """Apply parsed filters and the list ordering (historical contract:
    exact composition of :func:`filter_only` + :func:`apply_sort`)."""
    return apply_sort(filter_only(queryset, filters, timezone_name), filters.sort)


def apply_sort(queryset, sort: str):
    """Order a (filtered) queryset by one of the Library sort choices.

    ``relevance`` is NEVER ordered here: search-mode relevance order is
    the engine's comparator output and is preserved by the search
    orchestration layer, never translated into database ordering.
    """
    return queryset.order_by(*_sort_order(sort))


def search_scope_queryset(filters: ListFilters, timezone_name: str):
    """Lightweight ``Recording`` eligibility queryset for search scoping.

    The search backend receives ONLY this constrained QuerySet (never
    compiled SQL): the engine validates the model, clears ordering and
    forces a single-column PK selection before compiling it. Built on
    the plain model with just the ``effective_at`` annotation the date
    filters need — no presentation annotations, no select/prefetch
    related, no ordering — and it reuses :func:`filter_only`, so scope
    semantics can never diverge from the Library filters.
    """
    queryset = Recording.objects.annotate(effective_at=effective_at_annotation())
    return filter_only(queryset, filters, timezone_name)


def _sort_order(sort: str):
    """Deterministic database-side ordering for the Library.

    Title sorts use the exact annotated ``display_title`` under the
    Unicode-aware ``unicode_fold`` SQLite collation (NFC + casefold,
    registered on every connection by ``workflow.sqlite_unicode``) with
    PK as the final tie-break; the Coalesce in ``display_title``
    guarantees non-null values.
    """
    if sort == "oldest":
        return ("effective_at", "pk")
    if sort == "title_az":
        ensure_registered()
        return (folded_title_expression().asc(nulls_last=True), "pk")
    if sort == "title_za":
        ensure_registered()
        return (folded_title_expression().desc(nulls_last=True), "pk")
    return ("-effective_at", "pk")


def recording_detail_queryset(recording_pk: str):
    """Single recording with the same prefetch contract as the list."""
    return recording_list_queryset().filter(pk=recording_pk)


def tag_overview() -> list[dict]:
    """Tags with active-assignment counts, configured first."""
    tags = Tag.objects.annotate(
        active_count=Count("assignments", filter=Q(assignments__is_active=True)),
        total_count=Count("assignments"),
    ).order_by("is_configured", "name")
    return [
        {
            "tag": tag,
            "active_count": tag.active_count,
            "total_count": tag.total_count,
        }
        for tag in tags
    ]
