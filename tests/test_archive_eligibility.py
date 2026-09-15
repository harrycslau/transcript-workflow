"""Archived Recordings are ineligible across every user-facing surface.

The exclusion is centralized: ``workflow.query.filter_only`` (Library
projection/count/identity UNION + recording-scope search) and the Section
branch, ``workflow.services.review`` and the pipeline work selectors, plus
the Ask Recording scope and the read-only variant view-model. These tests
prove the archived rows never reach a result and that explicit mutations
fail safely, reusing the canonical scopes rather than duplicated filters.
"""

from __future__ import annotations

import types

import pytest
from django.utils import timezone

from brainlib.config import ConfigError
from factories import make_config, make_summary_version, make_tag, make_transcribed_recording
from workflow.context_processors import review_badge_count
from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
    RoutingDecision,
    RoutingMethod,
    Section,
    SummaryState,
)
from workflow.query import (
    ListFilters,
    library_item_key_queryset,
    library_item_queryset,
    library_items_by_keys,
    search_scope_queryset,
)
from workflow.services import ask as ask_service
from workflow.services import search_index as si
from workflow.services import search_query as sq
from workflow.services.archive import ArchivedRecordingError, archive_recording
from workflow.services.review import build_review_report
from workflow.services.segmentation import SegmentationError, save_segmented_version
from workflow.services.tags import TagOperationError, add_manual_tag

pytestmark = pytest.mark.django_db

TZ = "Europe/Helsinki"


def _items(filters=None):
    return list(
        library_item_queryset(filters or ListFilters(), TZ).values_list("item_key", flat=True)
    )


def _keys(filters=None):
    return set(
        library_item_key_queryset(filters or ListFilters(), TZ).values_list("item_key", flat=True)
    )


def _split(recording, transcript, splits, titles, start=0, end=None):
    if end is None:
        end = transcript.segments.count()
    result = save_segmented_version(
        recording.pk, transcript.pk, start, end, list(splits), list(titles)
    )
    return list(
        Section.objects.filter(segmented_version_id=result.version_id).order_by("ordinal")
    )


class TestLibraryAndScopeExclusion:
    def test_recording_item_excluded_from_projection_and_keys(self):
        active, _t, _s = make_transcribed_recording(["active text"], sha="elig-active")
        archived, _t2, _s2 = make_transcribed_recording(["hidden text"], sha="elig-archived")
        archive_recording(archived)
        keys = _keys()
        assert f"r:{active.pk}" in keys
        assert f"r:{archived.pk}" not in keys
        assert f"r:{archived.pk}" not in _items()

    def test_section_items_of_archived_parent_are_excluded(self):
        active, at, _s = make_transcribed_recording(["a", "b", "c"], sha="elig-sec-active")
        active_sections = _split(active, at, [1, 2], ["One", "Two", "Three"])
        archived, xt, _s2 = make_transcribed_recording(["x", "y", "z"], sha="elig-sec-arch")
        archived_sections = _split(archived, xt, [1, 2], ["Ex", "Why", "Zee"])
        archive_recording(archived)

        keys = _keys()
        for section in active_sections:
            assert f"s:{section.pk}" in keys
        for section in archived_sections:
            assert f"s:{section.pk}" not in keys
        assert f"r:{archived.pk}" not in keys

    def test_stale_engine_key_cannot_hydrate_archived_item(self):
        archived, _t, _s = make_transcribed_recording(["hidden"], sha="elig-hydrate")
        # Capture the key while active, then archive.
        assert f"r:{archived.pk}" in _keys()
        archive_recording(archived)
        cards = library_items_by_keys(
            [f"r:{archived.pk}"], ListFilters(), TZ
        )
        assert cards == []

    def test_recording_scope_search_queryset_excludes_archived(self):
        active, _t, _s = make_transcribed_recording(["a"], sha="elig-scope-a")
        archived, _t2, _s2 = make_transcribed_recording(["b"], sha="elig-scope-b")
        archive_recording(archived)
        pks = set(search_scope_queryset(ListFilters(), TZ).values_list("pk", flat=True))
        assert active.pk in pks
        assert archived.pk not in pks

    def test_keyword_item_mode_excludes_archived(self):
        active, _t, _s = make_transcribed_recording(["budget meeting"], sha="elig-kw-a")
        archived, _t2, _s2 = make_transcribed_recording(["budget secret"], sha="elig-kw-b")
        archive_recording(archived)
        si.rebuild_index()
        payload = sq.search_recordings(
            "budget",
            item_scope=library_item_key_queryset(ListFilters(), TZ),
        )
        assert payload["item_mode"] is True
        recording_ids = {r["recording_id"] for r in payload["results"]}
        assert active.pk in recording_ids
        assert archived.pk not in recording_ids


