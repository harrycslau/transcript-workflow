"""CLI tests for ``brain search`` in LIBRARY-ITEM mode (Step 6.3).

Every mode searches the UNFILTERED canonical Library item scope
(configured timezone), so a valid active split layout answers EXACTLY
its Section items with the parent Recording SUPPRESSED (never parent +
section duplicates) while unsplit/crop-only/historical/malformed
recordings answer their single Recording item. Hydratable Section rows
are enriched through ONE bounded ``library_items_by_keys`` hydration
(additive ``section_title``/``parent_title``/``section_range``; the
parent ``recording_id`` and the engine item fields are retained);
human output clearly distinguishes the Section and parent context while
unsplit keyword output stays compatible. The more-matches note unit is
driven by the engine's explicit ``item_mode`` marker (the CLI always
searches in item mode, so the note counts LIBRARY ITEMS even when the
visible ``--limit`` window holds only Recording rows), never inferred
from the visible rows. Exit codes, the
validation-before-health order, exactly-one sweep/embed, no
lock/write/rebuild purity and sanitized errors are unchanged. All
network is mocked; no real HTTP.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from brainlib import cli
from brainlib.config import EmbeddingConfig
from factories import (
    make_config,
    make_summary_version,
    make_transcribed_recording,
)
from workflow.models import (
    Recording,
    Section,
)
from workflow.services import embedding_index as ei
from workflow.services import search_fusion as sf
from workflow.services import search_index as si
from workflow.services import search_query as sq
from workflow.services import semantic_query as sem
from workflow.services.embedding_client import EmbeddingBatch

# transaction=True: the semantic/hybrid engines refuse to run inside a
# SQLite transaction, and pytest-django's default outer transaction would
# trip that fixed precondition.
pytestmark = pytest.mark.django_db(transaction=True)

DIM = 4
TIMEZONE = "America/New_York"  # a non-default configured timezone


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def cli_config(tmp_path, monkeypatch, *, model="test-embed-model"):
    """Point ``brainlib.config.load_config`` at a config with the
    embedding model set and a NON-default timezone (the item scope must
    carry the configured timezone)."""
    config = replace(
        make_config(
            tmp_path,
            embedding=EmbeddingConfig(
                base_url="http://127.0.0.1:1/v1",
                model=model,
                api_key_env="BRAIN_TEST_LLM_API_KEY",
                timeout_seconds=120,
                batch_size=32,
            ),
        ),
        timezone=TIMEZONE,
    )
    monkeypatch.setattr("brainlib.config.load_config", lambda: config)
    return config


def keyword_embedder(keywords, dim=DIM, *, tracker=None):
    """Deterministic fake embedder: text containing keyword ``k`` gets
    1.0 on axis ``idx(k)``; the query text dispatches identically."""
    state = {"calls": 0}

    def embed(config, texts):
        state["calls"] += 1
        if tracker is not None:
            tracker.append(list(texts))
        out = []
        for text in texts:
            vec = [0.0] + [0.01] * (dim - 1)
            for idx, keyword in enumerate(keywords):
                if keyword in text:
                    vec[idx] = 1.0
            out.append(EmbeddingBatch(text=text, embedding=tuple(vec)))
        return out

    embed.state = state
    return embed


def _patch_query_embedder(monkeypatch, embedder):
    monkeypatch.setattr("workflow.services.embedding_client.embed_texts", embedder)


def _split(recording, transcript, splits, titles, end):
    from workflow.services.segmentation import save_segmented_version

    save_segmented_version(
        recording.pk,
        transcript.pk,
        0,
        end,
        list(splits),
        list(titles),
        timezone_name=TIMEZONE,
    )
    return list(
        Section.objects.filter(segmented_version__transcript=transcript).order_by(
            "ordinal"
        )
    )


def seed_split_corpus():
    """Split parent (3 segments; crop [0,2) with one split => two topic
    Sections, a Summary on Section 1, a whole-recording Summary) plus one
    unsplit Recording with its own Summary. Callers rebuild afterwards.

    Every Section winner for ``alpha`` is deterministic: the split
    Section-1 Summary carries three ``alpha`` occurrences so it always
    ranks first, and the parent's Library display title is
    ``Whole recap``.
    """
    parent, transcript, fixed = make_transcribed_recording(
        ["alpha split one", "alpha split two", "omega cropped tail"],
        sha="cliitems-split",
    )
    sections = _split(parent, transcript, [1], ["First topic", "Second topic"], end=2)
    make_summary_version(
        parent,
        transcript,
        sections[0],
        title="First summary",
        overview="alpha section alpha body alpha",
        key_points=[],
        action_items=[],
        people=[],
        topics=[],
    )
    make_summary_version(
        parent,
        transcript,
        fixed,
        title="Whole recap",
        overview="omega whole parent body",
        key_points=[],
        action_items=[],
        people=[],
        topics=[],
    )
    plain, plain_transcript, plain_fixed = make_transcribed_recording(
        ["alpha plain solo"], sha="cliitems-plain"
    )
    make_summary_version(
        plain,
        plain_transcript,
        plain_fixed,
        title="Plain recap",
        overview="alpha plain body",
        key_points=[],
        action_items=[],
        people=[],
        topics=[],
    )
    return parent, sections, plain


def seed_keyword():
    """Split corpus + healthy keyword index."""
    seed_split_corpus()
    si.rebuild_index()


def seed_vector(tmp_path, monkeypatch):
    """Split corpus + healthy index + healthy active embedding
    generation (keyword-dispatch embedder)."""
    seed_split_corpus()
    si.rebuild_index()
    config = cli_config(tmp_path, monkeypatch)
    ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha"]))
    return config


def _payload(capsys, mode, query="alpha", extra=()):
    code = cli.main(["search", query, "--mode", mode, "--json", *extra])
    assert code == 0
    return json.loads(capsys.readouterr().out)


def _library_titles():
    """The normal Library's own derived display titles keyed by item_key
    (the EXACT projection the hydration card renders through)."""
    from workflow.query import ListFilters, library_item_queryset

    return {
        row["item_key"]: row["display_title"]
        for row in library_item_queryset(ListFilters(), TIMEZONE)
    }


def _seeded_items():
    """``(parent, sections, plain)`` re-derived from the database by the
    fixed seed shas — the same state every seed helper produces, readable
    regardless of which fixture/body performed the seeding."""
    parent = Recording.objects.get(sha256="cliitems-split")
    sections = list(
        Section.objects.filter(
            segmented_version__transcript__recording=parent,
            segmented_version__isnull=False,
        ).order_by("ordinal")
    )
    plain = Recording.objects.get(sha256="cliitems-plain")
    return parent, sections, plain


# ---------------------------------------------------------------------------
# 1. Keyword: split parents suppress, sibling sections appear
# ---------------------------------------------------------------------------


class TestKeywordItemScope:
    @pytest.fixture(autouse=True)
    def _cli_config(self, tmp_path, monkeypatch):
        """Every item-scope test runs the CLI on the NON-default
        configured timezone: the item scope and the bounded hydration
        must carry the CONFIGURED timezone, never the code default."""
        cli_config(tmp_path, monkeypatch)

    # -- seeding / identity hooks ---------------------------------------

    def _ensure_seeded(self):
        """Base class seeds per-test; the vector subclass pre-seeds the
        whole class through its autouse fixture."""
        seed_keyword()

    def _seeded(self):
        return _seeded_items()

    def _keys(self):
        parent, sections, plain = self._seeded()
        return f"r:{parent.pk}", f"s:{sections[1].pk}", f"r:{plain.pk}"

    def first_key(self):
        _parent, sections, _plain = self._seeded()
        return f"s:{sections[0].pk}"

    def second_key(self):
        _parent, sections, _plain = self._seeded()
        return f"s:{sections[1].pk}"

    def plain_key(self):
        _parent, _sections, plain = self._seeded()
        return f"r:{plain.pk}"

    def parent_pk(self):
        parent, _sections, _plain = self._seeded()
        return parent.pk

    def test_parent_suppressed_and_sections_appear(self, capsys):
        self._ensure_seeded()
        payload = _payload(capsys, "keyword")
        by_key = {row["item_key"]: row for row in payload["results"]}
        parent_key, second_key, plain_key = self._keys()
        assert set(by_key) == {
            self.first_key(),
            second_key,
            plain_key,
        }
        # The parent Recording item is SUPPRESSED, never duplicated.
        assert parent_key not in by_key
        assert payload["result_count"] == 3
        assert payload["more_items_matched"] == payload["more_recordings_matched"]

    def test_section_rows_retain_parent_and_item_fields(self, capsys):
        self._ensure_seeded()
        parent, sections, plain = self._seeded()
        payload = _payload(capsys, "keyword")
        by_key = {row["item_key"]: row for row in payload["results"]}
        first = by_key[f"s:{sections[0].pk}"]
        assert first["item_kind"] == "section"
        assert first["section_id"] == sections[0].pk
        assert first["recording_id"] == str(parent.pk)
        second = by_key[f"s:{sections[1].pk}"]
        assert second["recording_id"] == str(parent.pk)
        plain_row = by_key[f"r:{plain.pk}"]
        assert plain_row["item_kind"] == "recording"
        assert plain_row["section_id"] is None

    def test_section_rows_are_enriched_from_the_library_projection(self, capsys):
        self._ensure_seeded()
        _parent, sections, _plain = self._seeded()
        titles = _library_titles()
        payload = _payload(capsys, "keyword")
        by_key = {row["item_key"]: row for row in payload["results"]}
        first = by_key[f"s:{sections[0].pk}"]
        second = by_key[f"s:{sections[1].pk}"]
        # The Section's OWN Library display title (summary-derived for
        # the summary-backed Section, the stored topic title otherwise).
        assert first["section_title"] == titles[f"s:{sections[0].pk}"]
        assert second["section_title"] == titles[f"s:{sections[1].pk}"]
        assert second["section_title"] == "Second topic"
        # Parent context + bounded range; the engine title (parent's)
        # and every engine field are RETAINED next to the enrichment.
        assert first["parent_title"] == "Whole recap"
        assert first["title"] == "Whole recap"
        assert first["section_range"] == "segment 0"
        assert second["section_range"] == "segment 1"
        # Recording rows carry no enrichment keys at all (unchanged).
        plain_row = next(r for r in payload["results"] if r["item_kind"] == "recording")
        assert "section_title" not in plain_row
        assert "parent_title" not in plain_row

    def test_human_output_distinguishes_section_and_parent(self, capsys):
        self._ensure_seeded()
        assert cli.main(["search", "alpha"]) == 0
        out = capsys.readouterr().out
        assert '· section of "Whole recap"' in out
        assert "· segment 0" in out
        assert "· segment 1" in out

    def test_human_more_note_counts_items_when_sections_present(self, capsys):
        self._ensure_seeded()
        assert cli.main(["search", "alpha", "--limit", "1"]) == 0
        out = capsys.readouterr().out
        assert "1 result(s)" in out
        assert "2 more matching library item(s) beyond --limit." in out

    def test_hydration_runs_once_with_the_section_keys_only(self, monkeypatch, capsys):
        import workflow.query as query_module

        self._ensure_seeded()
        _parent, sections, _plain = self._seeded()
        seen = {}
        real = query_module.library_items_by_keys

        def spy(item_keys, filters, timezone_name, *, using="default"):
            seen["keys"] = list(item_keys)
            seen["filters"] = filters
            seen["timezone"] = timezone_name
            seen["calls"] = seen.get("calls", 0) + 1
            return real(item_keys, filters, timezone_name, using=using)

        monkeypatch.setattr(query_module, "library_items_by_keys", spy)
        payload = _payload(capsys, "keyword")
        assert seen["calls"] == 1
        section_rows = [r for r in payload["results"] if r["item_kind"] == "section"]
        assert seen["keys"] == [row["item_key"] for row in section_rows]
        assert set(seen["keys"]) == {f"s:{sections[0].pk}", f"s:{sections[1].pk}"}
        # UNFILTERED canonical scope with the CONFIGURED timezone.
        assert seen["filters"].as_pairs() == []
        assert seen["filters"].scope_valid
        assert seen["timezone"] == TIMEZONE

    def test_unhydratable_section_key_keeps_the_engine_row(self, monkeypatch, capsys):
        """A key that no longer names a normal Library item (racing
        split/delete) is NEVER fabricated: the engine row stays exactly
        as returned and the human line keeps the neutral marker."""
        import workflow.query as query_module

        self._ensure_seeded()
        _parent, sections, _plain = self._seeded()
        monkeypatch.setattr(
            query_module, "library_items_by_keys", lambda *a, **k: []
        )
        payload = _payload(capsys, "keyword")
        first = next(r for r in payload["results"] if r["item_key"] == f"s:{sections[0].pk}")
        assert "section_title" not in first
        assert "parent_title" not in first
        assert first["title"] == "Whole recap"  # the engine's parent title
        capsys.readouterr()
        assert cli.main(["search", "alpha"]) == 0
        out = capsys.readouterr().out
        assert "· section\n" in out or "· section " in out
        assert '· section of "Whole recap"' not in out

    def test_scope_is_built_unfiltered_once_per_invocation(self, monkeypatch, capsys):
        """One CLI scope construction per search on every mode: empty
        ListFilters + the configured timezone (semantic/hybrid consume
        the SAME value; internal hydration calls are keyed queries)."""
        self._ensure_seeded()
        calls = []
        import workflow.query as query_module

        real = query_module.library_item_key_queryset

        def spy(filters, timezone_name, *, using="default", item_keys=None):
            calls.append(
                {
                    "filters": filters,
                    "timezone": timezone_name,
                    "item_keys": item_keys,
                }
            )
            return real(filters, timezone_name, using=using, item_keys=item_keys)

        monkeypatch.setattr(query_module, "library_item_key_queryset", spy)
        mode = getattr(self, "scope_mode", "keyword")
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", mode]) == 0
        scope_calls = [c for c in calls if c["item_keys"] is None]
        assert len(scope_calls) == 1
        assert scope_calls[0]["filters"].as_pairs() == []
        assert scope_calls[0]["timezone"] == TIMEZONE


class TestKeywordItemScopeVectorModes(TestKeywordItemScope):
    """The same item-scope contract through the vector engines."""

    @pytest.fixture(autouse=True)
    def _vector_ready(self, tmp_path, monkeypatch):
        mode = self.scope_mode
        if mode == "keyword":
            return
        seed_vector(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha"]))

    scope_mode = "semantic"

    def _ensure_seeded(self):
        # The whole class is pre-seeded by the _vector_ready fixture;
        # per-test re-seeding would collide with the fixed seed shas.
        return

    def test_parent_suppressed_and_sections_appear(self, capsys):
        payload = _payload(capsys, self.scope_mode)
        by_key = {row["item_key"]: row for row in payload["results"]}
        assert set(by_key) == {self.first_key(), self.second_key(), self.plain_key()}

    def test_second_section_row_matches_the_shared_helpers(self, capsys):
        payload = _payload(capsys, self.scope_mode)
        by_key = {row["item_key"]: row for row in payload["results"]}
        second = by_key[self.second_key()]
        assert second["item_kind"] == "section"
        assert second["recording_id"] == str(self.parent_pk())

    # The more-matches note unit is the engine's item-mode marker in
    # EVERY mode (no longer a keyword-only concern): the same inherited
    # test now runs here with the semantic engine carrying its own
    # explicit ``item_mode`` truth.
    def test_human_more_note_counts_items_when_sections_present(self, capsys):
        mode = self.scope_mode
        assert cli.main(["search", "alpha", "--mode", mode, "--limit", "1"]) == 0
        out = capsys.readouterr().out
        assert "1 result(s)" in out
        # THREE matched items (two Sections + the plain Recording), one
        # shown: the item unit is stated for the vector modes too.
        assert "2 more matching library item(s) beyond --limit." in out


class TestHumanMoreNoteUnitFollowsTheItemModeFlag:
    """The visible ``--limit`` window can be ALL Recording rows while
    the OMITTED matches are Sections: the note unit comes from the
    engine's explicit ``item_mode`` marker (the CLI always searches in
    item mode), NEVER from inferring the unit off the visible rows."""

    @pytest.fixture(autouse=True)
    def _cli_config(self, tmp_path, monkeypatch):
        cli_config(tmp_path, monkeypatch)

    def _seed(self):
        # Split parent: both Section item winners are SEGMENT-backed
        # (no Section Summaries).
        parent, transcript, _fixed = make_transcribed_recording(
            ["alpha split one", "alpha split two"], sha="clnote-split"
        )
        _split(parent, transcript, [1], ["Left topic", "Right topic"], end=2)
        # Unsplit Recording: its item winner is a SUMMARY document, so
        # the deterministic keyword comparator (document type before
        # occurrence count or identity) puts this Recording row FIRST.
        plain, pt, pfixed = make_transcribed_recording(
            ["plain body"], sha="clnote-plain"
        )
        make_summary_version(
            plain,
            pt,
            pfixed,
            title="Plain recap",
            overview="alpha summary body",
            key_points=[],
            action_items=[],
            people=[],
            topics=[],
        )
        si.rebuild_index()
        return parent, plain

    def test_exact_note_says_library_items_with_only_recordings_visible(
        self, capsys
    ):
        self._seed()
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--limit", "1"]) == 0
        out = capsys.readouterr().out
        # The single visible row IS the plain Recording...
        assert "1 result(s)" in out
        assert "Plain recap" in out
        # ...while the TWO omitted matches are Sections: the note
        # counts library items, never "recordings".
        assert "2 more matching library item(s) beyond --limit." in out
        assert "recording(s)" not in out

    def test_visible_section_row_shape_and_note_are_untouched(self, capsys):
        self._seed()
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--limit", "2"]) == 0
        out = capsys.readouterr().out
        # A visible Section row keeps its context suffix unchanged; the
        # note stays the item unit for the omitted third item.
        assert "2 result(s)" in out
        assert "· section" in out
        assert "· segment" in out
        assert "1 more matching library item(s) beyond --limit." in out
        assert "recording(s)" not in out


# ---------------------------------------------------------------------------
# 2. Semantic / hybrid item mode through the CLI
# ---------------------------------------------------------------------------


class TestSemanticItemCLI:
    def test_semantic_answers_items_with_one_sweep_one_embed(
        self, tmp_path, monkeypatch, capsys
    ):
        seed_vector(tmp_path, monkeypatch)
        health = []
        real_status = si.build_status_report

        def counting_status(*args, **kwargs):
            health.append(True)
            return real_status(*args, **kwargs)

        monkeypatch.setattr(si, "build_status_report", counting_status)
        tracker = []
        _patch_query_embedder(
            monkeypatch, keyword_embedder(["alpha"], tracker=tracker)
        )
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", "semantic", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(health) == 1
        assert len(tracker) == 1
        by_key = {row["item_key"]: row for row in payload["results"]}
        assert set(by_key) == {
            f"s:{s.pk}" for s in Section.objects.filter(
                segmented_version__isnull=False
            ).order_by()
        } | {
            f"r:{row['recording_id']}"
            for row in payload["results"]
            if row["item_kind"] == "recording"
        }
        parent_rows = [r for r in payload["results"] if r["item_kind"] == "recording"]
        assert len(parent_rows) == 1  # only the unsplit Recording
        assert payload["mode"] == "semantic"
        assert payload["more_items_matched"] == payload["more_recordings_matched"]

    def test_semantic_section_rows_enriched_and_parent_retained(
        self, tmp_path, monkeypatch, capsys
    ):
        seed_vector(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha"]))
        titles = _library_titles()
        payload = _payload(capsys, "semantic")
        sections = list(Section.objects.filter(segmented_version__isnull=False))
        by_key = {row["item_key"]: row for row in payload["results"]}
        for section in sections:
            row = by_key[f"s:{section.pk}"]
            assert row["item_kind"] == "section"
            assert row["recording_id"]  # parent provenance retained
            assert row["section_title"] == titles[f"s:{section.pk}"]
            assert row["parent_title"] == "Whole recap"
            assert "score" in row

    def test_semantic_human_marks_section_context(self, tmp_path, monkeypatch, capsys):
        seed_vector(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha"]))
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", "semantic"]) == 0
        out = capsys.readouterr().out
        assert '· section of "Whole recap"' in out
        assert "[semantic]" in out


class TestHybridItemCLI:
    def _run(self, tmp_path, monkeypatch, capsys, argv_extra=()):
        seed_vector(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha"]))
        capsys.readouterr()
        assert cli.main(
            ["search", "alpha", "--mode", "hybrid", "--json", *argv_extra]
        ) == 0
        return json.loads(capsys.readouterr().out)

    def test_hybrid_fuses_section_items_and_suppresses_parent(
        self, tmp_path, monkeypatch, capsys
    ):
        payload = self._run(tmp_path, monkeypatch, capsys)
        by_key = {row["item_key"]: row for row in payload["results"]}
        parent_pks = {
            str(rec_pk)
            for rec_pk in Section.objects.filter(
                segmented_version__isnull=False
            ).values_list("transcript__recording_id", flat=True)
        }
        assert f"r:{parent_pks.pop()}" not in by_key
        sections = list(Section.objects.filter(segmented_version__isnull=False))
        assert {f"s:{s.pk}" for s in sections} <= set(by_key)
        for section in sections:
            row = by_key[f"s:{section.pk}"]
            assert row["item_kind"] == "section"
            assert row["parent_title"] == "Whole recap"
            assert set(row["evidence"]) == {
                "keyword_rank",
                "semantic_rank",
                "semantic_cosine",
                "rrf_score",
            }
        assert payload["mode"] == "hybrid"
        assert payload["more_items_matched"] == payload["more_recordings_matched"]

    def test_hybrid_honours_the_limit_on_items(self, tmp_path, monkeypatch, capsys):
        payload = self._run(tmp_path, monkeypatch, capsys, ("--limit", "1"))
        assert payload["result_count"] == 1
        assert payload["more_recordings_matched"] is None or (
            payload["more_recordings_matched"] >= 1
        )

    def test_hybrid_human_marks_section_context(
        self, tmp_path, monkeypatch, capsys
    ):
        seed_vector(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha"]))
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", "hybrid"]) == 0
        out = capsys.readouterr().out
        assert '· section of "Whole recap"' in out
        assert "[hybrid]" in out
        assert "(kw #" in out


# ---------------------------------------------------------------------------
# 3. Unsplit keyword compatibility
# ---------------------------------------------------------------------------


class TestUnsplitKeywordCompatibility:
    def _seed_unsplit(self):
        make_transcribed_recording(["quarterly budget review meeting"], sha="cmp-0")
        make_transcribed_recording(["garden planning discussion"], sha="cmp-1")
        si.rebuild_index()

    def test_human_line_shape_is_unchanged(self, capsys):
        self._seed_unsplit()
        capsys.readouterr()
        assert cli.main(["search", "budget"]) == 0
        out = capsys.readouterr().out
        assert re.search(
            r"^1\. Untitled recording  \[segment 0 @ 0:00\]$", out, re.M
        )
        assert "· section" not in out

    def test_more_note_counts_items_even_for_unsplit_corpus(self, capsys):
        make_transcribed_recording(["needle in this story"], sha="cmp-n1")
        make_transcribed_recording(["another needle tale"], sha="cmp-n2")
        si.rebuild_index()
        capsys.readouterr()
        assert cli.main(["search", "needle", "--limit", "1"]) == 0
        out = capsys.readouterr().out
        # Item mode ALWAYS names the unit: the CLI searches the Library
        # item scope even when every item is the Recording itself.
        assert "1 more matching library item(s) beyond --limit." in out

    def test_recording_rows_have_no_enrichment_keys(self, capsys):
        self._seed_unsplit()
        payload = _payload(capsys, "keyword", query="budget")
        row = payload["results"][0]
        assert row["item_kind"] == "recording"
        assert "section_title" not in row
        assert "parent_title" not in row
        assert "section_range" not in row


# ---------------------------------------------------------------------------
# 4. Order, purity and exit-code contract unchanged with item scope
# ---------------------------------------------------------------------------


class TestPurityAndOrder:
    @pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
    def test_usage_errors_exit_2_before_any_health(self, mode, capsys):
        # No index is built on purpose; the health spy must stay silent.
        calls = []
        import workflow.services.search_index as search_index

        real = search_index.build_status_report

        def counting(*args, **kwargs):
            calls.append(True)
            return real(*args, **kwargs)

        # Patch BEFORE the command runs; both the keyword preflight and
        # the vector engines resolve the sweep through this module.
        search_index.build_status_report = counting
        try:
            assert cli.main(["search", "   ", "--mode", mode]) == 2
        finally:
            search_index.build_status_report = real
        captured = capsys.readouterr()
        assert calls == []
        assert "empty" in captured.err
        assert "Traceback" not in captured.err

    @pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
    def test_search_never_locks_with_a_split_corpus(
        self, mode, tmp_path, monkeypatch, capsys
    ):
        import workflow.services.pipeline as pipeline_service
        import workflow.services.pipeline_lock as pipeline_lock_service

        if mode == "keyword":
            seed_keyword()
        else:
            seed_vector(tmp_path, monkeypatch)
            _patch_query_embedder(monkeypatch, keyword_embedder(["alpha"]))

        def forbidden(*args, **kwargs):
            raise AssertionError("search must not lock or recover")

        monkeypatch.setattr(pipeline_lock_service, "pipeline_lock", forbidden)
        monkeypatch.setattr(pipeline_service, "pipeline_lock", forbidden)
        monkeypatch.setattr(pipeline_service, "recover_interruptions", forbidden)
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", mode]) == 0

    @pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
    def test_search_and_hydration_never_write(self, mode, tmp_path, monkeypatch, capsys):
        if mode == "keyword":
            seed_keyword()
        else:
            seed_vector(tmp_path, monkeypatch)
            _patch_query_embedder(monkeypatch, keyword_embedder(["alpha"]))
        capsys.readouterr()
        with CaptureQueriesContext(connection) as ctx:
            assert cli.main(["search", "alpha", "--mode", mode]) == 0
        writes = [
            q["sql"]
            for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith(
                ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "REPLACE")
            )
        ]
        assert writes == []

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_stale_index_still_exits_1_sanitized(
        self, mode, tmp_path, monkeypatch, capsys
    ):
        seed_vector(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha"]))
        from workflow.models import TranscriptSegment

        TranscriptSegment.objects.filter(text="alpha split two").update(
            text="edited secret content"
        )
        assert cli.main(["search", "alpha", "--mode", mode]) == 1
        captured = capsys.readouterr()
        assert "edited secret content" not in captured.err
        assert "Traceback" not in captured.err
        assert captured.out == ""


# ---------------------------------------------------------------------------
# 5. Focused service-call contract (engines stubbed)
# ---------------------------------------------------------------------------


class TestServiceCallItemScope:
    """Every ``brain search`` mode must hand its engine EXACTLY ONE
    ``item_scope``: the value built by ONE unfiltered, unsliced
    ``workflow.query.library_item_key_queryset(ListFilters(),
    config.timezone)`` call (identity-checked through a sentinel). The
    engines are stubbed so these tests pin the CLI wiring itself —
    argument shapes, the validation-before-scope-before-health call
    order, the single compiled value shared by hybrid's components,
    the absence of any CLI-added keyword sweep, and the never-lock
    contract — independently of any engine result."""

    @pytest.fixture(autouse=True)
    def _cli_config(self, tmp_path, monkeypatch):
        return cli_config(tmp_path, monkeypatch)

    def _spy_scope(self, monkeypatch, order=None):
        """Replace ``library_item_key_queryset`` with a recording spy
        returning an identity sentinel; the engines are stubbed, so the
        sentinel is passed through and never evaluated."""
        import workflow.query as query_module

        calls = []
        sentinel = object()

        def spy(filters, timezone_name, *, using="default", item_keys=None):
            if order is not None:
                order.append("scope")
            calls.append(
                {
                    "filters": filters,
                    "timezone": timezone_name,
                    "item_keys": item_keys,
                }
            )
            return sentinel

        monkeypatch.setattr(query_module, "library_item_key_queryset", spy)
        return calls, sentinel

    def _assert_unfiltered_scope_call(self, calls):
        assert len(calls) == 1
        assert calls[0]["filters"].as_pairs() == []
        assert calls[0]["timezone"] == TIMEZONE
        assert calls[0]["item_keys"] is None  # unsliced, unfiltered

    @pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
    def test_every_mode_passes_the_unfiltered_item_scope(
        self, mode, _cli_config, monkeypatch, capsys
    ):
        order = []
        calls, sentinel = self._spy_scope(monkeypatch, order=order)
        seen = {}

        def record(name, payload_mode=None):
            def engine(query, **kwargs):
                order.append(name)
                seen["query"] = query
                seen["kwargs"] = kwargs
                payload = {"query": query, "results": []}
                if payload_mode is not None:
                    payload["mode"] = payload_mode
                return payload

            return engine

        monkeypatch.setattr(sq, "search_recordings", record("keyword"))
        monkeypatch.setattr(sem, "semantic_search", record("semantic", "semantic"))
        monkeypatch.setattr(sf, "hybrid_search", record("hybrid", "hybrid"))
        monkeypatch.setattr(
            "workflow.services.embedding_client.embed_texts",
            lambda *a, **k: pytest.fail("the CLI must not embed"),
        )
        if mode == "keyword":
            # The keyword preflight is observable but must stay the
            # single CLI-side sweep, BEFORE the engine runs.
            def health():
                order.append("health")

            monkeypatch.setattr(sq, "preflight_full_health", health)
        else:
            # Vector modes own their one sweep inside the service; the
            # CLI never adds a second one.
            def forbidden(*args, **kwargs):
                raise AssertionError(f"{mode} must not run the keyword preflight")

            monkeypatch.setattr(sq, "preflight_full_health", forbidden)

        capsys.readouterr()
        code = cli.main(
            ["search", "alpha", "--mode", mode, "--limit", "7", "--json"]
        )
        assert code == 0

        kwargs = seen["kwargs"]
        assert seen["query"] == "alpha"
        # THE contract: the engine receives the exact value the CLI's
        # single library_item_key_queryset call produced.
        assert kwargs["item_scope"] is sentinel
        self._assert_unfiltered_scope_call(calls)
        if mode == "keyword":
            assert set(kwargs) == {"limit", "item_scope"}
            assert kwargs["limit"] == 7
            assert order == ["scope", "health", "keyword"]
        else:
            assert set(kwargs) == {"limit", "config", "embedder", "item_scope"}
            assert kwargs["limit"] == 7
            assert kwargs["config"] is _cli_config
            # The embedder is resolved from the embedding_client MODULE
            # at call time (the established test-mock point).
            import workflow.services.embedding_client as ec

            assert kwargs["embedder"] is ec.embed_texts
            assert order == ["scope", mode]

    @pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
    def test_invalid_query_touches_nothing(self, mode, monkeypatch, capsys):
        """Cheap input validation stays FIRST: no scope construction, no
        health sweep, no engine call — exit 2 with the same stderr."""
        calls, _sentinel = self._spy_scope(monkeypatch)
        touched = []
        monkeypatch.setattr(
            sq, "search_recordings", lambda *a, **k: touched.append("engine")
        )
        monkeypatch.setattr(
            sq, "preflight_full_health", lambda: touched.append("health")
        )
        monkeypatch.setattr(
            sem, "semantic_search", lambda *a, **k: touched.append("semantic")
        )
        monkeypatch.setattr(
            sf, "hybrid_search", lambda *a, **k: touched.append("hybrid")
        )
        assert cli.main(["search", "   ", "--mode", mode]) == 2
        err = capsys.readouterr().err
        assert "empty" in err
        assert "Traceback" not in err
        assert touched == []
        assert calls == []

    @pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
    def test_engine_config_error_still_exits_1(self, mode, monkeypatch, capsys):
        from brainlib.config import ConfigError

        self._spy_scope(monkeypatch)

        def boom(*args, **kwargs):
            raise ConfigError("engine config boom")

        monkeypatch.setattr(sq, "search_recordings", boom)
        monkeypatch.setattr(sq, "preflight_full_health", lambda: None)
        monkeypatch.setattr(sem, "semantic_search", boom)
        monkeypatch.setattr(sf, "hybrid_search", boom)
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", mode]) == 1
        captured = capsys.readouterr()
        assert "engine config boom" in captured.err
        assert "Traceback" not in captured.err
        assert captured.out == ""

    @pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
    def test_item_mode_search_still_never_locks(self, mode, monkeypatch, capsys):
        import workflow.services.pipeline as pipeline_service
        import workflow.services.pipeline_lock as pipeline_lock_service

        self._spy_scope(monkeypatch)
        monkeypatch.setattr(
            sq, "search_recordings", lambda q, **k: {"query": q, "results": []}
        )
        monkeypatch.setattr(sq, "preflight_full_health", lambda: None)
        monkeypatch.setattr(
            sem,
            "semantic_search",
            lambda q, **k: {"query": q, "mode": "semantic", "results": []},
        )
        monkeypatch.setattr(
            sf,
            "hybrid_search",
            lambda q, **k: {"query": q, "mode": "hybrid", "results": []},
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("search must not lock or recover")

        monkeypatch.setattr(pipeline_lock_service, "pipeline_lock", forbidden)
        monkeypatch.setattr(pipeline_service, "pipeline_lock", forbidden)
        monkeypatch.setattr(pipeline_service, "recover_interruptions", forbidden)
        capsys.readouterr()
        assert cli.main(["search", "alpha", "--mode", mode, "--json"]) == 0
