"""Language detection acceptance tests.

Detection (explicit Original with unknown source) contract:

- invalid model output retries exactly once → exactly 2 calls;
- endpoint/HTTP/timeout/request-too-large/response-too-large failures
  NEVER retry → exactly 1 call (request-too-large: zero HTTP calls);
- the stable failure category is preserved (request_too_large is never
  conflated with invalid output);
- failure creates a durable finished attempt with exact scope
  provenance, requested selector "original", NO fake resolved language,
  NO variant state, and NO raw content/secrets.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from brainlib.config import LLMConfig
from workflow.models import (
    AttemptOutcome,
    ProcessingAttempt,
    SummaryVariantState,
)
from workflow.services.summarize import (
    _detect_source_language,
    _detect_source_language_with_attempt,
)  # noqa: F401 (helper tests + end-to-end usage below)
from factories import make_config, make_transcribed_recording, final_summary_json
from test_summarize import make_llm_config

pytestmark = pytest.mark.django_db


def _detector_config(max_input_characters=100000):
    llm_config = LLMConfig(
        provider="openai_compatible",
        base_url="http://test/v1",
        model="test",
        api_key_env="BRAIN_TEST_KEY",
        temperature=0.1,
        timeout_seconds=60,
    )
    config = MagicMock()
    config.llm = llm_config
    summarization = MagicMock()
    summarization.temperature = 0.1
    summarization.max_output_tokens = 100
    summarization.max_input_characters = max_input_characters
    config.summarization = summarization
    return config


def _counting_llm(behaviour):
    """Wrap a per-call behaviour (response or exception factory) with a counter."""
    counter = {"n": 0}

    def llm(*, system, user):
        counter["n"] += 1
        return behaviour()

    llm.counter = counter
    return llm


class TestDetectionCallPolicy:
    def test_invalid_output_retries_exactly_once(self):
        """Invalid detector output: exactly TWO calls, then failure."""
        rec, trans, sec = make_transcribed_recording(["hello world"])
        llm = _counting_llm(lambda: json.dumps({"language": "INVALID!"}))

        result, error = _detect_source_language(
            _detector_config(), trans, llm_call=llm
        )

        assert result is None
        assert error is not None
        assert error.code == "source_language_unknown"
        assert llm.counter["n"] == 2

    def test_endpoint_failure_does_not_retry(self):
        from workflow.services.llm import LLMUnavailable

        rec, trans, sec = make_transcribed_recording(["hello world"])
        llm = _counting_llm(
            lambda: (_ for _ in ()).throw(
                LLMUnavailable("endpoint_unavailable", "connection refused")
            )
        )

        result, error = _detect_source_language(_detector_config(), trans, llm_call=llm)

        assert result is None
        assert error.code == "endpoint_unavailable"
        assert llm.counter["n"] == 1

    def test_http_error_does_not_retry(self):
        from workflow.services.llm import LLMHTTPError

        rec, trans, sec = make_transcribed_recording(["hello world"])
        llm = _counting_llm(lambda: (_ for _ in ()).throw(LLMHTTPError(500)))

        result, error = _detect_source_language(_detector_config(), trans, llm_call=llm)

        assert result is None
        assert error.code == "http_error"
        assert llm.counter["n"] == 1

    def test_timeout_does_not_retry(self):
        from workflow.services.llm import LLMTimeout

        rec, trans, sec = make_transcribed_recording(["hello world"])
        llm = _counting_llm(lambda: (_ for _ in ()).throw(LLMTimeout("timed out")))

        result, error = _detect_source_language(_detector_config(), trans, llm_call=llm)

        assert result is None
        assert error.code == "timeout"
        assert llm.counter["n"] == 1

    def test_response_too_large_does_not_retry(self):
        from workflow.services.llm import LLMResponseTooLarge

        rec, trans, sec = make_transcribed_recording(["hello world"])
        llm = _counting_llm(lambda: (_ for _ in ()).throw(LLMResponseTooLarge()))

        result, error = _detect_source_language(_detector_config(), trans, llm_call=llm)

        assert result is None
        assert error.code == "response_too_large"
        assert llm.counter["n"] == 1

    def test_request_too_large_is_its_own_category(self):
        """Request-too-large is a distinct stable category — never
        generic invalid output — and never retried (zero HTTP calls)."""
        from workflow.services.chunking import InputTooLarge

        rec, trans, sec = make_transcribed_recording(["hello world"])
        llm = _counting_llm(lambda: json.dumps({"language": "fi"}))

        with pytest.raises(InputTooLarge):
            _detect_source_language(
                _detector_config(max_input_characters=1), trans, llm_call=llm
            )

        assert llm.counter["n"] == 0  # gated before any HTTP/model call

    def test_canonical_mixed_case_codes(self):
        rec, trans, sec = make_transcribed_recording(["hello world"])
        cases = {
            "FI": "fi",
            "en": "en",
            "EN-US": "en-US",
            "zh-hk": "zh-HK",
            "yue": "yue",
            "cmn-cn": "cmn-CN",
        }
        for raw, expected in cases.items():
            llm = _counting_llm(lambda raw=raw: json.dumps({"language": raw}))
            result, error = _detect_source_language(_detector_config(), trans, llm_call=llm)
            assert error is None
            assert result == expected, raw


class TestDetectionAttemptProvenance:
    def _run(self, tmp_path, behaviour, *, max_input_characters=100000):
        rec, trans, sec = make_transcribed_recording(["hello world"])
        llm = _counting_llm(behaviour)
        summarization = make_config(tmp_path).summarization
        if max_input_characters != 100000:
            from dataclasses import replace as dc_replace

            summarization = dc_replace(
                summarization, max_input_characters=max_input_characters
            )
        config = make_config(
            tmp_path, llm=make_llm_config(tmp_path).llm, summarization=summarization,
        )
        detection = _detect_source_language_with_attempt(
            config, rec, trans, sec, llm_call=llm
        )
        return rec, trans, sec, detection, detection.attempt, llm

    def test_failure_creates_durable_attempt_with_complete_provenance(
        self, tmp_path
    ):
        from workflow.services.llm import LLMUnavailable

        rec, trans, sec, detection, attempt, llm = self._run(
            tmp_path,
            lambda: (_ for _ in ()).throw(
                LLMUnavailable("endpoint_unavailable", "no connection")
            ),
        )

        assert detection.language is None
        assert detection.error_code == "endpoint_unavailable"
        assert attempt is not None
        assert attempt.outcome == AttemptOutcome.UNREACHABLE
        assert attempt.error_code == "endpoint_unavailable"
        assert attempt.finished_at is not None
        ctx = attempt.context_json
        assert ctx["language_detection"] is True
        assert ctx["requested"] == "original"
        assert ctx["transcript_id"] == trans.pk
        assert ctx["section_id"] == sec.pk
        # NO fake resolved language.
        assert "resolved" not in ctx
        assert "language" not in ctx
        # No raw model/endpoint detail beyond the stable category.
        assert "no connection" not in (attempt.error_message or "")

    def test_request_too_large_attempt_is_durable_and_distinct(self, tmp_path):
        rec, trans, sec, detection, attempt, llm = self._run(
            tmp_path, lambda: json.dumps({"language": "fi"}),
            max_input_characters=1,
        )

        assert detection.language is None
        assert detection.error_code == "request_too_large"
        assert attempt.outcome == AttemptOutcome.INPUT_TOO_LARGE
        assert attempt.error_code == "request_too_large"
        assert attempt.error_code != "source_language_unknown"
        assert llm.counter["n"] == 0

    def test_success_creates_successful_attempt(self, tmp_path):
        rec, trans, sec, detection, attempt, llm = self._run(
            tmp_path, lambda: json.dumps({"language": "en"})
        )

        assert detection.language == "en"
        assert detection.error_code is None
        assert attempt.outcome == AttemptOutcome.SUCCESS
        assert attempt.error_code == ""
        assert attempt.context_json["requested"] == "original"
        assert attempt.context_json["section_id"] == sec.pk

    def test_failure_creates_no_variant_state_and_no_summary(self, tmp_path):
        from workflow.services.llm import LLMUnavailable

        rec, trans, sec, detection, attempt, llm = self._run(
            tmp_path,
            lambda: (_ for _ in ()).throw(
                LLMUnavailable("endpoint_unavailable", "no connection")
            ),
        )

        assert detection.language is None
        assert not SummaryVariantState.objects.exists()
        # Recording-level default state untouched by a detection failure.
        rec.refresh_from_db()
        assert rec.summary_status == "missing"
        assert rec.last_failed_attempt_id is None

    def test_failure_persists_no_content_or_secrets(self, tmp_path):
        from workflow.services.llm import LLMUnavailable

        rec, trans, sec, detection, attempt, llm = self._run(
            tmp_path,
            lambda: (_ for _ in ()).throw(
                LLMUnavailable("endpoint_unavailable", "no connection")
            ),
        )

        blob = json.dumps(attempt.context_json) + (attempt.error_message or "")
        assert "hello world" not in blob  # no transcript excerpt
        assert "system" not in blob and "transcript>" not in blob  # no prompts
        assert "sk-" not in blob  # no secrets
        assert "http" not in blob.lower()  # no endpoint URLs


class TestDetectionCategorySurfacedThroughSummarizeOne:
    """End-to-end: summarize_one surfaces the durable attempt's actual
    stable category (never a generic source_language_unknown), and no
    variant state is created until a concrete output language exists."""

    def _recording(self):
        rec, trans, sec = make_transcribed_recording(["hello world"])
        return rec, trans, sec

    def _summarize_original(self, tmp_path, llm, *, max_input_characters=None):
        from dataclasses import replace as dc_replace

        from test_summarize import make_config as _make_config_full

        config = _make_config_full(tmp_path, llm=make_llm_config(tmp_path).llm)
        if max_input_characters is not None:
            config = dc_replace(
                config,
                summarization=dc_replace(
                    config.summarization, max_input_characters=max_input_characters
                ),
            )
        from workflow.services import summarize as summarize_service

        rec, trans, sec = self._recording()
        result = summarize_service.summarize_one(
            config, rec, target_language="original", llm_call=llm
        )
        attempt = ProcessingAttempt.objects.filter(
            recording=rec, stage="summarization"
        ).order_by("-ordinal").first()
        return rec, trans, sec, result, attempt

    def test_endpoint_failure_surfaces_endpoint_unavailable(self, tmp_path):
        from workflow.services.llm import LLMUnavailable

        llm = Scripted(
            lambda: (_ for _ in ()).throw(
                LLMUnavailable("endpoint_unavailable", "no connection")
            )
        )
        rec, trans, sec, result, attempt = self._summarize_original(tmp_path, llm)

        assert result["result"] == "failed"
        assert result["error_code"] == "endpoint_unavailable"
        assert attempt.outcome == AttemptOutcome.UNREACHABLE
        assert attempt.error_code == "endpoint_unavailable"
        assert attempt.finished_at is not None
        assert not SummaryVariantState.objects.exists()
        rec.refresh_from_db()
        assert rec.summary_status == "missing"
        assert rec.last_failed_attempt_id is None

    def test_timeout_failure_surfaces_timeout(self, tmp_path):
        from workflow.services.llm import LLMTimeout

        llm = Scripted(
            lambda: (_ for _ in ()).throw(LLMTimeout("timed out"))
        )
        _rec, _trans, _sec, result, attempt = self._summarize_original(tmp_path, llm)

        assert result["error_code"] == "timeout"
        assert attempt.outcome == AttemptOutcome.TIMEOUT

    def test_http_failure_surfaces_http_category(self, tmp_path):
        from workflow.services.llm import LLMHTTPError

        llm = Scripted(lambda: (_ for _ in ()).throw(LLMHTTPError(503)))
        _rec, _trans, _sec, result, attempt = self._summarize_original(tmp_path, llm)

        assert result["error_code"] == "http_error"
        assert attempt.outcome == AttemptOutcome.HTTP_ERROR

    def test_response_too_large_surfaces_its_category(self, tmp_path):
        from workflow.services.llm import LLMResponseTooLarge

        llm = Scripted(lambda: (_ for _ in ()).throw(LLMResponseTooLarge()))
        _rec, _trans, _sec, result, attempt = self._summarize_original(tmp_path, llm)

        assert result["error_code"] == "response_too_large"
        assert attempt.outcome == AttemptOutcome.RESPONSE_TOO_LARGE

    def test_request_too_large_surfaces_request_too_large(self, tmp_path):
        llm = Scripted(lambda: json.dumps({"language": "fi"}))
        _rec, _trans, _sec, result, attempt = self._summarize_original(
            tmp_path, llm, max_input_characters=1
        )

        assert result["error_code"] == "request_too_large"
        assert result["error_code"] != "source_language_unknown"
        assert attempt.outcome == AttemptOutcome.INPUT_TOO_LARGE
        assert attempt.error_code == "request_too_large"
        assert llm.counter["n"] == 0  # gated before any HTTP call

    def test_invalid_output_after_retry_surfaces_source_language_unknown(
        self, tmp_path
    ):
        llm = Scripted(lambda: json.dumps({"language": "INVALID!"}))
        _rec, _trans, _sec, result, attempt = self._summarize_original(tmp_path, llm)

        assert result["error_code"] == "source_language_unknown"
        assert attempt.outcome == AttemptOutcome.INVALID_OUTPUT
        assert llm.counter["n"] == 2  # invalid output retried exactly once

    def test_successful_detection_generates_original_variant(self, tmp_path):
        """The happy path: detection succeeds, then the original-language
        summary is generated with the detected concrete language."""
        from workflow.services import summarize as summarize_service

        responses = [
            json.dumps({"language": "fi"}),
            json.dumps({
                "title": "Suomenkielinen otsikko",
                "overview": "Yhteenveto suomeksi.",
                "key_points": [{"text": "Piste", "level": 1}],
                "action_items": [], "people": [], "organizations": [],
                "topics": [], "suggested_tags": [], "language": "fi",
            }),
        ]
        calls = {"n": 0}

        def llm(*, system, user):
            calls["n"] += 1
            return responses.pop(0)

        config = make_config(tmp_path, llm=make_llm_config(tmp_path).llm)
        rec, trans, sec = self._recording()
        result = summarize_service.summarize_one(
            config, rec, target_language="original", llm_call=llm
        )
        assert result["result"] == "summarized"
        assert result["output_language"] == "fi"
        trans.refresh_from_db()
        assert trans.language_observed == "fi"


class Scripted:
    """Scripted llm_call with a call counter; behaviour is a callable
    returning the response (or raising)."""

    def __init__(self, behaviour):
        self._behaviour = behaviour
        self.counter = {"n": 0}

    def __call__(self, *, system, user):
        self.counter["n"] += 1
        return self._behaviour()
