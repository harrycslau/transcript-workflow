"""Step 5B.4 incremental embedding synchronization tests.

Proves (per the approved plan):
- the scheduler runs embedding sync only after commit (rollback schedules
  nothing) and the existing mutation hooks automatically inherit it;
- search reconciliation runs BEFORE embedding; a search failure
  suppresses the unsafe embedding step and stays separately logged;
- no active / incompatible active generation is a normal no-op (zero
  network/DML, no warnings); config is loaded fresh inside the callback
  and a config failure is nonfatal with the fixed aggregate warning;
- missing/stale/invalid scoped rows are re-embedded, exact matching rows
  are skipped, and only the recording's scope is touched;
- removed keys are captured BEFORE the search reconciliation and deleted
  AFTER it (transcript/summary key removal), with no global sweep;
- bounded key pages / client batches, one request per work batch, no
  network inside a DB transaction;
- source change during HTTP, active-generation promotion, dimension
  mismatch, malformed response/vector and DB failures never produce
  false writes, preserve the authoritative/search success, and log only
  the fixed warning with hostile canaries absent;
- partial progress is durable and a later successful callback converges;
  an unattributable prior orphan stays detectable until repair removes
  it;
- concurrent/unlocked callback safety converges; GET purity is
  unchanged; a converged callback is idempotent (zero DML/network).
No real network, MacWhisper, oMLX or real audio anywhere.
"""

from __future__ import annotations

import logging
import threading

import pytest
from django.db import Error as DjangoDBError
from django.db import connection, connections, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from brainlib.config import ConfigError, EmbeddingConfig
from factories import make_config, make_summary_version, make_tag, make_transcribed_recording
from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    EmbeddingDocument,
    EmbeddingGeneration,
    EmbeddingGenerationState,
    ProcessingAttempt,
    Recording,
    SearchDocument,
    Section,
    Transcript,
    TranscriptSegment,
)
from workflow.services import embedding_index as ei
from workflow.services import embedding_sync
from workflow.services import search_index as si
from workflow.services import search_sync
from workflow.services.embedding_client import (
    EmbeddingBatch,
    EmbeddingHTTPError,
    EmbeddingInvalid,
)
from workflow.services.vector_codec import encode_vector

# transaction=True: real commits fire the on_commit callbacks for real
# (with autocommit, no outer transaction), and the 5B.3 rebuild helpers
# refuse to run inside a caller transaction.
pytestmark = pytest.mark.django_db(transaction=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def sync_config(tmp_path, **overrides):
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


def point_config(monkeypatch, config):
    """Make the sync callback load THIS config fresh (module-level name,
    so monkeypatching also proves the callback really loads it)."""
    monkeypatch.setattr(embedding_sync, "load_config", lambda: config)
    return config


def fake_embedder(dim=4, *, tracker=None, fail_calls=(), fill=0.1,
                  guard_no_txn=False, mutate_call=None, dims_by_call=None):
    state = {"calls": 0}

    def embed(config, texts):
        state["calls"] += 1
        if tracker is not None:
            tracker.append(list(texts))
        if guard_no_txn:
            assert connection.in_atomic_block is False, "network inside a transaction"
        if state["calls"] in fail_calls:
            raise EmbeddingHTTPError(503)
        if mutate_call is not None and state["calls"] in mutate_call:
            mutate_call[state["calls"]]()
        call_dim = dim
        if dims_by_call and state["calls"] in dims_by_call:
            call_dim = dims_by_call[state["calls"]]
        return [
            EmbeddingBatch(text=t, embedding=tuple([fill] * call_dim)) for t in texts
        ]

    embed.state = state
    return embed


def active_generation():
    return EmbeddingGeneration.objects.get(state=EmbeddingGenerationState.ACTIVE)


def active_vectors():
    return dict(
        EmbeddingDocument.objects.filter(generation=active_generation()).values_list(
            "document_key", "vector_blob"
        )
    )


def embed_warning_messages(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "workflow.services.embedding_sync"
    ]


def search_warning_messages(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "workflow.services.search_sync"
    ]


def seed_converged(tmp_path, monkeypatch, texts=("seed one", "seed two"), sha="emb-sync-seed"):
    """Recording + healthy search index + healthy active generation."""
    rec, transcript, section = make_transcribed_recording(texts, sha=sha)
    si.rebuild_index()
    config = sync_config(tmp_path)
    ei.rebuild_embedding_index(config, embedder=fake_embedder())
    point_config(monkeypatch, config)
    return rec, transcript, section, config


def schedule_via_atomic(recording_id):
    """Schedule a sync inside a transaction that commits for real."""
    with transaction.atomic():
        search_sync.schedule_recording_sync([recording_id])


# ---------------------------------------------------------------------------
# Scheduler contract: after-commit only, rollback, inherited hooks
# ---------------------------------------------------------------------------


class TestSchedulerContract:
    def test_rollback_schedules_nothing(self, tmp_path, monkeypatch, caplog):
        rec, transcript, _s, config = seed_converged(tmp_path, monkeypatch)
        before = active_vectors()
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", fake_embedder()
        )
        with caplog.at_level(logging.WARNING):
            with transaction.atomic():
                transcript.language_observed = "fi"
                transcript.save(update_fields=["language_observed"])
                search_sync.schedule_recording_sync([rec.pk])
                transaction.set_rollback(True)
        transcript.refresh_from_db()
        assert transcript.language_observed == ""
        assert active_vectors() == before  # nothing embedded
        assert embed_warning_messages(caplog) == []
        assert search_warning_messages(caplog) == []

    def test_mutation_hook_inherits_embedding_sync(self, tmp_path, monkeypatch):
        """A real authoritative hook (unlocked web tag edit) now also
        drives the embedding sync — one commit, one callback."""
        from workflow.services import tags as tags_service

        rec, _t, _s, config = seed_converged(tmp_path, monkeypatch, sha="emb-sync-hook")
        tracker = []
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts",
            fake_embedder(tracker=tracker),
        )
        tag = make_tag("Family")
        tags_service.add_manual_tag(rec, tag)  # real commit fires the callback
        meta = SearchDocument.objects.get(document_key=f"recording:{rec.pk}")
        assert meta.aux_text == "Family"  # search reconciled
        vec = EmbeddingDocument.objects.get(
            generation=active_generation(), document_key=meta.document_key
        )
        assert vec.source_content_hash == meta.content_hash  # embedding synced
        assert ei.build_embedding_status_report(config)["healthy"] is True

    def test_search_runs_before_embedding_and_failure_suppresses(
        self, tmp_path, monkeypatch, caplog
    ):
        rec, _t, _s, config = seed_converged(tmp_path, monkeypatch, sha="emb-sync-order")
        order: list[tuple] = []
        real_reconcile = search_sync.reconcile_recording

        def spy_reconcile(recording_id, **kwargs):
            order.append(("search", str(recording_id)))
            return real_reconcile(recording_id, **kwargs)

        real_embed = embedding_sync.sync_recording_embeddings

        def spy_embed(recording_id, **kwargs):
            order.append(("embed", str(recording_id)))
            return real_embed(recording_id, **kwargs)

        monkeypatch.setattr(search_sync, "reconcile_recording", spy_reconcile)
        monkeypatch.setattr(embedding_sync, "sync_recording_embeddings", spy_embed)
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", fake_embedder()
        )
        schedule_via_atomic(rec.pk)
        assert order == [("search", rec.pk), ("embed", rec.pk)]

        # A search failure suppresses the unsafe embedding step entirely.
        order.clear()
        boom = RuntimeError("CANARY-SEARCH-SECRET")

        def fail_reconcile(recording_id, **kwargs):
            raise boom

        monkeypatch.setattr(search_sync, "reconcile_recording", fail_reconcile)
        with caplog.at_level(logging.WARNING):
            schedule_via_atomic(rec.pk)
        assert order == []
        assert embed_warning_messages(caplog) == []
        assert search_warning_messages(caplog) == [
            "search index post-commit sync failed category=search_index_sync_failed count=1"
        ]
        joined = " ".join(search_warning_messages(caplog))
        assert "CANARY-SEARCH-SECRET" not in joined

    def test_one_recording_embedding_failure_never_blocks_others(
        self, tmp_path, monkeypatch, caplog
    ):
        rec_a, _ta, _sa, config = seed_converged(
            tmp_path, monkeypatch, sha="emb-sync-iso-a"
        )
        rec_b, _tb, _sb, _ = seed_converged(
            tmp_path, monkeypatch, texts=("iso b",), sha="emb-sync-iso-b"
        )
        real_embed = embedding_sync.sync_recording_embeddings

        def selective(recording_id, **kwargs):
            if str(recording_id) == rec_a.pk:
                raise EmbeddingHTTPError(500)
            return real_embed(recording_id, **kwargs)

        monkeypatch.setattr(embedding_sync, "sync_recording_embeddings", selective)
        with caplog.at_level(logging.WARNING):
            with transaction.atomic():
                search_sync.schedule_recording_sync([rec_a.pk, rec_b.pk])
        # Search reconciliation succeeded for BOTH (independent), and the
        # embedding failure of A never stopped B.
        assert SearchDocument.objects.filter(recording=rec_a).count() == 3
        assert SearchDocument.objects.filter(recording=rec_b).count() == 2
        assert embed_warning_messages(caplog) == [
            "embedding index post-commit sync failed "
            "category=embedding_index_sync_failed count=1"
        ]
        assert search_warning_messages(caplog) == []


