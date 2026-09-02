"""Web-facing mutating actions (Step 4).

Every web action:

1. acquires the SAME exclusive pipeline lock as CLI mutations (a busy
   lock raises :class:`PipelineBusy`, which the view converts to a 409
   response);
2. runs the idempotent interruption-recovery pass while holding the lock;
3. re-derives eligibility from the CURRENT database state (never trusts
   the rendered form alone);
4. compares a state fingerprint captured when the form was rendered; a
   mismatch is a safe no-op ("state changed") — a stale page or a
   duplicate submission can never re-run work against a state the user
   did not see.

Business logic is delegated to the existing pipeline services; nothing
here duplicates it. All messages are stable and sanitized (no secrets,
no raw exception text, no filesystem paths).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from brainlib.config import AppConfig
from workflow.models import (
    FailureStage,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
    SummaryState,
)
from workflow.services.pipeline_lock import PipelineBusy, pipeline_lock  # noqa: F401 (re-export)

ROUTE_ELIGIBLE_STATUSES = (
    ProcessingStatus.ROUTING,
    ProcessingStatus.NEEDS_REVIEW,
    ProcessingStatus.READY_TO_TRANSCRIBE,
    ProcessingStatus.TRANSCRIBED,
)

# Wording shown on the confirmation interstitial per summarize mode.
SUMMARIZE_MODE_LABELS = {
    "first": "Summarize",
    "retry_summary": "Retry summary",
    "regenerate": "Regenerate summary",
}

SUMMARIZE_MODE_NOTES = {
    "first": (
        "Creates the first summary for this recording. This contacts the "
        "local oMLX endpoint and may take a while for long recordings."
    ),
    "retry_summary": (
        "The previous summarization attempt failed and no summary exists. "
        "This retries it against the local oMLX endpoint and may take a while."
    ),
    "regenerate": (
        "Creates a NEW summary version. The existing summary stays active "
        "unless the replacement succeeds completely. This may take a while."
    ),
}


class ActionRejected(Exception):
    """The action is not allowed for the recording's current state.

    ``code`` is a stable identifier surfaced to the user; ``message`` is
    a friendly, sanitized explanation.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ActionOutcome:
    """Result of an executed web action (already durably applied)."""

    ok: bool
    result: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


def state_fingerprint(recording: Recording) -> str:
    """Stable fingerprint of the state an action form was rendered from.

    Includes the newest attempt id so that ANY completed processing
    attempt (success or failure) invalidates an in-flight form — a
    duplicate submission can never re-run the action against the state
    it was confirmed from.

    Also includes every stable local input that determines language
    resolution (active transcript identity, canonical source language
    and its verifier, the resolved default and Original output
    languages with an explicit unresolved marker): a source-language
    correction does not necessarily create a ProcessingAttempt, so a
    rendered summarize confirmation would otherwise survive a change
    that silently redirects `original`/`default` to a different
    language. Strictly read-only: database SELECTs only — no LLM
    detection, network, subprocess, or writes.
    """
    summary = recording.current_summary()
    last_attempt_id = (
        ProcessingAttempt.objects.filter(recording=recording)
        .order_by("-ordinal", "-pk")
        .values_list("pk", flat=True)
        .first()
    )
    # Include available variant languages for staleness detection
    from workflow.models import SummaryVariantState
    from workflow.services.langresolve import (
        resolve_default_language,
        resolve_output_language,
    )

    active_transcript = recording.transcripts.filter(is_active=True).first()
    variant_languages = []
    language_state = {
        "transcript_id": None,
        "source_language": "",
        "source_verified_by": "",
        "default_output": None,
        # "" means "Original unresolved (source unknown)".
        "original_output": None,
    }
    if active_transcript:
        variant_languages = sorted(
            SummaryVariantState.objects.filter(
                transcript=active_transcript,
                status=SummaryVariantState.VariantStatus.CURRENT,
            ).values_list("output_language", flat=True)
        )
        language_state = {
            "transcript_id": active_transcript.pk,
            "source_language": active_transcript.language_observed or "",
            "source_verified_by": active_transcript.language_observed_verified_by or "",
            "default_output": resolve_default_language(active_transcript),
            "original_output": resolve_output_language(active_transcript, "original"),
        }
    return json.dumps(
        {
            "status": recording.processing_status,
            "summary_status": recording.summary_status,
            "retranscription_failed": recording.retranscription_failed,
            "resummarization_failed": recording.resummarization_failed,
            "summary_ordinal": summary.ordinal if summary is not None else None,
            "last_attempt_id": last_attempt_id,
            "variant_languages": variant_languages,
            "language_state": language_state,
        },
        sort_keys=True,
    )


