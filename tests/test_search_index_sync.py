"""Step 5A.3 incremental search-index synchronization tests.

Proves (per the approved plan):
- per-recording reconciliation creates/updates/deletes registry + FTS
  rows after authoritative changes, with FULL-field comparison (a
  tampered field carrying the OLD hash is still repaired);
- FTS row states are handled explicitly (skip / UPDATE existing / INSERT
  missing with the existing registry pk — never UPDATE alone / create
  registry + INSERT);
- hooks fire via ``transaction.on_commit`` (captured), are discarded on
  rollback, and the early detection save syncs even when generation
  later fails;
- failures are isolated per recording, never escape the callback, and
  are logged ONLY as one fixed aggregate warning (a count, nothing
  else — no ids, exceptions, paths, SQL or content);
- strengthened canonical-row validation rejects cross-recording /
  mismatched provenance forgeries;
- unattributable orphan FTS rows are NEVER repairable per recording:
  both orphan scenarios (registry-only deletion; authoritative deletion
  via CASCADE) stay unhealthy until a full rebuild;
- idempotency (zero DML when converged), bounded streaming, and
  eventual convergence under contention — without pretending
  separate-commit callbacks are automatically coalesced.
No network, MacWhisper, oMLX or real audio anywhere.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading

import pytest
from django.db import connection, connections, transaction
from django.test.testcases import CaptureQueriesContext
from django.utils import timezone

from brainlib import cli
from factories import (
    final_summary_json,
    make_config,
    make_summary_version,
    make_tag,
    make_tag_assignment,
    make_transcribed_recording,
)
from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    AudioSource,
    ProcessingAttempt,
    RoutingDecision,
    RoutingMethod,
    SearchDocument,
    Section,
    Transcript,
    TranscriptSegment,
)
from workflow.services import search_index as si
from workflow.services import search_sync

pytestmark = pytest.mark.django_db


def _meta(recording) -> SearchDocument:
    return SearchDocument.objects.get(document_key=f"recording:{recording.pk}")


def _report() -> dict:
    return si.build_status_report()


def _healthy() -> bool:
    return _report()["healthy"]


def _sync_warning_messages(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "workflow.services.search_sync"
    ]


def _reconcile_spy(monkeypatch) -> list[str]:
    """Wrap the module-global reconcile with a recorder (the on_commit
    callback resolves ``reconcile_recording`` from module globals, so the
    spy observes every scheduled sync)."""
    real = search_sync.reconcile_recording
    calls: list[str] = []

    def spy(recording_id, **kwargs):
        result = real(recording_id, **kwargs)
        calls.append(str(recording_id))
        return result

    monkeypatch.setattr(search_sync, "reconcile_recording", spy)
    return calls


# ---------------------------------------------------------------------------
# Basic reconciliation: create / update / delete
# ---------------------------------------------------------------------------


class TestReconcileBasics:
    def test_missing_documents_created_and_status_healthy(self):
        rec, transcript, _section = make_transcribed_recording(
            ["first words", "more words"], sha="sync-1"
        )
        assert _healthy() is False
        counts = search_sync.reconcile_recording(rec.pk)
        assert counts["inserted"] == 3  # 2 segments + 1 metadata doc
        assert counts["deleted"] == 0
        assert _meta(rec).title_text == "Untitled recording"
        seg = SearchDocument.objects.get(document_key=f"segment:{transcript.pk}:0")
        assert seg.body_text == "first words"
        assert seg.content_hash == si.compute_content_hash(si._row_frame(seg))
        assert _healthy() is True

    def test_second_reconcile_is_idempotent_zero_dml(self):
        rec, _t, _s = make_transcribed_recording(["alpha", "beta"], sha="sync-2")
        search_sync.reconcile_recording(rec.pk)
        with CaptureQueriesContext(connection) as ctx:
            counts = search_sync.reconcile_recording(rec.pk)
        writes = [
            q["sql"]
            for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            and "workflow_search" in q["sql"]
        ]
        assert writes == [], writes
        assert counts["inserted"] == 0
        assert counts["updated"] == 0
        assert counts["deleted"] == 0
        assert counts["skipped"] == 3
        assert _healthy() is True

    def test_unknown_recording_is_a_safe_noop(self):
        rec, _t, _s = make_transcribed_recording(["keep"], sha="sync-unknown")
        counts = search_sync.reconcile_recording("11111111-1111-1111-1111-111111111111")
        assert counts["recording_missing"] == 1
        assert SearchDocument.objects.filter(recording=rec).count() == 0

    def test_summary_variant_added_then_replaced(self):
        rec, transcript, section = make_transcribed_recording(["content"], sha="sync-3")
        first = make_summary_version(rec, transcript, section, title="V1")
        search_sync.reconcile_recording(rec.pk)
        assert SearchDocument.objects.filter(document_key=f"summary:{first.pk}").exists()
        assert _meta(rec).title_text == "V1"
        # persist_summary semantics: the same output_language is replaced.
        first.is_active = False
        first.superseded_at = timezone.now()
        first.save(update_fields=["is_active", "superseded_at"])
        second = make_summary_version(rec, transcript, section, title="V2")
        search_sync.reconcile_recording(rec.pk)
        assert not SearchDocument.objects.filter(document_key=f"summary:{first.pk}").exists()
        assert SearchDocument.objects.filter(document_key=f"summary:{second.pk}").exists()
        assert _meta(rec).title_text == "V2"
        assert _healthy() is True

    def test_old_transcript_docs_removed_on_reactivation(self):
        rec, old_transcript, old_section = make_transcribed_recording(
            ["old segment one", "old segment two"], sha="sync-4"
        )
        old_summary = make_summary_version(rec, old_transcript, old_section, title="Old")
        si.rebuild_index()
        assert _healthy() is True
        # Simulate activation of a NEW transcript (exactly what
        # _persist_transcript leaves committed): old inactive, new active.
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
            transcript=new_transcript, ordinal=0, start_ms=0, end_ms=900, text="new segment"
        )
        Section.objects.create(transcript=new_transcript, ordinal=0, title="Full recording")
        old_transcript.is_active = False
        old_transcript.save(update_fields=["is_active"])
        new_transcript.is_active = True
        new_transcript.save(update_fields=["is_active"])
        search_sync.reconcile_recording(rec.pk)
        assert not SearchDocument.objects.filter(
            document_key=f"segment:{old_transcript.pk}:0"
        ).exists()
        assert not SearchDocument.objects.filter(
            document_key=f"summary:{old_summary.pk}"
        ).exists()
        assert SearchDocument.objects.filter(
            document_key=f"segment:{new_transcript.pk}:0"
        ).exists()
        # Authoritative history is NEVER deleted by the reconciler.
        old_transcript.refresh_from_db()
        old_summary.refresh_from_db()
        assert old_transcript.pk is not None
        assert old_summary.is_active is True  # historically valid on the old transcript
        assert _healthy() is True

    def test_tag_changes_update_metadata_aux(self):
        rec, _t, _s = make_transcribed_recording(["tag me"], sha="sync-5")
        si.rebuild_index()
        assert _meta(rec).aux_text == ""
        tag = make_tag("Family")
        assignment = make_tag_assignment(rec, tag)
        search_sync.reconcile_recording(rec.pk)
        assert _meta(rec).aux_text == "Family"
        assignment.is_active = False
        assignment.deactivated_by = "user"
        assignment.deactivated_at = timezone.now()
        assignment.save(update_fields=["is_active", "deactivated_by", "deactivated_at"])
        search_sync.reconcile_recording(rec.pk)
        assert _meta(rec).aux_text == ""
        assert _healthy() is True

    def test_manual_tag_service_hook_updates_index(
        self, django_capture_on_commit_callbacks
    ):
        rec, _t, _s = make_transcribed_recording(["tag hook"], sha="sync-5b")
        tag = make_tag("Family")
        si.rebuild_index()
        assert _meta(rec).aux_text == ""
        from workflow.services import tags as tags_service

        with django_capture_on_commit_callbacks(execute=True) as captured:
            tags_service.add_manual_tag(rec, tag)
        assert len(captured) == 1  # one hook per commit
        assert _meta(rec).aux_text == "Family"
        assert _healthy() is True

    def test_routing_confirm_via_service_hook_flips_title(
        self, django_capture_on_commit_callbacks
    ):
        rec, transcript, section = make_transcribed_recording(["xiexie"], sha="sync-6")
        make_summary_version(rec, transcript, section, title="EN title", output_language="en")
        make_summary_version(
            rec, transcript, section, title="Zhongwen biaoti", output_language="zh-Hant"
        )
        # Unverified MANUAL Chinese routing: does NOT flip the default
        # (only verified or automatic Chinese routing does).
        RoutingDecision.objects.create(
            recording=rec,
            ordinal=1,
            route_suggestion="cantonese",
            profile_name="cantonese",
            method=RoutingMethod.MANUAL,
            is_active=True,
        )
        si.rebuild_index()
        assert _meta(rec).title_text == "EN title"
        from workflow.services import pipeline

        with django_capture_on_commit_callbacks(execute=True):
            pipeline.confirm_routing(rec)
        # Verified Chinese routing flips the derived default to zh-Hant:
        # the metadata title follows via the confirm_routing hook alone.
        assert _meta(rec).title_text == "Zhongwen biaoti"
        assert _healthy() is True

    def test_summarize_one_success_syncs_variant_and_tags(
        self, tmp_path, django_capture_on_commit_callbacks
    ):
        from workflow.services import summarize as summarize_service

        from test_summarize import make_llm_config

        config = make_config(tmp_path, llm=make_llm_config(tmp_path).llm)
        rec, _t, _s = make_transcribed_recording(["a short meeting"], sha="sync-7")

        def llm(*, system, user):
            return final_summary_json()

        with django_capture_on_commit_callbacks(execute=True) as captured:
            result = summarize_service.summarize_one(config, rec, llm_call=llm)
        assert result["result"] == "summarized"
        assert SearchDocument.objects.filter(
            document_key=f"summary:{result['summary_id']}"
        ).exists()
        assert _meta(rec).title_text == "Meeting about grading"
        # Materialized default-variant tags reached the index via the
        # persist_summary hook (plus the late source-language save's
        # own idempotent sync): both fire, the result converges.
        assert "Academic" in _meta(rec).aux_text
        assert _healthy() is True
        assert len(captured) >= 1

    def test_ingest_attach_and_detach_sync_metadata(
        self, tmp_path, django_capture_on_commit_callbacks
    ):
        from workflow.services import ingest as ingest_service

        config = make_config(tmp_path)
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        audio = inbox / "lecture-2026.wav"
        audio.write_bytes(b"RIFF fake bytes")
        rec, _t, _s = make_transcribed_recording(["lecture"], sha="sync-8")
        source = AudioSource.objects.create(
            path=str(audio),
            path_identity=str(audio).casefold(),
            original_filename=audio.name,
            file_size=audio.stat().st_size,
            file_mtime=audio.stat().st_mtime,
            recording=rec,
            is_canonical=True,
        )
        si.rebuild_index()
        assert _meta(rec).body_text == "lecture-2026.wav"
        # Detach for rehash: the filename leaves the old Recording doc.
        with django_capture_on_commit_callbacks(execute=True):
            ingest_service._detach_for_rehash(source, "content_changed")
        assert _meta(rec).body_text == ""
        assert _meta(rec).title_text == "Untitled recording"
        assert _healthy() is True
        # Re-attach (as _attach_hashed_source does): it comes back.
        source.refresh_from_db()
        with django_capture_on_commit_callbacks(execute=True):
            ingest_service._attach_hashed_source(
                source, rec.sha256, config, ingest_service.IngestReport()
            )
        assert _meta(rec).body_text == "lecture-2026.wav"
        assert _meta(rec).title_text == "lecture-2026.wav"
        assert _healthy() is True


# ---------------------------------------------------------------------------
# Mandatory: detection succeeds, generation fails afterwards
# ---------------------------------------------------------------------------


class TestDetectionThenGenerationFailure:
    def test_detected_language_reaches_index_even_when_generation_fails(
        self, tmp_path, django_capture_on_commit_callbacks, monkeypatch
    ):
        from workflow.services import summarize as summarize_service
        from workflow.services.llm import LLMUnavailable

        from test_summarize import make_llm_config

        config = make_config(tmp_path, llm=make_llm_config(tmp_path).llm)
        rec, transcript, section = make_transcribed_recording(
            ["dianhua neirong"], sha="sync-det"
        )
        # Active English AND Traditional Chinese variants with different
        # titles; English is the derived default (no source language yet).
        make_summary_version(
            rec, transcript, section, title="English Title", output_language="en"
        )
        make_summary_version(
            rec, transcript, section, title="Chinese Title", output_language="zh-Hant"
        )
        si.rebuild_index()
        assert _meta(rec).title_text == "English Title"

        # Spy on reconciliation: the detection hook must schedule a sync
        # BEFORE generation runs (so it lands no matter how generation
        # ends).
        reconciled = _reconcile_spy(monkeypatch)

        llm_calls = {"n": 0}

        def llm(*, system, user):
            llm_calls["n"] += 1
            if llm_calls["n"] == 1:
                return json.dumps({"language": "yue"})  # detection succeeds
            raise LLMUnavailable("endpoint_unavailable", "no connection")  # generation fails

        with django_capture_on_commit_callbacks(execute=True):
            result = summarize_service.summarize_one(
                config, rec, target_language="original", regenerate=True, llm_call=llm
            )

        assert result["result"] == "failed"
        assert result["error_code"] == "endpoint_unavailable"
        assert llm_calls["n"] == 2
        transcript.refresh_from_db()
        assert transcript.language_observed == "yue"
        assert transcript.language_observed_verified_by == "llm_detection"
        # The detection-triggered sync ran even though generation failed…
        assert rec.pk in reconciled
        # …and the metadata title followed the new default (zh-Hant).
        assert _meta(rec).title_text == "Chinese Title"
        assert _healthy() is True


# ---------------------------------------------------------------------------
# Rollback contract
# ---------------------------------------------------------------------------


class TestRollbackContract:
    def test_rollback_discards_hook_and_leaves_index_untouched(
        self, django_capture_on_commit_callbacks
    ):
        rec, transcript, _s = make_transcribed_recording(["rolled back"], sha="sync-rb")
        si.rebuild_index()
        before = SearchDocument.objects.count()
        with django_capture_on_commit_callbacks(execute=True) as captured:
            with transaction.atomic():
                transcript.language_observed = "fi"
                transcript.save(update_fields=["language_observed"])
                search_sync.schedule_recording_sync([rec.pk])
                transaction.set_rollback(True)
        # Django discards on_commit callbacks registered inside a rolled
        # back (savepoint) transaction: nothing may have run.
        assert captured == []
        transcript.refresh_from_db()
        assert transcript.language_observed == ""
        assert SearchDocument.objects.count() == before
        assert _healthy() is True


# ---------------------------------------------------------------------------
# Failure isolation + log privacy
# ---------------------------------------------------------------------------


class TestFailureIsolation:
    def test_post_commit_failure_never_raises_and_surfaces_only_a_count(
        self, caplog, monkeypatch, django_capture_on_commit_callbacks
    ):
        rec, transcript, _s = make_transcribed_recording(["authoritative"], sha="sync-f1")

        def boom(recording_id, using, counts):
            raise RuntimeError("boom-secret-detail")

        monkeypatch.setattr(search_sync, "_upsert_expected", boom)
        with caplog.at_level(logging.WARNING, logger="workflow.services.search_sync"):
            with django_capture_on_commit_callbacks(execute=True):
                search_sync.schedule_recording_sync([rec.pk])
        # The committed operation is unaffected (no exception escaped);
        # the index merely stays stale/detectable.
        transcript.refresh_from_db()
        assert transcript.pk is not None
        report = _report()
        assert report["healthy"] is False
        assert report["categories"]["missing_from_registry"] >= 1
        messages = _sync_warning_messages(caplog)
        assert messages == [
            "search index post-commit sync failed category=search_index_sync_failed count=1"
        ]
        assert "boom-secret-detail" not in messages[0]
        assert str(rec.pk) not in messages[0]

    def test_one_failure_never_blocks_the_other_recordings(
        self, caplog, monkeypatch, django_capture_on_commit_callbacks
    ):
        rec_a, _ta, _sa = make_transcribed_recording(["a"], sha="sync-m1")
        rec_b, _tb, _sb = make_transcribed_recording(["b"], sha="sync-m2")
        rec_c, _tc, _sc = make_transcribed_recording(["c"], sha="sync-m3")
        real = search_sync.reconcile_recording

        def selective(recording_id, **kwargs):
            if str(recording_id) == rec_b.pk:
                raise sqlite3.OperationalError("simulated database lock")
            return real(recording_id, **kwargs)

        monkeypatch.setattr(search_sync, "reconcile_recording", selective)
        with caplog.at_level(logging.WARNING, logger="workflow.services.search_sync"):
            with django_capture_on_commit_callbacks(execute=True):
                search_sync.schedule_recording_sync([rec_a.pk, rec_b.pk, rec_c.pk])
        # GOOD recordings reconciled, the bad one did not stop them.
        assert SearchDocument.objects.filter(recording=rec_a).count() == 2
        assert SearchDocument.objects.filter(recording=rec_c).count() == 2
        assert SearchDocument.objects.filter(recording=rec_b).count() == 0
        messages = _sync_warning_messages(caplog)
        assert messages == [
            "search index post-commit sync failed category=search_index_sync_failed count=1"
        ]

    def test_log_line_contains_no_uuid_content_or_exception_text(
        self, caplog, monkeypatch, django_capture_on_commit_callbacks
    ):
        rec, _t, _s = make_transcribed_recording(["privacy"], sha="sync-priv")
        real = search_sync.reconcile_recording

        def selective(recording_id, **kwargs):
            raise sqlite3.OperationalError("invisible detail /home/user /secret.sql")

        monkeypatch.setattr(search_sync, "reconcile_recording", selective)
        with caplog.at_level(logging.WARNING, logger="workflow.services.search_sync"):
            with django_capture_on_commit_callbacks(execute=True):
                search_sync.schedule_recording_sync([rec.pk, rec.pk])  # deduped
        messages = _sync_warning_messages(caplog)
        assert messages == [
            "search index post-commit sync failed category=search_index_sync_failed count=1"
        ]
        joined = " ".join(messages)
        assert rec.pk not in joined
        assert "invisible" not in joined
        assert "secret" not in joined

    def test_failed_reconcile_transaction_rolls_back_completely(
        self, monkeypatch, django_capture_on_commit_callbacks
    ):
        rec, transcript, _s = make_transcribed_recording(["atomic"], sha="sync-f2")
        si.rebuild_index()
        doc = SearchDocument.objects.get(document_key=f"segment:{transcript.pk}:0")
        # Both layers need repair: registry version tampered AND the FTS
        # text drifted — the FTS UPDATE is the step that fails.
        SearchDocument.objects.filter(pk=doc.pk).update(index_version="9")
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE workflow_search_fts SET body_text = %s WHERE rowid = %s",
                ["half-write-probe", doc.pk],
            )

        def always_fail(cursor, rows, **kwargs):
            raise sqlite3.OperationalError("locked")

        monkeypatch.setattr(search_sync, "_update_fts_batch", always_fail)
        with django_capture_on_commit_callbacks(execute=True):
            search_sync.schedule_recording_sync([rec.pk])
        # The whole reconcile rolled back: the registry keeps its
        # tampered version (the spec write was discarded together with
        # the failed FTS update — no half-written index).
        doc.refresh_from_db()
        assert doc.index_version == "9"
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT body_text FROM workflow_search_fts WHERE rowid = %s", [doc.pk]
            )
            assert cursor.fetchone()[0] == "half-write-probe"
        report = _report()
        assert report["healthy"] is False
        assert report["categories"]["version_mismatch"] == 1


# ---------------------------------------------------------------------------
# Tampering: FULL-field comparison, never hash-trust
# ---------------------------------------------------------------------------


class TestTamperRepair:
    def test_index_version_only_tamper_with_unchanged_hash_is_repaired(self):
        rec, _t, _s = make_transcribed_recording(["v tamper"], sha="sync-t1")
        si.rebuild_index()
        SearchDocument.objects.filter(recording=rec).update(index_version="9")
        search_sync.reconcile_recording(rec.pk)
        versions = set(
            SearchDocument.objects.filter(recording=rec).values_list("index_version", flat=True)
        )
        assert versions == {si.INDEX_VERSION}
        assert _healthy() is True

    def test_provenance_only_tamper_with_unchanged_hash_is_repaired(self):
        rec, transcript, _s = make_transcribed_recording(["prov tamper"], sha="sync-t2")
        si.rebuild_index()
        key = f"segment:{transcript.pk}:0"
        original = SearchDocument.objects.get(document_key=key)
        # Forge start_ms while KEEPING the old (now lying) content hash.
        SearchDocument.objects.filter(document_key=key).update(start_ms=42)
        assert SearchDocument.objects.get(document_key=key).content_hash == original.content_hash
        search_sync.reconcile_recording(rec.pk)
        doc = SearchDocument.objects.get(document_key=key)
        assert doc.start_ms == 0
        assert doc.content_hash == original.content_hash
        assert _healthy() is True

    def test_title_only_tamper_with_matching_parts_still_repaired(self):
        rec, transcript, section = make_transcribed_recording(["body text"], sha="sync-t3")
        summary = make_summary_version(rec, transcript, section, title="Honest")
        si.rebuild_index()
        key = f"summary:{summary.pk}"
        SearchDocument.objects.filter(document_key=key).update(title_text="Lying")
        search_sync.reconcile_recording(rec.pk)
        assert SearchDocument.objects.get(document_key=key).title_text == "Honest"
        assert _healthy() is True

    def test_fts_text_only_drift_repaired(self):
        rec, _t, _s = make_transcribed_recording(["fts drift"], sha="sync-t4")
        si.rebuild_index()
        doc = SearchDocument.objects.filter(recording=rec).first()
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE workflow_search_fts SET body_text = %s WHERE rowid = %s",
                ["tampered fts", doc.pk],
            )
        report = _report()
        assert report["categories"]["content_mismatch"] == 1
        search_sync.reconcile_recording(rec.pk)
        assert _healthy() is True

    def test_missing_fts_row_never_repaired_by_update_alone(self, monkeypatch):
        rec, transcript, _s = make_transcribed_recording(["insert not update"], sha="sync-t5")
        si.rebuild_index()
        doc = SearchDocument.objects.filter(recording=rec).exclude(doc_type="recording").first()
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM workflow_search_fts WHERE rowid = %s", [doc.pk])
        updates: list[list] = []
        inserts: list[list] = []
        real_update = search_sync._update_fts_batch
        real_insert = search_sync._insert_fts_batch

        def spy_update(cursor, rows, **kwargs):
            updates.append(list(rows))
            return real_update(cursor, rows, **kwargs)

        def spy_insert(cursor, rows, **kwargs):
            inserts.append(list(rows))
            return real_insert(cursor, rows, **kwargs)

        monkeypatch.setattr(search_sync, "_update_fts_batch", spy_update)
        monkeypatch.setattr(search_sync, "_insert_fts_batch", spy_insert)
        search_sync.reconcile_recording(rec.pk)
        # The missing row is INSERTed with the EXISTING registry pk…
        assert any(row[0] == doc.pk for batch in inserts for row in batch)
        # …and is NEVER routed through an UPDATE statement.
        assert not any(row[0] == doc.pk for batch in updates for row in batch)
        with connection.cursor() as cursor:
            cursor.execute("SELECT rowid FROM workflow_search_fts WHERE rowid = %s", [doc.pk])
            assert cursor.fetchone()[0] == doc.pk
        assert _healthy() is True


# ---------------------------------------------------------------------------
# Strengthened canonical-row validation (shared status + reconcile)
# ---------------------------------------------------------------------------


class TestForgedProvenance:
    _COLUMNS = (
        "id, document_key, doc_type, recording_id, transcript_id, summary_id,"
        " segment_ordinal, start_ms, end_ms, output_language, title_text,"
        " body_text, aux_text, content_hash, index_version, created_at"
    )

    def _forge(self, *, pk, key, doc_type, recording_id, transcript_id=None,
               summary_id=None, ordinal=None, start_ms=None, end_ms=None,
               output_language="", body="forged"):
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO workflow_search_document ({self._COLUMNS})"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '', %s, '',"
                " '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',"
                " '1', '2026-01-01 00:00:00')",
                [pk, key, doc_type, recording_id, transcript_id, summary_id,
                 ordinal, start_ms, end_ms, output_language, body],
            )

    def test_cross_recording_segment_forgery_rejected_by_both_layers(self):
        rec_a, _ta, _sa = make_transcribed_recording(["victim a"], sha="sync-fg-a")
        si.rebuild_index()
        rec_b, trans_b, _sb = make_transcribed_recording(["foreign evidence"], sha="sync-fg-b")
        # B is not indexed yet: forge a segment doc on recording A that
        # points at recording B's ACTIVE transcript.
        self._forge(
            pk=410001,
            key=f"segment:{trans_b.pk}:0",
            doc_type="segment",
            recording_id=rec_a.pk,
            transcript_id=trans_b.pk,
            ordinal=0,
            start_ms=0,
            end_ms=500,
        )
        report = _report()
        assert report["categories"]["orphan_document"] == 1
        assert f"segment:{trans_b.pk}:0" in report["keys"]["orphan_document"]
        search_sync.reconcile_recording(rec_a.pk)
        assert SearchDocument.objects.filter(recording=rec_a).count() == 2  # forgery gone
        search_sync.reconcile_recording(rec_b.pk)
        assert _healthy() is True

    def test_cross_recording_summary_forgery_rejected(self):
        rec_a, _ta, _sa = make_transcribed_recording(["victim a"], sha="sync-fgs-a")
        si.rebuild_index()
        rec_b, trans_b, sec_b = make_transcribed_recording(["foreign"], sha="sync-fgs-b")
        summary_b = make_summary_version(rec_b, trans_b, sec_b, title="B title")
        self._forge(
            pk=410002,
            key=f"summary:{summary_b.pk}",
            doc_type="summary",
            recording_id=rec_a.pk,
            transcript_id=trans_b.pk,
            summary_id=summary_b.pk,
            output_language="en",
        )
        report = _report()
        assert report["categories"]["orphan_document"] == 1
        search_sync.reconcile_recording(rec_a.pk)
        assert not SearchDocument.objects.filter(
            document_key=f"summary:{summary_b.pk}", recording=rec_a
        ).exists()
        search_sync.reconcile_recording(rec_b.pk)
        assert SearchDocument.objects.filter(
            document_key=f"summary:{summary_b.pk}", recording=rec_b
        ).exists()
        assert _healthy() is True

    def test_summary_output_language_mismatch_is_not_canonical(self):
        rec, transcript, section = make_transcribed_recording(["lang forge"], sha="sync-fgo")
        summary = make_summary_version(rec, transcript, section, title="T",
                                       output_language="en")
        si.rebuild_index()
        key = f"summary:{summary.pk}"
        SearchDocument.objects.filter(document_key=key).update(
            output_language="fi", content_hash="f" * 64
        )
        search_sync.reconcile_recording(rec.pk)
        doc = SearchDocument.objects.get(document_key=key)
        assert doc.output_language == "en"
        assert doc.content_hash == si.compute_content_hash(si._row_frame(doc))
        assert _healthy() is True


# ---------------------------------------------------------------------------
# Orphan FTS boundary: two distinct scenarios, both rebuild-only
# ---------------------------------------------------------------------------


class TestOrphanFTSBoundary:
    def test_registry_only_deletion_recreates_pair_but_orphan_remains(self):
        rec, transcript, section = make_transcribed_recording(["orphan a"], sha="sync-o1")
        summary = make_summary_version(rec, transcript, section, title="KeepMe")
        si.rebuild_index()
        doc = SearchDocument.objects.get(document_key=f"summary:{summary.pk}")
        dead_rowid = doc.pk
        # Delete ONLY the registry row; its FTS row survives unattributed.
        SearchDocument.objects.filter(document_key=f"summary:{summary.pk}").delete()
        search_sync.reconcile_recording(rec.pk)
        fresh = SearchDocument.objects.get(document_key=f"summary:{summary.pk}")
        assert fresh.pk != dead_rowid  # recreated as the authoritative Summary is still eligible
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM workflow_search_fts WHERE rowid = %s",
                           [dead_rowid])
            assert cursor.fetchone()[0] == 1  # the OLD FTS row SURVIVES
        report = _report()
        assert report["healthy"] is False
        assert report["categories"]["orphan_fts_row"] == 1
        assert f"rowid:{dead_rowid}" in report["keys"]["orphan_fts_row"]
        si.rebuild_index()  # only the full rebuild may remove it
        assert _healthy() is True

    def test_authoritative_deletion_cascades_no_recreation_until_rebuild(self):
        rec, transcript, section = make_transcribed_recording(["orphan b"], sha="sync-o2")
        summary = make_summary_version(rec, transcript, section, title="Doomed")
        si.rebuild_index()
        key = f"summary:{summary.pk}"
        dead_rowid = SearchDocument.objects.get(document_key=key).pk
        # Delete the AUTHORITATIVE Summary: the registry row CASCADEs,
        # the FTS row survives.
        summary.delete()
        search_sync.reconcile_recording(rec.pk)
        # Nothing is recreated (the Summary is gone)…
        assert not SearchDocument.objects.filter(document_key=key).exists()
        assert SearchDocument.objects.filter(document_key__startswith="summary:").count() == 0
        # …the orphan FTS row remains…
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM workflow_search_fts WHERE rowid = %s",
                           [dead_rowid])
            assert cursor.fetchone()[0] == 1
        report = _report()
        assert report["healthy"] is False
        assert report["categories"]["orphan_fts_row"] == 1
        si.rebuild_index()
        assert _healthy() is True


# ---------------------------------------------------------------------------
# Concurrency contract (truth-derivation, nonfatal contention)
# ---------------------------------------------------------------------------


class TestConvergence:
    def test_separate_commits_get_separate_syncs_and_converge(
        self, monkeypatch, django_capture_on_commit_callbacks
    ):
        rec, transcript, section = make_transcribed_recording(["order"], sha="sync-c1")
        calls = _reconcile_spy(monkeypatch)
        # No cross-commit coalescing is claimed: each commit's callback
        # fires separately; each reconcile derives the CURRENT truth.
        with django_capture_on_commit_callbacks(execute=True):
            make_summary_version(rec, transcript, section, title="One")
            search_sync.schedule_recording_sync([rec.pk])
        assert calls == [rec.pk]
        with django_capture_on_commit_callbacks(execute=True):
            make_summary_version(rec, transcript, section, title="Two", is_active=False)
            search_sync.schedule_recording_sync([rec.pk])
        assert len(calls) == 2  # one sync per commit — never coalesced
        assert _meta(rec).title_text == "One"
        assert _healthy() is True

    def test_failed_sync_converges_on_next_success(
        self, caplog, monkeypatch, django_capture_on_commit_callbacks
    ):
        rec, transcript, section = make_transcribed_recording(["retry"], sha="sync-c2")
        real = search_sync.reconcile_recording
        state = {"fail": True}

        def flaky(recording_id, **kwargs):
            if state["fail"]:
                state["fail"] = False
                raise sqlite3.OperationalError("database is locked")
            return real(recording_id, **kwargs)

        monkeypatch.setattr(search_sync, "reconcile_recording", flaky)
        with caplog.at_level(logging.WARNING, logger="workflow.services.search_sync"):
            with django_capture_on_commit_callbacks(execute=True):
                make_summary_version(rec, transcript, section, title="Converged")
                search_sync.schedule_recording_sync([rec.pk])
        assert _healthy() is False  # the first attempt failed (nonfatal)
        assert _report()["categories"]["missing_from_registry"] >= 1
        # Any LATER sync converges (no automatic retry pretending).
        search_sync.reconcile_recording(rec.pk)
        assert _meta(rec).title_text == "Converged"
        assert _healthy() is True
        assert _sync_warning_messages(caplog) == [
            "search index post-commit sync failed category=search_index_sync_failed count=1"
        ]

    @pytest.mark.django_db(transaction=True)
    def test_unlocked_tag_service_race_converges(self):
        """Web tag edits run WITHOUT the flock: concurrent unlocked syncs
        must never raise into the request flow and must converge."""
        from workflow.models import TagAssignment
        from workflow.services import tags as tags_service

        rec, _t, _s = make_transcribed_recording(["racy"], sha="sync-c3")
        si.rebuild_index()
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
        # Even if a concurrent reconcile lost the SQLite write race (a
        # nonfatal, logged, swallowed failure), a final sync converges.
        search_sync.reconcile_recording(rec.pk)
        assert sorted(_meta(rec).aux_text.splitlines()) == ["Alpha", "Beta"]
        assert _healthy() is True


# ---------------------------------------------------------------------------
# Bounded streaming per recording
# ---------------------------------------------------------------------------


class TestBoundedSync:
    def test_large_recording_reconciles_in_bounded_pages(self, monkeypatch):
        rec, _t, _s = make_transcribed_recording(
            [f"segment {i}" for i in range(1200)], sha="sync-big"
        )
        monkeypatch.setattr(search_sync, "INSERT_CHUNK_SIZE", 100)
        insert_sizes: list[int] = []
        real_insert = search_sync._insert_fts_batch

        def spy_insert(cursor, rows, **kwargs):
            insert_sizes.append(len(rows))
            return real_insert(cursor, rows, **kwargs)

        monkeypatch.setattr(search_sync, "_insert_fts_batch", spy_insert)
        counts = search_sync.reconcile_recording(rec.pk)
        assert counts["inserted"] == 1201
        assert insert_sizes and max(insert_sizes) <= 100, insert_sizes
        assert len(insert_sizes) >= 12
        # The per-recording builder chunk bound holds as well.
        for chunk in si.iter_expected_documents_for_recording_ids([rec.pk], chunk_size=100):
            assert 1 <= len(chunk) <= 100
        assert _healthy() is True

    def test_per_recording_set_matches_rebuild_sweep(self):
        rec, _t, _s = make_transcribed_recording(["one", "two", "three"], sha="sync-parity")
        rebuilt = set()
        for batch in si.iter_expected_document_batches(batch_size=1):
            rebuilt.update(spec.document_key for spec in batch)
        synced = set()
        for batch in si.iter_expected_documents_for_recording_ids([rec.pk]):
            synced.update(spec.document_key for spec in batch)
        assert rebuilt == synced


# ---------------------------------------------------------------------------
# CLI integration (real transactions: hooks fire for real)
# ---------------------------------------------------------------------------


class TestCliIntegration:
    @pytest.mark.django_db(transaction=True)
    def test_cli_language_correction_syncs_index_without_rebuild(self, capsys):
        rec, transcript, section = make_transcribed_recording(["kielikorjaus"], sha="sync-cli1")
        make_summary_version(rec, transcript, section, title="Eng Title",
                             output_language="en")
        make_summary_version(rec, transcript, section, title="Kiin Title",
                             output_language="zh-Hant")
        assert cli.main(["search-index", "rebuild"]) == 0
        capsys.readouterr()
        assert _meta(rec).title_text == "Eng Title"
        assert cli.main(["transcript-language", str(rec.pk), "--set", "yue"]) == 0
        capsys.readouterr()
        # No rebuild: the on_commit hook already updated the index.
        assert _meta(rec).title_text == "Kiin Title"
        assert cli.main(["search-index", "status"]) == 0

    @pytest.mark.django_db(transaction=True)
    def test_sync_failure_never_breaks_the_command(
        self, capsys, monkeypatch, caplog
    ):
        rec, transcript, section = make_transcribed_recording(
            ["katse", "lisaa"], sha="sync-cli2"
        )
        make_summary_version(rec, transcript, section, title="Eng Title",
                             output_language="en")
        make_summary_version(rec, transcript, section, title="Kiin Title",
                             output_language="zh-Hant")
        assert cli.main(["search-index", "rebuild"]) == 0
        capsys.readouterr()
        assert _meta(rec).title_text == "Eng Title"

        def always_fail(recording_id, **kwargs):
            raise RuntimeError("must not escape")

        monkeypatch.setattr(search_sync, "reconcile_recording", always_fail)
        with caplog.at_level(logging.WARNING, logger="workflow.services.search_sync"):
            assert cli.main(["transcript-language", str(rec.pk), "--set", "yue"]) == 0
        capsys.readouterr()
        # Authoritative correction applied despite the sync failure…
        transcript.refresh_from_db()
        assert transcript.language_observed == "yue"
        # …the index stays stale (title no longer matches the derived
        # default)…
        assert _meta(rec).title_text == "Eng Title"
        # …with exactly ONE fixed warning, and detectable drift.
        assert _sync_warning_messages(caplog) == [
            "search index post-commit sync failed category=search_index_sync_failed count=1"
        ]
        assert cli.main(["search-index", "status", "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["healthy"] is False
