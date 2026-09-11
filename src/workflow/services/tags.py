"""Tag synchronization between YAML configuration and the database.

Synchronization is an upsert by normalized ``name_key``:

- new configured tags create config-owned ``Tag`` rows
  (``definition_origin=config``);
- description changes update rows;
- when a configured name normalizes to the key of an EXISTING custom
  tag (``definition_origin=custom``), sync PROMOTES that same row to
  config-owned and applies the configured name/description —
  assignments, suggestions and history keep their FK (the row is never
  replaced);
- tags removed from YAML are RETIRED (``is_configured=False``), never
  deleted — historical suggestions and assignments keep their FK;
  retirement applies ONLY to absent config-owned tags, never custom
  tags;
- a removed tag re-added to YAML reactivates the same row (display
  name and history intact).

Custom tags (``definition_origin=custom``) are global reusable
definitions created from the web UI via
:func:`create_custom_tag_and_assign`.

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

from brainlib.config import AppConfig, TagSpec, tag_name_key
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
    ``retired`` (config-owned tags removed from config), ``reactivated``
    (re-added), ``promoted`` (custom tags adopted by config — same row,
    assignments and history preserved).
    """
    configured: dict[str, TagSpec] = {}
    for spec in config.tags.allowed:
        configured[tag_name_key(spec.name)] = spec

    with transaction.atomic():
        existing = {tag.name_key: tag for tag in Tag.objects.select_for_update()}
        counts = {
            "created": 0,
            "updated": 0,
            "retired": 0,
            "reactivated": 0,
            "promoted": 0,
        }
        for key, spec in configured.items():
            tag = existing.get(key)
            if tag is None:
                Tag.objects.create(
                    name=spec.name, name_key=key, description=spec.description,
                    is_configured=True, definition_origin=Tag.DefinitionOrigin.CONFIG,
                )
                counts["created"] += 1
                continue
            changed = False
            if not tag.is_configured:
                tag.is_configured = True
                counts["reactivated"] += 1
                changed = True
            if tag.definition_origin == Tag.DefinitionOrigin.CUSTOM:
                # Custom tag adopted by config: the SAME row becomes
                # config-owned; the configured display name/description
                # apply. Assignments, suggestions and history keep their
                # FK — the row is never replaced.
                tag.definition_origin = Tag.DefinitionOrigin.CONFIG
                tag.name = spec.name
                counts["promoted"] += 1
                changed = True
            if tag.description != spec.description:
                tag.description = spec.description
                counts["updated"] += 1
                changed = True
            # Display name is preserved from first synchronization;
            # config re-spellings never rename history. (A custom tag's
            # name is replaced exactly once, at promotion.)
            if changed:
                tag.save()
        for key, tag in existing.items():
            if (
                key not in configured
                and tag.is_configured
                and tag.definition_origin == Tag.DefinitionOrigin.CONFIG
            ):
                tag.is_configured = False
                tag.save()
                counts["retired"] += 1
    return counts


def configured_tags() -> dict[str, Tag]:
    """Available (non-retired) tags keyed by ``name_key``.

    ``is_configured=True`` is the availability/retired state, so this
    includes both config-owned definitions and web-created custom ones.
    """
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


def _manual_assignment_for(recording, tag: Tag) -> tuple[TagAssignment, bool, bool, bool]:
    """Create/restore the user-owned manual assignment for ``tag``.

    Shared by every web tag mutation so the ownership/suppression
    semantics exist in exactly one place:

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

    Returns ``(assignment, created, promoted, reactivated)`` so callers
    can give precise feedback.
    """
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
    return assignment, created, promoted, reactivated


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
    assignment, created, promoted, reactivated = _manual_assignment_for(recording, tag)
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


