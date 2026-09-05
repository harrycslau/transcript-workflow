"""Keyword-search query service (Step 5A.4.1).

Proves: plain-text literal semantics (never raw MATCH syntax), AND of
whitespace terms, the 1–2-codepoint Unicode-aware LIKE fallback (and
mixed short/long queries), deterministic rebuild-stable selection
(document-key order, per-recording bound, truthful truncation, exact or
null ``more_recordings_matched``), per-Recording deduplication with
deterministic ranking, plain-text snippets with correct offsets, match
provenance, the FULL-sweep-vs-engine health separation, and absolute
read-only behavior.
"""

from __future__ import annotations

import sqlite3

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from factories import (
    make_summary_version,
    make_tag,
    make_tag_assignment,
    make_transcribed_recording,
)
from workflow.services import search_index as si
from workflow.services import search_query as sq

pytestmark = pytest.mark.django_db


def _seed(texts, sha, **kwargs):
    return make_transcribed_recording(texts, sha=sha, **kwargs)


def _built(texts, sha, **kwargs):
    made = _seed(texts, sha, **kwargs)
    si.rebuild_index()
    return made


# ---------------------------------------------------------------------------
# Query normalization and input bounds
# ---------------------------------------------------------------------------


class TestQueryInput:
    def test_normalize_is_nfc_and_stripped(self):
        assert sq.normalize_query("  a\u0308iti \n") == "äiti"

    def test_empty_or_whitespace_raises_without_echo(self):
        for raw in (None, "", "   \t\n "):
            with pytest.raises(sq.SearchQueryInputError) as excinfo:
                sq.normalize_query(raw)
            assert "empty" in str(excinfo.value)

    def test_too_long_raises_without_echoing_query(self):
        sentinel = "SUPER-PRIVATE-" + "x" * 300
        with pytest.raises(sq.SearchQueryInputError) as excinfo:
            sq.normalize_query(sentinel)
        assert "SUPER-PRIVATE" not in str(excinfo.value)
        assert "256" in str(excinfo.value)

    def test_too_many_terms_raises(self):
        with pytest.raises(sq.SearchQueryInputError) as excinfo:
            sq.search_recordings(" ".join(f"term{i}" for i in range(sq.MAX_QUERY_TERMS + 1)))
        assert "words" in str(excinfo.value)

    @pytest.mark.parametrize("bad_limit", [0, -1, sq.MAX_RESULT_LIMIT + 1, True, "5"])
    def test_limit_bounds(self, bad_limit):
        _built(["anything"], "lim")
        with pytest.raises(sq.SearchQueryInputError):
            sq.search_recordings("anything", limit=bad_limit)

    def test_missing_engine_bounds_args_rejected(self):
        _built(["anything"], "lim2")
        with pytest.raises(sq.SearchQueryInputError):
            sq.search_recordings("anything", max_scored_documents=0)


# ---------------------------------------------------------------------------
# Matching semantics (literal, Unicode, AND, LIKE fallback)
# ---------------------------------------------------------------------------


