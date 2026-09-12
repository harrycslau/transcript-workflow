"""Genuine MigrationExecutor tests for migration 0013 (Step 6.2a
temporary split titles).

Every test migrates an ISOLATED SQLite database (dedicated connection
alias on a tmp_path file) for real: 0012 -> 0013 forward (existing topic
Sections migrate with ``title_is_temporary`` = False — pre-0013 titles
are custom/user-owned by construction), the column shape, and 0013 ->
0012 reverse (additive column dropped with zero data loss).

Applying the migration never contacts the network or the embedding
client (raising guards).
"""

from __future__ import annotations

import copy as _copy
import importlib

import pytest
from django.db import connections
from django.db.migrations.executor import MigrationExecutor

TARGET_0012 = ("workflow", "0012_remove_tagassignment_uniq_tag_assignment_and_more")
TARGET_0013 = ("workflow", "0013_section_title_is_temporary")

ALIAS = "mig0013"

connections.databases.setdefault(
    ALIAS,
    {
        **_copy.deepcopy(connections.databases["default"]),
        "NAME": ":memory:",
        "TEST": {"NAME": ":memory:", "MIRROR": None, "MIGRATE": True},
    },
)

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", ALIAS])

from datetime import datetime as _datetime
from datetime import timezone as _dt_timezone

from django.utils import timezone as _dj_tz

T0 = "2026-01-01 00:00:00"


def _aware(value: str):
    return _dj_tz.make_aware(_datetime.fromisoformat(value), _dt_timezone.utc)


T0_AWARE = _aware(T0)


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
    db_path = str(tmp_path / "section-title-migration.sqlite3")
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


def _seed_topic_section(apps, alias, *, sha):
    """Seed a 0012-state recording/transcript/layout with one topic
    Section (custom title) plus the fixed ordinal-0 row."""
    Recording = apps.get_model("workflow", "Recording")
    ProcessingAttempt = apps.get_model("workflow", "ProcessingAttempt")
    Transcript = apps.get_model("workflow", "Transcript")
    Section = apps.get_model("workflow", "Section")
    SegmentedVersion = apps.get_model("workflow", "SegmentedVersion")
    recording = Recording.objects.using(alias).create(
        sha256=sha, processing_status="transcribed", summary_status="current"
    )
    attempt = ProcessingAttempt.objects.using(alias).create(
        recording=recording, stage="transcription", ordinal=1,
        outcome="success", finished_at=T0_AWARE,
    )
    transcript = Transcript.objects.using(alias).create(
        recording=recording, attempt=attempt, is_active=True, activated_at=T0_AWARE,
    )
    Section.objects.using(alias).create(
        transcript=transcript, ordinal=0, title="Full recording",
    )
    version = SegmentedVersion.objects.using(alias).create(
        transcript=transcript, revision=1, start_segment_ordinal=0,
        end_segment_ordinal_exclusive=2, is_active=True, activated_at=T0_AWARE,
    )
    Section.objects.using(alias).create(
        transcript=transcript, segmented_version=version, ordinal=1,
        title="Existing custom title",
        start_segment_ordinal=0, end_segment_ordinal_exclusive=2,
    )
    return recording, transcript, version


def test_migration_operations_shape_and_dependencies():
    mig = importlib.import_module(
        "workflow.migrations.0013_section_title_is_temporary"
    )
    from django.db import migrations

    assert mig.Migration.dependencies == [
        ("workflow", "0012_remove_tagassignment_uniq_tag_assignment_and_more")
    ]
    ops = mig.Migration.operations
    assert len(ops) == 1
    assert type(ops[0]) is migrations.AddField
    assert ops[0].name == "title_is_temporary"
    assert ops[0].field.default is False
    assert ops[0].field.get_internal_type() == "BooleanField"


def test_forward_adds_column_default_false_and_preserves_rows(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0012 = _migrate_to(executor, TARGET_0012)
    recording, transcript, version = _seed_topic_section(
        apps0012, alias, sha="a" * 64
    )

    _migrate_to(executor, TARGET_0013)

    with connection.cursor() as cursor:
        cursor.execute("PRAGMA table_info(workflow_section)")
        columns = {row[1]: (row[2].lower(), row[3]) for row in cursor.fetchall()}
    assert "title_is_temporary" in columns
    # Boolean stored as integer, NOT NULL (new rows get False via the
    # model's Python-side default; the migration-created column keeps the
    # existing rows' value — see the row check below).
    assert columns["title_is_temporary"][0] == "bool"
    assert columns["title_is_temporary"][1] == 1  # notnull

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT title, title_is_temporary FROM workflow_section ORDER BY ordinal"
        )
        rows = cursor.fetchall()
    # Every existing row (fixed + topic) migrated as CUSTOM (False).
    assert rows == [("Full recording", 0), ("Existing custom title", 0)]
    assert recording is not None and transcript is not None and version is not None


def test_reverse_drops_column_with_zero_data_loss(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0012 = _migrate_to(executor, TARGET_0012)
    recording, transcript, version = _seed_topic_section(
        apps0012, alias, sha="b" * 64
    )

    _migrate_to(executor, TARGET_0013)
    # Flip one topic title to temporary through the 0013 app registry.
    apps0013 = executor.loader.project_state([TARGET_0013]).apps
    Section0013 = apps0013.get_model("workflow", "Section")
    topic = Section0013.objects.using(alias).get(ordinal=1, segmented_version_id__isnull=False)
    topic.title = "Segment 1 of 202601010000"
    topic.title_is_temporary = True
    topic.save(using=alias, update_fields=["title", "title_is_temporary"])

    executor.migrate([TARGET_0012])
    executor.loader.build_graph()

    section_ddl = _table_ddl(connection, "workflow_section")
    assert "title_is_temporary" not in section_ddl
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT title FROM workflow_section ORDER BY ordinal"
        )
        assert cursor.fetchall() == [("Full recording",), ("Segment 1 of 202601010000",)]
    assert recording is not None and transcript is not None and version is not None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM django_migrations"
            " WHERE app='workflow' AND name='0013_section_title_is_temporary'"
        )
        assert cursor.fetchone()[0] == 0


def test_no_network_or_embedding_client_call_during_migration(executor_and_alias, monkeypatch):
    executor, connection, alias = executor_and_alias
    apps0012 = _migrate_to(executor, TARGET_0012)
    _seed_topic_section(apps0012, alias, sha="d" * 64)

    def _fail(what):
        def raiser(*args, **kwargs):
            raise AssertionError(f"{what} must not run during migration 0013")

        return raiser

    monkeypatch.setattr("httpx.Client", _fail("httpx.Client"))
    monkeypatch.setattr("httpx.get", _fail("httpx.get"))
    monkeypatch.setattr("httpx.post", _fail("httpx.post"))
    monkeypatch.setattr("subprocess.run", _fail("subprocess.run"))
    monkeypatch.setattr("subprocess.Popen", _fail("subprocess.Popen"))
    monkeypatch.setattr(
        "workflow.services.embedding_client.embed_texts", _fail("embed_texts")
    )

    _migrate_to(executor, TARGET_0013)
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA table_info(workflow_section)")
        assert "title_is_temporary" in {row[1] for row in cursor.fetchall()}
    executor.migrate([TARGET_0012])
    executor.loader.build_graph()
    section_ddl = _table_ddl(connection, "workflow_section")
    assert "title_is_temporary" not in section_ddl