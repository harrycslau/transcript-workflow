"""Step 6.3 item-aware hybrid fusion tests (focused).

Proves the hybrid layer on top of the two item-mode engines (the engines
themselves are covered by ``test_search_query_service.py`` and
``test_semantic_item_search.py``):

1. PURE fusion: identity is the fused item key — sibling Sections of one
   Recording fuse/dedup/tie-break independently, every fused row keeps
   the parent ``recording_id`` plus ``item_kind``/``section_id``, and
   ONLY the canonical ``r:<UUID>`` (own parent) / positive canonical
   ASCII-decimal ``s:<id>`` spellings are honoured by the ONE strict
   boundary helper; a missing/blank/non-str/malformed/leading-zero/
   foreign-digit/wrong-parent ``item_key`` falls back to the canonical
   ``r:<recording_id>`` identity (never a crash, never a fabricated
   section id, never a trusted ad-hoc identity).
2. End-to-end item mode through ``hybrid_search``: the EXACT Library
   item scope (``workflow.query.library_item_key_queryset`` compiled
   through the shared ``CompiledItemScope``) replaces a split parent
   with its Section items in BOTH components simultaneously (parent
   SUPPRESSED, never duplicated), crop-only/malformed layouts fail
   closed to the single Recording item, and both components run at the
   fixed depth 200 with the one health sweep / one integrity traversal /
   one query embedding / empty-scope-zero-embed contract unchanged.
3. Scope compilation: the UNION is compiled EXACTLY ONCE and the SAME
   immutable value is shared by the keyword component and the semantic
   snapshot; a precompiled value is never recompiled; the QuerySet and
   precompiled paths produce IDENTICAL payloads; usage misuse fails
   closed BEFORE any health work with the stable ``invalid_item_scope``
   taxonomy.
4. Deterministic fusion + counts by item: fused order, evidence, the
   exact fused-row counts (``more_items_matched`` is the SAME-VALUE
   alias of the historical ``more_recordings_matched``) and the
   payload-level malformed fallback.
5. Legacy preservation: the recording-mode payload (top-level keys,
   component metadata keys, result-row keys) is unchanged, no item
   compilation ever happens on the legacy path, and a split parent is
   still ONE per-Recording row outside item mode.

All network mocked (fake embedders replace ``embed_texts``); no real
HTTP, no oMLX, no user data, no web/CLI/Ask wiring.
"""

from __future__ import annotations

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from brainlib.config import EmbeddingConfig
from factories import make_config, make_summary_version, make_transcribed_recording
from workflow.models import (
    EmbeddingDocument,
    EmbeddingGeneration,
    EmbeddingGenerationState,
    Recording,
    Section,
)
from workflow.services import embedding_index as ei
from workflow.services import search_fusion as sf
from workflow.services import search_index as si
from workflow.services import search_query as kw
from workflow.services import semantic_query as sq
from workflow.services.embedding_client import EmbeddingBatch

# transaction=True: the hybrid engine refuses to run while the caller is
# inside a SQLite transaction, and pytest-django's default outer
# transaction would otherwise trip that fixed precondition.
pytestmark = pytest.mark.django_db(transaction=True)

DIM = 4
QUERY = "quantum"


# ---------------------------------------------------------------------------
# helpers (mirrors test_semantic_item_search.py + test_search_fusion.py)
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


def active_generation():
    return EmbeddingGeneration.objects.get(state=EmbeddingGenerationState.ACTIVE)


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


def build_healthy(tmp_path, *, model="test-embed-model"):
    """Two transcribed recordings with distinct content plus a healthy
    active embedding generation (dim 4, keyword-dispatch embedder)."""
    make_transcribed_recording(["alpha meeting discussion"], sha="hybi-h-0")
    make_transcribed_recording(["beta irrelevant chatter"], sha="hybi-h-1")
    si.rebuild_index()
    config = emb_config(tmp_path, model=model)
    ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha", "beta"]))
    return config


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


def _item_compile(**filter_kwargs):
    return sq.compile_item_scope(_item_key_union(**filter_kwargs), using="default")


def _hybrid_item(query, config, embedder, filters=None, **kwargs):
    """Hybrid call in item mode over the (optionally filtered) normal
    Library item-key UNION, always through the precompiled path."""
    return sf.hybrid_search(
        query,
        config=config,
        embedder=embedder,
        compiled_item_scope=_item_compile(**(filters or {})),
        **kwargs,
    )


def _index(config, embedder):
    si.rebuild_index()
    ei.rebuild_embedding_index(config, embedder=embedder)


