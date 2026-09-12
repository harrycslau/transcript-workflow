"""Segmented-version writer and validator (Step 6.1 foundation).

This module is the ONLY writer of ``SegmentedVersion`` rows and topic
``Section`` rows (sections with a non-null ``segmented_version``). It
materializes the exhaustive topic partition from a crop range plus
sorted split markers (zero markers => zero topic sections; N markers =>
exactly N+1 sections), and creates one new immutable active revision per
save while superseding the prior active one, all in ONE transaction.

Contract (approved Step 6.1):

- the recording's currently ACTIVE transcript is required; historical
  transcripts are never written;
- inputs are validated exactly: an exact bounded ``str`` recording id
  (``Recording.pk`` is a UUID ``CharField``), an exact positive ``int``
  transcript id, exact ints for the range/split markers (never bool),
  exact list/tuple collections, exact strings, bounded sizes
  (``MAX_TOPIC_SECTIONS``), nonblank titles (preserved exactly after
  the strip-based blank check), no newline/control characters, no
  coercion;
- an optional parallel ``title_is_temporary`` flags list (exact
  ``bool`` per title) marks SERVER-DERIVED titles: a True flag with a
  blank title is filled with ``Segment N of YYYYMMDDHHMM`` (N = the
  1-based canonical section ordinal; timestamp = ``recorded_at`` else
  ``discovered_at`` in the configured timezone) and a True flag with a
  non-blank title must EXACTLY equal that derived value or the save
  fails closed with the stable ``title_flag_forgery`` category — a
  custom title can never be claimed temporary; the no-op comparison
  uses the RAW submitted payload, so an UNCHANGED active layout stays a
  usable no-op even when the stored temporary titles were derived under
  an older effective timestamp;
- READ-side validation (``canonical_layout_*``, the fingerprint, the
  Library SQL predicate) requires a True temporary flag to carry the
  EXACT canonical SHAPE ``Segment <ordinal> of <12 ASCII digits>`` —
  an arbitrary custom title on a temporary row is corrupt stored state
  and fails closed as ``layout_invalid``; reads NEVER compare a stored
  temporary title to the CURRENT timestamp (titles are immutable
  creation-time metadata);
- segment ordinals are derived from the database and must be exactly
  contiguous ``0..count-1`` with a nonempty transcript; the working
  range must satisfy ``0 <= start < end <= count``;
- duplicate, endpoint, and outside-range splits are rejected; split
  order is canonicalized (sorted);
- an unchanged canonical payload is a no-op with ZERO DML (no new
  revision, no supersede, no callback); the initial full+zero state
  with no existing version is also a no-op;
- clear crop is represented by a new full ``[0, count)`` zero-section
  revision when it replaces a non-full/split active version;
- old versions/Sections are never mutated; no summaries, variant
  states, suggestions, or tags are copied;
- the service does NOT acquire the pipeline lock, does NOT schedule any
  search/embedding sync, logs nothing, and touches no network/files.

Failures raise :class:`SegmentationError` with a stable sanitized
``code``; the message is fixed per category and never contains input
values, ids, paths, or transcript content.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Count, Max, Min
from django.utils import timezone

from workflow.models import Recording, Section, SegmentedVersion, Transcript

# Hard defensive cap on topic sections per version (no config key).
MAX_TOPIC_SECTIONS = 200
# Follows Section.title max_length (the model's own limit).
MAX_TOPIC_TITLE_LENGTH = 255

_MESSAGES = {
    "invalid_input": "invalid segmentation input",
    "invalid_fingerprint": "invalid or missing state fingerprint",
    "stale_state": "The trim & split state changed since the page was opened — reload and try again.",
    "layout_invalid": "The stored trim & split revision is invalid.",
    "title_blank": "topic title is blank",
    "title_too_long": "topic title is too long",
    "title_invalid_chars": "topic title contains forbidden characters",
    "title_count_mismatch": "topic title count does not match split count",
    "title_flag_count_mismatch": "temporary-title flag count does not match the topic count",
    "title_flag_forgery": "A topic title marked temporary does not match the server-derived name.",
    "too_many_topics": "too many topic sections",
    "recording_not_found": "recording not found",
    "transcript_not_found": "transcript not found or does not belong to the recording",
    "transcript_not_active": "transcript is not the recording's active transcript",
    "transcript_empty": "transcript has no segments",
    "segments_not_contiguous": "transcript segments are not contiguous from ordinal 0",
    "range_out_of_bounds": "working range is outside the transcript segment count",
    "empty_range": "working range is empty",
    "split_out_of_range": "split marker is outside the working range",
    "split_at_endpoint": "split marker is at a range endpoint",
    "duplicate_split": "split markers contain duplicates",
    "storage_error": "segmentation storage failure",
    "section_not_found": "section not found",
    "section_not_topic": "section is not a topic section",
    "section_not_active": "section belongs to a historical layout revision",
    "section_not_in_layout": "section does not belong to the active layout",
    "section_not_in_recording": "section does not belong to the recording",
    "section_state_too_large": "section summary state is too large to fingerprint",
}


class SegmentationError(Exception):
    """A sanitized segmentation failure; ``code`` is a stable category.

    Messages are fixed per category and never contain input values,
    paths, ids, or transcript content.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(_MESSAGES.get(code, "invalid segmentation request"))


