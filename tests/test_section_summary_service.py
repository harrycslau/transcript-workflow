"""Focused service tests for ``summarize_section_one`` (Step 6.2 section
summaries).

Proves the section summary contract on the CURRENT schema:

- summary input is ALL and ONLY the topic Section's canonical segment
  range (deterministic full stored text; never source audio, never
  ``text_normalized``);
- output variants/versioning reuse the existing machinery (one active
  Summary per (transcript, section, output_language); regeneration
  deactivates the old version);
- default-variant suggestions materialize as SECTION-scoped
  ``TagAssignment`` rows (never recording-scoped); non-default variants
  materialize nothing;
- section summary success/failure/regeneration NEVER changes the
  Recording-level default tuple (``summary_status``,
  ``resummarization_failed``, ``last_failed_attempt``), processing
  status, or the whole-recording summary;
- failed initial generation creates the section VariantState ``failed``;
  failed regeneration preserves the active section Summary and marks
  only that exact variant ``regeneration_failed``;
- NO recording search sync is scheduled for section summaries;
- target validation (fixed/historical/cross-parent/malformed-layout
  sections rejected with stable sanitized ``SegmentationError``
  categories) and exact-scope interruption recovery;
- whole-recording behavior regressions stay intact (recording-scoped
  tags and summaries are never touched by a section summary).
"""

from __future__ import annotations

import pytest
from django.utils import timezone as dj_timezone

from brainlib.config import ConfigError, LLMConfig, TagSpec, TagsConfig
from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    ProcessingAttempt,
    Section,
    SegmentedVersion,
    Summary,
    SummaryState,
    SummaryTagSuggestion,
    SummaryVariantState,
    Tag,
    TagAssignment,
)
from workflow.services import summarize as summarize_service
from workflow.services.llm import LLMTimeout, LLMUnavailable
from workflow.services.segmentation import (
    SegmentationError,
    save_segmented_version,
)
from workflow.services.summarize import (
    SummaryRelationError,
    persist_summary,
    reconcile_recording_summary_state,
    summarize_section_one,
)

from factories import (
    final_summary_json,
    make_config,
    make_tag,
    make_tag_assignment,
    make_transcribed_recording,
    map_summary_json,
)

pytestmark = pytest.mark.django_db


def llm_config(tmp_path):
    return LLMConfig(
        provider="openai_compatible", base_url="http://127.0.0.1:1/v1", model="test-model",
        api_key_env="BRAIN_TEST_LLM_API_KEY", temperature=0.2, timeout_seconds=600,
    )


def tags_config(*names) -> TagsConfig:
    return TagsConfig(
        allowed=tuple(TagSpec(name=n, description=f"{n} description") for n in names)
    )


def make_config_for(tmp_path, *, tags=("Family", "Academic", "Unknown"), **overrides):
    return make_config(
        tmp_path,
        llm=llm_config(tmp_path),
        tags=tags_config(*tags),
        **overrides,
    )


