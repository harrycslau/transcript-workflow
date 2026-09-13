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
# Additive Library item-identity fields (6.3 groundwork): every CURRENT
# result is the whole Recording — r:<recording_id> / "recording" / None,
# mirroring the workflow.query Library item-union key contract.
# ---------------------------------------------------------------------------


class TestResultItemIdentity:
    def test_every_result_maps_to_recording_item_identity(self):
        rec1 = _seed(["item identity probe alpha"], "ident1")[0]
        rec2 = _seed(["item identity probe beta"], "ident2")[0]
        si.rebuild_index()
        payload = sq.search_recordings("identity")
        assert payload["result_count"] == 2
        for result in payload["results"]:
            assert result["item_kind"] == "recording"
            assert result["section_id"] is None
            assert result["item_key"] == f"r:{result['recording_id']}"
        assert {r["item_key"] for r in payload["results"]} == {
            f"r:{rec1.pk}",
            f"r:{rec2.pk}",
        }

    def test_summary_winner_result_keeps_recording_identity(self):
        rec, transcript, section = _seed(["identity in a plain segment"], "ident-sum")
        make_summary_version(
            rec, transcript, section, title="Identity recap",
            overview="plain body", output_language="en",
            key_points=[], action_items=[], people=[], topics=[],
        )
        si.rebuild_index()
        payload = sq.search_recordings("identity")
        assert payload["result_count"] == 1
        result = payload["results"][0]
        assert result["match"]["source"] == "summary"  # winner is the Summary…
        # …but the ITEM is still the whole Recording.
        assert result["item_kind"] == "recording"
        assert result["item_key"] == f"r:{result['recording_id']}"
        assert result["section_id"] is None

    def test_fields_are_additive_and_the_result_shape_is_exact(self):
        _built(["identity shape stability probe"], "ident-shape")
        result = sq.search_recordings("identity")["results"][0]
        assert set(result) == {
            "rank",
            "recording_id",
            "item_key",
            "item_kind",
            "section_id",
            "title",
            "match",
            "snippet",
        }
        # The historical fields keep their historical values.
        assert result["rank"] == 1
        assert result["title"] != ""
        assert result["match"]["source"] == "segment"
        assert result["snippet"]["field"] == "body_text"


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


# ---------------------------------------------------------------------------
# Recording scope (Step 5A.4.2a): filters inside the candidate set,
# before ranking, bounds and truncation — proven against ground truth.
# ---------------------------------------------------------------------------


def _scope(tags=(), *, date_from=None, timezone="Europe/Helsinki"):
    from workflow.query import ListFilters, search_scope_queryset

    filters = ListFilters(tags=list(tags), tag_match="all", date_from=date_from)
    return search_scope_queryset(filters, timezone)


def _rid(pk):
    """The raw storage-format id string the engine SELECT returns."""
    return str(pk)


