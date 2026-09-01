"""Pipeline orchestration: ingest -> route -> transcribe, plus manual actions.

Concurrency: all mutating entry points must run under the pipeline file
lock (see pipeline_lock). Read-only commands do not lock.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from brainlib.config import AppConfig
from brainlib.config import ConfigError
from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    AudioStatus,
    FailureStage,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
    RoutingDecision,
    RoutingMethod,
    SummaryState,
)
from workflow.services import ingest as ingest_service
from workflow.services import routing as routing_service
from workflow.services import transcription as transcription_service
from workflow.services.pipeline_lock import PipelineBusy, pipeline_lock
from workflow.services.statemachine import record_failure, transition

logger = logging.getLogger(__name__)

# Recording filename timestamp extraction (best effort): e.g. 2024-03-01_153045
_TIMESTAMP_PATTERNS = [
    re.compile(r"(?P<y>20\d{2})[-_.]?(?P<m>\d{2})[-_.]?(?P<d>\d{2})[-_.T ]?(?P<H>\d{2})?(?P<M>\d{2})?(?P<S>\d{2})?"),
]


def derive_recorded_at(filename: str, timezone_name: str = "Europe/Helsinki"):
    """Parse a recording timestamp from a filename, timezone-aware.

    The timezone comes from configuration (default Europe/Helsinki);
    naive datetimes are never stored. Unparseable filenames yield None.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    for pattern in _TIMESTAMP_PATTERNS:
        match = pattern.search(filename)
        if not match:
            continue
        parts = match.groupdict()
        try:
            naive = datetime(
                int(parts["y"]),
                int(parts["m"]),
                int(parts["d"]),
                int(parts["H"] or 0),
                int(parts["M"] or 0),
                int(parts["S"] or 0),
            )
        except (ValueError, TypeError):
            continue
        try:
            return naive.replace(tzinfo=ZoneInfo(timezone_name))
        except Exception:
            continue
    return None


def _set_duration_from_file(recording: Recording, source_path: Path, config: AppConfig) -> None:
    from workflow.services.audiosamples import get_duration

    duration = get_duration(source_path)
    if duration is not None:
        recording.duration_seconds = duration
    recorded = derive_recorded_at(Path(source_path).name, config.timezone)
    if recorded is not None and recording.recorded_at is None:
        recording.recorded_at = recorded


def run_ingest(config: AppConfig) -> dict:
    """One ingest pass. Returns the machine-readable report."""
    report = ingest_service.ingest(config)
    # Enrich newly created recordings with duration/recorded_at.
    for path in report.hashed:
        source = ingest_service.AudioSource.objects.filter(path=path).select_related("recording").first()
        if source is None or source.recording is None:
            continue
        recording = source.recording
        _set_duration_from_file(recording, Path(path), config)
        recording.save()
    return report.as_dict()


def _apply_outcome(config: AppConfig, recording: Recording, outcome: routing_service.RoutingOutcome) -> dict:
    """Create the RoutingDecision for an automatic outcome and transition.

    Ordering inside one transaction: lock the recording, deactivate the
    previous active decision, create the new active decision, then
    transition/save. Any failure rolls back to the previous valid state.
    """
    with transaction.atomic():
        recording = Recording.objects.select_for_update().get(pk=recording.pk)
        RoutingDecision.objects.filter(recording=recording, is_active=True).update(is_active=False)
        last = RoutingDecision.objects.filter(recording=recording).order_by("-ordinal").first()
        ordinal = last.ordinal + 1 if last else 1
        profile = outcome.profile_name or ""
        model_id = outcome.model_id or ""
        language_arg = outcome.language_arg
        decision = RoutingDecision.objects.create(
            recording=recording,
            ordinal=ordinal,
            route_suggestion=outcome.route,
            profile_name=profile or outcome.route,
            model_id=model_id,
            language_arg=language_arg,
            method=RoutingMethod.AUTOMATIC,
            confidence=outcome.confidence,
            reason_code=outcome.reason_code,
            evidence=outcome.evidence,
            routing_verified=False,
            is_active=True,
        )
        if outcome.ready_to_transcribe and config.macwhisper.routing.auto_transcribe:
            transition(recording, ProcessingStatus.READY_TO_TRANSCRIBE)
        elif outcome.ready_to_transcribe:
            # auto_transcribe disabled: automatic suggestions wait for review.
            transition(recording, ProcessingStatus.NEEDS_REVIEW)
        else:
            transition(recording, ProcessingStatus.NEEDS_REVIEW)
        recording.save()
    return {"decision_id": decision.pk, "status": recording.processing_status}


