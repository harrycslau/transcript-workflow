"""Step 6.3 item-aware NORMAL semantic search (focused tests).

Two layers, all network mocked (fake embedders replace ``embed_texts``;
no real HTTP, no oMLX, no user data):

1. ``select_semantic_item_winners`` — the pure bounded grouped
   per-LIBRARY-ITEM top-K primitive: one winner per ``item_key`` with
   the SAME ``(recording_id, document_key)`` order contract and
   comparator; non-contiguous item streams inside one Recording are
   handled (items finish when their Recording's contiguous section
   ends); the ``item_key`` boundary is strict; the legacy
   per-Recording primitive ignores the additive field.

2. ``semantic_search`` in Library-item mode — the EXACT Library scope
   and mapping (``workflow.query.library_item_key_queryset`` compiled
   through the shared ``CompiledItemScope`` + the shared canonical
   item-identity SQL): a valid active split layout yields exactly the
   Section items with the parent Recording SUPPRESSED (the parent
   metadata document and the fixed whole-recording Summary map to no
   item, never to a Section); cropped-out Segments are excluded;
   unsplit/crop-only/historical/malformed recordings fail closed to
   their single Recording item; grouping, ranks, dedup and the exact
   ``more_items_matched`` count (SAME-VALUE ``more_recordings_matched``
   alias) are item truths; the one sweep / one integrity traversal /
   one embedding request / bounded-page contracts are unchanged; the
   legacy recording mode and all item-scope misuse fail-closed paths
   stay intact.
"""

from __future__ import annotations

import pytest
from django.db import connection, transaction

from brainlib.config import EmbeddingConfig
from factories import (
    make_config,
    make_summary_version,
    make_tag,
    make_tag_assignment,
    make_transcribed_recording,
)
from workflow.models import EmbeddingDocument, Recording, Section
from workflow.services import embedding_index as ei
from workflow.services import search_index as si
from workflow.services import semantic_query as sq
from workflow.services.search_query import CompiledItemScope
from workflow.services.embedding_client import EmbeddingBatch

# The engine refuses to run inside a caller transaction; pytest-django's
# default outer transaction would otherwise trip the fixed precondition.
pytestmark = pytest.mark.django_db(transaction=True)

DIM = 4
QUERY = "quantum probe"


# ---------------------------------------------------------------------------
# helpers (fake embedders mirror test_semantic_search.py's contracts)
# ---------------------------------------------------------------------------


def emb_config(tmp_path, **overrides):
    return make_config(
        tmp_path,
        embedding=EmbeddingConfig(
            base_url="http://127.0.0.1:1/v1",
            model=overrides.pop("model", "test-embed-model"),
            api_key_env="BRAIN_TEST_LLM_API_KEY",
            timeout_seconds=120,
            batch_size=overrides.pop("batch_size", 32),
        ),
    )


def _unmatched_vector(dim=DIM):
    return [0.0] + [0.01] * (dim - 1)


def keyword_embedder(keywords, dim=DIM, *, tracker=None):
    """Text containing keyword ``k`` gets 1.0 on axis ``idx(k)``; the
    query text dispatches identically. Never the zero vector."""

    def embed(config, texts):
        if tracker is not None:
            tracker.append(list(texts))
        out = []
        for text in texts:
            vec = _unmatched_vector(dim)
            for idx, keyword in enumerate(keywords):
                if keyword in text:
                    vec[idx] = 1.0
            out.append(EmbeddingBatch(text=text, embedding=tuple(vec)))
        return out

    return embed


def rule_embedder(rules, query_vector, dim=DIM, *, tracker=None):
    """Document texts (carrying the ``brain-embedding-v1`` marker) match
    the first rule substring; the query text gets ``query_vector``."""

    def embed(config, texts):
        if tracker is not None:
            tracker.append(list(texts))
        out = []
        for text in texts:
            if text.startswith("brain-embedding-v1\n"):
                vec = _unmatched_vector(dim)
                for substring, vector in rules:
                    if substring in text:
                        vec = list(vector)
                        break
            else:
                vec = list(query_vector)
            out.append(EmbeddingBatch(text=text, embedding=tuple(vec)))
        return out

    return embed


def _seed(texts, sha, **kwargs):
    return make_transcribed_recording(texts, sha=sha, **kwargs)


