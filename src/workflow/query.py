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
from zoneinfo import ZoneInfo

from django.db.models import (
    Count,
    DateTimeField,
    Prefetch,
    Q,
)
from django.db.models.functions import Coalesce
from django.db.models.query import QuerySet

from brainlib.config import tag_name_key
from workflow.models import (
    AudioStatus,
    ProcessingStatus,
    Recording,
    RoutingDecision,
    AudioSource,
    Summary,
    SummaryState,
    Tag,
    TagAssignment,
)

MAX_TAG_FILTERS = 10

VALID_PROCESSING_STATUSES = {value for value, _ in ProcessingStatus.choices}
VALID_SUMMARY_STATUSES = {value for value, _ in SummaryState.choices}


def effective_at_annotation() -> Coalesce:
    return Coalesce("recorded_at", "discovered_at", output_field=DateTimeField())


def current_summary_prefetch(to_attr: str = "current_summary_rows") -> Prefetch:
    return Prefetch(
        "summaries",
        queryset=Summary.objects.filter(
            is_active=True, transcript__is_active=True, section__ordinal=0
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
            "is_active",
            "created_at",
            "model_id",
            "generation_mode",
        ),
        to_attr=to_attr,
    )


def recording_list_queryset() -> QuerySet:
    """Base list queryset: effective ordering + the full prefetch contract.

    The contract: callers render rows exclusively through
    :class:`RecordingCard` over the ``to_attr`` lists populated here
    (``current_summary_rows``, ``active_tag_assignments``,
    ``active_routing_decisions``, ``presentation_sources``). No
    per-row queries are allowed in the loop.
    """
    return (
        Recording.objects.annotate(effective_at=effective_at_annotation())
        .select_related("last_failed_attempt")
        .prefetch_related(
            current_summary_prefetch(),
            Prefetch(
                "tag_assignments",
                queryset=TagAssignment.objects.filter(is_active=True).select_related("tag"),
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
    def overview_excerpt(self) -> str:
        summary = self.current_summary
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

    errors: list[str] = dc_field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def as_querystring(self) -> str:
        """Canonical query string (without the page parameter) so filters
        persist across pagination links."""
        import urllib.parse

        params: dict[str, list[str]] = {}
        if self.date:
            params["date"] = [self.date.isoformat()]
        if self.date_from:
            params["from"] = [self.date_from.isoformat()]
        if self.date_to:
            params["to"] = [self.date_to.isoformat()]
        if self.tags:
            params["tag"] = self.tags
        if self.tag_match != "all":
            params["tag_match"] = [self.tag_match]
        if self.status:
            params["status"] = [self.status]
        if self.summary:
            params["summary"] = [self.summary]
        if self.review:
            params["review"] = ["1"]
        if self.audio:
            params["audio"] = [self.audio]
        if self.has_summary is not None:
            params["has_summary"] = ["1" if self.has_summary else "0"]
        return urllib.parse.urlencode(params, doseq=True)


def _parse_date(value: str) -> date | None:
    return date.fromisoformat(value)


def list_filters(GET, timezone_name: str = "Europe/Helsinki") -> ListFilters:
    """Parse and validate the recording-list query string.

    Invalid values append friendly messages to ``errors`` (the affected
    filter is ignored) — an invalid filter is never a server error.
    """
    filters = ListFilters()

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


def apply_filters(queryset, filters: ListFilters, timezone_name: str):
    """Apply parsed filters to the (already annotated) queryset."""
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
        if filters.tag_match == "any":
            queryset = queryset.filter(
                tag_assignments__is_active=True, tag_assignments__tag__name_key__in=filters.tags
            ).distinct()
        else:
            # AND semantics: the recording must carry EVERY selected tag.
            for key in filters.tags:
                queryset = queryset.filter(
                    tag_assignments__is_active=True, tag_assignments__tag__name_key=key
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
            | Q(
                processing_status=ProcessingStatus.TRANSCRIBED,
                routing_decisions__is_active=True,
                routing_decisions__routing_verified=False,
            )
        ).distinct()

    if filters.audio:
        queryset = queryset.filter(audio_status=filters.audio)

    if filters.has_summary is not None:
        current = Q(
            summaries__is_active=True,
            summaries__transcript__is_active=True,
            summaries__section__ordinal=0,
        )
        queryset = queryset.filter(current).distinct() if filters.has_summary else queryset.exclude(current).distinct()

    return queryset.order_by("-effective_at", "pk")


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
