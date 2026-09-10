"""Pure semantic-query contracts and bounded grouped top-K (Step 5C).

Proves the pure primitives ONLY: query normalization/validation (same
plain-text contract as keyword search, WITHOUT the eight-term cap),
``prepare_query_text`` (unchanged v1 payload), numerically safe cosine
with zero-norm distinctions, and the exact grouped per-recording top-K
(one finished winner per Recording, deterministic comparator, ordered
input validation, bounded K, fail-closed zero document vectors,
provenance carriers and privacy-safe errors).

No database, no network, no embeddings, no hybrid fusion, no web.
"""

from __future__ import annotations

import math
import sys

import pytest

from workflow.services import semantic_query as sq

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_version_and_caps(self):
        assert sq.SEMANTIC_QUERY_VERSION == "1"
        assert sq.SEMANTIC_RESULT_LIMIT_MAX == 200
        assert sq.SEMANTIC_SNIPPET_MAX_CODEPOINTS == 320
        assert sq.SEMANTIC_MAX_QUERY_CODEPOINTS == 256

    def test_caps_are_the_keyword_search_values(self):
        from workflow.services import search_query as kw

        assert sq.SEMANTIC_RESULT_LIMIT_MAX == kw.MAX_RESULT_LIMIT
        assert sq.SEMANTIC_SNIPPET_MAX_CODEPOINTS == kw.SNIPPET_MAX_CODEPOINTS
        assert sq.SEMANTIC_MAX_QUERY_CODEPOINTS == kw.MAX_QUERY_CODEPOINTS


# ---------------------------------------------------------------------------
# Query input
# ---------------------------------------------------------------------------


class TestQueryInput:
    def test_normalize_is_nfc_and_stripped(self):
        assert sq.normalize_semantic_query("  a\u0308iti \n") == "äiti"

    def test_none_empty_and_whitespace_raise_without_echo(self):
        for raw in (None, "", "   \t\n "):
            with pytest.raises(sq.SemanticQueryInputError) as excinfo:
                sq.normalize_semantic_query(raw)
            assert "empty" in str(excinfo.value)
            assert excinfo.value.code == sq.INVALID_QUERY

    def test_non_string_rejected(self):
        for raw in (b"bytes", 123, ["list"]):
            with pytest.raises(sq.SemanticQueryInputError) as excinfo:
                sq.normalize_semantic_query(raw)
            assert excinfo.value.code == sq.INVALID_QUERY
            assert "string" in str(excinfo.value)

    def test_too_long_raises_without_echoing_query(self):
        sentinel = "SUPER-PRIVATE-" + "x" * 300
        with pytest.raises(sq.SemanticQueryInputError) as excinfo:
            sq.normalize_semantic_query(sentinel)
        assert "SUPER-PRIVATE" not in str(excinfo.value)
        assert "256" in str(excinfo.value)

    def test_exactly_max_codepoints_accepted(self):
        query = "x" * sq.SEMANTIC_MAX_QUERY_CODEPOINTS
        assert sq.normalize_semantic_query(query) == query

    @pytest.mark.parametrize(
        "bad_limit", [0, -1, sq.SEMANTIC_RESULT_LIMIT_MAX + 1, True, False, "5", 1.0]
    )
    def test_limit_bounds(self, bad_limit):
        with pytest.raises(sq.SemanticQueryInputError) as excinfo:
            sq.validate_semantic_query("anything", limit=bad_limit)
        assert excinfo.value.code == sq.INVALID_LIMIT

    @pytest.mark.parametrize("limit", [1, 50, 200])
    def test_limit_in_range_accepted(self, limit):
        assert sq.validate_semantic_query("anything", limit=limit) == "anything"

    def test_more_than_eight_words_accepted(self):
        # Keyword search caps at eight terms; semantic search is one
        # embedding payload and must NOT inherit that cap.
        query = "one two three four five six seven eight nine ten"
        assert sq.validate_semantic_query(query) == query

    def test_prepare_query_text_returns_exact_normalized_unchanged(self):
        normalized = sq.normalize_semantic_query("  KÄYTÖSSÄ  ")
        assert sq.prepare_query_text(normalized) == "KÄYTÖSSÄ"

    def test_prepare_query_text_no_truncation(self):
        query = "y" * sq.SEMANTIC_MAX_QUERY_CODEPOINTS
        assert sq.prepare_query_text(query) == query
        assert len(sq.prepare_query_text(query)) == sq.SEMANTIC_MAX_QUERY_CODEPOINTS

    @pytest.mark.parametrize("bad", [None, 1, b"x", "", "   "])
    def test_prepare_query_text_rejects_bad(self, bad):
        with pytest.raises(sq.SemanticQueryInputError) as excinfo:
            sq.prepare_query_text(bad)
        assert excinfo.value.code == sq.INVALID_QUERY

    def test_prepare_query_text_rejects_non_normalized(self):
        # The argument must ALREADY equal its normalized form; this helper
        # never silently normalizes.
        for bad in ("  leading", "trailing  ", "a\u0308iti", "  KÄYTÖSSÄ  "):
            with pytest.raises(sq.SemanticQueryInputError) as excinfo:
                sq.prepare_query_text(bad)
            assert excinfo.value.code == sq.INVALID_QUERY

    def test_prepare_query_text_rejects_over_cap(self):
        query = "z" * (sq.SEMANTIC_MAX_QUERY_CODEPOINTS + 1)
        with pytest.raises(sq.SemanticQueryInputError) as excinfo:
            sq.prepare_query_text(query)
        assert excinfo.value.code == sq.INVALID_QUERY

    def test_prepare_query_text_accepts_exactly_normalized(self):
        assert sq.prepare_query_text("äiti") == "äiti"
        assert sq.prepare_query_text("中文 录音") == "中文 录音"