class TestMatching:
    def test_basic_body_match(self):
        _seed(["the quarterly budget review happened"], "basic")
        si.rebuild_index()
        payload = sq.search_recordings("budget")
        assert payload["result_count"] == 1
        assert payload["results"][0]["match"]["source"] == "segment"

    def test_no_match_is_clean(self):
        _built(["totally unrelated text here"], "nomatch")
        payload = sq.search_recordings("elephant")
        assert payload["result_count"] == 0
        assert payload["truncated"] is False
        assert payload["more_recordings_matched"] == 0

    def test_cjk_three_codepoint_match_and_two_codepoint_like(self):
        _seed(["中文录音摘要"], "cjk")
        si.rebuild_index()
        assert sq.search_recordings("普通話")["result_count"] == 0
        assert sq.search_recordings("录音")["result_count"] == 1  # 2 cp -> LIKE
        assert sq.search_recordings("录音摘")["result_count"] == 1  # 3 cp -> MATCH

    def test_finnish_case_insensitive_but_diacritics_never_folded(self):
        _seed(["KÄYTÖSSÄ marco todetti"], "fi1")
        si.rebuild_index()
        assert sq.search_recordings("käytössä")["result_count"] == 1
        assert sq.search_recordings("Käyt")["result_count"] == 1  # substring hit
        assert sq.search_recordings("kaytossa")["result_count"] == 0  # no folding

    def test_short_latin_and_diaeresis_queries_use_like(self):
        _seed(["Äiti soi pianoa"], "fi2")
        si.rebuild_index()
        assert sq.search_recordings("ä")["result_count"] == 1
        assert sq.search_recordings("ÄI")["result_count"] == 1

    def test_percent_is_literal_in_both_paths(self):
        from workflow.models import Recording

        _seed(["progress is 100% done today"], "pct")
        _seed(["plain segment text here"], "pct2")
        si.rebuild_index()
        pct_pk = Recording.objects.get(sha256="pct").pk
        assert [r["recording_id"] for r in sq.search_recordings("100%")["results"]] == [pct_pk]
        # a bare "%" is one codepoint (escaped LIKE wildcard), not "match all"
        assert len(sq.search_recordings("%")["results"]) == 1

    def test_underscore_and_backslash_are_escaped(self):
        _seed(["under_score segment text"], "und")
        _seed(["underXscore segment text"], "und2")
        _seed(["back\\slash segment"], "bs")
        si.rebuild_index()
        hits = sq.search_recordings("_")["results"]
        assert len(hits) == 1
        assert len(sq.search_recordings("\\")["results"]) == 1

    def test_quotes_and_fts_operators_are_literal_text(self):
        _seed(['she said "hello" and NEAR(x y) plus star* here'], "ops")
        si.rebuild_index()
        assert sq.search_recordings('said "hello"')["result_count"] == 1
        assert sq.search_recordings("NEAR(x y)")["result_count"] == 1
        assert sq.search_recordings("AND")["result_count"] == 1
        assert sq.search_recordings("star*")["result_count"] == 1

    def test_terms_combine_with_and_across_columns(self):
        rec, transcript, section = _seed(["allocations were reviewed today"], "and1")
        make_summary_version(rec, transcript, section, title="Budget review",
                             overview="allocations noted", output_language="en",
                             key_points=[], action_items=[], people=[], topics=[])
        _seed(["budget spent elsewhere"], "and2")
        si.rebuild_index()
        payload = sq.search_recordings("budget allocations")
        ids = [r["recording_id"] for r in payload["results"]]
        assert ids == [rec.pk]  # both terms inside ONE document, different fields

    def test_mixed_short_and_long_terms(self):
        _seed(["Äiti budget meeting notes"], "mix1")
        _seed(["budget only segment here"], "mix2")
        si.rebuild_index()
        payload = sq.search_recordings("ä budget")
        assert len(payload["results"]) == 1

    def test_astral_trigram_and_fold_agree(self):
        _seed(["a😀b😀c with emoji markers"], "emo")
        si.rebuild_index()
        assert sq.search_recordings("😀b😀")["result_count"] == 1


# ---------------------------------------------------------------------------
# Bounded, deterministic, rebuild-stable candidate selection
# ---------------------------------------------------------------------------


