"""Genuine MigrationExecutor tests for migration 0012 (Step 6.2
section-scoped tag assignments).

Every test migrates an ISOLATED SQLite database (dedicated connection
alias on a tmp_path file) for real: 0011 -> 0012 forward (seeding
existing recording-scoped TagAssignment rows through the historical 0011
models), the table/column/constraint shape, DB-level enforcement of the
conditional uniqueness, and 0012 -> 0011 reverse.

The reverse coverage includes the required hard case: section-only
``TagAssignment`` rows (created through the 0012 schema) are
deterministically DELETED before the old global ``(recording, tag)``
unique is restored, so reversing succeeds with 6.2 data while the
recording-scoped assignments are preserved untouched (PK/FKs intact).

Applying the migration never contacts the network or the embedding
client (raising guards).
"""

from __future__ import annotations

import copy as _copy
import importlib

import pytest
from django.db import IntegrityError, connections
from django.db.migrations.executor import MigrationExecutor

TARGET_0011 = ("workflow", "0011_segmentedversion_remove_section_uniq_section_ordinal_and_more")
TARGET_0012 = ("workflow", "0012_remove_tagassignment_uniq_tag_assignment_and_more")

ALIAS = "mig0012"

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
T1 = "2026-01-01 00:00:01"


def _aware(value: str):
    return _dj_tz.make_aware(_datetime.fromisoformat(value), _dt_timezone.utc)


T0_AWARE = _aware(T0)
T1_AWARE = _aware(T1)


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
    db_path = str(tmp_path / "tag-assignment-migration.sqlite3")
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


def _index_names(connection, table):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=%s", [table]
        )
        return {row[0] for row in cursor.fetchall()}


def _seed_recording_and_tag(apps, alias, *, sha, tag_name="Family"):
    """Seed canonical 0011-state data: Recording + Tag + one
    recording-scoped TagAssignment."""
    Recording = apps.get_model("workflow", "Recording")
    Tag = apps.get_model("workflow", "Tag")
    TagAssignment = apps.get_model("workflow", "TagAssignment")
    recording = Recording.objects.using(alias).create(
        sha256=sha, processing_status="transcribed"
    )
    tag = Tag.objects.using(alias).create(
        name=tag_name, name_key=tag_name.lower(), description="d",
        is_configured=True, definition_origin="config",
    )
    assignment = TagAssignment.objects.using(alias).create(
        recording=recording, tag=tag, origin="manual", is_active=True,
    )
    return recording, tag, assignment


def _seed_section(apps, alias, recording, transcript, *, ordinal=0, title="Full"):
    Section = apps.get_model("workflow", "Section")
    return Section.objects.using(alias).create(
        transcript=transcript, ordinal=ordinal, title=title
    )


