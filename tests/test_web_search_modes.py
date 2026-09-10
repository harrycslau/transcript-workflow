"""Web Keyword/Semantic/Hybrid modes (Step 5C — Task 6).

Covers the approved contract: the keyword Library search stays an
unchanged GET at ``/recordings/?q=...``; the dedicated
``/recordings/search/`` endpoint is POST-only (GET is a 405 with no
config/health/network/DB work) and CSRF-protected; semantic/hybrid
delegate to their services with exactly one health sweep, one integrity
traversal and one localhost embedding request (zero for an empty scope)
and no keyword preflight; invalid filters reject BEFORE health/network
instead of widening; the same Recording scope applies before both
rankings; the query never enters a URL, redirect, log or error; result
navigation (pagination/sort/filter/view) is POST-only with hidden
server-validated state; card/table rendering, snippets, provenance and
segment-link validation are shared with keyword search. All network is
mocked; no real HTTP.
"""

from __future__ import annotations

import re

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from brainlib.config import EmbeddingConfig
from factories import (
    default_web,
    make_config,
    make_tag,
    make_tag_assignment,
    make_transcribed_recording,
)
from workflow.models import EmbeddingDocument, EmbeddingGeneration, EmbeddingGenerationState
from workflow.services import embedding_index as ei
from workflow.services import search_fusion as sf
from workflow.services import search_index as si
from workflow.services import search_query as keyword_query
from workflow.services import search_web
from workflow.services import semantic_query as sq
from workflow.services.embedding_client import EmbeddingBatch, EmbeddingHTTPError

# transaction=True: the semantic/hybrid engines refuse to run inside a
# SQLite transaction, and pytest-django's default outer transaction would
# otherwise trip that fixed precondition.
pytestmark = pytest.mark.django_db(transaction=True)

DIM = 4
ENDPOINT = "/recordings/search/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unmatched_vector(dim=DIM):
    return [0.0] + [0.01] * (dim - 1)


def keyword_embedder(keywords, dim=DIM, *, tracker=None, fail_calls=(), guard_no_txn=False):
    """Deterministic fake embedder: a text containing keyword ``k`` gets
    1.0 on axis ``idx(k)``; the query text is dispatched identically."""
    state = {"calls": 0}

    def embed(config, texts):
        state["calls"] += 1
        if tracker is not None:
            tracker.append(list(texts))
        if guard_no_txn:
            assert connection.in_atomic_block is False, "network inside a transaction"
        if state["calls"] in fail_calls:
            raise EmbeddingHTTPError(503)
        out = []
        for text in texts:
            vec = _unmatched_vector(dim)
            for idx, keyword in enumerate(keywords):
                if keyword in text:
                    vec[idx] = 1.0
            out.append(EmbeddingBatch(text=text, embedding=tuple(vec)))
        return out

    embed.state = state
    return embed


def emb_config(tmp_path, *, per_page=25, model="test-embed-model"):
    return make_config(
        tmp_path,
        web=default_web(recordings_per_page=per_page),
        embedding=EmbeddingConfig(
            base_url="http://127.0.0.1:1/v1",
            model=model,
            api_key_env="BRAIN_TEST_LLM_API_KEY",
            timeout_seconds=120,
            batch_size=32,
        ),
    )


def build_healthy(tmp_path, monkeypatch, *, per_page=25, keywords=("alpha", "beta")):
    """Two transcribed recordings plus a healthy active embedding
    generation (dim 4, keyword-dispatch embedder)."""
    make_transcribed_recording(["alpha meeting discussion"], sha="wv-0")
    make_transcribed_recording(["beta irrelevant chatter"], sha="wv-1")
    si.rebuild_index()
    config = emb_config(tmp_path, per_page=per_page)
    ei.rebuild_embedding_index(config, embedder=keyword_embedder(list(keywords)))
    monkeypatch.setattr("workflow.views.recordings.get_config", lambda: config)
    return config