def _split(recording, transcript, splits, titles, start=0, end=None):
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


def _item_key_union(**filter_kwargs):
    from workflow.query import ListFilters, library_item_key_queryset

    return library_item_key_queryset(
        ListFilters(**filter_kwargs), "Europe/Helsinki"
    )


def _item_search(query, config, embedder, filters=None, **kwargs):
    """Engine call in item mode over the (optionally filtered) normal
    Library item-key UNION, always through the compiled path."""
    compiled = sq.compile_item_scope(
        _item_key_union(**(filters or {})), using="default"
    )
    return sq.semantic_search(
        query, config=config, embedder=embedder, compiled_item_scope=compiled,
        **kwargs,
    )


def _index(config, embedder):
    si.rebuild_index()
    ei.rebuild_embedding_index(config, embedder=embedder)


def _metadata_title(recording):
    from workflow.models import SearchDocument

    return SearchDocument.objects.get(
        document_key=f"recording:{recording.pk}"
    ).title_text


# ---------------------------------------------------------------------------
# 1. Pure primitive: select_semantic_item_winners
# ---------------------------------------------------------------------------


def _cand(rid, key, item, vector, doc_type="segment"):
    return sq.SemanticCandidate(
        recording_id=rid,
        document_key=key,
        doc_type=doc_type,
        vector=tuple(vector),
        item_key=item,
    )


Q2 = (1.0, 0.0)
BEST = (1.0, 0.0)
MID = (0.7071067811865476, 0.7071067811865475)
LOW = (0.0, 1.0)


class TestItemWinnerPrimitive:
    def test_one_winner_per_item_with_noncontiguous_stream(self):
        # One Recording; item s1's documents are separated by s2's
        # document (exactly how a Section's Summary sorts after another
        # Section's Segment in document_key order). s1's best arrives
        # LAST inside the Recording section.
        stream = [
            _cand("r1", "segment:r1:0", "s:1", LOW),
            _cand("r1", "segment:r1:1", "s:2", MID),
            _cand("r1", "summary:9", "s:1", BEST, doc_type="summary"),
        ]
        winners = sq.select_semantic_item_winners(Q2, stream)
        assert [(w.match.item_key, w.match.document_key) for w in winners] == [
            ("s:1", "summary:9"),
            ("s:2", "segment:r1:1"),
        ]
        assert [w.rank for w in winners] == [1, 2]
        assert winners[0].score > winners[1].score

    def test_legacy_primitive_still_returns_one_per_recording(self):
        stream = [
            _cand("r1", "segment:r1:0", "s:1", LOW),
            _cand("r1", "segment:r1:1", "s:2", MID),
            _cand("r1", "summary:9", "s:1", BEST, doc_type="summary"),
            _cand("r2", "segment:r2:0", "r:r2", MID),
        ]
        legacy = sq.select_semantic_winners(Q2, stream)
        assert [w.match.recording_id for w in legacy] == ["r1", "r2"]
        # The additive item_key is carried on the match but ignored.
        assert legacy[0].match.item_key in {"s:1", "s:2"}

    def test_item_key_is_required_valid_str(self):
        for bad in ("", None, 5, b"s:1"):
            cand = _cand("r1", "segment:r1:0", "s:1", BEST)
            object.__setattr__(cand, "item_key", bad)
            with pytest.raises(sq.SemanticQueryError) as excinfo:
                sq.select_semantic_item_winners(Q2, iter([cand]))
            assert excinfo.value.code == sq.INVALID_CANDIDATE

    def test_order_contract_identical_to_recording_primitive(self):
        # Duplicate document key inside one recording.
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_item_winners(
                Q2,
                [
                    _cand("r1", "a", "s:1", BEST),
                    _cand("r1", "a", "s:2", LOW),
                ],
            )
        assert excinfo.value.code == sq.CANDIDATE_ORDER
        # A recording may never reappear.
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_item_winners(
                Q2,
                [
                    _cand("r1", "a", "s:1", BEST),
                    _cand("r2", "a", "s:2", LOW),
                    _cand("r1", "b", "s:3", LOW),
                ],
            )
        assert excinfo.value.code == sq.CANDIDATE_ORDER

    def test_global_top_k_and_deterministic_rank(self):
        stream = [
            _cand("r1", "a", "r:r1", BEST),
            _cand("r2", "a", "r:r2", MID),
            _cand("r3", "a", "r:r3", LOW),
        ]
        winners = sq.select_semantic_item_winners(Q2, stream, limit=2)
        assert [w.match.item_key for w in winners] == ["r:r1", "r:r2"]
        assert [w.rank for w in winners] == [1, 2]

    def test_match_is_provenance_only_and_carries_item_key(self):
        winners = sq.select_semantic_item_winners(
            Q2, [_cand("r1", "a", "s:7", BEST, doc_type="summary")]
        )
        match = winners[0].match
        assert match.item_key == "s:7"
        assert not hasattr(match, "vector")
        # Zero query norm and zero document vector fail closed.
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_item_winners((0.0, 0.0), [_cand("r1", "a", "s:1", BEST)])
        assert excinfo.value.code == sq.INVALID_QUERY_VECTOR
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.select_semantic_item_winners(Q2, [_cand("r1", "a", "s:1", (0.0, 0.0))])
        assert excinfo.value.code == sq.INVALID_DOCUMENT_VECTOR

    def test_evidence_primitive_ignores_item_mode(self):
        # Step 5D Ask surface unchanged: still document-level, capped
        # per RECORDING (not per item), regardless of item_key values.
        stream = [
            _cand("r1", f"segment:r1:{i}", f"s:{i}", BEST) for i in range(5)
        ]
        winners = sq.select_semantic_evidence(
            Q2, stream, total_limit=10, per_recording_limit=3
        )
        assert len(winners) == 3
        assert {w.match.recording_id for w in winners} == {"r1"}


