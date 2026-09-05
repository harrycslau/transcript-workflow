"""Genuine MigrationExecutor tests for migration 0008 (search index).

Every test migrates an ISOLATED SQLite database (dedicated connection
alias on a tmp_path file) to 0007, creates fixtures through the
historical models, migrates 0007 -> 0008 for real, and asserts both the
registry and the FTS structure/content. The reverse direction is
exercised for real and must remove BOTH search structures while leaving
canonical source data intact. The whole forward migration is one
transaction (atomicity proven with a mid-backfill failure), and DDL
failures are converted to a single fixed sanitized error that carries
neither SQL text, paths nor a sentinel secret — verified in the formatted
traceback too.
"""

from __future__ import annotations

import copy as _copy

import pytest
from django.db import connections
from django.db.migrations.executor import MigrationExecutor

TARGET_0007 = ("workflow", "0007_summary_multilingual")
TARGET_0008 = ("workflow", "0008_search_index")

ALIAS = "mig0008"

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
    db_path = str(tmp_path / "migration.sqlite3")
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


# ---------------------------------------------------------------------------
# Historical 0007 fixture builders
# ---------------------------------------------------------------------------


def _make_recording(apps, alias, *, sha):
    Recording = apps.get_model("workflow", "Recording")
    return Recording.objects.using(alias).create(
        sha256=sha, duration_seconds=60.0, processing_status="transcribed", summary_status="current"
    )


def _make_transcript(apps, alias, recording, *, active=True, attempt_ordinal=1):
    from django.utils import timezone as tz

    ProcessingAttempt = apps.get_model("workflow", "ProcessingAttempt")
    Transcript = apps.get_model("workflow", "Transcript")
    Section = apps.get_model("workflow", "Section")
    attempt = ProcessingAttempt.objects.using(alias).create(
        recording=recording, stage="transcription", ordinal=attempt_ordinal,
        outcome="success", finished_at=tz.now(),
    )
    transcript = Transcript.objects.using(alias).create(recording=recording, attempt=attempt)
    Section.objects.using(alias).create(transcript=transcript, ordinal=0)
    if active:
        Transcript.objects.using(alias).filter(pk=transcript.pk).update(is_active=True, activated_at=tz.now())
    return Transcript.objects.using(alias).get(pk=transcript.pk)


def _segment(apps, alias, transcript, ordinal, text, *, speaker="", start_ms=0, end_ms=1000):
    TranscriptSegment = apps.get_model("workflow", "TranscriptSegment")
    return TranscriptSegment.objects.using(alias).create(
        transcript=transcript, ordinal=ordinal, text=text, speaker=speaker,
        start_ms=start_ms, end_ms=end_ms,
    )


def _summary(apps, alias, recording, transcript, *, output_language="en", title="T",
             overview="Overview", is_active=True, **overrides):
    from django.utils import timezone as tz

    ProcessingAttempt = apps.get_model("workflow", "ProcessingAttempt")
    Summary = apps.get_model("workflow", "Summary")
    section = transcript.sections.first()
    last = (
        ProcessingAttempt.objects.using(alias)
        .filter(recording=recording, stage="summarization")
        .order_by("-ordinal")
        .first()
    )
    attempt = ProcessingAttempt.objects.using(alias).create(
        recording=recording, stage="summarization",
        ordinal=(last.ordinal + 1) if last else 1, outcome="success", finished_at=tz.now(),
    )
    fields = dict(
        title=title, overview=overview, key_points=["kp"], action_items=[], people=["Alice"],
        organizations=["Acme"], topics=["grading"], language="en",
        output_language=output_language, suggested_tags_raw={}, model_id="m",
        prompt_version="1", parser_version="1", chunk_count=1, input_characters=10,
        generation_mode="automatic",
    )
    fields.update(overrides)
    last_summary = (
        Summary.objects.using(alias).filter(recording=recording).order_by("-ordinal").first()
    )
    return Summary.objects.using(alias).create(
        recording=recording, transcript=transcript, section=section, attempt=attempt,
        ordinal=(last_summary.ordinal + 1) if last_summary else 1,
        is_active=is_active, activated_at=tz.now() if is_active else None, **fields,
    )


