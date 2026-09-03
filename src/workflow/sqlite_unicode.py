"""Unicode-aware case-insensitive SQLite collation (infrastructure).

SQLite's built-in ``LOWER()`` is ASCII-only, so Title A–Z/Z–A ordering
needs a deterministic Unicode collation (NFC + casefold). Registration is
per-connection, never a process-global claim:

- ``connection_created`` registers the collation on every SQLite
  connection Django opens (application, test, CLI and server), including
  connections created or recreated later;
- :func:`ensure_registered` opens the CURRENT connection via
  ``ensure_connection`` and idempotently registers/verifies the collation
  on that concrete raw connection, so a Title sort works even as the
  very first database operation in a fresh process.

There is deliberately no ``_registered`` global: closing and reopening a
connection simply registers again on the new raw connection. Opening a
connection here only registers an in-memory collation — no rows or
schema are ever written (read-only GET semantics preserved).
"""

from __future__ import annotations

import unicodedata

from django.db import connections
from django.db.backends.signals import connection_created
from django.db.models import F, Func

COLLATION_NAME = "unicode_fold"

_UNAVAILABLE_MESSAGE = (
    f"The '{COLLATION_NAME}' SQLite collation could not be registered; "
    "Unicode-aware Title sorting is unavailable."
)


def _collate(left: str, right: str) -> int:
    folded_left = unicodedata.normalize("NFC", left or "").casefold()
    folded_right = unicodedata.normalize("NFC", right or "").casefold()
    return (folded_left > folded_right) - (folded_left < folded_right)


def _register(connection, **_kwargs) -> None:
    """Register the collation on one concrete SQLite connection (called
    by ``connection_created`` for every newly opened connection).

    Failures are converted to the fixed stable message (``from None``) so
    raw exception text, tracebacks, paths and sentinels never escape —
    even when the failure happens during ``ensure_connection``.
    """
    if connection.vendor != "sqlite":
        return
    try:
        connection.connection.create_collation(COLLATION_NAME, _collate)
    except Exception:
        raise RuntimeError(_UNAVAILABLE_MESSAGE) from None


def connect() -> None:
    """Register the ``connection_created`` handler. Called from
    ``WorkflowConfig.ready()``, before any connection is opened."""
    connection_created.connect(_register, dispatch_uid="brain_sqlite_unicode_collation")


def ensure_registered(using: str = "default") -> None:
    """Ensure the collation is registered on the given connection.

    Opens the connection if it is not already open (registering an
    in-memory collation is read-only), then idempotently registers the
    collation. Raises a stable, concise error when registration is
    impossible — never silently falls back to ASCII ordering and never
    leaks raw exception details.

    Both failure points are covered: ``ensure_connection()`` fires
    ``connection_created`` (whose ``_register`` receiver may raise) and
    the explicit registration below.
    """
    connection = connections[using]
    if connection.vendor != "sqlite":
        raise RuntimeError(
            f"Unicode-aware Title sorting requires a SQLite database; "
            f"connection '{using}' is a '{connection.vendor}' backend."
        )
    try:
        connection.ensure_connection()
        connection.connection.create_collation(COLLATION_NAME, _collate)
    except RuntimeError as exc:
        if exc.args and exc.args[0] == _UNAVAILABLE_MESSAGE:
            raise
        raise RuntimeError(_UNAVAILABLE_MESSAGE) from None
    except Exception:
        raise RuntimeError(_UNAVAILABLE_MESSAGE) from None


def folded_title_expression(field: str = "display_title"):
    """Database-side ordering key rendering ``"<field>" COLLATE unicode_fold``."""
    return Func(
        F(field),
        function=COLLATION_NAME,
        template="%(expressions)s COLLATE %(function)s",
    )