"""Recording processing state machine.

Legal transitions are explicit; anything else raises
:class:`InvalidTransition`. Failure/retry semantics:

- ``failed`` is entered only from an active processing stage and only
  when no usable transcript exists (see ``record_failure``).
- ``failed`` is left only through an explicit retry (or manual routing
  from a failed *routing* stage).
- ``needs_review`` is a routing outcome, not a failure.
"""

from __future__ import annotations

from workflow.models import FailureStage, ProcessingStatus, Recording

S = ProcessingStatus

# Allowed edges of the processing state machine. Note: file-level
# hashing state lives on AudioSource.discovery_state; the recording's
# `hashing` status is reserved for an in-flight hash on the recording.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    S.DISCOVERED: {S.HASHING, S.ROUTING},
    S.HASHING: {S.ROUTING, S.DISCOVERED},  # back to discovered = interrupted recovery
    S.ROUTING: {S.NEEDS_REVIEW, S.READY_TO_TRANSCRIBE, S.FAILED, S.ROUTING},
    S.NEEDS_REVIEW: {S.READY_TO_TRANSCRIBE, S.ROUTING},  # manual route / re-auto
    S.READY_TO_TRANSCRIBE: {S.TRANSCRIBING, S.ROUTING, S.READY_TO_TRANSCRIBE},  # self = idempotent re-apply
    S.TRANSCRIBING: {S.TRANSCRIBED, S.FAILED, S.READY_TO_TRANSCRIBE},
    S.TRANSCRIBED: {S.TRANSCRIBING, S.READY_TO_TRANSCRIBE},  # pending retranscription
    S.FAILED: {S.ROUTING, S.READY_TO_TRANSCRIBE},  # explicit retry only
}


class InvalidTransition(Exception):
    pass


def transition(recording: Recording, new_status: str) -> Recording:
    """Move ``recording`` to ``new_status`` if the edge is legal."""
    current = recording.processing_status
    if new_status not in ALLOWED_TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"illegal transition {current} -> {new_status}")
    recording.processing_status = new_status
    if new_status != S.FAILED:
        recording.failure_stage = ""
    return recording


def record_failure(recording: Recording, stage: str, error_code: str, message: str, attempt=None) -> bool:
    """Apply failure semantics for a failed attempt in ``stage``.

    A failed retranscription must not invalidate an existing successful
    active transcript: the recording stays ``transcribed`` and an
    explicit, queryable retranscription-failure marker is set
    (``retranscription_failed`` / ``last_failed_attempt``) so review,
    status, and ``brain retry`` can surface and clear it. Ordinary
    ``brain run`` never auto-retries it.

    Returns True when the recording entered ``failed``; False when an
    active transcript kept it ``transcribed``. Callers must save.
    """
    recording.failure_stage = stage
    has_active_transcript = recording.transcripts.filter(is_active=True).exists()
    if stage == FailureStage.TRANSCRIPTION and has_active_transcript:
        recording.processing_status = S.TRANSCRIBED
        recording.failure_stage = ""
        recording.retranscription_failed = True
        if attempt is not None:
            recording.last_failed_attempt = attempt
        return False
    recording.processing_status = S.FAILED
    recording.failure_stage = stage
    return True
