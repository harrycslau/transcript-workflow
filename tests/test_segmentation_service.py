"""Focused service tests for ``workflow.services.segmentation``.

Proves the Step 6.1 save contract on the CURRENT schema:

- creates one immutable active revision per save (superseding the prior
  active) in one atomic transaction; exact topic partition from crop
  range + sorted split markers (zero splits => zero Sections; N splits
  => N+1 exhaustive contiguous Sections);
- no-op semantics (unchanged payload and the initial full+zero state:
  ZERO DML, no new revision, no callback);
- clear crop creates a full ``[0, count)`` zero-section revision;
- ownership/activity enforcement (recording's ACTIVE transcript only),
  exact-input validation (ints not bools, list/tuple collections, exact
  strings, bounded titles/topic count), boundary/split validation, and
  atomic rollback on a mid-transaction failure;
- retranscription naturally creates no segmented version and copies
  nothing;
- no search/embedding sync callbacks, no summaries/tags copied, the
  fixed ordinal-0 Section is never touched.
"""

from __future__ import annotations

import pytest
from django.utils import timezone as dj_timezone

from workflow.models import Recording, Section, SegmentedVersion, Transcript, TranscriptSegment
from workflow.services.segmentation import (
    MAX_TOPIC_SECTIONS,
    MAX_TOPIC_TITLE_LENGTH,
    SegmentationError,
    save_segmented_version,
)
from factories import make_transcribed_recording

pytestmark = pytest.mark.django_db


def make_transcript(count: int, *, active=True):
    """A transcribed recording with ``count`` contiguous segments."""
    recording, transcript, fixed = make_transcribed_recording(
        [f"segment {i}" for i in range(count)]
    )
    if not active:
        transcript.is_active = False
        transcript.superseded_at = dj_timezone.now()
        transcript.save(update_fields=["is_active", "superseded_at"])
    return recording, transcript, fixed


def save(*args, **kwargs):
    return save_segmented_version(*args, **kwargs)


def active_version(transcript):
    return SegmentedVersion.objects.filter(transcript=transcript, is_active=True).first()


# ---------------------------------------------------------------------------
# Basic creation
# ---------------------------------------------------------------------------


class TestCreate:
    def test_crop_only_creates_zero_section_revision(self):
        recording, transcript, _ = make_transcript(5)
        result = save(
            recording.pk, transcript.pk, 1, 4, [], [],
        )
        assert result.created is True
        assert result.revision == 1
        assert result.superseded_revision is None
        assert result.topic_section_count == 0
        assert result.version_id is not None
        version = SegmentedVersion.objects.get(pk=result.version_id)
        assert version.transcript_id == transcript.pk
        assert version.is_active is True
        assert version.activated_at is not None
        assert version.superseded_at is None
        assert version.start_segment_ordinal == 1
        assert version.end_segment_ordinal_exclusive == 4
        assert version.sections.count() == 0
        # The fixed ordinal-0 section is untouched.
        fixed = transcript.sections.get(ordinal=0, segmented_version__isnull=True)
        assert fixed.title == "Full recording"

    def test_splits_create_exhaustive_partition(self):
        recording, transcript, _ = make_transcript(10)
        result = save(
            recording.pk, transcript.pk, 2, 8, [4, 6], ["A", "B", "C"],
        )
        assert result.created is True
        assert result.topic_section_count == 3
        sections = list(
            Section.objects.filter(segmented_version_id=result.version_id).order_by("ordinal")
        )
        assert [(s.ordinal, s.title, s.start_segment_ordinal,
                 s.end_segment_ordinal_exclusive) for s in sections] == [
            (1, "A", 2, 4),
            (2, "B", 4, 6),
            (3, "C", 6, 8),
        ]

    def test_split_order_is_canonicalized(self):
        recording, transcript, _ = make_transcript(10)
        save(recording.pk, transcript.pk, 0, 10, [6, 4], ["A", "B", "C"])
        sections = list(
            Section.objects.filter(
                segmented_version=active_version(transcript)
            ).order_by("ordinal")
        )
        assert [(s.ordinal, s.start_segment_ordinal,
                 s.end_segment_ordinal_exclusive) for s in sections] == [
            (1, 0, 4),
            (2, 4, 6),
            (3, 6, 10),
        ]

    def test_supersedes_prior_active_and_preserves_it(self):
        recording, transcript, _ = make_transcript(10)
        first = save(recording.pk, transcript.pk, 0, 10, [5], ["Old A", "Old B"])
        v1 = SegmentedVersion.objects.get(pk=first.version_id)
        second = save(recording.pk, transcript.pk, 0, 10, [5, 8], ["New A", "New B", "New C"])
        assert second.revision == 2
        assert second.superseded_revision == 1

        v1.refresh_from_db()
        assert v1.is_active is False
        assert v1.superseded_at is not None
        # Old version's sections are immutable (never mutated).
        old_sections = list(v1.sections.order_by("ordinal"))
        assert [(s.ordinal, s.title, s.start_segment_ordinal,
                 s.end_segment_ordinal_exclusive) for s in old_sections] == [
            (1, "Old A", 0, 5),
            (2, "Old B", 5, 10),
        ]

        v2 = active_version(transcript)
        assert v2.revision == 2
        assert v2.is_active is True
        assert v2.sections.count() == 3

    def test_revision_history_order(self):
        recording, transcript, _ = make_transcript(4)
        save(recording.pk, transcript.pk, 0, 4, [2], ["A", "B"])
        save(recording.pk, transcript.pk, 0, 4, [1, 3], ["A", "B", "C"])
        revisions = list(
            SegmentedVersion.objects.filter(transcript=transcript).values_list(
                "revision", "is_active"
            )
        )
        assert revisions == [(2, True), (1, False)]