def recover_interruptions(config: AppConfig) -> dict:
    """Recover interrupted attempts and orphan in-flight states.

    Must be called while the caller holds the global pipeline lock (so
    no live pipeline process can own the recovered rows). Idempotent:
    repeated calls with nothing to recover return zero counts.

    Stage-aware: only Recordings whose SUMMARIZATION attempt was actually
    recovered receive summary reconciliation (with that specific
    interrupted attempt as the authoritative event). An interrupted
    routing or transcription attempt never changes summary retry
    eligibility — a ``failed`` Summary stays ``failed`` and is never
    reopened for automatic retry.
    """
    now = timezone.now()
    recovered_attempts = 0
    recovered_recordings = 0
    # Recording IDs by the stage of the attempt that was recovered, and
    # the recovered summarization attempts themselves (authoritative
    # recovery events for summary reconciliation).
    recovered_summarization: list[tuple[str, ProcessingAttempt]] = []

    with transaction.atomic():
        stale = ProcessingAttempt.objects.filter(finished_at__isnull=True).select_related("recording")
        touched_recordings: set[str] = set()
        for attempt in stale:
            attempt.outcome = AttemptOutcome.INTERRUPTED
            attempt.finished_at = now
            attempt.error_code = "process_interrupted"
            attempt.error_message = ""
            attempt.save()
            recovered_attempts += 1
            touched_recordings.add(attempt.recording_id)
            if attempt.stage == AttemptStage.SUMMARIZATION:
                recovered_summarization.append((attempt.recording_id, attempt))

        # Orphan in-flight recording states (process died between the
        # status change and attempt creation, or after attempt recovery).
        for recording in Recording.objects.filter(
            processing_status__in=[ProcessingStatus.ROUTING, ProcessingStatus.TRANSCRIBING]
        ).select_for_update():
            touched_recordings.add(recording.pk)
            if recording.processing_status == ProcessingStatus.ROUTING:
                # Eligible for routing again (route_pending picks it up).
                continue
            has_active = recording.transcripts.filter(is_active=True).exists()
            recording.processing_status = (
                ProcessingStatus.TRANSCRIBED if has_active else ProcessingStatus.READY_TO_TRANSCRIBE
            )
            recording.save()
            recovered_recordings += 1

        # Recordings whose last attempt was interrupted mid-routing /
        # mid-transcription re-enter the pipeline.
        for pk in touched_recordings:
            recording = Recording.objects.filter(pk=pk).first()
            if recording is None:
                continue
            if recording.processing_status == ProcessingStatus.TRANSCRIBING:
                has_active = recording.transcripts.filter(is_active=True).exists()
                recording.processing_status = (
                    ProcessingStatus.TRANSCRIBED if has_active else ProcessingStatus.READY_TO_TRANSCRIBE
                )
                recording.save()
                recovered_recordings += 1
            elif (
                recording.processing_status == ProcessingStatus.READY_TO_TRANSCRIBE
                and not recording.transcripts.filter(is_active=True).exists()
            ):
                pass  # already eligible for transcribe_ready
            elif recording.processing_status == ProcessingStatus.DISCOVERED:
                transition(recording, ProcessingStatus.ROUTING)
                recording.save()

        # Summary reconciliation ONLY for recordings whose summarization
        # attempt was recovered in this pass: an interrupted
        # summarization attempt counts as the transcript's one automatic
        # attempt (failed / warned, explicit retry required). Unrelated
        # routing/transcription interruptions never touch summary state.
        from workflow.services.summarize import reconcile_recording_summary_state

        for recording_id, attempt in recovered_summarization:
            recording = Recording.objects.filter(pk=recording_id).first()
            if recording is not None:
                reconcile_recording_summary_state(recording, recovered_attempt=attempt)

    return {
        "recovered_attempts": recovered_attempts,
        "recovered_recordings": recovered_recordings,
    }


