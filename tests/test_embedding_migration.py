"""Genuine MigrationExecutor tests for migration 0009 (embedding storage).

Every test migrates an ISOLATED SQLite database (dedicated connection
alias on a tmp_path file) for real: 0008 -> 0009 forward, seeding data
through the historical models, and 0009 -> 0008 reverse. Proves the
migration is SCHEMA-ONLY (only two CreateModel operations; no rows
created even with existing SearchDocuments/source data), the tables/
columns/FK CASCADE shape, DB-level constraint enforcement of the exact
lifecycle/chronology/unique/dimension/blank-field rules, that reverse
removes ONLY the embedding tables while preserving SearchDocument and
all canonical source data, and that applying it never contacts the
network or calls the embedding client (raising guards).
"""

from __future__ import annotations

import copy as _copy
import importlib

import django.db.models.deletion as dj_deletion
import pytest
from django.db import IntegrityError, connections
from django.db.migrations.executor import MigrationExecutor

TARGET_0008 = ("workflow", "0008_search_index")
TARGET_0009 = ("workflow", "0009_embedding_foundation")

ALIAS = "mig0009"

connections.databases.setdefault(
    ALIAS,
    {
        **_copy.deepcopy(connections.databases["default"]),
        "NAME": ":memory:",
        "TEST": {"NAME": ":memory:", "MIRROR": None, "MIGRATE": True},
    },
)

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", ALIAS])

# Deterministic datetime literals matching Django's SQLite text format
# (space separator, optional microseconds). Chronology comparisons among
# raw values are lexicographic over this exact layout.
T0 = "2026-01-01 00:00:00"
T1 = "2026-01-01 00:00:01"
T2 = "2026-01-01 00:00:02"

GEN_COLUMNS = (
    "model, dimensions, embedding_version, source_index_version, state,"
    " created_at, completed_at, activated_at, superseded_at, failed_at"
)
DOC_COLUMNS = (
    "generation_id, document_key, source_content_hash, vector_blob, embedded_at"
)


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
    db_path = str(tmp_path / "embedding-migration.sqlite3")
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


def _embedding_tables(connection):
    return {t for t in _tables(connection) if t.startswith("workflow_embedding_")}


def _insert_generation(connection, *, state="building", dimensions=4, **timestamps):
    row = {
        "model": "local-embed-m",
        "dimensions": dimensions,
        "embedding_version": "1",
        "source_index_version": "1",
        "state": state,
        "created_at": T0,
        "completed_at": None,
        "activated_at": None,
        "superseded_at": None,
        "failed_at": None,
    }
    row.update(timestamps)
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO workflow_embedding_generation ({GEN_COLUMNS})"
            f" VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                row["model"], row["dimensions"], row["embedding_version"],
                row["source_index_version"], row["state"], row["created_at"],
                row["completed_at"], row["activated_at"], row["superseded_at"],
                row["failed_at"],
            ],
        )
        return cursor.lastrowid


def _insert_document(connection, generation_id, *, document_key="segment:r:0",
                     source_content_hash=None, vector_blob=b"\x00\x00\x80?"):
    if source_content_hash is None:
        source_content_hash = "a" * 64
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO workflow_embedding_document ({DOC_COLUMNS})"
            " VALUES (%s,%s,%s,%s,%s)",
            [generation_id, document_key, source_content_hash, vector_blob, T0],
        )
        return cursor.lastrowid


def _seed_source_and_search(apps, alias):
    """Seed canonical source rows + SearchDocument registry rows through
    the historical 0008 models so migration 0009 can prove it leaves them
    untouched."""
    Recording = apps.get_model("workflow", "Recording")
    SearchDocument = apps.get_model("workflow", "SearchDocument")
    recording = Recording.objects.using(alias).create(
        sha256="1" * 64, processing_status="transcribed", summary_status="current"
    )
    SearchDocument.objects.using(alias).create(
        document_key=f"recording:{recording.pk}", doc_type="recording",
        recording=recording, title_text="T", body_text="B", aux_text="",
        content_hash="2" * 64, index_version="1",
    )
    return recording


def _table_ddl(connection, table):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=%s", [table]
        )
        row = cursor.fetchone()
        return row[0] if row else None


def _index_names(connection, table):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=%s", [table]
        )
        return {row[0] for row in cursor.fetchall()}


# ---------------------------------------------------------------------------
# Schema-only, no backfill, no embedding-client/network use
# ---------------------------------------------------------------------------


def test_migration_operations_are_schema_only_create_models():
    mig = importlib.import_module("workflow.migrations.0009_embedding_foundation")
    from django.db import migrations

    assert mig.Migration.dependencies == [("workflow", "0008_search_index")]
    for op in mig.Migration.operations:
        assert type(op) is migrations.CreateModel, type(op)
        assert op.name in ("EmbeddingGeneration", "EmbeddingDocument")


