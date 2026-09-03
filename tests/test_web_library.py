"""Step 5A.1 Library page: routing, sort/title contract, view/cookie
precedence, mobile single-DOM table, review badge, query bounds.

Extends the Step 4 list tests in ``test_web_list.py``. No migrations,
no FTS/search, no network/subprocess/audio.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.test import Client, RequestFactory

from workflow.models import (
    AudioStatus,
    ProcessingStatus,
    Recording,
    RoutingDecision,
    RoutingMethod,
    Summary,
    SummaryState,
    Tag,
)

from factories import (
    make_summary_version,
    make_tag,
    make_tag_assignment,
    make_transcribed_recording,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


def _local(naive, tz_name="Europe/Helsinki"):
    return naive.replace(tzinfo=ZoneInfo(tz_name))


@pytest.fixture
def client():
    return Client()


def _make_recording(index, *, recorded_at=None, status=ProcessingStatus.TRANSCRIBED, **kwargs):
    recording, transcript, section = make_transcribed_recording(
        [f"segment {index}"], sha=f"lib-{index}", summary_status=SummaryState.CURRENT, **kwargs
    )
    if recorded_at is not None:
        recording.recorded_at = recorded_at
        recording.save(update_fields=["recorded_at"])
    return recording, transcript, section


def _summarize(recording, transcript, section, title, output_language, *, deactivate_same_lang=False, **kwargs):
    if deactivate_same_lang:
        old = Summary.objects.filter(
            recording=recording, output_language=output_language, is_active=True
        ).first()
        if old is not None:
            old.is_active = False
            old.save(update_fields=["is_active"])
    return make_summary_version(
        recording, transcript, section, title=title, output_language=output_language, **kwargs
    )


# ---------------------------------------------------------------------------
# Routing: / redirects, /status/ keeps the status page
# ---------------------------------------------------------------------------


class TestRouting:
    def test_root_redirects_to_recordings(self, client):
        response = client.get("/")
        assert response.status_code == 302
        assert response["Location"] == "/recordings/"

    def test_status_page_moved_to_status_url(self, client):
        response = client.get("/status/")
        assert response.status_code == 200
        content = response.content.decode()
        from brainlib import __version__

        assert __version__ in content
        assert "Inbox" in content

    def test_recordings_is_the_library(self, client):
        response = client.get("/recordings/")
        assert response.status_code == 200
        assert "Library" in response.content.decode()

    def test_nav_links(self, client):
        content = client.get("/recordings/").content.decode()
        assert 'href="/recordings/"' in content  # brand
        assert 'href="/review/"' in content
        assert 'href="/status/"' in content

    def test_review_and_health_unchanged(self, client):
        assert client.get("/review/").status_code == 200
        payload = client.get("/health/").json()
        assert payload["status"] in {"ok", "degraded"}


# ---------------------------------------------------------------------------
# Sort modes + pagination
# ---------------------------------------------------------------------------


class TestSort:
    def test_default_sort_newest(self, client):
        older, _t, _s = _make_recording(1, recorded_at=_local(datetime(2026, 1, 2, 10, 0)))
        newer, _t2, _s2 = _make_recording(2, recorded_at=_local(datetime(2026, 1, 3, 10, 0)))
        content = client.get("/recordings/").content.decode()
        assert content.index(str(newer.pk)) < content.index(str(older.pk))

    def test_sort_oldest(self, client):
        older, _t, _s = _make_recording(1, recorded_at=_local(datetime(2026, 1, 2, 10, 0)))
        newer, _t2, _s2 = _make_recording(2, recorded_at=_local(datetime(2026, 1, 3, 10, 0)))
        content = client.get("/recordings/?sort=oldest").content.decode()
        assert content.index(str(older.pk)) < content.index(str(newer.pk))

    def test_sort_title_az_case_insensitive(self, client):
        for idx, title in (("b", "beta"), ("a", "Alpha"), ("c", "gamma")):
            rec, t, s = _make_recording(idx)
            _summarize(rec, t, s, title=title, output_language="en")
        content = client.get("/recordings/?sort=title_az").content.decode()
        order = [content.index(loc) for loc in ("Alpha", "beta", "gamma")]
        assert order == sorted(order)

    def test_sort_title_za(self, client):
        for idx, title in (("b", "beta"), ("a", "Alpha"), ("c", "gamma")):
            rec, t, s = _make_recording(idx)
            _summarize(rec, t, s, title=title, output_language="en")
        content = client.get("/recordings/?sort=title_za").content.decode()
        order = [content.index(loc) for loc in ("gamma", "beta", "Alpha")]
        assert order == sorted(order)

    def test_title_sort_pk_tiebreak_deterministic(self, client):
        for idx in range(3):
            rec, t, s = _make_recording(idx)
            _summarize(rec, t, s, title="Same title", output_language="en")
        first = client.get("/recordings/?sort=title_az").content
        second = client.get("/recordings/?sort=title_az").content
        assert first == second

    def test_invalid_sort_falls_back_to_newest_with_error(self, client):
        response = client.get("/recordings/?sort=banana")
        assert response.status_code == 200
        assert any("sort" in e for e in response.context["filter_errors"])

    def test_title_sort_pagination_correct(self, client):
        for idx in range(30):
            rec, t, s = _make_recording(idx)
            _summarize(rec, t, s, title=f"Title {idx:02d}", output_language="en")
        response = client.get("/recordings/?sort=title_az")
        assert response.status_code == 200
        assert response.context["page"].paginator.num_pages >= 2
        # Page 1 carries the first 25 titles in order, no post-sort.
        titles = [card.title for card in response.context["cards"]]
        assert titles == sorted(titles, key=str.lower)
        second = client.get("/recordings/?sort=title_az&page=2")
        assert second.context["page"].number == 2


# ---------------------------------------------------------------------------
# Title contract
# ---------------------------------------------------------------------------


class TestTitleContract:
    def test_default_language_title_after_regeneration(self, client):
        """A regenerated default variant must win even when an optional
        variant has a LOWER ordinal — in display AND in Title sort order."""
        rec_a, t, s = _make_recording(1)
        t.language_observed = "zh-HK"
        t.save()
        _summarize(rec_a, t, s, "AAAA EN", "en")  # ordinal 1, lower
        _summarize(rec_a, t, s, "OLD ZH", "zh-Hant")  # ordinal 2
        _summarize(rec_a, t, s, "ZZZZ NEW", "zh-Hant", deactivate_same_lang=True)  # ordinal 3

        rec_b, t2, s2 = _make_recording(2)
        t2.language_observed = "zh-HK"
        t2.save()
        _summarize(rec_b, t2, s2, "MMMM M", "zh-Hant")

        cards = client.get("/recordings/?sort=title_az").context["cards"]
        # "MMMM M" < "ZZZZ NEW": rec_b first when sorting uses the
        # default-language title. Sorting by the lower-ordinal EN title
        # ("AAAA EN") would have put rec_a first.
        assert [c.recording.pk for c in cards] == [rec_b.pk, rec_a.pk]
        assert cards[1].title == "ZZZZ NEW"  # default-language title displayed

    def test_title_falls_back_to_filename(self, client):
        from workflow.models import AudioSource, DiscoveryState

        rec, _t, _s = _make_recording(1)
        AudioSource.objects.create(
            recording=rec,
            path_identity="audio/note.m4a",
            original_filename="note.m4a",
            discovery_state=DiscoveryState.HASHED,
            is_canonical=True,
        )
        row = client.get("/recordings/").context["cards"][0]
        assert row.title == "note.m4a"

    def test_title_falls_back_to_placeholder(self, client):
        rec, _t, _s = _make_recording(1)
        content = client.get("/recordings/").content.decode()
        assert "Untitled recording" in content

    def test_filename_fallback_prefers_canonical_then_earliest(self, client):
        from workflow.models import AudioSource, DiscoveryState

        rec, _t, _s = _make_recording(1)
        AudioSource.objects.create(
            recording=rec, path_identity="b/first.m4a", original_filename="first.m4a",
            discovery_state=DiscoveryState.HASHED, is_canonical=False,
            first_seen_at=_local(datetime(2026, 1, 1, 9, 0)),
        )
        AudioSource.objects.create(
            recording=rec, path_identity="a/second.m4a", original_filename="second.m4a",
            discovery_state=DiscoveryState.HASHED, is_canonical=False,
            first_seen_at=_local(datetime(2026, 1, 2, 9, 0)),
        )
        row = client.get("/recordings/").context["cards"][0]
        assert row.title == "first.m4a"


# ---------------------------------------------------------------------------
# Card/Table preference + cookie precedence
# ---------------------------------------------------------------------------


class TestViewPreference:
    def test_default_view_is_cards(self, client):
        _make_recording(1)
        response = client.get("/recordings/")
        content = response.content.decode()
        assert 'class="recording-list"' in content
        assert "recording-table" not in content

    def test_explicit_table_view_renders_one_table(self, client):
        _make_recording(1)
        response = client.get("/recordings/?view=table")
        content = response.content.decode()
        assert '<table class="recording-table">' in content
        assert 'class="recording-list"' not in content  # no duplicate card list

    def test_cookie_preference_when_no_param(self, client):
        _make_recording(1)
        client.cookies["brain_view_pref"] = "table"
        content = client.get("/recordings/").content.decode()
        assert '<table class="recording-table">' in content

    def test_explicit_param_overrides_cookie(self, client):
        _make_recording(1)
        client.cookies["brain_view_pref"] = "table"
        content = client.get("/recordings/?view=cards").content.decode()
        assert 'class="recording-list"' in content
        assert "recording-table" not in content

    def test_invalid_param_falls_back_to_cookie(self, client):
        _make_recording(1)
        client.cookies["brain_view_pref"] = "table"
        content = client.get("/recordings/?view=bogus").content.decode()
        assert '<table class="recording-table">' in content

    def test_invalid_cookie_falls_back_to_cards(self, client):
        _make_recording(1)
        client.cookies["brain_view_pref"] = "grid"
        content = client.get("/recordings/").content.decode()
        assert 'class="recording-list"' in content

    def test_valid_view_param_sets_http_only_cookie(self, client):
        response = client.get("/recordings/?view=table")
        cookie = response.cookies["brain_view_pref"]
        assert cookie.value == "table"
        assert cookie["httponly"]
        assert cookie["samesite"] == "Lax"
        assert cookie["path"] == "/"
        assert cookie["max-age"] == 31536000

    def test_secure_cookie_on_https_request(self, client):
        response = client.get("/recordings/?view=table", secure=True)
        assert response.cookies["brain_view_pref"]["secure"]

    def test_view_survives_filter_submission_without_cookies(self, client):
        """The GET form carries a hidden effective-view field, so
        Card/Table mode survives filtering even with cookies disabled."""
        _make_recording(1)
        content = client.get("/recordings/?view=table").content.decode()
        assert '<input type="hidden" name="view" value="table">' in content
        # A form submission re-sends the hidden view field explicitly.
        response = client.get("/recordings/?from=2026-01-01&to=2026-12-31&sort=oldest&view=table")
        assert '<table class="recording-table">' in response.content.decode()

    def test_view_toggle_links_preserve_filters_without_duplicate_view(self, client):
        make_tag("Family")
        _make_recording(1)
        content = client.get("/recordings/?view=table&sort=oldest&tag=Family").content.decode()
        # The link to Cards keeps sort + tag (tag normalized to its
        # casefolded identity), with a single view=cards.
        m = re.search(r'href="/recordings/\?view=cards([^"]*)"', content)
        assert m is not None, "cards toggle link not found"
        assert "sort=oldest" in m.group(1)
        assert "tag=family" in m.group(1)
        assert m.group(1).count("view=") == 0  # no conflicting duplicate view


# ---------------------------------------------------------------------------
# Mobile: single DOM, data-label cells
# ---------------------------------------------------------------------------


class TestMobileTable:
    def test_table_cells_carry_data_labels(self, client):
        rec, _t, _s = _make_recording(1)
        content = client.get("/recordings/?view=table").content.decode()
        for label in ("Date &amp; time", "Title", "Duration", "Tags", "Languages", "Status"):
            assert f'data-label="{label}"' in content
        assert f'<a class="col-title" href="/recordings/{rec.pk}/">' in content


# ---------------------------------------------------------------------------
# Month headings are server-rendered and sort-aware
# ---------------------------------------------------------------------------


class TestMonthHeadings:
    def test_month_headings_visible_for_newest(self, client):
        _make_recording(1, recorded_at=_local(datetime(2026, 9, 1, 10, 0)))
        _make_recording(2, recorded_at=_local(datetime(2026, 8, 1, 10, 0)))
        content = client.get("/recordings/?sort=newest").content.decode()
        assert "September 2026" in content
        assert "August 2026" in content

    def test_month_headings_hidden_for_title_sort(self, client):
        _make_recording(1, recorded_at=_local(datetime(2026, 9, 1, 10, 0)))
        _make_recording(2, recorded_at=_local(datetime(2026, 8, 1, 10, 0)))
        content = client.get("/recordings/?sort=title_az").content.decode()
        assert "September 2026" not in content
        assert "August 2026" not in content


# ---------------------------------------------------------------------------
# Locked controls + legacy params + search placeholder
# ---------------------------------------------------------------------------


class TestLockedControls:
    def test_only_approved_controls_rendered(self, client):
        make_tag("Family")
        content = client.get("/recordings/").content.decode()
        # Approved controls present.
        for name in ("name=\"from\"", "name=\"to\"", "name=\"sort\"", "name=\"tag\""):
            assert name in content
        # Old/extra controls absent.
        for forbidden in (
            "name=\"date\"", "name=\"tag_match\"", "name=\"status\"",
            "name=\"summary\"", "name=\"audio\"", "name=\"has_summary\"",
            "name=\"review\"", "<legend>", "FILTER",
        ):
            assert forbidden not in content, forbidden

    def test_legacy_status_param_still_filters(self, client):
        needs_review, _t, _s = _make_recording(1)
        Recording.objects.filter(pk=needs_review.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW
        )
        ok, _t2, _s2 = _make_recording(2)
        content = client.get("/recordings/?status=needs_review").content.decode()
        assert str(needs_review.pk) in content
        assert str(ok.pk) not in content

    def test_search_is_disabled_placeholder_only(self, client):
        content = client.get("/recordings/").content.decode()
        m = re.search(r'<input[^>]*type="search"[^>]*>', content)
        assert m is not None
        assert "disabled" in m.group(0)
        assert "Search coming soon" in content
        assert "name=" not in m.group(0)  # non-submitting


# ---------------------------------------------------------------------------
# Review badge
# ---------------------------------------------------------------------------


class TestReviewBadge:
    def test_overlapping_categories_counted_once(self, client):
        rec, _t, _s = _make_recording(1)
        Recording.objects.filter(pk=rec.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW,
            audio_status=AudioStatus.MISSING,
        )
        content = client.get("/recordings/").content.decode()
        badge = re.search(r'<span class="badge"[^>]*>(\d+)</span>', content)
        assert badge is not None
        assert badge.group(1) == "1"

    def test_badge_hidden_when_nothing_to_review(self, client):
        _make_recording(1)
        content = client.get("/recordings/").content.decode()
        assert '<span class="badge"' not in content

    def test_context_processor_issues_exactly_one_query(self, client):
        from workflow import context_processors

        factory = RequestFactory()
        request = factory.get("/recordings/")
        with CaptureQueriesContext(connection) as ctx:
            context_processors.nav_context(request)
        assert len(ctx.captured_queries) == 1

    def test_health_never_runs_badge_query(self, client):
        from workflow import context_processors

        with CaptureQueriesContext(connection) as ctx:
            client.get("/health/")
        assert not any("workflow_recording" in q["sql"] for q in ctx.captured_queries)


# ---------------------------------------------------------------------------
# Multilingual card consistency: title AND overview from the same
# default-language Summary (corrective finding 1)
# ---------------------------------------------------------------------------


class TestDisplaySummaryConsistency:
    def test_title_and_overview_come_from_default_variant(self, client):
        rec, t, s = _make_recording(1)
        t.language_observed = "zh-HK"
        t.save()
        # Optional variant with a LOWER ordinal carries a distinctive
        # title + overview; the regenerated default variant has a higher
        # ordinal. Both card fields must come from the default variant.
        _summarize(rec, t, s, "EN OPTIONAL", "en", deactivate_same_lang=False)
        _summarize(rec, t, s, "ZH OVERVIEW OLD", "zh-Hant", deactivate_same_lang=False)
        _summarize(
            rec, t, s, "ZH REGEN", "zh-Hant",
            overview="ZH REGEN OVERVIEW TEXT.", deactivate_same_lang=True,
        )

        cards = client.get("/recordings/").context["cards"]
        assert len(cards) == 1
        card = cards[0]
        assert card.title == "ZH REGEN"
        assert card.overview_excerpt == "ZH REGEN OVERVIEW TEXT."
        assert card.display_summary.title == "ZH REGEN"
        # Rendered card shows the same single title and the default overview.
        content = client.get("/recordings/").content.decode()
        assert "ZH REGEN" in content
        assert "ZH REGEN OVERVIEW TEXT." in content
        assert "ZH OVERVIEW OLD" not in content

    def test_display_summary_falls_back_like_display_title(self, client):
        # Non-Chinese default (en) with only an optional Finnish variant:
        # the fallback is the deterministic lowest-ordinal active summary.
        rec, t, s = _make_recording(1)
        t.language_observed = "fi"
        t.save()
        _summarize(rec, t, s, "FI ONLY", "fi", overview="FI OVERVIEW.", deactivate_same_lang=False)
        card = client.get("/recordings/").context["cards"][0]
        assert card.title == "FI ONLY"
        assert card.overview_excerpt == "FI OVERVIEW."

    def test_query_count_constant_with_multilingual_variants(self, client):
        def run(row_count):
            for index in range(row_count):
                rec, t, s = _make_recording(f"mc-{row_count}-{index}")
                t.language_observed = "zh-HK"
                t.save()
                _summarize(rec, t, s, f"EN {index}", "en")
                _summarize(rec, t, s, f"ZH {index}", "zh-Hant")
            with CaptureQueriesContext(connection) as ctx:
                client.get("/recordings/")
            return len(ctx.captured_queries)

        small = run(5)
        large = run(40)
        assert small == large, f"query count grew: {small} -> {large}"


# ---------------------------------------------------------------------------
# Unicode-aware Title sorting (corrective finding 3)
# ---------------------------------------------------------------------------


class TestUnicodeSort:
    @pytest.mark.parametrize(
        "pair",
        [
            ("äiti", "Äiti"),
            ("åland", "Åland"),
            ("örebro", "Örebro"),
            ("örebro", "o\u0308rebro"),  # NFC vs NFD equivalent
        ],
    )
    def test_casefold_equality_is_a_tie_pk_decides(self, client, pair):
        """Swapping equal-under-casefold titles must NOT change the DB
        order (a real casefold tie). ASCII LOWER() would flip it."""
        a, b = pair
        rec_a, t_a, s_a = _make_recording("pair-a")
        rec_b, t_b, s_b = _make_recording("pair-b")
        _summarize(rec_a, t_a, s_a, a, "en")
        _summarize(rec_b, t_b, s_b, b, "en")

        def order():
            return [c.recording.pk for c in client.get("/recordings/?sort=title_az").context["cards"]]

        first = order()
        assert set(first) == {rec_a.pk, rec_b.pk}
        _summarize(rec_a, t_a, s_a, b, "en", deactivate_same_lang=True)
        _summarize(rec_b, t_b, s_b, a, "en", deactivate_same_lang=True)
        assert order() == first, "titles are not a casefold tie under DB ordering"

    def test_title_az_matches_casefold_order(self, client):
        titles = ["Åland", "äiti", "Örebro"]
        recs = []
        pk_by_title = {}
        for index, title in enumerate(titles):
            rec, t, s = _make_recording(f"u-{index}")
            _summarize(rec, t, s, title, "en")
            recs.append(rec)
            pk_by_title[title] = rec.pk

        def fold(value):
            return unicodedata.normalize("NFC", value).casefold()

        expected = [
            pk_by_title[title]
            for title in sorted(titles, key=lambda title: (fold(title), pk_by_title[title]))
        ]
        got = [c.recording.pk for c in client.get("/recordings/?sort=title_az").context["cards"]]
        assert got == expected
        # Z–A is the reverse (distinct folded values => clean reversal).
        za = [c.recording.pk for c in client.get("/recordings/?sort=title_za").context["cards"]]
        assert za == list(reversed(expected))

    def test_title_za_reverses_casefold_order(self, client):
        for index, title in enumerate(("Örebro", "äiti", "Åland")):
            rec, t, s = _make_recording(f"z-{index}")
            _summarize(rec, t, s, title, "en")
        az = [c.title for c in client.get("/recordings/?sort=title_az").context["cards"]]
        za = [c.title for c in client.get("/recordings/?sort=title_za").context["cards"]]
        assert za == list(reversed(az))

    def test_title_pk_tiebreak_deterministic(self, client):
        for index in range(3):
            rec, t, s = _make_recording(f"tie-{index}")
            _summarize(rec, t, s, "Same title", "en")
        first = client.get("/recordings/?sort=title_az").content
        second = client.get("/recordings/?sort=title_az").content
        assert first == second

    def test_unicode_title_pagination_is_database_sorted(self, client):
        for index in range(30):
            rec, t, s = _make_recording(f"pg-{index}")
            _summarize(rec, t, s, f"{index:02d} Äiti", "en")
        page1 = client.get("/recordings/?sort=title_az").context["cards"]
        page2 = client.get("/recordings/?sort=title_az&page=2").context["cards"]
        assert len(page1) == 25 and len(page2) == 5
        titles = [c.title for c in page1] + [c.title for c in page2]
        assert titles == [f"{i:02d} Äiti" for i in range(30)]


# ---------------------------------------------------------------------------
# Clear-all preserves view mode without cookies (corrective finding 6)
# ---------------------------------------------------------------------------


class TestClearAllPreservesView:
    def test_clear_all_keeps_table_view(self, client):
        _make_recording(1)
        content = client.get("/recordings/?view=table&sort=oldest&tag=Family").content.decode()
        m = re.search(r'class="clear-filters" href="([^"]+)"', content)
        assert m is not None, "clear-all link not found"
        assert m.group(1) == "/recordings/?view=table"
        assert m.group(1).count("view=") == 1

    def test_clear_all_keeps_cards_view(self, client):
        _make_recording(1)
        content = client.get("/recordings/?view=cards&sort=oldest").content.decode()
        m = re.search(r'class="clear-filters" href="([^"]+)"', content)
        assert m is not None
        assert m.group(1) == "/recordings/?view=cards"


# ---------------------------------------------------------------------------
# View-toggle is plain navigation (corrective finding 7)
# ---------------------------------------------------------------------------


class TestToggleMarkup:
    def test_toggle_uses_links_and_aria_current(self, client):
        _make_recording(1)
        content = client.get("/recordings/?view=table").content.decode()
        assert 'class="view-toggle"' in content
        assert 'role="radiogroup"' not in content
        assert 'role="radio"' not in content
        assert "aria-checked" not in content
        # Active link carries navigation state; the other is a plain link.
        assert 'class="view-toggle-btn active" href="/recordings/?view=table' in content
        assert 'aria-current="true"' in content
        assert content.count('aria-current="true"') == 1

    def test_toggle_cards_active_marks_cards(self, client):
        _make_recording(1)
        content = client.get("/recordings/").content.decode()
        assert 'class="view-toggle-btn active" href="/recordings/?view=cards' in content
        assert content.count('aria-current="true"') == 1


# ---------------------------------------------------------------------------
# Mobile Status link stays visible (corrective finding 2)
# ---------------------------------------------------------------------------


class TestMobileStatusLink:
    def test_status_label_visible_in_markup(self, client):
        content = client.get("/recordings/").content.decode()
        assert '<span class="btn-label">Status</span>' in content

    def test_mobile_css_does_not_hide_nav_button_labels(self):
        css = (Path(__file__).resolve().parent.parent / "src" / "static" / "workflow" / "base.css").read_text()
        mobile = re.search(r"@media \(max-width: 40rem\)\s*\{(.*?)\}", css, re.S)
        assert mobile is not None, "mobile media block not found"
        assert ".topbar-btn .btn-label" not in mobile.group(1), (
            "mobile rule must not hide the only content of the Status link"
        )


# ---------------------------------------------------------------------------
# nav_context logs a stable category, never exception details (finding 5)
# ---------------------------------------------------------------------------


class TestNavContextLogging:
    def test_badge_failure_logs_stable_message_and_returns_zero(self, client, caplog, monkeypatch):
        from django.test import RequestFactory

        from workflow import context_processors

        sentinel = "BRAIN-SENTINEL-/Users/secret/private/db.sqlite3-secret-42"
        factory = RequestFactory()

        def boom():
            raise RuntimeError(f"db gone at {sentinel}")

        monkeypatch.setattr(context_processors, "review_badge_count", boom)
        with caplog.at_level("WARNING", logger="workflow.context_processors"):
            result = context_processors.nav_context(factory.get("/recordings/"))
        assert result == {"review_count": 0}
        assert sentinel not in caplog.text
        assert "review badge count unavailable" in caplog.text
        # Rendered output never contains the exception details either.
        content = client.get("/recordings/").content.decode()
        assert sentinel not in content


# ---------------------------------------------------------------------------
# Query contract
# ---------------------------------------------------------------------------


class TestQueryContract:
    def _list_query_count(self, client, row_count):
        tags = [
            Tag.objects.get_or_create(name_key=f"tag{i}", defaults={"name": f"Tag{i}"})[0]
            for i in range(3)
        ]
        for index in range(row_count):
            rec, t, s = _make_recording(f"{row_count}-{index}")
            make_tag_assignment(rec, tags[index % 3])
            _summarize(rec, t, s, title=f"Summary {index}", output_language="en")
        with CaptureQueriesContext(connection) as ctx:
            client.get("/recordings/")
        return len(ctx.captured_queries)

    def test_query_count_constant_as_rows_grow(self, client):
        small = self._list_query_count(client, 5)
        large = self._list_query_count(client, 40)
        assert small == large, f"query count grew: {small} -> {large}"

    def test_languages_come_from_prefetch_not_related_manager(self, client):
        rec, t, s = _make_recording(1)
        _summarize(rec, t, s, title="EN", output_language="en")
        _summarize(rec, t, s, title="ZH", output_language="zh-Hant")
        with CaptureQueriesContext(connection) as ctx:
            response = client.get("/recordings/?view=table")
        # Only the prefetch touches workflow_summary (plus the main SELECT
        # whose subqueries reference it) — no per-row summary queries.
        summary_queries = [
            q["sql"] for q in ctx.captured_queries if "workflow_summary" in q["sql"]
        ]
        assert len(summary_queries) <= 2
        content = response.content.decode()
        assert "en" in content and "zh-Hant" in content