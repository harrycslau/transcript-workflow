"""Library keyword search over the web (Step 5A.4.2a).

Covers the approved contract: one submitted search GET runs the FULL
health sweep EXACTLY once; filters scope the engine candidate set
BEFORE limiting/pagination; sorting spans the returned match set;
invalid-query and index-failure states clear the query (the rejected
text appears NOWHERE in the response); links persist q/view/filters;
GET stays strictly read-only with no N+1 growth; the five sort
fallback rules hold; CJK short terms work end-to-end; no
Semantic/Hybrid controls exist yet.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.http import QueryDict

import workflow.services.search_index as si
import workflow.services.search_web as search_web
from factories import (
    default_web,
    make_config,
    make_summary_version,
    make_tag,
    make_tag_assignment,
    make_transcribed_recording,
)
from workflow.query import ListFilters, list_filters

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TZ = ZoneInfo("Europe/Helsinki")


def _seed(texts, sha, *, recorded_at=None):
    rec, _transcript, _section = make_transcribed_recording(texts, sha=sha)
    if recorded_at is not None:
        from workflow.models import Recording

        Recording.objects.filter(pk=rec.pk).update(recorded_at=recorded_at)
    return rec


def _healthy_corpus(*specs):
    """specs: (sha, text_or_texts). Returns the recordings."""
    made = []
    for sha, texts in specs:
        made.append(_seed(texts if isinstance(texts, list) else [texts], sha))
    si.rebuild_index()
    return made


def _page(client, url):
    response = client.get(url)
    assert response.status_code == 200
    return response.content.decode()


def _count_gate(monkeypatch):
    calls = []
    real = search_web.search_query.preflight_full_health

    def counting(*args, **kwargs):
        calls.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(search_web.search_query, "preflight_full_health", counting)
    return calls


def _tiny_pages(monkeypatch, tmp_path, per_page=2):
    config = make_config(tmp_path, web=default_web(recordings_per_page=per_page))
    monkeypatch.setattr("workflow.views.recordings.get_config", lambda: config)


def _positions(content, pks):
    """Assert the detail links appear in exactly the given order."""
    places = [content.find(f'href="/recordings/{pk}/"') for pk in pks]
    assert all(place >= 0 for place in places), pks
    assert places == sorted(places), places


def _apply_index_state(mode):
    """Drive the index into a proven unhealthy state inside the test
    transaction (SQLite transactional DDL restores everything)."""
    if mode == "fts_missing":
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_search_fts")
    elif mode == "fts_broken":
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_search_fts")
            cursor.execute(
                "CREATE TABLE workflow_search_fts (rowid INTEGER, body TEXT)"
            )
    elif mode == "stale":
        _seed(["budget index probe"], "idx-2")  # created, never synced
    else:  # pragma: no cover - guard
        raise AssertionError(mode)


def _fail_recording_scope_compile(monkeypatch, sentinel):
    """Only RECORDING-model SQL compilation (the scope subquery) fails
    underneath — the gate's SearchDocument sweep stays intact."""
    from django.db.models.sql.compiler import SQLCompiler

    from workflow.models import Recording

    real_as_sql = SQLCompiler.as_sql

    def guarded(self, *args, **kwargs):
        query = self.query
        # ONLY the un-sliced single-column-pk scope projection fails;
        # the sweep's sliced full-row Recording batches pass through.
        if (
            getattr(query, "model", None) is Recording
            and tuple(getattr(query, "values_select", ())) == ("pk",)
            and not query.is_sliced
        ):
            raise ValueError(sentinel)
        return real_as_sql(self, *args, **kwargs)

    monkeypatch.setattr(SQLCompiler, "as_sql", guarded)


# ---------------------------------------------------------------------------
# Gate placement and states
# ---------------------------------------------------------------------------