class TestCandidateSelection:
    def test_selection_orders_by_document_key_not_rowid(self):
        for i in range(3):
            _seed([f"selection probe text {i}"], f"sel{i}")
        si.rebuild_index()
        sql, _params, _fold = sq._build_selection(["selection"])
        assert "ORDER BY document_key" in sql
        assert "ROW_NUMBER() OVER" in sql
        assert "ORDER BY rowid" not in sql
        assert "rowid LIMIT" not in sql
        # Bound overflow is DETECTED (exact window counts) and the
        # per-recording bound keeps higher-priority classes first.
        assert "COUNT(*) OVER () AS total_matches" in sql
        assert "COUNT(*) OVER (PARTITION BY d.recording_id)" in sql
        assert "ORDER BY CASE d.doc_type" in sql
        rows, _truncated, _complete = sq._select_candidates(
            ["selection"], using="default",
            max_scored_documents=99, per_recording_candidates=99,
        )
        keys = [row["document_key"] for row in rows]
        assert keys == sorted(keys)
        from workflow.models import SearchDocument

        expected = list(
            SearchDocument.objects.filter(document_key__startswith="segment:")
            .order_by("document_key")
            .values_list("document_key", flat=True)
        )
        assert keys == expected

    def test_per_recording_bound_prevents_starvation(self):
        many = [f"starvation probe number {i} continues" for i in range(6)]
        _seed(many, "many")
        _seed(["starvation probe only once"], "one")
        _seed(["starvation probe appears here"], "two")
        si.rebuild_index()
        payload = sq.search_recordings(
            "starvation", max_scored_documents=100, per_recording_candidates=2
        )
        assert payload["result_count"] == 3  # the flood cannot evict the others
        # The flooded recording EXCEEDED its per-recording bound: the
        # overflow is reported, never silently discarded. Every matched
        # Recording still survives, so the count stays exact.
        assert payload["truncated"] is True
        assert payload["more_recordings_matched"] == 0

    def test_global_cap_is_truthful_and_more_is_unknown_when_capped(self):
        _seed([f"flooding needle {i} tail" for i in range(8)], "flood")
        _seed(["a single needle instance"], "single")
        si.rebuild_index()
        payload = sq.search_recordings(
            "needle", max_scored_documents=4, per_recording_candidates=100
        )
        assert payload["truncated"] is True
        assert payload["more_recordings_matched"] is None
        # Below the cap the same query is complete and exact.
        full = sq.search_recordings("needle")
        assert full["truncated"] is False
        assert full["result_count"] == 2

    def test_more_recordings_matched_is_exact_when_not_truncated(self):
        for i in range(3):
            _seed([f"exact counting needle {i}"], f"ex{i}")
        si.rebuild_index()
        payload = sq.search_recordings("needle", limit=2)
        assert payload["result_count"] == 2
        assert payload["more_recordings_matched"] == 1

    def test_selection_unit_per_recording_bound(self):
        _seed([f"bound probe {i} end" for i in range(4)], "bA")
        _seed(["bound probe appears once"], "bB")
        si.rebuild_index()
        rows, truncated, complete = sq._select_candidates(
            ["bound"], using="default", max_scored_documents=99,
            per_recording_candidates=1,
        )
        by_rec: dict[str, list] = {}
        for row in rows:
            by_rec.setdefault(row["recording_id"], []).append(row["document_key"])
        assert {len(v) for v in by_rec.values()} == {1}
        # 4 candidates exceeded the bound of 1: truncated is TRUE, but
        # the bounded fetch was not cut, so every matched Recording is
        # still present (recordings complete).
        assert truncated is True
        assert complete is True

    def test_per_recording_bound_cannot_evict_winning_summary(self):
        # REGRESSION (review finding 1): segments sort BEFORE the
        # summary by document_key ("segment:..." < "summary:..."). With
        # more matching segments than the per-recording bound, the
        # Summary that the comparator must pick may NOT be silently
        # discarded by a document-key-only window order — it is kept
        # first by the doc-type priority in the window — and the
        # overflow is reported via `truncated` instead of hidden.
        rec, transcript, section = _seed(
            [f"kiss segment number {i} continues" for i in range(4)], "prcap"
        )
        make_summary_version(
            rec, transcript, section, title="Quiet title",
            overview="kiss kiss kiss and kiss again", output_language="en",
            key_points=[], action_items=[], people=[], topics=[],
        )
        si.rebuild_index()
        capped = sq.search_recordings(
            "kiss", max_scored_documents=99, per_recording_candidates=2
        )
        assert capped["result_count"] == 1
        assert capped["results"][0]["match"]["source"] == "summary"
        assert capped["results"][0]["match"]["occurrences"] == 4
        assert capped["truncated"] is True  # overflow detected, not hidden
        assert capped["more_recordings_matched"] == 0
        # The same query without the tiny bound is complete and agrees.
        full = sq.search_recordings("kiss")
        assert full["results"][0]["match"]["source"] == "summary"
        assert full["truncated"] is False

    def test_global_bound_fairness_and_honest_flags(self):
        for i in range(3):
            _seed(
                [f"fair needle copy {i} part {p}" for p in range(3)], f"fair{i}"
            )
        _seed(["fair needle single instance"], "fair3")
        si.rebuild_index()
        payload = sq.search_recordings(
            "fair", max_scored_documents=4, per_recording_candidates=1
        )
        # 10 matching candidates exceeded the global bound of 4 —
        # detected from the exact window COUNT even though the trimmed
        # per-recording candidate set itself fitted the fetch: the
        # approximation is always surfaced, never hidden.
        assert payload["truncated"] is True
        # The trim removed extra candidates of ALREADY-MATCHED
        # recordings, so the matched-Recording set is still complete
        # and the count is exactly knowable (not a guess):
        assert payload["more_recordings_matched"] == 0
        # Fairness: the per-recording bound gives every Recording one
        # slot inside the bounded fetch, so a segment flood in one
        # recording cannot starve the others out of the results.
        assert payload["result_count"] == 4

    def test_global_fetch_cut_makes_more_unknown(self):
        # When the GLOBAL bound actually cuts the (already trimmed)
        # candidate fetch, whole Recordings may be missing: the count
        # is unknowable and must be null, never a guessed low number.
        for i in range(5):
            _seed(
                [f"cutting needle copy {i} part {p}" for p in range(2)], f"cut{i}"
            )
        si.rebuild_index()
        payload = sq.search_recordings(
            "needle", max_scored_documents=3, per_recording_candidates=1
        )
        assert payload["truncated"] is True
        assert payload["result_count"] == 3
        assert payload["more_recordings_matched"] is None

    def test_results_survive_rebuild_identically(self):
        _seed(["rebuild stability content word"], "rs1")
        _seed(["another content word here"], "rs2")
        si.rebuild_index()
        first = sq.search_recordings("content")
        si.rebuild_index()
        second = sq.search_recordings("content")
        assert first == second