class ScriptedLLM:
    """Scripted llm_call: records prompts, returns queued responses."""

    def __init__(self, responses=None, handler=None):
        self.responses = list(responses or [])
        self.handler = handler
        self.calls: list[dict] = []

    def __call__(self, *, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        if self.handler is not None:
            return self.handler(system=system, user=user)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def user_prompts(self) -> list[str]:
        return [c["user"] for c in self.calls]


def split_recording(texts, splits, titles):
    """A transcribed recording with an active topic layout; returns
    (recording, transcript, topic_sections ordered by ordinal)."""
    recording, transcript, _fixed = make_transcribed_recording(texts)
    result = save_segmented_version(
        recording.pk, transcript.pk, 0, len(texts), splits, titles
    )
    version = SegmentedVersion.objects.get(pk=result.version_id)
    sections = list(
        Section.objects.filter(segmented_version=version).order_by("ordinal")
    )
    return recording, transcript, sections


def recording_unchanged(recording, *, summary_status=None, processing_status="transcribed"):
    """Capture the recording's whole-level tuple and assert nothing
    changed after a section summary operation."""
    recording.refresh_from_db()
    assert recording.summary_status == (
        summary_status if summary_status is not None else recording.summary_status
    )
    assert recording.resummarization_failed is False
    assert recording.last_failed_attempt_id is None
    assert recording.processing_status == processing_status


# ---------------------------------------------------------------------------
# Exact range input
# ---------------------------------------------------------------------------


class TestExactRangeInput:
    def test_input_is_only_the_sections_segments(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three", "delta four", "epsilon five"],
            [2], ["First half", "Second half"],
        )
        second = sections[1]  # ordinal 2, range [2, 5)
        llm = ScriptedLLM([final_summary_json()])
        result = summarize_section_one(config, second, llm_call=llm)
        assert result["result"] == "summarized"
        # Exactly one logical call (single chunk) whose input is ONLY the
        # section's segments.
        assert llm.call_count == 1
        user = llm.user_prompts[0]
        assert "gamma three" in user
        assert "delta four" in user
        assert "epsilon five" in user
        assert "alpha one" not in user
        assert "beta two" not in user
        summary = Summary.objects.get(pk=result["summary_id"])
        assert summary.section_id == second.pk
        assert summary.ordinal >= 1  # shares the recording summary ordinal space

    def test_first_section_gets_only_its_own_text(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three", "delta four"],
            [2], ["First half", "Second half"],
        )
        first = sections[0]  # range [0, 2)
        llm = ScriptedLLM([final_summary_json()])
        result = summarize_section_one(config, first, llm_call=llm)
        assert result["result"] == "summarized"
        user = llm.user_prompts[0]
        assert "alpha one" in user
        assert "beta two" in user
        assert "gamma three" not in user
        assert "delta four" not in user


# ---------------------------------------------------------------------------
# Tags: section-scoped materialization
# ---------------------------------------------------------------------------


class TestSectionTagMaterialization:
    def test_default_variant_materializes_section_scoped_tags_only(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three"], [2], ["A", "B"]
        )
        section = sections[0]
        llm = ScriptedLLM([final_summary_json(suggested_tags=["Academic"])])
        result = summarize_section_one(config, section, llm_call=llm)
        assert result["result"] == "summarized"
        assert result["tags"] == ["Academic"]
        summary = Summary.objects.get(pk=result["summary_id"])
        tag = Tag.objects.get(name_key="academic")
        # Section-scoped suggestion + assignment.
        assert SummaryTagSuggestion.objects.filter(summary=summary, tag=tag).exists()
        assignment = TagAssignment.objects.get(section=section, tag=tag)
        assert assignment.origin == "suggested"
        assert assignment.source_summary_id == summary.pk
        assert assignment.recording_id == recording.pk
        # Recording scope stays untouched (defense-in-depth).
        assert not TagAssignment.objects.filter(
            recording=recording, section__isnull=True, tag=tag
        ).exists()

    def test_non_default_variant_materializes_nothing(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two"], [1], ["A", "B"]
        )
        # Default resolves to en (unknown source fallback); zh-Hant is a
        # non-default variant.
        section = sections[0]
        llm = ScriptedLLM(
            [final_summary_json(
                title="会议", overview="讨论了评分计划。",
                key_points=[{"text": "周一评分开始", "level": 1}],
                action_items=[],
                language="zh-Hant",
                suggested_tags=["Academic"],
            )]
        )
        result = summarize_section_one(config, section, target_language="zh-Hant", llm_call=llm)
        assert result["result"] == "summarized"
        assert result["output_language"] == "zh-Hant"
        summary = Summary.objects.get(pk=result["summary_id"])
        # Non-default variants are suggestions-free in the SummaryTagSuggestion
        # table AND assignment-free in every scope (exact existing
        # whole-recording semantics: only the default variant materializes).
        assert not summary.tag_suggestions.exists()
        assert not TagAssignment.objects.filter(
            recording=recording, tag__name_key="academic"
        ).exists()

    def test_regeneration_replaces_suggested_and_keeps_manual(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three"], [2], ["A", "B"]
        )
        section = sections[0]
        family = make_tag("Family")  # config tag created directly
        make_tag_assignment(recording, family, origin="manual")
        # Section-scoped manual assignment that must survive regeneration.
        section_manual = TagAssignment.objects.create(
            recording=recording, section=section, tag=family,
            origin="manual", is_active=True,
        )
        assert section_manual.section_id == section.pk

        summarize_section_one(
            config, section, llm_call=ScriptedLLM([final_summary_json(suggested_tags=["Academic"])])
        )
        # Regeneration drops Academic suggestion from the section scope.
        summarize_section_one(
            config, section, regenerate=True,
            llm_call=ScriptedLLM([final_summary_json(title="V2", suggested_tags=[])])
        )
        # The model-deactivated section suggested row is inactive...
        academic = Tag.objects.get(name_key="academic")
        suggested = TagAssignment.objects.get(section=section, tag=academic)
        assert suggested.is_active is False
        assert suggested.deactivated_by == "model"
        # ...while the section manual row survives untouched.
        section_manual.refresh_from_db()
        assert section_manual.is_active is True
        assert section_manual.origin == "manual"
        # Recording-scoped rows are never touched by section regeneration.
        rec_family = TagAssignment.objects.get(recording=recording, section__isnull=True, tag=family)
        assert rec_family.is_active is True
        assert rec_family.origin == "manual"

    def test_user_suppression_is_section_local(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three"], [2], ["A", "B"]
        )
        section = sections[0]
        # User suppressed Academic in the SECTION scope only.
        academic = make_tag("Academic")
        suppressed = TagAssignment.objects.create(
            recording=recording, section=section, tag=academic,
            origin="suggested", is_active=False, deactivated_by="user",
        )
        suppressed.deactivated_at = dj_timezone.now()
        suppressed.save()
        # The recording scope has an ACTIVE Academic assignment.
        make_tag_assignment(recording, academic)

        summarize_section_one(
            config, section, llm_call=ScriptedLLM([final_summary_json(suggested_tags=["Academic"])])
        )
        # The section suppression survives; the recording assignment is
        # untouched (a section summary never mutates recording tags).
        suppressed.refresh_from_db()
        assert suppressed.is_active is False
        assert suppressed.deactivated_by == "user"
        rec_assignment = TagAssignment.objects.get(
            recording=recording, section__isnull=True, tag=academic
        )
        assert rec_assignment.is_active is True


# ---------------------------------------------------------------------------
# Recording-level state is never touched
# ---------------------------------------------------------------------------


class TestRecordingStateUnchanged:
    def test_success_leaves_recording_default_tuple_untouched(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three"], [2], ["A", "B"]
        )
        recording.summary_status = SummaryState.MISSING
        recording.save(update_fields=["summary_status"])
        section = sections[0]
        result = summarize_section_one(config, section, llm_call=ScriptedLLM([final_summary_json()]))
        assert result["result"] == "summarized"
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.MISSING
        assert recording.resummarization_failed is False
        assert recording.last_failed_attempt_id is None
        assert recording.processing_status == "transcribed"
        # No whole-recording Summary was created (ordinal-0 scope).
        fixed = transcript.sections.get(ordinal=0, segmented_version__isnull=True)
        assert not Summary.objects.filter(section=fixed).exists()

    def test_failure_leaves_recording_default_tuple_untouched(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three"], [2], ["A", "B"]
        )
        recording.summary_status = SummaryState.CURRENT
        recording.save(update_fields=["summary_status"])
        section = sections[0]
        result = summarize_section_one(
            config, section, llm_call=ScriptedLLM([LLMUnavailable("down")])
        )
        assert result["result"] == "failed"
        assert result["error_code"] == "endpoint_unavailable"
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.CURRENT
        assert recording.resummarization_failed is False
        assert recording.last_failed_attempt_id is None
        assert recording.processing_status == "transcribed"
        # The SECTION VariantState records the failure.
        vs = SummaryVariantState.objects.get(
            transcript=transcript, section=section, output_language="en"
        )
        assert vs.status == SummaryVariantState.VariantStatus.FAILED
        assert vs.regeneration_failed is False


# ---------------------------------------------------------------------------
# Failures / regeneration / recovery
# ---------------------------------------------------------------------------


class TestFailureAndRegeneration:
    def test_failed_initial_creates_section_failed_state(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two"], [1], ["A", "B"]
        )
        section = sections[0]
        result = summarize_section_one(
            config, section, llm_call=ScriptedLLM([LLMTimeout(), LLMTimeout()])
        )
        assert result["result"] == "failed"
        assert result["error_code"] == "timeout"
        vs = SummaryVariantState.objects.get(
            transcript=transcript, section=section, output_language="en"
        )
        assert vs.status == SummaryVariantState.VariantStatus.FAILED
        assert vs.regeneration_failed is False
        assert vs.last_failed_attempt is not None
        # Attempt provenance proves the exact section scope.
        attempt = vs.last_failed_attempt
        lang = attempt.context_json["language"]
        assert str(lang["transcript_id"]) == str(transcript.pk)
        assert str(lang["section_id"]) == str(section.pk)
        assert lang["resolved"] == "en"

    def test_failed_regeneration_preserves_active_summary_and_marks_variant(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three"], [2], ["A", "B"]
        )
        section = sections[0]
        first = summarize_section_one(
            config, section, llm_call=ScriptedLLM([final_summary_json(title="V1")])
        )
        assert first["result"] == "summarized"
        result = summarize_section_one(
            config, section, regenerate=True,
            llm_call=ScriptedLLM([LLMTimeout(), LLMTimeout()]),
        )
        assert result["result"] == "failed"
        assert result["kept_current_summary"] is True
        # The active section Summary is preserved (V1).
        active = Summary.objects.get(
            transcript=transcript, section=section, output_language="en", is_active=True
        )
        assert active.title == "V1"
        # Only THIS variant carries the regeneration failure marker.
        vs = SummaryVariantState.objects.get(
            transcript=transcript, section=section, output_language="en"
        )
        assert vs.status == SummaryVariantState.VariantStatus.CURRENT
        assert vs.regeneration_failed is True
        assert vs.last_failed_attempt is not None
        # The recording tuple is untouched.
        recording.refresh_from_db()
        assert recording.resummarization_failed is False
        assert recording.last_failed_attempt_id is None

    def test_regeneration_versions_and_deactivates_old(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three"], [2], ["A", "B"]
        )
        section = sections[0]
        first = summarize_section_one(
            config, section, llm_call=ScriptedLLM([final_summary_json(title="V1")])
        )
        second = summarize_section_one(
            config, section, regenerate=True,
            llm_call=ScriptedLLM([final_summary_json(title="V2")]),
        )
        assert second["result"] == "summarized"
        v1 = Summary.objects.get(pk=first["summary_id"])
        v2 = Summary.objects.get(pk=second["summary_id"])
        assert v1.is_active is False
        assert v1.superseded_at is not None
        assert v2.is_active is True
        assert v1.pk != v2.pk
        assert v1.ordinal < v2.ordinal

    def test_interruption_recovery_is_exact_scope(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three"], [2], ["A", "B"]
        )
        section = sections[0]
        recording.summary_status = SummaryState.CURRENT
        recording.save(update_fields=["summary_status"])
        # A running section summary attempt with complete exact-scope
        # provenance (what summarize_section_one writes before any work).
        attempt = ProcessingAttempt.objects.create(
            recording=recording,
            stage=AttemptStage.SUMMARIZATION,
            ordinal=summarize_service.next_ordinal(recording, AttemptStage.SUMMARIZATION),
            model_id="test-model",
            context_json={
                "language": {
                    "requested": "default",
                    "resolved": "en",
                    "source": "",
                    "is_default": True,
                    "source_method": "",
                    "transcript_id": transcript.pk,
                    "section_id": section.pk,
                }
            },
        )
        # Recovery semantics: close the attempt as interrupted, then the
        # exact-scope reconciler derives state.
        attempt.outcome = AttemptOutcome.INTERRUPTED
        attempt.error_code = "process_interrupted"
        attempt.finished_at = dj_timezone.now()
        attempt.save()

        changed = reconcile_recording_summary_state(recording, recovered_attempt=attempt)
        # The SECTION variant becomes failed...
        vs = SummaryVariantState.objects.get(
            transcript=transcript, section=section, output_language="en"
        )
        assert vs.status == SummaryVariantState.VariantStatus.FAILED
        # ...but the Recording-level default tuple is NOT changed (the
        # recovery event is section-scoped, not the ordinal-0 default).
        assert changed is False
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.CURRENT
        assert recording.last_failed_attempt_id is None
        assert recording.resummarization_failed is False


# ---------------------------------------------------------------------------
# No sync / no audio / no files
# ---------------------------------------------------------------------------


class TestNoSync:
    def test_section_summary_schedules_no_recording_sync(self, tmp_path, monkeypatch):
        from workflow.services import summarize as summarize_module

        called: list = []
        monkeypatch.setattr(
            summarize_module, "schedule_recording_sync",
            lambda ids: called.append(list(ids)),
        )
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three"], [2], ["A", "B"]
        )
        section = sections[0]
        result = summarize_section_one(
            config, section, llm_call=ScriptedLLM([final_summary_json()])
        )
        assert result["result"] == "summarized"
        assert called == []

    def test_failed_section_summary_schedules_no_sync(self, tmp_path, monkeypatch):
        from workflow.services import summarize as summarize_module

        called: list = []
        monkeypatch.setattr(
            summarize_module, "schedule_recording_sync",
            lambda ids: called.append(list(ids)),
        )
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two"], [1], ["A", "B"]
        )
        result = summarize_section_one(
            config, sections[0], llm_call=ScriptedLLM([LLMUnavailable("down")])
        )
        assert result["result"] == "failed"
        assert called == []

    def test_no_audio_or_file_access(self, tmp_path):
        # make_transcribed_recording creates no AudioSource rows; the
        # section service must work without any file.
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two"], [1], ["A", "B"]
        )
        result = summarize_section_one(
            config, sections[0], llm_call=ScriptedLLM([final_summary_json()])
        )
        assert result["result"] == "summarized"
        assert recording.sources.count() == 0