def _source(apps, alias, recording, *, filename, path, canonical=False):
    AudioSource = apps.get_model("workflow", "AudioSource")
    return AudioSource.objects.using(alias).create(
        recording=recording, path=path, path_identity=path.lower(),
        original_filename=filename, is_canonical=canonical,
    )


def _tag(apps, alias, recording, name):
    from brainlib.config import tag_name_key

    Tag = apps.get_model("workflow", "Tag")
    TagAssignment = apps.get_model("workflow", "TagAssignment")
    tag = Tag.objects.using(alias).create(name=name, name_key=tag_name_key(name))
    TagAssignment.objects.using(alias).create(
        recording=recording, tag=tag, origin="manual", is_active=True, deactivated_by=""
    )
    return tag


def _build_corpus(apps, alias):
    """Two recordings: one fully transcribed/summarized (with a superseded
    transcript whose Summary must NOT be indexed), one metadata-only."""
    r1 = _make_recording(apps, alias, sha="a" * 64)
    t_active = _make_transcript(apps, alias, r1, active=True, attempt_ordinal=1)
    _segment(apps, alias, t_active, 0, "hello world", speaker="S1", start_ms=0, end_ms=1500)
    _segment(apps, alias, t_active, 1, "  \n  ", speaker="")  # whitespace-only: excluded
    _segment(apps, alias, t_active, 2, "second segment", speaker="S2", start_ms=1500, end_ms=3000)
    _summary(apps, alias, r1, t_active, output_language="en", title="English title")
    _summary(apps, alias, r1, t_active, output_language="fi", title="Finnish title")

    # Superseded transcript with its own (scope-active) Summary: must be absent.
    t_old = _make_transcript(apps, alias, r1, active=False, attempt_ordinal=2)
    _segment(apps, alias, t_old, 0, "obsolete segment")
    _summary(apps, alias, r1, t_old, output_language="en", title="Obsolete title")

    _source(apps, alias, r1, filename="lecture.wav", path="/data/inbox/lecture.wav", canonical=True)
    _tag(apps, alias, r1, "Family")

    r2 = _make_recording(apps, alias, sha="b" * 64)  # metadata-only
    return r1, t_active, t_old, r2


def _registry_keys(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT document_key FROM workflow_search_document ORDER BY document_key")
        return [row[0] for row in cursor.fetchall()]


def _fts_state(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='workflow_search_fts'")
        row = cursor.fetchone()
        return row[0] if row else None


def _search_tables(connection):
    return {t for t in _tables(connection) if t.startswith("workflow_search")}


# ---------------------------------------------------------------------------
# Forward migration: structures + backfill content
# ---------------------------------------------------------------------------


def test_forward_creates_structures_and_backfills(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0007 = _migrate_to(executor, TARGET_0007)
    r1, t_active, t_old, r2 = _build_corpus(apps0007, alias)

    _migrate_to(executor, TARGET_0008)

    tables = _tables(connection)
    assert "workflow_search_document" in tables
    assert "workflow_search_fts" in tables
    ddl = _fts_state(connection)
    assert ddl is not None and "fts5(" in ddl.lower() and "tokenize='trigram'" in ddl.lower()

    keys = _registry_keys(connection)
    # segments (only the active transcript, only non-empty) + 2 summaries + 2 recordings
    assert f"segment:{t_active.pk}:0" in keys
    assert f"segment:{t_active.pk}:2" in keys
    assert f"segment:{t_active.pk}:1" not in keys  # whitespace-only segment excluded
    assert f"segment:{t_old.pk}:0" not in keys  # superseded transcript not indexed
    assert f"recording:{r1.pk}" in keys
    assert f"recording:{r2.pk}" in keys
    summary_keys = [k for k in keys if k.startswith("summary:")]
    assert len(summary_keys) == 2  # current transcript only, per output language

    # FTS rowid == SearchDocument.pk for every document
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM workflow_search_document d "
            "LEFT JOIN workflow_search_fts f ON d.id = f.rowid WHERE f.rowid IS NULL"
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT count(*) FROM workflow_search_fts f "
            "LEFT JOIN workflow_search_document d ON f.rowid = d.id WHERE d.id IS NULL"
        )
        assert cursor.fetchone()[0] == 0

    # Trigram MATCH works against backfilled content
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT d.document_key FROM workflow_search_document d "
            "JOIN workflow_search_fts f ON d.id = f.rowid "
            "WHERE workflow_search_fts MATCH %s ORDER BY d.document_key",
            ["hel"],
        )
        matched = [row[0] for row in cursor.fetchall()]
    assert any(k.endswith(":0") and k.startswith("segment:") for k in matched)


def test_segment_documents_do_not_carry_title_or_timestamps(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0007 = _migrate_to(executor, TARGET_0007)
    r1, t_active, _, _ = _build_corpus(apps0007, alias)
    _migrate_to(executor, TARGET_0008)

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT d.title_text, d.aux_text, d.start_ms, d.end_ms, f.aux_text "
            "FROM workflow_search_document d JOIN workflow_search_fts f ON d.id=f.rowid "
            "WHERE d.document_key = %s",
            [f"segment:{t_active.pk}:0"],
        )
        title, aux, start, end, fts_aux = cursor.fetchone()
    assert title == ""
    assert aux == "S1"
    assert fts_aux == "S1"
    assert (start, end) == (0, 1500)  # registry provenance retained
    # timestamps are NOT part of searchable FTS text
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM workflow_search_document d "
            "JOIN workflow_search_fts f ON d.id=f.rowid "
            "WHERE d.document_key = %s AND f.aux_text LIKE %s",
            [f"segment:{t_active.pk}:0", "%1500%"],
        )
        assert cursor.fetchone()[0] == 0


