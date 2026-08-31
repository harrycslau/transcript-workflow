"""Regression tests for the Step 2 code-review findings (1-11)."""

from __future__ import annotations

import ast
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
    AudioSource,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
    RoutingDecision,
    RoutingMethod,
    Section,
    Transcript,
)
from workflow.services import routing as routing_service
from workflow.services.pipeline import (
    _apply_outcome,
    confirm_routing,
    manual_route,
    recover_interruptions,
    retry,
    route_one,
    run_pipeline,
    transcribe_one,
)
from workflow.services.routing import RoutingInvalid, RoutingUnavailable
from workflow.services.transcription import parse_mw_json, timeout_for

from factories import make_config
from workflow.services import transcription as _ts_module

# Captured at import time, before any test patches this module attribute.
PRISTINE_TRANSCRIBE = _ts_module.transcribe_recording

from test_pipeline import (
    MW_JSON,
    hash_inbox,
    mock_full_transcription,
    mock_routing,
    seed_inbox,
    write_wav,
)


def _routed(config):
    """Move seeded recordings into the routing state for direct route_one calls."""
    Recording.objects.update(processing_status=ProcessingStatus.ROUTING)
    return config

pytestmark = pytest.mark.django_db

CANTONESE = "呢個係我哋嘅屋企，唔係你嘅地方，佢哋都唔知去咗邊度，你話係咪。"
MANDARIN = "这是我们的家，不是你的地方，他们都不知道去了哪里，你说什么呢。"


# ---------------------------------------------------------------------------
# Finding 1: all sample windows are used
# ---------------------------------------------------------------------------


