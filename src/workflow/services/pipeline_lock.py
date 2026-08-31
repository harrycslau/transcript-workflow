"""Pipeline process locking for Brain.

SQLite has no advisory locks, so mutating pipeline commands serialize
through an exclusive ``flock`` on ``<storage.temp>/locks/pipeline.lock``.
The kernel releases the lock automatically when the process exits or
crashes, so there is no stale-lock cleanup and a live process's lock can
never be removed. Database uniqueness constraints (at most one unfinished
attempt per recording/stage) act as a second safety layer.

Read-only commands (status, review, transcripts, doctor, serve) never
take this lock.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    from brainlib.config import AppConfig

LOCK_FILE_NAME = "pipeline.lock"

# Exit code used by the CLI when another pipeline process holds the lock.
EXIT_BUSY = 3


class PipelineBusy(Exception):
    """Raised when another process already holds the pipeline lock."""

    def __init__(self, holder_pid: str = "") -> None:
        self.holder_pid = holder_pid
        detail = f" (pid {holder_pid})" if holder_pid else ""
        super().__init__(f"another pipeline process is active{detail}")


@contextmanager
def pipeline_lock(config: AppConfig) -> Iterator[None]:
    """Acquire the exclusive pipeline lock or raise :class:`PipelineBusy`."""
    lock_dir = Path(config.storage.temp) / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / LOCK_FILE_NAME
    handle = open(lock_path, "a+")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder_pid = ""
            try:
                handle.seek(0)
                holder_pid = handle.read().strip()
            except OSError:
                pass
            raise PipelineBusy(holder_pid) from None
        # Best-effort PID note for diagnostics only; never trusted for
        # correctness (the kernel owns lock lifetime).
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
        except OSError:
            pass
        try:
            yield
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        handle.close()
