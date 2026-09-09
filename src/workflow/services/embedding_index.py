"""Bounded embedding index status/rebuild/repair (Step 5B.3).

Delivers the three bounded EXPLICIT operations over the Step 5B.2
generation store. This module owns ONLY the explicit
status/rebuild/repair operations; the incremental per-recording
embedding synchronization (Step 5B.4) lives in
``workflow.services.embedding_sync`` (which reuses the mapping helpers
defined here). No semantic retrieval, no web changes, no new
schema/migration/config keys:

- ``build_embedding_status_report(config, *, using='default')`` —
  strictly read-only (SELECT/PRAGMA only; never locks, never touches the
  network, never repairs or writes). It runs the existing complete
  ``search_index.build_status_report`` EXACTLY once and surfaces an
  unhealthy source index as the stable ``source_index_unhealthy``
  category. Integrity is computed for the ACTIVE generation only:
  failed/building/superseded generations are counted but their
  documents can never make a healthy active generation unhealthy.
- ``rebuild_embedding_index(config, *, using='default', embedder=...)``
  — the caller holds the pipeline lock (the service never acquires it)
  and must NOT already be inside a SQLite transaction (fixed
  precondition; HTTP is never called while ``in_atomic_block``).
  Preflights source search-index health once, streams ALL current
  ``SearchDocument`` rows deterministically by ``document_key`` in
  batches bounded by ``config.embedding.batch_size``, discovers the
  returned dimension from the first real batch (or one fixed synthetic
  probe for an empty source), persists one bounded short transaction
  per batch (rechecking each source key/hash inside the transaction),
  validates a deterministic rolling ``(document_key, content_hash)``
  snapshot plus a fresh complete source sweep and target-generation
  integrity before promotion, closes the final-validation/promotion
  race with a ``PRAGMA data_version`` guard and a write reservation,
  and promotes in ONE short transaction (verify the active generation
  identity, supersede the old active first, then activate the target;
  DB unique is the final guard). HEALTH IS ESTABLISHED BY THAT
  PRE-PROMOTION VALIDATION AND THE WRITE-LOCK GUARD AT THE PROMOTION
  COMMIT — after ``_promote`` succeeds the verified result is returned
  directly and there are NO post-promotion failure points. Any failure
  after generation creation marks the generation ``failed`` (best
  effort) and never touches the old active generation.
- ``repair_embedding_index(config, *, using='default', embedder=...)``
  — never called while already inside a SQLite transaction (fixed
  precondition); preflights source health, REQUIRES the current active
  generation to be exactly compatible with the configured model /
  EMBEDDING_VERSION / INDEX_VERSION (else a stable rebuild-required
  error), re-embeds missing/stale/invalid current keys (each row at
  most once, HTTP outside transactions, returned dimension must equal
  the active generation's), deletes active-generation orphans in
  bounded pages with an absence recheck, and only succeeds when the
  final status is healthy. Partial batch progress is durable; failures
  never mark the active generation failed.

Mapping contract (bound to ``EMBEDDING_VERSION``): text preparation is
the pure ``prepare_document_text`` helper over an existing
``SearchDocument`` row (rows are NEVER reconstructed here —
``SearchDocument`` is the immediate source and its ``content_hash`` /
``search_index.INDEX_VERSION`` provenance are copied verbatim into
``EmbeddingDocument.source_content_hash`` / ``source_index_version``).
The configured embedding model is the EXACT configured/canonical
string (5B.1 sends it verbatim): blankness is tested with ``.strip()``
but the stored/expected/comparison identity is NEVER stripped or
otherwise canonicalized.

Safety: every error is a sanitized :class:`EmbeddingIndexError`
(subclass of ``ConfigError`` so the CLI exits 1 without a traceback)
with fixed static messages; document text, vectors, response bodies,
API keys, SQL, paths and raw exception text never appear. ALL
unexpected operational ``Exception`` failures (not
``KeyboardInterrupt``/``SystemExit``) are converted to a fixed
sanitized error at the public service boundaries. This module emits no
logs.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterator

from django.db import Error as DjangoDBError
from django.db import connections, transaction
from django.db.models import Count
from django.utils import timezone

from brainlib.config import ConfigError
from workflow.models import (
    EmbeddingDocument,
    EmbeddingGeneration,
    EmbeddingGenerationState,
    SearchDocument,
)
from workflow.services import search_index
from workflow.services.embedding_client import (
    HARD_MAX_BATCH,
    EmbeddingError,
    embed_texts,
)
from workflow.services.vector_codec import (
    MAX_DIMENSION,
    VectorCodecError,
    encode_vector,
    validate_vector_blob,
)

# The FULL production embedding mapping contract: deterministic text
# preparation (``prepare_document_text``, v1 format below) PLUS the
# vector mapping (raw little-endian IEEE-754 float32 via
# ``vector_codec.encode_vector``). Distinct from
# ``search_index.INDEX_VERSION`` (the SearchDocument index contract).
# ANY change to the text format, field order, or vector encoding MUST
# bump this constant: a generation is only reusable when its
# ``embedding_version`` equals this value.
EMBEDDING_VERSION = "1"

# Fixed synthetic dimension probe used ONLY when the source search index
# has zero current documents (an empty corpus still needs a concrete
# dimension for the empty building generation). Deliberately
# non-sensitive, fixed, and documented.
SYNTHETIC_DIMENSION_PROBE = "brain embedding dimension probe"

# Stable report categories (never renamed silently).
CATEGORIES = (
    "schema_missing",
    "source_index_unhealthy",
    "model_not_configured",
    "no_active_generation",
    "model_mismatch",
    "embedding_version_mismatch",
    "source_index_version_mismatch",
    "missing_document",
    "stale_content",
    "orphan_document",
    "invalid_vector",
)

KEY_LIMIT = 20  # identifier samples per category (exact omitted count kept)
_STATUS_PAGE_SIZE = 500  # keyset page for status/repair classification
_VALIDATION_PAGE_SIZE = 500  # keyset page for pre-promotion validation
_BLOB_CHECK_CHUNK = 50  # bounded decode chunk for exact-length vector blobs

# Fixed sanitized messages — never interpolate SQL, paths, indexed
# text, vectors, response bodies or raw exception details.
_SOURCE_INDEX_UNHEALTHY = (
    "the search index is not healthy; run 'brain search-index status' "
    "and repair it with 'brain search-index rebuild' first"
)
_EMBEDDING_SCHEMA_MISSING = (
    "the embedding index schema is missing; apply pending migrations with: "
    "uv run python src/manage.py migrate"
)
_MODEL_NOT_CONFIGURED = (
    "no embedding model is configured; set the 'embedding.model' configuration value"
)
_NO_ACTIVE_REQUIRES_REBUILD = (
    "no active embedding generation exists; run 'brain embedding-index rebuild'"
)
_INCOMPATIBLE_REQUIRES_REBUILD = (
    "the active embedding generation is incompatible with the configured model or "
    "versions; run 'brain embedding-index rebuild'"
)
_PAIRING_ERROR = "the embedding response does not match the requested texts"
_DIMENSION_ERROR = "embedding dimensions are inconsistent"
_DIMENSION_CHANGED_REQUIRES_REBUILD = (
    "the embedding endpoint returned a different dimension than the active "
    "generation; run 'brain embedding-index rebuild'"
)
_SOURCE_CHANGED = (
    "the search index changed while building; the generation was not completed"
)
_SNAPSHOT_MISMATCH = (
    "the search index changed during building; the generation was not completed"
)
_GENERATION_INTEGRITY = "the built generation failed integrity verification"
_PROMOTION_RACE = (
    "the search index changed concurrently; the generation was not promoted"
)
_PROMOTION_FAILED = "the generation could not be promoted"
_VECTOR_ERROR = "embedding vectors could not be encoded"
_DB_ERROR = "the embedding index database operation failed"
_REPAIR_NOT_CONVERGED = (
    "the embedding index is not fully healthy after repair; run "
    "'brain embedding-index status' to inspect"
)
_UNRESOLVED = (
    "the search index changed while repairing; the affected vectors were not updated"
)
_BATCH_CONFIG_ERROR = "embedding.batch_size must be a positive integer"
_BATCH_TOO_LARGE_ERROR = "embedding.batch_size must not exceed 128"
_IN_ATOMIC_BLOCK_ERROR = (
    "embedding requests must not be made inside a database transaction"
)
_UNEXPECTED_ERROR = (
    "the embedding index operation failed unexpectedly; run "
    "'brain embedding-index status' to inspect"
)
_STATUS_ERROR = "the embedding index status could not be computed"
_MAPPING_ERROR = "an embedding document has malformed text fields"
_SNAPSHOT_FRAME_ERROR = "an embedding snapshot contains a malformed document identity"


class EmbeddingIndexError(ConfigError):
    """Sanitized, actionable embedding-index failure (CLI exit 1)."""


# Known stable 5B.1 embedding-client error codes (the class-level
# ``EmbeddingError.code`` values and the fine ``EmbeddingInvalid.code``
# values). Exception ``.code`` interpolation is ALLOWLISTED: an unknown
# or hostile custom-subclass code maps to the fixed generic category,
# never echoed verbatim.
_KNOWN_EMBEDDING_CODES = frozenset(
    {
        "embedding_error",
        "model_not_configured",
        "endpoint_not_local",
        "invalid_input",
        "batch_too_large",
        "request_too_large",
        "endpoint_unavailable",
        "timeout",
        "http_error",
        "response_too_large",
        "malformed_http_json",
        "invalid_envelope",
        "invalid_vector",
        "dimension_mismatch",
        "invalid_encoding",
    }
)

# Known stable vector-codec codes.
_KNOWN_CODEC_CODES = frozenset({"invalid_dimension", "invalid_values", "invalid_blob"})

_GENERIC_EMBEDDING_CODE = "embedding_error"
_GENERIC_CODEC_CODE = "vector_codec_error"


def _safe_embedding_code(code) -> str:
    if isinstance(code, str) and code in _KNOWN_EMBEDDING_CODES:
        return code
    return _GENERIC_EMBEDDING_CODE


def _safe_codec_code(code) -> str:
    if isinstance(code, str) and code in _KNOWN_CODEC_CODES:
        return code
    return _GENERIC_CODEC_CODE


def _request_error(category) -> str:
    return (
        f"embedding request failed ({_safe_embedding_code(category)}); "
        "run 'brain embedding-index status' to inspect"
    )


def _vector_error(category) -> str:
    return f"{_VECTOR_ERROR} ({_safe_codec_code(category)})"


# ---------------------------------------------------------------------------
# Deterministic text preparation (v1 mapping contract)
# ---------------------------------------------------------------------------


def prepare_document_text(doc) -> str:
    """Produce the exact plain embedding payload for one SearchDocument row.

    v1 mapping contract (``EMBEDDING_VERSION``)::

        brain-embedding-v1
        doc_type:<len>:<doc_type>
        title_text:<len>:<title_text>
        body_text:<len>:<body_text>
        aux_text:<len>:<aux_text>

    where ``<len>`` is the UTF-8 byte length of the field value and
    ``<field>`` is the raw stored value — NEVER truncated or coerced;
    empty fields appear as ``<label>:0:``. The length prefixes keep the
    payload unambiguous for arbitrary multi-line/duplicate-label text,
    the leading version marker binds the payload to the mapping
    contract, and ``doc_type`` (plus the marker) keeps every payload
    non-empty. ``SearchDocument`` rows are the immediate source: this
    helper NEVER reconstructs documents; their ``content_hash`` and
    ``search_index.INDEX_VERSION`` provenance are copied unchanged into
    ``EmbeddingDocument.source_content_hash`` / ``source_index_version``.
    Any change to this exact format or field order MUST bump
    ``EMBEDDING_VERSION``.

    All four fields (``doc_type``/``title_text``/``body_text``/
    ``aux_text``) must be exact ``str`` — malformed rows raise one fixed
    sanitized :class:`EmbeddingIndexError` and the arbitrary ``__str__``
    of a hostile value is NEVER invoked.
    """
    fields = (
        ("doc_type", getattr(doc, "doc_type", None)),
        ("title_text", getattr(doc, "title_text", None)),
        ("body_text", getattr(doc, "body_text", None)),
        ("aux_text", getattr(doc, "aux_text", None)),
    )
    for label, value in fields:
        if type(value) is not str:
            raise EmbeddingIndexError(_MAPPING_ERROR)
    framed = "\n".join(
        f"{label}:{len(value.encode('utf-8'))}:{value}" for label, value in fields
    )
    return f"brain-embedding-v{EMBEDDING_VERSION}\n{framed}"


# ---------------------------------------------------------------------------
# Bounded streaming helpers (keyset-paged, deterministic document_key order)
# ---------------------------------------------------------------------------


def _chunks(items: list, size: int) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _iter_search_document_pages(*, using: str, page_size: int) -> Iterator[list[SearchDocument]]:
    """Current ``SearchDocument`` rows ordered by ``document_key`` in
    fixed-size pages (no lower-bound on the first page)."""
    last: str | None = None
    while True:
        qs = SearchDocument.objects.using(using)
        if last is not None:
            qs = qs.filter(document_key__gt=last)
        page = list(qs.order_by("document_key")[:page_size])
        if not page:
            return
        yield page
        last = page[-1].document_key


def _iter_active_document_pages(
    generation_id: int, *, using: str, page_size: int
) -> Iterator[list[EmbeddingDocument]]:
    """Active-generation documents ordered by ``document_key`` in
    fixed-size pages; vector blobs are NEVER loaded (only the identity
    and provenance columns)."""
    last: str | None = None
    while True:
        qs = EmbeddingDocument.objects.using(using).filter(generation_id=generation_id)
        if last is not None:
            qs = qs.filter(document_key__gt=last)
        page = list(
            qs.order_by("document_key")
            .only("id", "document_key", "source_content_hash")[:page_size]
        )
        if not page:
            return
        yield page
        last = page[-1].document_key


def _active_page_invalid(page: list[EmbeddingDocument], dimensions: int, using: str) -> set[int]:
    """Pks of an active-document page whose vector blob is malformed
    (wrong byte length or non-finite) WITHOUT loading oversized blobs:
    SQLite ``length()`` classifies first, and only exact-length blobs
    are fetched/decoded, in bounded chunks."""
    if not page:
        return set()
    ids = [d.pk for d in page]
    table = EmbeddingDocument._meta.db_table
    lengths: dict[int, int] = {}
    with connections[using].cursor() as cursor:
        placeholders = ", ".join(["%s"] * len(ids))
        cursor.execute(
            f"SELECT id, length(vector_blob) FROM {table} WHERE id IN ({placeholders})",
            ids,
        )
        lengths = dict(cursor.fetchall())
    expected = dimensions * 4
    invalid = {pk for pk in ids if lengths.get(pk) != expected}
    ok_ids = [pk for pk in ids if lengths.get(pk) == expected]
    for chunk in _chunks(ok_ids, _BLOB_CHECK_CHUNK):
        with connections[using].cursor() as cursor:
            placeholders = ", ".join(["%s"] * len(chunk))
            cursor.execute(
                f"SELECT id, vector_blob FROM {table} WHERE id IN ({placeholders})",
                chunk,
            )
            for pk, blob in cursor.fetchall():
                try:
                    validate_vector_blob(blob, dimensions=dimensions)
                except VectorCodecError:
                    invalid.add(pk)
    return invalid


def _iter_classified(active: EmbeddingGeneration, using: str, page_size: int) -> Iterator[tuple]:
    """Bounded two-stream merge over the active generation's documents
    and the current ``SearchDocument`` rows (both ordered by
    ``document_key``), yielding classification events:

    - ``("orphan", key)`` — an active vector whose key has NO current
      SearchDocument;
    - ``("work", category, current_row)`` with ``category`` one of
      ``missing_document`` (current key with no active vector),
      ``stale_content`` (active ``source_content_hash`` differs) or
      ``invalid_vector`` (matched active vector is malformed).

    Memory is bounded by the page size on both streams; exact counts
    are accumulated by the consumer without any unbounded set.
    """
    active_pages = _iter_active_document_pages(active.pk, using=using, page_size=page_size)
    current_pages = _iter_search_document_pages(using=using, page_size=page_size)
    apage = next(active_pages, None)
    cpage = next(current_pages, None)
    ai = ci = 0
    invalid: set[int] = set()
    while apage is not None or cpage is not None:
        if apage is not None and cpage is not None:
            if ai == 0:
                invalid = _active_page_invalid(apage, active.dimensions, using)
            akey = apage[ai].document_key
            ckey = cpage[ci].document_key
            if akey == ckey:
                arow = apage[ai]
                crow = cpage[ci]
                if crow.content_hash != arow.source_content_hash:
                    yield ("work", "stale_content", crow)
                elif arow.pk in invalid:
                    yield ("work", "invalid_vector", crow)
                ai += 1
                ci += 1
            elif akey < ckey:
                yield ("orphan", apage[ai].document_key)
                ai += 1
            else:
                yield ("work", "missing_document", cpage[ci])
                ci += 1
        elif apage is not None:
            yield ("orphan", apage[ai].document_key)
            ai += 1
        else:
            yield ("work", "missing_document", cpage[ci])
            ci += 1
        if apage is not None and ai >= len(apage):
            apage = next(active_pages, None)
            ai = 0
        if cpage is not None and ci >= len(cpage):
            cpage = next(current_pages, None)
            ci = 0


# ---------------------------------------------------------------------------
# Schema inspection (read-only)
# ---------------------------------------------------------------------------


def _embedding_schema_present(*, using: str) -> bool:
    """True iff both embedding tables exist.

    Genuine table ABSENCE returns False (the normal ``schema_missing``
    report / ``_EMBEDDING_SCHEMA_MISSING`` error). An introspection or
    query FAILURE is never misreported as absence: it propagates to the
    calling public boundary (status -> fixed ``_STATUS_ERROR``,
    rebuild/repair -> fixed ``_UNEXPECTED_ERROR``).
    """
    connection = connections[using]
    with connection.cursor() as cursor:
        names = set(connection.introspection.table_names(cursor))
    return {
        EmbeddingGeneration._meta.db_table,
        EmbeddingDocument._meta.db_table,
    } <= names


def _search_registry_present(*, using: str) -> bool:
    """True iff the SearchDocument registry table exists.

    Same contract as ``_embedding_schema_present``: genuine absence
    returns False, introspection/query failures propagate.
    """
    connection = connections[using]
    with connection.cursor() as cursor:
        names = set(connection.introspection.table_names(cursor))
    return SearchDocument._meta.db_table in names


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


# ---------------------------------------------------------------------------
# Read-only status report
# ---------------------------------------------------------------------------


class _Tally:
    def __init__(self) -> None:
        self.counts = {category: 0 for category in CATEGORIES}
        self.keys: dict[str, list[str]] = {}
        self.truncated: dict[str, int] = {}

    def add(self, category: str, identifier: str) -> None:
        self.counts[category] += 1
        listed = self.keys.setdefault(category, [])
        if len(listed) < KEY_LIMIT:
            listed.append(identifier)
        else:
            self.truncated[category] = self.truncated.get(category, 0) + 1


def build_embedding_status_report(config, *, using: str = "default") -> dict:
    """Strictly read-only embedding index status (SELECT/PRAGMA only).

    Runs ``search_index.build_status_report(using=...)`` EXACTLY once and
    surfaces an unhealthy source index as ``source_index_unhealthy``
    (never repairs or rebuilds it). Reports structural table presence,
    the active generation's safe identity/timestamps/count metadata, the
    configured expected model / EMBEDDING_VERSION / INDEX_VERSION
    compatibility, and EXACT counts of the active generation versus all
    current SearchDocuments: missing keys, stale ``source_content_hash``,
    orphan keys and invalid vectors (wrong byte length or non-finite,
    classified without loading oversized blobs). Failed/building/
    superseded generations are counted but their documents never make a
    healthy active generation unhealthy. There is no configured
    dimensions value, so a same-name server dimension change is NOT
    detectable here — active dimensions are validated by the DB bounds
    and the vector blobs. Healthy iff the source index is healthy, the
    model is configured, exactly one compatible active generation exists
    and there are zero missing/stale/orphan/invalid active documents.

    The configured model identity is EXACT (never stripped or
    canonicalized; blankness is tested with ``.strip()`` only). Any
    structural/query/blob failure inside the internals raises a fixed
    sanitized :class:`EmbeddingIndexError` instead of misleading
    healthy/count output or a traceback; a genuinely missing embedding
    schema still reports the normal ``schema_missing`` category.
    """
    tally = _Tally()
    try:
        return _status_report(config, using=using, tally=tally)
    except EmbeddingIndexError:
        raise
    except Exception:
        raise EmbeddingIndexError(_STATUS_ERROR) from None


def _status_report(config, *, using: str, tally: _Tally) -> dict:
    source_report = search_index.build_status_report(using=using)
    if not source_report.get("healthy"):
        tally.add("source_index_unhealthy", "search-index")

    schema_present = _embedding_schema_present(using=using)
    if not schema_present:
        tally.add("schema_missing", "embedding")

    # Exact configured model identity: blankness via strip(), never
    # stripping the stored/expected/comparison value.
    raw_model = config.embedding.model
    model_configured = bool((raw_model or "").strip())
    if not model_configured:
        tally.add("model_not_configured", "embedding.model")

    generations = {state: 0 for state in EmbeddingGenerationState.values}
    generations["total"] = 0
    documents = {"total": 0, "active": 0}
    active = None
    active_info = None

    if schema_present:
        state_counts = dict(
            EmbeddingGeneration.objects.using(using)
            .values_list("state")
            .annotate(total=Count("pk"))
        )
        for state, count in state_counts.items():
            generations[state] = count
        generations["total"] = sum(state_counts.values())
        documents["total"] = EmbeddingDocument.objects.using(using).count()
        active = EmbeddingGeneration.objects.using(using).filter(
            state=EmbeddingGenerationState.ACTIVE
        ).first()

    if active is not None:
        documents["active"] = EmbeddingDocument.objects.using(using).filter(
            generation=active
        ).count()
        active_info = {
            "id": active.pk,
            "model": active.model,
            "dimensions": active.dimensions,
            "embedding_version": active.embedding_version,
            "source_index_version": active.source_index_version,
            "state": active.state,
            "created_at": _iso(active.created_at),
            "completed_at": _iso(active.completed_at),
            "activated_at": _iso(active.activated_at),
            "superseded_at": _iso(active.superseded_at),
            "failed_at": _iso(active.failed_at),
        }
        if model_configured and active.model != raw_model:
            tally.add("model_mismatch", active.model)
        if active.embedding_version != EMBEDDING_VERSION:
            tally.add("embedding_version_mismatch", active.embedding_version)
        if active.source_index_version != search_index.INDEX_VERSION:
            tally.add("source_index_version_mismatch", active.source_index_version)
        if _search_registry_present(using=using):
            for event in _iter_classified(active, using, _STATUS_PAGE_SIZE):
                if event[0] == "orphan":
                    tally.add("orphan_document", event[1])
                else:
                    tally.add(event[1], event[2].document_key)
    elif schema_present:
        tally.add("no_active_generation", "embedding")

    counts = {
        "current_search_documents": SearchDocument.objects.using(using).count(),
        "active_documents": documents["active"],
        "missing_document": tally.counts["missing_document"],
        "stale_content": tally.counts["stale_content"],
        "orphan_document": tally.counts["orphan_document"],
        "invalid_vector": tally.counts["invalid_vector"],
    }

    return {
        "healthy": not any(tally.counts.values()),
        "embedding_version": EMBEDDING_VERSION,
        "source_index_version": search_index.INDEX_VERSION,
        "expected": {
            "model": raw_model or None,
            "embedding_version": EMBEDDING_VERSION,
            "source_index_version": search_index.INDEX_VERSION,
        },
        "schema": {"present": schema_present},
        "model": {"configured": model_configured, "name": raw_model or None},
        "generations": generations,
        "documents": documents,
        "active_generation": active_info,
        "source_index": source_report,
        "counts": counts,
        "categories": dict(tally.counts),
        "keys": {k: v for k, v in tally.keys.items() if v},
        "keys_truncated": dict(tally.truncated),
    }


# ---------------------------------------------------------------------------
# Shared preflights and transaction helpers
# ---------------------------------------------------------------------------


def _reject_in_atomic_block(using: str) -> None:
    """Fixed precondition: public rebuild/repair must never run HTTP while
    already inside a caller SQLite transaction."""
    if connections[using].in_atomic_block:
        raise EmbeddingIndexError(_IN_ATOMIC_BLOCK_ERROR)


def _now():
    return timezone.now()


def _preflight_source_index(using: str) -> None:
    report = search_index.build_status_report(using=using)
    if not report.get("healthy"):
        raise EmbeddingIndexError(_SOURCE_INDEX_UNHEALTHY)


def _require_embedding_schema(using: str) -> None:
    if not _embedding_schema_present(using=using):
        raise EmbeddingIndexError(_EMBEDDING_SCHEMA_MISSING)


def _effective_batch_size(config) -> int:
    batch_size = config.embedding.batch_size
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise EmbeddingIndexError(_BATCH_CONFIG_ERROR)
    if batch_size > HARD_MAX_BATCH:
        raise EmbeddingIndexError(_BATCH_TOO_LARGE_ERROR)
    return batch_size


def _pragma_data_version(using: str) -> int:
    with connections[using].cursor() as cursor:
        cursor.execute("PRAGMA data_version")
        row = cursor.fetchone()
    return row[0] if row is not None else 0


def _active_generation_id(using: str) -> int | None:
    active = EmbeddingGeneration.objects.using(using).filter(
        state=EmbeddingGenerationState.ACTIVE
    ).first()
    return active.pk if active is not None else None


def _create_generation(config, dimensions: int, using: str) -> EmbeddingGeneration:
    try:
        with transaction.atomic(using=using):
            # Exact configured model identity — never stripped.
            return EmbeddingGeneration.objects.using(using).create(
                model=config.embedding.model,
                dimensions=dimensions,
                embedding_version=EMBEDDING_VERSION,
                source_index_version=search_index.INDEX_VERSION,
            )
    except DjangoDBError:
        raise EmbeddingIndexError(_DB_ERROR) from None


def _mark_generation_failed(generation_id: int, using: str) -> None:
    """Best-effort short transaction marking a building generation
    ``failed`` (leaves bounded partial documents). Never masks the
    original failure; never touches the old active generation. ANY
    failure here (including a broken DB write) is deliberately swallowed
    so the original sanitized failure always propagates; a swallowed
    mark leaves a detectable ``building`` generation that status
    reports."""
    try:
        now = _now()
        with transaction.atomic(using=using):
            EmbeddingGeneration.objects.using(using).filter(
                pk=generation_id, state=EmbeddingGenerationState.BUILDING
            ).update(
                state=EmbeddingGenerationState.FAILED,
                failed_at=now,
            )
    except Exception:
        pass


def _snapshot_frame(document_key: str, content_hash: str) -> bytes:
    """Deterministic length-prefixed UTF-8 framing for ONE exact
    ``(document_key, content_hash)`` pair, in the same unambiguous
    ``S<len>:<value>`` style as ``search_index.frame_parts``:
    ``S<len(key)>:<key>S<len(hash)>:<hash>`` (byte lengths, UTF-8).

    The length prefixes make delimiter/newline/unicode values (and
    hash-like text under tampering) unambiguous and guarantee that two
    adjacent pairs can never be confused with a boundary-shifted split.
    Both values must be EXACT ``str`` — anything else raises one fixed
    sanitized :class:`EmbeddingIndexError` (the calling public boundary
    sanitizes all failures). This ONE framing is shared byte-identically
    by the processing-time source snapshot and the pre-promotion
    generation-integrity snapshot.
    """
    if type(document_key) is not str or type(content_hash) is not str:
        raise EmbeddingIndexError(_SNAPSHOT_FRAME_ERROR)
    out = bytearray()
    for value in (document_key, content_hash):
        data = value.encode("utf-8")
        out += b"S" + str(len(data)).encode("ascii") + b":" + data
    return bytes(out)


def _snapshot_update(hasher: Any, page) -> None:
    for doc in page:
        hasher.update(_snapshot_frame(doc.document_key, doc.content_hash))


# ---------------------------------------------------------------------------
# Atomic rebuild
# ---------------------------------------------------------------------------


def rebuild_embedding_index(
    config, *, using: str = "default", embedder=embed_texts
) -> dict:
    """Rebuild the embedding index from ALL current SearchDocuments.

    The caller holds the pipeline lock; this service never acquires it
    and refuses to run while already inside a caller SQLite transaction
    (HTTP is never called with ``in_atomic_block``). Every HTTP call
    happens OUTSIDE any SQLite transaction (one request per batch, no
    retries, no network inside a transaction). The first real batch
    discovers the returned dimension (no extra probe); an empty source
    uses the fixed synthetic probe. Partial batches are durable per
    short write transaction; the old active generation is untouched
    until the ONE short promotion transaction (verify the active
    generation identity, supersede the old active first, then activate
    the target). HEALTH IS FULLY ESTABLISHED BY THE PRE-PROMOTION
    VALIDATION AND THE ``data_version`` WRITE-LOCK GUARD AT THE
    PROMOTION COMMIT: after promotion succeeds the verified result is
    returned directly — there are NO post-promotion failure points.
    The configured model identity is EXACT (blankness via ``.strip()``
    only; never stripped or canonicalized).
    """
    _reject_in_atomic_block(using)
    model = config.embedding.model
    if not (model or "").strip():
        raise EmbeddingIndexError(_MODEL_NOT_CONFIGURED)
    batch_size = _effective_batch_size(config)

    generation_id: int | None = None
    dimensions: int | None = None
    documents = 0
    batches = 0
    prior_active_id: int | None = None
    snapshot = hashlib.sha256()

    try:
        # Preflight + schema validation live INSIDE the sanitizing try:
        # an unexpected search-helper/DB exception here must become a
        # fixed sanitized error, never leak raw. The source preflight
        # still runs before ANY network/write.
        _preflight_source_index(using)
        _require_embedding_schema(using)
        pages = _iter_search_document_pages(using=using, page_size=batch_size)
        first = next(pages, None)
        if first is not None:
            embedded = embedder(config, [prepare_document_text(d) for d in first])
            dimensions = _first_batch_dimensions(first, embedded)
            prior_active_id = _active_generation_id(using)
            generation = _create_generation(config, dimensions, using)
            generation_id = generation.pk
            _persist_batch(generation, first, embedded, dimensions, using)
            _snapshot_update(snapshot, first)
            documents += len(first)
            batches += 1
        else:
            embedded = embedder(config, [SYNTHETIC_DIMENSION_PROBE])
            dimensions = _probe_dimensions(embedded)
            prior_active_id = _active_generation_id(using)
            generation = _create_generation(config, dimensions, using)
            generation_id = generation.pk
            batches += 1

        for page in pages:
            embedded = embedder(config, [prepare_document_text(d) for d in page])
            _persist_batch(generation, page, embedded, dimensions, using)
            _snapshot_update(snapshot, page)
            documents += len(page)
            batches += 1

        data_version_before = _pragma_data_version(using)
        _validate_before_promotion(generation, snapshot, dimensions, using)
        _promote(generation, prior_active_id, data_version_before, using)
    except EmbeddingIndexError:
        if generation_id is not None:
            _mark_generation_failed(generation_id, using)
        raise
    except EmbeddingError as exc:
        if generation_id is not None:
            _mark_generation_failed(generation_id, using)
        raise EmbeddingIndexError(_request_error(exc.code)) from None
    except VectorCodecError as exc:
        if generation_id is not None:
            _mark_generation_failed(generation_id, using)
        raise EmbeddingIndexError(_vector_error(exc.code)) from None
    except DjangoDBError:
        if generation_id is not None:
            _mark_generation_failed(generation_id, using)
        raise EmbeddingIndexError(_DB_ERROR) from None
    except Exception:
        # Unexpected operational failure: mark a created generation failed
        # best-effort (never the old active) and sanitize — but never
        # swallow KeyboardInterrupt/SystemExit (BaseException).
        if generation_id is not None:
            _mark_generation_failed(generation_id, using)
        raise EmbeddingIndexError(_UNEXPECTED_ERROR) from None

    return {
        "result": "rebuilt",
        "embedding_version": EMBEDDING_VERSION,
        "generation_id": generation_id,
        "model": model,
        "dimensions": dimensions,
        "documents": documents,
        "batches": batches,
        "prior_active_generation": prior_active_id,
        "healthy": True,
    }


def _first_batch_dimensions(page, embedded) -> int:
    if len(page) != len(embedded):
        raise EmbeddingIndexError(_PAIRING_ERROR)
    dimensions: int | None = None
    for doc, batch in zip(page, embedded):
        if type(batch.text) is not str or batch.text != prepare_document_text(doc):
            raise EmbeddingIndexError(_PAIRING_ERROR)
        if len(batch.embedding) < 1 or len(batch.embedding) > MAX_DIMENSION:
            raise EmbeddingIndexError(_DIMENSION_ERROR)
        if dimensions is None:
            dimensions = len(batch.embedding)
        elif len(batch.embedding) != dimensions:
            raise EmbeddingIndexError(_DIMENSION_ERROR)
    if dimensions is None:
        raise EmbeddingIndexError(_PAIRING_ERROR)
    return dimensions


def _probe_dimensions(embedded) -> int:
    if len(embedded) != 1:
        raise EmbeddingIndexError(_PAIRING_ERROR)
    batch = embedded[0]
    if type(batch.text) is not str or batch.text != SYNTHETIC_DIMENSION_PROBE:
        raise EmbeddingIndexError(_PAIRING_ERROR)
    dimensions = len(batch.embedding)
    if dimensions < 1 or dimensions > MAX_DIMENSION:
        raise EmbeddingIndexError(_DIMENSION_ERROR)
    return dimensions


def _persist_batch(generation, page, embedded, dimensions: int, using: str) -> None:
    """Validate exact client cardinality/text pairing and one consistent
    returned dimension, encode with the vector codec, then persist ONE
    bounded short transaction that rechecks each source key/content_hash
    before inserting — a changed/missing source aborts the generation
    instead of writing false provenance."""
    if len(page) != len(embedded):
        raise EmbeddingIndexError(_PAIRING_ERROR)
    payload: dict[str, tuple[str, bytes]] = {}
    for doc, batch in zip(page, embedded):
        if type(batch.text) is not str or batch.text != prepare_document_text(doc):
            raise EmbeddingIndexError(_PAIRING_ERROR)
        if len(batch.embedding) != dimensions:
            raise EmbeddingIndexError(_DIMENSION_ERROR)
        try:
            blob = encode_vector(batch.embedding, dimensions=dimensions)
        except VectorCodecError:
            raise EmbeddingIndexError(_VECTOR_ERROR) from None
        payload[doc.document_key] = (doc.content_hash, blob)
    try:
        with transaction.atomic(using=using):
            current = {
                row.document_key: row.content_hash
                for row in SearchDocument.objects.using(using).filter(
                    document_key__in=list(payload)
                )
            }
            rows = []
            for key, (content_hash, blob) in payload.items():
                if current.get(key) != content_hash:
                    raise EmbeddingIndexError(_SOURCE_CHANGED)
                rows.append(
                    EmbeddingDocument(
                        generation_id=generation.pk,
                        document_key=key,
                        source_content_hash=content_hash,
                        vector_blob=blob,
                    )
                )
            EmbeddingDocument.objects.using(using).bulk_create(rows)
    except EmbeddingIndexError:
        raise
    except DjangoDBError:
        raise EmbeddingIndexError(_DB_ERROR) from None


def _validate_before_promotion(
    generation, snapshot: Any, dimensions: int, using: str
) -> None:
    """Fresh complete source health sweep, complete current
    SearchDocument key/hash snapshot equality, and target generation
    integrity (exact key/hash set plus vector dimensions/finiteness) —
    all in bounded reads, no network, no full-scan accumulation."""
    source_report = search_index.build_status_report(using=using)
    if not source_report.get("healthy"):
        raise EmbeddingIndexError(_SOURCE_INDEX_UNHEALTHY)

    fresh = hashlib.sha256()
    for page in _iter_search_document_pages(using=using, page_size=_VALIDATION_PAGE_SIZE):
        _snapshot_update(fresh, page)
    if fresh.hexdigest() != snapshot.hexdigest():
        raise EmbeddingIndexError(_SNAPSHOT_MISMATCH)

    generation_hash = hashlib.sha256()
    for page in _iter_active_document_pages(
        generation.pk, using=using, page_size=_VALIDATION_PAGE_SIZE
    ):
        for doc in page:
            # Byte-identical framing to the source snapshot: the
            # generation rows carry the copied source content_hash.
            generation_hash.update(
                _snapshot_frame(doc.document_key, doc.source_content_hash)
            )
        if _active_page_invalid(page, dimensions, using):
            raise EmbeddingIndexError(_GENERATION_INTEGRITY)
    if generation_hash.hexdigest() != snapshot.hexdigest():
        raise EmbeddingIndexError(_GENERATION_INTEGRITY)


def _promote(
    generation, prior_active_id: int | None, data_version_before: int, using: str
) -> None:
    """ONE short promotion transaction: acquire the SQLite write
    reservation via a harmless no-op update of the building generation,
    re-read ``PRAGMA data_version`` and require equality with the value
    captured before final validation (any other connection's commit
    fails conservatively), then read the CURRENT active generation id
    and require it exactly equals the captured ``prior_active_id``
    (including None — an active that appeared/changed in the meantime
    is a promotion race), supersede the old active generation FIRST
    (the UPDATE must affect exactly one row), then activate the target.
    Any failure rolls the whole transaction back with the old active
    generation unchanged; the DB unique constraint is the final guard.
    No network and no full scans in this transaction; a single-threaded
    operation cannot race itself on the same connection."""
    now = _now()
    try:
        with transaction.atomic(using=using):
            # Write reservation: a real UPDATE (same value) takes the
            # SQLite write lock so the data_version re-read below cannot
            # be interleaved by another writer before our commit.
            EmbeddingGeneration.objects.using(using).filter(
                pk=generation.pk
            ).update(model=generation.model)
            if _pragma_data_version(using) != data_version_before:
                raise EmbeddingIndexError(_PROMOTION_RACE)
            current_active_id = (
                EmbeddingGeneration.objects.using(using)
                .filter(state=EmbeddingGenerationState.ACTIVE)
                .values_list("pk", flat=True)
                .first()
            )
            if current_active_id != prior_active_id:
                raise EmbeddingIndexError(_PROMOTION_RACE)
            if prior_active_id is not None:
                _supersede_active(prior_active_id, now, using)
            updated = EmbeddingGeneration.objects.using(using).filter(
                pk=generation.pk, state=EmbeddingGenerationState.BUILDING
            ).update(
                state=EmbeddingGenerationState.ACTIVE,
                completed_at=now,
                activated_at=now,
            )
            if updated != 1:
                raise EmbeddingIndexError(_PROMOTION_FAILED)
    except EmbeddingIndexError:
        raise
    except DjangoDBError:
        raise EmbeddingIndexError(_DB_ERROR) from None


def _supersede_active(prior_active_id: int, now, using: str) -> None:
    """Supersede the prior active generation; the UPDATE must affect
    EXACTLY one row (anything else is a concurrent promotion race)."""
    updated = EmbeddingGeneration.objects.using(using).filter(
        pk=prior_active_id, state=EmbeddingGenerationState.ACTIVE
    ).update(
        state=EmbeddingGenerationState.SUPERSEDED,
        superseded_at=now,
    )
    if updated != 1:
        raise EmbeddingIndexError(_PROMOTION_RACE)


# ---------------------------------------------------------------------------
# Repair (reconcile the active generation with the current source)
# ---------------------------------------------------------------------------


def repair_embedding_index(
    config, *, using: str = "default", embedder=embed_texts
) -> dict:
    """Reconcile the ACTIVE generation with the current SearchDocuments.

    Requires the current active generation to exist and be exactly
    compatible with the configured model, EMBEDDING_VERSION and
    INDEX_VERSION (otherwise a stable rebuild-required error; a
    building/failed/superseded/incompatible generation is never
    chosen). Missing/stale/invalid current keys are re-embedded at most
    once (HTTP outside transactions; returned dimensions must equal the
    active generation's dimensions before any batch write) and active
    orphans are deleted in bounded pages with an absence recheck. Each
    short write transaction re-reads the source key/hash and active
    compatibility; changed rows remain unresolved and are never given
    false provenance. Partial batch progress is durable and the active
    generation is never marked failed; the operation succeeds only when
    the final status is healthy. No network when only deleting orphans
    or when already healthy. Never runs while already inside a caller
    SQLite transaction; the configured model identity is EXACT (never
    stripped or canonicalized).
    """
    _reject_in_atomic_block(using)
    model = config.embedding.model
    if not (model or "").strip():
        raise EmbeddingIndexError(_MODEL_NOT_CONFIGURED)
    batch_size = _effective_batch_size(config)

    embedded_count = 0
    deleted_orphans = 0
    work_rows: list[SearchDocument] = []
    orphan_keys: list[str] = []

    try:
        # Preflight + schema + active-compatibility validation live INSIDE
        # the sanitizing try: an unexpected search-helper/DB exception
        # here must become a fixed sanitized error, never leak raw. The
        # source preflight still runs before ANY network/write.
        _preflight_source_index(using)
        _require_embedding_schema(using)
        active = _require_compatible_active(model, using)
        dimensions = active.dimensions
        for event in _iter_classified(active, using, _STATUS_PAGE_SIZE):
            if event[0] == "orphan":
                orphan_keys.append(event[1])
                if len(orphan_keys) >= _STATUS_PAGE_SIZE:
                    deleted_orphans += _delete_orphans(active, orphan_keys, using)
                    orphan_keys = []
            else:
                work_rows.append(event[2])
                if len(work_rows) >= batch_size:
                    embedded = embedder(
                        config, [prepare_document_text(r) for r in work_rows]
                    )
                    _persist_repair_batch(active, work_rows, embedded, dimensions, using)
                    embedded_count += len(work_rows)
                    work_rows = []
        if work_rows:
            embedded = embedder(config, [prepare_document_text(r) for r in work_rows])
            _persist_repair_batch(active, work_rows, embedded, dimensions, using)
            embedded_count += len(work_rows)
            work_rows = []
        if orphan_keys:
            deleted_orphans += _delete_orphans(active, orphan_keys, using)
            orphan_keys = []
    except EmbeddingIndexError:
        raise
    except EmbeddingError as exc:
        raise EmbeddingIndexError(_request_error(exc.code)) from None
    except VectorCodecError as exc:
        raise EmbeddingIndexError(_vector_error(exc.code)) from None
    except DjangoDBError:
        raise EmbeddingIndexError(_DB_ERROR) from None
    except Exception:
        # Unexpected operational failure: sanitize (never mark the active
        # generation failed); KeyboardInterrupt/SystemExit propagate.
        raise EmbeddingIndexError(_UNEXPECTED_ERROR) from None

    final_status = build_embedding_status_report(config, using=using)
    if not final_status.get("healthy"):
        raise EmbeddingIndexError(_REPAIR_NOT_CONVERGED)

    return {
        "result": "repaired",
        "embedding_version": EMBEDDING_VERSION,
        "generation_id": active.pk,
        "model": model,
        "dimensions": dimensions,
        "embedded": embedded_count,
        "deleted_orphans": deleted_orphans,
        "healthy": True,
    }


def _require_compatible_active(model: str, using: str) -> EmbeddingGeneration:
    active = EmbeddingGeneration.objects.using(using).filter(
        state=EmbeddingGenerationState.ACTIVE
    ).first()
    if active is None:
        raise EmbeddingIndexError(_NO_ACTIVE_REQUIRES_REBUILD)
    if (
        active.model != model
        or active.embedding_version != EMBEDDING_VERSION
        or active.source_index_version != search_index.INDEX_VERSION
    ):
        raise EmbeddingIndexError(_INCOMPATIBLE_REQUIRES_REBUILD)
    return active


def _active_is_compatible(active: EmbeddingGeneration, using: str) -> bool:
    row = EmbeddingGeneration.objects.using(using).filter(pk=active.pk).first()
    if row is None or row.state != EmbeddingGenerationState.ACTIVE:
        return False
    return (
        row.model == active.model
        and row.dimensions == active.dimensions
        and row.embedding_version == EMBEDDING_VERSION
        and row.source_index_version == search_index.INDEX_VERSION
    )


def _persist_repair_batch(
    active: EmbeddingGeneration, rows, embedded, dimensions: int, using: str
) -> None:
    """Validate pairing and that the returned dimension equals the ACTIVE
    generation's (mismatch fails requiring rebuild BEFORE any write),
    then ONE short transaction that re-reads source key/hash and active
    compatibility and upserts exactly the target rows — changed rows
    remain unresolved and never receive false provenance."""
    if len(rows) != len(embedded):
        raise EmbeddingIndexError(_PAIRING_ERROR)
    payload: dict[str, tuple[str, bytes]] = {}
    for row, batch in zip(rows, embedded):
        if type(batch.text) is not str or batch.text != prepare_document_text(row):
            raise EmbeddingIndexError(_PAIRING_ERROR)
        if len(batch.embedding) != dimensions:
            raise EmbeddingIndexError(_DIMENSION_CHANGED_REQUIRES_REBUILD)
        try:
            blob = encode_vector(batch.embedding, dimensions=dimensions)
        except VectorCodecError:
            raise EmbeddingIndexError(_VECTOR_ERROR) from None
        payload[row.document_key] = (row.content_hash, blob)
    try:
        with transaction.atomic(using=using):
            current = {
                r.document_key: r.content_hash
                for r in SearchDocument.objects.using(using).filter(
                    document_key__in=list(payload)
                )
            }
            if not _active_is_compatible(active, using):
                raise EmbeddingIndexError(_INCOMPATIBLE_REQUIRES_REBUILD)
            for key, (content_hash, _blob) in payload.items():
                if current.get(key) != content_hash:
                    raise EmbeddingIndexError(_UNRESOLVED)
            for key, (content_hash, blob) in payload.items():
                EmbeddingDocument.objects.using(using).update_or_create(
                    generation=active,
                    document_key=key,
                    defaults={
                        "source_content_hash": content_hash,
                        "vector_blob": blob,
                    },
                )
    except EmbeddingIndexError:
        raise
    except DjangoDBError:
        raise EmbeddingIndexError(_DB_ERROR) from None


def _delete_orphans(active: EmbeddingGeneration, keys: list[str], using: str) -> int:
    """Delete active-generation vectors whose key still has NO current
    SearchDocument, rechecking absence inside the short transaction."""
    try:
        with transaction.atomic(using=using):
            still_present = set(
                SearchDocument.objects.using(using)
                .filter(document_key__in=keys)
                .values_list("document_key", flat=True)
            )
            to_delete = [key for key in keys if key not in still_present]
            if to_delete:
                EmbeddingDocument.objects.using(using).filter(
                    generation=active, document_key__in=to_delete
                ).delete()
            return len(to_delete)
    except DjangoDBError:
        raise EmbeddingIndexError(_DB_ERROR) from None