@dataclass(frozen=True)
class SegmentationResult:
    """Safe counts/identifiers for one save; never raw data."""

    created: bool
    version_id: str | None = None
    revision: int | None = None
    superseded_revision: int | None = None
    topic_section_count: int = 0


def _require_int(value):
    """Exact int only; ``bool`` is rejected (no coercion)."""
    if type(value) is not int:
        raise SegmentationError("invalid_input")
    return value


def _require_recording_id(value):
    """Exact nonblank bounded string id (``Recording.pk`` is UUID)."""
    if type(value) is not str or not value.strip() or len(value) > 36:
        raise SegmentationError("invalid_input")
    return value


def _require_transcript_id(value):
    """Exact positive int only; ``bool`` is rejected (no coercion)."""
    if type(value) is not int or value <= 0:
        raise SegmentationError("invalid_input")
    return value


def _require_sequence(value):
    """Exact list/tuple only; subclasses and other iterables are
    rejected (``type`` identity, no coercion)."""
    if type(value) not in (list, tuple):
        raise SegmentationError("invalid_input")
    return value


def _validate_title(title: str) -> None:
    if type(title) is not str:
        raise SegmentationError("invalid_input")
    if not title.strip():
        raise SegmentationError("title_blank")
    if len(title) > MAX_TOPIC_TITLE_LENGTH:
        raise SegmentationError("title_too_long")
    # C0 controls (incl. newline/tab/CR) and DEL are forbidden.
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in title):
        raise SegmentationError("title_invalid_chars")


def _transcript_stats(transcript, *, using: str) -> tuple[int, int, int]:
    """(count, lo, hi) of the transcript's segment ordinals in ONE bounded
    aggregate (never materializing every ordinal)."""
    stats = transcript.segments.using(using).aggregate(
        count=Count("pk"), lo=Min("ordinal"), hi=Max("ordinal")
    )
    return stats["count"], stats["lo"], stats["hi"]


def _stored_title_valid(title) -> bool:
    """A stored topic title is usable only as an exact nonblank bounded
    string without control characters (same rules as user input)."""
    return (
        type(title) is str
        and title.strip()
        and len(title) <= MAX_TOPIC_TITLE_LENGTH
        and not any(ord(ch) < 32 or ord(ch) == 127 for ch in title)
    )


def _temporary_title_shape_valid(ordinal, title) -> bool:
    """Strict canonical SHAPE of a stored TEMPORARY title (Step 6.2a
    read contract): exactly ``Segment <ordinal> of <12 ASCII digits>``.

    Titles are immutable creation-time metadata and the configured
    timezone / effective timestamp may later change, so reads NEVER
    compare a stored temporary title to the CURRENT server-derived
    value — only the exact canonical shape is validated. A corrupt row
    with ``title_is_temporary=True`` and an arbitrary custom title
    therefore fails closed in every canonical service read.
    """
    if type(title) is not str or type(ordinal) is not int or ordinal < 1:
        return False
    prefix = f"Segment {ordinal} of "
    if not title.startswith(prefix):
        return False
    digits = title[len(prefix):]
    return len(digits) == 12 and digits.isascii() and digits.isdigit()


def derive_temporary_section_title(recording, ordinal: int, timezone_name: str) -> str:
    """Server-authoritative auto title for a split-created topic Section.

    ``Segment N of YYYYMMDDHHMM`` where N is the 1-based canonical
    Section ordinal and the timestamp is the recording's effective time
    (``recorded_at`` else ``discovered_at``) rendered in the configured
    timezone. The client NEVER derives this value:

    - a blank True-flag title is FILLED with this value at save time;
    - a non-blank True-flag title MUST EXACTLY equal this value (any
      other non-blank value is a forgery — a custom title can never be
      claimed temporary).

    An aware datetime is converted to the configured zone; a naive one
    is formatted as already-local (the same convention as the Library's
    month labels). Pure and deterministic; never a write/network.
    """
    if type(ordinal) is not int or ordinal < 1:
        raise SegmentationError("invalid_input")
    dt = recording.recorded_at or recording.discovered_at
    if dt is None:
        dt = timezone.now()
    if timezone.is_aware(dt):
        dt = dt.astimezone(ZoneInfo(timezone_name))
    return f"Segment {ordinal} of {dt.strftime('%Y%m%d%H%M')}"


