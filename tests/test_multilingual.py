"""Tests for multilingual summary variants.

Covers: language resolution, SummaryVariantState, output_language
persistence, target-aware validation, prompt parameterization, tag
isolation, and CLI/web language selection.
"""
from __future__ import annotations

import json

import pytest
from django.test import TestCase
from django.utils import timezone

from workflow.models import (
    AttemptOutcome,
    Summary,
    SummaryState,
    SummaryVariantState,
)
from workflow.services.summarize import (
    _detect_source_language,
    _final_system_prompt,
    _language_instruction,
    _map_system_prompt,
    _validate_language_consistency,
    config_fingerprint,
    persist_summary,
    reconcile_recording_summary_state,
    resolve_default_language,
    resolve_output_language,
)
from factories import (
    make_summary_version,
    make_transcribed_recording,
)
from test_summarize import (
    ScriptedLLM,
    final_summary_json,
    make_config,
    make_llm_config,
    make_running_attempt,
    tags_config,
)


# ---------------------------------------------------------------------------
# Source language resolution
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSourceLanguageResolution:
    def test_from_confirmed_cantonese_routing(self):
        recording, transcript, _ = make_transcribed_recording(["hello"])
        from workflow.models import RoutingDecision
        RoutingDecision.objects.create(
            recording=recording, ordinal=1,
            route_suggestion="cantonese", profile_name="cantonese",
            model_id="apple:zh-HK", method="manual",
            routing_verified=True, is_active=True,
        )
        assert resolve_default_language(transcript) == "zh-Hant"

    def test_from_confirmed_mandarin_routing(self):
        recording, transcript, _ = make_transcribed_recording(["hello"])
        from workflow.models import RoutingDecision
        RoutingDecision.objects.create(
            recording=recording, ordinal=1,
            route_suggestion="mandarin", profile_name="mandarin",
            model_id="apple:zh-CN", method="manual",
            routing_verified=True, is_active=True,
        )
        assert resolve_default_language(transcript) == "zh-Hant"

    def test_from_european_routing_no_specific_language(self):
        recording, transcript, _ = make_transcribed_recording(["hello"])
        from workflow.models import RoutingDecision
        RoutingDecision.objects.create(
            recording=recording, ordinal=1,
            route_suggestion="european", profile_name="european",
            model_id="parakeet-pro:nvidia_parakeet-v3", method="automatic",
            routing_verified=False, is_active=True,
        )
        # European without source language → en
        assert resolve_default_language(transcript) == "en"

    def test_from_transcript_populated_by_llm(self):
        recording, transcript, _ = make_transcribed_recording(["hello"])
        transcript.language_observed = "fi"
        transcript.language_observed_verified_by = "llm_detection"
        transcript.save(update_fields=["language_observed", "language_observed_verified_by"])
        assert resolve_default_language(transcript) == "en"

    def test_from_transcript_chinese(self):
        recording, transcript, _ = make_transcribed_recording(["hello"])
        transcript.language_observed = "zh-HK"
        transcript.save(update_fields=["language_observed"])
        assert resolve_default_language(transcript) == "zh-Hant"

    def test_user_correction_outranks_routing(self):
        recording, transcript, _ = make_transcribed_recording(["hello"])
        from workflow.models import RoutingDecision
        RoutingDecision.objects.create(
            recording=recording, ordinal=1,
            route_suggestion="european", profile_name="european",
            model_id="parakeet-pro:nvidia_parakeet-v3", method="automatic",
            routing_verified=False, is_active=True,
        )
        transcript.language_observed = "fi"
        transcript.language_observed_verified_by = "user"
        transcript.save(update_fields=[
            "language_observed", "language_observed_verified_by",
        ])
        assert resolve_default_language(transcript) == "en"

    def test_empty_falls_back_to_en(self):
        recording, transcript, _ = make_transcribed_recording(["hello"])
        assert resolve_default_language(transcript) == "en"