# ---------------------------------------------------------------------------
# 2. Engine: split parent replaced by its Section items
# ---------------------------------------------------------------------------


class TestItemAwareSemanticSearch:
    def _corpus(self, sha="its-"):
        """Split Recording (3 segments, crop [0,2) with one split ⇒ two
        topic Sections, a Summary on Section 1 and a whole-recording
        Summary) plus one unsplit Recording with its own Summary."""
        rec, transcript, fixed = _seed(
            [
                "split itemone quantum alpha",
                "split itemtwo quantum beta",
                "cropped gamma kwisolated",
            ],
            f"{sha}split",
        )
        sections = _split(rec, transcript, [1], ["First topic", "Second topic"], end=2)
        first_summary = make_summary_version(
            rec, transcript, sections[0], title="First summary",
            overview="first section summary quantum body",
            key_points=[], action_items=[], people=[], topics=[],
        )
        whole = make_summary_version(
            rec, transcript, fixed, title="Whole recap",
            overview="whole parent body kwisolated",
            key_points=[], action_items=[], people=[], topics=[],
        )
        plain, pt, pfixed = _seed(["plain quantum solo"], f"{sha}plain")
        plain_summary = make_summary_version(
            plain, pt, pfixed, title="Plain recap",
            overview="plain summary quantum body",
            key_points=[], action_items=[], people=[], topics=[],
        )
        return rec, transcript, fixed, sections, first_summary, whole, plain, plain_summary

    def test_split_parent_is_replaced_by_its_section_items(self, tmp_path):
        config = emb_config(tmp_path)
        (
            rec, _t, _f, sections, first_summary, _whole, plain, plain_summary,
        ) = self._corpus("its-replace-")
        _index(config, keyword_embedder(["quantum"]))
        payload = _item_search(QUERY, config, keyword_embedder(["quantum"]))

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

        first = by_key[f"s:{sections[0].pk}"]
        assert first["item_kind"] == "section"
        assert first["section_id"] == sections[0].pk
        # recording_id stays the parent provenance on a section item.
        assert first["recording_id"] == rec.pk
        # The per-item winner is the Section's own Summary (doc-type
        # priority beats the retained Segment).
        assert first["match"]["source"] == "summary"
        assert first["match"]["summary_id"] == first_summary.pk
        second = by_key[f"s:{sections[1].pk}"]
        assert second["item_kind"] == "section"
        assert second["match"]["source"] == "segment"
        assert second["match"]["segment_ordinal"] == 1
        assert second["recording_id"] == rec.pk

        plain_result = by_key[f"r:{plain.pk}"]
        assert plain_result["item_kind"] == "recording"
        assert plain_result["section_id"] is None
        assert plain_result["match"]["summary_id"] == plain_summary.pk

        # Titles stay the PARENT Recording's Library display title.
        assert first["title"] == _metadata_title(rec)
        assert first["title"] not in ("First topic", "First summary")
        # Deterministic descending order.
        scores = [r["score"] for r in payload["results"]]
        assert scores == sorted(scores, reverse=True)
        assert [r["rank"] for r in payload["results"]] == [1, 2, 3]

    def test_parent_metadata_and_fixed_summary_map_to_no_item(self, tmp_path):
        config = emb_config(tmp_path)
        rec, _t, _f, sections, _fs, whole, plain, _ps = self._corpus("its-fixed-")
        rules = [
            ("kwisolated", (0.99, 0.14, 0.0, 0.0)),
            ("quantum", (0.2, 0.98, 0.0, 0.0)),
        ]
        embedder = rule_embedder(rules, query_vector=(1.0, 0.0, 0.0, 0.0))
        _index(config, embedder)
        payload = _item_search(QUERY, config, embedder)

        # The fixed whole-recording Summary (best document, scoring
        # 0.99) maps to NO item: no Section item carries it and the
        # parent Recording item never appears.
        by_key = {r["item_key"]: r for r in payload["results"]}
        assert set(by_key) == {f"s:{sections[0].pk}", f"s:{sections[1].pk}", f"r:{plain.pk}"}
        assert f"r:{rec.pk}" not in by_key
        assert all(r["match"].get("summary_id") != whole.pk for r in payload["results"])

        # Legacy recording mode still surfaces the whole-recording
        # Summary as the parent's winner (unchanged contract).
        legacy = sq.semantic_search(QUERY, config=config, embedder=embedder)
        parent_rows = [r for r in legacy["results"] if r["recording_id"] == rec.pk]
        assert len(parent_rows) == 1
        assert parent_rows[0]["match"]["source"] == "summary"
        assert parent_rows[0]["match"]["summary_id"] == whole.pk
        assert parent_rows[0]["item_key"] == f"r:{rec.pk}"

    def test_cropped_out_segment_maps_to_no_item(self, tmp_path):
        config = emb_config(tmp_path)
        rules = [
            ("cropped", (0.99, 0.14, 0.0, 0.0)),
            ("quantum", (0.2, 0.98, 0.0, 0.0)),
        ]
        embedder = rule_embedder(rules, query_vector=(1.0, 0.0, 0.0, 0.0))
        rec, _t, _f, _s, _fs, _w, _p, _ps = self._corpus("its-crop-")
        _index(config, embedder)
        payload = _item_search(QUERY, config, embedder)
        # The cropped-out segment (ordinal 2, highest-scoring document)
        # is excluded: no result carries it.
        assert all(
            r["match"].get("segment_ordinal") != 2 for r in payload["results"]
        )
        # Legacy mode surfaces it as the parent's winner.
        legacy = sq.semantic_search(QUERY, config=config, embedder=embedder)
        parent_rows = [r for r in legacy["results"] if r["recording_id"] == rec.pk]
        assert parent_rows[0]["match"]["source"] == "segment"
        assert parent_rows[0]["match"]["segment_ordinal"] == 2

    def test_unsplit_recording_item_payload_matches_legacy(self, tmp_path):
        config = emb_config(tmp_path)
        rec, transcript, fixed = _seed(["solo quantum body"], "its-unsplit")
        make_summary_version(
            rec, transcript, fixed, title="Solo recap",
            overview="quantum summary body",
            key_points=[], action_items=[], people=[], topics=[],
        )
        _index(config, keyword_embedder(["quantum"]))
        embedder = keyword_embedder(["quantum"])
        legacy = sq.semantic_search(QUERY, config=config, embedder=embedder)
        item_payload = _item_search(QUERY, config, embedder)
        # No split anywhere: every item is the whole Recording and the
        # full payload is identical to recording mode apart from the
        # simple item-mode marker the item engine adds (the legacy
        # payload never carries it).
        assert item_payload.pop("item_mode") is True
        assert "item_mode" not in legacy
        assert item_payload == legacy
        assert legacy["result_count"] == 1
        assert legacy["results"][0]["item_key"] == f"r:{rec.pk}"
        assert legacy["results"][0]["item_kind"] == "recording"
        assert legacy["results"][0]["section_id"] is None

    def test_crop_only_layout_falls_back_to_recording_item(self, tmp_path):
        config = emb_config(tmp_path)
        rec, transcript, _fixed = _seed(
            ["keep quantum one", "kept quantum two", "cut quantum three"],
            "its-croponly",
        )
        _split(rec, transcript, [], [], start=1, end=3)  # crop, zero topics
        _index(config, keyword_embedder(["quantum"]))
        payload = _item_search(QUERY, config, keyword_embedder(["quantum"]))
        # A crop-only layout is NOT a canonical split: every segment
        # (the cropped-out one included) keeps the whole-Recording item.
        assert {r["item_key"] for r in payload["results"]} == {f"r:{rec.pk}"}
        assert payload["result_count"] == 1

    def test_historical_layout_falls_back_to_recording_item(self, tmp_path):
        config = emb_config(tmp_path)
        rec, transcript, _fixed = _seed(
            ["old layout quantum one", "old layout quantum two"], "its-history"
        )
        sections = _split(rec, transcript, [1], ["Old A", "Old B"])
        make_summary_version(
            rec, transcript, sections[0], title="Old section recap",
            overview="quantum section body",
            key_points=[], action_items=[], people=[], topics=[],
        )
        _index(config, keyword_embedder(["quantum"]))
        payload = _item_search(QUERY, config, keyword_embedder(["quantum"]))
        assert {r["item_key"] for r in payload["results"]} == {
            f"s:{sections[0].pk}",
            f"s:{sections[1].pk}",
        }

        # Supersede: the layout becomes history and the parent reappears.
        _split(rec, transcript, [], [])
        _index(config, keyword_embedder(["quantum"]))
        payload = _item_search(QUERY, config, keyword_embedder(["quantum"]))
        assert payload["result_count"] == 1
        assert payload["results"][0]["item_key"] == f"r:{rec.pk}"
        assert payload["results"][0]["item_kind"] == "recording"
        # The stale section-summary document is gone; the winner is a
        # segment under the parent item.
        assert payload["results"][0]["match"]["source"] == "segment"

    def test_malformed_layout_fails_closed_to_recording_item(self, tmp_path):
        config = emb_config(tmp_path)
        rec, transcript, _fixed = _seed(
            ["malformed quantum one", "malformed quantum two"], "its-malformed"
        )
        sections = _split(rec, transcript, [1], ["Alpha", "Beta"])
        make_summary_version(
            rec, transcript, sections[1], title="Beta recap",
            overview="quantum section body",
            key_points=[], action_items=[], people=[], topics=[],
        )
        _index(config, keyword_embedder(["quantum"]))
        # Corrupt stored state: an arbitrary custom title on a row
        # flagged temporary fails the canonical read (fail closed).
        Section.objects.filter(pk=sections[0].pk).update(
            title="Ad-hoc custom", title_is_temporary=True
        )
        _index(config, keyword_embedder(["quantum"]))
        payload = _item_search(QUERY, config, keyword_embedder(["quantum"]))
        # ONE whole-Recording item, never section items or duplicates.
        assert payload["result_count"] == 1
        assert payload["results"][0]["item_key"] == f"r:{rec.pk}"
        assert payload["results"][0]["item_kind"] == "recording"
        assert payload["results"][0]["section_id"] is None

    def test_library_filters_are_item_exact(self, tmp_path):
        from workflow.models import TagAssignment

        config = emb_config(tmp_path)
        tag = make_tag("Work")
        unsplit, ut, _uf = _seed(["quarterly quantum unsplit"], "its-tag-u")
        make_tag_assignment(unsplit, tag, origin="manual")
        parent, pt, _pf = _seed(
            ["quarterly quantum split one", "quarterly quantum split two"],
            "its-tag-p",
        )
        left, right = _split(parent, pt, [1], ["Left", "Right"])
        make_summary_version(
            parent, pt, left, title="Left recap",
            overview="left part body",
            key_points=[], action_items=[], people=[], topics=[],
        )
        TagAssignment.objects.create(
            recording=parent, tag=tag, section=left, origin="manual",
            is_active=True, deactivated_by="",
        )
        _index(config, keyword_embedder(["quantum"]))

        unfiltered = _item_search(QUERY, config, keyword_embedder(["quantum"]))
        assert {r["item_key"] for r in unfiltered["results"]} == {
            f"r:{unsplit.pk}",
            f"s:{left.pk}",
            f"s:{right.pk}",
        }
        # The scope is ITEM-exact: the tagged Section and the tagged
        # unsplit Recording stay; the untagged sibling Section drops.
        tagged = _item_search(
            QUERY, config, keyword_embedder(["quantum"]), filters={"tags": ["work"]}
        )
        assert {r["item_key"] for r in tagged["results"]} == {
            f"r:{unsplit.pk}",
            f"s:{left.pk}",
        }
        assert tagged["more_items_matched"] == 0
        assert tagged["more_recordings_matched"] == 0

    def test_limit_counts_items_exactly(self, tmp_path):
        config = emb_config(tmp_path)
        a = _seed(["delta high a"], "its-cap-a")[0]
        parent, pt, _pf = _seed(["delta low one", "delta mid two"], "its-cap-p")
        left, right = _split(parent, pt, [1], ["L", "R"])
        _index(
            config,
            rule_embedder(
                [
                    ("delta high a", (0.99, 0.14, 0.0, 0.0)),
                    ("delta mid two", (0.7, 0.71, 0.0, 0.0)),
                    ("delta low one", (0.3, 0.95, 0.0, 0.0)),
                ],
                query_vector=(1.0, 0.0, 0.0, 0.0),
            ),
        )
        embedder = rule_embedder(
            [
                ("delta high a", (0.99, 0.14, 0.0, 0.0)),
                ("delta mid two", (0.7, 0.71, 0.0, 0.0)),
                ("delta low one", (0.3, 0.95, 0.0, 0.0)),
            ],
            query_vector=(1.0, 0.0, 0.0, 0.0),
        )
        payload = _item_search(QUERY, config, embedder, limit=2)
        assert payload["result_count"] == 2
        keys = [r["item_key"] for r in payload["results"]]
        assert keys == [f"r:{a.pk}", f"s:{right.pk}"]
        scores = [r["score"] for r in payload["results"]]
        assert scores == sorted(scores, reverse=True)
        # THREE matched items, two shown: the item-neutral count is
        # exact and the recording alias carries the SAME value.
        assert payload["more_items_matched"] == 1
        assert payload["more_recordings_matched"] == 1
        assert payload["truncated"] is False
        # Item mode carries the flag regardless of which rows are shown.
        assert payload["item_mode"] is True

    def test_payload_contract_in_item_mode(self, tmp_path):
        config = emb_config(tmp_path)
        _seed(["contract quantum"], "its-contract")
        _index(config, keyword_embedder(["quantum"]))
        payload = _item_search(QUERY, config, keyword_embedder(["quantum"]))
        assert payload["mode"] == "semantic"
        assert payload["query"] == QUERY
        assert payload["truncated"] is False
        assert set(payload) == {
            "query",
            "mode",
            "semantic_query_version",
            "index_version",
            "embedding_generation",
            "limit",
            "results",
            "result_count",
            "truncated",
            "more_items_matched",
            "more_recordings_matched",
            "item_mode",
        }
        result = payload["results"][0]
        assert {"item_key", "item_kind", "section_id"} <= set(result)
        assert (
            payload["more_items_matched"] == payload["more_recordings_matched"]
        )
        # Item mode ALWAYS carries the plain mode flag — even over an
        # all-Recording (unsplit) population.
        assert payload["item_mode"] is True

    def test_item_mode_flag_names_items_with_only_recordings_visible(
        self, tmp_path
    ):
        # The web/CLI bug scenario at engine level: the FIRST page
        # shows ONLY Recording rows while every OMITTED match is a
        # Section — the explicit item-mode flag still declares the
        # matched unit to be Library items, so the note unit can never
        # be inferred from the visible rows.
        config = emb_config(tmp_path)
        plain = _seed(["delta high plain"], "its-sig-plain")[0]
        parent, pt, _pf = _seed(["delta low one", "delta mid two"], "its-sig-parent")
        _split(parent, pt, [1], ["L", "R"])
        rules = [
            ("delta high plain", (0.99, 0.14, 0.0, 0.0)),
            ("delta mid two", (0.7, 0.71, 0.0, 0.0)),
            ("delta low one", (0.3, 0.95, 0.0, 0.0)),
        ]
        _index(config, rule_embedder(rules, query_vector=(1.0, 0.0, 0.0, 0.0)))
        embedder = rule_embedder(rules, query_vector=(1.0, 0.0, 0.0, 0.0))
        payload = _item_search(QUERY, config, embedder, limit=1)
        # The single visible row is the unsplit Recording...
        assert [r["item_key"] for r in payload["results"]] == [f"r:{plain.pk}"]
        assert all(r["item_kind"] == "recording" for r in payload["results"])
        # ...while the two OMITTED matches are the split parent's
        # Sections. The flag is the run's MODE, not a row inspection.
        assert payload["more_items_matched"] == 2
        assert payload["item_mode"] is True