def canonical_layout_for_transcript(version, transcript, *, using: str = "default") -> dict:
    """Bounded READ-ONLY canonical validation of one SegmentedVersion.

    Used by the save comparison, the fingerprint, and GET presentation —
    ONE shared canonical representation. Queries at most
    ``MAX_TOPIC_SECTIONS + 1`` Section rows (limit+1 sentinel) and never
    trusts stored state: any oversized, cross-transcript, malformed,
    non-partitioning, out-of-range, or title-invalid layout raises the one
    fixed sanitized ``layout_invalid`` category (never a partial result,
    never a misleading "no layout").

    Returns:
      {"id", "revision", "start", "end_exclusive",
       "sections": tuple[{ordinal,start,end,title,title_is_temporary}, ...],
       "splits": tuple[int, ...], "titles": tuple[str, ...]}
    """
    if version is None or version.transcript_id != transcript.pk:
        raise SegmentationError("layout_invalid")
    count, lo, hi = _transcript_stats(transcript, using=using)
    rows = list(
        version.sections.using(using)
        .order_by("ordinal")
        .values_list(
            "transcript_id", "ordinal", "start_segment_ordinal",
            "end_segment_ordinal_exclusive", "title", "title_is_temporary",
        )[: MAX_TOPIC_SECTIONS + 1]
    )
    return canonical_layout_from_rows(version, transcript, count, lo, hi, rows)


def canonical_layout_from_rows(
    version, transcript, segment_count: int, segment_lo, segment_hi, rows
) -> dict:
    """Shared bounded canonical validation over PRE-FETCHED section rows.

    The row-level core of :func:`canonical_layout_for_transcript`, used
    by callers that already hold the Section rows for a batch of versions
    (the Library prepass) so the SAME fail-closed validation runs without
    a per-layout query. ``rows`` are exactly the tuples
    ``(transcript_id, ordinal, start_segment_ordinal,
    end_segment_ordinal_exclusive, title, title_is_temporary)`` in
    ordinal order (the shape ``canonical_layout_for_transcript``
    fetches); ``segment_count``/``segment_lo``/``segment_hi`` come from
    the shared bounded segment aggregate. Malformed stored state raises
    the one fixed sanitized ``layout_invalid`` category — never a
    partial result.
    """
    if segment_count == 0 or segment_lo != 0 or segment_hi != segment_count - 1:
        raise SegmentationError("layout_invalid")
    start = version.start_segment_ordinal
    end = version.end_segment_ordinal_exclusive
    if type(start) is not int or type(end) is not int or start < 0 or end > segment_count or start >= end:
        raise SegmentationError("layout_invalid")
    if len(rows) > MAX_TOPIC_SECTIONS:
        raise SegmentationError("layout_invalid")
    if len(rows) == 1:
        # A lone topic Section is not representable by the approved
        # contract: zero splits => zero Sections, N>=1 splits => N+1>=2
        # Sections. Exactly one Section (whatever its range) is therefore
        # corrupt stored state, never a valid crop/split layout.
        raise SegmentationError("layout_invalid")
    sections: list[dict] = []
    splits: list[int] = []
    titles: list[str] = []
    if rows:
        # Exact exhaustive partition of [start, end): ordinals 1..N,
        # contiguous nonempty sub-ranges, first start == start, last end
        # == end; every Section row must belong to the SAME transcript as
        # its layout (SQLite cannot CHECK this cross-table). A crop-only
        # version legitimately has ZERO rows (its range may still be
        # nonempty). The temporary-title flag is part of the immutable
        # layout state and must be an exact bool (a non-bool stored value
        # is corrupt state, never silently coerced).
        cursor = start
        for index, (row_transcript_id, ordinal, sec_start, sec_end, title, flag) in enumerate(rows, start=1):
            if (
                row_transcript_id != transcript.pk
                or ordinal != index
                or sec_start != cursor
                or type(sec_end) is not int
                or sec_end <= sec_start
                or not _stored_title_valid(title)
                or type(flag) is not bool
                # A True temporary flag requires the exact canonical
                # "Segment <ordinal> of <12 ASCII digits>" SHAPE — an
                # arbitrary custom title on a temporary row is corrupt
                # stored state (reads never compare to the current
                # timestamp, only the shape).
                or (flag and not _temporary_title_shape_valid(ordinal, title))
            ):
                raise SegmentationError("layout_invalid")
            sections.append(
                {
                    "ordinal": ordinal,
                    "start": sec_start,
                    "end": sec_end,
                    "title": title,
                    "title_is_temporary": flag,
                }
            )
            titles.append(title)
            cursor = sec_end
        if cursor != end:
            raise SegmentationError("layout_invalid")
        splits = [sec["end"] for sec in sections[:-1]]
    return {
        "id": version.pk,
        "revision": version.revision,
        "start": start,
        "end_exclusive": end,
        "sections": tuple(sections),
        "splits": tuple(splits),
        "titles": tuple(titles),
    }