# ---------------------------------------------------------------------------
# No-op semantics: no active / incompatible active / blank model
# ---------------------------------------------------------------------------


class TestNoOp:
    def test_no_active_generation_noop(self, tmp_path, monkeypatch, caplog):
        rec, _t, _s = make_transcribed_recording(["no active"], sha="emb-sync-noact")
        si.rebuild_index()
        config = sync_config(tmp_path)
        point_config(monkeypatch, config)

        def forbidden(config, texts):
            raise AssertionError("no network without an active generation")

        monkeypatch.setattr("workflow.services.embedding_index.embed_texts", forbidden)
        assert EmbeddingGeneration.objects.count() == 0
        with caplog.at_level(logging.WARNING):
            schedule_via_atomic(rec.pk)
        assert EmbeddingDocument.objects.count() == 0
        assert embed_warning_messages(caplog) == []
        assert search_warning_messages(caplog) == []

    def test_incompatible_active_generation_noop(self, tmp_path, monkeypatch, caplog):
        rec, _t, _s, config = seed_converged(tmp_path, monkeypatch, sha="emb-sync-incomp")
        EmbeddingGeneration.objects.filter(state=EmbeddingGenerationState.ACTIVE).update(
            model="other-model"
        )
        before = active_vectors()

        def forbidden(config, texts):
            raise AssertionError("incompatible active generation must not embed")

        monkeypatch.setattr("workflow.services.embedding_index.embed_texts", forbidden)
        with caplog.at_level(logging.WARNING):
            schedule_via_atomic(rec.pk)
        assert active_vectors() == before
        assert embed_warning_messages(caplog) == []
        assert search_warning_messages(caplog) == []

    def test_blank_config_model_noop(self, tmp_path, monkeypatch, caplog):
        rec, _t, _s, _config = seed_converged(tmp_path, monkeypatch, sha="emb-sync-blank")
        blank = sync_config(tmp_path, model="   ")
        point_config(monkeypatch, blank)
        before = active_vectors()

        def forbidden(config, texts):
            raise AssertionError("blank model must not embed")

        monkeypatch.setattr("workflow.services.embedding_index.embed_texts", forbidden)
        with caplog.at_level(logging.WARNING):
            schedule_via_atomic(rec.pk)
        assert active_vectors() == before
        assert embed_warning_messages(caplog) == []
        assert search_warning_messages(caplog) == []


# ---------------------------------------------------------------------------
# Fresh config load in the callback; config failure nonfatal
# ---------------------------------------------------------------------------


