"""CLI tests for ``brain ask`` (Step 5D).

All network is mocked (fake embedder + fake chat); no real HTTP. Proves:
exit 0 for an answer OR the explicit insufficiency result, exit 2 for an
invalid question BEFORE any health/network work, exit 1 for sanitized
operational failures, no lock/recovery/writes, JSON/human output with
citation metadata and server-owned URLs, and the migration preflight.
"""

from __future__ import annotations

import json

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from brainlib import cli
from brainlib.config import EmbeddingConfig, LLMConfig
from factories import make_config, make_transcribed_recording
from workflow.services import embedding_index as ei
from workflow.services import search_index as si
from workflow.services.embedding_client import EmbeddingBatch
from workflow.services.llm import LLMHTTPError

pytestmark = pytest.mark.django_db(transaction=True)

DIM = 4
LOCAL_LLM = "http://127.0.0.1:8000/v1"


def ask_config(tmp_path, *, model="test-embed-model", llm_base_url=LOCAL_LLM):
    return make_config(
        tmp_path,
        embedding=EmbeddingConfig(
            base_url="http://127.0.0.1:1/v1",
            model=model,
            api_key_env="BRAIN_TEST_LLM_API_KEY",
            timeout_seconds=120,
            batch_size=32,
        ),
        llm=LLMConfig(
            provider="openai_compatible",
            base_url=llm_base_url,
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


def chat_json(answer, citations=(), *, insufficient=False, calls=None, raise_exc=None):
    def chat(config, *, system_prompt, user_prompt, temperature, max_tokens):
        if calls is not None:
            calls.append(True)
        if raise_exc is not None:
            raise raise_exc
        return json.dumps(
            {"answer": answer, "citations": list(citations), "insufficient": insufficient}
        )

    return chat


def seed_corpus(tmp_path, monkeypatch, *, with_summary=True):
    make_transcribed_recording(
        ["alpha segment zero", "alpha segment one"], sha="askcli-0"
    )
    if with_summary:
        from factories import make_summary_version

        rec, t, s = make_transcribed_recording(["alpha base"], sha="askcli-1")
        make_summary_version(rec, t, s, title="alpha summary", overview="alpha overview")
    si.rebuild_index()
    config = ask_config(tmp_path)
    ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha"]))
    monkeypatch.setattr("brainlib.config.load_config", lambda: config)
    return config


def patch_network(monkeypatch, *, chat=None, embedder=None):
    monkeypatch.setattr(
        "workflow.services.embedding_client.embed_texts",
        embedder or keyword_embedder(["alpha"]),
    )
    monkeypatch.setattr(
        "workflow.services.llm.chat_completion",
        chat or chat_json("Answer [C1].", ["C1"]),
    )


class TestAskCli:
    def test_answer_json_and_human(self, tmp_path, monkeypatch, capsys):
        seed_corpus(tmp_path, monkeypatch)
        patch_network(monkeypatch)
        capsys.readouterr()
        assert cli.main(["ask", "alpha", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["state"] == "answered"
        assert payload["question"] == "alpha"
        assert payload["evidence_truncated"] is False
        assert payload["citations"]
        citation = payload["citations"][0]
        assert citation["id"] == "C1"
        assert citation["url"].startswith("/recordings/")
        # Full evidence text is never exposed by default.
        assert "segment zero" not in json.dumps(payload)
        assert "alpha overview" not in json.dumps(payload)

        capsys.readouterr()
        assert cli.main(["ask", "alpha"]) == 0
        out = capsys.readouterr().out
        assert "Answer:" in out
        assert "Citations:" in out
        assert "[C1]" in out
        assert "/recordings/" in out

    def test_insufficient_exit_zero(self, tmp_path, monkeypatch, capsys):
        # Healthy but empty corpus.
        si.rebuild_index()
        config = ask_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha"]))
        monkeypatch.setattr("brainlib.config.load_config", lambda: config)

        def forbidden(*args, **kwargs):
            raise AssertionError("no chat call without evidence")

        monkeypatch.setattr("workflow.services.embedding_client.embed_texts", keyword_embedder(["alpha"]))
        monkeypatch.setattr("workflow.services.llm.chat_completion", forbidden)
        capsys.readouterr()
        assert cli.main(["ask", "anything"]) == 0
        out = capsys.readouterr().out
        assert "not sufficient" in out
        assert "note:" not in out  # nothing was excerpted or dropped

    def test_human_notes_evidence_truncation_when_excerpted(self, tmp_path, monkeypatch, capsys):
        from factories import make_transcribed_recording

        make_transcribed_recording(["alpha " + "x" * 4000], sha="askcli-long-0")
        si.rebuild_index()
        config = ask_config(tmp_path)
        ei.rebuild_embedding_index(
            config, embedder=keyword_embedder(["alpha", "x"])
        )
        monkeypatch.setattr("brainlib.config.load_config", lambda: config)
        patch_network(monkeypatch)
        capsys.readouterr()
        assert cli.main(["ask", "alpha"]) == 0
        out = capsys.readouterr().out
        assert "note:" in out
        assert "excerpted or omitted" in out
        # The note is application-owned: it never echoes evidence content.
        assert "x" * 20 not in out

    def test_human_json_carries_excerpted_flag(self, tmp_path, monkeypatch, capsys):
        from factories import make_transcribed_recording

        make_transcribed_recording(["alpha " + "x" * 4000], sha="askcli-long-1")
        si.rebuild_index()
        config = ask_config(tmp_path)
        ei.rebuild_embedding_index(
            config, embedder=keyword_embedder(["alpha", "x"])
        )
        monkeypatch.setattr("brainlib.config.load_config", lambda: config)
        patch_network(monkeypatch)
        capsys.readouterr()
        assert cli.main(["ask", "alpha", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["evidence_truncated"] is True

    def test_invalid_question_exit_2_before_health(self, monkeypatch, capsys):
        def forbidden(*args, **kwargs):
            raise AssertionError("no health work for invalid input")

        monkeypatch.setattr(si, "build_status_report", forbidden)
        assert cli.main(["ask", "   "]) == 2
        assert "empty" in capsys.readouterr().err

        canary = "SECRET-ASK-CLI-" + "x" * 300
        assert cli.main(["ask", canary]) == 2
        err = capsys.readouterr().err
        assert "256" in err
        assert canary not in err
        assert "SECRET-ASK-CLI" not in err

    def test_operational_error_exit_1_sanitized(self, tmp_path, monkeypatch, capsys):
        seed_corpus(tmp_path, monkeypatch)
        patch_network(monkeypatch, chat=chat_json("", [], raise_exc=LLMHTTPError(500)))
        capsys.readouterr()
        assert cli.main(["ask", "alpha"]) == 1
        captured = capsys.readouterr()
        assert "HTTP error" in captured.err
        assert "alpha" not in captured.err
        assert "Traceback" not in captured.err
        assert captured.out == ""

    def test_invalid_endpoint_exit_1_no_transport(self, tmp_path, monkeypatch, capsys):
        seed_corpus(tmp_path, monkeypatch)
        bad = ask_config(tmp_path, llm_base_url="http://example.com/v1")
        monkeypatch.setattr("brainlib.config.load_config", lambda: bad)

        def forbidden_embed(*args, **kwargs):
            raise AssertionError("no embedding transport")

        def forbidden_chat(*args, **kwargs):
            raise AssertionError("no chat transport")

        monkeypatch.setattr("workflow.services.embedding_client.embed_texts", forbidden_embed)
        monkeypatch.setattr("workflow.services.llm.chat_completion", forbidden_chat)
        capsys.readouterr()
        assert cli.main(["ask", "alpha"]) == 1
        err = capsys.readouterr().err
        assert "loopback" in err

    def test_ask_never_locks_or_recovers(self, tmp_path, monkeypatch, capsys):
        from workflow.services import pipeline as pipeline_service
        from workflow.services import pipeline_lock as pipeline_lock_service

        seed_corpus(tmp_path, monkeypatch)
        patch_network(monkeypatch)

        def forbidden(*args, **kwargs):
            raise AssertionError("ask must not lock or recover")

        monkeypatch.setattr(pipeline_lock_service, "pipeline_lock", forbidden)
        monkeypatch.setattr(pipeline_service, "pipeline_lock", forbidden)
        monkeypatch.setattr(pipeline_service, "recover_interruptions", forbidden)
        capsys.readouterr()
        assert cli.main(["ask", "alpha"]) == 0

    def test_ask_never_writes(self, tmp_path, monkeypatch, capsys):
        seed_corpus(tmp_path, monkeypatch)
        patch_network(monkeypatch)
        capsys.readouterr()
        with CaptureQueriesContext(connection) as ctx:
            assert cli.main(["ask", "alpha"]) == 0
        writes = [
            query["sql"]
            for query in ctx.captured_queries
            if query["sql"].lstrip().upper().startswith(
                ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "REPLACE")
            )
        ]
        assert writes == []