# ---------------------------------------------------------------------------
# Deduplication + deterministic ranking + variants
# ---------------------------------------------------------------------------


class TestRankingDedup:
    def test_one_result_per_recording_and_best_is_summary(self):
        rec, transcript, section = _seed(["kiss happened once here"], "rank1")
        make_summary_version(
            rec, transcript, section, title="Quiet summary",
            overview="kiss kiss kiss were the words", output_language="en",
            key_points=[], action_items=[], people=[], topics=[],
        )
        si.rebuild_index()
        payload = sq.search_recordings("kiss")
        assert payload["result_count"] == 1
        match = payload["results"][0]["match"]
        assert match["source"] == "summary"
        assert match["output_language"] == "en"
        assert match["occurrences"] == 3

    def test_title_match_outranks_body_match(self):
        title_rec, t1, s1 = _seed(["unrelated body text"], "rank2")
        make_summary_version(title_rec, t1, s1, title="Budget planning notes",
                             overview="No term here.", output_language="en",
                             key_points=[], action_items=[], people=[], topics=[])
        _seed(["the budget lived only in the body"], "rank3")
        si.rebuild_index()
        payload = sq.search_recordings("budget")
        assert payload["results"][0]["recording_id"] == title_rec.pk
        assert payload["results"][0]["match"]["fields_matched"] == ["title_text"]

    def test_occurrences_then_offset_then_key_ordering(self):
        more, _, _ = _seed(["needle needle needle in here"], "occ-more")
        earlier, _, _ = _seed(["needle at the start point"], "occ-start")
        later, _, _ = _seed(["some later needle place"], "occ-later")
        si.rebuild_index()
        payload = sq.search_recordings("needle")
        ids = [r["recording_id"] for r in payload["results"]]
        assert ids[0] == more.pk
        assert ids[1] == earlier.pk
        assert ids[2] == later.pk

    def test_language_variants_dedup_to_best(self):
        rec, transcript, section = _seed(["body without the term"], "var")
        make_summary_version(rec, transcript, section, title="English minutes",
                             overview="alpha alpha mentions", output_language="en",
                             key_points=[], action_items=[], people=[], topics=[])
        make_summary_version(rec, transcript, section, title="中文摘要",
                             overview="alpha 一次", output_language="zh-Hant",
                             key_points=[], action_items=[], people=[], topics=[])
        si.rebuild_index()
        payload = sq.search_recordings("alpha")
        assert payload["result_count"] == 1
        match = payload["results"][0]["match"]
        assert match["source"] == "summary"
        assert match["output_language"] == "en"  # more fold-occurrences wins

    def test_metadata_tag_match_provenance(self):
        rec, _t, _s = _seed(["segment without tagword"], "meta")
        tag = make_tag("Healthcare")
        make_tag_assignment(rec, tag)
        si.rebuild_index()
        payload = sq.search_recordings("healthcare")
        assert payload["result_count"] == 1
        match = payload["results"][0]["match"]
        assert match["source"] == "metadata"
        assert match["fields_matched"] == ["aux_text"]
        assert "output_language" not in match
        assert payload["results"][0]["title"] != ""


# ---------------------------------------------------------------------------
# Snippets (plain text + offsets)
# ---------------------------------------------------------------------------