class TestConfigLoad:
    def test_config_loaded_fresh_in_callback(self, tmp_path, monkeypatch, caplog):
        rec, _t, _s, config = seed_converged(tmp_path, monkeypatch, sha="emb-sync-fresh")
        calls: list[str] = []
        real = embedding_sync.load_config

        def spy():
            calls.append("loaded")
            return real()

        monkeypatch.setattr(embedding_sync, "load_config", spy)
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", fake_embedder()
        )
        # Make real work: the metadata doc changes via a tag edit hook.
        from workflow.services import tags as tags_service

        tag = make_tag("Fresh")
        tags_service.add_manual_tag(rec, tag)
        assert calls == ["loaded"]
        meta = SearchDocument.objects.get(document_key=f"recording:{rec.pk}")
        vec = EmbeddingDocument.objects.get(
            generation=active_generation(), document_key=meta.document_key
        )
        assert vec.source_content_hash == meta.content_hash
        assert embed_warning_messages(caplog) == []

    def test_config_failure_nonfatal_fixed_warning(self, tmp_path, monkeypatch, caplog):
        rec, _t, _s, _config = seed_converged(tmp_path, monkeypatch, sha="emb-sync-cfgfail")

        def boom():
            raise ConfigError("CANARY-CONFIG-SECRET path=/secret/config.yaml")

        monkeypatch.setattr(embedding_sync, "load_config", boom)
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", fake_embedder()
        )
        # The authoritative change still commits and the search reconcile
        # still runs; only the embedding step fails, nonfatally.
        with caplog.at_level(logging.WARNING):
            with transaction.atomic():
                make_summary_version(rec, _t, _s, title="ConfigFail Title")
                search_sync.schedule_recording_sync([rec.pk])
        assert SearchDocument.objects.get(
            document_key=f"recording:{rec.pk}"
        ).title_text == "ConfigFail Title"
        messages = embed_warning_messages(caplog)
        assert messages == [
            "embedding index post-commit sync failed "
            "category=embedding_index_sync_failed count=1"
        ]
        joined = " ".join(messages)
        assert "CANARY-CONFIG-SECRET" not in joined
        assert "/secret/config.yaml" not in joined
        assert search_warning_messages(caplog) == []

    def test_snapshot_capture_failure_still_reconciles_search(
        self, tmp_path, monkeypatch, caplog
    ):
        rec, _t, _s, config = seed_converged(tmp_path, monkeypatch, sha="emb-sync-snapfail")

        def boom(*args, **kwargs):
            raise RuntimeError("CANARY-SNAPSHOT-SECRET")

        monkeypatch.setattr(embedding_sync, "_embedding_schema_present", boom)
        with caplog.at_level(logging.WARNING):
            with transaction.atomic():
                make_summary_version(rec, _t, _s, title="SnapFail Title")
                search_sync.schedule_recording_sync([rec.pk])
        # Search reconciliation proceeded despite the snapshot failure…
        assert SearchDocument.objects.get(
            document_key=f"recording:{rec.pk}"
        ).title_text == "SnapFail Title"
        # …and the embedding failure was counted with the fixed warning.
        messages = embed_warning_messages(caplog)
        assert messages == [
            "embedding index post-commit sync failed "
            "category=embedding_index_sync_failed count=1"
        ]
        assert "CANARY-SNAPSHOT-SECRET" not in " ".join(messages)


# ---------------------------------------------------------------------------
# Classification: missing / stale / invalid / skipped, recording scope
# ---------------------------------------------------------------------------


class TestClassification:
    def test_missing_stale_invalid_reembedded_only_recording_scope(self, tmp_path, monkeypatch):
        rec_a, _ta, _sa, config = seed_converged(
            tmp_path, monkeypatch, texts=("a one",), sha="emb-sync-cls-a"
        )
        rec_b, trans_b, _sb, _ = seed_converged(
            tmp_path, monkeypatch, texts=("b one",), sha="emb-sync-cls-b"
        )
        b_keys = set(
            SearchDocument.objects.filter(recording=rec_b).values_list(
                "document_key", flat=True
            )
        )
        b_before = {k: v for k, v in active_vectors().items() if k in b_keys}

        # missing: a brand new recording C is embedded scoped to itself.
        rec_c, _tc, _sc = make_transcribed_recording(["c one"], sha="emb-sync-cls-c")
        si.rebuild_index()
        tracker: list[list[str]] = []
        assert embedding_sync.capture_removed_key_snapshot(rec_c.pk) is True
        counts = embedding_sync.sync_recording_embeddings(
            rec_c.pk, embedder=fake_embedder(tracker=tracker)
        )
        assert counts["embedded"] == 2  # segment + metadata
        c_keys = set(SearchDocument.objects.filter(recording=rec_c).values_list(
            "document_key", flat=True
        ))
        assert {t for batch in tracker for t in batch} == {
            ei.prepare_document_text(
                SearchDocument.objects.get(document_key=k)
            )
            for k in c_keys
        }

        # stale: A's segment text edited; only that row re-embedded.
        TranscriptSegment.objects.filter(transcript=_ta, ordinal=0).update(
            text="a one EDITED"
        )
        si.rebuild_index()
        tracker.clear()
        assert embedding_sync.capture_removed_key_snapshot(rec_a.pk) is True
        counts = embedding_sync.sync_recording_embeddings(
            rec_a.pk, embedder=fake_embedder(tracker=tracker)
        )
        assert counts["embedded"] == 1
        seg_key = f"segment:{_ta.pk}:0"
        assert ei.prepare_document_text(
            SearchDocument.objects.get(document_key=seg_key)
        ) in [t for batch in tracker for t in batch]
        # A's metadata row was EXACTLY matching: skipped, not embedded.
        assert counts["skipped"] == 1

        # invalid: corrupt A's metadata vector blob; re-embedded.
        gen = active_generation()
        victim = EmbeddingDocument.objects.get(
            generation=gen, document_key=f"recording:{rec_a.pk}"
        )
        EmbeddingDocument.objects.filter(pk=victim.pk).update(vector_blob=b"\x00\x00")
        tracker.clear()
        assert embedding_sync.capture_removed_key_snapshot(rec_a.pk) is True
        counts = embedding_sync.sync_recording_embeddings(
            rec_a.pk, embedder=fake_embedder(tracker=tracker)
        )
        assert counts["embedded"] == 1
        assert counts["skipped"] == 1
        victim.refresh_from_db()
        assert victim.vector_blob == ei.encode_vector(
            [0.1] * gen.dimensions, dimensions=gen.dimensions
        )

        # B's vectors were never touched.
        b_after = {k: v for k, v in active_vectors().items() if k in b_keys}
        assert b_after == b_before

    def test_exact_matching_rows_skipped_zero_dml_network(self, tmp_path, monkeypatch):
        rec, _t, _s, _config = seed_converged(tmp_path, monkeypatch, sha="emb-sync-skip")

        def forbidden(config, texts):
            raise AssertionError("converged recording must not embed")

        assert embedding_sync.capture_removed_key_snapshot(rec.pk) is True
        with CaptureQueriesContext(connection) as ctx:
            counts = embedding_sync.sync_recording_embeddings(rec.pk, embedder=forbidden)
        writes = [
            q["sql"] for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            and "workflow_embedding" in q["sql"]
        ]
        assert writes == [], writes
        expected = SearchDocument.objects.filter(recording=rec).count()
        assert counts["skipped"] == expected
        assert counts["embedded"] == 0
        assert counts["deleted_removed"] == 0
        assert counts["batches"] == 0

    def test_converged_callback_zero_dml_network(self, tmp_path, monkeypatch):
        rec, _t, _s, _config = seed_converged(tmp_path, monkeypatch, sha="emb-sync-conv")

        def forbidden(config, texts):
            raise AssertionError("converged callback must not embed")

        monkeypatch.setattr("workflow.services.embedding_index.embed_texts", forbidden)
        with CaptureQueriesContext(connection) as ctx:
            schedule_via_atomic(rec.pk)
        writes = [
            q["sql"] for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            and "workflow_embedding" in q["sql"]
        ]
        assert writes == [], writes


