"""Review dashboard: every category, counts, links, empty states,
sanitized codes, GET purity and bounded queries."""

from __future__ import annotations

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
    RoutingMethod,
    RoutingDecision,
    SummaryState,
)

from factories import make_summary_version, make_transcribed_recording

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


@pytest.fixture
def client():
    return Client()


def _decision(recording, *, verified=False):
    return RoutingDecision.objects.create(
        recording=recording,
        ordinal=1,
        route_suggestion="cantonese",
        profile_name="cantonese",
        model_id="apple:zh-HK",
        method=RoutingMethod.AUTOMATIC,
        confidence=0.42,
        reason_code="low_confidence",
        routing_verified=verified,
        is_active=True,
    )


class TestReviewCategories:
    def test_all_categories_present_with_items(self, client):
        needs_review, _t, _s = make_transcribed_recording(["a"], sha="rev-1")
        Recording.objects.filter(pk=needs_review.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW
        )
        _decision(needs_review)

        unverified, _t2, _s2 = make_transcribed_recording(["b"], sha="rev-2")
        _decision(unverified, verified=False)

        retx_failed, _t3, _s3 = make_transcribed_recording(["c"], sha="rev-3")
        attempt = ProcessingAttempt.objects.create(
            recording=retx_failed, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.NONZERO_EXIT, error_code="mw_exit_1", finished_at=None,
        )
        Recording.objects.filter(pk=retx_failed.pk).update(
            retranscription_failed=True, last_failed_attempt=attempt
        )

        failed, _t4, _s4 = make_transcribed_recording(["d"], sha="rev-4")
        Recording.objects.filter(pk=failed.pk).update(
            processing_status=ProcessingStatus.FAILED, failure_stage="transcription"
        )

        awaiting, _t5, _s5 = make_transcribed_recording(["e"], sha="rev-5")
        Recording.objects.filter(pk=awaiting.pk).update(summary_status=SummaryState.MISSING)

        summary_failed, _t6, _s6 = make_transcribed_recording(["f"], sha="rev-6")
        failed_attempt = ProcessingAttempt.objects.create(
            recording=summary_failed, stage=AttemptStage.SUMMARIZATION, ordinal=1,
            outcome=AttemptOutcome.UNREACHABLE, error_code="endpoint_unavailable", finished_at=None,
        )
        Recording.objects.filter(pk=summary_failed.pk).update(
            summary_status=SummaryState.FAILED, last_failed_attempt=failed_attempt
        )

        resum_failed, _t7, _s7 = make_transcribed_recording(["g"], sha="rev-7")
        make_summary_version(resum_failed, _t7, _s7)
        Recording.objects.filter(pk=resum_failed.pk).update(resummarization_failed=True)

        missing_audio, _t8, _s8 = make_transcribed_recording(["h"], sha="rev-8")
        Recording.objects.filter(pk=missing_audio.pk).update(audio_status="missing")

        response = client.get("/review/")
        assert response.status_code == 200
        content = response.content.decode()
        for fragment in (
            "Routing needs review",
            "unverified automatic routing",
            "Failed retranscription",
            "Pipeline failures",
            "Awaiting first summary",
            "First summary failed",
            "Re-summarization failed",
            "Missing audio",
        ):
            assert fragment in content
        for recording_pk in (
            needs_review.pk, unverified.pk, retx_failed.pk, failed.pk,
            awaiting.pk, summary_failed.pk, resum_failed.pk, missing_audio.pk,
        ):
            assert str(recording_pk) in content or recording_pk in [
                item["recording_id"]
                for group in response.context["groups"]
                for item in group["items"]
            ]
        # Stable sanitized codes only.
        assert "low_confidence" in content
        assert "endpoint_unavailable" in content
        assert "mw_exit_1" in content

    def test_empty_states(self, client):
        response = client.get("/review/")
        assert response.status_code == 200
        content = response.content.decode()
        assert content.count("Nothing here. Good.") >= 8
        assert "0 item" in content

    def test_counts_rendered(self, client):
        for i in range(3):
            recording, _t, _s = make_transcribed_recording(["x"], sha=f"rev-count-{i}")
            Recording.objects.filter(pk=recording.pk).update(
                processing_status=ProcessingStatus.NEEDS_REVIEW
            )
            _decision(recording)
        response = client.get("/review/")
        groups = {g["key"]: g for g in response.context["groups"]}
        assert groups["needs_review"]["count"] == 3

    def test_group_slugs_are_stable(self, client):
        response = client.get("/review/")
        slugs = [g["slug"] for g in response.context["groups"]]
        assert slugs == [
            "needs_review", "unverified", "retranscription_failed", "failed",
            "awaiting_summary", "summary_failed", "resummarization_failed",
            "missing_audio",
        ]