def build_empty_embedding(tmp_path, monkeypatch, *, per_page=25):
    """Healthy but EMPTY corpus + empty active embedding generation."""
    si.rebuild_index()
    config = emb_config(tmp_path, per_page=per_page)
    ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha", "beta"]))
    monkeypatch.setattr("workflow.views.recordings.get_config", lambda: config)
    return config


def active_generation():
    return EmbeddingGeneration.objects.get(state=EmbeddingGenerationState.ACTIVE)


def active_row_count():
    return EmbeddingDocument.objects.filter(generation=active_generation()).count()


def post(client, data):
    response = client.post(ENDPOINT, data)
    assert response.status_code == 200, response.status_code
    return response.content.decode()


def _counting(monkeypatch, module, name):
    calls = []
    real = getattr(module, name)

    def counting(*args, **kwargs):
        calls.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, counting)
    return calls


def _counting_decode(monkeypatch):
    state = {"rows": 0}
    real = sq._classify_active_page

    def counting(page, dimensions, using):
        state["rows"] += len(page)
        return real(page, dimensions, using)

    monkeypatch.setattr(sq, "_classify_active_page", counting)
    return state


def _forbid_embedding(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the embedding endpoint must not be called")

    monkeypatch.setattr("workflow.services.embedding_client.embed_texts", forbidden)


def _forbid_health(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the source health sweep must not run")

    monkeypatch.setattr(si, "build_status_report", forbidden)


def _patch_query_embedder(monkeypatch, embedder):
    monkeypatch.setattr("workflow.services.embedding_client.embed_texts", embedder)


def _assert_read_only(ctx):
    for query in ctx.captured_queries:
        verb = query["sql"].lstrip().split(None, 1)[0].upper()
        assert verb in ("SELECT", "PRAGMA"), query["sql"]


# ---------------------------------------------------------------------------
# 1. Keyword GET parity / forged mode / endpoint GET=405
# ---------------------------------------------------------------------------


class TestGetPurityAndEndpoint:
    def test_forged_mode_on_get_stays_keyword_and_never_embeds(
        self, tmp_path, monkeypatch
    ):
        build_healthy(tmp_path, monkeypatch)

        def forbidden(*args, **kwargs):
            raise AssertionError("a GET must never embed or run a vector search")

        _forbid_embedding(monkeypatch)
        monkeypatch.setattr(sq, "semantic_search", forbidden)
        monkeypatch.setattr(sf, "hybrid_search", forbidden)

        for url in (
            "/recordings/",
            "/recordings/?q=alpha",
            "/recordings/?q=alpha&mode=semantic",
            "/recordings/?q=alpha&mode=hybrid",
        ):
            with CaptureQueriesContext(connection) as ctx:
                response = Client().get(url)
            assert response.status_code == 200
            content = response.content.decode()
            assert "semantic search result" not in content
            assert "hybrid search result" not in content
            if "q=alpha" in url:
                assert "search result" in content  # ordinary keyword result
            _assert_read_only(ctx)

    def test_endpoint_get_is_405_without_any_work(self, tmp_path, monkeypatch):
        build_healthy(tmp_path, monkeypatch)
        _forbid_embedding(monkeypatch)
        _forbid_health(monkeypatch)
        with CaptureQueriesContext(connection) as ctx:
            response = Client().get(ENDPOINT)
        assert response.status_code == 405
        assert response.headers.get("Allow") == "POST"
        assert ctx.captured_queries == []

    def test_keyword_get_parity_with_existing_contract(self, tmp_path, monkeypatch):
        build_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        content = Client().get("/recordings/?q=alpha").content.decode()
        assert "search result" in content
        assert "semantic search result" not in content
        # Keyword pagination stays a GET link (unchanged behavior).
        assert "match-chip" in content


# ---------------------------------------------------------------------------
# 2. CSRF + invalid mode/query states
# ---------------------------------------------------------------------------


class TestValidationAndCsrf:
    def test_csrf_enforced_on_post(self, tmp_path, monkeypatch):
        build_healthy(tmp_path, monkeypatch)
        client = Client(enforce_csrf_checks=True)
        response = client.post(ENDPOINT, {"mode": "semantic", "q": "alpha"})
        assert response.status_code == 403

    def test_form_carries_a_csrf_token(self, tmp_path, monkeypatch):
        build_healthy(tmp_path, monkeypatch)
        content = Client().get("/recordings/").content.decode()
        assert 'name="csrfmiddlewaretoken"' in content

    def test_invalid_mode_is_a_friendly_fixed_state_without_network(
        self, tmp_path, monkeypatch
    ):
        build_healthy(tmp_path, monkeypatch)
        _forbid_embedding(monkeypatch)
        _forbid_health(monkeypatch)
        content = post(Client(), {"mode": "bogus", "q": "alpha"})
        assert "Choose a valid search mode" in content
        assert "semantic search result" not in content
        assert "hybrid search result" not in content

    def test_missing_mode_is_invalid_not_silent_keyword(self, tmp_path, monkeypatch):
        build_healthy(tmp_path, monkeypatch)
        _forbid_embedding(monkeypatch)
        _forbid_health(monkeypatch)
        content = post(Client(), {"q": "alpha"})
        assert "Choose a valid search mode" in content
        assert "search result" not in content

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_empty_query_is_invalid_and_skips_health(self, tmp_path, monkeypatch, mode):
        build_healthy(tmp_path, monkeypatch)
        _forbid_embedding(monkeypatch)
        _forbid_health(monkeypatch)
        content = post(Client(), {"mode": mode, "q": "   "})
        assert "must not be empty" in content

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_over_long_query_clears_the_canary(self, tmp_path, monkeypatch, mode):
        build_healthy(tmp_path, monkeypatch)
        _forbid_embedding(monkeypatch)
        _forbid_health(monkeypatch)
        canary = "PRIVCANARY" + "x" * 300
        content = post(Client(), {"mode": mode, "q": canary})
        assert canary not in content
        assert "PRIVCANARY" not in content
        assert "at most" in content


# ---------------------------------------------------------------------------
# 3. Success / zero / unavailable states
# ---------------------------------------------------------------------------


class TestOutcomes:
    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_success_renders_rows_and_echoes_query_autoescaped(
        self, tmp_path, monkeypatch, mode
    ):
        build_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        content = post(Client(), {"mode": mode, "q": "alpha"})
        assert f"{mode} search result" in content
        assert "alpha" in content
        # The query is never encoded into a URL.
        assert "?q=alpha" not in content
        assert "q=alpha" not in content
        assert 'href="/recordings/search/' not in content

    def test_semantic_xss_query_echoed_only_escaped(self, tmp_path, monkeypatch):
        build_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        content = post(Client(), {"mode": "semantic", "q": "<em>alpha</em>"})
        assert "<em>" not in content
        assert "&lt;em&gt;" in content

    def test_zero_results_from_an_empty_scope_makes_zero_embeds(
        self, tmp_path, monkeypatch
    ):
        build_healthy(tmp_path, monkeypatch)
        _forbid_embedding(monkeypatch)
        content = post(
            Client(), {"mode": "semantic", "q": "alpha", "tag": "NoSuchTag"}
        )
        assert "No semantic search results for" in content
        assert "recording-card" not in content

    def test_zero_results_empty_corpus(self, tmp_path, monkeypatch):
        build_empty_embedding(tmp_path, monkeypatch)
        _forbid_embedding(monkeypatch)
        content = post(Client(), {"mode": "semantic", "q": "alpha"})
        assert "No semantic search results for" in content

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_source_index_unavailable_is_a_friendly_state(
        self, tmp_path, monkeypatch, mode
    ):
        build_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        from workflow.models import Recording, TranscriptSegment

        rec = Recording.objects.get(sha256="wv-0")
        TranscriptSegment.objects.filter(transcript__recording=rec, ordinal=0).update(
            text="edited secret content"
        )
        content = post(Client(), {"mode": mode, "q": "alpha"})
        assert "not healthy" in content
        assert "edited secret content" not in content
        assert "search result" not in content

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_no_active_generation_is_a_friendly_state(
        self, tmp_path, monkeypatch, mode
    ):
        build_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        EmbeddingGeneration.objects.all().delete()
        content = post(Client(), {"mode": mode, "q": "alpha"})
        assert "no active embedding generation" in content
        assert "search result" not in content

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_endpoint_error_is_sanitized(self, tmp_path, monkeypatch, mode):
        build_healthy(tmp_path, monkeypatch)
        canary = "SECRETQUERYCANARY"

        def failing(config, texts):
            raise EmbeddingHTTPError(503)

        _patch_query_embedder(monkeypatch, failing)
        content = post(Client(), {"mode": mode, "q": canary})
        assert "embedding request failed" in content
        assert canary not in content
        assert "SECRETQUERYCANARY" not in content

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_concurrent_change_is_a_friendly_state(
        self, tmp_path, monkeypatch, mode
    ):
        build_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        calls = {"n": 0}
        real = sq._pragma_data_version

        def flaky(using):
            calls["n"] += 1
            if calls["n"] < 3:
                return real(using)
            return real(using) + 1

        monkeypatch.setattr(sq, "_pragma_data_version", flaky)
        content = post(Client(), {"mode": mode, "q": "alpha"})
        assert "changed during" in content
        assert "search result" not in content


# ---------------------------------------------------------------------------
# 4. Exact one health / integrity / embed; no keyword preflight
# ---------------------------------------------------------------------------


class TestOrchestrationCounts:
    def test_semantic_exactly_one_health_integrity_embed(self, tmp_path, monkeypatch):
        build_healthy(tmp_path, monkeypatch)
        health = _counting(monkeypatch, si, "build_status_report")
        decoded = _counting_decode(monkeypatch)
        tracker = []
        _patch_query_embedder(
            monkeypatch, keyword_embedder(["alpha", "beta"], tracker=tracker)
        )
        post(Client(), {"mode": "semantic", "q": "alpha"})
        assert len(health) == 1
        assert len(tracker) == 1
        assert tracker[0] == ["alpha"]
        assert decoded["rows"] == active_row_count()

    def test_hybrid_exactly_one_one_one_and_no_keyword_preflight(
        self, tmp_path, monkeypatch
    ):
        build_healthy(tmp_path, monkeypatch)
        health = _counting(monkeypatch, si, "build_status_report")
        decoded = _counting_decode(monkeypatch)
        keyword_calls = _counting(monkeypatch, sf, "search_recordings")

        def forbidden(*args, **kwargs):
            raise AssertionError("hybrid must not call the keyword preflight")

        monkeypatch.setattr(keyword_query, "preflight_full_health", forbidden)
        tracker = []
        _patch_query_embedder(
            monkeypatch, keyword_embedder(["alpha", "beta"], tracker=tracker)
        )
        post(Client(), {"mode": "hybrid", "q": "alpha"})
        assert len(health) == 1
        assert len(tracker) == 1
        assert tracker[0] == ["alpha"]
        assert len(keyword_calls) == 1
        assert decoded["rows"] == active_row_count()

    def test_semantic_never_calls_keyword_preflight(self, tmp_path, monkeypatch):
        build_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))

        def forbidden(*args, **kwargs):
            raise AssertionError("semantic must not run the keyword preflight")

        monkeypatch.setattr(keyword_query, "preflight_full_health", forbidden)
        post(Client(), {"mode": "semantic", "q": "alpha"})