# ---------------------------------------------------------------------------
# Target validation (shared segmentation canonical validation)
# ---------------------------------------------------------------------------


class TestTargetValidation:
    def test_fixed_section_rejected(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, _transcript, fixed = make_transcribed_recording(["a", "b"])
        with pytest.raises(SegmentationError) as excinfo:
            summarize_section_one(config, fixed, llm_call=ScriptedLLM([]))
        assert excinfo.value.code == "section_not_topic"

    def test_historical_superseded_version_rejected(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three"], [2], ["A", "B"]
        )
        section = sections[0]
        version = section.segmented_version
        version.is_active = False
        version.superseded_at = dj_timezone.now()
        version.save(update_fields=["is_active", "superseded_at"])
        with pytest.raises(SegmentationError) as excinfo:
            summarize_section_one(config, section, llm_call=ScriptedLLM([]))
        assert excinfo.value.code == "section_not_active"

    def test_historical_transcript_rejected(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three"], [2], ["A", "B"]
        )
        transcript.is_active = False
        transcript.superseded_at = dj_timezone.now()
        transcript.save(update_fields=["is_active", "superseded_at"])
        with pytest.raises(SegmentationError) as excinfo:
            summarize_section_one(config, sections[0], llm_call=ScriptedLLM([]))
        assert excinfo.value.code == "transcript_not_active"

    def test_cross_parent_section_rejected(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three"], [2], ["A", "B"]
        )
        # The ACTIVE version of transcript A, referenced from a Section
        # whose transcript is a DIFFERENT (active) transcript.
        active_version = sections[0].segmented_version
        other_rec, other_transcript, _fixed = make_transcribed_recording(
            ["x", "y", "z"], sha="other" + "0" * 60
        )
        cross = Section.objects.create(
            transcript=other_transcript,
            segmented_version=active_version,
            ordinal=3,
            title="Cross",
            start_segment_ordinal=0,
            end_segment_ordinal_exclusive=3,
        )
        with pytest.raises(SegmentationError) as excinfo:
            summarize_section_one(config, cross, llm_call=ScriptedLLM([]))
        assert excinfo.value.code == "layout_invalid"

    def test_malformed_layout_rejected(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three", "delta four"], [2], ["A", "B"]
        )
        # Corrupt the active layout: delete one topic section, so the
        # stored layout no longer partitions the range (lone section).
        sections[0].delete()
        with pytest.raises(SegmentationError) as excinfo:
            summarize_section_one(config, sections[1], llm_call=ScriptedLLM([]))
        assert excinfo.value.code == "layout_invalid"

    def test_invalid_selector_raises_config_error(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two"], [1], ["A", "B"]
        )
        with pytest.raises(ConfigError):
            summarize_section_one(
                config, sections[0], target_language="fi", llm_call=ScriptedLLM([])
            )

    def test_disabled_summarization_skips(self, tmp_path):
        config = make_config_for(
            tmp_path,
            summarization=summarize_service_chunk_config(enabled=False),
        )
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two"], [1], ["A", "B"]
        )
        result = summarize_section_one(config, sections[0], llm_call=ScriptedLLM([]))
        assert result["result"] == "skipped"
        assert result["reason"] == "summarization_disabled"
        assert result["section_id"] == sections[0].pk

    def test_existing_variant_without_regenerate_skips(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two"], [1], ["A", "B"]
        )
        section = sections[0]
        summarize_section_one(config, section, llm_call=ScriptedLLM([final_summary_json()]))
        result = summarize_section_one(config, section, llm_call=ScriptedLLM([]))
        assert result["result"] == "skipped"
        assert result["reason"] == "variant_current"