def require_active_topic_section(section, *, using: str = "default") -> dict:
    """Validate a Section as a LIVE topic target (Step 6.2).

    A usable section target must be a TOPIC section (``segmented_version``
    non-NULL) whose transcript is the recording's ACTIVE transcript and
    whose segmented version is that transcript's ACTIVE revision, with a
    canonical stored layout. Fixed whole-recording sections, historical/
    superseded revisions, cross-parent rows (the section's transcript
    differing from its version's transcript), and malformed stored
    layouts are rejected with stable sanitized ``SegmentationError``
    codes — the same fail-closed canonical validation used by the
    Step 6.1 editor:

    - ``section_not_found``: no section object (or it no longer exists);
    - ``section_not_topic``: a fixed whole-recording section (no version);
    - ``transcript_not_active``: the section's transcript is historical;
    - ``section_not_active``: the section's version is not the active
      revision (historical layout);
    - ``layout_invalid``: cross-parent ownership or malformed stored
      layout (via :func:`canonical_layout_for_transcript`);
    - ``section_not_in_layout``: the section is not one of the active
      version's canonical topic sections.

    The Section, its transcript and its version are RE-FETCHED from the
    database (never the caller's cached FK state), so a layout that
    became historical after the caller captured the object is always
    rejected — callers that run inside a retried top-level transaction
    therefore re-validate the CURRENT state on every attempt.

    Returns the canonical layout dict of the active version (see
    :func:`canonical_layout_for_transcript`). READ-ONLY; never acquires
    locks, never writes, logs nothing.
    """
    if section is None or not isinstance(section, Section):
        raise SegmentationError("section_not_found")
    fresh = (
        Section.objects.using(using)
        .select_related("transcript", "segmented_version")
        .filter(pk=section.pk)
        .first()
    )
    if fresh is None:
        raise SegmentationError("section_not_found")
    if fresh.segmented_version_id is None:
        raise SegmentationError("section_not_topic")
    transcript = fresh.transcript
    if not transcript.is_active:
        raise SegmentationError("transcript_not_active")
    version = fresh.segmented_version
    if not version.is_active:
        raise SegmentationError("section_not_active")
    if fresh.transcript_id != version.transcript_id:
        # Cross-parent: a topic Section row whose transcript differs from
        # its version's transcript (SQLite cannot CHECK this cross-table;
        # the service is the authority).
        raise SegmentationError("layout_invalid")
    canonical = canonical_layout_for_transcript(version, transcript, using=using)
    if not any(sec["ordinal"] == fresh.ordinal for sec in canonical["sections"]):
        raise SegmentationError("section_not_in_layout")
    return canonical


def _canonical_payload(version, transcript, *, using: str = "default") -> tuple:
    """The canonical ``(start, end_exclusive, splits, titles, flags)``
    payload of a version — fail-closed: malformed stored state raises
    ``layout_invalid`` instead of being treated as a no-op. The
    temporary-title flags are part of the immutable layout state and
    therefore part of the no-op comparison."""
    canonical = canonical_layout_for_transcript(version, transcript, using=using)
    return (
        canonical["start"],
        canonical["end_exclusive"],
        canonical["splits"],
        canonical["titles"],
        tuple(sec["title_is_temporary"] for sec in canonical["sections"]),
    )