# ---------------------------------------------------------------------------
# Removed-key attribution: captured before search, deleted after
# ---------------------------------------------------------------------------


class TestRemovedKeys:
    def test_summary_supersession_removes_old_vector(self, tmp_path, monkeypatch):
        rec, transcript, section = make_transcribed_recording(
            ["summary flip"], sha="emb-sync-sum"
        )
        first = make_summary_version(rec, transcript, section, title="V1")
        si.rebuild_index()
        config = sync_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=fake_embedder())
        point_config(monkeypatch, config)
        gen = active_generation()
        assert EmbeddingDocument.objects.filter(
            generation=gen, document_key=f"summary:{first.pk}"
        ).exists()
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", fake_embedder()
        )
        with transaction.atomic():
            first.is_active = False
            first.superseded_at = timezone.now()
            first.save(update_fields=["is_active", "superseded_at"])
            second = make_summary_version(rec, transcript, section, title="V2")
            search_sync.schedule_recording_sync([rec.pk])
        # The old summary SearchDocument row was removed by the search
        # reconciliation; its vector was deleted via the pre-reconcile
        # snapshot (no global sweep).
        assert not EmbeddingDocument.objects.filter(
            generation=gen, document_key=f"summary:{first.pk}"
        ).exists()
        assert EmbeddingDocument.objects.filter(
            generation=gen, document_key=f"summary:{second.pk}"
        ).exists()
        assert ei.build_embedding_status_report(config)["healthy"] is True

    def test_transcript_reactivation_removes_old_vectors(self, tmp_path, monkeypatch):
        rec, old_transcript, old_section = make_transcribed_recording(
            ["old one", "old two"], sha="emb-sync-txn"
        )
        old_summary = make_summary_version(rec, old_transcript, old_section, title="Old")
        si.rebuild_index()
        config = sync_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=fake_embedder())
        point_config(monkeypatch, config)
        gen = active_generation()
        old_keys = [
            f"segment:{old_transcript.pk}:0",
            f"segment:{old_transcript.pk}:1",
            f"summary:{old_summary.pk}",
        ]
        assert all(
            EmbeddingDocument.objects.filter(
                generation=gen, document_key=k
            ).exists()
            for k in old_keys
        )
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", fake_embedder()
        )
        with transaction.atomic():
            new_attempt = ProcessingAttempt.objects.create(
                recording=rec,
                stage=AttemptStage.TRANSCRIPTION,
                ordinal=2,
                outcome=AttemptOutcome.SUCCESS,
                finished_at=timezone.now(),
            )
            new_transcript = Transcript.objects.create(
                recording=rec, attempt=new_attempt, text_normalized="new text"
            )
            TranscriptSegment.objects.create(
                transcript=new_transcript, ordinal=0, start_ms=0, end_ms=900,
                text="new segment",
            )
            Section.objects.create(transcript=new_transcript, ordinal=0, title="Full")
            old_transcript.is_active = False
            old_transcript.save(update_fields=["is_active"])
            new_transcript.is_active = True
            new_transcript.save(update_fields=["is_active"])
            search_sync.schedule_recording_sync([rec.pk])
        assert not EmbeddingDocument.objects.filter(
            generation=gen, document_key__in=old_keys
        ).exists()
        assert EmbeddingDocument.objects.filter(
            generation=gen, document_key=f"segment:{new_transcript.pk}:0"
        ).exists()
        assert ei.build_embedding_status_report(config)["healthy"] is True

    def test_no_global_sweep_ghost_survives_until_repair(self, tmp_path, monkeypatch):
        from workflow.services.vector_codec import encode_vector

        rec, _t, _s, config = seed_converged(tmp_path, monkeypatch, sha="emb-sync-ghost")
        gen = active_generation()
        ghost_key = "segment:ghost-sync:0"
        EmbeddingDocument.objects.create(
            generation=gen,
            document_key=ghost_key,
            source_content_hash="a" * 64,
            vector_blob=encode_vector([0.1] * gen.dimensions, dimensions=gen.dimensions),
        )
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", fake_embedder()
        )
        schedule_via_atomic(rec.pk)
        # The ghost is not in the pre-reconcile snapshot and not in the
        # current SearchDocuments: a per-recording sync must NOT remove it.
        assert EmbeddingDocument.objects.filter(
            generation=gen, document_key=ghost_key
        ).exists()
        report = ei.build_embedding_status_report(config)
        assert report["healthy"] is False
        assert report["categories"]["orphan_document"] == 1
        # Explicit repair removes it (no global callback sweep needed).
        assert ei.repair_embedding_index(config, embedder=fake_embedder())["healthy"] is True
        assert not EmbeddingDocument.objects.filter(
            generation=gen, document_key=ghost_key
        ).exists()


# ---------------------------------------------------------------------------
# Bounded pages / batches / no network in transaction
# ---------------------------------------------------------------------------


