"""Mutating processing actions from the web (Step 4).

Design (per the approved plan):

- POST only, CSRF-protected, two-step confirmation: the first POST
  (without ``confirmed=1``) renders a confirmation interstitial that
  states what will run, how long it may take, and what is preserved on
  failure. The second POST (``confirmed=1``) executes.
- Execution acquires the global pipeline lock (busy → 409 page), runs
  recovery, re-derives eligibility, and compares the state fingerprint
  captured when the form was rendered; a mismatch is a safe no-op.
- All business logic lives in the existing pipeline services.
- Responses are POST→redirect→GET with flash messages. Failures render
  stable codes, never tracebacks or secrets.
"""

from __future__ import annotations

from django.contrib import messages as dj_messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from workflow.models import Recording
from workflow.services.web_actions import (
    SUMMARIZE_MODE_LABELS,
    SUMMARIZE_MODE_NOTES,
    ActionOutcome,
    ActionRejected,
    execute_web_action,
    state_fingerprint,
    summarize_mode,
)
from workflow.services.pipeline_lock import PipelineBusy
from workflow.views.helpers import conflict_response, get_config, rejection_response
from workflow.forms import RouteForm

_CONFIRMED = "1"


def _recording_or_404(recording_id: str) -> Recording:
    return get_object_or_404(Recording, pk=recording_id)


def _render_confirmation(
    request,
    recording: Recording,
    *,
    action: str,
    title: str,
    note: str,
    extra_hidden: dict[str, str] | None = None,
    form=None,
):
    fingerprint = (request.POST.get("fingerprint") or "").strip() or state_fingerprint(recording)
    hidden = {"fingerprint": fingerprint}
    if extra_hidden:
        hidden.update(extra_hidden)
    return render(
        request,
        "workflow/action_confirm.html",
        {
            "recording": recording,
            "action": action,
            "title": title,
            "note": note,
            "hidden": hidden,
            "form": form,
        },
    )


def _execute(request, recording: Recording, action: str, **kwargs):
    """Execute under the lock; map busy/rejected outcomes to responses.

    Returns an :class:`ActionOutcome`, or an ``HttpResponse`` rendered
    directly (409 conflict or friendly rejection). Callers must return
    the response when the result is not an ActionOutcome.
    """
    config = get_config()
    try:
        return execute_web_action(config, recording, action, **kwargs)
    except PipelineBusy as exc:
        return conflict_response(request, exc.holder_pid)
    except ActionRejected as exc:
        return rejection_response(request, exc.message, exc.code)


def _redirect_outcome(request, recording: Recording, outcome):
    if isinstance(outcome, ActionOutcome):
        if not outcome.ok:
            dj_messages.error(request, outcome.message)
        elif outcome.result == "state_changed":
            dj_messages.warning(request, outcome.message)
        else:
            dj_messages.success(request, outcome.message)
        return redirect("recording-detail", recording.pk)
    # Already a rendered response (409 conflict page or friendly 400
    # rejection) — return it unchanged.
    return outcome


@require_POST
def action_route(request, recording_id):
    recording = _recording_or_404(recording_id)
    config = get_config()
    form = RouteForm(config=config, data=request.POST)
    if not form.is_valid():
        return rejection_response(request, "Choose a valid routing profile.", "invalid_profile")
    profile_name = form.cleaned_data["profile"]
    if request.POST.get("confirmed") != _CONFIRMED:
        profile = config.macwhisper.profile(profile_name)
        manual_note = (
            " Selects the manual-only profile '{name}' ({model}) and marks the recording "
            "ready to transcribe.".format(name=profile_name, model=profile.model if profile else "")
            if profile is not None and profile.manual_only
            else ""
        )
        note = (
            "Appends a manual routing decision and marks the recording ready to transcribe."
            + manual_note
            + " If the recording already has a transcript, it stays active until a "
            "retranscription with the new profile succeeds. This may take a while."
        )
        return _render_confirmation(
            request,
            recording,
            action="route",
            title=f"Route manually with '{profile_name}'?",
            note=note,
            extra_hidden={"profile": profile_name},
        )
    outcome = _execute(
        request,
        recording,
        "route",
        profile_name=profile_name,
        expected_fingerprint=request.POST.get("fingerprint") or None,
    )
    return _redirect_outcome(request, recording, outcome)


@require_POST
def action_confirm_routing(request, recording_id):
    recording = _recording_or_404(recording_id)
    if request.POST.get("confirmed") != _CONFIRMED:
        return _render_confirmation(
            request,
            recording,
            action="confirm-routing",
            title="Confirm the active routing?",
            note=(
                "Marks the active routing decision as human-verified. Nothing is "
                "retranscribed and no summary changes."
            ),
        )
    outcome = _execute(
        request,
        recording,
        "confirm-routing",
        expected_fingerprint=request.POST.get("fingerprint") or None,
    )
    return _redirect_outcome(request, recording, outcome)


@require_POST
def action_transcribe(request, recording_id):
    recording = _recording_or_404(recording_id)
    if request.POST.get("confirmed") != _CONFIRMED:
        return _render_confirmation(
            request,
            recording,
            action="transcribe",
            title="Start transcription now?",
            note=(
                "Runs MacWhisper on the verified audio source with the routed model. "
                "This can take a long time for long recordings. If a transcript already "
                "exists, it stays active until the retranscription succeeds."
            ),
        )
    outcome = _execute(
        request,
        recording,
        "transcribe",
        expected_fingerprint=request.POST.get("fingerprint") or None,
    )
    return _redirect_outcome(request, recording, outcome)


@require_POST
def action_summarize(request, recording_id):
    recording = _recording_or_404(recording_id)
    mode = summarize_mode(recording)
    requested_mode = (request.POST.get("mode") or "").strip() or None
    if mode is None:
        return rejection_response(
            request,
            "Summarization is not available for this recording in its current state.",
            "ineligible_state",
        )
    if request.POST.get("confirmed") != _CONFIRMED:
        label = SUMMARIZE_MODE_LABELS.get(requested_mode or mode, "Summarize")
        note = SUMMARIZE_MODE_NOTES.get(mode, SUMMARIZE_MODE_NOTES["first"])
        return _render_confirmation(
            request,
            recording,
            action="summarize",
            title=f"{label} — are you sure?",
            note=note,
            extra_hidden={"mode": mode},
        )
    outcome = _execute(
        request,
        recording,
        "summarize",
        requested_mode=requested_mode,
        expected_fingerprint=request.POST.get("fingerprint") or None,
    )
    return _redirect_outcome(request, recording, outcome)


@require_POST
def action_retry(request, recording_id):
    recording = _recording_or_404(recording_id)
    if request.POST.get("confirmed") != _CONFIRMED:
        return _render_confirmation(
            request,
            recording,
            action="retry",
            title="Retry the failed stage?",
            note=(
                "Re-runs the failed pipeline stage (routing, transcription or summarization). "
                "This may take a while and contacts the local services involved."
            ),
        )
    outcome = _execute(
        request,
        recording,
        "retry",
        expected_fingerprint=request.POST.get("fingerprint") or None,
    )
    return _redirect_outcome(request, recording, outcome)
