"""Mutating processing actions from the web (Step 4).

Design (per the approved plan):

- POST only, CSRF-protected. Every mutating action — the recording-level
  actions (manual route, confirm routing, transcribe, summarize/
  regenerate, retry), the section-summary actions and the segmented-
  version save — executes on the FIRST POST from its page form; no
  confirmation interstitial is rendered anywhere. Each form carries the
  state fingerprint captured at render time, so a stale or duplicate
  submission is still the safe no-op below.
- Every recording-level executing POST must carry exactly ONE opaque
  state fingerprint — the canonical lowercase 64-hex SHA-256 digest
  ``web_actions.state_fingerprint`` renders into the form. Missing,
  empty, duplicate, malformed, oversized or uppercase values are
  rejected with one fixed sanitized friendly 400 BEFORE any execution:
  no pipeline lock, recovery, network or write is ever touched against
  an unvalidated fingerprint (a canonical but stale digest keeps the
  existing under-lock safe no-op).
- Execution acquires the global pipeline lock (busy → 409 page), runs
  recovery, re-derives eligibility, and compares the state fingerprint
  captured when the form was rendered; a mismatch is a safe no-op.
- All business logic lives in the existing pipeline services.
- Responses are POST→redirect→GET with flash messages. Failures render
  stable codes, never tracebacks or secrets.
"""

from __future__ import annotations

import re

from django.contrib import messages as dj_messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from workflow.models import Recording
from workflow.services.web_actions import (
    ActionOutcome,
    ActionRejected,
    execute_web_action,
)
from workflow.services.pipeline_lock import PipelineBusy
from workflow.views.helpers import conflict_response, get_config, rejection_response
from workflow.forms import RouteForm

# Server-owned allowlist of pages an action may return to. Never a raw
# client URL: anything outside this set falls back to recording detail.
RETURN_VIEWS = {
    "detail": "recording-detail",
    "summary": "recording-summary",
}


def _validated_return_view(request) -> str:
    raw = (request.POST.get("return_view") or "").strip()
    return raw if raw in RETURN_VIEWS else "detail"


# Recording-level action fingerprint input contract: exactly ONE opaque
# state fingerprint — the canonical lowercase 64-hex SHA-256 digest
# produced by ``web_actions.state_fingerprint`` at render time. Uppercase
# hex is rejected (the rendered form always carries the lowercase digest).
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}\Z")

# ONE fixed sanitized message (with a stable code) for every rejected
# recording-level fingerprint; never echoes the submitted value.
INVALID_FINGERPRINT_MESSAGE = (
    "The state fingerprint is missing or invalid — reload the page."
)


def _validated_fingerprint(request) -> str | None:
    """Parse the executing POST's recording-level state fingerprint.

    Returns the validated opaque digest, or ``None`` when the submission
    does not carry exactly one lowercase 64-hex ``fingerprint`` value
    (missing, empty, duplicate, malformed, oversized or uppercase).
    Strictly input parsing: no lock, recovery, network, DB read or write
    — callers reject ``None`` with a friendly 400 BEFORE ``_execute``.
    """
    values = request.POST.getlist("fingerprint")
    if len(values) != 1 or not _FINGERPRINT_RE.match(values[0]):
        return None
    return values[0]


def _fingerprint_rejection(request):
    """The single fixed friendly 400 for every rejected fingerprint."""
    return rejection_response(
        request, INVALID_FINGERPRINT_MESSAGE, "invalid_fingerprint"
    )