def _remove_assignment_for(assignment) -> bool:
    """Deactivate ``assignment`` as an explicit user removal (suppression).

    Shared by the single-mutation :func:`remove_tag` and the bulk
    :func:`apply_tag_selection` so the exact suppression semantics live in
    one place: ``deactivated_by="user"`` — future model suggestions remain
    recorded on their summary versions but never reactivate the row. All
    SummaryTagSuggestion history is preserved. Idempotent for
    already-inactive rows; returns whether the row was deactivated.
    """
    if assignment is None or not assignment.is_active:
        return False
    assignment.is_active = False
    assignment.deactivated_at = timezone.now()
    assignment.deactivated_by = TagDeactivatedBy.USER
    assignment.save()
    return True


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
    removed = _remove_assignment_for(assignment)
    if removed:
        # Step 5A.3: deactivation drops the tag name from the metadata
        # doc's indexed aux text.
        schedule_recording_sync([recording.pk])
    return {"removed": removed, "assignment": assignment}


# ---------------------------------------------------------------------------
# Bulk atomic tag selection (production UX correction)
# ---------------------------------------------------------------------------
#
# The + Add tag modal stages every selection change LOCALLY (checkbox
# toggles in the DOM only) and commits the COMPLETE desired active set
# atomically on Done via :func:`apply_tag_selection` — nothing mutates
# before Done. The service is authoritative under races: it re-locks and
# re-reads the recording and its assignments inside ONE transaction,
# validates the submitted desired-active IDs (bounded exact integers, no
# bools/duplicates, existence + category checked, size-capped), optionally
# creates ONE new custom tag with the exact custom validation/creation/
# collision rules, and applies the ownership semantics below. Exactly one
# recording search sync is scheduled inside the transaction ONLY when the
# effective indexed tag membership changed; an unchanged Done is zero DML
# and zero callback. No pipeline lock — this is a web tag mutation like
# the others.
_TAG_SELECTION_HARD_LIMIT = 512  # absolute cap; no unbounded IN/list
_TAG_SELECTION_TOO_LARGE_MESSAGE = (
    "Too many tags selected. Please narrow the selection and try again."
)


def _tag_selection_limit() -> int:
    """Bounded size cap for ONE bulk selection.

    At most one more than the number of existing tag definitions (config +
    custom + retired — the full pool a user could legitimately pick from,
    plus one slot for the new custom tag), hard-capped at a small limit so
    no unbounded IN query or in-memory list is ever built.
    """
    return min(_TAG_SELECTION_HARD_LIMIT, Tag.objects.count() + 1)


def _coerce_tag_ids(raw_ids, *, limit: int) -> list[int]:
    """Coerce/validate one submitted desired-active ID list.

    ``raw_ids`` must be a list/tuple — the exact bounded container shapes
    Django forms produce. A bare ``str``, ``None`` or any other iterable/
    container is rejected with a stable value-free ``TagOperationError``
    BEFORE any element is consumed, so a hostile direct caller (e.g. an
    infinite generator) can never force unbounded iteration or
    accumulation. Every element must be an exact integer — bool is
    rejected (it is a subclass of int) — or a plain digit string;
    duplicates and non-positive values are rejected. The running count is
    capped DURING coercion: once ``limit`` valid IDs are collected, the
    next element raises ``tag_selection_too_large`` (before that element
    is processed), so an over-limit list is rejected with bounded work and
    no unbounded ``int()`` of huge digit strings.
    """
    if not isinstance(raw_ids, (list, tuple)):
        raise TagOperationError(
            "invalid_tag_selection", "Choose a valid tag selection."
        )
    seen: set[int] = set()
    result: list[int] = []
    for raw in raw_ids:
        if len(result) >= limit:
            raise TagOperationError(
                "tag_selection_too_large", _TAG_SELECTION_TOO_LARGE_MESSAGE
            )
        if isinstance(raw, bool):
            raise TagOperationError(
                "invalid_tag_selection", "Choose a valid tag selection."
            )
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, str) and raw.isdigit():
            value = int(raw)
        else:
            raise TagOperationError(
                "invalid_tag_selection", "Choose a valid tag selection."
            )
        if value < 1 or value in seen:
            raise TagOperationError(
                "invalid_tag_selection", "Choose a valid tag selection."
            )
        seen.add(value)
        result.append(value)
    return result


