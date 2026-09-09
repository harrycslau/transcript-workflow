"""Incremental embedding index synchronization (Step 5B.4).

Keeps the ACTIVE ``EmbeddingGeneration``'s ``EmbeddingDocument`` rows
synchronized with the ``SearchDocument`` registry AFTER the Step 5A.3
search synchronization has reconciled that registry + FTS for a
recording. This module is the ONLY incremental ``EmbeddingDocument``
writer; the explicit status/rebuild/repair operations stay in
``workflow.services.embedding_index`` (Step 5B.3), which also owns the
mapping contract reused here verbatim (``prepare_document_text``,
``EMBEDDING_VERSION``, ``vector_codec.encode_vector`` and the
``embedding_client.embed_texts`` request path).

Orchestration (owned by ``search_sync.schedule_recording_sync``; this
module never schedules itself and never runs on GET):

1. ``capture_removed_key_snapshot`` — BEFORE the search reconciliation,
   snapshot the recording's current ``SearchDocument`` keys (keys only)
   into a connection-local SQLite TEMP table. Returns False (no
   snapshot) when no active embedding generation exists — embedding
   sync is then a normal no-op. Missing embedding schema / query
   failures raise a sanitized error (counted as an embedding-sync
   failure) and never stop the search reconciliation.
2. The search reconciliation runs.
3. Only if it SUCCEEDED, ``sync_recording_embeddings`` runs against the
   now-current SearchDocuments: it deletes vectors for snapshot keys
   the reconciliation removed (network-free, bounded short
   transactions, absence + active-compatibility rechecks), then
   classifies the recording's current SearchDocuments in deterministic
   ``document_key`` keyset pages bounded by the validated
   ``embedding.batch_size`` and embeds ONLY missing/stale/invalid rows
   (one HTTP request per non-empty batch, never inside a transaction;
   each write is one short transaction that re-reads the source key/hash
   and the active generation identity).

Failure contract: failures never escape the post-commit callback and
never affect the authoritative or search operation. Embedding failures
are counted per recording inside the callback and logged ONLY as one
fixed aggregate warning per callback (``embedding_index_sync_failed`` +
a count — no ids, model names, exception/code/text, paths, SQL,
document text, vectors or secrets). No automatic retries: a later
successful sync converges missing/stale work; an unattributable
embedding orphan (a vector whose SearchDocument was already gone before
the snapshot, e.g. after a prior failed deletion) is detected by
``brain embedding-index status`` and removed by explicit repair/rebuild
— there is no global callback sweep, retry, queue, daemon or background
job.

Concurrency: the sync never acquires the pipeline lock. Pipeline
callbacks normally run while the caller still holds the lock; unlocked
web tag callbacks rely on SQLite writer serialization. No HTTP is ever
made while ``connection.in_atomic_block`` is True (embedding calls
happen outside all DB transactions). Config is loaded FRESH inside the
callback via ``brainlib.config.load_config`` (never captured from
settings) and only when an active generation exists.
"""

from __future__ import annotations

import logging
from typing import Iterator

from django.db import Error as DjangoDBError
from django.db import connections, transaction

from brainlib.config import load_config
from workflow.models import (
    EmbeddingDocument,
    EmbeddingGeneration,
    EmbeddingGenerationState,
    SearchDocument,
)
from workflow.services import embedding_index as ei
from workflow.services import search_index
from workflow.services.embedding_client import EmbeddingError
from workflow.services.embedding_index import (
    EMBEDDING_VERSION,
    EmbeddingIndexError,
    _EMBEDDING_SCHEMA_MISSING,
    _INCOMPATIBLE_REQUIRES_REBUILD,
    _UNEXPECTED_ERROR,
    _active_is_compatible,
    _active_page_invalid,
    _effective_batch_size,
    _embedding_schema_present,
    _persist_repair_batch,
    _reject_in_atomic_block,
    _request_error,
    prepare_document_text,
)
from workflow.services.vector_codec import VectorCodecError

LOGGER = logging.getLogger(__name__)

