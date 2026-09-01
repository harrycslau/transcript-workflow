"""Helpers to build AppConfig objects in tests."""

from __future__ import annotations

from brainlib.config import (
    AppConfig,
    EmbeddingConfig,
    HeuristicAutoRouteConfig,
    LLMConfig,
    MacWhisperConfig,
    RetentionConfig,
    RoutingConfig,
    RoutingProfile,
    StorageConfig,
    SummarizationConfig,
    TagSpec,
    TagsConfig,
    WebConfig,
)


def default_web(**overrides) -> WebConfig:
    return WebConfig(
        recordings_per_page=overrides.pop("recordings_per_page", 25),
        transcript_segments_per_page=overrides.pop("transcript_segments_per_page", 200),
    )


def default_summarization(**overrides) -> SummarizationConfig:
    return SummarizationConfig(
        enabled=overrides.pop("enabled", True),
        prompt_version=overrides.pop("prompt_version", "1"),
        max_input_characters=overrides.pop("max_input_characters", 120000),
        chunk_characters=overrides.pop("chunk_characters", 24000),
        chunk_overlap_characters=overrides.pop("chunk_overlap_characters", 1000),
        max_chunk_count=overrides.pop("max_chunk_count", 8),
        max_total_characters=overrides.pop("max_total_characters", 960000),
        temperature=overrides.pop("temperature", 0.2),
        max_output_tokens=overrides.pop("max_output_tokens", 3000),
    )


def default_tags() -> TagsConfig:
    return TagsConfig(
        allowed=(
            TagSpec(name="Family", description="Family matters"),
            TagSpec(name="Academic", description="Academic work"),
            TagSpec(name="Unknown", description="Unclassifiable"),
        )
    )


def default_heuristic(**overrides) -> HeuristicAutoRouteConfig:
    return HeuristicAutoRouteConfig(
        enabled=overrides.pop("enabled", True),
        min_non_silent_windows=overrides.pop("min_non_silent_windows", 2),
        min_cjk_ratio=overrides.pop("min_cjk_ratio", 0.60),
        cantonese_enabled=overrides.pop("cantonese_enabled", True),
        cantonese_min_score=overrides.pop("cantonese_min_score", 4.0),
        mandarin_enabled=overrides.pop("mandarin_enabled", True),
        mandarin_min_score=overrides.pop("mandarin_min_score", 4.0),
        dominance_ratio=overrides.pop("dominance_ratio", 3.0),
        max_opposing_score=overrides.pop("max_opposing_score", 0.5),
    )


def default_routing(**overrides) -> RoutingConfig:
    profiles = overrides.pop(
        "profiles",
        {
            "cantonese": RoutingProfile(name="cantonese", model="apple:zh-HK", language=None),
            "mandarin": RoutingProfile(name="mandarin", model="apple:zh-CN", language=None),
            "european": RoutingProfile(name="european", model="parakeet-pro:nvidia_parakeet-v3", language=None),
            "european_small": RoutingProfile(
                name="european_small", model="parakeet-pro:nvidia_parakeet-v3_494MB", language=None, manual_only=True
            ),
        },
    )
    return RoutingConfig(
        enabled=overrides.pop("enabled", True),
        auto_transcribe=overrides.pop("auto_transcribe", True),
        confidence_threshold=overrides.pop("confidence_threshold", 0.80),
        default_profile=overrides.pop("default_profile", "european"),
        profiles=profiles,
        heuristic_auto_route=overrides.pop("heuristic_auto_route", None) or default_heuristic(),
    )


