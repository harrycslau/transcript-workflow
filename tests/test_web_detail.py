"""Recording detail / summary / transcript / history pages (Step 4).

Proves:
- current summary rendered from structured fields, escaped;
- transcript content escaped; model-provided HTML never trusted;
- long transcript bounded (segments paginated per config);
- current vs historical versions correctly labelled (scope-active
  old-transcript summaries are HISTORICAL, not current);
- missing summary/transcript states render friendly empty states;
- failure/retry warnings surfaced;
- GET purity: no subprocess, no network, no writes, no hashing;
- bounded query count on detail.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.db import connection

from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
    RoutingMethod,
    RoutingDecision,
    Summary,
    SummaryState,
    Transcript,
)

from factories import make_summary_version, make_tag, make_tag_assignment, make_transcribed_recording

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


@pytest.fixture
def client():
    return Client()


def _summary_recording():
    recording, transcript, section = make_transcribed_recording(["hello world"], sha="detail-1")
    summary = make_summary_version(recording, transcript, section)
    return recording, transcript, section, summary


class TestDetailPage:
    def test_detail_renders_structured_summary(self, client):
        recording, _t, _s, summary = _summary_recording()
        response = client.get(f"/recordings/{recording.pk}/")
        assert response.status_code == 200
        content = response.content.decode()
        assert summary.title in content
        assert summary.overview in content
        assert "Point one" in content  # key point
        assert "Do a thing" in content  # action item
        assert "Alice" in content  # person

    def test_summary_fields_escaped_not_raw_html(self, client):
        recording, transcript, section = make_transcribed_recording(["x"], sha="xss-1")
        summary = make_summary_version(
            recording, transcript, section,
            title="<script>alert(1)</script>",
            overview="Overview with <img src=x onerror=alert(2)> payload",
        )
        response = client.get(f"/recordings/{recording.pk}/")
        content = response.content.decode()
        assert "<script>alert(1)</script>" not in content
        assert "&lt;script&gt;" in content
        assert "<img src=x onerror" not in content

    def test_transcript_content_escaped(self, client):
        recording, transcript, section = make_transcribed_recording(
            ["<script>alert('transcript')</script>"], sha="xss-2"
        )
        make_summary_version(recording, transcript, section)
        response = client.get(f"/recordings/{recording.pk}/")
        content = response.content.decode()
        assert "<script>alert('transcript')</script>" not in content
        assert "&lt;script&gt;" in content

    def test_identity_and_status_fields(self, client):
        recording, _t, _s, _summary = _summary_recording()
        response = client.get(f"/recordings/{recording.pk}/")
        content = response.content.decode()
        assert recording.sha256[:12] in content
        assert "transcribed" in content
        assert "detail-1" in content  # source filename placeholder not present; sha shown

    def test_missing_summary_state_friendly(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="nosum-1")
        Recording.objects.filter(pk=recording.pk).update(summary_status=SummaryState.MISSING)
        response = client.get(f"/recordings/{recording.pk}/summary/")
        assert response.status_code == 200
        content = response.content.decode()
        assert "No summary yet" in content

    def test_failed_summary_state_friendly(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="failsum-1")
        Recording.objects.filter(pk=recording.pk).update(summary_status=SummaryState.FAILED)
        response = client.get(f"/recordings/{recording.pk}/summary/")
        content = response.content.decode()
        assert "failed" in content
        assert "Retry" in content

    def test_missing_transcript_state_friendly(self, client):
        recording = Recording.objects.create(sha256="notrans-1")
        response = client.get(f"/recordings/{recording.pk}/")
        assert response.status_code == 200
        assert "No transcript yet" in response.content.decode()

    def test_retranscription_failed_warning_surfaced(self, client):
        recording, transcript, section = make_transcribed_recording(["a"], sha="retx-1")
        make_summary_version(recording, transcript, section)
        attempt = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.NONZERO_EXIT, finished_at=None, error_code="mw_exit_1",
        )
        Recording.objects.filter(pk=recording.pk).update(
            retranscription_failed=True, last_failed_attempt=attempt
        )
        response = client.get(f"/recordings/{recording.pk}/")
        content = response.content.decode()
        assert "Retranscription failed" in content

    def test_unknown_recording_is_404(self, client):
        response = client.get("/recordings/00000000-0000-0000-0000-000000000000/")
        assert response.status_code == 404

    def test_detail_query_count_bounded(self, client):
        recording, transcript, section = make_transcribed_recording(["a"], sha="qc-1")
        make_summary_version(recording, transcript, section)
        for i in range(3):
            make_tag_assignment(recording, make_tag(f"QCTag{i}"))
        with CaptureQueriesContext(connection) as ctx:
            client.get(f"/recordings/{recording.pk}/")
        assert len(ctx.captured_queries) < 40, f"detail page issued {len(ctx.captured_queries)} queries"


class TestTranscriptPagination:
    def test_long_transcript_bounded_per_page(self, client):
        texts = [f"segment {i}" for i in range(450)]
        recording, transcript, _s = make_transcribed_recording(texts, sha="long-1")
        response = client.get(f"/recordings/{recording.pk}/transcript/")
        assert response.status_code == 200
        content = response.content.decode()
        # Default config: 200 segments per page. First page shows 0..199.
        assert "segment 0" in content
        assert "segment 199" in content
        assert "segment 200" not in content
        assert "450 segments" in content
        assert "Page 1 of 3" in content

    def test_transcript_page_2(self, client):
        texts = [f"segment {i}" for i in range(450)]
        recording, _t, _s = make_transcribed_recording(texts, sha="long-2")
        response = client.get(f"/recordings/{recording.pk}/transcript/?page=2")
        content = response.content.decode()
        assert "segment 200" in content
        assert "segment 0<" not in content

    def test_invalid_page_clamped_not_500(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="long-3")
        response = client.get(f"/recordings/{recording.pk}/transcript/?page=999")
        assert response.status_code == 200

    def test_detail_page_first_transcript_page_only(self, client):
        texts = [f"segment {i}" for i in range(450)]
        recording, _t, _s = make_transcribed_recording(texts, sha="long-4")
        response = client.get(f"/recordings/{recording.pk}/")
        content = response.content.decode()
        assert "segment 199" in content
        assert "segment 200" not in content

    def test_unknown_transcript_version_404(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="long-5")
        response = client.get(f"/recordings/{recording.pk}/transcript/?v=999999")
        assert response.status_code == 404


class TestSummaryVersions:
    def test_current_summary_page(self, client):
        recording, _t, _s, summary = _summary_recording()
        response = client.get(f"/recordings/{recording.pk}/summary/")
        assert response.status_code == 200
        content = response.content.decode()
        assert summary.title in content
        assert "current summary" in content

    def test_historical_summary_labelled_not_current(self, client):
        recording, transcript, section = make_transcribed_recording(["v1"], sha="hist-1")
        old_summary = make_summary_version(recording, transcript, section, title="V1 summary")
        # Create a second transcript (retranscription) + its own summary.
        attempt2 = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS, finished_at=None,
        )
        transcript2 = Transcript.objects.create(recording=recording, attempt=attempt2, text_normalized="v2")
        from workflow.models import Section

        section2 = Section.objects.create(transcript=transcript2, ordinal=0)
        from django.utils import timezone as tz

        # Retire the old transcript first, then promote the new one — the
        # partial unique constraint allows only one active transcript.
        transcript.is_active = False
        transcript.superseded_at = tz.now()
        transcript.save()
        transcript2.is_active = True
        transcript2.activated_at = tz.now()
        transcript2.save()
        new_summary = make_summary_version(recording, transcript2, section2, title="V2 summary")

        # Old summary is STILL active in its own scope — but historical.
        old_summary.refresh_from_db()
        assert old_summary.is_active is True

        response = client.get(f"/recordings/{recording.pk}/summaries/{old_summary.pk}/")
        assert response.status_code == 200
        content = response.content.decode()
        assert "HISTORICAL" in content
        assert "V1 summary" in content
        assert "not the current summary" in content
        # History table also labels it historical-for-recording.
        response = client.get(f"/recordings/{recording.pk}/history/")
        content = response.content.decode()
        assert "V1 summary" in content
        assert "historical for this recording" in content
        assert "V2 summary" in content
        assert "current" in content

    def test_cross_recording_summary_is_404(self, client):
        rec_a, _t, _s, summary_a = _summary_recording()
        rec_b, _t2, _s2 = make_transcribed_recording(["b"], sha="other-1")
        response = client.get(f"/recordings/{rec_b.pk}/summaries/{summary_a.pk}/")
        assert response.status_code == 404

    def test_history_lists_attempts_sanitized(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="hist-2")
        ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.NONZERO_EXIT, error_code="mw_exit_1",
            error_message="Transcribing x.mp3... | Error: boom", finished_at=None,
        )
        response = client.get(f"/recordings/{recording.pk}/history/")
        content = response.content.decode()
        assert "mw_exit_1" in content
        # Bounded sanitized detail is rendered (re-sanitized at the
        # rendering boundary), never raw stderr.
        assert "Transcribing x.mp3... | Error: boom" in content

    def test_history_error_detail_rendered_sanitized(self, client):
        """Rendering-boundary sanitization: unsafe content in a historical
        row (e.g. written by older versions) is sanitized for display."""
        recording, _t, _s = make_transcribed_recording(["a"], sha="hist-3")
        ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.NONZERO_EXIT, error_code="mw_exit_1",
            error_message="failed on /Users/harry/secret/inbox/file.wav", finished_at=None,
        )
        response = client.get(f"/recordings/{recording.pk}/history/")
        content = response.content.decode()
        assert "/Users/harry/secret/inbox/file.wav" not in content
        assert "&lt;path&gt;" in content  # escaped <path> replacement


class TestGetPurity:
    def test_detail_get_makes_no_writes(self, client):
        recording, _t, _s, _summary = _summary_recording()
        before_attempts = ProcessingAttempt.objects.count()
        before_recordings = Recording.objects.count()
        client.get(f"/recordings/{recording.pk}/")
        assert ProcessingAttempt.objects.count() == before_attempts
        assert Recording.objects.count() == before_recordings

    def test_list_get_makes_no_writes(self, client):
        _summary_recording()
        before = Recording.objects.count()
        client.get("/recordings/")
        assert Recording.objects.count() == before

    def test_export_get_makes_no_writes(self, client):
        recording, _t, _s, _summary = _summary_recording()
        before = Recording.objects.count()
        client.get(f"/recordings/{recording.pk}/summary/export/?format=markdown")
        assert Recording.objects.count() == before

    def test_detail_get_no_routing_or_hash_side_effects(self, client):
        recording, _t, _s, _summary = _summary_recording()
        before_decisions = RoutingDecision.objects.count()
        client.get(f"/recordings/{recording.pk}/")
        assert RoutingDecision.objects.count() == before_decisions