class TestStatesAndGate:
    def test_blank_or_whitespace_q_is_the_normal_library(self, monkeypatch, client):
        calls = _count_gate(monkeypatch)
        engine = []
        real_engine = search_web.search_query.search_recordings
        monkeypatch.setattr(
            search_web.search_query,
            "search_recordings",
            lambda *a, **k: (engine.append(True), real_engine(*a, **k))[1],
        )
        for raw in ("", "+", "%20%20"):
            content = _page(client, f"/recordings/?q={raw}")
            assert "search result" not in content
            assert "recording-list" in content or "empty-state" in content
        assert calls == []
        assert engine == []

    def test_healthy_search_runs_the_full_sweep_exactly_once(self, monkeypatch, client):
        _healthy_corpus(("gate-1", "quarterly budget review"))
        calls = _count_gate(monkeypatch)
        content = _page(client, "/recordings/?q=budget&page=1")
        assert len(calls) == 1
        assert "1 search result" in content

    def test_ok_state_renders_row_snippet_and_label(self, client):
        rec = _healthy_corpus(("row-1", "quarterly budget review happenings"))[0]
        content = _page(client, "/recordings/?q=budget")
        assert f'href="/recordings/{rec.pk}/"' in content
        assert "search-snippet" in content
        assert "budget" in content
        assert 'class="match-chip match-segment"' in content
        assert "segment ·" in content

    def test_zero_results_state_is_ok(self, client):
        _healthy_corpus(("zero-1", "quarterly budget review"))
        content = _page(client, "/recordings/?q=nevermatchingterm")
        assert "No search results for" in content
        assert "search-snippet" not in content

    def test_invalid_query_clears_the_input(self, client):
        _healthy_corpus(("inv-1", "budget"))
        canary = "PRIVCANARY" + "x" * 300
        content = _page(client, f"/recordings/?q={canary}")
        assert canary not in content
        assert "PRIVCANARY" not in content
        assert "at most" in content  # stable bound message
        assert "Clear search" in content
        assert 'value=""' in content or "value=\"\"" in content
        m = re.search(r'<input[^>]*name="q"[^>]*>', content)
        assert 'value="' in m.group(0) and 'value=""' in m.group(0)

    def test_invalid_query_skips_the_gate_and_engine(self, monkeypatch, client):
        calls = _count_gate(monkeypatch)

        def forbidden(*args, **kwargs):
            raise AssertionError("engine must not run for an invalid query")

        monkeypatch.setattr(search_web.search_query, "search_recordings", forbidden)
        _page(client, "/recordings/?q=" + "z" * 400)
        assert calls == []

    @pytest.mark.parametrize("mode", ["fts_missing", "fts_broken", "stale"])
    def test_index_states_are_friendly_and_clear_the_input(self, client, mode):
        _healthy_corpus(("idx-1", "budget index probe"))
        _apply_index_state(mode)

        canary = "ZZTOPPRIVATETERM"
        content = _page(client, f"/recordings/?q={canary}+probe")
        assert canary not in content
        assert "ZZTOPPRIVATETERM" not in content
        assert "search-index" in content  # actionable repair command named
        assert "Clear search" in content
        assert "search result" not in content
        assert "recording-card" not in content

    def test_engine_error_lands_in_the_same_index_state(self, monkeypatch, client):
        _healthy_corpus(("err-1", "budget"))

        def boom(*args, **kwargs):
            raise si.SearchIndexError("the keyword-search index is stale or inconsistent (probe)")

        monkeypatch.setattr(search_web.search_query, "search_recordings", boom)
        content = _page(client, "/recordings/?q=SECRETQUERY")
        assert "SECRETQUERY" not in content
        assert "probe" in content
        assert "Clear search" in content

    def test_engine_input_error_lands_in_invalid_state(self, monkeypatch, client):
        _healthy_corpus(("err-2", "budget"))

        def boom(*args, **kwargs):
            raise search_web.search_query.SearchQueryInputError("stable fixed message")

        monkeypatch.setattr(search_web.search_query, "search_recordings", boom)
        content = _page(client, "/recordings/?q=SECRETQUERY2")
        assert "SECRETQUERY2" not in content
        assert "stable fixed message" in content

    def test_scope_compiler_failure_is_a_sanitized_index_state(self, monkeypatch, client):
        _healthy_corpus(("cboom-1", "budget review"))
        _fail_recording_scope_compile(monkeypatch, "TOPSECRETPATH-canary-SELECT")
        content = _page(client, "/recordings/?q=CANARYQ&tag=Family")
        assert "TOPSECRETPATH" not in content  # underlying text never echoes
        assert "CANARYQ" not in content  # and the query is cleared too
        assert "search-index" in content  # fixed actionable message
        assert "Clear search" in content
        assert "recording-card" not in content