# ---------------------------------------------------------------------------
# Chunking: long section input reuses the deterministic bounded path
# ---------------------------------------------------------------------------


def summarize_service_chunk_config(enabled=True, chunk_characters=500,
                                   max_total_characters=100000):
    from brainlib.config import SummarizationConfig

    return SummarizationConfig(
        enabled=enabled,
        prompt_version="1",
        max_input_characters=100000,
        chunk_characters=chunk_characters,
        chunk_overlap_characters=0,
        max_chunk_count=8,
        max_total_characters=max_total_characters,
        temperature=0.2,
        max_output_tokens=3000,
    )


class TestChunkedSectionInput:
    def test_long_section_uses_map_reduce_path(self, tmp_path):
        config = make_config_for(
            tmp_path,
            summarization=summarize_service_chunk_config(chunk_characters=60),
        )
        recording, transcript, sections = split_recording(
            [f"segment {i} " + ("x" * 40) for i in range(6)],
            [3], ["A", "B"],
        )
        section = sections[1]  # segments 3..5 -> > 60 chars -> 2+ chunks

        def handler(*, system, user):
            if "ALLOWED TAGS" in system:
                return final_summary_json()
            return map_summary_json()

        llm = ScriptedLLM(handler=handler)
        result = summarize_section_one(config, section, llm_call=llm)
        assert result["result"] == "summarized"
        # Map calls for every chunk + final reduce.
        assert llm.call_count >= 2
        # Every user prompt only ever contains THIS section's text, never
        # the crop-excluded segments.
        excluded = ["segment 0", "segment 1", "segment 2"]
        for user in llm.user_prompts:
            for word in excluded:
                assert word not in user