class TestBounded:
    def test_pages_bounded_one_request_per_work_batch_no_network_in_txn(
        self, tmp_path, monkeypatch
    ):
        # A converged active generation exists first (batch_size 4).
        seed_converged(tmp_path, monkeypatch, texts=("base",), sha="emb-sync-bound-base")
        rec, _t, _s = make_transcribed_recording(
            [f"bound {i}" for i in range(12)], sha="emb-sync-bound"
        )
        si.rebuild_index()
        config = sync_config(tmp_path, batch_size=4)
        point_config(monkeypatch, config)
        tracker: list[list[str]] = []
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts",
            fake_embedder(tracker=tracker, guard_no_txn=True),
        )
        schedule_via_atomic(rec.pk)
        # 13 documents (12 segments + 1 metadata), pages of 4 => 4 calls.
        assert [len(batch) for batch in tracker] == [4, 4, 4, 1], tracker
        assert sum(len(b) for b in tracker) == 13
        active = active_generation()
        assert (
            EmbeddingDocument.objects.filter(generation=active).count()
            == SearchDocument.objects.count()
        )

    def test_removed_key_deletion_pages_bounded(self, tmp_path, monkeypatch):
        rec, old_transcript, old_section = make_transcribed_recording(
            [f"page {i}" for i in range(10)], sha="emb-sync-dpage"
        )
        si.rebuild_index()
        config = sync_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=fake_embedder())
        point_config(monkeypatch, config)
        old_keys = [
            f"segment:{old_transcript.pk}:{i}" for i in range(10)
        ]
        # bound the deletion pages and spy on their sizes
        sizes: list[int] = []
        real_delete_page = embedding_sync._delete_removed_key_page

        def spy_delete_page(active, keys, **kwargs):
            sizes.append(len(keys))
            return real_delete_page(active, keys, **kwargs)

        monkeypatch.setattr(embedding_sync, "_REMOVED_PAGE_SIZE", 4)
        monkeypatch.setattr(embedding_sync, "_delete_removed_key_page", spy_delete_page)
        with transaction.atomic():
            old_transcript.is_active = False
            old_transcript.save(update_fields=["is_active"])
            search_sync.schedule_recording_sync([rec.pk])
        assert sizes and max(sizes) <= 4, sizes
        assert not EmbeddingDocument.objects.filter(
            generation=active_generation(), document_key__in=old_keys
        ).exists()


# ---------------------------------------------------------------------------
# Failure safety: no false writes, authoritative/search preserved, canaries absent
# ---------------------------------------------------------------------------


class TestFailureSafety:
    def _fresh_stale_work(self, tmp_path, monkeypatch, sha):
        """Recording with a stale segment (edited + search rebuilt)."""
        rec, transcript, _s = make_transcribed_recording(
            ["UNIQUE-SYNC-ROW"], sha=sha
        )
        si.rebuild_index()
        config = sync_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=fake_embedder())
        point_config(monkeypatch, config)
        TranscriptSegment.objects.filter(transcript=transcript, ordinal=0).update(
            text="UNIQUE-SYNC-ROW EDITED"
        )
        si.rebuild_index()
        return rec, transcript, config

    def test_source_change_during_http_no_false_write(self, tmp_path, monkeypatch, caplog):
        rec, transcript, config = self._fresh_stale_work(
            tmp_path, monkeypatch, sha="emb-sync-race"
        )
        key = f"segment:{transcript.pk}:0"
        old_blob = EmbeddingDocument.objects.get(
            generation=active_generation(), document_key=key
        ).vector_blob

        def mutate_source():
            SearchDocument.objects.filter(document_key=key).update(
                content_hash="d" * 64
            )

        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts",
            fake_embedder(mutate_call={1: mutate_source}),
        )
        with caplog.at_level(logging.WARNING):
            schedule_via_atomic(rec.pk)
        # No false provenance: the row kept its old blob.
        assert (
            EmbeddingDocument.objects.get(
                generation=active_generation(), document_key=key
            ).vector_blob
            == old_blob
        )
        messages = embed_warning_messages(caplog)
        assert messages == [
            "embedding index post-commit sync failed "
            "category=embedding_index_sync_failed count=1"
        ]
        assert "changed" not in " ".join(messages)  # no internal detail either

    def test_active_promotion_during_http_no_false_write(self, tmp_path, monkeypatch, caplog):
        rec, transcript, config = self._fresh_stale_work(
            tmp_path, monkeypatch, sha="emb-sync-promo"
        )
        old_gen = active_generation()
        key = f"segment:{transcript.pk}:0"
        old_blob = EmbeddingDocument.objects.get(generation=old_gen, document_key=key).vector_blob

        def promote():
            now = timezone.now()
            old_gen.state = EmbeddingGenerationState.SUPERSEDED
            old_gen.superseded_at = now
            old_gen.save()
            EmbeddingGeneration.objects.create(
                model=config.embedding.model,
                dimensions=old_gen.dimensions,
                embedding_version=ei.EMBEDDING_VERSION,
                source_index_version=si.INDEX_VERSION,
                state=EmbeddingGenerationState.ACTIVE,
                completed_at=now,
                activated_at=now,
            )

        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts",
            fake_embedder(mutate_call={1: promote}),
        )
        with caplog.at_level(logging.WARNING):
            schedule_via_atomic(rec.pk)
        # The write was rejected against the superseded generation.
        assert (
            EmbeddingDocument.objects.get(generation=old_gen, document_key=key).vector_blob
            == old_blob
        )
        assert embed_warning_messages(caplog) == [
            "embedding index post-commit sync failed "
            "category=embedding_index_sync_failed count=1"
        ]

    def test_dimension_mismatch_no_false_write(self, tmp_path, monkeypatch, caplog):
        rec, transcript, config = self._fresh_stale_work(
            tmp_path, monkeypatch, sha="emb-sync-dimm"
        )
        gen = active_generation()
        before = dict(
            EmbeddingDocument.objects.filter(generation=gen).values_list(
                "document_key", "vector_blob"
            )
        )
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts",
            fake_embedder(dim=9),
        )
        with caplog.at_level(logging.WARNING):
            schedule_via_atomic(rec.pk)
        after = dict(
            EmbeddingDocument.objects.filter(generation=gen).values_list(
                "document_key", "vector_blob"
            )
        )
        assert after == before
        assert embed_warning_messages(caplog) == [
            "embedding index post-commit sync failed "
            "category=embedding_index_sync_failed count=1"
        ]

    def test_malformed_response_no_false_write(self, tmp_path, monkeypatch, caplog):
        rec, transcript, config = self._fresh_stale_work(
            tmp_path, monkeypatch, sha="emb-sync-malformed"
        )
        gen = active_generation()
        before = dict(
            EmbeddingDocument.objects.filter(generation=gen).values_list(
                "document_key", "vector_blob"
            )
        )

        def malformed(config, texts):
            # wrong text: pairing validation must reject before any write
            return [
                EmbeddingBatch(text="CANARY-WRONG-TEXT", embedding=(0.1, 0.1, 0.1, 0.1))
                for _ in texts
            ]

        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", malformed
        )
        with caplog.at_level(logging.WARNING):
            schedule_via_atomic(rec.pk)
        assert dict(
            EmbeddingDocument.objects.filter(generation=gen).values_list(
                "document_key", "vector_blob"
            )
        ) == before
        messages = embed_warning_messages(caplog)
        assert messages == [
            "embedding index post-commit sync failed "
            "category=embedding_index_sync_failed count=1"
        ]
        assert "CANARY-WRONG-TEXT" not in " ".join(messages)

    def test_embedding_error_sanitized_warning(self, tmp_path, monkeypatch, caplog):
        rec, transcript, config = self._fresh_stale_work(
            tmp_path, monkeypatch, sha="emb-sync-err"
        )

        def raise_invalid(config, texts):
            raise EmbeddingInvalid("invalid_vector", "CANARY-VECTOR-SECRET")

        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", raise_invalid
        )
        with caplog.at_level(logging.WARNING):
            schedule_via_atomic(rec.pk)
        messages = embed_warning_messages(caplog)
        assert messages == [
            "embedding index post-commit sync failed "
            "category=embedding_index_sync_failed count=1"
        ]
        assert "CANARY-VECTOR-SECRET" not in " ".join(messages)

    def test_db_failure_sanitized_warning(self, tmp_path, monkeypatch, caplog):
        rec, transcript, config = self._fresh_stale_work(
            tmp_path, monkeypatch, sha="emb-sync-dbfail"
        )
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", fake_embedder()
        )

        def boom(*args, **kwargs):
            raise DjangoDBError("CANARY-DB-SECRET sql=secret")

        monkeypatch.setattr(embedding_sync, "_persist_repair_batch", boom)
        with caplog.at_level(logging.WARNING):
            schedule_via_atomic(rec.pk)
        messages = embed_warning_messages(caplog)
        assert messages == [
            "embedding index post-commit sync failed "
            "category=embedding_index_sync_failed count=1"
        ]
        joined = " ".join(messages)
        assert "CANARY-DB-SECRET" not in joined
        assert "secret" not in joined
        assert search_warning_messages(caplog) == []