# ---------------------------------------------------------------------------
# Output language resolution
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOutputLanguageResolution:
    def test_default_non_chinese(self):
        recording, transcript, _ = make_transcribed_recording(["hello"])
        assert resolve_output_language(transcript, "default") == "en"

    def test_default_chinese(self):
        recording, transcript, _ = make_transcribed_recording(["hello"])
        from workflow.models import RoutingDecision
        RoutingDecision.objects.create(
            recording=recording, ordinal=1,
            route_suggestion="cantonese", profile_name="cantonese",
            model_id="apple:zh-HK", method="manual",
            routing_verified=True, is_active=True,
        )
        assert resolve_output_language(transcript, "default") == "zh-Hant"

    def test_original_english(self):
        recording, transcript, _ = make_transcribed_recording(["hello"])
        transcript.language_observed = "en"
        transcript.save(update_fields=["language_observed"])
        assert resolve_output_language(transcript, "original") == "en"

    def test_original_finnish(self):
        recording, transcript, _ = make_transcribed_recording(["hello"])
        transcript.language_observed = "fi"
        transcript.save(update_fields=["language_observed"])
        assert resolve_output_language(transcript, "original") == "fi"

    def test_original_chinese_maps_to_zh_hant(self):
        recording, transcript, _ = make_transcribed_recording(["hello"])
        transcript.language_observed = "zh-HK"
        transcript.save(update_fields=["language_observed"])
        assert resolve_output_language(transcript, "original") == "zh-Hant"

    def test_explicit_en(self):
        recording, transcript, _ = make_transcribed_recording(["hello"])
        assert resolve_output_language(transcript, "en") == "en"

    def test_explicit_zh_hant(self):
        recording, transcript, _ = make_transcribed_recording(["hello"])
        assert resolve_output_language(transcript, "zh-Hant") == "zh-Hant"

    def test_original_unknown_source_returns_empty(self):
        recording, transcript, _ = make_transcribed_recording(["hello"])
        # No language_observed set
        assert resolve_output_language(transcript, "original") == ""

    def test_original_unknown_chinese_source_resolves_zh_hant_after_correction(self):
        recording, transcript, _ = make_transcribed_recording(["hello"])
        transcript.language_observed = "YUE-HK"  # raw model casing
        transcript.save(update_fields=["language_observed"])
        assert resolve_output_language(transcript, "original") == "zh-Hant"

    def test_deduplication_default_equals_en(self):
        """English transcript: default/original/en all resolve to en."""
        recording, transcript, _ = make_transcribed_recording(["hello"])
        transcript.language_observed = "en"
        transcript.save(update_fields=["language_observed"])
        assert resolve_output_language(transcript, "default") == "en"
        assert resolve_output_language(transcript, "original") == "en"
        assert resolve_output_language(transcript, "en") == "en"

    def test_deduplication_chinese_all_to_zh_hant(self):
        """Chinese transcript: default/original/zh-Hant all resolve to zh-Hant."""
        recording, transcript, _ = make_transcribed_recording(["hello"])
        from workflow.models import RoutingDecision
        RoutingDecision.objects.create(
            recording=recording, ordinal=1,
            route_suggestion="cantonese", profile_name="cantonese",
            model_id="apple:zh-HK", method="manual",
            routing_verified=True, is_active=True,
        )
        transcript.language_observed = "zh-HK"
        transcript.save(update_fields=["language_observed"])
        assert resolve_output_language(transcript, "default") == "zh-Hant"
        assert resolve_output_language(transcript, "original") == "zh-Hant"
        assert resolve_output_language(transcript, "zh-Hant") == "zh-Hant"

    def test_finnish_three_distinct_variants(self):
        """Finnish transcript: default→en, original→fi, zh-Hant→zh-Hant."""
        recording, transcript, _ = make_transcribed_recording(["hello"])
        transcript.language_observed = "fi"
        transcript.save(update_fields=["language_observed"])
        assert resolve_output_language(transcript, "default") == "en"
        assert resolve_output_language(transcript, "original") == "fi"
        assert resolve_output_language(transcript, "zh-Hant") == "zh-Hant"


# ---------------------------------------------------------------------------
# SummaryVariantState
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSummaryVariantState:
    def test_unique_constraint(self):
        recording, transcript, section = make_transcribed_recording(["hello"])
        SummaryVariantState.objects.create(
            transcript=transcript, section=section,
            output_language="en", status="current",
        )
        with pytest.raises(Exception):
            SummaryVariantState.objects.create(
                transcript=transcript, section=section,
                output_language="en", status="current",
            )

    def test_different_output_languages_coexist(self):
        recording, transcript, section = make_transcribed_recording(["hello"])
        SummaryVariantState.objects.create(
            transcript=transcript, section=section,
            output_language="en", status="current",
        )
        SummaryVariantState.objects.create(
            transcript=transcript, section=section,
            output_language="zh-Hant", status="missing",
        )
        assert SummaryVariantState.objects.filter(transcript=transcript).count() == 2

    def test_status_transitions(self):
        recording, transcript, section = make_transcribed_recording(["hello"])
        vs = SummaryVariantState.objects.create(
            transcript=transcript, section=section,
            output_language="en", status="missing",
        )
        assert vs.status == "missing"
        vs.status = SummaryVariantState.VariantStatus.CURRENT
        vs.save()
        vs.refresh_from_db()
        assert vs.status == "current"

    def test_regeneration_failed_semantics(self):
        recording, transcript, section = make_transcribed_recording(["hello"])
        vs = SummaryVariantState.objects.create(
            transcript=transcript, section=section,
            output_language="en", status="current",
            regeneration_failed=True,
        )
        assert vs.status == "current"
        assert vs.regeneration_failed is True

    def test_belongs_to_specific_transcript(self):
        recording_a, transcript_a, section_a = make_transcribed_recording(["a"])
        recording_b, transcript_b, section_b = make_transcribed_recording(["b"])
        SummaryVariantState.objects.create(
            transcript=transcript_a, section=section_a,
            output_language="en", status="current",
        )
        assert SummaryVariantState.objects.filter(transcript=transcript_a).count() == 1
        assert SummaryVariantState.objects.filter(transcript=transcript_b).count() == 0