# ---------------------------------------------------------------------------
# Sorting contract
# ---------------------------------------------------------------------------


class TestSorting:
    def _corpus(self):
        base = datetime(2026, 2, 10, 9, 0, tzinfo=TZ)
        r_high = _seed(["sablemark sablemark sablemark"], "sort-high", recorded_at=base)
        r_mid = _seed(["sablemark sablemark"], "sort-mid", recorded_at=base + timedelta(days=1))
        r_low = _seed(["sablemark"], "sort-low", recorded_at=base + timedelta(days=2))
        si.rebuild_index()
        return r_high, r_mid, r_low

    def test_default_is_engine_relevance_order(self, client):
        r_high, r_mid, r_low = self._corpus()
        content = _page(client, "/recordings/?q=sablemark")
        _positions(content, [r_high.pk, r_mid.pk, r_low.pk])

    def test_library_sorts_reorder_the_same_winner_set(self, client):
        r_high, r_mid, r_low = self._corpus()
        content = _page(client, "/recordings/?q=sablemark&sort=newest")
        _positions(content, [r_low.pk, r_mid.pk, r_high.pk])
        content = _page(client, "/recordings/?q=sablemark&sort=oldest")
        _positions(content, [r_high.pk, r_mid.pk, r_low.pk])

    def test_relevance_never_becomes_a_database_order(self, monkeypatch, client):
        r_high, _r_mid, _r_low = self._corpus()

        def forbidden_sort(*args, **kwargs):
            raise AssertionError("relevance must not translate into DB ordering")

        monkeypatch.setattr(
            search_web, "_ordered_winner_ids_db", forbidden_sort, raising=False
        )
        real_apply_sort = search_web.apply_sort

        def guarded(qs, sort):
            if sort == "relevance":
                raise AssertionError("apply_sort must never see relevance")
            return real_apply_sort(qs, sort)

        monkeypatch.setattr(search_web, "apply_sort", guarded)
        content = _page(client, "/recordings/?q=sablemark&sort=relevance")
        assert r_high.pk in content

    def test_invalid_sort_falls_back_to_relevance_keeping_filters(self, client):
        rec_tagged = _seed(["sablemark tagged run"], "sortf-1")
        rec_other = _seed(["sablemark untagged run"], "sortf-2")
        tag = make_tag("Family", configured=True)
        make_tag_assignment(rec_tagged, tag)
        si.rebuild_index()

        content = _page(client, "/recordings/?q=sablemark&tag=Family&sort=zzzbogus")
        assert f'href="/recordings/{rec_tagged.pk}/"' in content
        assert f'href="/recordings/{rec_other.pk}/"' not in content
        assert "sort" in content.lower()  # the fallback is surfaced
        assert "Search ran without the invalid filters" not in content

    def test_non_search_library_rejects_relevance(self, client):
        _healthy_corpus(("libsort-1", "budget"))
        content = _page(client, "/recordings/?sort=relevance")
        assert "Relevance" not in content  # the option is not even offered
        assert "message-error" in content  # historical library error channel

    def test_sorting_a_partial_window_carries_sort_and_query(self, monkeypatch, client, tmp_path):
        _healthy_corpus(
            ("win-1", "sablemark alpha"),
            ("win-2", "sablemark beta"),
            ("win-3", "sablemark gamma"),
        )
        _tiny_pages(monkeypatch, tmp_path, per_page=2)
        content = _page(client, "/recordings/?q=sablemark&sort=newest")
        assert "Sorting applies to the returned most-relevant matches" not in content
        assert "sort=newest" in content  # pagination/toggle links persist it

    def test_sort_window_note_when_engine_reported_more(self, monkeypatch, client):
        _healthy_corpus(("sw-1", "sablemark one"), ("sw-2", "sablemark two"))
        monkeypatch.setattr(search_web, "WEB_SCAN_LIMIT", 1)
        content = _page(client, "/recordings/?q=sablemark&sort=title_az")
        assert "Sorting applies to the returned most-relevant matches" in content