# ---------------------------------------------------------------------------
# Cosine
# ---------------------------------------------------------------------------


class TestCosine:
    def test_identical_vectors(self):
        assert sq.cosine_similarity((1.0, 2.0, 3.0), (1.0, 2.0, 3.0)) == pytest.approx(
            1.0
        )

    def test_orthogonal_vectors(self):
        assert sq.cosine_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        assert sq.cosine_similarity((1.0, 0.0), (-1.0, 0.0)) == pytest.approx(-1.0)

    def test_accepts_lists_and_tuples_and_ints(self):
        assert sq.cosine_similarity([1, 0], (1.0, 0.0)) == pytest.approx(1.0)

    def test_zero_query_norm_code(self):
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.cosine_similarity((0.0, -0.0), (1.0, 0.0))
        assert excinfo.value.code == sq.INVALID_QUERY_VECTOR
        assert "invalid" in str(excinfo.value)

    def test_zero_document_norm_code(self):
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.cosine_similarity((1.0, 0.0), (0.0, 0.0))
        assert excinfo.value.code == sq.INVALID_DOCUMENT_VECTOR

    def test_tiny_nonzero_document_is_not_zero(self):
        # The smallest float32 subnormal is nonzero; it must be scored,
        # never treated as the zero-norm failure.
        result = sq.cosine_similarity((1.0, 0.0), (1e-45, 0.0))
        assert result == pytest.approx(1.0)

    def test_zero_document_under_top_k_fails_closed(self):
        candidates = [_candidate("r1", "segment:r1:0", "segment", (0.0, 0.0))]
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_winners((1.0, 0.0), candidates)
        assert excinfo.value.code == sq.INVALID_DOCUMENT_VECTOR

    def test_dimension_mismatch(self):
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.cosine_similarity((1.0, 0.0), (1.0, 0.0, 0.0))
        assert excinfo.value.code == sq.DIMENSION_MISMATCH

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_nonfinite_rejected(self, bad):
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.cosine_similarity((1.0, bad), (1.0, 0.0))
        assert excinfo.value.code == sq.INVALID_QUERY_VECTOR
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.cosine_similarity((1.0, 0.0), (1.0, bad))
        assert excinfo.value.code == sq.INVALID_DOCUMENT_VECTOR

    @pytest.mark.parametrize("bad", [True, "1", None, object()])
    def test_bad_component_types_rejected(self, bad):
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.cosine_similarity((1.0, bad), (1.0, 0.0))
        assert excinfo.value.code == sq.INVALID_QUERY_VECTOR

    def test_empty_and_non_sequence_rejected(self):
        for bad in ((), [], "ab", 1.0):
            with pytest.raises(sq.SemanticQueryError):
                sq.cosine_similarity(bad, (1.0, 0.0))

    def test_huge_int_is_sanitized(self):
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.cosine_similarity((10**400, 0), (1.0, 0.0))
        assert excinfo.value.code == sq.INVALID_QUERY_VECTOR

    def test_numerically_safe_large_values(self):
        # Values near the float32 range must not overflow the norm math.
        big = 3.0e38
        result = sq.cosine_similarity((big, 0.0), (big, 0.0))
        assert result == pytest.approx(1.0)
        assert math.isfinite(result)

    def test_huge_finite_values_do_not_overflow_score(self):
        # Huge-but-finite float64 inputs previously overflowed the raw
        # dot-product path into inf/nan; the unit-vector computation
        # returns the correct finite cosine instead.
        huge = 1e308
        result = sq.cosine_similarity((huge, 0.0), (huge, 0.0))
        assert result == pytest.approx(1.0)
        assert math.isfinite(result)

    def test_query_norm_overflow_is_sanitized(self):
        # math.hypot of two max-float64 values overflows to inf; this is
        # an arithmetic failure that must map to the role-appropriate
        # sanitized error, never a raw inf leak or math exception.
        huge = sys.float_info.max
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.cosine_similarity((huge, huge), (1.0, 0.0))
        assert excinfo.value.code == sq.INVALID_QUERY_VECTOR

    def test_document_norm_overflow_is_sanitized(self):
        huge = sys.float_info.max
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.cosine_similarity((1.0, 0.0), (huge, huge))
        assert excinfo.value.code == sq.INVALID_DOCUMENT_VECTOR