# ---------------------------------------------------------------------------
# No-op semantics
# ---------------------------------------------------------------------------


class TestNoOp:
    def test_unchanged_payload_is_zero_dml(self):
        recording, transcript, _ = make_transcript(6)
        save(recording.pk, transcript.pk, 0, 6, [2, 4], ["A", "B", "C"])
        version_count = SegmentedVersion.objects.count()
        section_count = Section.objects.count()

        result = save(recording.pk, transcript.pk, 0, 6, [4, 2], ["A", "B", "C"])
        assert result.created is False
        assert result.version_id is None
        assert result.revision is None
        assert result.topic_section_count == 0
        assert SegmentedVersion.objects.count() == version_count
        assert Section.objects.count() == section_count
        assert active_version(transcript).revision == 1

    def test_initial_full_zero_state_is_a_no_op(self):
        recording, transcript, _ = make_transcript(4)
        assert SegmentedVersion.objects.count() == 0
        result = save(recording.pk, transcript.pk, 0, 4, [], [])
        assert result.created is False
        assert SegmentedVersion.objects.count() == 0
        assert Section.objects.count() == 1  # only the fixed section

    def test_crop_only_no_op_when_crop_only_active(self):
        recording, transcript, _ = make_transcript(5)
        save(recording.pk, transcript.pk, 1, 4, [], [])
        result = save(recording.pk, transcript.pk, 1, 4, [], [])
        assert result.created is False
        assert SegmentedVersion.objects.count() == 1

    def test_clear_crop_creates_full_zero_section_revision(self):
        recording, transcript, _ = make_transcript(10)
        save(recording.pk, transcript.pk, 2, 9, [4, 6], ["A", "B", "C"])
        result = save(recording.pk, transcript.pk, 0, 10, [], [])
        assert result.created is True
        assert result.revision == 2
        assert result.superseded_revision == 1
        assert result.topic_section_count == 0
        version = active_version(transcript)
        assert version.start_segment_ordinal == 0
        assert version.end_segment_ordinal_exclusive == 10
        assert version.sections.count() == 0
        # The prior split version stays readable history.
        v1 = SegmentedVersion.objects.get(transcript=transcript, revision=1)
        assert v1.is_active is False
        assert v1.sections.count() == 3

    def test_clear_crop_no_op_when_already_full(self):
        recording, transcript, _ = make_transcript(5)
        save(recording.pk, transcript.pk, 1, 5, [], [])
        result = save(recording.pk, transcript.pk, 0, 5, [], [])
        assert result.created is True
        assert result.revision == 2
        # A second clear-crop save is a no-op.
        again = save(recording.pk, transcript.pk, 0, 5, [], [])
        assert again.created is False
        assert SegmentedVersion.objects.count() == 2


# ---------------------------------------------------------------------------
# Ownership / activity
# ---------------------------------------------------------------------------