# ---------------------------------------------------------------------------
# Language validation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLanguageValidation:
    def test_en_rejects_chinese(self):
        from workflow.services.llm import LLMInvalid
        payload = {
            "title": "教育研究",
            "overview": "本次會議討論了研究方法和統計分析的相關問題。",
            "key_points": [{"text": "不顯著結果不代表沒有效果", "level": 1}],
            "action_items": [],
        }
        with pytest.raises(LLMInvalid) as exc:
            _validate_language_consistency(payload, output_language="en")
        assert exc.value.code == "language_mismatch"

    def test_zh_hant_rejects_english(self):
        from workflow.services.llm import LLMInvalid
        payload = {
            "title": "Research meeting",
            "overview": "The team discussed statistical methods and research design.",
            "key_points": [{"text": "Results were not significant", "level": 1}],
            "action_items": [],
        }
        with pytest.raises(LLMInvalid) as exc:
            _validate_language_consistency(payload, output_language="zh-Hant")
        assert exc.value.code == "language_mismatch"

    def test_en_allows_small_foreign_phrases(self):
        payload = {
            "title": "Meeting with 王明",
            "overview": "Discussion about research methods.",
            "key_points": [{"text": "王明 presented the plan", "level": 1}],
            "action_items": [],
        }
        assert _validate_language_consistency(payload, output_language="en") is payload

    def test_original_accepts_any(self):
        payload = {
            "title": "任意语言",
            "overview": "任何语言都可以。",
            "key_points": [{"text": "测试", "level": 1}],
            "action_items": [],
        }
        assert _validate_language_consistency(payload, output_language="original") is payload


# ---------------------------------------------------------------------------
# Prompt parameterization
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPromptParameterization:
    def test_english_prompt(self):
        prompt = _final_system_prompt([], output_language="en")
        assert "English" in prompt
        assert "Write ALL summary prose" in prompt

    def test_zh_hant_prompt(self):
        prompt = _final_system_prompt([], output_language="zh-Hant")
        assert "Traditional Chinese" in prompt
        assert "繁體中文" in prompt

    def test_map_prompt_parameterized(self):
        prompt = _map_system_prompt(output_language="en")
        assert "English" in prompt

    def test_config_fingerprint_includes_output_language(self, tmp_path):
        config = make_config(tmp_path, llm=make_llm_config(tmp_path).llm)
        fp1 = config_fingerprint(config, [], output_language="en")
        fp2 = config_fingerprint(config, [], output_language="zh-Hant")
        assert fp1 != fp2


