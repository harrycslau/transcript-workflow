"""Genuine MigrationExecutor tests for migration 0015 (Section archive).

Migrates an ISOLATED SQLite database for real: 0014 -> 0015 forward
(existing Sections migrate as active/NULL, the column is nullable), the
column shape, and 0015 -> 0014 reverse (additive nullable column dropped
with zero data loss). Applying the migration never contacts the network,
MacWhisper, or the embedding client (raising guards). Migration 0014 is
never edited.
"""

from __future__ import annotations

import copy as _copy
import importlib

import pytest
from django.db import connections
from django.db.migrations.executor import MigrationExecutor

TARGET_0014 = ("workflow", "0014_recording_archived_at")
TARGET_0015 = ("workflow", "0015_section_archived_at")

ALIAS = "mig0015"

connections.databases.setdefault(
    ALIAS,
    {
        **_copy.deepcopy(connections.databases["default"]),
        "NAME": ":memory:",
        "TEST": {"NAME": ":memory:", "MIRROR": None, "MIGRATE": True},
    },
)

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", ALIAS])


@pytest.fixture()
def executor_and_alias(tmp_path):
    alias = ALIAS
    old = connections[alias]
    try:
        old.close()
    except Exception:
        pass
    try:
        delattr(connections._connections, alias)
    except AttributeError:
        pass
    connections.databases[alias]["NAME"] = str(tmp_path / "section-archive-migration.sqlite3")
    connection = connections[alias]
    executor = MigrationExecutor(connection)
    try:
        yield executor, connection, alias
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _migrate_to(executor, target):
    executor.migrate([target])
    executor.loader.build_graph()
    return executor.loader.project_state([target]).apps


def _table_ddl(connection, table):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=%s", [table]
        )
        row = cursor.fetchone()
        return row[0] if row else None


def _seed_section(apps, alias, sha):
    Recording = apps.get_model("workflow", "Recording")
    ProcessingAttempt = apps.get_model("workflow", "ProcessingAttempt")
    Transcript = apps.get_model("workflow", "Transcript")
    Section = apps.get_model("workflow", "Section")
    recording = Recording.objects.using(alias).create(
        sha256=sha, processing_status="transcribed", summary_status="current"
    )
    attempt = ProcessingAttempt.objects.using(alias).create(
        recording=recording,
        stage="transcription",
        ordinal=1,
        outcome="success",
        finished_at="2026-01-01 00:00:00+00:00",
    )
    transcript = Transcript.objects.using(alias).create(
        recording=recording, attempt=attempt, text_normalized="hello"
    )
    section = Section.objects.using(alias).create(
        transcript=transcript, ordinal=0, title="Full recording"
    )
    return recording, transcript, section


def test_migration_operations_shape_and_dependencies():
    mig = importlib.import_module("workflow.migrations.0015_section_archived_at")
    from django.db import migrations

    assert mig.Migration.dependencies == [("workflow", "0014_recording_archived_at")]
    ops = mig.Migration.operations
    assert len(ops) == 1
    assert type(ops[0]) is migrations.AddField
    assert ops[0].model_name == "section"
    assert ops[0].name == "archived_at"
    assert ops[0].field.null is True
    assert ops[0].field.get_internal_type() == "DateTimeField"


def test_forward_adds_nullable_column_existing_rows_null(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0014 = _migrate_to(executor, TARGET_0014)
    _recording, _transcript, section = _seed_section(apps0014, alias, "e" * 64)

    _migrate_to(executor, TARGET_0015)

    with connection.cursor() as cursor:
        cursor.execute("PRAGMA table_info(workflow_section)")
        columns = {row[1]: row for row in cursor.fetchall()}
    assert "archived_at" in columns
    assert columns["archived_at"][3] == 0  # notnull flag: nullable
    assert columns["archived_at"][2].lower() == "datetime"

    with connection.cursor() as cursor:
        cursor.execute("SELECT archived_at FROM workflow_section")
        assert cursor.fetchall() == [(None,)]
    assert section is not None

    # The 0015 model accepts setting the marker.
    Section = _migrate_to(executor, TARGET_0015).get_model("workflow", "Section")
    Section.objects.using(alias).filter(pk=section.pk).update(
        archived_at="2026-01-01 00:00:00+00:00"
    )
    assert Section.objects.using(alias).get(pk=section.pk).archived_at is not None


def test_reverse_drops_column_with_zero_data_loss(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0014 = _migrate_to(executor, TARGET_0014)
    _recording, _transcript, section = _seed_section(apps0014, alias, "f" * 64)

    apps0015 = _migrate_to(executor, TARGET_0015)
    Section = apps0015.get_model("workflow", "Section")
    Section.objects.using(alias).filter(pk=section.pk).update(
        archived_at="2026-01-01 00:00:00+00:00"
    )

    executor.migrate([TARGET_0014])
    executor.loader.build_graph()

    ddl = _table_ddl(connection, "workflow_section")
    assert "archived_at" not in ddl
    with connection.cursor() as cursor:
        cursor.execute("SELECT ordinal, title FROM workflow_section")
        assert cursor.fetchall() == [(0, "Full recording")]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM django_migrations"
            " WHERE app='workflow' AND name='0015_section_archived_at'"
        )
        assert cursor.fetchone()[0] == 0


def test_no_network_or_subprocess_during_migration(executor_and_alias, monkeypatch):
    executor, _connection, alias = executor_and_alias
    apps0014 = _migrate_to(executor, TARGET_0014)
    _seed_section(apps0014, alias, "1" * 64)

    def _fail(what):
        def raiser(*args, **kwargs):
            raise AssertionError(f"{what} must not run during migration 0015")

        return raiser

    monkeypatch.setattr("httpx.Client", _fail("httpx.Client"))
    monkeypatch.setattr("httpx.get", _fail("httpx.get"))
    monkeypatch.setattr("httpx.post", _fail("httpx.post"))
    monkeypatch.setattr("subprocess.run", _fail("subprocess.run"))
    monkeypatch.setattr("subprocess.Popen", _fail("subprocess.Popen"))
    monkeypatch.setattr(
        "workflow.services.embedding_client.embed_texts", _fail("embed_texts")
    )

    _migrate_to(executor, TARGET_0015)
