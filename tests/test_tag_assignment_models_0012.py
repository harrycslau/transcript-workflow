"""Model/constraint tests for Step 6.2 section-scoped tag assignments
(migration 0012).

Proves the DB-level contracts on the CURRENT schema:

- ``TagAssignment.section``: nullable FK (PROTECT) with the Section-side
  ``tag_assignments`` related name; ``recording`` stays a REQUIRED
  denormalized parent for both scopes;
- the conditional uniques replace the old recording-only ones: one
  assignment row per scope/tag REGARDLESS of active state — recording
  scope unique on ``(recording, tag)`` where ``section IS NULL`` and
  section scope unique on ``(section, tag)`` where ``section IS NOT
  NULL``;
- a recording and its sections (and two sections) hold INDEPENDENT
  assignments of the same tag; the deactivation-state CHECK still
  applies to every scope;
- PROTECT ownership: deleting a Section with section-scoped assignments
  is rejected;
- the documented DB limitation that cross-parent provenance (a Section
  belonging to another transcript/recording) cannot be a SQLite CHECK —
  the services (``tags``/``summarize``) enforce it.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone as dj_timezone

from workflow.models import Recording, Section, SegmentedVersion, TagAssignment, Transcript
from factories import make_tag, make_transcribed_recording

pytestmark = pytest.mark.django_db


def make_version(transcript, *, revision=1, start=0, end=None, is_active=True):
    """Create a SegmentedVersion directly (bypasses the service)."""
    if end is None:
        end = transcript.segments.count()
    return SegmentedVersion.objects.create(
        transcript=transcript,
        revision=revision,
        start_segment_ordinal=start,
        end_segment_ordinal_exclusive=end,
        is_active=is_active,
        activated_at=dj_timezone.now(),
    )


def make_topic_section(transcript, version, *, ordinal=1, start=0, end=1, title="Topic"):
    return Section.objects.create(
        transcript=transcript,
        ordinal=ordinal,
        title=title,
        segmented_version=version,
        start_segment_ordinal=start,
        end_segment_ordinal_exclusive=end,
    )


def make_recording_assignment(recording, tag, **kwargs):
    defaults = dict(origin="manual", is_active=True)
    defaults.update(kwargs)
    return TagAssignment.objects.create(recording=recording, tag=tag, **defaults)


def make_section_assignment(recording, section, tag, **kwargs):
    defaults = dict(origin="manual", is_active=True)
    defaults.update(kwargs)
    return TagAssignment.objects.create(
        recording=recording, section=section, tag=tag, **defaults
    )


def expect_integrity_error(fn):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            fn()


# ---------------------------------------------------------------------------
# Column / FK shape
# ---------------------------------------------------------------------------


class TestTagAssignmentShape:
    def test_section_is_nullable_and_recording_required(self):
        recording, _t, _s = make_transcribed_recording(["a"])
        tag = make_tag("Work")
        assignment = make_recording_assignment(recording, tag)
        assert assignment.section_id is None
        # recording is still required: inserting without it is rejected.
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                TagAssignment.objects.create(tag=tag, origin="manual", is_active=True)

    def test_section_related_name_and_protect(self):
        recording, transcript, _fixed = make_transcribed_recording(["a", "b", "c"])
        version = make_version(transcript, end=3)
        topic = make_topic_section(transcript, version, ordinal=1, start=0, end=3)
        tag = make_tag("TopicOnly")
        make_section_assignment(recording, topic, tag)
        # Reverse related name from Section.
        assert topic.tag_assignments.count() == 1
        # PROTECT: deleting the Section with assignments is rejected.
        with pytest.raises(ProtectedError):
            topic.delete()

    def test_assignments_share_the_same_tag_definition(self):
        recording, _t, _s = make_transcribed_recording(["a"])
        tag = make_tag("Shared")
        make_recording_assignment(recording, tag)
        assert TagAssignment.objects.filter(tag=tag).count() == 1


# ---------------------------------------------------------------------------
# Conditional uniqueness (one row per scope/tag, active state irrelevant)
# ---------------------------------------------------------------------------


class TestConditionalUniqueness:
    def test_recording_scope_unique_recording_tag(self):
        recording, _t, _s = make_transcribed_recording(["a"])
        tag = make_tag("Work")
        make_recording_assignment(recording, tag)
        # A second row for the same (recording, tag) recording scope is
        # rejected EVEN when inactive (one row per scope/tag).
        expect_integrity_error(
            lambda: make_recording_assignment(recording, tag, origin="suggested",
                                              is_active=False, deactivated_by="model")
        )

    def test_section_scope_unique_section_tag(self):
        recording, transcript, _fixed = make_transcribed_recording(["a", "b", "c"])
        version = make_version(transcript, end=3)
        topic = make_topic_section(transcript, version, ordinal=1, start=0, end=3)
        tag = make_tag("Topic")
        make_section_assignment(recording, topic, tag)
        expect_integrity_error(
            lambda: make_section_assignment(recording, topic, tag, origin="suggested")
        )

    def test_recording_and_section_same_tag_coexist(self):
        recording, transcript, _fixed = make_transcribed_recording(["a", "b", "c"])
        version = make_version(transcript, end=3)
        topic = make_topic_section(transcript, version, ordinal=1, start=0, end=3)
        tag = make_tag("Shared")
        make_recording_assignment(recording, tag)
        make_section_assignment(recording, topic, tag)  # independent scope
        assert TagAssignment.objects.filter(tag=tag).count() == 2
        assert TagAssignment.objects.filter(tag=tag, section__isnull=True).count() == 1
        assert TagAssignment.objects.filter(tag=tag, section=topic).count() == 1

    def test_two_sections_same_tag_coexist(self):
        recording, transcript, _fixed = make_transcribed_recording(
            ["a", "b", "c", "d"]
        )
        v1 = make_version(transcript, revision=1, end=4)
        s1 = make_topic_section(transcript, v1, ordinal=1, start=0, end=2, title="A")
        s2 = make_topic_section(transcript, v1, ordinal=2, start=2, end=4, title="B")
        tag = make_tag("Shared")
        make_section_assignment(recording, s1, tag)
        make_section_assignment(recording, s2, tag)
        assert TagAssignment.objects.filter(section__isnull=False, tag=tag).count() == 2

    def test_same_tag_scope_repeats_across_recordings(self):
        rec_a, _t1, _s1 = make_transcribed_recording(["a"], sha="a" * 64)
        rec_b, _t2, _s2 = make_transcribed_recording(["b"], sha="b" * 64)
        tag = make_tag("Work")
        make_recording_assignment(rec_a, tag)
        make_recording_assignment(rec_b, tag)  # OK

    def test_section_scope_is_independent_of_active_state(self):
        recording, transcript, _fixed = make_transcribed_recording(["a", "b"])
        version = make_version(transcript, end=2)
        topic = make_topic_section(transcript, version, ordinal=1, start=0, end=2)
        tag = make_tag("Topic")
        make_section_assignment(recording, topic, tag)
        # A second ACTIVE row for the same (section, tag) is still
        # rejected: one row per scope/tag regardless of active state.
        expect_integrity_error(
            lambda: make_section_assignment(recording, topic, tag, origin="confirmed")
        )


# ---------------------------------------------------------------------------
# Deactivation-state CHECK still applies to both scopes
# ---------------------------------------------------------------------------


class TestDeactivationState:
    def test_active_rows_need_empty_deactivated_by(self):
        recording, _t, _s = make_transcribed_recording(["a"])
        tag = make_tag("Work")
        expect_integrity_error(
            lambda: make_recording_assignment(
                recording, tag, deactivated_by="user"
            )
        )

    def test_inactive_rows_need_an_actor(self):
        recording, transcript, _fixed = make_transcribed_recording(["a", "b"])
        version = make_version(transcript, end=2)
        topic = make_topic_section(transcript, version, ordinal=1, start=0, end=2)
        tag = make_tag("Topic")
        expect_integrity_error(
            lambda: make_section_assignment(
                recording, topic, tag, is_active=False, deactivated_by=""
            )
        )
        assignment = make_section_assignment(
            recording, topic, tag, is_active=False, deactivated_by="user"
        )
        assert assignment.is_active is False
        assert assignment.deactivated_by == "user"


# ---------------------------------------------------------------------------
# Documented DB limitation: cross-parent provenance is service-enforced
# ---------------------------------------------------------------------------


class TestCrossParentDocumentedLimitation:
    def test_cross_parent_section_reference_is_db_allowed(self):
        """SQLite cannot CHECK that the Section's transcript/recording
        matches the assignment's recording; the services enforce it."""
        rec_a, t_a, _ = make_transcribed_recording(["a", "b"], sha="a" * 64)
        _rec_b, t_b, _ = make_transcribed_recording(["c", "d"], sha="b" * 64)
        version = make_version(t_a, end=2)
        cross = make_topic_section(t_b, version, ordinal=1, start=0, end=2, title="X")
        tag = make_tag("Cross")
        assignment = TagAssignment.objects.create(
            recording=rec_a, section=cross, tag=tag, origin="manual", is_active=True
        )
        assert assignment.recording_id == rec_a.pk
        assert assignment.section_id == cross.pk
        assert cross.transcript_id == t_b.pk
        assert isinstance(assignment, TagAssignment)
        assert isinstance(rec_a, Recording)
        assert isinstance(t_a, Transcript)
        assert isinstance(t_b, Transcript)