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


def key_point_lines(points) -> list[str]:
    """Render new structured points and historical string points safely.

    Number counters are application-owned, so model output cannot produce
    duplicate or malformed outline numbers. Historical summaries remain
    readable as ordinary bullets.
    """
    lines: list[str] = []
    counters = [0, 0, 0]
    for point in points if isinstance(points, list) else []:
        if isinstance(point, str):
            lines.append(f"- {point}")
            continue
        if not isinstance(point, dict):
            continue
        text = str(point.get("text", "")).strip()
        if not text:
            continue
        level = point.get("level", 0)
        if isinstance(level, bool) or level not in (1, 2, 3):
            lines.append(f"- {text}")
            continue
        index = level - 1
        counters[index] += 1
        for lower in range(index + 1, 3):
            counters[lower] = 0
        number = ".".join(str(value) for value in counters[:level])
        label = f"{number}." if level == 1 else number
        lines.append(f"{label} {text}")
    return lines


def key_point_nodes(points) -> list[dict]:
    """Nested ordered-list tree for web rendering of key points.

    Returns a list of ``{"text": str, "children": [...]}`` nodes where a
    structured ``{"text": exact str, "level": 1|2|3}`` row nests under
    the most recent shallower row, giving real ``<ol>/<li>`` semantics
    (browser numbering reproduces 1. / 1.1 / 1.1.1).

    Historical exact strings and malformed/level-0 rows stay safely
    readable as flat top-level items; orphaned deeper levels fall back
    to a top-level item rather than being dropped. Arbitrary values are
    never coerced: a non-exact-``str`` ``text`` is skipped, and ``level``
    is accepted only as the exact ints 1..3 (bool rejected).
    """
    nodes: list[dict] = []
    last: dict[int, dict | None] = {1: None, 2: None, 3: None}
    for point in points if isinstance(points, list) else []:
        if isinstance(point, str):
            node = {"text": point, "children": []}
            nodes.append(node)
            last = {1: None, 2: None, 3: None}
            continue
        if not isinstance(point, dict):
            continue
        text = point.get("text")
        if not isinstance(text, str):
            continue  # never invoke arbitrary coercion on non-str values
        level = point.get("level", 0)
        if isinstance(level, bool) or level not in (1, 2, 3):
            node = {"text": text, "children": []}
            nodes.append(node)
            last = {1: None, 2: None, 3: None}
            continue
        node = {"text": text, "children": []}
        if level == 1:
            nodes.append(node)
            last = {1: node, 2: None, 3: None}
        else:
            parent = last[level - 1]
            if parent is None:
                nodes.append(node)  # orphaned deeper level: keep readable
            else:
                parent["children"].append(node)
            last[level] = node
            for lower in range(level + 1, 4):
                last[lower] = None
    return nodes


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
        lines += key_point_lines(summary.key_points)
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
        lines += key_point_lines(summary.key_points)
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
