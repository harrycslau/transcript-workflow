"""Inbox discovery, stability tracking, hashing, and deduplication.

Safety rules:
- Scanning is restricted to the configured inbox. Symlinks are resolved
  for identity, but a resolved path outside the inbox is ignored (never
  followed for processing).
- Source files are only ever read: never moved, renamed, truncated, or
  deleted.
- Hashing is verified with size/mtime checks before and after; a change
  discards the hash result and the source returns to stability waiting.
- Pre-hash state lives on ``AudioSource.discovery_state`` so interrupted
  runs recover safely even while ``recording`` is still null.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from brainlib.config import AppConfig
from workflow.models import (
    AudioStatus,
    AudioSource,
    DiscoveryState,
    ProcessingStatus,
    Recording,
)

logger = logging.getLogger(__name__)

SUPPORTED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a"}
_HASH_CHUNK = 1024 * 1024


@dataclass
class IngestReport:
    new_sources: list[str] = field(default_factory=list)
    hashed: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)  # new paths for existing recordings
    skipped_unstable: list[str] = field(default_factory=list)
    reconciled_missing: list[str] = field(default_factory=list)
    reconciled_present: list[str] = field(default_factory=list)
    ignored_paths: list[str] = field(default_factory=list)  # outside inbox

    def as_dict(self) -> dict:
        return {
            "new_sources": self.new_sources,
            "hashed": self.hashed,
            "duplicates": self.duplicates,
            "skipped_unstable": self.skipped_unstable,
            "reconciled_missing": self.reconciled_missing,
            "reconciled_present": self.reconciled_present,
            "ignored_paths": self.ignored_paths,
        }


def normalize_identity(path: Path, inbox: Path) -> Path | None:
    """Return the absolute, symlink-resolved identity path for ``path``.

    Returns None when the path does not exist or resolves outside the
    configured inbox (symlinks escaping the inbox are never followed).
    The identity string used for uniqueness is the casefolded path so a
    macOS case-insensitive filesystem yields one AudioSource per file.
    """
    try:
        real = Path(os.path.realpath(path, strict=True))
    except OSError:
        return None
    try:
        inbox_real = Path(os.path.realpath(inbox, strict=True))
    except OSError:
        return None
    if real != inbox_real and inbox_real not in real.parents:
        return None
    return real


def is_inside_inbox(path: Path, inbox: Path) -> bool:
    """Symlink-safe inbox boundary check (shared by ingestion and the
    pre-stage source validators).

    Uses non-strict realpath resolution so deleted files still resolve
    inside the inbox (and reconcile as missing rather than being
    misclassified as outside). A symlink inside the inbox that resolves
    outside it is rejected.
    """
    try:
        real = Path(os.path.realpath(path))
    except OSError:
        return False
    try:
        inbox_real = Path(os.path.realpath(inbox, strict=True))
    except OSError:
        return False
    return real == inbox_real or inbox_real in real.parents


def identity_key(identity_path: Path) -> str:
    return str(identity_path).casefold()


def discover_audio_files(inbox: Path) -> list[Path]:
    """Find supported audio files under ``inbox`` (symlink-safe).

    Extensions are matched case-insensitively. Unsupported files are
    left untouched and ignored; the supported set is deliberately
    explicit so arbitrary inbox contents are never sent to audio tools.
    """
    found: list[Path] = []
    if not inbox.exists():
        return found
    inbox_real = Path(os.path.realpath(inbox, strict=True))
    for root, dirs, files in os.walk(inbox, followlinks=False):
        root_path = Path(root)
        # Skip symlinked directories that resolve outside the inbox.
        kept_dirs = []
        for d in dirs:
            candidate = root_path / d
            if candidate.is_symlink():
                try:
                    resolved = Path(os.path.realpath(candidate, strict=True))
                except OSError:
                    continue
                if resolved != inbox_real and inbox_real not in resolved.parents:
                    continue
            kept_dirs.append(d)
        dirs[:] = kept_dirs
        for name in files:
            if Path(name).suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS:
                continue
            full = root_path / name
            identity = normalize_identity(full, inbox)
            if identity is None:
                logger.warning("Ignoring path outside inbox: %s", full)
                continue
            found.append(identity)
    return sorted(set(found), key=identity_key)


def discover_wavs(inbox: Path) -> list[Path]:
    """Backward-compatible name for :func:`discover_audio_files`.

    Kept for callers from Steps 1–4; despite the historical name, it now
    returns every supported audio format.
    """
    return discover_audio_files(inbox)


def _stat_or_none(path: Path):
    try:
        return os.stat(path)
    except OSError:
        return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _record_audio_status(recording: Recording) -> None:
    recording.audio_status = (
        AudioStatus.PRESENT
        if recording.sources.filter(presence=AudioStatus.PRESENT).exists()
        else AudioStatus.MISSING
    )


def _ensure_canonical(recording: Recording) -> None:
    """Pick a deterministic canonical source among present sources."""
    present = list(recording.sources.filter(presence=AudioStatus.PRESENT).order_by("first_seen_at", "pk"))
    changed = False
    for source in present:
        should_be_canonical = source is min(present, key=lambda s: (len(s.path), s.first_seen_at))
        if source.is_canonical != should_be_canonical:
            source.is_canonical = should_be_canonical
            source.save(update_fields=["is_canonical"])
            changed = True
    return changed


def _detach_for_rehash(source: AudioSource, note: str) -> None:
    """Detach a source from its recording and reset it to stability wait.

    The old Recording is never deleted or mutated beyond recalculated
    audio status / canonical source; its transcripts, attempts, routing
    decisions, and sections are preserved untouched.
    """
    old_recording = source.recording
    source.recording = None
    source.is_canonical = False
    source.discovery_state = DiscoveryState.OBSERVING
    source.stable_since = timezone.now()
    source.discovery_note = note
    source.save()
    if old_recording is not None:
        with transaction.atomic():
            recording = Recording.objects.select_for_update().get(pk=old_recording.pk)
            _record_audio_status(recording)
            _ensure_canonical(recording)
            recording.save()


def source_content_matches(recording: Recording, source: AudioSource) -> bool:
    """Re-validate a source's content identity against its Recording.

    Re-hashes the canonical file and compares size, mtime, and SHA-256.
    Used immediately before routing/transcription so changed content is
    never processed under the old Recording identity.
    """
    path = Path(source.path)
    st = _stat_or_none(path)
    if st is None:
        return False
    if st.st_size != source.file_size or st.st_mtime != source.file_mtime:
        return False
    if not recording.sha256:
        return True
    try:
        return sha256_file(path) == recording.sha256
    except OSError:
        return False


def reconcile_changed_source(source: AudioSource, st: os.stat_result) -> bool:
    """Handle an attached source whose size/mtime changed on disk.

    Detaches it for re-hashing, recalculates the old Recording's
    canonical source and audio status, and returns True. The new
    content re-enters the stability/hash workflow and will attach to
    the Recording matching its new SHA-256.
    """
    _detach_for_rehash(source, "content_changed")
    source.file_size = st.st_size
    source.file_mtime = st.st_mtime
    source.save(update_fields=["file_size", "file_mtime"])
    return True


def _attach_hashed_source(source: AudioSource, sha256: str, config: AppConfig, report: IngestReport) -> None:
    now = timezone.now()
    with transaction.atomic():
        recording = Recording.objects.filter(sha256=sha256).select_for_update().first()
        duplicate = recording is not None
        if recording is None:
            recording = Recording.objects.create(sha256=sha256, processing_status=ProcessingStatus.DISCOVERED)
        # Attach and save the source FIRST, then recalculate canonical
        # source and audio status so the new present source is counted.
        source.recording = recording
        source.discovery_state = DiscoveryState.HASHED
        source.discovery_note = ""
        source.last_seen_at = now
        source.save()
        _ensure_canonical(recording)
        _record_audio_status(recording)
        recording.save()
    if duplicate:
        report.duplicates.append(source.path)
    else:
        report.hashed.append(source.path)


def _hash_source(source: AudioSource, config: AppConfig, report: IngestReport) -> None:
    path = Path(source.path)
    before = _stat_or_none(path)
    if before is None:
        source.discovery_state = DiscoveryState.OBSERVING
        source.stable_since = None
        source.save()
        return
    try:
        sha256 = sha256_file(path)
    except OSError as exc:
        source.discovery_state = DiscoveryState.FAILED
        source.discovery_note = f"hash failed: {type(exc).__name__}"
        source.save()
        return
    after = _stat_or_none(path)
    if (
        after is None
        or after.st_size != before.st_size
        or after.st_mtime != before.st_mtime
        or after.st_size != (source.file_size or -1)
        or after.st_mtime != (source.file_mtime or -1)
    ):
        # File changed during hashing: discard the result and restart the
        # stability wait. Never trust a torn read.
        source.discovery_state = DiscoveryState.OBSERVING
        source.stable_since = timezone.now()
        if after is not None:
            source.file_size = after.st_size
            source.file_mtime = after.st_mtime
        source.save()
        report.skipped_unstable.append(source.path)
        return
    _attach_hashed_source(source, sha256, config, report)


def ingest(config: AppConfig, now=None) -> IngestReport:
    """Scan the inbox once: discover, observe stability, hash, dedupe."""
    report = IngestReport()
    now = now or timezone.now()
    inbox = Path(config.storage.inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    stable_seconds = config.macwhisper.file_stable_seconds

    seen_identities: set[str] = set()

    # Reconciliation of previously known sources first. Sources whose
    # stored path is outside the currently configured inbox are skipped
    # safely (no stat, no processing, no file access).
    all_sources = list(AudioSource.objects.select_related("recording").all())
    for source in all_sources:
        source_path = Path(source.path)
        # Non-strict resolution: deleted files must reconcile as missing,
        # not be misclassified as outside the inbox.
        if not is_inside_inbox(source_path, inbox):
            if source.discovery_note != "outside_current_inbox":
                source.discovery_note = "outside_current_inbox"
                source.save(update_fields=["discovery_note"])
                report.ignored_paths.append(source.path)
            continue
        st = _stat_or_none(source_path)
        if st is None:
            if source.presence == AudioStatus.PRESENT:
                source.presence = AudioStatus.MISSING
                source.missing_at = now
                source.save()
                if source.recording_id:
                    with transaction.atomic():
                        recording = Recording.objects.select_for_update().get(pk=source.recording_id)
                        _record_audio_status(recording)
                        recording.save()
                report.reconciled_missing.append(source.path)
            continue
        if source.presence == AudioStatus.MISSING:
            source.presence = AudioStatus.PRESENT
            source.missing_at = None
            source.last_seen_at = now
            source.save()
            if source.recording_id:
                with transaction.atomic():
                    recording = Recording.objects.select_for_update().get(pk=source.recording_id)
                    _record_audio_status(recording)
                    _ensure_canonical(recording)
                    recording.save()
            report.reconciled_present.append(source.path)

    for identity in discover_audio_files(inbox):
        key = identity_key(identity)
        if key in seen_identities:
            continue
        seen_identities.add(key)
        st = _stat_or_none(identity)
        if st is None:
            continue
        source = AudioSource.objects.filter(path_identity=key).first()
        if source is None:
            source = AudioSource.objects.create(
                path=str(identity),
                path_identity=key,
                original_filename=identity.name,
                file_size=st.st_size,
                file_mtime=st.st_mtime,
                first_seen_at=now,
                last_seen_at=now,
                discovery_state=DiscoveryState.OBSERVING,
                stable_since=now,
            )
            report.new_sources.append(source.path)
        else:
            source.last_seen_at = now
            if source.presence != AudioStatus.PRESENT:
                source.presence = AudioStatus.PRESENT
                source.missing_at = None

        if source.discovery_state == DiscoveryState.FAILED:
            # Retry failed hashing from the stability wait.
            source.discovery_state = DiscoveryState.OBSERVING
            source.stable_since = now
            source.discovery_note = ""

        if source.recording_id is not None:
            # Attached source: detect content replacement at this path.
            if source.file_size != st.st_size or source.file_mtime != st.st_mtime:
                reconcile_changed_source(source, st)
                report.skipped_unstable.append(source.path)
            else:
                source.last_seen_at = now
                source.save()
            continue

        if source.discovery_state == DiscoveryState.HASHING:
            _hash_source(source, config, report)
            continue

        stable_for = (now - source.stable_since).total_seconds() if source.stable_since else 0.0
        if stable_for >= stable_seconds:
            source.discovery_state = DiscoveryState.HASHING
            source.save()
            _hash_source(source, config, report)
        else:
            source.save()
            report.skipped_unstable.append(source.path)

    return report


def canonical_source(recording: Recording) -> AudioSource | None:
    return recording.sources.filter(presence=AudioStatus.PRESENT, is_canonical=True).first()
