"""Focused service tests for the Step 6.2 section-scoped tag services.

Proves on the CURRENT schema:

- recording and section scopes hold INDEPENDENT assignments of the same
  tag; every recording-scoped API keeps its exact behavior and never
  touches section rows;
- section targets are validated through the shared segmentation
  canonical validator (fixed/historical/cross-parent/malformed-layout
  sections are rejected with the stable ``section_not_available``
  category BEFORE any write; historical sections are read-only);
- the complete atomic selection semantics (create/reactivate/remove/
  unchanged no-op) are identical to the recording scope, scoped to one
  Section;
- suggestion confirmation preserves the same-section source_summary
  provenance (never cross-scope); user removal is section-local
  suppression;
- custom tag creation is global/config-compatible with the exact
  collision rules; only the assignment is section-scoped;
- no-op/zero-DML semantics; no pipeline lock; NO recording search sync
  is ever scheduled by section-scoped mutations;
- the recording serialization boundary precedes the authoritative
  active-section validation inside ONE top-level transaction, so a
  layout that becomes historical is rejected with no assignment (the
  validation re-fetches authoritative DB state, and a retry re-runs the
  whole fresh transaction and re-validates);
- the section bulk mutation does NOT nest the decorated recording
  wrapper: exactly ONE retry outside exactly ONE atomic, every retry in
  a fresh top-level transaction.
"""

from __future__ import annotations

import sqlite3

import pytest
from django.db import connection, OperationalError
from django.utils import timezone as dj_timezone

from brainlib.config import tag_name_key
from workflow.models import (
    Section,
    SegmentedVersion,
    Tag,
    TagAssignment,
    TagDeactivatedBy,
    TagOrigin,
)
from workflow.services import tags as tags_service
from workflow.services.segmentation import save_segmented_version
from workflow.services.tags import (
    TagOperationError,
    add_manual_tag_section,
    apply_section_tag_selection,
    confirm_section_suggestion,
    create_custom_tag_and_assign_section,
    remove_section_tag,
)

from factories import make_tag, make_tag_assignment, make_transcribed_recording

pytestmark = pytest.mark.django_db


def split_recording(texts, splits, titles):
    recording, transcript, _fixed = make_transcribed_recording(texts)
    result = save_segmented_version(
        recording.pk, transcript.pk, 0, len(texts), splits, titles
    )
    version = SegmentedVersion.objects.get(pk=result.version_id)
    sections = list(
        Section.objects.filter(segmented_version=version).order_by("ordinal")
    )
    return recording, transcript, sections


def _op(code: int, name: str, msg: str = "database is locked") -> OperationalError:
    """A ``django.db.OperationalError`` whose DIRECT cause is a
    ``sqlite3.OperationalError`` carrying ``sqlite_errorcode`` = ``code``."""
    try:
        raise sqlite3.OperationalError(msg)
    except sqlite3.OperationalError as orig:  # pragma: no branch
        orig.sqlite_errorcode = code
        orig.sqlite_errorname = name
        try:
            raise OperationalError(msg) from orig
        except OperationalError as exc:
            return exc


def split_recording(texts, splits, titles):
    recording, transcript, _fixed = make_transcribed_recording(texts)
    result = save_segmented_version(
        recording.pk, transcript.pk, 0, len(texts), splits, titles
    )
    version = SegmentedVersion.objects.get(pk=result.version_id)
    sections = list(
        Section.objects.filter(segmented_version=version).order_by("ordinal")
    )
    return recording, transcript, sections


def make_section_summary(recording, transcript, section, *, title="Section S", tags=()):
    """A section Summary row + summarization attempt with a suggested
    source_summary, mirroring what summarize_section_one persists."""
    from factories import make_summary_version

    summary = make_summary_version(
        recording, transcript, section, title=title, output_language="en"
    )
    for tag in tags:
        summary.tag_suggestions.get_or_create(tag=tag)
        TagAssignment.objects.create(
            recording=recording,
            section=section,
            tag=tag,
            origin=TagOrigin.SUGGESTED,
            source_summary=summary,
            is_active=True,
        )
    return summary


