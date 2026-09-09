"""Bounded local /embeddings client for the configured oMLX endpoint.

Safety properties (mirroring :mod:`workflow.services.llm` but for
embeddings):

- httpx only, argv-free; ``transport`` is injectable for tests.
- The API key is read from the environment by name at call time and
  used only for the Authorization header; it is never logged, stored,
  or included in any error message.
- The endpoint is validated before any transport use: http/https only,
  no credentials/query/fragment, and a hostname that is exactly
  ``localhost`` or a literal loopback IP. Anything else is rejected
  before a request is made.
- The serialized request body is built deterministically and bounded
  before HTTP; a hard byte cap is enforced on the fully serialized
  JSON payload.
- Every response body is streamed through a hard byte cap; oversized
  bodies raise :class:`EmbeddingError` before parsing.
- The OpenAI-compatible envelope is validated strictly; vectors are
  reordered back into input order and validated (exact cardinality,
  strict base64, little-endian IEEE-754 float32, finite values,
  consistent nonzero dimensions).
- Exception messages contain only static descriptions, exception type
  names, and HTTP status codes — never bodies, headers, vectors,
  inputs, prompts, or secrets.
"""

from __future__ import annotations

import base64
import json
import re
import struct
import urllib.parse
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any

import httpx

from brainlib.config import AppConfig
from workflow.services.vector_codec import MAX_DIMENSION

# Fixed production request encoding_format. There is no runtime fallback
# and no retry: a single encoding is always sent.
ENCODING_FORMAT = "base64"

# Client hard constants (independent of configuration).
REQUEST_CAP_BYTES = 1024 * 1024  # 1 MiB
RESPONSE_CAP_BYTES = 2 * 1024 * 1024  # 2 MiB
HARD_MAX_BATCH = 128


class EmbeddingError(Exception):
    """Base class for sanitized embedding failures. ``code`` is stable."""

    code = "embedding_error"


class EmbeddingNotConfigured(EmbeddingError):
    code = "model_not_configured"


class EmbeddingEndpointNotLocal(EmbeddingError):
    code = "endpoint_not_local"


class EmbeddingInvalidInput(EmbeddingError):
    code = "invalid_input"


class EmbeddingBatchTooLarge(EmbeddingError):
    code = "batch_too_large"


class EmbeddingRequestTooLarge(EmbeddingError):
    code = "request_too_large"


class EmbeddingEndpointUnavailable(EmbeddingError):
    code = "endpoint_unavailable"


class EmbeddingTimeout(EmbeddingError):
    code = "timeout"


class EmbeddingHTTPError(EmbeddingError):
    code = "http_error"

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"endpoint returned HTTP {status_code}")


class EmbeddingResponseTooLarge(EmbeddingError):
    code = "response_too_large"

    def __init__(self) -> None:
        super().__init__(f"response body exceeded the {RESPONSE_CAP_BYTES} byte cap")


