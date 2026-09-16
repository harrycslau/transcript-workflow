"""Open the configured inbox directory in Finder (source safety).

The ONLY destination is the server-loaded configuration path: no request
input selects a folder, executable or URL, and no shell is involved.
``/usr/bin/open`` (macOS Launch Services) is launched with a fixed argv
array — never configurable, never a PATH lookup, never ``shell=True``
and never a ``file://`` browser URL — with a short timeout, suppressed
stdout/stderr (never read, decoded, stored or surfaced) and a checked
return code.

Every failure mode (missing/non-directory inbox, ``OSError``,
``TimeoutExpired``, nonzero exit) raises ONE fixed sanitized error.
Nothing about the path or the process output is logged or returned.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Fixed, non-configurable launcher (macOS ``open``/Finder).
OPEN_EXECUTABLE = "/usr/bin/open"

# Opening a folder is fast; a hang is a failure, not a wait.
OPEN_TIMEOUT_SECONDS = 5

# ONE fixed sanitized message for every failure mode.
OPEN_INBOX_ERROR_MESSAGE = (
    "The inbox folder could not be opened. Check that it exists and that "
    "Finder is available, then try again."
)


class OpenInboxError(Exception):
    """The configured inbox could not be opened (fixed sanitized message)."""


def open_inbox(inbox) -> None:
    """Open ``inbox`` in Finder, or raise :class:`OpenInboxError`.

    ``inbox`` is the configured storage path (a directory). The path is
    passed as one argv element to the fixed executable — the process
    never goes through a shell, so no value can be interpreted as a
    command, option bundle or URL.
    """
    inbox_path = Path(inbox)
    if not inbox_path.is_dir():
        raise OpenInboxError(OPEN_INBOX_ERROR_MESSAGE)
    try:
        result = subprocess.run(
            [OPEN_EXECUTABLE, str(inbox_path)],
            timeout=OPEN_TIMEOUT_SECONDS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise OpenInboxError(OPEN_INBOX_ERROR_MESSAGE) from None
    if result.returncode != 0:
        raise OpenInboxError(OPEN_INBOX_ERROR_MESSAGE)


__all__ = [
    "OPEN_EXECUTABLE",
    "OPEN_INBOX_ERROR_MESSAGE",
    "OPEN_TIMEOUT_SECONDS",
    "OpenInboxError",
    "open_inbox",
]