def save_segmented_version(
    recording_id,
    transcript_id,
    start,
    end_exclusive,
    split_markers=(),
    topic_titles=(),
    title_is_temporary=(),
    *,
    timezone_name: str = "UTC",
    using: str = "default",
) -> SegmentationResult:
    """Create (or no-op) the active segmented version for a transcript.

    ``recording_id``/``transcript_id`` must identify the recording's
    currently ACTIVE transcript. ``[start, end_exclusive)`` is the
    canonical working range over segment ordinals. ``split_markers`` are
    interior boundaries inside ``(start, end_exclusive)``; each produces
    one additional topic section. ``topic_titles`` holds exactly the
    ordered titles (zero titles for zero splits; N+1 titles for N
    splits).

    ``title_is_temporary`` (optional) holds exactly one exact ``bool``
    per topic title (0/1-style ``True``/``False`` only; ``bool``
    subclasses are rejected). Semantics (Step 6.2a):

    - a True flag means the title is SERVER-DERIVED: a blank title is
      filled with ``Segment N of YYYYMMDDHHMM`` (N = the 1-based
      canonical Section ordinal; timestamp = ``recorded_at`` else
      ``discovered_at`` in ``timezone_name``) and a non-blank title must
      EXACTLY equal that derived value or the whole save fails closed
      with the stable ``title_flag_forgery`` category (a custom title
      can never be claimed temporary);
    - a False flag (or an omitted flags tuple — the legacy/custom
      default) validates the title exactly as before and stores it
      verbatim with ``title_is_temporary=False``.

    Returns a :class:`SegmentationResult` with safe counts only, or
    raises :class:`SegmentationError` (nothing written on failure).

    Sanitizing boundary: input-validation ``SegmentationError``
    categories pass through unchanged; any OTHER ``Exception`` raised by
    the storage layer is rolled back and mapped to the single fixed
    ``storage_error`` category (never values, ids, paths, SQL, or the
    original message). ``BaseException`` subclasses (``KeyboardInterrupt``
    / ``SystemExit``) are never caught.
    """
    recording_id = _require_recording_id(recording_id)
    transcript_id = _require_transcript_id(transcript_id)
    start = _require_int(start)
    end_exclusive = _require_int(end_exclusive)
    split_markers = _require_sequence(split_markers)
    topic_titles = _require_sequence(topic_titles)
    title_is_temporary = _require_sequence(title_is_temporary)

    # Bound collection sizes BEFORE touching any element: a hostile
    # oversized list (objects whose type/str behavior must never be
    # invoked) fails here with the stable too_many_topics category.
    # 199 markers are the maximum that can produce a valid 200-section
    # version (199 + 1); 200 titles are the maximum a valid version needs.
    if len(split_markers) > MAX_TOPIC_SECTIONS - 1:
        raise SegmentationError("too_many_topics")
    if len(topic_titles) > MAX_TOPIC_SECTIONS:
        raise SegmentationError("too_many_topics")
    if len(title_is_temporary) > MAX_TOPIC_SECTIONS:
        raise SegmentationError("too_many_topics")

    for marker in split_markers:
        _require_int(marker)
    # Exact flags only: any non-bool flag is rejected BEFORE any element
    # of the titles is touched (a hostile object's ``__str__`` is never
    # invoked on this path).
    if title_is_temporary:
        if len(title_is_temporary) != len(topic_titles):
            raise SegmentationError("title_flag_count_mismatch")
        for flag in title_is_temporary:
            if type(flag) is not bool:
                raise SegmentationError("invalid_input")
    else:
        # Omitted flags = the legacy/custom default: every title is
        # custom (migration 0013 default). The editor always sends
        # explicit flags; this keeps pre-0013 callers/tests valid.
        title_is_temporary = tuple(False for _ in topic_titles)

    # Title shape validation, flag-aware: a True-flag title may be blank
    # (the server fills the derived name inside the transaction) but any
    # non-blank True-flag title must still be shape-valid; a False-flag
    # (custom) title is validated exactly as before. The exact
    # derived-equality check for non-blank True-flag titles happens in the
    # transactional save (it needs the Recording row).
    for index, title in enumerate(topic_titles):
        if title_is_temporary[index]:
            if title:
                _validate_title(title)
        else:
            _validate_title(title)

    # Zero splits -> zero titles; N splits (N >= 1) -> exactly N+1 titles.
    topic_count = len(split_markers) + 1 if split_markers else 0
    if len(topic_titles) != topic_count:
        raise SegmentationError("title_count_mismatch")

    # Canonicalize split order; duplicates are rejected, never merged.
    splits = tuple(sorted(split_markers))
    if len(set(splits)) != len(splits):
        raise SegmentationError("duplicate_split")

    try:
        return _save_locked(
            recording_id,
            transcript_id,
            start,
            end_exclusive,
            splits,
            topic_titles,
            title_is_temporary,
            timezone_name=timezone_name,
            using=using,
        )
    except SegmentationError:
        raise
    except Exception:
        # Unexpected storage/runtime failure: roll back (the atomic block
        # already did) and surface ONE fixed safe category. ``from None``
        # keeps the original exception text/SQL/values out of the error.
        raise SegmentationError("storage_error") from None