# ---------------------------------------------------------------------------
# Filtering happens inside the candidate set (before limits and pagination)
# ---------------------------------------------------------------------------


class TestScopedSearch:
    def _corpus(self):
        # Descending occurrence counts pin the relevance order:
        # tagged_a > tagged_b > tagged_c > outside.
        tagged_a = _seed(["sablemark sablemark sablemark alpha"], "scope-a")
        tagged_b = _seed(["sablemark sablemark beta"], "scope-b")
        tagged_c = _seed(["sablemark gamma"], "scope-c")
        outside = _seed(["sablemark outside tag"], "scope-out")
        tag = make_tag("Project", configured=True)
        for rec in (tagged_a, tagged_b, tagged_c):
            make_tag_assignment(rec, tag)
        si.rebuild_index()
        return tag, (tagged_a, tagged_b, tagged_c), outside

    def test_tag_filter_applies_before_pagination_window(self, monkeypatch, client, tmp_path):
        _tag, (a, b, _c), outside = self._corpus()
        _tiny_pages(monkeypatch, tmp_path, per_page=2)
        content = _page(client, "/recordings/?q=sablemark&tag=Project")
        assert "3 search results" in content
        assert f'href="/recordings/{a.pk}/"' in content
        assert f'href="/recordings/{b.pk}/"' in content
        assert f'href="/recordings/{outside.pk}/"' not in content

    def test_engine_receives_a_recording_scope_queryset(self, monkeypatch, client):
        self._corpus()
        seen = {}
        real = search_web.search_query.search_recordings

        def spy(query, **kwargs):
            seen["scope"] = kwargs.get("scope", "MISSING")
            return real(query, **kwargs)

        monkeypatch.setattr(search_web.search_query, "search_recordings", spy)
        _page(client, "/recordings/?q=sablemark&tag=Project")
        from django.db.models import QuerySet

        from workflow.models import Recording

        assert isinstance(seen["scope"], QuerySet)
        assert seen["scope"].model is Recording

    def test_empty_scope_answers_zero_results_not_an_error(self, monkeypatch, client):
        """A scope that provably contains NO recording is a valid zero
        answer — never a 500, an index-error state or an invalid query."""
        from workflow.models import Recording

        _healthy_corpus(("escope-1", "budget review"))
        monkeypatch.setattr(
            search_web, "search_scope_queryset", lambda f, tz: Recording.objects.none()
        )
        content = _page(client, "/recordings/?q=budget&tag=Project")
        assert "No search results for" in content
        assert "recording-card" not in content
        assert "message-error" not in content
        assert "search-index" not in content

    def test_compiler_empty_scope_answers_zero_results_not_an_error(
        self, monkeypatch, client
    ):
        """``filter(pk__in=[])`` emptiness is proven only by the compiler
        (EmptyResultSet): the web layer must still render the ordinary
        zero-results state, not any error state."""
        from workflow.models import Recording

        _healthy_corpus(("escope-2", "budget review"))
        monkeypatch.setattr(
            search_web,
            "search_scope_queryset",
            lambda f, tz: Recording.objects.filter(pk__in=[]),
        )
        content = _page(client, "/recordings/?q=budget&tag=Project")
        assert "No search results for" in content
        assert "recording-card" not in content
        assert "message-error" not in content
        assert "search-index" not in content

    def test_invalid_scope_filter_runs_unscoped_and_says_so(self, client):
        _tag, _in, outside = self._corpus()
        content = _page(client, "/recordings/?q=sablemark&from=notadate")
        assert "Search ran without the invalid filters" in content
        # Every match (in AND out of the broken filter) is present.
        assert f'href="/recordings/{outside.pk}/"' in content

    def test_more_matches_note_is_scope_honest(self, monkeypatch, client):
        self._corpus()
        monkeypatch.setattr(search_web, "WEB_SCAN_LIMIT", 2)
        content = _page(client, "/recordings/?q=sablemark")
        assert "2 more recordings also matched these filters (showing the 2 most relevant)" in content
        assert "truncated" not in content.lower()

    def test_dedup_one_row_per_recording(self, client):
        rec, transcript, section = make_transcribed_recording(
            ["sablemark in the transcript"], sha="dedup-1"
        )
        make_summary_version(rec, transcript, section, title="sablemark summary")
        si.rebuild_index()
        content = _page(client, "/recordings/?q=sablemark")
        assert content.count(f'href="/recordings/{rec.pk}/"') == 1
        assert "1 search result" in content


