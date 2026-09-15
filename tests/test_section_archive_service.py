"""Reversible individual-Section archive service + eligibility.

Proves the Section archive contract directly on the service boundary and
across the shared Library/item-scope/search surfaces:

- archive sets ``Section.archived_at`` transactionally; restore clears it;
- both are idempotent (the second call is zero DML);
- only a canonical ACTIVE topic Section of an unarchived parent can be
  newly archived (fixed/historical/parent-archived/cross-recording all
  refuse);
- restore is deliberately permissive for a Section that later became
  historical (that is the only mutation);
- archive/restore never touch layout rows, other Section fields, files,
  the network, or the search/embedding index rows, and never schedule
  sync;
- the normal Library projection/count/item-key scope and EVERY search
  mode (keyword/semantic/hybrid) omit the archived Section while keeping
  siblings and the parent recording suppression;
- restore makes the Section eligible again with NO reindex/embed, and the
  retained index rows stay internally healthy throughout.
"""

from __future__ import annotations

import pytest

from brainlib.config import EmbeddingConfig
from factories import (
    make_config,
    make_summary_version,
    make_transcribed_recording,
)
from workflow.models import (
    EmbeddingDocument,
    Recording,
    SearchDocument,
    Section,
    SegmentedVersion,
)
from workflow.query import (
    ListFilters,
    library_item_key_queryset,
    library_item_queryset,
    library_items_by_keys,
)
from workflow.services import archive
from workflow.services import embedding_index as ei
from workflow.services import search_fusion as sf
from workflow.services import search_index as si
from workflow.services import search_query as sq
from workflow.services import semantic_query as semq
from workflow.services.embedding_client import EmbeddingBatch
from workflow.services.segmentation import save_segmented_version

pytestmark = pytest.mark.django_db(transaction=True)

TZ = "Europe/Helsinki"
DIM = 4


def emb_config(tmp_path, model="test-embed-model"):
    from brainlib.config import LLMConfig

    return make_config(
        tmp_path,
        embedding=EmbeddingConfig(
            base_url="http://127.0.0.1:1/v1",
            model=model,
            api_key_env="BRAIN_TEST_LLM_API_KEY",
            timeout_seconds=120,
            batch_size=32,
        ),
        llm=LLMConfig(
            provider="openai_compatible",
            base_url="http://127.0.0.1:1/v1",
            model="test-chat-model",
            api_key_env="BRAIN_TEST_LLM_API_KEY",
            temperature=0.2,
            timeout_seconds=600,
        ),
    )


def keyword_embedder(keywords, dim=DIM):
    def embed(config, texts):
        out = []
        for text in texts:
            vec = [0.0] + [0.01] * (dim - 1)
            for idx, keyword in enumerate(keywords):
                if keyword in text:
                    vec[idx] = 1.0
            out.append(EmbeddingBatch(text=text, embedding=tuple(vec)))
        return out

    return embed


def _split(recording, transcript, splits, titles, start=0, end=None):
    if end is None:
        end = transcript.segments.count()
    result = save_segmented_version(
        recording.pk, transcript.pk, start, end, list(splits), list(titles)
    )
    return list(
        Section.objects.filter(segmented_version_id=result.version_id).order_by("ordinal")
    )


def _split_with_summaries(tmp_path, sha):
    """One recording split into two ACTIVE topic Sections with ACTIVE
    default summaries on each; healthy search+embedding indexes."""
    rec, transcript, fixed = make_transcribed_recording(
        ["alpha one", "alpha two", "alpha three", "alpha four"], sha=sha
    )
    sections = _split(rec, transcript, [2], ["Alpha Topic", "Beta Topic"])
    make_summary_version(
        rec, transcript, sections[0], title="alpha section one", overview="alpha body one"
    )
    make_summary_version(
        rec, transcript, sections[1], title="alpha section two", overview="alpha body two"
    )
    si.rebuild_index()
    config = emb_config(tmp_path)
    ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha"]))
    return rec, transcript, sections, config


def _keys():
    return set(
        library_item_key_queryset(ListFilters(), TZ).values_list("item_key", flat=True)
    )


