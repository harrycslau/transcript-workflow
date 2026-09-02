"""Read-only summary-variant view-model for the web layer.

This module is the ONLY place that resolves a requested language
selector into what a page should display. Views, exports, and the POST
action layer all consume it, so "requested selector" and "concretely
resolved output language" are never conflated again.

GET-safety contract: every function here is strictly read-only —
database reads only. No network, no subprocess, no detection, no file
writes, no database writes. An unresolved Original (unknown source
language) is reported as a status, never resolved by side effects on a
GET.

Selector rules (approved policy):

- GENERATION (POST) accepts only ``default``, ``original``, ``en``,
  ``zh-Hant``.
- READ/DISPLAY/EXPORT (GET) additionally accepts a concrete
  ``output_language`` that already exists for the active transcript
  (e.g. ``fi`` after an Original generation).
- An unknown concrete language is a friendly 404 — never an uncaught
  exception and never a silent fallback to the default.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from workflow.models import (
    ProcessingStatus,
    Recording,
    Summary,
    SummaryState,
    SummaryVariantState,
    Transcript,
)
from workflow.services.languages import GENERATION_SELECTORS
from workflow.services.langresolve import (
    resolve_default_language,
    resolve_output_language,
)

# Friendly display names for common concrete languages; unknown codes
# are displayed as their canonical code.
LANGUAGE_LABELS = {
    "fi": "Finnish",
    "sv": "Swedish",
    "da": "Danish",
    "no": "Norwegian",
    "nb": "Norwegian Bokmål",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "ru": "Russian",
    "ja": "Japanese",
    "ko": "Korean",
    "th": "Thai",
    "vi": "Vietnamese",
}

_FIXED_LABELS = {
    "default": "Default",
    "en": "English",
    "zh-Hant": "Traditional Chinese",
    "original": "Original",
}


def label_for(selector: str, resolved: str) -> str:
    if selector in _FIXED_LABELS:
        return _FIXED_LABELS[selector]
    return LANGUAGE_LABELS.get(selector, selector)


@dataclass
class VariantOption:
    """One selectable language tab."""

    selector: str  # value for ?language= (read selector)
    label: str
    resolved: str  # concrete output language when locally resolvable, else ""
    is_default: bool = False
    status: str | None = None  # existing variant state status, else None
    regeneration_failed: bool = False
    # Generation selector for this tab's action form, or None when the
    # tab is READ-ONLY (a concrete language that cannot be represented
    # by an approved generation selector). Never a concrete read-only
    # selector itself: generation posts only default/original/en/zh-Hant.
    action_selector: str | None = None


@dataclass
class VariantView:
    """What a page should render for a requested language selector."""

    requested: str
    resolved: str = ""  # concrete output language, "" when not resolvable
    error: str | None = None  # stable error code, e.g. "unknown_language"
    summary: Summary | None = None
    variant_state: SummaryVariantState | None = None
    action_mode: str | None = None  # first / retry_summary / regenerate
    # The GENERATION selector the action form must submit for the
    # selected target (default/original/en/zh-Hant), or None when the
    # selected read-only tab cannot map to an approved generation
    # selector (no action is offered).
    action_selector: str | None = None
    unresolved_original: bool = False
    default_language: str = ""
    source_language: str = ""
    default_summary: Summary | None = None
    options: list[VariantOption] = field(default_factory=list)
    has_transcript: bool = False

    @property
    def export_query(self) -> str:
        """Query-string fragment preserving the requested language for
        export/copy links (``""`` for the default selector)."""
        if self.requested in ("", "default"):
            return ""
        return f"&language={self.requested}"


def _action_selector_for(selector: str, original_output: str) -> str | None:
    """Map a read selector to its approved GENERATION selector."""
    if selector in ("default", "en", "zh-Hant", "original"):
        return selector
    # Existing concrete read-only language: it can be regenerated via
    # the Original selector only when it IS the resolved original
    # output; otherwise it is read-only.
    if selector == original_output:
        return "original"
    return None


def existing_variant_languages(recording: Recording, transcript: Transcript) -> list[str]:
    """Concrete output languages that already exist for the active
    transcript (active summaries in scope ∪ variant states)."""
    languages = set(
        Summary.objects.filter(
            transcript=transcript,
            section__ordinal=0,
            is_active=True,
        )
        .exclude(output_language="")
        .values_list("output_language", flat=True)
    )
    languages.update(
        SummaryVariantState.objects.filter(transcript=transcript)
        .exclude(output_language="")
        .values_list("output_language", flat=True)
    )
    return sorted(languages)


def valid_read_selectors(recording: Recording) -> set[str]:
    """Selectors a GET may request: the four standard ones plus every
    concrete language that already exists for the active transcript."""
    selectors = set(GENERATION_SELECTORS)
    transcript = recording.transcripts.filter(is_active=True).first()
    if transcript is not None:
        selectors.update(existing_variant_languages(recording, transcript))
    return selectors


def build_variant_view(recording: Recording, requested: str | None) -> VariantView:
    """Resolve ``requested`` against current database state (read-only).

    Bounded queries: the active transcript, its ordinal-0 section, the
    routing decision, the in-scope active summaries, and the variant
    states are each fetched once and reused for every derived value.
    """
    requested = (requested or "").strip() or "default"
    view = VariantView(requested=requested)

    transcript = recording.transcripts.filter(is_active=True).first()
    if transcript is None:
        return view
    view.has_transcript = True
    section = transcript.sections.filter(ordinal=0).first()
    view.default_language = resolve_default_language(transcript)

    summaries_by_lang: dict[str, Summary] = {}
    states: dict[str, SummaryVariantState] = {}
    if section is not None:
        for summary in Summary.objects.filter(
            transcript=transcript, section=section, is_active=True
        ):
            summaries_by_lang.setdefault(summary.output_language, summary)
        states = {
            vs.output_language: vs
            for vs in SummaryVariantState.objects.filter(
                transcript=transcript, section=section
            )
        }

    view.source_language = transcript.language_observed or ""
    view.default_summary = summaries_by_lang.get(view.default_language)

    # Tab options: the four standard selectors + existing concrete
    # variants not already covered.
    existing = sorted(
        {lang for lang in summaries_by_lang if lang}
        | {lang for lang in states if lang}
    )
    covered = set(GENERATION_SELECTORS)
    original_output = resolve_output_language(transcript, "original")
    options = [
        VariantOption(
            selector="default",
            label="Default",
            resolved=view.default_language,
            is_default=True,
            action_selector="default",
        ),
        VariantOption(selector="en", label="English", resolved="en", action_selector="en"),
        VariantOption(
            selector="zh-Hant", label="Traditional Chinese",
            resolved="zh-Hant", action_selector="zh-Hant",
        ),
        VariantOption(
            selector="original", label="Original",
            resolved=original_output, action_selector="original",
        ),
    ]
    for language in existing:
        if language in covered:
            continue
        covered.add(language)
        options.append(
            VariantOption(
                selector=language,
                label=label_for(language, language),
                resolved=language,
                action_selector=_action_selector_for(language, original_output),
            )
        )
    for option in options:
        if option.resolved and option.resolved in states:
            option.status = states[option.resolved].status
            option.regeneration_failed = states[option.resolved].regeneration_failed
    view.options = options

    # Resolve the requested selector.
    if requested in ("default", "en", "zh-Hant", "original"):
        resolved = resolve_output_language(transcript, requested)
    elif requested in existing:
        resolved = requested
    else:
        view.error = "unknown_language"
        return view

    view.resolved = resolved
    view.action_selector = _action_selector_for(requested, original_output)
    if requested == "original" and not resolved:
        view.unresolved_original = True
        # Generation is still possible (it will run bounded detection),
        # but there is nothing to display yet.
        if recording.processing_status == ProcessingStatus.TRANSCRIBED:
            view.action_mode = "first"
        return view

    view.summary = summaries_by_lang.get(resolved)
    view.variant_state = states.get(resolved)
    view.action_mode = _mode_for_language(
        recording, section, resolved, summaries_by_lang, states, view.default_language
    )
    return view


def _mode_for_language(
    recording: Recording,
    section,
    resolved: str,
    summaries_by_lang: dict[str, Summary],
    states: dict[str, SummaryVariantState],
    default_language: str,
) -> str | None:
    """Per-language action mode, derived locally (no extra queries).

    Mirrors ``web_actions.summarize_mode(output_language=...)``: the
    variant state is authoritative when it exists; the recording-level
    tuple is the fallback for the currently derived default language.
    """
    from workflow.models import ProcessingStatus as _PS

    if recording.processing_status != _PS.TRANSCRIBED or section is None:
        return None
    vs = states.get(resolved)
    if vs is not None:
        if vs.status == SummaryVariantState.VariantStatus.CURRENT:
            return "regenerate"
        if vs.status == SummaryVariantState.VariantStatus.FAILED:
            return "retry_summary"
        return "first"
    if summaries_by_lang.get(resolved) is not None:
        return "regenerate"
    if resolved == default_language and recording.summary_status == SummaryState.FAILED:
        return "retry_summary"
    return "first"
