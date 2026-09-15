"""A canonical split parent is excluded from the ``awaiting_summary``
Review category (and the matching global Review badge condition).

A Recording whose active transcript has a fully canonical active split
layout has its recording-backed Library item replaced by its Section
items. Its fixed ordinal-0 whole-recording summary being missing is
therefore notification-only: it must NOT appear in "Awaiting first
summary" or contribute to the global badge for that ONE condition, while
every other Review category (pipeline/routing/retranscription/failed
summary/re-summarization/missing audio) still reports and counts the same
parent normally. Unsplit, crop-only, historical-layout and malformed-
layout recordings still project as a recording item and remain reported.

The exclusion reuses the ONE shared canonical-layout SQL predicate
(``segmentation.canonical_hidden_recording_ids``) through
``review.awaiting_summary_condition`` — page/report and badge share it.
Read-only: no lock, network or writes.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from factories import make_transcribed_recording
from workflow.context_processors import review_badge_count
from workflow.models import Recording, Section, SegmentedVersion, SummaryState
from workflow.services.archive import archive_recording
from workflow.services.review import build_review_report
from workflow.services.segmentation import save_segmented_version

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


@pytest.fixture
def client():
    return Client()


def _split(recording, transcript, splits, titles, start=0, end=None):
    if end is None:
        end = transcript.segments.count()
    result = save_segmented_version(
        recording.pk, transcript.pk, start, end, list(splits), list(titles)
    )
    return list(
        Section.objects.filter(segmented_version_id=result.version_id).order_by("ordinal")
    )


def _awaiting_ids(report):
    return [item["recording_id"] for item in report["awaiting_summary"]]


def _all_report_ids(report):
    return [
        item["recording_id"]
        for group in report.values()
        if isinstance(group, list)
        for item in group
    ]


def _groups(response):
    return {group["key"]: group for group in response.context["groups"]}


class TestCanonicalSplitMissingSummary:
    def test_canonical_split_parent_excluded_from_report_page_and_badge(self, client):
        recording, transcript, _s = make_transcribed_recording(
            ["a", "b", "c"], sha="split-missing"
        )
        _split(recording, transcript, [1, 2], ["One", "Two", "Three"])

        report = build_review_report()
        assert recording.pk not in _awaiting_ids(report)
        # The missing summary is the ONLY would-be category: the parent
        # appears nowhere on the report.
        assert recording.pk not in _all_report_ids(report)
        assert review_badge_count() == 0

        response = client.get("/review/")
        assert response.status_code == 200
        groups = _groups(response)
        assert groups["awaiting_summary"]["count"] == 0
        assert str(recording.pk) not in response.content.decode()

    def test_unsplit_missing_summary_remains(self, client):
        recording, _t, _s = make_transcribed_recording(["a", "b", "c"], sha="unsplit-missing")

        report = build_review_report()
        assert recording.pk in _awaiting_ids(report)
        assert review_badge_count() == 1
        assert _groups(client.get("/review/"))["awaiting_summary"]["count"] == 1

    def test_crop_only_missing_summary_remains(self, client):
        recording, transcript, _s = make_transcribed_recording(
            ["a", "b", "c"], sha="crop-missing"
        )
        # A crop-only layout has zero topic Sections: the recording-backed
        # item is NOT suppressed.
        save_segmented_version(recording.pk, transcript.pk, 0, 2, [], [])

        report = build_review_report()
        assert recording.pk in _awaiting_ids(report)
        assert review_badge_count() == 1
        assert _groups(client.get("/review/"))["awaiting_summary"]["count"] == 1

    def test_historical_layout_missing_summary_remains(self):
        recording, transcript, _s = make_transcribed_recording(
            ["a", "b", "c"], sha="historical-missing"
        )
        _split(recording, transcript, [1, 2], ["One", "Two", "Three"])
        # Supersede the only revision: the layout is now historical and the
        # recording projects as a recording item again.
        SegmentedVersion.objects.filter(transcript=transcript).update(
            is_active=False, superseded_at=timezone.now()
        )

        report = build_review_report()
        assert recording.pk in _awaiting_ids(report)
        assert review_badge_count() == 1

    def test_malformed_layout_missing_summary_remains(self):
        recording, transcript, _s = make_transcribed_recording(
            ["a", "b", "c"], sha="malformed-missing"
        )
        sections = _split(recording, transcript, [1, 2], ["One", "Two", "Three"])
        # Corrupt stored state: a blank/whitespace topic title fails the
        # shared canonical predicate closed, so the parent is NOT hidden.
        Section.objects.filter(pk=sections[0].pk).update(title="   ")

        report = build_review_report()
        assert recording.pk in _awaiting_ids(report)
        assert review_badge_count() == 1


class TestSplitParentOtherCategoriesUnchanged:
    def test_missing_audio_still_reported_and_counted_once(self):
        recording, transcript, _s = make_transcribed_recording(
            ["a", "b", "c"], sha="split-missing-audio"
        )
        _split(recording, transcript, [1, 2], ["One", "Two", "Three"])
        Recording.objects.filter(pk=recording.pk).update(audio_status="missing")

        report = build_review_report()
        assert recording.pk not in _awaiting_ids(report)
        assert recording.pk in [i["recording_id"] for i in report["missing_audio"]]
        # Distinct-recording badge semantics: the (excluded) awaiting
        # condition does not shadow the actionable missing-audio one.
        assert review_badge_count() == 1

    def test_failed_summary_still_reported_and_counted(self, client):
        recording, transcript, _s = make_transcribed_recording(
            ["a", "b", "c"], sha="split-failed-summary"
        )
        _split(recording, transcript, [1, 2], ["One", "Two", "Three"])
        Recording.objects.filter(pk=recording.pk).update(summary_status=SummaryState.FAILED)

        report = build_review_report()
        assert recording.pk not in _awaiting_ids(report)
        assert recording.pk in [i["recording_id"] for i in report["failed_summary"]]
        assert review_badge_count() == 1
        assert _groups(client.get("/review/"))["failed_summary"]["count"] == 1

    def test_archived_split_parent_excluded_everywhere(self):
        recording, transcript, _s = make_transcribed_recording(
            ["a", "b", "c"], sha="split-archived"
        )
        _split(recording, transcript, [1, 2], ["One", "Two", "Three"])
        Recording.objects.filter(pk=recording.pk).update(audio_status="missing")
        assert review_badge_count() == 1

        archive_recording(recording)
        assert review_badge_count() == 0
        assert recording.pk not in _all_report_ids(build_review_report())


class TestBadgeQueryBudget:
    def test_badge_remains_one_query(self):
        recording, transcript, _s = make_transcribed_recording(
            ["a", "b", "c"], sha="badge-one-query"
        )
        _split(recording, transcript, [1, 2], ["One", "Two", "Three"])
        Recording.objects.filter(pk=recording.pk).update(audio_status="missing")

        with CaptureQueriesContext(connection) as ctx:
            count = review_badge_count()
        assert count == 1
        assert len(ctx.captured_queries) == 1, [
            q["sql"] for q in ctx.captured_queries
        ]
        # The shared canonical-layout predicate is embedded as a subquery —
        # no Python id materialization.
        sql = ctx.captured_queries[0]["sql"].lower()
        assert "workflow_segmentedversion" in sql
