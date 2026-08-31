"""Runtime directory handling for Brain.

Directories are created lazily and safely. Only the paths declared in
the configuration are ever touched.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from brainlib.config import AppConfig


def runtime_directories(config: AppConfig) -> list[Path]:
    """Return the runtime directories that must exist for the app to run."""
    return [
        config.storage.inbox,
        config.storage.database.parent,
        config.storage.transcripts,
        config.storage.exports,
        config.storage.logs,
        config.storage.temp,
    ]


def ensure_runtime_dirs(config: AppConfig) -> list[Path]:
    """Create missing runtime directories (``mkdir -p`` semantics).

    Idempotent. Returns only the directories created by this call;
    callers wanting to validate the full configured set should use
    :func:`runtime_directories` afterwards.
    """
    created: list[Path] = []
    for directory in runtime_directories(config):
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            created.append(directory)
    return created


def is_writable_dir(path: Path) -> bool:
    """Check that ``path`` is an existing, writable directory.

    Uses :func:`tempfile.mkstemp` to create a unique probe file inside
    the directory, so no pre-existing file can ever be overwritten,
    modified, or deleted, and concurrent checks cannot collide. The
    probe is removed on success and failure. Permission bits alone are
    not trusted; the actual create/delete round-trip is the check.
    """
    if not path.is_dir():
        return False
    probe_name: str | None = None
    try:
        fd, probe_name = tempfile.mkstemp(dir=path, prefix=".brain-write-probe-")
        os.close(fd)
        return True
    except OSError:
        return False
    finally:
        if probe_name is not None:
            try:
                os.unlink(probe_name)
            except OSError:
                pass