class TestOwnershipAndActivity:
    def test_recording_not_found(self):
        recording, transcript, _ = make_transcript(4)
        with pytest.raises(SegmentationError) as exc:
            save("00000000-0000-0000-0000-000000000000", transcript.pk, 0, 4, [], [])
        assert exc.value.code == "recording_not_found"

    def test_transcript_not_found(self):
        recording, transcript, _ = make_transcript(4)
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, 999999, 0, 4, [], [])
        assert exc.value.code == "transcript_not_found"

    def test_transcript_of_another_recording_rejected(self):
        recording_a, _, _ = make_transcript(4)
        _, transcript_b, _ = make_transcript(4)
        with pytest.raises(SegmentationError) as exc:
            save(recording_a.pk, transcript_b.pk, 0, 4, [], [])
        assert exc.value.code == "transcript_not_found"

    def test_historical_transcript_rejected(self):
        recording, transcript, _ = make_transcript(4, active=False)
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 4, [], [])
        assert exc.value.code == "transcript_not_active"
        assert SegmentedVersion.objects.count() == 0

    def test_retranscription_has_no_segmented_version_and_copies_nothing(self):
        from workflow.models import (
            AttemptOutcome,
            AttemptStage,
            ProcessingAttempt,
        )

        recording, transcript, _ = make_transcript(4)
        save(recording.pk, transcript.pk, 0, 4, [2], ["Old topic", "Old 2"])
        old_version = active_version(transcript)

        # Retranscription on the SAME recording: the old transcript is
        # superseded first, then a new active Transcript is created with
        # its own fresh ordinal-0 section (exactly like the hook).
        transcript.is_active = False
        transcript.superseded_at = dj_timezone.now()
        transcript.save(update_fields=["is_active", "superseded_at"])

        attempt = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS, finished_at=dj_timezone.now(),
        )
        new_transcript = Transcript.objects.create(
            recording=recording, attempt=attempt, is_active=True,
            activated_at=dj_timezone.now(),
        )
        TranscriptSegment.objects.bulk_create(
            [
                TranscriptSegment(transcript=new_transcript, ordinal=i, text=f"s{i}")
                for i in range(6)
            ]
        )
        Section.objects.create(
            transcript=new_transcript, ordinal=0, title="Full recording"
        )

        # The new active transcript has NO segmented version and no topic
        # Sections copied from the old one.
        assert new_transcript.segmented_versions.count() == 0
        assert new_transcript.sections.filter(segmented_version__isnull=False).count() == 0
        # Saving on the new transcript creates its own revision 1.
        result = save(
            recording.pk, new_transcript.pk, 1, 5, [3], ["Fresh", "Fresh 2"]
        )
        assert result.created is True
        assert result.revision == 1
        # The old transcript's version remains attached and readable as
        # history (nothing was ever saved on the old transcript to
        # supersede it; the old transcript itself is now historical).
        old_version.refresh_from_db()
        assert old_version.transcript_id == transcript.pk
        assert old_version.sections.count() == 2
        assert transcript.segmented_versions.count() == 1
        transcript.refresh_from_db()
        assert transcript.is_active is False


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def _base(self):
        return make_transcript(8)

    def test_recording_id_must_be_nonblank_bounded_str(self):
        recording, transcript, _ = self._base()
        for bad in (True, False, 1, 1.0, None, "", "   ", "x" * 37):
            with pytest.raises(SegmentationError) as exc:
                save(bad, transcript.pk, 0, 8, [], [])
            assert exc.value.code == "invalid_input", bad

    def test_transcript_id_must_be_exact_positive_int(self):
        recording, transcript, _ = self._base()
        for bad in (True, False, 0, -1, "1", 1.0, None):
            with pytest.raises(SegmentationError) as exc:
                save(recording.pk, bad, 0, 8, [], [])
            assert exc.value.code == "invalid_input", bad

    def test_range_and_split_markers_must_be_exact_ints(self):
        recording, transcript, _ = self._base()
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, True, 8, [], [])
        assert exc.value.code == "invalid_input"
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, False, [], [])
        assert exc.value.code == "invalid_input"
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, [True], ["A", "B"])
        assert exc.value.code == "invalid_input"

    def test_collections_must_be_list_or_tuple(self):
        recording, transcript, _ = self._base()

        class ListSubclass(list):
            pass

        class TupleSubclass(tuple):
            pass

        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, "46", ["A", "B"])
        assert exc.value.code == "invalid_input"
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, [4], "AB")
        assert exc.value.code == "invalid_input"
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, {4}, ["A", "B"])
        assert exc.value.code == "invalid_input"
        # Subclasses of list/tuple are rejected too (exact type identity).
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, ListSubclass([4]), ["A", "B"])
        assert exc.value.code == "invalid_input"
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, TupleSubclass([4]), ["A", "B"])
        assert exc.value.code == "invalid_input"
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, [], ListSubclass(["A"]))
        assert exc.value.code == "invalid_input"

    def test_oversized_collections_rejected_before_touching_elements(self):
        """A hostile oversized list fails on SIZE before any element is
        typed, stringified, sorted, or hashed."""
        recording, transcript, _ = self._base()

        class Hostile:
            def __str__(self):
                raise AssertionError("hostile element was touched")

            def __eq__(self, other):
                raise AssertionError("hostile element was compared")

            def __hash__(self):
                raise AssertionError("hostile element was hashed")

        # 200 split markers would make 201 sections (> cap): rejected
        # before any element validation (invalid_input would prove touch).
        hostile_markers = [Hostile() for _ in range(MAX_TOPIC_SECTIONS)]
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, hostile_markers, [])
        assert exc.value.code == "too_many_topics"

        # 201 title entries: rejected before any title is touched.
        hostile_titles = [Hostile() for _ in range(MAX_TOPIC_SECTIONS + 1)]
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, [], hostile_titles)
        assert exc.value.code == "too_many_topics"

        # The cap boundary is exact: 199 markers + 200 titles is NOT a
        # size failure (it fails later for other reasons, e.g. range).
        ok_titles = [f"t{i}" for i in range(MAX_TOPIC_SECTIONS)]
        with pytest.raises(SegmentationError) as exc:
            save(
                recording.pk, transcript.pk, 0, 8,
                list(range(1, MAX_TOPIC_SECTIONS)), ok_titles,
            )
        assert exc.value.code != "too_many_topics"

    def test_split_markers_must_be_ints(self):
        recording, transcript, _ = self._base()
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, ["4"], ["A", "B"])
        assert exc.value.code == "invalid_input"
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, [4.0], ["A", "B"])
        assert exc.value.code == "invalid_input"

    def test_title_must_be_exact_str(self):
        recording, transcript, _ = self._base()
        for bad in (None, 42, b"x"):
            with pytest.raises(SegmentationError) as exc:
                save(recording.pk, transcript.pk, 0, 8, [4], [bad, "B"])
            assert exc.value.code == "invalid_input"

    def test_blank_title_rejected_but_exact_text_preserved(self):
        recording, transcript, _ = self._base()
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, [4], ["   ", "B"])
        assert exc.value.code == "title_blank"
        # Nonblank after strip but with surrounding spaces is preserved.
        result = save(recording.pk, transcript.pk, 0, 8, [4], ["  Topic A  ", "B"])
        section = Section.objects.get(
            segmented_version_id=result.version_id, ordinal=1
        )
        assert section.title == "  Topic A  "

    def test_newline_and_control_chars_rejected(self):
        recording, transcript, _ = self._base()
        for bad in ("line\nbreak", "tab\there", "cr\rhere", "ctl\x00x", "del\x7f"):
            with pytest.raises(SegmentationError) as exc:
                save(recording.pk, transcript.pk, 0, 8, [4], [bad, "B"])
            assert exc.value.code == "title_invalid_chars", bad

    def test_title_too_long_rejected(self):
        recording, transcript, _ = self._base()
        too_long = "x" * (MAX_TOPIC_TITLE_LENGTH + 1)
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, [4], [too_long, "B"])
        assert exc.value.code == "title_too_long"
        ok = "x" * MAX_TOPIC_TITLE_LENGTH
        result = save(recording.pk, transcript.pk, 0, 8, [4], [ok, "B"])
        assert result.created is True

    def test_title_count_mismatch(self):
        recording, transcript, _ = self._base()
        # Zero splits must have zero titles.
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, [], ["A"])
        assert exc.value.code == "title_count_mismatch"
        # N splits need exactly N+1 titles.
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, [4], ["A"])
        assert exc.value.code == "title_count_mismatch"
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, [4], ["A", "B", "C"])
        assert exc.value.code == "title_count_mismatch"

    def test_too_many_topics_rejected(self):
        recording, transcript, _ = self._base()
        markers = list(range(1, MAX_TOPIC_SECTIONS + 1))
        titles = [f"t{i}" for i in range(len(markers) + 1)]
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, markers, titles)
        assert exc.value.code == "too_many_topics"
        assert SegmentedVersion.objects.count() == 0

    def test_validation_failure_writes_nothing(self):
        recording, transcript, _ = make_transcript(4)
        for kwargs in (
            dict(start=-1, end_exclusive=4),
            dict(start=0, end_exclusive=0),
            dict(start=4, end_exclusive=8),
            dict(start=0, end_exclusive=4, split_markers=[0],
                 topic_titles=["A", "B"]),
            dict(start=0, end_exclusive=4, split_markers=[4],
                 topic_titles=["A", "B"]),
            dict(start=0, end_exclusive=4, split_markers=[5],
                 topic_titles=["A", "B"]),
            dict(start=0, end_exclusive=4, split_markers=[2, 2],
                 topic_titles=["A", "B", "C"]),
            dict(start=0, end_exclusive=4, split_markers=[2],
                 topic_titles=["A"]),
        ):
            with pytest.raises(SegmentationError):
                save(recording.pk, transcript.pk, **kwargs)
        assert SegmentedVersion.objects.count() == 0
        assert Section.objects.count() == 1  # only the fixed section


