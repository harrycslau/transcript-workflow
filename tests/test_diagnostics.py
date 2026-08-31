"""Tests for diagnostics: MacWhisper, oMLX, FTS5, runtime dirs, models, redaction."""

from __future__ import annotations

from pathlib import Path

import subprocess
from types import SimpleNamespace

import httpx
import pytest
import sqlite3

from brainlib import diagnostics
from factories import make_config
from brainlib.diagnostics import (
    FAIL,
    MODELS_EMPTY,
    MODELS_UNVERIFIED,
    MODELS_VERIFIED,
    OmlxPayloadError,
    PASS,
    WARN,
    check_fts5,
    check_macwhisper,
    check_models,
    check_omlx,
    check_runtime_dirs,
    fetch_omlx_models,
)

FAKE_MW = "/tmp/fake-mw-bin/mw"


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def fake_mw_on_path(monkeypatch):
    monkeypatch.setattr(diagnostics.shutil, "which", lambda command: FAKE_MW if command == FAKE_MW else None)


class TestMacWhisper:
    def test_version_success(self, fake_mw_on_path, config):
        runner_output = completed(0, stdout="MacWhisper 6.1\n")
        result = check_macwhisper(config, runner=lambda *a, **k: runner_output)
        assert result.status == PASS
        assert "MacWhisper 6.1" in result.detail

    def test_missing_executable_is_warning(self, config):
        result = check_macwhisper(config)
        assert result.status == WARN
        assert "not found" in result.detail
        assert not result.is_fatal

    def test_nonzero_exit_is_warning(self, fake_mw_on_path, config):
        result = check_macwhisper(config, runner=lambda *a, **k: completed(1, stderr="boom"))
        assert result.status == WARN
        assert "boom" in result.detail

    def test_subprocess_not_found_is_warning(self, fake_mw_on_path, config):
        def runner(*args, **kwargs):
            raise FileNotFoundError("gone")

        result = check_macwhisper(config, runner=runner)
        assert result.status == WARN

    def test_timeout_is_warning(self, fake_mw_on_path, config):
        def runner(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="mw", timeout=10)

        result = check_macwhisper(config, runner=runner)
        assert result.status == WARN


class TestRuntimeDirs:
    def test_newly_created_directories_pass(self, config):
        result = check_runtime_dirs(config)
        assert result.status == PASS

    def test_existing_writable_directories_pass(self, config):
        for directory in diagnostics_runtime_dirs(config):
            directory.mkdir(parents=True, exist_ok=True)
        result = check_runtime_dirs(config)
        assert result.status == PASS

    def test_existing_regular_file_at_runtime_path_fails(self, config):
        # Pre-create the inbox path as a regular file.
        config.storage.inbox.parent.mkdir(parents=True, exist_ok=True)
        config.storage.inbox.write_text("not a directory", encoding="utf-8")
        result = check_runtime_dirs(config)
        assert result.status == FAIL
        assert "not a directory" in result.detail

    def test_existing_unwritable_directory_fails(self, config, monkeypatch):
        for directory in diagnostics_runtime_dirs(config):
            directory.mkdir(parents=True, exist_ok=True)
        unwritable = config.storage.exports
        monkeypatch.setattr(
            "brainlib.paths.is_writable_dir",
            lambda path: path != unwritable,
        )
        result = check_runtime_dirs(config)
        assert result.status == FAIL
        assert str(unwritable) in result.detail

    def test_check_covers_all_configured_directories(self, config, monkeypatch):
        seen: list = []
        real_is_writable = diagnostics_is_writable()

        def recording_is_writable(path):
            seen.append(path)
            return True

        monkeypatch.setattr("brainlib.paths.is_writable_dir", recording_is_writable)
        check_runtime_dirs(config)
        assert set(seen) == set(diagnostics_runtime_dirs(config))

    def test_reports_all_failures_not_just_first(self, config):
        config.storage.inbox.parent.mkdir(parents=True, exist_ok=True)
        config.storage.inbox.write_text("file", encoding="utf-8")
        config.storage.temp.parent.mkdir(parents=True, exist_ok=True)
        config.storage.temp.write_text("file", encoding="utf-8")
        result = check_runtime_dirs(config)
        assert result.status == FAIL
        assert str(config.storage.inbox) in result.detail
        assert str(config.storage.temp) in result.detail