def test_metadata_only_recording_document_has_placeholder_title(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0007 = _migrate_to(executor, TARGET_0007)
    _, _, _, r2 = _build_corpus(apps0007, alias)
    _migrate_to(executor, TARGET_0008)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT title_text FROM workflow_search_document WHERE document_key = %s",
            [f"recording:{r2.pk}"],
        )
        assert cursor.fetchone()[0] == "Untitled recording"


def test_service_and_migration_fts_schema_sql_match():
    import importlib

    mig = importlib.import_module("workflow.migrations.0008_search_index")
    from workflow.services import search_index

    assert mig.FTS_SCHEMA_SQL == search_index.FTS_SCHEMA_SQL


# ---------------------------------------------------------------------------
# Migration <-> runtime canonicalization parity
# ---------------------------------------------------------------------------


def test_migration_backfill_matches_runtime_rebuild_parity(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0007 = _migrate_to(executor, TARGET_0007)
    _build_corpus(apps0007, alias)
    _migrate_to(executor, TARGET_0008)

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT document_key, content_hash, title_text, body_text, aux_text "
            "FROM workflow_search_document ORDER BY document_key"
        )
        backfilled = {row[0]: row[1:] for row in cursor.fetchall()}

    from workflow.services import search_index

    search_index.rebuild_index(using=alias)

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT document_key, content_hash, title_text, body_text, aux_text "
            "FROM workflow_search_document ORDER BY document_key"
        )
        rebuilt = {row[0]: row[1:] for row in cursor.fetchall()}

    assert backfilled.keys() == rebuilt.keys()
    # byte-identical content and identical hash for every document key
    for key in backfilled:
        assert backfilled[key] == rebuilt[key], key


# ---------------------------------------------------------------------------
# Legacy und variant within the active transcript
# ---------------------------------------------------------------------------


def test_legacy_und_variant_backfilled_then_reported(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0007 = _migrate_to(executor, TARGET_0007)
    r1, t_active, _, _ = _build_corpus(apps0007, alias)
    Summary = apps0007.get_model("workflow", "Summary")
    from django.utils import timezone as tz

    ProcessingAttempt = apps0007.get_model("workflow", "ProcessingAttempt")
    attempt = ProcessingAttempt.objects.using(alias).create(
        recording=r1, stage="summarization", ordinal=9, outcome="success", finished_at=tz.now()
    )
    Summary.objects.using(alias).create(
        recording=r1, transcript=t_active, section=t_active.sections.first(), attempt=attempt,
        ordinal=5, is_active=True, activated_at=tz.now(), title="und title", overview="o",
        key_points=[], action_items=[], people=[], organizations=[], topics=[],
        language="", output_language="und", suggested_tags_raw={}, model_id="m",
        prompt_version="1", parser_version="1", chunk_count=1, input_characters=1,
        generation_mode="automatic",
    )
    _migrate_to(executor, TARGET_0008)

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM workflow_search_document WHERE output_language = 'und'"
        )
        assert cursor.fetchone()[0] == 1

    from workflow.services import search_index

    report = search_index.build_status_report(using=alias)
    assert report["counts"]["legacy_und_variants"] == 1


