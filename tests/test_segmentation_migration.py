"""Genuine MigrationExecutor tests for migration 0011 (Step 6.1
segmented versions).

Every test migrates an ISOLATED SQLite database (dedicated connection
alias on a tmp_path file) for real: 0010 -> 0011 forward (seeding
existing ordinal-0 Sections through the historical 0010 models), the
table/column/constraint shape, DB-level enforcement of the Section
shape CHECK and conditional uniqueness, and 0011 -> 0010 reverse.

The reverse coverage includes the required hard case: topic Sections
whose ordinals DUPLICATE across multiple segmented revisions are
deterministically renumbered to unique positive values BEFORE the old
global ``(transcript, ordinal)`` unique is restored, so reversing never
fails on duplicate ordinals (PK/FKs unchanged, fixed ordinal-0 rows
untouched, layout/range metadata dropped as expected).

Applying the migration never contacts the network or the embedding
client (raising guards).
"""

from __future__ import annotations

import copy as _copy
import importlib

import pytest
from django.db import IntegrityError, connections
from django.db.migrations.executor import MigrationExecutor

TARGET_0010 = ("workflow", "0010_tag_definition_origin")
TARGET_0011 = ("workflow", "0011_segmentedversion_remove_section_uniq_section_ordinal_and_more")

ALIAS = "mig0011"

connections.databases.setdefault(
    ALIAS,
    {
        **_copy.deepcopy(connections.databases["default"]),
        "NAME": ":memory:",
        "TEST": {"NAME": ":memory:", "MIRROR": None, "MIGRATE": True},
    },
)

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", ALIAS])

# Deterministic datetime literals. Raw SQL inserts use the naive ISO text
# exactly as Django's SQLite adapter stores timezone-aware values (UTC
# without offset); ORM seeds pass timezone-aware equivalents so no
# naive-datetime RuntimeWarnings are emitted. Lexicographic comparisons
# among stored values are therefore consistent across both paths.
from datetime import datetime as _datetime
from datetime import timezone as _dt_timezone

from django.utils import timezone as _dj_tz

T0 = "2026-01-01 00:00:00"
T1 = "2026-01-01 00:00:01"
T2 = "2026-01-01 00:00:02"


def _aware(value: str):
    return _dj_tz.make_aware(_datetime.fromisoformat(value), _dt_timezone.utc)


T0_AWARE = _aware(T0)
T1_AWARE = _aware(T1)
T2_AWARE = _aware(T2)


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
    db_path = str(tmp_path / "segmentation-migration.sqlite3")
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


def _seed_transcript_and_fixed_section(apps, alias, *, sha):
    """Seed canonical 0010-state data: Recording -> attempt -> active
    Transcript -> fixed ordinal-0 whole-recording Section."""
    Recording = apps.get_model("workflow", "Recording")
    ProcessingAttempt = apps.get_model("workflow", "ProcessingAttempt")
    Transcript = apps.get_model("workflow", "Transcript")
    Section = apps.get_model("workflow", "Section")
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
    section = Section.objects.using(alias).create(
        transcript=transcript, ordinal=0, title="Full recording",
    )
    return recording, transcript, section


def _seed_topic_sections(apps, alias, transcript, version, titles):
    """Create topic Sections for ``version`` via the 0011 historical
    models: ordinal 1..N with exhaustive contiguous ranges."""
    Section = apps.get_model("workflow", "Section")
    count = len(titles)
    for i, title in enumerate(titles):
        Section.objects.using(alias).create(
            transcript=transcript,
            segmented_version=version,
            ordinal=i + 1,
            title=title,
            start_segment_ordinal=i,
            end_segment_ordinal_exclusive=i + 1,
        )


def _insert_section_raw(connection, *, transcript_id, ordinal, title,
                        segmented_version_id=None, start=None, end=None):
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO workflow_section (transcript_id, ordinal, title,"
            " segmented_version_id, start_segment_ordinal,"
            " end_segment_ordinal_exclusive, start_ms, end_ms, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [transcript_id, ordinal, title, segmented_version_id, start, end,
             None, None, T0],
        )
        return cursor.lastrowid