def _split_corpus(prefix):
    """Split Recording (3 segments, crop [0,2) with one split ⇒ two
    topic Sections, an active Summary on Section 1 and a whole-recording
    Summary) plus one unsplit Recording with its own Summary."""
    rec, transcript, fixed = make_transcribed_recording(
        [
            "split itemone quantum alpha",
            "split itemtwo quantum beta",
            "cropped gamma kwisolated",
        ],
        sha=f"{prefix}split",
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
    plain, pt, pfixed = make_transcribed_recording(
        ["plain quantum solo"], sha=f"{prefix}plain"
    )
    plain_summary = make_summary_version(
        plain, pt, pfixed, title="Plain recap",
        overview="plain summary quantum body",
        key_points=[], action_items=[], people=[], topics=[],
    )
    return rec, transcript, fixed, sections, first_summary, whole, plain, plain_summary


def _spies(monkeypatch):
    """Exact EXACT-count spies (mirror test_search_fusion.py): one source
    health sweep, every active vector decoded exactly once, keyword
    preflight forbidden."""
    calls = {"health": 0, "decoded_rows": 0, "preflight": 0}
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

    def forbidden(*args, **kwargs):
        calls["preflight"] += 1
        raise AssertionError("preflight_full_health must never be called")

    monkeypatch.setattr(
        "workflow.services.search_query.preflight_full_health", forbidden
    )
    return calls


# Crafted component rows (item identity is the load-bearing field).
def _kw_item(item, rid, rank, title="kw title"):
    return {
        "rank": rank,
        "recording_id": rid,
        "item_key": item,
        "item_kind": "section" if item.startswith("s:") else "recording",
        "section_id": int(item[2:]) if item.startswith("s:") and item[2:].isdigit() else None,
        "title": title,
        "match": {"source": "segment", "document_key": f"{item}:kw"},
        "snippet": {
            "field": "body_text",
            "text": "kw snippet",
            "matches": [{"start": 0, "end": 2}],
        },
    }


def _sem_item(item, rid, rank, score=0.5, title="sem title"):
    return {
        "rank": rank,
        "recording_id": rid,
        "item_key": item,
        "item_kind": "section" if item.startswith("s:") else "recording",
        "section_id": int(item[2:]) if item.startswith("s:") and item[2:].isdigit() else None,
        "title": title,
        "match": {"source": "segment", "document_key": f"{item}:sem"},
        "snippet": {"field": "body_text", "text": "sem snippet"},
        "score": score,
    }


def _payload(results, *, truncated=False, more=0):
    return {
        "query": QUERY,
        "result_count": len(results),
        "results": results,
        "truncated": truncated,
        # The historical SAME-VALUE compatibility alias the engines
        # always emit (fusion reads this key, both modes).
        "more_recordings_matched": more,
        "more_items_matched": more,
    }


# Boundary rows carry a RAW (possibly hostile) item_key and NO
# item_kind/section_id of their own: the fusion must derive everything.
def _boundary_row(rank, item=None, rid="R"):
    row = {
        "rank": rank,
        "title": f"boundary {rank}",
        "match": {"source": "segment", "document_key": f"b{rank}"},
        "snippet": {"field": "body_text", "text": "kw snippet"},
    }
    if rid is not None:
        row["recording_id"] = rid
    if item is not None:
        row["item_key"] = item
    return row


# Fixed canonical lowercase-hyphenated UUID spellings (pure-fusion
# literals; no DB identity is implied).
CANON = "2f7c1a9b-5d3e-4a6f-8b1c-0d9e8f7a6b5c"
OTHER = "9e8d7c6b-5a4f-4321-9876-5c4b3a2f1e0d"


def _patch_components(monkeypatch, kw_payload, sem_payload):
    monkeypatch.setattr(sf, "search_recordings", lambda *a, **k: kw_payload)
    monkeypatch.setattr(sf, "run_semantic_snapshot", lambda *a, **k: sem_payload)


# ---------------------------------------------------------------------------
# 1. PURE item-key fusion
# ---------------------------------------------------------------------------


class TestPureItemFusion:
    def test_sibling_sections_are_distinct_fused_rows(self):
        # Two Sections of ONE Recording: two fused rows (never one per
        # Recording), each retaining the parent recording_id and the
        # parsed section identity; the overlap merges by item key.
        kw_results = [
            _kw_item("s:10", "R", 1),
            _kw_item("s:11", "R", 2),
        ]
        sem_results = [
            _sem_item("s:11", "R", 1, score=0.9),
            _sem_item("s:12", "R", 2, score=0.4),
        ]
        fused = sf.reciprocal_rank_fusion(kw_results, sem_results)
        assert [f.item_key for f in fused] == ["s:11", "s:10", "s:12"]
        by_key = {f.item_key: f for f in fused}
        # The overlap fused into ONE row keyed by the item, NOT by the
        # shared parent recording id.
        assert by_key["s:11"].keyword_rank == 2
        assert by_key["s:11"].semantic_rank == 1
        assert by_key["s:11"].semantic_cosine == pytest.approx(0.9)
        # Parent provenance + parsed identity on every row.
        for fused_row in fused:
            assert fused_row.recording_id == "R"
        assert by_key["s:10"].item_kind == "section"
        assert by_key["s:10"].section_id == 10
        assert by_key["s:10"].semantic is None
        assert by_key["s:12"].keyword is None

    def test_tie_break_is_canonical_item_key_not_recording(self):
        kw_results = [
            _kw_item("s:2", "R", 1),
            _kw_item("s:1", "R", 100),
        ]
        sem_results = [
            _sem_item("s:2", "R", 100),
            _sem_item("s:1", "R", 1),
        ]
        fused = sf.reciprocal_rank_fusion(kw_results, sem_results)
        assert fused[0].rrf_score == fused[1].rrf_score
        # Same parent, so a recording-id tie-break could never order
        # them: the item key decides (ascending).
        assert [f.item_key for f in fused] == ["s:1", "s:2"]
        assert [f.recording_id for f in fused] == ["R", "R"]

    def test_duplicate_component_rows_dedupe_per_item(self):
        kw_results = [
            _kw_item("s:7", "R", 1),
            _kw_item("s:7", "R", 99),
        ]
        fused = sf.reciprocal_rank_fusion(kw_results, [])
        assert len(fused) == 1
        assert fused[0].keyword_rank == 1

    def test_malformed_identity_falls_back_defensively(self):
        # Missing, None, blank AND non-canonical item keys all collapse
        # to the SAME canonical parent r:<recording_id> identity (one
        # row); only a strictly canonical spelling is honoured.
        kw_results = [
            {"rank": 1, "recording_id": "R", "title": "a", "match": {}, "snippet": {}},
            {"rank": 2, "recording_id": "R", "item_key": None, "title": "b",
             "match": {}, "snippet": {}},
            {"rank": 3, "recording_id": "R", "item_key": "", "title": "c",
             "match": {}, "snippet": {}},
            _kw_item("s:zzz", "R", 4),
            _kw_item("s:5", "R", 5),
        ]
        fused = sf.reciprocal_rank_fusion(kw_results, [])
        by_key = {f.item_key: f for f in fused}
        assert set(by_key) == {"r:R", "s:5"}
        fallback = by_key["r:R"]
        assert fallback.keyword_rank == 1  # first occurrence wins
        assert fallback.item_kind == "recording"
        assert fallback.section_id is None
        assert by_key["s:5"].section_id == 5

    def test_rows_without_any_id_are_skipped(self):
        fused = sf.reciprocal_rank_fusion(
            [{"rank": 1}, _kw_item("s:1", "R", 2)], [{"recording_id": None}]
        )
        assert [f.item_key for f in fused] == ["s:1"]

    def test_recording_mode_identity_is_byte_identical(self):
        # Engine rows in recording mode carry r:<rid> — grouping and
        # the final tie-break equal the historical recording-id order.
        kw_results = [
            _kw_item("r:b", "b", 1),
            _kw_item("r:a", "a", 100),
        ]
        sem_results = [
            _sem_item("r:b", "b", 100),
            _sem_item("r:a", "a", 1),
        ]
        fused = sf.reciprocal_rank_fusion(kw_results, sem_results)
        assert fused[0].rrf_score == fused[1].rrf_score
        assert [f.recording_id for f in fused] == ["a", "b"]


# ---------------------------------------------------------------------------
# 1b. STRICT canonical boundary identity (the ONE helper is reused for
#     the grouping AND the fused identity fields)
# ---------------------------------------------------------------------------


class TestCanonicalIdentityBoundary:
    def test_leading_zero_and_zero_section_keys_fall_back(self):
        kw_results = [
            _boundary_row(1, item="s:007"),
            _boundary_row(2, item="s:0"),
            _boundary_row(3, item="s:7"),
        ]
        fused = sf.reciprocal_rank_fusion(kw_results, [])
        by_key = {f.item_key: f for f in fused}
        # Only the canonical spelling is honoured; the leading-zero and
        # zero spellings collapse to the canonical parent identity.
        assert set(by_key) == {"r:R", "s:7"}
        assert by_key["r:R"].keyword_rank == 1  # both merges, first wins
        assert by_key["r:R"].item_kind == "recording"
        assert by_key["r:R"].section_id is None
        assert by_key["s:7"].item_kind == "section"
        assert by_key["s:7"].section_id == 7

    def test_foreign_digit_section_keys_fall_back_without_crash(self):
        # Arabic-Indic, fullwidth and the superscript-two digit
        # spellings ``str.isdigit()`` accepts (and ``int()`` chokes on):
        # never honoured, never a fabricated section id, never a crash.
        kw_results = [
            _boundary_row(1, item="s:\u0661\u0662\u0663"),
            _boundary_row(2, item="s:\uff11\uff12"),
            _boundary_row(3, item="s:\u00b2"),
        ]
        fused = sf.reciprocal_rank_fusion(kw_results, [])
        assert [f.item_key for f in fused] == ["r:R"]
        assert fused[0].keyword_rank == 1
        assert fused[0].item_kind == "recording"
        assert fused[0].section_id is None

    def test_noncanonical_section_spellings_fall_back(self):
        spellings = [
            "s:+7", "s:-7", "s: 7", "s:7 ", "s:7x", "s:", "s:7:8",
            "S:7", "7", "section:7", "s:" + "9" * 25,
        ]
        kw_results = [
            _boundary_row(n, item=key) for n, key in enumerate(spellings, 1)
        ]
        fused = sf.reciprocal_rank_fusion(kw_results, [])
        assert [f.item_key for f in fused] == ["r:R"]
        assert fused[0].keyword_rank == 1
        assert fused[0].section_id is None

    def test_non_string_item_keys_fall_back(self):
        kw_results = [
            _boundary_row(1, item=7),
            _boundary_row(2, item=b"s:7"),
            _boundary_row(3, item=["s:7"]),
        ]
        fused = sf.reciprocal_rank_fusion(kw_results, [])
        assert [f.item_key for f in fused] == ["r:R"]
        assert fused[0].keyword_rank == 1

    def test_canonical_section_key_without_parent_is_honoured(self):
        # A canonical s: key needs no parent to be honoured; a
        # NON-canonical key with NO parent is skipped (never "r:None").
        fused = sf.reciprocal_rank_fusion(
            [
                _boundary_row(1, item="s:42", rid=None),
                _boundary_row(2, item="s:042", rid=None),
            ],
            [],
        )
        assert [f.item_key for f in fused] == ["s:42"]
        assert fused[0].item_kind == "section"
        assert fused[0].section_id == 42
        assert fused[0].recording_id is None

    def test_wrong_parent_recording_key_falls_back(self):
        # A canonical r:<UUID> naming ANOTHER recording is an ad-hoc
        # identity: it falls back to the row's OWN parent and can never
        # merge into — or hijack — the claimed identity.
        kw_results = [
            _boundary_row(1, item=f"r:{OTHER}", rid=CANON),  # wrong parent
            _boundary_row(2, item=f"r:{OTHER}", rid=OTHER),  # correct parent
        ]
        fused = sf.reciprocal_rank_fusion(kw_results, [])
        by_key = {f.item_key: f for f in fused}
        assert set(by_key) == {f"r:{CANON}", f"r:{OTHER}"}
        assert by_key[f"r:{CANON}"].recording_id == CANON
        assert by_key[f"r:{CANON}"].keyword_rank == 1
        assert by_key[f"r:{OTHER}"].recording_id == OTHER
        assert by_key[f"r:{OTHER}"].keyword_rank == 2
        for fused_row in fused:
            assert fused_row.item_kind == "recording"
            assert fused_row.section_id is None

    def test_noncanonical_recording_spellings_fall_back_to_parent(self):
        # Every equivalent-but-non-canonical UUID spelling is NOT
        # honoured; the canonical own-parent spelling IS (identically to
        # the fallback), and all rows merge into ONE canonical identity.
        spellings = [
            f"r:{CANON.upper()}",  # uppercase
            "r:" + CANON.replace("-", ""),  # hyphen-less
            "r:{" + CANON + "}",  # braced
            "r:urn:uuid:" + CANON,  # URN
        ]
        kw_results = [
            _boundary_row(n, item=key, rid=CANON)
            for n, key in enumerate(spellings, 1)
        ]
        kw_results.append(_boundary_row(5, item=f"r:{CANON}", rid=CANON))
        kw_results.append(_boundary_row(6, rid=CANON))  # no item_key at all
        fused = sf.reciprocal_rank_fusion(kw_results, [])
        assert [f.item_key for f in fused] == [f"r:{CANON}"]
        assert fused[0].keyword_rank == 1  # first occurrence wins
        assert fused[0].item_kind == "recording"
        assert fused[0].section_id is None
        assert fused[0].recording_id == CANON


# ---------------------------------------------------------------------------
# 2. End-to-end item mode: sibling sections, parent suppression, fail-closed
# ---------------------------------------------------------------------------


class TestHybridItemEndToEnd:
    def test_sibling_sections_fuse_and_parent_is_suppressed(self, tmp_path):
        config = emb_config(tmp_path)
        corpus = _split_corpus("hybi-e2e-")
        rec, _t, _f, sections, _fs, _w, plain, _ps = corpus
        embedder = keyword_embedder(["quantum"])
        _index(config, embedder)
        payload = _hybrid_item(QUERY, config, embedder)

        by_key = {r["item_key"]: r for r in payload["results"]}
        # A valid split yields EXACTLY the Section items + the unsplit
        # Recording item; the parent is SUPPRESSED, never duplicated.
        assert set(by_key) == {
            f"s:{sections[0].pk}",
            f"s:{sections[1].pk}",
            f"r:{plain.pk}",
        }
        assert f"r:{rec.pk}" not in by_key
        assert payload["result_count"] == 3
        # One fused row per item even though BOTH components matched
        # every item (no per-component duplication).
        for row in payload["results"]:
            assert set(row) == {
                "rank", "recording_id", "title", "match", "snippet",
                "evidence", "item_key", "item_kind", "section_id",
            }
            assert row["evidence"]["keyword_rank"] is not None
            assert row["evidence"]["semantic_rank"] is not None
            expected = (
                1.0 / (60 + row["evidence"]["keyword_rank"])
                + 1.0 / (60 + row["evidence"]["semantic_rank"])
            )
            assert row["evidence"]["rrf_score"] == pytest.approx(expected)
        first = by_key[f"s:{sections[0].pk}"]
        second = by_key[f"s:{sections[1].pk}"]
        # Siblings: distinct rows, shared parent provenance.
        assert first["recording_id"] == rec.pk
        assert second["recording_id"] == rec.pk
        assert first["item_kind"] == second["item_kind"] == "section"
        assert first["section_id"] == sections[0].pk
        assert second["section_id"] == sections[1].pk
        plain_row = by_key[f"r:{plain.pk}"]
        assert plain_row["item_kind"] == "recording"
        assert plain_row["section_id"] is None
        assert plain_row["recording_id"] == plain.pk
        # Item-level counts with the SAME-VALUE historical alias.
        assert payload["more_items_matched"] == 0
        assert payload["more_recordings_matched"] == 0
        assert payload["truncated"] is False
        # The explicit item-mode marker: the fused unit IS a Library item.
        assert payload["item_mode"] is True
        # Both components ran at depth 200.
        assert payload["depth"] == 200
        for name in ("keyword", "semantic"):
            assert payload["components"][name]["depth"] == 200
            # The REAL item-mode component engines carry their own
            # marker; the fixed metadata projection NEVER leaks it.
            assert "item_mode" not in payload["components"][name]
        # Deterministic fused order: strictly descending RRF.
        scores = [r["evidence"]["rrf_score"] for r in payload["results"]]
        assert scores == sorted(scores, reverse=True)
        assert [r["rank"] for r in payload["results"]] == [1, 2, 3]

    def test_parent_documents_never_surface_as_parent_item(self, tmp_path):
        config = emb_config(tmp_path)
        rec, _t, _f, sections, _fs, whole, plain, _ps = _split_corpus("hybi-fixed-")
        rules = [
            ("kwisolated", (0.99, 0.14, 0.0, 0.0)),
            ("quantum", (0.2, 0.98, 0.0, 0.0)),
        ]
        embedder = rule_embedder(rules, query_vector=(1.0, 0.0, 0.0, 0.0))
        _index(config, embedder)
        payload = _hybrid_item("kwisolated", config, embedder)
        # The fixed whole-recording Summary and the cropped-out Segment
        # (both matching "kwisolated" with the strongest vectors) map to
        # NO item: the parent never appears and no Section row carries
        # the whole summary. The unsplit plain Recording stays its own
        # item (it is in the corpus scope regardless of the winner).
        keys = {r["item_key"] for r in payload["results"]}
        assert keys == {
            f"s:{sections[0].pk}", f"s:{sections[1].pk}", f"r:{plain.pk}",
        }
        assert f"r:{rec.pk}" not in keys
        assert all(r["match"].get("summary_id") != whole.pk for r in payload["results"])
        assert all(
            r["match"].get("segment_ordinal") != 2 for r in payload["results"]
        )

        # The legacy recording mode is UNCHANGED: the parent surfaces
        # with the whole-recording Summary as its winner.
        legacy = sf.hybrid_search("kwisolated", config=config, embedder=embedder)
        parent_rows = [r for r in legacy["results"] if r["recording_id"] == rec.pk]
        assert len(parent_rows) == 1
        assert parent_rows[0]["match"]["source"] == "summary"
        assert parent_rows[0]["match"]["summary_id"] == whole.pk

    def test_crop_only_and_malformed_fail_closed_to_recording_item(self, tmp_path):
        config = emb_config(tmp_path)
        rec, transcript, _fixed = make_transcribed_recording(
            ["keep quantum one", "kept quantum two", "cut quantum three"],
            sha="hybi-crop-only",
        )
        _split(rec, transcript, [], [], start=1, end=3)  # crop, zero topics
        embedder = keyword_embedder(["quantum"])
        _index(config, embedder)
        payload = _hybrid_item(QUERY, config, embedder)
        # A crop-only layout is NOT a canonical split: ONE Recording item.
        assert {r["item_key"] for r in payload["results"]} == {f"r:{rec.pk}"}
        assert payload["results"][0]["item_kind"] == "recording"
        assert payload["results"][0]["section_id"] is None
        # Still item mode: the Library-item unit INCLUDES the Recording
        # items, so the mode marker stays true.
        assert payload["item_mode"] is True

        # Corrupt stored state on a SEPARATE split recording: a custom
        # title on a temporary-flagged row fails the canonical read —
        # it fails closed to its single Recording item, never section
        # items or duplicates, and never disturbs the crop-only item.
        bad, bt, _bf = make_transcribed_recording(
            ["malformed quantum one", "malformed quantum two"], sha="hybi-malformed"
        )
        bad_sections = _split(bad, bt, [1], ["Alpha", "Beta"])
        make_summary_version(
            bad, bt, bad_sections[1], title="Beta recap",
            overview="quantum section body",
            key_points=[], action_items=[], people=[], topics=[],
        )
        Section.objects.filter(pk=bad_sections[0].pk).update(
            title="Ad-hoc custom", title_is_temporary=True
        )
        _index(config, embedder)
        payload = _hybrid_item(QUERY, config, embedder)
        keys = {r["item_key"] for r in payload["results"]}
        # The malformed split fails closed to its Recording item; the
        # crop-only split was already a Recording item. NEVER section
        # items for either, and never parent + section duplicates.
        assert keys == {f"r:{rec.pk}", f"r:{bad.pk}"}
        for row in payload["results"]:
            assert row["item_kind"] == "recording"
            assert row["section_id"] is None


# ---------------------------------------------------------------------------
# 3. Scope compiled EXACTLY ONCE and shared; precompiled never recompiles
# ---------------------------------------------------------------------------


class TestScopeCompileOnce:
    def _capture_spies(self, monkeypatch):
        captured = {"compiles": 0, "kw_compiled": None, "snap_compiled": None,
                    "kw_scope": "unset", "kw_compiled_scope": "unset",
                    "snap_scope": "unset"}
        # The hybrid resolves the compiler through ITS OWN module
        # namespace, so that is the single spied seam.
        real_compile = sf.compile_item_scope

        def spy_compile(scope_qs, *, using):
            captured["compiles"] += 1
            captured["qs"] = scope_qs
            return real_compile(scope_qs, using=using)

        monkeypatch.setattr(sf, "compile_item_scope", spy_compile)

        real_kw = sf.search_recordings

        def spy_kw(
            query, *, limit, using="default", scope=None, compiled_scope=None,
            compiled_item_scope=None, **kwargs,
        ):
            captured["kw_scope"] = scope
            captured["kw_compiled_scope"] = compiled_scope
            captured["kw_compiled"] = compiled_item_scope
            captured["kw_limit"] = limit
            return real_kw(
                query, limit=limit, using=using, scope=scope,
                compiled_scope=compiled_scope,
                compiled_item_scope=compiled_item_scope, **kwargs,
            )

        monkeypatch.setattr(sf, "search_recordings", spy_kw)

        real_run = sf.run_semantic_snapshot

        def spy_run(snapshot, *, using, limit, verify_final=True):
            captured["snap_compiled"] = snapshot.item_scope
            captured["snap_scope"] = snapshot.scope
            captured["snap_limit"] = limit
            captured["snap_verify_final"] = verify_final
            return real_run(
                snapshot, using=using, limit=limit, verify_final=verify_final
            )

        monkeypatch.setattr(sf, "run_semantic_snapshot", spy_run)
        return captured

    def test_union_compiled_exactly_once_and_shared_verbatim(self, tmp_path, monkeypatch):
        config = emb_config(tmp_path)
        plain, pt, pfixed = make_transcribed_recording(
            ["plain quantum solo"], sha="hybi-compile-plain"
        )
        make_summary_version(
            plain, pt, pfixed, title="Plain recap",
            overview="plain summary quantum body",
            key_points=[], action_items=[], people=[], topics=[],
        )
        _index(config, keyword_embedder(["quantum"]))
        captured = self._capture_spies(monkeypatch)
        union = _item_key_union()
        payload = sf.hybrid_search(
            QUERY, config=config, embedder=keyword_embedder(["quantum"]),
            item_scope=union,
        )
        # EXACTLY ONE compilation of the UNION, on the SAME value object
        # the keyword engine AND the semantic snapshot consumed verbatim.
        assert captured["compiles"] == 1
        assert captured["qs"] is union
        assert captured["kw_compiled"] is captured["snap_compiled"]
        assert captured["kw_compiled"] is not None
        # The Recording-scope mechanisms stay entirely unused in item mode.
        assert captured["kw_scope"] is None
        assert captured["kw_compiled_scope"] is None
        assert captured["snap_scope"] is None
        # Both components still run at depth 200 with the shared value.
        assert captured["kw_compiled"].using == "default"
        assert captured["kw_limit"] == 200
        assert captured["snap_limit"] == 200
        assert captured["snap_verify_final"] is False
        # The engine actually consumed the shared scope (items, not recordings).
        keys = {r["item_key"] for r in payload["results"]}
        assert keys == {f"r:{plain.pk}"}

    def test_precompiled_scope_never_recompiled(self, tmp_path, monkeypatch):
        config = emb_config(tmp_path)
        make_transcribed_recording(["plain quantum solo"], sha="hybi-precompile-plain")
        _index(config, keyword_embedder(["quantum"]))
        captured = self._capture_spies(monkeypatch)
        compiled = _item_compile()
        payload = sf.hybrid_search(
            QUERY, config=config, embedder=keyword_embedder(["quantum"]),
            compiled_item_scope=compiled,
        )
        # A supplied CompiledItemScope is consumed AS-IS: ZERO compiles.
        assert captured["compiles"] == 0
        assert captured["kw_compiled"] is compiled
        assert captured["snap_compiled"] is compiled
        assert payload["results"]

    def test_empty_item_scope_zero_embed_shared_once(self, tmp_path, monkeypatch):
        config = emb_config(tmp_path)
        make_transcribed_recording(
            ["nonempty corpus quantum"], sha="hybi-empty-scope"
        )
        _index(config, keyword_embedder(["quantum"]))
        captured = self._capture_spies(monkeypatch)
        embed_calls = []
        payload = sf.hybrid_search(
            QUERY, config=config,
            embedder=keyword_embedder(["quantum"], tracker=embed_calls),
            item_scope=_item_key_union().none(),
        )
        assert captured["compiles"] == 1  # still compiled exactly once
        assert captured["kw_compiled"] is captured["snap_compiled"]
        assert payload["results"] == []
        assert payload["more_items_matched"] == 0
        assert payload["more_recordings_matched"] == 0
        assert embed_calls == []  # provably-empty scope: zero embedding


# ---------------------------------------------------------------------------
# 4. Deterministic fusion + item-level counts (assembled payload level)
# ---------------------------------------------------------------------------


class TestDeterministicFusionAndCounts:
    def _run(self, monkeypatch, config, kw_results, sem_results, limit, *, more_kw=0, more_sem=0):
        _patch_components(
            monkeypatch,
            _payload(kw_results, more=more_kw),
            _payload(sem_results, more=more_sem),
        )
        return _hybrid_item(
            QUERY, config, keyword_embedder(["quantum"]), limit=limit
        )

    def test_counts_are_fused_item_rows_with_same_value_alias(self, tmp_path, monkeypatch):
        config = emb_config(tmp_path)
        _index(config, keyword_embedder(["quantum"]))
        # Four distinct ITEMS across the two components (two of them
        # Sections of Recording "A" — per-Recording dedup would wrongly
        # yield 3).
        kw_results = [
            _kw_item("s:1", "A", 1),
            _kw_item("s:2", "A", 2),
            _kw_item("r:B", "B", 3),
        ]
        sem_results = [
            _sem_item("s:2", "A", 1),
            _sem_item("r:B", "B", 2),
            _sem_item("r:C", "C", 3),
        ]
        payload = self._run(monkeypatch, config, kw_results, sem_results, limit=2)
        assert payload["result_count"] == 2
        # Fused population is 4 items; exact more = 4 - 2.
        assert payload["more_recordings_matched"] == 2
        assert payload["more_items_matched"] == payload["more_recordings_matched"]
        assert payload["truncated"] is False
        # Item-mode fusion ALWAYS carries the plain mode marker — even
        # though these hand-built component payloads carry no extra key.
        assert payload["item_mode"] is True

    def test_item_mode_flag_is_the_fusion_mode_not_component_data(
        self, tmp_path, monkeypatch
    ):
        config = emb_config(tmp_path)
        _index(config, keyword_embedder(["quantum"]))
        kw_results = [_kw_item("r:B", "B", 1), _kw_item("s:1", "A", 2)]
        sem_results = [_sem_item("r:B", "B", 1)]
        # The visible fused page is ALL Recordings (limit cuts the
        # Section row out) — the mode marker still declares library
        # items; it is the run's mode, never derived from rows.
        payload = self._run(monkeypatch, config, kw_results, sem_results, limit=1)
        assert [r["item_key"] for r in payload["results"]] == ["r:B"]
        assert payload["item_mode"] is True
        # Component payloads are projected through the FIXED metadata
        # keys: a component-level extra key can never leak into the
        # assembled payload.
        for name in ("keyword", "semantic"):
            assert "item_mode" not in payload["components"][name]

    def test_order_is_deterministic_rrf_then_identity(self, tmp_path, monkeypatch):
        config = emb_config(tmp_path)
        _index(config, keyword_embedder(["quantum"]))
        # s:2 and r:B tie on an identical {k2,s1} rank set (math.fsum is
        # order-independent); the item identity ("r:B" < "s:2") decides.
        kw_results = [_kw_item("s:2", "A", 2), _kw_item("r:B", "B", 2)]
        sem_results = [_sem_item("s:2", "A", 1), _sem_item("r:B", "B", 1)]
        payload = self._run(monkeypatch, config, kw_results, sem_results, limit=10)
        assert [r["item_key"] for r in payload["results"]] == ["r:B", "s:2"]
        assert payload["results"][0]["recording_id"] == "B"

    def test_keyword_presentation_wins_identity_is_fused(self, tmp_path, monkeypatch):
        config = emb_config(tmp_path)
        _index(config, keyword_embedder(["quantum"]))
        kw_results = [_kw_item("s:9", "A", 5, title="kw section title")]
        sem_results = [_sem_item("s:9", "A", 1, score=0.77)]
        payload = self._run(monkeypatch, config, kw_results, sem_results, limit=5)
        row = payload["results"][0]
        # Keyword evidence is preferred even when the semantic rank wins,
        # while the identity is the FUSED item (Section), parent retained.
        assert row["title"] == "kw section title"
        assert row["item_key"] == "s:9"
        assert row["item_kind"] == "section"
        assert row["section_id"] == 9
        assert row["recording_id"] == "A"
        assert row["evidence"]["semantic_cosine"] == pytest.approx(0.77)

    def test_incomplete_component_makes_more_null(self, tmp_path, monkeypatch):
        config = emb_config(tmp_path)
        _index(config, keyword_embedder(["quantum"]))
        kw_results = [_kw_item("s:1", "A", 1), _kw_item("s:2", "A", 2)]
        sem_results = [_sem_item("s:1", "A", 1)]
        payload = self._run(
            monkeypatch, config, kw_results, sem_results, limit=10, more_sem=None
        )
        assert payload["more_recordings_matched"] is None
        assert payload["more_items_matched"] is None

    def test_assembled_malformed_identity_falls_back(self, tmp_path, monkeypatch):
        config = emb_config(tmp_path)
        _index(config, keyword_embedder(["quantum"]))
        # A component row WITHOUT the additive item_key fuses under the
        # defensive r:<recording_id> identity (no crash, no fabricated
        # section), while the well-formed sibling keeps its key.
        broken = {
            "rank": 1, "recording_id": "A", "title": "broken",
            "match": {"source": "segment", "document_key": "broken:kw"},
            "snippet": {"field": "body_text", "text": "x"},
        }
        kw_results = [broken, _kw_item("s:3", "A", 2)]
        payload = self._run(monkeypatch, config, kw_results, [], limit=5)
        by_key = {r["item_key"]: r for r in payload["results"]}
        assert set(by_key) == {"r:A", "s:3"}
        assert by_key["r:A"]["item_kind"] == "recording"
        assert by_key["r:A"]["section_id"] is None
        assert by_key["r:A"]["recording_id"] == "A"
        assert by_key["s:3"]["section_id"] == 3

    def test_assembled_noncanonical_identities_fall_back(self, tmp_path, monkeypatch):
        config = emb_config(tmp_path)
        _index(config, keyword_embedder(["quantum"]))
        # Leading-zero, foreign-digit and wrong-parent item keys are NOT
        # canonical: the assembled payload presents them under the
        # canonical parent r:<recording_id> identity (each component
        # merging first-occurrence-wins), never as fabricated section
        # items and never disturbing the canonical sibling.
        kw_results = [
            _boundary_row(1, item="s:007", rid="A"),
            _boundary_row(2, item="s:\uff13", rid="A"),
            _boundary_row(3, item=f"r:{OTHER}", rid="A"),
            _kw_item("s:3", "A", 4),
        ]
        sem_results = [
            {
                "rank": 1, "recording_id": "A", "title": "missing key",
                "match": {"source": "segment", "document_key": "m:sem"},
                "snippet": {"field": "body_text", "text": "s"},
                "score": 0.3,
            },
        ]
        payload = self._run(
            monkeypatch, config, kw_results, sem_results, limit=5
        )
        by_key = {r["item_key"]: r for r in payload["results"]}
        assert set(by_key) == {"r:A", "s:3"}
        parent = by_key["r:A"]
        assert parent["item_kind"] == "recording"
        assert parent["section_id"] is None
        assert parent["recording_id"] == "A"
        assert parent["evidence"]["keyword_rank"] == 1
        assert parent["evidence"]["semantic_rank"] == 1
        assert by_key["s:3"]["item_kind"] == "section"
        assert by_key["s:3"]["section_id"] == 3
        assert by_key["s:3"]["evidence"]["keyword_rank"] == 4
        assert by_key["s:3"]["evidence"]["semantic_rank"] is None


# ---------------------------------------------------------------------------
# 5. Legacy recording mode byte-for-byte unchanged
# ---------------------------------------------------------------------------


class TestLegacyPreserved:
    def test_recording_mode_payload_and_row_shape_unchanged(self, tmp_path, monkeypatch):
        config = build_healthy(tmp_path)
        compiles = {"n": 0}
        real_compile = sf.compile_item_scope

        def spy_compile(*args, **kwargs):
            compiles["n"] += 1
            return real_compile(*args, **kwargs)

        monkeypatch.setattr(sf, "compile_item_scope", spy_compile)
        payload = sf.hybrid_search(
            "alpha", config=config, embedder=keyword_embedder(["alpha", "beta"])
        )
        # The recording-mode TOP-LEVEL and COMPONENT keys are exactly the
        # historical contract (no item-mode keys leak out).
        assert set(payload) == {
            "query", "mode", "index_version", "semantic_query_version",
            "embedding_generation", "rrf_k", "depth", "limit", "results",
            "result_count", "truncated", "more_recordings_matched",
            "components",
        }
        assert "more_items_matched" not in payload
        for name in ("keyword", "semantic"):
            assert set(payload["components"][name]) == {
                "depth", "result_count", "truncated", "more_recordings_matched",
            }
        for row in payload["results"]:
            assert set(row) == {
                "rank", "recording_id", "title", "match", "snippet", "evidence",
            }
        # The item-scope compiler was never touched on the legacy path.
        assert compiles["n"] == 0

    def test_split_parent_stays_one_recording_row_outside_item_mode(self, tmp_path):
        config = emb_config(tmp_path)
        rec, _t, _f, _sections, _fs, _w, plain, _ps = _split_corpus("hybi-leg-")
        embedder = keyword_embedder(["quantum"])
        _index(config, embedder)
        payload = sf.hybrid_search(QUERY, config=config, embedder=embedder)
        # Recording mode: ONE row per Recording (parent included) even
        # though a canonical split exists — unchanged behavior.
        ids = {r["recording_id"] for r in payload["results"]}
        assert ids == {rec.pk, plain.pk}
        assert "item_key" not in payload["results"][0]

    def test_legacy_recording_scope_compiles_once_unchanged(self, tmp_path, monkeypatch):
        # The historical Recording-scope path (spy on kw._compile_scope)
        # still compiles exactly once and never touches the item scope.
        config = build_healthy(tmp_path)
        alpha = Recording.objects.get(sha256="hybi-h-0")
        compiles = {"n": 0}
        real_compile = kw._compile_scope

        def spy_compile(scope_qs, *, using):
            compiles["n"] += 1
            return real_compile(scope_qs, using=using)

        monkeypatch.setattr(kw, "_compile_scope", spy_compile)
        payload = sf.hybrid_search(
            "alpha", config=config, scope=Recording.objects.filter(pk=alpha.pk),
            embedder=keyword_embedder(["alpha", "beta"]),
        )
        assert compiles["n"] == 1
        assert {r["recording_id"] for r in payload["results"]} == {alpha.pk}


# ---------------------------------------------------------------------------
# 6. Item mode keeps the one-sweep / one-embed / read-only contract
# ---------------------------------------------------------------------------


class TestItemModePurity:
    def test_one_health_one_integrity_one_embed_item_mode(self, tmp_path, monkeypatch):
        config = emb_config(tmp_path)
        _split_corpus("hybi-pure-")
        embedder = keyword_embedder(["quantum"])
        _index(config, embedder)
        calls = _spies(monkeypatch)
        tracker = []
        payload = _hybrid_item(
            QUERY, config, keyword_embedder(["quantum"], tracker=tracker)
        )
        assert payload["results"]
        assert calls["health"] == 1
        assert calls["preflight"] == 0
        assert len(tracker) == 1
        gen = active_generation()
        active_rows = EmbeddingDocument.objects.filter(generation=gen).count()
        # The shared traversal decodes every active vector exactly once.
        assert calls["decoded_rows"] == active_rows

    def test_item_mode_is_strictly_read_only(self, tmp_path):
        config = emb_config(tmp_path)
        make_transcribed_recording(["plain quantum solo"], sha="hybi-readonly")
        _index(config, keyword_embedder(["quantum"]))
        with CaptureQueriesContext(connection) as ctx:
            _hybrid_item(QUERY, config, keyword_embedder(["quantum"]))
        writes = [
            q["sql"]
            for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith(
                ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "REPLACE")
            )
        ]
        assert writes == []

    def test_rejects_caller_transaction_zero_embedder_calls_item(self, tmp_path):
        config = emb_config(tmp_path)
        _index(config, keyword_embedder(["quantum"]))

        def forbidden(config, texts):
            raise AssertionError("no embedder call inside a caller transaction")

        compiled = _item_compile()
        with transaction.atomic():
            with pytest.raises(sq.SemanticQueryError) as excinfo:
                sf.hybrid_search(
                    QUERY, config=config, embedder=forbidden,
                    compiled_item_scope=compiled,
                )
        assert excinfo.value.code == sq.SEMANTIC_IN_TRANSACTION