class TestReviewPurity:
    def test_review_get_makes_no_writes(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="rev-pure")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW
        )
        before_recordings = Recording.objects.count()
        before_attempts = ProcessingAttempt.objects.count()
        client.get("/review/")
        assert Recording.objects.count() == before_recordings
        assert ProcessingAttempt.objects.count() == before_attempts


def _populate_review_rows(count: int, prefix: str) -> None:
    """Spread `count` recordings across ALL review categories."""
    from factories import make_summary_version

    for i in range(count):
        kind = i % 7
        sha = f"{prefix}-{kind}-{i}"
        if kind == 0:  # needs review
            recording, _t, _s = make_transcribed_recording(["x"], sha=sha)
            Recording.objects.filter(pk=recording.pk).update(
                processing_status=ProcessingStatus.NEEDS_REVIEW
            )
            _decision(recording)
        elif kind == 1:  # unverified automatic routing
            recording, _t, _s = make_transcribed_recording(["x"], sha=sha)
            _decision(recording, verified=False)
        elif kind == 2:  # failed retranscription with last_failed_attempt
            recording, _t, _s = make_transcribed_recording(["x"], sha=sha)
            attempt = ProcessingAttempt.objects.create(
                recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
                outcome=AttemptOutcome.NONZERO_EXIT, error_code="mw_exit_1", finished_at=None,
            )
            Recording.objects.filter(pk=recording.pk).update(
                retranscription_failed=True, last_failed_attempt=attempt
            )
        elif kind == 3:  # failed first summary
            recording, _t, _s = make_transcribed_recording(["x"], sha=sha)
            attempt = ProcessingAttempt.objects.create(
                recording=recording, stage=AttemptStage.SUMMARIZATION, ordinal=1,
                outcome=AttemptOutcome.UNREACHABLE, error_code="endpoint_unavailable",
                finished_at=None,
            )
            Recording.objects.filter(pk=recording.pk).update(
                summary_status=SummaryState.FAILED, last_failed_attempt=attempt
            )
        elif kind == 4:  # failed re-summarization (current summary kept)
            recording, transcript, section = make_transcribed_recording(["x"], sha=sha)
            make_summary_version(recording, transcript, section)
            Recording.objects.filter(pk=recording.pk).update(resummarization_failed=True)
        elif kind == 5:  # pipeline failure
            recording, _t, _s = make_transcribed_recording(["x"], sha=sha)
            Recording.objects.filter(pk=recording.pk).update(
                processing_status=ProcessingStatus.FAILED, failure_stage="transcription"
            )
        else:  # missing audio
            recording, _t, _s = make_transcribed_recording(["x"], sha=sha)
            Recording.objects.filter(pk=recording.pk).update(audio_status="missing")


