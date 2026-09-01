"""Copy/export endpoints: formats, headers, Unicode, historical versions,
read-only behaviour, filename sanitization."""

from __future__ import annotations

import json

import pytest
from django.test import Client

from workflow.models import Recording

from factories import make_summary_version, make_transcribed_recording

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


@pytest.fixture
def client():
    return Client()


def _summary_recording(sha="exp-1"):
    recording, transcript, section = make_transcribed_recording(["hello world"], sha=sha)
    summary = make_summary_version(recording, transcript, section)
    return recording, transcript, section, summary


class TestSummaryExport:
    def test_markdown_content_and_headers(self, client):
        recording, _t, _s, summary = _summary_recording()
        response = client.get(f"/recordings/{recording.pk}/summary/export/?format=markdown")
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/markdown")
        assert "charset=utf-8" in response["Content-Type"]
        body = response.content.decode("utf-8")
        assert f"# {summary.title}" in body
        assert "## Overview" in body
        assert summary.overview in body
        assert "## Action items" in body
        disposition = response["Content-Disposition"]
        assert 'filename="brain-summary-' in disposition
        assert recording.sha256[:12] in disposition
        # Filenames never contain titles (no header injection vector).
        assert summary.title.replace(" ", "") not in disposition.replace("%20", "")

    def test_text_export(self, client):
        recording, _t, _s, _summary = _summary_recording()
        response = client.get(f"/recordings/{recording.pk}/summary/export/?format=text")
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/plain")
        body = response.content.decode("utf-8")
        assert body.startswith("Title: Meeting about grading")

    def test_json_export_includes_both_activity_flags(self, client):
        recording, _t, _s, _summary = _summary_recording()
        response = client.get(f"/recordings/{recording.pk}/summary/export/?format=json")
        assert response.status_code == 200
        assert response["Content-Type"].startswith("application/json")
        payload = json.loads(response.content.decode("utf-8"))
        assert payload["is_active_in_scope"] is True
        assert payload["is_current_for_recording"] is True
        assert payload["historical"] is False
        assert payload["title"] == "Meeting about grading"
        # No secrets: no API keys, no prompts.
        assert "api_key" not in json.dumps(payload)

    def test_historical_summary_export_labelled(self, client):
        recording, transcript, section = make_transcribed_recording(["v1"], sha="exp-hist")
        old = make_summary_version(recording, transcript, section, title="V1")
        # Retire the transcript: the summary stays active in its own scope
        # but is no longer current for the recording.
        transcript.is_active = False
        transcript.save()
        response = client.get(
            f"/recordings/{recording.pk}/summary/export/?format=json&version={old.pk}"
        )
        payload = json.loads(response.content.decode("utf-8"))
        assert payload["is_active_in_scope"] is True  # still active in its scope
        assert payload["is_current_for_recording"] is False  # but not current
        assert payload["historical"] is True

        response = client.get(
            f"/recordings/{recording.pk}/summary/export/?format=markdown&version={old.pk}"
        )
        body = response.content.decode("utf-8")
        assert "Historical summary" in body
        assert f"# {old.title}" in body

    def test_unicode_roundtrip_cantonese_finnish_emoji(self, client):
        recording, transcript, section = make_transcribed_recording(["x"], sha="exp-uni")
        make_summary_version(
            recording, transcript, section,
            title="會議記錄 🎙️ Päätökset",
            overview=" Cantonais同English混合。Emoji: 👍. Suomea: äöå. ",
        )
        response = client.get(f"/recordings/{recording.pk}/summary/export/?format=markdown")
        body = response.content.decode("utf-8")
        assert "會議記錄 🎙️ Päätökset" in body
        assert "👍" in body
        assert "äöå" in body

    def test_invalid_format_is_400(self, client):
        recording, _t, _s, _summary = _summary_recording()
        response = client.get(f"/recordings/{recording.pk}/summary/export/?format=xml")
        assert response.status_code == 400

    def test_missing_summary_is_404(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="exp-none")
        response = client.get(f"/recordings/{recording.pk}/summary/export/?format=markdown")
        assert response.status_code == 404

    def test_cross_recording_version_is_404(self, client):
        rec_a, _t, _s, summary_a = _summary_recording("exp-xa")
        rec_b, _t2, _s2 = make_transcribed_recording(["b"], sha="exp-xb")
        response = client.get(
            f"/recordings/{rec_b.pk}/summary/export/?format=markdown&version={summary_a.pk}"
        )
        assert response.status_code == 404


class TestTranscriptExport:
    def test_text_export_full_content(self, client):
        recording, transcript, _s = make_transcribed_recording(
            ["first line", "second line"], sha="texp-1"
        )
        response = client.get(f"/recordings/{recording.pk}/transcript/export/?format=text")
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/plain")
        body = response.content.decode("utf-8")
        assert "first line" in body
        assert "second line" in body

    def test_timestamped_export(self, client):
        recording, _t, _s = make_transcribed_recording(["hello"], sha="texp-2")
        response = client.get(f"/recordings/{recording.pk}/transcript/export/?format=timestamped")
        body = response.content.decode("utf-8")
        assert "[00:00] hello" in body

    def test_unicode_transcript(self, client):
        recording, _t, _s = make_transcribed_recording(
            ["廣東話測試 🎧", "äiti"], sha="texp-3"
        )
        response = client.get(f"/recordings/{recording.pk}/transcript/export/?format=text")
        body = response.content.decode("utf-8")
        assert "廣東話測試 🎧" in body
        assert "äiti" in body

    def test_historical_transcript_version(self, client):
        from django.utils import timezone as tz

        from workflow.models import AttemptOutcome, AttemptStage, ProcessingAttempt, Transcript

        recording, transcript, _s = make_transcribed_recording(["old"], sha="texp-4")
        attempt2 = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS, finished_at=tz.now(),
        )
        transcript2 = Transcript.objects.create(
            recording=recording, attempt=attempt2, text_normalized="new"
        )
        transcript.is_active = False
        transcript.save()
        transcript2.is_active = True
        transcript2.save()
        response = client.get(
            f"/recordings/{recording.pk}/transcript/export/?format=text&version={transcript.pk}"
        )
        assert response.status_code == 200
        assert "old" in response.content.decode("utf-8")
        disposition = response["Content-Disposition"]
        assert f"-v{transcript.pk}" in disposition

    def test_no_transcript_is_400(self, client):
        recording = Recording.objects.create(sha256="texp-5")
        response = client.get(f"/recordings/{recording.pk}/transcript/export/?format=text")
        assert response.status_code == 400

    def test_exports_are_read_only(self, client):
        recording, _t, _s, _summary = _summary_recording("texp-6")
        before_recordings = Recording.objects.count()
        before_summaries = recording.summaries.count()
        client.get(f"/recordings/{recording.pk}/summary/export/?format=json")
        client.get(f"/recordings/{recording.pk}/transcript/export/?format=text")
        assert Recording.objects.count() == before_recordings
        assert recording.summaries.count() == before_summaries

    def test_post_not_allowed(self, client):
        recording, _t, _s, _summary = _summary_recording("texp-7")
        response = client.post(f"/recordings/{recording.pk}/summary/export/?format=json", {})
        assert response.status_code == 405
