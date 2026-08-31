"""Tests for Django views: home page, health endpoint, error pages.

Home-page and health tests prove that ordinary requests never launch
MacWhisper (no subprocess calls) and never make HTTP calls (no httpx
requests) - both are patched to raise if invoked.
"""

from __future__ import annotations

import pytest
from django.test import RequestFactory, override_settings

from workflow import views


@pytest.fixture(autouse=True)
def forbid_external_effects(monkeypatch):
    """Any subprocess or HTTP call from a view test fails the test."""

    def _no_subprocess(*args, **kwargs):
        raise AssertionError("subprocess must not be called during page requests")

    def _no_http(*args, **kwargs):
        raise AssertionError("HTTP requests must not be made during page requests")

    monkeypatch.setattr("subprocess.run", _no_subprocess)
    monkeypatch.setattr("subprocess.Popen", _no_subprocess)
    monkeypatch.setattr("httpx.get", _no_http)
    monkeypatch.setattr("httpx.post", _no_http)
    monkeypatch.setattr("httpx.Client", _no_http)


class TestHomePage:
    @pytest.mark.django_db
    def test_home_renders_status(self, client):
        response = client.get("/")
        assert response.status_code == 200
        content = response.content.decode()
        assert "Brain" in content
        from brainlib import __version__

        assert __version__ in content
        assert "data/inbox" in content or "inbox" in content
        assert "(not configured)" in content  # blank models warn visually

    @pytest.mark.django_db
    def test_home_does_not_expose_secrets(self, client, monkeypatch):
        monkeypatch.setenv("BRAIN_TEST_LLM_API_KEY", "super-secret-value")
        content = client.get("/").content.decode()
        assert "super-secret-value" not in content
        assert "BRAIN_TEST_LLM_API_KEY" not in content

    @pytest.mark.django_db
    def test_home_reports_macwhisper_presence_without_spawning(self, client):
        content = client.get("/").content.decode()
        # Session test config points at a nonexistent mw binary.
        assert "not found on PATH" in content


class TestHealthEndpoint:
    @pytest.mark.django_db
    def test_healthy_returns_200_with_structure(self, client):
        response = client.get("/health/")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] in {"ok", "degraded"}
        assert payload["application"]["config"] == "ok"
        assert payload["application"]["database"] == "ok"
        assert "macwhisper" in payload["dependencies"]
        assert "omlx" in payload["dependencies"]

    @pytest.mark.django_db
    def test_missing_optional_dependency_is_degraded_not_500(self, client, monkeypatch):
        # Session config's mw path does not exist -> degraded, still 200.
        monkeypatch.setattr(views.shutil, "which", lambda command: None)
        response = client.get("/health/")
        assert response.status_code == 200
        assert response.json()["status"] == "degraded"

    @pytest.mark.django_db
    def test_database_failure_returns_503(self, client, monkeypatch):
        class BrokenCursor:
            def execute(self, *args, **kwargs):
                raise RuntimeError("db gone: /Users/harry/secret/brain.sqlite3 boom")

        class BrokenConnection:
            def cursor(self):
                return BrokenCursor()

        monkeypatch.setattr(views, "connection", BrokenConnection())
        response = client.get("/health/")
        assert response.status_code == 503
        payload = response.json()
        assert payload["status"] == "unhealthy"
        assert payload["application"]["database"] == "error"
        # Raw exception text must not leak into the response.
        content = response.content.decode()
        assert "db gone" not in content
        assert "/Users/harry/secret" not in content

    @pytest.mark.django_db
    def test_missing_runtime_directory_returns_503(self, client, monkeypatch):
        monkeypatch.setattr(
            views, "runtime_directories",
            lambda config: [config.storage.inbox, config.storage.inbox.parent / "does-not-exist"],
        )
        response = client.get("/health/")
        assert response.status_code == 503
        payload = response.json()
        assert payload["status"] == "unhealthy"
        assert payload["application"]["runtime_directories"] == "error"

    def test_invalid_config_returns_503(self, client, monkeypatch):
        from brainlib.config import ConfigError

        def broken_load():
            raise ConfigError("Configuration file not found: /tmp/xyz/private/config.yaml")

        monkeypatch.setattr(views, "load_config", broken_load)
        response = client.get("/health/")
        assert response.status_code == 503
        payload = response.json()
        assert payload["status"] == "unhealthy"
        assert payload["application"]["config"] == "error"
        assert payload["application"]["runtime_directories"] == "unknown"
        assert payload["application"]["database"] == "unknown"
        # Exception text (including the path) must not leak into the response.
        content = response.content.decode()
        assert "not found" not in content
        assert "/tmp/xyz/private" not in content

    @pytest.mark.django_db
    def test_health_never_exposes_secrets(self, client, monkeypatch):
        monkeypatch.setenv("BRAIN_TEST_LLM_API_KEY", "super-secret-value")
        payload = client.get("/health/").json()

        def walk(node):
            if isinstance(node, dict):
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
            elif isinstance(node, str):
                assert "super-secret-value" not in node

        walk(payload)

    @pytest.mark.django_db
    def test_health_contains_no_absolute_database_or_config_paths(self, client, monkeypatch):
        monkeypatch.setenv("BRAIN_TEST_LLM_API_KEY", "planted-secret-value")
        content = client.get("/health/").content.decode()
        assert "planted-secret-value" not in content
        assert ".sqlite3" not in content
        assert "/Users/" not in content
        assert "/private/var" not in content


class TestErrorPages:
    @override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
    def test_404_uses_custom_template(self, client):
        response = client.get("/this-page-does-not-exist/")
        assert response.status_code == 404
        assert "404.html" in [t.name for t in response.templates]

    def test_500_handler_renders(self, rf):
        request = rf.get("/")
        response = views.error_500(request)
        assert response.status_code == 500
        assert b"500" in response.content