def summarize_mode(recording: Recording, *, output_language: str = "") -> str | None:
    """Derive the summarization action available right now, or None.

    If ``output_language`` is specified, checks that variant. Otherwise
    checks the default language variant.

    - ``first``: transcribed with an active transcript, never attempted.
    - ``retry_summary``: the one automatic attempt failed; explicit
      retry only (never automatic).
    - ``regenerate``: a current summary exists; the user explicitly asks
      for a new version (the current one survives a failed attempt).
    """
    if recording.processing_status != ProcessingStatus.TRANSCRIBED:
        return None
    if not recording.transcripts.filter(is_active=True).exists():
        return None
    transcript = recording.transcripts.filter(is_active=True).first()
    section = transcript.sections.filter(ordinal=0).first() if transcript else None
    if section is None:
        return None
    if output_language:
        # Check specific variant
        from workflow.services.langresolve import resolve_default_language
        from workflow.models import SummaryVariantState
        vs = SummaryVariantState.objects.filter(
            transcript=transcript, section=section, output_language=output_language,
        ).first()
        if vs and vs.status == SummaryVariantState.VariantStatus.CURRENT:
            return "regenerate"
        if vs and vs.status == SummaryVariantState.VariantStatus.FAILED:
            return "retry_summary"
        if vs is not None and vs.status == SummaryVariantState.VariantStatus.MISSING:
            return "first"
        # No variant-state row (or missing): for the currently derived
        # DEFAULT language the recording-level tuple is authoritative
        # (pre-variant data keeps its state there).
        if output_language == resolve_default_language(transcript):
            return summarize_mode(recording)
        return "first"
        return None
    # Default language path (existing behavior)
    if recording.current_summary() is not None:
        return "regenerate"
    if recording.summary_status == SummaryState.FAILED:
        return "retry_summary"
    if recording.summary_status == SummaryState.MISSING:
        return "first"
    return None


def routing_confirmed(recording: Recording) -> bool:
    decision = recording.routing_decisions.filter(is_active=True).first()
    return decision is not None and decision.routing_verified


def retry_eligible(recording: Recording) -> bool:
    if recording.processing_status == ProcessingStatus.FAILED:
        return True
    if recording.processing_status != ProcessingStatus.TRANSCRIBED:
        return False
    return (
        recording.retranscription_failed
        or recording.summary_status == SummaryState.FAILED
        or recording.resummarization_failed
    )


def route_eligible(recording: Recording) -> bool:
    routing_failed = (
        recording.processing_status == ProcessingStatus.FAILED
        and recording.failure_stage == FailureStage.ROUTING
    )
    return recording.processing_status in ROUTE_ELIGIBLE_STATUSES or routing_failed


def execute_web_action(
    config: AppConfig,
    recording: Recording,
    action: str,
    *,
    profile_name: str | None = None,
    requested_mode: str | None = None,
    expected_fingerprint: str | None = None,
    language: str = "default",
) -> ActionOutcome:
    """Run one mutating web action under the global pipeline lock.

    Raises :class:`PipelineBusy` when another pipeline process holds the
    lock (the view renders 409) and :class:`ActionRejected` when the
    action is not allowed for the current state (the view renders a
    friendly rejection — never a traceback).
    """
    with pipeline_lock(config):
        from workflow.services.pipeline import recover_interruptions

        recover_interruptions(config)
        recording = Recording.objects.get(pk=recording.pk)
        if expected_fingerprint is not None and state_fingerprint(recording) != expected_fingerprint:
            return ActionOutcome(
                ok=True,
                result="state_changed",
                message=(
                    "The recording changed since the form was opened, so nothing was run. "
                    "Reload the page and try again if still needed."
                ),
            )
        if action == "route":
            return _action_route(config, recording, profile_name)
        if action == "confirm-routing":
            return _action_confirm_routing(recording)
        if action == "transcribe":
            return _action_transcribe(config, recording)
        if action == "summarize":
            return _action_summarize(config, recording, requested_mode, language=language)
        if action == "retry":
            return _action_retry(config, recording)
    raise ActionRejected("unknown_action", f"Unknown action '{action}'.")


