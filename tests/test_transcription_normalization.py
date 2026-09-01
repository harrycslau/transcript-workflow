"""MacWhisper stderr extraction, input normalization, speakers fallback.

Synthetic fixtures only; no real MacWhisper, network, or user audio.
"""

from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

import pytest

from workflow.models import AttemptOutcome, Recording
from workflow.services import transcription as transcription_module
from workflow.services.audiosamples import SampleExtractionError
from workflow.services.transcription import (
    ERROR_DETAIL_CAP,
    categorize_mw_error,
    extract_mw_error,
    transcribe_recording,
)

from factories import make_config

pytestmark = pytest.mark.django_db

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "macwhisper" / "parakeet_json.json"
STDOUT = json.dumps(json.loads(FIXTURE.read_text())["example"])

DIARIZATION_STDERR = (
    "Transcribing outlive2024.mp3...\n"
    "Error: The model 'apple:zh-HK' does not support speaker detection (diarization). "
    "Remove --speakers, or choose a model that supports it (run 'mw models').\n"
)


class TestExtractMwError:
    def test_progress_line_is_never_the_error(self):
        code, detail = extract_mw_error(DIARIZATION_STDERR)
        assert code == "mw_speakers_failure"
        assert "Transcribing outlive2024.mp3" not in detail
        assert detail.startswith("Error:")

    def test_error_line_with_following_diagnostics(self):
        stderr = (
            "Transcribing x.wav...\n"
            "Error: Could not connect to MacWhisper.\n"
            "Connecting to /Users/x/Library/cli.sock was refused.\n"
        )
        code, detail = extract_mw_error(stderr)
        assert code == "mw_connection_failure"
        assert "was refused" in detail
        assert "/Users/x/Library" not in detail  # paths sanitized

    def test_fallback_to_last_line_when_no_error_prefix(self):
        stderr = "Transcribing x.wav...\nSomething went wrong\n"
        code, detail = extract_mw_error(stderr)
        assert detail == "Something went wrong"

    def test_empty_stderr(self):
        assert extract_mw_error("") == ("mw_nonzero_exit", "")
        assert extract_mw_error(None) == ("mw_nonzero_exit", "")

    def test_detail_capped(self):
        stderr = "Error: " + ("x" * 5000)
        _code, detail = extract_mw_error(stderr)
        assert len(detail) <= ERROR_DETAIL_CAP

    def test_paths_sanitized(self):
        stderr = "Error: cannot open /Users/harry/secret/inbox/file.mp3 for reading"
        _code, detail = extract_mw_error(stderr)
        assert "/Users/harry" not in detail
        assert "<path>" in detail


class TestCategorize:
    def test_connection(self):
        text = "Error: Could not connect to MacWhisper (Operation not permitted)."
        assert categorize_mw_error(text) == "mw_connection_failure"

    def test_speakers(self):
        assert categorize_mw_error(DIARIZATION_STDERR.splitlines()[1]) == "mw_speakers_failure"

    def test_input_unreadable(self):
        assert categorize_mw_error("Error: unsupported input format") == "mw_input_unreadable"

    def test_generic(self):
        assert categorize_mw_error("Error: something unexpected") == "mw_nonzero_exit"


def _fake_mp3(tmp_path) -> tuple[Recording, Path]:
    from workflow.models import AudioSource

    source = tmp_path / "data" / "inbox" / "rec.mp3"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"fake mp3 bytes")
    recording = Recording.objects.create(
        sha256=f"norm-{Recording.objects.count()}", duration_seconds=120.0
    )
    AudioSource.objects.create(
        recording=recording,
        path=str(source),
        path_identity=str(source).casefold(),
        original_filename=source.name,
        presence="present",
        is_canonical=True,
        discovery_state="hashed",
    )
    return recording, source


