"""Tests for the bounded local /embeddings client (Step 5B.1).

All tests are fully mocked / network-free: httpx MockTransport only, no
real network. Covers request URL/body/base64 format, auth/no-auth,
single/batch and response reorder; every input/bound failure proving
zero transport calls; local URL acceptance/rejection; connectivity,
timeout, HTTP status, both response-cap paths, malformed JSON;
malformed envelopes, model mismatches, cardinality, malformed indexes,
malformed base64, invalid vectors, dimension consistency; privacy
canaries; exactly one request and no retry.
"""

from __future__ import annotations

import base64
import json
import struct

import httpx
import pytest

from brainlib.config import EmbeddingConfig
from workflow.services import embedding_client as ec

from factories import make_config


def make_emb_config(tmp_path, monkeypatch, **overrides):
    """Build a config with an embedding section pointing at loopback."""
    monkeypatch.setenv("BRAIN_TEST_LLM_API_KEY", overrides.pop("api_key", "test-secret"))
    return make_config(
        tmp_path,
        embedding=EmbeddingConfig(
            base_url=overrides.pop("base_url", "http://127.0.0.1:1/v1"),
            model=overrides.pop("model", "test-embed-model"),
            api_key_env=overrides.pop("api_key_env", "BRAIN_TEST_LLM_API_KEY"),
            timeout_seconds=overrides.pop("timeout_seconds", 120),
            batch_size=overrides.pop("batch_size", 32),
        ),
    )


def f32_bytes(values):
    """Encode float32 values as strict little-endian base64."""
    raw = b"".join(struct.pack("<f", v) for v in values)
    return base64.b64encode(raw).decode("ascii")


def vector(*values):
    if len(values) == 1 and isinstance(values[0], (list, tuple)):
        values = values[0]
    return f32_bytes(values)


def envelope(data, model="test-embed-model", extra=None):
    payload = {
        "object": "list",
        "data": data,
        "model": model,
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }
    if extra:
        payload.update(extra)
    return payload


def data_items(*vectors, model="test-embed-model"):
    items = []
    for i, vec in enumerate(vectors):
        items.append({"object": "embedding", "index": i, "embedding": vec})
    return items


def ok_transport(config_model="test-embed-model", dim=4):
    def handler(request):
        body = json.loads(request.content)
        count = len(body["input"])
        items = []
        for i in range(count):
            items.append({"object": "embedding", "index": i, "embedding": vector([0.1] * dim)})
        return httpx.Response(200, content=json.dumps(envelope(items, model=config_model)).encode())

    return httpx.MockTransport(handler)


def call(config, transport, texts, *, timeout=None):
    kwargs = {"transport": transport}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return ec.embed_texts(config, texts, **kwargs)


