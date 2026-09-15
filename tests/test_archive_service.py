"""Reversible Recording archive service (never source-file deletion).

Proves the archive contract directly on the service boundary:

- archive sets ``archived_at`` transactionally; restore clears it;
- both are idempotent (the second call is zero DML);
- an unfinished ``ProcessingAttempt`` refuses archiving (nothing written);
- archive/restore never touch any other Recording field, history, files,
  the network, or the search/embedding index rows, and never schedule
  synchronization;
- the derived search index stays internal-healthy across archive/restore
  (documents remain stored; ineligibility is a query-scope concern).
"""

from __future__ import annotations

import pytest

from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
)
from workflow.services import archive
from workflow.services import search_index as si

pytestmark = pytest.mark.django_db


def _recording(sha="archive-service"):
    return Recording.objects.create(
        sha256=sha, processing_status=ProcessingStatus.TRANSCRIBED
    )


class TestArchiveRestore:
    def test_archive_sets_and_restore_clears(self):
        recording = _recording("archive-a")
        result = archive.archive_recording(recording)
        assert result == {
            "recording_id": recording.pk,
            "result": "archived",
            "archived": True,
        }
        recording.refresh_from_db()
        assert recording.archived_at is not None
        assert archive.is_archived(recording) is True

        restored = archive.restore_recording(recording)
        assert restored == {
            "recording_id": recording.pk,
            "result": "restored",
            "archived": False,
        }
        recording.refresh_from_db()
        assert recording.archived_at is None

    def test_idempotent_second_call_is_unchanged(self):
        recording = _recording("archive-b")
        first = archive.archive_recording(recording)
        assert first["result"] == "archived"
        recording.refresh_from_db()
        stamp = recording.archived_at
        second = archive.archive_recording(recording)
        assert second["result"] == "unchanged"
        recording.refresh_from_db()
        assert recording.archived_at == stamp

        archive.restore_recording(recording)
        third = archive.restore_recording(recording)
        assert third["result"] == "unchanged"

    def test_unfinished_attempt_refuses_archive(self):
        recording = _recording("archive-c")
        ProcessingAttempt.objects.create(
            recording=recording,
            stage=AttemptStage.TRANSCRIPTION,
            ordinal=1,
            outcome=AttemptOutcome.RUNNING,
            finished_at=None,
        )
        result = archive.archive_recording(recording)
        assert result["result"] == "refused"
        assert result["reason"] == "unfinished_attempt"
        recording.refresh_from_db()
        assert recording.archived_at is None

    def test_archive_touches_only_archived_at(self):
        recording = _recording("archive-d")
        before = {
            field: getattr(recording, field)
            for field in (
                "sha256",
                "processing_status",
                "summary_status",
                "audio_status",
                "retranscription_failed",
                "resummarization_failed",
                "discovered_at",
                "created_at",
            )
        }
        archive.archive_recording(recording)
        recording.refresh_from_db()
        after = {field: getattr(recording, field) for field in before}
        assert after == before
        archive.restore_recording(recording)
        recording.refresh_from_db()
        assert {field: getattr(recording, field) for field in before} == before

    def test_ensure_not_archived_raises_stable_category(self):
        recording = _recording("archive-e")
        archive.ensure_not_archived(recording)  # active: no raise
        archive.archive_recording(recording)
        recording.refresh_from_db()
        with pytest.raises(archive.ArchivedRecordingError) as excinfo:
            archive.ensure_not_archived(recording)
        assert excinfo.value.code == "recording_archived"
        assert "archived" in str(excinfo.value).lower()


class TestNoSideEffects:
    def test_archive_restore_do_not_schedule_sync_or_network(self, monkeypatch):
        from workflow.services import search_sync

        def _fail(*args, **kwargs):
            raise AssertionError("archive/restore must not schedule sync")

        monkeypatch.setattr(search_sync, "schedule_recording_sync", _fail)
        monkeypatch.setattr(
            "workflow.services.embedding_client.embed_texts",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no embedding")),
        )
        monkeypatch.setattr(
            "subprocess.run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no subprocess"))
        )
        monkeypatch.setattr(
            "httpx.post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no http"))
        )
        recording = _recording("archive-f")
        archive.archive_recording(recording)
        archive.restore_recording(recording)

    def test_index_rows_retained_and_status_healthy(self):
        from factories import make_transcribed_recording

        recording, _t, _s = make_transcribed_recording(
            ["an archived but internally indexed document"], sha="archive-index"
        )
        si.rebuild_index()
        before = si.build_status_report()
        assert before["healthy"] is True
        document_count = recording.search_documents.count()
        assert document_count > 0

        archive.archive_recording(recording)
        # Rows are retained AND the index stays healthy: ineligibility is
        # enforced by query scopes, never by deleting index rows.
        recording.refresh_from_db()
        assert recording.search_documents.count() == document_count
        after_archive = si.build_status_report()
        assert after_archive["healthy"] is True

        archive.restore_recording(recording)
        assert si.build_status_report()["healthy"] is True