def _redirect_outcome(
    request,
    recording: Recording,
    outcome,
    *,
    language: str = "default",
    return_language: str | None = None,
    return_view: str = "detail",
):
    if isinstance(outcome, ActionOutcome):
        if not outcome.ok:
            dj_messages.error(request, outcome.message)
        elif outcome.result == "state_changed":
            dj_messages.warning(request, outcome.message)
        else:
            dj_messages.success(request, outcome.message)
        # Return to the page the action originated from (server-owned
        # allowlist token — never an arbitrary client URL), keeping the
        # validated read selector. The return selector is a READ
        # selector (validated against the read-only view-model by the
        # caller); it may differ from the generation selector (e.g. a
        # Finnish tab regenerates via `original`). Unvalidated or
        # unknown return selectors fall back to the generation
        # selector — never arbitrary query injection.
        target = return_language or language
        url_name = RETURN_VIEWS.get(return_view, "recording-detail")
        if target and target != "default":
            from django.urls import reverse

            url = reverse(url_name, args=[recording.pk])
            return redirect(f"{url}?language={target}")
        return redirect(url_name, recording.pk)
    # Already a rendered response (409 conflict page or friendly 400
    # rejection) — return it unchanged.
    return outcome


def _recording_or_404(recording_id: str) -> Recording:
    return get_object_or_404(Recording, pk=recording_id)


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


@require_POST
def action_route(request, recording_id):
    recording = _recording_or_404(recording_id)
    fingerprint = _validated_fingerprint(request)
    if fingerprint is None:
        return _fingerprint_rejection(request)
    config = get_config()
    form = RouteForm(config=config, data=request.POST)
    if not form.is_valid():
        return rejection_response(request, "Choose a valid routing profile.", "invalid_profile")
    profile_name = form.cleaned_data["profile"]
    outcome = _execute(
        request,
        recording,
        "route",
        profile_name=profile_name,
        expected_fingerprint=fingerprint,
    )
    return _redirect_outcome(request, recording, outcome)


@require_POST
def action_confirm_routing(request, recording_id):
    recording = _recording_or_404(recording_id)
    fingerprint = _validated_fingerprint(request)
    if fingerprint is None:
        return _fingerprint_rejection(request)
    outcome = _execute(
        request,
        recording,
        "confirm-routing",
        expected_fingerprint=fingerprint,
    )
    return _redirect_outcome(request, recording, outcome)


@require_POST
def action_transcribe(request, recording_id):
    recording = _recording_or_404(recording_id)
    fingerprint = _validated_fingerprint(request)
    if fingerprint is None:
        return _fingerprint_rejection(request)
    outcome = _execute(
        request,
        recording,
        "transcribe",
        expected_fingerprint=fingerprint,
    )
    return _redirect_outcome(request, recording, outcome)


@require_POST
def action_summarize(request, recording_id):
    recording = _recording_or_404(recording_id)
    fingerprint = _validated_fingerprint(request)
    if fingerprint is None:
        return _fingerprint_rejection(request)
    language = (request.POST.get("language") or "default").strip()
    if language not in ("default", "original", "en", "zh-Hant"):
        return rejection_response(
            request,
            f"'{language}' is not a valid generation target. Only default, "
            "English, Traditional Chinese and Original can be generated.",
            "unsupported_language",
        )
    # Optional READ selector to return to after the action (a concrete
    # tab such as `fi` regenerates via `original`). Validated against
    # the read-only view-model; anything unknown falls back to the
    # generation selector at redirect time.
    return_language = (request.POST.get("return_language") or "").strip() or None
    if return_language is not None:
        from workflow.services.variant_view import build_variant_view

        if build_variant_view(recording, return_language).error:
            return_language = None
    # Server-owned page token: which page the action was initiated from
    # (detail | summary). Missing/invalid/forged values fall back to
    # recording detail; arbitrary client URLs are never accepted.
    return_view = _validated_return_view(request)
    # Cheap pre-lock eligibility probe: the mode is only used to reject
    # an obviously ineligible recording (no active transcript / no
    # derivable action) BEFORE taking the pipeline lock. Execution is
    # authoritative: the service re-derives the mode under the lock and
    # compares it against the submitted mode (a changed state is the
    # fingerprint/mode-guarded safe no-op, never a surprise run).
    from workflow.services.summarize import resolve_output_language
    from workflow.services.web_actions import summarize_mode as _summarize_mode

    transcript = recording.transcripts.filter(is_active=True).first()
    mode = None
    if transcript is not None:
        try:
            output_language = resolve_output_language(transcript, language)
        except Exception:
            output_language = ""
        mode = (
            "first"
            if not output_language
            else _summarize_mode(recording, output_language=output_language)
        )
    requested_mode = (request.POST.get("mode") or "").strip() or None
    if mode is None:
        return rejection_response(
            request,
            "Summarization is not available for this recording in its current state.",
            "ineligible_state",
        )
    outcome = _execute(
        request,
        recording,
        "summarize",
        requested_mode=requested_mode,
        expected_fingerprint=fingerprint,
        language=language,
    )
    return _redirect_outcome(
        request, recording, outcome, language=language,
        return_language=return_language, return_view=return_view,
    )


