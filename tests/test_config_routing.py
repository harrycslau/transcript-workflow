"""Tests for Step 2 configuration: routing profiles, legacy handling."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from brainlib.config import ConfigError, load_config

EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "config.example.yaml"


def write_config(tmp_path: Path, data, name: str = "config.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def base_config() -> dict:
    return {
        "storage": {
            "inbox": "/tmp/x/inbox",
            "database": "/tmp/x/database/brain.sqlite3",
            "transcripts": "/tmp/x/transcripts",
            "exports": "/tmp/x/exports",
            "logs": "/tmp/x/logs",
            "temp": "/tmp/x/temp",
        }
    }


class TestRoutingProfiles:
    def test_defaults_when_routing_absent(self, tmp_path):
        config = load_config(write_config(tmp_path, base_config()))
        routing = config.macwhisper.routing
        assert routing.enabled is True
        assert routing.auto_transcribe is True
        assert routing.confidence_threshold == 0.80
        assert routing.default_profile == "european"
        assert set(routing.profiles) == {"cantonese", "mandarin", "european", "european_small"}
        assert routing.profiles["cantonese"].model == "apple:zh-HK"
        assert routing.profiles["cantonese"].language is None
        assert routing.profiles["mandarin"].model == "apple:zh-CN"
        # Validated against MacWhisper 14.7.1: parakeet rejects "multilingual",
        # so the default european profile does not pass --language.
        assert routing.profiles["european"].model == "parakeet-pro:nvidia_parakeet-v3"
        assert routing.profiles["european"].language is None
        assert routing.profiles["european_small"].manual_only is True

    def test_example_config_has_routing(self):
        config = load_config(EXAMPLE)
        assert "routing" in yaml.safe_load(EXAMPLE.read_text())["macwhisper"]
        assert config.macwhisper.routing.default_profile == "european"

    def test_custom_profiles(self, tmp_path):
        data = base_config()
        data["macwhisper"] = {
            "command": "/usr/local/bin/mw",
            "routing": {
                "enabled": True,
                "auto_transcribe": False,
                "confidence_threshold": 0.9,
                "default_profile": "mine",
                "profiles": {
                    "mine": {"model": "apple:zh-HK", "language": "auto"},
                    "cantonese": {"model": "apple:zh-HK", "language": None},
                    "mandarin": {"model": "apple:zh-CN", "language": None},
                    "european": {"model": "parakeet-pro:nvidia_parakeet-v3", "language": None},
                },
            },
        }
        config = load_config(write_config(tmp_path, data))
        assert config.macwhisper.routing.auto_transcribe is False
        assert config.macwhisper.routing.profiles["mine"].language == "auto"

    def test_enabled_routing_requires_semantic_profiles(self, tmp_path):
        data = base_config()
        data["macwhisper"] = {
            "routing": {
                "default_profile": "mine",
                "profiles": {"mine": {"model": "apple:zh-HK", "language": None}},
            }
        }
        with pytest.raises(ConfigError, match="required profile 'cantonese' is missing"):
            load_config(write_config(tmp_path, data))

    def test_enabled_routing_rejects_manual_only_required_profile(self, tmp_path):
        data = base_config()
        data["macwhisper"] = {
            "routing": {
                "profiles": {
                    "cantonese": {"model": "apple:zh-HK", "language": None},
                    "mandarin": {"model": "apple:zh-CN", "language": None},
                    "european": {"model": "parakeet-pro:nvidia_parakeet-v3", "language": None, "manual_only": True},
                }
            }
        }
        with pytest.raises(ConfigError, match="must not be manual_only"):
            load_config(write_config(tmp_path, data))

    def test_disabled_routing_allows_arbitrary_profiles(self, tmp_path):
        data = base_config()
        data["macwhisper"] = {
            "routing": {
                "enabled": False,
                "default_profile": "mine",
                "profiles": {"mine": {"model": "apple:zh-HK", "language": None}},
            }
        }
        config = load_config(write_config(tmp_path, data))
        assert config.macwhisper.routing.enabled is False

    def test_default_profile_must_exist(self, tmp_path):
        data = base_config()
        data["macwhisper"] = {
            "routing": {
                "default_profile": "nope",
                "profiles": {"mine": {"model": "apple:zh-HK", "language": None}},
            }
        }
        with pytest.raises(ConfigError, match="default_profile"):
            load_config(write_config(tmp_path, data))

    def test_profile_model_must_be_nonblank(self, tmp_path):
        data = base_config()
        data["macwhisper"] = {
            "routing": {"profiles": {"x": {"model": "  ", "language": None}}}
        }
        with pytest.raises(ConfigError, match="model"):
            load_config(write_config(tmp_path, data))

    def test_confidence_threshold_bounds(self, tmp_path):
        data = base_config()
        data["macwhisper"] = {
            "routing": {"confidence_threshold": 1.5, "profiles": {"x": {"model": "m"}}}
        }
        with pytest.raises(ConfigError, match="confidence_threshold"):
            load_config(write_config(tmp_path, data))

    def test_confidence_threshold_rejects_bool(self, tmp_path):
        data = base_config()
        data["macwhisper"] = {
            "routing": {"confidence_threshold": True, "profiles": {"x": {"model": "m"}}}
        }
        with pytest.raises(ConfigError, match="got bool"):
            load_config(write_config(tmp_path, data))

    def test_file_stable_seconds_positive(self, tmp_path):
        data = base_config()
        data["macwhisper"] = {"file_stable_seconds": 0}
        with pytest.raises(ConfigError, match="file_stable_seconds"):
            load_config(write_config(tmp_path, data))

    def test_file_stable_seconds_rejects_bool(self, tmp_path):
        data = base_config()
        data["macwhisper"] = {"file_stable_seconds": True}
        with pytest.raises(ConfigError, match="got bool"):
            load_config(write_config(tmp_path, data))


class TestLegacyModelKey:
    def test_null_legacy_model_loads_default_profiles(self, tmp_path):
        """Regression: Step 1 configs with `model: null` must keep working."""
        data = base_config()
        data["macwhisper"] = {"command": "/usr/local/bin/mw", "model": None, "language": "auto"}
        config = load_config(write_config(tmp_path, data))
        routing = config.macwhisper.routing
        assert set(routing.profiles) == {"cantonese", "mandarin", "european", "european_small"}
        assert routing.default_profile == "european"
        assert config.macwhisper.legacy_model_notice is not None
        assert "no longer used" in config.macwhisper.legacy_model_notice

    def test_blank_legacy_model_same_as_null(self, tmp_path):
        data = base_config()
        data["macwhisper"] = {"command": "/usr/local/bin/mw", "model": "  "}
        config = load_config(write_config(tmp_path, data))
        assert "legacy" not in config.macwhisper.routing.profiles
        assert config.macwhisper.legacy_model_notice is not None

    def test_nonblank_legacy_model_becomes_manual_only_profile(self, tmp_path):
        data = base_config()
        data["macwhisper"] = {"command": "/usr/local/bin/mw", "model": "apple:zh-CN"}
        config = load_config(write_config(tmp_path, data))
        legacy = config.macwhisper.routing.profiles.get("legacy")
        assert legacy is not None
        assert legacy.model == "apple:zh-CN"
        assert legacy.manual_only is True
        assert config.macwhisper.routing.default_profile == "legacy"
        assert "manual-only" in config.macwhisper.legacy_model_notice

    def test_legacy_key_ignored_when_routing_configured(self, tmp_path):
        data = base_config()
        data["macwhisper"] = {
            "model": "apple:zh-CN",
            "routing": {
                "default_profile": "mine",
                "profiles": {
                    "mine": {"model": "parakeet-pro:nvidia_parakeet-v3", "language": None},
                    "cantonese": {"model": "apple:zh-HK", "language": None},
                    "mandarin": {"model": "apple:zh-CN", "language": None},
                    "european": {"model": "parakeet-pro:nvidia_parakeet-v3", "language": None},
                },
            },
        }
        config = load_config(write_config(tmp_path, data))
        assert "legacy" not in config.macwhisper.routing.profiles
        assert config.macwhisper.routing.default_profile == "mine"
        assert "ignored" in config.macwhisper.legacy_model_notice