# ---------------------------------------------------------------------------
# persist_summary with output_language
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPersistSummaryMultilingual:
    def test_output_language_stored(self):
        recording, transcript, section = make_transcribed_recording(["hello"])
        attempt = make_running_attempt(recording)
        payload = {
            "title": "Test", "overview": "Test overview",
            "key_points": [], "action_items": [],
            "people": [], "organizations": [], "topics": [],
            "language": "en", "suggested": [], "rejected": [],
        }
        summary = persist_summary(
            recording=recording, transcript=transcript, section=section,
            attempt=attempt, payload=payload, output_language="en",
            is_default=True, model_id="m", base_url="u",
            prompt_version="2", fingerprint="f", chunk_count=1,
            input_characters=100, limits_used={}, generation_mode="manual",
        )
        assert summary.output_language == "en"
        assert summary.language == "en"

    def test_same_language_superseded(self):
        recording, transcript, section = make_transcribed_recording(["hello"])
        s1 = make_summary_version(recording, transcript, section, output_language="en")
        assert s1.is_active
        attempt = make_running_attempt(recording)
        payload = {
            "title": "Test 2", "overview": "Test overview 2",
            "key_points": [], "action_items": [],
            "people": [], "organizations": [], "topics": [],
            "language": "en", "suggested": [], "rejected": [],
        }
        s2 = persist_summary(
            recording=recording, transcript=transcript, section=section,
            attempt=attempt, payload=payload, output_language="en",
            is_default=True, model_id="m", base_url="u",
            prompt_version="2", fingerprint="f", chunk_count=1,
            input_characters=100, limits_used={}, generation_mode="manual",
        )
        s1.refresh_from_db()
        assert s1.is_active is False
        assert s2.is_active is True

    def test_different_languages_coexist(self):
        recording, transcript, section = make_transcribed_recording(["hello"])
        s_en = make_summary_version(recording, transcript, section, output_language="en")
        s_zh = make_summary_version(recording, transcript, section, output_language="zh-Hant")
        assert s_en.is_active is True
        assert s_zh.is_active is True

    def test_variant_state_updated_on_persist(self):
        recording, transcript, section = make_transcribed_recording(["hello"])
        attempt = make_running_attempt(recording)
        payload = {
            "title": "Test", "overview": "Test overview",
            "key_points": [], "action_items": [],
            "people": [], "organizations": [], "topics": [],
            "language": "en", "suggested": [], "rejected": [],
        }
        persist_summary(
            recording=recording, transcript=transcript, section=section,
            attempt=attempt, payload=payload, output_language="en",
            is_default=True, model_id="m", base_url="u",
            prompt_version="2", fingerprint="f", chunk_count=1,
            input_characters=100, limits_used={}, generation_mode="manual",
        )
        vs = SummaryVariantState.objects.get(
            transcript=transcript, section=section, output_language="en",
        )
        assert vs.status == "current"
        assert vs.regeneration_failed is False

    def test_tags_materialized_only_for_default(self, tmp_path):
        recording, transcript, section = make_transcribed_recording(["hello"])
        from workflow.services.tags import sync_tags
        config = make_config(
            tmp_path, llm=make_llm_config(tmp_path).llm,
            tags=tags_config("Family"),
        )
        sync_tags(config)
        attempt = make_running_attempt(recording)
        from workflow.models import Tag
        tag = Tag.objects.filter(name_key="family").first()
        payload = {
            "title": "Test", "overview": "Test overview",
            "key_points": [], "action_items": [],
            "people": [], "organizations": [], "topics": [],
            "language": "en", "suggested": [tag] if tag else [], "rejected": [],
        }
        persist_summary(
            recording=recording, transcript=transcript, section=section,
            attempt=attempt, payload=payload, output_language="en",
            is_default=True, model_id="m", base_url="u",
            prompt_version="2", fingerprint="f", chunk_count=1,
            input_characters=100, limits_used={}, generation_mode="manual",
        )
        from workflow.models import TagAssignment
        assert TagAssignment.objects.filter(recording=recording, is_active=True).exists()

    def test_optional_variant_does_not_materialize_tags(self, tmp_path):
        recording, transcript, section = make_transcribed_recording(["hello"])
        from workflow.services.tags import sync_tags
        config = make_config(
            tmp_path, llm=make_llm_config(tmp_path).llm,
            tags=tags_config("Family"),
        )
        sync_tags(config)
        attempt = make_running_attempt(recording)
        from workflow.models import Tag
        tag = Tag.objects.filter(name_key="family").first()
        payload = {
            "title": "Test", "overview": "Test overview",
            "key_points": [], "action_items": [],
            "people": [], "organizations": [], "topics": [],
            "language": "en", "suggested": [tag] if tag else [], "rejected": [],
        }
        persist_summary(
            recording=recording, transcript=transcript, section=section,
            attempt=attempt, payload=payload, output_language="zh-Hant",
            is_default=False, model_id="m", base_url="u",
            prompt_version="2", fingerprint="f", chunk_count=1,
            input_characters=100, limits_used={}, generation_mode="manual",
        )
        from workflow.models import TagAssignment
        assert not TagAssignment.objects.filter(recording=recording, is_active=True).exists()


