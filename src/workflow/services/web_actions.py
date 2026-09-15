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

from brainlib.config import AppConfig, MAX_SUMMARY_MODEL_CHARS, available_summary_models
from workflow.models import (
    FailureStage,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
    RoutingDecision,
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


# ONE fixed friendly message (with a stable code) for every rejected
# summary-model selection; never echoes the submitted value.
INVALID_SUMMARY_MODEL_MESSAGE = (
    "The selected summary model is missing or not available — reload the page."
)


def _fingerprint_model_state(config: AppConfig | None) -> tuple[list[str], str]:
    """(effective model allowlist, default model) bound by action fingerprints.

    ``config`` is the caller's loaded configuration; when omitted the
    boot-time settings configuration is used (the same source the web
    layer renders with), so read-only fingerprint calls stay consistent.
    The identities are hashed into the opaque digest and never rendered.
    """
    if config is None:
        try:
            from brain import settings as django_settings

            config = django_settings.BRAIN_CONFIG_OBJ
        except Exception:
            return [], ""
    return list(available_summary_models(config)), config.llm.model


def resolve_submitted_model(
    config: AppConfig, values: list[str], *, default_only: bool
) -> str | None:
    """Strictly parse the executing POST's selected summary model.

    ``values`` is the raw ``request.POST.getlist("model")``. Returns the
    exact selected model, or ``None`` for ONE fixed friendly 400:
    missing, duplicate, blank, oversized or not in the effective
    allowlist. ``default_only`` is the initial-Generate case: no selector
    is rendered, so an absent value is the configured default and a
    forged alternate is rejected. Pure input validation — no lock,
    recovery, network, DB read or write.
    """
    default = config.llm.model
    if default_only:
        if not values:
            return default
        if len(values) != 1 or values[0] != default:
            return None
        return default
    if len(values) != 1:
        return None
    value = values[0]
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > MAX_SUMMARY_MODEL_CHARS
        or value not in available_summary_models(config)
    ):
        return None
    return value


def _validated_effective_model(config: AppConfig, model: str | None, *, mode: str) -> str:
    """Service-boundary validation of a summary action's selected model.

    Initial ``first`` generation has no selector and stays restricted to
    the configured default (an alternate forged value is rejected, never
    silently honored). ``retry_summary``/``regenerate`` require an exact
    member of the effective allowlist. Raises :class:`ActionRejected`
    with ONE fixed sanitized message/code for every rejection.
    """
    default = config.llm.model
    if mode == "first":
        if model is None or model == default:
            return default
        raise ActionRejected("invalid_model", INVALID_SUMMARY_MODEL_MESSAGE)
    if (
        model is None
        or type(model) is not str
        or not model.strip()
        or len(model) > MAX_SUMMARY_MODEL_CHARS
        or model not in available_summary_models(config)
    ):
        raise ActionRejected("invalid_model", INVALID_SUMMARY_MODEL_MESSAGE)
    return model


