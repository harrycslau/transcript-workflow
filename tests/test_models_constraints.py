"""Database constraint and path-identity tests for the Step 2 schema."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone as dj_timezone

from workflow.models import (
    AudioSource,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
    RoutingDecision,
    RoutingMethod,
    Transcript,
)
from workflow.services.transcription import next_ordinal

pytestmark = pytest.mark.django_db


def make_recording(**kwargs) -> Recording:
    defaults = {"sha256": f"hash-{Recording.objects.count()}"}
    defaults.update(kwargs)
    return Recording.objects.create(**defaults)


class TestConstraints:
    def _finished_attempt(self, recording, ordinal: int) -> ProcessingAttempt:
        attempt = ProcessingAttempt.objects.create(recording=recording, stage="transcription", ordinal=ordinal)
        attempt.outcome = "success"
        attempt.finished_at = dj_timezone.now()
        attempt.save()
        return attempt

    def test_only_one_active_transcript_per_recording(self):
        recording = make_recording()
        attempt1 = self._finished_attempt(recording, 1)
        attempt2 = self._finished_attempt(recording, 2)
        Transcript.objects.create(recording=recording, attempt=attempt1, is_active=True)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Transcript.objects.create(recording=recording, attempt=attempt2, is_active=True)

    def test_superseded_transcript_allows_new_active(self):
        recording = make_recording()
        attempt1 = self._finished_attempt(recording, 1)
        attempt2 = self._finished_attempt(recording, 2)
        first = Transcript.objects.create(recording=recording, attempt=attempt1, is_active=True)
        first.is_active = False
        first.superseded_at = dj_timezone.now()
        first.save()
        Transcript.objects.create(recording=recording, attempt=attempt2, is_active=True)  # OK

    def test_only_one_active_routing_decision(self):
        recording = make_recording()
        RoutingDecision.objects.create(
            recording=recording, ordinal=1, route_suggestion="cantonese",
            profile_name="cantonese", model_id="apple:zh-HK", method=RoutingMethod.AUTOMATIC,
        )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                RoutingDecision.objects.create(
                    recording=recording, ordinal=2, route_suggestion="mandarin",
                    profile_name="mandarin", model_id="apple:zh-CN", method=RoutingMethod.MANUAL,
                )

    def test_only_one_unfinished_attempt_per_stage(self):
        recording = make_recording()
        ProcessingAttempt.objects.create(recording=recording, stage="transcription", ordinal=1)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                ProcessingAttempt.objects.create(recording=recording, stage="transcription", ordinal=2)

    def test_unfinished_allowed_per_different_stage(self):
        recording = make_recording()
        ProcessingAttempt.objects.create(recording=recording, stage="transcription", ordinal=1)
        ProcessingAttempt.objects.create(recording=recording, stage="routing", ordinal=1)  # OK

    def test_finished_attempt_allows_new_one(self):
        recording = make_recording()
        first = ProcessingAttempt.objects.create(recording=recording, stage="transcription", ordinal=1)
        first.outcome = "success"
        first.finished_at = dj_timezone.now()
        first.save()
        ProcessingAttempt.objects.create(recording=recording, stage="transcription", ordinal=2)  # OK

    def test_attempt_ordinal_unique(self):
        recording = make_recording()
        attempt = ProcessingAttempt.objects.create(recording=recording, stage="routing", ordinal=1)
        attempt.outcome = "success"
        attempt.finished_at = dj_timezone.now()
        attempt.save()
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                ProcessingAttempt.objects.create(recording=recording, stage="routing", ordinal=1)

    def test_next_ordinal_increments(self):
        recording = make_recording()
        assert next_ordinal(recording, "routing") == 1
        ProcessingAttempt.objects.create(
            recording=recording, stage="routing", ordinal=1, outcome="success", finished_at=dj_timezone.now()
        )
        assert next_ordinal(recording, "routing") == 2

    def test_audio_source_path_identity_unique(self):
        recording = make_recording()
        AudioSource.objects.create(
            recording=recording, path="/x/a.wav", path_identity="/x/a.wav",
            original_filename="a.wav",
        )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                AudioSource.objects.create(
                    recording=recording, path="/x/A.wav", path_identity="/x/a.wav",
                    original_filename="a.wav",
                )


class TestConfigImmutability:
    def test_load_config_never_writes_the_config_file(self, tmp_path):
        """The loader must never modify the user's configuration file."""
        import hashlib
        import yaml

        from brainlib.config import load_config

        data = {
            "storage": {
                "inbox": str(tmp_path / "inbox"),
                "database": str(tmp_path / "db.sqlite3"),
                "transcripts": str(tmp_path / "transcripts"),
                "exports": str(tmp_path / "exports"),
                "logs": str(tmp_path / "logs"),
                "temp": str(tmp_path / "temp"),
            },
            "macwhisper": {
                "command": "/usr/local/bin/mw",
                "model": None,
                "routing": {
                    "profiles": {
                        "cantonese": {"model": "apple:zh-HK", "language": None},
                        "mandarin": {"model": "apple:zh-CN", "language": None},
                        "european": {"model": "parakeet-pro:nvidia_parakeet-v3", "language": None},
                    }
                },
            },
        }
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
        before = hashlib.sha256(config_path.read_bytes()).hexdigest()

        for _ in range(3):
            load_config(config_path)

        after = hashlib.sha256(config_path.read_bytes()).hexdigest()
        assert before == after