def test_forward_creates_tables_creates_no_rows_and_preserves_existing_data(
    executor_and_alias,
):
    executor, connection, alias = executor_and_alias
    apps0008 = _migrate_to(executor, TARGET_0008)
    recording = _seed_source_and_search(apps0008, alias)

    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM workflow_search_document")
        search_before = cursor.fetchone()[0]
        cursor.execute(
            "SELECT document_key FROM workflow_search_document ORDER BY document_key"
        )
        keys_before = [row[0] for row in cursor.fetchall()]
        cursor.execute("SELECT count(*) FROM workflow_recording")
        recordings_before = cursor.fetchone()[0]

    assert _embedding_tables(connection) == set()

    _migrate_to(executor, TARGET_0009)

    tables = _tables(connection)
    assert "workflow_embedding_generation" in tables
    assert "workflow_embedding_document" in tables
    # Existing SearchDocument / source data untouched ...
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM workflow_search_document")
        assert cursor.fetchone()[0] == search_before
        cursor.execute(
            "SELECT document_key FROM workflow_search_document ORDER BY document_key"
        )
        assert [row[0] for row in cursor.fetchall()] == keys_before
        cursor.execute("SELECT count(*) FROM workflow_recording")
        assert cursor.fetchone()[0] == recordings_before
        # ... and the migration created NO embedding rows (no backfill).
        cursor.execute("SELECT count(*) FROM workflow_embedding_generation")
        assert cursor.fetchone()[0] == 0
        cursor.execute("SELECT count(*) FROM workflow_embedding_document")
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT count(*) FROM django_migrations"
            " WHERE app='workflow' AND name='0009_embedding_foundation'"
        )
        assert cursor.fetchone()[0] == 1
    assert recording is not None


def test_no_network_or_embedding_client_call_during_migration(executor_and_alias, monkeypatch):
    executor, connection, alias = executor_and_alias
    apps0008 = _migrate_to(executor, TARGET_0008)
    _seed_source_and_search(apps0008, alias)

    def _fail(what):
        def raiser(*args, **kwargs):
            raise AssertionError(f"{what} must not run during migration 0009")

        return raiser

    monkeypatch.setattr("httpx.Client", _fail("httpx.Client"))
    monkeypatch.setattr("httpx.get", _fail("httpx.get"))
    monkeypatch.setattr("httpx.post", _fail("httpx.post"))
    monkeypatch.setattr("subprocess.run", _fail("subprocess.run"))
    monkeypatch.setattr("subprocess.Popen", _fail("subprocess.Popen"))
    monkeypatch.setattr(
        "workflow.services.embedding_client.embed_texts", _fail("embed_texts")
    )

    _migrate_to(executor, TARGET_0009)
    assert "workflow_embedding_generation" in _tables(connection)
    # Reverse too.
    executor.migrate([TARGET_0008])
    executor.loader.build_graph()
    assert _embedding_tables(connection) == set()


# ---------------------------------------------------------------------------
# Table shape: columns, FK CASCADE, constraints
# ---------------------------------------------------------------------------


