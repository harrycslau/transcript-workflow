"""Tag synchronization between YAML configuration and the database.

Synchronization is an upsert by normalized ``name_key``:

- new configured tags create ``Tag`` rows;
- description changes update rows;
- tags removed from YAML are RETIRED (``is_configured=False``), never
  deleted — historical suggestions and assignments keep their FK;
- a removed tag re-added to YAML reactivates the same row (display
  name and history intact).

``sync_tags`` mutates the database and therefore runs ONLY inside
locked mutating commands (``brain summarize``, ``brain tags --sync``);
``brain tags`` without ``--sync`` is genuinely read-only.
"""

from __future__ import annotations

import sqlite3
import time
from functools import wraps

from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from brainlib.config import AppConfig, tag_name_key
from workflow.models import Tag, TagAssignment, TagDeactivatedBy, TagOrigin
from workflow.services.search_sync import schedule_recording_sync

# ---------------------------------------------------------------------------
# SQLite contention retry policy (Pre-5B stability patch)
# ---------------------------------------------------------------------------
#
# Web tag mutations run WITHOUT the pipeline flock (AGENTS.md), so
# concurrent unlocked requests can legitimately collide on SQLite's
# writer lock or shared-cache read lock. SQLite surfaces those as
# ``sqlite3.OperationalError`` wrapped by Django into
# ``django.db.OperationalError`` whose ``__cause__`` is the original
# ``sqlite3.OperationalError`` carrying ``sqlite_errorcode``. The PRIMARY
# byte of that code is SQLITE_BUSY or SQLITE_LOCKED — extended codes such
# as ``SQLITE_LOCKED_SHAREDCACHE`` (0x106) keep the same primary byte and
# therefore qualify too. We retry ONLY such contention, with a finite
# attempt budget and a short fixed backoff. Anything else re-raises
# immediately; exhausted contention re-raises the last error. Retries
# never cover search-sync work: ``schedule_recording_sync`` stays INSIDE
# the caller's transaction and post-commit sync remains nonfatal under
# ``search_sync``'s own policy.
_TAG_RETRY_ATTEMPTS = 3  # total attempts: 1 initial + 2 retries
_TAG_RETRY_BACKOFF_SECONDS = 0.02  # fixed short delay between attempts
_TAG_RETRY_BUSY_CODES = frozenset(
    {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
)


def _is_retryable_contention(exc: OperationalError) -> bool:
    """True only for SQLite BUSY / LOCKED contention.

    The direct underlying cause must be a ``sqlite3.OperationalError``
    exposing a usable integer ``sqlite_errorcode`` whose primary byte is
    SQLITE_BUSY or SQLITE_LOCKED (extended codes qualify via the primary
    byte). Non-SQLite errors, SQLite errors without a usable integer
    code, and unrelated primary codes all return False.
    """
    cause = exc.__cause__
    if cause is None or not isinstance(cause, sqlite3.OperationalError):
        return False
    code = getattr(cause, "sqlite_errorcode", None)
    if not isinstance(code, int):
        return False
    return (code & 0xFF) in _TAG_RETRY_BUSY_CODES


def _retry_on_sqlite_contention(func):
    """Retry ``func`` on bounded SQLite busy/locked contention.

    ``func`` must open its OWN transaction (``@transaction.atomic``) so
    each attempt runs in a fresh transaction: by the time the exception
    reaches us, Django's atomic block has already rolled the failed
    attempt back, so the next invocation starts from a clean connection.
    The retry wrapper therefore lives OUTSIDE ``@transaction.atomic``.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        for attempt in range(_TAG_RETRY_ATTEMPTS):
            try:
                return func(*args, **kwargs)
            except OperationalError as exc:
                if not _is_retryable_contention(exc):
                    raise
                if attempt == _TAG_RETRY_ATTEMPTS - 1:
                    raise
                time.sleep(_TAG_RETRY_BACKOFF_SECONDS)

    return wrapper


def sync_tags(config: AppConfig) -> dict[str, int]:
    """Synchronize ``Tag`` rows with ``config.tags.allowed``. Idempotent.

    Returns counts: ``created``, ``updated`` (description changes),
    ``retired`` (removed from config), ``reactivated`` (re-added).
    """
    configured: dict[str, str] = {}
    for spec in config.tags.allowed:
        configured[tag_name_key(spec.name)] = spec.description

    with transaction.atomic():
        existing = {tag.name_key: tag for tag in Tag.objects.select_for_update()}
        counts = {"created": 0, "updated": 0, "retired": 0, "reactivated": 0}
        for key, description in configured.items():
            tag = existing.get(key)
            if tag is None:
                display = next(
                    (spec.name for spec in config.tags.allowed if tag_name_key(spec.name) == key), key
                )
                Tag.objects.create(name=display, name_key=key, description=description, is_configured=True)
                counts["created"] += 1
                continue
            changed = False
            if not tag.is_configured:
                tag.is_configured = True
                counts["reactivated"] += 1
                changed = True
            if tag.description != description:
                tag.description = description
                counts["updated"] += 1
                changed = True
            # Display name is preserved from first synchronization;
            # config re-spellings never rename history.
            if changed:
                tag.save()
        for key, tag in existing.items():
            if key not in configured and tag.is_configured:
                tag.is_configured = False
                tag.save()
                counts["retired"] += 1
    return counts


def configured_tags() -> dict[str, Tag]:
    """Configured (non-retired) tags keyed by ``name_key``."""
    return {tag.name_key: tag for tag in Tag.objects.filter(is_configured=True)}


# ---------------------------------------------------------------------------
# Web tag editing (Step 4)
# ---------------------------------------------------------------------------


class TagOperationError(Exception):
    """A web tag edit is not allowed for the current state.

    ``code`` is a stable identifier; ``message`` is friendly and
    sanitized.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _lock_recording(recording_pk: str) -> None:
    """Serialize tag mutations per recording inside the caller's transaction.

    On SQLite ``select_for_update`` is a no-op, but correctness does not
    depend on it: writers serialize anyway and the
    unique(recording, tag) constraint plus idempotent re-select handle
    any residual race. The redundant partial active-unique constraint is
    never relied upon.
    """
    from workflow.models import Recording

    Recording.objects.select_for_update().get(pk=recording_pk)


@_retry_on_sqlite_contention
@transaction.atomic
def add_manual_tag(recording, tag: Tag, *, include_retired: bool = False) -> dict:
    """Assign ``tag`` to ``recording`` as a user-owned manual assignment.

    Semantics by current state:

    - no assignment          -> create active ``manual``;
    - inactive (suppressed or model-deactivated) -> reactivate as
      ``manual`` (a deliberate user act that clears the suppression);
    - active ``suggested``   -> PROMOTE to ``manual``: the user owned
      the decision, so a later re-summarization must never deactivate
      it (``source_summary`` is cleared; the suggestion history on its
      summary versions stays untouched);
    - active ``manual``      -> idempotent no-op;
    - active ``confirmed``   -> idempotent no-op, ``confirmed`` is kept
      (already user-owned; never downgraded to ``manual``).

    Retired tags require an explicit ``include_retired`` opt-in. The
    result dict distinguishes ``created`` / ``promoted`` /
    ``reactivated`` / no-op so the UI can give precise feedback.
    """
    if not tag.is_configured and not include_retired:
        raise TagOperationError(
            "retired_tag",
            "This tag is retired and no longer configured. Tick 'include retired tags' "
            "if you deliberately want to restore it.",
        )
    _lock_recording(recording.pk)
    try:
        assignment, created = TagAssignment.objects.get_or_create(
            recording=recording,
            tag=tag,
            defaults={
                "origin": TagOrigin.MANUAL,
                "is_active": True,
                "source_summary": None,
            },
        )
    except IntegrityError:
        # Known conflict: unique(recording, tag). Re-select and treat
        # as idempotent.
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        created = False
    promoted = False
    reactivated = False
    if created:
        pass  # active manual assignment with clean provenance
    elif not assignment.is_active:
        assignment.is_active = True
        assignment.origin = TagOrigin.MANUAL
        assignment.source_summary = None
        assignment.deactivated_at = None
        assignment.deactivated_by = TagDeactivatedBy.NONE
        assignment.save()
        reactivated = True
    elif assignment.origin == TagOrigin.SUGGESTED:
        # Promote the active model suggestion to a user-owned manual
        # assignment: same single row, provenance cleared, suggestion
        # history preserved.
        assignment.origin = TagOrigin.MANUAL
        assignment.source_summary = None
        assignment.save()
        promoted = True
    # else: already manual or confirmed and active -> idempotent no-op.
    # Step 5A.3: effective tag names are indexed aux text (metadata doc).
    schedule_recording_sync([recording.pk])
    return {
        "assignment": assignment,
        "created": created,
        "promoted": promoted,
        "reactivated": reactivated,
    }


@_retry_on_sqlite_contention
@transaction.atomic
def confirm_suggestion(recording, tag: Tag) -> dict:
    """Confirm a currently suggested tag: origin becomes ``confirmed``.

    Confirmed assignments are user-owned and survive future
    re-summarization exactly like manual ones. The originating summary
    reference is preserved. Idempotent.
    """
    _lock_recording(recording.pk)
    assignment = TagAssignment.objects.filter(recording=recording, tag=tag).first()
    if assignment is None or not assignment.is_active:
        raise TagOperationError(
            "no_active_assignment",
            "Only an active (suggested or manual) tag can be confirmed.",
        )
    already_confirmed = assignment.origin == TagOrigin.CONFIRMED
    if not already_confirmed:
        assignment.origin = TagOrigin.CONFIRMED
        assignment.save()
    return {"assignment": assignment, "already_confirmed": already_confirmed}


@_retry_on_sqlite_contention
@transaction.atomic
def remove_tag(recording, tag: Tag) -> dict:
    """Deactivate the effective assignment as an explicit user removal.

    Sets ``deactivated_by="user"`` — a suppression: future model
    suggestions remain recorded on their summary versions but never
    reactivate the assignment. All SummaryTagSuggestion history is
    preserved. Idempotent for already-inactive rows.
    """
    _lock_recording(recording.pk)
    assignment = TagAssignment.objects.filter(recording=recording, tag=tag).first()
    if assignment is None or not assignment.is_active:
        return {"removed": False, "assignment": assignment}
    assignment.is_active = False
    assignment.deactivated_at = timezone.now()
    assignment.deactivated_by = TagDeactivatedBy.USER
    assignment.save()
    # Step 5A.3: deactivation drops the tag name from the metadata doc's
    # indexed aux text.
    schedule_recording_sync([recording.pk])
    return {"removed": True, "assignment": assignment}
