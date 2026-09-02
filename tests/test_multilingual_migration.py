"""Genuine MigrationExecutor acceptance tests for migration 0007.

Every test in this module migrates an ISOLATED SQLite database (a
dedicated connection alias in tmp_path) to historical ``0006``, creates
fixtures through historical ``apps.get_model`` models, migrates
``0006 → 0007`` for real, and asserts the resulting rows and state
tuples. The real configured database is never touched.

The reverse direction is exercised for real: the irreversible backfill
is the LAST operation, so a reverse attempt must raise
``IrreversibleError`` BEFORE any schema or data mutation.
"""

from __future__ import annotations

import pytest
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.migration import IrreversibleError

TARGET_0006 = ("workflow", "0006_attempt_context_json")
TARGET_0007 = ("workflow", "0007_summary_multilingual")

ALIAS = "migtest"

# Register the isolated alias up front so pytest-django's databases
# allow-list (checked against settings.DATABASES) accepts it, cloning
# the default alias's settings (TIME_ZONE, TEST, ...) so Django's
# connection checks are satisfied. Each test points it at its own
# tmp_path file via the fixture below.
import copy as _copy

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
    # Discard the session-created wrapper (pytest-django migrates every
    # registered alias to HEAD at setup; its shared-memory DB must not
    # leak between tests) and build a fresh wrapper on this test's own
    # SQLite file.
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
        # The alias REGISTRATION stays (pytest-django validates the
        # allow-list against settings.DATABASES at every test); each
        # test's fixture re-points NAME at its own file.


def _migrate_to(executor, target):
    executor.migrate([target])
    executor.loader.build_graph()
    return executor.loader.project_state([target]).apps


def _tables(connection):
    with connection.cursor() as cursor:
        tables = connection.introspection.table_names(cursor)
    return set(tables)


def _columns(connection, table):
    with connection.cursor() as cursor:
        columns = {col.name for col in connection.introspection.get_table_description(cursor, table)}
    return columns


# ---------------------------------------------------------------------------
# Historical 0006 fixture builders (historical models only)
# ---------------------------------------------------------------------------


def _make_recording_0006(apps, alias, *, sha, summary_status="missing", **overrides):
    Recording = apps.get_model("workflow", "Recording")
    fields = dict(
        sha256=sha,
        duration_seconds=60.0,
        processing_status="transcribed",
        summary_status=summary_status,
    )
    fields.update(overrides)
    return Recording.objects.using(alias).create(**fields)


def _make_transcript_0006(apps, alias, recording, *, sha_suffix, language="", active=True):
    from django.utils import timezone as tz

    ProcessingAttempt = apps.get_model("workflow", "ProcessingAttempt")
    Transcript = apps.get_model("workflow", "Transcript")
    Section = apps.get_model("workflow", "Section")

    attempt = ProcessingAttempt.objects.using(alias).create(
        recording=recording,
        stage="transcription",
        ordinal=ProcessingAttempt.objects.using(alias).filter(recording=recording).count() + 1,
        outcome="success",
        finished_at=tz.now(),
    )
    transcript = Transcript.objects.using(alias).create(
        recording=recording,
        attempt=attempt,
        text_normalized="hello world",
        language_observed=language,
    )
    Section.objects.using(alias).create(transcript=transcript, ordinal=0, title="Full recording")
    if active:
        transcript.is_active = True
        transcript.activated_at = tz.now()
        transcript.save()
    return transcript


def _make_summary_0006(apps, alias, recording, transcript, *, ordinal=1, language="en",
                       title="Meeting about grading", overview="Discussed grading plans.",
                       is_active=True):
    from django.utils import timezone as tz

    ProcessingAttempt = apps.get_model("workflow", "ProcessingAttempt")
    Summary = apps.get_model("workflow", "Summary")
    Section = apps.get_model("workflow", "Section")

    section = Section.objects.using(alias).get(transcript=transcript, ordinal=0)
    attempt = ProcessingAttempt.objects.using(alias).create(
        recording=recording,
        stage="summarization",
        ordinal=ProcessingAttempt.objects.using(alias).filter(recording=recording).count() + 1,
        outcome="success",
        finished_at=tz.now(),
    )
    return Summary.objects.using(alias).create(
        recording=recording,
        transcript=transcript,
        section=section,
        attempt=attempt,
        ordinal=ordinal,
        is_active=is_active,
        activated_at=tz.now() if is_active else None,
        title=title,
        overview=overview,
        key_points=[{"text": "Point one", "level": 1}],
        action_items=[],
        people=[],
        organizations=[],
        topics=[],
        language=language,
        model_id="test-model",
        prompt_version="1",
        parser_version="1",
        chunk_count=1,
        input_characters=100,
        generation_mode="manual",
    )