class TestReviewExclusion:
    def test_report_and_badge_exclude_archived(self):
        recording, _t, _s = make_transcribed_recording(
            ["x"], sha="elig-review", summary_status=SummaryState.FAILED
        )
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.FAILED, failure_stage="transcription"
        )
        assert review_badge_count() >= 1
        report = build_review_report()
        assert any(item["recording_id"] == recording.pk for item in report["failed"])

        archive_recording(recording)
        assert review_badge_count() == 0
        report = build_review_report()
        assert all(
            item["recording_id"] != recording.pk
            for group in report.values()
            if isinstance(group, list)
            for item in group
        )


class TestPipelineExclusion:
    def test_work_selectors_skip_archived(self, config, monkeypatch):
        from workflow.services import pipeline
        from workflow.services import summarize as summarize_service

        archived_route = Recording.objects.create(
            sha256="elig-pipe-route", processing_status=ProcessingStatus.ROUTING
        )
        archive_recording(archived_route)
        archived_ready = Recording.objects.create(
            sha256="elig-pipe-ready", processing_status=ProcessingStatus.READY_TO_TRANSCRIBE
        )
        archive_recording(archived_ready)

        def _must_not_run(*args, **kwargs):
            raise AssertionError("archived recordings must not be processed")

        monkeypatch.setattr(pipeline.routing_service, "route_recording", _must_not_run)
        monkeypatch.setattr(pipeline, "transcribe_one", _must_not_run)
        monkeypatch.setattr(summarize_service, "summarize_one", _must_not_run)

        assert pipeline.route_pending(config) == []
        assert pipeline.transcribe_ready(config) == []
        assert summarize_service.summarize_pending(config)["results"] == []

    def test_explicit_mutations_refuse_archived(self, config):
        from workflow.services import pipeline, summarize

        recording = Recording.objects.create(
            sha256="elig-pipe-explicit", processing_status=ProcessingStatus.FAILED
        )
        archive_recording(recording)
        recording.refresh_from_db()

        with pytest.raises(ConfigError):
            pipeline.manual_route(recording, "european")
        with pytest.raises(ConfigError):
            pipeline.confirm_routing(recording)

        assert pipeline.transcribe_one(config, recording)["reason"] == "archived"
        assert pipeline.retry(config, recording)["reason"] == "archived"
        assert pipeline.route_one(config, recording)["reason"] == "archived"
        assert summarize.summarize_one(config, recording)["reason"] == "archived"

    def test_recovery_leaves_archived_untouched(self, config):
        from workflow.services.pipeline import recover_interruptions

        recording = Recording.objects.create(
            sha256="elig-pipe-recover", processing_status=ProcessingStatus.DISCOVERED
        )
        archive_recording(recording)
        recover_interruptions(config)
        recording.refresh_from_db()
        assert recording.archived_at is not None
        assert recording.processing_status == ProcessingStatus.DISCOVERED


class TestStaleInstanceArchiveRace:
    """The locked row is authoritative for archival.

    A caller instance loaded while active must not race an archive
    committed before the service's transaction: the cheap precheck passes
    on the stale instance, so the service must re-check archived on the
    LOCKED re-fetch before any eligibility check, DML or sync scheduling.
    """

    @staticmethod
    def _archive_db_row(recording):
        # Archive the DB row WITHOUT mutating the caller's instance, so it
        # stays a stale active object (the exact race being guarded).
        Recording.objects.filter(pk=recording.pk).update(
            archived_at=timezone.now()
        )

    def test_manual_route_stale_instance_rechecks_under_lock(self, monkeypatch):
        from workflow.services import pipeline

        recording, _t, _s = make_transcribed_recording(["x"], sha="stale-route")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW
        )
        recording.refresh_from_db()  # caller instance: active, needs_review
        assert recording.archived_at is None
        self._archive_db_row(recording)

        sync_calls = []
        monkeypatch.setattr(
            pipeline,
            "schedule_recording_sync",
            lambda *args, **kwargs: sync_calls.append((args, kwargs)),
        )
        with pytest.raises(ArchivedRecordingError) as excinfo:
            pipeline.manual_route(recording, "european")
        assert excinfo.value.code == "recording_archived"
        assert RoutingDecision.objects.filter(recording=recording).count() == 0
        assert sync_calls == []
        # The caller instance itself was never mutated.
        assert recording.archived_at is None

    def test_confirm_routing_stale_instance_rechecks_under_lock(self, monkeypatch):
        from workflow.services import pipeline

        recording, _t, _s = make_transcribed_recording(["x"], sha="stale-confirm")
        decision = RoutingDecision.objects.create(
            recording=recording,
            ordinal=1,
            route_suggestion="european",
            profile_name="european",
            model_id="test-model",
            method=RoutingMethod.AUTOMATIC,
            routing_verified=False,
            is_active=True,
        )
        recording.refresh_from_db()
        assert recording.archived_at is None
        self._archive_db_row(recording)

        sync_calls = []
        monkeypatch.setattr(
            pipeline,
            "schedule_recording_sync",
            lambda *args, **kwargs: sync_calls.append((args, kwargs)),
        )
        with pytest.raises(ArchivedRecordingError) as excinfo:
            pipeline.confirm_routing(recording)
        assert excinfo.value.code == "recording_archived"
        decision.refresh_from_db()
        assert decision.routing_verified is False
        assert decision.verified_at is None
        assert decision.verified_by == ""
        assert RoutingDecision.objects.filter(recording=recording).count() == 1
        assert sync_calls == []


