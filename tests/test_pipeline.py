"""Tests for the pipeline orchestration and Step 2 CLI commands."""

from __future__ import annotations

import json
import struct
import subprocess
import wave
from pathlib import Path

import pytest
from django.utils import timezone as dj_timezone
from datetime import timedelta

import brainlib.cli as cli
from workflow.models import (
    AttemptOutcome,
    AudioSource,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
    RoutingDecision,
    RoutingMethod,
    Section,
    Transcript,
)
from workflow.services.pipeline import (
    PipelineBusy,
    confirm_routing,
    manual_route,
    pipeline_lock,
    retry,
    route_one,
    run_pipeline,
    transcribe_one,
)

from factories import make_config

pytestmark = pytest.mark.django_db

CANTONESE = "呢個係我哋嘅屋企，唔係你嘅地方，佢哋都唔知去咗邊度，你話係咪。"
MANDARIN = "这是我们的家，不是你的地方，他们都不知道去了哪里。"
MW_JSON = json.dumps({"segments": [{"start": 0, "end": 5000, "text": "hello world"}], "text": "hello world"})


def write_wav(path: Path, seconds: float = 5.0, amplitude: int = 8000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * 16000)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"".join(struct.pack("<h", amplitude * (i % 7 - 3)) for i in range(frames)))
    return path


def seed_inbox(tmp_path) -> Path:
    config = make_config(tmp_path, file_stable_seconds=1)
    config.storage.inbox.mkdir(parents=True, exist_ok=True)
    write_wav(config.storage.inbox / "2024-03-01_120000.wav")
    return config


def hash_inbox(config):
    """Run ingest twice with backdated stability so the file is hashed."""
    from workflow.services.ingest import ingest

    ingest(config)
    AudioSource.objects.update(stable_since=dj_timezone.now() - timedelta(seconds=120))
    ingest(config)


def mock_routing(monkeypatch, classifier_route="cantonese", confidence=0.95, texts=None):
    """Mock sample transcription and the classifier for deterministic routing."""
    texts = texts or {
        "apple:zh-HK": CANTONESE,
        "apple:zh-CN": CANTONESE.replace("唔", "不"),
        "parakeet-pro:nvidia_parakeet-v3": "garbled",
    }

    def fake_run(config, audio_path, model_id, language_arg, speakers, runner=None, timeout_seconds=None):
        assert speakers is False
        return texts.get(model_id)

    monkeypatch.setattr("workflow.services.transcription.run_mw_transcription", fake_run)

    def classifier(config, labelled):
        return {"route": classifier_route, "confidence": confidence, "reason_code": "test", "evidence": "e"}

    monkeypatch.setattr("workflow.services.routing.classify_with_omlx", classifier)


def mock_full_transcription(monkeypatch, stdout=MW_JSON):
    from workflow.services import transcription as transcription_service

    real = transcription_service.transcribe_recording

    def fake_transcribe(config, recording, source_path, model_id, language_arg, runner=None, source_info=None):
        def ok_runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        return real(config, recording, source_path, model_id=model_id, language_arg=language_arg, runner=ok_runner)

    monkeypatch.setattr("workflow.services.transcription.transcribe_recording", fake_transcribe)