def _make_failed_attempt_0006(apps, alias, recording, *, ordinal=None, context=None):
    from django.utils import timezone as tz

    ProcessingAttempt = apps.get_model("workflow", "ProcessingAttempt")
    return ProcessingAttempt.objects.using(alias).create(
        recording=recording,
        stage="summarization",
        ordinal=ordinal
        or ProcessingAttempt.objects.using(alias).filter(recording=recording).count() + 1,
        outcome="invalid_output",
        error_code="schema_validation",
        finished_at=tz.now(),
        context_json=context,
    )


def _provenance(transcript, section, resolved):
    return {
        "language": {
            "requested": "default",
            "resolved": resolved,
            "is_default": True,
            "transcript_id": transcript.pk,
            "section_id": section.pk,
        }
    }


def _summary_after_migrate(alias, historical_summary):
    """Re-fetch through the REAL (0007-current) model on the isolated DB."""
    from workflow.models import Summary

    return Summary.objects.using(alias).get(pk=historical_summary.pk)


# ---------------------------------------------------------------------------
# Forward migration 0006 → 0007
# ---------------------------------------------------------------------------


class TestForwardMigration0006To0007:
    def test_english_summary_gets_output_language_and_current_variant_state(
        self, executor_and_alias
    ):
        executor, connection, alias = executor_and_alias
        apps = _migrate_to(executor, TARGET_0006)
        recording = _make_recording_0006(apps, alias, sha="mig-fwd-1", summary_status="current")
        transcript = _make_transcript_0006(apps, alias, recording, sha_suffix="a", language="fi")
        summary = _make_summary_0006(apps, alias, recording, transcript, language="en")

        executor.migrate([TARGET_0007])

        summary = _summary_after_migrate(alias, summary)
        assert summary.output_language == "en"
        from workflow.models import SummaryVariantState

        section = transcript.sections.using(alias).get(ordinal=0)
        vs = SummaryVariantState.objects.using(alias).get(
            transcript_id=transcript.pk, section_id=section.pk, output_language="en"
        )
        assert vs.status == "current"
        assert vs.regeneration_failed is False
        recording.refresh_from_db(using=alias)
        assert recording.summary_status == "current"

    def test_chinese_prose_labeled_english_becomes_zh_hant_and_english_default_missing(
        self, executor_and_alias
    ):
        """The canonical counterexample: prose inspection wins over the
        mislabeled stored language. The (derived) English default has no
        summary afterwards, so the recording becomes eligible for normal
        summarization (missing — never failed)."""
        executor, connection, alias = executor_and_alias
        apps = _migrate_to(executor, TARGET_0006)
        recording = _make_recording_0006(apps, alias, sha="mig-fwd-2", summary_status="current")
        # No Chinese routing and no source language: derived default is en.
        transcript = _make_transcript_0006(apps, alias, recording, sha_suffix="b")
        summary = _make_summary_0006(
            apps,
            alias,
            recording,
            transcript,
            language="en",
            title="教育研究統計方法",
            overview="本次工作坊詳細介紹統計方法及研究設計中的重要注意事項，並且說明評分安排。",
        )

        executor.migrate([TARGET_0007])

        from workflow.models import SummaryVariantState

        summary = _summary_after_migrate(alias, summary)
        assert summary.output_language == "zh-Hant"
        section = transcript.sections.using(alias).get(ordinal=0)
        assert SummaryVariantState.objects.using(alias).get(
            transcript_id=transcript.pk, section_id=section.pk, output_language="zh-Hant"
        ).status == "current"
        assert SummaryVariantState.objects.using(alias).get(
            transcript_id=transcript.pk, section_id=section.pk, output_language="en"
        ).status == "missing"
        recording.refresh_from_db(using=alias)
        assert recording.summary_status == "missing"
        assert recording.resummarization_failed is False
        assert recording.last_failed_attempt_id is None

    def test_optional_failure_does_not_mark_default_failed(self, executor_and_alias):
        executor, connection, alias = executor_and_alias
        apps = _migrate_to(executor, TARGET_0006)
        recording = _make_recording_0006(apps, alias, sha="mig-fwd-3")
        transcript = _make_transcript_0006(apps, alias, recording, sha_suffix="c")
        section = transcript.sections.using(alias).get(ordinal=0)
        _make_failed_attempt_0006(
            apps, alias, recording, context=_provenance(transcript, section, "zh-Hant")
        )

        executor.migrate([TARGET_0007])

        from workflow.models import SummaryVariantState

        assert SummaryVariantState.objects.using(alias).get(
            transcript_id=transcript.pk, section_id=section.pk, output_language="en"
        ).status == "missing"
        # An optional-language failure creates NO variant state row: VS
        # rows exist only for the derived default and real summaries.
        assert not SummaryVariantState.objects.using(alias).filter(
            transcript_id=transcript.pk, section_id=section.pk, output_language="zh-Hant"
        ).exists()
        recording.refresh_from_db(using=alias)
        assert recording.summary_status == "missing"
        assert recording.last_failed_attempt_id is None

    def test_historical_transcript_failure_does_not_fail_new_active_transcript(
        self, executor_and_alias
    ):
        executor, connection, alias = executor_and_alias
        apps = _migrate_to(executor, TARGET_0006)
        recording = _make_recording_0006(apps, alias, sha="mig-fwd-4")
        old_transcript = _make_transcript_0006(
            apps, alias, recording, sha_suffix="d", language="", active=False
        )
        new_transcript = _make_transcript_0006(
            apps, alias, recording, sha_suffix="e", language="", active=True
        )
        old_section = old_transcript.sections.using(alias).get(ordinal=0)
        # Proven default failure belonging to the OLD transcript.
        _make_failed_attempt_0006(
            apps, alias, recording, context=_provenance(old_transcript, old_section, "en")
        )

        executor.migrate([TARGET_0007])

        from workflow.models import SummaryVariantState

        new_section = new_transcript.sections.using(alias).get(ordinal=0)
        assert SummaryVariantState.objects.using(alias).get(
            transcript_id=new_transcript.pk, section_id=new_section.pk, output_language="en"
        ).status == "missing"
        recording.refresh_from_db(using=alias)
        assert recording.summary_status == "missing"
        assert recording.last_failed_attempt_id is None

    def test_proven_matching_default_failure_becomes_failed_with_exact_pointer(
        self, executor_and_alias
    ):
        executor, connection, alias = executor_and_alias
        apps = _migrate_to(executor, TARGET_0006)
        recording = _make_recording_0006(apps, alias, sha="mig-fwd-5")
        transcript = _make_transcript_0006(apps, alias, recording, sha_suffix="f")
        section = transcript.sections.using(alias).get(ordinal=0)
        failure = _make_failed_attempt_0006(
            apps, alias, recording, context=_provenance(transcript, section, "en")
        )

        executor.migrate([TARGET_0007])

        from workflow.models import SummaryVariantState

        vs = SummaryVariantState.objects.using(alias).get(
            transcript_id=transcript.pk, section_id=section.pk, output_language="en"
        )
        assert vs.status == "failed"
        assert vs.last_failed_attempt_id == failure.pk
        recording.refresh_from_db(using=alias)
        assert recording.summary_status == "failed"
        assert recording.last_failed_attempt_id == failure.pk
        assert recording.resummarization_failed is False

    def test_ambiguous_legacy_failure_becomes_missing(self, executor_and_alias):
        executor, connection, alias = executor_and_alias
        apps = _migrate_to(executor, TARGET_0006)
        recording = _make_recording_0006(apps, alias, sha="mig-fwd-6")
        transcript = _make_transcript_0006(apps, alias, recording, sha_suffix="g")
        section = transcript.sections.using(alias).get(ordinal=0)
        # A legacy failure WITHOUT provenance can never be attributed.
        _make_failed_attempt_0006(apps, alias, recording, context=None)

        executor.migrate([TARGET_0007])

        from workflow.models import SummaryVariantState

        vs = SummaryVariantState.objects.using(alias).get(
            transcript_id=transcript.pk, section_id=section.pk, output_language="en"
        )
        assert vs.status == "missing"
        assert vs.last_failed_attempt_id is None
        recording.refresh_from_db(using=alias)
        assert recording.summary_status == "missing"

    def test_complete_stale_state_tuple_is_corrected(self, executor_and_alias):
        """A recording whose tuple claims failure but has an active
        default summary is reconciled to current (complete tuple write)."""
        executor, connection, alias = executor_and_alias
        apps = _migrate_to(executor, TARGET_0006)
        recording = _make_recording_0006(
            apps, alias, sha="mig-fwd-7", summary_status="failed",
            resummarization_failed=True,
        )
        transcript = _make_transcript_0006(apps, alias, recording, sha_suffix="h")
        summary = _make_summary_0006(apps, alias, recording, transcript, language="en")
        # An OLD matching failure (older than the summary's attempt).
        _make_failed_attempt_0006(
            apps, alias, recording,
            ordinal=summary.attempt.ordinal - 1,
            context=_provenance(transcript, transcript.sections.using(alias).get(ordinal=0), "en"),
        )
        # The stale tuple points at the wrong attempt.
        stale_failure = _make_failed_attempt_0006(
            apps, alias, recording, context=_provenance(transcript, transcript.sections.using(alias).get(ordinal=0), "zh-Hant")
        )
        recording.last_failed_attempt = stale_failure
        recording.save(using=alias)

        executor.migrate([TARGET_0007])

        recording.refresh_from_db(using=alias)
        assert recording.summary_status == "current"
        assert recording.resummarization_failed is False
        assert recording.last_failed_attempt_id is None

    def test_newer_matching_failure_marks_regeneration_failed(self, executor_and_alias):
        executor, connection, alias = executor_and_alias
        apps = _migrate_to(executor, TARGET_0006)
        recording = _make_recording_0006(apps, alias, sha="mig-fwd-8", summary_status="current")
        transcript = _make_transcript_0006(apps, alias, recording, sha_suffix="i")
        section = transcript.sections.using(alias).get(ordinal=0)
        summary = _make_summary_0006(apps, alias, recording, transcript, language="en")
        # A matching failed regeneration NEWER than the summary attempt.
        failure = _make_failed_attempt_0006(
            apps, alias, recording, ordinal=summary.attempt.ordinal + 1,
            context=_provenance(transcript, section, "en"),
        )

        executor.migrate([TARGET_0007])

        from workflow.models import SummaryVariantState

        vs = SummaryVariantState.objects.using(alias).get(
            transcript_id=transcript.pk, section_id=section.pk, output_language="en"
        )
        assert vs.status == "current"
        assert vs.regeneration_failed is True
        assert vs.last_failed_attempt_id == failure.pk
        recording.refresh_from_db(using=alias)
        assert recording.summary_status == "current"
        assert recording.resummarization_failed is True
        assert recording.last_failed_attempt_id == failure.pk

    def test_user_corrected_chinese_source_derives_zh_hant_default(
        self, executor_and_alias
    ):
        """User-corrected source language outranks everything (the 0007
        fields exist only after the schema part; corrections made at
        0006 had no verified_by column, so this exercises the source-
        language fallback of the derivation instead)."""
        executor, connection, alias = executor_and_alias
        apps = _migrate_to(executor, TARGET_0006)
        recording = _make_recording_0006(apps, alias, sha="mig-fwd-9")
        transcript = _make_transcript_0006(
            apps, alias, recording, sha_suffix="j", language="zh-HK"
        )
        section = transcript.sections.using(alias).get(ordinal=0)
        _make_failed_attempt_0006(
            apps, alias, recording, context=_provenance(transcript, section, "zh-Hant")
        )

        executor.migrate([TARGET_0007])

        from workflow.models import SummaryVariantState

        vs = SummaryVariantState.objects.using(alias).get(
            transcript_id=transcript.pk, section_id=section.pk, output_language="zh-Hant"
        )
        assert vs.status == "failed"
        recording.refresh_from_db(using=alias)
        assert recording.summary_status == "failed"


