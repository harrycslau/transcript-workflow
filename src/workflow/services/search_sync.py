"""Incremental search index synchronization (Step 5A.3).

Keeps the ``SearchDocument`` registry and the ``workflow_search_fts`` FTS5
table synchronized with authoritative data AFTER the authoritative
transaction commits, without requiring a full rebuild:

- ``schedule_recording_sync(recording_ids)`` is the ONLY trigger: mutating
  services call it INSIDE their transaction; the work runs through
  ``transaction.on_commit`` (immediately under plain autocommit) and
  NEVER inside a GET.
- ``reconcile_recording(recording_id)`` is the ONE per-recording writer.
  It recomputes the canonical expected documents with the Step 5A.2
  builders (``search_index`` — never a forked mapping) and reconciles the
  registry + FTS for exactly that Recording in ONE transaction:
  non-canonical registry rows are deleted (shared canonical-row
  validation, foreign/mismatched provenance rejected), then expected
  documents are upserted with FULL-field comparison — a row is skipped
  only when every registry field AND the exact FTS text equal the
  expected spec; ``content_hash`` alone is never trusted.

FTS row handling is explicit and never ambiguous:
  registry + FTS both correct      -> skip;
  registry exists, FTS text differs -> UPDATE the existing rowid;
  registry exists, FTS row missing  -> repair registry fields if needed,
    then INSERT the FTS row using the EXISTING registry pk as rowid;
  registry row missing              -> create it, then INSERT the FTS row.
A missing FTS row is never "repaired" with UPDATE alone.

Failure contract (AGENTS.md): an index failure NEVER rolls back or
falsely fails transcript, summary, routing, tag or ingestion operations.
Recordings inside one callback are reconciled INDEPENDENTLY — one
failure never prevents the others from syncing; the ONLY log is a single
fixed aggregate warning carrying the failure COUNT and nothing else
(no ids, exceptions, paths, SQL or indexed content). ``brain
search-index status`` remains the detection mechanism and ``rebuild``
the authoritative repair.

Self-healing scope: reconcile repairs everything attributable to this
recording (missing/stale/tampered registry rows, missing/mismatched FTS
rows for existing registry rows, non-canonical registry rows and their
FTS rows). An FTS row whose registry row is gone (FK CASCADE left it
behind) has NO attribution from FTS alone — reconcile recreates the new
canonical registry/FTS rows when the data is still eligible but can
NEVER remove such an orphan; ``status`` reports ``orphan_fts_row`` and
only a full ``rebuild`` clears it.

Concurrency: pipeline CLI commands and web actions commit while holding
the pipeline flock, so their hooks run locked; web tag edits have no
flock and their syncs rely on SQLite writer serialization instead. A
busy/serialization error is a NONFATAL sync failure (caught, counted in
the aggregate warning); the index stays detectably stale and converges
via the next successful sync or a rebuild. ``reconcile_recording``
recomputes truth from the database inside its own transaction and is
idempotent: a converged recording produces zero DML.

This module never registers a sync from within ``reconcile_recording``
(no recursion), never acquires the pipeline lock, and never runs on GET.
"""

from __future__ import annotations

import logging
from typing import Sequence

from django.db import connections, transaction

from workflow.models import Recording, SearchDocument
from workflow.services.search_index import (
    INSERT_CHUNK_SIZE,
    _delete_fts_rows,
    _expected_registry_page_is_canonical,
    _insert_fts_batch,
    _read_fts_rows,
    _registry_schema_present,
    _REGISTRY_SCHEMA_ERROR,
    _SYNC_INSERT_ERROR,
    _update_fts_batch,
    SearchIndexError,
    apply_spec_to_registry_row,
    document_from_spec,
    iter_expected_documents_for_recording_ids,
    registry_row_matches_spec,
)

LOGGER = logging.getLogger(__name__)

# Fixed, sanitized log line ONLY — never ids, exception text, paths, SQL
# or indexed content (AGENTS.md: log categories/counts, never values).
SYNC_FAILURE_CATEGORY = "search_index_sync_failed"
_SYNC_FAILED_LOG = (
    "search index post-commit sync failed category=" + SYNC_FAILURE_CATEGORY + " count=%d"
)

_CATEGORIES = (
    "inserted",
    "updated",
    "fts_repaired",
    "deleted",
    "skipped",
    "recording_missing",
)


def reconcile_recording(recording_id: str, *, using: str = "default") -> dict:
    """Synchronize registry + FTS for exactly ONE Recording, atomically.

    Returns counts only (never content): inserted / updated /
    fts_repaired / deleted / skipped / recording_missing. Idempotent: a
    second run over converged data issues no DML. Any failure rolls the
    whole per-recording transaction back (the index is never left
    half-written by a failed sync); the caller decides reporting.
    """
    counts = {name: 0 for name in _CATEGORIES}
    if not _registry_schema_present(using=using):
        raise SearchIndexError(_REGISTRY_SCHEMA_ERROR)
    with transaction.atomic(using=using):
        if not Recording.objects.using(using).filter(pk=recording_id).exists():
            # The registry rows cascade with the Recording; nothing is
            # attributable to reconcile here (orphans are status/rebuild
            # territory by contract).
            counts["recording_missing"] = 1
            return counts
        _delete_noncanonical(recording_id, using, counts)
        _upsert_expected(recording_id, using, counts)
    return counts