def test_forward_schema_columns_constraints_and_fk_cascade(executor_and_alias):
    executor, connection, alias = executor_and_alias
    _migrate_to(executor, TARGET_0008)
    apps = _migrate_to(executor, TARGET_0009)

    with connection.cursor() as cursor:
        cursor.execute("PRAGMA table_info(workflow_embedding_generation)")
        gen_columns = {row[1]: (row[2].lower(), row[3]) for row in cursor.fetchall()}
        cursor.execute("PRAGMA table_info(workflow_embedding_document)")
        doc_columns = {row[1]: (row[2].lower(), row[3]) for row in cursor.fetchall()}

    # Generation columns + nullability/type. Django's SQLite BigAutoField
    # ids are declared INTEGER PRIMARY KEY AUTOINCREMENT, while the FK
    # column referencing one is declared bigint.
    assert gen_columns["id"] == ("integer", 1)
    assert gen_columns["model"] == ("text", 1)
    assert gen_columns["dimensions"] == ("integer unsigned", 1)
    assert gen_columns["embedding_version"] == ("varchar(16)", 1)
    assert gen_columns["source_index_version"] == ("varchar(16)", 1)
    assert gen_columns["state"] == ("varchar(16)", 1)
    assert gen_columns["created_at"] == ("datetime", 1)
    for name in ("completed_at", "activated_at", "superseded_at", "failed_at"):
        assert gen_columns[name] == ("datetime", 0)  # nullable
    # Document columns; FK column is generation_id; no dimensions column.
    assert doc_columns["id"] == ("integer", 1)
    assert doc_columns["generation_id"] == ("bigint", 1)
    assert doc_columns["document_key"] == ("text", 1)
    assert doc_columns["source_content_hash"] == ("varchar(64)", 1)
    assert doc_columns["vector_blob"] == ("blob", 1)
    assert "dimensions" not in doc_columns

    # Constraint + partial-index names present.
    gen_ddl = _table_ddl(connection, "workflow_embedding_generation")
    for name in (
        "chk_embedding_generation_dimensions_bounds",
        "chk_embedding_generation_identity_nonempty",
        "chk_embedding_generation_lifecycle_shape",
        "chk_embedding_generation_completed_before_activated",
        "chk_embedding_generation_activated_before_superseded",
    ):
        assert name in gen_ddl, name
    doc_ddl = _table_ddl(connection, "workflow_embedding_document")
    assert "chk_embedding_document_fields_nonempty" in doc_ddl
    # The (generation, document_key) uniqueness is an inline UNIQUE
    # constraint (sqlite_autoindex), not a separate named index.
    assert 'UNIQUE ("generation_id", "document_key")' in doc_ddl
    gen_indexes = _index_names(connection, "workflow_embedding_generation")
    assert "uniq_active_embedding_generation" in gen_indexes

    # The FK REFERENCES clause exists and is DB-enforced; Django performs
    # the CASCADE at the ORM level (SQLite DDL carries no ON DELETE
    # clause — Django's collector deletes dependent rows first).
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA foreign_key_list(workflow_embedding_document)")
        fks = cursor.fetchall()
    assert any(
        row[2] == "workflow_embedding_generation" and row[3] == "generation_id"
        for row in fks
    ), fks

    # Historical apps expose the new models after the migration.
    EmbeddingGeneration = apps.get_model("workflow", "EmbeddingGeneration")
    EmbeddingDocument = apps.get_model("workflow", "EmbeddingDocument")
    assert EmbeddingGeneration._meta.db_table == "workflow_embedding_generation"
    assert EmbeddingDocument._meta.db_table == "workflow_embedding_document"
    assert (
        EmbeddingDocument._meta.get_field("generation").remote_field.on_delete
        is dj_deletion.CASCADE
    )

    # ORM-level CASCADE: deleting the generation deletes its documents.
    generation_id = _insert_generation(connection, state="building")
    _insert_document(connection, generation_id)
    _insert_document(connection, generation_id, document_key="segment:r:1")
    gen_row = EmbeddingGeneration.objects.using(alias).get(pk=generation_id)
    gen_row.delete()
    assert EmbeddingDocument.objects.using(alias).count() == 0


# ---------------------------------------------------------------------------
# DB-level constraint enforcement on the MIGRATED schema (raw SQL)
# ---------------------------------------------------------------------------


def test_generation_lifecycle_and_chronology_enforced(executor_and_alias):
    executor, connection, alias = executor_and_alias
    _migrate_to(executor, TARGET_0008)
    _migrate_to(executor, TARGET_0009)

    # Accepted canonical shapes.
    _insert_generation(connection, state="building")
    active_id = _insert_generation(
        connection, state="active", completed_at=T0, activated_at=T0
    )
    _insert_generation(
        connection, state="superseded", completed_at=T0, activated_at=T1,
        superseded_at=T2,
    )
    _insert_generation(connection, state="failed", failed_at=T0)

    def invalid(**kwargs):
        with pytest.raises(IntegrityError):
            _insert_generation(connection, **kwargs)

    # Unknown state rejected (shape check doubles as the allowlist).
    invalid(state="mystery")
    invalid(state="mystery", failed_at=T0)
    # Shape violations.
    invalid(state="building", failed_at=T0)
    invalid(state="building", activated_at=T0)
    invalid(state="active")  # missing completed + activated
    invalid(state="active", activated_at=T0)
    invalid(state="active", completed_at=T0)
    invalid(
        state="active", completed_at=T0, activated_at=T0, superseded_at=T0
    )
    invalid(
        state="active", completed_at=T0, activated_at=T0, failed_at=T0
    )
    invalid(state="superseded", completed_at=T0, activated_at=T0)  # no superseded_at
    invalid(
        state="superseded", completed_at=T0, activated_at=T1, superseded_at=T2,
        failed_at=T0,
    )
    invalid(state="failed")  # no failed_at
    invalid(state="failed", failed_at=T0, completed_at=T0)
    # Chronology violations.
    invalid(state="active", completed_at=T1, activated_at=T0)  # completed after activated
    invalid(state="superseded", completed_at=T0, activated_at=T2, superseded_at=T1)

    # Second active generation rejected (at most one active).
    with pytest.raises(IntegrityError):
        _insert_generation(connection, state="active", completed_at=T0, activated_at=T0)
    assert active_id is not None