# ---------------------------------------------------------------------------
# 5. Invalid filters reject before health/network; scope applies
# ---------------------------------------------------------------------------


class TestScoping:
    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_invalid_filters_reject_before_health_and_network(
        self, tmp_path, monkeypatch, mode
    ):
        build_healthy(tmp_path, monkeypatch)
        _forbid_embedding(monkeypatch)
        _forbid_health(monkeypatch)
        content = post(Client(), {"mode": mode, "q": "alpha", "from": "notadate"})
        assert search_web.INVALID_VECTOR_FILTERS_MESSAGE in content
        assert "Search ran without the invalid filters" not in content

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_tag_scope_applies_before_both_rankings(self, tmp_path, monkeypatch, mode):
        make_transcribed_recording(["alpha meeting discussion"], sha="wv-0")
        make_transcribed_recording(["beta irrelevant chatter"], sha="wv-1")
        from workflow.models import Recording

        rec_in = Recording.objects.get(sha256="wv-0")
        rec_out = Recording.objects.get(sha256="wv-1")
        tag = make_tag("Project", configured=True)
        make_tag_assignment(rec_in, tag)
        # Rebuild AFTER the tag assignment so the index is not stale.
        si.rebuild_index()
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha", "beta"]))
        monkeypatch.setattr("workflow.views.recordings.get_config", lambda: config)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        content = post(Client(), {"mode": mode, "q": "alpha", "tag": "Project"})
        assert f'href="/recordings/{rec_in.pk}/"' in content
        assert f'href="/recordings/{rec_out.pk}/"' not in content

    def test_invalid_sort_falls_back_keeping_valid_filters(
        self, tmp_path, monkeypatch
    ):
        build_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        content = post(
            Client(),
            {"mode": "semantic", "q": "alpha", "tag": "NoSuchTag", "sort": "zzzbogus"},
        )
        # The invalid sort did not poison the valid scope filter: the
        # (empty) tag scope still answers zero results, not unscoped rows.
        assert "No semantic search results for" in content
        assert "sort" in content.lower()


