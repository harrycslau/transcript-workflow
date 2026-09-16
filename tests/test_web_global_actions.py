"""Global Library actions: Run now and Open inbox.

Covers the two POST-only global controls on the normal Library front
page: rendering/CSRF/pending hooks, GET 405 purity, the exact
``run_pipeline(config, respect_stability_window=False)`` call under the
lock with no duplicated recovery, the migration preflight, sanitized
failures, and the fixed safe Finder argv for Open inbox.

No real MacWhisper, oMLX, network, Finder, subprocess or audio is used:
the pipeline and ``subprocess.run`` are mocked.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.test import Client

from brainlib.migrations import RECOVERY_COMMAND

pytestmark = [pytest.mark.django_db]


@pytest.fixture
def client():
    return Client()


@contextlib.contextmanager
def _recording_lock(entered):
    entered.append(True)
    yield


def _inbox_config(inbox: Path):
    return SimpleNamespace(storage=SimpleNamespace(inbox=inbox))


# ---------------------------------------------------------------------------
# Rendering: both controls on the normal Library, with CSRF and hooks
# ---------------------------------------------------------------------------


class TestGlobalHeaderControlsRender:
    def _content(self, client, url="/recordings/"):
        response = client.get(url)
        assert response.status_code == 200
        return response.content.decode()

    def _library_content(self, client):
        return self._content(client, "/recordings/")

    def test_controls_are_leftmost_in_topbar(self, client):
        content = self._library_content(client)
        assert 'data-action-form="run-now"' in content
        assert 'data-action-form="open-inbox"' in content
        assert 'action="/recordings/run-now/"' in content
        assert 'action="/recordings/open-inbox/"' in content
        assert ">Run now<" in content
        assert ">Open inbox<" in content
        # Order inside the fixed top navigation: Run now, Open inbox are
        # the LEFTMOST controls, before Ask, Review and Status.
        assert (
            content.index('class="topbar-right"')
            < content.index(">Run now<")
            < content.index(">Open inbox<")
            < content.index(">Ask</a>")
            < content.index(">Review")
            < content.index("Status</span>")
        )
        # The old content-area Library actions row is gone entirely.
        assert 'class="library-actions"' not in content

    def test_accent_button_class_and_css(self, client):
        content = self._library_content(client)
        assert content.count('class="topbar-action-btn"') == 2
        # No leftover neutral class on the action buttons.
        assert 'class="topbar-btn">Run now' not in content
        assert 'class="topbar-btn">Open inbox' not in content
        css = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "static"
            / "workflow"
            / "base.css"
        ).read_text(encoding="utf-8")
        assert ".topbar-action-btn {" in css
        assert "background: var(--color-accent)" in css
        assert "border: 1px solid var(--color-accent)" in css
        assert "color: #fff" in css
        assert ".topbar-action-btn:hover" in css
        assert "background: var(--color-accent-2)" in css

    def test_controls_render_on_all_base_pages_without_duplication(self, client):
        from factories import make_transcribed_recording

        recording, _transcript, _section = make_transcribed_recording(
            ["hello"], sha="nav-all-pages"
        )
        urls = [
            "/recordings/",
            f"/recordings/{recording.pk}/",
            f"/recordings/{recording.pk}/transcript/",
            "/ask/",
            "/review/",
            "/status/",
        ]
        for url in urls:
            content = self._content(client, url)
            assert content.count('data-action-form="run-now"') == 1, url
            assert content.count('data-action-form="open-inbox"') == 1, url
            assert ">Run now<" in content
            assert ">Open inbox<" in content
            assert 'data-action-live="run-now"' not in content
            assert 'data-action-live="open-inbox"' not in content

    def test_topbar_pending_disabled_style_and_js(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "src" / "static" / "workflow" / "base.css").read_text()
        assert ".topbar-action-btn:disabled" in css
        assert '.topbar-action-btn[aria-disabled="true"]' in css
        assert "cursor: not-allowed" in css
        assert "opacity: 0.6" in css
        # Disabled hover cannot look active (accent restored, not accent-2).
        assert ".topbar-action-btn:disabled:hover" in css
        js = (root / "src" / "static" / "workflow" / "app.js").read_text()
        # The submit control is really disabled while the native POST runs.
        assert "control.disabled = true" in js

    def test_controls_render_once_each(self, client):
        content = self._library_content(client)
        assert content.count('data-action-form="run-now"') == 1
        assert content.count('data-action-form="open-inbox"') == 1

    def test_controls_render_on_filtered_view(self, client):
        content = self._content(client, "/recordings/?sort=oldest")
        assert 'data-action-form="run-now"' in content
        assert 'data-action-form="open-inbox"' in content

    def test_forms_carry_csrf_and_no_live_regions(self, client):
        content = self._library_content(client)
        assert content.count('name="csrfmiddlewaretoken"') >= 2
        # No live region / pending text for these topbar controls.
        assert 'data-action-live="run-now"' not in content
        assert 'data-action-live="open-inbox"' not in content
        assert "Opening the inbox folder" not in content
        assert "Running the full pipeline" not in content

    def test_app_js_pending_labels_only(self):
        from django.contrib.staticfiles import finders

        path = finders.find("workflow/app.js")
        assert path is not None
        source = Path(path).read_text(encoding="utf-8")
        assert '"run-now": { label: "Running…", message: "" }' in source
        assert '"open-inbox": { label: "Opening…", message: "" }' in source
        # No visible pending text for these controls.
        assert "Opening the inbox folder" not in source
        assert "ingest, route, transcribe and summarize" not in source


# ---------------------------------------------------------------------------
# GET is a 405 and performs zero work
# ---------------------------------------------------------------------------


class TestGetMethod:
    @pytest.mark.parametrize("url", ["/recordings/run-now/", "/recordings/open-inbox/"])
    def test_get_is_405(self, client, url):
        assert client.get(url).status_code == 405

    def test_get_never_loads_config(self, client, monkeypatch):
        def _fail():
            raise AssertionError("GET must not load configuration")

        monkeypatch.setattr("workflow.views.global_actions.get_config", _fail)
        assert client.get("/recordings/run-now/").status_code == 405
        assert client.get("/recordings/open-inbox/").status_code == 405

    def test_open_inbox_get_never_launches_subprocess(self, client, monkeypatch):
        def _fail(*args, **kwargs):
            raise AssertionError("subprocess must not run on GET")

        monkeypatch.setattr(subprocess, "run", _fail)
        assert client.get("/recordings/open-inbox/").status_code == 405


# ---------------------------------------------------------------------------
# Run now
# ---------------------------------------------------------------------------


class TestRunNow:
    def test_success_runs_pipeline_under_lock_and_redirects(self, client, monkeypatch):
        calls = []
        lock_entries = []

        monkeypatch.setattr(
            "workflow.views.global_actions._migration_preflight_error", lambda: None
        )
        monkeypatch.setattr(
            "workflow.views.global_actions.pipeline_lock",
            lambda config: _recording_lock(lock_entries),
        )
        monkeypatch.setattr(
            "workflow.views.global_actions.run_pipeline",
            lambda config, **kwargs: calls.append((config, kwargs)),
        )
        response = client.post("/recordings/run-now/")
        assert response.status_code == 302
        assert response["Location"] == "/recordings/"
        assert len(calls) == 1
        _config, kwargs = calls[0]
        assert kwargs == {"respect_stability_window": False}
        assert lock_entries == [True]

    def test_recovery_is_not_duplicated(self, client, monkeypatch):
        """``run_pipeline`` already recovers; the view must not call
        recovery again."""
        def _fail(*args, **kwargs):
            raise AssertionError("recovery must not be called by the view")

        monkeypatch.setattr(
            "workflow.views.global_actions._migration_preflight_error", lambda: None
        )
        monkeypatch.setattr(
            "workflow.views.global_actions.pipeline_lock", lambda config: _recording_lock([])
        )
        monkeypatch.setattr(
            "workflow.views.global_actions.run_pipeline", lambda config, **kwargs: {}
        )
        monkeypatch.setattr("workflow.services.pipeline.recover_interruptions", _fail)
        assert client.post("/recordings/run-now/").status_code == 302

    def test_source_never_calls_recovery_directly(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "workflow"
            / "views"
            / "global_actions.py"
        )
        source = path.read_text(encoding="utf-8")
        assert "recover_interruptions(" not in source

    def test_busy_lock_returns_409(self, client, monkeypatch):
        from workflow.services.pipeline_lock import PipelineBusy

        monkeypatch.setattr(
            "workflow.views.global_actions._migration_preflight_error", lambda: None
        )

        def _busy(config):
            raise PipelineBusy("1234")

        monkeypatch.setattr("workflow.views.global_actions.pipeline_lock", _busy)
        response = client.post("/recordings/run-now/")
        assert response.status_code == 409

    def test_pending_migrations_reject_before_config_lock_and_pipeline(self, client, monkeypatch):
        monkeypatch.setattr(
            "brainlib.migrations.unapplied_migrations", lambda: ["workflow.0099_fake"]
        )

        def _forbidden(*args, **kwargs):
            raise AssertionError("config/lock/pipeline must not run with pending migrations")

        monkeypatch.setattr("workflow.views.global_actions.get_config", _forbidden)
        monkeypatch.setattr("workflow.views.global_actions.pipeline_lock", _forbidden)
        monkeypatch.setattr("workflow.views.global_actions.run_pipeline", _forbidden)
        response = client.post("/recordings/run-now/")
        assert response.status_code == 400
        content = response.content.decode()
        assert RECOVERY_COMMAND in content
        assert "uv run python src/manage.py migrate" in content

    def test_uninspectable_migrations_reject_before_config_lock_and_pipeline(
        self, client, monkeypatch
    ):
        from brainlib.migrations import MigrationInspectionError

        def _uninspectable():
            raise MigrationInspectionError("boom")

        monkeypatch.setattr("brainlib.migrations.unapplied_migrations", _uninspectable)

        def _forbidden(*args, **kwargs):
            raise AssertionError("config/lock/pipeline must not run")

        monkeypatch.setattr("workflow.views.global_actions.get_config", _forbidden)
        monkeypatch.setattr("workflow.views.global_actions.pipeline_lock", _forbidden)
        monkeypatch.setattr("workflow.views.global_actions.run_pipeline", _forbidden)
        response = client.post("/recordings/run-now/")
        assert response.status_code == 400
        assert RECOVERY_COMMAND in response.content.decode()

    def test_hostile_pipeline_result_never_rendered(self, client, monkeypatch):
        secret = "/private/tmp/secret-audio.wav"
        monkeypatch.setattr(
            "workflow.views.global_actions._migration_preflight_error", lambda: None
        )
        monkeypatch.setattr(
            "workflow.views.global_actions.pipeline_lock", lambda config: _recording_lock([])
        )
        monkeypatch.setattr(
            "workflow.views.global_actions.run_pipeline",
            lambda config, **kwargs: {"ingest": {"hashed": [secret]}},
        )
        response = client.post("/recordings/run-now/", follow=True)
        assert response.status_code == 200
        assert secret not in response.content.decode()
        assert "secret-audio" not in response.content.decode()

    def test_pipeline_failure_sanitized(self, client, monkeypatch):
        monkeypatch.setattr(
            "workflow.views.global_actions._migration_preflight_error", lambda: None
        )
        monkeypatch.setattr(
            "workflow.views.global_actions.pipeline_lock", lambda config: _recording_lock([])
        )

        def _explode(config, **kwargs):
            raise RuntimeError("boom at /private/secret/path with SQL SELECT")

        monkeypatch.setattr("workflow.views.global_actions.run_pipeline", _explode)
        response = client.post("/recordings/run-now/", follow=True)
        assert response.status_code == 200
        content = response.content.decode()
        assert "Run now could not be completed" in content
        assert "boom" not in content
        assert "/private/secret" not in content
        assert "SELECT" not in content

    def test_post_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        assert csrf_client.post("/recordings/run-now/").status_code == 403


# ---------------------------------------------------------------------------
# Open inbox
# ---------------------------------------------------------------------------


class TestOpenInbox:
    def test_success_calls_fixed_safe_argv(self, client, monkeypatch, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        captured = {}
        monkeypatch.setattr(
            "workflow.views.global_actions.get_config", lambda: _inbox_config(inbox)
        )

        def _run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(subprocess, "run", _run)
        response = client.post("/recordings/open-inbox/", follow=True)
        assert response.status_code == 200
        assert response.redirect_chain == [("/recordings/", 302)]
        content = response.content.decode()
        # Success shows NO flash message — only the plain Library renders.
        assert "Opening the inbox folder" not in content
        assert "inbox folder could not be opened" not in content
        assert captured["argv"] == ["/usr/bin/open", str(inbox)]
        kwargs = captured["kwargs"]
        assert kwargs["timeout"] == 5
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert "shell" not in kwargs or kwargs["shell"] is False

    def test_no_lock_or_recovery(self, client, monkeypatch, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        monkeypatch.setattr(
            "workflow.views.global_actions.get_config", lambda: _inbox_config(inbox)
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
        )
        monkeypatch.setattr(
            "workflow.views.global_actions.pipeline_lock",
            lambda config: (_ for _ in ()).throw(AssertionError("no lock")),
        )
        monkeypatch.setattr(
            "workflow.services.pipeline.recover_interruptions",
            lambda config: (_ for _ in ()).throw(AssertionError("no recovery")),
        )
        assert client.post("/recordings/open-inbox/").status_code == 302

    def test_client_path_injection_ignored(self, client, monkeypatch, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        captured = {}
        monkeypatch.setattr(
            "workflow.views.global_actions.get_config", lambda: _inbox_config(inbox)
        )

        def _run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(subprocess, "run", _run)
        response = client.post(
            "/recordings/open-inbox/?path=/etc",
            {"path": "/etc", "url": "file:///etc", "destination": "/Applications"},
        )
        assert response.status_code == 302
        assert captured["argv"] == ["/usr/bin/open", str(inbox)]
        assert "/etc" not in captured["argv"]

    def test_missing_directory_is_sanitized(self, client, monkeypatch, tmp_path):
        missing = tmp_path / "does-not-exist"
        monkeypatch.setattr(
            "workflow.views.global_actions.get_config", lambda: _inbox_config(missing)
        )

        def _fail(*args, **kwargs):
            raise AssertionError("subprocess must not run for a missing inbox")

        monkeypatch.setattr(subprocess, "run", _fail)
        response = client.post("/recordings/open-inbox/", follow=True)
        assert response.status_code == 200
        content = response.content.decode()
        assert "inbox folder could not be opened" in content
        assert "does-not-exist" not in content

    def test_oserror_is_sanitized(self, client, monkeypatch, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        monkeypatch.setattr(
            "workflow.views.global_actions.get_config", lambda: _inbox_config(inbox)
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda argv, **kwargs: (_ for _ in ()).throw(OSError("permission denied")),
        )
        response = client.post("/recordings/open-inbox/", follow=True)
        content = response.content.decode()
        assert "inbox folder could not be opened" in content
        assert "permission denied" not in content
        assert str(inbox) not in content

    def test_timeout_is_sanitized(self, client, monkeypatch, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        monkeypatch.setattr(
            "workflow.views.global_actions.get_config", lambda: _inbox_config(inbox)
        )

        def _timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

        monkeypatch.setattr(subprocess, "run", _timeout)
        response = client.post("/recordings/open-inbox/", follow=True)
        content = response.content.decode()
        assert "inbox folder could not be opened" in content
        assert str(inbox) not in content

    def test_nonzero_returncode_is_sanitized(self, client, monkeypatch, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        monkeypatch.setattr(
            "workflow.views.global_actions.get_config", lambda: _inbox_config(inbox)
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1),
        )
        response = client.post("/recordings/open-inbox/", follow=True)
        content = response.content.decode()
        assert "inbox folder could not be opened" in content
        assert str(inbox) not in content

    def test_post_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        assert csrf_client.post("/recordings/open-inbox/").status_code == 403