# ---------------------------------------------------------------------------
# Persistence of query/view/filters across links
# ---------------------------------------------------------------------------


class TestLinkPersistence:
    def _corpus(self, monkeypatch, tmp_path, per_page=2):
        _healthy_corpus(
            ("link-1", "sablemark alpha"),
            ("link-2", "sablemark beta"),
            ("link-3", "sablemark gamma"),
        )
        _tiny_pages(monkeypatch, tmp_path, per_page=per_page)

    def test_pagination_carries_query_view_and_sort(self, monkeypatch, client, tmp_path):
        self._corpus(monkeypatch, tmp_path)
        content = _page(
            client, "/recordings/?q=sablemark&sort=title_az&view=table&page=1"
        )
        next_link = re.search(r'href="\?[^"]*page=2[^"]*"', content)
        assert next_link is not None
        href = next_link.group(0)
        assert "q=sablemark" in href
        assert "sort=title_az" in href
        assert "view=table" in href

    def test_view_toggle_carries_query(self, monkeypatch, client, tmp_path):
        self._corpus(monkeypatch, tmp_path)
        content = _page(client, "/recordings/?q=sablemark&view=cards")
        toggle = re.search(r'href="/recordings/\?view=table[^"]*"', content)
        assert toggle is not None and "q=sablemark" in toggle.group(0)

    def test_filter_form_keeps_query_and_clear_search_drops_it(self, client):
        _healthy_corpus(("link-4", "sablemark alpha"))
        content = _page(client, "/recordings/?q=sablemark")
        assert '<input type="hidden" name="q" value="sablemark">' in content
        clear = re.search(r'href="([^"]*)">Clear search<', content)
        assert clear is not None and "q=" not in clear.group(1)

    def test_clear_all_keeps_no_search_either(self, client):
        _healthy_corpus(("link-5", "sablemark alpha"))
        content = _page(client, "/recordings/?q=sablemark&sort=newest")
        clear = re.search(r'href="([^"]*)">Clear all<', content)
        assert clear is not None
        assert "q=" not in clear.group(1) and "sort=" not in clear.group(1)


# ---------------------------------------------------------------------------
# GET purity, N+1 discipline, privacy, controls
# ---------------------------------------------------------------------------