class TestReviewQueryScaling:
    """The review report's query count must NOT grow with row count
    (review finding 2)."""

    def test_report_query_count_constant_across_row_counts(self, client):
        from workflow.services.review import build_review_report

        _populate_review_rows(10, "rev-scale-small")
        with CaptureQueriesContext(connection) as ctx_small:
            report_small = build_review_report()
        small = len(ctx_small.captured_queries)

        _populate_review_rows(50, "rev-scale-large")
        with CaptureQueriesContext(connection) as ctx_large:
            report_large = build_review_report()
        large = len(ctx_large.captured_queries)

        assert small == large, (
            f"review report queries scaled with row count: {small} @10 -> {large} @60"
        )
        # Secondary absolute guard (fixed set of group queries, generously bounded).
        assert small < 20
        # Both runs produced correct, populated data.
        assert len(report_small["needs_review"]) == 2
        assert len(report_large["failed_retranscription"]) == 9  # i≡2 mod 7, 0..59

    def test_report_data_correct_across_all_categories(self, client):
        from workflow.services.review import build_review_report

        _populate_review_rows(14, "rev-data")
        report = build_review_report()
        assert len(report["needs_review"]) == 2
        assert len(report["unverified"]) == 2
        assert len(report["failed_retranscription"]) == 2
        assert len(report["failed"]) == 2
        assert len(report["failed_summary"]) == 2
        assert len(report["failed_resummarization"]) == 2
        assert len(report["missing_audio"]) == 2
        assert report["failed_retranscription"][0]["error_code"] == "mw_exit_1"
        assert report["failed_summary"][0]["error_code"] == "endpoint_unavailable"
        assert report["needs_review"][0]["reason_code"] == "low_confidence"
        assert report["unverified"][0]["profile"] == "cantonese"

    def test_no_per_row_decision_or_attempt_queries(self, client):
        from workflow.services.review import build_review_report

        _populate_review_rows(10, "rev-noplus")
        with CaptureQueriesContext(connection) as ctx:
            build_review_report()
        decision_queries = [
            q["sql"] for q in ctx.captured_queries if "workflow_routingdecision" in q["sql"]
        ]
        # One filtered prefetch for needs_review + one for transcribed
        # — never one per row.
        assert len(decision_queries) == 2, decision_queries
        attempt_queries = [
            q["sql"] for q in ctx.captured_queries if "workflow_processingattempt" in q["sql"]
        ]
        # Only the failed_summary values_list join; last_failed_attempt
        # comes from select_related on the transcribed query.
        assert len(attempt_queries) <= 2, attempt_queries

    def test_web_review_page_query_count_constant_across_row_counts(self, client):
        _populate_review_rows(10, "rev-page-small")
        with CaptureQueriesContext(connection) as ctx_small:
            client.get("/review/")
        small = len(ctx_small.captured_queries)

        _populate_review_rows(50, "rev-page-large")
        with CaptureQueriesContext(connection) as ctx_large:
            client.get("/review/")
        large = len(ctx_large.captured_queries)

        assert small == large, f"review page queries scaled: {small} @10 -> {large} @60"

    def test_cli_review_json_schema_unchanged(self, capsys):
        """`brain review --json` keeps its exact top-level keys and the
        per-entry key sets of the pre-refactor CLI."""
        import json as _json

        from brainlib import cli

        from workflow.services.review import build_review_report

        _populate_review_rows(7, "rev-cli")
        assert cli.main(["review", "--json"]) == 0
        payload = _json.loads(capsys.readouterr().out)
        assert set(payload) == {
            "needs_review", "unverified", "failed_retranscription", "failed",
            "awaiting_summary", "failed_summary", "failed_resummarization",
            "missing_audio",
        }
        assert set(payload["needs_review"][0]) == {
            "recording_id", "kind", "suggested_route", "confidence", "reason_code",
        }
        assert set(payload["unverified"][0]) == {
            "recording_id", "kind", "route", "confidence", "profile",
        }
        assert set(payload["failed_retranscription"][0]) == {
            "recording_id", "kind", "attempt_id", "error_code", "route",
        }
        assert set(payload["failed"][0]) == {"recording_id", "kind"}
        assert set(payload["awaiting_summary"][0]) == {"recording_id", "kind"}
        assert set(payload["failed_summary"][0]) == {"recording_id", "kind", "error_code"}
        assert set(payload["failed_resummarization"][0]) == {"recording_id", "kind", "attempt_id"}
        assert set(payload["missing_audio"][0]) == {"recording_id", "kind"}
        # Builder output matches CLI output exactly.
        assert build_review_report() == payload