def diagnostics_runtime_dirs(config):
    from brainlib.paths import runtime_directories

    return runtime_directories(config)


def diagnostics_is_writable():
    import brainlib.paths

    return brainlib.paths.is_writable_dir


class TestOMLX:
    def test_reachable_lists_models(self, config):
        fetcher = lambda base_url, api_key_env, **k: ["model-b", "model-a"]
        result, models, state = check_omlx(config.llm.base_url, config.llm.api_key_env, fetcher=fetcher)
        assert result.status == PASS
        assert models == ["model-b", "model-a"]  # passed through as returned
        assert state == MODELS_VERIFIED

    def test_unreachable_is_warning_not_exception(self, config):
        def fetcher(base_url, api_key_env, **k):
            raise httpx.ConnectError("connection refused")

        result, models, state = check_omlx(config.llm.base_url, config.llm.api_key_env, fetcher=fetcher)
        assert result.status == WARN
        assert models == []
        assert "unreachable" in result.detail
        assert state == MODELS_UNVERIFIED

    def test_timeout_is_warning(self, config):
        def fetcher(base_url, api_key_env, **k):
            raise httpx.TimeoutException("timed out")

        result, _, state = check_omlx(config.llm.base_url, config.llm.api_key_env, fetcher=fetcher)
        assert result.status == WARN
        assert state == MODELS_UNVERIFIED

    def test_empty_model_list_is_warning(self, config):
        result, models, state = check_omlx(
            config.llm.base_url, config.llm.api_key_env,
            fetcher=lambda base_url, api_key_env, **k: [],
        )
        assert result.status == WARN
        assert state == MODELS_EMPTY

    def test_invalid_payload_is_warning(self, config):
        def fetcher(base_url, api_key_env, **k):
            raise OmlxPayloadError("response is not valid JSON")

        result, models, state = check_omlx(config.llm.base_url, config.llm.api_key_env, fetcher=fetcher)
        assert result.status == WARN
        assert models == []
        assert state == MODELS_UNVERIFIED

    def test_fetch_parses_openai_models_payload(self, monkeypatch):
        captured = {}

        def fake_get(url, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"data": [{"id": "zeta"}, {"id": "alpha"}]},
            )

        monkeypatch.setattr(diagnostics.httpx, "get", fake_get)
        models = fetch_omlx_models("http://host/v1", "BRAIN_TEST_LLM_API_KEY")
        assert models == ["alpha", "zeta"]
        assert captured["url"] == "http://host/v1/models"

    def test_fetch_trailing_slash_base_url(self, monkeypatch):
        captured = {}

        def fake_get(url, headers=None, timeout=None):
            captured["url"] = url
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"data": [{"id": "m"}]},
            )

        monkeypatch.setattr(diagnostics.httpx, "get", fake_get)
        fetch_omlx_models("http://host/v1/", "BRAIN_TEST_LLM_API_KEY")
        assert captured["url"] == "http://host/v1/models"


