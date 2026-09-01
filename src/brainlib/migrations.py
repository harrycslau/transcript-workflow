"""Migration-readiness inspection shared by ``brain doctor`` and the CLI
schema preflight.

Uses Django's migration APIs (``MigrationExecutor`` / ``MigrationRecorder``)
against the configured database. Strictly READ-ONLY: the inspection runs
``SELECT`` queries only (``django_migrations`` table presence and recorded
entries); it never applies, fakes, or records migrations, and never writes
application rows.

Labels are ``app_label.migration_name`` pairs derived from the migration
graph — safe identifiers containing no paths, SQL, secrets, or content.
"""

from __future__ import annotations

# Stable error categories surfaced to users; raw exception text never
# reaches CLI/doctor output.
CATEGORY_UNAVAILABLE = "migration table unavailable (database never migrated)"
CATEGORY_INCONSISTENT = "migration history inconsistent or unreadable"


class MigrationInspectionError(Exception):
    """Migration state could not be inspected; nothing was modified."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def unapplied_migrations() -> list[str]:
    """Return ``app_label.migration_name`` labels of every migration the
    current code requires that the configured database has not applied.

    The result is the forward migration plan from the recorded state to
    the graph's leaf nodes, in dependency order. Raises
    :class:`MigrationInspectionError` with a stable category when
    inspection itself fails; the underlying cause is chained for local
    logging but is never included in user-facing output.
    """
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from django.db.migrations.recorder import MigrationRecorder

    recorder = MigrationRecorder(connection)
    try:
        if not recorder.has_table():
            raise MigrationInspectionError(CATEGORY_UNAVAILABLE)
        executor = MigrationExecutor(connection)
        # Explicit consistency check: the `migrate` command runs this,
        # but read-only inspection must surface the same problem (a
        # KNOWN applied migration with an UNAPPLIED known parent).
        executor.loader.check_consistent_history(connection)
        targets = executor.loader.graph.leaf_nodes()
        plan = executor.migration_plan(targets)
    except MigrationInspectionError:
        raise
    except Exception:
        # Graph inconsistency, unreadable history, or broken migration
        # modules: report a stable category only (cause stays chained).
        raise MigrationInspectionError(CATEGORY_INCONSISTENT) from None
    # Plan entries are (Migration, backwards?) pairs; forward-only
    # inspection must yield exclusively backwards=False entries. A
    # backwards entry would mean the recorded state diverges from the
    # graph, which the executor reports as an inconsistency.
    return [
        f"{migration.app_label}.{migration.name}"
        for migration, backwards in plan
        if not backwards
    ]


def summarize_pending(pending: list[str], max_labels: int = 5) -> str:
    """A concise, sanitized, count-led summary of pending migrations."""
    shown = ", ".join(pending[:max_labels])
    if len(pending) > max_labels:
        shown += ", …"
    return f"{len(pending)} unapplied migration(s): {shown}"


RECOVERY_COMMAND = "uv run python src/manage.py migrate"