# ---------------------------------------------------------------------------
# 6. POST-only navigation, state preservation, no query in URLs
# ---------------------------------------------------------------------------


class TestPostNavigation:
    def _corpus(self, tmp_path, monkeypatch, per_page=2):
        make_transcribed_recording(["alpha one"], sha="nav-0")
        make_transcribed_recording(["alpha two"], sha="nav-1")
        make_transcribed_recording(["alpha three"], sha="nav-2")
        si.rebuild_index()
        config = emb_config(tmp_path, per_page=per_page)
        ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha"]))
        monkeypatch.setattr("workflow.views.recordings.get_config", lambda: config)
        return config

    def test_pagination_is_a_post_form_carrying_state(self, tmp_path, monkeypatch):
        self._corpus(tmp_path, monkeypatch, per_page=2)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha"]))
        content = post(Client(), {"mode": "semantic", "q": "alpha", "view": "cards"})
        assert "Page 1 of 2" in content
        pagination = re.search(
            r'<nav class="pagination".*?</nav>', content, re.DOTALL
        )
        assert pagination is not None
        block = pagination.group(0)
        assert f'action="{ENDPOINT}"' in block
        assert 'method="post"' in block
        assert 'name="csrfmiddlewaretoken"' in block
        assert 'name="mode" value="semantic"' in block
        assert 'name="q" value="alpha"' in block
        assert 'name="view" value="cards"' in block
        assert 'name="page" value="2"' in block
        # No GET link can trigger or claim a vector search.
        assert 'href="?q=' not in content
        assert 'href="/recordings/search/' not in content

    def test_page_two_post_returns_the_next_window(self, tmp_path, monkeypatch):
        self._corpus(tmp_path, monkeypatch, per_page=2)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha"]))
        content = post(
            Client(), {"mode": "semantic", "q": "alpha", "page": "2", "view": "cards"}
        )
        assert "Page 2 of 2" in content

    def test_sort_and_view_are_submitted_by_post(self, tmp_path, monkeypatch):
        self._corpus(tmp_path, monkeypatch, per_page=2)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha"]))
        content = post(
            Client(),
            {"mode": "semantic", "q": "alpha", "sort": "title_az", "view": "table"},
        )
        assert 'data-label="Match"' in content  # table view rendered
        # The vector filter form is a POST carrying mode/query.
        assert f'action="{ENDPOINT}"' in content
        assert 'name="mode" value="semantic"' in content
        assert 'name="q" value="alpha"' in content

    def test_clear_search_returns_to_plain_get_library(self, tmp_path, monkeypatch):
        self._corpus(tmp_path, monkeypatch, per_page=2)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha"]))
        content = post(Client(), {"mode": "semantic", "q": "alpha"})
        clear = re.search(r'href="([^"]*)">Clear search<', content)
        assert clear is not None
        assert "q=" not in clear.group(1)
        assert clear.group(1).startswith("/recordings/?")


