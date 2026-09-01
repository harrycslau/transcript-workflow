"""Tests for the summarization pipeline: schema validation, lifecycle,
chunked map/reduce orchestration, pre-flight limits, and persistence."""

from __future__ import annotations

import json

import httpx
import pytest

from brainlib.config import LLMConfig, SummarizationConfig, TagSpec, TagsConfig
from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    ProcessingAttempt,
    Recording,
    Summary,
    SummaryState,
    Tag,
    TagAssignment,
)
from workflow.services import summarize as summarize_service
from workflow.services.llm import LLMInvalid, LLMTimeout, LLMUnavailable
from workflow.services.summarize import (
    SummaryRelationError,
    _final_system_prompt,
    _map_system_prompt,
    _reduce_user_prompt,
    persist_summary,
    validate_final_payload,
    validate_map_payload,
)

from factories import (
    final_summary_json,
    make_config,
    make_transcribed_recording,
    map_summary_json,
    omlx_envelope,
)

pytestmark = pytest.mark.django_db


def small_config(tmp_path, **overrides) -> SummarizationConfig:
    base = dict(
        enabled=True,
        prompt_version="1",
        max_input_characters=overrides.pop("max_input_characters", 100000),
        chunk_characters=overrides.pop("chunk_characters", 500),
        chunk_overlap_characters=overrides.pop("chunk_overlap_characters", 0),
        max_chunk_count=overrides.pop("max_chunk_count", 8),
        max_total_characters=overrides.pop("max_total_characters", 100000),
        temperature=0.2,
        max_output_tokens=3000,
    )
    base.update(overrides)
    return SummarizationConfig(**base)


def make_llm_config(tmp_path, **llm_overrides):
    return make_config(
        tmp_path,
        llm=LLMConfig(
            provider="openai_compatible",
            base_url=llm_overrides.pop("base_url", "http://127.0.0.1:1/v1"),
            model=llm_overrides.pop("model", "test-model"),
            api_key_env="BRAIN_TEST_LLM_API_KEY",
            temperature=0.2,
            timeout_seconds=600,
        ),
    )


def tags_config(*names) -> TagsConfig:
    return TagsConfig(allowed=tuple(TagSpec(name=n, description=f"{n} description") for n in names))