def make_config(tmp_path, **overrides) -> AppConfig:
    root = tmp_path / "data"
    storage = overrides.pop(
        "storage",
        StorageConfig(
            inbox=root / "inbox",
            database=root / "database" / "brain.sqlite3",
            transcripts=root / "transcripts",
            exports=root / "exports",
            logs=root / "logs",
            temp=root / "temp",
        ),
    )
    macwhisper = overrides.pop(
        "macwhisper",
        None,
    ) or MacWhisperConfig(
        command=str(tmp_path / "nonexistent" / "mw"),
        model=None,
        language="auto",
        speakers=True,
        # Most transcription tests feed fake RIFF bytes and mock the
        # runner; normalization tests opt in explicitly.
        normalize_input=overrides.pop("normalize_input", False),
        speakers_fallback=overrides.pop("speakers_fallback", False),
        output_format="json",
        file_stable_seconds=overrides.pop("file_stable_seconds", 30),
        cli_timeout_seconds=overrides.pop("cli_timeout_seconds", 7200),
        routing=overrides.pop("routing", default_routing()),
        legacy_model_notice=None,
    )
    return AppConfig(
        config_path=overrides.pop("config_path", tmp_path / "config.yaml"),
        storage=storage,
        macwhisper=macwhisper,
        llm=overrides.pop(
            "llm",
            LLMConfig(
                provider="openai_compatible",
                base_url="http://127.0.0.1:1/v1",
                model="",
                api_key_env="BRAIN_TEST_LLM_API_KEY",
                temperature=0.2,
                timeout_seconds=600,
            ),
        ),
        embedding=overrides.pop(
            "embedding",
            EmbeddingConfig(base_url="http://127.0.0.1:1/v1", model="", api_key_env="BRAIN_TEST_LLM_API_KEY"),
        ),
        retention=overrides.pop(
            "retention",
            RetentionConfig(enabled=False, audio_days=3, delete_mode="permanent", require_transcript=True, require_summary=True),
        ),
        summarization=overrides.pop("summarization", None) or default_summarization(),
        tags=overrides.pop("tags", None) or default_tags(),
        initial_tags=overrides.pop("initial_tags", [TagSpec(name="Unknown", description="Unclassifiable")]),
        web=overrides.pop("web", None) or default_web(),
    )