# ---------------------------------------------------------------------------
# Reverse migration
# ---------------------------------------------------------------------------


def test_reverse_removes_structures_and_keeps_source(executor_and_alias):
    executor, connection, alias = executor_and_alias
    apps0007 = _migrate_to(executor, TARGET_0007)
    _build_corpus(apps0007, alias)
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM workflow_transcriptsegment")
        segment_count = cursor.fetchone()[0]

    _migrate_to(executor, TARGET_0008)
    assert {"workflow_search_document", "workflow_search_fts"} <= _search_tables(connection)

    executor.migrate([TARGET_0007])
    executor.loader.build_graph()

    # Both structures (and FTS shadow tables) are gone
    leftover = {t for t in _tables(connection) if t.startswith("workflow_search")}
    assert leftover == set()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE 'workflow_search_fts%'"
        )
        assert cursor.fetchone()[0] == 0

    # Canonical source data is untouched
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM workflow_transcriptsegment")
        assert cursor.fetchone()[0] == segment_count


# ---------------------------------------------------------------------------
# Atomic forward migration (mid-backfill failure rolls everything back)
# ---------------------------------------------------------------------------


def test_forward_is_atomic_on_midbackfill_failure(executor_and_alias, monkeypatch):
    import importlib

    executor, connection, alias = executor_and_alias
    apps0007 = _migrate_to(executor, TARGET_0007)
    _build_corpus(apps0007, alias)

    mig = importlib.import_module("workflow.migrations.0008_search_index")
    real_insert = mig._insert_fts_rows
    calls: list[int] = []

    def flaky_insert(cursor, rows):
        # Write the first batch for real (registry + FTS now hold PARTIAL
        # data), then explode. The migration transaction must undo all of
        # it, including the CREATE VIRTUAL TABLE and CreateModel DDL.
        real_insert(cursor, rows)
        calls.append(len(rows))
        raise RuntimeError("synthetic mid-backfill failure")

    monkeypatch.setattr(mig, "_insert_fts_rows", flaky_insert)

    with pytest.raises(Exception, match="synthetic mid-backfill failure"):
        executor.migrate([TARGET_0008])
    executor.loader.build_graph()

    assert calls, "the injected failure never fired"
    # Nothing persisted: registry table absent, FTS absent, 0008 not recorded.
    assert _search_tables(connection) == set()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM django_migrations WHERE app='workflow' AND name='0008_search_index'"
        )
        assert cursor.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Sanitized DDL failure (fixed error, no SQL/paths/sentinel in traceback)
# ---------------------------------------------------------------------------


def test_fts_ddl_failure_is_sanitized(executor_and_alias, monkeypatch):
    import importlib

    executor, connection, alias = executor_and_alias
    apps0007 = _migrate_to(executor, TARGET_0007)
    _build_corpus(apps0007, alias)

    mig = importlib.import_module("workflow.migrations.0008_search_index")
    sentinel = "SENTINEL-SECRET-ZZZ /Users/spy/private/brain.sqlite3"

    def exploding_execute(schema_editor, sql):
        # Simulate a raw SQLite DDL error carrying sensitive text.
        raise RuntimeError(f"raw sqlite error near {sentinel}")

    monkeypatch.setattr(mig, "_execute_ddl", exploding_execute)

    import traceback

    with pytest.raises(RuntimeError) as excinfo:
        executor.migrate([TARGET_0008])
    rendered = "".join(
        traceback.format_exception(excinfo.type, excinfo.value, excinfo.value.__traceback__)
    )

    # The converted message is the single fixed sanitized error ...
    assert excinfo.value.args[0] == mig.DDL_ERROR
    # ... and the sentinel/SQL/paths never appear in message OR traceback.
    assert "SENTINEL-SECRET-ZZZ" not in rendered
    assert "/Users/spy/private" not in rendered
    assert "CREATE VIRTUAL TABLE" not in rendered

    # Atomic: nothing left behind.
    assert _search_tables(connection) == set()


