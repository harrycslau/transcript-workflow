"""MacWhisper transcription execution and transcript persistence.

Subprocess safety:
- argv arrays only, never shell-interpolated strings
- per-run ``--model`` override; the globally selected MacWhisper model
  is never changed
- bounded timeouts scaled by audio duration (hard-capped by
  ``macwhisper.cli_timeout_seconds``)
- stdout size-capped; JSON validated fully before any database write
- transcript persistence is atomic: new transcript + segments + section
  are created and the previous active transcript deactivated in one
  transaction
- ``--overwrite`` is never used against canonical user files (output is
  read from stdout)
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from brainlib.config import AppConfig
from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    FailureStage,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
    Section,
    Transcript,
    TranscriptSegment,
)

logger = logging.getLogger(__name__)

PARSER_VERSION = "1"
STDOUT_CAP_BYTES = 32 * 1024 * 1024
DEFAULT_SAMPLE_TIMEOUT = 600
# Startup/model-load allowance: short audio never gets less than this.
MIN_TIMEOUT_SECONDS = 300
# Seconds of transcription budget per second of audio, plus startup slack.
TIMEOUT_FACTOR = 4.0
TIMEOUT_SLACK = 120


class TranscriptionError(Exception):
    """Sanitized transcription failure (no paths/secret content)."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


def build_mw_argv(
    config: AppConfig,
    audio_path: Path,
    model_id: str,
    language_arg: str | None,
    speakers: bool,
) -> list[str]:
    """Construct a safe argv for ``mw transcribe``. No shell, no secrets."""
    argv = [
        config.macwhisper.command,
        "transcribe",
        "--model",
        model_id,
        "--format",
        "json",
    ]
    if language_arg:
        argv += ["--language", language_arg]
    argv += ["--speakers"] if speakers else ["--no-speakers"]
    argv.append(str(audio_path))
    return argv


def timeout_for(duration_seconds: float | None, config: AppConfig, sample: bool = False) -> int:
    """Bounded transcription timeout.

    ``min(hard_cap, max(minimum, scaled))``: ``cli_timeout_seconds`` is
    the hard maximum cap; short audio gets at least MIN_TIMEOUT_SECONDS
    (not the full cap); normal audio scales with duration. Missing or
    zero duration falls back to the safe minimum. Sample runs use a
    separate smaller bound.
    """
    if sample:
        return min(DEFAULT_SAMPLE_TIMEOUT, config.macwhisper.cli_timeout_seconds) or DEFAULT_SAMPLE_TIMEOUT
    hard_cap = config.macwhisper.cli_timeout_seconds
    if duration_seconds and duration_seconds > 0:
        scaled = int(duration_seconds * TIMEOUT_FACTOR + TIMEOUT_SLACK)
    else:
        scaled = MIN_TIMEOUT_SECONDS
    return min(hard_cap, max(MIN_TIMEOUT_SECONDS, scaled))


