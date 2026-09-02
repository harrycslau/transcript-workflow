"""Migration readiness (post-Step-4 hardening).

Regression coverage for the first-real-audio failure: doctor PASSed the
database while migrations 0003-0005 were unapplied, and `brain run`
then crashed with an ORM OperationalError.

Proves:
- the read-only inspection via Django's real MigrationExecutor/graph;
- doctor's Database-migrations check (PASS when applied, FAIL with safe
  labels when pending, sanitized on inspection failure, exit 1, no
  traceback, optional-dependency semantics unchanged);
- the shared CLI schema preflight: every ORM command fails cleanly with
  exit 1 and the exact recovery command BEFORE any lock, recovery,
  inbox scan, subprocess, network, or DB mutation; never auto-migrates;
- `brain serve` refuses to bind when migrations are pending;
- exit codes 0/2/3 semantics unchanged on a fully migrated database;
- one faithful fresh-process regression against a partially migrated
  throwaway database (never the user's real database).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.test.utils import CaptureQueriesContext
from django.db import connection

from brainlib import cli, diagnostics
from brainlib.migrations import (
    CATEGORY_INCONSISTENT,
    CATEGORY_UNAVAILABLE,
    MigrationInspectionError,
    unapplied_migrations,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

RECOVERY = "uv run python src/manage.py migrate"


def _no_applied(self):
    return {}


def _partially_applied(self):
    # Mirrors the observed real incident: 0001-0002 applied, 0003-0005 not.
    return {
        ("workflow", "0001_initial"): None,
        ("workflow", "0002_recording_last_failed_attempt_and_more"): None,
    }


@pytest.fixture
def no_external(monkeypatch):
    def _no_subprocess(*args, **kwargs):
        raise AssertionError("subprocess must not run during the failed preflight")

    def _no_http(*args, **kwargs):
        raise AssertionError("HTTP must not be contacted during the failed preflight")

    monkeypatch.setattr("subprocess.run", _no_subprocess)
    monkeypatch.setattr("subprocess.Popen", _no_subprocess)
    monkeypatch.setattr("httpx.Client", _no_http)
    monkeypatch.setattr("httpx.get", _no_http)
    monkeypatch.setattr("httpx.post", _no_http)


def _guard_no_pipeline_side_effects(monkeypatch):
    def _fail(what):
        def raiser(*args, **kwargs):
            raise AssertionError(f"{what} must not run after a failed preflight")

        return raiser

    monkeypatch.setattr("workflow.services.pipeline.recover_interruptions", _fail("recovery"))
    monkeypatch.setattr("workflow.services.pipeline.pipeline_lock", _fail("pipeline lock"))
    monkeypatch.setattr("workflow.services.pipeline.run_ingest", _fail("ingest"))
    monkeypatch.setattr("workflow.services.pipeline.run_pipeline", _fail("run"))
    monkeypatch.setattr("workflow.services.pipeline.transcribe_ready", _fail("transcribe"))
    monkeypatch.setattr("django.core.management.call_command", _fail("management command"))


def _db_counts() -> dict:
    from workflow.models import ProcessingAttempt, Recording, Tag, TagAssignment

    return {
        "recordings": Recording.objects.count(),
        "attempts": ProcessingAttempt.objects.count(),
        "tags": Tag.objects.count(),
        "assignments": TagAssignment.objects.count(),
    }


# ---------------------------------------------------------------------------
# Inspection (real executor/recorder state, read-only)
# ---------------------------------------------------------------------------


class TestMigrationInspection:
    @pytest.mark.django_db
    def test_fully_migrated_database_passes(self):
        # The pytest test database is fully migrated by pytest-django.
        assert unapplied_migrations() == []

    @pytest.mark.django_db
    def test_nothing_applied_fails_listing_all_workflow_migrations(self, monkeypatch):
        monkeypatch.setattr(
            "django.db.migrations.recorder.MigrationRecorder.applied_migrations", _no_applied
        )
        pending = unapplied_migrations()
        assert pending, "empty applied set must yield a pending plan"
        assert any(p.startswith("workflow.0003_") for p in pending)
        assert any(p.startswith("workflow.0004_") for p in pending)
        assert any(p.startswith("workflow.0005_") for p in pending)
        assert any(p.startswith("workflow.0006_") for p in pending)
        # Labels are safe identifiers: no paths, spaces, SQL, or content.
        for label in pending:
            assert "/" not in label
            assert " " not in label
            assert label.count(".") == 1

    @pytest.mark.django_db
    def test_partial_history_mirrors_the_real_incident(self, monkeypatch):
        monkeypatch.setattr(
            "django.db.migrations.recorder.MigrationRecorder.applied_migrations",
            _partially_applied,
        )
        pending = unapplied_migrations()
        assert pending == [
            "workflow.0003_tag_recording_resummarization_failed_and_more",
            "workflow.0004_backfill_summary_status",
            "workflow.0005_tagassignment_deactivated_by_and_more",
            "workflow.0006_attempt_context_json",
            "workflow.0007_summary_multilingual",
        ]

    def test_unavailable_migration_table_category(self, monkeypatch):
        monkeypatch.setattr(
            "django.db.migrations.recorder.MigrationRecorder.has_table", lambda self: False
        )
        with pytest.raises(MigrationInspectionError) as excinfo:
            unapplied_migrations()
        assert excinfo.value.category == CATEGORY_UNAVAILABLE

    @pytest.mark.django_db
    def test_inspection_failure_is_sanitized(self, monkeypatch):
        def broken_executor(*args, **kwargs):
            raise RuntimeError("raw internals: /Users/harry/secret/brain.sqlite3 boom")

        monkeypatch.setattr(
            "django.db.migrations.executor.MigrationExecutor", broken_executor
        )
        with pytest.raises(MigrationInspectionError) as excinfo:
            unapplied_migrations()
        assert excinfo.value.category == CATEGORY_INCONSISTENT
        assert "secret" not in excinfo.value.category
        assert "/Users/" not in str(excinfo.value)

    @pytest.mark.django_db
    def test_inconsistent_history_category(self, monkeypatch):
        # A KNOWN applied migration with an UNAPPLIED known parent is
        # what Django's check_consistent_history treats as an
        # inconsistent history (unknown recorded names are skipped).
        monkeypatch.setattr(
            "django.db.migrations.recorder.MigrationRecorder.applied_migrations",
            lambda self: {("workflow", "0003_tag_recording_resummarization_failed_and_more"): None},
        )
        with pytest.raises(MigrationInspectionError) as excinfo:
            unapplied_migrations()
        assert excinfo.value.category == CATEGORY_INCONSISTENT

    @pytest.mark.django_db
    def test_inspection_is_read_only_no_writes(self, monkeypatch):
        monkeypatch.setattr(
            "django.db.migrations.recorder.MigrationRecorder.applied_migrations", _no_applied
        )
        with CaptureQueriesContext(connection) as ctx:
            unapplied_migrations()
        for q in ctx.captured_queries:
            sql = q["sql"].lstrip().upper()
            assert sql.startswith(("SELECT", "SAVEPOINT", "RELEASE")), sql


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


class TestDoctorMigrationCheck:
    @pytest.mark.django_db
    def test_fully_migrated_doctor_reports_pass_and_exit_zero(
        self, monkeypatch, capsys
    ):
        import httpx

        monkeypatch.setattr(
            diagnostics, "fetch_omlx_models",
            lambda base_url, api_key_env, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")),
        )
        assert cli.main(["doctor"]) == 0
        output = capsys.readouterr().out
        assert "Database migrations" in output
        migration_line = next(line for line in output.splitlines() if "Database migrations" in line)
        assert "PASS" in migration_line
        assert "all migrations applied" in migration_line

    @pytest.mark.django_db
    def test_pending_migrations_doctor_fails_exits_one_no_traceback(
        self, monkeypatch, capsys
    ):  # doctor's MacWhisper check legitimately spawns `mw version`
        monkeypatch.setattr(
            "django.db.migrations.recorder.MigrationRecorder.applied_migrations",
            _partially_applied,
        )
        assert cli.main(["doctor"]) == 1
        output = capsys.readouterr().out
        assert "Database migrations" in output
        migration_line = next(line for line in output.splitlines() if "Database migrations" in line)
        assert "FAIL" in migration_line
        assert "workflow.0003_" in migration_line
        assert RECOVERY in migration_line
        assert "Traceback" not in output
        assert "Traceback" not in capsys.readouterr().err

    @pytest.mark.django_db
    def test_doctor_optional_dependencies_stay_warnings_when_migrations_pending(
        self, monkeypatch, capsys
    ):
        # mw missing + oMLX unreachable + pending migrations: the migration
        # FAIL dominates the exit code, but optional deps remain WARN.
        monkeypatch.setattr(
            "django.db.migrations.recorder.MigrationRecorder.applied_migrations",
            _partially_applied,
        )
        assert cli.main(["doctor"]) == 1
        output = capsys.readouterr().out
        assert "not found" in output      # MacWhisper missing -> WARN
        assert "unreachable" in output    # oMLX unavailable -> WARN
        migration_line = next(line for line in output.splitlines() if "Database migrations" in line)
        assert "FAIL" in migration_line

    @pytest.mark.django_db
    def test_doctor_inspection_failure_is_sanitized(self, monkeypatch, capsys):
        def broken_executor(*args, **kwargs):
            raise RuntimeError("raw internals: /Users/harry/secret/brain.sqlite3 boom")

        monkeypatch.setattr(
            "django.db.migrations.executor.MigrationExecutor", broken_executor
        )
        assert cli.main(["doctor"]) == 1
        output = capsys.readouterr().out
        assert "cannot verify" in output
        assert "raw internals" not in output
        assert "/Users/" not in output
        assert "Traceback" not in output


# ---------------------------------------------------------------------------
# CLI schema preflight
# ---------------------------------------------------------------------------


class TestCliPreflight:
    @pytest.mark.django_db
    def test_run_fails_cleanly_before_any_work(self, monkeypatch, capsys, no_external):
        monkeypatch.setattr(
            "django.db.migrations.recorder.MigrationRecorder.applied_migrations",
            _partially_applied,
        )
        _guard_no_pipeline_side_effects(monkeypatch)
        before = _db_counts()
        assert cli.main(["run", "--json"]) == 1
        captured = capsys.readouterr()
        assert before == _db_counts()  # no rows created
        assert "out of date" in captured.err
        assert RECOVERY in captured.err
        assert "Traceback" not in captured.err
        assert "Traceback" not in captured.out
        assert captured.out == ""

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        "argv",
        [
            ["ingest", "--json"],
            ["route", "--json"],
            ["transcribe", "--json"],
            ["run", "--json"],
            ["summarize", "--json"],
            ["retry", "some-id", "--json"],
            ["status", "--json"],
            ["review", "--json"],
            ["transcripts", "some-id", "--json"],
            ["summaries", "some-id", "--json"],
            ["summary", "some-id", "--json"],
            ["tags", "--json"],
            ["tags", "--sync", "--json"],
        ],
    )
    def test_every_orm_command_exits_one_with_recovery_command(
        self, monkeypatch, capsys, no_external, argv
    ):
        monkeypatch.setattr(
            "django.db.migrations.recorder.MigrationRecorder.applied_migrations",
            _partially_applied,
        )
        _guard_no_pipeline_side_effects(monkeypatch)
        before = _db_counts()
        assert cli.main(argv) == 1
        captured = capsys.readouterr()
        assert RECOVERY in captured.err
        assert "Traceback" not in captured.err
        assert before == _db_counts()

    @pytest.mark.django_db
    def test_fully_migrated_read_only_command_unchanged(self, capsys):
        assert cli.main(["status", "--json"]) == 0
        captured = capsys.readouterr()
        assert "error:" not in captured.err

    @pytest.mark.django_db
    def test_lock_contention_exit_code_3_preserved(self, monkeypatch, capsys):
        from workflow.services.pipeline_lock import PipelineBusy

        def busy_lock(config):
            raise PipelineBusy("4242")

        monkeypatch.setattr("workflow.services.pipeline.pipeline_lock", busy_lock)
        assert cli.main(["status", "--json"]) == 0  # read-only never locks
        assert cli.main(["ingest", "--json"]) == 3
        captured = capsys.readouterr()
        assert "another pipeline process is active" in captured.err

    @pytest.mark.django_db
    def test_never_auto_migrates(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "django.db.migrations.recorder.MigrationRecorder.applied_migrations",
            _partially_applied,
        )
        _guard_no_pipeline_side_effects(monkeypatch)  # call_command -> raiser
        assert cli.main(["status", "--json"]) == 1


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


class TestServePreflight:
    @pytest.mark.django_db
    def test_pending_migrations_serve_exits_one_never_binds(
        self, monkeypatch, capsys, no_external
    ):
        monkeypatch.setattr(
            "django.db.migrations.recorder.MigrationRecorder.applied_migrations",
            _partially_applied,
        )

        def fail_call_command(*args, **kwargs):
            raise AssertionError("runserver must never start with pending migrations")

        monkeypatch.setattr("django.core.management.call_command", fail_call_command)
        assert cli.main(["serve", "--host", "127.0.0.1", "--port", "8787"]) == 1
        captured = capsys.readouterr()
        assert RECOVERY in captured.err
        assert "Starting Brain" not in captured.out
        assert "Traceback" not in captured.err

    @pytest.mark.django_db
    def test_fully_migrated_serve_still_reaches_runserver(self, monkeypatch, capsys):
        recorded = {}

        def fake_call_command(name, address, **kwargs):
            recorded["name"] = name
            recorded["address"] = address

        monkeypatch.setattr("django.core.management.call_command", fake_call_command)
        assert cli.main(["serve"]) == 0
        assert recorded["name"] == "runserver"
        assert recorded["address"] == "127.0.0.1:8787"


# ---------------------------------------------------------------------------
# Faithful fresh-process regression (throwaway partially-migrated DB;
# the user's real database is never touched)
# ---------------------------------------------------------------------------


def _write_bare_config(tmp_path: Path) -> Path:
    import yaml

    from factories import make_config

    config = make_config(tmp_path)
    data = {
        "storage": {name: str(getattr(config.storage, name)) for name in
                    ("inbox", "database", "transcripts", "exports", "logs", "temp")},
        "macwhisper": {
            "command": config.macwhisper.command,
            "model": None,
            "language": "auto",
            "speakers": True,
            "output_format": json_default(),
            "file_stable_seconds": 30,
            "cli_timeout_seconds": 600,
        },
        "llm": {
            "provider": "openai_compatible",
            "base_url": "http://127.0.0.1:1/v1",
            "model": "",
            "api_key_env": "BRAIN_TEST_LLM_API_KEY",
            "temperature": 0.2,
            "timeout_seconds": 600,
        },
        "embedding": {"base_url": "http://127.0.0.1:1/v1", "model": "",
                      "api_key_env": "BRAIN_TEST_LLM_API_KEY"},
        "retention": {"enabled": False, "audio_days": 3, "delete_mode": "permanent",
                      "require_transcript": True, "require_summary": True},
        "summarization": {"enabled": True, "prompt_version": "1",
                          "max_input_characters": 120000, "chunk_characters": 24000,
                          "chunk_overlap_characters": 1000, "max_chunk_count": 8,
                          "max_total_characters": 960000, "temperature": 0.2,
                          "max_output_tokens": 3000},
        "tags": {"allowed": [{"name": "Unknown", "description": "x"}]},
        "web": {"recordings_per_page": 25, "transcript_segments_per_page": 200},
    }
    path = tmp_path / "mig-config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def json_default() -> str:
    return "json"


def run_fresh(args: list[str], brain_config: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "BRAIN_CONFIG": brain_config}
    env.pop("DJANGO_SETTINGS_MODULE", None)
    return subprocess.run(
        [sys.executable, "-m", "brainlib.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=180,
    )


def run_manage(tmp_path: Path, config_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "BRAIN_CONFIG": str(config_path)}
    env.pop("DJANGO_SETTINGS_MODULE", None)
    return subprocess.run(
        [sys.executable, "src/manage.py", *args, "--no-color"],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=180,
    )


class TestFreshProcessPartiallyMigratedDatabase:
    @pytest.fixture
    def isolated_config(self, tmp_path):
        return _write_bare_config(tmp_path)

    def test_partially_migrated_database_reproduces_the_real_incident(
        self, tmp_path, isolated_config
    ):
        # Apply ONLY 0001-0002, exactly like the machine that crashed.
        applied = run_manage(tmp_path, isolated_config, "migrate", "workflow", "0002")
        assert applied.returncode == 0, applied.stderr

        doctor = run_fresh(["doctor"], str(isolated_config))
        assert doctor.returncode == 1
        assert "Database migrations" in doctor.stdout
        assert "workflow.0003_" in doctor.stdout
        assert RECOVERY in doctor.stdout
        assert "Traceback" not in doctor.stdout
        assert "Traceback" not in doctor.stderr

        status = run_fresh(["status", "--json"], str(isolated_config))
        assert status.returncode == 1
        assert RECOVERY in status.stderr
        assert "out of date" in status.stderr
        assert "Traceback" not in status.stderr
        assert status.stdout == ""

        run = run_fresh(["run", "--json"], str(isolated_config))
        assert run.returncode == 1
        assert RECOVERY in run.stderr
        assert "OperationalError" not in run.stderr
        assert "no such column" not in run.stderr
        assert "Traceback" not in run.stderr

    def test_fully_migrated_throwable_database_cli_works(self, tmp_path, isolated_config):
        applied = run_manage(tmp_path, isolated_config, "migrate")
        assert applied.returncode == 0, applied.stderr
        status = run_fresh(["status", "--json"], str(isolated_config))
        assert status.returncode == 0, status.stderr
        doctor = run_fresh(["doctor"], str(isolated_config))
        assert doctor.returncode == 0, doctor.stdout
        assert "all migrations applied" in doctor.stdout
