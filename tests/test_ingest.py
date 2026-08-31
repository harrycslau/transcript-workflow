"""Tests for inbox discovery, stability, hashing, deduplication."""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import wave
from pathlib import Path

import pytest
from django.utils import timezone as dj_timezone

from workflow.models import AudioStatus, AudioSource, DiscoveryState, ProcessingStatus, Recording
pytestmark = pytest.mark.django_db

from workflow.services.ingest import (
    discover_wavs,
    ingest,
    normalize_identity,
    sha256_file,
)


def write_wav(path: Path, seconds: float = 1.0, rate: int = 16000, amplitude: int = 8000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(struct.pack("<h", amplitude * (i % 7 - 3)) for i in range(frames)))
    return path


def make_config(tmp_path, **kwargs):
    from factories import make_config as factory_make_config

    return factory_make_config(tmp_path, **kwargs)


def test_sha256_file(tmp_path):
    path = tmp_path / "x.bin"
    path.write_bytes(b"hello world")
    assert sha256_file(path) == hashlib.sha256(b"hello world").hexdigest()


class TestDiscovery:
    def test_case_insensitive_wav_extension(self, tmp_path):
        config = make_config(tmp_path)
        write_wav(config.storage.inbox / "A.WAV")
        write_wav(config.storage.inbox / "b.Wav")
        (config.storage.inbox / "notes.txt").write_text("not audio")
        found = discover_wavs(config.storage.inbox)
        assert len(found) == 2
        assert all(p.suffix.casefold() == ".wav" for p in found)

    def test_identity_paths_are_absolute(self, tmp_path):
        config = make_config(tmp_path)
        write_wav(config.storage.inbox / "rec.wav")
        found = discover_wavs(config.storage.inbox)
        assert found[0].is_absolute()

    def test_symlink_outside_inbox_ignored(self, tmp_path):
        config = make_config(tmp_path)
        outside = tmp_path / "outside"
        write_wav(outside / "secret.wav")
        inbox = config.storage.inbox
        inbox.mkdir(parents=True, exist_ok=True)
        os.symlink(outside / "secret.wav", inbox / "link.wav")
        assert discover_wavs(inbox) == []

    def test_symlink_outside_inbox_not_followed_for_identity(self, tmp_path):
        config = make_config(tmp_path)
        outside = tmp_path / "outside.wav"
        write_wav(outside)
        inbox = config.storage.inbox
        inbox.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, inbox / "link.wav")
        assert normalize_identity(inbox / "link.wav", inbox) is None

    def test_normal_file_inside_inbox_resolves(self, tmp_path):
        config = make_config(tmp_path)
        write_wav(config.storage.inbox / "rec.wav")
        identity = normalize_identity(config.storage.inbox / "rec.wav", config.storage.inbox)
        assert identity is not None
        assert identity.name == "rec.wav"