def _save_locked(
    recording_id: str,
    transcript_id: int,
    start: int,
    end_exclusive: int,
    splits: tuple,
    topic_titles: list | tuple,
    topic_flags: list | tuple,
    *,
    timezone_name: str,
    using: str,
) -> SegmentationResult:
    """Locked single-transaction save; unexpected failures are mapped to
    ``storage_error`` by the public boundary."""
    with transaction.atomic(using=using):
        recording = (
            Recording.objects.using(using)
            .select_for_update()
            .filter(pk=recording_id)
            .first()
        )
        if recording is None:
            raise SegmentationError("recording_not_found")

        transcript = (
            Transcript.objects.using(using)
            .select_for_update()
            .filter(pk=transcript_id, recording=recording)
            .first()
        )
        if transcript is None:
            raise SegmentationError("transcript_not_found")
        if not transcript.is_active:
            raise SegmentationError("transcript_not_active")

        # Derive the segment count and contiguity from ONE bounded
        # aggregate (never materializing every ordinal). ``(transcript,
        # ordinal)`` is unique, so count distinct values all lying in
        # [0, count-1] (min == 0 and max == count-1) are exactly
        # 0..count-1.
        count, lo, hi = _transcript_stats(transcript, using=using)
        if count == 0:
            raise SegmentationError("transcript_empty")
        if lo != 0 or hi != count - 1:
            raise SegmentationError("segments_not_contiguous")

        if start < 0 or end_exclusive > count:
            raise SegmentationError("range_out_of_bounds")
        if start >= end_exclusive:
            raise SegmentationError("empty_range")
        for marker in splits:
            if marker < start or marker > end_exclusive:
                raise SegmentationError("split_out_of_range")
            if marker == start or marker == end_exclusive:
                raise SegmentationError("split_at_endpoint")

        # The no-op comparison uses the RAW submitted titles/flags: an
        # unchanged active payload stays a no-op even when the stored
        # temporary titles were derived under an older effective timestamp
        # (reads validate shape only, never current-timestamp equality).
        raw_payload = (
            start,
            end_exclusive,
            splits,
            tuple(topic_titles),
            tuple(topic_flags),
        )

        current = (
            SegmentedVersion.objects.using(using)
            .filter(transcript=transcript, is_active=True)
            .first()
        )
        # No-op: payload identical to the current active version, or the
        # initial full+zero state with no existing version at all. The
        # current active layout is validated through the SAME bounded
        # canonical representation (malformed stored state fails closed
        # with layout_invalid — never a silent no-op).
        current_payload = None
        if current is not None:
            current_payload = _canonical_payload(current, transcript, using=using)
        if current_payload == raw_payload:
            return SegmentationResult(created=False)
        if current is None and raw_payload == (0, count, (), (), ()):
            return SegmentationResult(created=False)

        # A REAL change: apply the temporary-title writer rules. A True
        # flag with a non-blank title must EXACTLY equal the CURRENT
        # server-derived name (forgery); a blank True-flag title is
        # filled with it; a False flag validates the custom title
        # exactly as before.
        if splits:
            bounds = (start,) + splits + (end_exclusive,)
            topic_sections = []
            for i in range(1, len(bounds)):
                ordinal = i
                title = topic_titles[i - 1]
                flag = topic_flags[i - 1]
                if flag:
                    expected = derive_temporary_section_title(
                        recording, ordinal, timezone_name
                    )
                    if title and title != expected:
                        raise SegmentationError("title_flag_forgery")
                    title = expected
                else:
                    _validate_title(title)
                topic_sections.append(
                    (ordinal, bounds[i - 1], bounds[i], title, flag)
                )
        else:
            topic_sections = []

        now = timezone.now()
        superseded_revision = None
        if current is not None:
            superseded_revision = current.revision
            current.is_active = False
            current.superseded_at = now
            current.save(update_fields=["is_active", "superseded_at"])

        last_revision = (
            SegmentedVersion.objects.using(using)
            .filter(transcript=transcript)
            .order_by("-revision")
            .values_list("revision", flat=True)
            .first()
        )
        revision = (last_revision or 0) + 1
        version = SegmentedVersion.objects.using(using).create(
            transcript=transcript,
            revision=revision,
            start_segment_ordinal=start,
            end_segment_ordinal_exclusive=end_exclusive,
            is_active=True,
            activated_at=now,
        )
        Section.objects.using(using).bulk_create(
            [
                Section(
                    transcript=transcript,
                    segmented_version=version,
                    ordinal=ordinal,
                    title=title,
                    title_is_temporary=flag,
                    start_segment_ordinal=lo,
                    end_segment_ordinal_exclusive=hi,
                )
                for ordinal, lo, hi, title, flag in topic_sections
            ]
        )
        return SegmentationResult(
            created=True,
            version_id=version.pk,
            revision=revision,
            superseded_revision=superseded_revision,
            topic_section_count=len(topic_sections),
        )


def range_label(start: int, end_exclusive: int) -> str:
    """User-friendly inclusive segment label for a half-open range.

    ``[start, end_exclusive)`` renders as ``segments N–M`` (inclusive) or
    ``segment N`` for a single-segment range. Pure string formatting;
    segment ordinals are canonical ints, so nothing here is ever hostile.
    """
    if end_exclusive - start <= 1:
        return f"segment {start}"
    return f"segments {start}–{end_exclusive - 1}"