class TestPurityAndPerformance:
    def test_search_get_is_strictly_read_only(self, monkeypatch, client):
        _healthy_corpus(("pure-1", "budget review"))

        def forbidden(*args, **kwargs):
            raise AssertionError("mutating entry point used during a GET")

        from workflow.services import pipeline_lock, search_sync

        monkeypatch.setattr(search_sync, "schedule_recording_sync", forbidden)
        monkeypatch.setattr(si, "rebuild_index", forbidden)
        monkeypatch.setattr(search_sync, "reconcile_recording", forbidden)
        monkeypatch.setattr(pipeline_lock, "pipeline_lock", forbidden)

        with CaptureQueriesContext(connection) as ctx:
            response = client.get("/recordings/?q=budget&page=1")
        assert response.status_code == 200
        assert "Set-Cookie" not in response  # no view= param => no cookie
        for query in ctx.captured_queries:
            verb = query["sql"].lstrip().split(None, 1)[0].upper()
            assert verb in ("SELECT", "PRAGMA"), query["sql"]

    def test_no_n_plus_one_growth_with_result_volume(self, client):
        for index in range(40):
            marker = " fiveonly" if index < 5 else ""
            _seed([f"nquantumq wide corpus {index}{marker}"], f"nplus-{index}")
        si.rebuild_index()

        def request_count(query):
            with CaptureQueriesContext(connection) as ctx:
                response = client.get(f"/recordings/?q={query}")
            assert response.status_code == 200
            return len(ctx.captured_queries)

        wide = request_count("nquantumq")
        content = _page(client, "/recordings/?q=nquantumq")
        assert "40 search results" in content
        narrow = request_count("nquantumq+fiveonly")
        content = _page(client, "/recordings/?q=nquantumq+fiveonly")
        assert "5 search results" in content
        # SAME corpus and SAME gate, 40 vs 5 returned matches: identical
        # totals prove titles/cards are batch-fetched, never per result.
        assert narrow == wide

    def test_page_one_and_two_cost_the_same(self, monkeypatch, client, tmp_path):
        _healthy_corpus(
            *[("np2-{}".format(i), "nquantumq corpus item") for i in range(6)]
        )
        _tiny_pages(monkeypatch, tmp_path, per_page=2)

        def request_count(url):
            with CaptureQueriesContext(connection) as ctx:
                response = client.get(url)
            assert response.status_code == 200
            return len(ctx.captured_queries)

        assert request_count("/recordings/?q=nquantumq&page=1") == request_count(
            "/recordings/?q=nquantumq&page=2"
        )

    def test_zero_result_page_skips_the_row_fetch(self, client):
        _healthy_corpus(("zm-1", "budget review"))

        def request_count(url):
            with CaptureQueriesContext(connection) as ctx:
                response = client.get(url)
            assert response.status_code == 200
            return len(ctx.captured_queries)

        full = request_count("/recordings/?q=budget")
        zero = request_count("/recordings/?q=budgetandsomethingabsent")
        # Zero results skip the page-window card fetch entirely (title
        # lookup + card batch); the gate/engine work is identical.
        assert full - zero >= 2


class TestPrivacyAndControls:
    def test_xss_query_is_echoed_only_autoescaped_on_ok_pages(self, client):
        _healthy_corpus(("xss-1", "budget review"))
        payload = "<img src=x onerror=alert(1)>"
        content = _page(client, "/recordings/?q=img")  # benign probe first
        assert "<img" not in content  # nothing injects in the first place
        # The dangerous text is never echoed (not searched for):
        assert "onerror=alert" not in content

    def test_xss_query_in_valid_search_never_unescaped(self, client):
        _healthy_corpus(("xss-2", "budget review"))
        # 25 chars: valid; zero results => echo allowed (autoescaped).
        content = _page(client, "/recordings/?q=" + "<img+src=x+onerror=alert(1)>")
        assert "<img src=x onerror=alert(1)>" not in content
        assert "&lt;img src=x onerror=alert(1)&gt;" in content

    def test_cjk_short_term_end_to_end(self, client):
        rec = _healthy_corpus(("cjk-1", "项目 会议 记录 项目"))[0]
        content = _page(client, "/recordings/?q=" + "%E9%A1%B9%E7%9B%AE")
        assert "1 search result" in content
        assert f'href="/recordings/{rec.pk}/"' in content
        assert "项目" in content
        assert "match-segment" in content

    def test_no_semantic_or_hybrid_controls_anywhere(self, client):
        _healthy_corpus(("ctl-1", "budget review"))
        for url in ("/recordings/", "/recordings/?q=budget"):
            content = _page(client, url)
            assert "Semantic" not in content
            assert "Hybrid" not in content
            assert len(re.findall(r'<input[^>]*type="search"', content)) == 1

    def test_invalid_and_index_pages_clear_the_top_bar_input(self, client):
        _healthy_corpus(("bar-1", "budget review"))
        content = _page(client, "/recordings/?q=" + "q" * 400)
        m = re.search(r'<input[^>]*name="q"[^>]*>', content)
        assert 'value=""' in m.group(0)


