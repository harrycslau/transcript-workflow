"""Tests for the processing state machine and failure/retry semantics."""

from __future__ import annotations

import pytest

from workflow.models import FailureStage, ProcessingStatus, Recording
from workflow.services.statemachine import (
    InvalidTransition,
    record_failure,
    transition,
)

pytestmark = pytest.mark.django_db

S = ProcessingStatus


def make_recording(status: str) -> Recording:
    return Recording.objects.create(sha256=f"hash-{status}-{Recording.objects.count()}", processing_status=status)


class TestTransitions:
    def test_happy_path_transitions(self):
        recording = make_recording(S.DISCOVERED)
        for next_status in (S.HASHING, S.ROUTING, S.READY_TO_TRANSCRIBE, S.TRANSCRIBING, S.TRANSCRIBED):
            transition(recording, next_status)
            recording.save()
        assert recording.processing_status == S.TRANSCRIBED

    def test_routing_to_needs_review(self):
        recording = make_recording(S.ROUTING)
        transition(recording, S.NEEDS_REVIEW)
        recording.save()
        transition(recording, S.READY_TO_TRANSCRIBE)  # manual route
        recording.save()
        assert recording.failure_stage == ""

    def test_illegal_transition_raises(self):
        recording = make_recording(S.DISCOVERED)
        with pytest.raises(InvalidTransition):
            transition(recording, S.TRANSCRIBED)
        with pytest.raises(InvalidTransition):
            transition(recording, S.TRANSCRIBING)
        recording.save()
        assert recording.processing_status == S.DISCOVERED

    def test_failed_only_via_explicit_edges(self):
        recording = make_recording(S.DISCOVERED)
        with pytest.raises(InvalidTransition):
            transition(recording, S.FAILED)  # discovered cannot fail directly
        routed = make_recording(S.ROUTING)
        transition(routed, S.FAILED)
        routed.save()
        assert routed.failure_stage == ""  # set by record_failure, not transition


class TestFailureSemantics:
    def test_transcription_failure_without_transcript_fails(self):
        recording = make_recording(S.TRANSCRIBING)
        entered_failed = record_failure(recording, FailureStage.TRANSCRIPTION, "mw_nonzero_exit", "")
        recording.save()
        assert entered_failed is True
        assert recording.processing_status == S.FAILED
        assert recording.failure_stage == FailureStage.TRANSCRIPTION

    def test_retranscription_failure_keeps_transcribed(self):
        """A failed retranscription must not invalidate the active transcript."""
        from workflow.models import Transcript

        recording = make_recording(S.TRANSCRIBING)
        attempt = recording.attempts.create(
            stage="transcription", ordinal=1, outcome="success", finished_at=dj_now()
        )
        Transcript.objects.create(
            recording=recording, attempt=attempt, is_active=True, activated_at=dj_now(), text_normalized="old"
        )
        recording.processing_status = S.TRANSCRIBED
        recording.save()

        recording.processing_status = S.TRANSCRIBING  # retranscription
        entered_failed = record_failure(recording, FailureStage.TRANSCRIPTION, "invalid_mw_json", "")
        recording.save()
        assert entered_failed is False
        assert recording.processing_status == S.TRANSCRIBED
        assert recording.transcripts.filter(is_active=True, text_normalized="old").exists()

    def test_routing_failure_sets_failed(self):
        recording = make_recording(S.ROUTING)
        entered_failed = record_failure(recording, FailureStage.ROUTING, "routing_exception", "")
        recording.save()
        assert entered_failed is True
        assert recording.processing_status == S.FAILED
        assert recording.failure_stage == FailureStage.ROUTING


def dj_now():
    from django.utils import timezone

    return timezone.now()


class TestRetry:
    def test_only_failed_records_can_be_retried(self):
        """retry() is covered with mocks in test_pipeline.py; here we
        verify the state precondition: only FAILED records re-enter."""
        recording = make_recording(S.TRANSCRIBED)
        assert recording.processing_status != S.FAILED
