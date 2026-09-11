"""Genuine MigrationExecutor tests for migration 0010 (Tag definition
origin: config/custom provenance).

Every test migrates an ISOLATED SQLite database (dedicated connection
alias on a tmp_path file) for real: 0009 -> 0010 forward (seeding a Tag
through the historical 0009 models), the column/constraint shape and DB
CHECK allowlist enforcement, and 0010 -> 0009 reverse. Proves the
migration is additive and reversible: existing Tag rows become
``config`` by default, the CHECK accepts exactly ``config``/``custom``,
reverse drops ONLY the new column + constraint (all other tables and
rows preserved), and applying it never contacts the network or the
embedding client (raising guards).
"""

from __future__ import annotations

import copy as _copy
import importlib

import pytest
from django.db import IntegrityError, connections
from django.db.migrations.executor import MigrationExecutor

TARGET_0009 = ("workflow", "0009_embedding_foundation")
TARGET_0010 = ("workflow", "0010_tag_definition_origin")

ALIAS = "mig0010"

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
    db_path = str(tmp_path / "tag-origin-migration.sqlite3")
    connections.databases[alias]["NAME"] = db_path
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


def _tables(connection):
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def _table_ddl(connection, table):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=%s", [table]
        )
        row = cursor.fetchone()
        return row[0] if row else None


def _seed_tag(apps, alias, *, name="Family", name_key="family", description="d"):
    Tag = apps.get_model("workflow", "Tag")
    return Tag.objects.using(alias).create(
        name=name, name_key=name_key, description=description, is_configured=True
    )


def _insert_origin(connection, value, key=None):
    if key is None:
        key = f"key-{value}-{id(value)}"
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO workflow_tag (name, name_key, description, is_configured,"
            " definition_origin, created_at, updated_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)",
            ["X", key, "", 1, value, "2026-01-01 00:00:00", "2026-01-01 00:00:00"],
        )
        return cursor.lastrowid


# ---------------------------------------------------------------------------
# Operations shape: additive and reversible, no RunPython
# ---------------------------------------------------------------------------


def test_migration_operations_are_additive_field_and_constraint():
    mig = importlib.import_module("workflow.migrations.0010_tag_definition_origin")
    from django.db import migrations

    assert mig.Migration.dependencies == [("workflow", "0009_embedding_foundation")]
    assert [type(op) for op in mig.Migration.operations] == [
        migrations.AddField,
        migrations.AddConstraint,
    ]


# ---------------------------------------------------------------------------
# Forward: column + constraint + existing rows default to config
# ---------------------------------------------------------------------------


def test_forward_adds_column_and_constraint_and_defaults_existing_rows(
    executor_and_alias,
):
    executor, connection, alias = executor_and_alias
    apps0009 = _migrate_to(executor, TARGET_0009)
    tag = _seed_tag(apps0009, alias)
    _seed_tag(apps0009, alias, name="Academic", name_key="academic", description="")

    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM workflow_tag")
        tags_before = cursor.fetchone()[0]

    _migrate_to(executor, TARGET_0010)

    with connection.cursor() as cursor:
        cursor.execute("PRAGMA table_info(workflow_tag)")
        columns = {row[1]: row for row in cursor.fetchall()}
    assert "definition_origin" in columns
    # CharField(max_length=16), NOT NULL (Django applies the model-level
    # default during the table rebuild; SQLite carries no literal DEFAULT).
    assert columns["definition_origin"][2].lower() == "varchar(16)"
    assert columns["definition_origin"][3] == 1  # notnull

    # The DB CHECK allowlist is present on the migrated table.
    ddl = _table_ddl(connection, "workflow_tag")
    assert "chk_tag_definition_origin_allowlist" in ddl
    assert "definition_origin" in ddl

    # Existing rows migrated as config (no data migration needed).
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM workflow_tag")
        assert cursor.fetchone()[0] == tags_before
        cursor.execute(
            "SELECT definition_origin FROM workflow_tag ORDER BY name_key"
        )
        assert [row[0] for row in cursor.fetchall()] == ["config", "config"]
    assert tag is not None


def test_check_constraint_allowlist_enforced_on_migrated_schema(executor_and_alias):
    executor, connection, alias = executor_and_alias
    _migrate_to(executor, TARGET_0009)
    _migrate_to(executor, TARGET_0010)

    _insert_origin(connection, "config")
    _insert_origin(connection, "custom")
    with pytest.raises(IntegrityError):
        _insert_origin(connection, "mystery")
    with pytest.raises(IntegrityError):
        _insert_origin(connection, "")


def test_no_network_or_embedding_client_call_during_migration(
    executor_and_alias, monkeypatch
):
    executor, connection, alias = executor_and_alias
    apps0009 = _migrate_to(executor, TARGET_0009)
    _seed_tag(apps0009, alias)

    def _fail(what):
        def raiser(*args, **kwargs):
            raise AssertionError(f"{what} must not run during migration 0010")

        return raiser

    monkeypatch.setattr("httpx.Client", _fail("httpx.Client"))
    monkeypatch.setattr("httpx.get", _fail("httpx.get"))
    monkeypatch.setattr("httpx.post", _fail("httpx.post"))
    monkeypatch.setattr("subprocess.run", _fail("subprocess.run"))
    monkeypatch.setattr("subprocess.Popen", _fail("subprocess.Popen"))
    monkeypatch.setattr(
        "workflow.services.embedding_client.embed_texts", _fail("embed_texts")
    )

    _migrate_to(executor, TARGET_0010)
    assert "definition_origin" in _table_ddl(connection, "workflow_tag")


# ---------------------------------------------------------------------------
# Reverse: drops ONLY the new column + constraint, preserves everything
# ---------------------------------------------------------------------------


def test_reverse_removes_only_origin_column_and_preserves_data(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0009 = _migrate_to(executor, TARGET_0009)
    _seed_tag(apps0009, alias)
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM workflow_tag")
        tags_before = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM workflow_recording")
        recordings_before = cursor.fetchone()[0]

    _migrate_to(executor, TARGET_0010)
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO workflow_tag (name, name_key, description, is_configured,"
            " definition_origin, created_at, updated_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)",
            ["Custom", "custom", "", 1, "custom",
             "2026-01-01 00:00:00", "2026-01-01 00:00:00"],
        )

    executor.migrate([TARGET_0009])
    executor.loader.build_graph()

    ddl = _table_ddl(connection, "workflow_tag")
    assert "definition_origin" not in ddl
    assert "chk_tag_definition_origin_allowlist" not in ddl
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM workflow_tag")
        assert cursor.fetchone()[0] == tags_before + 1  # rows preserved
        cursor.execute("SELECT count(*) FROM workflow_recording")
        assert cursor.fetchone()[0] == recordings_before
        cursor.execute(
            "SELECT count(*) FROM django_migrations"
            " WHERE app='workflow' AND name='0010_tag_definition_origin'"
        )
        assert cursor.fetchone()[0] == 0
    # All pre-existing tables survive.
    assert "workflow_search_document" in _tables(connection)
    assert "workflow_embedding_generation" in _tables(connection)