"""Focused service tests for one-operation model selection.

Proves that an explicit selected model is validated against the effective
allowlist and threaded into EVERY request of one summarization operation
(source-language detection included), the durable attempt provenance, the
Summary provenance and the config fingerprint — while default callers
(no override) keep the exact configured ``config.llm.model`` behavior.
No network, no real audio.
"""

from __future__ import annotations

import pytest

from brainlib.config import ConfigError, LLMConfig
from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    ProcessingAttempt,
    Section,
    Summary,
    Tag,
)
from workflow.services import summarize as summarize_service
from workflow.services.segmentation import SegmentedVersion, save_segmented_version
from workflow.services.summarize import config_fingerprint, summarize_one, summarize_section_one

from factories import (
    default_summarization,
    final_summary_json,
    make_config,
    make_transcribed_recording,
    map_summary_json,
)

pytestmark = pytest.mark.django_db

DEFAULT_MODEL = "default-model"
ALT_MODEL = "alt-model"


def llm_config(model=DEFAULT_MODEL):
    return LLMConfig(
        provider="openai_compatible",
        base_url="http://127.0.0.1:1/v1",
        model=model,
        api_key_env="BRAIN_TEST_LLM_API_KEY",
        temperature=0.2,
        timeout_seconds=600,
    )


def model_config(tmp_path, *, models=(ALT_MODEL,), chunk_characters=24000):
    return make_config(
        tmp_path,
        llm=llm_config(),
        summarization=default_summarization(
            models=models,
            chunk_characters=chunk_characters,
            chunk_overlap_characters=0,
            max_input_characters=100000,
            max_total_characters=100000,
        ),
    )


class CapturingChat:
    """Fake ``llm_service.chat_completion`` that records the selected model."""

    def __init__(self, *, language="fi"):
        self.calls: list[dict] = []
        self.language = language

    def __call__(
        self,
        config,
        *,
        system_prompt,
        user_prompt,
        temperature,
        max_tokens,
        response_format=None,
        timeout=None,
        transport=None,
        model=None,
    ):
        self.calls.append({"model": model, "response_format": response_format})
        if response_format is None:
            return '{"language": "%s"}' % self.language
        name = response_format["json_schema"]["name"]
        if name == "brain_summary_map":
            return map_summary_json()
        return final_summary_json(language=self.language)

    @property
    def models(self):
        return [call["model"] for call in self.calls]


def _configured_tags():
    return list(Tag.objects.filter(is_configured=True).order_by("name"))