# ---------------------------------------------------------------------------
# Grouped per-recording top-K
# ---------------------------------------------------------------------------


def _candidate(recording_id, document_key, doc_type, vector, **provenance):
    return sq.SemanticCandidate(
        recording_id=recording_id,
        document_key=document_key,
        doc_type=doc_type,
        vector=tuple(float(value) for value in vector),
        **provenance,
    )


# A canonical Summary PK string (the Summary model contract:
# CharField(36) default _uuid = str(uuid.uuid4())).
SUMMARY_UUID = "6d4e1e5e-6f7a-4f4e-8f0e-1234567890ab"


class TestTopK:
    def test_empty_input(self):
        assert sq.select_semantic_winners((1.0, 0.0), []) == []

    def test_single_recording_best_last_summary_wins(self):
        # Within one Recording the document_key order is recording,
        # segment, summary — i.e. the doc-type ranks (1, 2, 0) are NOT
        # monotonic. The best (last) summary must still win: the group is
        # finished only when the Recording changes.
        candidates = [
            _candidate("r1", "recording:r1", "recording", (0.1, 0.995)),
            _candidate("r1", "segment:r1:0", "segment", (0.2, 0.980)),
            _candidate("r1", "summary:r1:0", "summary", (0.99, 0.141)),
        ]
        winners = sq.select_semantic_winners((1.0, 0.0), candidates)
        assert len(winners) == 1
        assert winners[0].match.document_key == "summary:r1:0"
        assert winners[0].match.doc_type == "summary"

    def test_late_best_changes_small_k_membership(self):
        # With K=1, r2 wins if only r1's early documents are considered;
        # r1's LAST document (a summary) is the best overall and must
        # change the K membership.
        candidates = [
            _candidate("r1", "recording:r1", "recording", (0.5, 0.866)),
            _candidate("r1", "segment:r1:0", "segment", (0.5, 0.866)),
            _candidate("r1", "summary:r1:0", "summary", (0.99, 0.141)),
            _candidate("r2", "recording:r2", "recording", (0.9, 0.436)),
        ]
        winners = sq.select_semantic_winners((1.0, 0.0), candidates, limit=1)
        assert [w.match.recording_id for w in winners] == ["r1"]
        assert winners[0].match.document_key == "summary:r1:0"

    def test_per_recording_dedup_one_winner_each(self):
        candidates = [
            _candidate("r1", "segment:r1:0", "segment", (0.5, 0.866)),
            _candidate("r1", "summary:r1:0", "summary", (1.0, 0.0)),
            _candidate("r2", "segment:r2:0", "segment", (1.0, 0.0)),
        ]
        winners = sq.select_semantic_winners((1.0, 0.0), candidates)
        assert [w.match.recording_id for w in winners] == ["r1", "r2"]
        assert [w.rank for w in winners] == [1, 2]

    def test_global_order_ties_by_doc_type(self):
        # Equal score: summary ranks before segment even when it appears
        # later in the input.
        candidates = [
            _candidate("r1", "segment:r1:0", "segment", (1.0, 0.0)),
            _candidate("r2", "summary:r2:0", "summary", (1.0, 0.0)),
        ]
        winners = sq.select_semantic_winners((1.0, 0.0), candidates)
        assert [w.match.recording_id for w in winners] == ["r2", "r1"]

    def test_global_order_ties_by_document_key(self):
        candidates = [
            _candidate("r1", "summary:aaa", "summary", (1.0, 0.0)),
            _candidate("r2", "summary:bbb", "summary", (1.0, 0.0)),
        ]
        winners = sq.select_semantic_winners((1.0, 0.0), candidates)
        assert [w.match.recording_id for w in winners] == ["r1", "r2"]

    def test_global_order_ties_by_recording_id(self):
        candidates = [
            _candidate("r1", "summary:same", "summary", (1.0, 0.0)),
            _candidate("r2", "summary:same", "summary", (1.0, 0.0)),
        ]
        winners = sq.select_semantic_winners((1.0, 0.0), candidates)
        assert [w.match.recording_id for w in winners] == ["r1", "r2"]

    def test_scores_are_descending(self):
        candidates = [
            _candidate("r1", "summary:r1", "summary", (0.1, 0.995)),
            _candidate("r2", "summary:r2", "summary", (0.9, 0.436)),
            _candidate("r3", "summary:r3", "summary", (0.5, 0.866)),
        ]
        winners = sq.select_semantic_winners((1.0, 0.0), candidates)
        scores = [w.score for w in winners]
        assert scores == sorted(scores, reverse=True)

    def test_result_count_bounded_by_limit(self):
        candidates = [
            _candidate(f"r{index:02d}", f"summary:r{index:02d}", "summary", (1.0, 0.0))
            for index in range(10)
        ]
        winners = sq.select_semantic_winners((1.0, 0.0), candidates, limit=3)
        assert len(winners) == 3
        # Best-first: all scores equal, so document_key/recording_id order.
        assert [w.match.recording_id for w in winners] == ["r00", "r01", "r02"]

    @pytest.mark.parametrize(
        "bad_limit", [0, -1, sq.SEMANTIC_RESULT_LIMIT_MAX + 1, True, "3"]
    )
    def test_limit_bounds(self, bad_limit):
        candidates = [_candidate("r1", "summary:r1", "summary", (1.0, 0.0))]
        with pytest.raises(sq.SemanticQueryInputError) as excinfo:
            sq.select_semantic_winners((1.0, 0.0), candidates, limit=bad_limit)
        assert excinfo.value.code == sq.INVALID_LIMIT

    def test_limit_one_and_max_accepted(self):
        candidates = [
            _candidate(f"r{index:03d}", f"summary:r{index:03d}", "summary", (1.0, 0.0))
            for index in range(sq.SEMANTIC_RESULT_LIMIT_MAX)
        ]
        assert len(sq.select_semantic_winners((1.0, 0.0), candidates, limit=1)) == 1
        assert (
            len(
                sq.select_semantic_winners(
                    (1.0, 0.0), candidates, limit=sq.SEMANTIC_RESULT_LIMIT_MAX
                )
            )
            == sq.SEMANTIC_RESULT_LIMIT_MAX
        )

    def test_document_key_regression_rejected(self):
        candidates = [
            _candidate("r1", "summary:b", "summary", (1.0, 0.0)),
            _candidate("r1", "summary:a", "summary", (1.0, 0.0)),
        ]
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_winners((1.0, 0.0), candidates)
        assert excinfo.value.code == sq.CANDIDATE_ORDER

    def test_duplicate_document_key_rejected(self):
        candidates = [
            _candidate("r1", "summary:a", "summary", (1.0, 0.0)),
            _candidate("r1", "summary:a", "summary", (1.0, 0.0)),
        ]
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_winners((1.0, 0.0), candidates)
        assert excinfo.value.code == sq.CANDIDATE_ORDER

    def test_recording_regression_rejected(self):
        candidates = [
            _candidate("r2", "summary:a", "summary", (1.0, 0.0)),
            _candidate("r1", "summary:b", "summary", (1.0, 0.0)),
        ]
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_winners((1.0, 0.0), candidates)
        assert excinfo.value.code == sq.CANDIDATE_ORDER

    def test_recording_reappearance_rejected(self):
        candidates = [
            _candidate("r1", "summary:a", "summary", (1.0, 0.0)),
            _candidate("r2", "summary:b", "summary", (1.0, 0.0)),
            _candidate("r1", "summary:c", "summary", (1.0, 0.0)),
        ]
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_winners((1.0, 0.0), candidates)
        assert excinfo.value.code == sq.CANDIDATE_ORDER

    def test_malformed_candidate_type_rejected(self):
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_winners((1.0, 0.0), [{"recording_id": "r1"}])
        assert excinfo.value.code == sq.INVALID_CANDIDATE

    def test_malformed_candidate_field_rejected(self):
        candidate = sq.SemanticCandidate(
            recording_id=123,  # not a str
            document_key="summary:a",
            doc_type="summary",
            vector=(1.0, 0.0),
        )
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_winners((1.0, 0.0), [candidate])
        assert excinfo.value.code == sq.INVALID_CANDIDATE

    def test_zero_query_vector_rejected_before_candidates(self):
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_winners((0.0, 0.0), [])
        assert excinfo.value.code == sq.INVALID_QUERY_VECTOR

    def test_provenance_carried_without_text(self):
        candidate = _candidate(
            "r1",
            "segment:r1:0",
            "segment",
            (1.0, 0.0),
            transcript_id=1,
            segment_ordinal=0,
            start_ms=1000,
            end_ms=2000,
        )
        winner = sq.select_semantic_winners((1.0, 0.0), [candidate])[0]
        assert winner.rank == 1
        assert winner.score == pytest.approx(1.0)
        assert winner.match.transcript_id == 1
        assert winner.match.segment_ordinal == 0
        assert winner.match.start_ms == 1000
        assert winner.match.end_ms == 2000
        # No text attribute is part of the payload.
        assert not hasattr(winner.match, "text")

    def test_summary_provenance_carried(self):
        candidate = _candidate(
            "r1",
            "summary:r1:0",
            "summary",
            (1.0, 0.0),
            transcript_id=1,
            summary_id=SUMMARY_UUID,
            output_language="en",
        )
        winner = sq.select_semantic_winners((1.0, 0.0), [candidate])[0]
        assert winner.match.summary_id == SUMMARY_UUID
        assert winner.match.output_language == "en"

    def test_winners_carry_metadata_only_never_vector(self):
        # Required regression: the returned winner and its match must not
        # expose or retain the vector (or the vector-carrying carrier).
        candidate = _candidate(
            "r1",
            "segment:r1:0",
            "segment",
            (1.0, 0.0),
            transcript_id=1,
            segment_ordinal=0,
        )
        winner = sq.select_semantic_winners((1.0, 0.0), [candidate])[0]
        assert isinstance(winner.match, sq.SemanticMatch)
        assert not hasattr(winner, "vector")
        assert not hasattr(winner, "candidate")
        assert not hasattr(winner.match, "vector")
        assert winner.match.document_key == "segment:r1:0"
        assert winner.match.recording_id == "r1"
        # The vector lives only on the input carrier, never on the winner.
        assert hasattr(candidate, "vector")

    def test_winners_do_not_retain_input_vector_reference(self):
        # Mutating the input vector after selection must not affect the
        # winner (it is decoupled from the carrier at offer time).
        vector = [1.0, 0.0]
        candidate = sq.SemanticCandidate(
            recording_id="r1",
            document_key="summary:r1",
            doc_type="summary",
            vector=vector,
        )
        winner = sq.select_semantic_winners((1.0, 0.0), [candidate])[0]
        vector[:] = [0.0, 0.0]
        assert winner.score == pytest.approx(1.0)
        assert winner.match.document_key == "summary:r1"

    @pytest.mark.parametrize("bad_id", [True, 0, -1, "1", 1.0, object()])
    def test_present_transcript_id_must_be_positive_int(self, bad_id):
        candidate = sq.SemanticCandidate(
            recording_id="r1",
            document_key="segment:r1:0",
            doc_type="segment",
            vector=(1.0, 0.0),
            transcript_id=bad_id,
        )
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_winners((1.0, 0.0), [candidate])
        assert excinfo.value.code == sq.INVALID_CANDIDATE
        assert "segment:r1:0" not in str(excinfo.value)

    @pytest.mark.parametrize(
        "bad_id", [True, 0, -5, 2.0, "", object(), 123]
    )
    def test_present_summary_id_must_be_nonempty_exact_str(self, bad_id):
        candidate = sq.SemanticCandidate(
            recording_id="r1",
            document_key="summary:r1:0",
            doc_type="summary",
            vector=(1.0, 0.0),
            summary_id=bad_id,
        )
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_winners((1.0, 0.0), [candidate])
        assert excinfo.value.code == sq.INVALID_CANDIDATE

    def test_present_summary_id_accepts_legacy_or_custom_string(self):
        # The Summary PK is a CharField with a UUID default, but manually
        # supplied / legacy nonempty strings are valid: they pass through
        # direct provenance UNCHANGED (no UUID parse, no coercion).
        for legacy in ("legacy-id-1", "not-a-uuid", "  spaced  ", "0"):
            candidate = _candidate(
                "r1",
                f"summary:{legacy}",
                "summary",
                (1.0, 0.0),
                transcript_id=1,
                summary_id=legacy,
                output_language="en",
            )
            winner = sq.select_semantic_winners((1.0, 0.0), [candidate])[0]
            assert winner.match.summary_id == legacy

    def test_present_summary_id_rejects_str_subclass(self):
        class StrSub(str):
            pass

        candidate = sq.SemanticCandidate(
            recording_id="r1",
            document_key="summary:r1:0",
            doc_type="summary",
            vector=(1.0, 0.0),
            summary_id=StrSub("legacy-id"),
        )
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_winners((1.0, 0.0), [candidate])
        assert excinfo.value.code == sq.INVALID_CANDIDATE

    def test_absent_provenance_ids_are_valid(self):
        # None means the column is absent for this doc type; shape
        # completeness is a later DB-integration concern, not validated
        # here.
        candidate = _candidate("r1", "recording:r1", "recording", (1.0, 0.0))
        winner = sq.select_semantic_winners((1.0, 0.0), [candidate])[0]
        assert winner.match.transcript_id is None
        assert winner.match.summary_id is None

    @pytest.mark.parametrize("bad_type", ["notes", "meta", "transcript", ""])
    def test_unknown_doc_type_rejected(self, bad_type):
        candidate = _candidate("r1", "recording:r1", bad_type, (1.0, 0.0))
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_winners((1.0, 0.0), [candidate])
        assert excinfo.value.code == sq.INVALID_CANDIDATE

    @pytest.mark.parametrize("bad_language", [None, 5, b"en", ["en"], object()])
    def test_output_language_must_be_exact_str(self, bad_language):
        candidate = _candidate(
            "r1",
            "summary:r1:0",
            "summary",
            (1.0, 0.0),
            transcript_id=1,
            summary_id=SUMMARY_UUID,
            output_language=bad_language,
        )
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_winners((1.0, 0.0), [candidate])
        assert excinfo.value.code == sq.INVALID_CANDIDATE

    @pytest.mark.parametrize("bad_ordinal", [True, -1, "0", 1.0])
    def test_segment_ordinal_must_be_nonnegative_exact_int(self, bad_ordinal):
        candidate = _candidate(
            "r1",
            "segment:1:0",
            "segment",
            (1.0, 0.0),
            transcript_id=1,
            segment_ordinal=bad_ordinal,
        )
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_winners((1.0, 0.0), [candidate])
        assert excinfo.value.code == sq.INVALID_CANDIDATE

    @pytest.mark.parametrize("field", ["start_ms", "end_ms"])
    @pytest.mark.parametrize("bad_value", [True, "1", 1.0, object()])
    def test_start_end_ms_must_be_exact_ints(self, field, bad_value):
        candidate = _candidate(
            "r1",
            "segment:1:0",
            "segment",
            (1.0, 0.0),
            transcript_id=1,
            segment_ordinal=0,
            **{field: bad_value},
        )
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_winners((1.0, 0.0), [candidate])
        assert excinfo.value.code == sq.INVALID_CANDIDATE

    def test_valid_db_shapes_accepted(self):
        # The exact shapes the SearchDocument contract produces.
        recording = _candidate("r1", "recording:r1", "recording", (1.0, 0.0))
        segment = _candidate(
            "r1",
            "segment:1:0",
            "segment",
            (1.0, 0.0),
            transcript_id=1,
            segment_ordinal=0,
            start_ms=0,
            end_ms=1000,
        )
        summary = _candidate(
            "r1",
            "summary:zzz",
            "summary",
            (1.0, 0.0),
            transcript_id=1,
            summary_id=SUMMARY_UUID,
            output_language="en",
        )
        winners = sq.select_semantic_winners(
            (1.0, 0.0), [recording, segment, summary]
        )
        assert len(winners) == 1
        assert winners[0].match.doc_type == "summary"

    def test_generator_input_is_single_pass(self):
        consumed = []

        def stream():
            for candidate in (
                _candidate("r1", "summary:r1", "summary", (0.5, 0.866)),
                _candidate("r2", "summary:r2", "summary", (1.0, 0.0)),
            ):
                consumed.append(candidate)
                yield candidate

        winners = sq.select_semantic_winners((1.0, 0.0), stream())
        assert [w.match.recording_id for w in winners] == ["r2", "r1"]
        assert len(consumed) == 2