# Explicit source-validation outcomes (never an ambiguous None).
SOURCE_VALID = "valid"
SOURCE_MISSING = "missing"
SOURCE_CHANGED = "changed"
SOURCE_OUTSIDE_INBOX = "outside_inbox"
SOURCE_NONE = "no_source"

_SKIP_REASONS = {
    SOURCE_MISSING: "source_missing",
    SOURCE_CHANGED: "source_changed",
    SOURCE_OUTSIDE_INBOX: "outside_current_inbox",
    SOURCE_NONE: "no_canonical_source",
}


def _mark_source_missing(source: AudioSource) -> None:
    from workflow.services.ingest import _ensure_canonical, _record_audio_status

    now = timezone.now()
    with transaction.atomic():
        recording = Recording.objects.select_for_update().get(pk=source.recording_id)
        source.presence = AudioStatus.MISSING
        source.missing_at = now
        source.is_canonical = False
        source.save(update_fields=["presence", "missing_at", "is_canonical"])
        _record_audio_status(recording)
        _ensure_canonical(recording)
        recording.save()


def _recalculate_recording_sources(recording: Recording) -> None:
    from workflow.services.ingest import _ensure_canonical, _record_audio_status

    with transaction.atomic():
        recording = Recording.objects.select_for_update().get(pk=recording.pk)
        _record_audio_status(recording)
        _ensure_canonical(recording)
        recording.save()


def validate_source_for_processing(config: AppConfig, recording: Recording) -> tuple[AudioSource | None, str]:
    """Validate a usable source for routing/transcription with an explicit
    outcome.

    Returns ``(source, SOURCE_VALID)`` when a present, in-inbox source
    still matches the Recording's size/mtime/SHA-256 (it is promoted to
    canonical). Otherwise returns ``(None, status)`` where status is one
    of SOURCE_MISSING / SOURCE_CHANGED / SOURCE_OUTSIDE_INBOX /
    SOURCE_NONE. Side effects:

    - missing file -> AudioSource marked missing; Recording audio status
      and canonical source recalculated;
    - changed content (including same-size/same-mtime replacement,
      caught by SHA-256) -> source detached into the stability/rehash
      workflow; the old Recording identity is never processed;
    - outside-inbox sources are NOT statted, read, or hashed;
    - every present source is tried (canonical first) before giving up.
    """
    from workflow.services.ingest import (
        _ensure_canonical,
        _record_audio_status,
        is_inside_inbox,
        reconcile_changed_source,
        sha256_file,
    )

    sources = list(
        recording.sources.filter(presence=AudioStatus.PRESENT).order_by("-is_canonical", "first_seen_at", "pk")
    )
    if not sources:
        _recalculate_recording_sources(recording)
        return None, SOURCE_NONE

    last_status = SOURCE_NONE
    for source in sources:
        path = Path(source.path)
        # Boundary check BEFORE any exists/stat/open/hash operation.
        if not is_inside_inbox(path, Path(config.storage.inbox)):
            last_status = SOURCE_OUTSIDE_INBOX
            continue
        try:
            st = os.stat(path)
        except OSError:
            _mark_source_missing(source)
            last_status = SOURCE_MISSING
            continue
        if st.st_size != (source.file_size or -1) or st.st_mtime != (source.file_mtime or -1):
            reconcile_changed_source(source, st)
            last_status = SOURCE_CHANGED
            continue
        if recording.sha256:
            try:
                content_ok = sha256_file(path) == recording.sha256
            except OSError:
                _mark_source_missing(source)
                last_status = SOURCE_MISSING
                continue
            if not content_ok:
                # Same-size/same-mtime replacement: caught via SHA-256.
                reconcile_changed_source(source, st)
                last_status = SOURCE_CHANGED
                continue
        if not source.is_canonical:
            with transaction.atomic():
                recording = Recording.objects.select_for_update().get(pk=recording.pk)
                for other in recording.sources.filter(is_canonical=True):
                    other.is_canonical = False
                    other.save(update_fields=["is_canonical"])
                source.is_canonical = True
                source.save(update_fields=["is_canonical"])
        return source, SOURCE_VALID

    _recalculate_recording_sources(recording)
    return None, last_status


