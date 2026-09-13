"""Library keyword search over the web (Step 5A.4.2a).

Covers the approved contract: one submitted search GET runs the FULL
health sweep EXACTLY once; filters scope the engine candidate set
BEFORE limiting/pagination; sorting spans the returned match set;
invalid-query and index-failure states clear the query (the rejected
text appears NOWHERE in the response); links persist q/view/filters;
GET stays strictly read-only with no N+1 growth; the five sort
fallback rules hold; CJK short terms work end-to-end; the unified top
bar is the ONE query input POSTing every mode (keyword → canonical
GET redirect) to the dedicated endpoint (Step 5C + unified top bar).
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
from workflow.models import Section
from workflow.query import (
    LibraryItemCard,
    ListFilters,
    library_item_key_queryset,
    list_filters,
)
from workflow.services.segmentation import save_segmented_version

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


def _fail_item_scope_compile(monkeypatch, sentinel):
    """Only the LIBRARY-ITEM-scope UNION compilation (the one-column
    ``item_key`` subquery) fails underneath — the gate's SearchDocument
    sweep and every other query stay intact."""
    from django.db.models.sql.compiler import SQLCompiler

    real_as_sql = SQLCompiler.as_sql

    def guarded(self, *args, **kwargs):
        query = self.query
        # ONLY the unsliced one-column item_key UNION fails; the union
        # branches and every unrelated query pass through.
        projection = tuple(getattr(query, "values_select", ())) + tuple(
            getattr(query, "annotation_select", ())
        )
        if (
            getattr(query, "combinator", None) == "union"
            and not query.is_sliced
            and projection == ("item_key",)
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

    def test_item_scope_compiler_failure_is_a_sanitized_index_state(
        self, monkeypatch, client
    ):
        _healthy_corpus(("cboom-1", "budget review"))
        _fail_item_scope_compile(monkeypatch, "TOPSECRETPATH-canary-SELECT")
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

        real_apply_item_sort = search_web.apply_item_sort

        def guarded(qs, sort):
            if sort == "relevance":
                raise AssertionError("apply_item_sort must never see relevance")
            return real_apply_item_sort(qs, sort)

        monkeypatch.setattr(search_web, "apply_item_sort", guarded)
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

    # -- Step 6.3 item-native sorts -----------------------------------------

    def _item_corpus(self, sha_prefix):
        """One split recording (Sections "Zeta topic"/"Alpha topic") plus
        one unsplit recording titled "Mid recording"; every item matches
        the query, so the winner set IS the Library item set."""
        base = datetime(2026, 2, 10, 9, 0, tzinfo=TZ)
        rec, transcript, _fixed = make_transcribed_recording(
            [f"{sha_prefix} sablemark first", f"{sha_prefix} sablemark second"],
            sha=f"{sha_prefix}-split",
        )
        from workflow.models import Recording

        Recording.objects.filter(pk=rec.pk).update(recorded_at=base)
        save_segmented_version(
            rec.pk, transcript.pk, 0, 2, [1], ["Zeta topic", "Alpha topic"]
        )
        sections = list(
            Section.objects.filter(
                segmented_version__transcript=transcript
            ).order_by("ordinal")
        )
        mid, m_transcript, m_section = make_transcribed_recording(
            [f"{sha_prefix} sablemark middle"], sha=f"{sha_prefix}-mid"
        )
        Recording.objects.filter(pk=mid.pk).update(recorded_at=base + timedelta(days=1))
        make_summary_version(mid, m_transcript, m_section, title="Mid recording")
        si.rebuild_index()
        return rec, mid, sections

    def test_title_sorts_interleave_sections_with_recordings(self, client):
        """Non-relevance sorts use the EXACT Library ITEM sort semantics:
        a Section sorts by its OWN derived display title — interleaving
        with Recording items — never parent-only grouping that keeps one
        parent's Sections adjacent."""
        rec, mid, sections = self._item_corpus("isort-title")
        zeta, alpha = sections  # ordinal 1 is "Zeta topic", 2 is "Alpha topic"
        az_order = [
            f"{rec.pk}/sections/{alpha.pk}",
            f"{mid.pk}",
            f"{rec.pk}/sections/{zeta.pk}",
        ]
        za_order = [
            f"{rec.pk}/sections/{zeta.pk}",
            f"{mid.pk}",
            f"{rec.pk}/sections/{alpha.pk}",
        ]
        content = _page(client, "/recordings/?q=sablemark&sort=title_az&view=table")
        _positions(content, az_order)
        content = _page(client, "/recordings/?q=sablemark&sort=title_za&view=table")
        _positions(content, za_order)

    def test_date_sorts_use_parent_effective_date_with_item_key_ties(self, client):
        """newest/oldest order by the PARENT effective date (shared with
        the Library) and a same-parent Section tie resolves through the
        unique item_key tie-breaker — deterministic, never engine order."""
        rec, mid, sections = self._item_corpus("isort-date")
        # ``mid`` was recorded one day AFTER the split parent: newest
        # starts with it, oldest ends with it; the two Sections of the
        # older parent keep their shared item_key tie order.
        tie = sorted([f"{rec.pk}/sections/{s.pk}" for s in sections])
        newest = _page(client, "/recordings/?q=sablemark&sort=newest&view=table")
        _positions(newest, [f"{mid.pk}", *tie])
        oldest = _page(client, "/recordings/?q=sablemark&sort=oldest&view=table")
        _positions(oldest, [*tie, f"{mid.pk}"])

    def test_library_sorts_equal_the_normal_library_item_order(self):
        """Oracle test: for every non-relevance sort the search winner
        order EQUALS the normal Library's ``apply_item_sort`` order over
        the same Library items under the same (empty) filters."""
        from workflow.query import apply_item_sort, library_item_queryset

        rec, mid, sections = self._item_corpus("isort-oracle")
        winners = {
            f"s:{s.pk}" for s in sections
        } | {f"r:{search_web._norm_id(mid.pk)}"}
        for sort in ("newest", "oldest", "title_az", "title_za"):
            outcome = search_web.run_web_search(
                raw_query="sablemark",
                filters=ListFilters(sort=sort),
                timezone_name="Europe/Helsinki",
                page_number=1,
                per_page=25,
            )
            assert outcome.state == search_web.STATE_OK
            got = [search_web._item_key_for_card(row.card) for row in outcome.rows]
            assert set(got) == winners, sort
            ordered = apply_item_sort(
                library_item_queryset(ListFilters(), "Europe/Helsinki"), sort
            )
            expected = [
                row["item_key"] for row in ordered if row["item_key"] in winners
            ]
            assert got == expected, sort


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

    def test_engine_receives_a_library_item_scope_union(self, monkeypatch, client):
        """Step 6.3: the web hands the keyword engine the normal Library's
        item scope — the unsliced one-column ``item_key`` UNION from
        ``library_item_key_queryset`` — and never the Recording scope."""
        self._corpus()
        seen = {}
        real = search_web.search_query.search_recordings

        def spy(query, **kwargs):
            seen["item_scope"] = kwargs.get("item_scope", "MISSING")
            seen["scope"] = kwargs.get("scope", "MISSING")
            return real(query, **kwargs)

        monkeypatch.setattr(search_web.search_query, "search_recordings", spy)
        _page(client, "/recordings/?q=sablemark&tag=Project")
        from django.db.models import QuerySet

        from workflow.services.search_query import _item_projection_names

        assert seen["scope"] == "MISSING"  # never both eligibility mechanisms
        scope = seen["item_scope"]
        assert isinstance(scope, QuerySet)
        query = scope.query
        assert not query.is_sliced
        assert query.combinator == "union"
        # The exact shape the shared compile_item_scope contract expects.
        assert _item_projection_names(query) == ("item_key",)
        assert all(
            _item_projection_names(branch) == ("item_key",)
            for branch in query.combined_queries
        )

    def test_empty_scope_answers_zero_results_not_an_error(self, monkeypatch, client):
        """A scope that provably contains NO Library item is a valid zero
        answer — never a 500, an index-error state or an invalid query."""
        from workflow.query import library_item_key_queryset as real_keys

        _healthy_corpus(("escope-1", "budget review"))

        def _empty_keys(filters, timezone_name, using="default"):
            return real_keys(filters, timezone_name, using=using).none()

        monkeypatch.setattr(search_web, "library_item_key_queryset", _empty_keys)
        content = _page(client, "/recordings/?q=budget&tag=Project")
        assert "No search results for" in content
        assert "recording-card" not in content
        assert "message-error" not in content
        assert "search-index" not in content

    def test_compiler_empty_scope_answers_zero_results_not_an_error(
        self, monkeypatch, client
    ):
        """Branch-level ``filter(pk__in=[])`` emptiness is proven only by
        the compiler (``EmptyResultSet`` from the UNION's ``as_sql``): the
        web layer must still render the ordinary zero-results state, not
        any error state."""
        from django.db.models import CharField, F, Value
        from django.db.models.functions import Concat

        from workflow.models import Recording, Section

        _healthy_corpus(("escope-2", "budget review"))

        def _empty_branch_union(*_args, **_kwargs):
            recording_keys = (
                Recording.objects.filter(pk__in=[])
                .annotate(
                    item_key=Concat(
                        Value("r:"), F("pk"), output_field=CharField()
                    )
                )
                .order_by()
                .values("item_key")
            )
            section_keys = (
                Section.objects.filter(pk__in=[])
                .annotate(
                    item_key=Concat(
                        Value("s:"), F("pk"), output_field=CharField()
                    )
                )
                .order_by()
                .values("item_key")
            )
            return recording_keys.union(section_keys)

        monkeypatch.setattr(
            search_web, "library_item_key_queryset", _empty_branch_union
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

    def test_invalid_scope_filter_fallback_uses_unfiltered_item_scope(
        self, monkeypatch
    ):
        """Step 6.3: the invalid-filter keyword fallback runs over the
        UNFILTERED canonical Library ITEM scope — still item mode, never
        the historical whole-Recording engine mode — and the page-window
        item revalidation runs under the SAME unfiltered identity, so a
        filter the policy just ignored never drops a rendered item."""
        from workflow.services.search_query import _item_projection_names

        _tag, _in, outside = self._corpus()

        seen = {}
        real = search_web.search_query.search_recordings

        def spy(query, **kwargs):
            seen["scope"] = kwargs.get("scope", "MISSING")
            seen["item_scope"] = kwargs.get("item_scope", "MISSING")
            return real(query, **kwargs)

        monkeypatch.setattr(search_web.search_query, "search_recordings", spy)

        hydrations = {}
        real_items = search_web.library_items_by_keys

        def spy_items(item_keys, item_filters, timezone_name, *, using="default"):
            hydrations["keys"] = list(item_keys)
            hydrations["filters"] = item_filters
            return real_items(item_keys, item_filters, timezone_name, using=using)

        monkeypatch.setattr(search_web, "library_items_by_keys", spy_items)

        filters = list_filters(
            QueryDict("tag=Project&from=notadate&sort=relevance"),
            "Europe/Helsinki",
            allow_relevance=True,
        )
        assert not filters.scope_valid  # the date error poisons the scope
        assert filters.tags == ["project"]  # the valid tag was parsed anyway

        outcome = search_web.run_web_search(
            raw_query="sablemark",
            filters=filters,
            timezone_name="Europe/Helsinki",
            page_number=1,
            per_page=25,
        )
        assert outcome.state == search_web.STATE_OK
        assert outcome.unscoped_filters is True

        # Item mode, never the Recording scope and never a scope-less
        # whole-Recording engine call.
        assert seen["scope"] == "MISSING"
        scope = seen["item_scope"]
        assert scope.query.combinator == "union"
        assert not scope.query.is_sliced
        assert _item_projection_names(scope.query) == ("item_key",)

        # UNFILTERED: exactly the canonical unfiltered item identity —
        # the ignored tag/date change neither the SQL nor its parameters.
        plain_sql, plain_params = (
            library_item_key_queryset(ListFilters(), "Europe/Helsinki")
            .query.get_compiler("default")
            .as_sql()
        )
        sql, params = scope.query.get_compiler("default").as_sql()
        assert (sql, params) == (plain_sql, plain_params)

        # Every match survives (the ignored tag hides nothing), and the
        # revalidation ran under the UNFILTERED canonical scope.
        assert outcome.result_count == 4
        assert len(outcome.rows) == 4
        assert hydrations["keys"]
        assert hydrations["filters"] is not filters
        assert not hydrations["filters"].tags
        assert hydrations["filters"].date_from is None
        assert str(outside.pk) in {
            search_web._norm_id(row.card.recording_id) for row in outcome.rows
        }

    def test_more_matches_note_is_scope_honest(self, monkeypatch, client):
        self._corpus()
        monkeypatch.setattr(search_web, "WEB_SCAN_LIMIT", 2)
        content = _page(client, "/recordings/?q=sablemark")
        # Item mode ALWAYS counts library items — even though this
        # unsplit corpus matches only Recording-backed items.
        assert "2 more library items also matched these filters (showing the 2 " in content
        assert "more recordings also matched" not in content
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
# Step 6.3 item hydration: the page window hydrates through
# library_items_by_keys into LibraryItemCards (order, provenance and the
# page bound preserved; stale engine keys never resurrect an item)
# ---------------------------------------------------------------------------


class TestItemHydration:
    def _split(self, sha, texts, splits, titles):
        """One transcribed recording split into len(splits)+1 topic
        Sections plus a healthy index."""
        rec, transcript, _fixed = make_transcribed_recording(texts, sha=sha)
        save_segmented_version(
            rec.pk,
            transcript.pk,
            0,
            len(texts),
            list(splits),
            list(titles),
        )
        si.rebuild_index()
        sections = list(
            Section.objects.filter(segmented_version__transcript=transcript).order_by(
                "ordinal"
            )
        )
        return rec, transcript, sections

    def _run(self, query):
        return search_web.run_web_search(
            raw_query=query,
            filters=ListFilters(sort="relevance"),
            timezone_name="Europe/Helsinki",
            page_number=1,
            per_page=25,
        )

    def test_split_recording_answers_section_item_cards(self, client):
        """A valid active split layout yields the Section items as
        LibraryItemCards with the parent Recording suppressed — never
        parent + section duplicates."""
        rec, _transcript, sections = self._split(
            "itemh-split-1",
            [
                "sablemark opening discussion",
                "plain second segment",
                "sablemark closing discussion",
                "plain fourth segment",
            ],
            [2],
            ["Opening topic", "Closing topic"],
        )
        outcome = self._run("sablemark")
        assert outcome.state == search_web.STATE_OK
        assert outcome.result_count == 2
        assert len(outcome.rows) == 2
        assert all(isinstance(row.card, LibraryItemCard) for row in outcome.rows)
        assert all(row.card.is_section for row in outcome.rows)
        assert {row.card.section_id for row in outcome.rows} == {
            section.pk for section in sections
        }
        # The parent Recording answers NO recording-backed item row.
        assert not any(
            not row.card.is_section and row.card.recording_id == rec.pk
            for row in outcome.rows
        )
        # Two distinct Section winners of ONE parent stay two distinct
        # rows (never collapsed by the parent id).
        content = _page(client, "/recordings/?q=sablemark")
        assert "2 search results" in content
        assert "Opening topic" in content and "Closing topic" in content

    def test_rows_follow_the_engine_winner_order(self, monkeypatch):
        """Hydration reorders back to the engine's exact winner order
        (the returned cards are never left in hydration order)."""
        _rec, _transcript, sections = self._split(
            "itemh-order-1",
            [
                "sablemark opening discussion",
                "plain second segment",
                "sablemark closing discussion",
                "plain fourth segment",
            ],
            [2],
            ["Opening topic", "Closing topic"],
        )
        captured = {}
        real = search_web.search_query.search_recordings

        def spy(query, **kwargs):
            payload = real(query, **kwargs)
            captured["keys"] = [result["item_key"] for result in payload["results"]]
            return payload

        monkeypatch.setattr(search_web.search_query, "search_recordings", spy)
        outcome = self._run("sablemark")
        assert captured["keys"]  # sanity: both section items matched
        assert [
            search_web._item_key_for_card(row.card) for row in outcome.rows
        ] == captured["keys"]
        assert {search_web._item_key_for_card(row.card) for row in outcome.rows} == {
            f"s:{section.pk}" for section in sections
        }

    def test_segment_provenance_survives_item_hydration(self):
        """Each Section winner keeps its OWN validated segment link:
        the batch validation is keyed by the winner key and joins the
        transcript to the item's parent Recording."""
        _rec, _transcript, _sections = self._split(
            "itemh-link-1",
            [
                "sablemark opening discussion",
                "plain second segment",
                "sablemark closing discussion",
                "plain fourth segment",
            ],
            [2],
            ["Opening topic", "Closing topic"],
        )
        outcome = self._run("sablemark")
        assert len(outcome.rows) == 2
        anchors = {row.link_anchor for row in outcome.rows}
        assert anchors == {"segment-0", "segment-2"}
        assert all(row.link_page == 1 for row in outcome.rows)
        assert all(row.match_source == "segment" for row in outcome.rows)

    def test_recording_backed_rows_are_item_cards_too(self):
        """An unsplit corpus hydrates its Recording items through the
        SAME item contract: every row carries a LibraryItemCard."""
        _healthy_corpus(("itemh-plain-1", "sablemark plain recording"))
        outcome = self._run("sablemark")
        assert outcome.result_count == 1
        assert len(outcome.rows) == 1
        card = outcome.rows[0].card
        assert isinstance(card, LibraryItemCard)
        assert not card.is_section

    def test_only_the_page_window_is_hydrated(self, monkeypatch, client, tmp_path):
        """The hydration input is the bounded page window, never the
        whole winner set."""
        _healthy_corpus(
            *[(f"itemh-bound-{index}", f"sablemark recording {index}") for index in range(4)]
        )
        _tiny_pages(monkeypatch, tmp_path, per_page=2)
        calls = []
        real_items = search_web.library_items_by_keys

        def spy(item_keys, *args, **kwargs):
            calls.append(list(item_keys))
            return real_items(item_keys, *args, **kwargs)

        monkeypatch.setattr(search_web, "library_items_by_keys", spy)
        content = _page(client, "/recordings/?q=sablemark")
        assert "4 search results" in content
        assert len(calls) == 1
        assert len(calls[0]) == 2

    def test_more_note_counts_library_items_for_a_split_population(
        self, monkeypatch, client
    ):
        """The rendered beyond-window note follows the engine's explicit
        ``item_mode`` marker: the web search runs in item mode, so a
        bounded scan window counts LIBRARY ITEMS — never "recordings",
        whatever rows the page shows."""
        self._split(
            "itemh-note-1",
            [
                "sablemark opening discussion",
                "plain second segment",
                "sablemark closing discussion",
                "plain fourth segment",
            ],
            [2],
            ["Opening topic", "Closing topic"],
        )
        _healthy_corpus(("itemh-note-plain", "sablemark plain recording"))
        monkeypatch.setattr(search_web, "WEB_SCAN_LIMIT", 2)
        content = _page(client, "/recordings/?q=sablemark")
        # THREE matched items (two Sections + the plain Recording) with
        # a window of two: the honest note names library items.
        assert (
            "1 more library items also matched these filters "
            "(showing the 2 most relevant)." in content
        )
        assert "more recordings also matched" not in content

    def _stale_payload(self, monkeypatch, forge):
        """The real engine runs, then every winner key is FORGED to the
        simulate-a-stale-engine-truth shape ``forge`` produces."""
        real = search_web.search_query.search_recordings

        def spy(query, **kwargs):
            payload = real(query, **kwargs)
            for result in payload["results"]:
                result["item_key"] = forge(result)
            return payload

        monkeypatch.setattr(search_web.search_query, "search_recordings", spy)

    def test_replaced_parent_key_never_resurrects_a_row(self, monkeypatch, client):
        """A stale engine key naming the parent Recording of a valid
        active split layout is dropped by the revalidation — the
        replacement can never be bypassed into a fake parent row."""
        rec, _transcript, sections = self._split(
            "itemh-stale-1",
            ["sablemark opening discussion", "plain second segment"],
            [1],
            ["Opening topic", "Closing topic"],
        )
        assert len(sections) == 2
        outcome = self._run("sablemark")
        assert outcome.result_count == 1  # sanity: the section item matched
        self._stale_payload(
            monkeypatch, lambda result: f"r:{result['recording_id']}"
        )
        outcome = self._run("sablemark")
        assert outcome.state == search_web.STATE_OK
        assert outcome.rows == []
        response = client.get("/recordings/?q=sablemark")
        assert response.status_code == 200

    def test_unknown_section_key_never_resurrects_a_row(self, monkeypatch):
        """A key naming a Section that is not a valid active-layout topic
        item hydrates nothing — never a fabricated card."""
        self._split(
            "itemh-stale-2",
            ["sablemark opening discussion", "plain second segment"],
            [1],
            ["Opening topic", "Closing topic"],
        )
        self._stale_payload(monkeypatch, lambda result: "s:999999")
        outcome = self._run("sablemark")
        assert outcome.state == search_web.STATE_OK
        assert outcome.rows == []


# ---------------------------------------------------------------------------
# Step 6.3 item-native search presentation: Section rows mirror the
# normal Library card/table semantics (section-detail links, derived
# title, item duration, section-scoped tags/languages, parent/range
# context) with the snippet/provenance on top and NO library-return
# token; Recording rows keep their historical search rendering
# ---------------------------------------------------------------------------


def _search_cards(content):
    """The rendered result cards, in page order (cards view only)."""
    return re.findall(r'<li class="recording-card.*?</li>', content, re.S)


def _search_table_rows(content):
    """The rendered result rows, in page order (table view only)."""
    body = content[content.find("<tbody>") :]
    return re.findall(r"<tr class=.*?</tr>", body, re.S)


class TestItemPresentation:
    """One split recording (two Section items) plus one unsplit
    Recording item; every item matches ``sablemark``.

    Section layout: ``[0,2)`` "Draft opener" carrying a DEFAULT-variant
    Summary titled "Summarised opener" (the derived display title that
    supersedes the stored one) and a Section-scoped "Split Child" tag;
    ``[2,4)`` "Plain closer" whose DEFAULT variant FAILED. The parent
    holds an active whole-recording Summary "Parent umbrella" and a
    recording-scoped "Parent Only" tag the Section items must not show.
    """

    @staticmethod
    def _corpus():
        from workflow.models import SummaryVariantState, TagAssignment

        rec, transcript, fixed = make_transcribed_recording(
            [
                "sablemark opening discussion",
                "plain second segment",
                "sablemark closing discussion",
                "plain fourth segment",
            ],
            sha="ispres-split",
        )
        save_segmented_version(
            rec.pk, transcript.pk, 0, 4, [2], ["Draft opener", "Plain closer"]
        )
        first, second = Section.objects.filter(
            segmented_version__transcript=transcript
        ).order_by("ordinal")
        make_summary_version(rec, transcript, fixed, title="Parent umbrella")
        make_summary_version(
            rec,
            transcript,
            first,
            title="Summarised opener",
            overview="Opening overview.",
        )
        SummaryVariantState.objects.create(
            transcript=transcript,
            section=second,
            output_language="en",
            status="failed",
        )
        parent_tag = make_tag("Parent Only")
        make_tag_assignment(rec, parent_tag, origin="manual")
        child_tag = make_tag("Split Child")
        TagAssignment.objects.create(
            recording=rec, section=first, tag=child_tag, origin="manual", is_active=True
        )
        plain, plain_transcript, plain_section = make_transcribed_recording(
            ["sablemark plain recording"], sha="ispres-plain"
        )
        make_summary_version(
            plain, plain_transcript, plain_section, title="Plain recording"
        )
        standalone = make_tag("Standalone")
        make_tag_assignment(plain, standalone, origin="manual")
        si.rebuild_index()
        return rec, first, second, plain

    @staticmethod
    def _card_for(cards, needle):
        matches = [card for card in cards if needle in card]
        assert len(matches) == 1, (needle, len(matches))
        return matches[0]

    def test_card_view_section_rows_match_the_library(self, client):
        rec, first, second, plain = self._corpus()
        content = _page(client, "/recordings/?q=sablemark&view=cards")
        cards = _search_cards(content)
        assert len(cards) == 3  # two Section items + one Recording item

        first_card = self._card_for(cards, f"/sections/{first.pk}/")
        # Section title links to the SECTION DETAIL with the derived
        # (default-Summary) title, never the stored temporary title.
        assert f'href="/recordings/{rec.pk}/sections/{first.pk}/"' in first_card
        assert ">Summarised opener</a>" in first_card
        assert "Draft opener" not in first_card
        # Item duration (the two-segment span), NOT the parent's 60s.
        assert "2s" in first_card and "1m 00s" not in first_card
        # Section-scoped tags only: the parent's recording tag is absent.
        assert "Split Child" in first_card
        assert "Parent Only" not in first_card
        # Section summary language.
        assert 'class="lang-code"' in first_card and "en" in first_card
        # Parent/range context exactly like the normal Library card.
        assert (
            f'Topic · segments 0–1 · in <a href="/recordings/{rec.pk}/">'
            "Parent umbrella</a>" in first_card
        )
        # Snippet and validated segment provenance survive.
        assert "<mark>sablemark</mark>" in first_card
        assert f'href="/recordings/{rec.pk}/transcript/?page=1#segment-0"' in first_card
        assert "needs-attention" not in first_card

        second_card = self._card_for(cards, f"/sections/{second.pk}/")
        assert f'href="/recordings/{rec.pk}/sections/{second.pk}/"' in second_card
        assert ">Plain closer</a>" in second_card  # stored title without a Summary
        # The FAILED section-scoped default-variant state surfaces as
        # item-level needs-attention (the sibling and the parent do not
        # carry it) — the same row class the normal Library renders.
        assert "needs-attention" in second_card
        assert "Topic · segments 2–3" in second_card

        plain_card = self._card_for(cards, f'href="/recordings/{plain.pk}/"')
        assert f'href="/recordings/{plain.pk}/"' in plain_card
        assert "Plain recording" in plain_card
        assert "1m 00s" in plain_card  # the recording's own duration
        assert "Standalone" in plain_card  # recording-scope tags unchanged
        assert "section-context" not in plain_card  # recording rows unchanged
        assert content.count("section-context") == 2  # exactly the two Section rows

    def test_table_view_section_rows_match_the_library(self, client):
        rec, first, second, plain = self._corpus()
        content = _page(client, "/recordings/?q=sablemark&view=table")
        rows = _search_table_rows(content)
        assert len(rows) == 3

        first_row = self._card_for(rows, f"/sections/{first.pk}/")
        assert f'href="/recordings/{rec.pk}/sections/{first.pk}/"' in first_row
        assert ">Summarised opener</a>" in first_row
        assert "2s" in first_row and "1m 00s" not in first_row
        assert "Split Child" in first_row
        assert "Parent Only" not in first_row
        assert "en" in first_row
        assert "<mark>sablemark</mark>" in first_row

        second_row = self._card_for(rows, f"/sections/{second.pk}/")
        assert 'class="needs-attention"' in second_row

        plain_row = self._card_for(rows, f'href="/recordings/{plain.pk}/"')
        assert f'href="/recordings/{plain.pk}/"' in plain_row
        assert "1m 00s" in plain_row

    def test_card_and_table_carry_the_same_item_links(self, client):
        """Card/table parity: both views lead every result unit with the
        exact same item link (Section → section-detail, Recording →
        recording-detail) in the same order."""
        rec, first, second, plain = self._corpus()
        links = re.compile(r'href="(/recordings/[^"#?]*)"')
        cards_view = _page(client, "/recordings/?q=sablemark&view=cards")
        table_view = _page(client, "/recordings/?q=sablemark&view=table")

        def item_links(units):
            found = []
            for unit in units:
                for href in links.findall(unit):
                    # The title link is the unit's FIRST non-provenance
                    # recording link in BOTH branches.
                    if not href.endswith("/transcript/"):
                        found.append(href)
                        break
            return found

        per_card = item_links(_search_cards(cards_view))
        per_row = item_links(_search_table_rows(table_view))
        assert per_card == per_row
        assert set(per_card) == {
            f"/recordings/{rec.pk}/sections/{first.pk}/",
            f"/recordings/{rec.pk}/sections/{second.pk}/",
            f"/recordings/{plain.pk}/",
        }

    def test_no_library_return_token_on_search_links(self, client):
        """Search results NEVER carry a lib_return token — but the same
        Section's normal-Library link does."""
        rec, first, _second, _plain = self._corpus()
        library = _page(client, "/recordings/")
        assert f"/recordings/{rec.pk}/sections/{first.pk}/?lib_return=" in library
        for view in ("cards", "table"):
            content = _page(client, f"/recordings/?q=sablemark&view={view}")
            assert "lib_return" not in content

    def test_unscoped_fallback_carries_no_token(self, client):
        """The unscoped-fallback render (invalid filter) also carries no
        token on any link."""
        self._corpus()
        content = _page(client, "/recordings/?q=sablemark&from=notadate")
        assert "Search ran without the invalid filters" in content
        assert "lib_return" not in content


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

    def test_unified_topbar_is_the_only_query_input(self, client):
        _healthy_corpus(("ctl-1", "budget review"))
        for url in ("/recordings/", "/recordings/?q=budget"):
            content = _page(client, url)
            # The global top bar is the ONE query input: a single POST
            # form to the dedicated endpoint carrying the query, a native
            # Keyword/Semantic/Hybrid select and a submit button.
            assert (
                '<form class="topbar-search" role="search" method="post" action="/recordings/search/">'
                in content
            )
            assert 'name="csrfmiddlewaretoken"' in content
            assert '<select id="global-search-mode" name="mode"' in content
            assert '<option value="keyword"' in content
            assert '<option value="semantic"' in content
            assert '<option value="hybrid"' in content
            assert '<button type="submit" class="topbar-search-btn">Search</button>' in content
            assert 'href="/recordings/search/' not in content
            # No duplicate query input remains in Library content: exactly
            # one search input on the whole page.
            assert len(re.findall(r'<input[^>]*type="search"', content)) == 1
            # No semantic/hybrid section in Library content any more.
            assert "vector-search" not in content

    def test_topbar_defaults_to_keyword_and_echoes_the_current_query(self, client):
        _healthy_corpus(("ctl-2", "budget review"))
        # Plain Library: Keyword selected, empty input.
        content = _page(client, "/recordings/")
        assert '<option value="keyword" selected>' in content
        assert '<option value="semantic">' in content
        assert '<option value="hybrid">' in content
        # Keyword GET results: Keyword stays selected and the input echoes
        # the active query for refinement.
        content = _page(client, "/recordings/?q=budget")
        assert '<option value="keyword" selected>' in content
        m = re.search(r'<input[^>]*name="q"[^>]*>', content)
        assert 'value="budget"' in m.group(0)

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

    def test_build_notes_unit_follows_the_item_mode_flag(self):
        base = {"truncated": False, "more_recordings_matched": 3}
        # EXPLICIT item mode: both the exact and the unknown
        # beyond-window notes count LIBRARY ITEMS.
        exact = search_web.build_notes(
            dict(base, item_mode=True), "relevance", scan_limit=2
        )
        assert exact == [
            search_web.NOTE_MORE_EXACT_ITEMS.format(more=3, scan=2)
        ]
        assert "library items" in exact[0]
        unknown = search_web.build_notes(
            dict(base, more_recordings_matched=None, item_mode=True),
            "relevance",
        )
        assert unknown == [search_web.NOTE_MORE_UNKNOWN_ITEMS]
        assert "library items" in unknown[0]
        # The non-relevance sort-window note still appends after the
        # item-unit note.
        sorted_notes = search_web.build_notes(
            dict(base, item_mode=True), "newest", scan_limit=2
        )
        assert sorted_notes == [
            search_web.NOTE_MORE_EXACT_ITEMS.format(more=3, scan=2),
            search_web.NOTE_SORT_WINDOW,
        ]
        # A payload WITHOUT the marker — a legacy/hand-built recording
        # payload (item engines always carry it) — keeps the historical
        # Recording wordings verbatim.
        for payload in (dict(base, item_mode=False), base):
            assert search_web.build_notes(payload, "relevance", scan_limit=2) == [
                search_web.NOTE_MORE_EXACT.format(more=3, scan=2)
            ]
            assert search_web.build_notes(
                dict(payload, more_recordings_matched=None), "relevance"
            ) == [search_web.NOTE_MORE_UNKNOWN]

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
        _fail_item_scope_compile(
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



# ---------------------------------------------------------------------------
# Step 5A.4.2b — snippet fragments (deterministic malformed-range policy)
# ---------------------------------------------------------------------------


def _assert_partition(fragments, text):
    """Fragments must partition the exact text in exact plain str."""
    assert "".join(f.text for f in fragments) == text
    for fragment in fragments:
        assert type(fragment.text) is str


class TestSnippetFragmentPolicy:
    def test_marked_fragments_partition_the_text(self):
        text = "quarterly budget review budget end"
        fragments = search_web.snippet_fragments(
            {"text": text, "matches": [{"start": 10, "end": 16}, {"start": 24, "end": 30}]}
        )
        _assert_partition(fragments, text)
        assert [(f.text, f.mark) for f in fragments] == [
            ("quarterly ", False),
            ("budget", True),
            (" review ", False),
            ("budget", True),
            (" end", False),
        ]

    def test_touching_and_overlapping_ranges_merge(self):
        touching = search_web.snippet_fragments(
            {"text": "abcdef", "matches": [{"start": 1, "end": 3}, {"start": 3, "end": 5}]}
        )
        assert [(f.text, f.mark) for f in touching] == [
            ("a", False),
            ("bcde", True),
            ("f", False),
        ]
        overlap = search_web.snippet_fragments(
            {"text": "abcdef", "matches": [{"start": 1, "end": 4}, {"start": 2, "end": 6}]}
        )
        assert [(f.text, f.mark) for f in overlap] == [("a", False), ("bcdef", True)]

    def test_ranges_are_clamped_and_reordered(self):
        fragments = search_web.snippet_fragments(
            {"text": "abcdef", "matches": [{"start": 4, "end": 99}, {"start": -5, "end": 2}]}
        )
        _assert_partition(fragments, "abcdef")
        assert [(f.text, f.mark) for f in fragments] == [
            ("ab", True),
            ("cd", False),
            ("ef", True),
        ]

    def test_inverted_and_zero_length_ranges_ignored(self):
        fragments = search_web.snippet_fragments(
            {"text": "abcdef", "matches": [{"start": 5, "end": 5}, {"start": 4, "end": 1}]}
        )
        assert [(f.text, f.mark) for f in fragments] == [("abcdef", False)]

    def test_malformed_individual_ranges_are_ignored(self):
        garbage = [
            "not-a-range",
            None,
            12,
            {"start": "a", "end": 2},
            {"start": 2, "end": None},
            {"start": True, "end": 3},
            {"start": 1, "end": True},
            {"start": 0},
            {"end": 3},
        ]
        fragments = search_web.snippet_fragments({"text": "abcdef", "matches": garbage})
        assert [(f.text, f.mark) for f in fragments] == [("abcdef", False)]
        # Malformed entries never suppress the VALID neighbours either.
        fragments = search_web.snippet_fragments(
            {"text": "abcdef", "matches": ["junk", {"start": 0, "end": 2}, {"start": True, "end": 5}]}
        )
        assert [(f.text, f.mark) for f in fragments] == [
            ("ab", True),
            ("cdef", False),
        ]

    def test_unusable_matches_shape_yields_one_plain_fragment(self):
        for matches in (None, "nope", {"start": 0, "end": 2}, 42, {}):
            fragments = search_web.snippet_fragments({"text": "abcdef", "matches": matches})
            assert [(f.text, f.mark) for f in fragments] == [("abcdef", False)]

    def test_non_str_text_yields_no_fragments_without_coercion(self):
        for snippet in (None, 123, "text", [], {}, {"text": 123}, {"text": None}, {"text": ""}):
            assert search_web.snippet_fragments(snippet) == []

    def test_never_raises_and_never_emits_safe_strings(self):
        from django.utils.safestring import SafeString, mark_safe

        hostile = {"text": mark_safe("<b>bold & friends</b>"), "matches": [{"start": 3, "end": 7}]}
        fragments = search_web.snippet_fragments(hostile)
        # A str SUBCLASS is normalized to a plain str copy, never kept.
        for fragment in fragments:
            assert type(fragment.text) is str
            assert not isinstance(fragment.text, SafeString)
        _assert_partition(fragments, "<b>bold & friends</b>")

    def test_matches_may_be_empty(self):
        fragments = search_web.snippet_fragments({"text": "abc", "matches": []})
        assert [(f.text, f.mark) for f in fragments] == [("abc", False)]


# ---------------------------------------------------------------------------
# Step 5A.4.2b — <mark> rendering (engine-real, autoescape-only)
# ---------------------------------------------------------------------------


class TestHighlightRendering:
    def test_basic_match_highlights_render_as_mark(self, client):
        _healthy_corpus(("hi-1", "quarterly budget review happenings"))
        content = _page(client, "/recordings/?q=budget")
        assert "<mark>budget</mark>" in content
        # The echoed query in the header is NOT part of the snippet markup.
        assert content.count("<mark>budget</mark>") == 1

    def test_multiple_ranges_highlighted_in_order(self, client):
        _healthy_corpus(("hi-2", "alpha and beta and alpha again inside one window"))
        content = _page(client, "/recordings/?q=alpha+beta")
        assert content.count("<mark>alpha</mark>") >= 2
        assert "<mark>beta</mark>" in content

    def test_sharp_s_casefold_expansion_highlight(self, client):
        import urllib.parse

        _healthy_corpus(("hi-ss", "die straße war sauber"))
        content = _page(client, "/recordings/?" + urllib.parse.urlencode({"q": "aß"}))
        assert "<mark>aß</mark>" in content  # whole source char, not "a" + half

    def test_ligature_casefold_expansion_highlight(self, client):
        _healthy_corpus(("hi-ffi", "ligature preﬃx marker here"))
        content = _page(client, "/recordings/?q=ff")
        assert "<mark>ﬃ</mark>" in content

    def test_cjk_and_emoji_highlights(self, client):
        import urllib.parse

        _healthy_corpus(("hi-cjk", "粤语同普通話嘅分別好重要"))
        content = _page(client, "/recordings/?" + urllib.parse.urlencode({"q": "普通話"}))
        assert "<mark>普通話</mark>" in content

        _healthy_corpus(("hi-emo", "mark a\U0001F600b\U0001F600c tail here"))
        content = _page(client, "/recordings/?" + urllib.parse.urlencode({"q": "\U0001F600b\U0001F600"}))
        assert "<mark>\U0001F600b\U0001F600</mark>" in content

    def test_malicious_indexed_content_escaped_while_benign_match_marks(self, client):
        _healthy_corpus(("hi-xss", "audit <img src=x onerror=alert(1)> meeting notes"))
        content = _page(client, "/recordings/?q=audit")
        assert "<mark>audit</mark>" in content
        assert "&lt;img src=x onerror=alert(1)&gt;" in content
        assert "<img" not in content
        assert "onerror=alert" not in content.replace("&lt;img src=x onerror=alert(1)&gt;", "")

    def test_malicious_query_term_marked_escaped(self, client):
        import urllib.parse

        _healthy_corpus(("hi-xss2", "budget <b> injection tail"))
        content = _page(client, "/recordings/?" + urllib.parse.urlencode({"q": "budget <b>"}))
        assert "<mark>budget</mark>" in content
        assert "<mark>&lt;b&gt;</mark>" in content
        assert "<mark><b></mark>" not in content

    def test_malicious_query_escaped_in_input_and_heading(self, client):
        import urllib.parse

        _healthy_corpus(("hi-xss3", "budget review"))
        payload = "<em>budget absentmatch"
        content = _page(client, "/recordings/?" + urllib.parse.urlencode({"q": payload}))
        assert "<em>" not in content
        assert "&lt;em&gt;budget absentmatch" in content  # input value + heading
        assert 'value="&lt;em&gt;budget absentmatch"' in content

    def test_no_safe_html_anywhere_in_the_search_rendering(self):
        from pathlib import Path

        root = Path(search_web.__file__).resolve().parent.parent.parent
        files = [
            root / "workflow" / "services" / "search_web.py",
            root / "templates" / "workflow" / "_search_snippet.html",
            root / "templates" / "workflow" / "_search_provenance.html",
            root / "templates" / "workflow" / "recording_list.html",
        ]
        for path in files:
            source = path.read_text()
            if path.suffix == ".html":
                assert "mark_safe" not in source
                assert "|safe" not in source
                assert "autoescape off" not in source
            else:
                # Production service: no safe-string machinery, no
                # django.utils.safestring at all, no generated HTML.
                assert "mark_safe(" not in source
                assert "SafeString(" not in source
                assert "SafeData" not in source
                assert "django.utils.safestring" not in source
                assert "format_html" not in source
                assert "conditional_escape" not in source


# ---------------------------------------------------------------------------
# Step 5A.4.2b — segment provenance jump links (bounded pair validation)
# ---------------------------------------------------------------------------


def _tiny_transcripts(monkeypatch, tmp_path, seg_per_page, recordings_per_page=25):
    config = make_config(
        tmp_path,
        web=default_web(
            recordings_per_page=recordings_per_page,
            transcript_segments_per_page=seg_per_page,
        ),
    )
    monkeypatch.setattr("workflow.views.recordings.get_config", lambda: config)


def _fake_engine(monkeypatch, results):
    def fake(query, **kwargs):
        return {
            "results": results,
            "result_count": len(results),
            "truncated": False,
            "more_recordings_matched": 0,
        }

    monkeypatch.setattr(search_web.search_query, "search_recordings", fake)


def _segment_result(rec, transcript, *, transcript_id="KEEP", ordinal=3, text="gamma marker tail"):
    match = {
        "source": "segment",
        "start_ms": 640,
        "transcript_id": transcript.pk if transcript_id == "KEEP" else transcript_id,
        "segment_ordinal": ordinal,
    }
    return {
        "rank": 1,
        "recording_id": str(rec.pk),
        "match": match,
        "snippet": {"text": text, "matches": [{"start": 6, "end": 12}]},
    }


class TestSegmentJumpLinks:
    def _link_corpus(self, sha, texts):
        rec, transcript, _s = make_transcribed_recording(texts, sha=sha)
        si.rebuild_index()
        return rec, transcript

    def test_active_same_recording_provenance_links_to_computed_page(self, client):
        rec, transcript = self._link_corpus(
            "link-a",
            ["nothing useful here", "marker one tail", "nothing else here", "marker marker tail"],
        )
        content = _page(client, "/recordings/?q=marker")
        # Ordinal 3 wins (two fold-occurrences) and lands on page 1 of 200.
        expected = f'href="/recordings/{rec.pk}/transcript/?page=1#segment-3"'
        assert expected in content
        assert '<a class="match-chip match-segment"' in content

    def test_ordinal_boundaries_map_onto_transcript_pages(self, monkeypatch, client, tmp_path):
        _tiny_transcripts(monkeypatch, tmp_path, seg_per_page=2)
        # Ordinal 1 -> page 1 (boundary 0..1), ordinal 3 -> page 2 (2..3).
        rec_low, _t_low = self._link_corpus("link-bound-low", ["plain tail", "marker one tail"])
        rec_high, _t_high = self._link_corpus(
            "link-bound-high", ["plain tail", "plain two", "plain three", "marker three tail"]
        )
        content = _page(client, "/recordings/?q=marker")
        assert f'href="/recordings/{rec_low.pk}/transcript/?page=1#segment-1"' in content
        assert f'href="/recordings/{rec_high.pk}/transcript/?page=2#segment-3"' in content

    def test_last_boundary_ordinal_enters_next_page(self, monkeypatch, client, tmp_path):
        _tiny_transcripts(monkeypatch, tmp_path, seg_per_page=1)
        rec, _t = self._link_corpus("link-bound-edge", ["a tail", "b tail", "marker three tail"])
        content = _page(client, "/recordings/?q=marker")
        assert f'href="/recordings/{rec.pk}/transcript/?page=3#segment-2"' in content

    def test_link_target_page_actually_carries_the_anchor(self, monkeypatch, client, tmp_path):
        _tiny_transcripts(monkeypatch, tmp_path, seg_per_page=2)
        rec, _t = self._link_corpus(
            "link-anchor", ["plain tail", "plain two", "plain three", "marker three tail"]
        )
        content = _page(client, "/recordings/?q=marker")
        href = f'href="/recordings/{rec.pk}/transcript/?page=2#segment-3"'
        assert href in content
        # Fragments are browser-side: fetch the path+query WITHOUT the
        # fragment and prove the target page renders the anchor id.
        path_and_query = href[len('href="') : -1].split("#")[0]
        target = _page(client, path_and_query)
        assert 'id="segment-3"' in target
        assert 'id="segment-1"' not in target  # page 1 anchors stay on page 1

    def test_page_one_anchor_present(self, client):
        rec, _t = self._link_corpus("link-anchor1", ["plain tail", "marker one tail"])
        content = _page(client, "/recordings/?q=marker")
        assert f'href="/recordings/{rec.pk}/transcript/?page=1#segment-1"' in content
        target = _page(client, f"/recordings/{rec.pk}/transcript/")
        assert 'id="segment-1"' in target

    def test_non_segment_provenance_never_links(self, client):
        rec, transcript, section = make_transcribed_recording(
            ["nothing matches this word"], sha="link-sum"
        )
        make_summary_version(rec, transcript, section, title="zebra photos unique")
        si.rebuild_index()
        content = _page(client, "/recordings/?q=zebra")
        assert '<span class="match-chip match-summary"' in content
        assert '<a class="match-chip' not in content

    def test_stale_provenance_pairs_keep_plain_chip(self, monkeypatch, client):
        rec_ok, t_ok = self._link_corpus("link-stale-a", ["marker alpha tail"])
        rec_bad, _t_bad = self._link_corpus("link-stale-b", ["marker beta tail"])
        _fake_engine(
            monkeypatch,
            [
                _segment_result(rec_ok, t_ok),
                # rec_bad's row claims the transcript of rec_ok — an
                # active transcript but belonging to ANOTHER recording.
                _segment_result(rec_bad, t_ok),
            ],
        )
        content = _page(client, "/recordings/?q=marker")
        assert f'href="/recordings/{rec_ok.pk}/transcript/?page=1#segment-3"' in content
        assert f'href="/recordings/{rec_bad.pk}/transcript/?page=' not in content
        assert content.count('<span class="match-chip match-segment"') == 1

    def test_inactive_transcript_keeps_plain_chip(self, monkeypatch, client):
        """The narrow post-commit race: the registry still carries the
        segment document while the transcript has already been
        superseded. The gate is stubbed (the registry is deliberately
        stale here); the REAL engine answers from the registry and the
        link validation must fall back to the plain chip."""
        from workflow.models import Transcript

        rec, transcript = self._link_corpus("link-inact", ["marker alpha tail"])
        Transcript.objects.filter(pk=transcript.pk).update(is_active=False)
        monkeypatch.setattr(
            search_web.search_query, "preflight_full_health", lambda *a, **k: None
        )
        content = _page(client, "/recordings/?q=marker")
        assert f'href="/recordings/{rec.pk}/transcript/?page=' not in content
        assert '<a class="match-chip' not in content
        assert '<span class="match-chip match-segment"' in content

    @pytest.mark.parametrize(
        "transcript_id", ["not-a-number", None, True, -4, 0, 2.5, "", "5x", [5]])
    def test_malformed_transcript_ids_keep_plain_chip(self, monkeypatch, client, transcript_id):
        rec, transcript = self._link_corpus("link-badid", ["marker alpha tail"])
        _fake_engine(
            monkeypatch,
            [_segment_result(rec, transcript, transcript_id=transcript_id)],
        )
        content = _page(client, "/recordings/?q=marker")
        assert '<a class="match-chip' not in content
        assert '<span class="match-chip match-segment"' in content

    @pytest.mark.parametrize("ordinal", [None, "3", True, -1, 1.5])
    def test_malformed_ordinals_keep_plain_chip(self, monkeypatch, client, ordinal):
        rec, transcript = self._link_corpus("link-badord", ["marker alpha tail"])
        _fake_engine(monkeypatch, [_segment_result(rec, transcript, ordinal=ordinal)])
        content = _page(client, "/recordings/?q=marker")
        assert '<a class="match-chip' not in content
        assert '<span class="match-chip match-segment"' in content

    def test_clean_transcript_pk_acceptance(self):
        assert search_web._clean_transcript_pk(5) == 5
        assert search_web._clean_transcript_pk("5") == 5
        for bad in (None, True, False, 0, -3, 2.5, "abc", "", "5x", [5], {"pk": 5}):
            assert search_web._clean_transcript_pk(bad) is None, bad

    def test_clean_ordinal_acceptance(self):
        assert search_web._clean_ordinal(0) == 0
        assert search_web._clean_ordinal(12) == 12
        for bad in (None, True, False, -1, "2", 2.5, [2]):
            assert search_web._clean_ordinal(bad) is None, bad

    def test_link_validation_is_one_batch_query_not_per_row(self, client):
        made = []
        for index in range(4):
            extra = " lonemarker" if index == 0 else ""
            made.append(
                make_transcribed_recording(
                    [f"marker shared tail {index}{extra}"], sha=f"linkcost-{index}"
                )[0]
            )
        si.rebuild_index()

        def request_count(query):
            with CaptureQueriesContext(connection) as ctx:
                response = client.get(f"/recordings/?q={query}")
            assert response.status_code == 200
            return len(ctx.captured_queries)

        many = request_count("marker")
        content = _page(client, "/recordings/?q=marker")
        assert "4 search results" in content
        one = request_count("lonemarker")
        content = _page(client, "/recordings/?q=lonemarker")
        assert "1 search result for" in content
        assert one == many  # the pair-validation SELECT is one bounded query

    def test_link_page_get_is_strictly_read_only(self, monkeypatch, client):
        self._link_corpus("linkpure-1", ["marker alpha tail"])

        def forbidden(*args, **kwargs):
            raise AssertionError("mutating entry point used during a GET")

        from workflow.services import pipeline_lock, search_sync

        monkeypatch.setattr(search_sync, "schedule_recording_sync", forbidden)
        monkeypatch.setattr(si, "rebuild_index", forbidden)
        monkeypatch.setattr(pipeline_lock, "pipeline_lock", forbidden)

        with CaptureQueriesContext(connection) as ctx:
            response = client.get("/recordings/?q=marker")
        assert response.status_code == 200
        assert '<a class="match-chip match-segment"' in response.content.decode()
        for query in ctx.captured_queries:
            verb = query["sql"].lstrip().split(None, 1)[0].upper()
            assert verb in ("SELECT", "PRAGMA"), query["sql"]


# ---------------------------------------------------------------------------
# Step 5A.4.2b — Card/Table parity, presentation, accessibility, CSP
# ---------------------------------------------------------------------------


class TestParityAndAccessibility:
    def _corpus(self, client=None):
        _healthy_corpus(("par-1", "quarterly budget review happenings budget end"))

    def _marks(self, content):
        return re.findall(r"<mark>[^<]*</mark>", content)

    def _seg_links(self, content):
        return re.findall(r'<a class="match-chip match-segment" href="([^"]*)"', content)

    def test_card_and_table_show_identical_highlights_and_links(self, client):
        rec = _healthy_corpus(("par-2", "quarterly budget review happenings"))[0]
        card = _page(client, "/recordings/?q=budget&view=cards")
        table = _page(client, "/recordings/?q=budget&view=table")
        assert self._marks(card) == self._marks(table)
        assert self._marks(card) == ["<mark>budget</mark>"]
        assert self._seg_links(card) == self._seg_links(table)
        assert self._seg_links(card) == [f"/recordings/{rec.pk}/transcript/?page=1#segment-0"]

    def test_search_table_cells_keep_data_labels(self, client):
        _healthy_corpus(("par-3", "budget review"))
        content = _page(client, "/recordings/?q=budget&view=table")
        assert 'data-label="Date &amp; time"' in content
        assert 'data-label="Title"' in content
        assert 'data-label="Match"' in content

    def test_search_table_date_cell_keeps_the_label_prefix(self, client):
        """The normal-Library date-column change is scoped to the NORMAL
        Library table only: search-result table rows keep the historical
        'Recorded <timestamp>' / 'Discovered <timestamp>' label."""
        rec = _seed(
            ["budget review"], "par-label", recorded_at=datetime(2026, 1, 2, 10, 0, tzinfo=TZ)
        )
        si.rebuild_index()
        content = _page(client, "/recordings/?q=budget&view=table")
        assert f"Recorded 2026-01-02 10:00" in content

    def test_ok_result_page_uses_one_status_region_and_no_live_rows(self, client):
        _healthy_corpus(("a11y-1", "budget review"))
        content = _page(client, "/recordings/?q=budget")
        assert 'role="status"' in content
        # Exactly ONE live region on a successful page: the reusable
        # page-frame message container. The results region itself is
        # static (role=status), so nothing spams announcements per row.
        assert content.count("aria-live") == 1
        assert 'aria-live="polite"' in content
        assert "aria-live" not in content[content.index('role="status"') :]
        # The segment chip stays a keyboard-reachable link with label text.
        assert re.search(
            r'<a class="match-chip match-segment" href="[^"]*"[^>]*>segment · ', content
        )

    def test_error_pages_keep_polite_live_region(self, client):
        _healthy_corpus(("a11y-2", "budget review"))
        content = _page(client, "/recordings/?q=" + "q" * 400)
        assert 'aria-live="polite"' in content

    def test_csp_header_unchanged_on_search_pages(self, client):
        from workflow.middleware import CSP

        _healthy_corpus(("csp-1", "budget review"))
        for url in ("/recordings/?q=budget", "/recordings/?q=" + "q" * 400):
            response = client.get(url)
            assert response.status_code == 200
            assert response.headers["Content-Security-Policy"] == CSP

    def test_external_stylesheets_carry_the_highlight_styling(self):
        from pathlib import Path

        css_path = (
            Path(search_web.__file__).resolve().parent.parent.parent
            / "static"
            / "workflow"
            / "base.css"
        )
        css = css_path.read_text()
        assert "mark {" in css  # semantic highlight styled externally
        assert ".transcript-segment:target" in css  # jump-link landing state
        assert "a.match-chip" in css  # chip links look/act like chips
        assert "a:focus-visible" in css  # keyboard focus stays global
        # No inline event handlers or styles ride along anywhere.
        html_root = Path(search_web.__file__).resolve().parent.parent.parent / "templates"
        for name in ("workflow/_search_snippet.html", "workflow/_search_provenance.html"):
            source = (html_root / name).read_text()
            assert "onclick" not in source and "style=" not in source


# ---------------------------------------------------------------------------
# Step 5A.4.2b review — SafeString hardening (exact built-in str only)
# ---------------------------------------------------------------------------


class _HostileStr(str):
    """str subclass whose overridden hooks must NEVER run: ``str(x)``
    would call ``__str__`` and an iterable copy (e.g. ``"".join``) would
    call ``__iter__``; the unbound base-str conversion runs neither."""

    def __iter__(self):
        raise AssertionError("__iter__ must not run")

    def __str__(self):
        raise AssertionError("__str__ must not run")


class TestSafeStringHardening:
    PAYLOAD = "<img src=x onerror=alert(1)>"

    def _payloads(self):
        from django.utils.safestring import mark_safe

        return {"trusted-wrapper": mark_safe(self.PAYLOAD), "hostile": _HostileStr(self.PAYLOAD)}

    @pytest.mark.parametrize(
        "matches",
        [
            [],
            None,
            ["junk", {"start": "a", "end": 2}, {"start": True, "end": 5},
             {"start": 9, "end": 3}],
            [{"start": 0, "end": 4}],
        ],
    )
    def test_safe_string_never_survives_into_a_fragment(self, matches):
        """str(SafeString) returns the SAME trusted object and a hostile
        __iter__/__str__ would raise — the fragment builder must emit
        EXACT built-in str without running ANY subclass hook."""
        from django.utils.safestring import SafeData, SafeString

        for text in self._payloads().values():
            fragments = search_web.snippet_fragments({"text": text, "matches": matches})
            assert fragments
            for fragment in fragments:
                assert type(fragment.text) is str  # exact built-in str
                assert not isinstance(fragment.text, SafeData)
                assert not isinstance(fragment.text, SafeString)
            assert "".join(f.text for f in fragments) == self.PAYLOAD

    def test_plain_str_input_still_works(self):
        fragments = search_web.snippet_fragments({"text": "abc", "matches": []})
        assert type(fragments[0].text) is str

    def _render(self, monkeypatch, client, matches, text=None, sha="safestr-1"):
        from django.utils.safestring import mark_safe

        if text is None:
            text = mark_safe(self.PAYLOAD)
        rec, _t, _s = make_transcribed_recording(
            ["rendered corpus marker"], sha=sha
        )
        si.rebuild_index()  # a healthy index for the gate
        _fake_engine(
            monkeypatch,
            [
                {
                    "rank": 1,
                    "recording_id": str(rec.pk),
                    "match": {"source": "segment", "start_ms": 4200},
                    "snippet": {"text": text, "matches": matches},
                }
            ],
        )
        content = _page(client, "/recordings/?q=rendered")
        assert f'href="/recordings/{rec.pk}/"' in content  # the row rendered
        return content

    def test_no_range_payload_renders_escaped(self, monkeypatch, client):
        for content in (
            self._render(monkeypatch, client, []),
            self._render(monkeypatch, client, [], text=_HostileStr(self.PAYLOAD),
                         sha="safestr-2"),
        ):
            assert "&lt;img src=x onerror=alert(1)&gt;" in content
            assert "<img" not in content
            stripped = content.replace("&lt;img src=x onerror=alert(1)&gt;", "")
            assert "onerror=alert" not in stripped  # never an executable attribute

    def test_none_matches_payload_renders_escaped(self, monkeypatch, client):
        content = self._render(monkeypatch, client, None)
        assert "&lt;img src=x onerror=alert(1)&gt;" in content
        assert "<img" not in content
        stripped = content.replace("&lt;img src=x onerror=alert(1)&gt;", "")
        assert "onerror=alert" not in stripped

    def test_malformed_ranges_payload_renders_escaped(self, monkeypatch, client):
        content = self._render(
            monkeypatch, client, ["junk", {"start": 7, "end": "b"}, {"start": 20, "end": 3}]
        )
        assert "&lt;img src=x onerror=alert(1)&gt;" in content
        assert "<img" not in content
        stripped = content.replace("&lt;img src=x onerror=alert(1)&gt;", "")
        assert "onerror=alert" not in stripped

    def test_valid_range_marks_still_render_escaped(self, monkeypatch, client):
        for content in (
            self._render(monkeypatch, client, [{"start": 0, "end": 4}]),
            self._render(monkeypatch, client, [{"start": 0, "end": 4}],
                         text=_HostileStr(self.PAYLOAD), sha="safestr-4"),
        ):
            assert "<mark>&lt;img</mark>" in content  # <mark> wraps ESCAPED text
            assert "<mark><img" not in content
            assert "<img" not in content
            stripped = content.replace("<mark>&lt;img</mark>", "")
            stripped = stripped.replace("src=x onerror=alert(1)&gt;", "")
            assert "onerror=alert" not in stripped