class TestSnippets:
    def _segment_result(self, query, sha):
        from workflow.models import Recording

        pk = Recording.objects.get(sha256=sha).pk
        payload = sq.search_recordings(query)
        results = [r for r in payload["results"] if r["recording_id"] == pk]
        assert results, f"no result for {sha}"
        return results[0]["snippet"]

    def test_snippet_structure_and_offsets(self):
        _built(["The quick brown fox jumps over the lazy dog near a park"], "sn1")
        snippet = self._segment_result("fox", "sn1")
        assert snippet["field"] == "body_text"
        assert "<" not in snippet["text"] and ">" not in snippet["text"]
        start = snippet["matches"][0]["start"]
        end = snippet["matches"][0]["end"]
        assert snippet["text"][start:end] == "fox"

    def test_folded_offsets_point_at_original_case(self):
        _built(["KÄYTTÖ version disclosed"], "sn2")
        snippet = self._segment_result("käyttö", "sn2")
        m = snippet["matches"][0]
        assert snippet["text"][m["start"] : m["end"]] == "KÄYTTÖ"

    def test_bounds_and_ellipsis(self):
        padding = "x" * 400
        _built([f"{padding} needle {padding}"], "sn3")
        si.rebuild_index()
        snippet = self._segment_result("needle", "sn3")
        assert len(snippet["text"]) <= sq.SNIPPET_MAX_CODEPOINTS + 2
        assert snippet["ellipsis_before"] is True
        assert snippet["ellipsis_after"] is True
        assert snippet["text"].startswith(sq.ELLIPSIS)
        assert snippet["text"].endswith(sq.ELLIPSIS)
        m = snippet["matches"][0]
        assert snippet["text"][m["start"] : m["end"]] == "needle"

    def test_multiple_term_ranges_highlighted(self):
        _built(["alpha and beta and alpha again inside one window"], "sn4")
        snippet = self._segment_result("alpha beta", "sn4")
        assert len(snippet["matches"]) >= 3
        for m in snippet["matches"]:
            assert m["start"] < m["end"]
        # matches are sorted and non-overlapping
        pairs = [(m["start"], m["end"]) for m in snippet["matches"]]
        assert pairs == sorted(pairs)
        assert all(pairs[i][1] <= pairs[i + 1][0] for i in range(len(pairs) - 1))

    def test_title_field_snippet_preferred(self):
        rec, t, s = _seed(["body says nothing"], "sn5")
        make_summary_version(rec, t, s, title="Zebra photos", overview="z",
                             output_language="en", key_points=[], action_items=[],
                             people=[], topics=[])
        si.rebuild_index()
        payload = sq.search_recordings("zebra")
        snippet = payload["results"][0]["snippet"]
        assert snippet["field"] == "title_text"

    def test_cjk_and_emoji_offsets(self):
        _seed(["粤语同普通話嘅分別好重要"], "sn6")
        _seed(["mark a😀b😀c tail here"], "sn7")
        si.rebuild_index()
        snippet = self._segment_result("普通話", "sn6")
        m = snippet["matches"][0]
        assert snippet["text"][m["start"] : m["end"]] == "普通話"
        snippet = self._segment_result("😀b😀", "sn7")
        m = snippet["matches"][0]
        assert snippet["text"][m["start"] : m["end"]] == "😀b😀"

    def test_overlapping_occurrences_merge(self):
        _built(["the aaa value here now"], "sn8")
        snippet = self._segment_result("aa", "sn8")
        assert len(snippet["matches"]) == 1

    def test_divergent_match_kept_unhighlighted(self):
        row = {
            "title_text": "", "body_text": "needle in a haystack", "aux_text": "",
            "doc_type": "segment", "document_key": "segment:x:0",
        }
        monkey_fold = sq.fold_text
        try:
            sq.fold_text = lambda value: "never-never"
            score = sq._score_document(row, ["needle"])
        finally:
            sq.fold_text = monkey_fold
        assert score["divergent"] is True
        assert score["occurrences"] == 0
        assert sq._build_snippet(row, score) is None

    # -- casefold EXPANSION endpoint mapping (review finding 2) --------

    def test_ligature_partial_match_yields_whole_source_character(self):
        # "ﬃ" casefolds to three codepoints; a "ff" query match ends
        # PARTLY inside the expanded character. The old endpoint
        # mapping (spans[end]) produced a ZERO-LENGTH range there.
        _built(["ligature preﬃx marker here"], "exp1")
        snippet = self._segment_result("ff", "exp1")
        m = snippet["matches"][0]
        assert m["start"] < m["end"]
        assert snippet["text"][m["start"] : m["end"]] == "ﬃ"

    def test_sharp_s_partial_and_full_expansion(self):
        _built(["die straße war sauber"], "exp2")
        # "aß" folds to "ass"; the match's LAST folded codepoint is
        # ß's second expanded codepoint — the range must still cover
        # the whole ß.
        snippet = self._segment_result("aß", "exp2")
        m = snippet["matches"][0]
        assert snippet["text"][m["start"] : m["end"]] == "aß"
        # A full-word match keeps working exactly as before.
        snippet = self._segment_result("straße", "exp2")
        m = snippet["matches"][0]
        assert snippet["text"][m["start"] : m["end"]] == "straße"

    def test_all_ranges_are_nonempty_in_bounds_source_slices(self):
        _built(["Grüße ﬂiegen ﬃx öﬀnen Straße"], "exp3")
        for query in ("ff", "fl", "ss", "straße", "grüße", "ﬃ", "ﬀ"):
            snippet = self._segment_result(query, "exp3")
            text = snippet["text"]
            pairs = [(m["start"], m["end"]) for m in snippet["matches"]]
            assert pairs, f"no ranges for {query!r}"
            assert pairs == sorted(pairs)
            for start, end in pairs:
                assert 0 <= start < end <= len(text)  # non-empty, in bounds
                slice_text = text[start:end]
                assert slice_text
                # the highlighted slice really contains the folded match
                needle = sq.fold_text(query)
                assert needle in sq.fold_text(slice_text)

    # -- hard snippet window bound (review finding 3) -------------------

    def test_long_term_window_is_hard_bounded_mid_text(self):
        term = "q" * sq.MAX_QUERY_CODEPOINTS
        _built(["a" * 500 + " " + term + " " + "b" * 500], "hb-long")
        snippet = self._segment_result(term, "hb-long")
        content_length = (
            len(snippet["text"])
            - int(snippet["ellipsis_before"])
            - int(snippet["ellipsis_after"])
        )
        assert content_length <= sq.SNIPPET_MAX_CODEPOINTS
        assert len(snippet["text"]) <= sq.SNIPPET_MAX_CODEPOINTS + 2
        m = snippet["matches"][0]
        assert 0 <= m["start"] < m["end"] <= len(snippet["text"])
        assert snippet["text"][m["start"] : m["end"]] == term  # fully visible

    def test_long_term_window_shifts_bounded_at_text_tail(self):
        term = "z" * sq.MAX_QUERY_CODEPOINTS
        _built(["c" * 600 + " " + term], "hb-tail")
        snippet = self._segment_result(term, "hb-tail")
        assert snippet["ellipsis_before"] is True
        assert snippet["ellipsis_after"] is False
        assert len(snippet["text"]) - 1 <= sq.SNIPPET_MAX_CODEPOINTS
        m = snippet["matches"][0]
        assert m["end"] == len(snippet["text"])
        assert snippet["text"][m["start"] : m["end"]] == term

    # -- window anchored on the FIRST INDIVIDUAL match (review round 2) --

    def _assert_snippet_invariants(self, snippet):
        text = snippet["text"]
        content_length = (
            len(text)
            - int(snippet["ellipsis_before"])
            - int(snippet["ellipsis_after"])
        )
        assert content_length <= sq.SNIPPET_MAX_CODEPOINTS
        assert len(text) <= sq.SNIPPET_MAX_CODEPOINTS + 2
        pairs = [(m["start"], m["end"]) for m in snippet["matches"]]
        assert pairs, "a matching snippet must carry at least one range"
        # every range: non-empty, fully inside the returned text
        assert all(0 <= s < e <= len(text) for s, e in pairs)
        # ordered and non-overlapping (merged for display only)
        assert all(pairs[i][1] <= pairs[i + 1][0] for i in range(len(pairs) - 1))

    def test_repeated_text_never_exceeds_snippet_cap(self):
        # REGRESSION: 1000 repeated characters matched by one
        # codepoint chain 1000 adjacent ranges; the window was once
        # anchored on the GLOBAL merge and the defensive branch
        # emitted the whole 1000-codepoint range. (Repeated "ü" so no
        # Recording-metadata text can compete for the winner.)
        _built(["ü" * 1000], "rep1000")
        snippet = self._segment_result("ü", "rep1000")
        self._assert_snippet_invariants(snippet)
        # Anchor at 0 => window [0, 320) with a trailing ellipsis; the
        # adjacent single-character highlights merge into one display
        # range over the WHOLE capped window.
        assert snippet["ellipsis_before"] is False
        assert snippet["ellipsis_after"] is True
        assert len(snippet["text"]) == sq.SNIPPET_MAX_CODEPOINTS + 1
        assert snippet["matches"] == [
            {"start": 0, "end": sq.SNIPPET_MAX_CODEPOINTS}
        ]

    def test_chained_overlapping_terms_hard_bounded(self):
        # Two terms whose occurrences tile into one another produce an
        # over-cap chain under global merging; the displayed snippet
        # stays hard-bounded around the FIRST individual match.
        _built(["ab" * 400], "chaincap")
        snippet = self._segment_result("ab ba", "chaincap")
        self._assert_snippet_invariants(snippet)
        assert snippet["text"] != "ab" * 400  # never the whole chain

    def test_ranges_clip_at_window_boundary(self):
        # A match crossing the selected window boundary is CLIPPED
        # (stays non-empty and fully inside the returned text); only
        # fully-before/after matches drop out.
        _built(["needle" + "y" * 312 + "needle"], "edgeclip")
        snippet = self._segment_result("needle", "edgeclip")
        self._assert_snippet_invariants(snippet)
        assert snippet["ellipsis_before"] is False
        assert snippet["ellipsis_after"] is True
        assert len(snippet["text"]) == sq.SNIPPET_MAX_CODEPOINTS + 1
        pairs = [(m["start"], m["end"]) for m in snippet["matches"]]
        # full first match + the (318..324) match clipped at 320
        assert pairs == [(0, 6), (318, sq.SNIPPET_MAX_CODEPOINTS)]
        assert snippet["text"][318:320] == "ne"

    def test_anchor_longer_than_cap_never_oversizes(self):
        # Defensive-branch proof at the engine boundary: an INDIVIDUAL
        # mapped match of 400 codepoints (reachable only through an
        # expanded pathological query) must produce the capped window
        # from the match start, never an oversized snippet and never a
        # dropped highlight.
        row = {
            "title_text": "", "body_text": "s" * 400, "aux_text": "",
            "doc_type": "segment", "document_key": "segment:y:0",
        }
        score = sq._score_document(row, ["s" * 400])
        assert not score["divergent"]
        snippet = sq._build_snippet(row, score)
        self._assert_snippet_invariants(snippet)
        assert snippet["matches"][0]["end"] == sq.SNIPPET_MAX_CODEPOINTS
        assert snippet["ellipsis_after"] is True


