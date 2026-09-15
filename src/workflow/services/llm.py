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
  envelopes raise :class:`LLMInvalid`. ``finish_reason`` is validated
  too: ``length`` means the model output was truncated
  (``output_truncated``) and any other unrecognized reason is rejected
  without echoing its value.
- An optional OpenAI-compatible ``response_format`` may be supplied and
  is serialized deterministically inside the measured request; local
  validation remains authoritative whether or not the server enforces
  the format.
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

# Hard cap on the bounded error-body sample inspected ONLY to classify an
# explicit response_format/json_schema capability rejection. The sample
# is never stored, returned, or logged.
ERROR_BODY_SAMPLE_BYTES = 4096

# Allowlisted finish_reason semantics. ``stop`` is the normal completion;
# ``length`` means the response was truncated; anything else is rejected
# with a fixed sanitized message (never the raw value).
FINISH_REASON_STOP = "stop"
FINISH_REASON_LENGTH = "length"
OUTPUT_TRUNCATED_MESSAGE = "model output was truncated before completion"

# Explicit response_format/json_schema capability rejection: HTTP 400/422
# with an explicit mention of the parameter AND explicit
# unsupported/unknown/unexpected-parameter semantics. Mirrors the routing
# classifier's independent allowlist; generic "unknown parameter" text
# alone is insufficient.
_CAPABILITY_STATUS_CODES = (400, 422)
_CAPABILITY_PARAM_PATTERNS = ("response_format", "responseformat", "json_schema")
_CAPABILITY_SEMANTIC_PATTERNS = (
    "unsupported",
    "unknown parameter",
    "unexpected parameter",
    "unrecognized parameter",
    "not supported",
    "invalid parameter",
)


class LLMError(Exception):
    """Base class for sanitized oMLX failures. ``code`` is stored on attempts."""

    code = "llm_error"


class LLMUnavailable(LLMError):
    code = "endpoint_unavailable"


class LLMTimeout(LLMError):
    code = "timeout"


class LLMHTTPError(LLMError):
    code = "http_error"

    def __init__(self, status_code: int, *, capability_rejection: bool = False) -> None:
        self.status_code = status_code
        # True ONLY for an explicit HTTP 400/422 response_format/
        # json_schema capability rejection (classified from a bounded,
        # discarded error-body sample). No body text is retained.
        self.capability_rejection = capability_rejection
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
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": config.llm.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    return payload


def request_payload_characters(payload: dict[str, Any]) -> int:
    """Character length of the fully serialized request body.

    The per-request safety check measures the actual serialized payload
    (static scaffolding, an optional ``response_format`` schema, dynamic
    input, and JSON escaping included), never a pre-serialization
    estimate.
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
    finish_reason = first.get("finish_reason")
    if finish_reason is not None:
        if not isinstance(finish_reason, str):
            raise LLMInvalid("invalid_envelope", "choice has an invalid finish reason")
        if finish_reason == FINISH_REASON_LENGTH:
            # Truncation is its own stable category and must be detected
            # BEFORE any content parsing: a partial/empty content string
            # would otherwise be misclassified as malformed output.
            raise LLMInvalid("output_truncated", OUTPUT_TRUNCATED_MESSAGE)
        if finish_reason != FINISH_REASON_STOP:
            # Unknown values are never echoed back.
            raise LLMInvalid("invalid_envelope", "choice has an unsupported finish reason")
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
    response_format: dict[str, Any] | None = None,
    timeout: float | None = None,
    transport=None,
) -> str:
    """POST one chat completion and return the validated message content.

    Raises the :class:`LLMError` taxonomy on every failure mode; the
    caller decides how failures map onto attempt state. ``response_format``
    is an optional OpenAI-compatible structured-output request.
    """
    payload = build_chat_payload(
        config,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
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


def _capability_rejection(status: int, body_text: str) -> bool:
    """True only for an explicit response_format/json_schema rejection.

    Requires ALL three: HTTP 400/422; explicit mention of
    response_format/json_schema; explicit unsupported/unknown/unexpected
    -parameter semantics. The body text is inspected transiently and is
    never stored or logged.
    """
    if status not in _CAPABILITY_STATUS_CODES:
        return False
    text = (body_text or "")[:ERROR_BODY_SAMPLE_BYTES].lower()
    has_param = any(pattern in text for pattern in _CAPABILITY_PARAM_PATTERNS)
    has_semantic = any(pattern in text for pattern in _CAPABILITY_SEMANTIC_PATTERNS)
    return has_param and has_semantic


def _read_error_sample(response) -> str:
    """Best-effort bounded decode of an HTTP-error body for classification.

    The sample is capped and discarded; any read failure degrades to an
    empty sample so the original HTTP error is always raised unchanged.
    """
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > ERROR_BODY_SAMPLE_BYTES:
                break
            chunks.append(chunk)
    except Exception:
        return ""
    return b"".join(chunks).decode("utf-8", errors="replace")


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
                    sample = _read_error_sample(response)
                    raise LLMHTTPError(
                        response.status_code,
                        capability_rejection=_capability_rejection(response.status_code, sample),
                    )
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