class TestStabilityAndHashing:
    def test_new_file_waits_for_stability(self, tmp_path):
        config = make_config(tmp_path, file_stable_seconds=3600)
        write_wav(config.storage.inbox / "rec.wav")
        report = ingest(config)
        assert report.new_sources and not report.hashed
        assert report.skipped_unstable
        source = AudioSource.objects.get()
        assert source.discovery_state == DiscoveryState.OBSERVING
        assert source.recording_id is None  # pre-hash: explicit persisted state

    def test_stable_file_is_hashed_and_registered(self, tmp_path):
        config = make_config(tmp_path, file_stable_seconds=1)
        path = write_wav(config.storage.inbox / "rec.wav")
        # Backdate the stability observation.
        ingest(config)
        source = AudioSource.objects.get()
        AudioSource.objects.filter(pk=source.pk).update(
            stable_since=dj_timezone.now() - __import__("datetime").timedelta(seconds=120)
        )
        report = ingest(config)
        assert len(report.hashed) == 1
        recording = Recording.objects.get()
        assert recording.sha256 == sha256_file(path)
        assert recording.processing_status == ProcessingStatus.DISCOVERED
        assert recording.audio_status == AudioStatus.PRESENT
        source.refresh_from_db()
        assert source.discovery_state == DiscoveryState.HASHED
        assert source.recording_id == recording.pk
        assert source.is_canonical is True

    def test_repeated_ingest_is_idempotent(self, tmp_path):
        config = make_config(tmp_path, file_stable_seconds=1)
        write_wav(config.storage.inbox / "rec.wav")
        ingest(config)
        AudioSource.objects.update(stable_since=dj_timezone.now() - __import__("datetime").timedelta(seconds=120))
        ingest(config)
        first = ingest(config)
        assert first.hashed == [] and first.new_sources == []
        assert Recording.objects.count() == 1
        assert AudioSource.objects.count() == 1

    def test_file_change_during_hashing_discards_result(self, tmp_path, monkeypatch):
        config = make_config(tmp_path, file_stable_seconds=1)
        path = write_wav(config.storage.inbox / "rec.wav")
        ingest(config)
        AudioSource.objects.update(stable_since=dj_timezone.now() - __import__("datetime").timedelta(seconds=120))

        real_sha = sha256_file(path)
        calls = {"n": 0}

        def changing_sha256(path_arg):
            calls["n"] += 1
            # Simulate the file growing during hashing.
            path = Path(path_arg)
            if calls["n"] == 1:
                with open(path, "ab") as handle:
                    handle.write(b"extra data")
            return real_sha

        monkeypatch.setattr("workflow.services.ingest.sha256_file", changing_sha256)
        report = ingest(config)
        assert report.hashed == []
        source = AudioSource.objects.get()
        assert source.recording_id is None
        assert source.discovery_state == DiscoveryState.OBSERVING
        assert Recording.objects.count() == 0

    def test_interrupted_hashing_recovers(self, tmp_path):
        """A source persisted as HASHING with null recording re-hashes safely."""
        config = make_config(tmp_path, file_stable_seconds=1)
        write_wav(config.storage.inbox / "rec.wav")
        ingest(config)
        source = AudioSource.objects.get()
        # Simulate an interrupted run: state HASHING, no recording attached.
        AudioSource.objects.filter(pk=source.pk).update(
            discovery_state=DiscoveryState.HASHING,
            stable_since=None,
        )
        report = ingest(config)
        assert len(report.hashed) == 1
        assert Recording.objects.count() == 1

    def test_failed_hash_resets_to_observing_for_retry(self, tmp_path, monkeypatch):
        config = make_config(tmp_path, file_stable_seconds=1)
        write_wav(config.storage.inbox / "rec.wav")
        ingest(config)
        AudioSource.objects.update(stable_since=dj_timezone.now() - __import__("datetime").timedelta(seconds=120))

        def broken_sha(path):
            raise OSError("disk error")

        monkeypatch.setattr("workflow.services.ingest.sha256_file", broken_sha)
        report = ingest(config)
        assert report.hashed == []
        source = AudioSource.objects.get()
        assert source.discovery_state == DiscoveryState.FAILED
        assert "hash failed" in source.discovery_note

        # Next run resets FAILED -> OBSERVING (stability wait restarts);
        # the run after that retries hashing successfully.
        monkeypatch.setattr("workflow.services.ingest.sha256_file", sha256_file)
        ingest(config)
        AudioSource.objects.update(stable_since=dj_timezone.now() - __import__("datetime").timedelta(seconds=120))
        report = ingest(config)
        assert len(report.hashed) == 1


class TestDuplicates:
    def test_duplicate_content_same_recording_two_sources(self, tmp_path):
        config = make_config(tmp_path, file_stable_seconds=1)
        config.storage.inbox.mkdir(parents=True, exist_ok=True)
        content = write_wav(tmp_path / "master.wav")
        shutil.copyfile(content, config.storage.inbox / "2024-03-01_120000.wav")
        ingest(config)
        AudioSource.objects.update(stable_since=dj_timezone.now() - __import__("datetime").timedelta(seconds=120))
        ingest(config)
        # Same content, different filename and folder.
        (config.storage.inbox / "sub").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(content, config.storage.inbox / "sub" / "another-name.wav")
        ingest(config)  # observe the new path
        AudioSource.objects.update(stable_since=dj_timezone.now() - __import__("datetime").timedelta(seconds=120))
        ingest(config)  # now hash it -> dedupe onto the existing recording
        assert Recording.objects.count() == 1
        assert AudioSource.objects.count() == 2
        sources = list(AudioSource.objects.all())
        assert len({s.recording_id for s in sources}) == 1
        assert sum(1 for s in sources if s.is_canonical) == 1

    def test_same_filename_different_folders_are_deduped_by_content(self, tmp_path):
        config = make_config(tmp_path, file_stable_seconds=1)
        config.storage.inbox.mkdir(parents=True, exist_ok=True)
        wav = write_wav(tmp_path / "master.wav")
        (config.storage.inbox / "a").mkdir(parents=True, exist_ok=True) or shutil.copyfile(wav, config.storage.inbox / "a" / "2024-03-01_120000.wav")
        other = write_wav(tmp_path / "other.wav", amplitude=1234)
        (config.storage.inbox / "b").mkdir(parents=True, exist_ok=True) or shutil.copyfile(other, config.storage.inbox / "b" / "2024-03-01_120000.wav")
        ingest(config)
        AudioSource.objects.update(stable_since=dj_timezone.now() - __import__("datetime").timedelta(seconds=120))
        ingest(config)
        assert AudioSource.objects.count() == 2
        assert Recording.objects.count() == 2  # different content

    def test_case_insensitive_identity_single_source(self, tmp_path):
        config = make_config(tmp_path, file_stable_seconds=1)
        write_wav(config.storage.inbox / "Recording.WAV")
        ingest(config)
        AudioSource.objects.update(stable_since=dj_timezone.now() - __import__("datetime").timedelta(seconds=120))
        ingest(config)
        # macOS case-insensitive FS: both spellings address the same file.
        assert AudioSource.objects.count() == 1