def segmentation_fingerprint(
    recording_id, transcript, *, timezone_name: str = "UTC", using: str = "default"
) -> str:
    """Opaque read-only fingerprint of the segmentation state a page was
    rendered from.

    Strictly SELECT-only (no writes, network, subprocess, or locks) and
    bounded. Returns a canonical 64-lowercase-hex SHA-256 over the
    deterministic state that determines what a save would produce:

    - the recording's currently ACTIVE transcript identity;
    - the selected transcript's segment count and ordinal shape
      (``0..count-1`` contiguity);
    - the active ``SegmentedVersion`` identity/revision/range and its
      canonical bounded section metadata (via
      :func:`canonical_layout_for_transcript` — malformed stored state
      raises ``layout_invalid``, it is never folded into the hash);
    - the timezone name and the recording's effective timestamp
      (``recorded_at`` else ``discovered_at``): temporary split titles
      are derived from them, so a change in either invalidates every
      rendered save form.

    The value is OPAQUE: titles/ids never appear in the hidden form value.
    The confirmed save re-computes this after the pipeline lock and treats
    any mismatch as a safe no-op, so a stale or duplicate form can never
    save against a state the user did not see.
    """
    active_transcript_pk = (
        Transcript.objects.using(using)
        .filter(recording_id=str(recording_id), is_active=True)
        .values_list("pk", flat=True)
        .first()
    )
    effective_row = (
        Recording.objects.using(using)
        .filter(pk=str(recording_id))
        .values_list("recorded_at", "discovered_at")
        .first()
    )
    effective_iso = None
    if effective_row is not None:
        effective = effective_row[0] or effective_row[1]
        if effective is not None:
            effective_iso = effective.isoformat()
    count, lo, hi = _transcript_stats(transcript, using=using)
    active = (
        SegmentedVersion.objects.using(using)
        .filter(transcript=transcript, is_active=True)
        .first()
    )
    version_state = None
    if active is not None:
        canonical = canonical_layout_for_transcript(active, transcript, using=using)
        version_state = (
            canonical["id"],
            canonical["revision"],
            canonical["start"],
            canonical["end_exclusive"],
            canonical["sections"],
        )
    state = {
        "recording": str(recording_id),
        "active_transcript": active_transcript_pk,
        "transcript": transcript.pk,
        "segment_count": count,
        "segment_lo": lo,
        "segment_hi": hi,
        "timezone": timezone_name,
        "effective_at": effective_iso,
        "version": version_state,
    }
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def validate_payload_for_transcript(
    recording, transcript, payload, *, timezone_name: str = "UTC", using: str = "default"
) -> None:
    """READ-ONLY semantic validation of a parsed payload (no lock/recovery/
    write). Rejects, with a fixed sanitized category:

    - a transcript that is not the recording's ACTIVE transcript;
    - a nonempty non-contiguous segment shape;
    - out-of-bounds/empty ranges, endpoint/out-of-range/duplicate splits;
    - blank/oversized/control-character titles (custom titles) and
      temporary-title flag forgeries (a True-flag non-blank title that
      does not EXACTLY equal the server-derived ``Segment N of
      YYYYMMDDHHMM`` value for its ordinal) — EXCEPT for an UNCHANGED
      payload, whose stored temporary titles are immutable creation-time
      metadata and remain a usable no-op even after the effective
      timestamp/timezone changed;
    - a corrupt current active layout (fail-closed, via the shared
      canonical validator — a temporary row with an arbitrary custom
      title is corrupt).

    The fingerprint STALENESS comparison is deliberately NOT part of this
    helper: the first POST compares it in the view before showing the
    confirmation; the confirmed POST recomputes it after the pipeline lock
    (stale => safe no-op conflict).
    """
    if recording.pk != transcript.recording_id:
        raise SegmentationError("transcript_not_found")
    if not transcript.is_active:
        raise SegmentationError("transcript_not_active")
    count, lo, hi = _transcript_stats(transcript, using=using)
    if count == 0:
        raise SegmentationError("transcript_empty")
    if lo != 0 or hi != count - 1:
        raise SegmentationError("segments_not_contiguous")

    start = payload["start"]
    end = payload["end_exclusive"]
    if start < 0 or end > count:
        raise SegmentationError("range_out_of_bounds")
    if start >= end:
        raise SegmentationError("empty_range")
    splits = payload["splits"]
    if len(set(splits)) != len(splits):
        raise SegmentationError("duplicate_split")
    for marker in splits:
        if marker < start or marker > end:
            raise SegmentationError("split_out_of_range")
        if marker == start or marker == end:
            raise SegmentationError("split_at_endpoint")

    # Title + temporary-flag validation: exact flags only (cardinality and
    # type). For a REAL change, a True flag with a non-blank title must
    # EXACTLY equal the CURRENT server-derived temporary title (forgery)
    # and a False flag validates the custom title exactly as before. An
    # UNCHANGED payload (a no-op re-submission of the current active
    # layout) skips the current-timestamp equality check: stored
    # temporary titles are immutable creation-time metadata and remain
    # usable even when the effective timestamp/timezone later changed.
    flags = payload["title_is_temporary"]
    if len(flags) != len(payload["titles"]):
        raise SegmentationError("title_flag_count_mismatch")
    for flag in flags:
        if type(flag) is not bool:
            raise SegmentationError("invalid_input")

    # Existing current state: the active layout must be canonical (fail
    # closed on corrupt stored state before any confirmation/execution;
    # a corrupt temporary row raises layout_invalid here).
    current = (
        SegmentedVersion.objects.using(using)
        .filter(transcript=transcript, is_active=True)
        .first()
    )
    raw_payload = (
        payload["start"],
        payload["end_exclusive"],
        tuple(payload["splits"]),
        tuple(payload["titles"]),
        tuple(flags),
    )
    unchanged = False
    if current is not None:
        unchanged = _canonical_payload(current, transcript, using=using) == raw_payload
    elif raw_payload == (0, count, (), (), ()):
        unchanged = True
    if unchanged:
        return

    for index, title in enumerate(payload["titles"]):
        flag = flags[index]
        if flag:
            expected = derive_temporary_section_title(recording, index + 1, timezone_name)
            if title and title != expected:
                raise SegmentationError("title_flag_forgery")
        else:
            _validate_title(title)


def _require_decimal_int(value) -> int:
    """Canonical ASCII decimal int only; ``bool``/float/blank/foreign
    digits are rejected with the stable ``invalid_input`` category."""
    if type(value) is not str or not value.isascii() or not value.isdigit():
        raise SegmentationError("invalid_input")
    return int(value)


_HEX64 = re.compile(r"[0-9a-f]{64}\Z")

# Fields the Step 6.1 save route accepts. ``split``/``title`` are the only
# repeated fields (bounded); everything else must appear exactly once
# (``csrfmiddlewaretoken`` at most once; ``confirmed`` absent or exactly
# one ``1``).
_ALLOWED_FIELDS = frozenset(
    {
        "csrfmiddlewaretoken",
        "transcript_id",
        "start",
        "end_exclusive",
        "fingerprint",
        "confirmed",
        "split",
        "title",
        "title_is_temporary",
    }
)