class TestSuccess:
    def test_single_text_returns_vector(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        result = call(config, ok_transport(), ["hello"])
        assert len(result) == 1
        assert result[0].text == "hello"
        assert isinstance(result[0].embedding, tuple)
        assert len(result[0].embedding) == 4
        assert all(v == pytest.approx(0.1) for v in result[0].embedding)

    def test_batch_and_response_reorder(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        # Response returns items in REVERSED order; client must reorder
        # by index back into input order.
        def handler(request):
            body = json.loads(request.content)
            count = len(body["input"])
            items = [{"object": "embedding", "index": i, "embedding": vector([float(i)] * 3)} for i in range(count)]
            items.reverse()
            return httpx.Response(200, content=json.dumps(envelope(items)).encode())

        result = call(config, httpx.MockTransport(handler), ["a", "b", "c"])
        assert [r.text for r in result] == ["a", "b", "c"]
        assert [r.embedding[0] for r in result] == [0.0, 1.0, 2.0]

    def test_tuple_input_accepted(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        result = call(config, ok_transport(), ("x", "y"))
        assert [r.text for r in result] == ["x", "y"]

    def test_request_url_and_body_base64(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch, base_url="http://localhost:8000/v1/")
        captured = {}

        def handler(request):
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, content=json.dumps(envelope(data_items(vector([1.0, 2.0, 3.0, 4.0])))).encode())

        call(config, httpx.MockTransport(handler), ["text"])
        # Endpoint is base_url.rstrip('/') + '/embeddings'
        assert captured["url"] == "http://localhost:8000/v1/embeddings"
        assert captured["body"]["model"] == "test-embed-model"
        assert captured["body"]["input"] == ["text"]
        assert captured["body"]["encoding_format"] == ec.ENCODING_FORMAT == "base64"

    def test_auth_header_when_key_set(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch, api_key="sekrit")
        captured = {}

        def handler(request):
            captured["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, content=json.dumps(envelope(data_items(vector([0.0, 0.0, 0.0, 0.0])))).encode())

        call(config, httpx.MockTransport(handler), ["t"])
        assert captured["auth"] == "Bearer sekrit"

    def test_no_auth_header_when_key_unset(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch, api_key_env="BRAIN_TEST_MISSING_KEY")
        captured = {}

        def handler(request):
            captured["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, content=json.dumps(envelope(data_items(vector([0.0, 0.0, 0.0, 0.0])))).encode())

        call(config, httpx.MockTransport(handler), ["t"])
        assert captured["auth"] == ""

    def test_extra_fields_ignored(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        def handler(request):
            items = [{"object": "embedding", "index": 0, "embedding": vector([1.0, 0.0, 0.0, 0.0]), "junk": True}]
            return httpx.Response(200, content=json.dumps(envelope(items)).encode())
        result = call(config, httpx.MockTransport(handler), ["t"])
        assert len(result) == 1


class TestModelNotConfigured:
    def test_blank_model_rejected_before_transport(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch, model="")
        calls = []

        def handler(request):
            calls.append(1)
            raise AssertionError("transport must not be called")

        with pytest.raises(ec.EmbeddingNotConfigured) as excinfo:
            call(config, httpx.MockTransport(handler), ["t"])
        assert excinfo.value.code == "model_not_configured"
        assert calls == []


class TestInputValidation:
    """Every invalid input must fail BEFORE any transport call."""

    def _assert_zero_transport(self, config, texts, code):
        calls = []

        def handler(request):
            calls.append(1)
            raise AssertionError("transport must not be called")

        with pytest.raises(ec.EmbeddingError) as excinfo:
            call(config, httpx.MockTransport(handler), texts)
        assert excinfo.value.code == code
        assert calls == []

    def test_str_rejected_as_sequence(self, tmp_path, monkeypatch):
        self._assert_zero_transport(make_emb_config(tmp_path, monkeypatch), "hello", "invalid_input")

    def test_empty_batch_rejected(self, tmp_path, monkeypatch):
        self._assert_zero_transport(make_emb_config(tmp_path, monkeypatch), [], "invalid_input")

    def test_non_list_tuple_rejected(self, tmp_path, monkeypatch):
        self._assert_zero_transport(make_emb_config(tmp_path, monkeypatch), "nope", "invalid_input")

    def test_int_item_rejected(self, tmp_path, monkeypatch):
        self._assert_zero_transport(make_emb_config(tmp_path, monkeypatch), ["a", 5], "invalid_input")

    def test_empty_string_item_rejected(self, tmp_path, monkeypatch):
        self._assert_zero_transport(make_emb_config(tmp_path, monkeypatch), ["a", ""], "invalid_input")

    def test_str_subclass_rejected(self, tmp_path, monkeypatch):
        class MyStr(str):
            pass
        self._assert_zero_transport(make_emb_config(tmp_path, monkeypatch), [MyStr("x")], "invalid_input")


class TestBatchTooLarge:
    def test_over_configured_batch_size(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch, batch_size=2)
        calls = []

        def handler(request):
            calls.append(1)
            raise AssertionError("transport must not be called")

        with pytest.raises(ec.EmbeddingBatchTooLarge) as excinfo:
            call(config, httpx.MockTransport(handler), ["a", "b", "c"])
        assert excinfo.value.code == "batch_too_large"
        assert calls == []

    def test_hard_max_batch_enforced(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch, batch_size=200)
        texts = [f"t{i}" for i in range(ec.HARD_MAX_BATCH + 1)]
        calls = []

        def handler(request):
            calls.append(1)
            raise AssertionError("transport must not be called")

        with pytest.raises(ec.EmbeddingBatchTooLarge) as excinfo:
            call(config, httpx.MockTransport(handler), texts)
        assert excinfo.value.code == "batch_too_large"
        assert calls == []


class TestRequestTooLarge:
    def test_serialized_request_over_cap(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        calls = []

        def handler(request):
            calls.append(1)
            raise AssertionError("transport must not be called")

        big_text = "x" * (ec.REQUEST_CAP_BYTES + 100)
        with pytest.raises(ec.EmbeddingRequestTooLarge) as excinfo:
            call(config, httpx.MockTransport(handler), [big_text])
        assert excinfo.value.code == "request_too_large"
        assert calls == []


class TestTimeoutOverride:
    """Caller-supplied timeout overrides are validated BEFORE transport.

    Only exact int/float, finite, > 0 and <= 600 are accepted (bool
    rejected); every invalid override raises sanitized invalid_input
    with zero transport calls.
    """

    def _assert_invalid(self, config, bad_timeout):
        calls = []

        def handler(request):
            calls.append(1)
            raise AssertionError("transport must not be called")

        with pytest.raises(ec.EmbeddingInvalidInput) as excinfo:
            ec.embed_texts(config, ["t"], timeout=bad_timeout, transport=httpx.MockTransport(handler))
        assert excinfo.value.code == "invalid_input"
        assert calls == []

    def test_bool_rejected(self, tmp_path, monkeypatch):
        self._assert_invalid(make_emb_config(tmp_path, monkeypatch), True)

    def test_zero_rejected(self, tmp_path, monkeypatch):
        self._assert_invalid(make_emb_config(tmp_path, monkeypatch), 0)

    def test_negative_rejected(self, tmp_path, monkeypatch):
        self._assert_invalid(make_emb_config(tmp_path, monkeypatch), -1)

    def test_nan_rejected(self, tmp_path, monkeypatch):
        self._assert_invalid(make_emb_config(tmp_path, monkeypatch), float("nan"))

    def test_infinity_rejected(self, tmp_path, monkeypatch):
        self._assert_invalid(make_emb_config(tmp_path, monkeypatch), float("inf"))
        self._assert_invalid(make_emb_config(tmp_path, monkeypatch), float("-inf"))

    def test_over_cap_rejected(self, tmp_path, monkeypatch):
        self._assert_invalid(make_emb_config(tmp_path, monkeypatch), 601)

    def test_str_rejected(self, tmp_path, monkeypatch):
        self._assert_invalid(make_emb_config(tmp_path, monkeypatch), "fast")

    def test_finite_override_passes_through(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        captured = {}
        real_client = httpx.Client

        class CaptureClient(real_client):
            def __init__(self, *args, **kwargs):
                captured["timeout"] = kwargs.get("timeout")
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(ec.httpx, "Client", CaptureClient)
        call(config, ok_transport(), ["t"], timeout=12.5)
        assert captured["timeout"] == 12.5

    def test_int_override_passes_through(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        captured = {}
        real_client = httpx.Client

        class CaptureClient(real_client):
            def __init__(self, *args, **kwargs):
                captured["timeout"] = kwargs.get("timeout")
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(ec.httpx, "Client", CaptureClient)
        call(config, ok_transport(), ["t"], timeout=5)
        assert captured["timeout"] == 5


class TestEndpointValidation:
    """Local URL acceptance/rejection, all before any transport."""

    def _make(self, tmp_path, monkeypatch, base_url):
        return make_emb_config(tmp_path, monkeypatch, base_url=base_url, model="m")

    def _assert_rejected(self, config):
        calls = []

        def handler(request):
            calls.append(1)
            raise AssertionError("transport must not be called")

        with pytest.raises(ec.EmbeddingEndpointNotLocal) as excinfo:
            call(config, httpx.MockTransport(handler), ["t"])
        assert excinfo.value.code == "endpoint_not_local"
        assert calls == []

    def test_accepts_localhost(self, tmp_path, monkeypatch):
        config = self._make(tmp_path, monkeypatch, "http://localhost:8000/v1")
        # Should not raise endpoint_not_local; use a normal transport.
        call(config, ok_transport(config_model="m"), ["t"])

    def test_accepts_https_localhost(self, tmp_path, monkeypatch):
        config = self._make(tmp_path, monkeypatch, "https://localhost:8000/v1")
        # HTTPS is an accepted scheme; the endpoint stays local.
        call(config, ok_transport(config_model="m"), ["t"])

    def test_accepts_loopback_ipv4(self, tmp_path, monkeypatch):
        config = self._make(tmp_path, monkeypatch, "http://127.0.0.1:8000/v1")
        call(config, ok_transport(config_model="m"), ["t"])

    def test_accepts_loopback_ipv6(self, tmp_path, monkeypatch):
        config = self._make(tmp_path, monkeypatch, "http://[::1]:8000/v1")
        call(config, ok_transport(config_model="m"), ["t"])

    def test_rejects_non_loopback_ip(self, tmp_path, monkeypatch):
        self._assert_rejected(self._make(tmp_path, monkeypatch, "http://8.8.8.8:8000/v1"))

    def test_rejects_remote_hostname(self, tmp_path, monkeypatch):
        self._assert_rejected(self._make(tmp_path, monkeypatch, "http://example.com/v1"))

    def test_rejects_credentials(self, tmp_path, monkeypatch):
        self._assert_rejected(self._make(tmp_path, monkeypatch, "http://user:pass@localhost:8000/v1"))

    def test_rejects_query(self, tmp_path, monkeypatch):
        self._assert_rejected(self._make(tmp_path, monkeypatch, "http://localhost:8000/v1?x=1"))

    def test_rejects_fragment(self, tmp_path, monkeypatch):
        self._assert_rejected(self._make(tmp_path, monkeypatch, "http://localhost:8000/v1#frag"))

    def test_rejects_bad_scheme(self, tmp_path, monkeypatch):
        self._assert_rejected(self._make(tmp_path, monkeypatch, "ftp://localhost/v1"))


class TestTransportFailures:
    def test_connect_failure(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        def handler(request):
            raise httpx.ConnectError("refused")
        with pytest.raises(ec.EmbeddingEndpointUnavailable) as excinfo:
            call(config, httpx.MockTransport(handler), ["t"])
        assert excinfo.value.code == "endpoint_unavailable"

    def test_timeout(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        def handler(request):
            raise httpx.ConnectTimeout("timed out")
        with pytest.raises(ec.EmbeddingTimeout) as excinfo:
            call(config, httpx.MockTransport(handler), ["t"])
        assert excinfo.value.code == "timeout"

    def test_http_4xx(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        with pytest.raises(ec.EmbeddingHTTPError) as excinfo:
            call(config, httpx.MockTransport(lambda r: httpx.Response(422)), ["t"])
        assert excinfo.value.code == "http_error"
        assert "422" in str(excinfo.value)

    def test_http_5xx(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        with pytest.raises(ec.EmbeddingHTTPError) as excinfo:
            call(config, httpx.MockTransport(lambda r: httpx.Response(500)), ["t"])
        assert excinfo.value.code == "http_error"

    def test_response_content_length_over_cap(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, headers={"content-length": str(ec.RESPONSE_CAP_BYTES + 1)})
        )
        with pytest.raises(ec.EmbeddingResponseTooLarge) as excinfo:
            call(config, transport, ["t"])
        assert excinfo.value.code == "response_too_large"

    def test_response_streamed_over_cap(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        big = b"x" * (ec.RESPONSE_CAP_BYTES + 10)
        transport = httpx.MockTransport(lambda r: httpx.Response(200, content=big))
        with pytest.raises(ec.EmbeddingResponseTooLarge) as excinfo:
            call(config, transport, ["t"])
        assert excinfo.value.code == "response_too_large"


class TestMalformedJson:
    def test_malformed_json(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"not json{"))
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            call(config, transport, ["t"])
        assert excinfo.value.code == "malformed_http_json"


class TestEnvelopeValidation:
    def _call(self, tmp_path, monkeypatch, payload_bytes):
        config = make_emb_config(tmp_path, monkeypatch)
        return call(config, httpx.MockTransport(lambda r: httpx.Response(200, content=payload_bytes)), ["t"])

    def test_not_an_object(self, tmp_path, monkeypatch):
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, b"[1,2]")
        assert excinfo.value.code == "invalid_envelope"

    def test_missing_data(self, tmp_path, monkeypatch):
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, json.dumps({"model": "test-embed-model"}).encode())
        assert excinfo.value.code == "invalid_envelope"

    def test_data_not_list(self, tmp_path, monkeypatch):
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, json.dumps({"data": {"x": 1}, "model": "test-embed-model"}).encode())
        assert excinfo.value.code == "invalid_envelope"

    def test_wrong_model(self, tmp_path, monkeypatch):
        payload = json.dumps(envelope(data_items(vector([1.0, 0.0, 0.0, 0.0])), model="other-model")).encode()
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, payload)
        assert excinfo.value.code == "invalid_envelope"

    def test_missing_model(self, tmp_path, monkeypatch):
        payload = json.dumps({"data": data_items(vector([1.0, 0.0, 0.0, 0.0]))}).encode()
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, payload)
        assert excinfo.value.code == "invalid_envelope"

    def test_wrong_cardinality_too_many(self, tmp_path, monkeypatch):
        items = data_items(vector([1.0, 0.0, 0.0, 0.0]), vector([1.0, 0.0, 0.0, 0.0]))
        payload = json.dumps(envelope(items)).encode()
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, payload)
        assert excinfo.value.code == "invalid_envelope"

    def test_data_item_not_object(self, tmp_path, monkeypatch):
        payload = json.dumps(envelope([42])).encode()
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, payload)
        assert excinfo.value.code == "invalid_envelope"

    def test_index_bool(self, tmp_path, monkeypatch):
        items = [{"object": "embedding", "index": True, "embedding": vector([1.0, 0.0, 0.0, 0.0])}]
        payload = json.dumps(envelope(items)).encode()
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, payload)
        assert excinfo.value.code == "invalid_envelope"

    def test_index_out_of_range(self, tmp_path, monkeypatch):
        items = [{"object": "embedding", "index": 5, "embedding": vector([1.0, 0.0, 0.0, 0.0])}]
        payload = json.dumps(envelope(items)).encode()
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, payload)
        assert excinfo.value.code == "invalid_envelope"

    def test_index_duplicate(self, tmp_path, monkeypatch):
        # Two inputs, two response rows, BOTH with index 0: cardinality
        # matches, so this must be rejected as a DUPLICATE (not via a
        # cardinality mismatch or a missing-index gap).
        config = make_emb_config(tmp_path, monkeypatch)
        items = [
            {"object": "embedding", "index": 0, "embedding": vector([1.0, 0.0, 0.0, 0.0])},
            {"object": "embedding", "index": 0, "embedding": vector([1.0, 0.0, 0.0, 0.0])},
        ]
        payload = json.dumps(envelope(items)).encode()
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            call(config, httpx.MockTransport(lambda r: httpx.Response(200, content=payload)), ["a", "b"])
        assert excinfo.value.code == "invalid_envelope"

    def test_index_negative(self, tmp_path, monkeypatch):
        items = [{"object": "embedding", "index": -1, "embedding": vector([1.0, 0.0, 0.0, 0.0])}]
        payload = json.dumps(envelope(items)).encode()
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, payload)
        assert excinfo.value.code == "invalid_envelope"

    def test_index_gap(self, tmp_path, monkeypatch):
        items = [{"object": "embedding", "index": 1, "embedding": vector([1.0, 0.0, 0.0, 0.0])}]
        payload = json.dumps(envelope(items)).encode()
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, payload)
        assert excinfo.value.code == "invalid_envelope"