class ScriptedLLM:
    """Scripted llm_call: records prompts, returns queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, *, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def call_count(self) -> int:
        return len(self.calls)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestFinalValidation:
    def _payload(self, **overrides):
        data = json.loads(final_summary_json())
        data.update(overrides)
        return data

    def _allowed(self):
        from brainlib.config import tag_name_key
        from workflow.models import Tag

        for name in ("Family", "Academic", "Unknown"):
            Tag.objects.get_or_create(
                name_key=tag_name_key(name), defaults={"name": name, "description": f"{name} description"}
            )
        return {t.name_key: t for t in Tag.objects.filter(is_configured=True)}

    def test_valid_result(self):
        result = validate_final_payload(self._payload(), self._allowed())
        assert result["title"] == "Meeting about grading"
        assert result["suggested"][0].name == "Academic"
        assert result["rejected"] == []

    def test_fenced_json_supported(self):
        content = f"```json\n{final_summary_json()}\n```"
        data = json.loads(content.split("```json\n")[1].rsplit("```")[0])
        assert validate_final_payload(data, self._allowed())["title"]

    def test_missing_title_rejected(self):
        data = self._payload()
        del data["title"]
        with pytest.raises(LLMInvalid, match="title"):
            validate_final_payload(data, self._allowed())

    def test_empty_title_rejected(self):
        with pytest.raises(LLMInvalid, match="title"):
            validate_final_payload(self._payload(title="   "), self._allowed())

    def test_boolean_overview_rejected(self):
        with pytest.raises(LLMInvalid, match="overview"):
            validate_final_payload(self._payload(overview=True), self._allowed())

    def test_boolean_list_item_rejected(self):
        with pytest.raises(LLMInvalid, match="key_points"):
            validate_final_payload(self._payload(key_points=[True]), self._allowed())

    def test_wrong_field_types_rejected(self):
        with pytest.raises(LLMInvalid, match="key_points"):
            validate_final_payload(self._payload(key_points="not a list"), self._allowed())
        with pytest.raises(LLMInvalid, match="action_items"):
            validate_final_payload(self._payload(action_items="nope"), self._allowed())

    def test_over_length_string_rejected(self):
        with pytest.raises(LLMInvalid, match="200"):
            validate_final_payload(self._payload(title="x" * 201), self._allowed())
        with pytest.raises(LLMInvalid, match="8000"):
            validate_final_payload(self._payload(overview="x" * 8001), self._allowed())

    def test_over_long_action_item_rejected(self):
        items = [{"text": "x" * 1001, "owner": None, "due_date": None}]
        with pytest.raises(LLMInvalid, match="action_items"):
            validate_final_payload(self._payload(action_items=items), self._allowed())

    def test_excessive_list_counts_rejected(self):
        with pytest.raises(LLMInvalid, match="30"):
            validate_final_payload(self._payload(key_points=["p"] * 31), self._allowed())
        with pytest.raises(LLMInvalid, match="50"):
            validate_final_payload(self._payload(people=["n"] * 51), self._allowed())

    def test_action_item_variants(self):
        result = validate_final_payload(self._payload(action_items=[]), self._allowed())
        assert result["action_items"] == []
        result = validate_final_payload(
            self._payload(action_items=[{"text": "Do it", "owner": "Bo", "due_date": "Mon"}]),
            self._allowed(),
        )
        assert result["action_items"] == [{"text": "Do it", "owner": "Bo", "due_date": "Mon"}]

    def test_action_item_boolean_text_rejected(self):
        items = [{"text": True}]
        with pytest.raises(LLMInvalid, match="action_items"):
            validate_final_payload(self._payload(action_items=items), self._allowed())

    def test_unconfigured_tags_rejected_recorded(self):
        result = validate_final_payload(
            self._payload(suggested_tags=["Academic", "MadeUpTag"]), self._allowed()
        )
        assert [t.name for t in result["suggested"]] == ["Academic"]
        assert result["rejected"] == ["MadeUpTag"]

    def test_unknown_dropped_when_real_tags_present(self):
        result = validate_final_payload(
            self._payload(suggested_tags=["Academic", "Family", "Unknown"]), self._allowed()
        )
        assert sorted(t.name for t in result["suggested"]) == ["Academic", "Family"]

    def test_unknown_alone_is_kept(self):
        result = validate_final_payload(
            self._payload(suggested_tags=["Unknown"]), self._allowed()
        )
        assert [t.name for t in result["suggested"]] == ["Unknown"]

    def test_case_insensitive_tag_matching(self):
        result = validate_final_payload(self._payload(suggested_tags=["aCaDeMic"]), self._allowed())
        assert [t.name for t in result["suggested"]] == ["Academic"]

    def test_duplicate_suggestions_deduplicated(self):
        result = validate_final_payload(
            self._payload(suggested_tags=["Academic", "ACADEMIC"]), self._allowed()
        )
        assert len(result["suggested"]) == 1


class TestMapValidation:
    def test_valid(self):
        payload = validate_map_payload(json.loads(map_summary_json()))
        assert payload == {
            "overview": "Part summary.",
            "key_points": ["Point one"],
        }


class TestSummaryPrompts:
    def test_final_prompt_requires_requested_style_and_grounding(self):
        prompt = _final_system_prompt([])
        assert "ALWAYS use Traditional Chinese" in prompt
        assert "50–80 Chinese" in prompt
        assert "maximum three levels" in prompt
        assert "never force hierarchy" in prompt
        assert "If uncertain use []" in prompt
        assert "explicitly named identifiable people" in prompt
        assert "fill for completeness" in prompt

    def test_map_and_reduce_preserve_evidence_without_invention(self):
        map_prompt = _map_system_prompt()
        assert "ALWAYS use Traditional Chinese" in map_prompt
        assert "explicit future actions" in map_prompt
        assert "named people/organizations" in map_prompt

        reduce_prompt = _reduce_user_prompt(
            [{"overview": "摘要", "key_points": ["重點"]}], final=True
        )
        assert "add no new actions, people, organizations, or topics" in reduce_prompt

    def test_missing_overview_rejected(self):
        with pytest.raises(LLMInvalid, match="overview"):
            validate_map_payload({"key_points": []})

    def test_over_limit_rejected(self):
        with pytest.raises(LLMInvalid, match="4000"):
            validate_map_payload({"overview": "x" * 4001, "key_points": []})
        with pytest.raises(LLMInvalid, match="15"):
            validate_map_payload({"overview": "ok", "key_points": ["p"] * 16})


def make_running_attempt(recording, *, running: bool = True) -> ProcessingAttempt:
    from django.utils import timezone

    return ProcessingAttempt.objects.create(
        recording=recording,
        stage=AttemptStage.SUMMARIZATION,
        ordinal=ProcessingAttempt.objects.filter(recording=recording).count() + 1,
        started_at=timezone.now(),
        finished_at=None if running else timezone.now(),
    )


# ---------------------------------------------------------------------------
# Persistence invariants (clarification 3)
# ---------------------------------------------------------------------------


class TestPersistenceInvariants:
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

    def test_section_from_other_transcript_rejected(self, tmp_path):
        _, transcript_a, section_a = make_transcribed_recording(["a"])
        recording_b, transcript_b, section_b = make_transcribed_recording(["b"])
        attempt = make_running_attempt(transcript_a.recording)
        with pytest.raises(SummaryRelationError):
            persist_summary(
                recording=transcript_a.recording,
                transcript=transcript_a,
                section=section_b,  # belongs to another transcript
                attempt=attempt,
                payload=self._payload(),
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

    def test_transcript_from_other_recording_rejected(self, tmp_path):
        _, transcript_a, section_a = make_transcribed_recording(["a"])
        recording_other, _, _ = make_transcribed_recording(["b"])
        attempt = make_running_attempt(recording_other)
        with pytest.raises(SummaryRelationError):
            persist_summary(
                recording=recording_other,
                transcript=transcript_a,  # belongs to a different recording
                section=section_a,
                attempt=attempt,
                payload=self._payload(),
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


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def _config(self, tmp_path):
        config = make_llm_config(tmp_path)
        return make_config(
            tmp_path,
            llm=config.llm,
            tags=tags_config("Family", "Academic", "Unknown"),
        )

    def test_first_successful_summary(self, tmp_path):
        config = self._config(tmp_path)
        recording, transcript, _ = make_transcribed_recording(["hello world"])
        llm = ScriptedLLM([final_summary_json(suggested_tags=["Academic", "Family"])])
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "summarized"
        summary = recording.current_summary()
        assert summary is not None
        assert summary.is_active and summary.ordinal == 1
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.CURRENT
        assert recording.processing_status == "transcribed"
        attempt = ProcessingAttempt.objects.get(pk=summary.attempt_id)
        assert attempt.outcome == AttemptOutcome.SUCCESS
        assert attempt.stage == AttemptStage.SUMMARIZATION
        # Provenance
        assert summary.model_id == "test-model"
        assert summary.prompt_version == "1"
        assert summary.parser_version == "1"
        assert len(summary.config_fingerprint) == 64
        assert summary.input_truncated is False
        assert summary.limits_used["max_input_characters"] == 120000
        # Fingerprint stored on the attempt (no prompts, no secrets)
        assert attempt.cli_args_json["kind"] == "omlx_summarization"
        assert "hello world" not in json.dumps(attempt.cli_args_json)

    def test_idempotent_normal_run(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = ScriptedLLM([final_summary_json()])
        summarize_service.summarize_one(config, recording, llm_call=llm)
        result = summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([]))
        assert result == {
            "recording_id": recording.pk,
            "result": "skipped",
            "reason": "summary_current",
            "summary_status": SummaryState.CURRENT,
        }
        assert Summary.objects.count() == 1

    def test_explicit_regeneration_creates_new_version(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        summarize_service.summarize_one(
            config, recording, llm_call=ScriptedLLM([final_summary_json(title="V1")])
        )
        result = summarize_service.summarize_one(
            config, recording, regenerate=True,
            llm_call=ScriptedLLM([final_summary_json(title="V2")]),
        )
        assert result["result"] == "summarized"
        assert result["regeneration"] is True
        summaries = list(Summary.objects.order_by("ordinal"))
        assert [s.title for s in summaries] == ["V1", "V2"]
        assert [s.is_active for s in summaries] == [False, True]
        assert summaries[0].superseded_at is not None
        recording.refresh_from_db()
        assert recording.current_summary().title == "V2"

    def test_failed_regeneration_preserves_active_summary(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([final_summary_json(title="V1")]))
        result = summarize_service.summarize_one(
            config, recording, regenerate=True,
            llm_call=ScriptedLLM([LLMUnavailable("endpoint down")]),
        )
        assert result["result"] == "failed"
        assert result["kept_current_summary"] is True
        recording.refresh_from_db()
        assert recording.current_summary().title == "V1"
        assert recording.resummarization_failed is True
        assert recording.summary_status == SummaryState.CURRENT
        assert recording.processing_status == "transcribed"
        assert recording.last_failed_attempt.outcome == AttemptOutcome.UNREACHABLE
        assert Summary.objects.count() == 1

    def test_failure_without_previous_summary(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        result = summarize_service.summarize_one(
            config, recording, llm_call=ScriptedLLM([LLMTimeout()])
        )
        assert result["result"] == "failed"
        assert result["kept_current_summary"] is False
        recording.refresh_from_db()
        assert recording.current_summary() is None
        assert recording.summary_status == SummaryState.FAILED
        assert recording.resummarization_failed is False
        assert recording.processing_status == "transcribed"  # never looks pipeline-failed
        attempt = ProcessingAttempt.objects.filter(
            recording=recording, stage=AttemptStage.SUMMARIZATION
        ).first()
        assert attempt.outcome == AttemptOutcome.TIMEOUT
        assert attempt.error_code == "timeout"

    def test_new_transcript_requires_new_current_summary(self, tmp_path):
        config = self._config(tmp_path)
        recording, transcript, _ = make_transcribed_recording(["hello world"])
        summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([final_summary_json(title="V1")]))
        # Retranscription: new active transcript (simulate transcription service).
        from django.utils import timezone as tz

        from workflow.models import Section, Transcript, TranscriptSegment

        attempt2 = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS, finished_at=tz.now(),
        )
        transcript2 = Transcript.objects.create(recording=recording, attempt=attempt2, text_normalized="new")
        TranscriptSegment.objects.create(transcript=transcript2, ordinal=0, text="new")
        Section.objects.create(transcript=transcript2, ordinal=0, title="Full recording")
        Transcript.objects.exclude(pk=transcript2.pk).filter(recording=recording).update(is_active=False)
        Transcript.objects.filter(pk=transcript2.pk).update(is_active=True, activated_at=tz.now())
        recording.summary_status = SummaryState.MISSING
        recording.save()

        old_summary = Summary.objects.get(title="V1")
        assert old_summary.is_active is True  # historically active in its own scope
        assert recording.current_summary() is None  # but no longer current
        assert recording.summary_status == SummaryState.MISSING
        # New summary for the new transcript works.
        result = summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([final_summary_json(title="V2")]))
        assert result["result"] == "summarized"
        old_summary.refresh_from_db()
        assert old_summary.is_active is True  # still historically active
        assert recording.current_summary().transcript_id == transcript2.pk

    def test_uniqueness_constraints(self, tmp_path):
        from django.db import IntegrityError, transaction

        from django.utils import timezone as tz

        recording, transcript, section = make_transcribed_recording(["a"])
        attempt = make_running_attempt(recording)
        Summary.objects.create(
            recording=recording, transcript=transcript, section=section, attempt=attempt,
            ordinal=1, is_active=True, activated_at=tz.now(), title="A", overview="o",
            generation_mode="manual",
        )
        attempt2 = make_running_attempt(recording, running=False)
        # Second ACTIVE summary in the same scope violates the partial unique.
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Summary.objects.create(
                    recording=recording, transcript=transcript, section=section, attempt=attempt2,
                    ordinal=2, is_active=True, activated_at=tz.now(), title="B", overview="o",
                    generation_mode="manual",
                )
        # Duplicate ordinal violates uniq_summary_ordinal.
        attempt3 = make_running_attempt(recording, running=False)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Summary.objects.create(
                    recording=recording, transcript=transcript, section=section, attempt=attempt3,
                    ordinal=1, is_active=False, title="C", overview="o",
                    generation_mode="manual",
                )

    def test_interrupted_attempt_recovery(self, tmp_path):
        from workflow.services.pipeline import recover_interruptions

        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        # Interrupted FIRST attempt counts as the transcript's one
        # automatic attempt -> FAILED, never missing (no auto-retry).
        make_running_attempt(recording)  # left running
        recover_interruptions(config)
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.FAILED
        assert recording.resummarization_failed is False
        attempt = ProcessingAttempt.objects.filter(
            recording=recording, stage=AttemptStage.SUMMARIZATION
        ).first()
        assert attempt.outcome == AttemptOutcome.INTERRUPTED
        assert attempt.error_code == "process_interrupted"
        assert attempt.finished_at is not None
        assert recording.last_failed_attempt_id == attempt.pk

        # Interrupted REGENERATION -> current summary kept + retryable warning.
        from workflow.services.pipeline import retry

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=json.dumps(omlx_envelope(final_summary_json(title="V1"))).encode()
            )
        )
        retry(config, recording, transport=transport)
        recording.refresh_from_db()
        assert recording.current_summary().title == "V1"
        make_running_attempt(recording)  # left running mid-regeneration
        recover_interruptions(config)
        recording.refresh_from_db()
        assert recording.current_summary().title == "V1"
        assert recording.resummarization_failed is True
        assert recording.summary_status == SummaryState.CURRENT
        interrupted = ProcessingAttempt.objects.filter(
            recording=recording, stage=AttemptStage.SUMMARIZATION
        ).order_by("-ordinal").first()
        assert recording.last_failed_attempt_id == interrupted.pk

    def test_explicit_retry_of_failed_summary(self, tmp_path):
        from workflow.services.pipeline import retry

        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([LLMUnavailable("down")]))
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.FAILED
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=json.dumps(omlx_envelope(final_summary_json(title="Retried"))).encode()
            )
        )
        result = retry(config, recording, transport=transport)
        assert result["result"] == "retried"
        assert result["stage"] == "summarization"
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.CURRENT
        assert recording.current_summary().title == "Retried"
        assert Summary.objects.count() == 1

    def test_run_skips_failed_summary_no_infinite_retry(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([LLMUnavailable("down")]))
        recording.refresh_from_db()
        pending = summarize_service.summarize_pending(config)
        assert pending["results"] == []
        assert Recording.objects.get(pk=recording.pk).summary_status == SummaryState.FAILED


# ---------------------------------------------------------------------------
# Pre-flight limits (clarification 1): durable attempt, zero HTTP, no Summary
# ---------------------------------------------------------------------------


class TestPreFlightLimits:
    def _forbidden_llm(self):
        def llm_call(**kwargs):
            raise AssertionError("HTTP/LLM must not be called for pre-flight failures")

        return llm_call

    def test_total_limit_creates_finished_attempt_no_summary(self, tmp_path):
        config = make_config(
            tmp_path,
            llm=make_llm_config(tmp_path).llm,
            summarization=small_config(tmp_path, max_total_characters=50, max_chunk_count=100),
            tags=tags_config("Academic"),
        )
        recording, _, _ = make_transcribed_recording(["x" * 100])
        result = summarize_service.summarize_one(
            config, recording, llm_call=self._forbidden_llm()
        )
        assert result["result"] == "failed"
        assert result["error_code"] == "input_too_large"
        attempt = ProcessingAttempt.objects.filter(
            recording=recording, stage=AttemptStage.SUMMARIZATION
        ).first()
        assert attempt.outcome == AttemptOutcome.INPUT_TOO_LARGE
        assert attempt.error_code == "input_too_large"
        assert attempt.finished_at is not None
        fingerprint = attempt.cli_args_json
        assert fingerprint["input_characters"] == 100
        assert fingerprint["chunk_count"] == 1
        assert fingerprint["limits"]["max_total_characters"] == 50
        assert Summary.objects.count() == 0
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.FAILED
        assert recording.processing_status == "transcribed"

    def test_chunk_count_limit(self, tmp_path):
        config = make_config(
            tmp_path,
            llm=make_llm_config(tmp_path).llm,
            summarization=small_config(tmp_path, chunk_characters=40, max_chunk_count=2, max_total_characters=10**6),
            tags=tags_config("Academic"),
        )
        recording, _, _ = make_transcribed_recording(["y" * 30] * 5)
        result = summarize_service.summarize_one(
            config, recording, llm_call=self._forbidden_llm()
        )
        assert result["error_code"] == "input_too_large"
        attempt = ProcessingAttempt.objects.filter(
            recording=recording, stage=AttemptStage.SUMMARIZATION
        ).first()
        assert attempt.cli_args_json["chunk_count"] > 2
        assert attempt.cli_args_json["limits"]["max_chunk_count"] == 2
        assert Summary.objects.count() == 0

    def test_regeneration_preflight_failure_preserves_summary(self, tmp_path):
        config = make_config(
            tmp_path,
            llm=make_llm_config(tmp_path).llm,
            summarization=small_config(tmp_path, max_total_characters=50, max_chunk_count=100),
            tags=tags_config("Academic"),
        )
        recording, _, _ = make_transcribed_recording(["hello"])
        summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([final_summary_json(title="V1")]))
        # Simulate a retranscription creating a much larger active transcript.
        from django.utils import timezone as tz

        from workflow.models import Section, Transcript, TranscriptSegment

        attempt2 = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS, finished_at=tz.now(),
        )
        transcript2 = Transcript.objects.create(recording=recording, attempt=attempt2, text_normalized="big")
        TranscriptSegment.objects.create(transcript=transcript2, ordinal=0, text="z" * 100)
        Section.objects.create(transcript=transcript2, ordinal=0, title="Full recording")
        Transcript.objects.exclude(pk=transcript2.pk).filter(recording=recording).update(is_active=False)
        Transcript.objects.filter(pk=transcript2.pk).update(is_active=True, activated_at=tz.now())
        recording.summary_status = SummaryState.MISSING
        recording.save()

        result = summarize_service.summarize_one(config, recording, llm_call=self._forbidden_llm())
        assert result["result"] == "failed"
        assert result["error_code"] == "input_too_large"
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.FAILED
        # The old summary is untouched (historically active on its transcript).
        assert Summary.objects.get(title="V1").is_active is True
        assert Summary.objects.count() == 1

    def test_request_size_gate_fails_before_http(self, tmp_path):
        # chunk size leaves no room for the prompt scaffolding: the fully
        # serialized request exceeds max_input_characters pre-HTTP.
        config = make_config(
            tmp_path,
            llm=make_llm_config(tmp_path).llm,
            summarization=small_config(
                tmp_path, chunk_characters=490, max_input_characters=500, max_chunk_count=100
            ),
            tags=tags_config("Academic"),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no HTTP call may be made when the request exceeds the cap")

        recording, _, _ = make_transcribed_recording(["a" * 480])
        result = summarize_service.summarize_one(
            config, recording,
            llm_call=None, transport=httpx.MockTransport(handler),
        )
        assert result["result"] == "failed"
        assert result["error_code"] == "input_too_large"
        assert Summary.objects.count() == 0


# ---------------------------------------------------------------------------
# Chunked map/reduce orchestration
# ---------------------------------------------------------------------------


class TestMapReduce:
    def _config(self, tmp_path, **overrides):
        llm_config = make_llm_config(tmp_path)
        return make_config(
            tmp_path,
            llm=llm_config.llm,
            summarization=small_config(tmp_path, **overrides),
            tags=tags_config("Academic"),
        )

    def test_short_transcript_uses_one_call(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["short transcript"])
        llm = ScriptedLLM([final_summary_json()])
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "summarized"
        assert llm.call_count == 1
        assert result["chunk_count"] == 1

    def test_long_transcript_map_then_reduce(self, tmp_path):
        config = self._config(tmp_path, chunk_characters=50, chunk_overlap_characters=0)
        recording, _, _ = make_transcribed_recording([f"segment {i} " + "w" * 40 for i in range(5)])
        llm = ScriptedLLM([map_summary_json(overview=f"part {i}") for i in range(5)] + [final_summary_json()])
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "summarized"
        assert llm.call_count == 6  # 5 map calls + 1 reduce
        assert result["chunk_count"] == 5
        assert Summary.objects.get().chunk_count == 5
        # Chronological order: map prompts mention parts in order.
        map_prompts = [c["user"] for c in llm.calls[:5]]
        for index, prompt in enumerate(map_prompts):
            assert f"part={index + 1}" in prompt

    def test_chunk_failure_fails_attempt(self, tmp_path):
        config = self._config(tmp_path, chunk_characters=50, chunk_overlap_characters=0)
        recording, _, _ = make_transcribed_recording([f"segment {i} " + "w" * 40 for i in range(3)])
        llm = ScriptedLLM([map_summary_json(), LLMUnavailable("down"), map_summary_json()])
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "failed"
        assert result["error_code"] == "endpoint_unavailable"
        assert Summary.objects.count() == 0
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.FAILED

    def test_reduce_stage_failure_fails_attempt(self, tmp_path):
        # Schema-invalid final reduce output retries the complete logical
        # call exactly once, then fails with the specific error code.
        config = self._config(tmp_path, chunk_characters=50, chunk_overlap_characters=0)
        recording, _, _ = make_transcribed_recording([f"segment {i} " + "w" * 40 for i in range(2)])
        llm = ScriptedLLM([
            map_summary_json(), map_summary_json(),
            LLMInvalid("schema_validation", "bad"), LLMInvalid("schema_validation", "bad"),
        ])
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "failed"
        assert result["error_code"] == "schema_validation"
        assert llm.call_count == 4  # 2 map calls + 2 reduce attempts
        assert Summary.objects.count() == 0

    def test_hierarchical_reduce_on_oversized_reduce_payload(self, tmp_path):
        # Build chunks whose combined intermediate summaries exceed the
        # per-request cap, forcing deterministic sub-reduction. The gate
        # runs on the real client path (MockTransport), so every request
        # is measured after full serialization.
        config = make_config(
            tmp_path,
            llm=make_llm_config(tmp_path).llm,
            summarization=small_config(
                tmp_path,
                chunk_characters=60,
                chunk_overlap_characters=0,
                max_chunk_count=8,
                max_input_characters=4500,
            ),
            tags=tags_config("Academic"),
        )
        recording, _, _ = make_transcribed_recording([f"segment {i} " + "w" * 50 for i in range(6)])
        # Intermediates with long overviews: six of them together exceed
        # the 4500-char cap; pairs fit including the current prompt scaffolding.
        big_map = map_summary_json(overview="o" * 600, key_points=["p" * 100] * 3)
        sub_map = map_summary_json(overview="m" * 600, key_points=["p" * 100] * 3)
        final_calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert len(request.content.decode()) <= config.summarization.max_input_characters
            system = body["messages"][0]["content"]
            user = body["messages"][1]["content"]
            if "ALLOWED TAGS" in system:
                final_calls["n"] += 1
                return httpx.Response(200, content=json.dumps(omlx_envelope(final_summary_json())).encode())
            if "<partial_summaries>" in user:
                return httpx.Response(200, content=json.dumps(omlx_envelope(sub_map)).encode())
            return httpx.Response(200, content=json.dumps(omlx_envelope(big_map)).encode())

        transport = httpx.MockTransport(handler)
        result = summarize_service.summarize_one(config, recording, transport=transport)
        assert result["result"] == "summarized"
        assert result["chunk_count"] == 6
        assert final_calls["n"] >= 1  # sub-reduce calls merged hierarchically

    def test_reduce_impossible_fails_cleanly_before_http(self, tmp_path):
        # Even two intermediates cannot fit the per-request cap.
        config = make_config(
            tmp_path,
            llm=make_llm_config(tmp_path).llm,
            summarization=small_config(
                tmp_path,
                chunk_characters=60,
                chunk_overlap_characters=0,
                max_chunk_count=4,
                max_input_characters=1200,
            ),
            tags=tags_config("Academic"),
        )
        recording, _, _ = make_transcribed_recording([f"segment {i} " + "w" * 50 for i in range(4)])
        big_map = map_summary_json(overview="o" * 600, key_points=["p" * 100] * 3)

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            user = body["messages"][1]["content"]
            if "<partial_summaries>" in user:
                raise AssertionError("HTTP must not be reached when even a pair cannot fit")
            return httpx.Response(200, content=json.dumps(omlx_envelope(big_map)).encode())

        result = summarize_service.summarize_one(config, recording, transport=httpx.MockTransport(handler))
        assert result["result"] == "failed"
        assert result["error_code"] == "input_too_large"
        assert Summary.objects.count() == 0

    def test_every_request_within_cap(self, tmp_path):
        config = make_config(
            tmp_path,
            llm=make_llm_config(tmp_path).llm,
            summarization=small_config(
                tmp_path, chunk_characters=120, chunk_overlap_characters=20, max_input_characters=3000
            ),
            tags=tags_config("Academic"),
        )
        recording, _, _ = make_transcribed_recording([f"s{i} " + "w" * 100 for i in range(8)])
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(len(request.content.decode()))
            system = json.loads(request.content)["messages"][0]["content"]
            content = final_summary_json() if "ALLOWED TAGS" in system else map_summary_json()
            return httpx.Response(200, content=json.dumps(omlx_envelope(content)).encode())

        result = summarize_service.summarize_one(config, recording, transport=httpx.MockTransport(handler))
        assert result["result"] == "summarized"
        assert seen and all(size <= config.summarization.max_input_characters for size in seen)


# ---------------------------------------------------------------------------
# Missing WAV / eligibility
# ---------------------------------------------------------------------------


class TestEligibility:
    def test_summarization_works_without_any_audio_source(self, tmp_path):
        config = make_config(tmp_path, llm=make_llm_config(tmp_path).llm, tags=tags_config("Academic"))
        recording, _, _ = make_transcribed_recording(["hello"])  # no AudioSource rows
        result = summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([final_summary_json()]))
        assert result["result"] == "summarized"

    def test_disabled_summarization_skips(self, tmp_path):
        config = make_config(
            tmp_path, summarization=small_config(tmp_path, enabled=False), tags=tags_config("Academic")
        )
        recording, _, _ = make_transcribed_recording(["hello"])
        result = summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([]))
        assert result["result"] == "skipped"
        assert result["reason"] == "summarization_disabled"

    def test_blank_model_raises_config_error(self, tmp_path):
        from brainlib.config import ConfigError

        config = make_config(
            tmp_path,
            llm=LLMConfig(
                provider="openai_compatible", base_url="http://127.0.0.1:1/v1", model="",
                api_key_env="BRAIN_TEST_LLM_API_KEY", temperature=0.2, timeout_seconds=600,
            ),
            tags=tags_config("Academic"),
        )
        recording, _, _ = make_transcribed_recording(["hello"])
        with pytest.raises(ConfigError):
            summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([]))

    def test_no_active_transcript_skips(self, tmp_path):
        config = make_config(tmp_path, tags=tags_config("Academic"))
        recording = Recording.objects.create(sha256="no-transcript")
        result = summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([]))
        assert result["result"] == "skipped"
        assert result["reason"] == "no_active_transcript"


# ---------------------------------------------------------------------------
# Findings 1 & 2 regression: failure bookkeeping and the state table
# ---------------------------------------------------------------------------


def _llm_config_for_pending(tmp_path):
    return make_llm_config(tmp_path)


class TestFailureBookkeeping:
    """Finding 2: every completed failure persists last_failed_attempt."""

    def _config(self, tmp_path):
        return make_config(tmp_path, llm=make_llm_config(tmp_path).llm, tags=tags_config("Academic"))

    def test_first_endpoint_failure_stores_last_failed_attempt(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        result = summarize_service.summarize_one(
            config, recording, llm_call=ScriptedLLM([LLMUnavailable("down")])
        )
        assert result["result"] == "failed"
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.FAILED
        assert recording.resummarization_failed is False
        assert recording.last_failed_attempt is not None
        assert recording.last_failed_attempt.error_code == "endpoint_unavailable"
        assert recording.last_failed_attempt.stage == AttemptStage.SUMMARIZATION

    def test_first_input_too_large_stores_last_failed_attempt(self, tmp_path):
        config = make_config(
            tmp_path,
            llm=make_llm_config(tmp_path).llm,
            summarization=small_config(tmp_path, max_total_characters=50, max_chunk_count=100),
            tags=tags_config("Academic"),
        )
        recording, _, _ = make_transcribed_recording(["x" * 100])
        summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([]))
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.FAILED
        assert recording.last_failed_attempt.error_code == "input_too_large"

    def test_empty_transcript_stores_last_failed_attempt(self, tmp_path):
        from django.utils import timezone as tz

        from workflow.models import Section, Transcript

        config = self._config(tmp_path)
        recording = Recording.objects.create(sha256="empty-transcript-rec",
                                             processing_status="transcribed",
                                             summary_status=SummaryState.MISSING)
        attempt = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=1,
            outcome=AttemptOutcome.SUCCESS, finished_at=tz.now(),
        )
        transcript = Transcript.objects.create(recording=recording, attempt=attempt, text_normalized="")
        Section.objects.create(transcript=transcript, ordinal=0, title="Full recording")
        transcript.is_active = True
        transcript.activated_at = tz.now()
        transcript.save()
        result = summarize_service.summarize_one(
            config, recording, llm_call=ScriptedLLM([])  # must never be reached
        )
        assert result["result"] == "failed"
        assert result["error_code"] == "empty_transcript"
        recording.refresh_from_db()
        assert recording.last_failed_attempt.error_code == "empty_transcript"

    def test_failed_regeneration_stores_last_failed_attempt(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([final_summary_json(title="V1")]))
        result = summarize_service.summarize_one(
            config, recording, regenerate=True,
            llm_call=ScriptedLLM([LLMTimeout(), LLMTimeout()]),
        )
        assert result["result"] == "failed"
        recording.refresh_from_db()
        assert recording.current_summary().title == "V1"
        assert recording.resummarization_failed is True
        assert recording.last_failed_attempt.error_code == "timeout"

    def test_success_clears_last_failed_attempt(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([LLMUnavailable("down")]))
        recording.refresh_from_db()
        assert recording.last_failed_attempt is not None
        result = summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([final_summary_json()]))
        assert result["result"] == "summarized"
        recording.refresh_from_db()
        assert recording.last_failed_attempt is None
        assert recording.resummarization_failed is False
        assert recording.summary_status == SummaryState.CURRENT


class TestStateMachineTable:
    """Locked state table: (summary_status, resummarization_failed,
    last_failed_attempt, auto-run eligible)."""

    @pytest.fixture
    def config(self, tmp_path):
        return make_config(tmp_path, llm=make_llm_config(tmp_path).llm, tags=tags_config("Academic"))

    def _build_row(self, config, situation, tmp_path):
        if situation == "missing":
            recording, _, _ = make_transcribed_recording(["text one"])
        elif situation == "failed_first":
            recording, _, _ = make_transcribed_recording(["text one"])
            summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([LLMUnavailable("down")]))
        elif situation == "interrupted_first":
            recording, _, _ = make_transcribed_recording(["text one"])
            make_running_attempt(recording)
            from workflow.services.pipeline import recover_interruptions

            recover_interruptions(config)
        elif situation == "current":
            recording, _, _ = make_transcribed_recording(["text one"])
            summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([final_summary_json()]))
        elif situation == "regen_failed":
            recording, _, _ = make_transcribed_recording(["text one"])
            summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([final_summary_json()]))
            summarize_service.summarize_one(
                config, recording, regenerate=True, llm_call=ScriptedLLM([LLMUnavailable("down"), LLMUnavailable("down")])
            )
        elif situation == "regen_interrupted":
            recording, _, _ = make_transcribed_recording(["text one"])
            summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([final_summary_json()]))
            make_running_attempt(recording)
            from workflow.services.pipeline import recover_interruptions

            recover_interruptions(config)
        elif situation == "retry_success":
            recording, _, _ = make_transcribed_recording(["text one"])
            summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([LLMUnavailable("down")]))
            from workflow.services.pipeline import retry

            transport = httpx.MockTransport(
                lambda request: httpx.Response(
                    200, content=json.dumps(omlx_envelope(final_summary_json())).encode()
                )
            )
            retry(config, recording, transport=transport)
        recording.refresh_from_db()
        return recording

    EXPECTED = {
        "missing": ("missing", False, False),
        "failed_first": ("failed", False, True),
        "interrupted_first": ("failed", False, True),
        "current": ("current", False, False),
        "regen_failed": ("current", True, True),
        "regen_interrupted": ("current", True, True),
        "retry_success": ("current", False, False),
    }

    @pytest.mark.parametrize("situation", list(EXPECTED))
    def test_state_table(self, config, tmp_path, situation):
        status, resummarization_failed, has_failed_attempt = self.EXPECTED[situation]
        recording = self._build_row(config, situation, tmp_path)
        assert recording.summary_status == status
        assert recording.resummarization_failed is resummarization_failed
        assert (recording.last_failed_attempt is not None) is has_failed_attempt

    def test_only_never_attempted_recordings_are_auto_processed(self, config, tmp_path, monkeypatch):
        for situation in self.EXPECTED:
            self._build_row(config, situation, tmp_path)
        calls = []

        def forbidden(config_arg, **kwargs):
            calls.append(1)
            return json.dumps(json.loads(final_summary_json()))

        monkeypatch.setattr(
            "workflow.services.summarize.llm_service.chat_completion", forbidden
        )
        pending = summarize_service.summarize_pending(config)
        assert len(pending["results"]) == 1
        assert pending["results"][0]["result"] == "summarized"
        assert pending["results"][0]["recording_id"] != ""
        # Exactly one LLM call: only the never-attempted recording.
        assert len(calls) == 1
        processed_id = pending["results"][0]["recording_id"]
        processed = Recording.objects.get(pk=processed_id)
        assert processed.summary_status == SummaryState.CURRENT

    def test_run_pipeline_does_not_retry_failed_or_interrupted(self, config, tmp_path, monkeypatch):
        failed_recording = self._build_row(config, "failed_first", tmp_path)
        interrupted_recording = self._build_row(config, "interrupted_first", tmp_path)

        def forbidden(config_arg, **kwargs):
            raise AssertionError("run_pipeline must not retry failed/interrupted summaries")

        monkeypatch.setattr(
            "workflow.services.summarize.llm_service.chat_completion", forbidden
        )
        from workflow.services.pipeline import run_pipeline

        result = run_pipeline(config)
        assert result["summarization"]["results"] == []
        failed_recording.refresh_from_db()
        assert failed_recording.summary_status == SummaryState.FAILED
        interrupted_recording.refresh_from_db()
        assert interrupted_recording.summary_status == SummaryState.FAILED

    def test_recovery_is_idempotent(self, config, tmp_path):
        recording = self._build_row(config, "interrupted_first", tmp_path)
        assert recording.summary_status == SummaryState.FAILED
        assert recording.last_failed_attempt is not None
        from workflow.services.pipeline import recover_interruptions

        before_attempts = ProcessingAttempt.objects.filter(recording=recording).count()
        before_failed = recording.last_failed_attempt_id
        for _ in range(3):
            recover_interruptions(config)
        recording.refresh_from_db()
        assert ProcessingAttempt.objects.filter(recording=recording).count() == before_attempts
        assert recording.last_failed_attempt_id == before_failed
        assert recording.summary_status == SummaryState.FAILED
        assert recording.resummarization_failed is False


# ---------------------------------------------------------------------------
# Finding 3 regression: retry must not clear markers prematurely
# ---------------------------------------------------------------------------


class TestRetryMarkerDurability:
    def _failed_regeneration(self, config):
        recording, _, _ = make_transcribed_recording(["hello world"])
        summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([final_summary_json(title="V1")]))
        summarize_service.summarize_one(
            config, recording, regenerate=True,
            llm_call=ScriptedLLM([LLMUnavailable("down"), LLMUnavailable("down")]),
        )
        recording.refresh_from_db()
        assert recording.resummarization_failed is True
        assert recording.last_failed_attempt is not None
        return recording, recording.last_failed_attempt_id

    def _config(self, tmp_path):
        return make_config(tmp_path, llm=make_llm_config(tmp_path).llm, tags=tags_config("Academic"))

    def test_marker_survives_tag_sync_failure_before_attempt_creation(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        recording, old_attempt_id = self._failed_regeneration(config)

        def broken_sync(config_arg):
            raise RuntimeError("tag sync exploded")

        monkeypatch.setattr("workflow.services.summarize.tags_service.sync_tags", broken_sync)
        with pytest.raises(RuntimeError):
            from workflow.services.pipeline import retry

            retry(config, recording)
        recording.refresh_from_db()
        assert recording.resummarization_failed is True
        assert recording.last_failed_attempt_id == old_attempt_id
        # No new summarization attempt was created (success + failed regen only).
        assert ProcessingAttempt.objects.filter(
            recording=recording, stage=AttemptStage.SUMMARIZATION
        ).count() == 2

    def test_marker_survives_attempt_creation_failure(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        recording, old_attempt_id = self._failed_regeneration(config)

        def broken_ordinal(recording_arg, stage):
            raise RuntimeError("db exploded before attempt creation")

        monkeypatch.setattr("workflow.services.summarize.next_ordinal", broken_ordinal)
        with pytest.raises(RuntimeError):
            from workflow.services.pipeline import retry

            retry(config, recording)
        recording.refresh_from_db()
        assert recording.resummarization_failed is True
        assert recording.last_failed_attempt_id == old_attempt_id

    def test_successful_retry_clears_marker_and_failed_attempt(self, tmp_path):
        config = self._config(tmp_path)
        recording, _ = self._failed_regeneration(config)
        from workflow.services.pipeline import retry

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=json.dumps(omlx_envelope(final_summary_json(title="V2"))).encode()
            )
        )
        result = retry(config, recording, transport=transport)
        assert result["result"] == "retried"
        recording.refresh_from_db()
        assert recording.resummarization_failed is False
        assert recording.last_failed_attempt is None
        assert recording.summary_status == SummaryState.CURRENT
        assert recording.current_summary().title == "V2"

    def test_failed_retry_points_to_new_attempt(self, tmp_path):
        config = self._config(tmp_path)
        recording, old_attempt_id = self._failed_regeneration(config)
        from workflow.services.pipeline import retry

        result = retry(config, recording)  # unreachable endpoint -> fails again
        assert result["result"] == "retried"
        assert result["summarize_result"]["result"] == "failed"
        recording.refresh_from_db()
        assert recording.resummarization_failed is True
        new_id = recording.last_failed_attempt_id
        assert new_id != old_attempt_id
        new_attempt = ProcessingAttempt.objects.get(pk=new_id)
        assert new_attempt.error_code == "endpoint_unavailable"
        assert new_attempt.ordinal > ProcessingAttempt.objects.get(pk=old_attempt_id).ordinal

    def test_interrupted_retry_is_recovered_to_warning(self, tmp_path):
        config = self._config(tmp_path)
        recording, _ = self._failed_regeneration(config)
        from workflow.services.pipeline import recover_interruptions

        # Simulate the retry process dying right after attempt creation:
        # a new RUNNING attempt exists; the old marker still points at the
        # old attempt until recovery updates it.
        make_running_attempt(recording)
        recover_interruptions(config)
        recording.refresh_from_db()
        assert recording.resummarization_failed is True
        assert recording.summary_status == SummaryState.CURRENT
        interrupted = ProcessingAttempt.objects.filter(
            recording=recording, stage=AttemptStage.SUMMARIZATION
        ).order_by("-ordinal").first()
        assert interrupted.outcome == AttemptOutcome.INTERRUPTED
        assert recording.last_failed_attempt_id == interrupted.pk

    def test_first_summary_retry_keeps_failed_until_success(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([LLMUnavailable("down")]))
        from workflow.services.pipeline import retry

        # During the retry the recording still shows failed (marker visible);
        # only successful persistence flips it atomically.
        seen = {}

        real_persist = summarize_service.persist_summary

        def spy_persist(**kwargs):
            seen["status_during"] = Recording.objects.get(pk=recording.pk).summary_status
            return real_persist(**kwargs)

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=json.dumps(omlx_envelope(final_summary_json())).encode()
            )
        )
        monkey = getattr(self, "_monkey", None)
        summarize_service.persist_summary = spy_persist
        try:
            result = retry(config, recording, transport=transport)
        finally:
            summarize_service.persist_summary = real_persist
        assert result["summarize_result"]["result"] == "summarized"
        assert seen["status_during"] == SummaryState.FAILED
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.CURRENT
        assert recording.last_failed_attempt is None


# ---------------------------------------------------------------------------
# Finding 4 regression: invalid-output retry covers parse + validation
# ---------------------------------------------------------------------------


class TestInvalidOutputRetry:
    """Exact call counts for the complete logical-call retry semantics."""

    def _config(self, tmp_path, **overrides):
        return make_config(
            tmp_path,
            llm=make_llm_config(tmp_path).llm,
            summarization=small_config(tmp_path, **overrides),
            tags=tags_config("Academic"),
        )

    def test_malformed_model_json_then_valid_final_two_calls(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = ScriptedLLM(["{not json at all", final_summary_json()])
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "summarized"
        assert llm.call_count == 2

    def test_invalid_final_schema_then_valid_two_calls(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        bad = json.dumps({"title": 12, "overview": "ok"})  # boolean-ish type error
        llm = ScriptedLLM([bad, final_summary_json()])
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "summarized"
        assert llm.call_count == 2

    def test_invalid_map_schema_then_valid_two_calls_for_that_map(self, tmp_path):
        config = self._config(tmp_path, chunk_characters=50, chunk_overlap_characters=0)
        recording, _, _ = make_transcribed_recording([f"segment {i} " + "w" * 40 for i in range(3)])
        bad_map = json.dumps({"overview": True, "key_points": []})  # boolean overview
        llm = ScriptedLLM([bad_map, map_summary_json(), map_summary_json(), map_summary_json(), final_summary_json()])
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "summarized"
        # 3 map calls + 1 retry on the first invalid map + 1 reduce = 5
        assert llm.call_count == 5
        assert result["chunk_count"] == 3

    def test_invalid_intermediate_reduce_then_valid(self, tmp_path):
        config = self._config(tmp_path, chunk_characters=50, chunk_overlap_characters=0)
        recording, _, _ = make_transcribed_recording([f"segment {i} " + "w" * 40 for i in range(2)])
        bad_reduce = json.dumps({"overview": "ok", "key_points": "not-a-list"})
        llm = ScriptedLLM([
            map_summary_json(), map_summary_json(),
            bad_reduce,  # final reduce, invalid
            final_summary_json(),  # retried, valid
        ])
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "summarized"
        assert llm.call_count == 4

    def test_invalid_final_reduce_twice_exactly_two_calls_failed(self, tmp_path):
        config = self._config(tmp_path, chunk_characters=50, chunk_overlap_characters=0)
        recording, _, _ = make_transcribed_recording([f"segment {i} " + "w" * 40 for i in range(2)])
        llm = ScriptedLLM([
            map_summary_json(), map_summary_json(),
            LLMInvalid("schema_validation", "bad"), LLMInvalid("schema_validation", "bad"),
        ])
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "failed"
        assert result["error_code"] == "schema_validation"
        assert llm.call_count == 4  # 2 map + exactly 2 final-reduce attempts
        assert Summary.objects.count() == 0

    def test_endpoint_unavailable_exactly_one_call(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = ScriptedLLM([LLMUnavailable("down"), final_summary_json()])
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "failed"
        assert result["error_code"] == "endpoint_unavailable"
        assert llm.call_count == 1

    def test_timeout_exactly_one_call(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = ScriptedLLM([LLMTimeout(), final_summary_json()])
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "failed"
        assert result["error_code"] == "timeout"
        assert llm.call_count == 1

    def test_http_error_exactly_one_call(self, tmp_path):
        from workflow.services.llm import LLMHTTPError

        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = ScriptedLLM([LLMHTTPError(429), final_summary_json()])
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "failed"
        assert result["error_code"] == "http_error"
        assert llm.call_count == 1

    def test_response_too_large_exactly_one_call(self, tmp_path):
        from workflow.services.llm import LLMResponseTooLarge

        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = ScriptedLLM([LLMResponseTooLarge(), final_summary_json()])
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "failed"
        assert result["error_code"] == "response_too_large"
        assert llm.call_count == 1

    def test_request_too_large_zero_calls(self, tmp_path):
        config = self._config(
            tmp_path, chunk_characters=490, max_input_characters=500, max_chunk_count=100
        )
        recording, _, _ = make_transcribed_recording(["a" * 480])

        def forbidden(**kwargs):
            raise AssertionError("request too large must fail before any call")

        result = summarize_service.summarize_one(config, recording, llm_call=forbidden)
        assert result["result"] == "failed"
        assert result["error_code"] == "input_too_large"

    def test_both_invalid_preserve_final_specific_code(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = ScriptedLLM([
            LLMInvalid("malformed_model_json", "first"), LLMInvalid("malformed_model_json", "second"),
        ])
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "failed"
        assert result["error_code"] == "malformed_model_json"
        assert llm.call_count == 2
        attempt = ProcessingAttempt.objects.filter(
            recording=recording, stage=AttemptStage.SUMMARIZATION
        ).first()
        assert attempt.error_code == "malformed_model_json"

    def test_no_prompt_or_transcript_or_secrets_in_attempt_error(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["CONFIDENTIAL transcript body 我嘅秘密"])
        llm = ScriptedLLM([
            LLMInvalid("malformed_model_json", "bad output"), LLMInvalid("malformed_model_json", "bad again"),
        ])
        summarize_service.summarize_one(config, recording, llm_call=llm)
        attempt = ProcessingAttempt.objects.filter(
            recording=recording, stage=AttemptStage.SUMMARIZATION
        ).first()
        stored = json.dumps(attempt.cli_args_json) + attempt.error_message + attempt.error_code
        assert "CONFIDENTIAL" not in stored
        assert "秘密" not in stored
        assert "bad output" not in stored
        assert "super-secret" not in stored


# ---------------------------------------------------------------------------
# Error hygiene (Finding 2): no sensitive content persisted or emitted
# ---------------------------------------------------------------------------

KEY_SENTINEL = "sk-SUPERSECRET-KEY-7f3a9"
TRANSCRIPT_SENTINEL = "TRANSCRIPT-SENTINEL-私人內容-1a2b"
PROMPT_SENTINEL = "PROMPT-SENTINEL-9d81c"
RESPONSE_BODY_SENTINEL = "RESPONSE-BODY-SENTINEL-42xy"
URL_TOKEN_SENTINEL = "urltoken=SECRETVALUE123"
PRIVATE_VALUE_SENTINEL = "PRIVATE-VALUE-藏-8817"


def _combined_surfaces(attempt, log_text="", cli_out="", cli_err=""):
    """Every persistence and emission surface, joined for sentinel checks."""
    return (
        json.dumps(attempt.cli_args_json)
        + attempt.error_code
        + attempt.error_message
        + log_text
        + cli_out
        + cli_err
    )


class TestErrorHygiene:
    """Sentinel-based proof that failures never persist or emit sensitive
    content. The assertions are effective: the combined surface is
    asserted non-empty and to contain the expected stable error codes."""

    def _config(self, tmp_path):
        return make_config(
            tmp_path,
            llm=make_llm_config(tmp_path).llm,
            summarization=small_config(tmp_path),
            tags=tags_config("Academic"),
        )

    def _attempt(self, recording):
        return ProcessingAttempt.objects.filter(
            recording=recording, stage=AttemptStage.SUMMARIZATION
        ).order_by("-ordinal").first()

    def test_malformed_model_response_sentinel_absent_everywhere(self, tmp_path, caplog):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording([TRANSCRIPT_SENTINEL])
        bad_content = f'{{"leak": "{RESPONSE_BODY_SENTINEL}", "title": "{PRIVATE_VALUE_SENTINEL}"'
        llm = ScriptedLLM([bad_content, bad_content])  # retried once, both invalid
        with caplog.at_level("DEBUG"):
            result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "failed"
        assert result["error_code"] == "malformed_model_json"
        attempt = self._attempt(recording)
        surface = _combined_surfaces(attempt, caplog.text)
        assert surface  # non-empty: the assertions below can actually bite
        assert "malformed_model_json" in surface  # stable code IS retained
        for sentinel in (
            RESPONSE_BODY_SENTINEL, PRIVATE_VALUE_SENTINEL, TRANSCRIPT_SENTINEL,
            PROMPT_SENTINEL, KEY_SENTINEL, URL_TOKEN_SENTINEL,
        ):
            assert sentinel not in surface

    def test_schema_invalid_response_private_value_absent(self, tmp_path, caplog):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording([TRANSCRIPT_SENTINEL])
        bad_content = json.dumps({
            "title": PRIVATE_VALUE_SENTINEL, "overview": "ok",
            "key_points": ["x" * 2000],  # over-limit item carrying the value
        })
        llm = ScriptedLLM([bad_content, bad_content])
        with caplog.at_level("DEBUG"):
            result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["error_code"] == "schema_validation"
        attempt = self._attempt(recording)
        surface = _combined_surfaces(attempt, caplog.text)
        assert "schema_validation" in surface
        for sentinel in (PRIVATE_VALUE_SENTINEL, TRANSCRIPT_SENTINEL, PROMPT_SENTINEL, KEY_SENTINEL):
            assert sentinel not in surface

    def test_http_error_sensitive_body_not_retained(self, tmp_path, caplog):
        import httpx

        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording([TRANSCRIPT_SENTINEL])
        body = json.dumps({"error": f"internal leak {RESPONSE_BODY_SENTINEL} {KEY_SENTINEL}"}).encode()
        transport = httpx.MockTransport(lambda request: httpx.Response(500, content=body))
        with caplog.at_level("DEBUG"):
            result = summarize_service.summarize_one(config, recording, transport=transport)
        assert result["result"] == "failed"
        assert result["error_code"] == "http_error"
        attempt = self._attempt(recording)
        surface = _combined_surfaces(attempt, caplog.text)
        assert surface
        # Only the status code/type is retained.
        assert "500" in surface or "http_500" in surface
        for sentinel in (RESPONSE_BODY_SENTINEL, KEY_SENTINEL, TRANSCRIPT_SENTINEL, "internal leak"):
            assert sentinel not in surface

    def test_endpoint_exception_message_sanitized(self, tmp_path, caplog):
        import httpx

        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording([TRANSCRIPT_SENTINEL])

        def handler(request):
            raise httpx.ConnectError(f"refused at /v1?{URL_TOKEN_SENTINEL} key={KEY_SENTINEL}")

        with caplog.at_level("DEBUG"):
            result = summarize_service.summarize_one(config, recording, transport=httpx.MockTransport(handler))
        assert result["result"] == "failed"
        assert result["error_code"] == "endpoint_unavailable"
        attempt = self._attempt(recording)
        surface = _combined_surfaces(attempt, caplog.text)
        assert surface
        # Only the sanitized exception type is retained (never the raw
        # exception message).
        assert "LLMUnavailable" in surface
        for sentinel in (URL_TOKEN_SENTINEL, "SECRETVALUE123", KEY_SENTINEL, TRANSCRIPT_SENTINEL):
            assert sentinel not in surface

    def test_llm_call_exception_message_sanitized(self, tmp_path, caplog):
        # A raw exception from an injected call (e.g. a misbehaving script)
        # is classified without leaking its message.
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording([TRANSCRIPT_SENTINEL])
        llm = ScriptedLLM([LLMUnavailable(f"boom {KEY_SENTINEL} {URL_TOKEN_SENTINEL}"),
                           LLMUnavailable("boom")])
        with caplog.at_level("DEBUG"):
            result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["error_code"] == "endpoint_unavailable"
        attempt = self._attempt(recording)
        surface = _combined_surfaces(attempt, caplog.text)
        assert KEY_SENTINEL not in surface
        assert "SECRETVALUE123" not in surface

    def test_prompt_and_transcript_sentinels_absent_from_attempt(self, tmp_path, caplog):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording([TRANSCRIPT_SENTINEL])
        llm = ScriptedLLM([LLMTimeout(), LLMTimeout()])
        with caplog.at_level("DEBUG"):
            summarize_service.summarize_one(config, recording, llm_call=llm)
        attempt = self._attempt(recording)
        surface = _combined_surfaces(attempt, caplog.text)
        assert surface
        assert "timeout" in surface
        for sentinel in (TRANSCRIPT_SENTINEL, PROMPT_SENTINEL, KEY_SENTINEL):
            assert sentinel not in surface

    def test_api_key_from_env_absent_from_attempt_and_errors(self, tmp_path, monkeypatch, caplog):
        import httpx

        monkeypatch.setenv("BRAIN_TEST_LLM_API_KEY", KEY_SENTINEL)
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording([TRANSCRIPT_SENTINEL])
        transport = httpx.MockTransport(lambda request: httpx.Response(503))
        with caplog.at_level("DEBUG"):
            result = summarize_service.summarize_one(config, recording, transport=transport)
        assert result["error_code"] == "http_error"
        attempt = self._attempt(recording)
        surface = _combined_surfaces(attempt, caplog.text)
        assert KEY_SENTINEL not in surface
        assert "Bearer" not in surface


# ---------------------------------------------------------------------------
# Stage-aware recovery regression (cross-stage finding)
# ---------------------------------------------------------------------------


class TestStageAwareRecovery:
    """Unrelated routing/transcription interruptions must never reopen a
    failed Summary for automatic retry."""

    def _config(self, tmp_path):
        return make_config(tmp_path, llm=make_llm_config(tmp_path).llm, tags=tags_config("Academic"))

    def _leave_unfinished_attempt(self, recording, stage, ordinal=None):
        from django.utils import timezone as tz

        return ProcessingAttempt.objects.create(
            recording=recording, stage=stage,
            ordinal=ordinal or ProcessingAttempt.objects.filter(recording=recording).count() + 1,
            started_at=tz.now(),
        )

    def _failed_first_summary(self, config):
        recording, _, _ = make_transcribed_recording(["hello world"])
        summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([LLMUnavailable("down")]))
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.FAILED
        return recording

    def _failed_regeneration(self, config):
        recording, _, _ = make_transcribed_recording(["hello world"])
        summarize_service.summarize_one(config, recording, llm_call=ScriptedLLM([final_summary_json(title="V1")]))
        summarize_service.summarize_one(
            config, recording, regenerate=True,
            llm_call=ScriptedLLM([LLMUnavailable("down"), LLMUnavailable("down")]),
        )
        recording.refresh_from_db()
        assert recording.resummarization_failed is True
        return recording

    @pytest.mark.parametrize("stage", [AttemptStage.ROUTING, AttemptStage.TRANSCRIPTION])
    def test_unrelated_interruption_keeps_failed_summary(self, tmp_path, stage):
        from workflow.services.pipeline import recover_interruptions

        config = self._config(tmp_path)
        recording = self._failed_first_summary(config)
        summary_failed_attempt_id = recording.last_failed_attempt_id

        # An unrelated attempt of another stage is left unfinished.
        unfinished = self._leave_unfinished_attempt(recording, stage)
        recoveries = recover_interruptions(config)
        assert recoveries["recovered_attempts"] == 1

        unfinished.refresh_from_db()
        assert unfinished.outcome == AttemptOutcome.INTERRUPTED
        assert unfinished.error_code == "process_interrupted"
        recording.refresh_from_db()
        # Summary state completely untouched by the cross-stage recovery.
        assert recording.summary_status == SummaryState.FAILED
        assert recording.resummarization_failed is False
        assert recording.last_failed_attempt_id == summary_failed_attempt_id
        assert recording.last_failed_attempt_id != unfinished.pk

    @pytest.mark.parametrize("stage", [AttemptStage.ROUTING, AttemptStage.TRANSCRIPTION])
    def test_unrelated_interruption_keeps_failed_regeneration(self, tmp_path, stage):
        from workflow.services.pipeline import recover_interruptions

        config = self._config(tmp_path)
        recording = self._failed_regeneration(config)
        summary_failed_attempt_id = recording.last_failed_attempt_id

        self._leave_unfinished_attempt(recording, stage)
        recover_interruptions(config)
        recording.refresh_from_db()
        assert recording.current_summary().title == "V1"
        assert recording.summary_status == SummaryState.CURRENT
        assert recording.resummarization_failed is True
        assert recording.last_failed_attempt_id == summary_failed_attempt_id

    def test_summarization_interruption_still_recovered(self, tmp_path):
        from workflow.services.pipeline import recover_interruptions

        config = self._config(tmp_path)
        recording = self._failed_first_summary(config)
        summary_failed_attempt_id = recording.last_failed_attempt_id
        # A NEW summarization attempt is left running (crashed retry).
        running = self._leave_unfinished_attempt(recording, AttemptStage.SUMMARIZATION)
        recover_interruptions(config)
        recording.refresh_from_db()
        running.refresh_from_db()
        assert running.outcome == AttemptOutcome.INTERRUPTED
        assert recording.summary_status == SummaryState.FAILED
        assert recording.resummarization_failed is False
        # Marker moved to the recovered attempt (the authoritative event).
        assert recording.last_failed_attempt_id == running.pk
        assert recording.last_failed_attempt_id != summary_failed_attempt_id

    def test_mixed_recovery_one_pass_each_stage_reconciled_only_for_itself(self, tmp_path):
        from workflow.services.pipeline import recover_interruptions

        config = self._config(tmp_path)
        # Routing recording: summary state at an unusual value to prove it
        # is not modified by its routing recovery.
        routing_rec, _, _ = make_transcribed_recording(["r text"])
        routing_rec.summary_status = SummaryState.FAILED
        routing_rec.save(update_fields=["summary_status"])
        self._leave_unfinished_attempt(routing_rec, AttemptStage.ROUTING)

        # Transcription recording: likewise untouched summary state.
        transcription_rec, _, _ = make_transcribed_recording(["t text"])
        transcription_rec.summary_status = SummaryState.FAILED
        transcription_rec.save(update_fields=["summary_status"])
        self._leave_unfinished_attempt(transcription_rec, AttemptStage.TRANSCRIPTION)

        # Summarization recording: gets the approved interrupted treatment.
        summary_rec, _, _ = make_transcribed_recording(["s text"])
        self._leave_unfinished_attempt(summary_rec, AttemptStage.SUMMARIZATION)

        recoveries = recover_interruptions(config)
        assert recoveries["recovered_attempts"] == 3

        routing_rec.refresh_from_db()
        assert routing_rec.summary_status == SummaryState.FAILED  # untouched
        assert routing_rec.last_failed_attempt is None
        transcription_rec.refresh_from_db()
        assert transcription_rec.summary_status == SummaryState.FAILED  # untouched
        assert transcription_rec.last_failed_attempt is None
        summary_rec.refresh_from_db()
        assert summary_rec.summary_status == SummaryState.FAILED  # interrupted first attempt
        assert summary_rec.last_failed_attempt.outcome == AttemptOutcome.INTERRUPTED
        assert summary_rec.resummarization_failed is False

    def test_cross_stage_recovery_then_run_makes_zero_llm_calls(self, tmp_path, monkeypatch):
        from workflow.services.pipeline import recover_interruptions, run_pipeline

        config = self._config(tmp_path)
        recording = self._failed_first_summary(config)
        self._leave_unfinished_attempt(recording, AttemptStage.ROUTING)
        self._leave_unfinished_attempt(recording, AttemptStage.TRANSCRIPTION)
        before_attempts = ProcessingAttempt.objects.filter(
            recording=recording, stage=AttemptStage.SUMMARIZATION
        ).count()

        def forbidden(config_arg, **kwargs):
            raise AssertionError("cross-stage recovery must not trigger summarization")

        monkeypatch.setattr("workflow.services.summarize.llm_service.chat_completion", forbidden)
        result = run_pipeline(config)
        assert result["summarization"]["results"] == []
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.FAILED
        assert ProcessingAttempt.objects.filter(
            recording=recording, stage=AttemptStage.SUMMARIZATION
        ).count() == before_attempts  # no new summarization attempt

    def test_recovery_remains_idempotent_across_stages(self, tmp_path):
        from workflow.services.pipeline import recover_interruptions

        config = self._config(tmp_path)
        recording = self._failed_first_summary(config)
        failed_id = recording.last_failed_attempt_id
        self._leave_unfinished_attempt(recording, AttemptStage.ROUTING)
        self._leave_unfinished_attempt(recording, AttemptStage.SUMMARIZATION)

        recover_interruptions(config)
        recording.refresh_from_db()
        recovered_summarization_id = recording.last_failed_attempt_id
        attempt_count = ProcessingAttempt.objects.filter(recording=recording).count()

        for _ in range(3):
            assert recover_interruptions(config)["recovered_attempts"] == 0
        recording.refresh_from_db()
        assert ProcessingAttempt.objects.filter(recording=recording).count() == attempt_count
        assert recording.last_failed_attempt_id == recovered_summarization_id
        assert recording.summary_status == SummaryState.FAILED

    def test_defensive_reconcile_never_upgrades_failed_to_missing(self, tmp_path):
        # Contract: without a recovered attempt, failed stays failed even
        # though no current summary exists.
        config = self._config(tmp_path)
        recording = self._failed_first_summary(config)
        failed_id = recording.last_failed_attempt_id
        changed = summarize_service.reconcile_recording_summary_state(recording)
        assert changed is False
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.FAILED
        assert recording.last_failed_attempt_id == failed_id

    def test_new_transcript_activation_still_sets_missing(self, tmp_path):
        from django.utils import timezone as tz

        from workflow.models import Section, Transcript, TranscriptSegment

        config = self._config(tmp_path)
        recording = self._failed_first_summary(config)
        attempt2 = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=9,
            outcome=AttemptOutcome.SUCCESS, finished_at=tz.now(),
        )
        transcript2 = Transcript.objects.create(recording=recording, attempt=attempt2, text_normalized="new")
        TranscriptSegment.objects.create(transcript=transcript2, ordinal=0, text="new")
        Section.objects.create(transcript=transcript2, ordinal=0, title="Full recording")
        Transcript.objects.exclude(pk=transcript2.pk).filter(recording=recording).update(is_active=False)
        Transcript.objects.filter(pk=transcript2.pk).update(is_active=True, activated_at=tz.now())
        recording.summary_status = SummaryState.MISSING
        recording.resummarization_failed = False
        recording.save()
        # The genuinely new transcript is auto-run eligible.
        pending = summarize_service.summarize_pending(config)
        assert [r["recording_id"] for r in pending["results"]] == [recording.pk]
