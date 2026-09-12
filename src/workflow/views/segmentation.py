"""Step 6.1 segmented-version save action (web).

One POST-only, recording/transcript-parent-scoped route with the standard
two-step confirmation:

- the FIRST POST (without ``confirmed=1``) strictly parses the bounded
  staged payload, verifies the submitted transcript is the recording's
  ACTIVE transcript, runs the shared READ-ONLY semantic validation (no
  lock, recovery, or write), compares the submitted opaque state
  fingerprint against a freshly computed one (stale => reject before any
  confirmation), and only then renders the autoescaped confirmation page.
- the CONFIRMED POST goes through ``web_actions.execute_segmentation_save``
  (the existing schema-ready web path): exclusive pipeline lock, idempotent
  recovery, post-lock re-derivation + read-only segmentation-fingerprint
  comparison, then ``segmentation.save_segmented_version`` (which
  revalidates transactionally). Stale state is a safe no-op (zero DML);
  lock busy is the existing friendly 409; every error is a fixed
  sanitized message.

The redirect target is ALWAYS the parent transcript page — arbitrary return
URLs are never accepted. Topic titles / transcript text never enter URLs,
logs, or errors.
"""

from __future__ import annotations

from django.contrib import messages as dj_messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from workflow.models import Recording
from workflow.services.segmentation import (
    SegmentationError,
    parse_segmentation_payload,
    range_label,
    segmentation_fingerprint,
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


def _payload_summary(recording: Recording, transcript, payload: dict) -> dict:
    """Bounded, value-free summary fields for the confirmation page.

    The only user content carried is the topic title list; the template
    renders it with normal autoescaping (never raw HTML).
    """
    segment_count = transcript.segments.count()
    start = payload["start"]
    end = payload["end_exclusive"]
    splits = payload["splits"]
    return {
        "range_label": range_label(start, end),
        "segment_count": segment_count,
        "cropped_above": start,
        "cropped_below": segment_count - end,
        "split_count": len(splits),
        "topics": list(payload["titles"]),
    }


def _render_confirmation(request, recording, transcript, payload: dict):
    hidden = {
        "fingerprint": payload["fingerprint"],
        "transcript_id": str(payload["transcript_id"]),
        "start": str(payload["start"]),
        "end_exclusive": str(payload["end_exclusive"]),
    }
    return render(
        request,
        "workflow/segmentation_confirm.html",
        {
            "recording": recording,
            "transcript": transcript,
            "payload": payload,
            "summary": _payload_summary(recording, transcript, payload),
            "hidden": hidden,
        },
    )


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
    """POST-only two-step save of one segmented version.

    GET is a 405 (``require_POST``) with zero work. The payload is strictly
    parsed and bounded here; the shared read-only validator checks the
    semantics against the CURRENT database (no lock/recovery/write), and
    the segmentation service remains the authority on the transactional
    save. The first POST rejects stale fingerprints before showing any
    confirmation; a confirmed POST without a fingerprint is rejected
    BEFORE the pipeline lock.
    """
    recording = _recording_or_404(recording_id)
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
        validate_payload_for_transcript(recording, transcript, payload)
    except SegmentationError as exc:
        return rejection_response(
            request, segmentation_friendly_message(exc.code), exc.code
        )
    if payload["confirmed"]:
        config = get_config()
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
    # First POST: the submitted opaque fingerprint must equal the freshly
    # computed read-only one — a stale page is rejected here, never
    # silently re-synthesized.
    try:
        current_fingerprint = segmentation_fingerprint(recording.pk, transcript)
    except SegmentationError as exc:
        return rejection_response(
            request, segmentation_friendly_message(exc.code), exc.code
        )
    if current_fingerprint != payload["fingerprint"]:
        return rejection_response(
            request, segmentation_friendly_message("stale_state"), "stale_state"
        )
    return _render_confirmation(request, recording, transcript, payload)