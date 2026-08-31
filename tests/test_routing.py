"""Tests for language routing: heuristics, classifier, decision flow."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from brainlib.config import ConfigError
from workflow.services import routing as routing_service
from workflow.services.audiosamples import SampleExtractionError
from workflow.services.routing import (
    REASON_AUTO_CONFIDENT,
    REASON_CLASSIFIER_UNAVAILABLE,
    REASON_CONTRADICTORY,
    REASON_LOW_CONFIDENCE,
    REASON_SAMPLING_FAILED,
    REASON_SILENT,
    REASON_ZH_AMBIGUOUS,
    _parse_classifier_json,
    _zh_verdict,
    cjk_ratio,
    classify_with_omlx,
    marker_score,
    route_recording,
    script_ratio,
)

from factories import default_routing, make_config

pytestmark = pytest.mark.django_db

CANTONESE_SAMPLE = "呢個係我哋嘅屋企，唔係你嘅地方，佢哋都唔知去咗邊度。"
MANDARIN_SAMPLE = "这是我们的家，不是你的地方，他们都不知道去了哪里，你说什么？"
FINNISH_SAMPLE = "Hyvää päivää, tänään on aurinkoinen sää ja menemme kotiin ja syömme valmiiksi."
ENGLISH_SAMPLE = "This is our home, not your place. They do not know where it went today."


def write_wav(path: Path, seconds: float = 5.0, rate: int = 16000, amplitude: int = 8000) -> Path:
    import struct
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(struct.pack("<h", amplitude * (i % 7 - 3)) for i in range(frames)))
    return path


class TestHeuristics:
    def test_cjk_ratio(self):
        assert cjk_ratio("中文文本") > 0.9
        assert cjk_ratio("english words") == 0.0

    def test_marker_score_cantonese(self):
        assert marker_score(CANTONESE_SAMPLE, routing_service.CANTONESE_MARKERS) > 0
        assert marker_score(MANDARIN_SAMPLE, routing_service.CANTONESE_MARKERS) == 0

    def test_marker_score_mandarin(self):
        assert marker_score(MANDARIN_SAMPLE, routing_service.MANDARIN_MARKERS) > 0

    def test_script_ratio_is_weak_evidence_only(self):
        # The zh-CN model may output simplified script for Cantonese speech;
        # script ratio alone can never approve a Cantonese/Mandarin decision.
        zh_verdict, ambiguous = _zh_verdict(
            {
                "zh_hk_cantonese_score": 0.0,
                "zh_hk_mandarin_score": 0.0,
                "zh_cn_cantonese_score": 0.0,
                "zh_cn_mandarin_score": 0.0,
                "zh_hk_script_traditional_ratio": 1.0,  # fully traditional
                "zh_cn_script_traditional_ratio": 0.0,  # fully simplified
            }
        )
        assert zh_verdict == routing_service.ROUTE_UNCERTAIN
        assert ambiguous is True

    def test_zh_verdict_cantonese(self):
        zh_route, ambiguous = _zh_verdict(
            {
                "zh_hk_cantonese_score": 4.0,
                "zh_hk_mandarin_score": 0.0,
                "zh_cn_cantonese_score": 0.0,
                "zh_cn_mandarin_score": 0.5,
                "zh_hk_script_traditional_ratio": 0.8,
                "zh_cn_script_traditional_ratio": 0.1,
            }
        )
        assert zh_route == routing_service.ROUTE_CANTONESE
        assert ambiguous is False

    def test_zh_verdict_ambiguous_on_near_tie(self):
        zh_route, ambiguous = _zh_verdict(
            {
                "zh_hk_cantonese_score": 1.0,
                "zh_hk_mandarin_score": 0.0,
                "zh_cn_cantonese_score": 0.0,
                "zh_cn_mandarin_score": 1.2,
                "zh_hk_script_traditional_ratio": 0.5,
                "zh_cn_script_traditional_ratio": 0.5,
            }
        )
        assert zh_route == routing_service.ROUTE_UNCERTAIN
        assert ambiguous is True


class TestClassifierParsing:
    def test_valid_payload(self):
        parsed = _parse_classifier_json(
            json.dumps({"route": "cantonese", "confidence": 0.9, "reason_code": "yue_markers", "evidence": "x"})
        )
        assert parsed["route"] == "cantonese"
        assert parsed["confidence"] == 0.9

    def test_code_fence_accepted(self):
        parsed = _parse_classifier_json('```json\n{"route": "european", "confidence": 0.95}\n```')
        assert parsed["route"] == "european"

    def test_invalid_route_rejected(self):
        with pytest.raises(routing_service.RoutingInvalid):
            _parse_classifier_json(json.dumps({"route": "klingon", "confidence": 0.9}))

    def test_out_of_range_confidence_rejected(self):
        with pytest.raises(routing_service.RoutingInvalid):
            _parse_classifier_json(json.dumps({"route": "cantonese", "confidence": 1.5}))

    def test_boolean_confidence_rejected(self):
        with pytest.raises(routing_service.RoutingInvalid):
            _parse_classifier_json(json.dumps({"route": "cantonese", "confidence": True}))

    def test_non_json_rejected(self):
        with pytest.raises(routing_service.RoutingInvalid):
            _parse_classifier_json("I think it is Cantonese.")

    def test_non_object_rejected(self):
        with pytest.raises(routing_service.RoutingInvalid):
            _parse_classifier_json('["cantonese", 0.9]')

    def test_classify_never_logs_secrets(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        config = make_config(
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
        monkeypatch.setenv("BRAIN_TEST_LLM_API_KEY", "super-secret-value")

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"route": "european", "confidence": 0.9}'}}]})

        result = classify_with_omlx(config, {"zh_hk": "a", "zh_cn": "b", "european": "c"}, transport=httpx.MockTransport(handler))
        assert result["route"] == "european"
        assert captured["auth"] == "Bearer super-secret-value"
        # The secret must never appear in the result payload.
        assert "super-secret-value" not in json.dumps(result)


class TestRoutingFlow:
    def _recording_with_source(self, tmp_path):
        from workflow.models import AudioSource, Recording

        wav = write_wav(tmp_path / "audio" / "rec.wav")
        recording = Recording.objects.create(sha256=f"route-{Recording.objects.count()}")
        AudioSource.objects.create(
            recording=recording,
            path=str(wav),
            path_identity=str(wav).casefold(),
            original_filename=wav.name,
            presence="present",
            is_canonical=True,
            discovery_state="hashed",
        )
        return recording, wav

    def _mocked_transcription(self, monkeypatch, texts: dict[str, str]):
        def fake_run(config, audio_path, model_id, language_arg, speakers, runner=None, timeout_seconds=None):
            # Sample transcriptions must never enable speaker detection.
            assert speakers is False
            return texts.get(model_id)

        monkeypatch.setattr(
            "workflow.services.transcription.run_mw_transcription", fake_run
        )

    def test_high_confidence_cantonese_auto_routes(self, tmp_path, monkeypatch):
        recording, wav = self._recording_with_source(tmp_path)
        self._mocked_transcription(
            monkeypatch,
            {"apple:zh-HK": CANTONESE_SAMPLE, "apple:zh-CN": CANTONESE_SAMPLE.replace("唔", "不"), "parakeet-pro:nvidia_parakeet-v3": "garbled"},
        )
        config = make_config(tmp_path)
        attempt_dir = Path(config.storage.temp) / "routing" / str(recording.pk) / "attempt_1"

        def classifier(config, candidates):
            assert "<zh_hk>" in candidates["zh_hk"] or candidates["zh_hk"]
            assert set(candidates) == {"zh_hk", "zh_cn", "european"}
            return {"route": "cantonese", "confidence": 0.95, "reason_code": "yue", "evidence": "e"}

        outcome = route_recording(config, recording, wav, attempt_dir, classifier=classifier)
        assert outcome.ready_to_transcribe is True
        assert outcome.profile_name == "cantonese"
        assert outcome.method == "automatic"
        assert outcome.reason_code == REASON_AUTO_CONFIDENT
        # Temp dir was left for the caller; service-level cleanup is tested
        # in test_pipeline.py (route_one cleans up in finally).

    def test_low_confidence_does_not_auto_route(self, tmp_path, monkeypatch):
        recording, wav = self._recording_with_source(tmp_path)
        # zh candidates produce CJK text (Chinese family, consistent with
        # the classifier), but the classifier is unsure -> below threshold.
        self._mocked_transcription(
            monkeypatch,
            {"apple:zh-HK": CANTONESE_SAMPLE, "apple:zh-CN": MANDARIN_SAMPLE, "parakeet-pro:nvidia_parakeet-v3": "garbled"},
        )
        config = make_config(tmp_path)
        outcome = route_recording(
            config, recording, wav, Path(config.storage.temp) / "a1",
            classifier=lambda config, labelled: {"route": "cantonese", "confidence": 0.4, "reason_code": "unsure", "evidence": ""},
        )
        assert outcome.ready_to_transcribe is False
        assert outcome.reason_code == REASON_LOW_CONFIDENCE
        assert outcome.profile_name is None

    def test_zh_ambiguous_goes_to_review_even_with_high_confidence(self, tmp_path, monkeypatch):
        recording, wav = self._recording_with_source(tmp_path)
        # No colloquial markers at all in either zh candidate -> ambiguous.
        self._mocked_transcription(
            monkeypatch,
            {"apple:zh-HK": "這是地方", "apple:zh-CN": "这是地方", "parakeet-pro:nvidia_parakeet-v3": "garbled"},
        )
        config = make_config(tmp_path)
        outcome = route_recording(
            config, recording, wav, Path(config.storage.temp) / "a2",
            classifier=lambda config, labelled: {"route": "cantonese", "confidence": 0.99, "reason_code": "x", "evidence": ""},
        )
        assert outcome.ready_to_transcribe is False
        assert outcome.reason_code == REASON_ZH_AMBIGUOUS

    def test_contradictory_evidence_blocks_auto(self, tmp_path, monkeypatch):
        recording, wav = self._recording_with_source(tmp_path)
        # The zh candidates emit degenerate Latin text (as observed for
        # Chinese audio on the zh models) while parakeet produces clean
        # European text -> family verdict european, classifier cantonese.
        self._mocked_transcription(
            monkeypatch,
            {"apple:zh-HK": "sindeditestentent merisecoman lin otput", "apple:zh-CN": "sindeatc testenance michisper comand", "parakeet-pro:nvidia_parakeet-v3": ENGLISH_SAMPLE},
        )
        config = make_config(tmp_path)
        outcome = route_recording(
            config, recording, wav, Path(config.storage.temp) / "a3",
            classifier=lambda config, labelled: {"route": "cantonese", "confidence": 0.99, "reason_code": "x", "evidence": ""},
        )
        assert outcome.ready_to_transcribe is False
        assert outcome.reason_code == REASON_CONTRADICTORY

    def test_classifier_unavailable_needs_review(self, tmp_path, monkeypatch):
        recording, wav = self._recording_with_source(tmp_path)
        self._mocked_transcription(
            monkeypatch,
            {"apple:zh-HK": CANTONESE_SAMPLE, "apple:zh-CN": MANDARIN_SAMPLE, "parakeet-pro:nvidia_parakeet-v3": "garbled"},
        )
        config = make_config(tmp_path)

        def classifier(config, labelled):
            raise routing_service.RoutingUnavailable("endpoint down")

        outcome = route_recording(config, recording, wav, Path(config.storage.temp) / "a4", classifier=classifier)
        assert outcome.ready_to_transcribe is False
        assert outcome.reason_code == REASON_CLASSIFIER_UNAVAILABLE

    def test_classifier_invalid_needs_review(self, tmp_path, monkeypatch):
        recording, wav = self._recording_with_source(tmp_path)
        self._mocked_transcription(
            monkeypatch,
            {"apple:zh-HK": CANTONESE_SAMPLE, "apple:zh-CN": MANDARIN_SAMPLE, "parakeet-pro:nvidia_parakeet-v3": "garbled"},
        )
        config = make_config(tmp_path)

        def classifier(config, labelled):
            raise routing_service.RoutingInvalid("bad json")

        outcome = route_recording(config, recording, wav, Path(config.storage.temp) / "a5", classifier=classifier)
        assert outcome.ready_to_transcribe is False
        assert outcome.reason_code == "classifier_invalid"

    def test_blank_classifier_model_is_unavailable(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)  # llm.model blank
        with pytest.raises(routing_service.RoutingUnavailable):
            classify_with_omlx(config, {"zh_hk": "a", "zh_cn": "b", "european": "c"})

    def test_silent_audio_needs_review(self, tmp_path, monkeypatch):
        recording, wav = self._recording_with_source(tmp_path)
        # Overwrite with digital silence.
        import struct
        import wave

        with wave.open(str(wav), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * 16000 * 5)
        config = make_config(tmp_path)
        outcome = route_recording(config, recording, wav, Path(config.storage.temp) / "a6")
        assert outcome.ready_to_transcribe is False
        assert outcome.reason_code == REASON_SILENT

    def test_too_short_audio_needs_review(self, tmp_path, monkeypatch):
        recording, wav = self._recording_with_source(tmp_path)
        write_wav(wav, seconds=1.0)
        config = make_config(tmp_path)
        outcome = route_recording(config, recording, wav, Path(config.storage.temp) / "a7")
        assert outcome.ready_to_transcribe is False
        assert outcome.reason_code == "too_short"

    def test_sampling_failure_needs_review(self, tmp_path, monkeypatch):
        recording, wav = self._recording_with_source(tmp_path)
        wav.write_bytes(b"not a wav at all")  # unreadable, afconvert missing in test env
        config = make_config(tmp_path)
        outcome = route_recording(config, recording, wav, Path(config.storage.temp) / "a8")
        assert outcome.ready_to_transcribe is False
        assert outcome.reason_code in (REASON_SAMPLING_FAILED, "too_short", REASON_SAMPLING_FAILED)

    def test_routing_evidence_is_bounded(self, tmp_path, monkeypatch):
        recording, wav = self._recording_with_source(tmp_path)
        long_text = CANTONESE_SAMPLE * 500
        self._mocked_transcription(
            monkeypatch,
            {"apple:zh-HK": long_text, "apple:zh-CN": MANDARIN_SAMPLE * 500, "parakeet-pro:nvidia_parakeet-v3": ENGLISH_SAMPLE * 500},
        )
        config = make_config(tmp_path)
        outcome = route_recording(
            config, recording, wav, Path(config.storage.temp) / "a9",
            classifier=lambda config, labelled: {"route": "cantonese", "confidence": 0.95, "reason_code": "y", "evidence": ""},
        )
        assert outcome.ready_to_transcribe is True
        for key, value in outcome.evidence.items():
            assert len(str(value)) < 1000, key
        json.dumps(outcome.evidence)  # must stay JSON-serializable


def _captured_argv(argv=None):  # retained for potential future assertions
    return argv or []