def _skip_outcome(recording: Recording, status: str) -> dict:
    """Clean park/skip result; no misleading failure attempt is created."""
    return {
        "recording_id": recording.pk,
        "result": "parked",
        "reason": _SKIP_REASONS.get(status, status),
        "status": recording.processing_status,
    }


def route_pending(config: AppConfig, recording_ids: list[str] | None = None) -> list[dict]:
    """Automatically route all recordings in the ``routing`` state."""
    results: list[dict] = []
    recordings = Recording.objects.filter(processing_status=ProcessingStatus.ROUTING)
    if recording_ids:
        recordings = recordings.filter(id__in=recording_ids)
    for recording in recordings:
        results.append(route_one(config, recording))
    return results


def route_one(config: AppConfig, recording: Recording) -> dict:
    # Routing-disabled is enforced BEFORE any source hashing, sample
    # extraction, MacWhisper or network work: a clean needs_review outcome.
    if not config.macwhisper.routing.enabled:
        outcome = routing_service.RoutingOutcome(
            route=routing_service.ROUTE_UNCERTAIN, profile_name=None, model_id=None,
            language_arg=None, method="automatic", confidence=None,
            reason_code=routing_service.REASON_ROUTING_DISABLED,
            evidence={"routing": "disabled"},
        )
        applied = _apply_outcome(config, recording, outcome)
        return {
            "recording_id": recording.pk,
            "result": "needs_review",
            "reason": routing_service.REASON_ROUTING_DISABLED,
            "status": applied["status"],
        }

    # Validate a usable source with an explicit outcome; changed content
    # is never routed under the old Recording identity. No misleading
    # failure attempt is created when the file was manually deleted.
    source, status = validate_source_for_processing(config, recording)
    if source is None:
        return _skip_outcome(recording, status)

    attempt = ProcessingAttempt.objects.create(
        recording=recording,
        stage=AttemptStage.ROUTING,
        ordinal=transcription_service.next_ordinal(recording, AttemptStage.ROUTING),
        router_version=routing_service.ROUTER_VERSION,
    )
    attempt_dir = Path(config.storage.temp) / "routing" / str(recording.pk) / f"attempt_{attempt.ordinal}"
    try:
        outcome = routing_service.route_recording(
            config, recording, Path(source.path), attempt_dir,
        )
    except Exception as exc:  # unexpected: routing infrastructure failure
        logger.exception("routing failed for %s", recording.pk)
        attempt.outcome = AttemptOutcome.NONZERO_EXIT
        attempt.error_code = "routing_exception"
        attempt.error_message = type(exc).__name__
        attempt.finished_at = timezone.now()
        with transaction.atomic():
            recording = Recording.objects.select_for_update().get(pk=recording.pk)
            attempt.save()
            record_failure(recording, FailureStage.ROUTING, "routing_exception", "")
            recording.save()
        return {"recording_id": recording.pk, "result": "failed", "reason": "routing_exception"}
    finally:
        from workflow.services.audiosamples import cleanup_attempt_dir

        cleanup_attempt_dir(attempt_dir)

    attempt.outcome = AttemptOutcome.SUCCESS
    attempt.finished_at = timezone.now()
    attempt.save()
    applied = _apply_outcome(config, recording, outcome)
    return {
        "recording_id": recording.pk,
        "result": "routed" if applied["status"] == ProcessingStatus.READY_TO_TRANSCRIBE else "needs_review",
        "route": outcome.route,
        "confidence": outcome.confidence,
        "reason_code": outcome.reason_code,
        "status": applied["status"],
    }


