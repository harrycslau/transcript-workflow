"""Tests for tag suggestions, assignments, provenance and history preservation."""

from __future__ import annotations

import pytest

from brainlib.config import LLMConfig, TagSpec, TagsConfig
from workflow.models import Summary, Tag, TagAssignment, TagOrigin, SummaryState
from workflow.services import summarize as summarize_service
from workflow.services.tags import sync_tags

from factories import final_summary_json, make_config, make_transcribed_recording

pytestmark = pytest.mark.django_db


def tags_config(*names) -> TagsConfig:
    return TagsConfig(allowed=tuple(TagSpec(name=n, description=f"{n} description") for n in names))


def llm_config(tmp_path):
    return LLMConfig(
        provider="openai_compatible", base_url="http://127.0.0.1:1/v1", model="test-model",
        api_key_env="BRAIN_TEST_LLM_API_KEY", temperature=0.2, timeout_seconds=600,
    )


def make_llm(*responses):
    calls = []
    queue = list(responses)

    def llm_call(*, system, user):
        calls.append((system, user))
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    llm_call.calls = calls
    return llm_call


class TestSuggestionsAndProvenance:
    def _config(self, tmp_path):
        return make_config(tmp_path, llm=llm_config(tmp_path), tags=tags_config("Family", "Academic", "Unknown"))

    def test_multiple_suggested_tags_with_provenance(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = make_llm(final_summary_json(suggested_tags=["Academic", "Family"]))
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "summarized"
        assert sorted(result["tags"]) == ["Academic", "Family"]
        summary = recording.current_summary()
        suggestions = list(summary.tag_suggestions.order_by("tag__name"))
        assert [s.tag.name for s in suggestions] == ["Academic", "Family"]
        assert all(s.suggested_by_model for s in suggestions)
        # Effective assignments point back at the summary version.
        for assignment in TagAssignment.objects.filter(recording=recording, is_active=True):
            assert assignment.origin == TagOrigin.SUGGESTED
            assert assignment.source_summary_id == summary.pk

    def test_case_insensitive_matching_persists_display_name(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = make_llm(final_summary_json(suggested_tags=["aCaDeMic"]))
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["tags"] == ["Academic"]
        tag = Tag.objects.get(name_key="academic")
        assert TagAssignment.objects.get(recording=recording, tag=tag).is_active

    def test_unconfigured_suggestion_not_persisted_but_recorded(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = make_llm(final_summary_json(suggested_tags=["Academic", "Nonexistent"]))
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["tags"] == ["Academic"]
        raw = recording.current_summary().suggested_tags_raw
        assert raw["rejected"] == ["Nonexistent"]
        assert not Tag.objects.filter(name="Nonexistent").exists()

    def test_unknown_conflict_end_to_end(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = make_llm(final_summary_json(suggested_tags=["Academic", "Unknown"]))
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["tags"] == ["Academic"]
        assert not TagAssignment.objects.filter(recording=recording, tag__name_key="unknown").exists()

    def test_unknown_alone_persisted(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = make_llm(final_summary_json(suggested_tags=["Unknown"]))
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["tags"] == ["Unknown"]
        assert TagAssignment.objects.filter(recording=recording, tag__name_key="unknown", is_active=True).exists()

    def test_no_duplicate_effective_assignments(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = make_llm(final_summary_json(suggested_tags=["Academic", "ACADEMIC", "academic"]))
        summarize_service.summarize_one(config, recording, llm_call=llm)
        tag = Tag.objects.get(name_key="academic")
        assert TagAssignment.objects.filter(recording=recording, tag=tag).count() == 1


class TestRegenerationAndHistory:
    def _config(self, tmp_path):
        return make_config(tmp_path, llm=llm_config(tmp_path), tags=tags_config("Family", "Academic", "Unknown"))

    def test_manual_assignment_not_erased_by_regeneration(self, tmp_path):
        config = self._config(tmp_path)
        sync_tags(config)
        recording, _, _ = make_transcribed_recording(["hello world"])
        family = Tag.objects.get(name_key="family")
        # The user manually assigned Family before any summarization.
        manual = TagAssignment.objects.create(
            recording=recording, tag=family, origin=TagOrigin.MANUAL, is_active=True
        )
        summarize_service.summarize_one(
            config, recording, llm_call=make_llm(final_summary_json(suggested_tags=["Academic"]))
        )
        manual.refresh_from_db()
        assert manual.is_active is True
        assert manual.origin == TagOrigin.MANUAL
        assert manual.source_summary is None
        # Regeneration suggesting Family still leaves the manual row intact.
        summarize_service.summarize_one(
            config, recording, regenerate=True,
            llm_call=make_llm(final_summary_json(title="V2", suggested_tags=["Academic", "Family"])),
        )
        manual.refresh_from_db()
        assert manual.is_active is True
        assert manual.origin == TagOrigin.MANUAL
        assignments = TagAssignment.objects.filter(recording=recording, is_active=True)
        assert sorted(a.tag.name for a in assignments) == ["Academic", "Family"]

    def test_suggested_assignments_replaced_on_regeneration(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        summarize_service.summarize_one(
            config, recording, llm_call=make_llm(final_summary_json(suggested_tags=["Academic"]))
        )
        summarize_service.summarize_one(
            config, recording, regenerate=True,
            llm_call=make_llm(final_summary_json(title="V2", suggested_tags=["Family"])),
        )
        active = TagAssignment.objects.filter(recording=recording, is_active=True)
        assert [a.tag.name for a in active] == ["Family"]
        academic = Tag.objects.get(name_key="academic")
        old = TagAssignment.objects.get(recording=recording, tag=academic)
        assert old.is_active is False
        assert old.deactivated_at is not None
        # Provenance history: both summary versions recorded their suggestions.
        assert Summary.objects.count() == 2
        versions = {s.ordinal: sorted(s.tag_suggestions.values_list("tag__name", flat=True)) for s in Summary.objects.all()}
        assert versions[1] == ["Academic"]
        assert versions[2] == ["Family"]

    def test_retired_tag_keeps_historical_assignment(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        summarize_service.summarize_one(
            config, recording, llm_call=make_llm(final_summary_json(suggested_tags=["Academic"]))
        )
        # Remove Academic from configuration; sync retires it.
        retired_config = make_config(
            tmp_path, llm=llm_config(tmp_path), tags=tags_config("Family", "Unknown")
        )
        counts = sync_tags(retired_config)
        assert counts["retired"] == 1
        academic = Tag.objects.get(name_key="academic")
        assert academic.is_configured is False
        assignment = TagAssignment.objects.get(recording=recording, tag=academic)
        assert assignment.is_active is True  # history preserved
        # Retired tags are not offered to the model afterwards.
        # The prompt's allowed list excludes retired tags.
        from workflow.services.summarize import _final_system_prompt

        system_prompt = _final_system_prompt(list(Tag.objects.filter(is_configured=True)))
        assert "Academic" not in system_prompt
        assert "Family" in system_prompt

    def test_suggestion_for_manually_tagged_recording_is_provenance_only(self, tmp_path):
        config = self._config(tmp_path)
        sync_tags(config)
        recording, _, _ = make_transcribed_recording(["hello world"])
        family = Tag.objects.get(name_key="family")
        TagAssignment.objects.create(recording=recording, tag=family, origin=TagOrigin.MANUAL, is_active=True)
        summarize_service.summarize_one(
            config, recording, llm_call=make_llm(final_summary_json(suggested_tags=["Family", "Academic"]))
        )
        family_assignment = TagAssignment.objects.get(recording=recording, tag=family)
        assert family_assignment.origin == TagOrigin.MANUAL
        assert family_assignment.source_summary is None