# ---------------------------------------------------------------------------
# Corrupted legacy data fails the migration clearly and atomically
# ---------------------------------------------------------------------------


class TestMigrationFailsOnCorruptedData:
    def test_duplicate_active_summaries_abort_migration_without_mutation(
        self, executor_and_alias
    ):
        executor, connection, alias = executor_and_alias
        apps = _migrate_to(executor, TARGET_0006)
        recording = _make_recording_0006(apps, alias, sha="mig-bad-1", summary_status="current")
        transcript = _make_transcript_0006(apps, alias, recording, sha_suffix="k")
        _make_summary_0006(apps, alias, recording, transcript, ordinal=1, language="en")
        # Corrupt the data by bypassing the 0006 partial unique constraint.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name="
                "'workflow_summary' AND name NOT LIKE 'sqlite_%'"
            )
            indexes = [row[0] for row in cursor.fetchall()]
            for index_name in indexes:
                cursor.execute(f'DROP INDEX "{index_name}"')
            # Full-row duplicate (different pk/ordinal) via a temp table.
        spare_attempt = _make_failed_attempt_0006(
            apps, alias, recording, context=None
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name="
                "'workflow_summary' AND name NOT LIKE 'sqlite_%'"
            )
            indexes = [row[0] for row in cursor.fetchall()]
            for index_name in indexes:
                cursor.execute(f'DROP INDEX "{index_name}"')
            cursor.execute(
                "CREATE TEMPORARY TABLE dup_summary AS "
                "SELECT * FROM workflow_summary WHERE ordinal = 1"
            )
            cursor.execute(
                "UPDATE dup_summary SET id = id || '-dup', ordinal = ordinal + 50"
            )
            cursor.execute(
                "UPDATE dup_summary SET attempt_id = ?", (spare_attempt.pk,)
            )
            cursor.execute("INSERT INTO workflow_summary SELECT * FROM dup_summary")
            cursor.execute("DROP TABLE dup_summary")

        with pytest.raises(RuntimeError, match="corrupted legacy data"):
            executor.migrate([TARGET_0007])

        # The migration aborted atomically: no new table, no new column,
        # and the corrupted rows are exactly as before.
        assert "workflow_summaryvariantstate" not in _tables(connection)
        assert "output_language" not in _columns(connection, "workflow_summary")
        assert "language_observed_verified_by" not in _columns(
            connection, "workflow_transcript"
        )


