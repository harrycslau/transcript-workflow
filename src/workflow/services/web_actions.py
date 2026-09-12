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

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from brainlib.config import AppConfig
from workflow.models import (
    FailureStage,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
    Section,
    SummaryState,
    Transcript,
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

# Step 6.2 section-action input contract (shared by the web view and the
# service boundary):
# - the opaque section fingerprint is exactly ONE canonical 64-hex value
#   (upper or lower case), normalized to lowercase before comparison;
# - the submitted action mode must be exactly one of the section action
#   modes (the values ``_mode_for_language``/``summarize_mode`` produce).
SECTION_ACTION_MODES = ("first", "retry_summary", "regenerate")
_HEX64_CI = re.compile(r"[0-9a-fA-F]{64}\Z")

# Defensive cap on the current scope state (variant-state rows / active
# Summaries per output language) that ``section_state_fingerprint`` binds.
# Output-language variants are a small bounded set in practice; an unusual
# row count fails closed instead of being materialized into the hash.
_SECTION_FINGERPRINT_STATE_CAP = 64


def canonical_section_fingerprint(value: str) -> str | None:
    """Canonicalize one submitted opaque section-action fingerprint.

    Accepts a single 64-hex string (lower or upper case) and returns the
    normalized lowercase form; ``None`` for missing/malformed/oversized
    values. Callers that must reject duplicate submissions use
    ``getlist`` and check cardinality before calling this.
    """
    if type(value) is not str or not _HEX64_CI.match(value):
        return None
    return value.lower()


def _section_stale_outcome() -> ActionOutcome:
    """The fixed safe no-op for a stale/invalid section action submission."""
    return ActionOutcome(
        ok=True,
        result="state_changed",
        message=(
            "The section changed since this form was opened, so "
            "nothing was run. Reload the page and try again if still needed."
        ),
    )


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
    section = (
        transcript.sections.filter(ordinal=0, segmented_version__isnull=True).first()
        if transcript
        else None
    )
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


def section_state_fingerprint(recording: Recording, section) -> str:
    """OPAQUE read-only fingerprint of a SECTION summary action form.

    Strictly SELECT-only (no writes, network, subprocess, or locks) and
    bounded. Returns a canonical 64-lowercase-hex SHA-256 over the
    deterministic state that determines the section-scoped action result.
    The returned value is opaque: ids, titles, ranges and raw JSON never
    appear in it (only the hash is ever placed in a hidden form value).
    Binds every stable input that determines the section-scoped action:

    - the recording's currently ACTIVE transcript identity and the
      canonical ACTIVE layout/section identity/range/title (via the
      shared segmentation canonical validator — a layout that became
      historical or malformed raises the stable ``SegmentationError``,
      never a folded-in hash);
    - the transcript source-language resolution inputs (source language,
      verifier, resolved default and Original output languages with an
      explicit unresolved marker) — a source-language correction
      invalidates rendered confirmations even when no attempt exists;
    - the section-scoped summary attempts via a SUFFICIENT append-only
      contract — the attempt COUNT (bounded aggregate) plus the LATEST
      mutable attempt's (pk, ordinal, outcome, error_code, finished_at).
      This is deliberately NOT the full attempt list: because attempts are
      append-only, any new attempt changes the count or the latest row,
      and any mutation of the newest attempt (recovery writes
      outcome/error_code/finished_at) changes the latest row — so the
      fingerprint still invalidates on every relevant attempt change
      without materializing the append-only history;
    - the Section's variant states and active summaries, bound by a
      defensive cap: an unusual row count fails closed with the stable
      sanitized ``section_state_too_large`` category (never materialized).

    A layout/title/source-language/attempt/variant change therefore
    invalidates the fingerprint.
    """
    from workflow.models import AttemptStage, ProcessingAttempt, Summary, SummaryVariantState
    from workflow.services.langresolve import (
        resolve_default_language,
        resolve_output_language,
    )
    from workflow.services.segmentation import (
        SegmentationError,
        require_active_topic_section,
    )

    canonical = require_active_topic_section(section)
    # Re-fetch the Section and its transcript FRESH: the caller may hold
    # a cached FK whose ``language_observed`` etc. predate a correction,
    # and the fingerprint must bind the CURRENT source-language state.
    fresh_section = Section.objects.select_related("transcript").get(pk=section.pk)
    transcript = fresh_section.transcript
    # Defense-in-depth parent ownership: the Section must belong to the
    # passed recording (the caller 404s earlier; this boundary fails
    # closed with a stable sanitized category, never a cross-recording
    # fingerprint).
    if transcript.recording_id != recording.pk:
        raise SegmentationError("section_not_in_recording")
    # The canonical layout dict holds the FRESH section title/range; the
    # caller's object may be stale, so the fingerprint binds the canonical
    # entry, never the cached FK values.
    fresh_entry = next(
        sec for sec in canonical["sections"] if sec["ordinal"] == fresh_section.ordinal
    )
    active_transcript_pk = (
        Transcript.objects.filter(recording=recording, is_active=True)
        .values_list("pk", flat=True)
        .first()
    )
    # Bounded attempt state: ONE aggregate count plus the latest mutable
    # attempt only — never the append-only attempt history.
    attempts_qs = ProcessingAttempt.objects.filter(
        recording=recording,
        stage=AttemptStage.SUMMARIZATION,
        context_json__language__section_id=section.pk,
    )
    attempt_count = attempts_qs.count()
    latest_attempt_row = (
        attempts_qs.order_by("-ordinal", "-pk")
        .values_list("pk", "ordinal", "outcome", "error_code", "finished_at")
        .first()
    )
    latest_attempt = None
    if latest_attempt_row is not None:
        pk, ordinal, outcome, error_code, finished_at = latest_attempt_row
        latest_attempt = (
            pk, ordinal, outcome, error_code,
            finished_at.isoformat() if finished_at is not None else None,
        )
    # Capped current scope state (variant states + active Summaries are
    # unique per output_language — a small bounded set in practice).
    variant_states = list(
        SummaryVariantState.objects.filter(transcript=transcript, section=section)
        .order_by("output_language")
        .values_list(
            "output_language", "status", "regeneration_failed", "last_failed_attempt_id"
        )[:_SECTION_FINGERPRINT_STATE_CAP + 1]
    )
    if len(variant_states) > _SECTION_FINGERPRINT_STATE_CAP:
        raise SegmentationError("section_state_too_large")
    current_summaries = list(
        Summary.objects.filter(transcript=transcript, section=section, is_active=True)
        .order_by("output_language")
        .values_list("pk", "output_language", "ordinal")[
            : _SECTION_FINGERPRINT_STATE_CAP + 1
        ]
    )
    if len(current_summaries) > _SECTION_FINGERPRINT_STATE_CAP:
        raise SegmentationError("section_state_too_large")
    state = {
        "recording": recording.pk,
        "active_transcript": active_transcript_pk,
        "layout_id": canonical["id"],
        "layout_revision": canonical["revision"],
        "layout_start": canonical["start"],
        "layout_end": canonical["end_exclusive"],
        "section_ordinal": fresh_entry["ordinal"],
        "section_title": fresh_entry["title"],
        "section_start": fresh_entry["start"],
        "section_end": fresh_entry["end"],
        "language_state": {
            "source_language": transcript.language_observed or "",
            "source_verified_by": transcript.language_observed_verified_by or "",
            "default_output": resolve_default_language(transcript),
            "original_output": resolve_output_language(transcript, "original"),
        },
        "attempt_count": attempt_count,
        "latest_attempt": latest_attempt,
        "variant_states": variant_states,
        "current_summaries": current_summaries,
    }
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# Fixed sanitized user-facing messages per section summarization failure.
_SECTION_SUMMARIZE_MESSAGES = {
    "section_not_found": "The section no longer exists.",
    "section_not_topic": "Only topic sections can be summarized.",
    "transcript_not_active": "Only the active transcript's sections can be summarized.",
    "section_not_active": "The section belongs to a historical layout revision and is read-only.",
    "section_not_in_layout": "The section is not part of the active layout.",
    "layout_invalid": "The stored trim & split revision is invalid.",
    "section_not_in_recording": "The section does not belong to this recording.",
    "section_state_too_large": (
        "This section's summary state is too large to process — reload the page and try again."
    ),
    "unsupported_language": (
        "'{language}' is not a valid generation target. Only default, English, "
        "Traditional Chinese and Original can be generated."
    ),
}


def section_summarize_friendly_message(code: str, **fmt) -> str:
    """A fixed, sanitized message for a section summarization failure
    code. The ONLY interpolation is the (validated) generation selector
    for ``unsupported_language`` — never raw values, ids, paths, or
    transcript content."""
    template = _SECTION_SUMMARIZE_MESSAGES.get(
        code, "The section summary request could not be completed."
    )
    if code == "unsupported_language" and fmt:
        return template.format(language=fmt["language"])
    return template


def execute_section_summarize(
    config: AppConfig,
    recording: Recording,
    section,
    *,
    requested_mode: str | None = None,
    expected_fingerprint: str | None = None,
    language: str = "default",
) -> ActionOutcome:
    """Run one Step 6.2 section-summarization action under the lock.

    Mirrors the processing-action contract exactly:

    0. service-boundary validation BEFORE any lock/recovery/network/write:
       a missing/duplicate/malformed opaque fingerprint or action mode and
       a cross-parent Section are a safe stale no-op (the web view rejects
       these before reaching the service; this boundary is defense-in-
       depth and never runs against unvalidated state);
    1. acquires the SAME exclusive pipeline lock (busy raises
       :class:`PipelineBusy`, which the view converts to a 409 response);
    2. runs the idempotent interruption-recovery pass while holding the lock;
    3. re-fetches the section and re-validates it as a LIVE topic target
       of the ACTIVE layout (a section that became historical is a safe
       stale no-op — never a write against read-only history);
    4. compares the opaque section action fingerprint captured when the
       page was rendered — a stale or duplicate submission is a safe
       no-op with zero DML;
    5. re-derives the per-variant action mode and compares it to the
       submitted mode — a state change since the confirmation is a safe
       no-op;
    6. only then calls ``summarize_section_one`` (caller-held lock
       contract; the service never touches the Recording-level summary
       tuple and schedules no recording search/embedding sync).
    """
    from workflow.services.pipeline import recover_interruptions
    from workflow.services.segmentation import SegmentationError, require_active_topic_section
    from workflow.services.summarize import summarize_section_one

    # Defensive input boundary (BEFORE the lock): the confirmed POST must
    # carry exactly one canonical 64-hex fingerprint and exactly one valid
    # section action mode; the Section must belong to the recording.
    # Anything else is a safe no-op — never a run, never a write.
    expected_fingerprint = (
        canonical_section_fingerprint(expected_fingerprint)
        if expected_fingerprint is not None
        else None
    )
    if expected_fingerprint is None:
        return _section_stale_outcome()
    if (
        requested_mode is None
        or type(requested_mode) is not str
        or requested_mode.strip().lower() not in SECTION_ACTION_MODES
    ):
        return _section_stale_outcome()
    requested_mode = requested_mode.strip().lower()
    fresh_section = (
        Section.objects.select_related("transcript").filter(pk=section.pk).first()
    )
    if (
        fresh_section is None
        or fresh_section.transcript.recording_id != recording.pk
    ):
        return _section_stale_outcome()
    section = fresh_section

    with pipeline_lock(config):
        recover_interruptions(config)
        recording = Recording.objects.get(pk=recording.pk)
        section = Section.objects.get(pk=section.pk)
        try:
            require_active_topic_section(section)
        except SegmentationError:
            return _section_stale_outcome()
        try:
            current = section_state_fingerprint(recording, section)
        except SegmentationError:
            return _section_stale_outcome()
        if current != expected_fingerprint:
            return _section_stale_outcome()
        # Re-derive the mode from the CURRENT state for the requested
        # GENERATION selector (section scope: variant state authoritative,
        # never the Recording tuple). An unresolved Original is a valid
        # first-generation request.
        transcript = recording.transcripts.filter(is_active=True).first()
        if transcript is None or transcript.pk != section.transcript_id:
            return _section_stale_outcome()
        from workflow.services.variant_view import build_variant_view

        variant = build_variant_view(recording, language, section=section)
        mode = variant.action_mode
        if mode is None:
            return ActionOutcome(
                ok=False,
                result="failed",
                message=(
                    "Summarization is not available for this section in its current state."
                ),
            )
        if requested_mode != mode:
            return _section_stale_outcome()
        result = summarize_section_one(
            config,
            section,
            target_language=language,
            regenerate=(mode == "regenerate"),
        )
        if result.get("result") == "summarized":
            return ActionOutcome(
                ok=True,
                result="summarized",
                message=f"Section summary generated ({result.get('output_language')}).",
                detail=result,
            )
        if result.get("result") == "skipped":
            return ActionOutcome(
                ok=False,
                result="skipped",
                message=f"Section summarization was skipped ({result.get('reason')}).",
                detail=result,
            )
        return ActionOutcome(
            ok=False,
            result="failed",
            message=f"Section summarization failed ({result.get('error_code') or 'unknown_error'}). "
            "You can retry it explicitly.",
            detail=result,
        )


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


# Fixed sanitized user-facing messages per SegmentationError code. Never
# contains input values, ids, paths, or transcript content.
_SEGMENTATION_MESSAGES = {
    "invalid_input": "The trim & split payload is malformed.",
    "invalid_fingerprint": "The state fingerprint is missing or invalid — reload the page.",
    "stale_state": "The trim & split state changed since the page was opened — reload and try again.",
    "layout_invalid": "The stored trim & split revision is invalid.",
    "title_blank": "Every topic section needs a name.",
    "title_too_long": "A topic name is too long (at most 255 characters).",
    "title_invalid_chars": "A topic name contains forbidden characters.",
    "title_count_mismatch": "The topic count does not match the number of splits.",
    "title_flag_count_mismatch": "The temporary-title flags do not match the topic count.",
    "title_flag_forgery": (
        "A topic marked as auto-named must use the server-generated name — "
        "type a custom name instead."
    ),
    "too_many_topics": "Too many topic sections.",
    "recording_not_found": "The recording no longer exists.",
    "transcript_not_found": "The transcript no longer exists.",
    "transcript_not_active": "Only the active transcript can be trimmed and split.",
    "transcript_empty": "The transcript has no segments.",
    "segments_not_contiguous": "The transcript segments are not contiguous.",
    "range_out_of_bounds": "The working range is outside the transcript.",
    "empty_range": "The working range must leave at least one segment.",
    "split_out_of_range": "A split marker is outside the working range.",
    "split_at_endpoint": "Splits can only be placed inside the working range.",
    "duplicate_split": "Duplicate split markers are not allowed.",
    "storage_error": "Saving failed — try again.",
}


def segmentation_friendly_message(code: str) -> str:
    """A fixed, sanitized message for a :class:`SegmentationError` code."""
    return _SEGMENTATION_MESSAGES.get(code, "The trim & split request could not be completed.")


def execute_segmentation_save(
    config: AppConfig,
    recording: Recording,
    payload: dict,
    *,
    expected_fingerprint: str | None = None,
) -> ActionOutcome:
    """Run one Step 6.1 segmented-version save under the pipeline lock.

    Mirrors the processing-action contract exactly:

    1. acquires the SAME exclusive pipeline lock (busy raises
       :class:`PipelineBusy`, which the view converts to a 409 response);
    2. runs the idempotent interruption-recovery pass while holding the lock;
    3. re-derives the ACTIVE transcript from the CURRENT database state and
       re-computes the read-only segmentation fingerprint after the lock;
    4. compares the fingerprint captured when the page was rendered — a
       stale or duplicate submission is a safe no-op with zero DML;
    5. only then calls ``segmentation.save_segmented_version`` (the ONLY
       segmented-version writer).

    No search/embedding sync is scheduled (6.1 changes no indexed content),
    no SQLite retry is added (the pipeline lock owns concurrency), and the
    service itself logs nothing and touches no network/files.
    """
    with pipeline_lock(config):
        from workflow.services.pipeline import recover_interruptions
        from workflow.services.segmentation import (
            SegmentationError,
            save_segmented_version,
            segmentation_fingerprint,
        )

        recover_interruptions(config)
        recording = Recording.objects.get(pk=recording.pk)
        transcript = recording.transcripts.filter(is_active=True).first()
        if transcript is None or transcript.pk != payload["transcript_id"]:
            return ActionOutcome(
                ok=True,
                result="state_changed",
                message=(
                    "The transcript changed since this form was opened, so nothing was saved. "
                    "Reload the page and try again if still needed."
                ),
            )
        if expected_fingerprint is not None:
            try:
                current = segmentation_fingerprint(
                    recording.pk, transcript, timezone_name=config.timezone
                )
            except SegmentationError as exc:
                return ActionOutcome(
                    ok=False,
                    result="failed",
                    message=segmentation_friendly_message(exc.code),
                )
            if current != expected_fingerprint:
                return ActionOutcome(
                    ok=True,
                    result="state_changed",
                    message=(
                        "The trim & split state changed since the page was opened, so nothing was saved. "
                        "Reload the page and try again if still needed."
                    ),
                )
        try:
            result = save_segmented_version(
                recording.pk,
                transcript.pk,
                payload["start"],
                payload["end_exclusive"],
                payload["splits"],
                payload["titles"],
                payload["title_is_temporary"],
                timezone_name=config.timezone,
            )
        except SegmentationError as exc:
            return ActionOutcome(
                ok=False,
                result="failed",
                message=segmentation_friendly_message(exc.code),
            )
        if not result.created:
            return ActionOutcome(
                ok=True,
                result="unchanged",
                message="No changes to save — the working layout is already as shown.",
            )
        return ActionOutcome(
            ok=True,
            result="saved",
            message=f"Trim & split revision {result.revision} saved.",
            detail={
                "revision": result.revision,
                "version_id": result.version_id,
                "superseded_revision": result.superseded_revision,
                "topic_section_count": result.topic_section_count,
            },
        )