class EmbeddingInvalid(EmbeddingError):
    """Malformed or schema-invalid output. ``code`` carries the fine cause."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


# Stable fine-grained failure codes surfaced via ``EmbeddingInvalid.code``.
INVALID_ENVELOPE = "invalid_envelope"
INVALID_VECTOR = "invalid_vector"
DIMENSION_MISMATCH = "dimension_mismatch"
INVALID_ENCODING = "invalid_encoding"
MALFORMED_HTTP_JSON = "malformed_http_json"


@dataclass(frozen=True)
class EmbeddingBatch:
    """One text and its validated embedding vector (input order)."""

    text: str
    embedding: tuple[float, ...]


def _validate_endpoint(base_url: str) -> str:
    """Validate the endpoint URL before any transport use.

    Accepts only http/https with no credentials, query, or fragment, and
    a hostname that is exactly ``localhost`` or a literal loopback IP.
    Returns the normalized endpoint (base URL with trailing slash
    stripped, then ``/embeddings`` appended) or raises
    :class:`EmbeddingEndpointNotLocal`.
    """
    if not base_url or not base_url.strip():
        raise EmbeddingEndpointNotLocal("embedding base_url is blank")
    try:
        parsed = urllib.parse.urlsplit(base_url.strip())
    except ValueError:
        raise EmbeddingEndpointNotLocal("embedding base_url is not a valid URL") from None
    if parsed.scheme not in ("http", "https"):
        raise EmbeddingEndpointNotLocal("embedding endpoint must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise EmbeddingEndpointNotLocal("embedding endpoint must not contain credentials")
    if parsed.query or parsed.fragment:
        raise EmbeddingEndpointNotLocal("embedding endpoint must not contain a query or fragment")
    hostname = parsed.hostname
    if not hostname:
        raise EmbeddingEndpointNotLocal("embedding endpoint must have a hostname")
    hostname_lower = hostname.lower()
    if hostname_lower == "localhost":
        pass  # exactly localhost
    else:
        try:
            addr = ip_address(hostname)
        except ValueError:
            raise EmbeddingEndpointNotLocal(
                "embedding endpoint hostname must be localhost or a literal loopback IP"
            ) from None
        if not addr.is_loopback:
            raise EmbeddingEndpointNotLocal(
                "embedding endpoint hostname must be localhost or a literal loopback IP"
            )
    return f"{base_url.strip().rstrip('/')}/embeddings"


def _validate_input(texts: Any) -> list[str]:
    """Validate the input texts; returns the concrete list of strings.

    Rejects a str (which is a sequence), an empty batch, a non
    list/tuple, any non-str element, and any empty string. No coercion
    or truncation.
    """
    if isinstance(texts, str):
        raise EmbeddingInvalidInput("texts must be a list of strings, not a single string")
    if not isinstance(texts, (list, tuple)):
        raise EmbeddingInvalidInput("texts must be a list of strings")
    if not texts:
        raise EmbeddingInvalidInput("texts must not be empty")
    for item in texts:
        if type(item) is not str:
            raise EmbeddingInvalidInput("every text must be a string")
    for item in texts:
        if item == "":
            raise EmbeddingInvalidInput("texts must not contain empty strings")
    return list(texts)


def _build_request_body(config: AppConfig, batch: list[str]) -> bytes:
    """Serialize the JSON request body deterministically and bound it."""
    payload = {
        "model": config.embedding.model,
        "input": batch,
        "encoding_format": ENCODING_FORMAT,
    }
    try:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError):
        raise EmbeddingInvalidInput("texts could not be serialized") from None
    if len(body) > REQUEST_CAP_BYTES:
        raise EmbeddingRequestTooLarge(
            f"serialized request body exceeded the {REQUEST_CAP_BYTES} byte cap"
        )
    return body


def _validate_envelope(body: Any, model: str, expected_count: int) -> list[dict[str, Any]]:
    """Strict OpenAI-compatible envelope validation for /embeddings.

    Returns the list of item dicts in whatever order they appear; the
    caller reorders by index.
    """
    if not isinstance(body, dict):
        raise EmbeddingInvalid(INVALID_ENVELOPE, "response is not a JSON object")
    data = body.get("data")
    if not isinstance(data, list):
        raise EmbeddingInvalid(INVALID_ENVELOPE, "response has no data list")
    if len(data) != expected_count:
        raise EmbeddingInvalid(INVALID_ENVELOPE, "response data cardinality does not match the request")
    returned_model = body.get("model")
    if not isinstance(returned_model, str) or returned_model != model:
        raise EmbeddingInvalid(INVALID_ENVELOPE, "response model does not match the configured model")
    for item in data:
        if not isinstance(item, dict):
            raise EmbeddingInvalid(INVALID_ENVELOPE, "response data item is not an object")
    return data


def _validate_timeout(timeout: Any) -> float:
    """Validate a caller-supplied timeout override before any transport use.

    Accepts exact int/float (bool rejected) that is finite, > 0 and
    <= 600 (the same cap enforced on the configured value). Any other
    value raises a sanitized :class:`EmbeddingInvalidInput` — the
    override never reaches the transport.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise EmbeddingInvalidInput("timeout override must be a number")
    if timeout != timeout or timeout in (float("inf"), float("-inf")):
        raise EmbeddingInvalidInput("timeout override must be finite")
    if timeout <= 0:
        raise EmbeddingInvalidInput("timeout override must be positive")
    if timeout > 600:
        raise EmbeddingInvalidInput("timeout override must not exceed 600")
    return float(timeout)


def _decode_vector(embedding: Any) -> list[float]:
    """Strictly validate and decode a single base64 float32 vector."""
    if not isinstance(embedding, str):
        raise EmbeddingInvalid(INVALID_VECTOR, "embedding is not a string")
    # Strict base64: reject non-alphabet characters, misplaced/extra
    # padding and trailing data. Length must be a multiple of 4 and the
    # regex bounds padding to the trailing position; b64decode(validate=True)
    # additionally rejects excess/incorrect padding.
    text = embedding
    if len(text) % 4 != 0:
        raise EmbeddingInvalid(INVALID_ENCODING, "embedding base64 length is not a multiple of 4")
    # Reject characters outside the strict base64 alphabet (including any
    # padding characters that are not in the expected position).
    if not re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", text):
        raise EmbeddingInvalid(INVALID_ENCODING, "embedding base64 contains invalid characters")
    try:
        decoded = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        raise EmbeddingInvalid(INVALID_ENCODING, "embedding base64 could not be decoded") from None
    # Canonical base64: re-encoding the decoded bytes must reproduce the
    # supplied string exactly. This rejects noncanonical pad-bit
    # encodings (e.g. "AB==" for one zero byte) without normalizing them.
    if base64.b64encode(decoded).decode("ascii") != text:
        raise EmbeddingInvalid(INVALID_ENCODING, "embedding base64 is not canonically encoded")
    if not decoded:
        raise EmbeddingInvalid(INVALID_ENCODING, "embedding decoded to empty bytes")
    if len(decoded) % 4 != 0:
        raise EmbeddingInvalid(INVALID_ENCODING, "embedding decoded bytes are not divisible by 4")
    count = len(decoded) // 4
    if count > MAX_DIMENSION:
        raise EmbeddingInvalid(INVALID_VECTOR, "embedding dimension exceeds the hard cap")
    values = struct.unpack(f"<{count}f", decoded)
    for value in values:
        if value != value or value in (float("inf"), float("-inf")):
            raise EmbeddingInvalid(INVALID_VECTOR, "embedding contains a non-finite value")
    return list(values)