class TestRunPipeline:
    def test_end_to_end_auto_route_and_transcribe(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        mock_routing(monkeypatch, classifier_route="cantonese", confidence=0.95)
        mock_full_transcription(monkeypatch)

        payload = run_pipeline(config)
        assert payload["routing"] and payload["routing"][0]["result"] == "routed"
        assert payload["transcription"] and payload["transcription"][0]["result"] == "transcribed"

        recording = Recording.objects.get()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED
        decision = RoutingDecision.objects.get(recording=recording, is_active=True)
        assert decision.method == RoutingMethod.AUTOMATIC
        assert decision.routing_verified is False  # unverified automatic transcription
        assert decision.model_id == "apple:zh-HK"
        assert Transcript.objects.get(recording=recording, is_active=True).text_normalized == "hello world"
        assert Section.objects.count() == 1

        # Temp sample artifacts cleaned up.
        routing_temp = Path(config.storage.temp) / "routing"
        assert not any(routing_temp.rglob("sample_*.wav"))

    def test_repeated_run_is_idempotent(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        mock_routing(monkeypatch, confidence=0.95)
        mock_full_transcription(monkeypatch)

        run_pipeline(config)
        payload = run_pipeline(config)
        assert payload["ingest"]["hashed"] == []
        assert payload["routing"] == []
        assert payload["transcription"] == []
        assert Recording.objects.count() == 1
        assert Transcript.objects.count() == 1

    def test_auto_transcribe_false_sends_to_review(self, tmp_path, monkeypatch):
        from factories import default_routing

        config = seed_inbox(tmp_path)
        config = make_config(tmp_path, file_stable_seconds=1, routing=default_routing(auto_transcribe=False))
        hash_inbox(config)
        mock_routing(monkeypatch, confidence=0.95)

        payload = run_pipeline(config)
        assert payload["routing"][0]["result"] == "needs_review"
        recording = Recording.objects.get()
        assert recording.processing_status == ProcessingStatus.NEEDS_REVIEW
        assert payload["transcription"] == []  # nothing auto-transcribed

    def test_low_confidence_needs_review_and_no_transcription(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        mock_routing(monkeypatch, confidence=0.3)
        mock_full_transcription(monkeypatch)

        payload = run_pipeline(config)
        assert payload["routing"][0]["result"] == "needs_review"
        assert payload["transcription"] == []
        recording = Recording.objects.get()
        assert recording.processing_status == ProcessingStatus.NEEDS_REVIEW

    def test_unavailable_classifier_needs_review(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        mock_routing(monkeypatch)

        def unavailable(config, labelled):
            from workflow.services import routing as r

            raise r.RoutingUnavailable("down")

        monkeypatch.setattr("workflow.services.routing.classify_with_omlx", unavailable)
        payload = run_pipeline(config)
        assert payload["routing"][0]["result"] == "needs_review"
        decision = RoutingDecision.objects.get(is_active=True)
        assert decision.reason_code == "classifier_unavailable"

    def test_failed_records_not_retried_by_run(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        mock_routing(monkeypatch, confidence=0.95)
        # Transcription always fails.
        from workflow.services import transcription as transcription_service

        real_transcribe = transcription_service.transcribe_recording

        def failing_transcribe(config, recording, source_path, model_id, language_arg, runner=None, source_info=None):
            def bad_runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Error: x")

            return real_transcribe(
                config, recording, source_path, model_id=model_id, language_arg=language_arg, runner=bad_runner
            )

        monkeypatch.setattr("workflow.services.transcription.transcribe_recording", failing_transcribe)
        run_pipeline(config)
        recording = Recording.objects.get()
        assert recording.processing_status == ProcessingStatus.FAILED

        second = run_pipeline(config)
        assert second["transcription"] == []  # no automatic retry
        assert Recording.objects.get().processing_status == ProcessingStatus.FAILED


class TestManualRouting:
    def _needs_review_recording(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        mock_routing(monkeypatch, confidence=0.3)
        run_pipeline(config)
        return config, Recording.objects.get()

    def test_manual_route_appends_and_activates(self, tmp_path, monkeypatch):
        config, recording = self._needs_review_recording(tmp_path, monkeypatch)
        automatic = RoutingDecision.objects.get(recording=recording, is_active=True)

        result = manual_route(recording, "mandarin")
        recording.refresh_from_db()
        assert result["status"] == ProcessingStatus.READY_TO_TRANSCRIBE
        manual = RoutingDecision.objects.get(pk=result["decision_id"])
        assert manual.method == RoutingMethod.MANUAL
        assert manual.routing_verified is True
        assert manual.model_id == "apple:zh-CN"
        automatic.refresh_from_db()
        assert automatic.is_active is False  # preserved in history
        assert RoutingDecision.objects.filter(recording=recording).count() == 2

    def test_unknown_profile_rejected(self, tmp_path, monkeypatch):
        config, recording = self._needs_review_recording(tmp_path, monkeypatch)
        with pytest.raises(Exception, match="unknown routing profile"):
            manual_route(recording, "klingon")

    def test_confirm_without_retranscription(self, tmp_path, monkeypatch):
        from factories import default_routing

        config = seed_inbox(tmp_path)
        config = make_config(tmp_path, file_stable_seconds=1, routing=default_routing(auto_transcribe=False))
        hash_inbox(config)
        mock_routing(monkeypatch, confidence=0.95)
        run_pipeline(config)
        recording = Recording.objects.get()
        decision = RoutingDecision.objects.get(recording=recording, is_active=True)

        result = confirm_routing(recording)
        decision.refresh_from_db()
        assert result["verified"] is True
        assert decision.routing_verified is True
        assert decision.verified_at is not None
        assert decision.method == RoutingMethod.AUTOMATIC
        # No transcription happened.
        assert Transcript.objects.count() == 0

    def test_manual_profile_change_retranscribes_with_versions(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        latin_garbage = {
            "apple:zh-HK": "sindeditestentent merisecoman lin otput",
            "apple:zh-CN": "sindeatc testenance michisper comand",
            "parakeet-pro:nvidia_parakeet-v3": "This is our home, not your place. They do not know where it went.",
        }
        mock_routing(monkeypatch, classifier_route="european", confidence=0.95, texts=latin_garbage)
        mock_full_transcription(monkeypatch)
        run_pipeline(config)
        recording = Recording.objects.get()
        assert Transcript.objects.count() == 1

        manual_route(recording, "cantonese")
        transcribe_one(config, Recording.objects.get(pk=recording.pk))
        assert Transcript.objects.count() == 2
        active = Transcript.objects.get(recording=recording, is_active=True)
        assert active.attempt.model_id == "apple:zh-HK"
        assert active.superseded_at is None
        old = Transcript.objects.filter(recording=recording, is_active=False).first()
        assert old.superseded_at is not None
        assert old.segments.exists()

    def test_retry_failed_transcription(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        mock_routing(monkeypatch, confidence=0.95)

        attempts = {"n": 0}
        from workflow.services import transcription as transcription_service

        real_transcribe = transcription_service.transcribe_recording

        def flaky_transcribe(config, recording, source_path, model_id, language_arg, runner=None, source_info=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                def bad_runner(argv, **kwargs):
                    return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Error: x")

                return real_transcribe(
                    config, recording, source_path, model_id=model_id, language_arg=language_arg, runner=bad_runner
                )
            return real_transcribe(
                config, recording, source_path, model_id=model_id, language_arg=language_arg,
                runner=lambda argv, **k: subprocess.CompletedProcess(argv, 0, stdout=MW_JSON, stderr=""),
            )

        monkeypatch.setattr("workflow.services.transcription.transcribe_recording", flaky_transcribe)
        run_pipeline(config)
        recording = Recording.objects.get()
        assert recording.processing_status == ProcessingStatus.FAILED

        # Retry: the mock now succeeds on its second invocation.
        result = retry(config, Recording.objects.get(pk=recording.pk))
        recording.refresh_from_db()
        assert result["result"] == "retried"
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED
        assert ProcessingAttempt.objects.filter(recording=recording, stage="transcription").count() == 2


def _ok_transcribe(config, recording, source_path, model_id, language_arg):
    from workflow.services.transcription import transcribe_recording

    def ok_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=MW_JSON, stderr="")

    return transcribe_recording(
        config, recording, source_path, model_id=model_id, language_arg=language_arg, runner=ok_runner
    )


class TestCli:
    def test_status_json(self, tmp_path, capsys, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        Recording.objects.update(processing_status=ProcessingStatus.NEEDS_REVIEW)
        assert cli.main(["status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["counts"]["needs_review"] == 1

    def test_review_json_sections(self, tmp_path, capsys, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        mock_routing(monkeypatch, confidence=0.95)
        mock_full_transcription(monkeypatch)
        run_pipeline(config)
        Recording.objects.update(processing_status=ProcessingStatus.NEEDS_REVIEW)
        assert cli.main(["review", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["needs_review"]

    def test_transcripts_json(self, tmp_path, capsys, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        mock_routing(monkeypatch, confidence=0.95)
        mock_full_transcription(monkeypatch)
        run_pipeline(config)
        recording = Recording.objects.get()
        assert cli.main(["transcripts", str(recording.pk), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["transcripts"]) == 1
        assert payload["transcripts"][0]["is_active"] is True

    def test_transcripts_unknown_id_exits_one(self, capsys):
        assert cli.main(["transcripts", "nope", "--json"]) == 1
        assert "not found" in capsys.readouterr().err

    def test_lock_contention_exit_code(self, tmp_path, capsys, monkeypatch):
        config = seed_inbox(tmp_path)

        def busy(config):
            raise PipelineBusy("1234")

        monkeypatch.setattr("workflow.services.pipeline.pipeline_lock", busy)
        assert cli.main(["ingest", "--json"]) == cli.EXIT_BUSY
        assert "another pipeline process" in capsys.readouterr().err

    def test_migrations_up_to_date(self):
        from django.core.management import call_command

        try:
            call_command("makemigrations", "workflow", "--check", "--dry-run")
            exit_code = 0
        except SystemExit as exc:
            exit_code = exc.code
        assert exit_code == 0


class TestCliManualRoute:
    def _reviewable(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        mock_routing(monkeypatch, confidence=0.3)
        run_pipeline(config)
        return Recording.objects.get()

    def test_route_profile_moves_to_ready_without_transcribing(self, tmp_path, monkeypatch, capsys):
        recording = self._reviewable(tmp_path, monkeypatch)
        assert cli.main(["route", str(recording.pk), "--profile", "mandarin", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        recording.refresh_from_db()
        assert payload["status"] == ProcessingStatus.READY_TO_TRANSCRIBE
        assert recording.processing_status == ProcessingStatus.READY_TO_TRANSCRIBE
        assert Transcript.objects.count() == 0  # no transcription without --transcribe-now
        decision = RoutingDecision.objects.get(recording=recording, is_active=True)
        assert decision.method == RoutingMethod.MANUAL
        assert decision.routing_verified is True

    def test_route_profile_transcribe_now_flag(self, tmp_path, monkeypatch, capsys):
        from factories import write_cli_config

        write_cli_config(tmp_path, monkeypatch)
        recording = self._reviewable(tmp_path, monkeypatch)
        from workflow.services import transcription as transcription_service

        real = transcription_service.transcribe_recording

        def fake(config, rec, source_path, model_id, language_arg, runner=None, source_info=None):
            def ok_runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, stdout=MW_JSON, stderr="")

            return real(config, rec, source_path, model_id=model_id, language_arg=language_arg, runner=ok_runner)

        monkeypatch.setattr("workflow.services.transcription.transcribe_recording", fake)
        assert cli.main(["route", str(recording.pk), "--profile", "mandarin", "--transcribe-now", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["transcription"][0]["result"] == "transcribed"
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED
        decision = RoutingDecision.objects.get(recording=recording, is_active=True)
        assert decision.model_id == "apple:zh-CN"