# ---------------------------------------------------------------------------
# Range / split / segment-state validation
# ---------------------------------------------------------------------------


class TestRangeAndSegments:
    def _base(self):
        return make_transcript(8)

    def test_range_out_of_bounds(self):
        recording, transcript, _ = self._base()
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, -1, 8, [], [])
        assert exc.value.code == "range_out_of_bounds"
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 9, [], [])
        assert exc.value.code == "range_out_of_bounds"

    def test_empty_range_rejected(self):
        recording, transcript, _ = self._base()
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 3, 3, [], [])
        assert exc.value.code == "empty_range"
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 5, 3, [], [])
        assert exc.value.code == "empty_range"

    def test_split_out_of_range_rejected(self):
        recording, transcript, _ = self._base()
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 2, 6, [1], ["A", "B"])
        assert exc.value.code == "split_out_of_range"
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 2, 6, [7], ["A", "B"])
        assert exc.value.code == "split_out_of_range"

    def test_endpoint_splits_rejected(self):
        recording, transcript, _ = self._base()
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 2, 6, [2], ["A", "B"])
        assert exc.value.code == "split_at_endpoint"
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 2, 6, [6], ["A", "B"])
        assert exc.value.code == "split_at_endpoint"

    def test_duplicate_splits_rejected(self):
        recording, transcript, _ = self._base()
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 8, [3, 3], ["A", "B", "C"])
        assert exc.value.code == "duplicate_split"

    def test_empty_transcript_rejected(self):
        recording = Recording.objects.create(
            sha256=f"empty-{Recording.objects.count()}",
            processing_status="transcribed",
        )
        from workflow.models import AttemptOutcome, AttemptStage, ProcessingAttempt
        attempt = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=1,
            outcome=AttemptOutcome.SUCCESS, finished_at=dj_timezone.now(),
        )
        transcript = Transcript.objects.create(
            recording=recording, attempt=attempt, is_active=True,
            activated_at=dj_timezone.now(),
        )
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 0, [], [])
        assert exc.value.code == "transcript_empty"

    def test_non_contiguous_segments_rejected(self):
        recording, transcript, _ = make_transcript(3)
        # Make ordinals non-contiguous: 0, 1, 3.
        TranscriptSegment.objects.filter(transcript=transcript, ordinal=2).update(
            ordinal=3
        )
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 3, [], [])
        assert exc.value.code == "segments_not_contiguous"
        assert SegmentedVersion.objects.count() == 0

    def test_large_transcript_uses_bounded_aggregate_queries(self):
        """The contiguity proof is ONE COUNT/MIN/MAX aggregate — the full
        ordinal list is never materialized, whatever the segment count."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        recording, transcript, _ = make_transcript(1)
        TranscriptSegment.objects.bulk_create(
            [
                TranscriptSegment(transcript=transcript, ordinal=i, text=f"s{i}")
                for i in range(1, 1500)
            ]
        )
        with CaptureQueriesContext(connection) as ctx:
            result = save(recording.pk, transcript.pk, 10, 1490, [], [])
        assert result.created is True
        assert result.topic_section_count == 0
        version = active_version(transcript)
        assert version.start_segment_ordinal == 10
        assert version.end_segment_ordinal_exclusive == 1490

        # A handful of bounded queries in total (never one per segment).
        assert len(ctx.captured_queries) < 25
        # The ONLY queries touching the segment table are the aggregate
        # (COUNT/MIN/MAX); a full ordinal-materialization SELECT would
        # fail this assertion.
        for query in ctx.captured_queries:
            sql = query["sql"]
            if "workflow_transcriptsegment" in sql:
                assert "COUNT(" in sql and "MIN(" in sql and "MAX(" in sql, sql


# ---------------------------------------------------------------------------
# Atomicity / side-effect freedom
# ---------------------------------------------------------------------------


class TestAtomicityAndSideEffects:
    def test_mid_transaction_failure_rolls_back_supersede(self, monkeypatch):
        recording, transcript, _ = make_transcript(6)
        first = save(recording.pk, transcript.pk, 0, 6, [3], ["Old", "Old 2"])
        v1 = SegmentedVersion.objects.get(pk=first.version_id)

        # Fail ONLY at the new-version create (after the supersede write),
        # proving the whole save rolls back atomically.
        real_using = SegmentedVersion.objects.using

        def patched_using(using="default"):
            manager = real_using(using)

            def create(*args, **kwargs):
                raise RuntimeError("simulated write failure")

            manager.create = create
            return manager

        monkeypatch.setattr(SegmentedVersion.objects, "using", patched_using)
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 6, [2], ["New", "New 2"])
        monkeypatch.undo()

        # Atomic rollback: no new revision, prior active NOT superseded.
        v1.refresh_from_db()
        assert v1.is_active is True
        assert v1.superseded_at is None
        assert SegmentedVersion.objects.count() == 1
        assert Section.objects.count() == 3  # fixed + the two old topics

    def test_unexpected_storage_failure_is_sanitized(self, monkeypatch):
        """Unexpected ORM/runtime failures map to ONE fixed safe category
        with no raw message/SQL/values leak."""
        recording, transcript, _ = make_transcript(4)
        real_using = SegmentedVersion.objects.using

        def patched_using(using="default"):
            manager = real_using(using)

            def boom(*args, **kwargs):
                raise RuntimeError(
                    "raw sqlite near /Users/spy/private/brain.sqlite3 "
                    "INSERT INTO workflow_segmentedversion boom"
                )

            manager.create = boom
            return manager

        monkeypatch.setattr(SegmentedVersion.objects, "using", patched_using)
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 0, 4, [2], ["A", "B"])
        assert exc.value.code == "storage_error"
        message = str(exc.value)
        assert message == "segmentation storage failure"
        assert "/Users/" not in message
        assert "brain.sqlite3" not in message
        assert "INSERT" not in message
        assert "boom" not in message
        assert exc.value.__cause__ is None  # original exception suppressed
        assert SegmentedVersion.objects.count() == 0
        assert Section.objects.count() == 1

    def test_keyboard_interrupt_and_system_exit_are_not_caught(self, monkeypatch):
        recording, transcript, _ = make_transcript(4)
        real_using = SegmentedVersion.objects.using
        current = {"exc": KeyboardInterrupt()}

        def patched_using(using="default"):
            manager = real_using(using)

            def create(*args, **kwargs):
                raise current["exc"]

            manager.create = create
            return manager

        monkeypatch.setattr(SegmentedVersion.objects, "using", patched_using)
        for exc in (KeyboardInterrupt(), SystemExit()):
            current["exc"] = exc
            with pytest.raises(type(exc)):
                save(recording.pk, transcript.pk, 0, 4, [2], ["A", "B"])
            assert SegmentedVersion.objects.count() == 0

    def test_no_search_or_embedding_sync_callback(self, monkeypatch):
        recording, transcript, _ = make_transcript(6)

        def no_sync(*args, **kwargs):
            raise AssertionError("search sync must not be scheduled")

        monkeypatch.setattr(
            "workflow.services.search_sync.schedule_recording_sync", no_sync
        )
        monkeypatch.setattr(
            "workflow.services.embedding_sync.sync_recording_embeddings", no_sync
        )

        def no_on_commit(*args, **kwargs):
            raise AssertionError("no post-commit callback may be registered")

        monkeypatch.setattr("django.db.transaction.on_commit", no_on_commit)

        result = save(recording.pk, transcript.pk, 0, 6, [3], ["A", "B"])
        assert result.created is True
        # No-op save also fires no callback.
        again = save(recording.pk, transcript.pk, 0, 6, [3], ["A", "B"])
        assert again.created is False

    def test_no_summaries_or_tags_copied(self):
        recording, transcript, _ = make_transcript(6)
        from workflow.models import Summary, SummaryVariantState, TagAssignment
        assert Summary.objects.count() == 0
        assert SummaryVariantState.objects.count() == 0
        assert TagAssignment.objects.count() == 0
        save(recording.pk, transcript.pk, 0, 6, [3], ["A", "B"])
        save(recording.pk, transcript.pk, 0, 6, [2, 4], ["C", "D", "E"])
        # The service never creates summaries/variants/tags.
        assert Summary.objects.count() == 0
        assert SummaryVariantState.objects.count() == 0
        assert TagAssignment.objects.count() == 0

    def test_safe_result_counts_only(self):
        recording, transcript, _ = make_transcript(4)
        result = save(recording.pk, transcript.pk, 0, 4, [2], ["A", "B"])
        assert result.version_id is not None
        assert result.revision == 1
        assert isinstance(result.topic_section_count, int)
        # No transcript text / raw data in the result.
        assert "segment" not in str(result)
        assert "text" not in str(result)


# ---------------------------------------------------------------------------
# Fail-closed stored layouts: a lone topic Section is not representable
# (zero splits => zero Sections; N>=1 splits => N+1>=2 Sections), so every
# consumer of the shared canonical reader fails it as layout_invalid.
# ---------------------------------------------------------------------------


class TestSingleSectionLayoutFailClosed:
    def _make_single_section_layout(self, transcript):
        """A version whose ONLY topic Section spans the whole range — the
        unrepresentable one-Section shape the contract forbids."""
        version = SegmentedVersion.objects.create(
            transcript=transcript,
            revision=1,
            start_segment_ordinal=2,
            end_segment_ordinal_exclusive=8,
            is_active=True,
            activated_at=dj_timezone.now(),
        )
        Section.objects.create(
            transcript=transcript,
            segmented_version=version,
            ordinal=1,
            title="Lone section",
            start_segment_ordinal=2,
            end_segment_ordinal_exclusive=8,
        )
        return version

    def test_canonical_validator_rejects_single_section(self):
        from workflow.services.segmentation import canonical_layout_for_transcript

        recording, transcript, _ = make_transcript(10)
        version = self._make_single_section_layout(transcript)
        with pytest.raises(SegmentationError) as exc:
            canonical_layout_for_transcript(version, transcript)
        assert exc.value.code == "layout_invalid"

    def test_fingerprint_rejects_single_section(self):
        from workflow.services.segmentation import segmentation_fingerprint

        recording, transcript, _ = make_transcript(10)
        self._make_single_section_layout(transcript)
        with pytest.raises(SegmentationError) as exc:
            segmentation_fingerprint(recording.pk, transcript)
        assert exc.value.code == "layout_invalid"

    def test_save_over_single_section_fails_closed(self):
        """Saving over a single-Section corrupt active layout must fail
        closed with layout_invalid (never a silent no-op against corrupt
        stored state) and write nothing."""
        recording, transcript, _ = make_transcript(10)
        self._make_single_section_layout(transcript)
        before_versions = SegmentedVersion.objects.count()
        before_sections = Section.objects.count()
        with pytest.raises(SegmentationError) as exc:
            save(recording.pk, transcript.pk, 2, 8, [], [])
        assert exc.value.code == "layout_invalid"
        # Zero DML: no new revision, no supersede, no Section mutation.
        assert SegmentedVersion.objects.count() == before_versions
        assert Section.objects.count() == before_sections
        assert SegmentedVersion.objects.filter(transcript=transcript, is_active=True).count() == 1