# ---------------------------------------------------------------------------
# summarize_one with target_language
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSummarizeOneMultilingual:
    def test_default_generates_en(self, tmp_path):
        from workflow.services import summarize as summarize_service
        config = make_config(
            tmp_path,
            llm=make_llm_config(tmp_path).llm,
            tags=tags_config("Family"),
        )
        recording, transcript, _ = make_transcribed_recording(["hello world"])
        llm = ScriptedLLM([final_summary_json()])
        result = summarize_service.summarize_one(
            config, recording, target_language="default", llm_call=llm,
        )
        assert result["result"] == "summarized"
        assert result["output_language"] == "en"

    def test_explicit_en_generates_en(self, tmp_path):
        from workflow.services import summarize as summarize_service
        config = make_config(
            tmp_path,
            llm=make_llm_config(tmp_path).llm,
            tags=tags_config("Family"),
        )
        recording, transcript, _ = make_transcribed_recording(["hello world"])
        llm = ScriptedLLM([final_summary_json()])
        result = summarize_service.summarize_one(
            config, recording, target_language="en", llm_call=llm,
        )
        assert result["result"] == "summarized"
        assert result["output_language"] == "en"

    def test_explicit_zh_hant_generates_zh_hant(self, tmp_path):
        from workflow.services import summarize as summarize_service
        config = make_config(
            tmp_path,
            llm=make_llm_config(tmp_path).llm,
            tags=tags_config("Family"),
        )
        recording, transcript, _ = make_transcribed_recording(["hello world"])
        # Provide Chinese content for zh-Hant target
        zh_payload = final_summary_json(
            title="會議記錄",
            overview="討論了評分計劃。",
            key_points=[{"text": "評分將於週一開始", "level": 1}],
            language="zh-HK",
        )
        llm = ScriptedLLM([zh_payload, zh_payload])
        result = summarize_service.summarize_one(
            config, recording, target_language="zh-Hant", llm_call=llm,
        )
        assert result["result"] == "summarized"
        assert result["output_language"] == "zh-Hant"

    def test_variant_coexistence(self, tmp_path):
        from workflow.services import summarize as summarize_service
        config = make_config(
            tmp_path,
            llm=make_llm_config(tmp_path).llm,
            tags=tags_config("Family"),
        )
        recording, transcript, _ = make_transcribed_recording(["hello world"])
        # Generate English
        llm = ScriptedLLM([final_summary_json(), final_summary_json()])
        summarize_service.summarize_one(
            config, recording, target_language="en", llm_call=llm,
        )
        # Generate Chinese (should not replace English)
        zh_payload = final_summary_json(
            title="會議記錄",
            overview="討論了評分計劃。",
            key_points=[{"text": "評分將於週一開始", "level": 1}],
            language="zh-HK",
        )
        llm = ScriptedLLM([zh_payload, zh_payload])
        result = summarize_service.summarize_one(
            config, recording, target_language="zh-Hant", llm_call=llm,
        )
        assert result["result"] == "summarized"
        # Both should be active
        active = Summary.objects.filter(
            transcript=transcript, is_active=True,
        )
        assert active.count() == 2
        languages = set(active.values_list("output_language", flat=True))
        assert languages == {"en", "zh-Hant"}

    def test_skip_if_variant_exists(self, tmp_path):
        from workflow.services import summarize as summarize_service
        config = make_config(
            tmp_path,
            llm=make_llm_config(tmp_path).llm,
            tags=tags_config("Family"),
        )
        recording, transcript, _ = make_transcribed_recording(["hello world"])
        llm = ScriptedLLM([final_summary_json()])
        summarize_service.summarize_one(
            config, recording, target_language="en", llm_call=llm,
        )
        # Second call should skip
        result = summarize_service.summarize_one(
            config, recording, target_language="en", llm_call=ScriptedLLM([]),
        )
        assert result["result"] == "skipped"
        assert result["reason"] == "variant_current"

    def test_regenerate_replaces_same_language(self, tmp_path):
        from workflow.services import summarize as summarize_service
        config = make_config(
            tmp_path,
            llm=make_llm_config(tmp_path).llm,
            tags=tags_config("Family"),
        )
        recording, transcript, section = make_transcribed_recording(["hello world"])
        llm = ScriptedLLM([final_summary_json(), final_summary_json()])
        summarize_service.summarize_one(
            config, recording, target_language="en", llm_call=llm,
        )
        first = Summary.objects.filter(
            transcript=transcript, output_language="en", is_active=True,
        ).first()
        # Regenerate
        llm = ScriptedLLM([final_summary_json(title="New title"), final_summary_json(title="New title")])
        result = summarize_service.summarize_one(
            config, recording, target_language="en",
            regenerate=True, llm_call=llm,
        )
        assert result["result"] == "summarized"
        first.refresh_from_db()
        assert first.is_active is False
        new = Summary.objects.filter(
            transcript=transcript, output_language="en", is_active=True,
        ).first()
        assert new is not None
        assert new.pk != first.pk


    def test_unsupported_generation_selector_rejected(self, tmp_path):
        """Generation accepts ONLY default/original/en/zh-Hant — an
        arbitrary concrete language (fi) can never create a variant."""
        from brainlib.config import ConfigError

        from workflow.services import summarize as summarize_service
        from workflow.services.languages import GENERATION_SELECTORS

        config = make_config(
            tmp_path, llm=make_llm_config(tmp_path).llm,
        )
        recording, _transcript, _section = make_transcribed_recording(["hello world"])
        with pytest.raises(ConfigError, match="unsupported generation target"):
            summarize_service.summarize_one(
                config, recording, target_language="fi", llm_call=ScriptedLLM([]),
            )
        assert "default" in GENERATION_SELECTORS and "original" in GENERATION_SELECTORS