def _action_route(config: AppConfig, recording: Recording, profile_name: str | None) -> ActionOutcome:
    from brainlib.config import ConfigError
    from workflow.services.pipeline import manual_route

    if not profile_name:
        raise ActionRejected("profile_missing", "No routing profile was selected.")
    if not route_eligible(recording):
        raise ActionRejected(
            "ineligible_state",
            f"Manual routing is not available while the recording is "
            f"'{recording.processing_status}'.",
        )
    try:
        result = manual_route(recording, profile_name, confirmed_by="web")
    except ConfigError as exc:
        raise ActionRejected("unknown_profile", str(exc)) from None
    retranscription_pending = (
        result["status"] == ProcessingStatus.READY_TO_TRANSCRIBE
        and recording.transcripts.filter(is_active=True).exists()
    )
    if result.get("result") == "verified_no_retranscription":
        message = "Routing confirmed — the selected profile was already active, nothing was retranscribed."
    elif retranscription_pending:
        message = "Routing updated. The existing transcript stays active until the retranscription succeeds."
    else:
        message = "Routing updated. The recording is ready to transcribe."
    return ActionOutcome(ok=True, result=result.get("result", "routed"), message=message, detail=result)


def _action_confirm_routing(recording: Recording) -> ActionOutcome:
    from workflow.services.pipeline import confirm_routing

    decision = recording.routing_decisions.filter(is_active=True).first()
    if decision is None:
        raise ActionRejected("no_active_decision", "There is no active routing decision to confirm.")
    if decision.routing_verified:
        return ActionOutcome(
            ok=True,
            result="already_confirmed",
            message="Routing was already confirmed.",
            detail={"decision_id": decision.pk},
        )
    result = confirm_routing(recording, confirmed_by="web")
    return ActionOutcome(
        ok=True,
        result="confirmed",
        message="Routing confirmed. The transcription stays as-is.",
        detail=result,
    )


def _action_transcribe(config: AppConfig, recording: Recording) -> ActionOutcome:
    from workflow.services.pipeline import transcribe_one

    if recording.processing_status != ProcessingStatus.READY_TO_TRANSCRIBE:
        raise ActionRejected(
            "ineligible_state",
            f"Transcription is not available while the recording is "
            f"'{recording.processing_status}'.",
        )
    decision = recording.routing_decisions.filter(is_active=True).first()
    if decision is None or not decision.model_id:
        raise ActionRejected("no_routing_decision", "No usable routing decision exists for this recording.")
    result = transcribe_one(config, recording)
    if result.get("result") == "transcribed":
        message = "Transcription completed."
        if result.get("speakers_fallback"):
            message = (
                "Transcription completed WITHOUT speaker labels: diarization "
                "failed once and the fallback retry (no speaker detection) "
                "succeeded. Both runs are recorded in the attempt history."
            )
        return ActionOutcome(
            ok=True,
            result="transcribed",
            message=message,
            detail=result,
        )
    if result.get("result") == "parked":
        return ActionOutcome(
            ok=False,
            result="parked",
            message=f"The audio source is not usable right now ({result.get('reason')}). "
            "Nothing was transcribed; run 'brain ingest' or check the file.",
            detail=result,
        )
    return ActionOutcome(
        ok=False,
        result="failed",
        message=f"Transcription failed ({result.get('error_code') or 'unknown_error'})"
        + (
            f": {result['error_message']}"
            if result.get("error_message")
            else ""
        )
        + ". You can retry it explicitly.",
        detail=result,
    )