# ---------------------------------------------------------------------------
# Durable partial progress; later successful callback converges
# ---------------------------------------------------------------------------


class TestDurability:
    def test_partial_progress_durable_then_later_converges(self, tmp_path, monkeypatch, caplog):
        rec, _t, _s = make_transcribed_recording(
            [f"dur {i}" for i in range(4)], sha="emb-sync-dur"
        )
        si.rebuild_index()
        config = sync_config(tmp_path, batch_size=2)
        ei.rebuild_embedding_index(config, embedder=fake_embedder())
        point_config(monkeypatch, config)
        gen = active_generation()
        # make all 4 segment rows stale (metadata stays unchanged)
        for i, seg in enumerate(TranscriptSegment.objects.filter(transcript__recording=rec)):
            TranscriptSegment.objects.filter(pk=seg.pk).update(text=f"dur {i} EDITED")
        si.rebuild_index()
        before = dict(
            EmbeddingDocument.objects.filter(generation=gen).values_list(
                "document_key", "vector_blob"
            )
        )
        # batch_size=2; page 1 ([metadata, seg0]) succeeds, page 2
        # ([seg1, seg2]) fails -> durable progress then failure.
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts",
            fake_embedder(fail_calls=(2,), fill=0.9),
        )
        with caplog.at_level(logging.WARNING):
            schedule_via_atomic(rec.pk)
        after_fail = dict(
            EmbeddingDocument.objects.filter(generation=gen).values_list(
                "document_key", "vector_blob"
            )
        )
        changed = [k for k in before if k in after_fail and after_fail[k] != before[k]]
        assert len(changed) == 1, changed  # exactly the durable first batch
        assert embed_warning_messages(caplog) == [
            "embedding index post-commit sync failed "
            "category=embedding_index_sync_failed count=1"
        ]
        # A later successful callback converges everything.
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", fake_embedder(fill=0.9)
        )
        schedule_via_atomic(rec.pk)
        assert ei.build_embedding_status_report(config)["healthy"] is True


# ---------------------------------------------------------------------------
# Concurrency: unlocked callbacks rely on SQLite serialization
# ---------------------------------------------------------------------------


class TestConcurrent:
    def test_unlocked_tag_service_race_converges_with_embeddings(self, tmp_path, monkeypatch):
        from workflow.models import TagAssignment
        from workflow.services import tags as tags_service

        rec, _t, _s, config = seed_converged(
            tmp_path, monkeypatch, sha="emb-sync-race"
        )
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", fake_embedder()
        )
        tag_x = make_tag("Alpha")
        tag_y = make_tag("Beta")
        errors: list[BaseException] = []

        def worker(tag):
            try:
                tags_service.add_manual_tag(rec, tag)
            except BaseException as exc:  # pragma: no cover - only on bugs
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(tag,)) for tag in (tag_x, tag_y)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert errors == []
        assert TagAssignment.objects.filter(recording=rec, is_active=True).count() == 2
        # Even if a concurrent sync lost the SQLite write race (a
        # nonfatal, logged, swallowed failure), a final sync converges.
        schedule_via_atomic(rec.pk)
        meta = SearchDocument.objects.get(document_key=f"recording:{rec.pk}")
        assert sorted(meta.aux_text.splitlines()) == ["Alpha", "Beta"]
        vec = EmbeddingDocument.objects.get(
            generation=active_generation(), document_key=meta.document_key
        )
        assert vec.source_content_hash == meta.content_hash
        assert ei.build_embedding_status_report(config)["healthy"] is True


# ---------------------------------------------------------------------------
# GET purity stays unchanged
# ---------------------------------------------------------------------------


class TestGetPurity:
    def test_get_purity_unchanged(self, tmp_path, monkeypatch, client):
        rec, _t, _s, _config = seed_converged(tmp_path, monkeypatch, sha="emb-sync-get")

        def forbidden_worker(*args, **kwargs):
            raise AssertionError("embedding sync must never run on GET")

        def forbidden_embed(config, texts):
            raise AssertionError("embedding network must never run on GET")

        monkeypatch.setattr(embedding_sync, "sync_recording_embeddings", forbidden_worker)
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", forbidden_embed
        )
        assert client.get(f"/recordings/{rec.pk}/").status_code == 200
        with CaptureQueriesContext(connection) as ctx:
            assert client.get("/recordings/").status_code == 200
        writes = [
            q["sql"] for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            and "workflow_embedding" in q["sql"]
        ]
        assert writes == []