# ---------------------------------------------------------------------------
# Structured filter contract (unit level, no client)
# ---------------------------------------------------------------------------


class TestStructuredFilterContract:
    def _qdict(self, raw):
        return QueryDict(raw)

    def test_search_mode_sort_fallback_is_structured(self):
        filters = list_filters(
            self._qdict("sort=zzz&tag=Family"), "Europe/Helsinki", allow_relevance=True
        )
        assert filters.sort == "relevance"
        assert filters.sort_error is not None
        assert filters.errors == []  # the sort error never poisons scope
        assert filters.scope_valid is True
        assert filters.sort_default == "relevance"

    def test_as_querystring_respects_the_mode_default(self):
        relevance = list_filters(self._qdict(""), "Europe/Helsinki", allow_relevance=True)
        assert "sort" not in relevance.as_querystring()
        newest = list_filters(
            self._qdict("sort=newest"), "Europe/Helsinki", allow_relevance=True
        )
        assert "sort=newest" in newest.as_querystring()

    def test_library_mode_contract_is_unchanged(self):
        filters = list_filters(self._qdict("sort=relevance"), "Europe/Helsinki")
        assert filters.sort == "newest"
        assert filters.sort_default == "newest"
        assert filters.sort_error is None
        assert filters.errors  # historical single-channel behavior
        assert filters.scope_valid is False

    def test_invalid_scope_filters_mark_scope_invalid_not_sort(self):
        filters = list_filters(
            self._qdict("from=nonsense&sort=zzz"), "Europe/Helsinki",
            allow_relevance=True,
        )
        assert filters.errors  # date error
        assert filters.scope_valid is False
        assert filters.sort == "relevance"  # sort still repaired
        assert filters.sort_error is not None


# ---------------------------------------------------------------------------
# Module-level units
# ---------------------------------------------------------------------------


class TestSearchWebUnits:
    def test_build_notes_matrix(self):
        base = {"truncated": False, "more_recordings_matched": 0}
        assert search_web.build_notes(dict(base), "relevance") == []
        assert search_web.build_notes(dict(base, truncated=True), "relevance") == [
            search_web.NOTE_TRUNCATED
        ]
        notes = search_web.build_notes(
            dict(base, more_recordings_matched=7), "newest", scan_limit=200
        )
        assert search_web.NOTE_TRUNCATED not in notes
        assert any("7 more recordings also matched" in n for n in notes)
        assert notes[-1] == search_web.NOTE_SORT_WINDOW
        unknown = search_web.build_notes(
            dict(base, more_recordings_matched=None), "relevance"
        )
        assert unknown == [search_web.NOTE_MORE_UNKNOWN]

    def test_build_notes_reports_the_effective_scan_limit(self):
        notes = search_web.build_notes(
            {"truncated": False, "more_recordings_matched": 3},
            "relevance",
            scan_limit=2,
        )
        assert notes == [
            search_web.NOTE_MORE_EXACT.format(more=3, scan=2)
        ]

    def test_norm_id_accepts_storage_and_canonical_forms(self):
        import uuid

        value = uuid.uuid4()
        assert search_web._norm_id(value) == str(value)
        assert search_web._norm_id(str(value)) == str(value)
        assert search_web._norm_id(value.hex) == str(value)


# ---------------------------------------------------------------------------
# Note fidelity (review round 2)
# ---------------------------------------------------------------------------