def _insert_topic_section_raw(connection, *, transcript_id, version_id, ordinal,
                              title, start, end):
    """Insert a topic Section row in ONE statement (the 0011 ORM cannot
    create a topic-shaped row in two steps because the shape CHECK
    rejects an ordinal>=1 row without a version at CREATE time)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO workflow_section (transcript_id, ordinal, title,"
            " segmented_version_id, start_segment_ordinal,"
            " end_segment_ordinal_exclusive, start_ms, end_ms, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [transcript_id, ordinal, title, version_id, start, end,
             None, None, T0],
        )
        return cursor.lastrowid


def _seed_transcript(apps, alias, recording, *, active=True):
    ProcessingAttempt = apps.get_model("workflow", "ProcessingAttempt")
    Transcript = apps.get_model("workflow", "Transcript")
    attempt = ProcessingAttempt.objects.using(alias).create(
        recording=recording, stage="transcription", ordinal=1,
        outcome="success", finished_at=T0_AWARE,
    )
    transcript = Transcript.objects.using(alias).create(
        recording=recording, attempt=attempt, is_active=active,
        activated_at=T0_AWARE if active else None,
    )
    return transcript


def _insert_section_assignment_raw(connection, *, recording_id, tag_id, section_id,
                                   origin="suggested", is_active=1, deactivated_by=""):
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO workflow_tagassignment (recording_id, tag_id, section_id,"
            " origin, source_summary_id, is_active, deactivated_by, deactivated_at,"
            " created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [recording_id, tag_id, section_id, origin, None, int(is_active),
             deactivated_by, None, T0],
        )
        return cursor.lastrowid


# ---------------------------------------------------------------------------
# Operations shape
# ---------------------------------------------------------------------------


def test_migration_operations_shape_and_dependencies():
    mig = importlib.import_module(
        "workflow.migrations.0012_remove_tagassignment_uniq_tag_assignment_and_more"
    )
    from django.db import migrations

    assert mig.Migration.dependencies == [
        ("workflow", "0011_segmentedversion_remove_section_uniq_section_ordinal_and_more")
    ]
    ops = mig.Migration.operations
    assert type(ops[-1]) is migrations.RunPython
    # Forward is a schema-only no-op; the reverse deletes section rows.
    assert ops[-1].code is migrations.RunPython.noop
    assert callable(ops[-1].reverse_code)


# ---------------------------------------------------------------------------
# Forward: preserves recording rows, adds column + conditional uniques
# ---------------------------------------------------------------------------


def test_forward_preserves_recording_rows_and_adds_section_column(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0011 = _migrate_to(executor, TARGET_0011)
    recording, tag, assignment = _seed_recording_and_tag(apps0011, alias, sha="f" * 64)

    _migrate_to(executor, TARGET_0012)

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT recording_id, tag_id, section_id, is_active FROM workflow_tagassignment"
        )
        row = cursor.fetchone()
    # Existing recording-scoped rows keep section NULL.
    assert row == (recording.pk, tag.pk, None, 1)
    assert assignment.pk is not None

    with connection.cursor() as cursor:
        cursor.execute("PRAGMA table_info(workflow_tagassignment)")
        columns = {row[1]: (row[2].lower(), row[3]) for row in cursor.fetchall()}
    assert "section_id" in columns
    assert columns["section_id"][1] == 0  # nullable
    assert columns["recording_id"][1] == 1  # still NOT NULL (required parent)


def test_forward_schema_constraints(executor_and_alias):
    executor, connection, alias = executor_and_alias
    _migrate_to(executor, TARGET_0011)
    _migrate_to(executor, TARGET_0012)

    indexes = _index_names(connection, "workflow_tagassignment")
    assert "uniq_tag_assignment_recording" in indexes
    assert "uniq_tag_assignment_section" in indexes
    assert "uniq_tag_assignment" not in indexes
    assert "uniq_active_tag_assignment" not in indexes
    ddl = _table_ddl(connection, "workflow_tagassignment")
    assert "chk_tagassignment_deactivation_state" in ddl
    assert "section_id" in ddl


# ---------------------------------------------------------------------------
# DB-level enforcement on the MIGRATED schema (raw SQL)
# ---------------------------------------------------------------------------


def _seed_0012_basics(executor, alias):
    """Seed a 0011-state recording + tag + a fixed section + a topic
    version/section, then migrate to 0012 and return the 0012 apps +
    (recording, tag, topic_section_id)."""
    connection = connections[alias]
    apps0011 = _migrate_to(executor, TARGET_0011)
    recording, tag, _assignment = _seed_recording_and_tag(apps0011, alias, sha="e" * 64)
    transcript = _seed_transcript(apps0011, alias, recording)
    _seed_section(apps0011, alias, recording, transcript, ordinal=0, title="Full")
    # Topic layout (0011 models).
    SegmentedVersion = apps0011.get_model("workflow", "SegmentedVersion")
    version = SegmentedVersion.objects.using(alias).create(
        transcript=transcript, revision=1, start_segment_ordinal=0,
        end_segment_ordinal_exclusive=3, is_active=True, activated_at=T1_AWARE,
    )
    topic_pk = _insert_topic_section_raw(
        connection, transcript_id=transcript.pk, version_id=version.pk,
        ordinal=1, title="Topic A", start=0, end=3,
    )
    apps0012 = _migrate_to(executor, TARGET_0012)
    return apps0012, recording, tag, topic_pk


def test_conditional_uniqueness_enforced_on_migrated_schema(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0012, recording, tag, topic_pk = _seed_0012_basics(executor, alias)

    # Recording scope: a second (recording, tag) row with section NULL is
    # rejected even when inactive.
    with pytest.raises(IntegrityError):
        _insert_section_assignment_raw(
            connection, recording_id=recording.pk, tag_id=tag.pk, section_id=None,
            origin="suggested", is_active=0, deactivated_by="model",
        )

    # Section scope: a second (section, tag) row is rejected.
    _insert_section_assignment_raw(
        connection, recording_id=recording.pk, tag_id=tag.pk, section_id=topic_pk
    )
    with pytest.raises(IntegrityError):
        _insert_section_assignment_raw(
            connection, recording_id=recording.pk, tag_id=tag.pk, section_id=topic_pk,
            origin="confirmed",
        )

    # A DIFFERENT section may hold the same tag (independent scope).
    Section = apps0012.get_model("workflow", "Section")
    other = Section.objects.using(alias).create(
        transcript_id=Section.objects.using(alias).get(pk=topic_pk).transcript_id,
        ordinal=2, title="Topic B",
        segmented_version_id=Section.objects.using(alias).get(pk=topic_pk).segmented_version_id,
        start_segment_ordinal=1, end_segment_ordinal_exclusive=3,
    )
    _insert_section_assignment_raw(
        connection, recording_id=recording.pk, tag_id=tag.pk, section_id=other.pk,
        origin="manual",
    )


def test_no_network_or_embedding_client_call_during_migration(executor_and_alias, monkeypatch):
    executor, connection, alias = executor_and_alias
    apps0011 = _migrate_to(executor, TARGET_0011)
    recording, tag, _assignment = _seed_recording_and_tag(apps0011, alias, sha="d" * 64)

    def _fail(what):
        def raiser(*args, **kwargs):
            raise AssertionError(f"{what} must not run during migration 0012")

        return raiser

    monkeypatch.setattr("httpx.Client", _fail("httpx.Client"))
    monkeypatch.setattr("httpx.get", _fail("httpx.get"))
    monkeypatch.setattr("httpx.post", _fail("httpx.post"))
    monkeypatch.setattr("subprocess.run", _fail("subprocess.run"))
    monkeypatch.setattr("subprocess.Popen", _fail("subprocess.Popen"))
    monkeypatch.setattr(
        "workflow.services.embedding_client.embed_texts", _fail("embed_texts")
    )

    _migrate_to(executor, TARGET_0012)
    assert "section_id" in _table_ddl(connection, "workflow_tagassignment")
    executor.migrate([TARGET_0011])
    executor.loader.build_graph()
    assert "section_id" not in _table_ddl(connection, "workflow_tagassignment")


# ---------------------------------------------------------------------------
# Reverse: discards section-only rows, preserves recording assignments
# ---------------------------------------------------------------------------


def test_reverse_with_section_rows_discards_them_and_preserves_recording_rows(
    executor_and_alias,
):
    executor, connection, alias = executor_and_alias
    apps0012, recording, tag, topic_pk = _seed_0012_basics(executor, alias)
    # Also create a second recording-scoped assignment and a second tag to
    # prove preserved rows are untouched.
    Tag = apps0012.get_model("workflow", "Tag")
    TagAssignment = apps0012.get_model("workflow", "TagAssignment")
    tag2 = Tag.objects.using(alias).create(
        name="Academic", name_key="academic", description="d",
        is_configured=True, definition_origin="config",
    )
    TagAssignment.objects.using(alias).create(
        recording_id=recording.pk, tag=tag2, origin="suggested", is_active=True,
    )
    # One section-scoped assignment.
    _insert_section_assignment_raw(
        connection, recording_id=recording.pk, tag_id=tag.pk, section_id=topic_pk,
        origin="suggested",
    )

    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM workflow_tagassignment")
        assert cursor.fetchone()[0] == 3  # 2 recording + 1 section

    executor.migrate([TARGET_0011])
    executor.loader.build_graph()

    ddl = _table_ddl(connection, "workflow_tagassignment")
    assert "section_id" not in ddl
    assert "uniq_tag_assignment_recording" not in ddl
    assert "uniq_tag_assignment_section" not in ddl
    # The old full (recording, tag) unique is an inline UNIQUE
    # (SQLite autoindex); the partial active unique is a named index.
    assert 'UNIQUE ("recording_id", "tag_id")' in ddl
    indexes = _index_names(connection, "workflow_tagassignment")
    assert "uniq_active_tag_assignment" in indexes

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tag_id, origin, is_active FROM workflow_tagassignment"
            " ORDER BY tag_id"
        )
        rows = cursor.fetchall()
    # Section-scoped row deleted; both recording rows preserved.
    assert [(r[0], r[1], r[2]) for r in rows] == [
        (tag.pk, "manual", 1),
        (tag2.pk, "suggested", 1),
    ]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM django_migrations"
            " WHERE app='workflow' AND name='0012_remove_tagassignment_uniq_tag_assignment_and_more'"
        )
        assert cursor.fetchone()[0] == 0


def test_reverse_without_section_rows_is_clean(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0011 = _migrate_to(executor, TARGET_0011)
    recording, tag, assignment = _seed_recording_and_tag(apps0011, alias, sha="c" * 64)

    _migrate_to(executor, TARGET_0012)
    executor.migrate([TARGET_0011])
    executor.loader.build_graph()

    assert "section_id" not in _table_ddl(connection, "workflow_tagassignment")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT recording_id, tag_id, is_active FROM workflow_tagassignment"
        )
        assert cursor.fetchone() == (recording.pk, tag.pk, 1)
    assert assignment.pk is not None