class TestAskAndVariantExclusion:
    def test_ask_passes_archived_excluding_scope(self, config, monkeypatch):
        active, _t, _s = make_transcribed_recording(["a"], sha="elig-ask-a")
        archived, _t2, _s2 = make_transcribed_recording(["b"], sha="elig-ask-b")
        archive_recording(archived)

        captured = {}

        def fake_retrieve(raw, *, scope=None, **kwargs):
            captured["scope"] = scope
            return types.SimpleNamespace(matches=())

        monkeypatch.setattr(
            ask_service.semantic_query, "retrieve_semantic_evidence", fake_retrieve
        )
        from workflow.services.ask import ask_question

        ask_question("anything", config=config, embedder=lambda *a, **k: [])
        pks = set(captured["scope"].values_list("pk", flat=True))
        assert active.pk in pks
        assert archived.pk not in pks

    def test_variant_view_offers_no_action_for_archived(self):
        from workflow.services.variant_view import build_variant_view

        recording, _t, _s = make_transcribed_recording(
            ["x"], sha="elig-variant", summary_status=SummaryState.MISSING
        )
        archive_recording(recording)
        recording.refresh_from_db()
        view = build_variant_view(recording, "default")
        assert view.action_mode is None
        assert view.action_selector is None


class TestExplicitServiceGuards:
    def test_segmentation_save_refuses_archived(self):
        recording, transcript, _s = make_transcribed_recording(
            ["a", "b"], sha="elig-seg"
        )
        archive_recording(recording)
        with pytest.raises(SegmentationError) as excinfo:
            save_segmented_version(
                recording.pk, transcript.pk, 0, 2, [1], ["One", "Two"]
            )
        assert excinfo.value.code == "recording_archived"

    def test_tag_mutation_refuses_archived(self):
        recording, _t, _s = make_transcribed_recording(["a"], sha="elig-tags")
        tag = make_tag("ArchiveTag")
        archive_recording(recording)
        with pytest.raises(TagOperationError) as excinfo:
            add_manual_tag(recording, tag)
        assert excinfo.value.code == "recording_archived"


class TestIngestDedupPreservesArchive:
    def test_rehash_reattaches_same_recording_without_unarchiving(self, config):
        import os
        from pathlib import Path

        from workflow.models import AudioSource
        from workflow.services import ingest as ingest_service

        recording = Recording.objects.create(
            sha256="elig-ingest-sha", processing_status=ProcessingStatus.DISCOVERED
        )
        archive_recording(recording)
        inbox = Path(config.storage.inbox)
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / "rediscovered.wav"
        path.write_bytes(b"RIFF0000WAVE")
        os.utime(path, (1, 1))
        source = AudioSource.objects.create(
            path=str(path),
            path_identity=str(path).casefold(),
            original_filename=path.name,
            file_size=path.stat().st_size,
            file_mtime=path.stat().st_mtime,
        )
        report = ingest_service.IngestReport()
        ingest_service._attach_hashed_source(
            source, "elig-ingest-sha", config, report
        )
        recording.refresh_from_db()
        source.refresh_from_db()
        assert recording.archived_at is not None
        assert source.recording_id == recording.pk
        assert Recording.objects.filter(sha256="elig-ingest-sha").count() == 1

    def test_duplicate_discovery_then_archive_stays_archived(self, config):
        # A fresh hash attaches to a brand-new Recording; archiving after
        # the fact is never undone by a later duplicate attach.
        import os
        from pathlib import Path

        from workflow.models import AudioSource
        from workflow.services import ingest as ingest_service

        recording = Recording.objects.create(
            sha256="elig-ingest-dup", processing_status=ProcessingStatus.DISCOVERED
        )
        archive_recording(recording)
        inbox = Path(config.storage.inbox)
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / "dup.wav"
        path.write_bytes(b"RIFF0000WAVE")
        os.utime(path, (2, 2))
        source = AudioSource.objects.create(
            path=str(path),
            path_identity=str(path).casefold(),
            original_filename=path.name,
            file_size=path.stat().st_size,
            file_mtime=path.stat().st_mtime,
        )
        ingest_service._attach_hashed_source(
            source, "elig-ingest-dup", config, ingest_service.IngestReport()
        )
        assert Recording.objects.filter(sha256="elig-ingest-dup").count() == 1
        assert Recording.objects.get(sha256="elig-ingest-dup").archived_at is not None
