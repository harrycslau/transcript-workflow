"""Focused tests for the optional ``summarization.models`` allowlist.

Covers the static YAML allowlist that feeds the web Retry/Regenerate
model selectors: default (omitted => single configured model), exact
order/de-duplication with ``llm.model`` always first, exact-identity
storage (no stripping/canonicalization), and every malformed/bound
rejection. No network, no real config file mutation.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from brainlib.config import (
    ConfigError,
    available_summary_models,
    load_config,
)


def minimal_config() -> dict:
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
        "llm": {"base_url": "http://127.0.0.1:1/v1", "model": "base-model"},
        "embedding": {"base_url": "http://127.0.0.1:1/v1"},
        "retention": {
            "enabled": False,
            "audio_days": 3,
            "delete_mode": "permanent",
            "require_transcript": True,
            "require_summary": True,
        },
    }


def write_and_load(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(path)


def with_models(tmp_path, models):
    data = minimal_config()
    data["summarization"] = {"models": models}
    return write_and_load(tmp_path, data)


class TestDefaults:
    def test_omitted_models_defaults_to_empty_and_single_choice(self, tmp_path):
        config = write_and_load(tmp_path, minimal_config())
        assert config.summarization.models == ()
        assert available_summary_models(config) == ("base-model",)

    def test_empty_models_list_keeps_single_choice(self, tmp_path):
        config = with_models(tmp_path, [])
        assert config.summarization.models == ()
        assert available_summary_models(config) == ("base-model",)


class TestEffectiveChoices:
    def test_default_first_then_configured_order(self, tmp_path):
        config = with_models(tmp_path, ["alt-a", "alt-b"])
        assert available_summary_models(config) == ("base-model", "alt-a", "alt-b")

    def test_configured_list_need_not_repeat_llm_model(self, tmp_path):
        config = with_models(tmp_path, ["alt-a"])
        assert available_summary_models(config) == ("base-model", "alt-a")

    def test_repeating_llm_model_is_deduplicated(self, tmp_path):
        config = with_models(tmp_path, ["base-model", "alt-a"])
        assert available_summary_models(config) == ("base-model", "alt-a")

    def test_exact_identity_is_preserved_no_stripping(self, tmp_path):
        # A spaced-but-nonblank identity is stored and returned exactly.
        config = with_models(tmp_path, [" alt model "])
        assert config.summarization.models == (" alt model ",)
        assert available_summary_models(config) == ("base-model", " alt model ")

    def test_blank_default_is_excluded_but_alternatives_remain(self, tmp_path):
        data = minimal_config()
        data["llm"]["model"] = ""
        data["summarization"] = {"models": ["alt-a"]}
        config = write_and_load(tmp_path, data)
        assert available_summary_models(config) == ("alt-a",)


class TestMalformed:
    def test_non_list_rejected(self, tmp_path):
        with pytest.raises(ConfigError):
            with_models(tmp_path, "alt-a")

    def test_boolean_entry_rejected(self, tmp_path):
        with pytest.raises(ConfigError):
            with_models(tmp_path, [True])

    def test_non_string_entry_rejected(self, tmp_path):
        with pytest.raises(ConfigError):
            with_models(tmp_path, [1])

    def test_blank_entry_rejected(self, tmp_path):
        with pytest.raises(ConfigError):
            with_models(tmp_path, ["   "])

    def test_control_and_newline_entry_rejected(self, tmp_path):
        for bad in ("bad\nmodel", "bad\tmodel", "bad\x00model"):
            with pytest.raises(ConfigError):
                with_models(tmp_path, [bad])

    def test_duplicate_entry_rejected(self, tmp_path):
        with pytest.raises(ConfigError):
            with_models(tmp_path, ["same", "same"])


class TestBounds:
    def test_too_many_models_rejected(self, tmp_path):
        with pytest.raises(ConfigError):
            with_models(tmp_path, [f"m{i}" for i in range(33)])

    def test_max_models_accepted(self, tmp_path):
        config = with_models(tmp_path, [f"m{i}" for i in range(32)])
        assert len(config.summarization.models) == 32

    def test_over_length_entry_rejected(self, tmp_path):
        with pytest.raises(ConfigError):
            with_models(tmp_path, ["x" * 256])

    def test_max_length_entry_accepted(self, tmp_path):
        model = "x" * 255
        config = with_models(tmp_path, [model])
        assert config.summarization.models == (model,)


class TestExampleConfig:
    def test_example_documents_models_key(self):
        from pathlib import Path

        text = (
            Path(__file__).resolve().parent.parent / "config" / "config.example.yaml"
        ).read_text(encoding="utf-8")
        # The key is documented in the example (value may remain empty).
        assert "models:" in text
