"""Recovery acceptance tests for multilingual summary variants.

All tests run the REAL ``recover_interruptions`` workflow (attempt
closure + stage-aware reconciliation). Recovery must bind to the
attempt's EXACT provenanced scope and never guess: an interrupted
attempt on an old transcript must not touch the new active transcript
or the Recording-level default; forged provenance, provenance-less
legacy attempts and detection attempts must change nothing.
"""

from __future__ import annotations

import pytest

from factories import make_config, make_transcribed_recording
from test_summarize import make_running_attempt
from workflow.models import (
    AttemptStage,
    AttemptOutcome,
    Recording,
    Section,
    SummaryState,
    SummaryVariantState,
    Transcript,
    TranscriptSegment,
)
from workflow.services.pipeline import recover_interruptions


def _make_second_transcript(recording, texts=("new transcript",)):
    """Create and activate a second transcript, superseding the first."""
    from django.utils import timezone as tz

    from workflow.models import ProcessingAttempt

    old = recording.transcripts.filter(is_active=True).first()
    if old is not None:
        old.is_active = False
        old.superseded_at = tz.now()
        old.save(update_fields=["is_active", "superseded_at"])
    attempt = ProcessingAttempt.objects.create(
        recording=recording,
        stage=AttemptStage.TRANSCRIPTION,
        ordinal=ProcessingAttempt.objects.filter(recording=recording).count() + 1,
        outcome=AttemptOutcome.SUCCESS,
        finished_at=tz.now(),
    )
    transcript = Transcript.objects.create(
        recording=recording, attempt=attempt, text_normalized="\n".join(texts)
    )
    TranscriptSegment.objects.bulk_create(
        [
            TranscriptSegment(transcript=transcript, ordinal=i, start_ms=i * 1000, end_ms=(i + 1) * 1000, text=t)
            for i, t in enumerate(texts)
        ]
    )
    Section.objects.create(transcript=transcript, ordinal=0, title="Full recording")
    transcript.is_active = True
    transcript.activated_at = tz.now()
    transcript.save()
    return transcript


def _provenance(transcript, section, resolved, *, is_default=True):
    return {
        "language": {
            "requested": "default",
            "resolved": resolved,
            "source": transcript.language_observed or "",
            "is_default": is_default,
            "source_method": transcript.language_observed_verified_by or "",
            "transcript_id": transcript.pk,
            "section_id": section.pk,
        },
    }


@pytest.mark.django_db
class TestRecoveryExactScope:
    """Interrupted summarization attempts reconcile ONLY their exact scope."""

    def test_old_transcript_attempt_does_not_touch_new_active_transcript(
        self, tmp_path
    ):
        """Scenario: unfinished default attempt on transcript A; transcript
        B becomes active; recovery. Only A's historical variant state may
        change; B and the Recording-level default remain untouched."""
        config = make_config(tmp_path)
        recording, transcript_a, section_a = make_transcribed_recording(["hello"])
        attempt = make_running_attempt(recording)
        attempt.context_json = _provenance(transcript_a, section_a, "en")
        attempt.save()
        # Transcript B becomes active after the attempt was started.
        transcript_b = _make_second_transcript(recording)
        section_b = transcript_b.sections.get(ordinal=0)
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.MISSING

        recoveries = recover_interruptions(config)

        assert recoveries["recovered_attempts"] == 1
        attempt.refresh_from_db()
        assert attempt.outcome == AttemptOutcome.INTERRUPTED
        # Transcript A's variant state marks the failure (historical record).
        vs_a = SummaryVariantState.objects.get(
            transcript=transcript_a, section=section_a, output_language="en"
        )
        assert vs_a.status == SummaryVariantState.VariantStatus.FAILED
        assert vs_a.last_failed_attempt_id == attempt.pk
        # Transcript B: no variant state, nothing failed.
        assert not SummaryVariantState.objects.filter(transcript=transcript_b).exists()
        # Recording-level default state untouched (B's default is missing).
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.MISSING
        assert recording.last_failed_attempt_id is None
        assert recording.resummarization_failed is False

    def test_modern_default_attempt_recovery_marks_failed(self, tmp_path):
        """Positive control: the interrupted attempt's transcript is still
        the active one and its language is still the default → failed."""
        config = make_config(tmp_path)
        recording, transcript, section = make_transcribed_recording(["hello"])
        attempt = make_running_attempt(recording)
        attempt.context_json = _provenance(transcript, section, "en")
        attempt.save()

        recover_interruptions(config)

        vs = SummaryVariantState.objects.get(
            transcript=transcript, section=section, output_language="en"
        )
        assert vs.status == SummaryVariantState.VariantStatus.FAILED
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.FAILED
        assert recording.last_failed_attempt_id == attempt.pk

    def test_recovery_is_idempotent(self, tmp_path):
        config = make_config(tmp_path)
        recording, transcript, section = make_transcribed_recording(["hello"])
        attempt = make_running_attempt(recording)
        attempt.context_json = _provenance(transcript, section, "en")
        attempt.save()

        first = recover_interruptions(config)
        assert first["recovered_attempts"] == 1
        second = recover_interruptions(config)
        assert second["recovered_attempts"] == 0

        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.FAILED
        assert recording.last_failed_attempt_id == attempt.pk
        vs = SummaryVariantState.objects.get(
            transcript=transcript, section=section, output_language="en"
        )
        assert vs.status == SummaryVariantState.VariantStatus.FAILED