def state_fingerprint(recording: Recording, *, config: AppConfig | None = None) -> str:
    """OPAQUE stable fingerprint of the state an action form was rendered from.

    Returns the canonical lowercase 64-hex SHA-256 digest over the exact
    deterministic JSON bytes of the bound state (sort-keys JSON, UTF-8):
    ids, statuses, languages and the raw JSON never appear in the value —
    only this opaque digest is ever placed in a hidden form value.

    The digested state includes the newest attempt id so that ANY
    completed processing attempt (success or failure) invalidates an
    in-flight form — a duplicate submission can never re-run the action
    against the state it was submitted from.

    Also includes every stable local input that determines language
    resolution (active transcript identity, canonical source language
    and its verifier, the resolved default and Original output
    languages with an explicit unresolved marker): a source-language
    correction does not necessarily create a ProcessingAttempt, so a
    rendered summarize form would otherwise survive a change
    that silently redirects `original`/`default` to a different
    language. It also binds the ACTIVE routing decision's stable
    identity/behavior fields (decision pk + ordinal, profile_name,
    model_id, language_arg, routing_verified, with an explicit
    no-active marker): a routing update can append or verify a
    decision WITHOUT changing the recording status or creating an
    attempt, so a rendered route/confirm/transcribe form must still
    go stale. The bounded projection never includes raw evidence,
    confidence, reason text, verifier or timestamps. Strictly
    read-only: database SELECTs only — no LLM detection, network,
    subprocess, or writes.
    """
    summary = recording.current_summary()
    last_attempt_id = (
        ProcessingAttempt.objects.filter(recording=recording)
        .order_by("-ordinal", "-pk")
        .values_list("pk", flat=True)
        .first()
    )
    # Active routing decision: ONE bounded single-row stable-field
    # projection (the partial unique constraint guarantees at most one
    # active decision; no related rows are materialized).
    decision_row = (
        RoutingDecision.objects.filter(recording=recording, is_active=True)
        .values_list(
            "pk",
            "ordinal",
            "profile_name",
            "model_id",
            "language_arg",
            "routing_verified",
        )
        .first()
    )
    if decision_row is None:
        # Explicit no-active marker — distinct from any real decision.
        routing_state: dict[str, Any] = {"active": False}
    else:
        pk, ordinal, profile_name, model_id, language_arg, verified = decision_row
        routing_state = {
            "active": True,
            "decision_id": pk,
            "ordinal": ordinal,
            "profile_name": profile_name,
            "model_id": model_id,
            "language_arg": language_arg,
            "routing_verified": verified,
        }
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
    # Effective summary-model allowlist/default: a configuration change
    # (configured alternatives or the default model identity) invalidates
    # rendered Retry/Regenerate forms. Identities are hashed into the
    # opaque digest, never rendered.
    model_choices, default_model = _fingerprint_model_state(config)
    # The hashed payload is the EXACT historical deterministic JSON
    # serialization (sort_keys, default separators, UTF-8); the returned
    # value is its opaque lowercase hex SHA-256 digest.
    return hashlib.sha256(
        json.dumps(
            {
                "status": recording.processing_status,
                "summary_status": recording.summary_status,
                "retranscription_failed": recording.retranscription_failed,
                "resummarization_failed": recording.resummarization_failed,
                "summary_ordinal": summary.ordinal if summary is not None else None,
                "last_attempt_id": last_attempt_id,
                # Archive state: a rendered form from the active state can
                # never execute after an archive, and a rendered restore
                # form can never execute after a restore.
                "archived": recording.archived_at is not None,
                "variant_languages": variant_languages,
                "language_state": language_state,
                "routing_state": routing_state,
                "summary_models": model_choices,
                "summary_default_model": default_model,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


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
    expected_fingerprint: str,
    profile_name: str | None = None,
    requested_mode: str | None = None,
    language: str = "default",
    model: str | None = None,
) -> ActionOutcome:
    """Run one mutating web action under the global pipeline lock.

    ``expected_fingerprint`` is REQUIRED: the view parser has already
    accepted exactly one opaque lowercase 64-hex ``state_fingerprint``
    digest BEFORE this call (missing/malformed submissions are a
    friendly 400 before any lock). A canonical but STALE digest keeps
    the under-lock safe no-op below.

    Raises :class:`PipelineBusy` when another pipeline process holds the
    lock (the view renders 409) and :class:`ActionRejected` when the
    action is not allowed for the current state (the view renders a
    friendly rejection — never a traceback).
    """
    with pipeline_lock(config):
        from workflow.services.pipeline import recover_interruptions

        recover_interruptions(config)
        recording = Recording.objects.get(pk=recording.pk)
        if state_fingerprint(recording, config=config) != expected_fingerprint:
            return ActionOutcome(
                ok=True,
                result="state_changed",
                message=(
                    "The recording changed since the form was opened, so nothing was run. "
                    "Reload the page and try again if still needed."
                ),
            )
        if action not in ("archive", "restore") and recording.archived_at is not None:
            # Defense-in-depth: an archived Recording refuses every other
            # mutating action. The archived-state fingerprint already
            # invalidates pre-archive forms; this makes a forged/
            # forged-state submission fail safely too.
            raise ActionRejected(
                "recording_archived",
                "This recording is archived. Restore it before running pipeline actions.",
            )
        if action == "route":
            return _action_route(config, recording, profile_name)
        if action == "confirm-routing":
            return _action_confirm_routing(recording)
        if action == "transcribe":
            return _action_transcribe(config, recording)
        if action == "summarize":
            return _action_summarize(
                config, recording, requested_mode, language=language, model=model
            )
        if action == "retry":
            return _action_retry(config, recording)
        if action == "archive":
            return _action_archive(recording)
        if action == "restore":
            return _action_restore(recording)
    raise ActionRejected("unknown_action", f"Unknown action '{action}'.")


def _action_archive(recording: Recording) -> ActionOutcome:
    from workflow.services.archive import archive_recording

    result = archive_recording(recording)
    if result.get("result") == "refused":
        raise ActionRejected(
            "unfinished_attempt",
            "This recording still has a running pipeline attempt, so it was not "
            "archived. Wait for it to finish (or recover it) and try again.",
        )
    if result.get("result") == "unchanged":
        return ActionOutcome(
            ok=True,
            result="already_archived",
            message="This recording was already archived.",
        )
    return ActionOutcome(
        ok=True,
        result="archived",
        message=(
            "Recording archived. Its audio, transcript, summaries, tags and history "
            "are retained; it no longer appears in the Library, search, Ask or Review. "
            "Restore it any time from the Archived page."
        ),
    )


def _action_restore(recording: Recording) -> ActionOutcome:
    from workflow.services.archive import restore_recording

    result = restore_recording(recording)
    if result.get("result") == "unchanged":
        return ActionOutcome(
            ok=True,
            result="already_active",
            message="This recording is not archived.",
        )
    return ActionOutcome(
        ok=True,
        result="restored",
        message="Recording restored. It is eligible for the Library and search again.",
    )


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
    *, language: str = "default", model: str | None = None,
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
    # Resolve exactly as the view's pre-lock probe did. An unresolved
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
    # Service-boundary defense-in-depth: the selected model is validated
    # again here (initial Generate stays default-only; Retry/Regenerate
    # must be an exact effective-allowlist member). Never an arbitrary
    # submitted string.
    selected_model = _validated_effective_model(config, model, mode=mode)
    result = summarize_one(
        config, recording,
        target_language=language,
        regenerate=(mode == "regenerate"),
        model=selected_model,
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


def section_state_fingerprint(
    recording: Recording, section, *, config: AppConfig | None = None
) -> str:
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
      invalidates rendered action forms even when no attempt exists;
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
    model_choices, default_model = _fingerprint_model_state(config)
    state = {
        "recording": recording.pk,
        "archived": recording.archived_at is not None,
        # The Section's OWN archive marker: a rendered Archive form can
        # never execute after an archive, and a rendered Restore form can
        # never execute after a restore (stale/duplicate => safe no-op).
        "section_archived": fresh_section.archived_at is not None,
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
        # Effective summary-model allowlist/default (hashed, opaque):
        # a config choice change invalidates rendered section forms.
        "summary_models": model_choices,
        "summary_default_model": default_model,
    }
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def section_restore_fingerprint(recording: Recording, section) -> str:
    """OPAQUE read-only fingerprint of a SECTION restore form.

    Unlike :func:`section_state_fingerprint` this deliberately does NOT
    require the Section's layout/transcript to still be ACTIVE: an
    archived Section that later became historical stays restorable
    (reversible archive). It binds only the stable ownership/state
    identities needed to make a stale or duplicate submission a safe
    no-op: the parent Recording id and its archived state, the Section id
    and its OWN archived state, the transcript id/active state, the
    SegmentedVersion id/active state and the Section ordinal.

    Strictly SELECT-only (no writes, network, subprocess or locks) and
    bounded. Returns a canonical 64-lowercase-hex SHA-256; ids and raw
    JSON never appear in it. A missing/cross-parent/fixed (non-topic)
    target raises the stable sanitized ``SegmentationError`` categories.
    """
    from workflow.services.segmentation import SegmentationError

    if section is None or not isinstance(section, Section):
        raise SegmentationError("section_not_found")
    fresh = (
        Section.objects.select_related("transcript", "segmented_version")
        .filter(pk=section.pk)
        .first()
    )
    if fresh is None:
        raise SegmentationError("section_not_found")
    if fresh.segmented_version_id is None:
        raise SegmentationError("section_not_topic")
    if fresh.transcript.recording_id != recording.pk:
        raise SegmentationError("section_not_in_recording")
    recording_archived_at = (
        Recording.objects.filter(pk=recording.pk)
        .values_list("archived_at", flat=True)
        .first()
    )
    state = {
        "recording": fresh.transcript.recording_id,
        "recording_archived": recording_archived_at is not None,
        "section": fresh.pk,
        "section_archived": fresh.archived_at is not None,
        "transcript": fresh.transcript_id,
        "transcript_active": bool(fresh.transcript.is_active),
        "layout": fresh.segmented_version_id,
        "layout_active": bool(fresh.segmented_version.is_active),
        "ordinal": fresh.ordinal,
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
    model: str | None = None,
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
       submitted mode — a state change since the form was rendered is a
       safe no-op;
    6. only then calls ``summarize_section_one`` (caller-held lock
       contract; the service never touches the Recording-level summary
       tuple and schedules no recording search/embedding sync).
    """
    from workflow.services.pipeline import recover_interruptions
    from workflow.services.segmentation import SegmentationError, require_active_topic_section
    from workflow.services.summarize import summarize_section_one

    # Defensive input boundary (BEFORE the lock): the executing POST must
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
        if recording.archived_at is not None:
            return _section_stale_outcome()
        section = Section.objects.get(pk=section.pk)
        if section.archived_at is not None:
            # An archived individual Section is read-only: no ordinary
            # summary action may run against it (the archived detail
            # renders only Restore; this rejects forged submissions).
            return _section_stale_outcome()
        try:
            require_active_topic_section(section)
        except SegmentationError:
            return _section_stale_outcome()
        try:
            current = section_state_fingerprint(recording, section, config=config)
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
        # Service-boundary defense-in-depth: validate the selected model
        # again (initial Generate default-only; Retry/Regenerate exact
        # effective-allowlist member). Never an arbitrary submitted value.
        try:
            selected_model = _validated_effective_model(config, model, mode=mode)
        except ActionRejected:
            return ActionOutcome(
                ok=False,
                result="failed",
                message=INVALID_SUMMARY_MODEL_MESSAGE,
            )
        result = summarize_section_one(
            config,
            section,
            target_language=language,
            regenerate=(mode == "regenerate"),
            model=selected_model,
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


_SECTION_ARCHIVE_MESSAGES = {
    "section_not_found": "The section no longer exists.",
    "section_not_in_recording": "The section does not belong to this recording.",
    "section_not_topic": "Only topic sections can be archived.",
    "transcript_not_active": "Only the active transcript's sections can be archived.",
    "section_not_active": "The section belongs to a historical layout revision and is read-only.",
    "section_not_in_layout": "The section is not part of the active layout.",
    "layout_invalid": "The stored trim & split revision is invalid.",
    "parent_archived": "This recording is archived — restore it before archiving its sections.",
}


def execute_section_archive(
    config: AppConfig,
    recording: Recording,
    section,
    *,
    action: str,
    expected_fingerprint: str | None = None,
) -> ActionOutcome:
    """Run one reversible Section archive/restore under the pipeline lock.

    Mirrors the section-summary contract: service-boundary validation
    BEFORE any lock (missing/malformed fingerprint or cross-parent
    Section → safe stale no-op), then the exclusive pipeline lock,
    interruption recovery, live Section re-validation via the SHARED
    ``section_state_fingerprint`` (stale/duplicate => safe no-op with zero
    DML), then the transactional ``archive_section``/``restore_section``
    service. An archived parent Recording suppresses the operation (the
    web hides the controls; this is defense-in-depth). No files, network,
    index rows, layout revisions or sync are touched.
    """
    from workflow.services import archive as archive_service
    from workflow.services.pipeline import recover_interruptions
    from workflow.services.segmentation import SegmentationError

    if action not in ("archive", "restore"):
        return _section_stale_outcome()
    expected_fingerprint = (
        canonical_section_fingerprint(expected_fingerprint)
        if expected_fingerprint is not None
        else None
    )
    if expected_fingerprint is None:
        return _section_stale_outcome()
    fresh_section = (
        Section.objects.select_related("transcript").filter(pk=section.pk).first()
    )
    if (
        fresh_section is None
        or fresh_section.transcript.recording_id != recording.pk
    ):
        return _section_stale_outcome()

    with pipeline_lock(config):
        recover_interruptions(config)
        recording = Recording.objects.get(pk=recording.pk)
        if recording.archived_at is not None:
            return _section_stale_outcome()
        section = Section.objects.get(pk=section.pk)
        if action == "restore":
            # Reversible restore uses the DEDICATED archive-state
            # fingerprint so a Section that became historical remains
            # restorable; stale/duplicate submissions (layout, transcript
            # or archive-state change) are safe no-ops.
            try:
                current = section_restore_fingerprint(recording, section)
            except SegmentationError:
                return _section_stale_outcome()
            if current != expected_fingerprint:
                return _section_stale_outcome()
            result = archive_service.restore_section(recording, section)
        else:
            # New archive stays canonical-ACTIVE-topic-only: the ordinary
            # summary/archive fingerprint already fails closed for a
            # historical target.
            try:
                current = section_state_fingerprint(recording, section, config=config)
            except SegmentationError:
                return _section_stale_outcome()
            if current != expected_fingerprint:
                return _section_stale_outcome()
            result = archive_service.archive_section(recording, section)

    outcome = result.get("result")
    if outcome == "archived":
        return ActionOutcome(
            ok=True,
            result="archived",
            message=(
                "Section archived. It no longer appears in the Library, search or "
                "Ask; its summary, tags, transcript and history are retained."
            ),
        )
    if outcome == "restored":
        return ActionOutcome(
            ok=True,
            result="restored",
            message="Section restored. It is eligible for the Library and search again.",
        )
    if outcome == "unchanged":
        if action == "archive":
            return ActionOutcome(
                ok=True, result="already_archived", message="This section was already archived."
            )
        return ActionOutcome(
            ok=True, result="already_active", message="This section is not archived."
        )
    reason = result.get("reason") or "unknown"
    message = _SECTION_ARCHIVE_MESSAGES.get(
        reason, "The section archive request could not be completed."
    )
    return ActionOutcome(ok=False, result="refused", message=message)


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
    "recording_archived": "This recording is archived — restore it before editing trim & splits.",
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