@_retry_on_sqlite_contention
@transaction.atomic
def apply_tag_selection(
    recording,
    selected_available_ids,
    selected_retired_ids,
    *,
    new_tag_name: str = "",
) -> dict:
    """Apply the COMPLETE desired active tag selection atomically (Done).

    ``selected_available_ids`` / ``selected_retired_ids`` are the desired
    active set: available IDs must reference ``is_configured=True``
    definitions, retired IDs must reference ``is_configured=False``
    definitions (the explicit retired opt-in). Each submitted ID list must
    be a list/tuple (the bounded container shape Django forms produce);
    the combined set is size-capped against the actual tag pool, with the
    cap enforced DURING coercion, and a new custom tag is only created
    when the combined selection already has room for it within that same
    cap (an over-cap selection + custom name is rejected before any
    write). ``new_tag_name`` must be an exact
    ``str`` — only an exact empty/whitespace string means no custom tag,
    while ``None``/bool/any non-str raises a stable ``invalid_tag_name``.
    A non-blank name optionally creates ONE new global custom tag (exact
    custom validation/creation/collision rules) inside an INNER atomic
    savepoint and includes it in the desired active set INSIDE the same
    outer transaction — a validation/collision failure rolls back every
    selection change and never leaves the outer transaction unusable.

    Ownership semantics by current assignment state:

    - already active AND remains selected -> UNCHANGED: an active
      ``suggested`` stays suggested, an active ``confirmed``/``manual``
      preserves origin and provenance — pressing Done never promotes a
      suggestion;
    - no assignment + selected        -> create active ``manual``;
    - selected + inactive             -> reactivate as ``manual`` and
      clear suppression / source summary;
    - active + unselected             -> explicit user-removal suppression
      (``deactivated_by="user"``, exact :func:`remove_tag` semantics);
    - inactive + unselected           -> unchanged.

    Returns safe counts only (``changed``, per-state counts,
    ``created_tag``, the created ``tag`` when any).
    """
    # One bounded limit for the whole selection: derived from the actual
    # tag pool and hard-capped. It caps each submitted list DURING
    # coercion AND the combined available+retired set.
    limit = _tag_selection_limit()
    available_ids = _coerce_tag_ids(selected_available_ids, limit=limit)
    retired_ids = _coerce_tag_ids(selected_retired_ids, limit=limit)
    if len(available_ids) + len(retired_ids) > limit:
        raise TagOperationError(
            "tag_selection_too_large", _TAG_SELECTION_TOO_LARGE_MESSAGE
        )
    # Serialize writers per recording and re-read the row inside the txn.
    _lock_recording(recording.pk)

    # Category-checked tag existence (one bounded IN query).
    all_ids = available_ids + retired_ids
    tags_by_pk: dict[int, Tag] = {}
    if all_ids:
        found = {tag.pk: tag for tag in Tag.objects.filter(pk__in=all_ids)}
        for tag_id in available_ids:
            tag = found.get(tag_id)
            if tag is None or not tag.is_configured:
                raise TagOperationError(
                    "invalid_tag_selection", "Choose a valid tag."
                )
            tags_by_pk[tag_id] = tag
        for tag_id in retired_ids:
            tag = found.get(tag_id)
            if tag is None or tag.is_configured:
                raise TagOperationError(
                    "invalid_tag_selection",
                    "Choose a valid retired tag to restore.",
                )
            tags_by_pk[tag_id] = tag

    # Optional ONE new custom tag: only an EXACT empty/whitespace str means
    # "no custom tag" — None/bool/any non-str is a stable invalid_tag_name,
    # never silently ignored. Non-blank names use the exact custom
    # validation/creation/collision rules inside an INNER atomic savepoint
    # (exactly like the standalone create_custom_tag_and_assign): a
    # concurrent unique-collision IntegrityError rolls back only that
    # savepoint, leaving the outer transaction usable for the re-query,
    # and the stable duplicate_tag error then propagates so the WHOLE
    # selection change rolls back — never a TransactionManagementError or
    # raw leak. The new definition becomes one more desired-active member,
    # so the combined selection must already have room for it WITHIN the
    # same ``limit`` that capped the submitted IDs — otherwise the
    # documented hard cap would be violated by the appended definition and
    # the request is rejected BEFORE any write.
    created_tag = False
    new_tag: Tag | None = None
    if type(new_tag_name) is not str:
        raise TagOperationError("invalid_tag_name", "Enter a tag name as text.")
    if new_tag_name.strip():
        if len(available_ids) + len(retired_ids) >= limit:
            raise TagOperationError(
                "tag_selection_too_large", _TAG_SELECTION_TOO_LARGE_MESSAGE
            )
        name, key = _validate_custom_tag_name(new_tag_name)
        try:
            with transaction.atomic():
                new_tag = Tag.objects.create(
                    name=name, name_key=key, description="",
                    is_configured=True, definition_origin=Tag.DefinitionOrigin.CUSTOM,
                )
                created_tag = True
        except IntegrityError:
            # A concurrent writer created this normalized key first. The
            # inner savepoint already rolled back, so this re-query runs
            # on a usable transaction.
            existing = Tag.objects.filter(name_key=key).first()
            display = existing.name if existing is not None else name
            raise TagOperationError(
                "duplicate_tag",
                f"A tag named '{display}' already exists.",
            ) from None
        tags_by_pk[new_tag.pk] = new_tag
        available_ids = available_ids + [new_tag.pk]

    desired_active = set(available_ids) | set(retired_ids)

    # Authoritative per-recording re-read of every assignment row (the
    # lock above serialized writers; the unique(recording, tag) constraint
    # plus this single read keep the loop deterministic).
    current = {
        assignment.tag_id: assignment
        for assignment in TagAssignment.objects.filter(recording=recording)
    }

    changed = False
    counts = {"created": 0, "reactivated": 0, "removed": 0, "unchanged": 0}
    for tag_id, assignment in current.items():
        if tag_id in desired_active:
            if assignment.is_active:
                # Already active AND stays selected: untouched — suggested
                # stays suggested, confirmed/manual keep origin/provenance.
                counts["unchanged"] += 1
                continue
            # Selected inactive -> reactivate as manual, clearing the
            # suppression / source summary (a deliberate user act).
            assignment.is_active = True
            assignment.origin = TagOrigin.MANUAL
            assignment.source_summary = None
            assignment.deactivated_at = None
            assignment.deactivated_by = TagDeactivatedBy.NONE
            assignment.save()
            changed = True
            counts["reactivated"] += 1
        elif assignment.is_active:
            # Active but unselected -> explicit user-removal suppression.
            if _remove_assignment_for(assignment):
                changed = True
                counts["removed"] += 1
            else:
                counts["unchanged"] += 1
        else:
            # Inactive and unselected -> unchanged.
            counts["unchanged"] += 1

    for tag_id in desired_active:
        if tag_id in current:
            continue
        TagAssignment.objects.create(
            recording=recording,
            tag=tags_by_pk[tag_id],
            origin=TagOrigin.MANUAL,
            source_summary=None,
            is_active=True,
            deactivated_by=TagDeactivatedBy.NONE,
        )
        changed = True
        counts["created"] += 1

    if changed:
        # Step 5A.3: effective tag membership changed (add/reactivate/
        # remove/new custom) — exactly ONE recording sync inside the txn.
        schedule_recording_sync([recording.pk])

    return {
        "changed": changed,
        "counts": counts,
        "created_tag": created_tag,
        "tag": new_tag,
    }


