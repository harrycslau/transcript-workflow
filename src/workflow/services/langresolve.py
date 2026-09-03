"""Output-language resolution (default / original / explicit).

Kept separate from ``summarize`` so the variant-state reconciler and
the summarization pipeline can both depend on it without a circular
import. ``summarize`` re-exports these names for backwards
compatibility.
"""

from __future__ import annotations

from django.db.models import Case, CharField, Exists, OuterRef, Q, Value, When

from brainlib.config import ConfigError
from workflow.models import RoutingDecision, RoutingMethod, Transcript
from workflow.services.languages import (
    CHINESE_FAMILY_PRIMARIES,
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


def _primary_subtag_q(field: str, primary: str) -> Q:
    """Q matching ``field`` equal to ``primary`` or ``primary-<subtags>``.

    Stored language values are always canonical (see ``languages.py``),
    so a primary-subtag test is an exact match or a ``primary-`` prefix.
    """
    return Q(**{field: primary}) | Q(**{f"{field}__startswith": f"{primary}-"})


def _chinese_family_q(field: str) -> Q:
    q = Q()
    for primary in CHINESE_FAMILY_PRIMARIES:
        q |= _primary_subtag_q(field, primary)
    return q


def default_output_language_expression():
    """ORM expression producing the same value as :func:`resolve_default_language`.

    Meant to be used as an annotation inside a Transcript query context
    (typically a Subquery over the ACTIVE Transcript of a Recording), so
    the routing-decision evidence is read through ``Exists`` subqueries
    against the transcript's recording — no Python per-recording calls,
    no duplication of the language policy in unrelated layers.

    Condition order mirrors ``resolve_default_language`` exactly:
    user-corrected source (zh/non-zh), verified Chinese routing, then
    unverified automatic Chinese routing, then transcript-observed
    source (zh/non-zh), then the English fallback.
    """
    observed = ~Q(language_observed="")
    chinese = _chinese_family_q("language_observed")
    user_corrected = Q(language_observed_verified_by="user") & observed

    confirmed_chinese = Exists(
        RoutingDecision.objects.filter(
            recording=OuterRef("recording_id"),
            is_active=True,
            routing_verified=True,
            route_suggestion__in=("cantonese", "mandarin"),
        )
    )
    automatic_chinese = Exists(
        RoutingDecision.objects.filter(
            recording=OuterRef("recording_id"),
            is_active=True,
            method=RoutingMethod.AUTOMATIC,
            route_suggestion__in=("cantonese", "mandarin"),
        )
    )

    return Case(
        When(user_corrected & chinese, then=Value(ZH_HANT)),
        When(user_corrected & ~chinese, then=Value(DEFAULT_OUTPUT_LANGUAGE)),
        When(confirmed_chinese, then=Value(ZH_HANT)),
        When(automatic_chinese, then=Value(ZH_HANT)),
        When(observed & chinese, then=Value(ZH_HANT)),
        When(observed & ~chinese, then=Value(DEFAULT_OUTPUT_LANGUAGE)),
        default=Value(DEFAULT_OUTPUT_LANGUAGE),
        output_field=CharField(max_length=32),
    )
