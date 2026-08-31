"""Tests for summarization/tags configuration and tag synchronization."""

from __future__ import annotations

import pytest
import yaml

from brainlib.config import ConfigError, tag_name_key
from workflow.models import Tag

pytestmark = pytest.mark.django_db


def write_config(tmp_path, data, monkeypatch=None, name="config.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    if monkeypatch is not None:
        monkeypatch.setenv("BRAIN_CONFIG", str(path))
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


def with_summarization(data, **overrides):
    base = {
        "enabled": True,
        "prompt_version": "1",
        "max_input_characters": 120000,
        "chunk_characters": 24000,
        "chunk_overlap_characters": 1000,
        "max_chunk_count": 8,
        "max_total_characters": 960000,
        "temperature": 0.2,
        "max_output_tokens": 3000,
    }
    base.update(overrides)
    data["summarization"] = base
    return data


class TestSummarizationConfig:
    def test_valid_section_loads(self, tmp_path):
        from brainlib.config import load_config

        config = load_config(write_config(tmp_path, with_summarization(minimal_valid())))
        assert config.summarization.enabled is True
        assert config.summarization.prompt_version == "1"
        assert config.summarization.chunk_characters == 24000

    def test_omitted_section_uses_defaults(self, tmp_path):
        from brainlib.config import load_config

        config = load_config(write_config(tmp_path, minimal_valid()))
        assert config.summarization.max_input_characters == 120000
        assert config.summarization.max_chunk_count == 8

    def _invalid(self, tmp_path, **overrides):
        from brainlib.config import load_config

        with pytest.raises(ConfigError, match="summarization"):
            load_config(
                write_config(tmp_path, with_summarization(minimal_valid(), **overrides))
            )

    def test_boolean_rejected(self, tmp_path):
        self._invalid(tmp_path, chunk_characters=True)

    def test_zero_chunk_rejected(self, tmp_path):
        self._invalid(tmp_path, chunk_characters=0)

    def test_overlap_must_be_smaller_than_chunk(self, tmp_path):
        self._invalid(tmp_path, chunk_overlap_characters=24000)
        self._invalid(tmp_path, chunk_overlap_characters=-1)

    def test_chunk_must_not_exceed_request_limit(self, tmp_path):
        self._invalid(tmp_path, chunk_characters=200000)

    def test_total_limit_must_cover_request_limit(self, tmp_path):
        self._invalid(tmp_path, max_total_characters=60000)

    def test_temperature_range(self, tmp_path):
        self._invalid(tmp_path, temperature=3.0)
        self._invalid(tmp_path, temperature=True)

    def test_zero_max_output_tokens_rejected(self, tmp_path):
        self._invalid(tmp_path, max_output_tokens=0)


class TestTagsConfig:
    def test_valid_tag_list_loads(self, tmp_path):
        from brainlib.config import load_config

        data = minimal_valid()
        data["tags"] = {
            "allowed": [
                {"name": "Family", "description": "Family matters"},
                {"name": "Read2Learn", "description": "Reading"},
            ]
        }
        config = load_config(write_config(tmp_path, data))
        assert [t.name for t in config.tags.allowed] == ["Family", "Read2Learn"]

    def test_case_insensitive_duplicate_rejected(self, tmp_path):
        from brainlib.config import load_config

        data = minimal_valid()
        data["tags"] = {
            "allowed": [
                {"name": "Family", "description": "a"},
                {"name": "FAMILY ", "description": "b"},
            ]
        }
        with pytest.raises(ConfigError, match="duplicate tag name"):
            load_config(write_config(tmp_path, data))

    def test_blank_name_rejected(self, tmp_path):
        from brainlib.config import load_config

        data = minimal_valid()
        data["tags"] = {"allowed": [{"name": "  ", "description": "a"}]}
        with pytest.raises(ConfigError, match="'name' must be a non-empty string"):
            load_config(write_config(tmp_path, data))

    def test_non_string_description_rejected(self, tmp_path):
        from brainlib.config import load_config

        data = minimal_valid()
        data["tags"] = {"allowed": [{"name": "A", "description": 3}]}
        with pytest.raises(ConfigError, match="'description' must be a string"):
            load_config(write_config(tmp_path, data))

    def test_empty_allowed_list_permitted(self, tmp_path):
        from brainlib.config import load_config

        data = minimal_valid()
        data["tags"] = {"allowed": []}
        config = load_config(write_config(tmp_path, data))
        assert config.tags.allowed == ()

    def test_legacy_initial_tags_seed_with_notice(self, tmp_path):
        from brainlib.config import load_config

        data = minimal_valid()
        data["initial_tags"] = [{"name": "Legacy", "description": "old key"}]
        config = load_config(write_config(tmp_path, data))
        assert [t.name for t in config.tags.allowed] == ["Legacy"]
        assert config.legacy_tags_notice and "migrate" in config.legacy_tags_notice

    def test_both_keys_present_legacy_ignored_with_notice(self, tmp_path):
        from brainlib.config import load_config

        data = minimal_valid()
        data["initial_tags"] = [{"name": "Legacy", "description": "old key"}]
        data["tags"] = {"allowed": [{"name": "Current", "description": "new key"}]}
        config = load_config(write_config(tmp_path, data))
        assert [t.name for t in config.tags.allowed] == ["Current"]
        assert config.legacy_tags_notice and "ignored" in config.legacy_tags_notice


class TestTagNameKey:
    def test_normalization(self):
        assert tag_name_key("  Family ") == "family"
        # NFC composes combining sequences: decomposed é == composed é.
        assert tag_name_key("cafe\u0301") == tag_name_key("caf\u00e9")
        assert tag_name_key("中文標籤") == "中文標籤"

    def test_case_insensitive_identity(self):
        assert tag_name_key("Read2Learn") == tag_name_key("read2learn")


class TestTagSync:
    def _config(self, tmp_path, names):
        from factories import make_config
        from brainlib.config import TagSpec, TagsConfig

        return make_config(
            tmp_path,
            tags=TagsConfig(allowed=tuple(TagSpec(name=n, description=f"desc-{n}") for n in names)),
        )

    def test_creates_updates_retires_reactivates(self, tmp_path):
        from workflow.services.tags import sync_tags
        from brainlib.config import TagSpec, TagsConfig
        from factories import make_config

        config = self._config(tmp_path, ["Family", "Academic"])
        counts = sync_tags(config)
        assert counts["created"] == 2
        family = Tag.objects.get(name_key="family")
        assert family.is_configured is True

        # Description change -> updated.
        config2 = make_config(
            tmp_path,
            tags=TagsConfig(allowed=(TagSpec(name="Family", description="changed"),)),
        )
        counts = sync_tags(config2)
        assert counts["updated"] == 1
        assert counts["retired"] == 1  # Academic removed
        family.refresh_from_db()
        assert family.description == "changed"
        academic = Tag.objects.get(name_key="academic")
        assert academic.is_configured is False  # retired, not deleted

        # Re-added -> same row reactivated, display name/history intact.
        counts = sync_tags(config)
        assert counts["reactivated"] == 1
        academic.refresh_from_db()
        assert academic.is_configured is True
        assert Tag.objects.filter(name_key="academic").count() == 1

    def test_display_name_preserved_on_config_respelling(self, tmp_path):
        from workflow.services.tags import sync_tags
        from brainlib.config import TagSpec, TagsConfig
        from factories import make_config

        sync_tags(self._config(tmp_path, ["Family"]))
        config = make_config(
            tmp_path, tags=TagsConfig(allowed=(TagSpec(name="FAMILY", description="d"),))
        )
        sync_tags(config)
        tag = Tag.objects.get(name_key="family")
        assert tag.name == "Family"  # first spelling preserved
        assert tag.is_configured is True

    def test_idempotent(self, tmp_path):
        from workflow.services.tags import sync_tags

        config = self._config(tmp_path, ["Family"])
        first = sync_tags(config)
        second = sync_tags(config)
        assert first["created"] == 1
        assert second == {"created": 0, "updated": 0, "retired": 0, "reactivated": 0}
