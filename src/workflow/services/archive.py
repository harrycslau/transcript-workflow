"""Minimal reversible Recording and Section archive (never deletion).

One Recording may be archived by setting ``Recording.archived_at``. One
individual TOPIC Section may independently be archived by setting
``Section.archived_at``. Archival is purely a database eligibility
marker:

- source audio files, transcript/summary/tag/history rows, layout
  revisions and the derived ``SearchDocument``/``EmbeddingDocument``
  index rows stay physically stored and internally healthy;
- an archived Recording is ineligible for every user-facing Library /
  search / Ask / Review result and for all automatic pipeline work
  (the exclusion is centralized in ``workflow.query.filter_only`` / the
  Section branch, ``workflow.services.review`` and the pipeline work
  selectors);
- an archived TOPIC Section is ineligible for every user-facing Library /
  search / Ask item result (the exclusion is centralized in
  ``workflow.query._section_base_queryset``) while the canonical layout
  validator keeps treating it as structurally present — archive is
  eligibility, not topology. Parent suppression stays based on the FULL
  canonical layout, so an archived Section creates an intentional hidden
  item rather than resurrecting the parent Recording;
- explicit mutations refuse safely via :func:`ensure_not_archived`.

Section archive is NOT layout deletion: it never merges ranges and never
mutates/supersedes a ``SegmentedVersion``. Only a canonical topic
Section of the active transcript's active valid layout whose parent
Recording is not archived may be NEWLY archived. Restore clears the
marker; it is deliberately permissive so a Section that later became
historical can still be un-archived (that is the only mutation; the
layout stays untouched and the Section stays read-only).

Restore simply clears the marker — nothing else is touched. The
operations are transactional and idempotent, take/re-fetch the exact
Recording/Section, and never schedule search/embedding synchronization,
touch files, the network, index rows or history. Return values are safe
counts only (never content, paths or secrets).

Recording archive refuses while ANY unfinished ``ProcessingAttempt``
exists (the mutating web action runs interruption recovery first, so a
live pipeline process can never be archived mid-flight); archive never
blocks reads.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from brainlib.config import ConfigError
from workflow.models import Recording, Section

ARCHIVED_MESSAGE = (
    "This recording is archived. Restore it before running pipeline or "
    "editing actions."
)


class ArchivedRecordingError(ConfigError):
    """A mutating action was requested against an archived Recording."""

    code = "recording_archived"

    def __init__(self, message: str = ARCHIVED_MESSAGE) -> None:
        super().__init__(message)


def is_archived(recording: Recording) -> bool:
    """Read-only: whether ``recording`` is archived (NULL-safe)."""
    return recording.archived_at is not None


def ensure_not_archived(recording: Recording) -> None:
    """Raise a stable sanitized error when ``recording`` is archived."""
    if is_archived(recording):
        raise ArchivedRecordingError()


def archive_recording(recording: Recording) -> dict:
    """Archive one Recording (idempotent, transactional, no side effects).

    Returns a safe status dict:

    - ``{"result": "archived"}`` — this call set ``archived_at``;
    - ``{"result": "unchanged"}`` — already archived (zero DML);
    - ``{"result": "refused", "reason": "unfinished_attempt"}`` — a live
      ``ProcessingAttempt`` exists (nothing written; the caller normally
      ran interruption recovery first).

    Never touches files, network, index rows or history and never
    schedules search/embedding synchronization.
    """
    with transaction.atomic():
        fresh = Recording.objects.select_for_update().get(pk=recording.pk)
        if fresh.archived_at is not None:
            return {"recording_id": fresh.pk, "result": "unchanged", "archived": True}
        if fresh.attempts.filter(finished_at__isnull=True).exists():
            return {
                "recording_id": fresh.pk,
                "result": "refused",
                "reason": "unfinished_attempt",
                "archived": False,
            }
        fresh.archived_at = timezone.now()
        fresh.save(update_fields=["archived_at", "updated_at"])
        return {"recording_id": fresh.pk, "result": "archived", "archived": True}


def restore_recording(recording: Recording) -> dict:
    """Restore one archived Recording (idempotent, transactional).

    Returns ``{"result": "restored"}`` when this call cleared
    ``archived_at``, or ``{"result": "unchanged"}`` for an already-active
    Recording (zero DML). No files, network, index rows, history or sync
    are touched.
    """
    with transaction.atomic():
        fresh = Recording.objects.select_for_update().get(pk=recording.pk)
        if fresh.archived_at is None:
            return {"recording_id": fresh.pk, "result": "unchanged", "archived": False}
        fresh.archived_at = None
        fresh.save(update_fields=["archived_at", "updated_at"])
        return {"recording_id": fresh.pk, "result": "restored", "archived": False}


def _section_refusal(fresh, reason: str) -> dict:
    """One safe refusal payload for a Section archive request."""
    return {
        "section_id": fresh.pk if fresh is not None else None,
        "result": "refused",
        "reason": reason,
        "archived": bool(fresh is not None and fresh.archived_at is not None),
    }


def archive_section(recording: Recording, section: Section) -> dict:
    """Archive one canonical ACTIVE topic Section (idempotent, no side effects).

    Re-fetches the Recording and the Section under the transaction, then:

    - ``unchanged`` when the Section is already archived (zero DML);
    - ``refused`` with a stable ``reason`` when the Section does not exist,
      does not belong to the passed Recording (``section_not_in_recording``)
      or is not a canonical active topic Section of the active layout
      (``SegmentationError`` codes), or when the parent Recording is itself
      archived (``parent_archived``);
    - ``archived`` after setting ONLY ``section.archived_at``.

    Never touches files, the network, index rows, layout revisions or
    history and never schedules search/embedding synchronization.
    """
    from workflow.services.segmentation import (
        SegmentationError,
        require_active_topic_section,
    )

    with transaction.atomic():
        parent = (
            Recording.objects.select_for_update().filter(pk=recording.pk).first()
        )
        fresh = (
            Section.objects.select_for_update()
            .select_related("transcript")
            .filter(pk=section.pk)
            .first()
        )
        if fresh is None:
            return _section_refusal(None, "section_not_found")
        if fresh.transcript.recording_id != recording.pk:
            return _section_refusal(fresh, "section_not_in_recording")
        if fresh.archived_at is not None:
            return {
                "section_id": fresh.pk,
                "result": "unchanged",
                "archived": True,
            }
        if parent is None or parent.archived_at is not None:
            return _section_refusal(fresh, "parent_archived")
        try:
            require_active_topic_section(fresh)
        except SegmentationError as exc:
            return _section_refusal(fresh, exc.code)
        fresh.archived_at = timezone.now()
        fresh.save(update_fields=["archived_at"])
        return {"section_id": fresh.pk, "result": "archived", "archived": True}


def restore_section(recording: Recording, section: Section) -> dict:
    """Restore one archived topic Section (idempotent, transactional).

    Deliberately PERMISSIVE: it clears an existing ``Section.archived_at``
    even when the Section later became historical (a layout revision was
    superseded) — that is the ONLY mutation; the layout stays untouched
    and the Section stays read-only. Returns ``restored`` when this call
    cleared the marker, ``unchanged`` for an already-active Section (zero
    DML), or ``refused`` when the Section is missing or does not belong to
    the passed Recording. No files, network, index rows, history or sync
    are touched.
    """
    with transaction.atomic():
        fresh = (
            Section.objects.select_for_update()
            .select_related("transcript")
            .filter(pk=section.pk)
            .first()
        )
        if fresh is None:
            return _section_refusal(None, "section_not_found")
        if fresh.transcript.recording_id != recording.pk:
            return _section_refusal(fresh, "section_not_in_recording")
        if fresh.archived_at is None:
            return {"section_id": fresh.pk, "result": "unchanged", "archived": False}
        fresh.archived_at = None
        fresh.save(update_fields=["archived_at"])
        return {"section_id": fresh.pk, "result": "restored", "archived": False}


__all__ = [
    "ARCHIVED_MESSAGE",
    "ArchivedRecordingError",
    "archive_recording",
    "archive_section",
    "ensure_not_archived",
    "is_archived",
    "restore_recording",
    "restore_section",
]
