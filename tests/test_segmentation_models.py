"""Model/constraint tests for Step 6.1 segmented versions.

Proves the DB-level contracts of migration 0011 on the CURRENT schema:

- ``SegmentedVersion``: unique revision per transcript, at most one
  active per transcript, lifecycle-shape CHECK (active = activated, no
  supersede; historical = activated + superseded), chronology CHECK
  (activated_at <= superseded_at), nonempty range CHECK;
- ``Section``: the two mutually exclusive shapes (fixed ordinal-0 with
  NULL canonical fields vs topic ordinal >= 1 with non-NULL canonical
  fields and end > start), conditional ordinal uniqueness (fixed unique
  per transcript; topic unique per segmented version, repeatable across
  revisions), PROTECT ownership of topic sections by their version;
- the documented DB limitation that cross-transcript ownership of a
  topic Section cannot be a SQLite CHECK (the service enforces it).
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone as dj_timezone

from workflow.models import Section, SegmentedVersion, Transcript
from factories import make_transcribed_recording

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
        activated_at=dj_timezone.now() if is_active else dj_timezone.now(),
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


def expect_integrity_error(fn):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            fn()


# ---------------------------------------------------------------------------
# SegmentedVersion lifecycle / uniqueness / range
# ---------------------------------------------------------------------------


class TestSegmentedVersionConstraints:
    def test_unique_revision_per_transcript(self):
        _, transcript, _ = make_transcribed_recording(["a", "b"])
        make_version(transcript, revision=1)
        expect_integrity_error(lambda: make_version(transcript, revision=1))

    def test_revision_repeats_across_transcripts(self):
        _, t1, _ = make_transcribed_recording(["a", "b"])
        _, t2, _ = make_transcribed_recording(["c", "d", "e"])
        make_version(t1, revision=1)
        make_version(t2, revision=1)  # OK

    def test_at_most_one_active_per_transcript(self):
        _, transcript, _ = make_transcribed_recording(["a", "b"])
        make_version(transcript, revision=1, is_active=True)
        expect_integrity_error(
            lambda: make_version(transcript, revision=2, is_active=True)
        )

    def test_superseded_allows_new_active(self):
        _, transcript, _ = make_transcribed_recording(["a", "b"])
        first = make_version(transcript, revision=1, is_active=True)
        first.is_active = False
        first.superseded_at = dj_timezone.now()
        first.save(update_fields=["is_active", "superseded_at"])
        make_version(transcript, revision=2, is_active=True)  # OK

    def test_active_requires_activated_at_and_no_superseded_at(self):
        _, transcript, _ = make_transcribed_recording(["a", "b"])
        now = dj_timezone.now()
        expect_integrity_error(
            lambda: SegmentedVersion.objects.create(
                transcript=transcript, revision=1, start_segment_ordinal=0,
                end_segment_ordinal_exclusive=2, is_active=True,
            )
        )
        expect_integrity_error(
            lambda: SegmentedVersion.objects.create(
                transcript=transcript, revision=2, start_segment_ordinal=0,
                end_segment_ordinal_exclusive=2, is_active=True,
                activated_at=now, superseded_at=now,
            )
        )

    def test_historical_requires_both_timestamps(self):
        _, transcript, _ = make_transcribed_recording(["a", "b"])
        now = dj_timezone.now()
        # is_active=False with no superseded_at is not a valid history row.
        expect_integrity_error(
            lambda: SegmentedVersion.objects.create(
                transcript=transcript, revision=1, start_segment_ordinal=0,
                end_segment_ordinal_exclusive=2, is_active=False,
                activated_at=now,
            )
        )
        SegmentedVersion.objects.create(
            transcript=transcript, revision=2, start_segment_ordinal=0,
            end_segment_ordinal_exclusive=2, is_active=False,
            activated_at=now, superseded_at=now,
        )  # OK

    def test_chronology_activated_before_superseded(self):
        _, transcript, _ = make_transcribed_recording(["a", "b"])
        later = dj_timezone.now()
        earlier = later - __import__("datetime").timedelta(seconds=1)
        expect_integrity_error(
            lambda: SegmentedVersion.objects.create(
                transcript=transcript, revision=1, start_segment_ordinal=0,
                end_segment_ordinal_exclusive=2, is_active=False,
                activated_at=later, superseded_at=earlier,
            )
        )

    def test_revision_must_be_positive(self):
        _, transcript, _ = make_transcribed_recording(["a", "b"])
        now = dj_timezone.now()
        expect_integrity_error(
            lambda: SegmentedVersion.objects.create(
                transcript=transcript, revision=0, start_segment_ordinal=0,
                end_segment_ordinal_exclusive=2, is_active=True,
                activated_at=now,
            )
        )
        expect_integrity_error(
            lambda: SegmentedVersion.objects.create(
                transcript=transcript, revision=-1, start_segment_ordinal=0,
                end_segment_ordinal_exclusive=2, is_active=True,
                activated_at=now,
            )
        )
        make_version(transcript, revision=1)  # revision 1 is valid

    def test_range_must_be_nonempty(self):
        _, transcript, _ = make_transcribed_recording(["a", "b"])
        expect_integrity_error(
            lambda: make_version(transcript, revision=1, start=2, end=2)
        )
        expect_integrity_error(
            lambda: make_version(transcript, revision=2, start=3, end=2)
        )

    def test_history_ordering_newest_revision_first(self):
        _, transcript, _ = make_transcribed_recording(["a", "b"])
        v1 = make_version(transcript, revision=1, is_active=True)
        v1.is_active = False
        v1.superseded_at = dj_timezone.now()
        v1.save(update_fields=["is_active", "superseded_at"])
        make_version(transcript, revision=2, is_active=True)
        versions = list(SegmentedVersion.objects.filter(transcript=transcript))
        assert [v.revision for v in versions] == [2, 1]
        assert v1.revision == 1


# ---------------------------------------------------------------------------
# Section shape CHECK and conditional uniqueness
# ---------------------------------------------------------------------------


class TestSectionShapeConstraints:
    def test_fixed_section_shape_ok(self):
        _, transcript, fixed = make_transcribed_recording(["a", "b"])
        assert fixed.segmented_version_id is None
        assert fixed.start_segment_ordinal is None
        assert fixed.end_segment_ordinal_exclusive is None

    def test_topic_section_shape_ok(self):
        _, transcript, _ = make_transcribed_recording(["a", "b", "c"])
        version = make_version(transcript)
        topic = make_topic_section(
            transcript, version, ordinal=1, start=0, end=3, title="T"
        )
        assert topic.segmented_version_id == version.pk
        assert topic.start_segment_ordinal == 0
        assert topic.end_segment_ordinal_exclusive == 3

    def test_half_fixed_half_topic_rejected(self):
        _, transcript, _ = make_transcribed_recording(["a", "b"])
        version = make_version(transcript)
        # Fixed row with canonical fields set.
        expect_integrity_error(
            lambda: Section.objects.create(
                transcript=transcript, ordinal=0, title="x",
                start_segment_ordinal=0, end_segment_ordinal_exclusive=2,
            )
        )
        # Fixed row with a segmented version.
        expect_integrity_error(
            lambda: Section.objects.create(
                transcript=transcript, ordinal=0, title="x",
                segmented_version=version,
            )
        )
        # Topic row with NULL canonical fields.
        expect_integrity_error(
            lambda: Section.objects.create(
                transcript=transcript, ordinal=1, title="x",
                segmented_version=version,
            )
        )
        # Topic row with ordinal 0.
        expect_integrity_error(
            lambda: Section.objects.create(
                transcript=transcript, ordinal=0, title="x",
                segmented_version=version,
                start_segment_ordinal=0, end_segment_ordinal_exclusive=2,
            )
        )
        # Topic row with a non-empty but reversed range.
        expect_integrity_error(
            lambda: Section.objects.create(
                transcript=transcript, ordinal=1, title="x",
                segmented_version=version,
                start_segment_ordinal=3, end_segment_ordinal_exclusive=2,
            )
        )

    def test_fixed_ordinal_unique_per_transcript(self):
        _, transcript, _ = make_transcribed_recording(["a", "b"])
        # The fixture already created the ordinal-0 section; a second one
        # for the same transcript is rejected.
        expect_integrity_error(
            lambda: Section.objects.create(
                transcript=transcript, ordinal=0, title="Duplicate"
            )
        )

    def test_topic_ordinal_unique_per_version(self):
        _, transcript, _ = make_transcribed_recording(["a", "b", "c", "d"])
        version = make_version(transcript, end=4)
        make_topic_section(transcript, version, ordinal=1, start=0, end=2, title="A")
        expect_integrity_error(
            lambda: make_topic_section(
                transcript, version, ordinal=1, start=2, end=4, title="B"
            )
        )

    def test_topic_ordinal_repeats_across_revisions(self):
        _, transcript, _ = make_transcribed_recording(["a", "b", "c", "d"])
        v1 = make_version(transcript, revision=1, is_active=True)
        make_topic_section(transcript, v1, ordinal=1, start=0, end=2, title="A")
        # Supersede v1 before activating v2 (realistic history).
        v1.is_active = False
        v1.superseded_at = dj_timezone.now()
        v1.save(update_fields=["is_active", "superseded_at"])
        v2 = make_version(transcript, revision=2, is_active=True)
        # Same ordinal 1 in a different revision is allowed.
        make_topic_section(transcript, v2, ordinal=1, start=2, end=4, title="B")
        assert Section.objects.filter(segmented_version=v2, ordinal=1).exists()

    def test_fixed_and_topic_ordinal_zero_never_collide(self):
        _, transcript, _ = make_transcribed_recording(["a", "b"])
        version = make_version(transcript)
        # The fixture's fixed ordinal-0 section and a topic section coexist.
        assert Section.objects.filter(transcript=transcript, ordinal=0,
                                      segmented_version__isnull=True).count() == 1
        make_topic_section(transcript, version, ordinal=1, start=0, end=2, title="T")

    def test_version_protects_topic_sections(self):
        _, transcript, _ = make_transcribed_recording(["a", "b"])
        version = make_version(transcript)
        make_topic_section(transcript, version, ordinal=1, start=0, end=2, title="T")
        with pytest.raises(ProtectedError):
            version.delete()

    def test_cross_transcript_ownership_is_db_allowed(self):
        """SQLite cannot CHECK a Section's transcript against its
        version's transcript; the service enforces this instead."""
        _, t1, _ = make_transcribed_recording(["a", "b"])
        _, t2, _ = make_transcribed_recording(["c", "d"])
        version = make_version(t1)
        cross = make_topic_section(t2, version, ordinal=1, start=0, end=2, title="X")
        assert cross.transcript_id == t2.pk
        assert cross.segmented_version_id == version.pk
        assert isinstance(cross, Section)
        assert isinstance(version, SegmentedVersion)
        assert isinstance(t1, Transcript)