def _insert_version_raw(connection, *, transcript_id, revision,
                        start, end, is_active, activated_at=None,
                        superseded_at=None):
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO workflow_segmentedversion (id, revision,"
            " start_segment_ordinal, end_segment_ordinal_exclusive,"
            " is_active, activated_at, superseded_at, created_at,"
            " transcript_id)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [f"rev-{transcript_id}-{revision}",
             revision, start, end, int(is_active), activated_at, superseded_at,
             T0, transcript_id],
        )
        return cursor.lastrowid


# ---------------------------------------------------------------------------
# Operations shape
# ---------------------------------------------------------------------------


def test_migration_operations_shape_and_dependencies():
    mig = importlib.import_module(
        "workflow.migrations.0011_segmentedversion_remove_section_uniq_section_ordinal_and_more"
    )
    from django.db import migrations

    assert mig.Migration.dependencies == [("workflow", "0010_tag_definition_origin")]
    ops = mig.Migration.operations
    assert ops[0].name == "SegmentedVersion"
    assert type(ops[0]) is migrations.CreateModel
    assert type(ops[-1]) is migrations.RunPython
    # The forward code is a schema-only no-op; the reverse renumbers.
    assert ops[-1].code is migrations.RunPython.noop
    assert callable(ops[-1].reverse_code)


# ---------------------------------------------------------------------------
# Forward: preserves existing ordinal-0 rows, adds columns/constraints
# ---------------------------------------------------------------------------


def test_forward_preserves_existing_ordinal0_rows(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0010 = _migrate_to(executor, TARGET_0010)
    recording, transcript, section = _seed_transcript_and_fixed_section(
        apps0010, alias, sha="f" * 64
    )

    _migrate_to(executor, TARGET_0011)

    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM workflow_section")
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "SELECT ordinal, segmented_version_id, start_segment_ordinal,"
            " end_segment_ordinal_exclusive FROM workflow_section"
        )
        row = cursor.fetchone()
    # Existing ordinal-0 rows keep layout NULL and null canonical fields.
    assert row == (0, None, None, None)
    assert recording is not None and transcript is not None and section is not None


def test_forward_schema_columns_and_constraints(executor_and_alias):
    executor, connection, alias = executor_and_alias
    _migrate_to(executor, TARGET_0010)
    _migrate_to(executor, TARGET_0011)

    with connection.cursor() as cursor:
        cursor.execute("PRAGMA table_info(workflow_section)")
        columns = {row[1]: (row[2].lower(), row[3]) for row in cursor.fetchall()}
        cursor.execute("PRAGMA table_info(workflow_segmentedversion)")
        version_columns = {row[1]: (row[2].lower(), row[3]) for row in cursor.fetchall()}

    # Section gained the ownership + canonical range columns (nullable).
    for name in ("segmented_version_id", "start_segment_ordinal",
                 "end_segment_ordinal_exclusive"):
        assert name in columns, name
        assert columns[name][1] == 0  # nullable

    # SegmentedVersion shape.
    assert version_columns["id"][0] == "varchar(36)"
    assert version_columns["id"][1] == 1  # notnull primary key
    assert version_columns["revision"][0] == "integer unsigned"
    assert version_columns["start_segment_ordinal"][0] == "integer unsigned"
    assert version_columns["end_segment_ordinal_exclusive"][0] == "integer unsigned"
    assert version_columns["transcript_id"][0] == "bigint"
    for name in ("activated_at", "superseded_at"):
        assert version_columns[name][1] == 0  # nullable

    section_ddl = _table_ddl(connection, "workflow_section")
    assert "chk_section_shape_segmentation" in section_ddl
    assert "segmented_version_id" in section_ddl
    section_indexes = _index_names(connection, "workflow_section")
    assert "uniq_section_ordinal_fixed" in section_indexes
    assert "uniq_section_ordinal_topic" in section_indexes
    assert "uniq_section_ordinal" not in section_indexes

    version_ddl = _table_ddl(connection, "workflow_segmentedversion")
    for name in (
        "chk_segmented_version_revision_positive",
        "chk_segmented_version_range",
        "chk_segmented_version_lifecycle_shape",
        "chk_segmented_version_activated_before_superseded",
    ):
        assert name in version_ddl, name
    # The full (non-conditional) (transcript, revision) unique is an
    # inline UNIQUE (SQLite autoindex); the partial active unique is a
    # named index.
    assert 'UNIQUE ("transcript_id", "revision")' in version_ddl
    version_indexes = _index_names(connection, "workflow_segmentedversion")
    assert "uniq_active_segmented_version" in version_indexes