# ---------------------------------------------------------------------------
# Whole-recording behavior regressions
# ---------------------------------------------------------------------------


class TestWholeBehaviorRegressions:
    def test_recording_and_section_assignments_coexist_same_tag(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three"], [2], ["A", "B"]
        )
        section = sections[0]
        # A recording-scoped manual assignment for the same tag exists.
        academic = make_tag("Academic")
        make_tag_assignment(recording, academic, origin="manual")
        # A section summary suggesting the same tag creates an INDEPENDENT
        # section-scoped row (recording scope untouched).
        result = summarize_section_one(
            config, section, llm_call=ScriptedLLM([final_summary_json(suggested_tags=["Academic"])])
        )
        assert result["result"] == "summarized"
        summary = Summary.objects.get(pk=result["summary_id"])
        assert TagAssignment.objects.filter(
            recording=recording, section__isnull=True, tag=academic
        ).get().origin == "manual"
        section_assignment = TagAssignment.objects.get(section=section, tag=academic)
        assert section_assignment.origin == "suggested"
        assert section_assignment.source_summary_id == summary.pk

    def test_whole_summary_still_syncs_and_recording_summary_unaffected(self, tmp_path, monkeypatch):
        from workflow.services import summarize as summarize_module

        called: list = []
        monkeypatch.setattr(
            summarize_module, "schedule_recording_sync",
            lambda ids: called.append(list(ids)),
        )
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three"], [2], ["A", "B"]
        )
        # Generate the whole-recording summary first (existing behavior:
        # schedules one sync, recording-scoped tags).
        recording.summary_status = SummaryState.MISSING
        recording.save(update_fields=["summary_status"])
        whole = summarize_service.summarize_one(
            config, recording, llm_call=ScriptedLLM([final_summary_json(suggested_tags=["Academic"])])
        )
        assert whole["result"] == "summarized"
        assert called  # the whole-recording summary synced
        called.clear()
        # Now a section summary on the same recording.
        section_result = summarize_section_one(
            config, sections[0], llm_call=ScriptedLLM([final_summary_json(title="Section V1")])
        )
        assert section_result["result"] == "summarized"
        # The section summary schedules nothing.
        assert called == []
        # The whole-recording current summary is untouched.
        current = recording.current_summary()
        assert isinstance(current, Summary)
        assert current.pk == whole["summary_id"]