def write_cli_config(tmp_path, monkeypatch, **kwargs):
    """Write a YAML config matching make_config(tmp_path) and point
    BRAIN_CONFIG at it, so cli.main() operates on the same storage as
    the test fixtures."""
    import yaml

    config = make_config(tmp_path, **kwargs)
    data = {
        "storage": {name: str(getattr(config.storage, name)) for name in
                    ("inbox", "database", "transcripts", "exports", "logs", "temp")},
        "macwhisper": {
            "command": config.macwhisper.command,
            "model": None,
            "language": "auto",
            "speakers": True,
            "normalize_input": config.macwhisper.normalize_input,
            "speakers_fallback": config.macwhisper.speakers_fallback,
            "output_format": "json",
            "file_stable_seconds": config.macwhisper.file_stable_seconds,
            "cli_timeout_seconds": config.macwhisper.cli_timeout_seconds,
            "routing": {
                "enabled": config.macwhisper.routing.enabled,
                "auto_transcribe": config.macwhisper.routing.auto_transcribe,
                "confidence_threshold": config.macwhisper.routing.confidence_threshold,
                "default_profile": config.macwhisper.routing.default_profile,
                "heuristic_auto_route": {
                    "enabled": config.macwhisper.routing.heuristic_auto_route.enabled,
                    "min_non_silent_windows": config.macwhisper.routing.heuristic_auto_route.min_non_silent_windows,
                    "min_cjk_ratio": config.macwhisper.routing.heuristic_auto_route.min_cjk_ratio,
                    "cantonese_enabled": config.macwhisper.routing.heuristic_auto_route.cantonese_enabled,
                    "cantonese_min_score": config.macwhisper.routing.heuristic_auto_route.cantonese_min_score,
                    "mandarin_enabled": config.macwhisper.routing.heuristic_auto_route.mandarin_enabled,
                    "mandarin_min_score": config.macwhisper.routing.heuristic_auto_route.mandarin_min_score,
                    "dominance_ratio": config.macwhisper.routing.heuristic_auto_route.dominance_ratio,
                    "max_opposing_score": config.macwhisper.routing.heuristic_auto_route.max_opposing_score,
                },
                "profiles": {
                    p.name: {"model": p.model, "language": p.language, "manual_only": p.manual_only}
                    for p in config.macwhisper.routing.profiles.values()
                },
            },
        },
        "llm": {
            "provider": config.llm.provider,
            "base_url": config.llm.base_url,
            "model": config.llm.model,
            "api_key_env": config.llm.api_key_env,
            "temperature": config.llm.temperature,
            "timeout_seconds": config.llm.timeout_seconds,
        },
        "embedding": {
            "base_url": config.embedding.base_url,
            "model": config.embedding.model,
            "api_key_env": config.embedding.api_key_env,
        },
        "retention": {
            "enabled": config.retention.enabled,
            "audio_days": config.retention.audio_days,
            "delete_mode": config.retention.delete_mode,
            "require_transcript": config.retention.require_transcript,
            "require_summary": config.retention.require_summary,
        },
        "summarization": {
            "enabled": config.summarization.enabled,
            "prompt_version": config.summarization.prompt_version,
            "max_input_characters": config.summarization.max_input_characters,
            "chunk_characters": config.summarization.chunk_characters,
            "chunk_overlap_characters": config.summarization.chunk_overlap_characters,
            "max_chunk_count": config.summarization.max_chunk_count,
            "max_total_characters": config.summarization.max_total_characters,
            "temperature": config.summarization.temperature,
            "max_output_tokens": config.summarization.max_output_tokens,
        },
        "tags": {
            "allowed": [{"name": t.name, "description": t.description} for t in config.tags.allowed]
        },
        "web": {
            "recordings_per_page": config.web.recordings_per_page,
            "transcript_segments_per_page": config.web.transcript_segments_per_page,
        },
        "timezone": config.timezone,
    }
    path = tmp_path / "cli-config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("BRAIN_CONFIG", str(path))
    return config


# ---------------------------------------------------------------------------
# Synthetic recordings / transcripts / oMLX responses (no audio files)
# ---------------------------------------------------------------------------


def make_transcribed_recording(texts, *, sha: str | None = None, summary_status=None):
    """Create a Recording with an active transcript, segments, a
    whole-recording Section, and a successful transcription attempt.

    No AudioSource is created: summarization must work without any WAV.
    Returns (recording, transcript, section).
    """
    from django.utils import timezone as tz

    from workflow.models import (
        AttemptOutcome,
        AttemptStage,
        ProcessingAttempt,
        ProcessingStatus,
        Recording,
        Section,
        SummaryState,
        Transcript,
        TranscriptSegment,
    )

    recording = Recording.objects.create(
        sha256=sha or f"synthetic-{Recording.objects.count()}-{tz.now().timestamp()}",
        duration_seconds=60.0,
        processing_status=ProcessingStatus.TRANSCRIBED,
        summary_status=SummaryState.MISSING if summary_status is None else summary_status,
    )
    attempt = ProcessingAttempt.objects.create(
        recording=recording,
        stage=AttemptStage.TRANSCRIPTION,
        ordinal=1,
        outcome=AttemptOutcome.SUCCESS,
        finished_at=tz.now(),
    )
    transcript = Transcript.objects.create(
        recording=recording, attempt=attempt, text_normalized="\n".join(texts)
    )
    TranscriptSegment.objects.bulk_create(
        [
            TranscriptSegment(
                transcript=transcript, ordinal=i, start_ms=i * 1000, end_ms=(i + 1) * 1000, text=text
            )
            for i, text in enumerate(texts)
        ]
    )
    section = Section.objects.create(transcript=transcript, ordinal=0, title="Full recording")
    transcript.is_active = True
    transcript.activated_at = tz.now()
    transcript.save()
    return recording, transcript, section


