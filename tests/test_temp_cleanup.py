"""Namespace-bounded orphan temp-dir cleanup (integrated with recovery).

Simulated process-death only: SIGKILL skips ``finally`` cleanup, so the
sweeper must remove orphaned attempt dirs — and ONLY them — from the
strictly bounded data/temp/routing and data/temp/transcription
namespaces. No path is ever taken blindly from the database.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from workflow.models import AttemptStage, ProcessingAttempt, Recording
from workflow.services.pipeline import recover_interruptions
from workflow.services.tempcleanup import sweep_orphan_attempt_dirs

from factories import make_config

pytestmark = pytest.mark.django_db


def _attempt_dir(tmp_path, recording, stage: str, ordinal: int) -> Path:
    root = Path(make_config(tmp_path).storage.temp) / stage / str(recording.pk) / f"attempt_{ordinal}"
    root.mkdir(parents=True, exist_ok=True)
    (root / "normalized.wav").write_bytes(b"payload")
    return root


def _recording(sha_suffix: str = "") -> Recording:
    return Recording.objects.create(sha256=f"tc-{Recording.objects.count()}{sha_suffix}")


class TestSweepOrphanAttemptDirs:
    def test_removes_orphans_without_unfinished_attempts(self, tmp_path):
        recording = _recording()
        orphan = _attempt_dir(tmp_path, recording, "transcription", 1)
        removed = sweep_orphan_attempt_dirs(make_config(tmp_path))
        assert removed["transcription"] == 1
        assert not orphan.exists()

    def test_keeps_dir_for_live_unfinished_attempt(self, tmp_path):
        recording = _recording()
        live = _attempt_dir(tmp_path, recording, "transcription", 1)
        ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=1  # finished_at null
        )
        removed = sweep_orphan_attempt_dirs(make_config(tmp_path))
        assert removed["transcription"] == 0
        assert live.exists()

    def test_removes_completed_attempt_dirs(self, tmp_path):
        from django.utils import timezone as tz

        recording = _recording()
        done = _attempt_dir(tmp_path, recording, "transcription", 2)
        ProcessingAttempt.objects.create(
            recording=recording,
            stage=AttemptStage.TRANSCRIPTION,
            ordinal=1,
            finished_at=tz.now(),  # completed: no longer needs temp files
        )
        sweep_orphan_attempt_dirs(make_config(tmp_path))
        assert not done.exists()

    def test_keeps_routing_namespace_aware_of_stage(self, tmp_path):
        recording = _recording()
        routing_dir = _attempt_dir(tmp_path, recording, "routing", 1)
        transcription_dir = _attempt_dir(tmp_path, recording, "transcription", 1)
        # Unfinished TRANSCRIPTION attempt must not save the ROUTING dir.
        ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=1
        )
        removed = sweep_orphan_attempt_dirs(make_config(tmp_path))
        assert not routing_dir.exists()
        assert removed["routing"] == 1
        assert removed["transcription"] == 0  # live attempt dir untouched

    def test_ignores_invalid_and_malicious_names(self, tmp_path):
        config = make_config(tmp_path)
        root = Path(config.storage.temp) / "transcription"
        decoys = [
            root / ".." / "escaped",
            root / "not-a-uuid",
            root / "12345",  # not a UUID
            root / str(uuid.uuid4()) / "attempt_notanumber",
            root / str(uuid.uuid4()) / "attempt_-1",
        ]
        escaped = tmp_path / "data" / "escaped-target"
        escaped.mkdir(parents=True)
        (escaped / "precious.txt").write_text("keep me")
        for decoy in decoys:
            decoy.mkdir(parents=True, exist_ok=True)
        removed = sweep_orphan_attempt_dirs(config)
        assert removed["transcription"] == 0
        assert escaped.exists()
        assert (escaped / "precious.txt").exists()

    def test_symlinks_never_followed_or_deleted(self, tmp_path):
        config = make_config(tmp_path)
        recording = _recording()
        outside = tmp_path / "outside-payload"
        outside.mkdir()
        (outside / "data.txt").write_text("precious")
        link = Path(config.storage.temp) / "transcription" / str(recording.pk) / "attempt_1"
        link.mkdir(parents=True, exist_ok=True)
        target = link / "normalized.wav"
        target.symlink_to(outside / "data.txt")
        sweep_orphan_attempt_dirs(config)
        assert outside.exists()
        assert (outside / "data.txt").exists()

    def test_namespace_outside_root_never_touched(self, tmp_path):
        config = make_config(tmp_path)
        # A valid-looking path that is actually a symlinked recording dir
        # pointing outside must be skipped, not followed.
        recording = _recording()
        outside = tmp_path / "outside-recording-dir"
        outside.mkdir()
        (outside / "f.txt").write_text("keep")
        root = Path(config.storage.temp) / "transcription"
        link_dir = root / str(recording.pk)
        link_dir.mkdir(parents=True)
        (link_dir / "attempt_1").symlink_to(outside)
        sweep_orphan_attempt_dirs(config)
        assert outside.exists()

    def test_idempotent(self, tmp_path):
        recording = _recording()
        _attempt_dir(tmp_path, recording, "transcription", 1)
        _attempt_dir(tmp_path, recording, "routing", 2)
        config = make_config(tmp_path)
        first = sweep_orphan_attempt_dirs(config)
        second = sweep_orphan_attempt_dirs(config)
        assert first == {"transcription": 1, "routing": 1}
        assert second == {"transcription": 0, "routing": 0}

    def test_missing_namespace_is_safe(self, tmp_path):
        config = make_config(tmp_path)
        assert sweep_orphan_attempt_dirs(config) == {"transcription": 0, "routing": 0}


class TestRecoveryIntegration:
    def test_recover_interruptions_reports_temp_dirs_removed(self, tmp_path):
        recording = _recording()
        _attempt_dir(tmp_path, recording, "transcription", 3)
        recovery = recover_interruptions(make_config(tmp_path))
        assert recovery["temp_dirs_removed"]["transcription"] == 1
        assert recovery["temp_dirs_removed"]["routing"] == 0

    def test_recovery_after_process_death_cleans_and_preserves_state(self, tmp_path):
        """End-to-end: attempt recovery plus the orphan sweep in one pass.
        The unfinished attempt is marked interrupted first, so its temp
        dir no longer corresponds to an unfinished attempt and is swept
        in the same recovery pass."""
        recording = _recording()
        attempt = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=1
        )
        temp_dir = _attempt_dir(tmp_path, recording, "transcription", 1)
        recovery = recover_interruptions(make_config(tmp_path))
        attempt.refresh_from_db()
        assert attempt.outcome == "interrupted"
        assert attempt.finished_at is not None
        assert not temp_dir.exists()
        assert recovery["recovered_attempts"] >= 1
        assert recovery["temp_dirs_removed"]["transcription"] == 1
