"""Security and privacy controls for the Step 4 web interface.

Proves: security headers (CSP, X-Frame-Options, nosniff, referrer
policy), message-cookie flags, no secret/path leakage on any page, and
custom error templates.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.test import Client, override_settings

from brainlib.config import StorageConfig
from factories import make_summary_version, make_tag, make_tag_assignment, make_transcribed_recording

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


@pytest.fixture
def client():
    return Client()


class TestSecurityHeaders:
    def test_csp_header_on_all_pages(self, client):
        for url in ("/", "/recordings/", "/tags/", "/review/", "/health/"):
            response = client.get(url)
            csp = response.headers.get("Content-Security-Policy", "")
            assert "default-src 'self'" in csp, url
            assert "frame-ancestors 'none'" in csp
            assert "script-src 'self'" in csp
            # No inline script allowance, no external origins.
            assert "unsafe-inline" not in csp
            assert "http://" not in csp and "https://" not in csp

    def test_frame_deny_and_nosniff(self, client):
        response = client.get("/recordings/")
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("Referrer-Policy") == "same-origin"

    def test_csp_allows_only_external_local_scripts(self, client):
        """Rendered pages may include the local app.js, but never inline
        scripts, inline styles, or inline event handlers."""
        import re

        content = client.get("/recordings/").content.decode("utf-8")
        scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", content, re.DOTALL)
        # The only script is the external local app.js with an empty body.
        assert len(scripts) == 1
        assert scripts[0].strip() == ""
        for match in re.finditer(r"<script\b[^>]*>", content):
            tag = match.group(0)
            src = re.search(r'\bsrc=["\']([^"\']+)["\']', tag)
            assert src, f"script without src: {tag}"
            assert src.group(1).startswith("/static/"), f"non-local script src: {tag}"
        # No inline styles anywhere.
        assert "<style" not in content
        assert "style=" not in content
        # No inline event handlers (precise attribute match, not a bare
        # substring scan of the whole page).
        for handler in re.findall(r'\bon(?:click|change|submit|load|error)\s*=', content):
            raise AssertionError(f"inline event handler found: {handler}")

    def test_message_cookie_security_flags(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="sec-1")
        # A POST/redirect/GET cycle sets the signed message cookie.
        client.get(f"/recordings/{recording.pk}/")  # warm-up
        response = client.post(
            f"/recordings/{recording.pk}/tags/add/", {"tag": ""}
        )
        assert response.status_code == 302
        cookie = None
        for name, value in response.cookies.items():
            if "message" in name:
                cookie = value
        assert cookie is not None, "expected a message cookie"
        assert cookie["httponly"] is True
        assert cookie["samesite"] == "Lax"


class TestSecretHygiene:
    def test_no_secrets_on_pages(self, client, monkeypatch):
        monkeypatch.setenv("BRAIN_TEST_LLM_API_KEY", "super-secret-value-42")
        recording, _t, _s = make_transcribed_recording(["x"], sha="sec-2")
        make_summary_version(recording, _t, _s)
        make_tag_assignment(recording, make_tag("Family"))
        # `/` is the pre-existing Step 1 status page, which intentionally
        # shows the owner's configured storage paths locally; everything
        # else (all Step 4 pages) must show no secrets and no paths.
        for url in (
            "/recordings/",
            f"/recordings/{recording.pk}/",
            f"/recordings/{recording.pk}/summary/",
            f"/recordings/{recording.pk}/history/",
            "/tags/",
            "/review/",
            "/status/",
            f"/recordings/{recording.pk}/summary/export/?format=json",
        ):
            content = client.get(url).content.decode("utf-8")
            assert "super-secret-value-42" not in content, url
            assert "BRAIN_TEST_LLM_API_KEY" not in content, url
            assert "/Users/" not in content, url
            assert ".sqlite3" not in content, url
        home = client.get("/status/").content.decode("utf-8")
        assert "super-secret-value-42" not in home
        assert "BRAIN_TEST_LLM_API_KEY" not in home

    def test_no_absolute_paths_or_tracebacks_on_detail(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="sec-3")
        content = client.get(f"/recordings/{recording.pk}/").content.decode("utf-8")
        assert "Traceback" not in content
        assert "/private/var" not in content
        assert "/Users/" not in content

    def test_health_stays_sanitized(self, client):
        payload = client.get("/health/").json()
        assert payload["status"] in {"ok", "degraded"}


class TestErrorPages:
    @override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
    def test_404_uses_custom_template(self, client):
        response = client.get("/this-page-does-not-exist/")
        assert response.status_code == 404
        assert "404.html" in [t.name for t in response.templates]

    def test_400_rejection_page(self, client):
        from workflow.views.helpers import rejection_response

        response = rejection_response(Client().get("/").wsgi_request, "nope", "test_code")
        assert response.status_code == 400
        assert "nope" in response.content.decode()

    def test_409_conflict_page(self, client):
        from workflow.views.helpers import conflict_response

        response = conflict_response(Client().get("/").wsgi_request, "")
        assert response.status_code == 409
        assert "Pipeline busy" in response.content.decode()


class TestLocalBindingDocs:
    def test_allowed_hosts_remain_local(self):
        from django.conf import settings

        assert set(settings.ALLOWED_HOSTS) <= {"localhost", "127.0.0.1", "testserver"}
        assert settings.X_FRAME_OPTIONS == "DENY"
        assert settings.MESSAGE_STORAGE.endswith("CookieStorage")


class TestHomePagePathPrivacy:
    """Home page must show storage availability WITHOUT absolute paths
    (review finding 3). A unique sentinel path would be unmistakable
    if leaked."""

    SENTINEL_NAME = "BRAIN-SENTINEL-7f3a9c2e4b1d"

    def _sentinel_config(self, tmp_path):
        from factories import make_config

        self.SENTINEL = str(tmp_path / self.SENTINEL_NAME / "9e2c1a")
        sentinel = Path(self.SENTINEL)
        return make_config(tmp_path, storage=StorageConfig(
            inbox=sentinel / "inbox",
            database=sentinel / "db" / f"brain-{self.SENTINEL_NAME}.sqlite3",
            transcripts=sentinel / "transcripts",
            exports=sentinel / "exports",
            logs=sentinel / "logs",
            temp=sentinel / "temp",
        ))

    def _no_mutation(self, client):
        from workflow.models import Recording

        return Recording.objects.count()

    def test_home_hides_absolute_paths_behind_sentinel(self, client, tmp_path, monkeypatch):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        from workflow import views

        sentinel_config = self._sentinel_config(tmp_path)
        before_counts = self._no_mutation(client)
        monkeypatch.setattr(views, "load_config", lambda: sentinel_config)
        with CaptureQueriesContext(connection) as ctx:
            response = client.get("/status/")
        content = response.content.decode()

        # 1-3. The sentinel and any absolute-path fragment are absent.
        assert self.SENTINEL not in content
        assert self.SENTINEL_NAME not in content
        assert "9e2c1a" not in content
        assert "/var/folders" not in content
        assert "/Users/" not in content
        assert "/private/var" not in content
        assert ".sqlite3" not in content
        # 4. Safe labels and statuses present.
        for label in ("Inbox", "Database", "Transcripts", "Exports", "Logs", "Temporary storage"):
            assert label in content
        assert "available" in content or "missing" in content
        assert "macwhisper.command" not in content
        # 5. No path smuggled into attributes or scripts.
        import re

        for attr_value in re.findall(r'(?:href|src|data-[\w-]+)="([^"]*)"', content):
            assert self.SENTINEL not in attr_value
            assert not attr_value.startswith("/var") and not attr_value.startswith("/Users")
        for script in re.findall(r"<script[^>]*>(.*?)</script>", content, re.DOTALL):
            assert self.SENTINEL not in script
            assert script.strip() == ""  # CSP-safe: no inline scripts at all
        # 6-7. No writes and no processing queries (only SELECT-status probes).
        assert self._no_mutation(client) == before_counts
        for q in ctx.captured_queries:
            sql = q["sql"].lstrip().upper()
            assert sql.startswith("SELECT") or sql.startswith("SAVEPOINT") or sql.startswith("RELEASE"), sql

    def test_health_path_free_with_sentinel_dirs_missing(self, client, tmp_path, monkeypatch):
        """With the sentinel storage dirs absent, /health/ reports 503
        unhealthy with statuses only — the sentinel never appears."""
        from workflow import views

        sentinel_config = self._sentinel_config(tmp_path)
        monkeypatch.setattr(views, "load_config", lambda: sentinel_config)
        response = client.get("/health/")
        content = response.content.decode()
        assert response.status_code == 503
        assert self.SENTINEL not in content
        assert self.SENTINEL_NAME not in content
        assert ".sqlite3" not in content
        payload = response.json()
        assert payload["status"] == "unhealthy"
        # Health reports statuses, not paths.
        assert set(payload["application"]) == {"config", "runtime_directories", "database"}

    def test_doctor_diagnostics_unchanged_by_privacy_fix(self, tmp_path):
        """`brain doctor`'s diagnostics surface is unchanged: it still
        inspects and names the real configured locations (paths remain a
        terminal concern, not a web-page concern)."""
        from brainlib.diagnostics import check_database_location

        sentinel_config = self._sentinel_config(tmp_path)
        db_result = check_database_location(sentinel_config)
        # The parent dir does not exist -> FAIL naming the real location
        # for the terminal user. No web page ever shows this.
        assert db_result.status == "FAIL"
        assert self.SENTINEL in db_result.detail