# ---------------------------------------------------------------------------
# 7. Item-scope misuse fails closed BEFORE any health/DB work
# ---------------------------------------------------------------------------


class TestItemScopeMisuse:
    def _health_spy(self, monkeypatch):
        calls = {"health": 0}
        real = si.build_status_report

        def spy(*args, **kwargs):
            calls["health"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(si, "build_status_report", spy)
        return calls

    def _config(self, tmp_path):
        config = emb_config(tmp_path)
        _index(config, keyword_embedder(["quantum"]))
        return config

    def test_recording_scope_and_item_scope_are_exclusive(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        calls = self._health_spy(monkeypatch)
        with pytest.raises(sq.SemanticQueryInputError) as excinfo:
            sf.hybrid_search(
                QUERY, config=config, embedder=keyword_embedder(["quantum"]),
                scope=Recording.objects.all(), item_scope=_item_key_union(),
            )
        assert excinfo.value.code == sq.INVALID_ITEM_SCOPE
        assert "never both" in str(excinfo.value)
        assert calls["health"] == 0

    def test_item_scope_and_compiled_item_scope_ambiguous(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        calls = self._health_spy(monkeypatch)
        with pytest.raises(sq.SemanticQueryInputError) as excinfo:
            sf.hybrid_search(
                QUERY, config=config, embedder=keyword_embedder(["quantum"]),
                item_scope=_item_key_union(), compiled_item_scope=_item_compile(),
            )
        assert excinfo.value.code == sq.INVALID_ITEM_SCOPE
        assert calls["health"] == 0

    def test_wrong_typed_compiled_item_scope_rejected(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        calls = self._health_spy(monkeypatch)
        with pytest.raises(sq.SemanticQueryInputError) as excinfo:
            sf.hybrid_search(
                QUERY, config=config, embedder=keyword_embedder(["quantum"]),
                compiled_item_scope=("SELECT 1", ()),  # type: ignore[arg-type]
            )
        assert excinfo.value.code == sq.INVALID_ITEM_SCOPE
        assert "CompiledItemScope" in str(excinfo.value)
        assert calls["health"] == 0

    def test_wrong_alias_compiled_item_scope_rejected(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        calls = self._health_spy(monkeypatch)
        compiled = _item_compile()
        other = kw.CompiledItemScope(
            sql=compiled.sql, params=compiled.params, using="other"
        )
        with pytest.raises(sq.SemanticQueryInputError) as excinfo:
            sf.hybrid_search(
                QUERY, config=config, embedder=keyword_embedder(["quantum"]),
                compiled_item_scope=other,
            )
        assert excinfo.value.code == sq.INVALID_ITEM_SCOPE
        assert "different database connection" in str(excinfo.value)
        assert calls["health"] == 0

    def test_wrong_shaped_item_queryset_rejected_sanitized(self, tmp_path):
        config = self._config(tmp_path)
        from workflow.models import SearchDocument

        with pytest.raises(kw.SearchQueryInputError):
            sf.hybrid_search(
                QUERY, config=config, embedder=keyword_embedder(["quantum"]),
                item_scope=SearchDocument.objects.all(),
            )
        with pytest.raises(kw.SearchQueryInputError):
            sf.hybrid_search(
                QUERY, config=config, embedder=keyword_embedder(["quantum"]),
                item_scope=_item_key_union()[:1],
            )