# ---------------------------------------------------------------------------
# Finding 5: persistence-time revalidation — a layout superseded during
# generation never persists a Summary/tag; the durable attempt carries a
# stable sanitized failure and only that section's VariantState is
# reconciled (an old current summary is preserved).
# ---------------------------------------------------------------------------


class TestLayoutChangedDuringGeneration:
    def _supersede(self, section):
        SegmentedVersion.objects.filter(pk=section.segmented_version_id).update(
            is_active=False, superseded_at=dj_timezone.now()
        )

    def test_superseded_during_mock_llm_no_persist(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three"], [2], ["A", "B"]
        )
        section = sections[0]

        def handler(*, system, user):
            # A concurrent layout save supersedes the ACTIVE revision
            # while the (mocked) LLM is running.
            self._supersede(section)
            return final_summary_json()

        result = summarize_section_one(config, section, llm_call=ScriptedLLM(handler=handler))
        assert result["result"] == "failed"
        assert result["error_code"] == "section_layout_changed"
        assert result["kept_current_summary"] is False
        # NEVER persisted a Summary or a tag for the historical section.
        assert not Summary.objects.filter(section=section).exists()
        assert not TagAssignment.objects.filter(section=section).exists()
        # The durable exact-scope attempt carries the stable failure.
        attempt = ProcessingAttempt.objects.filter(
            recording=recording, stage=AttemptStage.SUMMARIZATION
        ).order_by("-ordinal").first()
        assert attempt.error_code == "section_layout_changed"
        assert attempt.outcome == AttemptOutcome.INVALID_OUTPUT
        # Only the section VariantState is reconciled (failed; no active
        # Summary), and the Recording-level default tuple is untouched.
        vs = SummaryVariantState.objects.get(
            transcript=transcript, section=section, output_language="en"
        )
        assert vs.status == SummaryVariantState.VariantStatus.FAILED
        assert vs.regeneration_failed is False
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.MISSING
        assert recording.resummarization_failed is False
        assert recording.last_failed_attempt_id is None
        assert recording.processing_status == "transcribed"

    def test_superseded_during_mock_llm_preserves_old_current_summary(self, tmp_path):
        config = make_config_for(tmp_path)
        recording, transcript, sections = split_recording(
            ["alpha one", "beta two", "gamma three"], [2], ["A", "B"]
        )
        section = sections[0]
        first = summarize_section_one(
            config, section, llm_call=ScriptedLLM([final_summary_json(title="V1")])
        )
        assert first["result"] == "summarized"

        def handler(*, system, user):
            self._supersede(section)
            return final_summary_json(title="V2")

        result = summarize_section_one(
            config, section, regenerate=True, llm_call=ScriptedLLM(handler=handler)
        )
        assert result["result"] == "failed"
        assert result["error_code"] == "section_layout_changed"
        assert result["kept_current_summary"] is True
        # The old active section Summary is preserved; no V2 was created.
        active = Summary.objects.get(
            transcript=transcript, section=section, output_language="en", is_active=True
        )
        assert active.pk == first["summary_id"]
        assert active.title == "V1"
        assert Summary.objects.filter(section=section).count() == 1
        # Only THIS variant carries the regeneration failure marker.
        vs = SummaryVariantState.objects.get(
            transcript=transcript, section=section, output_language="en"
        )
        assert vs.status == SummaryVariantState.VariantStatus.CURRENT
        assert vs.regeneration_failed is True
        assert vs.last_failed_attempt is not None
        assert vs.last_failed_attempt.error_code == "section_layout_changed"
        # Recording-level untouched.
        recording.refresh_from_db()
        assert recording.resummarization_failed is False
        assert recording.last_failed_attempt_id is None


