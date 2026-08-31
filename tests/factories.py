"""Helpers to build AppConfig objects in tests."""

from __future__ import annotations

from brainlib.config import (
    AppConfig,
    EmbeddingConfig,
    LLMConfig,
    MacWhisperConfig,
    RetentionConfig,
    RoutingConfig,
    RoutingProfile,
    StorageConfig,
    TagSpec,
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
        initial_tags=overrides.pop("initial_tags", [TagSpec(name="Unknown", description="Unclassifiable")]),
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
            "output_format": "json",
            "file_stable_seconds": config.macwhisper.file_stable_seconds,
            "cli_timeout_seconds": config.macwhisper.cli_timeout_seconds,
            "routing": {
                "enabled": config.macwhisper.routing.enabled,
                "auto_transcribe": config.macwhisper.routing.auto_transcribe,
                "confidence_threshold": config.macwhisper.routing.confidence_threshold,
                "default_profile": config.macwhisper.routing.default_profile,
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
        "initial_tags": [{"name": t.name, "description": t.description} for t in config.initial_tags],
        "timezone": config.timezone,
    }
    path = tmp_path / "cli-config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("BRAIN_CONFIG", str(path))
    return config
