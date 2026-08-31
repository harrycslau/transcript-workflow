"""Tests for deterministic transcript chunking."""

from __future__ import annotations

import pytest

from workflow.services.chunking import (
    ChunkPlan,
    InputTooLarge,
    build_chunks,
    check_chunk_limits,
    split_oversized,
)


def segment_texts(plan: ChunkPlan) -> list[str]:
    return plan.chunks


class TestBuildChunks:
    def test_short_transcript_is_one_chunk(self):
        plan = build_chunks(["hello world"], chunk_characters=100, overlap_characters=10)
        assert plan.chunks == ["hello world"]
        assert plan.input_characters == 11

    def test_splits_on_segment_boundaries(self):
        texts = ["a" * 60, "b" * 60, "c" * 60]
        plan = build_chunks(texts, chunk_characters=100, overlap_characters=0)
        assert len(plan.chunks) == 3
        assert plan.chunks[0] == "a" * 60
        assert plan.chunks[1] == "b" * 60
        assert plan.chunks[2] == "c" * 60

    def test_chunks_never_exceed_limit(self):
        texts = ["x" * 37 for _ in range(20)]
        plan = build_chunks(texts, chunk_characters=100, overlap_characters=0)
        assert all(len(chunk) <= 100 for chunk in plan.chunks)
        assert plan.chunks[0] == "\n".join(["x" * 37, "x" * 37])  # 75 chars; third would exceed

    def test_chronological_order_preserved(self):
        texts = [f"seg{i:02d} " + "y" * 20 for i in range(10)]
        plan = build_chunks(texts, chunk_characters=80, overlap_characters=0)
        joined = "\n".join(plan.chunks)
        positions = [joined.index(f"seg{i:02d}") for i in range(10)]
        assert positions == sorted(positions)

    def test_overlap_trailing_segments_of_previous_chunk(self):
        texts = ["a" * 20, "b" * 20, "c" * 20]
        plan = build_chunks(texts, chunk_characters=45, overlap_characters=25)
        # Chunk 1: a+b; chunk 2 starts with the trailing overlap (b) then c.
        assert plan.chunks[0] == "a" * 20 + "\n" + "b" * 20
        assert plan.chunks[1].startswith("b" * 20 + "\n")
        assert plan.chunks[1].endswith("c" * 20)
        # Overlap respects the character budget.
        overlap_part = plan.chunks[1].rsplit("\n", 1)[0]
        assert len(overlap_part) <= 25

    def test_oversized_segment_hard_split(self):
        text = "z" * 250
        plan = build_chunks([text], chunk_characters=100, overlap_characters=10)
        assert len(plan.chunks) == 3
        assert "".join(plan.chunks) == text

    def test_unicode_code_points_never_split(self):
        # Astral-plane characters (surrogate-pair territory in UTF-16).
        text = "😀😁😂🤣😃😄" * 40
        plan = build_chunks([text], chunk_characters=20, overlap_characters=0)
        assert "".join(plan.chunks) == text
        for chunk in plan.chunks:
            for char in chunk:
                assert "\ud800" > char or char > "\udfff"  # never a lone surrogate

    def test_cantonese_content_round_trips(self):
        text = "今日天氣好agi好，我哋去咗飲茶。" * 30
        plan = build_chunks([text], chunk_characters=64, overlap_characters=8)
        assert "".join(plan.chunks) == text

    def test_processed_characters_counts_overlap(self):
        texts = ["a" * 40, "b" * 40, "c" * 40]
        plan = build_chunks(texts, chunk_characters=90, overlap_characters=45)
        assert plan.processed_characters > plan.input_characters


class TestLimits:
    def test_total_limit_fails_cleanly_with_measured_counts(self):
        texts = ["a" * 100 for _ in range(30)]
        plan = build_chunks(texts, chunk_characters=1000, overlap_characters=0)
        with pytest.raises(InputTooLarge) as excinfo:
            check_chunk_limits(plan, max_total_characters=1000, max_chunk_count=100)
        assert excinfo.value.reason == "total_too_large"
        assert "3000" in str(excinfo.value)

    def test_chunk_count_limit_fails_cleanly(self):
        texts = ["a" * 10 for _ in range(30)]
        plan = build_chunks(texts, chunk_characters=25, overlap_characters=0)
        with pytest.raises(InputTooLarge) as excinfo:
            check_chunk_limits(plan, max_total_characters=10**6, max_chunk_count=5)
        assert excinfo.value.reason == "too_many_chunks"

    def test_within_limits_passes(self):
        plan = build_chunks(["hello"], chunk_characters=100, overlap_characters=0)
        check_chunk_limits(plan, max_total_characters=1000, max_chunk_count=5)


class TestSplitOversized:
    def test_basic(self):
        assert split_oversized("abcdef", 2) == ["ab", "cd", "ef"]

    def test_exact_multiple(self):
        assert split_oversized("abcd", 2) == ["ab", "cd"]

    def test_rejects_non_positive_limit(self):
        with pytest.raises(ValueError):
            split_oversized("abc", 0)

    def test_empty_text(self):
        assert split_oversized("", 5) == []