# ---------------------------------------------------------------------------
# Final-payload source-language canonicalization (persistence boundary)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFinalPayloadSourceLanguage:
    """The model-reported source language is canonicalized before
    storage; malformed values follow the invalid-output retry policy."""

    def _payload(self, language):
        return {
            "title": "Meeting", "overview": "Discussed plans.",
            "key_points": [{"text": "Point", "level": 1}],
            "action_items": [], "people": [], "organizations": [],
            "topics": [], "suggested_tags": [], "language": language,
        }

    def _validate(self, language):
        from workflow.services.summarize import validate_final_payload

        return validate_final_payload(self._payload(language), {})

    def test_mixed_case_codes_canonicalized(self):
        assert self._validate("FI")["language"] == "fi"
        assert self._validate("en-us")["language"] == "en-US"
        assert self._validate("yue-hk")["language"] == "yue-HK"
        assert self._validate("zh-hant")["language"] == "zh-Hant"

    def test_empty_allowed_only_as_unknown_source(self):
        # Documented rule: empty stays empty (unknown source), never an error.
        assert self._validate("")["language"] == ""
        assert self._validate("   ")["language"] == ""

    def test_malformed_code_rejected_with_stable_code(self):
        from workflow.services.llm import LLMInvalid

        with pytest.raises(LLMInvalid) as exc:
            self._validate("INVALID!")
        assert exc.value.code == "schema_validation"

    def test_malformed_code_gets_invalid_output_retry(self, tmp_path):
        """A malformed language value counts as invalid model output:
        exactly one retry, then the valid response wins."""
        from workflow.services import summarize as summarize_service

        config = make_config(tmp_path, llm=make_llm_config(tmp_path).llm)
        recording, transcript, _section = make_transcribed_recording(["hello world"])
        bad = json.dumps(self._payload("INVALID!"))
        good = json.dumps(self._payload("FI"))
        llm = ScriptedLLM([bad, good])
        result = summarize_service.summarize_one(
            config, recording, target_language="en", llm_call=llm,
        )
        assert result["result"] == "summarized"
        assert llm.call_count == 2  # exactly one retry

    def test_persistence_receives_canonical_value(self, tmp_path):
        """Summary.language AND Transcript.language_observed both get the
        same canonical value — never raw mixed-case model output."""
        from workflow.services import summarize as summarize_service

        config = make_config(tmp_path, llm=make_llm_config(tmp_path).llm)
        recording, transcript, _section = make_transcribed_recording(["hello world"])
        llm = ScriptedLLM([json.dumps(self._payload("FI"))])
        result = summarize_service.summarize_one(
            config, recording, target_language="en", llm_call=llm,
        )
        assert result["result"] == "summarized"
        summary = Summary.objects.get(recording=recording, is_active=True)
        assert summary.language == "fi"
        transcript.refresh_from_db()
        assert transcript.language_observed == "fi"

    def test_malformed_language_never_persisted(self, tmp_path):
        from workflow.services import summarize as summarize_service
        from workflow.services.llm import LLMUnavailable

        config = make_config(tmp_path, llm=make_llm_config(tmp_path).llm)
        recording, transcript, _section = make_transcribed_recording(["hello world"])
        llm = ScriptedLLM([json.dumps(self._payload("INVALID!")), LLMUnavailable("down")])
        result = summarize_service.summarize_one(
            config, recording, target_language="en", llm_call=llm,
        )
        assert result["result"] == "failed"
        assert result["error_code"] == "endpoint_unavailable"
        assert not Summary.objects.filter(recording=recording).exists()
        transcript.refresh_from_db()
        assert transcript.language_observed == ""