# ---------------------------------------------------------------------------
# DB-level enforcement on the MIGRATED schema (raw SQL)
# ---------------------------------------------------------------------------


def _seed_0011_basics(executor, alias):
    """Seed one 0010-state fixed section, then migrate to 0011 and return
    the 0011 apps + (recording, transcript, fixed_section_id).

    The transcript is re-fetched through the 0011 app registry so FK
    assignments to 0011 models use a compatible instance.
    """
    apps0010 = _migrate_to(executor, TARGET_0010)
    recording, transcript, fixed = _seed_transcript_and_fixed_section(
        apps0010, alias, sha="e" * 64
    )
    transcript_pk = transcript.pk
    fixed_pk = fixed.pk
    apps0011 = _migrate_to(executor, TARGET_0011)
    Transcript0011 = apps0011.get_model("workflow", "Transcript")
    transcript0011 = Transcript0011.objects.using(alias).get(pk=transcript_pk)
    return apps0011, recording, transcript0011, fixed_pk


def test_section_shape_check_enforced_on_migrated_schema(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0011, recording, transcript, fixed_id = _seed_0011_basics(executor, alias)

    SegmentedVersion = apps0011.get_model("workflow", "SegmentedVersion")
    version = SegmentedVersion.objects.using(alias).create(
        transcript=transcript, revision=1, start_segment_ordinal=0,
        end_segment_ordinal_exclusive=3, is_active=True, activated_at=T1_AWARE,
    )
    vid = version.pk

    # Valid topic shape (the fixed ordinal-0 row already exists from seed).
    _insert_section_raw(connection, transcript_id=transcript.pk, ordinal=1,
                        title="A", segmented_version_id=vid, start=0, end=2)

    def invalid(**kwargs):
        with pytest.raises(IntegrityError):
            _insert_section_raw(connection, transcript_id=transcript.pk, **kwargs)

    # Fixed row with canonical fields set.
    invalid(ordinal=0, title="x", start=0, end=2)
    # Fixed row with a segmented version.
    invalid(ordinal=0, title="x", segmented_version_id=vid)
    # Topic row with null canonical fields.
    invalid(ordinal=1, title="x", segmented_version_id=vid)
    # Topic row with ordinal 0.
    invalid(ordinal=0, title="x", segmented_version_id=vid, start=0, end=2)
    # Topic row with reversed/empty range.
    invalid(ordinal=2, title="x", segmented_version_id=vid, start=3, end=2)
    invalid(ordinal=2, title="x", segmented_version_id=vid, start=2, end=2)


def test_conditional_uniqueness_on_migrated_schema(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0011, recording, transcript, fixed_id = _seed_0011_basics(executor, alias)
    SegmentedVersion = apps0011.get_model("workflow", "SegmentedVersion")
    v1 = SegmentedVersion.objects.using(alias).create(
        transcript=transcript, revision=1, start_segment_ordinal=0,
        end_segment_ordinal_exclusive=3, is_active=True, activated_at=T1_AWARE,
    )
    v2 = SegmentedVersion.objects.using(alias).create(
        transcript=transcript, revision=2, start_segment_ordinal=0,
        end_segment_ordinal_exclusive=3, is_active=False,
        activated_at=T1_AWARE, superseded_at=T2_AWARE,
    )

    _insert_section_raw(connection, transcript_id=transcript.pk, ordinal=1,
                        title="A", segmented_version_id=v1.pk, start=0, end=2)
    _insert_section_raw(connection, transcript_id=transcript.pk, ordinal=2,
                        title="B", segmented_version_id=v1.pk, start=2, end=3)
    # Duplicate fixed ordinal-0 per transcript rejected (seed already
    # created the fixed ordinal-0 row).
    with pytest.raises(IntegrityError):
        _insert_section_raw(connection, transcript_id=transcript.pk, ordinal=0,
                            title="Duplicate")
    # Duplicate topic ordinal within ONE version rejected.
    with pytest.raises(IntegrityError):
        _insert_section_raw(connection, transcript_id=transcript.pk, ordinal=1,
                            title="A2", segmented_version_id=v1.pk, start=0, end=2)
    # The same topic ordinal in a SECOND revision is allowed.
    _insert_section_raw(connection, transcript_id=transcript.pk, ordinal=1,
                        title="C", segmented_version_id=v2.pk, start=0, end=2)


def test_version_lifecycle_chronology_and_range_enforced(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0011, recording, transcript, fixed_id = _seed_0011_basics(executor, alias)

    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO workflow_segmentedversion (id, revision,"
            " start_segment_ordinal, end_segment_ordinal_exclusive,"
            " is_active, activated_at, superseded_at, created_at,"
            " transcript_id)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ["ok1", 1, 0, 3, 1, T1, None, T0, transcript.pk],
        )

    def invalid(**kwargs):
        with pytest.raises(IntegrityError):
            _insert_version_raw(connection, transcript_id=transcript.pk, **kwargs)

    # Range violations.
    invalid(revision=2, start=3, end=3, is_active=True, activated_at=T1)
    invalid(revision=3, start=4, end=3, is_active=True, activated_at=T1)
    # Revision-positivity violation (chk_segmented_version_revision_positive).
    invalid(revision=0, start=0, end=3, is_active=True, activated_at=T1)
    invalid(revision=-1, start=0, end=3, is_active=True, activated_at=T1)
    # Lifecycle violations.
    invalid(revision=4, start=0, end=3, is_active=True)  # no activated_at
    invalid(revision=5, start=0, end=3, is_active=True, activated_at=T1,
            superseded_at=T2)
    invalid(revision=6, start=0, end=3, is_active=False, activated_at=T1)
    # Chronology violation.
    invalid(revision=7, start=0, end=3, is_active=False, activated_at=T2,
            superseded_at=T1)
    # Second active rejected (at most one per transcript).
    invalid(revision=8, start=0, end=3, is_active=True, activated_at=T2)


def test_no_network_or_embedding_client_call_during_migration(executor_and_alias, monkeypatch):
    executor, connection, alias = executor_and_alias
    apps0010 = _migrate_to(executor, TARGET_0010)
    _seed_transcript_and_fixed_section(apps0010, alias, sha="d" * 64)

    def _fail(what):
        def raiser(*args, **kwargs):
            raise AssertionError(f"{what} must not run during migration 0011")

        return raiser

    monkeypatch.setattr("httpx.Client", _fail("httpx.Client"))
    monkeypatch.setattr("httpx.get", _fail("httpx.get"))
    monkeypatch.setattr("httpx.post", _fail("httpx.post"))
    monkeypatch.setattr("subprocess.run", _fail("subprocess.run"))
    monkeypatch.setattr("subprocess.Popen", _fail("subprocess.Popen"))
    monkeypatch.setattr(
        "workflow.services.embedding_client.embed_texts", _fail("embed_texts")
    )

    _migrate_to(executor, TARGET_0011)
    assert "workflow_segmentedversion" in _tables(connection)
    executor.migrate([TARGET_0010])
    executor.loader.build_graph()
    assert "workflow_segmentedversion" not in _tables(connection)


# ---------------------------------------------------------------------------
# Reverse
# ---------------------------------------------------------------------------


def test_reverse_with_duplicate_topic_ordinals_across_revisions(executor_and_alias):
    """The required hard case: two revisions hold topic sections with
    the SAME ordinals. Reverse must renumber them to unique positive
    values per transcript and restore the old global unique without
    failing, keeping PK/FKs and titles, and dropping the layout/range
    metadata."""
    executor, connection, alias = executor_and_alias
    apps0011, recording, transcript, fixed_id = _seed_0011_basics(executor, alias)
    SegmentedVersion = apps0011.get_model("workflow", "SegmentedVersion")

    # Revision 1: active, two topic sections (ordinals 1, 2).
    v1 = SegmentedVersion.objects.using(alias).create(
        transcript=transcript, revision=1, start_segment_ordinal=0,
        end_segment_ordinal_exclusive=4, is_active=True, activated_at=T1_AWARE,
    )
    _seed_topic_sections(apps0011, alias, transcript, v1, ["Alpha", "Beta"])
    v1.is_active = False
    v1.superseded_at = T2_AWARE
    v1.save(update_fields=["is_active", "superseded_at"])

    # Revision 2: active, topic sections with the SAME ordinals 1, 2.
    v2 = SegmentedVersion.objects.using(alias).create(
        transcript=transcript, revision=2, start_segment_ordinal=1,
        end_segment_ordinal_exclusive=4, is_active=True, activated_at=T2_AWARE,
    )
    _seed_topic_sections(apps0011, alias, transcript, v2, ["Gamma", "Delta"])

    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM workflow_section")
        assert cursor.fetchone()[0] == 5  # fixed + 2 + 2
        cursor.execute(
            "SELECT segmented_version_id, ordinal FROM workflow_section"
            " WHERE segmented_version_id IS NOT NULL ORDER BY ordinal"
        )
        # Duplicate ordinals exist across revisions (the old global
        # unique could NOT be restored without renumbering).
        assert [row[1] for row in cursor.fetchall()] == [1, 1, 2, 2]

    executor.migrate([TARGET_0010])
    executor.loader.build_graph()

    assert "workflow_segmentedversion" not in _tables(connection)
    section_ddl = _table_ddl(connection, "workflow_section")
    assert "segmented_version_id" not in section_ddl
    assert "chk_section_shape_segmentation" not in section_ddl
    assert 'UNIQUE ("transcript_id", "ordinal")' in section_ddl

    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM workflow_section")
        assert cursor.fetchone()[0] == 5  # all rows preserved
        cursor.execute(
            "SELECT id, ordinal, title FROM workflow_section"
            " WHERE transcript_id=%s ORDER BY ordinal",
            [transcript.pk],
        )
        rows = cursor.fetchall()
    # Fixed section keeps ordinal 0; the four topic sections were
    # renumbered to unique positive values in revision-then-ordinal
    # order (Alpha/Beta first, then Gamma/Delta).
    assert [(r[1], r[2]) for r in rows] == [
        (0, "Full recording"),
        (1, "Alpha"),
        (2, "Beta"),
        (3, "Gamma"),
        (4, "Delta"),
    ]
    assert len({r[0] for r in rows}) == 5  # PKs unchanged and distinct
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM django_migrations"
            " WHERE app='workflow' AND name='0011_segmentedversion_remove_section_uniq_section_ordinal_and_more'"
        )
        assert cursor.fetchone()[0] == 0


def test_reverse_without_topic_sections_is_clean(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0010 = _migrate_to(executor, TARGET_0010)
    recording, transcript, section = _seed_transcript_and_fixed_section(
        apps0010, alias, sha="c" * 64
    )

    _migrate_to(executor, TARGET_0011)
    executor.migrate([TARGET_0010])
    executor.loader.build_graph()

    assert "workflow_segmentedversion" not in _tables(connection)
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM workflow_section")
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "SELECT ordinal, title FROM workflow_section WHERE transcript_id=%s",
            [transcript.pk],
        )
        assert cursor.fetchone() == (0, "Full recording")
    assert recording is not None and section is not None