class TestReconciliation:
    def test_deleted_source_marks_recording_missing(self, tmp_path):
        config = make_config(tmp_path, file_stable_seconds=1)
        path = write_wav(config.storage.inbox / "rec.wav")
        ingest(config)
        AudioSource.objects.update(stable_since=dj_timezone.now() - __import__("datetime").timedelta(seconds=120))
        ingest(config)
        path.unlink()
        ingest(config)
        recording = Recording.objects.get()
        recording.refresh_from_db()
        assert recording.audio_status == AudioStatus.MISSING
        source = AudioSource.objects.get()
        assert source.presence == AudioStatus.MISSING
        assert source.missing_at is not None

    def test_transcribed_recording_stays_transcribed_when_audio_missing(self, tmp_path):
        config = make_config(tmp_path, file_stable_seconds=1)
        path = write_wav(config.storage.inbox / "rec.wav")
        ingest(config)
        AudioSource.objects.update(stable_since=dj_timezone.now() - __import__("datetime").timedelta(seconds=120))
        ingest(config)
        recording = Recording.objects.get()
        recording.processing_status = ProcessingStatus.TRANSCRIBED
        recording.save()
        path.unlink()
        ingest(config)
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED
        assert recording.audio_status == AudioStatus.MISSING

    def test_one_missing_duplicate_path_another_present(self, tmp_path):
        config = make_config(tmp_path, file_stable_seconds=1)
        config.storage.inbox.mkdir(parents=True, exist_ok=True)
        content = write_wav(tmp_path / "master.wav")
        copy1 = shutil.copyfile(content, config.storage.inbox / "one.wav")
        shutil.copyfile(content, config.storage.inbox / "two.wav")
        ingest(config)
        AudioSource.objects.update(stable_since=dj_timezone.now() - __import__("datetime").timedelta(seconds=120))
        ingest(config)
        recording = Recording.objects.get()
        copy1.unlink()
        ingest(config)
        recording.refresh_from_db()
        assert recording.audio_status == AudioStatus.PRESENT  # two.wav remains
        assert AudioSource.objects.filter(presence=AudioStatus.MISSING).count() == 1

    def test_reappearing_file_flips_back_to_present(self, tmp_path):
        config = make_config(tmp_path, file_stable_seconds=1)
        config.storage.inbox.mkdir(parents=True, exist_ok=True)
        content = write_wav(tmp_path / "master.wav")
        copy1 = shutil.copyfile(content, config.storage.inbox / "one.wav")
        ingest(config)
        AudioSource.objects.update(stable_since=dj_timezone.now() - __import__("datetime").timedelta(seconds=120))
        ingest(config)
        copy1.unlink()
        ingest(config)
        recording = Recording.objects.get()
        recording.refresh_from_db()
        assert recording.audio_status == AudioStatus.MISSING

        # The file reappears (new mtime) -> detached for re-hashing, then
        # re-attaches to the Recording with the same content hash.
        shutil.copyfile(content, config.storage.inbox / "one.wav")
        ingest(config)
        source = AudioSource.objects.get()
        assert source.recording_id is None  # back in the stability/hash workflow
        AudioSource.objects.update(stable_since=dj_timezone.now() - __import__("datetime").timedelta(seconds=120))
        ingest(config)
        recording.refresh_from_db()
        source.refresh_from_db()
        assert recording.audio_status == AudioStatus.PRESENT
        assert source.recording_id == recording.pk


class TestAudioSafety:
    def test_source_files_never_modified(self, tmp_path):
        config = make_config(tmp_path, file_stable_seconds=1)
        path = write_wav(config.storage.inbox / "rec.wav")
        before = (path.stat().st_size, path.stat().st_mtime, sha256_file(path))
        for _ in range(3):
            AudioSource.objects.update(stable_since=dj_timezone.now() - __import__("datetime").timedelta(seconds=120))
            ingest(config)
        after = (path.stat().st_size, path.stat().st_mtime, sha256_file(path))
        assert before == after
