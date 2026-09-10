"""Web tests for the Step 5D Ask page.

GET renders the form with zero health/embedding/chat work and no writes;
POST on the same endpoint executes the read-only Ask with CSRF
protection. Invalid input skips health/network; operational failures
render one sanitized unavailable state and clear the submitted question;
insufficiency is a successful explicit state; every rendered value is
autoescaped plain data. All network is mocked.
"""

from __future__ import annotations

import json

import pytest
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.db import connection

from brainlib.config import EmbeddingConfig
from factories import make_config, make_summary_version, make_transcribed_recording
from workflow.models import Summary
from workflow.services import embedding_index as ei
from workflow.services import search_index as si
from workflow.services.embedding_client import EmbeddingBatch
from workflow.services.llm import LLMHTTPError

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("forbid_external_effects")]

DIM = 4


def ask_config(tmp_path):
    from brainlib.config import LLMConfig

    return make_config(
        tmp_path,
        embedding=EmbeddingConfig(
            base_url="http://127.0.0.1:1/v1",
            model="test-embed-model",
            api_key_env="BRAIN_TEST_LLM_API_KEY",
            timeout_seconds=120,
            batch_size=32,
        ),
        llm=LLMConfig(
            provider="openai_compatible",
            base_url="http://127.0.0.1:8000/v1",
            model="test-chat-model",
            api_key_env="BRAIN_TEST_LLM_API_KEY",
            temperature=0.2,
            timeout_seconds=600,
        ),
    )


def keyword_embedder(keywords, dim=DIM):
    def embed(config, texts):
        out = []
        for text in texts:
            vec = [0.0] + [0.01] * (dim - 1)
            for idx, keyword in enumerate(keywords):
                if keyword in text:
                    vec[idx] = 1.0
            out.append(EmbeddingBatch(text=text, embedding=tuple(vec)))
        return out

    return embed


def chat_json(answer, citations=(), *, insufficient=False, raise_exc=None, calls=None):
    def chat(config, *, system_prompt, user_prompt, temperature, max_tokens):
        if calls is not None:
            calls.append(True)
        if raise_exc is not None:
            raise raise_exc
        return json.dumps(
            {"answer": answer, "citations": list(citations), "insufficient": insufficient}
        )

    return chat


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def seed(tmp_path, monkeypatch):
    """Recording with segments + summary, healthy embedding index, and the
    Ask view's config/network seams patched to the test doubles."""

    def _seed(*, with_summary=True, chat=None, embedder=None):
        rec, t, s = make_transcribed_recording(
            ["alpha segment zero", "alpha segment one"], sha="webask-0"
        )
        if with_summary:
            make_summary_version(
                rec, t, s, title="alpha summary", overview="alpha overview"
            )
        si.rebuild_index()
        config = ask_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha"]))
        monkeypatch.setattr("workflow.views.ask.get_config", lambda: config)
        monkeypatch.setattr(
            "workflow.services.embedding_client.embed_texts",
            embedder or keyword_embedder(["alpha"]),
        )
        monkeypatch.setattr(
            "workflow.services.llm.chat_completion",
            chat or chat_json("Answer [C1].", ["C1"]),
        )
        return rec, t, s

    return _seed


class TestAskPageGet:
    def test_other_methods_return_405_before_any_work(self, client, seed, monkeypatch):
        seed()
        calls = {"health": 0}
        real_status = si.build_status_report
        monkeypatch.setattr(
            si,
            "build_status_report",
            lambda **k: (calls.__setitem__("health", calls["health"] + 1), real_status(**k))[1],
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("non GET/POST methods must not touch the network")

        monkeypatch.setattr("workflow.services.embedding_client.embed_texts", forbidden)
        monkeypatch.setattr("workflow.services.llm.chat_completion", forbidden)
        for method in ("put", "delete", "patch"):
            response = getattr(client, method)("/ask/", {"question": "alpha"})
            assert response.status_code == 405
        assert calls["health"] == 0

    def test_get_renders_form_zero_work_no_writes(self, client, seed, monkeypatch):
        seed()
        calls = {"health": 0, "embed": 0, "chat": 0}

        def forbidden(*args, **kwargs):
            raise AssertionError("GET must not do health/embedding/chat work")

        real_status = si.build_status_report
        monkeypatch.setattr(si, "build_status_report", lambda **k: (calls.__setitem__("health", calls["health"] + 1), real_status(**k))[1])
        monkeypatch.setattr(
            "workflow.services.embedding_client.embed_texts", forbidden
        )
        monkeypatch.setattr("workflow.services.llm.chat_completion", forbidden)
        with CaptureQueriesContext(connection) as ctx:
            response = client.get("/ask/")
        assert response.status_code == 200
        content = response.content.decode()
        assert 'name="question"' in content
        assert "method=\"post\"" in content
        assert calls["health"] == 0
        writes = [
            q["sql"]
            for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith(
                ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "REPLACE")
            )
        ]
        assert writes == []