# ---------------------------------------------------------------------------
# 3. One sweep / one traversal / one embedding + empty scope + purity
# ---------------------------------------------------------------------------


class TestItemModeContracts:
    def _corpus(self, tmp_path, keywords=("alpha", "beta")):
        make_transcribed_recording(["alpha meeting discussion"], sha="itim-a")
        make_transcribed_recording(["beta irrelevant chatter"], sha="itim-b")
        config = emb_config(tmp_path)
        _index(config, keyword_embedder(list(keywords)))
        return config

    def test_one_sweep_one_traversal_one_embedding_in_item_mode(
        self, tmp_path, monkeypatch
    ):
        config = self._corpus(tmp_path)
        calls = {"health": 0, "decoded_rows": 0}
        real_status = si.build_status_report

        def spy_status(*args, **kwargs):
            calls["health"] += 1
            return real_status(*args, **kwargs)

        monkeypatch.setattr(si, "build_status_report", spy_status)
        real_decode = ei._classify_active_page

        def spy_decode(page, dimensions, using):
            calls["decoded_rows"] += len(page)
            return real_decode(page, dimensions, using)

        monkeypatch.setattr(sq, "_classify_active_page", spy_decode)
        tracker = []
        payload = _item_search(
            "alpha", config, keyword_embedder(["alpha", "beta"], tracker=tracker)
        )
        assert calls["health"] == 1  # EXACTLY one source health sweep
        assert len(tracker) == 1  # EXACTLY one embedding request
        assert tracker[0] == ["alpha"]
        from workflow.models import EmbeddingGeneration, EmbeddingGenerationState

        gen = EmbeddingGeneration.objects.get(state=EmbeddingGenerationState.ACTIVE)
        active_rows = EmbeddingDocument.objects.filter(generation=gen).count()
        assert active_rows > 0
        # ONE integrity traversal: every active vector decoded exactly
        # once, in bounded pages, even in item mode.
        assert calls["decoded_rows"] == active_rows

    def test_empty_item_scope_zero_embedding_but_integrity_validated(
        self, tmp_path
    ):
        config = self._corpus(tmp_path)
        tracker = []
        empty_scope = sq.compile_item_scope(
            _item_key_union().none(), using="default"
        )
        payload = sq.semantic_search(
            "alpha",
            config=config,
            embedder=keyword_embedder(["alpha", "beta"], tracker=tracker),
            compiled_item_scope=empty_scope,
        )
        assert tracker == []  # zero embedding requests for an empty scope
        assert payload["results"] == []
        assert payload["result_count"] == 0
        assert payload["more_items_matched"] == 0
        assert payload["more_recordings_matched"] == 0
        assert payload["truncated"] is False
        # global integrity is STILL validated: a corrupted vector
        # anywhere fails the empty-scope search closed.
        victim = EmbeddingDocument.objects.first()
        EmbeddingDocument.objects.filter(pk=victim.pk).update(vector_blob=b"\x00\x00")
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            sq.semantic_search(
                "alpha",
                config=config,
                embedder=keyword_embedder(["alpha", "beta"]),
                compiled_item_scope=empty_scope,
            )
        assert excinfo.value.code == sq.SEMANTIC_INDEX_INTEGRITY

    def test_out_of_scope_integrity_defect_fails_closed(self, tmp_path):
        from workflow.models import SearchDocument

        config = emb_config(tmp_path)
        tag = make_tag("Scoped")
        kept, _kt, _ks = make_transcribed_recording(
            ["alpha in scope"], sha="itim-scope-k"
        )
        make_tag_assignment(kept, tag, origin="manual")
        out, _ot, _os = make_transcribed_recording(
            ["beta out of scope"], sha="itim-scope-o"
        )
        _index(config, keyword_embedder(["alpha", "beta"]))
        # Corrupt the OUT-OF-SCOPE recording's vector: item membership
        # excludes its rows from scoring, but global integrity is
        # validated over the COMPLETE active generation.
        target = SearchDocument.objects.get(document_key=f"recording:{out.pk}")
        EmbeddingDocument.objects.filter(document_key=target.document_key).update(
            vector_blob=b"\x00\x00"
        )
        with pytest.raises(sq.SemanticQueryError) as excinfo:
            _item_search(
                "alpha", config, keyword_embedder(["alpha", "beta"]),
                filters={"tags": ["scoped"]},
            )
        assert excinfo.value.code == sq.SEMANTIC_INDEX_INTEGRITY

    def test_traversal_never_loads_searchable_text_in_item_mode(self, tmp_path):
        from django.test.utils import CaptureQueriesContext

        config = self._corpus(tmp_path)
        with CaptureQueriesContext(connection) as ctx:
            _item_search("alpha", config, keyword_embedder(["alpha", "beta"]))
        page_queries = [
            q["sql"]
            for q in ctx.captured_queries
            if 'AS "item_scope_key"' in q["sql"]
            and q["sql"].lstrip().upper().startswith("SELECT")
        ]
        assert page_queries  # the item-mode traversal page SELECT ran
        for sql in page_queries:
            assert "title_text" not in sql
            assert "body_text" not in sql
            assert "aux_text" not in sql

    def test_rejects_caller_transaction_zero_embedder_calls(self, tmp_path):
        config = self._corpus(tmp_path)

        def forbidden(config, texts):
            raise AssertionError("no embedder call inside a caller transaction")

        compiled = sq.compile_item_scope(_item_key_union(), using="default")
        with transaction.atomic():
            with pytest.raises(sq.SemanticQueryError) as excinfo:
                sq.semantic_search(
                    "alpha",
                    config=config,
                    embedder=forbidden,
                    compiled_item_scope=compiled,
                )
        assert excinfo.value.code == sq.SEMANTIC_IN_TRANSACTION