def test_duplicate_identity_generations_allowed_but_one_active(executor_and_alias):
    executor, connection, alias = executor_and_alias
    _migrate_to(executor, TARGET_0008)
    _migrate_to(executor, TARGET_0009)

    # Two identical contract generations coexist.
    g1 = _insert_generation(connection, state="building")
    g2 = _insert_generation(connection, state="building")
    assert g1 != g2
    # Same contract may be (re)built while an active generation exists.
    active = _insert_generation(
        connection, state="active", completed_at=T0, activated_at=T0
    )
    same_contract_rebuild = _insert_generation(connection, state="building")
    assert active != same_contract_rebuild
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM workflow_embedding_generation")
        assert cursor.fetchone()[0] == 4


def test_generation_dimensions_and_identity_fields_enforced(executor_and_alias):
    executor, connection, alias = executor_and_alias
    _migrate_to(executor, TARGET_0008)
    _migrate_to(executor, TARGET_0009)

    with pytest.raises(IntegrityError):
        _insert_generation(connection, dimensions=0)
    with pytest.raises(IntegrityError):
        _insert_generation(connection, dimensions=-1)
    with pytest.raises(IntegrityError):
        _insert_generation(connection, dimensions=16385)
    _insert_generation(connection, dimensions=1)
    _insert_generation(connection, dimensions=16384)

    with pytest.raises(IntegrityError):
        _insert_generation(connection, model="")
    with pytest.raises(IntegrityError):
        _insert_generation(connection, embedding_version="")
    with pytest.raises(IntegrityError):
        _insert_generation(connection, source_index_version="")


def test_document_uniqueness_and_blank_field_enforcement(executor_and_alias):
    executor, connection, alias = executor_and_alias
    _migrate_to(executor, TARGET_0008)
    _migrate_to(executor, TARGET_0009)

    g1 = _insert_generation(connection, dimensions=1)
    g2 = _insert_generation(connection, dimensions=1)

    _insert_document(connection, g1, document_key="segment:r:0")
    # Same key in a second generation is allowed.
    _insert_document(connection, g2, document_key="segment:r:0")
    # Duplicate key within ONE generation is rejected.
    with pytest.raises(IntegrityError):
        _insert_document(connection, g1, document_key="segment:r:0")

    # Blank stored fields rejected.
    with pytest.raises(IntegrityError):
        _insert_document(connection, g1, document_key="")
    with pytest.raises(IntegrityError):
        _insert_document(
            connection, g1, source_content_hash="", vector_blob=b"\x00\x00\x80?"
        )
    with pytest.raises(IntegrityError):
        _insert_document(connection, g1, vector_blob=b"")


# ---------------------------------------------------------------------------
# Reverse migration
# ---------------------------------------------------------------------------


def test_reverse_removes_only_embedding_tables_and_preserves_data(
    executor_and_alias,
):
    executor, connection, alias = executor_and_alias
    apps0008 = _migrate_to(executor, TARGET_0008)
    _seed_source_and_search(apps0008, alias)
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM workflow_search_document")
        search_count = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM workflow_recording")
        recording_count = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM workflow_transcriptsegment")
        segment_count = cursor.fetchone()[0]

    _migrate_to(executor, TARGET_0009)
    generation_id = _insert_generation(connection, state="active",
                                       completed_at=T0, activated_at=T0)
    _insert_document(connection, generation_id, document_key="segment:r:0")
    _insert_document(connection, generation_id, document_key="segment:r:1")
    assert _embedding_tables(connection) == {
        "workflow_embedding_document",
        "workflow_embedding_generation",
    }

    executor.migrate([TARGET_0008])
    executor.loader.build_graph()

    # Only the embedding tables disappear — every pre-existing table and
    # row (SearchDocument registry included) survives.
    assert _embedding_tables(connection) == set()
    leftover = {t for t in _tables(connection) if t.startswith("workflow_embedding")}
    assert leftover == set()
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM workflow_search_document")
        assert cursor.fetchone()[0] == search_count
        cursor.execute("SELECT count(*) FROM workflow_recording")
        assert cursor.fetchone()[0] == recording_count
        cursor.execute("SELECT count(*) FROM workflow_transcriptsegment")
        assert cursor.fetchone()[0] == segment_count
        cursor.execute(
            "SELECT count(*) FROM django_migrations"
            " WHERE app='workflow' AND name='0009_embedding_foundation'"
        )
        assert cursor.fetchone()[0] == 0