def schedule_recording_sync(recording_ids: Sequence[str], *, using: str = "default") -> None:
    """Run per-recording reconciliation for the given ids AFTER the
    current transaction commits (immediately under plain autocommit).

    Ids are deduplicated within the call. Each recording reconciles
    independently; failures are swallowed per id and reported ONLY as one
    fixed aggregate warning with the failure count. This function never
    raises and never affects the committed authoritative operation."""
    ids = list(dict.fromkeys(str(pk) for pk in recording_ids if pk))
    if not ids:
        return

    def _sync_after_commit() -> None:
        failed = 0
        for recording_id in ids:
            try:
                reconcile_recording(recording_id, using=using)
            except Exception:
                failed += 1
        if failed:
            LOGGER.warning(_SYNC_FAILED_LOG, failed)

    transaction.on_commit(_sync_after_commit, using=using)


def _delete_noncanonical(recording_id: str, using: str, counts: dict) -> None:
    """Delete-phase: keyset-page (<= INSERT_CHUNK_SIZE) the recording's
    registry rows and remove every row the SHARED canonical-row
    validation rejects (deactivated provenance, cross-recording or
    mismatched provenance forgeries, malformed keys). FTS rows are
    deleted first (rowid = registry pk), then the registry rows."""
    connection = connections[using]
    last_id: int | None = None
    while True:
        page_query = SearchDocument.objects.using(using).filter(recording_id=recording_id)
        if last_id is not None:
            page_query = page_query.filter(id__gt=last_id)
        rows = list(page_query.order_by("id")[:INSERT_CHUNK_SIZE])
        if not rows:
            return
        last_id = rows[-1].pk
        expected = _expected_registry_page_is_canonical(using, rows)
        stale = [row.pk for row in rows if row.pk not in expected]
        if not stale:
            continue
        with connection.cursor() as cursor:
            _delete_fts_rows(cursor, stale)
        SearchDocument.objects.using(using).filter(pk__in=stale).delete()
        counts["deleted"] += len(stale)


def _upsert_expected(recording_id: str, using: str, counts: dict) -> None:
    """Upsert-phase: stream the expected specs in bounded chunks and
    reconcile registry + FTS with FULL-field comparison (content_hash
    alone is never trusted).

    FTS handling per spec is decided from the ACTUAL FTS text map (never
    a blind UPDATE): present-and-equal ⇒ skip; present-and-different ⇒
    UPDATE the existing rowid; absent ⇒ INSERT with the row's pk."""
    connection = connections[using]
    for specs in iter_expected_documents_for_recording_ids(
        [recording_id], using=using, chunk_size=INSERT_CHUNK_SIZE
    ):
        existing = {
            row.document_key: row
            for row in SearchDocument.objects.using(using).filter(
                document_key__in=[spec.document_key for spec in specs]
            )
        }
        fts_text: dict[int, tuple] = {}
        if existing:
            with connection.cursor() as cursor:
                fts_text = _read_fts_rows(cursor, [row.pk for row in existing.values()])

        fts_updates: list[tuple] = []
        fts_repairs: list[tuple] = []
        new_docs: list[SearchDocument] = []
        for spec in specs:
            row = existing.get(spec.document_key)
            if row is None:
                new_docs.append(document_from_spec(spec))
                continue
            expected_text = (spec.title_text, spec.body_text, spec.aux_text)
            current_text = fts_text.get(row.pk)
            registry_ok = registry_row_matches_spec(row, spec)
            if registry_ok and current_text == expected_text:
                counts["skipped"] += 1
                continue
            if not registry_ok:
                apply_spec_to_registry_row(row, spec)
                counts["updated"] += 1
            if current_text is None:
                # MISSING FTS row: INSERT with the EXISTING registry pk as
                # rowid (never UPDATE alone).
                fts_repairs.append((row.pk, spec.title_text, spec.body_text, spec.aux_text))
            elif current_text != expected_text:
                fts_updates.append((row.pk, spec.title_text, spec.body_text, spec.aux_text))

        if fts_updates:
            with connection.cursor() as cursor:
                _update_fts_batch(cursor, fts_updates)
            counts["fts_repaired"] += len(fts_updates)
        if fts_repairs:
            with connection.cursor() as cursor:
                _insert_fts_batch(cursor, fts_repairs, error=_SYNC_INSERT_ERROR)
            counts["fts_repaired"] += len(fts_repairs)
        if new_docs:
            created = SearchDocument.objects.using(using).bulk_create(new_docs)
            with connection.cursor() as cursor:
                _insert_fts_batch(
                    cursor,
                    [
                        (doc.pk, doc.title_text, doc.body_text, doc.aux_text)
                        for doc in created
                    ],
                    error=_SYNC_INSERT_ERROR,
                )
            counts["inserted"] += len(created)
