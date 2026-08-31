"""Tests for MacWhisper subprocess handling and transcript persistence."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
    Section,
    Transcript,
    TranscriptSegment,
)
from workflow.services.transcription import (
    build_mw_argv,
    parse_mw_json,
    timeout_for,
    transcribe_recording,
)

from factories import make_config

pytestmark = pytest.mark.django_db

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "macwhisper" / "parakeet_json.json"


def fixture_json() -> dict:
    return json.loads(FIXTURE.read_text())["example"]


def fixture_stdout() -> str:
    return json.dumps(fixture_json())


def make_recording(tmp_path) -> tuple[Recording, Path]:
    wav = tmp_path / "audio" / "rec.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"RIFFfake")
    recording = Recording.objects.create(sha256=f"tr-{Recording.objects.count()}", duration_seconds=120.0)
    from workflow.models import AudioSource

    AudioSource.objects.create(
        recording=recording,
        path=str(wav),
        path_identity=str(wav).casefold(),
        original_filename=wav.name,
        presence="present",
        is_canonical=True,
        discovery_state="hashed",
    )
    return recording, wav


class TestArgvConstruction:
    def test_argv_is_array_never_shell_string(self, tmp_path):
        config = make_config(tmp_path)
        argv = build_mw_argv(config, Path("/tmp/a.wav"), "apple:zh-HK", None, speakers=True)
        assert isinstance(argv, list)
        assert argv[0] == config.macwhisper.command
        assert "transcribe" in argv
        assert argv[argv.index("--model") + 1] == "apple:zh-HK"
        assert "--format" in argv and argv[argv.index("--format") + 1] == "json"
        # No language flag when language_arg is None (per CLI validation).
        assert "--language" not in argv
        assert "--speakers" in argv

    def test_language_flag_only_when_configured(self, tmp_path):
        config = make_config(tmp_path)
        argv = build_mw_argv(config, Path("/tmp/a.wav"), "m", "auto", speakers=False)
        assert argv[argv.index("--language") + 1] == "auto"
        assert "--no-speakers" in argv

    def test_no_overwrite_flag_against_user_files(self, tmp_path):
        config = make_config(tmp_path)
        argv = build_mw_argv(config, Path("/tmp/a.wav"), "m", None, speakers=True)
        assert "--overwrite" not in argv
        assert "--output-dir" not in argv


class TestTimeoutPolicy:
    def test_short_audio_gets_minimum_not_cap(self, tmp_path):
        config = make_config(tmp_path, cli_timeout_seconds=7200)
        assert timeout_for(10.0, config) == 300  # minimum, not two hours

    def test_normal_audio_scales(self, tmp_path):
        config = make_config(tmp_path, cli_timeout_seconds=7200)
        assert timeout_for(1000.0, config) == int(1000 * 4.0 + 120)

    def test_very_long_audio_never_exceeds_cap(self, tmp_path):
        config = make_config(tmp_path, cli_timeout_seconds=7200)
        assert timeout_for(100000.0, config) == 7200  # hard cap

    def test_missing_duration_uses_safe_default(self, tmp_path):
        config = make_config(tmp_path, cli_timeout_seconds=7200)
        assert timeout_for(None, config) == 300
        assert timeout_for(0, config) == 300

    def test_low_cap_is_respected_for_long_audio(self, tmp_path):
        config = make_config(tmp_path, cli_timeout_seconds=900)
        assert timeout_for(100000.0, config) == 900

    def test_sample_timeout_bounded(self, tmp_path):
        config = make_config(tmp_path, cli_timeout_seconds=7200)
        assert timeout_for(None, config, sample=True) == 600


class TestJsonParsing:
    def test_valid_fixture_parses(self):
        parsed = parse_mw_json(fixture_stdout())
        assert parsed is not None
        assert isinstance(parsed.text, str)
        assert parsed.segments
        first = parsed.segments[0]
        assert set(first) == {"ordinal", "start_ms", "end_ms", "speaker", "text"}

    def test_invalid_json_returns_none(self):
        assert parse_mw_json("not json {") is None

    def test_partial_json_returns_none(self):
        raw = fixture_stdout()[: len(fixture_stdout()) // 2]
        assert parse_mw_json(raw) is None

    def test_non_object_rejected(self):
        assert parse_mw_json('["segments"]') is None

    def test_missing_segments_rejected(self):
        assert parse_mw_json('{"text": "hello"}') is None

    def test_segment_missing_text_rejected(self):
        assert parse_mw_json('{"segments": [{"start": 0}], "text": "x"}') is None

    def test_speaker_null_handled(self):
        raw = json.dumps(
            {"segments": [{"start": 0, "end": 10, "text": "hi", "speaker": None}], "text": "hi"}
        )
        parsed = parse_mw_json(raw)
        assert parsed.segments[0]["speaker"] == ""


class TestFullTranscription:
    def _ok_runner(self, stdout: str, calls: list):
        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        return runner

    def test_success_creates_transcript_segments_section(self, tmp_path):
        config = make_config(tmp_path)
        recording, wav = make_recording(tmp_path)
        recording.processing_status = ProcessingStatus.TRANSCRIBING
        recording.save()
        calls: list = []
        attempt = transcribe_recording(
            config, recording, wav, model_id="parakeet-pro:nvidia_parakeet-v3",
            language_arg=None, runner=self._ok_runner(fixture_stdout(), calls),
        )
        assert attempt.outcome == AttemptOutcome.SUCCESS
        transcript = Transcript.objects.get(recording=recording)
        assert transcript.is_active is True
        assert transcript.attempt_id == attempt.pk
        assert transcript.segments.count() == len(fixture_json()["segments"])
        sections = Section.objects.filter(transcript=transcript)
        assert sections.count() == 1  # exactly one whole-recording Section
        assert sections.get().title == "Full recording"
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED
        # Safe argv recorded for provenance.
        assert isinstance(attempt.cli_args_json, list)
        assert "--model" in attempt.cli_args_json

    def test_retranscription_versions_and_swaps_active(self, tmp_path):
        config = make_config(tmp_path)
        recording, wav = make_recording(tmp_path)
        recording.processing_status = ProcessingStatus.TRANSCRIBING
        recording.save()
        transcribe_recording(
            config, recording, wav, model_id="m1", language_arg=None,
            runner=self._ok_runner(fixture_stdout(), []),
        )
        first = Transcript.objects.get(recording=recording, is_active=True)

        # Retranscribe with different output.
        alt = json.dumps({"segments": [{"start": 0, "end": 5, "text": "v2"}], "text": "v2"})
        recording.processing_status = ProcessingStatus.TRANSCRIBING
        recording.save()
        transcribe_recording(
            config, recording, wav, model_id="m2", language_arg=None,
            runner=self._ok_runner(alt, []),
        )
        assert Transcript.objects.filter(recording=recording).count() == 2
        active = Transcript.objects.get(recording=recording, is_active=True)
        assert active.text_normalized == "v2"
        first.refresh_from_db()
        assert first.is_active is False
        assert first.superseded_at is not None
        # Old segments preserved.
        assert first.segments.exists()
        assert Section.objects.filter(transcript=first).count() == 1

    def test_nonzero_exit_fails_without_transcript(self, tmp_path):
        config = make_config(tmp_path)
        recording, wav = make_recording(tmp_path)
        recording.processing_status = ProcessingStatus.TRANSCRIBING
        recording.save()

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Error: boom")

        attempt = transcribe_recording(config, recording, wav, model_id="m", language_arg=None, runner=runner)
        assert attempt.outcome == AttemptOutcome.NONZERO_EXIT
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.FAILED
        assert recording.failure_stage == "transcription"
        assert Transcript.objects.count() == 0

    def test_timeout_fails(self, tmp_path):
        config = make_config(tmp_path)
        recording, wav = make_recording(tmp_path)
        recording.processing_status = ProcessingStatus.TRANSCRIBING
        recording.save()

        def runner(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd="mw", timeout=10)

        attempt = transcribe_recording(config, recording, wav, model_id="m", language_arg=None, runner=runner)
        assert attempt.outcome == AttemptOutcome.TIMEOUT
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.FAILED

    def test_invalid_json_fails(self, tmp_path):
        config = make_config(tmp_path)
        recording, wav = make_recording(tmp_path)
        recording.processing_status = ProcessingStatus.TRANSCRIBING
        recording.save()
        attempt = transcribe_recording(
            config, recording, wav, model_id="m", language_arg=None,
            runner=self._ok_runner("{partial", []),
        )
        assert attempt.outcome == AttemptOutcome.INVALID_OUTPUT
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.FAILED

    def test_failed_retranscription_keeps_active_transcript(self, tmp_path):
        """Requirement 3: failed retranscription must not invalidate a
        successful active transcript; status stays transcribed."""
        config = make_config(tmp_path)
        recording, wav = make_recording(tmp_path)
        recording.processing_status = ProcessingStatus.TRANSCRIBING
        recording.save()
        transcribe_recording(
            config, recording, wav, model_id="m", language_arg=None,
            runner=self._ok_runner(fixture_stdout(), []),
        )
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED

        # Now a retranscription that fails.
        recording.processing_status = ProcessingStatus.TRANSCRIBING
        recording.save()
        attempt = transcribe_recording(
            config, recording, wav, model_id="m", language_arg=None,
            runner=self._ok_runner("{broken", []),
        )
        assert attempt.outcome == AttemptOutcome.INVALID_OUTPUT
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED
        active = Transcript.objects.get(recording=recording, is_active=True)
        assert active.text_normalized == fixture_json()["text"]

    def test_error_messages_sanitized(self, tmp_path):
        config = make_config(tmp_path)
        recording, wav = make_recording(tmp_path)
        recording.processing_status = ProcessingStatus.TRANSCRIBING
        recording.save()

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 3, stdout="", stderr="Error: /Users/harry/secret/path.wav bad")

        attempt = transcribe_recording(config, recording, wav, model_id="m", language_arg=None, runner=runner)
        stored = attempt.error_message
        assert "/Users/harry" not in stored
