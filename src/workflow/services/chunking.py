"""Deterministic, bounded transcript chunking on segment boundaries.

Design (Step 3):

- The ENTIRE transcript is chunked; nothing is ever truncated. The
  ``max_total_characters`` and ``max_chunk_count`` limits are enforced
  by :func:`check_chunk_limits` AFTER the deterministic chunk plan is
  computed, so both failure modes can report the measured input size
  and computed chunk count before any HTTP call is made.
- Chunks split on whole transcript segments (chronological order). A
  single segment longer than ``chunk_characters`` is hard-split at a
  character boundary — Python string slicing operates on code points,
  so a split can never fall inside a Unicode code point.
- Overlap: each chunk after the first begins with the trailing whole
  segments of the previous chunk totalling at most
  ``chunk_overlap_characters``; if the overlap would leave no room for
  the next segment it is trimmed from the front.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class InputTooLarge(Exception):
    """Pre-flight failure: the input exceeds a hard configured limit.

    ``reason`` is one of ``total_too_large`` | ``too_many_chunks``;
    the message contains only counts and limits (never transcript text).
    """

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass
class ChunkPlan:
    """A computed, fully measured chunk plan (limits not yet applied)."""

    chunks: list[str] = field(default_factory=list)
    input_characters: int = 0  # source transcript characters (overlap excluded)
    processed_characters: int = 0  # sum of chunk payloads (overlap included)


def split_oversized(text: str, limit: int) -> list[str]:
    """Split ``text`` into pieces of at most ``limit`` characters.

    Python strings are sequences of Unicode code points, so slicing
    never splits mid-code-point (astral-plane characters included).
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def _overlap_segments(chunk_texts: list[str], overlap_characters: int) -> list[str]:
    """Trailing whole segments of the previous chunk, chronologically."""
    if overlap_characters <= 0 or not chunk_texts:
        return []
    picked: list[str] = []
    used = 0
    for text in reversed(chunk_texts):
        cost = len(text) + (1 if picked else 0)  # newline separator
        if used + cost > overlap_characters:
            break
        picked.insert(0, text)
        used += cost
    return picked


def build_chunks(
    texts: list[str],
    *,
    chunk_characters: int,
    overlap_characters: int,
) -> ChunkPlan:
    """Deterministically split segment texts into bounded chunk payloads.

    Pure function: no limits beyond ``chunk_characters`` are applied
    here, so the plan (and its measured sizes) exists before the
    ``max_total_characters`` / ``max_chunk_count`` pre-flight checks.
    """
    plan = ChunkPlan(input_characters=sum(len(t) for t in texts))
    current: list[str] = []

    def current_length(parts: list[str]) -> int:
        return sum(len(p) for p in parts) + max(0, len(parts) - 1)

    for text in texts:
        pieces = [text] if len(text) <= chunk_characters else split_oversized(text, chunk_characters)
        for piece in pieces:
            if current and current_length(current) + 1 + len(piece) > chunk_characters:
                closed = current
                plan.chunks.append("\n".join(closed))
                current = _overlap_segments(closed, overlap_characters)
                # Trim overlap from the front while the new piece still
                # does not fit; the piece alone always fits.
                while current and current_length(current) + 1 + len(piece) > chunk_characters:
                    current.pop(0)
            current.append(piece)
            if current_length(current) >= chunk_characters:
                plan.chunks.append("\n".join(current))
                current = []
    if current:
        plan.chunks.append("\n".join(current))
    plan.processed_characters = sum(len(c) for c in plan.chunks)
    return plan


def check_chunk_limits(
    plan: ChunkPlan,
    *,
    max_total_characters: int,
    max_chunk_count: int,
) -> None:
    """Pre-flight hard-limit checks; raises :class:`InputTooLarge`.

    Both checks see the measured plan, so failures can record the input
    character count and the computed chunk count durably.
    """
    if plan.input_characters > max_total_characters:
        raise InputTooLarge(
            "total_too_large",
            f"transcript has {plan.input_characters} characters, exceeding the total limit "
            f"of {max_total_characters}",
        )
    if len(plan.chunks) > max_chunk_count:
        raise InputTooLarge(
            "too_many_chunks",
            f"transcript splits into {len(plan.chunks)} chunks, exceeding the limit of {max_chunk_count}",
        )
