"""Tests for the bounded oMLX chat-completions client."""

from __future__ import annotations

import json

import httpx
import pytest

from brainlib.config import LLMConfig
from workflow.services import llm as llm_service

from factories import make_config


def make_llm_config(tmp_path, monkeypatch, **overrides):
    return make_config(
        tmp_path,
        llm=LLMConfig(
            provider="openai_compatible",
            base_url=overrides.pop("base_url", "http://127.0.0.1:1/v1"),
            model=overrides.pop("model", "test-model"),
            api_key_env=overrides.pop("api_key_env", "BRAIN_TEST_LLM_API_KEY"),
            temperature=overrides.pop("temperature", 0.2),
            timeout_seconds=overrides.pop("timeout_seconds", 600),
        ),
    )


def transport_from_responses(handler):
    return httpx.MockTransport(handler)


def ok_transport(content: str):
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]})
    return httpx.MockTransport(lambda request: httpx.Response(200, content=body.encode()))


def call(config, transport):
    return llm_service.chat_completion(
        config,
        system_prompt="system text",
        user_prompt="user text",
        temperature=0.2,
        max_tokens=100,
        transport=transport,
    )


class TestSuccess:
    def test_returns_content(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        assert call(config, ok_transport("hello")) == "hello"

    def test_request_contains_expected_fields(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, content=json.dumps(
                {"choices": [{"message": {"content": "ok"}}]}).encode())

        assert call(config, httpx.MockTransport(handler)) == "ok"
        assert captured["body"]["model"] == "test-model"
        assert captured["body"]["messages"][0]["role"] == "system"
        assert captured["body"]["max_tokens"] == 100

    def test_api_key_header_only_when_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAIN_TEST_LLM_API_KEY", "sekrit")
        config = make_llm_config(tmp_path, monkeypatch)
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, content=json.dumps(
                {"choices": [{"message": {"content": "ok"}}]}).encode())

        call(config, httpx.MockTransport(handler))
        assert captured["auth"] == "Bearer sekrit"


class TestFailures:
    def test_http_error_maps_to_http_error(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        transport = httpx.MockTransport(lambda request: httpx.Response(429))
        with pytest.raises(llm_service.LLMHTTPError) as excinfo:
            call(config, transport)
        assert excinfo.value.code == "http_error"
        assert "429" in str(excinfo.value)

    def test_malformed_http_json(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json{"))
        with pytest.raises(llm_service.LLMInvalid) as excinfo:
            call(config, transport)
        assert excinfo.value.code == "malformed_http_json"

    def test_envelope_not_an_object(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"[1,2]"))
        with pytest.raises(llm_service.LLMInvalid) as excinfo:
            call(config, transport)
        assert excinfo.value.code == "invalid_envelope"

    def test_envelope_missing_choices(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=b'{"other": 1}')
        )
        with pytest.raises(llm_service.LLMInvalid) as excinfo:
            call(config, transport)
        assert excinfo.value.code == "invalid_envelope"

    def test_envelope_content_not_string(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        body = json.dumps({"choices": [{"message": {"content": 42}}]}).encode()
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
        with pytest.raises(llm_service.LLMInvalid) as excinfo:
            call(config, transport)
        assert excinfo.value.code == "invalid_envelope"

    def test_connect_failure_maps_to_unavailable(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)

        def handler(request):
            raise httpx.ConnectError("connection refused")

        with pytest.raises(llm_service.LLMUnavailable):
            call(config, httpx.MockTransport(handler))

    def test_timeout_maps_to_timeout(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)

        def handler(request):
            raise httpx.ConnectTimeout("timed out")

        with pytest.raises(llm_service.LLMTimeout):
            call(config, httpx.MockTransport(handler))

    def test_oversized_content_length_rejected(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-length": str(llm_service.RESPONSE_CAP_BYTES + 1)}
            )
        )
        with pytest.raises(llm_service.LLMResponseTooLarge):
            call(config, transport)

    def test_oversized_streamed_body_rejected(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        big = b"x" * (llm_service.RESPONSE_CAP_BYTES + 10)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=big))
        with pytest.raises(llm_service.LLMResponseTooLarge):
            call(config, transport)

    def test_secrets_never_in_error_messages(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAIN_TEST_LLM_API_KEY", "super-secret-value")
        config = make_llm_config(tmp_path, monkeypatch)
        transport = httpx.MockTransport(lambda request: httpx.Response(500))
        with pytest.raises(llm_service.LLMError) as excinfo:
            call(config, transport)
        assert "super-secret-value" not in str(excinfo.value)
        assert "Bearer" not in str(excinfo.value)


class TestPayloadMeasurement:
    def test_measures_fully_serialized_payload(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        payload = llm_service.build_chat_payload(
            config, system_prompt="sys", user_prompt="user", temperature=0.2, max_tokens=10
        )
        import json as jsonlib

        assert llm_service.request_payload_characters(payload) == len(
            jsonlib.dumps(payload, ensure_ascii=False)
        )

    def test_unicode_counted_as_characters(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        payload = llm_service.build_chat_payload(
            config, system_prompt="s", user_prompt="我" * 50, temperature=0.2, max_tokens=10
        )
        assert llm_service.request_payload_characters(payload) < len(
            json.dumps(payload, ensure_ascii=True)
        )