def embed_texts(
    config: AppConfig,
    texts: Any,
    *,
    timeout: float | None = None,
    transport=None,
) -> list[EmbeddingBatch]:
    """Embed ``texts`` via the local /embeddings endpoint.

    ``texts`` may be a list or tuple of strings; the batch is processed
    as configured (``config.embedding.batch_size``) but never larger
    than :data:`HARD_MAX_BATCH`. Returns one :class:`EmbeddingBatch` per
    input text, in input order.

    ``timeout``, when supplied, overrides the configured timeout and is
    validated BEFORE any transport use: exact int/float only (bool
    rejected), finite, > 0 and <= 600; anything else raises a sanitized
    ``invalid_input`` error with zero transport calls.

    Raises the :class:`EmbeddingError` taxonomy on every failure mode.
    Makes exactly one HTTP request per batch and never retries.
    """
    model = config.embedding.model
    if not model or not model.strip():
        raise EmbeddingNotConfigured("no embedding model configured (blank)")

    texts = _validate_input(texts)

    batch_size = config.embedding.batch_size
    if not isinstance(batch_size, int) or isinstance(batch_size, bool):
        batch_size = 32
    batch_size = max(1, min(batch_size, HARD_MAX_BATCH))

    # Enforce the effective batch size (configured, clamped to the hard
    # max): no text is silently dropped.
    if len(texts) > batch_size:
        raise EmbeddingBatchTooLarge(
            f"batch of {len(texts)} exceeds the configured batch size {batch_size}"
        )

    endpoint = _validate_endpoint(config.embedding.base_url)

    api_key = config.api_key_for(config.embedding.api_key_env)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request_body = _build_request_body(config, list(texts))

    if timeout is not None:
        timeout = _validate_timeout(timeout)
    else:
        timeout = config.embedding.timeout_seconds
    response_body = _post_bounded(endpoint, request_body, headers, timeout, transport)

    try:
        body = json.loads(response_body)
    except ValueError:
        raise EmbeddingInvalid(MALFORMED_HTTP_JSON, "response body is not valid JSON") from None

    items = _validate_envelope(body, model, len(texts))

    # Reorder items by their index back into input order. Each item's
    # index must be an exact int (never bool) inside 0..n-1, and each
    # index may appear at most once — duplicates are rejected directly
    # at insertion, not inferred from cardinality or a later gap.
    by_index: dict[int, dict[str, Any]] = {}
    for item in items:
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise EmbeddingInvalid(INVALID_ENVELOPE, "response item index is not an integer")
        if not 0 <= index < len(texts):
            raise EmbeddingInvalid(INVALID_ENVELOPE, "response item index is out of range")
        if index in by_index:
            raise EmbeddingInvalid(INVALID_ENVELOPE, "response item index is duplicated")
        by_index[index] = item
    # Cardinality is exact and every index is unique within 0..n-1, so
    # 0..n-1 is complete by the pigeonhole principle; the explicit sweep
    # below documents and guards that invariant.
    for i in range(len(texts)):
        if i not in by_index:
            raise EmbeddingInvalid(INVALID_ENVELOPE, "response item index is missing")

    result: list[EmbeddingBatch] = []
    for i, text in enumerate(texts):
        item = by_index[i]
        vector = _decode_vector(item.get("embedding"))
        if not vector:
            raise EmbeddingInvalid(INVALID_VECTOR, "embedding vector is empty")
        # Consistent nonzero dimensions across the batch.
        if result and len(result[0].embedding) != len(vector):
            raise EmbeddingInvalid(DIMENSION_MISMATCH, "embedding dimensions are inconsistent")
        result.append(EmbeddingBatch(text=text, embedding=tuple(vector)))
    return result


def _post_bounded(
    url: str,
    request_body: bytes,
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
            with client.stream("POST", url, content=request_body, headers=headers) as response:
                if response.status_code >= 400:
                    raise EmbeddingHTTPError(response.status_code)
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > RESPONSE_CAP_BYTES:
                    raise EmbeddingResponseTooLarge()
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > RESPONSE_CAP_BYTES:
                        raise EmbeddingResponseTooLarge()
                    chunks.append(chunk)
    except httpx.TimeoutException:
        raise EmbeddingTimeout() from None
    except EmbeddingError:
        raise
    except httpx.HTTPError as exc:
        raise EmbeddingEndpointUnavailable(
            f"endpoint error: {type(exc).__name__}"
        ) from exc
    except OSError as exc:
        raise EmbeddingEndpointUnavailable(
            f"endpoint error: {type(exc).__name__}"
        ) from exc
    return b"".join(chunks)