# ---------------------------------------------------------------------------
# Irreversible reverse
# ---------------------------------------------------------------------------


class TestIrreversibleReverse:
    def test_reverse_raises_irreversibleerror_without_partial_mutation(
        self, executor_and_alias
    ):
        executor, connection, alias = executor_and_alias
        apps = _migrate_to(executor, TARGET_0006)
        recording = _make_recording_0006(apps, alias, sha="mig-rev-1", summary_status="current")
        transcript = _make_transcript_0006(apps, alias, recording, sha_suffix="l", language="fi")
        summary = _make_summary_0006(apps, alias, recording, transcript, language="en")

        executor.migrate([TARGET_0007])

        tables_before = _tables(connection)
        columns_before = _columns(connection, "workflow_summary")
        from django.db.migrations.recorder import MigrationRecorder

        applied_before = set(MigrationRecorder(connection).applied_migrations())

        # The 0007 apply happened after the last graph build; rebuild so
        # the executor's plan sees 0007 as applied and unapplies it.
        executor.loader.build_graph()
        with pytest.raises(IrreversibleError):
            executor.migrate([TARGET_0006])

        # NO partial reverse mutation: schema and data are untouched and
        # 0007 is still recorded as applied.
        assert _tables(connection) == tables_before
        assert _columns(connection, "workflow_summary") == columns_before
        assert "output_language" in _columns(connection, "workflow_summary")
        assert "workflow_summaryvariantstate" in _tables(connection)
        applied_after = set(MigrationRecorder(connection).applied_migrations())
        assert TARGET_0007 in applied_after
        assert applied_after == applied_before
        summary = _summary_after_migrate(alias, summary)
        assert summary.output_language == "en"

    def test_runpython_operations_carry_no_reverse_code(self):
        from importlib import import_module

        module = import_module("workflow.migrations.0007_summary_multilingual")
        runpythons = [
            op
            for op in module.Migration.operations
            if isinstance(op, __import__("django.db.migrations", fromlist=["RunPython"]).RunPython)
        ]
        assert len(runpythons) == 2
        for op in runpythons:
            assert op.reverse_code is None
        # The invariant check is the FIRST operation (corrupted data
        # fails before any schema change) and the data backfill the
        # LAST. Reverse safety comes from Django's unapply pre-check:
        # Migration.unapply inspects every operation for reversibility
        # and raises IrreversibleError before executing anything, which
        # the executor-level test above proves.
        assert module.Migration.operations[0] is runpythons[0]
        assert module.Migration.operations[-1] is runpythons[-1]
