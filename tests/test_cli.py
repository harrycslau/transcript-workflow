"""Tests for the ``brain`` CLI: doctor output/exit codes and serve startup."""

from __future__ import annotations

import httpx
import pytest

from brainlib import cli, diagnostics
from brainlib.config import ConfigError


@pytest.fixture
def reachable_omlx(monkeypatch):
    monkeypatch.setattr(
        diagnostics, "fetch_omlx_models",
        lambda base_url, api_key_env, **k: ["model-a"],
    )


@pytest.fixture
def unreachable_omlx(monkeypatch):
    def _raise(base_url, api_key_env, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(diagnostics, "fetch_omlx_models", _raise)


class TestDoctor:
    def test_all_warnings_exit_zero(self, monkeypatch, unreachable_omlx, capsys):
        # Session test config: mw missing, oMLX unreachable, blank models.
        assert cli.main(["doctor"]) == 0
        output = capsys.readouterr().out
        assert "FAIL" not in output
        assert "WARN" in output
        assert "PASS" in output
        assert "not found" in output          # MacWhisper missing
        assert "unreachable" in output        # oMLX unavailable
        assert "configured (blank)" in output

    def test_missing_config_exits_one(self, monkeypatch, capsys):
        monkeypatch.setenv("BRAIN_CONFIG", "/nonexistent/brain.yaml")
        assert cli.main(["doctor"]) == 1
        output = capsys.readouterr().out
        assert "Configuration file not found" in output

    def test_malformed_config_exits_one(self, tmp_path, monkeypatch, capsys):
        bad = tmp_path / "bad.yaml"
        bad.write_text("storage: [unclosed\n  bad: : yaml", encoding="utf-8")
        monkeypatch.setenv("BRAIN_CONFIG", str(bad))
        assert cli.main(["doctor"]) == 1
        assert "Malformed YAML" in capsys.readouterr().out

    def test_no_secrets_in_doctor_output(self, monkeypatch, unreachable_omlx, capsys):
        monkeypatch.setenv("BRAIN_TEST_LLM_API_KEY", "super-secret-value")
        cli.main(["doctor"])
        output = capsys.readouterr().out
        assert "super-secret-value" not in output


class TestServe:
    def test_invalid_config_reports_concisely_and_exits_one(self, monkeypatch, capsys):
        def broken_load():
            raise ConfigError("Configuration file not found: config/config.yaml")

        monkeypatch.setattr("brainlib.config.load_config", broken_load)

        def fail_call_command(*args, **kwargs):
            raise AssertionError("runserver must not start with an invalid config")

        monkeypatch.setattr("django.core.management.call_command", fail_call_command)
        assert cli.main(["serve"]) == 1
        error = capsys.readouterr().err
        assert "error:" in error
        assert "Configuration file not found" in error

    def test_valid_config_starts_runserver_on_localhost(self, monkeypatch):
        recorded = {}

        def fake_call_command(name, address, **kwargs):
            recorded["name"] = name
            recorded["address"] = address
            recorded["kwargs"] = kwargs

        monkeypatch.setattr("brainlib.paths.ensure_runtime_dirs", lambda config: [])
        monkeypatch.setattr("django.core.management.call_command", fake_call_command)
        assert cli.main(["serve", "--host", "127.0.0.1", "--port", "8787"]) == 0
        assert recorded["name"] == "runserver"
        assert recorded["address"] == "127.0.0.1:8787"
        assert recorded["kwargs"].get("use_reloader") is True

    def test_runtime_dir_setup_failure_reports_and_exits_one(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "brainlib.paths.ensure_runtime_dirs",
            lambda config: (_ for _ in ()).throw(OSError("read-only filesystem")),
        )

        def fail_django_setup(*args, **kwargs):
            raise AssertionError("django.setup() must not run after setup failure")

        def fail_call_command(*args, **kwargs):
            raise AssertionError("runserver must not start after setup failure")

        monkeypatch.setattr("django.setup", fail_django_setup)
        monkeypatch.setattr("django.core.management.call_command", fail_call_command)
        assert cli.main(["serve"]) == 1
        error = capsys.readouterr().err
        assert "error:" in error
        assert "runtime directories" in error
        assert "read-only filesystem" in error

    def test_serve_defaults(self, monkeypatch):
        recorded = {}

        def fake_call_command(name, address, **kwargs):
            recorded["address"] = address

        monkeypatch.setattr("brainlib.paths.ensure_runtime_dirs", lambda config: [])
        monkeypatch.setattr("django.core.management.call_command", fake_call_command)
        assert cli.main(["serve"]) == 0
        assert recorded["address"] == "127.0.0.1:8787"
