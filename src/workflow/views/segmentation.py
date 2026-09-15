"""Step 6.1 segmented-version save action (web).

One POST-only, recording/transcript-parent-scoped route that validates
and EXECUTES on the FIRST (and only) POST from the transcript editor:

- the payload is strictly parsed and bounded (a malformed payload, a
  missing/duplicate/forged fingerprint or an unknown field is rejected
  BEFORE any pipeline lock, recovery, network or write);
- the submitted transcript must be the recording's ACTIVE transcript,
  and the shared READ-ONLY semantic validation runs before the lock
  (no lock, no recovery, no write);
- execution goes through ``web_actions.execute_segmentation_save`` (the
  existing schema-ready web path): exclusive pipeline lock, idempotent
  recovery, post-lock re-derivation + read-only segmentation-fingerprint
  comparison, then ``segmentation.save_segmented_version`` (which
  revalidates transactionally). A stale fingerprint is a safe no-op
  (zero DML); lock busy is the existing friendly 409; every error is a
  fixed sanitized message.

The redirect target is ALWAYS the parent transcript page — arbitrary
return URLs are never accepted. Topic titles / transcript text never
enter URLs, logs, or errors.
"""

from __future__ import annotations

from django.contrib import messages as dj_messages
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from workflow.models import Recording
from workflow.services.segmentation import (
    SegmentationError,
    parse_segmentation_payload,
    validate_payload_for_transcript,
)
from workflow.services.web_actions import (
    ActionOutcome,
    execute_segmentation_save,
    segmentation_friendly_message,
)
from workflow.services.pipeline_lock import PipelineBusy
from workflow.views.helpers import conflict_response, get_config, rejection_response


def _recording_or_404(recording_id: str) -> Recording:
    return get_object_or_404(Recording, pk=recording_id)


def _redirect_outcome(request, recording, outcome):
    if isinstance(outcome, ActionOutcome):
        if not outcome.ok:
            dj_messages.error(request, outcome.message)
        elif outcome.result == "state_changed":
            dj_messages.warning(request, outcome.message)
        elif outcome.result == "unchanged":
            dj_messages.info(request, outcome.message)
        else:
            dj_messages.success(request, outcome.message)
        # Always the parent transcript page (the saved transcript is the
        # active one; historical pages are never writable).
        return redirect("recording-transcript", recording.pk)
    return outcome


@require_POST
def action_segmentation_save(request, recording_id):
    """POST-only direct-execution save of one segmented version.

    GET is a 405 (``require_POST``) with zero work. The payload is strictly
    parsed and bounded here; the shared read-only validator checks the
    semantics against the CURRENT database (no lock/recovery/write) — an
    invalid payload is rejected BEFORE the pipeline lock. The opaque
    segmentation fingerprint captured when the editor page was rendered
    is REQUIRED on this one executing POST; the service recomputes it
    under the lock and a mismatch is a safe no-op (never a save against
    a state the user did not see). ``segmentation.save_segmented_version``
    remains the authority on the transactional save.
    """
    recording = _recording_or_404(recording_id)
    config = get_config()
    try:
        payload = parse_segmentation_payload(request.POST)
    except SegmentationError as exc:
        return rejection_response(
            request, segmentation_friendly_message(exc.code), exc.code
        )
    # Parent scope + active-transcript guard BEFORE any lock/write: only
    # the recording's currently ACTIVE transcript is editable.
    transcript = recording.transcripts.filter(is_active=True).first()
    if transcript is None or transcript.pk != payload["transcript_id"]:
        return rejection_response(
            request,
            "Only the active transcript can be trimmed and split — reload the page.",
            "transcript_inactive",
        )
    # Shared READ-ONLY semantic validation (ownership, segment shape,
    # bounds/endpoints/duplicates/titles, existing current layout state).
    # No lock, no recovery, no write.
    try:
        validate_payload_for_transcript(
            recording, transcript, payload, timezone_name=config.timezone
        )
    except SegmentationError as exc:
        return rejection_response(
            request, segmentation_friendly_message(exc.code), exc.code
        )
    try:
        outcome = execute_segmentation_save(
            config,
            recording,
            payload,
            expected_fingerprint=payload["fingerprint"],
        )
    except PipelineBusy as exc:
        return conflict_response(request, exc.holder_pid)
    return _redirect_outcome(request, recording, outcome)
