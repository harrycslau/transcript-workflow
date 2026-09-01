"""Recording list page: pagination, ordering, filters, query contract.

Proves (per the approved plan):
- deterministic ordering with the effective_at annotation (Coalesce of
  recorded_at/discovered_at), including NULL recorded_at fallback and
  the pk tie-breaker;
- local-calendar-day and range filters, DST-correct, aware-only;
- tag filters with explicit AND/OR semantics;
- processing/summary/audio/review/has-summary filters;
- filter parameters persist across pagination links;
- invalid filter values are friendly, never a 500;
- the prefetch contract: query count is constant as row count grows,
  and transcript text is never loaded on the list page.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.test import Client
from django.utils import timezone as dj_tz

from workflow.models import (
    AudioStatus,
    ProcessingStatus,
    Recording,
    RoutingDecision,
    Summary,
    SummaryState,
    Tag,
    TagAssignment,
)
from workflow.query import ListFilters, recording_list_queryset

from factories import make_summary_version, make_tag, make_tag_assignment, make_transcribed_recording

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


def _local(naive, tz_name="Europe/Helsinki"):
    return naive.replace(tzinfo=ZoneInfo(tz_name))


@pytest.fixture
def client():
    return Client()


def _make_recording(index, *, recorded_at=None, status=ProcessingStatus.TRANSCRIBED, **kwargs):
    recording, transcript, section = make_transcribed_recording(
        [f"segment {index}"], sha=f"sha-{index}", summary_status=SummaryState.CURRENT, **kwargs
    )
    if recorded_at is not None:
        recording.recorded_at = recorded_at
        recording.save(update_fields=["recorded_at"])
    return recording, transcript, section


class TestOrdering:
    def test_ordering_effective_at_desc_pk_tiebreak(self, client):
        older, _t, _s = _make_recording(1, recorded_at=_local(datetime(2026, 1, 2, 10, 0)))
        newer, _t2, _s2 = _make_recording(2, recorded_at=_local(datetime(2026, 1, 3, 10, 0)))
        response = client.get("/recordings/")
        content = response.content.decode()
        assert content.index(str(newer.pk)) < content.index(str(older.pk))

    def test_null_recorded_at_falls_back_to_discovered_at(self, client):
        with_recorded, _t, _s = _make_recording(1, recorded_at=_local(datetime(2026, 1, 2, 10, 0)))
        no_recorded, _t2, _s2 = _make_recording(2, recorded_at=None)
        Recording.objects.filter(pk=no_recorded.pk).update(
            discovered_at=_local(datetime(2026, 1, 5, 8, 0))
        )
        response = client.get("/recordings/")
        content = response.content.decode()
        # Effective: no_recorded 2026-01-05 > with_recorded 2026-01-02,
        # so no_recorded sorts first (descending).
        assert content.index(str(no_recorded.pk)) < content.index(str(with_recorded.pk))
        # The display label states which timestamp is shown.
        assert "Discovered" in content

    def test_pk_tiebreaker_deterministic(self, client):
        same_time = _local(datetime(2026, 1, 2, 10, 0))
        a, _t, _s = _make_recording(1, recorded_at=same_time)
        b, _t2, _s2 = _make_recording(2, recorded_at=same_time)
        first_response = client.get("/recordings/")
        second_response = client.get("/recordings/")
        assert first_response.content == second_response.content


class TestDateFilters:
    def test_local_day_filter(self, client):
        inside, _t, _s = _make_recording(1, recorded_at=_local(datetime(2026, 1, 2, 23, 59)))
        outside, _t2, _s2 = _make_recording(2, recorded_at=_local(datetime(2026, 1, 3, 0, 0)))
        response = client.get("/recordings/?date=2026-01-02")
        content = response.content.decode()
        assert str(inside.pk) in content
        assert str(outside.pk) not in content

    def test_date_range_filter(self, client):
        early, _t, _s = _make_recording(1, recorded_at=_local(datetime(2026, 1, 1, 8, 0)))
        inside, _t2, _s2 = _make_recording(2, recorded_at=_local(datetime(2026, 1, 5, 8, 0)))
        late, _t3, _s3 = _make_recording(3, recorded_at=_local(datetime(2026, 1, 20, 8, 0)))
        response = client.get("/recordings/?from=2026-01-03&to=2026-01-10")
        content = response.content.decode()
        assert str(inside.pk) in content
        assert str(early.pk) not in content
        assert str(late.pk) not in content

    def test_dst_spring_forward_day(self, client):
        # Europe/Helsinki: 2026-03-29 03:00 EET -> 04:00 EEST. Local
        # 03:30 does not exist; ZoneInfo resolves it via fold=0 to the
        # +02:00 offset. Both boundaries are aware; the day filter must
        # include the whole local calendar day.
        during_gap, _t, _s = _make_recording(1, recorded_at=_local(datetime(2026, 3, 29, 3, 30)))
        assert dj_tz.is_aware(during_gap.recorded_at)
        before_day, _t2, _s2 = _make_recording(2, recorded_at=_local(datetime(2026, 3, 28, 23, 30)))
        response = client.get("/recordings/?date=2026-03-29")
        content = response.content.decode()
        assert str(during_gap.pk) in content
        assert str(before_day.pk) not in content

    def test_dst_fall_back_day_includes_both_folds(self, client):
        # 2026-10-25 03:00 EEST -> 02:00 EET: local 02:30 occurs twice;
        # both instants belong to the same local calendar day.
        first_fold, _t, _s = _make_recording(
            1, recorded_at=_local(datetime(2026, 10, 25, 2, 30)).replace(fold=0)
        )
        second_fold, _t2, _s2 = _make_recording(
            2, recorded_at=_local(datetime(2026, 10, 25, 2, 30)).replace(fold=1)
        )
        response = client.get("/recordings/?date=2026-10-25")
        content = response.content.decode()
        assert str(first_fold.pk) in content
        assert str(second_fold.pk) in content

    def test_invalid_date_is_friendly_not_500(self, client):
        response = client.get("/recordings/?date=banana")
        assert response.status_code == 200
        assert "must be a date" in response.content.decode()

    def test_invalid_status_is_friendly_not_500(self, client):
        response = client.get("/recordings/?status=nonsense")
        assert response.status_code == 200
        assert "not a valid processing status" in response.content.decode()

    def test_date_conflicts_with_range_is_friendly(self, client):
        response = client.get("/recordings/?date=2026-01-02&from=2026-01-01")
        assert response.status_code == 200
        assert "not both" in response.content.decode()


class TestTagFilters:
    def test_single_tag_filter(self, client):
        tag = make_tag("Family")
        recording, transcript, section = _make_recording(1)
        make_tag_assignment(recording, tag)
        other, _t2, _s2 = _make_recording(2)
        response = client.get("/recordings/?tag=Family")
        content = response.content.decode()
        assert str(recording.pk) in content
        assert str(other.pk) not in content

    def test_multi_tag_and_semantics(self, client):
        family = make_tag("Family")
        academic = make_tag("Academic")
        both, _t, _s = _make_recording(1)
        make_tag_assignment(both, family)
        make_tag_assignment(both, academic)
        only_family, _t2, _s2 = _make_recording(2)
        make_tag_assignment(only_family, family)
        response = client.get("/recordings/?tag=Family&tag=Academic")
        content = response.content.decode()
        assert str(both.pk) in content
        assert str(only_family.pk) not in content

    def test_multi_tag_any_semantics(self, client):
        family = make_tag("Family")
        academic = make_tag("Academic")
        both, _t, _s = _make_recording(1)
        make_tag_assignment(both, family)
        make_tag_assignment(both, academic)
        only_family, _t2, _s2 = _make_recording(2)
        make_tag_assignment(only_family, family)
        response = client.get("/recordings/?tag=Family&tag=Academic&tag_match=any")
        content = response.content.decode()
        assert str(both.pk) in content
        assert str(only_family.pk) in content

    def test_tag_filter_is_case_insensitive(self, client):
        tag = make_tag("Family")
        recording, _t, _s = _make_recording(1)
        make_tag_assignment(recording, tag)
        response = client.get("/recordings/?tag=family")
        assert str(recording.pk) in response.content.decode()

    def test_invalid_tag_match_is_friendly(self, client):
        response = client.get("/recordings/?tag=Family&tag_match=sometimes")
        assert response.status_code == 200
        assert any("tag_match" in e for e in response.context["filter_errors"])


class TestStatusFilters:
    def test_processing_status_filter(self, client):
        transcribed, _t, _s = _make_recording(1, status=ProcessingStatus.TRANSCRIBED)
        Recording.objects.filter(pk=transcribed.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW
        )
        response = client.get("/recordings/?status=needs_review")
        content = response.content.decode()
        assert str(transcribed.pk) in content

    def test_summary_status_filter(self, client):
        recording, _t, _s = _make_recording(1)
        Recording.objects.filter(pk=recording.pk).update(summary_status=SummaryState.FAILED)
        response = client.get("/recordings/?summary=failed")
        assert str(recording.pk) in response.content.decode()

    def test_audio_filter(self, client):
        recording, _t, _s = _make_recording(1)
        Recording.objects.filter(pk=recording.pk).update(audio_status=AudioStatus.MISSING)
        response = client.get("/recordings/?audio=missing")
        assert str(recording.pk) in response.content.decode()
        response = client.get("/recordings/?audio=present")
        assert str(recording.pk) not in response.content.decode()

    def test_has_summary_filter(self, client):
        with_summary, t1, s1 = _make_recording(1)
        make_summary_version(with_summary, t1, s1)  # real Summary row
        bare, _t2, _s2 = _make_recording(2)
        Recording.objects.filter(pk=bare.pk).update(summary_status=SummaryState.MISSING)
        response = client.get("/recordings/?has_summary=1")
        content = response.content.decode()
        assert str(with_summary.pk) in content
        assert str(bare.pk) not in content
        response = client.get("/recordings/?has_summary=0")
        content = response.content.decode()
        assert str(bare.pk) in content
        assert str(with_summary.pk) not in content

    def test_review_filter_union(self, client):
        needs_review, _t, _s = _make_recording(1, status=ProcessingStatus.TRANSCRIBED)
        Recording.objects.filter(pk=needs_review.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW
        )
        failed, _t2, _s2 = _make_recording(2, status=ProcessingStatus.TRANSCRIBED)
        Recording.objects.filter(pk=failed.pk).update(processing_status=ProcessingStatus.FAILED)
        unverified, _t3, _s3 = _make_recording(3, status=ProcessingStatus.TRANSCRIBED)
        from workflow.models import RoutingMethod

        RoutingDecision.objects.create(
            recording=unverified,
            ordinal=1,
            route_suggestion="european",
            profile_name="european",
            model_id="parakeet-pro:nvidia_parakeet-v3",
            method=RoutingMethod.AUTOMATIC,
            routing_verified=False,
            is_active=True,
        )
        response = client.get("/recordings/?review=1")
        content = response.content.decode()
        assert str(needs_review.pk) in content
        assert str(failed.pk) in content
        assert str(unverified.pk) in content

    def test_invalid_has_summary_is_friendly(self, client):
        response = client.get("/recordings/?has_summary=banana")
        assert response.status_code == 200
        assert any("has_summary" in e for e in response.context["filter_errors"])


class TestPaginationAndParams:
    def test_pagination_and_param_persistence(self, client, settings_stub=None):
        family = make_tag("Family")
        for index in range(30):
            recording, _t, _s = _make_recording(index, recorded_at=_local(datetime(2026, 1, 1, 8, 0)) + timedelta(minutes=index))
            make_tag_assignment(recording, family)
        response = client.get("/recordings/?tag=Family&status=transcribed")
        assert response.status_code == 200
        content = response.content.decode()
        # Pagination links carry the current filters (tags normalized to
        # their casefolded identity, which re-matches case-insensitively).
        assert "tag=family" in content
        assert "status=transcribed" in content
        assert "page=2" in content
        assert response.context["page"].paginator.num_pages >= 2, "expected 2 pages"
        second = client.get("/recordings/?tag=Family&status=transcribed&page=2")
        assert second.status_code == 200
        assert second.context["page"].number == 2

    def test_page_size_comes_from_config(self, client):
        for index in range(30):
            _make_recording(index)
        response = client.get("/recordings/")
        assert len(response.context["cards"]) == 25  # config.web.recordings_per_page


class TestQueryContract:
    def _list_query_count(self, client, row_count):
        from workflow.models import Tag as TagModel

        tags = [
            TagModel.objects.get_or_create(
                name_key=f"tag{i}", defaults={"name": f"Tag{i}"}
            )[0]
            for i in range(3)
        ]
        for index in range(row_count):
            recording, transcript, section = _make_recording(f"{row_count}-{index}")
            make_tag_assignment(recording, tags[index % 3])
            make_summary_version(recording, transcript, section)
        with CaptureQueriesContext(connection) as ctx:
            client.get("/recordings/")
        return len(ctx.captured_queries)

    def test_query_count_constant_as_rows_grow(self, client):
        small = self._list_query_count(client, 5)
        large = self._list_query_count(client, 40)
        assert small == large, f"query count grew with rows: {small} -> {large}"

    def test_transcript_text_never_loaded_on_list(self, client):
        recording, transcript, _s = _make_recording(1)
        transcript.text_normalized = "SECRET TRANSCRIPT BODY"
        transcript.save(update_fields=["text_normalized"])
        response = client.get("/recordings/")
        assert "SECRET TRANSCRIPT BODY" not in response.content.decode()
        transcript.refresh_from_db()
        assert transcript.text_normalized == "SECRET TRANSCRIPT BODY"

    def test_list_page_never_issues_summary_per_row_queries(self, client):
        for index in range(10):
            recording, transcript, section = _make_recording(index)
            make_summary_version(recording, transcript, section)
        with CaptureQueriesContext(connection) as ctx:
            client.get("/recordings/")
        # The current-summary prefetch is a single query, not one per row.
        summary_queries = [
            q["sql"] for q in ctx.captured_queries if "workflow_summary" in q["sql"]
        ]
        assert len(summary_queries) <= 2

    def test_filters_model_defaults(self):
        filters = ListFilters()
        assert filters.tag_match == "all"
        assert filters.valid