# Fixed, sanitized log line ONLY — never ids, model names, exception
# text, paths, SQL, document text, vectors or secrets (AGENTS.md: log
# categories/counts, never values).
SYNC_FAILURE_CATEGORY = "embedding_index_sync_failed"
_EMBEDDING_SYNC_FAILED_LOG = (
    "embedding index post-commit sync failed category="
    + SYNC_FAILURE_CATEGORY
    + " count=%d"
)

# Connection-local SQLite TEMP snapshot of a recording's pre-reconcile
# SearchDocument keys (removed-key attribution). Fixed table name
# (TEMP SQL is static except parameters); TEMP tables are
# per-connection so concurrent unlocked callbacks never share one, and
# the table is dropped in ``finally`` per callback.
_TEMP_CREATE_SQL = (
    "CREATE TEMP TABLE IF NOT EXISTS brain_embedding_removed_keys "
    "(document_key TEXT NOT NULL PRIMARY KEY)"
)
_TEMP_CLEAR_SQL = "DELETE FROM brain_embedding_removed_keys"
_TEMP_DROP_SQL = "DROP TABLE IF EXISTS brain_embedding_removed_keys"
# The registry table name is pinned by the model/migration
# (db_table="workflow_search_document"); interpolating that fixed
# constant mirrors the pattern used by search_index/search_sync.
_REGISTRY_TABLE = SearchDocument._meta.db_table
_SNAPSHOT_INSERT_SQL = (
    "INSERT INTO brain_embedding_removed_keys (document_key) "
    f"SELECT document_key FROM {_REGISTRY_TABLE} WHERE recording_id = %s"
)

_REMOVED_PAGE_SIZE = 100  # bounded removed-key deletion pages


def capture_removed_key_snapshot(recording_id: str, *, using: str = "default") -> bool:
    """Capture the recording's current SearchDocument keys (keys only)
    into the connection-local TEMP table so a later sync can attribute
    keys that the search reconciliation removes.

    Returns True when a snapshot was captured (the caller should run the
    embedding sync after a successful search reconciliation). Returns
    False when no active embedding generation exists — embedding sync is
    then a normal no-op (no network, no log). Every public/runtime
    failure raises a fixed sanitized :class:`EmbeddingIndexError`
    (schema genuinely absent -> schema-missing; expected DB errors ->
    database-operation; anything else -> unexpected; never raw
    exception text) and NEVER stops the search reconciliation.
    """
    try:
        if not _embedding_schema_present(using=using):
            raise EmbeddingIndexError(_EMBEDDING_SCHEMA_MISSING)
        if not EmbeddingGeneration.objects.using(using).filter(
            state=EmbeddingGenerationState.ACTIVE
        ).exists():
            return False
        connection = connections[using]
        with connection.cursor() as cursor:
            cursor.execute(_TEMP_CREATE_SQL)
            cursor.execute(_TEMP_CLEAR_SQL)
            cursor.execute(_SNAPSHOT_INSERT_SQL, [recording_id])
        return True
    except EmbeddingIndexError:
        raise
    except DjangoDBError:
        raise EmbeddingIndexError(ei._DB_ERROR) from None
    except Exception:
        # Unexpected operational failure — never KeyboardInterrupt/
        # SystemExit (BaseException) and never raw details.
        raise EmbeddingIndexError(_UNEXPECTED_ERROR) from None


def clear_removed_key_snapshot(*, using: str = "default") -> None:
    """Drop the connection-local TEMP snapshot table (best effort).

    Called in ``finally`` so one synchronous callback never leaks its
    snapshot into another; failures here are deliberately swallowed —
    cleanup can never mask or become a sync failure.
    """
    try:
        connection = connections[using]
        with connection.cursor() as cursor:
            cursor.execute(_TEMP_DROP_SQL)
    except Exception:
        pass


def log_embedding_sync_failed(count: int) -> None:
    """Emit the ONE fixed aggregate embedding-sync failure warning."""
    LOGGER.warning(_EMBEDDING_SYNC_FAILED_LOG, count)