def _real_wav(path: Path, seconds: float = 2.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x01\x02" * int(16000 * seconds))
    return path


class _Result:
    def __init__(self, returncode=0, stdout=STDOUT, stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestNormalization:
    def test_non_pcm_input_normalized_original_untouched_temp_cleaned(self, tmp_path, monkeypatch):
        recording, source = _fake_mp3(tmp_path)
        original_bytes = source.read_bytes()
        conversions = []

        def fake_convert(source_path, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(dest), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\x01\x02" * 32000)
            conversions.append(source_path)
            return dest

        monkeypatch.setattr("workflow.services.audiosamples.convert_to_pcm_16k", fake_convert)
        seen_argv = []

        def runner(argv, capture_output, text, timeout):
            seen_argv.append(argv)
            return _Result()

        config = make_config(tmp_path, normalize_input=True)
        attempt = transcribe_recording(
            config, recording, source, model_id="apple:zh-HK", language_arg=None,
            runner=runner, source_info={"source_audio_source_id": 4242},
        )
        assert attempt.outcome == AttemptOutcome.SUCCESS
        # The normalized temp file, not the compressed original, reached mw.
        argv = seen_argv[0]
        passed_path = Path(argv[-1])
        assert passed_path.name == "normalized.wav"
        assert "transcription" in passed_path.parts
        assert str(source) not in argv
        # The original was never touched.
        assert source.read_bytes() == original_bytes
        assert conversions == [source]
        # Provenance.
        context = attempt.context_json
        assert context["input"]["normalized"] is True
        assert context["input"]["source_format"] == "mp3"
        assert context["input"]["source_audio_source_id"]
        # cli_args_json keeps its historical argv list shape.
        assert isinstance(attempt.cli_args_json, list)
        # Temp cleanup on success.
        assert not passed_path.exists()
        assert not passed_path.parent.exists()

    def test_pcm_wav_used_directly_no_conversion(self, tmp_path, monkeypatch):
        from workflow.models import AudioSource

        source = _real_wav(tmp_path / "data" / "inbox" / "rec.wav")
        recording = Recording.objects.create(
            sha256=f"normwav-{Recording.objects.count()}", duration_seconds=2.0
        )
        AudioSource.objects.create(
            recording=recording,
            path=str(source),
            path_identity=str(source).casefold(),
            original_filename=source.name,
            presence="present",
            is_canonical=True,
            discovery_state="hashed",
        )

        def fail_convert(*args, **kwargs):
            raise AssertionError("PCM WAV must not be converted")

        monkeypatch.setattr("workflow.services.audiosamples.convert_to_pcm_16k", fail_convert)
        seen = []

        def runner(argv, capture_output, text, timeout):
            seen.append(argv)
            return _Result()

        config = make_config(tmp_path, normalize_input=True)
        attempt = transcribe_recording(
            config, recording, source, model_id="apple:zh-HK", language_arg=None, runner=runner
        )
        assert attempt.outcome == AttemptOutcome.SUCCESS
        assert seen[0][-1] == str(source)
        assert attempt.context_json["input"]["normalized"] is False

    def test_normalize_input_false_passes_original(self, tmp_path):
        recording, source = _fake_mp3(tmp_path)
        seen = []

        def runner(argv, capture_output, text, timeout):
            seen.append(argv)
            return _Result()

        config = make_config(tmp_path, normalize_input=False)
        attempt = transcribe_recording(
            config, recording, source, model_id="apple:zh-HK", language_arg=None, runner=runner
        )
        assert attempt.outcome == AttemptOutcome.SUCCESS
        assert seen[0][-1] == str(source)
        assert attempt.context_json["input"]["normalized"] is False

    def test_conversion_failure_is_a_clean_normalization_failed(self, tmp_path, monkeypatch):
        recording, source = _fake_mp3(tmp_path)

        def fail_convert(source_path, dest):
            raise SampleExtractionError("afconvert_failed")

        monkeypatch.setattr("workflow.services.audiosamples.convert_to_pcm_16k", fail_convert)
        seen = []

        def runner(argv, capture_output, text, timeout):
            seen.append(argv)
            return _Result()

        config = make_config(tmp_path, normalize_input=True)
        attempt = transcribe_recording(
            config, recording, source, model_id="apple:zh-HK", language_arg=None, runner=runner
        )
        assert attempt.outcome == AttemptOutcome.NONZERO_EXIT
        assert attempt.error_code == "normalization_failed"
        assert seen == []  # mw never invoked on an unverified path

    def test_temp_cleanup_on_nonzero_exit(self, tmp_path, monkeypatch):
        recording, source = _fake_mp3(tmp_path)
        temp_root = Path(make_config(tmp_path).storage.temp) / "transcription"

        def fake_convert(source_path, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(dest), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\x01\x02" * 32000)
            return dest

        monkeypatch.setattr("workflow.services.audiosamples.convert_to_pcm_16k", fake_convert)

        def runner(argv, capture_output, text, timeout):
            return _Result(returncode=1, stdout="", stderr=DIARIZATION_STDERR)

        config = make_config(tmp_path, normalize_input=True)
        attempt = transcribe_recording(
            config, recording, source, model_id="apple:zh-HK", language_arg=None, runner=runner
        )
        assert attempt.outcome == AttemptOutcome.NONZERO_EXIT
        assert list(temp_root.glob("**/*")) == []

    def test_temp_cleanup_on_timeout(self, tmp_path, monkeypatch):
        import subprocess

        recording, source = _fake_mp3(tmp_path)

        def fake_convert(source_path, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(dest), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\x01\x02" * 32000)
            return dest

        monkeypatch.setattr("workflow.services.audiosamples.convert_to_pcm_16k", fake_convert)

        def runner(argv, capture_output, text, timeout):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

        config = make_config(tmp_path, normalize_input=True)
        attempt = transcribe_recording(
            config, recording, source, model_id="apple:zh-HK", language_arg=None, runner=runner
        )
        assert attempt.outcome == AttemptOutcome.TIMEOUT
        temp_root = Path(config.storage.temp) / "transcription"
        assert list(temp_root.glob("**/*")) == []


class TestSpeakersFallback:
    def test_fallback_disabled_by_default_single_run(self, tmp_path):
        recording, source = _fake_mp3(tmp_path)
        seen = []

        def runner(argv, capture_output, text, timeout):
            seen.append(argv)
            return _Result(returncode=1, stdout="", stderr=DIARIZATION_STDERR)

        config = make_config(tmp_path, speakers_fallback=False)
        attempt = transcribe_recording(
            config, recording, source, model_id="apple:zh-HK", language_arg=None, runner=runner
        )
        assert attempt.outcome == AttemptOutcome.NONZERO_EXIT
        assert attempt.error_code == "mw_speakers_failure"
        assert "speaker detection" in attempt.error_message
        assert len(seen) == 1
        assert "--speakers" in seen[0]

    def test_fallback_retries_once_without_speakers_and_records_both_runs(self, tmp_path):
        recording, source = _fake_mp3(tmp_path)
        seen = []

        def runner(argv, capture_output, text, timeout):
            seen.append(argv)
            if "--speakers" in argv:
                return _Result(returncode=1, stdout="", stderr=DIARIZATION_STDERR)
            return _Result()

        config = make_config(tmp_path, speakers_fallback=True)
        attempt = transcribe_recording(
            config, recording, source, model_id="apple:zh-HK", language_arg=None, runner=runner
        )
        assert attempt.outcome == AttemptOutcome.SUCCESS
        assert [("--speakers" in argv, "--no-speakers" in argv) for argv in seen] == [(True, False), (False, True)]
        context = attempt.context_json
        assert context["speakers_fallback"] is True
        assert len(context["runs"]) == 2
        assert context["runs"][0]["speakers"] is True
        assert context["runs"][0]["error_code"] == "mw_speakers_failure"
        assert context["runs"][1]["speakers"] is False
        assert context["runs"][1]["outcome"] == AttemptOutcome.SUCCESS

    def test_no_fallback_on_generic_error(self, tmp_path):
        recording, source = _fake_mp3(tmp_path)
        seen = []

        def runner(argv, capture_output, text, timeout):
            seen.append(argv)
            return _Result(returncode=1, stdout="", stderr="Error: something unexpected\n")

        config = make_config(tmp_path, speakers_fallback=True)
        attempt = transcribe_recording(
            config, recording, source, model_id="apple:zh-HK", language_arg=None, runner=runner
        )
        assert attempt.outcome == AttemptOutcome.NONZERO_EXIT
        assert attempt.error_code == "mw_nonzero_exit"
        assert len(seen) == 1

    def test_no_fallback_when_speakers_not_requested(self, tmp_path):
        from workflow.models import AudioSource

        source = _real_wav(tmp_path / "data" / "inbox" / "rec.wav")
        recording = Recording.objects.create(
            sha256=f"ns-{Recording.objects.count()}", duration_seconds=2.0
        )
        AudioSource.objects.create(
            recording=recording,
            path=str(source),
            path_identity=str(source).casefold(),
            original_filename=source.name,
            presence="present",
            is_canonical=True,
            discovery_state="hashed",
        )
        seen = []

        def runner(argv, capture_output, text, timeout):
            seen.append(argv)
            return _Result(returncode=1, stdout="", stderr=DIARIZATION_STDERR)

        config = make_config(tmp_path, speakers_fallback=True)
        # Override the speakers setting: config-level False means the
        # diarization failure cannot be the requested mode failing.
        config = config.__class__(
            config_path=config.config_path,
            storage=config.storage,
            macwhisper=config.macwhisper.__class__(
                command=config.macwhisper.command,
                model=config.macwhisper.model,
                language=config.macwhisper.language,
                speakers=False,
                normalize_input=config.macwhisper.normalize_input,
                speakers_fallback=True,
                output_format=config.macwhisper.output_format,
                file_stable_seconds=config.macwhisper.file_stable_seconds,
                cli_timeout_seconds=config.macwhisper.cli_timeout_seconds,
                routing=config.macwhisper.routing,
                legacy_model_notice=config.macwhisper.legacy_model_notice,
            ),
            llm=config.llm,
            embedding=config.embedding,
            retention=config.retention,
            summarization=config.summarization,
            tags=config.tags,
            initial_tags=config.initial_tags,
            timezone=config.timezone,
        )
        attempt = transcribe_recording(
            config, recording, source, model_id="apple:zh-HK", language_arg=None, runner=runner
        )
        assert len(seen) == 1
        assert "--no-speakers" in seen[0]

    def test_cli_args_json_shape_unchanged(self, tmp_path, monkeypatch):
        recording, source = _fake_mp3(tmp_path)

        def fake_convert(source_path, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(dest), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\x01\x02" * 32000)
            return dest

        monkeypatch.setattr("workflow.services.audiosamples.convert_to_pcm_16k", fake_convert)

        def runner(argv, capture_output, text, timeout):
            return _Result()

        config = make_config(tmp_path, normalize_input=True)
        attempt = transcribe_recording(
            config, recording, source, model_id="apple:zh-HK", language_arg=None, runner=runner
        )
        assert isinstance(attempt.cli_args_json, list)
        assert "--model" in attempt.cli_args_json


class TestSpeakersFallbackSurfacing:
    def test_transcribe_one_reports_visible_degradation(self, tmp_path, monkeypatch):
        """transcribe_one surfaces the fallback warning; both runs stay
        recorded in the attempt context."""
        from workflow.models import AttemptStage, ProcessingAttempt, AudioSource, ProcessingStatus, RoutingDecision
        from workflow.services.ingest import sha256_file
        from workflow.services.pipeline import transcribe_one

        source = _real_wav(tmp_path / "data" / "inbox" / "rec.wav")
        recording = Recording.objects.create(
            sha256=sha256_file(source),
            duration_seconds=2.0,
            processing_status=ProcessingStatus.READY_TO_TRANSCRIBE,
        )
        AudioSource.objects.create(
            recording=recording,
            path=str(source),
            path_identity=str(source).casefold(),
            original_filename=source.name,
            presence="present",
            is_canonical=True,
            discovery_state="hashed",
            file_size=source.stat().st_size,
            file_mtime=source.stat().st_mtime,
        )
        RoutingDecision.objects.create(
            recording=recording,
            ordinal=1,
            route_suggestion="cantonese",
            profile_name="cantonese",
            model_id="apple:zh-HK",
            method="manual",
            is_active=True,
        )
        def fake_transcribe(config, rec, source_path, model_id, language_arg, runner=None, source_info=None):
            attempt = ProcessingAttempt.objects.create(
                recording=rec,
                stage=AttemptStage.TRANSCRIPTION,
                ordinal=1,
                model_id=model_id,
            )
            attempt.context_json = {
                "input": {"normalized": False},
                "runs": [
                    {"speakers": True, "outcome": "nonzero_exit", "error_code": "mw_speakers_failure", "detail": "Error: x"},
                    {"speakers": False, "outcome": "success"},
                ],
                "speakers_fallback": True,
            }
            attempt.outcome = AttemptOutcome.SUCCESS
            attempt.finished_at = tz_now()
            attempt.save()
            return attempt

        from django.utils import timezone as _tz

        def tz_now():
            return _tz.now()

        monkeypatch.setattr("workflow.services.pipeline.transcription_service.transcribe_recording", fake_transcribe)
        config = make_config(tmp_path)
        result = transcribe_one(config, recording)
        assert result["result"] == "transcribed"
        assert result["speakers_fallback"] is True
        assert "without speaker labels" in result["warning"]


class TestNormalizationExceptionLifecycle:
    """Unexpected normalization failures still clean the exact attempt
    temp dir; expected filesystem failures become clean finished attempts."""

    def _source_bytes(self, source: Path) -> bytes:
        return source.read_bytes()

    def test_dest_dir_creation_oserror_clean_attempt_no_mw_contact(self, tmp_path, monkeypatch):
        import shutil as _shutil

        recording, source = _fake_mp3(tmp_path)
        original = self._source_bytes(source)
        # Block directory creation: put a FILE where the recording dir
        # must be created, so mkdir raises NotADirectoryError (OSError).
        blocker = Path(make_config(tmp_path).storage.temp) / "transcription" / str(recording.pk)
        blocker.parent.mkdir(parents=True)
        blocker.write_text("i am a file, not a directory")
        # Deterministic afconvert presence (tests must not require it).
        monkeypatch.setattr(
            "workflow.services.audiosamples.shutil.which", lambda name: "/usr/bin/afconvert"
        )
        seen = []

        def fail_run(argv, capture_output, text, timeout):
            seen.append(argv)
            return _Result()

        config = make_config(tmp_path, normalize_input=True)
        attempt = transcribe_recording(
            config, recording, source, model_id="apple:zh-HK", language_arg=None, runner=fail_run
        )
        assert attempt.outcome == AttemptOutcome.NONZERO_EXIT
        assert attempt.error_code == "normalization_failed"
        assert attempt.error_message == "temp_dir_unwritable"
        assert attempt.finished_at is not None  # finished, not orphaned
        assert seen == []  # zero MacWhisper contact
        assert attempt.mw_version == ""  # no version probe either
        assert attempt.cli_args_json == []  # no argv recorded
        assert self._source_bytes(source) == original
        # No temp artifact was created beyond the pre-existing blocker file.
        assert blocker.is_file()
        assert not (Path(config.storage.temp) / "transcription" / str(recording.pk) / "attempt_1").exists()

    def test_sample_extraction_error_after_creating_temp_file_cleans(self, tmp_path, monkeypatch):
        recording, source = _fake_mp3(tmp_path)
        original = self._source_bytes(source)
        created = []

        def fake_convert(source_path, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(dest), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\x01\x02" * 32000)
            created.append(dest)
            raise SampleExtractionError("afconvert_failed")

        monkeypatch.setattr("workflow.services.audiosamples.convert_to_pcm_16k", fake_convert)

        def fail_run(argv, capture_output, text, timeout):
            raise AssertionError("mw must not run after normalization failure")

        config = make_config(tmp_path, normalize_input=True)
        attempt = transcribe_recording(
            config, recording, source, model_id="apple:zh-HK", language_arg=None, runner=fail_run
        )
        assert attempt.outcome == AttemptOutcome.NONZERO_EXIT
        assert attempt.error_code == "normalization_failed"
        assert created  # temp file was created before the failure
        temp_root = Path(config.storage.temp) / "transcription"
        assert list(temp_root.glob("**/*")) == []  # exact dir removed
        assert self._source_bytes(source) == original

    def test_unexpected_runtime_error_cleans_exact_dir_then_propagates(self, tmp_path, monkeypatch):
        recording, source = _fake_mp3(tmp_path)
        original = self._source_bytes(source)

        def fake_convert(source_path, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            (dest.parent / "attempt-scratch.txt").write_text("scratch")
            raise RuntimeError("unexpected internal failure")

        monkeypatch.setattr("workflow.services.audiosamples.convert_to_pcm_16k", fake_convert)

        def fail_run(argv, capture_output, text, timeout):
            raise AssertionError("mw must not run")

        config = make_config(tmp_path, normalize_input=True)
        with pytest.raises(RuntimeError):
            transcribe_recording(
                config, recording, source, model_id="apple:zh-HK", language_arg=None, runner=fail_run
            )
        temp_root = Path(config.storage.temp) / "transcription"
        assert list(temp_root.glob("**/*")) == []  # exact attempt dir removed
        assert self._source_bytes(source) == original  # no source mutation

    def test_initial_normalization_failure_state_requires_explicit_retry(self, tmp_path, monkeypatch):
        from workflow.models import ProcessingStatus

        recording, source = _fake_mp3(tmp_path)

        def fail_convert(source_path, dest):
            raise SampleExtractionError("afconvert_failed")

        monkeypatch.setattr("workflow.services.audiosamples.convert_to_pcm_16k", fail_convert)

        def fail_run(argv, capture_output, text, timeout):
            raise AssertionError("mw must not run")

        config = make_config(tmp_path, normalize_input=True)
        attempt = transcribe_recording(
            config, recording, source, model_id="apple:zh-HK", language_arg=None, runner=fail_run
        )
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.FAILED
        assert recording.failure_stage == "transcription"
        assert recording.retranscription_failed is False
        assert attempt.error_code == "normalization_failed"
        assert attempt.finished_at is not None

    def test_retranscription_normalization_failure_preserves_active_transcript(self, tmp_path, monkeypatch):
        from factories import make_transcribed_recording
        from workflow.models import ProcessingStatus, SummaryState

        recording, transcript, _section = make_transcribed_recording(["hello"], sha="renorm-1")
        recording.duration_seconds = 2.0
        recording.save(update_fields=["duration_seconds"])
        source = tmp_path / "data" / "inbox" / "again.wav"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"fake mp3 bytes")
        from workflow.models import AudioSource

        AudioSource.objects.create(
            recording=recording,
            path=str(source),
            path_identity=str(source).casefold(),
            original_filename=source.name,
            presence="present",
            is_canonical=True,
            discovery_state="hashed",
        )
        # Force the normalization path even though the source is PCM WAV:
        # a corrupt destination parent (file in place of the attempt dir).
        blocker = Path(make_config(tmp_path).storage.temp) / "transcription" / str(recording.pk)
        blocker.parent.mkdir(parents=True)
        blocker.write_text("not a dir")
        monkeypatch.setattr(
            "workflow.services.audiosamples.shutil.which", lambda name: "/usr/bin/afconvert"
        )

        def fail_run(argv, capture_output, text, timeout):
            raise AssertionError("mw must not run")

        config = make_config(tmp_path, normalize_input=True, speakers_fallback=False)
        attempt = transcribe_recording(
            config, recording, source, model_id="apple:zh-HK", language_arg=None, runner=fail_run
        )
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED
        assert recording.retranscription_failed is True
        assert recording.last_failed_attempt_id == attempt.pk
        assert recording.transcripts.filter(is_active=True).first().pk == transcript.pk
        assert attempt.error_code == "normalization_failed"

    def test_normalization_failure_context_json_is_bounded_safe_provenance(self, tmp_path, monkeypatch):
        recording, source = _fake_mp3(tmp_path)

        def fail_convert(source_path, dest):
            raise SampleExtractionError("afconvert_failed")

        monkeypatch.setattr("workflow.services.audiosamples.convert_to_pcm_16k", fail_convert)

        def fail_run(argv, capture_output, text, timeout):
            raise AssertionError("mw must not run")

        config = make_config(tmp_path, normalize_input=True)
        attempt = transcribe_recording(
            config, recording, source, model_id="apple:zh-HK", language_arg=None,
            runner=fail_run, source_info={"source_audio_source_id": 7},
        )
        context = attempt.context_json
        serialized = json.dumps(context)
        assert len(serialized) < 2000
        assert context["normalization_failure"] == "afconvert_failed"
        assert context["input"]["normalized"] is False
        assert context["input"]["source_audio_source_id"] == 7
        assert context["runs"] == []
        assert "/Users/" not in serialized and "normalized.wav content" not in serialized