def manual_route(recording: Recording, profile_name: str, confirmed_by: str = "cli") -> dict:
    """Append a manual routing decision.

    Eligible states: ``routing``, ``needs_review``, ``ready_to_transcribe``,
    ``transcribed``, and ``failed`` with ``failure_stage=routing``. A
    transcribing/hashing/discovered recording, or a failed transcription,
    cannot be routed (clean ConfigError).

    - Needs-review / routing / routing-failed recordings become
      ``ready_to_transcribe``.
    - ``ready_to_transcribe`` recordings selecting a different profile
      append the decision and remain ``ready_to_transcribe``.
    - Transcribed recordings selecting a DIFFERENT profile also become
      ``ready_to_transcribe`` (pending retranscription): the currently
      active transcript stays active until a new one succeeds.
    - Selecting the profile of the already-active decision is IDEMPOTENT:
      the active decision is verified in place and NO new decision row is
      appended. On a transcribed recording this confirms without
      retranscribing (safe, unsurprising default; use a different profile
      to trigger retranscription).
    """
    from brainlib.config import load_config

    config = load_config()
    profile = config.macwhisper.profile(profile_name)
    if profile is None:
        raise ConfigError(
            f"unknown routing profile '{profile_name}' "
            f"(available: {', '.join(sorted(config.macwhisper.routing.profiles))})"
        )
    with transaction.atomic():
        recording = Recording.objects.select_for_update().get(pk=recording.pk)
        routing_failed = (
            recording.processing_status == ProcessingStatus.FAILED
            and recording.failure_stage == FailureStage.ROUTING
        )
        eligible = recording.processing_status in (
            ProcessingStatus.ROUTING,
            ProcessingStatus.NEEDS_REVIEW,
            ProcessingStatus.READY_TO_TRANSCRIBE,
            ProcessingStatus.TRANSCRIBED,
        ) or routing_failed
        if not eligible:
            raise ConfigError(
                f"cannot route a recording in status '{recording.processing_status}'"
            )
        active_decision = RoutingDecision.objects.filter(recording=recording, is_active=True).first()

        if active_decision is not None and active_decision.profile_name == profile_name:
            # Same profile: idempotent — verify the active decision in
            # place, never append a duplicate decision row.
            if not active_decision.routing_verified:
                active_decision.routing_verified = True
                active_decision.verified_at = timezone.now()
                active_decision.verified_by = confirmed_by
                active_decision.save()
            if recording.processing_status in (
                ProcessingStatus.NEEDS_REVIEW,
                ProcessingStatus.ROUTING,
            ) or routing_failed:
                # Selecting the active profile from a review/failed-routing
                # limbo is an explicit go-ahead.
                recording.failure_stage = ""
                recording.retranscription_failed = False
                recording.processing_status = ProcessingStatus.READY_TO_TRANSCRIBE
                recording.save()
            return {
                "recording_id": recording.pk,
                "decision_id": active_decision.pk,
                "status": recording.processing_status,
                "result": "verified_no_retranscription",
            }

        RoutingDecision.objects.filter(recording=recording, is_active=True).update(is_active=False)
        last = RoutingDecision.objects.filter(recording=recording).order_by("-ordinal").first()
        decision = RoutingDecision.objects.create(
            recording=recording,
            ordinal=last.ordinal + 1 if last else 1,
            route_suggestion=profile_name,
            profile_name=profile_name,
            model_id=profile.model,
            language_arg=profile.language,
            method=RoutingMethod.MANUAL,
            confidence=None,
            reason_code="manual_selection",
            routing_verified=True,
            verified_at=timezone.now(),
            verified_by=confirmed_by,
            is_active=True,
        )
        if recording.processing_status != ProcessingStatus.READY_TO_TRANSCRIBE or routing_failed:
            # ready_to_transcribe recordings selecting a different profile
            # remain ready with the new decision appended.
            recording.failure_stage = ""
            recording.retranscription_failed = False
            recording.processing_status = ProcessingStatus.READY_TO_TRANSCRIBE
        recording.save()
    return {"recording_id": recording.pk, "decision_id": decision.pk, "status": recording.processing_status}