@pytest.mark.django_db
class TestRecoveryRejectsBadProvenance:
    def test_forged_transcript_from_another_recording_is_rejected(self, tmp_path):
        config = make_config(tmp_path)
        recording, _transcript, _section = make_transcribed_recording(["hello"])
        other_recording, other_transcript, other_section = make_transcribed_recording(
            ["other"]
        )
        attempt = make_running_attempt(recording)
        # Forge: provenance points at ANOTHER recording's transcript.
        attempt.context_json = {
            "language": {
                "requested": "default",
                "resolved": "en",
                "is_default": True,
                "transcript_id": other_transcript.pk,
                "section_id": other_section.pk,
            },
        }
        attempt.save()

        recover_interruptions(config)

        # Nothing was written anywhere.
        assert not SummaryVariantState.objects.exists()
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.MISSING
        assert recording.last_failed_attempt_id is None
        other_recording.refresh_from_db()
        assert other_recording.summary_status == SummaryState.MISSING

    def test_forged_section_from_another_transcript_is_rejected(self, tmp_path):
        config = make_config(tmp_path)
        recording, transcript, _section = make_transcribed_recording(["hello"])
        _other, other_transcript, other_section = make_transcribed_recording(["other"])
        attempt = make_running_attempt(recording)
        attempt.context_json = _provenance(transcript, other_section, "en")
        attempt.save()

        recover_interruptions(config)

        assert not SummaryVariantState.objects.exists()
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.MISSING
        assert recording.last_failed_attempt_id is None
        assert not SummaryVariantState.objects.filter(
            transcript=other_transcript
        ).exists()

    def test_provenance_less_legacy_attempt_changes_nothing(self, tmp_path):
        """Approved policy: legacy attempts without exact scope provenance
        are conservatively left alone (stable diagnostic, no inference)."""
        config = make_config(tmp_path)
        recording, transcript, section = make_transcribed_recording(["hello"])
        attempt = make_running_attempt(recording)
        attempt.context_json = None
        attempt.save()

        recoveries = recover_interruptions(config)

        assert recoveries["recovered_attempts"] == 1
        assert (
            recoveries["summary_reconciliation"]["legacy_scope_unknown"] == 1
        )
        attempt.refresh_from_db()
        assert attempt.outcome == AttemptOutcome.INTERRUPTED
        # No variant state created; recording-level state untouched.
        assert not SummaryVariantState.objects.exists()
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.MISSING
        assert recording.last_failed_attempt_id is None

    def test_detection_attempt_changes_no_summary_state(self, tmp_path):
        """An interrupted 'original'-variant language-detection attempt is
        NOT a summary-variant event and must never mark the default
        failed or create variant state."""
        config = make_config(tmp_path)
        recording, transcript, section = make_transcribed_recording(["hello"])
        attempt = _make_detection_attempt(recording, transcript, section)

        recoveries = recover_interruptions(config)

        assert recoveries["recovered_attempts"] == 1
        assert recoveries["summary_reconciliation"]["detection_attempts"] == 1
        attempt.refresh_from_db()
        assert attempt.outcome == AttemptOutcome.INTERRUPTED
        assert not SummaryVariantState.objects.exists()
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.MISSING
        assert recording.last_failed_attempt_id is None


def _make_detection_attempt(recording, transcript, section):
    from django.utils import timezone as tz

    from workflow.models import ProcessingAttempt

    return ProcessingAttempt.objects.create(
        recording=recording,
        stage=AttemptStage.SUMMARIZATION,
        ordinal=ProcessingAttempt.objects.filter(recording=recording).count() + 1,
        started_at=tz.now(),
        context_json={
            "language_detection": True,
            "requested": "original",
            "transcript_id": transcript.pk,
            "section_id": section.pk,
        },
    )