def omlx_envelope(content: str) -> dict:
    """An OpenAI-compatible chat-completion envelope carrying ``content``."""
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def final_summary_json(**overrides) -> str:
    """A valid final-schema model response as a JSON string."""
    payload = {
        "title": "Meeting about grading",
        "overview": "Discussed grading plans.",
        "key_points": ["Grading starts Monday"],
        "action_items": [{"text": "Prepare rubric", "owner": None, "due_date": None}],
        "people": ["Alice"],
        "organizations": [],
        "topics": ["grading"],
        "suggested_tags": ["Academic"],
        "language": "en",
    }
    payload.update(overrides)
    import json

    return json.dumps(payload, ensure_ascii=False)


def map_summary_json(**overrides) -> str:
    payload = {"overview": "Part summary.", "key_points": ["Point one"]}
    payload.update(overrides)
    import json

    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Step 4 web-test helpers (synthetic only; no audio, no network)
# ---------------------------------------------------------------------------


def make_tag(name, description="", *, configured=True):
    """Create a Tag row directly (bypasses YAML sync — UI tests only)."""
    from brainlib.config import tag_name_key
    from workflow.models import Tag

    return Tag.objects.create(
        name=name, name_key=tag_name_key(name), description=description, is_configured=configured
    )


def make_tag_assignment(recording, tag, *, origin="suggested", active=True, source_summary=None):
    from django.utils import timezone as tz

    from workflow.models import TagAssignment

    assignment = TagAssignment.objects.create(
        recording=recording,
        tag=tag,
        origin=origin,
        source_summary=source_summary,
        is_active=active,
        # Inactive rows must carry an actor at INSERT time to satisfy
        # the deactivation-state check constraint.
        deactivated_by="" if active else "model",
    )
    if not active:
        assignment.deactivated_at = tz.now()
        assignment.save()
    return assignment


def make_summary_version(recording, transcript, section, *, title="Meeting about grading",
                         overview="Discussed grading plans.", is_active=True, **field_overrides):
    """Create a Summary row + its summarization attempt directly.

    Mirrors what persist_summary writes (structured payload, provenance)
    without running any oMLX work. Updates the recording's summary
    status to CURRENT when the summary is active.
    """
    from django.utils import timezone as tz

    from workflow.models import (
        AttemptOutcome,
        AttemptStage,
        ProcessingAttempt,
        Summary,
        SummaryState,
    )

    last_attempt = ProcessingAttempt.objects.filter(
        recording=recording, stage=AttemptStage.SUMMARIZATION
    ).order_by("-ordinal").first()
    attempt = ProcessingAttempt.objects.create(
        recording=recording,
        stage=AttemptStage.SUMMARIZATION,
        ordinal=(last_attempt.ordinal + 1) if last_attempt else 1,
        outcome=AttemptOutcome.SUCCESS,
        finished_at=tz.now(),
        model_id="test-model",
    )
    last = Summary.objects.filter(recording=recording).order_by("-ordinal").first()
    fields = dict(
        title=title,
        overview=overview,
        key_points=["Point one"],
        action_items=[{"text": "Do a thing", "owner": None, "due_date": None}],
        people=["Alice"],
        organizations=[],
        topics=["grading"],
        language="en",
        suggested_tags_raw={"suggested": [], "rejected": []},
        model_id="test-model",
        prompt_version="1",
        parser_version="1",
        chunk_count=1,
        input_characters=100,
        generation_mode="manual",
    )
    fields.update(field_overrides)
    summary = Summary.objects.create(
        recording=recording,
        transcript=transcript,
        section=section,
        attempt=attempt,
        ordinal=(last.ordinal + 1) if last else 1,
        is_active=is_active,
        activated_at=tz.now() if is_active else None,
        **fields,
    )
    if is_active:
        recording.summary_status = SummaryState.CURRENT
        recording.save(update_fields=["summary_status"])
    return summary
