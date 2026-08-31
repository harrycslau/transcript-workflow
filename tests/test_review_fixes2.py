"""Regression tests for the follow-up Step 2 review findings:

1. Fresh CLI processes exit 1 with concise errors (no traceback) for
   missing/malformed config - exercised via real subprocesses.
2. Failed source validation never falls back to an unverified source;
   deleted sources park cleanly without fake attempts.
3. Pre-stage validation enforces the current inbox boundary.
4. Routing-disabled performs no hashing/extraction/subprocess/network work.
"""

from __future__ import annotations

import os
import subprocess
import sys
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
    Transcript,
)
from workflow.services import pipeline as pipeline_module
from workflow.services.pipeline import manual_route, route_one, transcribe_one

from factories import make_config, write_cli_config
from test_pipeline import (
    MW_JSON,
    hash_inbox,
    mock_full_transcription,
    mock_routing,
    run_pipeline,
    seed_inbox,
    write_wav,
)

pytestmark = pytest.mark.django_db

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Finding 1: fresh-process CLI config errors
# ---------------------------------------------------------------------------


def run_fresh_brain(args: list[str], brain_config: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "BRAIN_CONFIG": brain_config}
    env.pop("DJANGO_SETTINGS_MODULE", None)
    return subprocess.run(
        [sys.executable, "-m", "brainlib.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=120,
    )


class TestFreshProcessConfigErrors:
    MISSING = "/private/tmp/definitely-missing-brain-config.yaml"

    def test_missing_config_ingest_fresh_process(self, tmp_path):
        result = run_fresh_brain(["ingest", "--json"], self.MISSING)
        assert result.returncode == 1
        assert "Traceback" not in result.stderr
        assert "Traceback" not in result.stdout
        assert "Configuration file not found" in result.stderr
        assert result.stdout == "" or "error:" not in result.stdout

    def test_missing_config_status_fresh_process(self, tmp_path):
        result = run_fresh_brain(["status", "--json"], self.MISSING)
        assert result.returncode == 1
        assert "Traceback" not in result.stderr
        assert "Configuration file not found" in result.stderr

    @pytest.mark.parametrize("argv", [
        ["route", "--json"],
        ["transcribe", "--json"],
        ["run", "--json"],
        ["retry", "some-id", "--json"],
        ["review", "--json"],
        ["transcripts", "some-id", "--json"],
    ])
    def test_all_commands_missing_config_fresh_process(self, tmp_path, argv):
        result = run_fresh_brain(argv, self.MISSING)
        assert result.returncode == 1
        assert "Traceback" not in result.stderr
        assert "error:" in result.stderr

    def test_malformed_config_fresh_process(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("storage: [unclosed\n  bad: : yaml", encoding="utf-8")
        result = run_fresh_brain(["ingest", "--json"], str(bad))
        assert result.returncode == 1
        assert "Traceback" not in result.stderr
        assert "Malformed YAML" in result.stderr

    def test_error_message_contains_only_selected_path(self, tmp_path):
        result = run_fresh_brain(["ingest", "--json"], self.MISSING)
        assert self.MISSING in result.stderr  # the selected config path is expected
        assert "/Users/" not in result.stderr.replace(str(REPO_ROOT), "")


# ---------------------------------------------------------------------------
# Finding 2: validation outcomes never fall back to unverified sources
# ---------------------------------------------------------------------------


def _forbid_processing(monkeypatch):
    """Any sampling, MacWhisper, or classifier call fails the test."""

    def _no(*args, **kwargs):
        raise AssertionError("must not be called for an unverified source")

    monkeypatch.setattr("workflow.services.audiosamples.extract_samples", _no)
    monkeypatch.setattr("workflow.services.transcription.run_mw_transcription", _no)
    monkeypatch.setattr("workflow.services.routing.classify_with_omlx", _no)
    monkeypatch.setattr("workflow.services.transcription.transcribe_recording", _no)


class TestDeletedSource:
    def test_deleted_source_before_routing_parks_cleanly(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        _forbid_processing(monkeypatch)
        recording = Recording.objects.get()
        recording.processing_status = ProcessingStatus.ROUTING
        recording.save()
        source = AudioSource.objects.get()
        Path(source.path).unlink()

        result = route_one(config, Recording.objects.get(pk=recording.pk))

        assert result["result"] == "parked"
        assert result["reason"] == "source_missing"
        source.refresh_from_db()
        recording.refresh_from_db()
        assert source.presence == "missing"
        assert source.missing_at is not None
        assert recording.audio_status == "missing"
        assert recording.processing_status == ProcessingStatus.ROUTING  # recoverable
        assert ProcessingAttempt.objects.count() == 0  # no fake attempt

    def test_deleted_source_before_transcription_parks_cleanly(self, tmp_path, monkeypatch):
        from factories import default_routing

        config = seed_inbox(tmp_path)
        config = make_config(tmp_path, file_stable_seconds=1, routing=default_routing(auto_transcribe=False))
        hash_inbox(config)
        mock_routing(monkeypatch, confidence=0.95)
        run_pipeline(config)  # needs_review (auto_transcribe off)
        recording = Recording.objects.get()
        manual_route(recording, "cantonese")  # -> ready_to_transcribe
        source = AudioSource.objects.get()
        Path(source.path).unlink()
        _forbid_processing(monkeypatch)

        result = transcribe_one(config, Recording.objects.get(pk=recording.pk))

        assert result["result"] == "parked"
        assert result["reason"] == "source_missing"
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.READY_TO_TRANSCRIBE  # recoverable
        assert ProcessingAttempt.objects.filter(stage="transcription").count() == 0
        assert recording.audio_status == "missing"

    def test_missing_canonical_with_valid_duplicate_uses_duplicate(self, tmp_path, monkeypatch):
        from test_review_fixes import PRISTINE_TRANSCRIBE

        config = seed_inbox(tmp_path)
        hash_inbox(config)
        recording = Recording.objects.get()
        original = AudioSource.objects.get()

        # A second identical copy at another path, attached to the recording.
        import shutil

        alt_path = config.storage.inbox / "alt.wav"
        shutil.copyfile(original.path, alt_path)
        alt = AudioSource.objects.create(
            recording=recording,
            path=str(alt_path),
            path_identity=str(alt_path).casefold(),
            original_filename="alt.wav",
            file_size=original.file_size,
            file_mtime=alt_path.stat().st_mtime,
            presence="present",
            discovery_state="hashed",
        )
        # Deterministic: the first file is canonical, the duplicate is not.
        original.is_canonical = True
        original.save(update_fields=["is_canonical"])
        alt.is_canonical = False
        alt.save(update_fields=["is_canonical"])
        recording.processing_status = ProcessingStatus.READY_TO_TRANSCRIBE
        recording.save()
        from workflow.models import RoutingDecision, RoutingMethod

        RoutingDecision.objects.create(
            recording=recording, ordinal=1, route_suggestion="cantonese",
            profile_name="cantonese", model_id="apple:zh-HK",
            method=RoutingMethod.MANUAL, routing_verified=True, is_active=True,
        )

        # The canonical file is deleted right before transcription.
        Path(original.path).unlink()
        _forbid_processing(monkeypatch)

        used_paths = []

        def fake(config, rec, source_path, model_id, language_arg, runner=None):
            used_paths.append(str(source_path))

            def ok_runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, stdout=MW_JSON, stderr="")

            return PRISTINE_TRANSCRIBE(
                config, rec, source_path, model_id=model_id, language_arg=language_arg, runner=ok_runner
            )

        monkeypatch.setattr("workflow.services.transcription.transcribe_recording", fake)
        result = transcribe_one(config, Recording.objects.get(pk=recording.pk))

        assert result["result"] == "transcribed"
        assert used_paths == [str(alt_path)]  # alternate source independently validated and used
        alt.refresh_from_db()
        assert alt.is_canonical is True
        original.refresh_from_db()
        assert original.presence == "missing"
        recording.refresh_from_db()
        assert recording.audio_status == "present"


class TestSameSizeReplacement:
    def test_same_size_same_mtime_replacement_detected_by_sha(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        recording = Recording.objects.get()
        recording.processing_status = ProcessingStatus.ROUTING
        recording.save()
        source = AudioSource.objects.get()
        path = Path(source.path)
        original_size = path.stat().st_size

        # Replace with different content of identical size; restore mtime.
        replacement = tmp_path / "replacement.wav"
        write_wav(replacement, seconds=5.0, amplitude=321)
        assert replacement.stat().st_size == original_size
        replacement_bytes = replacement.read_bytes()
        path.write_bytes(replacement_bytes)
        os.utime(path, (path.stat().st_atime, source.file_mtime))

        _forbid_processing(monkeypatch)
        result = route_one(config, Recording.objects.get(pk=recording.pk))

        assert result["result"] == "parked"
        assert result["reason"] == "source_changed"
        source.refresh_from_db()
        assert source.recording_id is None  # detached into the rehash workflow
        assert source.stable_since is not None

    def test_changed_content_never_processed_under_old_identity(self, tmp_path, monkeypatch):
        config = seed_inbox(tmp_path)
        hash_inbox(config)
        mock_routing(monkeypatch, confidence=0.95)
        run_pipeline(config)
        recording = Recording.objects.get()
        source = AudioSource.objects.get()
        write_wav(Path(source.path), seconds=3.0, amplitude=321)

        _forbid_processing(monkeypatch)
        result = route_one(config, Recording.objects.get(pk=recording.pk))
        assert result["result"] == "parked"
        source.refresh_from_db()
        assert source.recording_id is None


# ---------------------------------------------------------------------------
# Finding 3: inbox boundary enforcement in pre-stage validation
# ---------------------------------------------------------------------------


class TestInboxBoundary:
    def _moved_inbox_config(self, tmp_path, monkeypatch):
        """Ingest from inbox A, then reconfigure to inbox B."""
        from dataclasses import replace

        config = seed_inbox(tmp_path)
        hash_inbox(config)
        new_inbox = tmp_path / "new-inbox"
        new_inbox.mkdir()
        moved = replace(config, storage=replace(config.storage, inbox=new_inbox))
        return config, moved

    def _forbid_filesystem(self, monkeypatch):
        """stat/open/SHA-256 must never run against the old source."""

        def _no_stat(*args, **kwargs):
            raise AssertionError("os.stat must not be called for an outside-inbox source")

        def _no_hash(*args, **kwargs):
            raise AssertionError("sha256_file must not be called for an outside-inbox source")

        monkeypatch.setattr("workflow.services.pipeline.os.stat", _no_stat)
        monkeypatch.setattr("workflow.services.ingest.sha256_file", _no_hash)
        _forbid_processing(monkeypatch)

    def test_route_one_parks_outside_inbox_source(self, tmp_path, monkeypatch):
        original_config, moved_config = self._moved_inbox_config(tmp_path, monkeypatch)
        recording = Recording.objects.get()
        recording.processing_status = ProcessingStatus.ROUTING
        recording.save()
        self._forbid_filesystem(monkeypatch)

        result = route_one(moved_config, Recording.objects.get(pk=recording.pk))

        assert result["result"] == "parked"
        assert result["reason"] == "outside_current_inbox"
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.ROUTING  # untouched, recoverable
        assert Recording.objects.count() == 1  # history intact

    def test_transcribe_one_parks_outside_inbox_source(self, tmp_path, monkeypatch):
        original_config, moved_config = self._moved_inbox_config(tmp_path, monkeypatch)
        mock_routing(monkeypatch, confidence=0.95)
        run_pipeline(original_config)  # route using the ORIGINAL inbox config
        recording = Recording.objects.get()
        recording.processing_status = ProcessingStatus.READY_TO_TRANSCRIBE
        recording.save()
        self._forbid_filesystem(monkeypatch)

        result = transcribe_one(moved_config, Recording.objects.get(pk=recording.pk))

        assert result["result"] == "parked"
        assert result["reason"] == "outside_current_inbox"
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.READY_TO_TRANSCRIBE
        assert Transcript.objects.count() == 0

    def test_symlink_resolving_outside_is_rejected(self, tmp_path, monkeypatch):
        from dataclasses import replace

        from workflow.services.ingest import is_inside_inbox

        config = seed_inbox(tmp_path)
        outside = write_wav(tmp_path / "outside.wav")
        link = config.storage.inbox / "link.wav"
        os.symlink(outside, link)
        assert is_inside_inbox(link, config.storage.inbox) is False

        hash_inbox(config)
        recording = Recording.objects.get()
        recording.processing_status = ProcessingStatus.ROUTING
        recording.save()
        # Simulate a stale DB row pointing at the symlink (only source).
        AudioSource.objects.create(
            recording=recording,
            path=str(link),
            path_identity=str(link).casefold(),
            original_filename="link.wav",
            presence="present",
            is_canonical=True,
            discovery_state="hashed",
        )
        AudioSource.objects.exclude(path=str(link)).delete()
        self._forbid_filesystem(monkeypatch)
        result = route_one(config, Recording.objects.get(pk=recording.pk))
        assert result["result"] == "parked"
        assert result["reason"] == "outside_current_inbox"

    def test_alternate_source_inside_current_inbox_is_used(self, tmp_path, monkeypatch):
        from dataclasses import replace

        config, moved_config = self._moved_inbox_config(tmp_path, monkeypatch)
        recording = Recording.objects.get()
        recording.processing_status = ProcessingStatus.ROUTING
        recording.save()

        # A second copy of the same content inside the NEW inbox.
        import shutil

        original = AudioSource.objects.get()
        alt_path = moved_config.storage.inbox / "inside.wav"
        shutil.copyfile(original.path, alt_path)
        AudioSource.objects.create(
            recording=recording,
            path=str(alt_path),
            path_identity=str(alt_path).casefold(),
            original_filename="inside.wav",
            file_size=original.file_size,
            file_mtime=alt_path.stat().st_mtime,
            presence="present",
            discovery_state="hashed",
        )

        used = {}
        real_run = pipeline_module.routing_service

        def spy_route(config, recording, source_path, attempt_dir, **kwargs):
            used["path"] = str(source_path)
            raise RuntimeError("stop before full routing")  # we only need the source choice

        monkeypatch.setattr("workflow.services.routing.route_recording", spy_route)
        result = route_one(moved_config, Recording.objects.get(pk=recording.pk))
        assert result["result"] == "failed"  # stopped intentionally after source choice
        assert used["path"] == str(alt_path)


# ---------------------------------------------------------------------------
# Additional check: routing disabled does no expensive work
# ---------------------------------------------------------------------------


class TestRoutingDisabledShortCircuit:
    def test_disabled_routing_performs_no_hashing_extraction_subprocess_or_network(self, tmp_path, monkeypatch):
        from factories import default_routing

        config = seed_inbox(tmp_path)
        config = make_config(
            tmp_path, file_stable_seconds=1, routing=default_routing(enabled=False)
        )
        hash_inbox(config)
        recording = Recording.objects.get()
        recording.processing_status = ProcessingStatus.ROUTING
        recording.save()

        def _no(*args, **kwargs):
            raise AssertionError("disabled routing must not do expensive work")

        monkeypatch.setattr("workflow.services.ingest.sha256_file", _no)
        monkeypatch.setattr("workflow.services.audiosamples.extract_samples", _no)
        monkeypatch.setattr("workflow.services.transcription.run_mw_transcription", _no)
        monkeypatch.setattr("workflow.services.routing.classify_with_omlx", _no)
        monkeypatch.setattr("workflow.services.routing.route_recording", _no)

        result = route_one(config, Recording.objects.get(pk=recording.pk))

        assert result["result"] == "needs_review"
        assert result["reason"] == "routing_disabled"
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.NEEDS_REVIEW
        decision = RoutingDecision.objects.get(recording=recording, is_active=True)
        assert decision.reason_code == "routing_disabled"