def _action_summarize(
    config: AppConfig, recording: Recording, requested_mode: str | None,
    *, language: str = "default",
) -> ActionOutcome:
    from workflow.services.languages import GENERATION_SELECTORS
    from workflow.services.summarize import summarize_one

    if language not in GENERATION_SELECTORS:
        raise ActionRejected(
            "unsupported_language",
            f"'{language}' is not a valid generation target. Only default, "
            "English, Traditional Chinese and Original can be generated.",
        )
    from workflow.services.summarize import resolve_output_language

    transcript = recording.transcripts.filter(is_active=True).first()
    if transcript is None:
        raise ActionRejected(
            "ineligible_state",
            "No active transcript for this recording.",
        )
    # Resolve exactly as the confirmation page did. An unresolved
    # Original (unknown source language) is a valid generation request:
    # execution performs bounded detection first.
    output_language = resolve_output_language(transcript, language)
    if not output_language:
        mode = "first"
    else:
        mode = summarize_mode(recording, output_language=output_language)
    if mode is None:
        raise ActionRejected(
            "ineligible_state",
            f"Summarization is not available while the recording is "
            f"'{recording.processing_status}' (summary status: "
            f"'{recording.summary_status}').",
        )
    if requested_mode is not None and requested_mode != mode:
        return ActionOutcome(
            ok=True,
            result="state_changed",
            message=(
                "The summary state changed since the form was opened, so nothing was run. "
                "Reload the page and try again if still needed."
            ),
        )
    result = summarize_one(
        config, recording,
        target_language=language,
        regenerate=(mode == "regenerate"),
    )
    if result.get("result") == "summarized":
        return ActionOutcome(
            ok=True,
            result="summarized",
            message=f"Summary generated ({result.get('output_language')}).",
            detail=result,
        )
    if result.get("result") == "skipped":
        return ActionOutcome(
            ok=False,
            result="skipped",
            message=f"Summarization was skipped ({result.get('reason')}).",
            detail=result,
        )
    return ActionOutcome(
        ok=False,
        result="failed",
        message=f"Summarization failed ({result.get('error_code') or 'unknown_error'}). "
        "You can retry it explicitly.",
        detail=result,
    )


def _action_retry(config: AppConfig, recording: Recording) -> ActionOutcome:
    from workflow.services.pipeline import retry

    if not retry_eligible(recording):
        raise ActionRejected(
            "ineligible_state",
            f"Retry is not available while the recording is '{recording.processing_status}' "
            "and there is no failed stage to retry.",
        )
    result = retry(config, recording)
    if result.get("result") == "skipped":
        return ActionOutcome(
            ok=True,
            result="state_changed",
            message="Nothing needed retrying — the recording state changed since the form was opened.",
            detail=result,
        )
    stage = result.get("stage")
    if stage == "summarization":
        summarize_result = result.get("summarize_result", {})
        if summarize_result.get("result") == "summarized":
            message = "Retry completed: the summary was generated."
        else:
            message = (
                f"Summary retry failed ({summarize_result.get('error_code') or 'unknown_error'}). "
                "You can retry it explicitly."
            )
        return ActionOutcome(ok=summarize_result.get("result") == "summarized", result="retried", message=message, detail=result)
    status = result.get("status")
    if status == ProcessingStatus.TRANSCRIBED:
        message = "Retry completed: the recording is transcribed."
    elif status == ProcessingStatus.FAILED:
        message = "Retry ran but the stage failed again. See the attempt details below."
    else:
        message = "Retry initiated; the recording re-entered the pipeline."
    ok = status not in (ProcessingStatus.FAILED,)
    return ActionOutcome(ok=ok, result="retried", message=message, detail=result)


def attempt_summary_for_display(recording: Recording, limit: int = 10) -> list[dict]:
    """Sanitized attempt rows for the detail/history pages.

    Exposes only stable, non-sensitive fields: stage, ordinal, outcome,
    error_code, a re-sanitized, length-capped error_message, model,
    timestamps. Never cli_args_json, context_json, raw stderr, or
    endpoints. Re-sanitizing at the rendering boundary means historical
    rows written before stricter persistence cannot leak unsafe content.
    """
    from workflow.services.transcription import ERROR_DETAIL_CAP, sanitize_error

    attempts = recording.attempts.order_by("-started_at", "-pk")[:limit]
    return [
        {
            "id": attempt.pk,
            "stage": attempt.stage,
            "ordinal": attempt.ordinal,
            "outcome": attempt.outcome,
            "error_code": attempt.error_code,
            "error_message": sanitize_error(attempt.error_message, limit=ERROR_DETAIL_CAP),
            "model_id": attempt.model_id,
            "started_at": attempt.started_at,
            "finished_at": attempt.finished_at,
        }
        for attempt in attempts
    ]


def unfinished_attempt_stage(recording: Recording) -> str | None:
    """Stage of an unfinished attempt, if any (displayed as in-progress)."""
    attempt = recording.attempts.filter(finished_at__isnull=True).first()
    return attempt.stage if attempt is not None else None
