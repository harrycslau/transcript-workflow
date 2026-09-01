"""Interruption-aware cleanup of orphaned pipeline temp directories.

``finally`` cleanup cannot run on SIGKILL/process death, so normalized
audio and routing samples can survive as orphans. This sweeper runs
inside stage-aware recovery (under the pipeline lock) and deletes ONLY
directories that:

- live directly beneath the strictly bounded ``data/temp/routing/`` or
  ``data/temp/transcription/`` namespaces,
- match the exact ``<recording_id>/attempt_<ordinal>`` structure with a
  parseable UUID recording id and a positive integer ordinal,
- are not symlinks (at any level, verified by resolved-path checks),
- correspond to NO unfinished ProcessingAttempt of the matching stage.

No path is ever taken blindly from the database: names must validate
against strict patterns and every removal is constrained to the fixed
namespace root. Existing files are never followed out of the namespace.
"""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from brainlib.config import AppConfig

# Namespace subdirectories of storage.temp that this sweeper owns.
STAGES = ("routing", "transcription")

_ATTEMPT_DIR_PATTERN = re.compile(r"^attempt_([0-9]+)$")


def _is_valid_recording_dir_name(name: str) -> bool:
    try:
        uuid.UUID(name)
    except (ValueError, AttributeError):
        return False
    return True


def sweep_orphan_attempt_dirs(config: AppConfig) -> dict:
    """Remove orphaned attempt temp dirs; returns removed-dir counts.

    Bounded and idempotent. Only validated, namespace-confined paths are
    ever deleted; anything unexpected is left untouched.
    """
    from workflow.models import AttemptStage, ProcessingAttempt

    removed: dict[str, int] = {}
    for stage in STAGES:
        removed[stage] = 0
        stage_stage = (
            AttemptStage.TRANSCRIPTION if stage == "transcription" else AttemptStage.ROUTING
        )
        namespace = Path(config.storage.temp) / stage
        if namespace.is_symlink() or not namespace.is_dir():
            continue
        namespace_root = namespace.resolve()
        try:
            recording_dirs = sorted(namespace.iterdir())
        except OSError:
            continue
        for recording_dir in recording_dirs:
            if recording_dir.is_symlink() or not recording_dir.is_dir():
                continue
            if not _is_valid_recording_dir_name(recording_dir.name):
                continue
            resolved_recording = recording_dir.resolve()
            if resolved_recording.parent != namespace_root:
                continue
            try:
                attempt_dirs = sorted(recording_dir.iterdir())
            except OSError:
                continue
            for attempt_dir in attempt_dirs:
                if attempt_dir.is_symlink() or not attempt_dir.is_dir():
                    continue
                match = _ATTEMPT_DIR_PATTERN.match(attempt_dir.name)
                if match is None:
                    continue
                resolved_attempt = attempt_dir.resolve()
                if resolved_attempt.parent != resolved_recording:
                    continue
                ordinal = int(match.group(1))
                unfinished = ProcessingAttempt.objects.filter(
                    recording_id=recording_dir.name,
                    stage=stage_stage,
                    ordinal=ordinal,
                    finished_at__isnull=True,
                ).exists()
                if unfinished:
                    continue
                shutil.rmtree(resolved_attempt, ignore_errors=True)
                removed[stage] += 1
            # Remove the recording dir when it is now empty (best effort).
            try:
                next(recording_dir.iterdir())
            except StopIteration:
                try:
                    recording_dir.rmdir()
                except OSError:
                    pass
            except OSError:
                continue
    return removed
