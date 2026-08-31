"""Deterministic renderers from the canonical structured summary.

The validated structured fields stored on ``Summary`` are canonical;
these renderers never parse model-generated Markdown and always produce
stable output for the same stored data. Empty optional sections are
omitted where practical; the "Action items" section renders an explicit
"No action items identified." line when empty (it is a meaningful
statement, unlike an absent heading).
"""

from __future__ import annotations

from workflow.models import Summary


def _action_item_line(item: dict) -> str:
    parts = [item.get("text", "")]
    owner = item.get("owner")
    due = item.get("due_date")
    detail = [d for d in (f"owner: {owner}" if owner else "", f"due: {due}" if due else "") if d]
    if detail:
        parts.append(f"({'; '.join(detail)})")
    return " ".join(parts)


def _tag_names(summary: Summary) -> list[str]:
    raw = summary.suggested_tags_raw if isinstance(summary.suggested_tags_raw, dict) else {}
    return [str(name) for name in raw.get("suggested", [])]


def render_markdown(summary: Summary) -> str:
    lines: list[str] = [f"# {summary.title}", "", "## Overview", "", summary.overview]
    if summary.key_points:
        lines += ["", "## Key points"]
        lines += [f"- {point}" for point in summary.key_points]
    lines += ["", "## Action items"]
    if summary.action_items:
        lines += [f"- {_action_item_line(item)}" for item in summary.action_items]
    else:
        lines += ["- No action items identified."]
    for heading, values in (
        ("People", summary.people),
        ("Organizations", summary.organizations),
        ("Topics", summary.topics),
    ):
        if values:
            lines += ["", f"## {heading}"]
            lines += [f"- {value}" for value in values]
    tags = _tag_names(summary)
    if tags:
        lines += ["", "## Tags", "", ", ".join(tags)]
    return "\n".join(lines) + "\n"


def render_text(summary: Summary) -> str:
    lines: list[str] = [f"Title: {summary.title}", "", f"Overview: {summary.overview}"]
    if summary.key_points:
        lines += ["", "Key points:"]
        lines += [f"- {point}" for point in summary.key_points]
    lines += ["", "Action items:"]
    if summary.action_items:
        lines += [f"- {_action_item_line(item)}" for item in summary.action_items]
    else:
        lines += ["- No action items identified."]
    for heading, values in (
        ("People", summary.people),
        ("Organizations", summary.organizations),
        ("Topics", summary.topics),
    ):
        if values:
            lines += ["", f"{heading}:"]
            lines += [f"- {value}" for value in values]
    tags = _tag_names(summary)
    if tags:
        lines += ["", f"Tags: {', '.join(tags)}"]
    return "\n".join(lines) + "\n"


def summary_to_dict(summary: Summary) -> dict:
    """Machine-readable structured payload (no secrets, no raw prompts)."""
    return {
        "summary_id": summary.pk,
        "recording_id": summary.recording_id,
        "transcript_id": summary.transcript_id,
        "ordinal": summary.ordinal,
        "is_active": summary.is_active,
        "title": summary.title,
        "overview": summary.overview,
        "key_points": summary.key_points,
        "action_items": summary.action_items,
        "people": summary.people,
        "organizations": summary.organizations,
        "topics": summary.topics,
        "language": summary.language,
        "suggested_tags": _tag_names(summary),
        "model_id": summary.model_id,
        "prompt_version": summary.prompt_version,
        "parser_version": summary.parser_version,
        "config_fingerprint": summary.config_fingerprint,
        "chunk_count": summary.chunk_count,
        "input_characters": summary.input_characters,
        "input_truncated": summary.input_truncated,
        "limits_used": summary.limits_used,
        "generation_mode": summary.generation_mode,
        "created_at": summary.created_at.isoformat(),
        "attempt_id": summary.attempt_id,
    }
