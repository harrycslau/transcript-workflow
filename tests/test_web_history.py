"""History page (v6 redesign): safe bounded captioned tables, routing
allowlist, source-display safety, truncation notices, GET purity.

Proves (per the approved plan):
- routing history exposes ONLY allowlisted safe fields (timestamp,
  profile, method, verification, bounded confidence label, model,
  stable reason code) — never raw evidence JSON;
- every potentially long collection is bounded by the local fixed limit
  with an exact truncation notice and exactly 100 rendered rows;
- source display uses the safe original filename only, never the source
  path; audio status is present/missing only, never "unchanged";
- attempts render through the existing sanitizer (no raw stderr/argv/
  context);
- transcript versions stay on History alongside transcription attempts
  and link to the historical transcript screen via ?v=;
- GETs are strictly read-only (SELECT only, no writes/external calls).
"""

from __future__ import annotations

import re

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    AudioSource,
    AudioStatus,
    ProcessingAttempt,
    Recording,
    RoutingDecision,
    RoutingMethod,
    Summary,
    Transcript,
)

from factories import make_summary_version, make_transcribed_recording

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


@pytest.fixture
def client():
    return Client()


def _summary_recording(sha="hist-1"):
    recording, transcript, section = make_transcribed_recording(["hello world"], sha=sha)
    make_summary_version(recording, transcript, section)
    return recording, transcript, section


def _routing_decision(recording, *, ordinal=1, verified=False, confidence=0.94,
                      reason_code="auto_confident", **overrides):
    return RoutingDecision.objects.create(
        recording=recording, ordinal=ordinal, route_suggestion="european",
        profile_name="european", model_id="parakeet-pro:nvidia_parakeet-v3",
        method=RoutingMethod.AUTOMATIC, confidence=confidence,
        reason_code=reason_code, routing_verified=verified,
        is_active=overrides.pop("is_active", True), **overrides,
    )


class TestHistorySections:
    def test_all_captioned_sections_render(self, client):
        recording, _t, _s = _summary_recording()
        _routing_decision(recording)
        response = client.get(f"/recordings/{recording.pk}/history/")
        assert response.status_code == 200
        content = response.content.decode()
        for heading in ("Routing", "Processing attempts", "Transcript versions",
                        "Summary versions", "Source &amp; technical information"):
            assert heading in content
        # Accessible captioned tables with explicit column scopes.
        tables = re.findall(r'<table class="history-table">(.*?)</table>', content, re.DOTALL)
        assert len(tables) == 4
        for table in tables:
            assert "<caption>" in table
            assert 'scope="col"' in table

    def test_history_get_is_select_only(self, client):
        recording, _t, _s = _summary_recording()
        _routing_decision(recording)
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(f"/recordings/{recording.pk}/history/")
        assert response.status_code == 200
        non_select = [
            q for q in ctx.captured_queries
            if not q["sql"].lstrip().upper().startswith("SELECT")
        ]
        assert non_select == []


class TestRoutingSafety:
    def test_routing_history_allowlisted_fields_only(self, client):
        """Raw evidence JSON (scores, excerpts, paths) is never dumped —
        only the allowlisted safe projection is rendered."""
        recording, _t, _s = _summary_recording()
        _routing_decision(
            recording,
            evidence={
                "classifier": {"cantonese": 0.1, "mandarin": 0.9, "scores": [1, 2, 3]},
                "path": "/Users/harry/secret/inbox/file.wav",
                "snippet": "raw excerpt would be sensitive",
            },
        )
        content = client.get(f"/recordings/{recording.pk}/history/").content.decode()
        # Allowlisted safe fields present.
        assert "european" in content          # profile
        assert "automatic" in content         # method
        assert "no" in content                # verification label
        assert "parakeet-pro:nvidia_parakeet-v3" in content  # model field
        assert "0.94" in content              # bounded confidence label
        assert "auto_confident" in content    # stable reason code
        # Raw evidence never surfaces.
        assert "classifier" not in content
        assert "snippet" not in content
        assert "/Users/" not in content
        assert "scores" not in content

    def test_confidence_missing_renders_dash(self, client):
        recording, _t, _s = _summary_recording()
        _routing_decision(recording, confidence=None)
        content = client.get(f"/recordings/{recording.pk}/history/").content.decode()
        assert "auto_confident" in content
        # No confidence value, no crash.
        assert "<td>—</td>" in content

    def test_routing_query_projects_only_allowlisted_columns(self, client):
        """The history routing query uses an explicit .only(...) with the
        allowlisted fields, so raw evidence is never even loaded."""
        recording, _t, _s = _summary_recording()
        _routing_decision(recording, evidence={"secret": "never-rendered"})
        with CaptureQueriesContext(connection) as ctx:
            client.get(f"/recordings/{recording.pk}/history/")
        routing_sql = ""
        for q in ctx.captured_queries:
            sql = q["sql"]
            # The display query is the routing query carrying the
            # allowlisted display columns (the base prefetch query and
            # the review-badge COUNT join also mention the table).
            if ("workflow_routingdecision" in sql
                    and "reason_code" in sql and "created_at" in sql):
                routing_sql = sql
                break
        assert routing_sql, "allowlisted routing display query not found"
        for column in (
            "created_at", "profile_name", "method", "routing_verified",
            "model_id", "confidence", "reason_code",
        ):
            assert column in routing_sql, column
        assert "evidence" not in routing_sql
        assert "language_arg" not in routing_sql
        assert "route_suggestion" not in routing_sql