class TestSectionArchiveService:
    def test_archive_restore_and_idempotency(self, tmp_path):
        rec, transcript, sections, _config = _split_with_summaries(tmp_path, "secsvc-a")
        target = sections[0]
        result = archive.archive_section(rec, target)
        assert result == {"section_id": target.pk, "result": "archived", "archived": True}
        target.refresh_from_db()
        assert target.archived_at is not None

        again = archive.archive_section(rec, target)
        assert again["result"] == "unchanged"

        restored = archive.restore_section(rec, target)
        assert restored == {"section_id": target.pk, "result": "restored", "archived": False}
        target.refresh_from_db()
        assert target.archived_at is None
        assert archive.restore_section(rec, target)["result"] == "unchanged"

    def test_fixed_section_refused(self, tmp_path):
        rec, _t, fixed = make_transcribed_recording(["a"], sha="secsvc-fixed")
        result = archive.archive_section(rec, fixed)
        assert result["result"] == "refused"
        assert result["reason"] == "section_not_topic"
        fixed.refresh_from_db()
        assert fixed.archived_at is None

    def test_historical_section_not_newly_archivable(self, tmp_path):
        rec, transcript, sections, _config = _split_with_summaries(tmp_path, "secsvc-hist")
        old = sections[0]
        # Supersede the layout: `old` becomes historical.
        _split(rec, transcript, [1], ["Gamma", "Delta"])
        old.refresh_from_db()
        result = archive.archive_section(rec, old)
        assert result["result"] == "refused"
        assert result["reason"] == "section_not_active"
        old.refresh_from_db()
        assert old.archived_at is None

    def test_parent_archived_refused(self, tmp_path):
        rec, _t, sections, _config = _split_with_summaries(tmp_path, "secsvc-parent")
        archive.archive_recording(rec)
        result = archive.archive_section(rec, sections[1])
        assert result["result"] == "refused"
        assert result["reason"] == "parent_archived"

    def test_cross_recording_refused(self, tmp_path):
        rec, _t, sections, _config = _split_with_summaries(tmp_path, "secsvc-cross-a")
        other, _t2, _s2 = make_transcribed_recording(["b"], sha="secsvc-cross-b")
        result = archive.archive_section(other, sections[0])
        assert result["result"] == "refused"
        assert result["reason"] == "section_not_in_recording"

    def test_restore_is_permissive_for_later_historical(self, tmp_path):
        rec, transcript, sections, _config = _split_with_summaries(tmp_path, "secsvc-restore")
        target = sections[0]
        assert archive.archive_section(rec, target)["result"] == "archived"
        # Supersede the layout so the archived Section becomes historical.
        _split(rec, transcript, [1, 3], ["A", "B", "C"])
        target.refresh_from_db()
        assert target.segmented_version.is_active is False
        result = archive.restore_section(rec, target)
        assert result["result"] == "restored"
        target.refresh_from_db()
        assert target.archived_at is None

    def test_archive_touches_only_archived_at(self, tmp_path):
        rec, _t, sections, _config = _split_with_summaries(tmp_path, "secsvc-only")
        target = sections[1]
        before = {
            field: getattr(target, field)
            for field in ("title", "ordinal", "segmented_version_id", "start_segment_ordinal")
        }
        archive.archive_section(rec, target)
        target.refresh_from_db()
        after = {field: getattr(target, field) for field in before}
        assert after == before
        # The layout revision is untouched.
        assert SegmentedVersion.objects.filter(
            id=target.segmented_version_id, is_active=True
        ).exists()

    def test_no_sync_or_network(self, tmp_path, monkeypatch):
        from workflow.services import search_sync

        rec, _t, sections, _config = _split_with_summaries(tmp_path, "secsvc-sync")
        monkeypatch.setattr(
            search_sync,
            "schedule_recording_sync",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no sync")),
        )
        monkeypatch.setattr(
            "workflow.services.embedding_client.embed_texts",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no embedding")),
        )
        archive.archive_section(rec, sections[0])
        archive.restore_section(rec, sections[0])


class TestArchivedSectionMutationsRefused:
    def test_section_summary_skipped(self, tmp_path):
        rec, _t, sections, config = _split_with_summaries(tmp_path, "secmut-summary")
        archive.archive_section(rec, sections[0])
        from workflow.services import summarize as summarize_service

        result = summarize_service.summarize_section_one(
            config, sections[0], target_language="default"
        )
        assert result.get("result") == "skipped"
        assert result.get("reason") == "archived"

    def test_section_tag_mutation_refused(self, tmp_path):
        from factories import make_tag
        from workflow.services.tags import TagOperationError, add_manual_tag_section

        rec, _t, sections, _config = _split_with_summaries(tmp_path, "secmut-tags")
        archive.archive_section(rec, sections[0])
        with pytest.raises(TagOperationError) as excinfo:
            add_manual_tag_section(sections[0], make_tag("ArchivedSectionTag"))
        assert excinfo.value.code == "section_archived"