@require_POST
def action_retry(request, recording_id):
    recording = _recording_or_404(recording_id)
    fingerprint = _validated_fingerprint(request)
    if fingerprint is None:
        return _fingerprint_rejection(request)
    outcome = _execute(
        request,
        recording,
        "retry",
        expected_fingerprint=fingerprint,
    )
    return _redirect_outcome(request, recording, outcome)


def _section_or_404(recording: Recording, section_id: int):
    """Parent-scoped topic-Section lookup: the Section must belong to
    ``recording`` (cross-recording/missing is a 404)."""
    from workflow.models import Section

    section = Section.objects.filter(
        pk=section_id, transcript__recording=recording
    ).select_related("transcript", "segmented_version").first()
    if section is None or section.segmented_version_id is None:
        raise Http404("Section not found")
    return section


def _redirect_section_outcome(
    request, recording: Recording, section, outcome, *, return_language: str | None = None,
    lib_return: str = "",
):
    """Redirect a section action outcome to the section detail page with
    the validated read selector — never an arbitrary return URL. A
    validated library-return token is preserved so the breadcrumb keeps
    the originating Library page/state."""
    if isinstance(outcome, ActionOutcome):
        if not outcome.ok:
            dj_messages.error(request, outcome.message)
        elif outcome.result == "state_changed":
            dj_messages.warning(request, outcome.message)
        else:
            dj_messages.success(request, outcome.message)
        target = return_language or "default"
        parts = []
        if target and target != "default":
            parts.append(f"language={target}")
        if lib_return:
            parts.append(f"lib_return={lib_return}")
        if parts:
            from django.urls import reverse

            url = reverse("section-detail", args=[recording.pk, section.pk])
            return redirect(f"{url}?{'&'.join(parts)}")
        return redirect("section-detail", recording.pk, section.pk)
    return outcome