def sync_recording_embeddings(
    recording_id: str, *, using: str = "default", embedder=None
) -> dict:
    """Reconcile the ACTIVE compatible embedding generation with the
    now-current SearchDocuments of exactly ONE Recording.

    Requires ``capture_removed_key_snapshot`` to have run first on the
    same connection (the TEMP table carries the removed-key
    attribution). Refuses to run while already inside a caller SQLite
    transaction (shared 5B.3 fixed precondition — HTTP is never called
    while ``in_atomic_block``). No active generation, or an active
    generation incompatible with the freshly loaded config model /
    EMBEDDING_VERSION / INDEX_VERSION, is a normal no-op (zero DML, zero
    network, no log).
    Deletes vectors for removed snapshot keys BEFORE any network, then
    embeds only missing/stale/invalid current rows — one HTTP request
    per non-empty batch, never inside a DB transaction, each write in
    one short transaction that re-reads the source and active-generation
    identity (no false provenance on concurrent change/promotion).
    Partial progress is durable; the active generation is never marked
    failed.

    Returns safe counts only (never keys/content): ``embedded``,
    ``deleted_removed``, ``skipped``, ``batches``. Every public/runtime
    failure is raised as a fixed sanitized :class:`EmbeddingIndexError`;
    the caller (search_sync callback) catches it and counts it.
    """
    counts = {"embedded": 0, "deleted_removed": 0, "skipped": 0, "batches": 0}
    try:
        _reject_in_atomic_block(using)
        _sync_recording(recording_id, counts, using=using, embedder=embedder)
    except EmbeddingIndexError:
        raise
    except EmbeddingError as exc:
        raise EmbeddingIndexError(_request_error(exc.code)) from None
    except VectorCodecError as exc:
        raise EmbeddingIndexError(ei._vector_error(exc.code)) from None
    except DjangoDBError:
        raise EmbeddingIndexError(ei._DB_ERROR) from None
    except Exception:
        # Unexpected operational failure — never KeyboardInterrupt/
        # SystemExit (BaseException) and never raw details.
        raise EmbeddingIndexError(_UNEXPECTED_ERROR) from None
    return counts


def _sync_recording(
    recording_id: str, counts: dict, *, using: str, embedder
) -> None:
    if not _embedding_schema_present(using=using):
        raise EmbeddingIndexError(_EMBEDDING_SCHEMA_MISSING)
    active = (
        EmbeddingGeneration.objects.using(using)
        .filter(state=EmbeddingGenerationState.ACTIVE)
        .order_by("pk")
        .first()
    )
    if active is None:
        return  # no active generation: normal no-op
    # Config is loaded FRESH inside the callback (never captured from
    # settings) and only once an active generation exists.
    config = load_config()
    model = config.embedding.model
    if not (model or "").strip():
        return  # embedding model not configured: no-op
    if (
        active.model != model
        or active.embedding_version != EMBEDDING_VERSION
        or active.source_index_version != search_index.INDEX_VERSION
    ):
        return  # incompatible active generation: no-op (status/rebuild remedy)
    dimensions = active.dimensions
    batch_size = _effective_batch_size(config)
    if embedder is None:
        embedder = ei.embed_texts

    # Removed keys first: network-free, so an endpoint failure can never
    # preserve known orphans (provided the safety checks pass).
    counts["deleted_removed"] = _delete_removed_keys(active, using=using)
    _sync_current_pages(
        recording_id,
        active,
        config,
        embedder,
        batch_size,
        dimensions,
        using=using,
        counts=counts,
    )


def _sync_current_pages(
    recording_id: str,
    active: EmbeddingGeneration,
    config,
    embedder,
    batch_size: int,
    dimensions: int,
    *,
    using: str,
    counts: dict,
) -> None:
    """Stream the recording's current SearchDocuments by ``document_key``
    in pages no larger than the validated ``embedding.batch_size`` and
    embed/upsert ONLY the missing/stale/invalid rows (one HTTP request
    per non-empty batch, outside all transactions)."""
    last: str | None = None
    while True:
        query = SearchDocument.objects.using(using).filter(recording_id=recording_id)
        if last is not None:
            query = query.filter(document_key__gt=last)
        page = list(query.order_by("document_key")[:batch_size])
        if not page:
            return
        last = page[-1].document_key
        work = _classify_page(active, page, dimensions, using=using, counts=counts)
        if not work:
            continue
        embedded = embedder(config, [prepare_document_text(row) for row in work])
        # One short write transaction: re-reads every source key/hash AND
        # re-reads that the same generation pk remains the sole active
        # compatible generation before updating exactly the target rows.
        _persist_repair_batch(active, work, embedded, dimensions, using=using)
        counts["embedded"] += len(work)
        counts["batches"] += 1