class TestCompositeSamples:
    def test_long_recording_uses_three_windows(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        # Long recording so beginning/middle/end windows are separate.
        write_wav(config.storage.inbox / "2024-03-01_120000.wav", seconds=60.0)
        hash_inbox(config)
        _routed(config)
        seen_paths = []
        texts = {
            "apple:zh-HK": CANTONESE,
            "apple:zh-CN": MANDARIN,
            "parakeet-pro:nvidia_parakeet-v3": "garbled",
        }

        def fake_run(config, audio_path, model_id, language_arg, speakers, runner=None, timeout_seconds=None):
            seen_paths.append(Path(audio_path).name)
            assert speakers is False
            return texts.get(model_id)

        monkeypatch.setattr("workflow.services.transcription.run_mw_transcription", fake_run)
        monkeypatch.setattr(
            "workflow.services.routing.classify_with_omlx",
            lambda config, candidates: {"route": "cantonese", "confidence": 0.95, "reason_code": "x", "evidence": ""},
        )
        recording = Recording.objects.get()
        route_one(config, recording)

        # Three windows -> one composite used by all three candidates.
        assert len(seen_paths) == 3
        assert all(name.startswith("composite") for name in seen_paths)
        decision = RoutingDecision.objects.get(recording=recording, is_active=True)
        assert decision.evidence["window_count"] == 3
        # Window order preserved: start < middle < end.
        windows = decision.evidence["windows"]
        assert windows[0]["start_seconds"] < windows[1]["start_seconds"] < windows[2]["start_seconds"]

    def test_composite_contains_all_windows_in_order(self, tmp_path):
        from workflow.services.audiosamples import extract_samples

        source = write_wav(tmp_path / "long.wav", seconds=60.0)
        attempt_dir = tmp_path / "attempt"
        bundle = extract_samples(source, attempt_dir)
        assert len(bundle.windows) == 3
        assert bundle.composite_path is not None and bundle.composite_path.exists()
        with wave.open(str(bundle.composite_path), "rb") as handle:
            composite_frames = handle.getnframes()
        expected = sum(int((e - s) * 16000) for s, e in bundle.windows)
        assert composite_frames == expected  # chronological concatenation

    def test_silent_head_voiced_rest_is_not_silent(self, tmp_path):
        from workflow.services.audiosamples import extract_samples

        source = tmp_path / "mixed.wav"
        source.parent.mkdir(parents=True, exist_ok=True)
        rate = 16000
        with wave.open(str(source), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            # 20s silence (head) + 40s voiced (middle and tail).
            handle.writeframes(b"\x00\x00" * rate * 20)
            handle.writeframes(
                b"".join(struct.pack("<h", 8000 * (i % 7 - 3)) for i in range(rate * 40))
            )
        bundle = extract_samples(source, tmp_path / "attempt")
        assert bundle.window_silence[0] is True
        assert bundle.is_silent is False

    def test_all_windows_silent_needs_review(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        # Replace with digital silence BEFORE hashing so identity matches.
        with wave.open(str(config.storage.inbox / "2024-03-01_120000.wav"), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * 16000 * 60)
        hash_inbox(config)

        def fail(*args, **kwargs):
            raise AssertionError("no transcription may run for silent audio")

        monkeypatch.setattr("workflow.services.transcription.run_mw_transcription", fail)
        _routed(config)
        recording = Recording.objects.get()
        outcome = route_one(config, recording)
        assert outcome["result"] == "needs_review"
        decision = RoutingDecision.objects.get(recording=recording, is_active=True)
        assert decision.reason_code == "silent_audio"
        assert all(w["silent"] for w in decision.evidence["windows"])

    def test_temp_cleanup_after_partial_candidate_failure(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        calls = {"n": 0}

        def flaky_run(config, audio_path, model_id, language_arg, speakers, runner=None, timeout_seconds=None):
            calls["n"] += 1
            if calls["n"] == 2:  # second candidate fails
                return None
            return CANTONESE

        monkeypatch.setattr("workflow.services.transcription.run_mw_transcription", flaky_run)
        _routed(config)
        recording = Recording.objects.get()
        route_one(config, recording)
        routing_temp = Path(config.storage.temp) / "routing" / str(recording.pk)
        assert not list(routing_temp.rglob("composite.wav"))  # cleaned in finally


# ---------------------------------------------------------------------------
# Finding 2: content replacement at an existing AudioSource path
# ---------------------------------------------------------------------------


class TestContentReplacement:
    def _processed_recording(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        mock_routing(monkeypatch, confidence=0.95)
        mock_full_transcription(monkeypatch)
        run_pipeline(config)
        return config, Recording.objects.get()

    def test_unchanged_content_is_idempotent(self, tmp_path, monkeypatch):
        config, recording = self._processed_recording(tmp_path, monkeypatch)
        payload = run_pipeline(config)
        assert payload["ingest"]["hashed"] == []
        assert Recording.objects.count() == 1
        assert recording.transcripts.count() == 1

    def test_overwritten_path_rehashes_and_reattaches(self, tmp_path, monkeypatch):
        config, old_recording = self._processed_recording(tmp_path, monkeypatch)
        source = AudioSource.objects.get()
        old_sha = old_recording.sha256
        old_status = old_recording.processing_status
        old_active = Transcript.objects.get(recording=old_recording, is_active=True)

        # Overwrite the same path with different audio content.
        write_wav(Path(source.path), seconds=3.0, amplitude=321)
        run_pipeline(config)  # marks changed -> detaches -> stability wait
        AudioSource.objects.update(stable_since=dj_timezone.now() - timedelta(seconds=120))
        run_pipeline(config)  # hashes and attaches to a new Recording
        AudioSource.objects.update(stable_since=dj_timezone.now() - timedelta(seconds=120))
        run_pipeline(config)

        assert Recording.objects.count() == 2
        new_recording = Recording.objects.exclude(pk=old_recording.pk).get()
        assert new_recording.sha256 != old_sha
        source.refresh_from_db()
        assert source.recording_id == new_recording.pk

        # Old recording's history untouched; audio missing (no sources left).
        old_recording.refresh_from_db()
        assert old_recording.sha256 == old_sha
        assert old_recording.processing_status == old_status
        assert old_active.is_active and old_active.superseded_at is None
        assert old_recording.transcripts.count() == 1
        assert old_recording.routing_decisions.count() == 1
        assert old_recording.audio_status == "missing"

    def test_old_recording_stays_present_with_other_source(self, tmp_path, monkeypatch):
        from workflow.services.ingest import ingest

        config = seed_inbox(tmp_path)
        hash_inbox(config)
        mock_routing(monkeypatch, confidence=0.95)
        mock_full_transcription(monkeypatch)
        run_pipeline(config)
        old_recording = Recording.objects.get()
        source_path = AudioSource.objects.get().path

        # A second identical copy at another path, then overwrite the first.
        import shutil

        shutil.copyfile(source_path, config.storage.inbox / "copy.wav")
        ingest(config)
        AudioSource.objects.update(stable_since=dj_timezone.now() - timedelta(seconds=120))
        ingest(config)
        write_wav(Path(source_path), seconds=3.0, amplitude=321)
        run_pipeline(config)
        AudioSource.objects.update(stable_since=dj_timezone.now() - timedelta(seconds=120))
        run_pipeline(config)

        old_recording.refresh_from_db()
        assert old_recording.audio_status == "present"  # copy.wav still there
        present_sources = old_recording.sources.filter(presence="present")
        assert present_sources.count() == 1

    def test_changed_source_not_routed_under_old_identity(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        _routed(config)
        recording = Recording.objects.get()
        source = AudioSource.objects.get()
        write_wav(Path(source.path), seconds=3.0, amplitude=321)  # content changed

        def fail(*args, **kwargs):
            raise AssertionError("must not route changed content under the old identity")

        monkeypatch.setattr("workflow.services.routing.route_recording", fail)
        result = route_one(config, Recording.objects.get(pk=recording.pk))
        source.refresh_from_db()
        assert source.recording_id is None  # detached into the hash workflow

    def test_file_changed_before_transcription_is_skipped(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        mock_routing(monkeypatch, confidence=0.95)
        mock_full_transcription(monkeypatch)
        run_pipeline(config)  # routed; recording is ready (not yet transcribed)
        recording = Recording.objects.get()
        recording.processing_status = ProcessingStatus.READY_TO_TRANSCRIBE
        recording.transcripts.update(is_active=False)  # simulate no transcript yet
        recording.save()

        from workflow.services.transcription import transcribe_recording

        write_wav(Path(AudioSource.objects.get().path), seconds=3.0, amplitude=321)

        def fail(*args, **kwargs):
            raise AssertionError("must not transcribe changed content under the old identity")

        monkeypatch.setattr("workflow.services.transcription.transcribe_recording", fail)
        result = transcribe_one(config, Recording.objects.get(pk=recording.pk))
        assert result["result"] == "parked"
        assert result["reason"] in ("source_changed", "source_missing")
        # No NEW transcript created and none activated under the old identity.
        assert Transcript.objects.filter(is_active=True).count() == 0

    def test_audio_never_modified(self, tmp_path, monkeypatch):
        from workflow.services.ingest import sha256_file

        config, recording = self._processed_recording(tmp_path, monkeypatch)
        path = Path(AudioSource.objects.get().path)
        before = sha256_file(path)
        run_pipeline(config)
        assert sha256_file(path) == before


# ---------------------------------------------------------------------------
# Finding 3: audio status recalculated after attaching a duplicate source
# ---------------------------------------------------------------------------


class TestDuplicateAudioStatus:
    def test_missing_recording_becomes_present_on_duplicate(self, tmp_path):
        from workflow.services.ingest import ingest

        config = seed_inbox(tmp_path, )
        hash_inbox(config)
        recording = Recording.objects.get()
        source = AudioSource.objects.get()
        # The only source goes missing -> recording missing.
        Path(source.path).unlink()
        ingest(config)
        recording.refresh_from_db()
        assert recording.audio_status == "missing"

        # The same content reappears at a new path -> present + canonical.
        import shutil
        import hashlib

        original = write_wav(tmp_path / "master.wav")
        # Match the original content exactly by re-creating identical bytes.
        from workflow.services.ingest import sha256_file as sha

        shutil.copyfile(original, config.storage.inbox / "dup.wav")
        ingest(config)  # observe new path
        AudioSource.objects.update(stable_since=dj_timezone.now() - timedelta(seconds=120))
        ingest(config)  # hash and attach
        recording.refresh_from_db()
        assert sha(original) == recording.sha256
        assert recording.audio_status == "present"
        new_source = recording.sources.get(path__endswith="dup.wav")
        assert new_source.is_canonical is True


# ---------------------------------------------------------------------------
# Finding 4: recovery of interrupted attempts / in-flight states
# ---------------------------------------------------------------------------


class TestRecovery:
    def test_interrupted_routing_attempt_recovers(self, tmp_path):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        recording = Recording.objects.get()
        recording.processing_status = ProcessingStatus.ROUTING
        recording.save()
        ProcessingAttempt.objects.create(recording=recording, stage="routing", ordinal=1)  # unfinished

        result = recover_interruptions(config)
        assert result["recovered_attempts"] == 1
        attempt = ProcessingAttempt.objects.get()
        assert attempt.outcome == "interrupted"
        assert attempt.finished_at is not None
        assert attempt.error_code == "process_interrupted"
        # New attempt can be created (constraint no longer blocks).
        ProcessingAttempt.objects.create(recording=recording, stage="routing", ordinal=2)

    def test_interrupted_initial_transcription_recovers_to_ready(self, tmp_path):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        recording = Recording.objects.get()
        recording.processing_status = ProcessingStatus.TRANSCRIBING
        recording.save()
        ProcessingAttempt.objects.create(recording=recording, stage="transcription", ordinal=1)

        recover_interruptions(config)
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.READY_TO_TRANSCRIBE

    def test_interrupted_retranscription_recovers_to_transcribed(self, tmp_path):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        recording = Recording.objects.get()
        attempt = ProcessingAttempt.objects.create(recording=recording, stage="transcription", ordinal=1)
        attempt.outcome = "success"
        attempt.finished_at = dj_timezone.now()
        attempt.save()
        Transcript.objects.create(
            recording=recording, attempt=attempt, is_active=True, text_normalized="old"
        )
        recording.processing_status = ProcessingStatus.TRANSCRIBING  # interrupted retranscription
        recording.save()
        ProcessingAttempt.objects.create(recording=recording, stage="transcription", ordinal=2)

        recover_interruptions(config)
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED
        assert Transcript.objects.get(is_active=True).text_normalized == "old"

    def test_orphan_transcribing_state_without_attempt_recovers(self, tmp_path):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        recording = Recording.objects.get()
        recording.processing_status = ProcessingStatus.TRANSCRIBING
        recording.save()
        result = recover_interruptions(config)
        assert result["recovered_recordings"] >= 1
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.READY_TO_TRANSCRIBE

    def test_recovery_is_idempotent(self, tmp_path):
        config = seed_inbox(tmp_path)
        first = recover_interruptions(config)
        second = recover_interruptions(config)
        assert second["recovered_attempts"] == 0
        assert second["recovered_recordings"] == 0
        assert first == first  # sanity

    def test_run_pipeline_reports_recovery(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        recording = Recording.objects.get()
        recording.processing_status = ProcessingStatus.TRANSCRIBING
        recording.save()
        ProcessingAttempt.objects.create(recording=recording, stage="transcription", ordinal=1)
        payload = run_pipeline(config)
        assert payload["recovery"]["recovered_attempts"] == 1


# ---------------------------------------------------------------------------
# Finding 5: CLI deduplication and lock contention
# ---------------------------------------------------------------------------


class TestCliStructure:
    def test_single_definition_per_command(self):
        source = Path(cli.__file__).read_text()
        tree = ast.parse(source)
        names = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        for command in ("cmd_ingest", "cmd_route", "cmd_transcribe", "cmd_run", "cmd_retry"):
            assert names.count(command) == 1, command

    @pytest.mark.parametrize("argv", [
        ["ingest", "--json"],
        ["route", "--json"],
        ["transcribe", "--json"],
        ["run", "--json"],
        ["retry", "some-id", "--json"],
        ["route", "some-id", "--profile", "cantonese", "--json"],
    ])
    def test_lock_contention_exits_three_without_traceback(self, tmp_path, monkeypatch, capsys, argv):
        from workflow.services.pipeline import PipelineBusy

        monkeypatch.setattr(
            "workflow.services.pipeline.pipeline_lock",
            lambda config: (_ for _ in ()).throw(PipelineBusy("4321")),
        )
        code = cli.main(argv)
        assert code == cli.EXIT_BUSY
        err = capsys.readouterr().err
        assert "another pipeline process is active (pid 4321)" in err
        assert "Traceback" not in err

    def test_missing_config_exits_one(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("BRAIN_CONFIG", "/nonexistent/config.yaml")
        assert cli.main(["ingest", "--json"]) == 1
        assert "Configuration file not found" in capsys.readouterr().err

    def test_json_success_output_parses(self, tmp_path, capsys):
        config = seed_inbox(tmp_path)
        assert cli.main(["run", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert "ingest" in payload


# ---------------------------------------------------------------------------
# Finding 6: manual reroute of a transcribed recording
# ---------------------------------------------------------------------------


class TestManualReroute:
    def _transcribed(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        mock_routing(monkeypatch, classifier_route="european", confidence=0.95,
                     texts={
                         "apple:zh-HK": "sindeditestent merisecoman",
                         "apple:zh-CN": "sindeatc testenance michisper",
                         "parakeet-pro:nvidia_parakeet-v3": "This is our home, not your place.",
                     })
        local = pytest.MonkeyPatch()
        mock_full_transcription(local)
        run_pipeline(config)
        local.undo()
        return config, Recording.objects.get()

    def test_different_profile_schedules_retranscription(self, tmp_path, monkeypatch, capsys):
        config, recording = self._transcribed(tmp_path, monkeypatch)
        assert cli.main(["route", str(recording.pk), "--profile", "cantonese", "--json"]) == 0
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.READY_TO_TRANSCRIBE
        decision = RoutingDecision.objects.get(recording=recording, is_active=True)
        assert decision.method == RoutingMethod.MANUAL
        assert decision.model_id == "apple:zh-HK"
        # Old transcript remains active until new success.
        assert Transcript.objects.get(recording=recording, is_active=True) is not None

    def test_ordinary_transcribe_processes_pending_retranscription(self, tmp_path, monkeypatch):
        from factories import write_cli_config
        from workflow.services import transcription as ts

        write_cli_config(tmp_path, monkeypatch)
        config, recording = self._transcribed(tmp_path, monkeypatch)
        manual_route(recording, "cantonese")
        old_active = Transcript.objects.get(recording=recording, is_active=True)

        real = PRISTINE_TRANSCRIBE

        def fake(config, rec, source_path, model_id, language_arg, runner=None):
            def ok_runner(argv, **kwargs):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"segments": [{"start": 0, "end": 5, "text": "v2"}], "text": "v2"}),
                    stderr="",
                )

            return real(config, rec, source_path, model_id=model_id, language_arg=language_arg, runner=ok_runner)

        monkeypatch.setattr("workflow.services.transcription.transcribe_recording", fake)
        assert cli.main(["transcribe", "--json"]) == 0
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED
        active = Transcript.objects.get(recording=recording, is_active=True)
        assert active.text_normalized == "v2"
        old_active.refresh_from_db()
        assert old_active.is_active is False

    def test_ordinary_run_processes_pending_retranscription(self, tmp_path, monkeypatch):
        config, recording = self._transcribed(tmp_path, monkeypatch)
        manual_route(recording, "cantonese")
        mock_full_transcription(monkeypatch)  # success for the retranscription
        payload = run_pipeline(config)
        assert payload["transcription"] and payload["transcription"][0]["result"] == "transcribed"
        assert Transcript.objects.get(recording=recording, is_active=True).text_normalized == "hello world"

    def test_same_profile_confirms_without_retranscription(self, tmp_path, monkeypatch):
        config, recording = self._transcribed(tmp_path, monkeypatch)
        result = manual_route(recording, "european")
        assert result["result"] == "verified_no_retranscription"
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED
        assert Transcript.objects.count() == 1
        decision = RoutingDecision.objects.get(recording=recording, is_active=True)
        assert decision.routing_verified is True

    def test_new_failure_preserves_old_active_transcript(self, tmp_path, monkeypatch):
        from workflow.services import transcription as ts

        config, recording = self._transcribed(tmp_path, monkeypatch)
        manual_route(recording, "cantonese")

        real = PRISTINE_TRANSCRIBE

        def failing(config, rec, source_path, model_id, language_arg, runner=None):
            def bad_runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Error: x")

            return real(config, rec, source_path, model_id=model_id, language_arg=language_arg, runner=bad_runner)

        monkeypatch.setattr("workflow.services.transcription.transcribe_recording", failing)
        transcribe_one(config, Recording.objects.get(pk=recording.pk))
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED
        active = Transcript.objects.get(recording=recording, is_active=True)
        assert active.text_normalized == "hello world"


# ---------------------------------------------------------------------------
# Finding 7: explicit failed-retranscription state
# ---------------------------------------------------------------------------


class TestFailedRetranscription:
    def _retranscription_failed(self, tmp_path, monkeypatch):
        config, recording = TestManualReroute()._transcribed(tmp_path, monkeypatch)
        manual_route(recording, "cantonese")

        from workflow.services import transcription as ts

        real = PRISTINE_TRANSCRIBE

        def failing(config, rec, source_path, model_id, language_arg, runner=None):
            def bad_runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Error: x")

            return real(config, rec, source_path, model_id=model_id, language_arg=language_arg, runner=bad_runner)

        monkeypatch.setattr("workflow.services.transcription.transcribe_recording", failing)
        transcribe_one(config, Recording.objects.get(pk=recording.pk))
        return config, Recording.objects.get(pk=recording.pk)

    def test_failed_retranscription_keeps_active_transcript_and_marker(self, tmp_path, monkeypatch):
        config, recording = self._retranscription_failed(tmp_path, monkeypatch)
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED
        assert recording.retranscription_failed is True
        assert recording.last_failed_attempt is not None
        assert Transcript.objects.get(recording=recording, is_active=True) is not None

    def test_review_and_status_expose_it(self, tmp_path, monkeypatch, capsys):
        config, recording = self._retranscription_failed(tmp_path, monkeypatch)
        assert cli.main(["review", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["failed_retranscription"][0]["recording_id"] == recording.pk

        assert cli.main(["status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["failed_retranscriptions"] == 1

    def test_explicit_retry_works_and_clears_marker(self, tmp_path, monkeypatch):
        config, recording = self._retranscription_failed(tmp_path, monkeypatch)
        # Retry now succeeds (mocked upstream produces valid output).
        from workflow.services import transcription as ts

        real = PRISTINE_TRANSCRIBE

        def ok(config, rec, source_path, model_id, language_arg, runner=None):
            def ok_runner(argv, **kwargs):
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout=json.dumps({"segments": [{"start": 0, "end": 5, "text": "v3"}], "text": "v3"}),
                    stderr="",
                )

            return real(config, rec, source_path, model_id=model_id, language_arg=language_arg, runner=ok_runner)

        monkeypatch.setattr("workflow.services.transcription.transcribe_recording", ok)
        result = retry(config, recording)
        recording.refresh_from_db()
        assert result["result"] == "retried"
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED
        assert recording.retranscription_failed is False
        assert recording.last_failed_attempt is None
        active = Transcript.objects.get(recording=recording, is_active=True)
        assert active.text_normalized == "v3"

    def test_normal_run_does_not_retry_it(self, tmp_path, monkeypatch):
        config, recording = self._retranscription_failed(tmp_path, monkeypatch)
        payload = run_pipeline(config)
        assert payload["transcription"] == []
        recording.refresh_from_db()
        assert recording.retranscription_failed is True

    def test_failed_initial_transcription_is_ordinary_failed(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        mock_routing(monkeypatch, confidence=0.95)
        from workflow.services import transcription as ts

        real = PRISTINE_TRANSCRIBE

        def failing(config, rec, source_path, model_id, language_arg, runner=None):
            def bad_runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Error: x")

            return real(config, rec, source_path, model_id=model_id, language_arg=language_arg, runner=bad_runner)

        monkeypatch.setattr("workflow.services.transcription.transcribe_recording", failing)
        run_pipeline(config)
        recording = Recording.objects.get()
        assert recording.processing_status == ProcessingStatus.FAILED
        assert recording.retranscription_failed is False


# ---------------------------------------------------------------------------
# Finding 8: deactivate-then-create for routing decisions
# ---------------------------------------------------------------------------


class TestRoutingDecisionOrdering:
    def test_re_auto_route_appends_and_keeps_history(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        recording = Recording.objects.get()
        recording.processing_status = ProcessingStatus.ROUTING
        recording.save()

        outcome = routing_service.RoutingOutcome(
            route="cantonese", profile_name="cantonese", model_id="apple:zh-HK",
            language_arg=None, method="automatic", confidence=0.95,
            reason_code="auto_confident", evidence={}, ready_to_transcribe=True,
        )
        _apply_outcome(config, recording, outcome)
        second = routing_service.RoutingOutcome(
            route="european", profile_name="european", model_id="parakeet-pro:nvidia_parakeet-v3",
            language_arg=None, method="automatic", confidence=0.9,
            reason_code="auto_confident", evidence={}, ready_to_transcribe=True,
        )
        _apply_outcome(config, Recording.objects.get(pk=recording.pk), second)

        decisions = list(RoutingDecision.objects.filter(recording=recording).order_by("ordinal"))
        assert len(decisions) == 2
        assert decisions[0].is_active is False
        assert decisions[1].is_active is True

    def test_injected_failure_rolls_back(self, tmp_path, monkeypatch):
        from workflow.models import RoutingDecision as RD

        config = seed_inbox(tmp_path)
        hash_inbox(config)
        recording = Recording.objects.get()
        recording.processing_status = ProcessingStatus.ROUTING
        recording.save()
        outcome = routing_service.RoutingOutcome(
            route="cantonese", profile_name="cantonese", model_id="apple:zh-HK",
            language_arg=None, method="automatic", confidence=0.95,
            reason_code="auto_confident", evidence={}, ready_to_transcribe=True,
        )
        _apply_outcome(config, recording, outcome)
        first = RoutingDecision.objects.get(recording=recording, is_active=True)

        original_create = RD.objects.create

        def boom(*args, **kwargs):
            raise RuntimeError("injected failure")

        monkeypatch.setattr(RD.objects, "create", boom)
        with pytest.raises(RuntimeError):
            _apply_outcome(config, Recording.objects.get(pk=recording.pk), outcome)
        monkeypatch.setattr(RD.objects, "create", original_create)

        assert RoutingDecision.objects.filter(recording=recording).count() == 1
        first.refresh_from_db()
        assert first.is_active is True


# ---------------------------------------------------------------------------
# Finding 10: malformed oMLX responses
# ---------------------------------------------------------------------------


class TestMalformedOmlx:
    def _classifier_error(self, monkeypatch, body=None, raw=None, http_error=None):
        import httpx as httpx_module

        def handler(request):
            if http_error is not None:
                raise http_error
            if raw is not None:
                return httpx_module.Response(200, text=raw)
            return httpx_module.Response(200, json=body)

        config = make_config(
            Path("/tmp/x"),
            llm=type(config_llm := make_config(Path("/tmp/x")).llm)(
                provider="openai_compatible", base_url="http://omlx.test/v1",
                model="m", api_key_env="BRAIN_TEST_LLM_API_KEY",
                temperature=0.2, timeout_seconds=600,
            ),
        )
        with pytest.raises(Exception) as excinfo:
            routing_service.classify_with_omlx(
                config, {"zh_hk": "a", "zh_cn": "b", "european": "c"},
                transport=httpx_module.MockTransport(handler),
            )
        return excinfo.value

    def test_invalid_json_is_routing_invalid(self, monkeypatch):
        error = self._classifier_error(monkeypatch, raw="{not json")
        assert isinstance(error, RoutingInvalid)

    def test_non_object_payload_is_routing_invalid(self, monkeypatch):
        error = self._classifier_error(monkeypatch, raw='["list"]')
        assert isinstance(error, RoutingInvalid)

    def test_missing_choices_is_routing_invalid(self, monkeypatch):
        error = self._classifier_error(monkeypatch, body={})
        assert isinstance(error, RoutingInvalid)

    def test_empty_choices_is_routing_invalid(self, monkeypatch):
        error = self._classifier_error(monkeypatch, body={"choices": []})
        assert isinstance(error, RoutingInvalid)

    def test_invalid_message_is_routing_invalid(self, monkeypatch):
        error = self._classifier_error(monkeypatch, body={"choices": [{"message": "nope"}]})
        assert isinstance(error, RoutingInvalid)

    def test_non_string_content_is_routing_invalid(self, monkeypatch):
        error = self._classifier_error(monkeypatch, body={"choices": [{"message": {"content": 42}}]})
        assert isinstance(error, RoutingInvalid)

    def test_invalid_classifier_json_is_routing_invalid(self, monkeypatch):
        error = self._classifier_error(
            monkeypatch, body={"choices": [{"message": {"content": "not json"}}]}
        )
        assert isinstance(error, RoutingInvalid)

    def test_connectivity_failure_is_routing_unavailable(self, monkeypatch):
        import httpx as httpx_module

        error = self._classifier_error(
            monkeypatch, http_error=httpx_module.ConnectError("refused")
        )
        assert isinstance(error, RoutingUnavailable)

    def test_timeout_is_routing_unavailable(self, monkeypatch):
        import httpx as httpx_module

        error = self._classifier_error(
            monkeypatch, http_error=httpx_module.TimeoutException("timed out")
        )
        assert isinstance(error, RoutingUnavailable)

    def test_malformed_responses_give_needs_review_not_failed(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        texts = {
            "apple:zh-HK": CANTONESE,
            "apple:zh-CN": MANDARIN,
            "parakeet-pro:nvidia_parakeet-v3": "garbled",
        }

        def fake_run(config, audio_path, model_id, language_arg, speakers, runner=None, timeout_seconds=None):
            return texts.get(model_id)

        monkeypatch.setattr("workflow.services.transcription.run_mw_transcription", fake_run)

        def invalid(config, candidates):
            raise RoutingInvalid("response is not valid JSON")

        monkeypatch.setattr("workflow.services.routing.classify_with_omlx", invalid)
        _routed(config)
        recording = Recording.objects.get()
        result = route_one(config, recording)
        assert result["result"] == "needs_review"
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.NEEDS_REVIEW

    def test_no_body_or_secret_leaks_into_error(self, monkeypatch):
        import httpx as httpx_module

        secret = "planted-secret-value"

        def handler(request):
            assert request.headers["authorization"] == f"Bearer {secret}"
            return httpx_module.Response(
                200, text=f'{{"error": "leaked {secret}", "choices": "bad"}}'
            )

        config = make_config(
            Path("/tmp/x"),
            llm=make_config(Path("/tmp/x")).llm.__class__(
                provider="openai_compatible", base_url="http://omlx.test/v1",
                model="m", api_key_env="BRAIN_TEST_LLM_API_KEY",
                temperature=0.2, timeout_seconds=600,
            ),
        )
        monkeypatch.setenv("BRAIN_TEST_LLM_API_KEY", secret)
        with pytest.raises(RoutingInvalid) as excinfo:
            routing_service.classify_with_omlx(
                config, {"zh_hk": "a", "zh_cn": "b", "european": "c"},
                transport=httpx_module.MockTransport(handler),
            )
        assert secret not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Finding 11: timestamp validation, TZ-aware recorded_at, misc
# ---------------------------------------------------------------------------


class TestTimestampValidation:
    def _ok(self, segments):
        parsed = parse_mw_json(json.dumps({"segments": segments, "text": "x"}))
        return parsed

    def test_valid_fixture_still_parses(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "macwhisper" / "parakeet_json.json").read_text())
        assert parse_mw_json(json.dumps(fixture["example"])) is not None

    def test_boolean_timestamps_rejected(self):
        assert self._ok([{"start": True, "end": 5, "text": "a"}]) is None

    def test_negative_timestamps_rejected(self):
        assert self._ok([{"start": -1, "end": 5, "text": "a"}]) is None

    def test_end_before_start_rejected(self):
        assert self._ok([{"start": 10, "end": 5, "text": "a"}]) is None

    def test_non_finite_rejected(self):
        assert self._ok([{"start": 0, "end": float("inf"), "text": "a"}]) is None
        assert self._ok([{"start": float("nan"), "end": 5, "text": "a"}]) is None

    def test_out_of_order_segments_rejected(self):
        assert self._ok([
            {"start": 100, "end": 200, "text": "a"},
            {"start": 50, "end": 90, "text": "b"},
        ]) is None

    def test_slight_overlap_tolerated(self):
        parsed = self._ok([
            {"start": 0, "end": 110, "text": "a"},
            {"start": 100, "end": 200, "text": "b"},
        ])
        assert parsed is not None and len(parsed.segments) == 2

    def test_non_string_speaker_rejected(self):
        assert self._ok([{"start": 0, "end": 5, "text": "a", "speaker": 7}]) is None

    def test_non_string_text_rejected(self):
        assert self._ok([{"start": 0, "end": 5, "text": 3}]) is None


class TestTimezoneAwareRecordedAt:
    def test_recorded_at_is_aware_and_helsinki_default(self, tmp_path):
        from workflow.services.pipeline import derive_recorded_at

        value = derive_recorded_at("2024-03-01_153045.wav")
        assert value is not None
        assert value.tzinfo is not None
        assert value.utcoffset().total_seconds() == 2 * 3600  # Helsinki in March

    def test_dst_aware(self, tmp_path):
        from workflow.services.pipeline import derive_recorded_at

        summer = derive_recorded_at("2024-07-01_153045.wav")
        winter = derive_recorded_at("2024-01-01_153045.wav")
        assert summer.utcoffset().total_seconds() == 3 * 3600
        assert winter.utcoffset().total_seconds() == 2 * 3600

    def test_config_timezone_override(self, tmp_path):
        from workflow.services.pipeline import derive_recorded_at

        value = derive_recorded_at("2024-03-01_153045.wav", "UTC")
        assert value.utcoffset().total_seconds() == 0

    def test_unparseable_filename_returns_none(self):
        from workflow.services.pipeline import derive_recorded_at

        assert derive_recorded_at("memo.wav") is None


class TestReconciliationScope:
    def test_sources_outside_current_inbox_are_skipped(self, tmp_path):
        from dataclasses import replace

        from workflow.services.ingest import ingest

        config = seed_inbox(tmp_path)
        hash_inbox(config)
        source = AudioSource.objects.get()
        # Simulate the configured inbox moving elsewhere.
        new_inbox = tmp_path / "elsewhere"
        new_inbox.mkdir()
        config = replace(config, storage=replace(config.storage, inbox=new_inbox))
        report = ingest(config)
        source.refresh_from_db()
        assert source.discovery_note == "outside_current_inbox"
        assert source.presence == "present"  # never touched
        assert str(source.path) in report.ignored_paths