class TestVectorValidation:
    def _call(self, tmp_path, monkeypatch, embedding):
        config = make_emb_config(tmp_path, monkeypatch)
        items = [{"object": "embedding", "index": 0, "embedding": embedding}]
        return call(config, httpx.MockTransport(lambda r: httpx.Response(200, content=json.dumps(envelope(items)).encode())), ["t"])

    def test_embedding_not_string(self, tmp_path, monkeypatch):
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, [1.0, 2.0])
        assert excinfo.value.code == "invalid_vector"

    def test_missing_padding(self, tmp_path, monkeypatch):
        # 5 base64 chars -> length not divisible by 4 (strict base64)
        bad = base64.b64encode(b"\x00\x00\x00\x00\x00").decode().rstrip("=")
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, bad)
        assert excinfo.value.code in ("invalid_encoding", "invalid_vector")

    def test_trailing_data_rejected(self, tmp_path, monkeypatch):
        # Valid base64 followed by an invalid trailing character
        good = base64.b64encode(b"\x00\x00\x00\x00").decode()
        bad = good + "!"
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, bad)
        assert excinfo.value.code == "invalid_encoding"

    def test_noncanonical_pad_bits_rejected(self, tmp_path, monkeypatch):
        # "AB==" and "AAB=" decode fine (validate=True) but carry
        # non-zero pad bits: re-encoding yields "AA==" / "AAA=". The
        # client must reject them as invalid_encoding, never normalize
        # the padding.
        for bad in ("AB==", "AAB="):
            with pytest.raises(ec.EmbeddingInvalid) as excinfo:
                self._call(tmp_path, monkeypatch, bad)
            assert excinfo.value.code == "invalid_encoding"

    def test_empty_bytes(self, tmp_path, monkeypatch):
        # base64 of empty string
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, "")
        assert excinfo.value.code in ("invalid_encoding", "invalid_vector")

    def test_undivisible_decoded_bytes(self, tmp_path, monkeypatch):
        # 2 bytes -> not divisible by 4
        bad = base64.b64encode(b"\x00\x00").decode()
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, bad)
        assert excinfo.value.code == "invalid_encoding"

    def test_nan_value_in_vector(self, tmp_path, monkeypatch):
        # A non-finite float32 (NaN) inside the decoded vector must be
        # rejected as invalid_vector.
        raw = struct.pack("<f", float("nan"))
        b64 = base64.b64encode(raw).decode()
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, b64)
        assert excinfo.value.code == "invalid_vector"

    def test_inf_value_in_vector(self, tmp_path, monkeypatch):
        raw = struct.pack("<f", float("inf"))
        b64 = base64.b64encode(raw).decode()
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, b64)
        assert excinfo.value.code == "invalid_vector"

    def test_little_endian_required(self, tmp_path, monkeypatch):
        # Bytes that are FINITE under big-endian reading but +inf under
        # little-endian reading (b"\x00\x00\x80\x7f"): a big-endian
        # decoder would accept the vector, but the client must reject it
        # because it reads strict little-endian IEEE-754 float32.
        raw = struct.pack("<f", float("inf"))
        assert struct.unpack(">f", raw)[0] != float("inf")  # finite under big-endian
        b64 = base64.b64encode(raw).decode()
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call(tmp_path, monkeypatch, b64)
        assert excinfo.value.code == "invalid_vector"