# ---------------------------------------------------------------------------
# capture_removed_key_snapshot public boundary: sanitized direct errors
# ---------------------------------------------------------------------------


class TestCaptureBoundary:
    def test_genuine_schema_absence_raises_schema_missing(self, tmp_path, monkeypatch):
        rec, _t, _s = make_transcribed_recording(["cap schema"], sha="emb-sync-cap-schema")
        monkeypatch.setattr(
            embedding_sync, "_embedding_schema_present", lambda **kw: False
        )
        with pytest.raises(ei.EmbeddingIndexError, match="schema is missing"):
            embedding_sync.capture_removed_key_snapshot(rec.pk)

    def test_introspection_db_error_sanitized(self, tmp_path, monkeypatch):
        rec, _t, _s = make_transcribed_recording(["cap db"], sha="emb-sync-cap-db")

        def boom(**kw):
            raise DjangoDBError("CANARY-CAPTURE-DB-SECRET sql=secret")

        monkeypatch.setattr(embedding_sync, "_embedding_schema_present", boom)
        with pytest.raises(
            ei.EmbeddingIndexError, match="database operation failed"
        ) as excinfo:
            embedding_sync.capture_removed_key_snapshot(rec.pk)
        text = str(excinfo.value)
        assert "CANARY-CAPTURE-DB-SECRET" not in text
        assert "secret" not in text

    def test_unexpected_error_sanitized(self, tmp_path, monkeypatch):
        rec, _t, _s = make_transcribed_recording(
            ["cap unexpected"], sha="emb-sync-cap-unexp"
        )

        def boom(**kw):
            raise RuntimeError("CANARY-CAPTURE-UNEXPECTED-SECRET")

        monkeypatch.setattr(embedding_sync, "_embedding_schema_present", boom)
        with pytest.raises(
            ei.EmbeddingIndexError, match="failed unexpectedly"
        ) as excinfo:
            embedding_sync.capture_removed_key_snapshot(rec.pk)
        assert "CANARY-CAPTURE-UNEXPECTED-SECRET" not in str(excinfo.value)

    def test_cursor_ddl_error_sanitized(self, tmp_path, monkeypatch):
        rec, _t, _s, _config = seed_converged(
            tmp_path, monkeypatch, sha="emb-sync-cap-ddl"
        )
        monkeypatch.setattr(embedding_sync, "_TEMP_CREATE_SQL", "BROKEN SQL HERE")
        with pytest.raises(
            ei.EmbeddingIndexError, match="database operation failed"
        ) as excinfo:
            embedding_sync.capture_removed_key_snapshot(rec.pk)
        assert "BROKEN" not in str(excinfo.value)

    def test_base_exception_propagates(self, tmp_path, monkeypatch):
        class _Fatal(BaseException):
            pass

        rec, _t, _s = make_transcribed_recording(["cap fatal"], sha="emb-sync-cap-fatal")

        def boom(**kw):
            raise _Fatal("must propagate, never sanitized")

        monkeypatch.setattr(embedding_sync, "_embedding_schema_present", boom)
        with pytest.raises(_Fatal):
            embedding_sync.capture_removed_key_snapshot(rec.pk)


# ---------------------------------------------------------------------------
# sync_recording_embeddings in-transaction precondition (shared 5B.3)
# ---------------------------------------------------------------------------


class TestInAtomicPrecondition:
    def test_direct_invocation_inside_transaction_rejected(self, tmp_path, monkeypatch):
        rec, transcript, _s, _config = seed_converged(
            tmp_path, monkeypatch, sha="emb-sync-inatomic"
        )
        # make real work exist (a stale segment) so HTTP would be attempted
        TranscriptSegment.objects.filter(transcript=transcript, ordinal=0).update(
            text="emb-sync-inatomic EDITED"
        )
        si.rebuild_index()
        assert embedding_sync.capture_removed_key_snapshot(rec.pk) is True

        def forbidden(config, texts):
            raise AssertionError("no embedder call inside a caller transaction")

        with transaction.atomic():
            with pytest.raises(
                ei.EmbeddingIndexError, match="database transaction"
            ) as excinfo:
                embedding_sync.sync_recording_embeddings(rec.pk, embedder=forbidden)
        assert "database transaction" in str(excinfo.value)
        # nothing was written: the stale row still carries the old hash
        gen = active_generation()
        seg = EmbeddingDocument.objects.get(
            generation=gen, document_key=f"segment:{transcript.pk}:0"
        )
        current = SearchDocument.objects.get(document_key=f"segment:{transcript.pk}:0")
        assert seg.source_content_hash != current.content_hash  # still stale


# ---------------------------------------------------------------------------
# Removed-key deletion reports ACTUAL rows deleted
# ---------------------------------------------------------------------------


class TestRemovedKeyCounts:
    def _ghost_doc(self, rec, transcript, key, ordinal):
        return SearchDocument.objects.create(
            document_key=key,
            doc_type="segment",
            recording=rec,
            transcript=transcript,
            segment_ordinal=ordinal,
            start_ms=0,
            end_ms=1,
            output_language="",
            title_text="",
            body_text="ghost body",
            aux_text="",
            content_hash="a" * 64,
            index_version=si.INDEX_VERSION,
        )

    def test_removed_snapshot_key_without_vector_not_counted(self, tmp_path, monkeypatch):
        rec, transcript, _s, _config = seed_converged(
            tmp_path, monkeypatch, sha="emb-sync-remcnt"
        )
        gen = active_generation()
        # two keys the search reconciliation would remove: one WITHOUT a
        # vector and one WITH a vector
        ghost_no_vec = self._ghost_doc(rec, transcript, "segment:ghost-novec:0", 9000)
        ghost_with_vec = self._ghost_doc(rec, transcript, "segment:ghost-vec:1", 9001)
        EmbeddingDocument.objects.create(
            generation=gen,
            document_key=ghost_with_vec.document_key,
            source_content_hash=ghost_with_vec.content_hash,
            vector_blob=encode_vector([0.1] * gen.dimensions, dimensions=gen.dimensions),
        )
        before_real = {
            k: v
            for k, v in active_vectors().items()
            if not k.startswith("segment:ghost")
        }
        assert embedding_sync.capture_removed_key_snapshot(rec.pk) is True
        # simulate the search reconciliation removing both ghost keys
        SearchDocument.objects.filter(pk__in=[ghost_no_vec.pk, ghost_with_vec.pk]).delete()

        def forbidden(config, texts):
            raise AssertionError("no embedding work for a converged recording")

        counts = embedding_sync.sync_recording_embeddings(rec.pk, embedder=forbidden)
        # ONLY the with-vector row was actually deleted
        assert counts["deleted_removed"] == 1
        assert not EmbeddingDocument.objects.filter(
            generation=gen, document_key=ghost_with_vec.document_key
        ).exists()
        after_real = {
            k: v
            for k, v in active_vectors().items()
            if not k.startswith("segment:ghost")
        }
        assert after_real == before_real

    def test_delete_removed_page_db_error_sanitized(self, tmp_path, monkeypatch):
        rec, _t, _s, _config = seed_converged(
            tmp_path, monkeypatch, sha="emb-sync-remdb"
        )
        gen = active_generation()

        def boom(*args, **kwargs):
            raise DjangoDBError("CANARY-DELETE-DB-SECRET sql=secret")

        monkeypatch.setattr(embedding_sync, "_active_is_compatible", boom)
        with pytest.raises(
            ei.EmbeddingIndexError, match="database operation failed"
        ) as excinfo:
            embedding_sync._delete_removed_key_page(
                gen, ["segment:ghost-del:0"], using="default"
            )
        text = str(excinfo.value)
        assert "CANARY-DELETE-DB-SECRET" not in text
        assert "secret" not in text