class TestAskPagePost:
    def test_post_answer_with_citation_links(self, client, seed):
        rec, t, s = seed()
        response = client.post("/ask/", {"question": "alpha"})
        assert response.status_code == 200
        content = response.content.decode()
        summary = Summary.objects.get(recording=rec)
        summary_url = f"/recordings/{rec.pk}/summaries/{summary.pk}/"
        assert summary_url in content
        # The answer fragments render the plain text plus a server-owned
        # citation reference link (never a raw model URL).
        assert "Answer " in content
        assert f'href="{summary_url}">[C1]</a>' in content
        assert 'class="citation-ref"' in content

    def test_post_answer_is_autoescaped(self, client, seed):
        seed(
            chat=chat_json(
                "The answer is <script>alert(1)</script> and [C1].",
                ["C1"],
            )
        )
        response = client.post("/ask/", {"question": "alpha"})
        content = response.content.decode()
        assert "&lt;script&gt;" in content
        assert "<script>alert" not in content

    def test_post_invalid_skips_health_and_network(self, client, seed, monkeypatch):
        seed()
        calls = {"health": 0}
        real_status = si.build_status_report
        monkeypatch.setattr(
            si, "build_status_report", lambda **k: (calls.__setitem__("health", calls["health"] + 1), real_status(**k))[1]
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("invalid input must not touch network")

        monkeypatch.setattr("workflow.services.embedding_client.embed_texts", forbidden)
        monkeypatch.setattr("workflow.services.llm.chat_completion", forbidden)
        response = client.post("/ask/", {"question": "   "})
        assert response.status_code == 200
        content = response.content.decode()
        assert "must not be empty" in content
        assert calls["health"] == 0

    def test_post_operational_unavailable_clears_question(self, client, seed):
        seed(chat=chat_json("", [], raise_exc=LLMHTTPError(500)))
        canary = "SECRET-WEB-ASK-QUESTION"
        response = client.post("/ask/", {"question": canary})
        assert response.status_code == 200
        content = response.content.decode()
        assert "HTTP error" in content
        assert canary not in content
        # The rejected question is never echoed back into the input.
        assert 'value="' + canary + '"' not in content

    def test_post_insufficiency_is_explicit_state(self, client, seed):
        seed(chat=chat_json("I cannot answer.", [], insufficient=True))
        response = client.post("/ask/", {"question": "alpha"})
        assert response.status_code == 200
        content = response.content.decode()
        assert "not sufficient" in content
        assert "I cannot answer" not in content  # model prose never trusted

    def test_post_requires_csrf_token(self, seed):
        client = Client(enforce_csrf_checks=True)
        seed()
        response = client.post("/ask/", {"question": "alpha"})
        assert response.status_code == 403

    def test_post_no_evidence_insufficient_zero_chat(self, client, tmp_path, monkeypatch):
        # Healthy but EMPTY corpus (no recordings at all): zero chat call.
        si.rebuild_index()
        config = ask_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha"]))
        monkeypatch.setattr("workflow.views.ask.get_config", lambda: config)
        monkeypatch.setattr(
            "workflow.services.embedding_client.embed_texts",
            keyword_embedder(["alpha"]),
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("no chat call without evidence")

        monkeypatch.setattr("workflow.services.llm.chat_completion", forbidden)
        response = client.post("/ask/", {"question": "alpha"})
        assert response.status_code == 200
        assert "not sufficient" in response.content.decode()

    def _excerpted_setup(self, tmp_path, monkeypatch, *, sha, chat):
        """Recording with a >1000-char segment: per-document excerpting."""
        make_transcribed_recording(["alpha " + "x" * 4000], sha=sha)
        si.rebuild_index()
        config = ask_config(tmp_path)
        ei.rebuild_embedding_index(
            config, embedder=keyword_embedder(["alpha", "x"])
        )
        monkeypatch.setattr("workflow.views.ask.get_config", lambda: config)
        monkeypatch.setattr(
            "workflow.services.embedding_client.embed_texts",
            keyword_embedder(["alpha", "x"]),
        )
        monkeypatch.setattr("workflow.services.llm.chat_completion", chat)

    def test_post_excerpted_evidence_renders_note(self, client, tmp_path, monkeypatch):
        self._excerpted_setup(
            tmp_path,
            monkeypatch,
            sha="webask-long-0",
            chat=chat_json("Answer [C1].", ["C1"]),
        )
        response = client.post("/ask/", {"question": "alpha"})
        content = response.content.decode()
        assert "excerpted or omitted" in content
        # The note is application-owned: no evidence content is exposed.
        assert "x" * 20 not in content

    def test_post_insufficient_with_excerpted_evidence_renders_note(
        self, client, tmp_path, monkeypatch
    ):
        self._excerpted_setup(
            tmp_path,
            monkeypatch,
            sha="webask-long-1",
            chat=chat_json("cannot say.", [], insufficient=True),
        )
        response = client.post("/ask/", {"question": "alpha"})
        content = response.content.decode()
        assert "not sufficient" in content
        assert "excerpted or omitted" in content
        assert "x" * 20 not in content


class TestSummaryRouteRegression:
    def test_non_uuid_summary_pk_resolves(self, client):
        rec, t, s = make_transcribed_recording(["x"], sha="route-0")
        # Summary PKs are CharField(36): legacy/manual non-UUID values are
        # valid and must resolve through the str converter (the old uuid
        # converter 404'd them).
        summary = make_summary_version(
            rec, t, s, title="Legacy", id="legacy-summary-id"
        )
        response = client.get(f"/recordings/{rec.pk}/summaries/{summary.pk}/")
        assert response.status_code == 200
        assert "Legacy" in response.content.decode()

    def test_uuid_summary_pk_still_resolves(self, client):
        rec, t, s = make_transcribed_recording(["x"], sha="route-1")
        summary = make_summary_version(rec, t, s, title="Modern")
        response = client.get(f"/recordings/{rec.pk}/summaries/{summary.pk}/")
        assert response.status_code == 200
        assert "Modern" in response.content.decode()