class TestDimension:
    def _call_embed(self, config, embedding):
        items = [{"object": "embedding", "index": 0, "embedding": embedding}]
        return call(config, httpx.MockTransport(lambda r: httpx.Response(200, content=json.dumps(envelope(items)).encode())), ["t"])

    def test_inconsistent_dimensions(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)

        def handler(request):
            items = [
                {"object": "embedding", "index": 0, "embedding": vector([1.0, 0.0, 0.0, 0.0])},
                {"object": "embedding", "index": 1, "embedding": vector([1.0, 0.0])},
            ]
            return httpx.Response(200, content=json.dumps(envelope(items)).encode())

        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            call(config, httpx.MockTransport(handler), ["a", "b"])
        assert excinfo.value.code == "dimension_mismatch"

    def test_max_dimension_cap(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        # One more than the hard cap -> rejected
        raw = b"\x00\x00\x00\x00" * (ec.MAX_DIMENSION + 1)
        b64 = base64.b64encode(raw).decode()
        with pytest.raises(ec.EmbeddingInvalid) as excinfo:
            self._call_embed(config, b64)
        assert excinfo.value.code == "invalid_vector"

    def test_at_max_dimension_accepted(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        raw = b"\x00\x00\x80\x3f" * ec.MAX_DIMENSION  # 1.0 in little-endian f32
        b64 = base64.b64encode(raw).decode()
        result = self._call_embed(config, b64)
        assert len(result[0].embedding) == ec.MAX_DIMENSION


class TestPrivacy:
    def test_input_never_in_error(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        secret_text = "SUPER-SECRET-TEXT-CANARY"
        def handler(request):
            raise httpx.ConnectError("refused")
        with pytest.raises(ec.EmbeddingError) as excinfo:
            call(config, httpx.MockTransport(handler), [secret_text])
        assert secret_text not in str(excinfo.value)

    def test_secret_key_never_in_error(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch, api_key="super-secret-value")
        def handler(request):
            raise httpx.ConnectError("refused")
        with pytest.raises(ec.EmbeddingError) as excinfo:
            call(config, httpx.MockTransport(handler), ["t"])
        assert "super-secret-value" not in str(excinfo.value)
        assert "Bearer" not in str(excinfo.value)

    def test_vector_canary_never_in_error_or_logs(self, tmp_path, monkeypatch, caplog):
        import logging

        config = make_emb_config(tmp_path, monkeypatch)
        # Distinctive canonical base64 that decodes to ASCII bytes plus a
        # NaN float: passes envelope/index/base64 checks and fails with
        # invalid_vector on the non-finite value.
        canary_raw = b"CANARY!!" + struct.pack("<f", float("nan")) + b"\x00\x00\x00\x00"
        canary_b64 = base64.b64encode(canary_raw).decode("ascii")
        items = [{"object": "embedding", "index": 0, "embedding": canary_b64}]
        payload = json.dumps(envelope(items)).encode()
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ec.EmbeddingInvalid) as excinfo:
                call(config, httpx.MockTransport(lambda r: httpx.Response(200, content=payload)), ["t"])
        assert excinfo.value.code == "invalid_vector"
        # The exact base64/vector canary never appears in the exception
        # message and the client emits no log records at all.
        assert canary_b64 not in str(excinfo.value)
        assert canary_b64 not in caplog.text
        assert not [r for r in caplog.records if r.name == "workflow.services.embedding_client"]

    def test_raw_response_canary_never_in_error_or_logs(self, tmp_path, monkeypatch, caplog):
        import logging

        config = make_emb_config(tmp_path, monkeypatch)
        raw_canary = "RAW-RESPONSE-CANARY-7f3a9b"
        canary_raw = b"CANARY!!" + struct.pack("<f", float("nan")) + b"\x00\x00\x00\x00"
        items = [{"object": "embedding", "index": 0, "embedding": base64.b64encode(canary_raw).decode("ascii")}]
        # Extra fields are ignored by validation, so the canary rides in
        # the raw response body all the way to the vector-decode failure.
        payload = json.dumps(envelope(items, extra={"x": raw_canary})).encode()
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ec.EmbeddingInvalid) as excinfo:
                call(config, httpx.MockTransport(lambda r: httpx.Response(200, content=payload)), ["t"])
        assert excinfo.value.code == "invalid_vector"
        assert raw_canary not in str(excinfo.value)
        assert raw_canary not in caplog.text
        assert not [r for r in caplog.records if r.name == "workflow.services.embedding_client"]

    def test_caplog_no_secrets(self, tmp_path, monkeypatch, caplog):
        import logging
        config = make_emb_config(tmp_path, monkeypatch, api_key="super-secret-value")
        secret_text = "SUPER-SECRET-TEXT-CANARY"
        def handler(request):
            raise httpx.ConnectError("refused")
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ec.EmbeddingError):
                call(config, httpx.MockTransport(handler), [secret_text])
        combined = caplog.text
        assert "super-secret-value" not in combined
        assert secret_text not in combined


class TestOneRequestNoRetry:
    def test_exactly_one_request_on_success(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        count = {"n": 0}

        def handler(request):
            count["n"] += 1
            body = json.loads(request.content)
            n = len(body["input"])
            return httpx.Response(200, content=json.dumps(envelope(data_items(*[vector([1.0, 0.0, 0.0, 0.0]) for _ in range(n)]))).encode())

        call(config, httpx.MockTransport(handler), ["a", "b"])
        assert count["n"] == 1

    def test_no_retry_on_transient_http_error(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        count = {"n": 0}

        def handler(request):
            count["n"] += 1
            return httpx.Response(503)

        with pytest.raises(ec.EmbeddingHTTPError):
            call(config, httpx.MockTransport(handler), ["t"])
        assert count["n"] == 1

    def test_no_retry_on_connect_failure(self, tmp_path, monkeypatch):
        config = make_emb_config(tmp_path, monkeypatch)
        count = {"n": 0}

        def handler(request):
            count["n"] += 1
            raise httpx.ConnectError("refused")

        with pytest.raises(ec.EmbeddingEndpointUnavailable):
            call(config, httpx.MockTransport(handler), ["t"])
        assert count["n"] == 1