# ---------------------------------------------------------------------------
# Privacy canaries (errors never echo input/content)
# ---------------------------------------------------------------------------


class TestPrivacy:
    def test_query_canary_never_echoed(self):
        canary = "SECRET-QUERY-CANARY"
        with pytest.raises(sq.SemanticQueryInputError) as excinfo:
            sq.normalize_semantic_query(canary + "x" * 300)
        assert canary not in str(excinfo.value)

    def test_order_canary_never_echoed(self):
        canary = "SECRET-RECORDING-CANARY"
        candidates = [
            _candidate("zzz-" + canary, "summary:a", "summary", (1.0, 0.0)),
            _candidate("aaa-" + canary, "summary:b", "summary", (1.0, 0.0)),
        ]
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_winners((1.0, 0.0), candidates)
        message = str(excinfo.value)
        assert canary not in message
        assert "summary:a" not in message
        assert "summary:b" not in message

    def test_vector_values_never_echoed(self):
        canary_value = 987654.321
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.cosine_similarity((canary_value, 0.0), (0.0, 0.0))
        assert str(canary_value) not in str(excinfo.value)
        # A malformed document vector carrying the canary must not echo it.
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.cosine_similarity((1.0, 0.0), (canary_value, float("nan")))
        assert excinfo.value.code == sq.INVALID_DOCUMENT_VECTOR
        assert str(canary_value) not in str(excinfo.value)