@pytest.mark.django_db
class TestSourceLanguageProvenance:
    """One deterministic provenance rule: a known Transcript source is
    authoritative for the generated Summary; the model's own language
    value only fills the genuinely unknown case."""

    def _payload(self, language):
        return {
            "title": "Meeting", "overview": "Discussed plans.",
            "key_points": [{"text": "Point", "level": 1}],
            "action_items": [], "people": [], "organizations": [],
            "topics": [], "suggested_tags": [], "language": language,
        }

    def _validate(self, language, source_language):
        from workflow.services.summarize import validate_final_payload

        return validate_final_payload(
            self._payload(language), {}, source_language=source_language
        )

    def test_known_source_overrides_empty_model_value(self):
        assert self._validate("", "fi")["language"] == "fi"

    def test_known_source_overrides_contradictory_model_value(self):
        assert self._validate("en", "fi")["language"] == "fi"

    def test_known_source_overrides_malformed_model_value(self):
        # The Transcript value is authoritative: a malformed model
        # answer is ignored, not retried.
        assert self._validate("INVALID!", "fi")["language"] == "fi"

    def test_unknown_source_canonicalizes_valid_model_value(self):
        assert self._validate("FI", "")["language"] == "fi"

    def test_unknown_source_empty_stays_unknown(self):
        assert self._validate("", "")["language"] == ""

    def test_unknown_source_malformed_rejected(self):
        from workflow.services.llm import LLMInvalid

        with pytest.raises(LLMInvalid) as exc:
            self._validate("INVALID!", "")
        assert exc.value.code == "schema_validation"

    def _summarize(self, tmp_path, llm, *, target_language="en", setup=None):
        from workflow.services import summarize as summarize_service

        config = make_config(tmp_path, llm=make_llm_config(tmp_path).llm)
        recording, transcript, _section = make_transcribed_recording(["hello world"])
        if setup is not None:
            setup(transcript)
        result = summarize_service.summarize_one(
            config, recording, target_language=target_language, llm_call=llm,
        )
        return recording, transcript, result

    def test_known_fi_with_empty_model_language_stays_fi(self, tmp_path):
        recording, transcript, result = self._summarize(
            tmp_path,
            ScriptedLLM([json.dumps(self._payload(""))]),
            setup=lambda t: setattr(t, "language_observed", "fi")
            or t.save(update_fields=["language_observed"]),
        )
        assert result["result"] == "summarized"
        summary = Summary.objects.get(recording=recording, is_active=True)
        assert summary.language == "fi"
        transcript.refresh_from_db()
        assert transcript.language_observed == "fi"
        assert transcript.language_observed_verified_by == ""

    def test_known_fi_with_contradictory_model_language_stays_fi(self, tmp_path):
        recording, transcript, result = self._summarize(
            tmp_path,
            ScriptedLLM([json.dumps(self._payload("en"))]),
            setup=lambda t: setattr(t, "language_observed", "fi")
            or t.save(update_fields=["language_observed"]),
        )
        assert result["result"] == "summarized"
        summary = Summary.objects.get(recording=recording, is_active=True)
        assert summary.language == "fi"
        transcript.refresh_from_db()
        assert transcript.language_observed == "fi"

    def test_user_verified_fi_is_never_displaced(self, tmp_path):
        def setup(transcript):
            transcript.language_observed = "fi"
            transcript.language_observed_verified_by = "user"
            transcript.save(update_fields=[
                "language_observed", "language_observed_verified_by",
            ])

        recording, transcript, result = self._summarize(
            tmp_path,
            ScriptedLLM([json.dumps(self._payload("en"))]),
            setup=setup,
        )
        assert result["result"] == "summarized"
        summary = Summary.objects.get(recording=recording, is_active=True)
        assert summary.language == "fi"
        transcript.refresh_from_db()
        assert transcript.language_observed == "fi"
        assert transcript.language_observed_verified_by == "user"

    def test_original_detection_then_empty_model_language_is_consistent(
        self, tmp_path
    ):
        """Explicit Original: detection establishes `fi`, then the summary
        response with an empty/conflicting language still yields `fi`
        provenance everywhere."""
        from workflow.services import summarize as summarize_service

        config = make_config(tmp_path, llm=make_llm_config(tmp_path).llm)
        recording, transcript, _section = make_transcribed_recording(["hello world"])
        llm = ScriptedLLM([
            json.dumps({"language": "fi"}),  # detection
            json.dumps(self._payload("")),   # summary: empty model language
            json.dumps(self._payload("")),   # (retry allowance unused)
        ])
        result = summarize_service.summarize_one(
            config, recording, target_language="original", llm_call=llm,
        )
        assert result["result"] == "summarized"
        assert result["output_language"] == "fi"
        summary = Summary.objects.get(recording=recording, is_active=True)
        assert summary.language == "fi"
        transcript.refresh_from_db()
        assert transcript.language_observed == "fi"
        assert transcript.language_observed_verified_by == "llm_detection"

    def test_original_detection_then_conflicting_model_language_is_consistent(
        self, tmp_path
    ):
        from workflow.services import summarize as summarize_service

        config = make_config(tmp_path, llm=make_llm_config(tmp_path).llm)
        recording, transcript, _section = make_transcribed_recording(["hello world"])
        llm = ScriptedLLM([
            json.dumps({"language": "fi"}),
            json.dumps(self._payload("zh-HK")),  # conflicting model answer
        ])
        result = summarize_service.summarize_one(
            config, recording, target_language="original", llm_call=llm,
        )
        assert result["result"] == "summarized"
        summary = Summary.objects.get(recording=recording, is_active=True)
        assert summary.language == "fi"
        transcript.refresh_from_db()
        assert transcript.language_observed == "fi"

    def test_unknown_source_empty_remains_unknown(self, tmp_path):
        recording, transcript, result = self._summarize(
            tmp_path,
            ScriptedLLM([json.dumps(self._payload(""))]),
        )
        assert result["result"] == "summarized"
        summary = Summary.objects.get(recording=recording, is_active=True)
        assert summary.language == ""
        transcript.refresh_from_db()
        assert transcript.language_observed == ""

    def test_unknown_source_malformed_gets_exactly_one_retry(self, tmp_path):
        recording, transcript, result = self._summarize(
            tmp_path,
            ScriptedLLM([
                json.dumps(self._payload("INVALID!")),
                json.dumps(self._payload("FI")),
            ]),
        )
        assert result["result"] == "summarized"
        summary = Summary.objects.get(recording=recording, is_active=True)
        assert summary.language == "fi"
        transcript.refresh_from_db()
        assert transcript.language_observed == "fi"