def _classify_page(
    active: EmbeddingGeneration,
    page: list[SearchDocument],
    dimensions: int,
    *,
    using: str,
    counts: dict,
) -> list[SearchDocument]:
    """Bounded query of the page's matching active rows; classify each
    current row as correct (skip), missing (no active vector), stale
    (``source_content_hash`` differs) or invalid (malformed vector blob,
    classified via the shared 5B.3 length-first/chunked helper)."""
    keys = [row.document_key for row in page]
    existing_rows = list(
        EmbeddingDocument.objects.using(using)
        .filter(generation=active, document_key__in=keys)
        .only("id", "document_key", "source_content_hash")
    )
    existing = {row.document_key: row for row in existing_rows}
    invalid = set()
    if existing_rows:
        invalid = _active_page_invalid(existing_rows, dimensions, using=using)
    work: list[SearchDocument] = []
    for row in page:
        active_row = existing.get(row.document_key)
        if (
            active_row is None
            or active_row.source_content_hash != row.content_hash
            or active_row.pk in invalid
        ):
            work.append(row)
        else:
            counts["skipped"] += 1
    return work


def _delete_removed_keys(active: EmbeddingGeneration, *, using: str) -> int:
    """Delete active-generation vectors whose key is in the TEMP snapshot
    but no longer in the SearchDocument registry (removed by the search
    reconciliation). Bounded snapshot pages, short transactions, absence
    and active-compatibility rechecks; a key recreated by a concurrent
    SearchDocument write is never deleted."""
    deleted = 0
    for page in _iter_removed_key_pages(using=using):
        deleted += _delete_removed_key_page(active, page, using=using)
    return deleted


def _iter_removed_key_pages(*, using: str) -> Iterator[list[str]]:
    """Keyset-page the snapshot keys that currently have NO registry row
    (bounded application memory; no global sweep)."""
    connection = connections[using]
    last: str | None = None
    while True:
        sql = (
            "SELECT t.document_key FROM brain_embedding_removed_keys t "
            f"WHERE NOT EXISTS (SELECT 1 FROM {_REGISTRY_TABLE} d "
            "WHERE d.document_key = t.document_key)"
        )
        params: list = []
        if last is not None:
            sql += " AND t.document_key > %s"
            params.append(last)
        sql += " ORDER BY t.document_key LIMIT %s"
        params.append(_REMOVED_PAGE_SIZE)
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            page = [row[0] for row in cursor.fetchall()]
        if not page:
            return
        yield page
        last = page[-1]


def _delete_removed_key_page(
    active: EmbeddingGeneration, keys: list[str], *, using: str
) -> int:
    try:
        with transaction.atomic(using=using):
            if not _active_is_compatible(active, using=using):
                raise EmbeddingIndexError(_INCOMPATIBLE_REQUIRES_REBUILD)
            still_present = set(
                SearchDocument.objects.using(using)
                .filter(document_key__in=keys)
                .values_list("document_key", flat=True)
            )
            to_delete = [key for key in keys if key not in still_present]
            if not to_delete:
                return 0
            result = EmbeddingDocument.objects.using(using).filter(
                generation=active, document_key__in=to_delete
            ).delete()
            # Django's QuerySet.delete() returns (total_deleted, {label: count});
            # report the ACTUAL rows deleted, never a guessed key count.
            if isinstance(result, tuple) and result:
                return int(result[0])
            return 0
    except EmbeddingIndexError:
        raise
    except DjangoDBError:
        raise EmbeddingIndexError(ei._DB_ERROR) from None
