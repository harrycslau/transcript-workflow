"""Genuine MigrationExecutor tests for migration 0014 (Recording archive).

Migrates an ISOLATED SQLite database for real: 0013 -> 0014 forward
(existing Recordings migrate as active/NULL, the column is nullable), the
column shape, and 0014 -> 0013 reverse (additive nullable column dropped
with zero data loss). Applying the migration never contacts the network,
MacWhisper, or the embedding client (raising guards).
"""

from __future__ import annotations

import copy as _copy
import importlib

import pytest
from django.db import connections
from django.db.migrations.executor import MigrationExecutor

TARGET_0013 = ("workflow", "0013_section_title_is_temporary")
TARGET_0014 = ("workflow", "0014_recording_archived_at")

ALIAS = "mig0014"

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
    connections.databases[alias]["NAME"] = str(tmp_path / "archive-migration.sqlite3")
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


def _seed_recording(apps, alias, sha):
    Recording = apps.get_model("workflow", "Recording")
    return Recording.objects.using(alias).create(
        sha256=sha, processing_status="transcribed", summary_status="current"
    )


def test_migration_operations_shape_and_dependencies():
    mig = importlib.import_module("workflow.migrations.0014_recording_archived_at")
    from django.db import migrations

    assert mig.Migration.dependencies == [("workflow", "0013_section_title_is_temporary")]
    ops = mig.Migration.operations
    assert len(ops) == 1
    assert type(ops[0]) is migrations.AddField
    assert ops[0].name == "archived_at"
    assert ops[0].field.null is True
    assert ops[0].field.get_internal_type() == "DateTimeField"


def test_forward_adds_nullable_column_existing_rows_null(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0013 = _migrate_to(executor, TARGET_0013)
    recording = _seed_recording(apps0013, alias, "e" * 64)

    _migrate_to(executor, TARGET_0014)

    with connection.cursor() as cursor:
        cursor.execute("PRAGMA table_info(workflow_recording)")
        columns = {row[1]: row for row in cursor.fetchall()}
    assert "archived_at" in columns
    assert columns["archived_at"][3] == 0  # notnull flag: nullable
    assert columns["archived_at"][2].lower() == "datetime"

    with connection.cursor() as cursor:
        cursor.execute("SELECT archived_at FROM workflow_recording")
        assert cursor.fetchall() == [(None,)]
    assert recording is not None

    # The 0014 model accepts setting the marker.
    Recording = _migrate_to(executor, TARGET_0014).get_model("workflow", "Recording")
    Recording.objects.using(alias).filter(pk=recording.pk).update(
        archived_at="2026-01-01 00:00:00+00:00"
    )
    assert Recording.objects.using(alias).get(pk=recording.pk).archived_at is not None


def test_reverse_drops_column_with_zero_data_loss(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0013 = _migrate_to(executor, TARGET_0013)
    recording = _seed_recording(apps0013, alias, "f" * 64)

    apps0014 = _migrate_to(executor, TARGET_0014)
    Recording = apps0014.get_model("workflow", "Recording")
    Recording.objects.using(alias).filter(pk=recording.pk).update(
        archived_at="2026-01-01 00:00:00+00:00"
    )

    executor.migrate([TARGET_0013])
    executor.loader.build_graph()

    ddl = _table_ddl(connection, "workflow_recording")
    assert "archived_at" not in ddl
    with connection.cursor() as cursor:
        cursor.execute("SELECT sha256, processing_status FROM workflow_recording")
        assert cursor.fetchall() == [("f" * 64, "transcribed")]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM django_migrations"
            " WHERE app='workflow' AND name='0014_recording_archived_at'"
        )
        assert cursor.fetchone()[0] == 0


def test_no_network_or_subprocess_during_migration(executor_and_alias, monkeypatch):
    executor, _connection, alias = executor_and_alias
    apps0013 = _migrate_to(executor, TARGET_0013)
    _seed_recording(apps0013, alias, "1" * 64)

    def _fail(what):
        def raiser(*args, **kwargs):
            raise AssertionError(f"{what} must not run during migration 0014")

        return raiser

    monkeypatch.setattr("httpx.Client", _fail("httpx.Client"))
    monkeypatch.setattr("httpx.get", _fail("httpx.get"))
    monkeypatch.setattr("httpx.post", _fail("httpx.post"))
    monkeypatch.setattr("subprocess.run", _fail("subprocess.run"))
    monkeypatch.setattr("subprocess.Popen", _fail("subprocess.Popen"))
    monkeypatch.setattr(
        "workflow.services.embedding_client.embed_texts", _fail("embed_texts")
    )

    _migrate_to(executor, TARGET_0014)