def run_mw_transcription(
    config: AppConfig,
    audio_path: Path,
    model_id: str,
    language_arg: str | None,
    speakers: bool,
    runner=None,
    timeout_seconds: int | None = None,
) -> str | None:
    """Run ``mw transcribe`` and return the parsed ``text`` field.

    Returns None on any failure (non-zero exit, timeout, invalid JSON).
    Raises only programmer errors. Used for both samples and full runs;
    callers decide how failures map onto state.
    """
    argv = build_mw_argv(config, audio_path, model_id, language_arg, speakers)
    runner = runner or subprocess.run
    timeout = timeout_seconds or timeout_for(None, config, sample=True)
    try:
        result = runner(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("mw transcribe timed out for %s", audio_path.name)
        return None
    except OSError as exc:
        logger.warning("mw transcribe failed to start: %s", type(exc).__name__)
        return None
    if result.returncode != 0:
        # stderr may contain paths; log only its first line, error type only.
        first_line = (result.stderr or "").strip().splitlines()
        logger.warning(
            "mw transcribe exited %s for %s: %s",
            result.returncode, audio_path.name, first_line[0][:120] if first_line else "",
        )
        return None
    stdout = result.stdout or ""
    if len(stdout.encode("utf-8", errors="replace")) > STDOUT_CAP_BYTES:
        logger.warning("mw transcribe stdout exceeded cap for %s", audio_path.name)
        return None
    parsed = parse_mw_json(stdout)
    return None if parsed is None else parsed.text


@dataclass
class ParsedTranscript:
    text: str
    segments: list[dict]
    language_observed: str = ""


def parse_mw_json(raw: str) -> ParsedTranscript | None:
    """Validate MacWhisper JSON output (schema observed in MacWhisper 14.7.1):

    {"segments": [{"start": ms, "end": ms, "id": ..., "text": str,
                   "words": [{"start": ms, "end": ms, "text": str}]}],
     "text": str}

    Timestamps must be finite, non-negative numbers (booleans rejected)
    with ``end >= start``; segment starts must be non-decreasing.
    Slight overlap between segments is tolerated (real MacWhisper
    output may overlap by a few ms), but regressions in start times are
    rejected.
    """
    import math

    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    segments_raw = payload.get("segments")
    text = payload.get("text")
    if not isinstance(segments_raw, list) or not isinstance(text, str):
        return None
    segments: list[dict] = []
    previous_start: float | None = None
    for index, item in enumerate(segments_raw):
        if not isinstance(item, dict):
            return None
        seg_text = item.get("text")
        if not isinstance(seg_text, str):
            return None
        speaker = item.get("speaker")
        if speaker is not None and not isinstance(speaker, str):
            return None

        def valid_timestamp(value) -> float | None:
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            number = float(value)
            if not math.isfinite(number) or number < 0:
                return None
            return number

        start = valid_timestamp(item.get("start"))
        end = valid_timestamp(item.get("end"))
        if item.get("start") is not None and start is None:
            return None
        if item.get("end") is not None and end is None:
            return None
        if start is not None and end is not None and end < start:
            return None
        if start is not None and previous_start is not None and start < previous_start:
            return None  # out-of-order segments
        previous_start = start
        segments.append(
            {
                "ordinal": index,
                "start_ms": int(start) if start is not None else None,
                "end_ms": int(end) if end is not None else None,
                "speaker": speaker or "",
                "text": seg_text,
            }
        )
    return ParsedTranscript(text=text, segments=segments)


def sanitize_error(text: str, limit: int = 200) -> str:
    """Strip filesystem paths and cap length for stored error messages."""
    cleaned = re.sub(r"(?<![\w])/[\w./\\-]+", "<path>", text or "")
    return cleaned.strip()[:limit]


def next_ordinal(recording: Recording, stage: str) -> int:
    last = (
        ProcessingAttempt.objects.filter(recording=recording, stage=stage)
        .order_by("-ordinal")
        .first()
    )
    return (last.ordinal + 1) if last else 1


def get_mw_version(config: AppConfig, runner=None) -> str:
    runner = runner or subprocess.run
    try:
        result = runner(
            [config.macwhisper.command, "version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip().splitlines()[0][:64] if (result.stdout or "").strip() else ""


def transcribe_recording(
    config: AppConfig,
    recording: Recording,
    source_path: Path,
    model_id: str,
    language_arg: str | None,
    runner=None,
) -> ProcessingAttempt:
    """Run full transcription for ``recording`` and persist versioned output.

    State semantics (see statemachine.record_failure):
    - success: new Transcript (active) + segments + one whole-recording
      Section; status -> transcribed.
    - failure with an existing active transcript: status stays
      ``transcribed``; the failed attempt is recorded (retranscription
      warning); retry stays explicit.
    - failure without an active transcript: status -> failed,
      failure_stage=transcription.
    """
    attempt = ProcessingAttempt.objects.create(
        recording=recording,
        stage=AttemptStage.TRANSCRIPTION,
        ordinal=next_ordinal(recording, AttemptStage.TRANSCRIPTION),
        model_id=model_id,
        language_arg=language_arg,
    )
    argv = build_mw_argv(config, Path(source_path), model_id, language_arg, config.macwhisper.speakers)
    attempt.cli_args_json = argv
    attempt.mw_version = get_mw_version(config)
    attempt.save(update_fields=["cli_args_json", "mw_version"])

    duration = recording.duration_seconds
    timeout = timeout_for(duration, config)
    try:
        runner = runner or subprocess.run
        result = runner(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _finish_failure(attempt, recording, AttemptOutcome.TIMEOUT, "timeout", "")
    except OSError as exc:
        return _finish_failure(attempt, recording, AttemptOutcome.NONZERO_EXIT, "mw_unreachable", type(exc).__name__)

    attempt.exit_code = result.returncode
    if result.returncode != 0:
        first_line = (result.stderr or "").strip().splitlines()
        return _finish_failure(
            attempt, recording, AttemptOutcome.NONZERO_EXIT, "mw_nonzero_exit",
            sanitize_error(first_line[0]) if first_line else "",
        )

    stdout = result.stdout or ""
    if len(stdout.encode("utf-8", errors="replace")) > STDOUT_CAP_BYTES:
        return _finish_failure(attempt, recording, AttemptOutcome.INVALID_OUTPUT, "stdout_too_large", "")
    parsed = parse_mw_json(stdout)
    if parsed is None:
        return _finish_failure(attempt, recording, AttemptOutcome.INVALID_OUTPUT, "invalid_mw_json", "")

    _persist_transcript(attempt, recording, parsed, stdout)
    return attempt


def _finish_failure(
    attempt: ProcessingAttempt,
    recording: Recording,
    outcome: str,
    error_code: str,
    message: str,
) -> ProcessingAttempt:
    from workflow.services.statemachine import record_failure

    attempt.outcome = outcome
    attempt.error_code = error_code
    attempt.error_message = message
    attempt.finished_at = timezone.now()
    with transaction.atomic():
        recording = Recording.objects.select_for_update().get(pk=recording.pk)
        attempt.save()
        record_failure(recording, FailureStage.TRANSCRIPTION, error_code, message, attempt=attempt)
        recording.save()
    return attempt


def _persist_transcript(
    attempt: ProcessingAttempt,
    recording: Recording,
    parsed: ParsedTranscript,
    raw_json: str,
) -> None:
    now = timezone.now()
    with transaction.atomic():
        recording = Recording.objects.select_for_update().get(pk=recording.pk)
        transcript = Transcript.objects.create(
            recording=recording,
            attempt=attempt,
            is_active=False,
            text_normalized=parsed.text,
            mw_json=json.loads(raw_json),
            parser_version=PARSER_VERSION,
            language_observed=parsed.language_observed,
        )
        TranscriptSegment.objects.bulk_create(
            [
                TranscriptSegment(
                    transcript=transcript,
                    ordinal=seg["ordinal"],
                    start_ms=seg["start_ms"],
                    end_ms=seg["end_ms"],
                    speaker=seg["speaker"],
                    text=seg["text"],
                )
                for seg in parsed.segments
            ]
        )
        # One whole-recording Section per successful transcript.
        Section.objects.create(
            transcript=transcript,
            ordinal=0,
            title="Full recording",
            start_ms=parsed.segments[0]["start_ms"] if parsed.segments else None,
            end_ms=parsed.segments[-1]["end_ms"] if parsed.segments else None,
        )
        previous = Transcript.objects.filter(recording=recording, is_active=True).first()
        if previous is not None:
            previous.is_active = False
            previous.superseded_at = now
            previous.save()
        transcript.is_active = True
        transcript.activated_at = now
        transcript.save()

        recording.processing_status = ProcessingStatus.TRANSCRIBED
        recording.failure_stage = ""
        recording.retranscription_failed = False
        recording.last_failed_attempt = None
        recording.save()
        attempt.outcome = AttemptOutcome.SUCCESS
        attempt.finished_at = now
        attempt.save()