# ---------------------------------------------------------------------------
# Health boundaries: FULL sweep (caller policy) vs structural engine check
# ---------------------------------------------------------------------------


class TestHealthBoundaries:
    def test_engine_never_runs_the_full_sweep(self, monkeypatch):
        _built(["engine boundary probe text"], "hb1")

        def forbidden(*args, **kwargs):
            raise AssertionError("the engine must never run build_status_report")

        monkeypatch.setattr(sq, "build_status_report", forbidden)
        payload = sq.search_recordings("boundary")
        assert payload["result_count"] == 1

    def test_preflight_full_health_healthy_and_single_call(self, monkeypatch):
        _built(["preflight probe text"], "hb2")
        calls = []
        real = si.build_status_report

        def counting(**kwargs):
            calls.append(kwargs)
            return real(**kwargs)

        monkeypatch.setattr(sq, "build_status_report", counting)
        report = sq.preflight_full_health()
        assert report["healthy"] is True
        assert len(calls) == 1

    def test_preflight_fails_stably_on_stale(self, monkeypatch):
        _seed(["stale private content XYZ"], "hb3")
        si.rebuild_index()
        from workflow.models import TranscriptSegment

        TranscriptSegment.objects.update(text="edited stale source")
        with pytest.raises(si.SearchIndexError) as excinfo:
            sq.preflight_full_health()
        message = str(excinfo.value)
        assert "stale_content" in message
        assert "brain search-index rebuild" in message
        assert "stale private content" not in message
        assert "edited stale source" not in message

    def test_engine_returns_through_staleness_by_design(self, monkeypatch):
        # The 5A.4.2 cached-health policy relies on this exact separation.
        made = _seed(["staleness separation probe"], "hb4")
        si.rebuild_index()
        from workflow.models import TranscriptSegment

        TranscriptSegment.objects.update(text="changed after indexing")
        payload = sq.search_recordings("staleness")  # old indexed text
        assert payload["result_count"] == 1

    def test_engine_detects_missing_fts_structurally(self):
        _built(["missing fts probe"], "hb5")
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_search_fts")
        with pytest.raises(si.SearchIndexError) as excinfo:
            sq.search_recordings("missing")
        assert "missing" in str(excinfo.value)
        assert "brain search-index rebuild" in str(excinfo.value)

    def test_engine_detects_broken_fts_schema(self):
        _built(["broken fts probe"], "hb6")
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_search_fts")
            cursor.execute(
                "CREATE VIRTUAL TABLE workflow_search_fts USING fts5(body_text)"
            )
        with pytest.raises(si.SearchIndexError) as excinfo:
            sq.search_recordings("broken")
        assert "broken" in str(excinfo.value)

    def test_query_time_sql_failure_maps_stably(self, monkeypatch):
        _built(["failure mapping probe"], "hb7")

        class ExplodingCursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError("simulated with sentinel-secret")

        class ExplodingConnection:
            def cursor(self):
                return ExplodingCursor()

        class Handler:
            def __getitem__(self, using):
                return ExplodingConnection()

        monkeypatch.setattr(sq, "connections", Handler())
        with pytest.raises(si.SearchIndexError) as excinfo:
            sq._select_candidates(["failure"], using="default",
                                  max_scored_documents=50,
                                  per_recording_candidates=50)
        assert "sentinel" not in str(excinfo.value)
        assert "brain search-index status" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Absolute read-only behavior