class TestLibraryAndScopeExclusion:
    def test_archived_section_excluded_siblings_and_parent_suppression(self, tmp_path):
        rec, _t, sections, _config = _split_with_summaries(tmp_path, "secscope")
        archive.archive_section(rec, sections[0])

        keys = _keys()
        assert f"s:{sections[0].pk}" not in keys
        assert f"s:{sections[1].pk}" in keys
        # The parent Recording stays suppressed (never resurrected).
        assert f"r:{rec.pk}" not in keys

        items = set(
            library_item_queryset(ListFilters(), TZ).values_list("item_key", flat=True)
        )
        assert f"s:{sections[0].pk}" not in items
        assert f"s:{sections[1].pk}" in items

    def test_stale_engine_key_cannot_hydrate_archived_section(self, tmp_path):
        rec, _t, sections, _config = _split_with_summaries(tmp_path, "secscope-stale")
        key = f"s:{sections[0].pk}"
        assert key in _keys()
        archive.archive_section(rec, sections[0])
        assert library_items_by_keys([key], ListFilters(), TZ) == []

    def test_restore_is_immediately_eligible(self, tmp_path):
        rec, _t, sections, _config = _split_with_summaries(tmp_path, "secscope-restore")
        key = f"s:{sections[0].pk}"
        archive.archive_section(rec, sections[0])
        assert key not in _keys()
        archive.restore_section(rec, sections[0])
        assert key in _keys()


class TestSearchModesOmitArchivedSection:
    def _search_all(self, config):
        item_scope = library_item_key_queryset(ListFilters(), TZ)
        keyword = sq.search_recordings("alpha", item_scope=item_scope)
        semantic = semq.semantic_search(
            "alpha",
            config=config,
            embedder=keyword_embedder(["alpha"]),
            item_scope=library_item_key_queryset(ListFilters(), TZ),
        )
        hybrid = sf.hybrid_search(
            "alpha",
            config=config,
            embedder=keyword_embedder(["alpha"]),
            item_scope=library_item_key_queryset(ListFilters(), TZ),
        )
        return keyword, semantic, hybrid

    def _item_keys(self, payload):
        return {result["item_key"] for result in payload["results"]}

    def test_all_modes_omit_archived_and_restore_reenables(self, tmp_path):
        rec, _t, sections, config = _split_with_summaries(tmp_path, "secsearch")
        archived_key = f"s:{sections[0].pk}"
        sibling_key = f"s:{sections[1].pk}"

        keyword, semantic, hybrid = self._search_all(config)
        for payload in (keyword, semantic, hybrid):
            assert archived_key in self._item_keys(payload)
            assert sibling_key in self._item_keys(payload)

        archive.archive_section(rec, sections[0])

        # No reindex/embed after archive: the engine item scopes exclude it.
        document_count = SearchDocument.objects.count()
        vector_count = EmbeddingDocument.objects.count()
        keyword, semantic, hybrid = self._search_all(config)
        for payload in (keyword, semantic, hybrid):
            keys = self._item_keys(payload)
            assert archived_key not in keys
            assert sibling_key in keys
        # Retained physical rows + internal health.
        assert SearchDocument.objects.count() == document_count
        assert EmbeddingDocument.objects.count() == vector_count
        assert si.build_status_report()["healthy"] is True
        assert ei.build_embedding_status_report(config)["healthy"] is True

        # Restore makes it eligible immediately — no reindex/embed.
        archive.restore_section(rec, sections[0])
        keyword, semantic, hybrid = self._search_all(config)
        for payload in (keyword, semantic, hybrid):
            assert archived_key in self._item_keys(payload)
        assert SearchDocument.objects.count() == document_count
        assert EmbeddingDocument.objects.count() == vector_count


class TestSectionArchiveDoesNotDeleteRows:
    def test_index_rows_retained_and_healthy(self, tmp_path):
        rec, _t, sections, config = _split_with_summaries(tmp_path, "secrows")
        before_docs = SearchDocument.objects.count()
        before_vectors = EmbeddingDocument.objects.count()
        archive.archive_section(rec, sections[0])
        archive.restore_section(rec, sections[0])
        assert SearchDocument.objects.count() == before_docs
        assert EmbeddingDocument.objects.count() == before_vectors
        assert si.build_status_report()["healthy"] is True

    def test_archive_never_merges_or_supersedes_layout(self, tmp_path):
        rec, _t, sections, _config = _split_with_summaries(tmp_path, "secrows-layout")
        version_id = sections[0].segmented_version_id
        archive.archive_section(rec, sections[0])
        assert SegmentedVersion.objects.filter(id=version_id, is_active=True).exists()
        assert Section.objects.filter(segmented_version_id=version_id).count() == 2