def _require_single(data, name: str) -> str:
    """Exactly one occurrence of a scalar field; duplicates rejected."""
    values = data.getlist(name)
    if len(values) != 1:
        raise SegmentationError("invalid_input")
    return values[0]


def _require_fingerprint(value) -> str:
    """Canonical nonblank 64-lowercase-hex SHA-256 fingerprint."""
    if type(value) is not str or not _HEX64.match(value):
        raise SegmentationError("invalid_fingerprint")
    return value


def parse_segmentation_payload(data) -> dict:
    """Strictly parse and bound the Step 6.1 save payload.

    ``data`` is a ``QueryDict`` (or any object with ``get``/``getlist``).
    Exact shape (any deviation is a fixed ``invalid_input`` /
    ``invalid_fingerprint`` category BEFORE any lock/write):

    - exactly one each of ``transcript_id`` / ``start`` / ``end_exclusive``
      / ``fingerprint`` (canonical ASCII-decimal ints; the fingerprint a
      canonical 64-lowercase-hex SHA-256);
    - ``confirmed`` absent or exactly one ``1``;
    - ``csrfmiddlewaretoken`` at most once (CSRF control);
    - ``split`` (<= ``MAX_TOPIC_SECTIONS - 1``), ``title``
      (<= ``MAX_TOPIC_SECTIONS``) and ``title_is_temporary``
      (<= ``MAX_TOPIC_SECTIONS``) as the only bounded repeated fields;
      every ``title_is_temporary`` value is exactly ``1`` or ``0`` and —
      when present — the count must exactly equal the title count
      (absent flags normalize to all ``0`` = custom, the legacy default);
    - unknown fields rejected.

    Returns
    ``{"transcript_id", "start", "end_exclusive", "splits", "titles",
    "title_is_temporary", "fingerprint", "confirmed"}``. Semantic
    validation against the database is deliberately NOT here — see
    :func:`validate_payload_for_transcript` (first POST) and
    :func:`save_segmented_version` (confirmed, transactional).
    """
    if not hasattr(data, "get") or not hasattr(data, "getlist"):
        raise SegmentationError("invalid_input")
    for key in data.keys():
        if key not in _ALLOWED_FIELDS:
            raise SegmentationError("invalid_input")
    if len(data.getlist("csrfmiddlewaretoken")) > 1:
        raise SegmentationError("invalid_input")

    transcript_id = _require_decimal_int(_require_single(data, "transcript_id"))
    if transcript_id <= 0:
        raise SegmentationError("invalid_input")
    start = _require_decimal_int(_require_single(data, "start"))
    end_exclusive = _require_decimal_int(_require_single(data, "end_exclusive"))
    fingerprint_values = data.getlist("fingerprint")
    if len(fingerprint_values) != 1:
        # Missing OR duplicated fingerprint: same stable category, and
        # rejected before any lock on the confirmed path.
        raise SegmentationError("invalid_fingerprint")
    fingerprint = _require_fingerprint(fingerprint_values[0])

    confirmed_values = data.getlist("confirmed")
    if confirmed_values:
        if len(confirmed_values) != 1 or confirmed_values[0] != "1":
            raise SegmentationError("invalid_input")
        confirmed = True
    else:
        confirmed = False

    split_values = data.getlist("split")
    title_values = data.getlist("title")
    if len(split_values) > MAX_TOPIC_SECTIONS - 1:
        raise SegmentationError("too_many_topics")
    if len(title_values) > MAX_TOPIC_SECTIONS:
        raise SegmentationError("too_many_topics")
    splits = [_require_decimal_int(value) for value in split_values]
    titles = list(title_values)
    # Exact shape: zero splits => zero titles; N splits => N+1 titles.
    # The service re-validates (it remains the authority), but a malformed
    # count is rejected here with the same stable category.
    topic_count = len(splits) + 1 if splits else 0
    if len(titles) != topic_count:
        raise SegmentationError("title_count_mismatch")

    # Temporary-title flags: exact ``0``/``1`` values only (nothing else —
    # ``True``/``on``/blank/foreign digits are rejected before any lock).
    flag_values = data.getlist("title_is_temporary")
    if len(flag_values) > MAX_TOPIC_SECTIONS:
        raise SegmentationError("too_many_topics")
    if flag_values:
        if len(flag_values) != len(titles):
            raise SegmentationError("title_flag_count_mismatch")
        flags = []
        for value in flag_values:
            if value not in ("0", "1"):
                raise SegmentationError("invalid_input")
            flags.append(value == "1")
    else:
        # Legacy payload without flags: every title is custom (migration
        # 0013 default). The editor always sends explicit flags.
        flags = [False] * len(titles)
    return {
        "transcript_id": transcript_id,
        "start": start,
        "end_exclusive": end_exclusive,
        "splits": splits,
        "titles": titles,
        "title_is_temporary": flags,
        "fingerprint": fingerprint,
        "confirmed": confirmed,
    }