# ---------------------------------------------------------------------------
# Reconciler output-language identity validation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestReconcilerOutputIdentityValidation:
    """The reconciler rejects malformed, noncanonical and source-style
    Chinese output identities with no writes of any kind."""

    def _recording(self):
        return make_transcribed_recording(["hello"])

    def _state_snapshot(self, recording):
        recording.refresh_from_db()
        return (
            recording.summary_status,
            recording.resummarization_failed,
            recording.last_failed_attempt_id,
            list(SummaryVariantState.objects.values_list("output_language", "status")),
        )

    def _reject(self, recording, transcript, section, identity):
        from workflow.services.variant_state import VariantScopeError, reconcile_variant_state

        before = self._state_snapshot(recording)
        with pytest.raises(VariantScopeError, match="invalid output-language identity"):
            reconcile_variant_state(
                recording=recording, transcript=transcript,
                section=section, output_language=identity,
            )
        assert self._state_snapshot(recording) == before  # no partial writes

    def test_malformed_identity_rejected(self):
        recording, transcript, section = self._recording()
        self._reject(recording, transcript, section, "INVALID!")

    def test_noncanonical_identity_rejected(self):
        recording, transcript, section = self._recording()
        self._reject(recording, transcript, section, "FI")

    def test_chinese_source_style_output_identity_rejected(self):
        for identity in ("yue-HK", "yue", "cmn", "zh-HK", "zh-CN", "zh"):
            recording, transcript, section = self._recording()
            self._reject(recording, transcript, section, identity)

    def test_und_is_not_a_runtime_output_identity(self):
        recording, transcript, section = self._recording()
        self._reject(recording, transcript, section, "und")

    def test_canonical_non_chinese_identity_accepted(self):
        from workflow.services.variant_state import reconcile_variant_state

        recording, transcript, section = self._recording()
        vs = reconcile_variant_state(
            recording=recording, transcript=transcript,
            section=section, output_language="fi",
        )
        assert vs.status == SummaryVariantState.VariantStatus.MISSING

    def test_canonical_zh_hant_identity_accepted(self):
        from workflow.services.variant_state import reconcile_variant_state

        recording, transcript, section = self._recording()
        vs = reconcile_variant_state(
            recording=recording, transcript=transcript,
            section=section, output_language="zh-Hant",
        )
        assert vs.status == SummaryVariantState.VariantStatus.MISSING

    def test_recovery_ignores_attempts_with_invalid_resolved_identity(self, tmp_path):
        """Provenance with scope ids but a malformed resolved value is
        never attributed: no variant rows, no recording changes, and the
        recovery report surfaces a distinct diagnostic."""
        from workflow.services.pipeline import recover_interruptions

        from factories import make_config

        config = make_config(tmp_path)
        recording, transcript, section = make_transcribed_recording(["hello"])
        attempt = make_running_attempt(recording)
        attempt.context_json = {
            "language": {
                "requested": "default", "resolved": "INVALID!",
                "is_default": True,
                "transcript_id": transcript.pk, "section_id": section.pk,
            },
        }
        attempt.save()

        recoveries = recover_interruptions(config)

        assert (
            recoveries["summary_reconciliation"]["invalid_output_identity"] == 1
        )
        assert not SummaryVariantState.objects.exists()
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.MISSING
        assert recording.last_failed_attempt_id is None


# ---------------------------------------------------------------------------
# current_summary with output_language
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCurrentSummaryLanguage:
    def test_default_returns_default_language(self):
        recording, transcript, section = make_transcribed_recording(["hello"])
        s_en = make_summary_version(recording, transcript, section, output_language="en")
        s_zh = make_summary_version(recording, transcript, section, output_language="zh-Hant")
        # Default should return en (or whatever resolve_default_language says)
        current = recording.current_summary()
        assert current is not None
        assert current.output_language == resolve_default_language(transcript)

    def test_specific_language_returns_that_variant(self):
        recording, transcript, section = make_transcribed_recording(["hello"])
        s_en = make_summary_version(recording, transcript, section, output_language="en")
        s_zh = make_summary_version(recording, transcript, section, output_language="zh-Hant")
        current_zh = recording.current_summary(output_language="zh-Hant")
        assert current_zh is not None
        assert current_zh.output_language == "zh-Hant"

    def test_nonexistent_language_returns_none(self):
        recording, transcript, section = make_transcribed_recording(["hello"])
        s_en = make_summary_version(recording, transcript, section, output_language="en")
        current = recording.current_summary(output_language="fi")
        assert current is None


# ---------------------------------------------------------------------------
# Recovery with SummaryVariantState
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRecoveryMultilingual:
    def test_recovery_updates_variant_state(self):
        recording, transcript, section = make_transcribed_recording(["hello"])
        vs = SummaryVariantState.objects.create(
            transcript=transcript, section=section,
            output_language="en", status="current",
        )
        attempt = make_running_attempt(recording)
        attempt.context_json = {
            "language": {
                "requested": "en",
                "resolved": "en",
                "source": "en",
                "is_default": True,
                "source_method": "",
                "transcript_id": transcript.pk,
                "section_id": section.pk,
            },
        }
        # Recovery closes the attempt as a finished failure BEFORE
        # reconciling; the reconciler only attributes FINISHED failures.
        attempt.outcome = AttemptOutcome.INTERRUPTED
        attempt.finished_at = timezone.now()
        attempt.save()
        reconcile_recording_summary_state(recording, recovered_attempt=attempt)
        vs.refresh_from_db()
        # No active summary → status should be failed
        assert vs.status == SummaryVariantState.VariantStatus.FAILED
        assert vs.last_failed_attempt_id == attempt.pk