@require_POST
def action_section_summarize(request, recording_id, section_id):
    """Step 6.2 POST-only section summary action (direct execution).

    The section detail form's FIRST (and only) POST validates and
    executes: the cheap pre-lock guards (generation selector, live
    active-topic-section ownership/canonical-layout validation, and the
    strict fingerprint/mode input contract) reject invalid submissions
    with a friendly 400 BEFORE any pipeline lock, recovery, network or
    write. Execution then runs schema preflight, the global pipeline
    lock, interruption recovery, live-section re-validation, the opaque
    section-fingerprint comparison (stale => safe no-op redirect), mode
    re-derivation, and ``summarize_section_one`` (caller-held lock
    contract). Busy => friendly 409; failures are stable sanitized
    messages; the redirect always targets the section detail page with
    the validated read selector and the validated library-return token.
    GET is a 405 via ``require_POST``.
    """
    config = get_config()
    recording = _recording_or_404(recording_id)
    section = _section_or_404(recording, section_id)
    from workflow.services.segmentation import SegmentationError, require_active_topic_section
    from workflow.services.variant_view import build_variant_view
    from workflow.services.web_actions import (
        SECTION_ACTION_MODES,
        canonical_section_fingerprint,
        execute_section_summarize,
        section_summarize_friendly_message,
    )

    language = (request.POST.get("language") or "default").strip()
    if language not in ("default", "original", "en", "zh-Hant"):
        return rejection_response(
            request,
            section_summarize_friendly_message("unsupported_language", language=language),
            "unsupported_language",
        )
    # Validated Library-return token (Step 6.2a): carried through the
    # execution redirect so the section breadcrumb keeps the originating
    # normal-Library page/state. A forged/invalid token is dropped
    # silently (the redirect then returns to the plain section detail
    # page).
    from workflow.services import library_return

    lib_return = ""
    raw_lib_return = (request.POST.get("lib_return") or "").strip()
    if raw_lib_return:
        if library_return.decode_token(raw_lib_return, config.timezone) is not None:
            lib_return = raw_lib_return
    # Optional READ selector to return to after the action; validated
    # against the read-only section view-model (unknown falls back).
    return_language = (request.POST.get("return_language") or "").strip() or None
    if return_language is not None and build_variant_view(
        recording, return_language, section=section
    ).error:
        return_language = None
    # Live active-topic-section guard BEFORE any lock/work (read-only
    # canonical validation; historical sections are never actionable —
    # a friendly 400, never a hard rejection after a lock was taken).
    # Execution still re-validates the LIVE section UNDER the pipeline
    # lock inside ``execute_section_summarize``: a section that became
    # historical between this guard and the lock is a safe stale no-op
    # (302 redirect with a warning), never a write to read-only history.
    try:
        require_active_topic_section(section)
    except SegmentationError as exc:
        return rejection_response(
            request, section_summarize_friendly_message(exc.code), exc.code
        )
    # Strict executing-POST input contract (BEFORE any pipeline lock,
    # recovery, network or write): exactly ONE canonical 64-hex opaque
    # fingerprint (upper or lower case, normalized to lowercase) and
    # exactly ONE valid section action mode. Missing/duplicate/malformed
    # values are a friendly rejection — never a run against unvalidated
    # state, never a 500.
    fingerprint_values = request.POST.getlist("fingerprint")
    if len(fingerprint_values) != 1:
        return rejection_response(
            request,
            "The state fingerprint is missing or invalid — reload the page.",
            "invalid_fingerprint",
        )
    fingerprint = canonical_section_fingerprint(fingerprint_values[0])
    if fingerprint is None:
        return rejection_response(
            request,
            "The state fingerprint is missing or invalid — reload the page.",
            "invalid_fingerprint",
        )
    mode_values = request.POST.getlist("mode")
    if len(mode_values) != 1:
        return rejection_response(
            request,
            "The submitted action mode is missing or invalid — reload the page.",
            "invalid_mode",
        )
    requested_mode = (mode_values[0] or "").strip().lower()
    if requested_mode not in SECTION_ACTION_MODES:
        return rejection_response(
            request,
            "The submitted action mode is missing or invalid — reload the page.",
            "invalid_mode",
        )
    # Cheap pre-lock eligibility probe: the section was just verified
    # LIVE, so a missing mode is a genuine ineligible state and is
    # rejected here. Execution is authoritative: the service re-derives
    # the mode under the lock and compares it against the submitted mode
    # (a changed state is the fingerprint/mode-guarded safe no-op).
    variant = build_variant_view(recording, language, section=section)
    if variant.action_mode is None:
        return rejection_response(
            request,
            "Summarization is not available for this section in its current state.",
            "ineligible_state",
        )
    try:
        outcome = execute_section_summarize(
            config,
            recording,
            section,
            requested_mode=requested_mode,
            expected_fingerprint=fingerprint,
            language=language,
        )
    except PipelineBusy as exc:
        return conflict_response(request, exc.holder_pid)
    return _redirect_section_outcome(
        request, recording, section, outcome,
        return_language=return_language, lib_return=lib_return,
    )