def test_missing_fts5_capability_stops_migration_sanitized(executor_and_alias, monkeypatch):
    import importlib
    import sqlite3

    executor, connection, alias = executor_and_alias
    apps0007 = _migrate_to(executor, TARGET_0007)
    _build_corpus(apps0007, alias)

    mig = importlib.import_module("workflow.migrations.0008_search_index")

    class BrokenConn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("no such module: fts5 with path /secret/db.sqlite3")

        def close(self):
            pass

    monkeypatch.setattr(mig.sqlite3, "connect", lambda *a, **k: BrokenConn())

    import traceback

    with pytest.raises(RuntimeError) as excinfo:
        executor.migrate([TARGET_0008])
    rendered = "".join(
        traceback.format_exception(excinfo.type, excinfo.value, excinfo.value.__traceback__)
    )
    assert excinfo.value.args[0] == mig.PROBE_ERROR
    assert "/secret/db.sqlite3" not in rendered
    assert _search_tables(connection) == set()



# ---------------------------------------------------------------------------
# Streaming backfill bound + new CheckConstraint mirrors (review fixes)
# ---------------------------------------------------------------------------


def test_backfill_streams_a_large_single_recording_in_bounded_chunks(
    executor_and_alias, monkeypatch
):
    import importlib

    executor, connection, alias = executor_and_alias
    apps0007 = _migrate_to(executor, TARGET_0007)
    r1 = _make_recording(apps0007, alias, sha="d" * 64)
    t1 = _make_transcript(apps0007, alias, r1, active=True)
    for ordinal in range(1200):
        _segment(apps0007, alias, t1, ordinal, f"streaming segment {ordinal}")

    mig = importlib.import_module("workflow.migrations.0008_search_index")
    real_insert = mig._insert_fts_rows
    sizes: list[int] = []

    def spy_insert(cursor, rows):
        sizes.append(len(rows))
        return real_insert(cursor, rows)

    monkeypatch.setattr(mig, "_insert_fts_rows", spy_insert)
    _migrate_to(executor, TARGET_0008)

    assert sizes, "the backfill seam spy never fired"
    assert max(sizes) <= mig.INSERT_CHUNK_SIZE, sizes
    assert len(sizes) >= 3, sizes  # 1200 segments cannot fit one flush
    assert sum(sizes) == 1201  # 1200 segment docs + 1 metadata doc
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM workflow_search_document")
        assert cursor.fetchone()[0] == 1201
        cursor.execute("SELECT count(*) FROM workflow_search_fts")
        assert cursor.fetchone()[0] == 1201


def test_new_check_constraints_are_mirrored_and_enforced_by_the_migration(
    executor_and_alias
):
    executor, connection, alias = executor_and_alias
    _migrate_to(executor, TARGET_0007)
    apps = _migrate_to(executor, TARGET_0008)

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='table'"
            " AND name='workflow_search_document'"
        )
        ddl = cursor.fetchone()[0]
    assert "chk_search_doc_type_allowed" in ddl
    assert "chk_search_doc_summary_output_language" in ddl

    # Enforced at the DB level on the MIGRATED schema (raw SQL bypasses
    # every Django-level validation):
    r = _make_recording(apps, alias, sha="e" * 64)
    with connection.cursor() as cursor:
        with pytest.raises(Exception):
            cursor.execute(
                "INSERT INTO workflow_search_document"
                " (document_key, doc_type, recording_id, output_language,"
                "  title_text, body_text, aux_text, content_hash,"
                "  index_version, created_at)"
                " VALUES (%s, %s, %s, '', '', %s, '', %s, '1',"
                "  '2026-01-01 00:00:00')",
                ["bogus:1", "bogus", r.pk, "body", "1" * 64],
            )
        # sanity: the very same insert SHAPE with a legitimate doc_type
        # passes (the CHECK above is what rejected it)
        cursor.execute(
            "INSERT INTO workflow_search_document"
            " (document_key, doc_type, recording_id, output_language,"
            "  title_text, body_text, aux_text, content_hash,"
            "  index_version, created_at)"
            " VALUES (%s, 'recording', %s, '', '', %s, '', %s, '1',"
            "  '2026-01-01 00:00:00')",
            ["recording:mig-ok", r.pk, "body", "1" * 64],
        )
        cursor.execute(
            "SELECT count(*) FROM workflow_search_document"
            " WHERE document_key = 'recording:mig-ok'"
        )
        assert cursor.fetchone()[0] == 1
