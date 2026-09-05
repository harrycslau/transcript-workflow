"""Unicode-aware case-insensitive SQLite infrastructure.

SQLite's built-in ``LOWER()`` is ASCII-only, so Title A–Z/Z–A ordering
needs a deterministic Unicode collation (NFC + casefold), and keyword
search (Step 5A.4.1) needs Unicode folding available inside SQL.
:func:`fold_text` is the SINGLE deterministic fold contract (NFC then
per-codepoint casefold) shared by the ``brain_fold`` SQL function and
the Python ranking/snippet code — the trigram tokenizer's internal
folding is only an approximation of it (they disagree on rare
case-expansion pairs). Registration is per-connection, never a
process-global claim:

- ``connection_created`` registers the collation on every SQLite
  connection Django opens (application, test, CLI and server), including
  connections created or recreated later, and BEST-EFFORT registers the
  ``brain_fold`` SQL function;
- a ``brain_fold`` registration failure NEVER breaks unrelated pages,
  ordinary ORM access or the Title collation: it is swallowed at
  registration and surfaced only at search use-time as the separate
  fixed ``_FOLD_UNAVAILABLE_MESSAGE`` raised by
  :func:`ensure_fold_function` (which idempotently retries the
  registration);
- :func:`ensure_registered` opens the CURRENT connection via
  ``ensure_connection`` and idempotently registers/verifies the collation
  on that concrete raw connection, so a Title sort works even as the
  very first database operation in a fresh process.

There is deliberately no ``_registered`` global: closing and reopening a
connection simply registers again on the new raw connection. Opening a
connection here only registers in-memory objects — no rows or schema are
ever written (read-only GET semantics preserved).
"""

from __future__ import annotations

import unicodedata

from django.db import connections
from django.db.backends.signals import connection_created
from django.db.models import F, Func

COLLATION_NAME = "unicode_fold"
FOLD_FUNCTION = "brain_fold"

_UNAVAILABLE_MESSAGE = (
    f"The '{COLLATION_NAME}' SQLite collation could not be registered; "
    "Unicode-aware Title sorting is unavailable."
)
_FOLD_UNAVAILABLE_MESSAGE = (
    f"The '{FOLD_FUNCTION}' SQLite function could not be registered; "
    "Unicode-aware keyword-search matching is unavailable on this connection."
)


def fold_text(value: str | None) -> str:
    """The canonical search fold: NFC, then per-codepoint ``casefold()``
    (position-preserving for the practical scripts this corpus uses;
    only rare case-expansion pairs change length). The SQL function
    ``brain_fold`` and all Python-side ranking/snippet code use EXACTLY
    this function, so a LIKE selection and its highlight offsets can
    never disagree on Unicode semantics."""
    if not value:
        return ""
    normalized = unicodedata.normalize("NFC", value)
    return "".join(character.casefold() for character in normalized)


def _collate(left: str, right: str) -> int:
    folded_left = unicodedata.normalize("NFC", left or "").casefold()
    folded_right = unicodedata.normalize("NFC", right or "").casefold()
    return (folded_left > folded_right) - (folded_left < folded_right)


def _register_fold(connection) -> None:
    """Idempotently register the deterministic ``brain_fold`` SQL
    function on one concrete raw connection, raising only the fixed
    stable message (``from None``) on failure."""
    if connection.vendor != "sqlite":
        return
    try:
        connection.connection.create_function(
            FOLD_FUNCTION, 1, fold_text, deterministic=True
        )
    except Exception:
        raise RuntimeError(_FOLD_UNAVAILABLE_MESSAGE) from None


def _register(connection, **_kwargs) -> None:
    """Register the collation (and best-effort the fold function) on one
    concrete SQLite connection (called by ``connection_created`` for
    every newly opened connection).

    Collation failures are converted to the fixed stable message
    (``from None``) so raw exception text, tracebacks, paths and
    sentinels never escape — even when the failure happens during
    ``ensure_connection``.

    A fold-function failure is SWALLOWED deliberately: the function is
    search-only, and letting it raise inside ``connection_created``
    would break every unrelated page, ORM access and the Title
    collation. The failure resurfaces only as a search error via
    :func:`ensure_fold_function`, which retries the registration.
    """
    if connection.vendor != "sqlite":
        return
    try:
        connection.connection.create_collation(COLLATION_NAME, _collate)
    except Exception:
        raise RuntimeError(_UNAVAILABLE_MESSAGE) from None
    try:
        _register_fold(connection)
    except RuntimeError:
        pass


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


def ensure_fold_function(using: str = "default") -> None:
    """Ensure the ``brain_fold`` SQL function is usable on the given
    connection (idempotent retry of the best-effort signal registration).

    Only the keyword-search LIKE fallback needs it, so search calls this
    right before folded LIKE SQL; a persistent failure raises the
    separate fixed stable message — never the collation's, never raw
    exception details, and never after having damaged anything else.
    """
    connection = connections[using]
    if connection.vendor != "sqlite":
        raise RuntimeError(
            f"The '{FOLD_FUNCTION}' function requires a SQLite database; "
            f"connection '{using}' is a '{connection.vendor}' backend."
        )
    try:
        connection.ensure_connection()
    except RuntimeError as exc:
        # A collation registration failure fired through the signal keeps
        # its OWN stable message; it is never mislabeled as a fold error.
        if exc.args and exc.args[0] == _UNAVAILABLE_MESSAGE:
            raise
        raise RuntimeError(_FOLD_UNAVAILABLE_MESSAGE) from None
    except Exception:
        raise RuntimeError(_FOLD_UNAVAILABLE_MESSAGE) from None
    _register_fold(connection)


def folded_title_expression(field: str = "display_title"):
    """Database-side ordering key rendering ``"<field>" COLLATE unicode_fold``."""
    return Func(
        F(field),
        function=COLLATION_NAME,
        template="%(expressions)s COLLATE %(function)s",
    )