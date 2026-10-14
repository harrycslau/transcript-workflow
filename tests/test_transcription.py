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
    MAX_START_REGRESSION_MS,
    PARSER_VERSION,
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


JSON_VALIDATION_CASES = [
    # (expected category, stdout, expected zero-based segment ordinal or None)
    ("malformed_json", "{not json", None),
    ("invalid_envelope", '["segments"]', None),
    ("invalid_envelope", '{"text": "hello"}', None),
    ("invalid_segment_item", '{"segments": ["not-a-dict"], "text": "x"}', 0),
    (
        "invalid_segment_item",
        json.dumps({"segments": [{"start": 0, "end": 5, "text": "ok"}, "bad"], "text": "x"}),
        1,
    ),
    ("invalid_segment_text", '{"segments": [{"start": 0, "end": 5}], "text": "x"}', 0),
    (
        "invalid_segment_speaker",
        json.dumps({"segments": [{"start": 0, "end": 5, "text": "hi", "speaker": 7}], "text": "x"}),
        0,
    ),
    (
        "invalid_timestamp",
        json.dumps({"segments": [{"start": True, "end": 5, "text": "a"}], "text": "x"}),
        0,
    ),
    (
        "invalid_timestamp",
        json.dumps({"segments": [{"start": 0, "end": -3, "text": "a"}], "text": "x"}),
        0,
    ),
    (
        "end_before_start",
        json.dumps({"segments": [{"start": 10, "end": 5, "text": "a"}], "text": "x"}),
        0,
    ),
    (
        "non_monotonic_start",
        json.dumps(
            {"segments": [{"start": 4000, "end": 4100, "text": "a"}, {"start": 399, "end": 450, "text": "b"}], "text": "x"}
        ),
        1,
    ),
]

# Fixed allowlisted regression-size buckets stored for a rejected
# non-monotonic start (inclusive ranges; deltas > 2000 always).
ALLOWLISTED_REGRESSION_BUCKETS = {
    "2001_5000_ms",
    "over_5000_ms",
}

# (regression delta ms, expected bucket label) covering every boundary.
START_REGRESSION_BUCKET_CASES = [
    (2001, "2001_5000_ms"),
    (2002, "2001_5000_ms"),
    (5000, "2001_5000_ms"),
    (5001, "over_5000_ms"),
    (60000, "over_5000_ms"),
]


class TestJsonValidationDiagnostics:
    """Privacy-safe structural diagnostics for invalid full-transcription JSON."""

    def _run_full(self, tmp_path, stdout: str) -> ProcessingAttempt:
        config = make_config(tmp_path)
        recording, wav = make_recording(tmp_path)
        recording.processing_status = ProcessingStatus.TRANSCRIBING
        recording.save()

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        attempt = transcribe_recording(config, recording, wav, model_id="m", language_arg=None, runner=runner)
        attempt.refresh_from_db()
        return attempt

    @pytest.mark.parametrize("category,stdout,ordinal", JSON_VALIDATION_CASES)
    def test_structural_category_persisted_on_failed_attempt(self, tmp_path, category, stdout, ordinal):
        attempt = self._run_full(tmp_path, stdout)
        assert attempt.outcome == AttemptOutcome.INVALID_OUTPUT
        assert attempt.error_code == "invalid_mw_json"
        assert attempt.error_message == ""
        diagnostic = attempt.context_json["json_validation"]
        assert diagnostic["category"] == category
        if ordinal is None:
            assert "segment_ordinal" not in diagnostic
        else:
            assert diagnostic["segment_ordinal"] == ordinal
        # Only a rejected non-monotonic start may carry a regression bucket;
        # every other category must not gain one.
        if category == "non_monotonic_start":
            assert diagnostic["regression_bucket"] in ALLOWLISTED_REGRESSION_BUCKETS
        else:
            assert "regression_bucket" not in diagnostic
        # Category is always an allowlisted stable string shape.
        assert isinstance(diagnostic["category"], str)
        assert diagnostic["category"].islower()

    def test_successful_parse_adds_no_diagnostic(self, tmp_path):
        attempt = self._run_full(tmp_path, fixture_stdout())
        assert attempt.outcome == AttemptOutcome.SUCCESS
        assert "json_validation" not in (attempt.context_json or {})

    def test_sensitive_marker_absent_from_malformed_diagnostic(self, tmp_path):
        marker = "SENSITIVE_MARKER_7F2A91"
        attempt = self._run_full(tmp_path, '{"text": "x", "marker": "' + marker)
        assert attempt.error_code == "invalid_mw_json"
        assert attempt.error_message == ""
        assert marker not in json.dumps(attempt.context_json)
        assert marker not in attempt.error_message
        assert marker not in attempt.error_code

    def test_sensitive_marker_absent_from_segment_diagnostic(self, tmp_path):
        marker = "SENSITIVE_MARKER_7F2A91"
        stdout = json.dumps(
            {
                "segments": [{"start": marker, "end": 5, "text": "a", "marker": marker}],
                "text": "x",
            }
        )
        attempt = self._run_full(tmp_path, stdout)
        assert attempt.error_code == "invalid_mw_json"
        assert attempt.error_message == ""
        assert attempt.context_json["json_validation"] == {
            "category": "invalid_timestamp",
            "segment_ordinal": 0,
        }
        assert marker not in json.dumps(attempt.context_json)
        assert marker not in attempt.error_message
        assert marker not in attempt.error_code