# ---------------------------------------------------------------------------
# 7. Rendering parity: snippets, marks, provenance, segment links
# ---------------------------------------------------------------------------


class TestRenderingParity:
    def test_semantic_snippets_are_unmarked(self, tmp_path, monkeypatch):
        build_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        content = post(Client(), {"mode": "semantic", "q": "alpha"})
        assert "search-snippet" in content
        assert "<mark>" not in content

    def test_hybrid_prefers_keyword_highlights(self, tmp_path, monkeypatch):
        build_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        content = post(Client(), {"mode": "hybrid", "q": "alpha"})
        assert "<mark>alpha</mark>" in content
        # Evidence chip carries only integer component ranks.
        assert "kw #" in content or "sem #" in content

    def test_card_and_table_parity_for_semantic(self, tmp_path, monkeypatch):
        build_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        cards = post(Client(), {"mode": "semantic", "q": "alpha", "view": "cards"})
        table = post(Client(), {"mode": "semantic", "q": "alpha", "view": "table"})
        assert cards.count("match-chip") == table.count("match-chip")
        assert cards.count("search-snippet") == table.count("search-snippet")

    def test_semantic_segment_provenance_links_when_validated(
        self, tmp_path, monkeypatch
    ):
        rec, _transcript, _section = make_transcribed_recording(
            ["alpha marker tail"], sha="wv-link"
        )
        si.rebuild_index()
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha"]))
        monkeypatch.setattr("workflow.views.recordings.get_config", lambda: config)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha"]))
        content = post(Client(), {"mode": "semantic", "q": "alpha"})
        assert f'href="/recordings/{rec.pk}/transcript/?page=1#segment-0"' in content

    def test_hybrid_evidence_label_is_plain_text(self, tmp_path, monkeypatch):
        build_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        content = post(Client(), {"mode": "hybrid", "q": "alpha"})
        chips = re.findall(r'<span class="evidence-chip">([^<]*)</span>', content)
        assert chips
        for chip in chips:
            assert re.fullmatch(r"(kw #\d+)?( · )?(sem #\d+)?", chip)