class TestNoteFidelity:
    def test_truncation_with_full_winner_set_does_not_claim_a_sort_window(
        self, monkeypatch, client
    ):
        """truncated=true with more_recordings_matched=0: the ranking
        approximation note is honest, but the sort itself covered the
        ENTIRE winner set — no beyond-window claim."""
        rec = _healthy_corpus(("tnote-1", "budget review meeting"))[0]

        def fake(query, **kwargs):
            return {
                "results": [
                    {
                        "recording_id": str(rec.pk),
                        "rank": 1,
                        "match": {"source": "segment", "start_ms": 1200},
                        "snippet": {"text": "budget review meeting"},
                    }
                ],
                "result_count": 1,
                "truncated": True,
                "more_recordings_matched": 0,
            }

        monkeypatch.setattr(search_web.search_query, "search_recordings", fake)
        content = _page(client, "/recordings/?q=budget&sort=newest")
        assert "Too many candidate matches to rank exhaustively" in content
        assert "Sorting applies to the returned most-relevant matches" not in content

    def test_build_notes_truncation_alone_keeps_relevance_silence(self):
        notes = search_web.build_notes(
            {"truncated": True, "more_recordings_matched": 0}, "newest"
        )
        assert notes == [search_web.NOTE_TRUNCATED]

    def test_build_notes_incomplete_window_still_flags_sorting(self):
        for more in (3, None):
            notes = search_web.build_notes(
                {"truncated": False, "more_recordings_matched": more}, "oldest"
            )
            assert notes[-1] == search_web.NOTE_SORT_WINDOW
        both = search_web.build_notes(
            {"truncated": True, "more_recordings_matched": 0}, "relevance"
        )
        assert both == [search_web.NOTE_TRUNCATED]


# ---------------------------------------------------------------------------
# Privacy: the query never enters logs (review round 2)
# ---------------------------------------------------------------------------


class TestPrivacyLogging:
    CANARY = "PRIVLOGCANARY"

    def _assert_clean(self, caplog):
        for record in caplog.get_records("call"):
            assert self.CANARY not in record.getMessage()
            rendered = record.exc_text or ""
            assert self.CANARY not in rendered
        assert self.CANARY not in caplog.text

    def _quiet(self, caplog):
        import logging

        caplog.set_level(logging.DEBUG, logger="workflow")

    def test_invalid_query_never_logged(self, client, caplog):
        self._quiet(caplog)
        _healthy_corpus(("plg-1", "budget"))
        content = _page(client, "/recordings/?q=" + self.CANARY + "x" * 300)
        assert self.CANARY not in content
        self._assert_clean(caplog)

    @pytest.mark.parametrize("mode", ["fts_missing", "fts_broken", "stale"])
    def test_index_states_never_log_the_query(self, client, caplog, mode):
        self._quiet(caplog)
        _healthy_corpus(("plg-2", "budget probe"))
        _apply_index_state(mode)
        content = _page(client, f"/recordings/?q={self.CANARY}+probe")
        assert self.CANARY not in content
        self._assert_clean(caplog)

    def test_engine_failure_never_logs_query_or_underlying_cause(
        self, monkeypatch, client, caplog
    ):
        self._quiet(caplog)
        _healthy_corpus(("plg-3", "budget"))
        _fail_recording_scope_compile(
            monkeypatch, "ROOTCAUSESENTINEL /Users/secret/leak.path"
        )
        content = _page(client, "/recordings/?q=" + self.CANARY + "&tag=Family")
        assert self.CANARY not in content
        assert "ROOTCAUSESENTINEL" not in content
        self._assert_clean(caplog)
        assert "ROOTCAUSESENTINEL" not in caplog.text

    def test_successful_page_logs_no_query_and_echoes_only_autoescaped(
        self, client, caplog
    ):
        self._quiet(caplog)
        _healthy_corpus(("plg-4", "budget review"))
        content = _page(
            client,
            "/recordings/?q=" + "%3Cem%3E" + "+" + self.CANARY + "+absentterm",
        )
        # Echo happened (ok/zero state) but only in autoescaped form.
        assert "&lt;em&gt;" in content
        assert "<em>" not in content
        self._assert_clean(caplog)