# ---------------------------------------------------------------------------
# Custom tag creation (production integration)
# ---------------------------------------------------------------------------

_TAG_NAME_MAX = Tag._meta.get_field("name").max_length
_TAG_KEY_MAX = Tag._meta.get_field("name_key").max_length


def _validate_custom_tag_name(raw_name) -> tuple[str, str]:
    """Validate a new custom tag name; returns ``(name, normalized_key)``.

    Rejects non-exact-``str`` values (``type(raw_name) is str`` — a str
    subclass is rejected), blank values, control characters (including
    newlines), names over ``Tag.name``'s max length, normalized keys
    over ``Tag.name_key``'s max length, and any collision with an
    EXISTING ``name_key`` — retired rows included. Errors are stable
    friendly ``TagOperationError`` codes; no paths/SQL ever leak.
    """
    import unicodedata

    if type(raw_name) is not str:
        raise TagOperationError("invalid_tag_name", "Enter a tag name as text.")
    name = raw_name.strip()
    if not name:
        raise TagOperationError("invalid_tag_name", "Enter a tag name.")
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        raise TagOperationError(
            "invalid_tag_name",
            "Tag name must not contain control characters or newlines.",
        )
    if len(name) > _TAG_NAME_MAX:
        raise TagOperationError(
            "invalid_tag_name",
            f"Tag name must be at most {_TAG_NAME_MAX} characters.",
        )
    key = tag_name_key(name)
    if len(key) > _TAG_KEY_MAX:
        raise TagOperationError(
            "invalid_tag_name",
            f"Tag name must be at most {_TAG_KEY_MAX} characters.",
        )
    collision = Tag.objects.filter(name_key=key).first()
    if collision is not None:
        raise TagOperationError(
            "duplicate_tag",
            f"A tag named '{collision.name}' already exists.",
        )
    return name, key