# ---------------------------------------------------------------------------
# 8. Read-only DB, no lock/rebuild/sync, bounded queries, privacy
# ---------------------------------------------------------------------------


class TestPurityAndPrivacy:
    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_vector_post_is_db_read_only_and_never_locks_or_writes(
        self, tmp_path, monkeypatch, mode
    ):
        build_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))

        def forbidden(*args, **kwargs):
            raise AssertionError("a vector POST must stay read-only")

        from workflow.services import pipeline_lock, search_sync

        monkeypatch.setattr(search_sync, "schedule_recording_sync", forbidden)
        monkeypatch.setattr(search_sync, "reconcile_recording", forbidden)
        monkeypatch.setattr(si, "rebuild_index", forbidden)
        monkeypatch.setattr(ei, "rebuild_embedding_index", forbidden)
        monkeypatch.setattr(ei, "repair_embedding_index", forbidden)
        monkeypatch.setattr(pipeline_lock, "pipeline_lock", forbidden)

        with CaptureQueriesContext(connection) as ctx:
            content = post(Client(), {"mode": mode, "q": "alpha"})
        assert "search result" in content
        _assert_read_only(ctx)

    def test_card_fetch_is_batch_not_per_result(self, tmp_path, monkeypatch):
        make_transcribed_recording(["alpha one"], sha="nq-0")
        si.rebuild_index()
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha"]))
        monkeypatch.setattr("workflow.views.recordings.get_config", lambda: config)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha"]))

        def request_count():
            with CaptureQueriesContext(connection) as ctx:
                response = Client().post(ENDPOINT, {"mode": "semantic", "q": "alpha"})
            assert response.status_code == 200
            return len(ctx.captured_queries)

        one = request_count()
        for index in range(1, 4):
            make_transcribed_recording([f"alpha item {index}"], sha=f"nq-{index}")
        si.rebuild_index()
        ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha"]))
        many = request_count()
        assert one == many

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_query_never_logged(self, tmp_path, monkeypatch, caplog, mode):
        import logging

        caplog.set_level(logging.DEBUG, logger="workflow")
        build_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        canary = "PRIVLOGCANARY"
        content = post(Client(), {"mode": mode, "q": canary})
        # A successful response may echo the query (autoescaped); the
        # invariant is that it never reaches the logs.
        assert canary in content
        for record in caplog.get_records("call"):
            assert canary not in record.getMessage()
            assert canary not in (record.exc_text or "")
        assert canary not in caplog.text

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_unavailable_state_clears_the_query_and_logs_nothing(
        self, tmp_path, monkeypatch, caplog, mode
    ):
        import logging

        caplog.set_level(logging.DEBUG, logger="workflow")
        build_healthy(tmp_path, monkeypatch)
        canary = "PRIVCANARYUNAVAIL"

        def failing(config, texts):
            raise EmbeddingHTTPError(503)

        _patch_query_embedder(monkeypatch, failing)
        content = post(Client(), {"mode": mode, "q": canary})
        assert canary not in content
        assert canary not in caplog.text


