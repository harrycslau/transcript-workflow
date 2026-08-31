"""Helpers to build AppConfig objects in tests."""

from __future__ import annotations

from brainlib.config import (
    AppConfig,
    EmbeddingConfig,
    LLMConfig,
    MacWhisperConfig,
    RetentionConfig,
    StorageConfig,
    TagSpec,
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
    return AppConfig(
        config_path=overrides.pop("config_path", tmp_path / "config.yaml"),
        storage=storage,
        macwhisper=overrides.pop(
            "macwhisper",
            MacWhisperConfig(command=str(tmp_path / "nonexistent" / "mw"), model=None, language="auto", speakers=True, output_format="json"),
        ),
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