class TestRecordingScope:
    def test_scope_none_is_identical_to_unscoped(self):
        from workflow.models import Recording

        _built(["the quarterly budget review"], "scope-parity")
        plain = sq.search_recordings("budget")
        explicit = sq.search_recordings("budget", scope=None)
        assert plain == explicit
        explicit2 = sq.search_recordings("budget", scope=Recording.objects.all())
        # A scope covering EVERYTHING matches the unscoped semantics.
        assert [r["recording_id"] for r in explicit2["results"]] == [
            r["recording_id"] for r in plain["results"]
        ]

    def test_scope_only_accepts_recording_querysets(self):
        from workflow.models import Recording, Transcript

        _built(["anything at all"], "scope-type")
        for bad in (
            Transcript.objects.all(),            # wrong model
            "SELECT id FROM workflow_recording",  # never SQL text
            {"ids": ["x"]},                      # never a mapping
            Recording.objects.all()[:1],         # never sliced
        ):
            with pytest.raises(sq.SearchQueryInputError) as excinfo:
                sq.search_recordings("anything", scope=bad)
            assert "scope" in str(excinfo.value)
            assert "SELECT" not in str(excinfo.value)

    def test_scope_clears_caller_order_selection_and_annotations(self):
        from workflow.models import Recording
        from workflow.query import recording_list_queryset

        made = _seed(["a scoped deterministic marker"], "scope-shape")
        si.rebuild_index()
        plain = sq.search_recordings(
            "deterministic", scope=Recording.objects.filter(pk=made[0].pk)
        )
        noisy = sq.search_recordings(
            "deterministic",
            scope=recording_list_queryset()
            .order_by("-discovered_at")
            .values("duration_seconds"),
        )
        assert plain == noisy

    def test_scope_matches_brute_force_ground_truth(self):
        from workflow.models import Recording

        keep1 = _seed(["budget marker for scope truth"], "scope-truth-1")[0]
        keep2 = _seed(["another budget marker scope truth run"], "scope-truth-2")[0]
        drop = _seed(["a budget marker excluded from scope truth"], "scope-truth-3")[0]
        tag = make_tag("Family")
        make_tag_assignment(keep1, tag)
        make_tag_assignment(keep2, tag)
        si.rebuild_index()

        kwargs = dict(max_scored_documents=100000, per_recording_candidates=500)
        unscoped = sq.search_recordings("budget", limit=200, **kwargs)
        scoped = sq.search_recordings(
            "budget", limit=200, scope=_scope(tags=["family"]), **kwargs
        )
        truth = [
            r for r in unscoped["results"] if r["recording_id"] in
            (_rid(keep1.pk), _rid(keep2.pk))
        ]
        assert [r["recording_id"] for r in scoped["results"]] == [
            r["recording_id"] for r in truth
        ]
        assert str(drop.pk) not in {r["recording_id"] for r in scoped["results"]}
        assert scoped["more_recordings_matched"] == 0
        assert scoped["truncated"] is False

    def test_flooded_corpus_never_starves_the_lower_ranked_in_scope_match(self):
        """The v2 review regression (corrected expectations):

        240 out-of-scope recordings rank HIGHER (more fold occurrences)
        than the single in-scope match. With default bounds the
        UNSCOPED run legitimately answers with the top 200, truncated=
        FALSE and an EXACT more_recordings_matched = 41 — the target
        simply falls outside that window. The scoped run sees only the
        target: rank 1, not truncated, zero more matches.
        """
        target = _seed(["alphaomega appears exactly once here"], "flood-target")[0]
        tag = make_tag("Family")
        make_tag_assignment(target, tag)
        for index in range(240):
            _seed(["alphaomega alphaomega alphaomega floods the corpus"], f"flood-{index}")
        si.rebuild_index()

        unscoped = sq.search_recordings("alphaomega", limit=sq.MAX_RESULT_LIMIT)
        ids = {r["recording_id"] for r in unscoped["results"]}
        assert unscoped["result_count"] == sq.MAX_RESULT_LIMIT
        assert unscoped["truncated"] is False
        assert unscoped["more_recordings_matched"] == 41
        assert str(target.pk) not in ids

        scoped = sq.search_recordings(
            "alphaomega", limit=sq.MAX_RESULT_LIMIT, scope=_scope(tags=["family"])
        )
        assert scoped["result_count"] == 1
        assert scoped["results"][0]["recording_id"] == _rid(target.pk)
        assert scoped["results"][0]["rank"] == 1
        assert scoped["truncated"] is False
        assert scoped["more_recordings_matched"] == 0

    def test_scope_excludes_out_of_scope_truncation_entirely(self):
        """With an explicit low global bound, the SAME flooded corpus is
        honestly truncated unscoped but NOT truncated in scope —
        out-of-scope candidates cannot inflate in-scope counts."""
        target = _seed(["gammaomega appears once in scope"], "trunc-target")[0]
        tag = make_tag("Family")
        make_tag_assignment(target, tag)
        for index in range(240):
            _seed(["gammaomega gammaomega gammaomega outside scope"], f"trunc-{index}")
        si.rebuild_index()

        kwargs = dict(limit=sq.MAX_RESULT_LIMIT, max_scored_documents=200)
        assert sq.search_recordings("gammaomega", **kwargs)["truncated"] is True

        scoped = sq.search_recordings(
            "gammaomega", scope=_scope(tags=["family"]), **kwargs
        )
        assert scoped["result_count"] == 1
        assert scoped["truncated"] is False
        assert scoped["more_recordings_matched"] == 0

    def test_scope_date_filter_uses_effective_at_semantics(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from workflow.models import Recording

        fresh = _seed(["scoped by effective date marker"], "scope-date-1")[0]
        old = _seed(["scoped by effective date marker"], "scope-date-2")[0]
        base = datetime(2026, 1, 10, 12, 0, tzinfo=ZoneInfo("Europe/Helsinki"))
        Recording.objects.filter(pk=fresh.pk).update(recorded_at=base)
        Recording.objects.filter(pk=old.pk).update(recorded_at=base - timedelta(days=9))
        si.rebuild_index()

        payload = sq.search_recordings(
            "effective",
            limit=50,
            scope=_scope(date_from=(base - timedelta(days=2)).date()),
        )
        ids = {r["recording_id"] for r in payload["results"]}
        assert ids == {_rid(fresh.pk)}

    def test_scope_params_bind_after_term_params_in_mixed_queries(self):
        """Short-term LIKE params + scope params: a tag name containing
        LIKE metacharacters must bind as data, never as a pattern."""
        from brainlib.config import tag_name_key

        target = _seed(["quarterly zeta marker with odd tag"], "scope-params-1")[0]
        decoy = _seed(["quarterly zeta marker elsewhere"], "scope-params-2")[0]
        tag = make_tag("100%_Møter")
        make_tag_assignment(target, tag)
        si.rebuild_index()
        del decoy

        payload = sq.search_recordings(
            "z zeta",  # 1-codepoint term (LIKE) AND 4-codepoint term (MATCH)
            limit=50,
            scope=_scope(tags=[tag_name_key(tag.name)]),
        )
        assert payload["result_count"] == 1
        assert payload["results"][0]["recording_id"] == _rid(target.pk)

    def test_scoped_engine_never_runs_the_full_sweep(self, monkeypatch):
        from workflow.models import Recording

        made = _seed(["sweep independence under scope"], "scope-sweep")
        si.rebuild_index()

        def forbidden(*args, **kwargs):
            raise AssertionError("the engine must never run build_status_report")

        monkeypatch.setattr(sq, "build_status_report", forbidden)
        payload = sq.search_recordings(
            "independence", scope=Recording.objects.filter(pk=made[0].pk)
        )
        assert payload["result_count"] == 1


class TestScopeCompilationFailures:
    """5A.4.2a review round 2: empty scopes answer zero results, every
    other compilation failure is the same sanitized index failure, and
    nothing internal (SQL, params, paths, the query, the underlying
    exception text) ever escapes."""

    def test_empty_scope_is_valid_and_answers_zero_results(self):
        from workflow.models import Recording

        _built(["anything at all remains outside an empty scope"], "scope-empty")
        payload = sq.search_recordings("anything", scope=Recording.objects.none())
        assert payload["result_count"] == 0
        assert payload["truncated"] is False
        assert payload["more_recordings_matched"] == 0
        assert payload["results"] == []

    def test_compiler_discovered_empty_scope_is_also_a_valid_zero_answer(self):
        """``filter(pk__in=[])`` carries NO internal empty flag; only the
        compiler (EmptyResultSet) proves it empty. It must answer the
        same normal zero result — never the sanitized index error."""
        from workflow.models import Recording

        _built(["anything at all remains outside an empty in scope"], "scope-empty-in")
        payload = sq.search_recordings(
            "anything", scope=Recording.objects.filter(pk__in=[])
        )
        assert payload["result_count"] == 0
        assert payload["truncated"] is False
        assert payload["more_recordings_matched"] == 0
        assert payload["results"] == []

    def test_filter_scope_matching_nothing_also_answers_zero(self):
        _built(["anything at all remains outside an empty filter"], "scope-empty2")
        # The approved builder path: a tag nobody carries compiles
        # normally but selects nobody — same zero answer, normal route.
        payload = sq.search_recordings("anything", scope=_scope(tags=["tag-nobody"]))
        assert payload["result_count"] == 0

    def test_scope_compiler_failure_is_sanitized(self, monkeypatch):
        import traceback as traceback_module

        from django.db.models.sql.compiler import SQLCompiler

        from workflow.models import Recording

        _built(["scope compilation sentinel budget"], "scope-boom")
        sentinel = "SENTINEL-SECRET /Users/owner/private/path SELECT secret_sql"
        real_as_sql = SQLCompiler.as_sql

        def guarded(self, *args, **kwargs):
            query = self.query
            if (
                getattr(query, "model", None) is Recording
                and tuple(getattr(query, "values_select", ())) == ("pk",)
                and not query.is_sliced
            ):
                raise ValueError(sentinel)
            return real_as_sql(self, *args, **kwargs)

        monkeypatch.setattr(SQLCompiler, "as_sql", guarded)
        with pytest.raises(sq.SearchIndexError) as excinfo:
            sq.search_recordings("budgetcanary", scope=Recording.objects.all())
        error = excinfo.value
        assert str(error) == sq._QUERY_FAILED_ERROR
        assert "SENTINEL" not in str(error)
        assert "budgetcanary" not in str(error)  # the query never rides along
        rendered = "".join(
            traceback_module.format_exception(type(error), error, error.__traceback__)
        )
        # The underlying failure text is gone: not in the message, not in
        # any rendered frame, and no chained exception is shown.
        assert "SENTINEL" not in rendered
        assert error.__cause__ is None
        assert error.__suppress_context__ is True


# ---------------------------------------------------------------------------
# Library item-scope COMPILER (Step 6.3): compile_item_scope turns the
# unsliced one-column item_key UNION of
# workflow.query.library_item_key_queryset into an immutable
# CompiledItemScope consumed by the engine (see the item-aware keyword
# search section below).
# ---------------------------------------------------------------------------


def _item_key_union(**filter_kwargs):
    from workflow.query import ListFilters, library_item_key_queryset

    return library_item_key_queryset(
        ListFilters(**filter_kwargs), "Europe/Helsinki"
    )


def _split(recording, transcript, splits, titles, start=0, end=None):
    from workflow.models import Section
    from workflow.services.segmentation import save_segmented_version

    if end is None:
        end = transcript.segments.count()
    result = save_segmented_version(
        recording.pk, transcript.pk, start, end, list(splits), list(titles)
    )
    return list(
        Section.objects.filter(segmented_version_id=result.version_id).order_by(
            "ordinal"
        )
    )


def _compiled_keys(compiled):
    """Execute the compiled subquery and return its sorted key list."""
    with connection.cursor() as cursor:
        cursor.execute(compiled.sql, list(compiled.params))
        return sorted(row[0] for row in cursor.fetchall())


class TestItemScopeCompilation:
    def test_compiles_the_library_union_to_the_exact_key_set(self):
        plain, _t, _s = make_transcribed_recording(["solo"], sha="itscope-plain")
        split, transcript, _fixed = make_transcribed_recording(
            ["a", "b", "c", "d", "e"], sha="itscope-split"
        )
        sections = _split(split, transcript, [2], ["Topic A", "Topic B"])

        compiled = sq.compile_item_scope(_item_key_union(), using="default")

        assert isinstance(compiled, sq.CompiledItemScope)
        assert compiled.using == "default"
        assert isinstance(compiled.params, tuple)
        # The split parent is REPLACED by its two Sections in the item
        # identity set — the compiled UNION answers exactly that set.
        assert _compiled_keys(compiled) == sorted(
            {f"r:{plain.pk}", f"s:{sections[0].pk}", f"s:{sections[1].pk}"}
        )

    def test_compiled_keys_match_the_live_union(self):
        make_transcribed_recording(["alpha one"], sha="itscope-live-1")
        rec, transcript, _fixed = make_transcribed_recording(
            ["a", "b", "c", "d"], sha="itscope-live-2"
        )
        _split(rec, transcript, [1, 3], ["T1", "T2", "T3"])

        union = _item_key_union()
        compiled = sq.compile_item_scope(union, using="default")
        live = sorted(row["item_key"] for row in union)  # the live UNION
        assert _compiled_keys(compiled) == live

    def test_bound_parameters_match_the_placeholders(self):
        compiled = sq.compile_item_scope(_item_key_union(), using="default")
        assert isinstance(compiled.params, tuple)
        assert compiled.sql.count("%s") == len(compiled.params)
        assert len(compiled.params) > 0  # the canonical RawSQL contract binds

    def test_compilation_executes_no_queries(self):
        """The QuerySet is captured, never executed: compile is SQL
        generation only (no DB round trip, no reads)."""
        with CaptureQueriesContext(connection) as ctx:
            sq.compile_item_scope(_item_key_union(), using="default")
        assert len(ctx.captured_queries) == 0

    def test_compile_is_deterministic_and_stores_the_alias(self):
        first = sq.compile_item_scope(_item_key_union(), using="default")
        second = sq.compile_item_scope(_item_key_union(), using="default")
        assert first.sql == second.sql
        assert first.params == second.params
        assert first.using == "default"

    def test_value_is_immutable(self):
        import dataclasses

        compiled = sq.compile_item_scope(_item_key_union(), using="default")
        for field, value in (("sql", "SELECT 1"), ("params", ()), ("using", "x")):
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(compiled, field, value)

    def test_value_is_distinct_from_the_recording_compiled_scope(self):
        compiled = sq.compile_item_scope(_item_key_union(), using="default")
        as_recording_scope = sq.CompiledScope(
            sql=compiled.sql, params=compiled.params, using=compiled.using
        )
        assert compiled != as_recording_scope
        assert not isinstance(compiled, sq.CompiledScope)

    def test_empty_union_answers_the_shared_empty_subquery(self):
        compiled = sq.compile_item_scope(_item_key_union().none(), using="default")
        assert compiled.sql == sq._EMPTY_SCOPE_SQL
        assert compiled.params == ()
        assert compiled.using == "default"
        assert _compiled_keys(compiled) == []


class TestItemScopeValidation:
    def test_rejects_non_queryset_inputs(self):
        for bad in (None, "SELECT item_key FROM x", {"item_key": 1}, 7, ["r:1"]):
            with pytest.raises(sq.SearchQueryInputError) as excinfo:
                sq.compile_item_scope(bad, using="default")
            assert str(excinfo.value) == sq._ITEM_SCOPE_TYPE_ERROR

    def test_rejects_precompiled_values(self):
        compiled = sq.compile_item_scope(_item_key_union(), using="default")
        for bad in (compiled, sq.CompiledScope("SELECT 1", (), "default")):
            with pytest.raises(sq.SearchQueryInputError) as excinfo:
                sq.compile_item_scope(bad, using="default")
            assert str(excinfo.value) == sq._ITEM_SCOPE_TYPE_ERROR

    def test_rejects_plain_recordings_and_wrong_models(self):
        from workflow.models import Recording, Transcript

        for bad in (
            Recording.objects.all(),
            Transcript.objects.all(),
            Recording.objects.values("pk"),  # not a union at all
        ):
            with pytest.raises(sq.SearchQueryInputError) as excinfo:
                sq.compile_item_scope(bad, using="default")
            assert str(excinfo.value) == sq._ITEM_SCOPE_TYPE_ERROR

    def test_rejects_a_slice_of_the_union(self):
        with pytest.raises(sq.SearchQueryInputError) as excinfo:
            sq.compile_item_scope(_item_key_union()[:2], using="default")
        assert str(excinfo.value) == sq._ITEM_SCOPE_TYPE_ERROR

    def test_rejects_a_union_without_an_item_key_column(self):
        from workflow.models import Recording

        wrong_name = Recording.objects.values("pk").union(
            Recording.objects.values("pk")
        )
        with pytest.raises(sq.SearchQueryInputError) as excinfo:
            sq.compile_item_scope(wrong_name, using="default")
        assert str(excinfo.value) == sq._ITEM_SCOPE_TYPE_ERROR

    def test_rejects_a_multi_column_union(self):
        from workflow.models import Recording

        multi = Recording.objects.values("pk", "sha256").union(
            Recording.objects.values("pk", "sha256")
        )
        with pytest.raises(sq.SearchQueryInputError) as excinfo:
            sq.compile_item_scope(multi, using="default")
        assert str(excinfo.value) == sq._ITEM_SCOPE_TYPE_ERROR

    def test_rejects_a_union_whose_second_branch_projects_another_column(self):
        """The combined query mirrors the FIRST branch; only the per-branch
        projection check catches a differently-named later branch."""
        from django.db.models import CharField, Value
        from django.db.models.functions import Concat

        from workflow.models import Recording

        keyed = (
            Recording.objects.annotate(
                item_key=Concat(Value("r:"), "pk", output_field=CharField())
            )
            .order_by()
            .values("item_key")
        )
        mixed = keyed.union(Recording.objects.order_by().values("sha256"))
        with pytest.raises(sq.SearchQueryInputError) as excinfo:
            sq.compile_item_scope(mixed, using="default")
        assert str(excinfo.value) == sq._ITEM_SCOPE_TYPE_ERROR

    def test_rejects_a_non_union_combinator(self):
        from workflow.models import Recording

        intersection = Recording.objects.values("pk").intersection(
            Recording.objects.values("pk")
        )
        with pytest.raises(sq.SearchQueryInputError) as excinfo:
            sq.compile_item_scope(intersection, using="default")
        assert str(excinfo.value) == sq._ITEM_SCOPE_TYPE_ERROR


class TestItemScopeCompilationFailures:
    """Same sanitization contract as the Recording scope: every non-empty
    compilation failure is the fixed index failure — SQL, params, paths
    and the underlying exception text never escape."""

    def test_compiler_failure_is_sanitized(self, monkeypatch):
        import traceback as traceback_module

        from django.db.models.sql.compiler import SQLCompiler

        sentinel = "SENTINEL-SECRET /Users/owner/private/path SELECT secret_sql"
        real_as_sql = SQLCompiler.as_sql

        def guarded(self, *args, **kwargs):
            if getattr(self.query, "combinator", None) == "union":
                raise ValueError(sentinel)
            return real_as_sql(self, *args, **kwargs)

        monkeypatch.setattr(SQLCompiler, "as_sql", guarded)
        with pytest.raises(sq.SearchIndexError) as excinfo:
            sq.compile_item_scope(_item_key_union(), using="default")
        error = excinfo.value
        assert str(error) == sq._QUERY_FAILED_ERROR
        assert "SENTINEL" not in str(error)
        rendered = "".join(
            traceback_module.format_exception(type(error), error, error.__traceback__)
        )
        # The underlying failure text is gone: not in the message, not in
        # any rendered frame, and no chained exception is shown.
        assert "SENTINEL" not in rendered
        assert error.__cause__ is None
        assert error.__suppress_context__ is True

    def test_unknown_alias_failure_is_sanitized(self):
        """Compiling on an unconfigured alias is the same fixed index
        failure — the alias never rides along in the message."""
        with pytest.raises(sq.SearchIndexError) as excinfo:
            sq.compile_item_scope(_item_key_union(), using="no-such-alias")
        error = excinfo.value
        assert str(error) == sq._QUERY_FAILED_ERROR
        assert "no-such-alias" not in str(error)


# ---------------------------------------------------------------------------
# ITEM-AWARE keyword search (Step 6.3): search_recordings consumes the
# compiled item scope. A valid active split layout yields EXACTLY the
# active Section items with the parent Recording SUPPRESSED (never
# parent + section duplicates); the parent metadata document, the fixed
# whole-recording Summary and cropped-out Segments derive no item;
# unsplit/crop-only/historical/malformed recordings fail closed to their
# single Recording item. Partition, per-item bound, dedup and the counts
# are item truths; ``more_recordings_matched`` is the SAME-VALUE alias
# of ``more_items_matched``. Titles stay the parent Recording's display
# title until the web slice.
# ---------------------------------------------------------------------------


def _item_search(query, filters=None, **kwargs):
    """Engine call in item mode over the (optionally filtered) normal
    Library item-key UNION, always through the compiled path."""
    compiled = sq.compile_item_scope(_item_key_union(**(filters or {})), using="default")
    return sq.search_recordings(query, compiled_item_scope=compiled, **kwargs)


def _item_corpus(sha="itmode-split"):
    """Split Recording (3 segments, crop [0,2) with one split ⇒ two
    topic Sections, a section Summary per section, a whole-recording
    Summary) plus one unsplit plain Recording. The third segment is
    cropped out of the retained range.

    Matches for the word ``keyword``: segment 0 (→ Section 1), segment
    1 (→ Section 2), the cropped segment (→ no item), both section
    Summaries, and the plain Recording's segment — EXACTLY three items.
    """
    rec, transcript, fixed = _seed(
        ["split keyword one", "split keyword two", "cropped keyword three"],
        sha=sha,
    )
    sections = _split(rec, transcript, [1], ["First topic", "Second topic"], end=2)
    section_summaries = [
        make_summary_version(
            rec, transcript, section, title=f"{label} summary",
            overview=f"keyword section {index}", output_language="en",
            key_points=[], action_items=[], people=[], topics=[],
        )
        for index, (section, label) in enumerate(zip(sections, ["First", "Second"]))
    ]
    whole = make_summary_version(
        rec, transcript, fixed, title="Whole recap",
        overview="whole parent body", output_language="en",
        key_points=[], action_items=[], people=[], topics=[],
    )
    plain, _pt, _ps = _seed(["plain keyword here"], f"{sha}-plain")
    si.rebuild_index()
    return rec, transcript, fixed, sections, section_summaries, whole, plain


class TestItemAwareKeywordSearch:
    def test_split_parent_is_replaced_by_its_section_items(self):
        from workflow.models import SearchDocument

        rec, _t, _f, sections, section_summaries, _w, plain = _item_corpus()
        payload = _item_search("keyword")

        by_key = {r["item_key"]: r for r in payload["results"]}
        assert set(by_key) == {
            f"s:{sections[0].pk}",
            f"s:{sections[1].pk}",
            f"r:{plain.pk}",
        }
        # The parent Recording is SUPPRESSED, never duplicated.
        assert f"r:{rec.pk}" not in by_key
        assert payload["result_count"] == 3
        assert payload["truncated"] is False
        assert payload["more_items_matched"] == 0
        assert payload["more_recordings_matched"] == payload["more_items_matched"]
        # Explicit item-mode flag: the matched unit IS a Library item.
        assert payload["item_mode"] is True

        first = by_key[f"s:{sections[0].pk}"]
        assert first["item_kind"] == "section"
        assert first["section_id"] == sections[0].pk
        # recording_id stays the parent provenance on a section item.
        assert first["recording_id"] == rec.pk
        # The per-item winner is the Section's own Summary (doc-type
        # priority inside the per-item bound).
        assert first["match"]["source"] == "summary"
        assert first["match"]["summary_id"] == section_summaries[0].pk
        assert first["match"]["output_language"] == "en"
        assert by_key[f"s:{sections[1].pk}"]["match"]["summary_id"] == section_summaries[1].pk
        assert by_key[f"r:{plain.pk}"]["match"]["source"] == "segment"

        # Titles stay the PARENT recording's Library display title
        # (until the web slice renders section titles).
        metadata_title = SearchDocument.objects.get(
            document_key=f"recording:{rec.pk}"
        ).title_text
        assert first["title"] == metadata_title
        assert first["title"] not in ("First topic", "First summary")
        # Deterministic comparator: the plain segment-backed item ranks
        # LAST (both Section items are summary-backed).
        assert payload["results"][-1]["match"]["source"] == "segment"
        assert payload["results"][-1]["item_key"] == f"r:{plain.pk}"

    def test_parent_metadata_and_fixed_summary_are_excluded_under_split(self):
        # The only "recap" carriers are the whole-recording Summary and
        # the parent metadata document (its display title chain): both
        # derive NO item under a canonical split.
        rec, _t, _f, _s, _ss, whole, _p = _item_corpus("itmode-fixed")
        item_payload = _item_search("recap")
        assert item_payload["result_count"] == 0
        assert item_payload["more_items_matched"] == 0

        # Legacy recording mode still finds it (unchanged contract).
        legacy = sq.search_recordings("recap")
        assert legacy["result_count"] == 1
        assert legacy["results"][0]["item_key"] == f"r:{rec.pk}"
        assert legacy["results"][0]["match"]["source"] == "summary"
        assert legacy["results"][0]["match"]["summary_id"] == whole.pk

    def test_cropped_out_segment_is_excluded_under_split(self):
        _item_corpus("itmode-crop")
        assert _item_search("cropped")["result_count"] == 0
        legacy = sq.search_recordings("cropped")
        assert legacy["result_count"] == 1
        assert legacy["results"][0]["match"]["source"] == "segment"

    def test_unsplit_recording_yields_its_recording_item(self):
        rec, transcript, fixed = _seed(["solo omega body"], "itmode-unsplit")
        make_summary_version(
            rec, transcript, fixed, title="Solo recap",
            overview="omega summary body", output_language="en",
            key_points=[], action_items=[], people=[], topics=[],
        )
        si.rebuild_index()
        legacy = sq.search_recordings("omega")
        item_payload = _item_search("omega")
        # No split anywhere: every item is the Recording and the FULL
        # payload is identical to recording mode apart from the simple
        # item-mode marker the item engine adds (and the legacy payload
        # never carries).
        assert item_payload.pop("item_mode") is True
        assert "item_mode" not in legacy
        assert item_payload == legacy
        assert legacy["result_count"] == 1
        assert legacy["results"][0]["item_key"] == f"r:{rec.pk}"
        assert legacy["results"][0]["item_kind"] == "recording"
        assert legacy["results"][0]["section_id"] is None

    def test_crop_only_layout_falls_back_to_recording_item(self):
        rec, transcript, _fixed = _seed(
            ["keep omega one", "keep omega two", "cut omega three"],
            "itmode-croponly",
        )
        _split(rec, transcript, [], [], start=1, end=3)  # crop, zero topics
        si.rebuild_index()
        for query in ("keep", "cut"):
            payload = _item_search(query)
            assert payload["result_count"] == 1, query
            # A crop-only layout is NOT a canonical split: even the
            # cropped-out segment keeps the whole-Recording item.
            assert payload["results"][0]["item_key"] == f"r:{rec.pk}"
            assert payload["results"][0]["item_kind"] == "recording"

    def test_historical_layout_falls_back_to_recording_item(self):
        rec, transcript, _fixed = _seed(
            ["old layout omega one", "old layout omega two"], "itmode-history"
        )
        sections = _split(rec, transcript, [1], ["Old A", "Old B"])
        make_summary_version(
            rec, transcript, sections[0], title="Old section recap",
            overview="omega section body", output_language="en",
            key_points=[], action_items=[], people=[], topics=[],
        )
        si.rebuild_index()
        payload = _item_search("omega")
        assert {r["item_key"] for r in payload["results"]} == {
            f"s:{sections[0].pk}",
            f"s:{sections[1].pk}",
        }

        # Supersede: the layout becomes history and the parent reappears.
        _split(rec, transcript, [], [])
        si.rebuild_index()
        payload = _item_search("omega")
        assert payload["result_count"] == 1
        assert payload["results"][0]["item_key"] == f"r:{rec.pk}"
        assert payload["results"][0]["item_kind"] == "recording"

    def test_malformed_layout_fails_closed_to_recording_item(self):
        from workflow.models import Section

        rec, transcript, _fixed = _seed(
            ["malformed omega one", "malformed omega two"], "itmode-malformed"
        )
        sections = _split(rec, transcript, [1], ["Alpha", "Beta"])
        make_summary_version(
            rec, transcript, sections[1], title="Beta recap",
            overview="omega section body", output_language="en",
            key_points=[], action_items=[], people=[], topics=[],
        )
        si.rebuild_index()
        # Corrupt stored state: an arbitrary custom title on a row
        # flagged temporary fails the canonical read (fail closed).
        Section.objects.filter(pk=sections[0].pk).update(
            title="Ad-hoc custom", title_is_temporary=True
        )
        payload = _item_search("omega")
        # ONE whole-Recording item, never section items or duplicates —
        # even while the stale section-summary document is still indexed.
        assert payload["result_count"] == 1
        assert payload["results"][0]["item_key"] == f"r:{rec.pk}"
        assert payload["results"][0]["item_kind"] == "recording"
        assert payload["results"][0]["section_id"] is None

    def test_tag_filters_are_item_exact(self):
        from workflow.models import TagAssignment

        tag = make_tag("Work")
        unsplit, ut, _uf = _seed(["quarterly delta unsplit"], "itmode-tag-u")
        make_tag_assignment(unsplit, tag, origin="manual")
        parent, pt, _pf = _seed(
            ["quarterly delta split one", "quarterly delta split two"],
            "itmode-tag-p",
        )
        left, right = _split(parent, pt, [1], ["Left", "Right"])
        make_summary_version(
            parent, pt, left, title="Left recap",
            overview="left part body", output_language="en",
            key_points=[], action_items=[], people=[], topics=[],
        )
        TagAssignment.objects.create(
            recording=parent, tag=tag, section=left, origin="manual",
            is_active=True, deactivated_by="",
        )
        si.rebuild_index()

        unfiltered = _item_search("delta")
        assert {r["item_key"] for r in unfiltered["results"]} == {
            f"r:{unsplit.pk}",
            f"s:{left.pk}",
            f"s:{right.pk}",
        }
        # The tag filter is ITEM-exact: the tagged Section and the
        # tagged unsplit Recording stay; the untagged sibling drops.
        tagged = _item_search("delta", filters={"tags": ["work"]})
        assert {r["item_key"] for r in tagged["results"]} == {
            f"r:{unsplit.pk}",
            f"s:{left.pk}",
        }
        # The tag NAME is indexed content: on the recording metadata
        # (recording-scoped) and the Section Summary's aux (section-
        # scoped) — it routes to the right ITEMS, never to the parent.
        named = _item_search("work")
        assert {r["item_key"] for r in named["results"]} == {
            f"r:{unsplit.pk}",
            f"s:{left.pk}",
        }

    def test_per_item_bound_is_per_item_not_per_recording(self):
        rec, transcript, _fixed = _seed(
            [f"kiss flood item {i} tail" for i in range(6)], "itmode-bound"
        )
        left, right = _split(rec, transcript, [3], ["Left", "Right"])
        make_summary_version(
            rec, transcript, left, title="Left recap",
            overview="kiss kiss kiss recap", output_language="en",
            key_points=[], action_items=[], people=[], topics=[],
        )
        si.rebuild_index()

        legacy = sq.search_recordings("kiss", per_recording_candidates=1)
        assert legacy["result_count"] == 1  # ONE Recording
        assert legacy["truncated"] is True

        item_payload = _item_search("kiss", per_recording_candidates=1)
        # Each SECTION gets its own candidate budget: both items stay
        # represented (per-item fairness), while the Recording mode's
        # single-partition bound keeps only one candidate for the whole
        # parent.
        assert item_payload["result_count"] == 2
        assert item_payload["truncated"] is True
        assert item_payload["more_items_matched"] == 0
        winners = {r["item_key"]: r for r in item_payload["results"]}
        assert set(winners) == {f"s:{left.pk}", f"s:{right.pk}"}
        # The kept candidate on the Summary-carrying item is the
        # Summary (doc-type priority inside the per-item bound).
        assert winners[f"s:{left.pk}"]["match"]["source"] == "summary"

    def test_global_bound_makes_the_item_count_unknown(self):
        for index in range(4):
            _seed([f"gamma omega run {index}"], f"itmode-global-{index}")
        si.rebuild_index()
        payload = _item_search("gamma omega", max_scored_documents=2)
        assert payload["result_count"] == 2
        assert payload["truncated"] is True
        assert payload["more_items_matched"] is None
        assert payload["more_recordings_matched"] is None
        # The item-mode marker is the engine's mode, independent of the
        # (now unknown) count.
        assert payload["item_mode"] is True

    def test_item_mode_flag_survives_the_fetch_bound(self):
        # The flag is a simple mode marker, NOT derived from fetched or
        # visible rows: it survives the fetch bound unchanged (and no
        # population scan of any kind backs it).
        _item_corpus("itmode-signal")
        bounded = _item_search("keyword", max_scored_documents=1)
        assert bounded["result_count"] == 1
        assert bounded["truncated"] is True
        assert bounded["more_items_matched"] is None
        assert bounded["item_mode"] is True
        assert _item_search("keyword")["item_mode"] is True
        # A legacy (recording-scope) run carries no marker at all.
        assert "item_mode" not in sq.search_recordings("keyword")

    def test_limit_truncation_counts_items_exactly(self):
        _item_corpus("itmode-limit")
        payload = _item_search("keyword", limit=2)
        assert payload["result_count"] == 2
        # Three matched ITEMS, two shown: the alias carries the same
        # item-level truth.
        assert payload["more_items_matched"] == 1
        assert payload["more_recordings_matched"] == 1
        assert payload["truncated"] is False

    def test_empty_item_scope_answers_zero(self):
        _item_corpus("itmode-empty")
        empty = sq.search_recordings(
            "keyword",
            compiled_item_scope=sq.compile_item_scope(
                _item_key_union().none(), using="default"
            ),
        )
        assert empty["result_count"] == 0
        assert empty["more_items_matched"] == 0
        assert empty["more_recordings_matched"] == 0
        # A filter that names nobody is a legitimate empty scope.
        nobody = _item_search("keyword", filters={"tags": ["tag-nobody"]})
        assert nobody["result_count"] == 0

    def test_raw_item_scope_and_compiled_path_answer_identically(self):
        _item_corpus("itmode-parity")
        union = _item_key_union()
        compiled_path = _item_search("keyword")
        raw_path = sq.search_recordings("keyword", item_scope=union)
        assert raw_path == compiled_path

    def test_item_mode_is_read_only_and_never_sweeps(self, monkeypatch):
        _item_corpus("itmode-readonly")

        def forbidden(*args, **kwargs):
            raise AssertionError("item search must not run the health sweep")

        monkeypatch.setattr(sq, "build_status_report", forbidden)
        with CaptureQueriesContext(connection) as ctx:
            payload = _item_search("keyword")
        assert payload["result_count"] == 3
        # Same contract as the recording mode: SELECT/PRAGMA only, no
        # DML/DDL anywhere in the item-mode path.
        offenders = [
            query["sql"]
            for query in ctx.captured_queries
            if not query["sql"].lstrip().upper().startswith(("SELECT", "PRAGMA"))
        ]
        assert offenders == []


class TestItemScopeEngineValidation:
    def test_engine_signature_accepts_the_item_scope_parameters(self):
        import inspect

        names = inspect.signature(sq.search_recordings).parameters
        assert {"item_scope", "compiled_item_scope"} <= set(names)

    def test_engine_rejects_a_compiled_item_scope_as_compiled_scope(self):
        _built(["item scope engine canary probe"], "itscope-engine")
        compiled = sq.compile_item_scope(_item_key_union(), using="default")
        with pytest.raises(sq.SearchQueryInputError) as excinfo:
            sq.search_recordings("canary", compiled_scope=compiled)
        assert "CompiledScope" in str(excinfo.value)

    def test_item_scope_and_compiled_item_scope_are_mutually_exclusive(self):
        _built(["item scope exclusive probe"], "itscope-exclusive")
        union = _item_key_union()
        compiled = sq.compile_item_scope(union, using="default")
        with pytest.raises(sq.SearchQueryInputError) as excinfo:
            sq.search_recordings("probe", item_scope=union, compiled_item_scope=compiled)
        assert str(excinfo.value) == sq._ITEM_SCOPE_AMBIGUOUS_ERROR

    @pytest.mark.parametrize("mode", ["scope+item", "scope+compiled_item",
                                      "compiled_scope+item", "both+item"])
    def test_recording_scope_and_item_scope_cannot_be_combined(self, mode):
        from workflow.models import Recording

        _built(["scope item conflict probe"], "itscope-conflict")
        union = _item_key_union()
        compiled_items = sq.compile_item_scope(union, using="default")
        compiled_rec = sq.compile_scope(Recording.objects.all(), using="default")
        combinations = {
            "scope+item": {"scope": Recording.objects.all(), "item_scope": union},
            "scope+compiled_item": {
                "scope": Recording.objects.all(),
                "compiled_item_scope": compiled_items,
            },
            "compiled_scope+item": {
                "compiled_scope": compiled_rec,
                "item_scope": union,
            },
            "both+item": {
                "compiled_scope": compiled_rec,
                "compiled_item_scope": compiled_items,
            },
        }
        with pytest.raises(sq.SearchQueryInputError) as excinfo:
            sq.search_recordings("probe", **combinations[mode])
        assert str(excinfo.value) == sq._SCOPE_ITEM_CONFLICT_ERROR

    def test_bad_item_scope_input_is_rejected_by_the_engine(self):
        from workflow.models import Recording

        _built(["item scope validation probe"], "itscope-validate")
        with pytest.raises(sq.SearchQueryInputError) as excinfo:
            sq.search_recordings("probe", item_scope=Recording.objects.all())
        assert str(excinfo.value) == sq._ITEM_SCOPE_TYPE_ERROR
        assert "SELECT" not in str(excinfo.value)

    def test_compiled_item_scope_wrong_type_and_alias_rejected(self):
        _built(["compiled item scope typing probe"], "itscope-typing")
        bad = sq.CompiledScope(sql="SELECT 1", params=(), using="default")
        with pytest.raises(sq.SearchQueryInputError) as excinfo:
            sq.search_recordings("probe", compiled_item_scope=bad)
        assert str(excinfo.value) == sq._COMPILED_ITEM_SCOPE_TYPE_ERROR

        compiled = sq.compile_item_scope(_item_key_union(), using="default")
        wrong_alias = sq.CompiledItemScope(
            sql=compiled.sql, params=compiled.params, using="other"
        )
        with pytest.raises(sq.SearchQueryInputError) as excinfo:
            sq.search_recordings("probe", compiled_item_scope=wrong_alias)
        assert str(excinfo.value) == sq._ITEM_SCOPE_ALIAS_ERROR
        assert "other" not in str(excinfo.value)