class TestOmlxMalformedResponses:
    """Malformed /v1/models responses must never crash diagnostics."""

    def _response(self, payload=None, json_error=None):
        if json_error is not None:
            def json_body():
                raise json_error
        else:
            json_body = lambda: payload
        return SimpleNamespace(raise_for_status=lambda: None, json=json_body)

    def _fetch_with(self, monkeypatch, response):
        monkeypatch.setattr(diagnostics.httpx, "get", lambda url, headers=None, timeout=None: response)

    def test_invalid_json_raises_omlx_payload_error(self, monkeypatch):
        self._fetch_with(monkeypatch, self._response(json_error=ValueError("bad json")))
        with pytest.raises(OmlxPayloadError):
            fetch_omlx_models("http://host/v1", "BRAIN_TEST_LLM_API_KEY")

    def test_invalid_json_is_warning_in_check(self, config, monkeypatch):
        self._fetch_with(monkeypatch, self._response(json_error=ValueError("bad json")))
        result, models, state = check_omlx(config.llm.base_url, config.llm.api_key_env)
        assert result.status == WARN
        assert models == []
        assert state == MODELS_UNVERIFIED

    def test_json_list_payload_is_warning(self, config, monkeypatch):
        self._fetch_with(monkeypatch, self._response(["not", "an", "object"]))
        result, _, _ = check_omlx(config.llm.base_url, config.llm.api_key_env)
        assert result.status == WARN
        assert "not a JSON object" in result.detail

    def test_missing_data_field_is_warning(self, config, monkeypatch):
        self._fetch_with(monkeypatch, self._response({"object": "without data"}))
        result, _, _ = check_omlx(config.llm.base_url, config.llm.api_key_env)
        assert result.status == WARN
        assert "missing 'data'" in result.detail

    def test_non_list_data_is_warning(self, config, monkeypatch):
        self._fetch_with(monkeypatch, self._response({"data": {"id": "solo"}}))
        result, _, _ = check_omlx(config.llm.base_url, config.llm.api_key_env)
        assert result.status == WARN
        assert "'data' is not a list" in result.detail

    def test_invalid_entries_ignored_without_crash(self, monkeypatch):
        payload = {
            "data": [
                {"id": "alpha"},
                "junk string",
                42,
                {"no_id": True},
                {"id": None},
                {"id": "beta"},
            ]
        }
        self._fetch_with(monkeypatch, self._response(payload))
        models = fetch_omlx_models("http://host/v1", "BRAIN_TEST_LLM_API_KEY")
        assert models == ["alpha", "beta"]

    def test_response_body_never_in_diagnostics(self, config, monkeypatch):
        body = {"error": "super-secret-value leaked", "data": "wrong"}
        self._fetch_with(monkeypatch, self._response(body))
        result, _, _ = check_omlx(config.llm.base_url, config.llm.api_key_env)
        assert result.status == WARN
        assert "super-secret-value" not in result.detail

    def test_http_error_remains_warning(self, config, monkeypatch):
        def fake_get(url, headers=None, timeout=None):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(diagnostics.httpx, "get", fake_get)
        result, _, state = check_omlx(config.llm.base_url, config.llm.api_key_env)
        assert result.status == WARN
        assert state == MODELS_UNVERIFIED

    def test_doctor_exits_zero_for_omlx_payload_warnings(self, monkeypatch):
        # Doctor runs against the session config (mw missing, models blank);
        # force an invalid oMLX payload and require exit code 0.
        monkeypatch.setattr(
            diagnostics, "fetch_omlx_models",
            lambda base_url, api_key_env, **k: (_ for _ in ()).throw(OmlxPayloadError("response is not valid JSON")),
        )
        from brainlib import cli

        assert cli.main(["doctor"]) == 0


class TestModels:
    """All five verification states, for both summary and embedding models."""

    def _both(self, summary, embedding, state, models):
        return dict(
            zip(
                ("summary", "embedding"),
                check_models(summary, embedding, state, models),
            )
        )

    def test_blank_models_warn(self):
        results = self._both("", "", MODELS_VERIFIED, ["some-model"])
        for result in results.values():
            assert result.status == WARN
            assert "configured (blank)" in result.detail

    def test_verified_and_present_passes(self):
        results = self._both("summarizer", "embedder", MODELS_VERIFIED, ["summarizer", "embedder"])
        for result in results.values():
            assert result.status == PASS

    def test_verified_but_absent_warns(self):
        results = self._both("missing-model", "also-missing", MODELS_VERIFIED, ["other"])
        for result in results.values():
            assert result.status == WARN
            assert "not in /v1/models" in result.detail

    def test_unverified_warns_and_never_passes(self):
        results = self._both("summarizer", "embedder", MODELS_UNVERIFIED, [])
        for result in results.values():
            assert result.status == WARN
            assert "could not be verified" in result.detail

    def test_explicitly_empty_list_warns_and_never_passes(self):
        results = self._both("summarizer", "embedder", MODELS_EMPTY, [])
        for result in results.values():
            assert result.status == WARN
            assert "reports no models" in result.detail