class TestNonMonotonicStartRegressionBucket:
    """Privacy-safe regression-size bucket for a rejected non-monotonic start."""

    def _run_non_monotonic(self, tmp_path, running_max: int, start_ms: int, text: str = "x") -> ProcessingAttempt:
        stdout = json.dumps(
            {
                "segments": [
                    {"start": running_max, "end": running_max + 100, "text": "a"},
                    {"start": start_ms, "end": start_ms + 100, "text": "b"},
                ],
                "text": text,
            }
        )
        config = make_config(tmp_path)
        recording, wav = make_recording(tmp_path)
        recording.processing_status = ProcessingStatus.TRANSCRIBING
        recording.save()

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        attempt = transcribe_recording(config, recording, wav, model_id="m", language_arg=None, runner=runner)
        attempt.refresh_from_db()
        return attempt

    @pytest.mark.parametrize("delta_ms,expected_bucket", START_REGRESSION_BUCKET_CASES)
    def test_bucket_persisted_for_every_boundary(self, tmp_path, delta_ms, expected_bucket):
        running_max = 900_000
        attempt = self._run_non_monotonic(tmp_path, running_max, running_max - delta_ms)
        assert attempt.outcome == AttemptOutcome.INVALID_OUTPUT
        assert attempt.error_code == "invalid_mw_json"
        assert attempt.error_message == ""
        assert attempt.context_json["json_validation"] == {
            "category": "non_monotonic_start",
            "segment_ordinal": 1,
            "regression_bucket": expected_bucket,
        }

    def test_diagnostic_is_privacy_safe(self, tmp_path):
        running_max = 987_654
        start_ms = running_max - 4321
        secret = "SECRET_TRANSCRIPT_TEXT_3A"
        attempt = self._run_non_monotonic(tmp_path, running_max, start_ms, text=secret)
        diagnostic = attempt.context_json["json_validation"]
        # Exactly the allowlisted keys; only a fixed allowlisted bucket label.
        assert set(diagnostic) == {"category", "segment_ordinal", "regression_bucket"}
        assert diagnostic["regression_bucket"] in ALLOWLISTED_REGRESSION_BUCKETS
        # No exact timestamps, rejected value, delta or transcript text survive.
        serialized = json.dumps(attempt.context_json)
        assert str(running_max) not in serialized
        assert str(start_ms) not in serialized
        assert str(running_max + 100) not in serialized
        assert secret not in serialized
        assert attempt.error_message == ""
        assert attempt.error_code == "invalid_mw_json"


