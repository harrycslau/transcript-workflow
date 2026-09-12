"""Safe Library-return token (Step 6.2a).

A small server-signed token that encodes ONLY canonical validated
normal-Library state:

- the canonical filter/sort ``(name, value)`` pairs of a
  :class:`workflow.query.ListFilters` (``ListFilters.as_pairs()`` —
  never a search query, never a ``page`` pair, never ``relevance``);
- a positive ``page`` number;
- the effective cards/table ``view``.

The destination is ALWAYS ``reverse('recordings')``: the token is placed
on the ``lib_return`` query parameter of the normal-Library recording
and section links, propagated verbatim through section-detail tabs,
section summary confirmation/execution redirects and section tag
redirects, and decoded by the Library view and the recording-detail
breadcrumb so the originating page/state is preserved. An
invalid/forged/oversized token decodes to ``None`` and the Library
falls back to its plain state.

Contract:

- ``make_token`` serializes the EXACT current effective state (the
  caller passes the filters/view/page actually rendered — for an
  invalid filter set that is the plain ``ListFilters()`` default);
- ``decode_token`` re-validates every decoded pair through the SHARED
  canonical parser (:func:`workflow.query.list_filters` with
  ``allow_relevance=False``) and requires an exact round-trip
  (``ListFilters.as_pairs()`` == the submitted pairs), so only
  canonical validated normal-Library state is ever accepted and a
  forged/unknown/duplicate/redundant pair is rejected as a whole;
- the token is signed with Django's HMAC signer (salt-scoped) and
  URL-safe base64-encoded; no expiry (the payload is benign filter
  state, never secrets);
- GETs stay strictly read-only: decoding is SELECT-only (``list_filters``
  touches no database state beyond parsing; the timezone comes from the
  validated config).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from django.core import signing
from django.http import QueryDict

from workflow.query import ListFilters, list_filters

# Only the two normal-Library views may be encoded (mirrors
# ``workflow.views.recordings.VALID_VIEWS`` without importing the view
# layer; the constant is kept in lockstep by tests).
VALID_RETURN_VIEWS = ("cards", "table")

_SALT = "brain.library-return"
_TOKEN_VERSION = 1
# Bounded defensive caps: a token is generated only by the Library page,
# whose canonical pair count is small (<= MAX_TAG_FILTERS tags + a dozen
# scalar filters); anything larger is rejected before any parsing.
_MAX_PAIRS = 64
_MAX_TOKEN_CHARS = 8192
# Positive page cap: absurd values are rejected outright (the Paginator
# would clamp them anyway, but a token is server-generated state and
# should not invite billion-page URLs).
_MAX_PAGE = 100_000_000


class LibraryReturnError(Exception):
    """A token could not be produced (programmer error, never user input)."""


@dataclass(frozen=True)
class LibraryReturn:
    """Validated normal-Library state carried by one token."""

    filters: ListFilters
    page: int
    view: str


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _dedupe_pairs(pairs):
    """Stable de-duplication of (name, value) pairs preserving first
    occurrence order.

    ``ListFilters.as_pairs()`` legitimately emits a repeated ``tag`` pair
    when the raw query string carried a duplicate tag value (``list_filters``
    preserves duplicates); a token must never carry that redundancy, so the
    SERVER-generated pair list is canonicalized here.
    """
    seen = set()
    out = []
    for pair in pairs:
        key = tuple(pair)
        if key not in seen:
            seen.add(key)
            out.append(pair)
    return out


def make_token(filters: ListFilters, page: int, view: str) -> str:
    """Sign one canonical normal-Library state into an opaque token.

    The caller MUST pass the state actually rendered (an invalid filter
    set is rendered as plain ``ListFilters()``). Raises
    :class:`LibraryReturnError` for non-canonical inputs (a programmer
    error, never a user-facing failure). The encoded pairs are STABLY
    DE-DUPLICATED so a redundant duplicate tag value never survives in
    the token.
    """
    if not filters.valid or filters.sort_error:
        raise LibraryReturnError("cannot tokenize an invalid filter set")
    if type(page) is not int or page < 1 or page > _MAX_PAGE:
        raise LibraryReturnError("page must be a bounded positive int")
    if view not in VALID_RETURN_VIEWS:
        raise LibraryReturnError("view must be cards or table")
    pairs = _dedupe_pairs(filters.as_pairs())
    if len(pairs) > _MAX_PAIRS:
        raise LibraryReturnError("filter pairs exceed the token cap")
    payload = json.dumps(
        {
            "v": _TOKEN_VERSION,
            "pairs": [[name, value] for name, value in pairs],
            "page": page,
            "view": view,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    signer = signing.Signer(salt=_SALT)
    return signer.sign(_b64encode(payload.encode("utf-8")))


def decode_token(token: Any, timezone_name: str) -> LibraryReturn | None:
    """Validate and decode one signed token, or ``None`` when it is
    missing/malformed/forged/non-canonical/oversized.

    Every decoded pair is re-validated through the shared canonical
    parser (``list_filters`` with ``allow_relevance=False``) and the
    re-serialized canonical pairs must round-trip EXACTLY — a token that
    smuggles a search query, a ``relevance`` sort, an unknown, a
    redundant or a DUPLICATE pair (the server never emits one after
    de-duplication) decodes to ``None``.
    """
    if type(token) is not str or not token or len(token) > _MAX_TOKEN_CHARS:
        return None
    try:
        signed = signing.Signer(salt=_SALT).unsign(token)
        raw = _b64decode(signed)
        payload = json.loads(raw.decode("utf-8"))
    except (signing.BadSignature, signing.SignatureExpired, ValueError, TypeError):
        return None
    except Exception:
        # base64/JSON decoding of a hostile payload: never surface.
        return None
    if type(payload) is not dict or payload.get("v") != _TOKEN_VERSION:
        return None
    pairs = payload.get("pairs")
    page = payload.get("page")
    view = payload.get("view")
    if type(pairs) is not list or len(pairs) > _MAX_PAIRS:
        return None
    for item in pairs:
        if type(item) is not list or len(item) != 2:
            return None
        name, value = item
        if type(name) is not str or type(value) is not str:
            return None
    # Duplicate pairs are never canonical server state (make_token
    # de-duplicates) — reject the WHOLE token.
    if len({tuple(item) for item in pairs}) != len(pairs):
        return None
    qd = QueryDict(mutable=True)
    for name, value in pairs:
        qd.appendlist(name, value)
    try:
        filters = list_filters(qd, timezone_name, allow_relevance=False)
    except Exception:
        return None
    if not filters.valid or filters.sort_error:
        return None
    if filters.as_pairs() != [tuple(item) for item in pairs]:
        return None
    if type(page) is not int or page < 1 or page > _MAX_PAGE:
        return None
    if view not in VALID_RETURN_VIEWS:
        return None
    return LibraryReturn(filters=filters, page=page, view=view)


def return_url(token: str) -> str:
    """The canonical destination URL carrying ``token`` on ``lib_return``.

    The destination is ALWAYS ``reverse('recordings')`` — arbitrary
    client URLs are never accepted.
    """
    from django.urls import reverse

    return f"{reverse('recordings')}?lib_return={token}"