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

import re

import pytest
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.db import connection

from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    AudioStatus,
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

    def test_detail_page_exactly_five_preview_segments(self, client):
        texts = [f"segment {i}" for i in range(450)]
        recording, _t, _s = make_transcribed_recording(texts, sha="long-4")
        response = client.get(f"/recordings/{recording.pk}/")
        content = response.content.decode()
        # Exactly the first 5 active-transcript segments are previewed on
        # the detail page; the transcript page owns the rest.
        for i in range(5):
            assert f"segment {i}" in content
        assert "segment 5" not in content
        assert "450 segments" in content  # accurate total + open link
        assert "Open transcript" in content

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


class TestSummaryHeadingHierarchy:
    """The shared _summary_body partial is context-aware: Recording
    Detail embeds it in detail mode (styled title paragraph + h3
    sections under its Summary h2), while the standalone current and
    historical summary pages default to h2 sections under their own h1
    and never repeat the title paragraph."""

    def test_detail_embedded_mode_h3_under_summary_h2(self, client):
        recording, _t, _s, _summary = _summary_recording()
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert '<h2 class="overview-block-title" id="summary-title">Summary</h2>' in content
        assert '<p class="summary-title">' in content  # title as styled paragraph
        for heading in ("Overview", "Key points", "Action items", "People", "Topics"):
            assert f"<h3>{heading}</h3>" in content, heading
        assert "<h4>" not in content

    def test_standalone_current_summary_h2_no_title_repeat(self, client):
        recording, _t, _s, summary = _summary_recording()
        response = client.get(f"/recordings/{recording.pk}/summary/")
        assert response.status_code == 200
        content = response.content.decode()
        assert "<h1>" in content  # page h1 carries the title
        for heading in ("Overview", "Key points", "Action items", "People", "Topics"):
            assert f"<h2>{heading}</h2>" in content, heading
        assert "<h3>" not in content  # no embedded-mode h3 sections
        assert '<p class="summary-title">' not in content  # no title repeat
        assert summary.title in content  # title still present via the h1

    def test_historical_summary_h2_no_title_repeat(self, client):
        recording, transcript, _s = make_transcribed_recording(["v1"], sha="hier-hist")
        section = transcript.sections.get(ordinal=0)
        old = make_summary_version(recording, transcript, section, title="V1 summary")
        # Retire the transcript: the summary stays active in its own scope
        # but is historical for the recording.
        transcript.is_active = False
        transcript.save()
        response = client.get(f"/recordings/{recording.pk}/summaries/{old.pk}/")
        assert response.status_code == 200
        content = response.content.decode()
        for heading in ("Overview", "Key points", "Action items", "People", "Topics"):
            assert f"<h2>{heading}</h2>" in content, heading
        assert "<h3>" not in content
        assert '<p class="summary-title">' not in content
        assert "V1 summary" in content  # title via the page h1