@_retry_on_sqlite_contention
@transaction.atomic
def create_custom_tag_and_assign(recording, raw_name: str) -> dict:
    """Create a global reusable custom Tag and assign it to ``recording``.

    Validation (exact ``str``, nonblank, no control/newline characters,
    name and normalized-key length bounds, no normalized-key collision —
    retired rows included) runs before any write. The Tag is created
    with ``definition_origin=custom`` and ``is_configured=True`` and is
    reusable across recordings; the manual assignment uses the exact
    user-owned semantics of :func:`add_manual_tag`. Exactly one
    recording search sync is scheduled inside the successful
    transaction. SQLite has no advisory locks, so a concurrent identical
    request can create the same ``name_key`` between our pre-validation
    and our insert; the DB unique constraint keeps exactly ONE winner
    row and the insert's ``IntegrityError`` is converted to the SAME
    stable ``duplicate_tag`` error (never leaked raw, never silently
    assigning a definition the user did not create). No pipeline lock —
    this is a web tag mutation like the others.
    """
    name, key = _validate_custom_tag_name(raw_name)
    try:
        with transaction.atomic():
            tag = Tag.objects.create(
                name=name, name_key=key, description="",
                is_configured=True, definition_origin=Tag.DefinitionOrigin.CUSTOM,
            )
            created_tag = True
    except IntegrityError:
        # A concurrent writer created this normalized key first. The
        # definition now exists globally, so a second "create" must be
        # told so — never silently assigned, never a leaked IntegrityError.
        existing = Tag.objects.filter(name_key=key).first()
        display = existing.name if existing is not None else name
        raise TagOperationError(
            "duplicate_tag",
            f"A tag named '{display}' already exists.",
        ) from None
    assignment, created, promoted, reactivated = _manual_assignment_for(recording, tag)
    schedule_recording_sync([recording.pk])
    return {
        "tag": tag,
        "created_tag": created_tag,
        "assignment": assignment,
        "assignment_created": created,
        "promoted": promoted,
        "reactivated": reactivated,
    }
