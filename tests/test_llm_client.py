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


SCHEMA = {
    "type": "object",
    "properties": {"overview": {"type": "string"}},
    "required": ["overview"],
    "additionalProperties": False,
}
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "probe", "strict": True, "schema": SCHEMA},
}


class TestResponseFormat:
    def test_build_payload_includes_response_format_only_when_given(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        plain = llm_service.build_chat_payload(
            config, system_prompt="s", user_prompt="u", temperature=0.0, max_tokens=5
        )
        assert "response_format" not in plain
        structured = llm_service.build_chat_payload(
            config,
            system_prompt="s",
            user_prompt="u",
            temperature=0.0,
            max_tokens=5,
            response_format=RESPONSE_FORMAT,
        )
        assert structured["response_format"] == RESPONSE_FORMAT
        # The size gate measures the schema, not a pre-serialization estimate.
        assert llm_service.request_payload_characters(structured) > llm_service.request_payload_characters(plain)

    def test_request_body_carries_response_format(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200, content=json.dumps({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}).encode()
            )

        assert (
            llm_service.chat_completion(
                config,
                system_prompt="s",
                user_prompt="u",
                temperature=0.0,
                max_tokens=5,
                response_format=RESPONSE_FORMAT,
                transport=httpx.MockTransport(handler),
            )
            == "ok"
        )
        assert captured["body"]["response_format"] == RESPONSE_FORMAT


class TestWarningHeaderDoesNotChangeBehaviour:
    """A 200 Warning never changes the client path and is never surfaced.

    There is deliberately no Warning classifier: any HTTP 200 — including
    one carrying an oMLX ``response_format`` not-enforced Warning — flows
    through the same envelope/content validation and the caller's own
    validation/repair policy. Only an explicit HTTP 400/422 capability
    rejection is special-cased.
    """

    def test_no_warning_classifier_api_remains(self):
        assert not hasattr(llm_service, "classify_format_enforcement")
        assert not hasattr(llm_service, "FORMAT_ENFORCEMENT_BEST_EFFORT")
        assert not hasattr(llm_service, "FORMAT_ENFORCEMENT_ENFORCED")
        assert not hasattr(llm_service, "FORMAT_ENFORCEMENT_UNKNOWN")

    def test_200_not_enforced_warning_is_an_ordinary_success(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        sentinel = "WARNING-SENTINEL-c0ffee"
        body = json.dumps(
            {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]}
        ).encode()
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=body,
                headers={"Warning": f'199 omlx "response_format not enforced {sentinel}"'},
            )
        )
        assert call(config, transport) == "hello"

    def test_200_warning_malformed_body_is_sanitized_without_headers(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        sentinel = "WARNING-SENTINEL-c0ffee"
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b"not json",
                headers={"Warning": f'199 omlx "response_format not enforced {sentinel}"'},
            )
        )
        with pytest.raises(llm_service.LLMInvalid) as excinfo:
            call(config, transport)
        assert excinfo.value.code == "malformed_http_json"
        assert sentinel not in str(excinfo.value)

    def test_200_warning_invalid_envelope_is_sanitized_without_headers(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        sentinel = "WARNING-SENTINEL-c0ffee"
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b'{"choices": []}',
                headers={"Warning": f'199 omlx "response_format not enforced {sentinel}"'},
            )
        )
        with pytest.raises(llm_service.LLMInvalid) as excinfo:
            call(config, transport)
        assert excinfo.value.code == "invalid_envelope"
        assert sentinel not in str(excinfo.value)


def envelope_with_finish(text: str, finish_reason):
    body = {"choices": [{"message": {"role": "assistant", "content": text}}]}
    if finish_reason is not None:
        body["choices"][0]["finish_reason"] = finish_reason
    return json.dumps(body).encode()


class TestFinishReason:
    def test_stop_is_accepted(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=envelope_with_finish("hello", "stop"))
        )
        assert call(config, transport) == "hello"

    def test_missing_is_accepted(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=envelope_with_finish("hello", None))
        )
        assert call(config, transport) == "hello"

    def test_length_is_output_truncated_before_content(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=envelope_with_finish('{"partial": "SENTINEL-TRUNC"', "length")
            )
        )
        with pytest.raises(llm_service.LLMInvalid) as excinfo:
            call(config, transport)
        assert excinfo.value.code == "output_truncated"
        assert str(excinfo.value) == llm_service.OUTPUT_TRUNCATED_MESSAGE
        assert "SENTINEL-TRUNC" not in str(excinfo.value)

    def test_unknown_reason_is_rejected_without_leaking_value(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        sentinel = "REASON-SENTINEL-9a1"
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=envelope_with_finish("hello", sentinel))
        )
        with pytest.raises(llm_service.LLMInvalid) as excinfo:
            call(config, transport)
        assert excinfo.value.code == "invalid_envelope"
        assert sentinel not in str(excinfo.value)

    def test_non_string_reason_is_rejected(self, tmp_path, monkeypatch):
        config = make_llm_config(tmp_path, monkeypatch)
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=envelope_with_finish("hello", 7))
        )
        with pytest.raises(llm_service.LLMInvalid) as excinfo:
            call(config, transport)
        assert excinfo.value.code == "invalid_envelope"


class TestCapabilityRejection:
    def _error_config(self, tmp_path, monkeypatch, status, body):
        config = make_llm_config(tmp_path, monkeypatch)
        transport = httpx.MockTransport(
            lambda request: httpx.Response(status, content=body.encode())
        )
        return config, transport

    def test_explicit_422_response_format_rejection_is_flagged(self, tmp_path, monkeypatch):
        config, transport = self._error_config(
            tmp_path, monkeypatch, 422, '{"error": "unsupported parameter: response_format"}'
        )
        with pytest.raises(llm_service.LLMHTTPError) as excinfo:
            call(config, transport)
        assert excinfo.value.code == "http_error"
        assert excinfo.value.capability_rejection is True

    def test_generic_400_is_not_flagged(self, tmp_path, monkeypatch):
        config, transport = self._error_config(tmp_path, monkeypatch, 400, '{"error": "bad model"}')
        with pytest.raises(llm_service.LLMHTTPError) as excinfo:
            call(config, transport)
        assert excinfo.value.capability_rejection is False

    def test_non_400_or_422_is_not_flagged(self, tmp_path, monkeypatch):
        config, transport = self._error_config(
            tmp_path, monkeypatch, 500, '{"error": "response_format unsupported"}'
        )
        with pytest.raises(llm_service.LLMHTTPError) as excinfo:
            call(config, transport)
        assert excinfo.value.capability_rejection is False

    def test_error_body_is_never_retained(self, tmp_path, monkeypatch):
        sentinel = "BODY-SENTINEL-42xy"
        config, transport = self._error_config(
            tmp_path, monkeypatch, 422, f'{{"error": "unsupported response_format {sentinel}"}}'
        )
        with pytest.raises(llm_service.LLMHTTPError) as excinfo:
            call(config, transport)
        assert excinfo.value.capability_rejection is True
        assert sentinel not in str(excinfo.value)