# ---------------------------------------------------------------------------
# 4. Item-scope usage is validated before any DB/embedding work
# ---------------------------------------------------------------------------


class TestItemScopeUsage:
    def test_scope_and_item_scope_are_mutually_exclusive(self):
        with pytest.raises(sq.SemanticQueryInputError) as excinfo:
            sq.semantic_search(
                "alpha",
                scope=Recording.objects.all(),
                item_scope=_item_key_union(),
                config=None,
                embedder=None,
            )
        assert excinfo.value.code == sq.INVALID_ITEM_SCOPE

    def test_raw_and_compiled_item_scope_are_ambiguous(self):
        compiled = CompiledItemScope(sql="SELECT NULL WHERE 1 = 0", params=(), using="default")
        with pytest.raises(sq.SemanticQueryInputError) as excinfo:
            sq.semantic_search(
                "alpha",
                item_scope=_item_key_union(),
                compiled_item_scope=compiled,
                config=None,
                embedder=None,
            )
        assert excinfo.value.code == sq.INVALID_ITEM_SCOPE

    def test_compiled_item_scope_type_and_alias_are_checked(self):
        for bad in ("SELECT NULL", 7, {"sql": "x"}, Recording.objects.all()):
            with pytest.raises(sq.SemanticQueryInputError) as excinfo:
                sq.semantic_search(
                    "alpha",
                    compiled_item_scope=bad,
                    config=None,
                    embedder=None,
                )
            assert excinfo.value.code == sq.INVALID_ITEM_SCOPE
        wrong_alias = CompiledItemScope(
            sql="SELECT NULL WHERE 1 = 0", params=(), using="nope"
        )
        with pytest.raises(sq.SemanticQueryInputError) as excinfo:
            sq.semantic_search(
                "alpha",
                compiled_item_scope=wrong_alias,
                config=None,
                embedder=None,
            )
        assert excinfo.value.code == sq.INVALID_ITEM_SCOPE
        # All misuse messages are fixed and content-free.
        assert "nope" not in str(excinfo.value)