class TestStartRegressionTolerance:
    """MacWhisper 15.0.2 diarization start tolerance (running maximum)."""

    def _parse(self, starts, ends=None):
        ends = ends or [start + 100 for start in starts]
        raw = json.dumps(
            {
                "segments": [
                    {"start": s, "end": e, "text": f"t{i}"} for i, (s, e) in enumerate(zip(starts, ends))
                ],
                "text": "x",
            }
        )
        return parse_mw_json(raw)

    @pytest.mark.parametrize("regression_ms", [1, 500, 1000, 2000])
    def test_regression_within_tolerance_accepted(self, regression_ms):
        # Exactly MAX_START_REGRESSION_MS is accepted; the constant is 2000.
        assert MAX_START_REGRESSION_MS == 2000
        parsed = self._parse([5000, 5000 - regression_ms])
        assert parsed is not None
        assert [seg["start_ms"] for seg in parsed.segments] == [5000, 5000 - regression_ms]

    def test_regression_over_tolerance_rejected(self):
        assert self._parse([5000, 5000 - (MAX_START_REGRESSION_MS + 1)]) is None

    def test_cumulative_drift_versus_running_maximum_rejected(self):
        # Each step is only 500 ms behind its predecessor, but the running
        # maximum must prevent drift accumulating beyond the tolerance.
        assert self._parse([3000, 2500, 2000, 1500, 1000]) is not None
        assert self._parse([3000, 2500, 2000, 1500, 1000, 500]) is None

    def test_null_start_does_not_bypass_running_maximum(self):
        # A null start neither fails nor resets the running maximum: the
        # 400 ms start is still 2600 ms behind the accepted 3000 ms maximum.
        raw = json.dumps(
            {
                "segments": [
                    {"start": 3000, "end": 3100, "text": "a"},
                    {"start": None, "end": 3150, "text": "b"},
                    {"start": 400, "end": 500, "text": "c"},
                ],
                "text": "x",
            }
        )
        assert parse_mw_json(raw) is None

    def test_null_start_itself_accepted(self):
        raw = json.dumps(
            {
                "segments": [
                    {"start": 1000, "end": 1100, "text": "a"},
                    {"start": None, "end": 1150, "text": "b"},
                    {"start": 950, "end": 1000, "text": "c"},
                ],
                "text": "x",
            }
        )
        parsed = parse_mw_json(raw)
        assert parsed is not None
        assert [seg["start_ms"] for seg in parsed.segments] == [1000, None, 950]

    def test_equal_and_increasing_starts_accepted(self):
        parsed = self._parse([0, 0, 500, 500, 1200])
        assert parsed is not None
        assert [seg["start_ms"] for seg in parsed.segments] == [0, 0, 500, 500, 1200]

    def test_accepted_regression_preserves_order_ordinals_and_timestamps(self):
        parsed = self._parse([1000, 950, 960])
        assert parsed is not None
        assert [seg["ordinal"] for seg in parsed.segments] == [0, 1, 2]
        assert [seg["text"] for seg in parsed.segments] == ["t0", "t1", "t2"]
        assert [seg["start_ms"] for seg in parsed.segments] == [1000, 950, 960]
        assert [seg["end_ms"] for seg in parsed.segments] == [1100, 1050, 1060]

    def test_full_persistence_preserves_parser_version_order_and_raw_json(self, tmp_path):
        config = make_config(tmp_path)
        recording, wav = make_recording(tmp_path)
        recording.processing_status = ProcessingStatus.TRANSCRIBING
        recording.save()
        payload = {
            "segments": [
                {
                    "start": 1000,
                    "end": 1100,
                    "text": "a",
                    "id": 7,
                    "words": [{"start": 1000, "end": 1100, "text": "a"}],
                },
                {"start": 950, "end": 1050, "text": "b"},
            ],
            "text": "ab",
        }

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

        attempt = transcribe_recording(config, recording, wav, model_id="m", language_arg=None, runner=runner)
        assert attempt.outcome == AttemptOutcome.SUCCESS
        transcript = Transcript.objects.get(recording=recording)
        assert transcript.parser_version == PARSER_VERSION == "3"
        assert transcript.mw_json == payload
        segments = list(TranscriptSegment.objects.filter(transcript=transcript).order_by("ordinal"))
        assert [seg.ordinal for seg in segments] == [0, 1]
        assert [seg.text for seg in segments] == ["a", "b"]
        assert [seg.start_ms for seg in segments] == [1000, 950]
        assert [seg.end_ms for seg in segments] == [1100, 1050]
