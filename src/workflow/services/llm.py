"""Bounded OpenAI-compatible chat-completions client for oMLX.

Safety properties (shared with the routing classifier, but centralized
here for summarization):

- httpx only, argv-free; ``transport`` is injectable for tests.
- The API key is read from the environment by name at call time and
  used only for the Authorization header; it is never logged, stored,
  or included in any error message.
- Every response body is read through a hard byte cap; oversized bodies
  raise :class:`LLMResponseTooLarge` before parsing.
- The OpenAI-compatible envelope is validated strictly; malformed
  envelopes raise :class:`LLMInvalid`.
- Exception messages contain only static descriptions, exception type
  names, and HTTP status codes — never bodies, headers, prompts, or
  secrets.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from brainlib.config import AppConfig

# Hard cap on a single HTTP response body, regardless of configuration.
RESPONSE_CAP_BYTES = 2 * 1024 * 1024


class LLMError(Exception):
    """Base class for sanitized oMLX failures. ``code`` is stored on attempts."""

    code = "llm_error"


class LLMUnavailable(LLMError):
    code = "endpoint_unavailable"


class LLMTimeout(LLMError):
    code = "timeout"


class LLMHTTPError(LLMError):
    code = "http_error"

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"endpoint returned HTTP {status_code}")


class LLMResponseTooLarge(LLMError):
    code = "response_too_large"

    def __init__(self) -> None:
        super().__init__(f"response body exceeded the {RESPONSE_CAP_BYTES} byte cap")


class LLMInvalid(LLMError):
    """Malformed or schema-invalid output. ``code`` carries the fine cause."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def build_chat_payload(
    config: AppConfig,
    *,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
) -> dict[str, Any]:
    return {
        "model": config.llm.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }


def request_payload_characters(payload: dict[str, Any]) -> int:
    """Character length of the fully serialized request body.

    The per-request safety check measures the actual serialized payload
    (static scaffolding, dynamic input, and JSON escaping included),
    never a pre-serialization estimate.
    """
    return len(json.dumps(payload, ensure_ascii=False))


def parse_envelope(body: Any) -> str:
    """Strict OpenAI-compatible envelope validation; returns message content."""
    if not isinstance(body, dict):
        raise LLMInvalid("invalid_envelope", "response is not a JSON object")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMInvalid("invalid_envelope", "response has no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise LLMInvalid("invalid_envelope", "invalid choice")
    message = first.get("message")
    if not isinstance(message, dict):
        raise LLMInvalid("invalid_envelope", "choice has no message object")
    content = message.get("content")
    if not isinstance(content, str):
        raise LLMInvalid("invalid_envelope", "message content is not a string")
    return content


def chat_completion(
    config: AppConfig,
    *,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    timeout: float | None = None,
    transport=None,
) -> str:
    """POST one chat completion and return the validated message content.

    Raises the :class:`LLMError` taxonomy on every failure mode; the
    caller decides how failures map onto attempt state.
    """
    payload = build_chat_payload(
        config,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    api_key = config.api_key_for(config.llm.api_key_env)
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = f"{config.llm.base_url.rstrip('/')}/chat/completions"
    timeout = timeout or config.llm.timeout_seconds
    body_bytes = _post_bounded(url, payload, headers, timeout, transport)
    try:
        body = json.loads(body_bytes)
    except ValueError:
        raise LLMInvalid("malformed_http_json", "response body is not valid JSON") from None
    return parse_envelope(body)


def _post_bounded(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
    transport=None,
) -> bytes:
    """Perform the HTTP POST with a hard response-body cap."""
    client_kwargs: dict[str, Any] = {"timeout": timeout}
    if transport is not None:
        client_kwargs["transport"] = transport
    try:
        with httpx.Client(**client_kwargs) as client:
            with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    raise LLMHTTPError(response.status_code)
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > RESPONSE_CAP_BYTES:
                    raise LLMResponseTooLarge()
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > RESPONSE_CAP_BYTES:
                        raise LLMResponseTooLarge()
                    chunks.append(chunk)
    except httpx.TimeoutException:
        raise LLMTimeout() from None
    except LLMError:
        raise
    except httpx.HTTPError as exc:
        # Connectivity problems, TLS failures, etc. Only the exception
        # type name is ever surfaced.
        raise LLMUnavailable(f"endpoint error: {type(exc).__name__}") from exc
    except OSError as exc:
        raise LLMUnavailable(f"endpoint error: {type(exc).__name__}") from exc
    return b"".join(chunks)