# ---------------------------------------------------------------------------


class TestReadOnly:
    def test_search_never_writes_locks_synchronizes_or_rebuilds(
        self, monkeypatch, forbid_external_effects
    ):
        _built(["read-only guarantee probe"], "ro1")

        def forbidden(*args, **kwargs):
            raise AssertionError("search must not mutate or lock anything")

        # Explicit pre-import: string targets would import workflow.services
        # .pipeline AFTER the first patch, binding the patched object as its
        # "original" module attribute forever.
        import workflow.services.pipeline as pipeline_service
        import workflow.services.pipeline_lock as pipeline_lock_service

        monkeypatch.setattr(pipeline_lock_service, "pipeline_lock", forbidden)
        monkeypatch.setattr(pipeline_service, "pipeline_lock", forbidden)
        monkeypatch.setattr(si, "rebuild_index", forbidden)
        monkeypatch.setattr(
            "workflow.services.search_sync.schedule_recording_sync", forbidden
        )

        with CaptureQueriesContext(connection) as ctx:
            sq.preflight_full_health()
            payload = sq.search_recordings("guarantee")
        assert payload["result_count"] == 1
        offenders = [
            query["sql"]
            for query in ctx.captured_queries
            if not query["sql"].lstrip().upper().startswith(("SELECT", "PRAGMA"))
        ]
        assert offenders == []

    def test_state_is_identical_after_searches(self):
        _built(["identical state probe"], "ro2")
        before = si.build_status_report()
        sq.search_recordings("identical")
        sq.search_recordings("identical", limit=1)
        assert si.build_status_report() == before


# ---------------------------------------------------------------------------
# brain_fold use-time availability (search-specific, isolated failure)
# ---------------------------------------------------------------------------


class TestFoldAvailability:
    def test_long_term_query_never_requires_the_fold_function(self, monkeypatch):
        _built(["long term queries need no fold function"], "fold1")

        def forbidden(*args, **kwargs):
            raise AssertionError("MATCH-only searches must not touch brain_fold")

        monkeypatch.setattr(sq, "ensure_fold_function", forbidden)
        assert sq.search_recordings("fold")["result_count"] == 1

    def test_short_term_fold_failure_maps_to_stable_search_error(self, monkeypatch):
        from workflow import sqlite_unicode

        _built(["a zigzagging fold probe"], "fold2")

        def boom(using="default"):
            raise RuntimeError(sqlite_unicode._FOLD_UNAVAILABLE_MESSAGE)

        monkeypatch.setattr(sq, "ensure_fold_function", boom)
        with pytest.raises(si.SearchIndexError) as excinfo:
            sq.search_recordings("z")
        assert "fold" in str(excinfo.value)