# ---------------------------------------------------------------------------
# 9. Accessibility / no generated HTML
# ---------------------------------------------------------------------------


class TestAccessibilityAndSafety:
    def test_advanced_form_has_accessible_labels_and_roles(self, tmp_path, monkeypatch):
        build_healthy(tmp_path, monkeypatch)
        content = Client().get("/recordings/").content.decode()
        assert 'role="search"' in content
        assert 'for="id_vector_q"' in content
        assert 'for="id_vector_mode"' in content
        assert 'aria-label="Semantic or hybrid search query"' in content
        assert 'aria-label="Semantic or hybrid search mode"' in content
        assert "local embeddings" in content

    def test_vector_mode_view_toggle_uses_submit_buttons(self, tmp_path, monkeypatch):
        build_healthy(tmp_path, monkeypatch)
        _patch_query_embedder(monkeypatch, keyword_embedder(["alpha", "beta"]))
        content = post(Client(), {"mode": "semantic", "q": "alpha", "view": "cards"})
        assert '<button type="submit" name="view" value="table"' in content
        assert '<button type="submit" name="view" value="cards"' in content

    def test_no_safe_string_or_generated_html_in_sources(self):
        from pathlib import Path

        root = Path(search_web.__file__).resolve().parent.parent.parent
        files = [
            root / "workflow" / "services" / "search_web.py",
            root / "templates" / "workflow" / "recording_list.html",
            root / "templates" / "workflow" / "_pagination_post.html",
            root / "templates" / "workflow" / "_search_snippet.html",
            root / "templates" / "workflow" / "_search_provenance.html",
        ]
        for path in files:
            source = path.read_text()
            if path.suffix == ".html":
                assert "mark_safe" not in source
                assert "|safe" not in source
                assert "autoescape off" not in source
                assert "onclick" not in source
                assert "style=" not in source
            else:
                assert "mark_safe(" not in source
                assert "SafeString(" not in source
                assert "django.utils.safestring" not in source
                assert "format_html" not in source

    def test_fragment_builder_still_emits_exact_plain_str(self):
        from django.utils.safestring import SafeString

        fragments = search_web.snippet_fragments(
            {"text": SafeString("<b>x</b>"), "matches": [{"start": 3, "end": 4}]}
        )
        for fragment in fragments:
            assert type(fragment.text) is str
            assert not isinstance(fragment.text, SafeString)