# ---------------------------------------------------------------------------
# Finding 4: persist_summary owns section validity — the scope is derived
# from the Section shape (no caller switches), the topic section is
# revalidated inside the persistence transaction, and invalid shapes are
# rejected.
# ---------------------------------------------------------------------------


class TestPersistSummaryScopeDerivation:
    def _payload(self):
        return {
            "title": "T",
            "overview": "O",
            "key_points": [],
            "action_items": [],
            "people": [],
            "organizations": [],
            "topics": [],
            "language": "en",
            "suggested": [],
            "rejected": [],
        }

    def _attempt(self, recording):
        return ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.SUMMARIZATION, ordinal=1,
            model_id="m",
        )

    def test_invalid_section_shape_rejected(self):
        recording, transcript, fixed = make_transcribed_recording(["a", "b"])
        attempt = self._attempt(recording)
        # An impossible shape (fixed-style NULL version but ordinal != 0) —
        # the DB CHECK forbids saving it; persist_summary must reject it
        # explicitly regardless.
        fixed.ordinal = 5
        with pytest.raises(SummaryRelationError):
            persist_summary(
                recording=recording,
                transcript=transcript,
                section=fixed,
                attempt=attempt,
                payload=self._payload(),
                output_language="en",
                is_default=True,
                model_id="m",
                base_url="u",
                prompt_version="1",
                fingerprint="f",
                chunk_count=1,
                input_characters=1,
                limits_used={},
                generation_mode="manual",
            )
        assert Summary.objects.count() == 0

    def test_topic_section_revalidated_inside_transaction(self):
        """A layout that became historical AFTER the caller captured the
        section is re-validated (fresh DB state) inside the persistence
        transaction, immediately before the Summary/tag writes — no
        Summary/tag is persisted."""
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["A", "B"]
        )
        section = sections[0]
        attempt = self._attempt(recording)
        # Supersede the active revision via a fresh UPDATE (the caller's
        # cached version object still looks active).
        SegmentedVersion.objects.filter(pk=section.segmented_version_id).update(
            is_active=False, superseded_at=dj_timezone.now()
        )
        with pytest.raises(SegmentationError) as excinfo:
            persist_summary(
                recording=recording,
                transcript=transcript,
                section=section,
                attempt=attempt,
                payload=self._payload(),
                output_language="en",
                is_default=True,
                model_id="m",
                base_url="u",
                prompt_version="1",
                fingerprint="f",
                chunk_count=1,
                input_characters=1,
                limits_used={},
                generation_mode="manual",
            )
        assert excinfo.value.code == "section_not_active"
        assert Summary.objects.count() == 0
        assert not TagAssignment.objects.filter(section=section).exists()

    def test_fixed_section_keeps_recording_scope_and_sync(self, tmp_path, monkeypatch):
        """The fixed ordinal-0 shape still derives recording-scoped tags +
        exactly one post-commit recording sync (existing whole behavior)."""
        from workflow.services import summarize as summarize_module

        called: list = []
        monkeypatch.setattr(
            summarize_module, "schedule_recording_sync",
            lambda ids: called.append(list(ids)),
        )
        recording, transcript, fixed = make_transcribed_recording(["a", "b"])
        attempt = self._attempt(recording)
        payload = self._payload()
        payload["suggested"] = [make_tag("Academic")]
        summary = persist_summary(
            recording=recording,
            transcript=transcript,
            section=fixed,
            attempt=attempt,
            payload=payload,
            output_language="en",
            is_default=True,
            model_id="m",
            base_url="u",
            prompt_version="1",
            fingerprint="f",
            chunk_count=1,
            input_characters=1,
            limits_used={},
            generation_mode="manual",
        )
        assert called == [[recording.pk]]
        # Recording-scoped assignment materialized; section-scoped none.
        assert TagAssignment.objects.filter(
            recording=recording, section__isnull=True, tag__name_key="academic"
        ).exists()
        assert not TagAssignment.objects.filter(
            section__isnull=False, tag__name_key="academic"
        ).exists()
        assert summary.ordinal == 1