class TestStatusPanel:
    """The single composite status/next-action panel covers the real
    state matrix: healthy, failed/retry, retranscription-failed,
    ready-to-transcribe, needs-review, summary failed / regeneration
    failed / missing, missing audio, running, and unverified routing."""

    def _status(self, client, recording):
        response = client.get(f"/recordings/{recording.pk}/")
        assert response.status_code == 200
        content = response.content.decode()
        match = re.search(
            r'<div class="status-panel status-panel-(\w+)"[^>]*>\s*'
            r'<span class="status-panel-label">Status</span>\s*'
            r'<span class="status-panel-main">(.*?)</span>',
            content,
            re.DOTALL,
        )
        assert match, "status panel not found"
        return match.group(1), match.group(2)

    def test_healthy_no_action_required(self, client):
        recording, _t, _s, _summary = _summary_recording()
        level, detail = self._status(client, recording)
        assert level == "ok"
        assert "no action required" in detail

    def test_failed_offers_retry(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-fail")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.FAILED, failure_stage="transcription"
        )
        recording.refresh_from_db()
        level, detail = self._status(client, recording)
        assert level == "danger"
        assert "retry is available" in detail

    def test_retranscription_failed_keeps_current(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-retx")
        Recording.objects.filter(pk=recording.pk).update(retranscription_failed=True)
        level, detail = self._status(client, recording)
        assert level == "warn"
        assert "existing transcript stays active" in detail

    def test_ready_to_transcribe(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-ready")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.READY_TO_TRANSCRIBE
        )
        level, detail = self._status(client, recording)
        assert level == "warn"
        assert "Ready to transcribe" in detail

    def test_needs_review(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-review")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW
        )
        level, detail = self._status(client, recording)
        assert level == "warn"
        assert "Routing needs review" in detail

    def test_summary_failed(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-sumfail")
        Recording.objects.filter(pk=recording.pk).update(summary_status=SummaryState.FAILED)
        level, detail = self._status(client, recording)
        assert level == "danger"
        assert "summarization attempt failed" in detail

    def test_resummarization_failed_keeps_summary(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-regen")
        Recording.objects.filter(pk=recording.pk).update(resummarization_failed=True)
        level, detail = self._status(client, recording)
        assert level == "warn"
        assert "current summary was kept" in detail

    def test_missing_audio(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-audio")
        Recording.objects.filter(pk=recording.pk).update(audio_status=AudioStatus.MISSING)
        level, detail = self._status(client, recording)
        assert level == "warn"
        assert "Audio missing" in detail

    def test_summary_missing(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-nosum")
        Recording.objects.filter(pk=recording.pk).update(summary_status=SummaryState.MISSING)
        level, detail = self._status(client, recording)
        assert level == "warn"
        assert "Summary not generated" in detail

    def test_running_attempt(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-run")
        ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.RUNNING, finished_at=None,
        )
        level, detail = self._status(client, recording)
        assert level == "running"
        assert "currently running" in detail

    def test_unverified_routing_warns(self, client):
        recording, _t, _s, _summary = _summary_recording()
        RoutingDecision.objects.create(
            recording=recording, ordinal=1, route_suggestion="european",
            profile_name="european", model_id="m", method=RoutingMethod.AUTOMATIC,
            routing_verified=False, is_active=True,
        )
        level, detail = self._status(client, recording)
        assert level == "warn"
        assert "routing unverified" in detail


class TestNestedKeyPoints:
    """Structured key points render as real nested <ol>/<li> semantics
    with an h1/h2/h3 hierarchy; historical strings and malformed rows
    stay safely readable flat items, always autoescaped."""

    def _detail_with_points(self, client, points, sha="kp-1"):
        recording, transcript, section = make_transcribed_recording(["x"], sha=sha)
        make_summary_version(recording, transcript, section, key_points=points)
        response = client.get(f"/recordings/{recording.pk}/")
        assert response.status_code == 200
        return response.content.decode()

    def test_nested_structured_points_render_nested_lists(self, client):
        content = self._detail_with_points(
            client,
            [
                {"text": "First level one", "level": 1},
                {"text": "Nested under first", "level": 2},
                {"text": "Deeper under first", "level": 3},
                {"text": "Second level one", "level": 1},
                {"text": "Nested under second", "level": 2},
            ],
        )
        compact = re.sub(r"\s+<", "<", content)
        # Real <ol>/<li> nesting: top list + one nested list per item
        # with children, browser numbering reproduces 1. / 1.1 / 1.1.1.
        kp = content[content.find("Key points"):content.find("Action items")]
        assert kp.count("<ol") == 4
        assert kp.count("</ol>") == 4
        assert kp.count("<li>") == 5
        assert (
            '<li>First level one<ol class="key-points"><li>Nested under first'
            '<ol class="key-points"><li>Deeper under first</li></ol></li></ol></li>'
            '<li>Second level one<ol class="key-points"><li>Nested under second</li></ol></li>'
        ) in compact

    def test_key_point_text_is_autoescaped(self, client):
        content = self._detail_with_points(
            client, [{"text": "<img src=x onerror=1>", "level": 1}], sha="kp-xss"
        )
        assert "<img src=x" not in content
        assert "&lt;img" in content

    def test_historical_strings_and_malformed_rows_stay_readable(self, client):
        content = self._detail_with_points(
            client,
            [
                "Plain historical point",
                {"text": "Level zero point", "level": 0},
                {"text": "Orphaned deeper point", "level": 2},
                {"text": 12345},   # non-str text: never coerced
                42,                # non-dict rows are skipped
                None,
            ],
            sha="kp-hist",
        )
        compact = re.sub(r"\s+<", "<", content)
        assert "<li>Plain historical point</li>" in compact
        assert "<li>Level zero point</li>" in compact
        assert "<li>Orphaned deeper point</li>" in compact
        assert "<li>12345</li>" not in compact
        # Only the single top-level list — no nesting for fallback rows.
        assert content.count("<ol") == 1

    def test_section_headings_are_h3(self, client):
        recording, transcript, section = make_transcribed_recording(["x"], sha="kp-h")
        make_summary_version(recording, transcript, section)
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        # Detail hierarchy: h1 (title) -> h2 (Summary) -> h3 sections;
        # the summary title is a styled paragraph, never a heading.
        for heading in ("Overview", "Key points", "Action items", "People", "Topics"):
            assert f"<h3>{heading}</h3>" in content, heading
        assert "<h4>" not in content
        assert '<p class="summary-title">' in content


class TestTranscriptScreen:
    def test_segment_anchors_present(self, client):
        recording, _t, _s = make_transcribed_recording(["a", "b"], sha="anchor-1")
        response = client.get(f"/recordings/{recording.pk}/transcript/")
        content = response.content.decode()
        assert 'id="segment-0"' in content
        assert 'id="segment-1"' in content

    def test_meta_context_and_export_links(self, client):
        recording, transcript, _s = make_transcribed_recording(["a"], sha="meta-1")
        transcript.language_observed = "fi"
        transcript.save(update_fields=["language_observed"])
        recording.attempts.filter(stage=AttemptStage.TRANSCRIPTION).update(model_id="parakeet-v3")
        response = client.get(f"/recordings/{recording.pk}/transcript/")
        content = response.content.decode()
        assert "1 segments" in content
        assert "language: fi" in content
        assert "model: parakeet-v3" in content
        assert "version: #" in content
        # Copy button reuses the existing export-URL mechanism; plain and
        # timestamped downloads stay available.
        assert "data-copy-url" in content
        assert "transcript/export/?format=text" in content
        assert "transcript/export/?format=timestamped" in content

    def test_transcript_page_queries_bounded_no_lazy_attempt(self, client):
        """The transcript is fetched with select_related('attempt'), so
        metadata rendering never triggers an incidental per-page query."""
        recording, _t, _s = make_transcribed_recording(["a"] * 10, sha="qc-tx")
        recording.attempts.filter(stage=AttemptStage.TRANSCRIPTION).update(model_id="parakeet-v3")
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(f"/recordings/{recording.pk}/transcript/")
        assert response.status_code == 200
        assert len(ctx.captured_queries) < 20, len(ctx.captured_queries)

    def test_historical_version_banner_and_versioned_exports(self, client):
        from django.utils import timezone as tz

        recording, transcript, _s = make_transcribed_recording(["old"], sha="histv-1")
        attempt2 = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS, finished_at=tz.now(),
        )
        transcript2 = Transcript.objects.create(recording=recording, attempt=attempt2, text_normalized="new")
        transcript.is_active = False
        transcript.superseded_at = tz.now()
        transcript.save()
        transcript2.is_active = True
        transcript2.activated_at = tz.now()
        transcript2.save()
        response = client.get(f"/recordings/{recording.pk}/transcript/?v={transcript.pk}")
        content = response.content.decode()
        assert "HISTORICAL transcript version" in content
        assert f"version={transcript.pk}" in content  # export links keep version=
        assert "old" in content  # the historical text renders


class TestDetailRedesign:
    def test_no_recent_attempts_table_on_detail(self, client):
        """Audit data lives on History; the detail page must not render a
        recent-attempts table or its raw attempt details."""
        recording, _t, _s, _summary = _summary_recording()
        ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.NONZERO_EXIT, error_code="mw_exit_1",
            error_message="raw stderr line", finished_at=None,
        )
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert "Recent attempts" not in content
        assert "mw_exit_1" not in content

    def test_collapsed_provenance_and_technical_details(self, client):
        recording, _t, _s, _summary = _summary_recording()
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'class="technical-details"' in content
        assert "Summary provenance" in content
        assert "Technical details" in content
        # JSON export stays available, but secondary (inside the
        # collapsed provenance block).
        assert "format=json" in content

    def test_detail_get_is_select_only(self, client):
        recording, _t, _s, _summary = _summary_recording()
        make_tag_assignment(recording, make_tag("DetailSelect"))
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(f"/recordings/{recording.pk}/")
        assert response.status_code == 200
        non_select = [
            q for q in ctx.captured_queries
            if not q["sql"].lstrip().upper().startswith("SELECT")
        ]
        assert non_select == []


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