class TestHistoryBounds:
    def test_collections_bounded_with_truncation_notices(self, client):
        from django.utils import timezone as tz

        recording, transcript, section = make_transcribed_recording(["x"], sha="flood-1")
        make_summary_version(recording, transcript, section)
        # 105 routing decisions (append-only ordinals).
        for i in range(1, 106):
            RoutingDecision.objects.create(
                recording=recording, ordinal=i, route_suggestion="european",
                profile_name=f"profile-{i:03d}", model_id="m",
                method=RoutingMethod.AUTOMATIC, routing_verified=False,
                is_active=(i == 105),
            )
        # 104 more transcription attempts + inactive transcripts (105 total).
        for i in range(2, 106):
            attempt = ProcessingAttempt.objects.create(
                recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=i,
                outcome=AttemptOutcome.SUCCESS, finished_at=tz.now(),
            )
            Transcript.objects.create(
                recording=recording, attempt=attempt, text_normalized=f"tx-{i}"
            )
        # 104 more summary versions (105 total; inactive rows only).
        for i in range(2, 106):
            attempt = ProcessingAttempt.objects.create(
                recording=recording, stage=AttemptStage.SUMMARIZATION, ordinal=i,
                outcome=AttemptOutcome.SUCCESS, finished_at=tz.now(),
            )
            Summary.objects.create(
                recording=recording, transcript=transcript, section=section,
                attempt=attempt, ordinal=i, is_active=False,
                title=f"S{i}", overview="o", key_points=[], action_items=[],
                people=[], organizations=[], topics=[], language="en",
                output_language="en", suggested_tags_raw={}, model_id="m",
                prompt_version="1", parser_version="1", chunk_count=1,
                input_characters=10, generation_mode="manual",
            )

        response = client.get(f"/recordings/{recording.pk}/history/")
        assert response.status_code == 200
        content = response.content.decode()
        # Every collection shows its exact truncation notice.
        assert content.count("Only the most recent 100") == 4
        # Each table renders exactly 1 header + 100 data rows.
        tables = re.findall(r'<table class="history-table">(.*?)</table>', content, re.DOTALL)
        assert len(tables) == 4
        for table in tables:
            assert table.count("<tr>") == 101, table.count("<tr>")
        # Routing rows are the newest 100 (105..006); the oldest 5 are gone.
        assert "profile-105" in content
        assert "profile-006" in content
        assert "profile-005" not in content


class TestSourceDisplaySafety:
    SENTINEL_PATH = "/Users/harry/secret/inbox/sentinel-only.wav"

    def _source(self, recording):
        return AudioSource.objects.create(
            recording=recording,
            path=self.SENTINEL_PATH,
            path_identity="sentinel-only-path-identity",
            original_filename="safe-name.wav",
            is_canonical=True,
            presence=AudioStatus.PRESENT,
        )

    def test_history_and_detail_show_filename_never_path(self, client):
        recording, _t, _s = _summary_recording()
        self._source(recording)
        for url in (
            f"/recordings/{recording.pk}/history/",
            f"/recordings/{recording.pk}/",
        ):
            content = client.get(url).content.decode()
            assert "safe-name.wav" in content, url
            assert self.SENTINEL_PATH not in content, url
            assert "/Users/" not in content, url
            assert "sentinel-only" not in content, url

    def test_audio_status_present_or_missing_only(self, client):
        recording, _t, _s = _summary_recording()
        Recording.objects.filter(pk=recording.pk).update(audio_status=AudioStatus.MISSING)
        content = client.get(f"/recordings/{recording.pk}/history/").content.decode()
        assert "missing" in content
        assert "unchanged" not in content  # persisted status is never "unchanged"


class TestAttemptSanitization:
    def test_attempts_sanitized_no_raw_provenance(self, client):
        recording, _t, _s = _summary_recording()
        ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.NONZERO_EXIT, error_code="mw_exit_1",
            error_message="failed on /Users/harry/secret/inbox/file.wav",
            cli_args_json=["mw", "--model", "x", "/Users/harry/secret/inbox/file.wav"],
            context_json={"input_path": "/Users/harry/secret/inbox/file.wav"},
        )
        content = client.get(f"/recordings/{recording.pk}/history/").content.decode()
        assert "mw_exit_1" in content
        assert "/Users/" not in content
        assert "&lt;path&gt;" in content  # sanitized path placeholder
        # Raw argv/context JSON is never rendered.
        assert "--model" not in content
        assert "context_json" not in content
        assert "cli_args_json" not in content


class TestTranscriptVersions:
    def test_versions_link_to_historical_transcript(self, client):
        from django.utils import timezone as tz

        recording, transcript, _s = _summary_recording()
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

        content = client.get(f"/recordings/{recording.pk}/history/").content.decode()
        assert "Transcript versions" in content
        # Both versions listed; segment counts link to ?v= pages.
        for t in (transcript, transcript2):
            assert f"href=\"/recordings/{recording.pk}/transcript/?v={t.pk}\"" in content
        # The historical target page renders the historical banner.
        page = client.get(f"/recordings/{recording.pk}/transcript/?v={transcript.pk}")
        assert "HISTORICAL transcript version" in page.content.decode()