def confirm_routing(recording: Recording, confirmed_by: str = "cli") -> dict:
    """Verify the active decision in place, without retranscription."""
    with transaction.atomic():
        recording = Recording.objects.select_for_update().get(pk=recording.pk)
        decision = RoutingDecision.objects.filter(recording=recording, is_active=True).first()
        if decision is None:
            raise ConfigError("no active routing decision to confirm")
        decision.routing_verified = True
        decision.verified_at = timezone.now()
        decision.verified_by = confirmed_by
        decision.save()
    return {"recording_id": recording.pk, "decision_id": decision.pk, "verified": True}


def transcribe_ready(config: AppConfig, recording_ids: list[str] | None = None) -> list[dict]:
    """Transcribe all recordings in ``ready_to_transcribe``."""
    results: list[dict] = []
    recordings = Recording.objects.filter(processing_status=ProcessingStatus.READY_TO_TRANSCRIBE)
    if recording_ids:
        recordings = recordings.filter(id__in=recording_ids)
    for recording in recordings:
        results.append(transcribe_one(config, recording))
    return results


def transcribe_one(config: AppConfig, recording: Recording) -> dict:
    with transaction.atomic():
        recording = Recording.objects.select_for_update().get(pk=recording.pk)
        if recording.processing_status not in (
            ProcessingStatus.READY_TO_TRANSCRIBE,
            ProcessingStatus.TRANSCRIBED,
            ProcessingStatus.FAILED,
        ):
            return {"recording_id": recording.pk, "result": "skipped", "status": recording.processing_status}
        decision = RoutingDecision.objects.filter(recording=recording, is_active=True).first()
        if decision is None or not decision.model_id:
            return {"recording_id": recording.pk, "result": "skipped", "status": recording.processing_status}

    # Validate a usable source with an explicit outcome, before any
    # MacWhisper work; missing/changed/outside-inbox sources park the
    # recording cleanly (no attempt, state stays recoverable).
    source, status = validate_source_for_processing(config, recording)
    if source is None:
        return _skip_outcome(recording, status)

    had_active_transcript = recording.transcripts.filter(is_active=True).exists()
    if recording.processing_status == ProcessingStatus.TRANSCRIBED:
        transition(recording, ProcessingStatus.TRANSCRIBING)  # retranscription
    elif recording.processing_status == ProcessingStatus.FAILED:
        recording.failure_stage = ""
        recording.processing_status = ProcessingStatus.TRANSCRIBING
    else:
        transition(recording, ProcessingStatus.TRANSCRIBING)
    recording.save()

    attempt = transcription_service.transcribe_recording(
        config,
        recording,
        Path(source.path),
        model_id=decision.model_id,
        language_arg=decision.language_arg,
    )
    if attempt.outcome == AttemptOutcome.SUCCESS:
        return {
            "recording_id": recording.pk,
            "result": "transcribed",
            "attempt_id": attempt.pk,
            "retranscription": had_active_transcript,
        }
    return {
        "recording_id": recording.pk,
        "result": "failed",
        "error_code": attempt.error_code,
        "kept_active_transcript": had_active_transcript,
    }