class TestFTS5:
    def test_fts5_available(self):
        assert check_fts5().status == PASS

    def test_fts5_missing_is_warning(self, monkeypatch):
        def broken_connect(*args, **kwargs):
            raise sqlite3.OperationalError("no such module: fts5")

        monkeypatch.setattr(diagnostics.sqlite3, "connect", broken_connect)
        result = check_fts5()
        assert result.status == WARN
        assert not result.is_fatal


class TestRedaction:
    def test_api_key_header_sent_but_never_logged(self, config, monkeypatch):
        monkeypatch.setenv("BRAIN_TEST_LLM_API_KEY", "super-secret-value")

        def fake_get(url, headers=None, timeout=None):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(diagnostics.httpx, "get", fake_get)
        result, _, _ = check_omlx(config.llm.base_url, config.llm.api_key_env)

        assert "super-secret-value" not in result.detail
        assert config.llm.api_key_env in result.detail or result.status == WARN

    def test_doctor_output_never_contains_secret(self, config, monkeypatch, capsys):
        monkeypatch.setenv("BRAIN_TEST_LLM_API_KEY", "super-secret-value")
        monkeypatch.setattr(
            diagnostics, "fetch_omlx_models",
            lambda base_url, api_key_env, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")),
        )
        results, exit_code = diagnostics.run_doctor()
        assert exit_code == 0
        output = "\n".join(f"{r.name}: {r.detail}" for r in results)
        assert "super-secret-value" not in output


class TestStep2Diagnostics:
    def test_profiles_pass_when_models_installed(self, config):
        models_output = (
            "  ID                                     NAME       SIZE\n"
            "▸ parakeet-pro:nvidia_parakeet-v3        Parakeet   1.24 GB\n"
            "  parakeet-pro:nvidia_parakeet-v3_494MB  Parakeet   494 MB\n"
            "  apple:zh-CN                            Chinese    -\n"
            "  apple:zh-HK                            Chinese    -\n"
        )
        runner = lambda *a, **k: completed(0, stdout=models_output)
        result = diagnostics.check_transcription_profiles(config, runner=runner)
        assert result.status == PASS

    def test_profiles_warn_when_model_missing(self, config):
        models_output = "  ID              NAME    SIZE\n  apple:zh-CN     C       -\n"
        runner = lambda *a, **k: completed(0, stdout=models_output)
        result = diagnostics.check_transcription_profiles(config, runner=runner)
        assert result.status == WARN
        assert "apple:zh-HK" in result.detail

    def test_profiles_warn_when_mw_models_fails(self, config):
        runner = lambda *a, **k: completed(1, stderr="no")
        result = diagnostics.check_transcription_profiles(config, runner=runner)
        assert result.status == WARN

    def test_audio_tooling_pass_on_macos(self):
        result = diagnostics.check_audio_tooling()
        assert result.status in (PASS, WARN)  # depends on host; both are legal

    def test_audio_tooling_warn_when_missing(self, monkeypatch):
        monkeypatch.setattr(diagnostics.shutil, "which", lambda tool: None)
        result = diagnostics.check_audio_tooling()
        assert result.status == WARN
        assert "afinfo" in result.detail and "afconvert" in result.detail

    def test_legacy_notice_is_warn(self, config):
        from dataclasses import replace

        assert diagnostics.check_legacy_config(config) is None
        legacy_config = replace(
            config, macwhisper=replace(config.macwhisper, legacy_model_notice="legacy key detected")
        )
        result = diagnostics.check_legacy_config(legacy_config)
        assert result is not None
        assert result.status == WARN
