"""Canonical language policy (single source of truth).

Every runtime path that touches a language code — manual correction,
detector validation, summary-payload source-language persistence,
default/original output resolution, attempt provenance, and
display/export — must use the functions in this module. There is no
second normalization anywhere in the codebase; the migration keeps a
historical-model-compatible local copy whose semantics mirror this
module (documented in the migration).

Supported subset and casing policy (standard BCP-47 canonical casing):

- structure: ``language[-script][-region]`` (e.g. ``en``, ``fi``,
  ``zh-Hant``, ``en-US``, ``yue-HK``, ``cmn-CN``);
- primary language subtag: 2–3 ASCII letters, lowercased;
- script subtag: 4 ASCII letters, Titlecase (e.g. ``Hant``);
- region subtag: 2 ASCII letters or 3 digits, UPPERCASED;
- anything else (empty, overlong, punctuation, unknown shapes) is
  malformed and is NEVER persisted — callers receive ``""`` and must
  treat that as "unknown / not persisted".

Chinese-family identities (``zh``, ``yue``, ``cmn``, with any script/
region subtags) are valid SOURCE identities and keep their canonical
source casing on the Transcript (e.g. ``zh-HK``, ``yue``), but every
Chinese-family DEFAULT and ORIGINAL output resolves to ``zh-Hant``.
Non-Chinese sources default to English output; an explicit Original
resolves to the canonical source language itself.
"""

from __future__ import annotations

import re

ZH_HANT = "zh-Hant"
DEFAULT_OUTPUT_LANGUAGE = "en"

# Selectors a user may use to REQUEST GENERATION. Read/display/export
# additionally accept any concrete output_language that already exists
# on the active transcript (see workflow.services.variant_view).
GENERATION_SELECTORS = ("default", "original", "en", "zh-Hant")

_CHINESE_FAMILY_PRIMARY = ("zh", "yue", "cmn")

_LANG_TAG_RE = re.compile(
    r"^(?P<language>[A-Za-z]{2,3})"
    r"(?:-(?P<script>[A-Za-z]{4}))?"
    r"(?:-(?P<region>[A-Za-z]{2}|[0-9]{3}))?$"
)


def canonicalize_language(code: str) -> str:
    """Canonicalize a raw language tag to the supported subset.

    Applies standard BCP-47 casing (primary lowercase, script
    Titlecase, region uppercase) and validates the structure.
    Returns ``""`` for empty or malformed input — malformed values are
    never persisted by callers.
    """
    raw = (code or "").strip()
    if not raw:
        return ""
    match = _LANG_TAG_RE.match(raw)
    if match is None:
        return ""
    parts = [match.group("language").lower()]
    if match.group("script"):
        script = match.group("script")
        parts.append(script[0].upper() + script[1:].lower())
    if match.group("region"):
        parts.append(match.group("region").upper())
    return "-".join(parts)


def is_chinese_family(code: str) -> bool:
    """True when the (raw or canonical) tag's primary subtag identifies
    a Chinese-family language: zh, zh-*, yue, yue-*, cmn, cmn-*."""
    canonical = canonicalize_language(code)
    if not canonical:
        return False
    primary = canonical.split("-", 1)[0]
    return primary in _CHINESE_FAMILY_PRIMARY


def output_language_for_source(code: str) -> str:
    """Map a canonical source language to its canonical OUTPUT language.

    Chinese family → ``zh-Hant`` (always, for both default and
    original). Any other valid code → itself. Invalid/empty → ``""``.
    """
    canonical = canonicalize_language(code)
    if not canonical:
        return ""
    if is_chinese_family(canonical):
        return ZH_HANT
    return canonical


# Backwards-compatible aliases for the pre-refactor helper names that
# other modules and tests import from summarize.py.
normalize_source_language = canonicalize_language


def canonical_source_for_output(code: str) -> str:
    return output_language_for_source(code)


def is_valid_output_identity(value: str) -> bool:
    """True when ``value`` is a canonical OUTPUT-language identity.

    Output identities are stricter than source identities:

    - the value must already be canonical (``canonicalize_language``
      returns it unchanged) — ``"FI"`` or ``"en-us"`` are rejected;
    - Chinese-family OUTPUT is exactly ``zh-Hant`` — source-style codes
      such as ``yue``, ``yue-HK``, ``cmn``, ``zh-HK`` or ``zh-CN`` are
      rejected (they are valid SOURCE identities, never outputs);
    - ``und`` (the migration backfill's "ambiguous" marker) is NOT a
      runtime output identity: runtime reconciliation rejects it;
      migration 0007 retains ``und`` rows only as historical backfill
      markers for prose it could not classify.
    """
    canonical = canonicalize_language(value)
    if not canonical or canonical != (value or "").strip():
        return False
    if canonical == "und":
        return False
    if is_chinese_family(canonical) and canonical != ZH_HANT:
        return False
    return True