# ---------------------------------------------------------------------------
# Scope independence
# ---------------------------------------------------------------------------


class TestScopeIndependence:
    def test_recording_and_section_coexist_same_tag(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        section = sections[0]
        tag = make_tag("Shared")
        rec_result = tags_service.add_manual_tag(recording, tag)
        assert rec_result["created"] is True
        sec_result = add_manual_tag_section(section, tag)
        assert sec_result["created"] is True
        assert TagAssignment.objects.filter(tag=tag).count() == 2
        rec_row = TagAssignment.objects.get(recording=recording, section__isnull=True, tag=tag)
        sec_row = TagAssignment.objects.get(section=section, tag=tag)
        assert rec_row.section_id is None
        assert sec_row.section_id == section.pk
        assert sec_row.recording_id == recording.pk

    def test_section_ops_never_touch_recording_rows(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        section = sections[0]
        tag = make_tag("Only")
        make_tag_assignment(recording, tag, origin="manual")  # recording scope
        add_manual_tag_section(section, tag)  # section scope
        # Removing from the SECTION scope leaves the recording row intact.
        result = remove_section_tag(section, tag)
        assert result["removed"] is True
        rec_row = TagAssignment.objects.get(recording=recording, section__isnull=True, tag=tag)
        assert rec_row.is_active is True
        sec_row = TagAssignment.objects.get(section=section, tag=tag)
        assert sec_row.is_active is False
        assert sec_row.deactivated_by == TagDeactivatedBy.USER


# ---------------------------------------------------------------------------
# Target validation (historical sections read-only)
# ---------------------------------------------------------------------------


class TestSectionTargetValidation:
    def test_fixed_section_rejected(self):
        recording, _t, fixed = make_transcribed_recording(["a", "b"])
        tag = make_tag("Work")
        with pytest.raises(TagOperationError) as excinfo:
            add_manual_tag_section(fixed, tag)
        assert excinfo.value.code == "section_not_available"

    def test_historical_superseded_layout_rejected(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        section = sections[0]
        version = section.segmented_version
        version.is_active = False
        version.superseded_at = dj_timezone.now()
        version.save(update_fields=["is_active", "superseded_at"])
        with pytest.raises(TagOperationError) as excinfo:
            add_manual_tag_section(section, make_tag("Work"))
        assert excinfo.value.code == "section_not_available"

    def test_historical_transcript_rejected(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        transcript.is_active = False
        transcript.superseded_at = dj_timezone.now()
        transcript.save(update_fields=["is_active", "superseded_at"])
        with pytest.raises(TagOperationError) as excinfo:
            remove_section_tag(sections[0], make_tag("Work"))
        assert excinfo.value.code == "section_not_available"

    def test_cross_parent_section_rejected(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        active_version = sections[0].segmented_version
        _other_rec, other_transcript, _fixed = make_transcribed_recording(
            ["x", "y", "z"], sha="x" * 64
        )
        cross = Section.objects.create(
            transcript=other_transcript,
            segmented_version=active_version,
            ordinal=3,
            title="Cross",
            start_segment_ordinal=0,
            end_segment_ordinal_exclusive=3,
        )
        with pytest.raises(TagOperationError) as excinfo:
            add_manual_tag_section(cross, make_tag("Work"))
        assert excinfo.value.code == "section_not_available"

    def test_malformed_layout_rejected(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c", "d"], [2], ["A", "B"]
        )
        sections[0].delete()  # corrupt the active layout
        with pytest.raises(TagOperationError) as excinfo:
            add_manual_tag_section(sections[1], make_tag("Work"))
        assert excinfo.value.code == "section_not_available"

    def test_rejection_writes_nothing(self):
        recording, _t, fixed = make_transcribed_recording(["a", "b"])
        tag = make_tag("Work")
        with pytest.raises(TagOperationError):
            add_manual_tag_section(fixed, tag)
        assert not TagAssignment.objects.filter(tag=tag).exists()


# ---------------------------------------------------------------------------
# Manual add / confirm / remove semantics
# ---------------------------------------------------------------------------


class TestSectionMutations:
    def test_add_manual_is_idempotent_no_op(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        section = sections[0]
        tag = make_tag("Work")
        first = add_manual_tag_section(section, tag)
        assert first["created"] is True
        second = add_manual_tag_section(section, tag)
        assert second["created"] is False
        assert second["promoted"] is False
        assert second["reactivated"] is False
        assert TagAssignment.objects.filter(section=section, tag=tag).count() == 1

    def test_add_promotes_suggested_to_manual(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        section = sections[0]
        summary = make_section_summary(
            recording, transcript, section,
            tags=[make_tag("Suggested")],
        )
        suggested = TagAssignment.objects.get(section=section, tag__name_key="suggested")
        assert suggested.origin == TagOrigin.SUGGESTED
        result = add_manual_tag_section(section, suggested.tag)
        assert result["promoted"] is True
        suggested.refresh_from_db()
        assert suggested.origin == TagOrigin.MANUAL
        assert suggested.source_summary_id is None
        # Suggestion history on the summary version is preserved.
        assert summary.tag_suggestions.filter(tag=suggested.tag).exists()

    def test_add_reactivates_suppressed_and_clears_suppression(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        section = sections[0]
        tag = make_tag("Work")
        suppressed = TagAssignment.objects.create(
            recording=recording, section=section, tag=tag,
            origin=TagOrigin.SUGGESTED, is_active=False, deactivated_by=TagDeactivatedBy.USER,
        )
        suppressed.deactivated_at = dj_timezone.now()
        suppressed.save()
        result = add_manual_tag_section(section, tag)
        assert result["reactivated"] is True
        suppressed.refresh_from_db()
        assert suppressed.is_active is True
        assert suppressed.origin == TagOrigin.MANUAL
        assert suppressed.deactivated_by == TagDeactivatedBy.NONE
        assert suppressed.deactivated_at is None

    def test_retired_tag_requires_opt_in(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        retired = make_tag("Old", configured=False)
        with pytest.raises(TagOperationError) as excinfo:
            add_manual_tag_section(sections[0], retired)
        assert excinfo.value.code == "retired_tag"
        assert not TagAssignment.objects.filter(tag=retired).exists()
        result = add_manual_tag_section(sections[0], retired, include_retired=True)
        assert result["created"] is True

    def test_confirm_suggestion_preserves_same_section_provenance(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        section = sections[0]
        summary = make_section_summary(
            recording, transcript, section,
            tags=[make_tag("Suggested")],
        )
        suggested = TagAssignment.objects.get(section=section, tag__name_key="suggested")
        assert suggested.source_summary_id == summary.pk
        result = confirm_section_suggestion(section, suggested.tag)
        assert result["already_confirmed"] is False
        suggested.refresh_from_db()
        assert suggested.origin == TagOrigin.CONFIRMED
        # The originating summary reference is preserved AND belongs to
        # the SAME section.
        assert suggested.source_summary_id == summary.pk
        assert suggested.source_summary.section_id == section.pk
        # Idempotent.
        result2 = confirm_section_suggestion(section, suggested.tag)
        assert result2["already_confirmed"] is True

    def test_confirm_rejects_inactive_or_missing(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        tag = make_tag("Work")
        with pytest.raises(TagOperationError) as excinfo:
            confirm_section_suggestion(sections[0], tag)
        assert excinfo.value.code == "no_active_assignment"

    def test_remove_is_section_local_suppression(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        section = sections[0]
        summary = make_section_summary(
            recording, transcript, section, tags=[make_tag("Suggested")]
        )
        suggested = TagAssignment.objects.get(section=section, tag__name_key="suggested")
        result = remove_section_tag(section, suggested.tag)
        assert result["removed"] is True
        suggested.refresh_from_db()
        assert suggested.is_active is False
        assert suggested.deactivated_by == TagDeactivatedBy.USER
        assert suggested.source_summary_id == summary.pk
        # Idempotent for already-inactive rows.
        result2 = remove_section_tag(section, suggested.tag)
        assert result2["removed"] is False


# ---------------------------------------------------------------------------
# Complete atomic selection (Done semantics)
# ---------------------------------------------------------------------------


class TestSectionSelection:
    def _setup(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        return recording, transcript, sections

    def test_apply_complete_selection_atomically(self):
        recording, transcript, sections = self._setup()
        section = sections[0]
        keep = make_tag("Keep")
        drop = make_tag("Drop")
        add = make_tag("Add")
        # Recording scope rows for the same tags must NOT be affected.
        make_tag_assignment(recording, keep, origin="manual")
        make_tag_assignment(recording, drop, origin="manual")
        # Section scope: keep (manual), drop (suggested active), add (none).
        TagAssignment.objects.create(
            recording=recording, section=section, tag=keep, origin=TagOrigin.MANUAL,
            is_active=True,
        )
        make_section_summary(recording, transcript, section, tags=[drop])

        result = apply_section_tag_selection(
            section, [keep.pk, add.pk], [], new_tag_name=""
        )
        assert result["changed"] is True
        assert result["counts"]["unchanged"] == 1  # keep stays manual
        assert result["counts"]["removed"] == 1    # drop suppressed
        assert result["counts"]["created"] == 1    # add created
        assert TagAssignment.objects.filter(section=section, tag=keep).get().origin == TagOrigin.MANUAL
        dropped = TagAssignment.objects.get(section=section, tag=drop)
        assert dropped.is_active is False
        assert dropped.deactivated_by == TagDeactivatedBy.USER
        assert TagAssignment.objects.get(section=section, tag=add).is_active is True
        # Recording-scoped rows untouched.
        assert TagAssignment.objects.get(recording=recording, section__isnull=True, tag=drop).is_active is True

    def test_unchanged_selection_is_zero_dml(self):
        recording, transcript, sections = self._setup()
        section = sections[0]
        tag = make_tag("Only")
        make_tag_assignment(recording, tag, origin="manual")  # recording scope only
        # Section scope has no rows: selecting nothing is a no-op.
        result = apply_section_tag_selection(section, [], [], new_tag_name="")
        assert result["changed"] is False
        assert TagAssignment.objects.filter(section=section).count() == 0

    def test_invalid_ids_and_sizes_rejected(self):
        recording, transcript, sections = self._setup()
        section = sections[0]
        make_tag("A")
        make_tag("B")
        with pytest.raises(TagOperationError) as excinfo:
            apply_section_tag_selection(section, [True], [], new_tag_name="")
        assert excinfo.value.code == "invalid_tag_selection"
        with pytest.raises(TagOperationError) as excinfo:
            apply_section_tag_selection(section, ["1", "1"], [], new_tag_name="")
        assert excinfo.value.code == "invalid_tag_selection"
        with pytest.raises(TagOperationError) as excinfo:
            apply_section_tag_selection(section, "notalist", [], new_tag_name="")
        assert excinfo.value.code == "invalid_tag_selection"

    def test_new_custom_tag_created_and_section_assigned(self):
        recording, transcript, sections = self._setup()
        section = sections[0]
        result = apply_section_tag_selection(
            section, [], [], new_tag_name="Brand New Section Tag"
        )
        assert result["created_tag"] is True
        tag = result["tag"]
        assert tag.definition_origin == Tag.DefinitionOrigin.CUSTOM
        assert tag.is_configured is True
        assignment = TagAssignment.objects.get(section=section, tag=tag)
        assert assignment.origin == TagOrigin.MANUAL
        assert assignment.recording_id == recording.pk

    def test_custom_collision_rejected_and_rolls_back(self):
        recording, transcript, sections = self._setup()
        section = sections[0]
        make_tag("Existing")
        with pytest.raises(TagOperationError) as excinfo:
            apply_section_tag_selection(section, [], [], new_tag_name="existing")
        assert excinfo.value.code == "duplicate_tag"
        # The whole selection change rolled back.
        assert TagAssignment.objects.filter(section=section).count() == 0


# ---------------------------------------------------------------------------
# Custom create + assign section
# ---------------------------------------------------------------------------


class TestCustomCreateSection:
    def test_creates_global_custom_tag_and_section_assignment(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        section = sections[0]
        result = create_custom_tag_and_assign_section(section, "Section Custom")
        assert result["created_tag"] is True
        assert result["assignment_created"] is True
        tag = result["tag"]
        assert tag.definition_origin == Tag.DefinitionOrigin.CUSTOM
        assert tag.name_key == tag_name_key("Section Custom")
        assignment = TagAssignment.objects.get(section=section, tag=tag)
        assert assignment.origin == TagOrigin.MANUAL
        # The definition is global and reusable: the SAME tag row can be
        # assigned to another section through the manual-add path (a
        # second create with the same name is a collision, exactly like
        # the recording scope).
        second = add_manual_tag_section(sections[1], tag)
        assert second["created"] is True
        assert TagAssignment.objects.filter(section__isnull=False, tag=tag).count() == 2
        with pytest.raises(TagOperationError) as excinfo:
            create_custom_tag_and_assign_section(sections[1], "Section Custom")
        assert excinfo.value.code == "duplicate_tag"

    def test_collision_raises_stable_duplicate(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        make_tag("Clash")
        with pytest.raises(TagOperationError) as excinfo:
            create_custom_tag_and_assign_section(sections[0], "CLASH")
        assert excinfo.value.code == "duplicate_tag"

    def test_invalid_name_rejected(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        with pytest.raises(TagOperationError) as excinfo:
            create_custom_tag_and_assign_section(sections[0], "  ")
        assert excinfo.value.code == "invalid_tag_name"


# ---------------------------------------------------------------------------
# No sync / retry wrapper
# ---------------------------------------------------------------------------


class TestNoSyncAndRetry:
    def test_no_section_mutation_schedules_sync(self, monkeypatch):
        from workflow.services import tags as tags_module

        called: list = []
        monkeypatch.setattr(
            tags_module, "schedule_recording_sync",
            lambda ids: called.append(list(ids)),
        )
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        section = sections[0]
        tag = make_tag("Work")
        add_manual_tag_section(section, tag)
        apply_section_tag_selection(section, [tag.pk], [], new_tag_name="")
        remove_section_tag(section, tag)
        # Confirm without an active assignment raises (still no sync).
        with pytest.raises(TagOperationError) as excinfo:
            confirm_section_suggestion(section, tag)
        assert excinfo.value.code == "no_active_assignment"
        create_custom_tag_and_assign_section(section, "Another")
        assert called == []

    def test_section_functions_carry_the_contention_retry_wrapper(self):
        # The same local SQLite BUSY/LOCKED retry decorator wraps every
        # section-scoped mutation (functools.wraps leaves __wrapped__).
        for fn in (
            add_manual_tag_section,
            confirm_section_suggestion,
            remove_section_tag,
            apply_section_tag_selection,
            create_custom_tag_and_assign_section,
        ):
            assert getattr(fn, "__wrapped__", None) is not None, fn.__name__

    def test_section_selection_no_pipeline_lock(self, monkeypatch):
        # Section tag edits never acquire the pipeline lock.
        def forbid(*a, **k):
            raise AssertionError("pipeline lock must not be acquired")

        monkeypatch.setattr("workflow.services.pipeline.pipeline_lock", forbid)
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        section = sections[0]
        tag = make_tag("Work")
        add_manual_tag_section(section, tag)
        assert TagAssignment.objects.filter(section=section, tag=tag).exists()


# ---------------------------------------------------------------------------
# Finding 2: one retry outside one atomic — the section bulk op never
# nests the decorated recording wrapper and retries in fresh transactions
# ---------------------------------------------------------------------------


class TestSectionBulkRetryContract:
    def test_section_bulk_does_not_call_the_decorated_recording_wrapper(
        self, monkeypatch
    ):
        """The section bulk op calls the ONE undecorated shared
        implementation directly — never the decorated recording-scoped
        wrapper (which would violate the fresh-top-level-transaction
        retry contract)."""
        monkeypatch.setattr(
            tags_service,
            "apply_tag_selection",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("decorated apply_tag_selection must not be called")
            ),
        )
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        section = sections[0]
        tag = make_tag("Bulk")
        result = apply_section_tag_selection(section, [tag.pk], [], new_tag_name="")
        assert result["changed"] is True
        assert TagAssignment.objects.filter(section=section, tag=tag).count() == 1

    @pytest.mark.django_db(transaction=True)
    def test_section_bulk_retry_runs_in_a_fresh_top_level_transaction(
        self, monkeypatch
    ):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        section = sections[0]
        tag = make_tag("FreshBulk")
        real_lock = tags_service._lock_recording
        exc = _op(sqlite3.SQLITE_LOCKED, "SQLITE_LOCKED")
        state = {"n": 0}

        def flaky_lock(pk):
            state["n"] += 1
            if state["n"] == 1:
                raise exc
            return real_lock(pk)

        monkeypatch.setattr(tags_service, "_lock_recording", flaky_lock)
        monkeypatch.setattr(tags_service, "_TAG_RETRY_ATTEMPTS", 3)
        monkeypatch.setattr(tags_service, "_TAG_RETRY_BACKOFF_SECONDS", 0)

        result = apply_section_tag_selection(section, [tag.pk], [], new_tag_name="")
        assert state["n"] == 2  # one contention + one fresh retry
        assert result["changed"] is True
        assert TagAssignment.objects.filter(section=section, tag=tag).count() == 1
        # The retried invocation was NOT in a broken transaction.
        assert connection.in_atomic_block is False
        assert connection.needs_rollback is False


# ---------------------------------------------------------------------------
# Finding 3: serialization boundary BEFORE authoritative validation; a
# layout that becomes historical never receives a section assignment
# ---------------------------------------------------------------------------


class TestHistoricalLayoutRace:
    def test_layout_becomes_historical_before_write_no_assignment(self):
        """The caller's cached Section still points at an 'active' version
        object, but the DB row was superseded. The post-lock validation
        re-fetches authoritative DB state and rejects — no historical
        assignment is ever written."""
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        section = sections[0]
        tag = make_tag("Work")
        # Fresh UPDATE only (bypasses the cached version object's in-memory
        # is_active, which is still True).
        SegmentedVersion.objects.filter(pk=section.segmented_version_id).update(
            is_active=False, superseded_at=dj_timezone.now()
        )
        with pytest.raises(TagOperationError) as excinfo:
            add_manual_tag_section(section, tag)
        assert excinfo.value.code == "section_not_available"
        assert not TagAssignment.objects.filter(section=section, tag=tag).exists()

    @pytest.mark.django_db(transaction=True)
    def test_retry_revalidates_layout_that_became_historical(self, monkeypatch):
        """First attempt fails with contention; between the rolled-back
        attempt and the fresh retry a concurrent writer supersedes the
        layout. The retry re-runs the WHOLE top-level transaction
        (fresh reads), re-validates, and rejects — no assignment."""
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        section = sections[0]
        tag = make_tag("Work")
        real_lock = tags_service._lock_recording
        exc = _op(sqlite3.SQLITE_LOCKED, "SQLITE_LOCKED")
        state = {"n": 0}

        def flaky_lock(pk):
            state["n"] += 1
            if state["n"] == 1:
                raise exc
            return real_lock(pk)

        monkeypatch.setattr(tags_service, "_lock_recording", flaky_lock)
        monkeypatch.setattr(tags_service, "_TAG_RETRY_ATTEMPTS", 3)

        def supersede_then_sleep(delay):
            # Runs OUTSIDE the rolled-back first attempt (the retry
            # wrapper's backoff), so the supersede is committed before
            # the fresh retry starts.
            SegmentedVersion.objects.filter(pk=section.segmented_version_id).update(
                is_active=False, superseded_at=dj_timezone.now()
            )

        monkeypatch.setattr(tags_service.time, "sleep", supersede_then_sleep)

        with pytest.raises(TagOperationError) as excinfo:
            add_manual_tag_section(section, tag)
        assert excinfo.value.code == "section_not_available"
        assert state["n"] == 2
        assert not TagAssignment.objects.filter(section=section, tag=tag).exists()