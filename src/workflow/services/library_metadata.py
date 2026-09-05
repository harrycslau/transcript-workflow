"""Shared deterministic Recording-metadata contract.

ONE home for the Library display-title chain and the deterministic
source-filename / active-tag extractions. Both the web Library
(``workflow.query`` — where SQL annotations mirror these semantics and a
parity test binds them) and the search index
(``workflow.services.search_index``) use these helpers; neither layer
reimplements the policy, and the search layer never imports the web
layer.

The title chain (identical to ``query._display_title_expression``):
active default-language whole-recording Summary title → any active
whole-recording Summary title (deterministic lowest ordinal) → preferred
AudioSource filename → placeholder.

Every function here is pure: it reads data the caller already has
(prefetched ``to_attr`` lists or plain iterables) and NEVER issues a
database query.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from workflow.models import AudioSource, Summary, TagAssignment

TITLE_PLACEHOLDER = "Untitled recording"


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value or "")


def source_sort_key(source: "AudioSource") -> tuple:
    """Deterministic AudioSource order: canonical first, then earliest
    ``first_seen_at``, then PK (mirrors ``query._source_filename_subquery``)."""
    return (
        0 if source.is_canonical else 1,
        source.first_seen_at,
        source.pk,
    )


def sorted_source_rows(sources: Iterable["AudioSource"]) -> list:
    """Sources in deterministic presentation order (pure; no queries)."""
    return sorted(sources, key=source_sort_key)


def preferred_source_filename(sources: Iterable["AudioSource"]) -> str | None:
    """The preferred source's original filename, or None when there is
    no source. Filenames only — never directory paths."""
    for source in sorted_source_rows(sources):
        if source.original_filename:
            return _nfc(source.original_filename)
    return None


def deterministic_source_filenames(sources: Iterable["AudioSource"]) -> list[str]:
    """All source filenames in deterministic order, duplicates removed,
    empty names skipped. Never contains a directory separator by design
    of ``AudioSource.original_filename``; nothing here adds one."""
    names: list[str] = []
    seen: set[str] = set()
    for source in sorted_source_rows(sources):
        name = _nfc(source.original_filename).strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def summary_title_parts(
    summaries: Iterable["Summary"], default_language: str | None
) -> tuple[str | None, str | None]:
    """The two Summary title chain inputs from active whole-recording
    Summary rows ordered by ordinal (the Library prefetch contract).

    Returns ``(default_language_title, any_title)`` — the first title in
    the derived default output language, and the lowest-ordinal active
    title. Pure; no queries.
    """
    rows = list(summaries)
    any_title: str | None = None
    default_title: str | None = None
    for row in rows:
        title = _nfc(row.title)
        if title and any_title is None:
            any_title = title
        if title and default_language and row.output_language == default_language:
            if default_title is None:
                default_title = title
        if any_title is not None and default_title is not None:
            break
    return default_title, any_title


def display_title_from_parts(
    *,
    default_summary_title: str | None,
    fallback_summary_title: str | None,
    preferred_filename: str | None,
) -> str:
    """The single Library title from its three chain inputs."""
    for value in (default_summary_title, fallback_summary_title, preferred_filename):
        if value:
            return value
    return TITLE_PLACEHOLDER


def display_title_from_recording(recording) -> str:
    """Python reference implementation of the display-title contract,
    reading ONLY prefetched data when present (never issues a query):

    - ``default_output_language`` annotation (optional)
    - ``current_summary_rows`` to_attr (active ordinal-0 Summaries, ordinal order)
    - ``presentation_sources`` to_attr
    """
    default_language = getattr(recording, "default_output_language", None)
    summaries = getattr(recording, "current_summary_rows", None)
    sources = getattr(recording, "presentation_sources", None)
    if sources is None:
        sources = getattr(recording, "sources", None) or []
    default_title, any_title = summary_title_parts(summaries or [], default_language)
    return display_title_from_parts(
        default_summary_title=default_title,
        fallback_summary_title=any_title,
        preferred_filename=preferred_source_filename(sources),
    )


def active_tag_names(assignments: Iterable["TagAssignment"]) -> list[str]:
    """Active/effective tag display names, sorted deterministically by
    the NFC+casefold ``name_key`` (tie-break display name)."""
    rows = sorted(
        (a for a in assignments if a.is_active and a.tag_id),
        key=lambda a: (a.tag.name_key, a.tag.name),
    )
    return [_nfc(a.tag.name) for a in rows]
