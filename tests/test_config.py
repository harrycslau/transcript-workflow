"""Tests for YAML configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from brainlib.config import (
    ConfigError,
    config_file_path,
    load_config,
    project_root,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "config.example.yaml"


def write_config(tmp_path: Path, data, name: str = "config.yaml") -> Path:
    path = tmp_path / name
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def minimal_valid() -> dict:
    return {
        "storage": {
            "inbox": "/tmp/x/inbox",
            "database": "/tmp/x/database/brain.sqlite3",
            "transcripts": "/tmp/x/transcripts",
            "exports": "/tmp/x/exports",
            "logs": "/tmp/x/logs",
            "temp": "/tmp/x/temp",
        },
        "macwhisper": {
            "command": "/usr/local/bin/mw",
            "model": None,
            "language": "auto",
            "speakers": True,
            "output_format": "json",
        },
        "llm": {"base_url": "http://127.0.0.1:1/v1"},
        "embedding": {"base_url": "http://127.0.0.1:1/v1"},
        "retention": {
            "enabled": False,
            "audio_days": 3,
            "delete_mode": "permanent",
            "require_transcript": True,
            "require_summary": True,
        },
    }


class TestExampleConfig:
    def test_example_config_loads_with_expected_shape(self):
        config = load_config(EXAMPLE)
        assert config.llm.provider == "openai_compatible"
        assert config.llm.api_key_env == "BRAIN_LLM_API_KEY"
        assert config.llm.temperature == 0.2
        assert config.llm.timeout_seconds == 600
        assert config.embedding.api_key_env == "BRAIN_LLM_API_KEY"
        assert config.macwhisper.command == "/usr/local/bin/mw"
        assert config.macwhisper.model is None
        assert config.macwhisper.speakers is True
        assert config.retention.enabled is False
        assert config.retention.audio_days == 3
        assert [t.name for t in config.tags.allowed] == [
            "Family", "Seminar", "Academic", "Read2Learn", "WisdomEd", "Unknown",
        ]
        assert config.summarization.enabled is True
        assert config.summarization.max_input_characters == 120000
        assert config.summarization.chunk_characters == 24000
        assert config.summarization.max_total_characters == 960000

    def test_relative_paths_resolve_against_project_root(self):
        config = load_config(EXAMPLE)
        assert config.storage.inbox == project_root() / "data" / "inbox"
        assert config.storage.database == project_root() / "data" / "database" / "brain.sqlite3"
        assert config.storage.inbox.is_absolute()


class TestMissingAndMalformed:
    def test_missing_config_gives_concise_error(self, tmp_path):
        with pytest.raises(ConfigError) as excinfo:
            load_config(tmp_path / "absent.yaml")
        message = str(excinfo.value)
        assert "not found" in message
        assert "config.example.yaml" in message

    def test_malformed_yaml_gives_readable_error(self, tmp_path):
        path = write_config(tmp_path, "storage: [unclosed\n  bad: : yaml")
        with pytest.raises(ConfigError) as excinfo:
            load_config(path)
        assert "Malformed YAML" in str(excinfo.value)

    def test_non_mapping_top_level_rejected(self, tmp_path):
        path = write_config(tmp_path, "- just\n- a\n- list\n")
        with pytest.raises(ConfigError, match="mapping"):
            load_config(path)

    def test_empty_file_rejected(self, tmp_path):
        path = write_config(tmp_path, "")
        with pytest.raises(ConfigError, match="empty"):
            load_config(path)

    def test_wrong_type_reports_section_and_key(self, tmp_path):
        data = minimal_valid()
        data["storage"]["inbox"] = 123
        path = write_config(tmp_path, data)
        with pytest.raises(ConfigError) as excinfo:
            load_config(path)
        assert "[storage]" in str(excinfo.value)
        assert "'inbox'" in str(excinfo.value)

    def test_null_required_value_rejected(self, tmp_path):
        data = minimal_valid()
        data["llm"]["base_url"] = None
        path = write_config(tmp_path, data)
        with pytest.raises(ConfigError) as excinfo:
            load_config(path)
        assert "[llm]" in str(excinfo.value)
        assert "'base_url'" in str(excinfo.value)

    def test_bad_tag_entry_rejected(self, tmp_path):
        data = minimal_valid()
        data["initial_tags"] = [{"name": ""}]
        path = write_config(tmp_path, data)
        with pytest.raises(ConfigError, match="initial_tags"):
            load_config(path)


class TestDefaultsAndEnv:
    def test_partial_config_uses_code_defaults(self, tmp_path):
        data = {"storage": minimal_valid()["storage"]}
        config = load_config(write_config(tmp_path, data))
        assert config.llm.provider == "openai_compatible"
        assert config.llm.api_key_env == "BRAIN_LLM_API_KEY"
        assert config.retention.enabled is False
        assert config.initial_tags == []

    def test_brain_config_env_overrides_default_location(self, tmp_path, monkeypatch):
        path = write_config(tmp_path, minimal_valid())
        monkeypatch.setenv("BRAIN_CONFIG", str(path))
        assert config_file_path() == path
        config = load_config()
        assert config.storage.inbox == Path("/tmp/x/inbox")

    def test_relative_brain_config_resolves_against_cwd(self, tmp_path, monkeypatch):
        path = write_config(tmp_path, minimal_valid(), name="my.yaml")
        monkeypatch.setenv("BRAIN_CONFIG", "my.yaml")
        monkeypatch.chdir(tmp_path)
        assert config_file_path() == path

    def test_env_secret_handling(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("BRAIN_TEST_LLM_API_KEY=from-dotenv\n", encoding="utf-8")
        monkeypatch.delenv("BRAIN_TEST_LLM_API_KEY", raising=False)

        config = load_config(write_config(tmp_path, minimal_valid()), env_file=env_path)
        assert config.api_key_for("BRAIN_TEST_LLM_API_KEY") == "from-dotenv"

        # Real environment variables win over .env values.
        monkeypatch.setenv("BRAIN_TEST_LLM_API_KEY", "from-env")
        assert config.api_key_for("BRAIN_TEST_LLM_API_KEY") == "from-env"

    def test_api_key_never_appears_in_repr(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAIN_TEST_LLM_API_KEY", "super-secret-value")
        config = load_config(write_config(tmp_path, minimal_valid()))
        assert "super-secret-value" not in repr(config)
        assert "super-secret-value" not in str(config)


class TestSemanticValidation:
    def _invalid(self, tmp_path, mutate, match):
        data = minimal_valid()
        mutate(data)
        with pytest.raises(ConfigError, match=match):
            load_config(write_config(tmp_path, data))

    def test_audio_days_rejects_boolean(self, tmp_path):
        self._invalid(tmp_path, lambda d: d["retention"].update(audio_days=True), "got bool")

    def test_audio_days_rejects_non_positive(self, tmp_path):
        self._invalid(tmp_path, lambda d: d["retention"].update(audio_days=0), "positive integer")
        self._invalid(tmp_path, lambda d: d["retention"].update(audio_days=-3), "positive integer")

    def test_timeout_seconds_rejects_boolean(self, tmp_path):
        self._invalid(tmp_path, lambda d: d["llm"].update(timeout_seconds=True), "got bool")

    def test_timeout_seconds_rejects_non_positive(self, tmp_path):
        self._invalid(tmp_path, lambda d: d["llm"].update(timeout_seconds=0), "positive")

    def test_temperature_rejects_boolean(self, tmp_path):
        self._invalid(tmp_path, lambda d: d["llm"].update(temperature=True), "got bool")

    def test_blank_storage_path_rejected(self, tmp_path):
        self._invalid(tmp_path, lambda d: d["storage"].update(inbox="   "), "must not be blank")

    def test_blank_macwhisper_command_rejected(self, tmp_path):
        self._invalid(tmp_path, lambda d: d["macwhisper"].update(command=""), "must not be blank")

    def test_blank_llm_base_url_rejected(self, tmp_path):
        self._invalid(tmp_path, lambda d: d["llm"].update(base_url="  "), "must not be blank")

    def test_blank_llm_api_key_env_rejected(self, tmp_path):
        self._invalid(tmp_path, lambda d: d["llm"].update(api_key_env=""), "must not be blank")

    def test_blank_embedding_base_url_rejected(self, tmp_path):
        self._invalid(tmp_path, lambda d: d["embedding"].update(base_url=""), "must not be blank")

    def test_blank_embedding_api_key_env_rejected(self, tmp_path):
        self._invalid(tmp_path, lambda d: d["embedding"].update(api_key_env="   "), "must not be blank")

    def test_blank_llm_provider_rejected(self, tmp_path):
        self._invalid(tmp_path, lambda d: d["llm"].update(provider=""), "must not be blank")


class TestWebSection:
    def test_web_defaults_when_section_omitted(self, tmp_path):
        config = load_config(write_config(tmp_path, minimal_valid()))
        assert config.web.recordings_per_page == 25
        assert config.web.transcript_segments_per_page == 200

    def test_web_values_parsed(self, tmp_path):
        data = minimal_valid()
        data["web"] = {"recordings_per_page": 10, "transcript_segments_per_page": 50}
        config = load_config(write_config(tmp_path, data))
        assert config.web.recordings_per_page == 10
        assert config.web.transcript_segments_per_page == 50

    def test_web_rejects_boolean(self, tmp_path):
        data = minimal_valid()
        data["web"] = {"recordings_per_page": True}
        with pytest.raises(ConfigError, match="recordings_per_page"):
            load_config(write_config(tmp_path, data))

    def test_web_rejects_non_positive(self, tmp_path):
        data = minimal_valid()
        data["web"] = {"recordings_per_page": 0}
        with pytest.raises(ConfigError, match="positive"):
            load_config(write_config(tmp_path, data))

    def test_web_rejects_string(self, tmp_path):
        data = minimal_valid()
        data["web"] = {"transcript_segments_per_page": "many"}
        with pytest.raises(ConfigError, match="transcript_segments_per_page"):
            load_config(write_config(tmp_path, data))

    def test_web_rejects_non_mapping_section(self, tmp_path):
        data = minimal_valid()
        data["web"] = 25
        with pytest.raises(ConfigError, match="\\[web\\] must be a mapping"):
            load_config(write_config(tmp_path, data))

    def test_example_config_documents_web_section(self):
        config = load_config(EXAMPLE)
        assert config.web.recordings_per_page == 25
        assert config.web.transcript_segments_per_page == 200
