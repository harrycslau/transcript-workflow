"""Output-language resolution (default / original / explicit).

Kept separate from ``summarize`` so the variant-state reconciler and
the summarization pipeline can both depend on it without a circular
import. ``summarize`` re-exports these names for backwards
compatibility.
"""

from __future__ import annotations

from brainlib.config import ConfigError
from workflow.models import Transcript
from workflow.services.languages import (
    DEFAULT_OUTPUT_LANGUAGE,
    ZH_HANT,
    canonicalize_language,
    is_chinese_family,
    output_language_for_source,
)


def resolve_default_language(transcript: Transcript) -> str:
    """Deterministic default output language from source-family policy.

    Priority: user-corrected source > confirmed routing > unverified
    automatic Chinese routing > transcript source language > fallback en.
    Chinese-family sources resolve to ``zh-Hant``; everything else to
    ``en``.
    """
    recording = transcript.recording

    # 1. User-corrected source language
    if transcript.language_observed and transcript.language_observed_verified_by == "user":
        if is_chinese_family(transcript.language_observed):
            return ZH_HANT
        return DEFAULT_OUTPUT_LANGUAGE

    # 2. Confirmed routing decision
    decision = recording.routing_decisions.filter(is_active=True).first()
    if (
        decision
        and decision.routing_verified
        and decision.route_suggestion in ("cantonese", "mandarin")
    ):
        return ZH_HANT

    # 3. Unverified automatic Chinese routing (reliable for family detection)
    if (
        decision
        and decision.method == "automatic"
        and decision.route_suggestion in ("cantonese", "mandarin")
    ):
        return ZH_HANT

    # 4. Transcript source language (from LLM detection or script evidence)
    if transcript.language_observed:
        if is_chinese_family(transcript.language_observed):
            return ZH_HANT
        return DEFAULT_OUTPUT_LANGUAGE

    # 5. Fallback: European/uncertain/unknown
    return DEFAULT_OUTPUT_LANGUAGE


def resolve_output_language(
    transcript: Transcript, requested_language: str
) -> str:
    """Resolve a GENERATION selector to a concrete output_language.

    Selectors: ``default``, ``original``, ``en``, ``zh-Hant`` — nothing
    else is a valid generation target. Returns the canonical output
    language (e.g. ``en``, ``fi``, ``zh-Hant``), or ``""`` when an
    explicit Original is requested but the source language is unknown
    (the caller may perform bounded detection; read paths must treat
    ``""`` as "unresolved", never silently fall back).
    """
    if requested_language == "default":
        return resolve_default_language(transcript)
    if requested_language == "en":
        return "en"
    if requested_language == "zh-Hant":
        return ZH_HANT
    if requested_language == "original":
        source = canonicalize_language(transcript.language_observed or "")
        if not source:
            return ""
        return output_language_for_source(source)
    raise ConfigError(f"unsupported language target: {requested_language}")
