"""Restricted tolerant classifier parsing + finite request state machine.

The classifier may make at most: one structured request; one plain
initial request (only after an explicit response_format/json_schema
capability rejection on HTTP 400/422); one repair request (only after
an HTTP-successful but schema-invalid structured response). No loops.
"""

from __future__ import annotations

import json

import httpx
import pytest

from workflow.services import routing as routing_service
from workflow.services.routing import _parse_classifier_json, classify_with_omlx, route_recording

from factories import make_config

pytestmark = pytest.mark.django_db

VALID_CONTENT = '{"route": "european", "confidence": 0.9}'
OPEN = "<think>"
CLOSE = "</think>"


def _omlx_config(tmp_path):
    config = make_config(tmp_path)
    return make_config(
        tmp_path,
        llm=type(config.llm)(
            provider="openai_compatible",
            base_url="http://omlx.test/v1",
            model="test-model",
            api_key_env="BRAIN_TEST_LLM_API_KEY",
            temperature=0.2,
            timeout_seconds=600,
        ),
    )


def _envelope(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _run_classifier(tmp_path, handler):
    config = _omlx_config(tmp_path)
    calls = []

    def tracking_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(body)
        return handler(request, body)

    result = None
    error = None
    try:
        result = classify_with_omlx(
            config, {"zh_hk": "a", "zh_cn": "b", "european": "c"},
            transport=httpx.MockTransport(tracking_handler),
        )
    except (routing_service.RoutingUnavailable, routing_service.RoutingInvalid) as exc:
        error = exc
    return result, error, calls


class TestRestrictedParser:
    def test_pure_json(self):
        parsed = _parse_classifier_json(VALID_CONTENT)
        assert parsed["route"] == "european"

    def test_single_fence_accepted(self):
        parsed = _parse_classifier_json("```json\n" + VALID_CONTENT + "\n```")
        assert parsed["route"] == "european"

    def test_closed_think_block_then_object_accepted(self):
        content = OPEN + " probes the model's dialect before answering. " + CLOSE + "\n" + VALID_CONTENT
        parsed = _parse_classifier_json(content)
        assert parsed["route"] == "european"

    def test_leading_commentary_rejected(self):
        with pytest.raises(routing_service.RoutingInvalid, match="no_json_object"):
            _parse_classifier_json("Sure, here you go: " + VALID_CONTENT)

    def test_trailing_commentary_rejected(self):
        with pytest.raises(routing_service.RoutingInvalid, match="trailing_commentary"):
            _parse_classifier_json(VALID_CONTENT + " hope that helps!")

    def test_multiple_objects_rejected(self):
        with pytest.raises(routing_service.RoutingInvalid, match="multiple_objects"):
            _parse_classifier_json(VALID_CONTENT + "\n" + VALID_CONTENT)

    def test_unclosed_think_block_rejected(self):
        with pytest.raises(routing_service.RoutingInvalid, match="no_json_object"):
            _parse_classifier_json("think about it... " + VALID_CONTENT)

    def test_think_block_plus_fence_rejected(self):
        content = OPEN + "thinking...\n" + CLOSE + "\n```json\n" + VALID_CONTENT + "\n```"
        with pytest.raises(routing_service.RoutingInvalid, match="no_json_object"):
            _parse_classifier_json(content)

    def test_oversized_think_block_rejected(self):
        huge = "x" * (routing_service.MAX_THINK_BLOCK_CHARS + 1)
        content = OPEN + huge + CLOSE + "\n" + VALID_CONTENT
        with pytest.raises(routing_service.RoutingInvalid, match="think_block_too_large"):
            _parse_classifier_json(content)

    def test_invalid_route_stable_code_no_raw_value_in_message(self):
        bad = json.dumps({"route": "klingon is the answer", "confidence": 0.9})
        with pytest.raises(routing_service.RoutingInvalid, match="invalid_route") as excinfo:
            _parse_classifier_json(bad)
        assert "klingon" not in str(excinfo.value)

    def test_multiple_objects_message_has_no_content(self):
        with pytest.raises(routing_service.RoutingInvalid, match="multiple_objects"):
            _parse_classifier_json('{"route": "european", "confidence": 0.5} {"a": 1}')


class TestClassifierStateMachine:
    def test_structured_success_single_call(self, tmp_path):
        def handler(request, body):
            assert body.get("response_format", {}).get("type") == "json_schema"
            return httpx.Response(200, json=_envelope(VALID_CONTENT))

        result, error, calls = _run_classifier(tmp_path, handler)
        assert error is None and result["route"] == "european"
        assert len(calls) == 1

    def test_capability_rejection_falls_back_to_exactly_one_plain_call(self, tmp_path):
        seen = []

        def handler(request, body):
            seen.append("response_format" in body)
            if "response_format" in body:
                return httpx.Response(
                    400,
                    json={"error": {"message": "Unknown parameter: response_format is not supported by this server"}},
                )
            return httpx.Response(200, json=_envelope(VALID_CONTENT))

        result, error, calls = _run_classifier(tmp_path, handler)
        assert error is None and result["route"] == "european"
        assert seen == [True, False]
        assert len(calls) == 2
        assert "response_format" not in calls[1]

    def test_generic_400_does_not_fall_back(self, tmp_path):
        def handler(request, body):
            return httpx.Response(400, json={"error": {"message": "unexpected token"}})

        result, error, calls = _run_classifier(tmp_path, handler)
        assert result is None
        assert isinstance(error, routing_service.RoutingUnavailable)
        assert len(calls) == 1  # no plain fallback

    def test_400_without_param_mention_does_not_fall_back(self, tmp_path):
        def handler(request, body):
            return httpx.Response(400, json={"error": {"message": "unknown parameter 'temperature'"}})

        result, error, calls = _run_classifier(tmp_path, handler)
        assert isinstance(error, routing_service.RoutingUnavailable)
        assert len(calls) == 1

    def test_auth_error_never_falls_back_even_naming_response_format(self, tmp_path):
        def handler(request, body):
            return httpx.Response(401, json={"error": {"message": "response_format unsupported without auth"}})

        result, error, calls = _run_classifier(tmp_path, handler)
        assert isinstance(error, routing_service.RoutingUnavailable)
        assert len(calls) == 1

    def test_other_statuses_never_fall_back(self, tmp_path):
        for status in (404, 409, 429, 500):
            def handler(request, body, status=status):
                return httpx.Response(status, json={"error": {"message": "response_format unknown parameter"}})

            result, error, calls = _run_classifier(tmp_path, handler)
            assert isinstance(error, routing_service.RoutingUnavailable), status
            assert len(calls) == 1, status

    def test_schema_invalid_content_triggers_exactly_one_repair(self, tmp_path):
        bodies = []

        def handler(request, body):
            bodies.append(body)
            if len(bodies) == 1:
                return httpx.Response(200, json=_envelope("I think it is European, honestly."))
            return httpx.Response(200, json=_envelope(VALID_CONTENT))

        result, error, calls = _run_classifier(tmp_path, handler)
        assert error is None and result["route"] == "european"
        assert len(bodies) == 2
        assert "failed validation" in bodies[1]["messages"][0]["content"]
        # The repair prompt never echoes the raw previous output.
        assert "I think it is European" not in bodies[1]["messages"][0]["content"]

    def test_repair_also_invalid_is_terminal(self, tmp_path):
        bodies = []

        def handler(request, body):
            bodies.append(body)
            return httpx.Response(200, json=_envelope("still not json"))

        result, error, calls = _run_classifier(tmp_path, handler)
        assert result is None
        assert isinstance(error, routing_service.RoutingInvalid)
        assert len(bodies) == 2  # structured + repair; no loops

    def test_plain_path_invalid_gets_no_repair(self, tmp_path):
        bodies = []

        def handler(request, body):
            bodies.append(body)
            if "response_format" in body:
                return httpx.Response(
                    400, json={"error": {"message": "unknown parameter: response_format not supported"}}
                )
            return httpx.Response(200, json=_envelope("nope"))

        result, error, calls = _run_classifier(tmp_path, handler)
        assert isinstance(error, routing_service.RoutingInvalid)
        assert len(bodies) == 2  # structured + plain only

    def test_diagnostics_are_bounded_and_body_free(self, tmp_path):
        def handler(request, body):
            return httpx.Response(500, json={"error": {"message": "boom with sk-secret-abc /Users/harry/inbox/x.wav"}})

        result, error, calls = _run_classifier(tmp_path, handler)
        assert isinstance(error, routing_service.RoutingUnavailable)
        diag = error.diagnostics
        assert set(diag) <= {"classifier_calls", "structured_output", "classifier_validation", "calls", "repair_used"}
        assert diag["classifier_calls"] == 1
        serialized = json.dumps(diag)
        assert "sk-secret-abc" not in serialized
        assert "/Users/harry" not in serialized
        assert "boom" not in serialized

    def test_capability_rejection_422_recorded(self, tmp_path):
        def handler(request, body):
            if "response_format" in body:
                return httpx.Response(422, json={"error": {"message": "json_schema is unsupported"}})
            return httpx.Response(200, json=_envelope(VALID_CONTENT))

        result, error, calls = _run_classifier(tmp_path, handler)
        assert error is None and result["route"] == "european"


def _two_arg(handler):
    """Adapt a handler(request, body) to httpx.MockTransport(request)."""
    def wrapped(request: httpx.Request) -> httpx.Response:
        return handler(request, json.loads(request.content.decode()))
    return wrapped


class TestSuccessfulClassifierProvenance:
    """Successful recovery paths must persist bounded diagnostics too."""

    def _route_through_recording(self, tmp_path, monkeypatch, handler):  # handler(request, body)
        """Full route_recording with the real classifier over MockTransport."""
        import struct
        import wave
        from pathlib import Path

        from workflow.models import AudioSource, Recording
        from workflow.services.routing import route_recording

        wav = tmp_path / "audio" / "rec.wav"
        wav.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(wav), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(
                b"".join(struct.pack("<h", 8000 * (i % 7 - 3)) for i in range(16000 * 50))
            )
        recording = Recording.objects.create(sha256=f"diag-{Recording.objects.count()}")
        AudioSource.objects.create(
            recording=recording,
            path=str(wav),
            path_identity=str(wav).casefold(),
            original_filename=wav.name,
            presence="present",
            is_canonical=True,
            discovery_state="hashed",
        )
        texts = {
            "apple:zh-HK": "sindetestenten merisecoman lin otput" * 20,
            "apple:zh-CN": "sindeatc testenance michisper comand" * 20,
            "parakeet-pro:nvidia_parakeet-v3": "This is our home, not your place. They do not know where it went today. " * 10,
        }

        def fake_sample(config, audio_path, model_id, language_arg, speakers, runner=None, timeout_seconds=None):
            assert speakers is False
            return texts.get(model_id)

        monkeypatch.setattr("workflow.services.transcription.run_mw_transcription", fake_sample)

        def classifier(config, candidates):
            return classify_with_omlx(config, candidates, transport=httpx.MockTransport(_two_arg(handler)))

        config = _omlx_config(tmp_path)
        return route_recording(config, recording, wav, Path(config.storage.temp) / "diag", classifier=classifier)

    def test_direct_structured_success_records_one_call(self, tmp_path, monkeypatch):
        def handler(request, body):
            assert body.get("response_format", {}).get("type") == "json_schema"
            return httpx.Response(200, json=_envelope(VALID_CONTENT))

        outcome = self._route_through_recording(tmp_path, monkeypatch, handler)
        diag = outcome.evidence["classifier_diagnostics"]
        assert diag["classifier_calls"] == 1
        assert diag["calls"] == ["structured"]
        assert diag["structured_output"] == "used"
        assert not diag["repair_used"]
        assert diag["classifier_validation"] == ""

    def test_capability_rejection_plain_success_records_both_calls(self, tmp_path, monkeypatch):
        def handler(request, body):
            if "response_format" in body:
                return httpx.Response(400, json={"error": {"message": "response_format is not supported (unknown parameter)"}})
            return httpx.Response(200, json=_envelope(VALID_CONTENT))

        outcome = self._route_through_recording(tmp_path, monkeypatch, handler)
        assert outcome.route == "european"  # semantics unchanged
        diag = outcome.evidence["classifier_diagnostics"]
        assert diag["classifier_calls"] == 2
        assert diag["calls"] == ["structured", "plain"]
        assert diag["structured_output"] == "rejected_unsupported"
        assert not diag["repair_used"]

    def test_invalid_then_repair_success_records_repair_provenance(self, tmp_path, monkeypatch):
        bodies = []

        def handler(request, body):
            bodies.append(body)
            if len(bodies) == 1:
                return httpx.Response(200, json=_envelope("I think it is European, honestly."))
            return httpx.Response(200, json=_envelope(VALID_CONTENT))

        outcome = self._route_through_recording(tmp_path, monkeypatch, handler)
        assert outcome.route == "european"
        diag = outcome.evidence["classifier_diagnostics"]
        assert diag["classifier_calls"] == 2
        assert diag["calls"] == ["structured", "repair"]
        assert diag["structured_output"] == "used"
        assert diag["repair_used"] is True
        assert diag["classifier_validation"] == "no_json_object"

    def test_diagnostics_persist_into_routing_decision_evidence(self, tmp_path, monkeypatch):
        from workflow.models import ProcessingStatus, Recording, RoutingDecision
        from workflow.services.pipeline import _apply_outcome

        def handler(request, body):
            return httpx.Response(200, json=_envelope(VALID_CONTENT))

        outcome = self._route_through_recording(tmp_path, monkeypatch, handler)
        config = _omlx_config(tmp_path)
        config = type(config)(
            config_path=config.config_path,
            storage=config.storage,
            macwhisper=type(config.macwhisper)(
                command=config.macwhisper.command,
                model=config.macwhisper.model,
                language=config.macwhisper.language,
                speakers=config.macwhisper.speakers,
                normalize_input=config.macwhisper.normalize_input,
                speakers_fallback=config.macwhisper.speakers_fallback,
                output_format=config.macwhisper.output_format,
                file_stable_seconds=config.macwhisper.file_stable_seconds,
                cli_timeout_seconds=config.macwhisper.cli_timeout_seconds,
                routing=type(config.macwhisper.routing)(
                    enabled=True,
                    auto_transcribe=True,
                    confidence_threshold=0.80,
                    default_profile=config.macwhisper.routing.default_profile,
                    profiles=config.macwhisper.routing.profiles,
                    heuristic_auto_route=config.macwhisper.routing.heuristic_auto_route,
                ),
                legacy_model_notice=None,
            ),
            llm=config.llm,
            embedding=config.embedding,
            retention=config.retention,
            summarization=config.summarization,
            tags=config.tags,
            initial_tags=config.initial_tags,
            timezone=config.timezone,
        )
        recording = Recording.objects.create(
            sha256=f"diag-decision-{Recording.objects.count()}",
            processing_status=ProcessingStatus.ROUTING,
        )
        _apply_outcome(config, recording, outcome)
        decision = RoutingDecision.objects.filter(recording=recording, is_active=True).latest("ordinal")
        assert decision.evidence["classifier_diagnostics"]["classifier_calls"] == 1
        assert decision.evidence["classifier"]["route"] == "european"

    def test_diagnostics_never_contain_response_or_prompt_content(self, tmp_path, monkeypatch):
        def handler(request, body):
            return httpx.Response(
                200,
                json=_envelope('{"route": "european", "confidence": 0.9, "reason_code": "r", "evidence": "MODEL-OUTPUT-MARKER"}'),
            )

        outcome = self._route_through_recording(tmp_path, monkeypatch, handler)
        diag = outcome.evidence["classifier_diagnostics"]
        serialized = json.dumps(diag)
        assert "MODEL-OUTPUT-MARKER" not in serialized
        assert "/Users/" not in serialized
        assert "Bearer" not in serialized
        assert "<zh_hk>" not in serialized  # no prompt fragments
        # The classifier evidence block stays the strict four-field mapping.
        assert set(outcome.evidence["classifier"]) == {"route", "confidence", "reason_code", "evidence"}

    def test_injected_classifier_callable_remains_compatible(self, tmp_path, monkeypatch):
        """A plain dict return from an injected callable still routes and
        receives synthesized bounded diagnostics."""
        import struct
        import wave
        from pathlib import Path

        from workflow.models import AudioSource, Recording

        wav = tmp_path / "audio" / "rec.wav"
        wav.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(wav), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(
                b"".join(struct.pack("<h", 8000 * (i % 7 - 3)) for i in range(16000 * 50))
            )
        recording = Recording.objects.create(sha256=f"inj-{Recording.objects.count()}")
        AudioSource.objects.create(
            recording=recording,
            path=str(wav),
            path_identity=str(wav).casefold(),
            original_filename=wav.name,
            presence="present",
            is_canonical=True,
            discovery_state="hashed",
        )
        texts = {
            "apple:zh-HK": "sindetestenten merisecoman lin otput" * 20,
            "apple:zh-CN": "sindeatc testenance michisper comand" * 20,
            "parakeet-pro:nvidia_parakeet-v3": "This is our home, not your place. They do not know where it went today. " * 10,
        }

        def fake_sample(config, audio_path, model_id, language_arg, speakers, runner=None, timeout_seconds=None):
            assert speakers is False
            return texts.get(model_id)

        monkeypatch.setattr("workflow.services.transcription.run_mw_transcription", fake_sample)
        config = _omlx_config(tmp_path)
        outcome = route_recording(
            config, recording, wav, Path(config.storage.temp) / "inj",
            classifier=lambda config, labelled: {"route": "european", "confidence": 0.9, "reason_code": "r", "evidence": "e"},
        )
        assert outcome.route == "european"
        assert outcome.ready_to_transcribe is True
        diag = outcome.evidence["classifier_diagnostics"]
        assert diag["classifier_calls"] == 1
        assert diag["structured_output"] == "injected"
        assert len(json.dumps(outcome.evidence)) < 20000