class TestRecordingSelection:
    def test_selected_model_reaches_every_chunked_request_and_provenance(
        self, tmp_path, monkeypatch
    ):
        config = model_config(tmp_path, chunk_characters=60)
        recording, _transcript, _section = make_transcribed_recording(
            [f"segment number {i} " + ("x" * 20) for i in range(6)]
        )
        chat = CapturingChat()
        monkeypatch.setattr(
            "workflow.services.summarize.llm_service.chat_completion", chat
        )
        result = summarize_one(config, recording, model=ALT_MODEL)
        assert result["result"] == "summarized"
        # Map + reduce requests: every request used the selected model.
        assert len(chat.calls) >= 3
        assert set(chat.models) == {ALT_MODEL}
        # Durable attempt provenance.
        attempt = (
            ProcessingAttempt.objects.filter(
                recording=recording, stage=AttemptStage.SUMMARIZATION
            )
            .order_by("-ordinal")
            .first()
        )
        assert attempt.model_id == ALT_MODEL
        assert attempt.cli_args_json["model"] == ALT_MODEL
        # Summary provenance and config fingerprint reflect the selection.
        summary = Summary.objects.get(pk=result["summary_id"])
        assert summary.model_id == ALT_MODEL
        assert summary.config_fingerprint == config_fingerprint(
            config, _configured_tags(), output_language=summary.output_language, model=ALT_MODEL
        )
        assert summary.config_fingerprint != config_fingerprint(
            config, _configured_tags(), output_language=summary.output_language
        )
        # The selection applies to this ONE operation only: the frozen
        # configuration is never mutated.
        assert config.llm.model == DEFAULT_MODEL
        assert config.summarization.models == (ALT_MODEL,)

    def test_source_language_detection_uses_selected_model(self, tmp_path, monkeypatch):
        config = model_config(tmp_path)
        recording, transcript, _section = make_transcribed_recording(["hello world"])
        chat = CapturingChat(language="fi")
        monkeypatch.setattr(
            "workflow.services.summarize.llm_service.chat_completion", chat
        )
        result = summarize_one(config, recording, target_language="original", model=ALT_MODEL)
        assert result["result"] == "summarized"
        # The detection request (plain path, no response_format) also used
        # the selected model; the detection attempt records it.
        assert chat.calls[0]["response_format"] is None
        assert chat.models[0] == ALT_MODEL
        assert set(chat.models) == {ALT_MODEL}
        detection = ProcessingAttempt.objects.get(
            recording=recording,
            stage=AttemptStage.SUMMARIZATION,
            context_json__language_detection=True,
        )
        assert detection.model_id == ALT_MODEL
        assert detection.cli_args_json["model"] == ALT_MODEL
        summary = Summary.objects.get(pk=result["summary_id"])
        assert summary.model_id == ALT_MODEL

    def test_default_caller_keeps_configured_model(self, tmp_path):
        config = model_config(tmp_path)
        recording, _transcript, _section = make_transcribed_recording(["hello world"])
        from test_section_summary_service import ScriptedLLM

        llm = ScriptedLLM([final_summary_json()])
        result = summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "summarized"
        attempt = (
            ProcessingAttempt.objects.filter(
                recording=recording, stage=AttemptStage.SUMMARIZATION
            )
            .order_by("-ordinal")
            .first()
        )
        assert attempt.model_id == DEFAULT_MODEL
        assert attempt.cli_args_json["model"] == DEFAULT_MODEL
        summary = Summary.objects.get(pk=result["summary_id"])
        assert summary.model_id == DEFAULT_MODEL
        assert summary.config_fingerprint == config_fingerprint(
            config, _configured_tags(), output_language=summary.output_language
        )

    def test_arbitrary_override_rejected_with_no_side_effects(self, tmp_path):
        config = model_config(tmp_path)
        recording, _transcript, _section = make_transcribed_recording(["hello world"])
        before = ProcessingAttempt.objects.filter(recording=recording).count()
        with pytest.raises(ConfigError):
            summarize_one(config, recording, model="evil-model")
        assert ProcessingAttempt.objects.filter(recording=recording).count() == before
        assert not Summary.objects.filter(recording=recording).exists()
        # The configured default and configured alternatives are allowed.
        assert ALT_MODEL in config.summarization.models


class TestSectionSelection:
    def _split(self):
        recording, transcript, _fixed = make_transcribed_recording(
            [f"segment {i}" for i in range(6)]
        )
        result = save_segmented_version(
            recording.pk, transcript.pk, 0, 6, [3], ["First topic", "Second topic"]
        )
        version = SegmentedVersion.objects.get(pk=result.version_id)
        sections = list(
            Section.objects.filter(segmented_version=version).order_by("ordinal")
        )
        return recording, transcript, sections

    def test_selected_model_reaches_section_summary_provenance(
        self, tmp_path, monkeypatch
    ):
        config = model_config(tmp_path)
        recording, _transcript, sections = self._split()
        chat = CapturingChat()
        monkeypatch.setattr(
            "workflow.services.summarize.llm_service.chat_completion", chat
        )
        result = summarize_section_one(config, sections[0], model=ALT_MODEL)
        assert result["result"] == "summarized"
        assert set(chat.models) == {ALT_MODEL}
        summary = Summary.objects.get(pk=result["summary_id"])
        assert summary.model_id == ALT_MODEL
        attempt = summary.attempt
        assert attempt.model_id == ALT_MODEL
        assert attempt.cli_args_json["model"] == ALT_MODEL

    def test_section_default_caller_keeps_configured_model(self, tmp_path):
        config = model_config(tmp_path)
        recording, _transcript, sections = self._split()
        from test_section_summary_service import ScriptedLLM

        llm = ScriptedLLM([final_summary_json()])
        result = summarize_section_one(config, sections[0], llm_call=llm)
        assert result["result"] == "summarized"
        summary = Summary.objects.get(pk=result["summary_id"])
        assert summary.model_id == DEFAULT_MODEL

    def test_section_arbitrary_override_rejected(self, tmp_path):
        config = model_config(tmp_path)
        recording, _transcript, sections = self._split()
        with pytest.raises(ConfigError):
            summarize_section_one(config, sections[0], model="evil-model")
        assert not Summary.objects.filter(recording=recording).exists()