# ---------------------------------------------------------------------------
# Multi-recording single callback: TEMP snapshot isolation per recording
# ---------------------------------------------------------------------------


class TestMultiRecordingCallback:
    def test_temppable_cleared_per_recording_and_failure_counted_per_recording(
        self, tmp_path, monkeypatch, caplog
    ):
        rec_a, ta, sa = make_transcribed_recording(["multi a"], sha="emb-sync-multi-a")
        sum_a1 = make_summary_version(rec_a, ta, sa, title="A V1")
        rec_b, tb, sb = make_transcribed_recording(["multi b"], sha="emb-sync-multi-b")
        sum_b1 = make_summary_version(rec_b, tb, sb, title="B V1")
        si.rebuild_index()
        config = sync_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=fake_embedder())
        point_config(monkeypatch, config)
        gen = active_generation()
        b_meta_old = EmbeddingDocument.objects.get(
            generation=gen, document_key=f"recording:{rec_b.pk}"
        ).vector_blob
        a_seg_key = f"segment:{ta.pk}:0"
        a_seg_old = EmbeddingDocument.objects.get(
            generation=gen, document_key=a_seg_key
        ).vector_blob
        b_seg_key = f"segment:{tb.pk}:0"
        b_seg_old = EmbeddingDocument.objects.get(
            generation=gen, document_key=b_seg_key
        ).vector_blob

        # ONE callback, two recordings: A's worker (embed call 1)
        # succeeds; B's worker (embed call 2) fails AFTER deleting B's
        # own removed vector.
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts",
            fake_embedder(fail_calls=(2,), fill=0.9),
        )
        with caplog.at_level(logging.WARNING):
            with transaction.atomic():
                sum_a1.is_active = False
                sum_a1.superseded_at = timezone.now()
                sum_a1.save(update_fields=["is_active", "superseded_at"])
                sum_a2 = make_summary_version(rec_a, ta, sa, title="A V2")
                sum_b1.is_active = False
                sum_b1.superseded_at = timezone.now()
                sum_b1.save(update_fields=["is_active", "superseded_at"])
                sum_b2 = make_summary_version(rec_b, tb, sb, title="B V2")
                search_sync.schedule_recording_sync([rec_a.pk, rec_b.pk])

        # A: its own removed vector was deleted and its new vector embedded.
        assert not EmbeddingDocument.objects.filter(
            generation=gen, document_key=f"summary:{sum_a1.pk}"
        ).exists()
        a2 = EmbeddingDocument.objects.get(
            generation=gen, document_key=f"summary:{sum_a2.pk}"
        )
        assert a2.source_content_hash == SearchDocument.objects.get(
            document_key=f"summary:{sum_a2.pk}"
        ).content_hash
        # B: its OWN removed vector was deleted (from B's snapshot, never
        # A's), but B's new vector was NOT embedded (B's call failed).
        assert not EmbeddingDocument.objects.filter(
            generation=gen, document_key=f"summary:{sum_b1.pk}"
        ).exists()
        assert not EmbeddingDocument.objects.filter(
            generation=gen, document_key=f"summary:{sum_b2.pk}"
        ).exists()
        # No cross-recording writes: B's metadata kept the old blob and
        # neither scope touched the segment rows.
        assert (
            EmbeddingDocument.objects.get(
                generation=gen, document_key=f"recording:{rec_b.pk}"
            ).vector_blob
            == b_meta_old
        )
        assert (
            EmbeddingDocument.objects.get(generation=gen, document_key=a_seg_key).vector_blob
            == a_seg_old
        )
        assert (
            EmbeddingDocument.objects.get(generation=gen, document_key=b_seg_key).vector_blob
            == b_seg_old
        )
        # Failure counted per recording: exactly ONE (B); search clean.
        assert embed_warning_messages(caplog) == [
            "embedding index post-commit sync failed "
            "category=embedding_index_sync_failed count=1"
        ]
        assert search_warning_messages(caplog) == []
        # The connection-local TEMP snapshot was dropped after the callback.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name FROM sqlite_temp_master "
                "WHERE name = 'brain_embedding_removed_keys'"
            )
            assert cursor.fetchall() == []

    def test_duplicate_ids_deduplicated_in_one_callback(
        self, tmp_path, monkeypatch, caplog
    ):
        rec, _t, _s, _config = seed_converged(
            tmp_path, monkeypatch, sha="emb-sync-dedup"
        )
        calls: list[str] = []
        real = embedding_sync.sync_recording_embeddings

        def spy(recording_id, **kwargs):
            calls.append(str(recording_id))
            return real(recording_id, **kwargs)

        monkeypatch.setattr(embedding_sync, "sync_recording_embeddings", spy)
        monkeypatch.setattr(
            "workflow.services.embedding_index.embed_texts", fake_embedder()
        )
        with caplog.at_level(logging.WARNING):
            with transaction.atomic():
                search_sync.schedule_recording_sync([rec.pk, rec.pk])
        # one unique id => one worker invocation, no duplicate warnings
        assert calls == [rec.pk]
        assert embed_warning_messages(caplog) == []
        assert search_warning_messages(caplog) == []