def retry(config: AppConfig, recording: Recording, transport=None) -> dict:
    """Explicit retry of a failed recording, a failed retranscription, or
    a failed/interrupted summarization.

    Normal ``brain run`` never retries automatically; only this command
    reactivates the failed stage. Transcription-stage failures take
    precedence; a ``summary_status=failed`` recording (or a failed
    re-summarization, ``resummarization_failed``) is retried through the
    summarization path.
    """
    with transaction.atomic():
        recording = Recording.objects.select_for_update().get(pk=recording.pk)
        is_failed_retranscription = (
            recording.processing_status == ProcessingStatus.TRANSCRIBED
            and recording.retranscription_failed
        )
        is_failed_summary = (
            recording.processing_status == ProcessingStatus.TRANSCRIBED
            and not is_failed_retranscription
            and (recording.summary_status == SummaryState.FAILED or recording.resummarization_failed)
        )
        if (
            recording.processing_status != ProcessingStatus.FAILED
            and not is_failed_retranscription
            and not is_failed_summary
        ):
            return {
                "recording_id": recording.pk,
                "result": "skipped",
                "status": recording.processing_status,
            }
        if is_failed_summary:
            # Durable failure markers are NOT cleared here: the retry's
            # attempt becomes visible only when created, and success/
            # failure states are (re)asserted atomically by
            # summarize_one's persistence or failure path. If the process
            # dies before attempt creation, the old marker survives.
            regenerate = recording.resummarization_failed
        elif is_failed_retranscription:
            recording.failure_stage = ""
            recording.processing_status = ProcessingStatus.READY_TO_TRANSCRIBE
            recording.save()
        elif (recording.failure_stage or FailureStage.ROUTING) == FailureStage.ROUTING:
            transition(recording, ProcessingStatus.ROUTING)
            recording.save()
        else:
            recording.failure_stage = ""
            recording.processing_status = ProcessingStatus.READY_TO_TRANSCRIBE
            recording.save()
    if is_failed_summary:
        from workflow.services.summarize import summarize_one

        summary_result = summarize_one(
            config, Recording.objects.get(pk=recording.pk),
            regenerate=regenerate, transport=transport,
        )
        return {
            "recording_id": recording.pk,
            "result": "retried",
            "stage": "summarization",
            "summarize_result": summary_result,
            "summary_status": Recording.objects.get(pk=recording.pk).summary_status,
        }
    if recording.processing_status == ProcessingStatus.ROUTING:
        route_one(config, Recording.objects.get(pk=recording.pk))
    else:
        transcribe_one(config, Recording.objects.get(pk=recording.pk))
    refreshed = Recording.objects.get(pk=recording.pk)
    return {
        "recording_id": recording.pk,
        "result": "retried",
        "status": refreshed.processing_status,
    }


def run_pipeline(config: AppConfig) -> dict:
    """Compose recover -> ingest -> route -> transcribe -> summarize.

    Caller holds the lock. The summarization stage only processes
    never-attempted recordings (``summary_status=missing``); it never
    retries failed summaries or regenerates current ones.
    """
    from workflow.services.summarize import summarize_pending

    recovery = recover_interruptions(config)
    ingest_report = run_ingest(config)
    # Newly hashed recordings enter the routing stage.
    for recording in Recording.objects.filter(processing_status=ProcessingStatus.DISCOVERED):
        with transaction.atomic():
            recording = Recording.objects.select_for_update().get(pk=recording.pk)
            transition(recording, ProcessingStatus.ROUTING)
            recording.save()
    routing_results = route_pending(config)
    transcription_results = transcribe_ready(config)
    summarization_results = summarize_pending(config)
    return {
        "recovery": recovery,
        "ingest": ingest_report,
        "routing": routing_results,
        "transcription": transcription_results,
        "summarization": summarization_results,
    }


__all__ = [
    "PipelineBusy",
    "pipeline_lock",
    "recover_interruptions",
    "run_ingest",
    "run_pipeline",
    "route_pending",
    "route_one",
    "manual_route",
    "confirm_routing",
    "transcribe_ready",
    "transcribe_one",
    "retry",
    "derive_recorded_at",
]
