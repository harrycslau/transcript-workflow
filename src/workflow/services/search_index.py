"""Keyword-search index foundation (Step 5A.2).

Owns the relational ``SearchDocument`` registry and the unified
contentful FTS5 table ``workflow_search_fts`` (rowid = SearchDocument.pk,
tokenize='trigram'). NO query parsing, ranking, highlighting, web
search, incremental synchronization (Step 5A.3) or embeddings (later)
live here — only deterministic document construction, atomic rebuild,
and complete read-only status/integrity comparison.

Canonical document set:

- ``segment``   every non-empty (``text.strip()``) segment of the ACTIVE
  Transcript. ``title_text`` is empty (the Recording metadata document
  owns the searchable title); ``body_text`` is the COMPLETE segment text
  (NFC-normalized, internal whitespace and line boundaries preserved,
  never truncated); ``aux_text`` is the speaker or empty. Segment
  ordinal/start/end are registry provenance bound into the content hash
  but never searchable FTS text.
- ``summary``   every current whole-recording variant
  (``Summary.is_active`` AND ``transcript.is_active`` AND
  ``section__ordinal=0``). Legacy ``output_language="und"`` rows inside
  the current transcript stay as stored and are reported as legacy.
- ``recording`` one deterministic metadata document: Library display
  title (shared ``library_metadata`` contract), deterministic source
  FILENAMES only (never paths), sorted active/effective tag names.
  Never secrets, diagnostics or rejected model suggestions.

Hashing: sha256 over a length-prefixed UTF-8 frame that distinguishes
null / empty / value and binds the index version, document key and type,
all provenance IDs, segment ordinal/start/end, output language and the
exact stored title/body/aux text.

All iteration, insertion and status comparison is chunk-bounded
(deterministic PK sweeps, fixed-size document chunks streamed through
both rebuild and status, keyset-paged registry/FTS sweeps): memory is
bounded by the chunk size even for a single recording with millions of
segments, the whole corpus is never held in memory, and no per-row
queries are issued.
"""

from __future__ import annotations

import hashlib
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Iterator, Sequence

from django.db import connections, transaction
from django.db.models import Q

from brainlib.config import ConfigError
from workflow.models import (
    AudioSource,
    Recording,
    SearchDocument,
    SearchDocType,
    Summary,
    TagAssignment,
    Transcript,
    TranscriptSegment,
)
from workflow.services.langresolve import default_output_language_expression
from workflow.services.library_metadata import (
    active_tag_names,
    deterministic_source_filenames,
    display_title_from_parts,
    preferred_source_filename,
    summary_title_parts,
)

INDEX_VERSION = "1"

FTS_TABLE = "workflow_search_fts"
FTS_COLUMNS = ("title_text", "body_text", "aux_text")
# The EXACT production schema. Migration 0008 keeps a byte-identical
# local copy (migrations must not import mutable service modules); the
# migration test asserts both constants agree.
FTS_SCHEMA_SQL = (
    "CREATE VIRTUAL TABLE workflow_search_fts "
    "USING fts5(title_text, body_text, aux_text, tokenize='trigram')"
)
FTS_PROBE_SQL = "CREATE VIRTUAL TABLE brain_fts5_probe USING fts5(x, tokenize='trigram')"

DEFAULT_BATCH_SIZE = 100
INSERT_CHUNK_SIZE = 500
STATUS_KEY_LIMIT = 20

# Stable report categories (never renamed silently).
CATEGORIES = (
    "registry_schema_missing",
    "fts_missing",
    "fts_broken",
    "missing_from_registry",
    "orphan_document",
    "missing_from_fts",
    "orphan_fts_row",
    "content_mismatch",
    "stale_content",
    "version_mismatch",
)

# Fixed sanitized messages — never interpolate SQL text, paths, raw
# exception details or indexed content.
_FTS_DDL_ERROR = "the search index FTS table could not be recreated on this SQLite connection"
_FTS_UNAVAILABLE_ERROR = (
    "keyword search indexing requires SQLite FTS5 with the trigram tokenizer; "
    "this Python SQLite build does not provide it"
)
_VERIFY_ERROR = "search index rebuild verification failed; the previous index was preserved"
_REGISTRY_SCHEMA_ERROR = (
    "search index registry schema is missing; apply pending migrations with: "
    "uv run python src/manage.py migrate"
)
_SQLITE_ONLY_ERROR = "the search index requires a SQLite database connection"


class SearchIndexError(ConfigError):
    """Sanitized, actionable search-index failure (CLI exit 1)."""


@dataclass(frozen=True)
class DocumentSpec:
    document_key: str
    doc_type: str
    recording_id: str
    transcript_id: str | None
    summary_id: str | None
    segment_ordinal: int | None
    start_ms: int | None
    end_ms: int | None
    output_language: str
    title_text: str
    body_text: str
    aux_text: str
    content_hash: str
    index_version: str = INDEX_VERSION


# ---------------------------------------------------------------------------
# Canonical text + hashing (mirrored byte-for-byte in migration 0008)
# ---------------------------------------------------------------------------


def nfc(value: str | None) -> str:
    """NFC-normalize a string; NEVER strip or collapse whitespace —
    stored searchable text keeps internal whitespace and line boundaries."""
    return unicodedata.normalize("NFC", value or "")


def frame_parts(parts: Sequence[str | None]) -> bytes:
    """Length-prefixed UTF-8 framing. ``None`` frames as the single byte
    ``N`` (distinct from ``S0:`` which frames the empty string), so
    null / empty / value are distinguishable and unambiguous."""
    out = bytearray()
    for part in parts:
        if part is None:
            out += b"N"
        else:
            data = part.encode("utf-8")
            out += b"S" + str(len(data)).encode("ascii") + b":" + data
    return bytes(out)


def compute_content_hash(spec_fields: dict) -> str:
    """sha256 hex over the canonical frame. ``spec_fields`` keys define
    the framed order; every value must be str or None."""
    payload = frame_parts([spec_fields[name] for name in _HASH_FRAME_ORDER])
    return hashlib.sha256(payload).hexdigest()


_HASH_FRAME_ORDER = (
    "index_version",
    "doc_type",
    "document_key",
    "recording_id",
    "transcript_id",
    "summary_id",
    "segment_ordinal",
    "start_ms",
    "end_ms",
    "output_language",
    "title_text",
    "body_text",
    "aux_text",
)


def _as_frame_value(value) -> str | None:
    if value is None:
        return None
    return str(value)


def make_spec(
    *,
    doc_type: str,
    document_key: str,
    recording_id: str,
    transcript_id: str | None = None,
    summary_id: str | None = None,
    segment_ordinal: int | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    output_language: str = "",
    title_text: str = "",
    body_text: str,
    aux_text: str = "",
) -> DocumentSpec:
    framed = {
        "index_version": INDEX_VERSION,
        "doc_type": str(doc_type),
        "document_key": document_key,
        "recording_id": _as_frame_value(recording_id),
        "transcript_id": _as_frame_value(transcript_id),
        "summary_id": _as_frame_value(summary_id),
        "segment_ordinal": _as_frame_value(segment_ordinal),
        "start_ms": _as_frame_value(start_ms),
        "end_ms": _as_frame_value(end_ms),
        "output_language": output_language,
        "title_text": title_text,
        "body_text": body_text,
        "aux_text": aux_text,
    }
    return DocumentSpec(
        document_key=document_key,
        doc_type=doc_type,
        recording_id=recording_id,
        transcript_id=transcript_id,
        summary_id=summary_id,
        segment_ordinal=segment_ordinal,
        start_ms=start_ms,
        end_ms=end_ms,
        output_language=output_language,
        title_text=title_text,
        body_text=body_text,
        aux_text=aux_text,
        content_hash=compute_content_hash(framed),
    )


# ---------------------------------------------------------------------------
# Canonical per-type field mappings (mirrored in migration 0008)
# ---------------------------------------------------------------------------


def _list_item_text(item) -> str:
    if isinstance(item, dict):
        text = item.get("text", "")
        return text if isinstance(text, str) else ""
    return item if isinstance(item, str) else ""


def summary_body_text(summary) -> str:
    """Deterministic prose: overview, then key_points texts, then
    action_items texts, model order, joined by newlines (NFC)."""
    parts = [nfc(summary.overview).strip("\r\n")]
    for item in summary.key_points or []:
        text = nfc(_list_item_text(item)).strip("\r\n")
        if text:
            parts.append(text)
    for item in summary.action_items or []:
        text = nfc(_list_item_text(item)).strip("\r\n")
        if text:
            parts.append(text)
    return "\n".join(part for part in parts if part)


def summary_aux_text(summary) -> str:
    """People / organizations / topics as deterministic searchable aux
    text (model list order preserved, NFC, joined by newlines)."""
    parts: list[str] = []
    for field_values in (summary.people, summary.organizations, summary.topics):
        if not field_values:
            continue
        for value in field_values:
            if isinstance(value, str) and nfc(value).strip():
                parts.append(nfc(value).strip())
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Expected-document generation (deterministic, chunk-streamed, read-only)
# ---------------------------------------------------------------------------


def iter_expected_document_batches(
    *,
    using: str = "default",
    batch_size: int = DEFAULT_BATCH_SIZE,
    chunk_size: int = INSERT_CHUNK_SIZE,
) -> Iterator[list[DocumentSpec]]:
    """Yield LISTS of at most ``chunk_size`` expected ``DocumentSpec``
    rows, in deterministic global order (per Recording-PK sweep:
    segments by (transcript, ordinal), then the Recording metadata doc,
    then Summary variants).

    Genuinely streaming: documents are buffered up to ``chunk_size`` and
    flushed immediately — a single recording with millions of segments
    never materializes an unbounded list. ``batch_size`` bounds the
    number of Recordings per sweep (and the per-sweep metadata
    prefetched), ``chunk_size`` bounds every yielded list. Bounded
    queries per sweep (transcripts, segments, summaries, sources, tags —
    one query each), never per row.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    buffer: list[DocumentSpec] = []
    last_pk: str | None = None
    while True:
        sweep = Recording.objects.using(using).order_by("pk")
        if last_pk is not None:
            sweep = sweep.filter(pk__gt=last_pk)
        recordings = list(sweep[:batch_size])
        if not recordings:
            break  # flush the final partial buffer below (never drop docs)
        last_pk = recordings[-1].pk
        rec_ids = [rec.pk for rec in recordings]

        transcripts = list(
            Transcript.objects.using(using)
            .filter(recording__in=rec_ids, is_active=True)
            .annotate(default_output=default_output_language_expression())
            .values("id", "recording_id", "default_output")
        )
        transcript_id_by_rec: dict[str, str] = {}
        default_language_by_rec: dict[str, str | None] = {}
        for row in transcripts:
            transcript_id_by_rec[row["recording_id"]] = row["id"]
            default_language_by_rec[row["recording_id"]] = row["default_output"] or None

        summaries_by_rec: dict[str, list[Summary]] = {}
        for summary in (
            Summary.objects.using(using)
            .filter(
                recording__in=rec_ids,
                is_active=True,
                transcript__is_active=True,
                section__ordinal=0,
            )
            .order_by("recording_id", "ordinal")
        ):
            summaries_by_rec.setdefault(summary.recording_id, []).append(summary)

        sources_by_rec: dict[str, list[AudioSource]] = {}
        for source in AudioSource.objects.using(using).filter(recording__in=rec_ids):
            sources_by_rec.setdefault(source.recording_id, []).append(source)

        tags_by_rec: dict[str, list[TagAssignment]] = {}
        assignments = (
            TagAssignment.objects.using(using)
            .filter(recording__in=rec_ids, is_active=True)
            .select_related("tag")
        )
        for assignment in assignments:
            tags_by_rec.setdefault(assignment.recording_id, []).append(assignment)

        transcript_ids = list(transcript_id_by_rec.values())
        rec_id_by_transcript = {tid: rid for rid, tid in transcript_id_by_rec.items()}
        segments = (
            TranscriptSegment.objects.using(using)
            .filter(transcript__in=transcript_ids)
            .order_by("transcript_id", "ordinal")
            .iterator(chunk_size=chunk_size)
        )
        for segment in segments:
            if not segment.text or not segment.text.strip():
                continue  # eligibility uses strip(); stored text is NOT stripped
            buffer.append(
                make_spec(
                    doc_type=SearchDocType.SEGMENT,
                    document_key=f"segment:{segment.transcript_id}:{segment.ordinal}",
                    recording_id=rec_id_by_transcript[segment.transcript_id],
                    transcript_id=segment.transcript_id,
                    segment_ordinal=segment.ordinal,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    body_text=nfc(segment.text),
                    aux_text=nfc(segment.speaker),
                )
            )
            if len(buffer) >= chunk_size:
                yield buffer
                buffer = []

        for recording in recordings:
            rec_sources = sources_by_rec.get(recording.pk, [])
            default_title, any_title = summary_title_parts(
                summaries_by_rec.get(recording.pk, []),
                default_language_by_rec.get(recording.pk),
            )
            title = display_title_from_parts(
                default_summary_title=default_title,
                fallback_summary_title=any_title,
                preferred_filename=preferred_source_filename(rec_sources),
            )
            buffer.append(
                make_spec(
                    doc_type=SearchDocType.RECORDING,
                    document_key=f"recording:{recording.pk}",
                    recording_id=recording.pk,
                    title_text=title,
                    body_text="\n".join(deterministic_source_filenames(rec_sources)),
                    aux_text="\n".join(
                        active_tag_names(tags_by_rec.get(recording.pk, []))
                    ),
                )
            )
            if len(buffer) >= chunk_size:
                yield buffer
                buffer = []

        for summary in (s for rows in summaries_by_rec.values() for s in rows):
            buffer.append(
                make_spec(
                    doc_type=SearchDocType.SUMMARY,
                    document_key=f"summary:{summary.pk}",
                    recording_id=summary.recording_id,
                    transcript_id=summary.transcript_id,
                    summary_id=summary.pk,
                    output_language=summary.output_language,
                    title_text=nfc(summary.title),
                    body_text=summary_body_text(summary),
                    aux_text=summary_aux_text(summary),
                )
            )
            if len(buffer) >= chunk_size:
                yield buffer
                buffer = []

    if buffer:
        yield buffer


def _chunks(items: list, size: int) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ---------------------------------------------------------------------------
# FTS5 capability + schema inspection (read-only where possible)
# ---------------------------------------------------------------------------


def fts5_trigram_available() -> bool:
    """Probe FTS5 + trigram on a SEPARATE in-memory database that is
    always closed in ``finally``; never touches the live connection."""
    probe = None
    try:
        probe = sqlite3.connect(":memory:")
        probe.execute(FTS_PROBE_SQL)
        return True
    except sqlite3.Error:
        return False
    finally:
        if probe is not None:
            try:
                probe.close()
            except sqlite3.Error:
                pass


def inspect_fts_schema(*, using: str = "default") -> dict:
    """Read-only validation of the REAL FTS table via ``sqlite_master``:
    must exist, be an fts5 virtual table with exactly ``FTS_COLUMNS``
    and the trigram tokenizer. Never creates or modifies anything."""
    connection = connections[using]
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = %s",
                [FTS_TABLE],
            )
            row = cursor.fetchone()
            if row is None:
                return {"state": "missing", "category": "fts_missing"}
            definition = (row[0] or "").lower()
            cursor.execute(f"PRAGMA table_info({FTS_TABLE})")
            columns = [r[1] for r in cursor.fetchall() if len(r) < 6 or r[5] == 0]
            cursor.execute(f"SELECT count(*) FROM {FTS_TABLE}")
            cursor.fetchone()
    except Exception:
        return {"state": "broken", "category": "fts_broken"}
    if tuple(columns) != FTS_COLUMNS:
        return {"state": "broken", "category": "fts_columns"}
    collapsed = " ".join(definition.split())
    if "using fts5(" not in collapsed or "tokenize='trigram'" not in collapsed:
        return {"state": "broken", "category": "fts_tokenizer"}
    return {"state": "ok", "category": None}


def _registry_schema_present(*, using: str = "default") -> bool:
    connection = connections[using]
    try:
        with connection.cursor() as cursor:
            table = SearchDocument._meta.db_table
            names = set(connection.introspection.table_names(cursor))
            if table not in names:
                return False
            columns = {
                column.name
                for column in connection.introspection.get_table_description(cursor, table)
            }
    except Exception:
        return False
    required = {
        column.attname
        for column in SearchDocument._meta.concrete_fields
    }
    return required.issubset(columns)


# ---------------------------------------------------------------------------
# Atomic rebuild (mutating — CLI holds the pipeline lock)
# ---------------------------------------------------------------------------


def _registry_table() -> str:
    return SearchDocument._meta.db_table


def _drop_fts(cursor) -> None:
    try:
        cursor.execute(f"DROP TABLE IF EXISTS {FTS_TABLE}")
    except Exception:
        raise SearchIndexError(_FTS_DDL_ERROR) from None


def _create_fts(cursor) -> None:
    try:
        cursor.execute(FTS_SCHEMA_SQL)
    except Exception:
        raise SearchIndexError(_FTS_DDL_ERROR) from None


def _insert_fts_batch(cursor, rows: list[tuple]) -> None:
    try:
        cursor.executemany(
            f"INSERT INTO {FTS_TABLE} (rowid, title_text, body_text, aux_text) "
            "VALUES (%s, %s, %s, %s)",
            rows,
        )
    except Exception:
        raise SearchIndexError(_VERIFY_ERROR) from None


def _verify_rebuild(using: str, total: int) -> None:
    """Complete in-transaction verification; any mismatch raises and the
    caller's transaction rolls everything back."""
    with connections[using].cursor() as check:
        check.execute(f"SELECT count(*) FROM {_registry_table()}")
        if check.fetchone()[0] != total:
            raise SearchIndexError(_VERIFY_ERROR)
        check.execute(f"SELECT count(*) FROM {FTS_TABLE}")
        if check.fetchone()[0] != total:
            raise SearchIndexError(_VERIFY_ERROR)
        check.execute(
            f"SELECT count(*) FROM {FTS_TABLE} f "
            f"LEFT JOIN {_registry_table()} d ON f.rowid = d.id "
            "WHERE d.id IS NULL"
        )
        if check.fetchone()[0] != 0:
            raise SearchIndexError(_VERIFY_ERROR)
        check.execute(
            f"SELECT count(*) FROM {_registry_table()} d "
            f"LEFT JOIN {FTS_TABLE} f ON d.id = f.rowid "
            "WHERE f.rowid IS NULL"
        )
        if check.fetchone()[0] != 0:
            raise SearchIndexError(_VERIFY_ERROR)

    # Streamed per-batch integrity: registry self-hash + FTS text.
    last_id = 0
    while True:
        rows = list(
            SearchDocument.objects.using(using)
            .filter(id__gt=last_id)
            .order_by("id")[:INSERT_CHUNK_SIZE]
        )
        if not rows:
            break
        last_id = rows[-1].id
        framed_by_id = {}
        for row in rows:
            recomputed = compute_content_hash(_row_frame(row))
            if recomputed != row.content_hash:
                raise SearchIndexError(_VERIFY_ERROR)
            framed_by_id[row.pk] = row
        placeholders = ", ".join(["%s"] * len(framed_by_id))
        with connections[using].cursor() as check:
            check.execute(
                f"SELECT rowid, title_text, body_text, aux_text FROM {FTS_TABLE} "
                f"WHERE rowid IN ({placeholders})",
                list(framed_by_id),
            )
            fts_rows = {r[0]: r for r in check.fetchall()}
        for row_id, row in framed_by_id.items():
            fts_row = fts_rows.get(row_id)
            if fts_row is None:
                raise SearchIndexError(_VERIFY_ERROR)
            if (fts_row[1], fts_row[2], fts_row[3]) != (
                row.title_text,
                row.body_text,
                row.aux_text,
            ):
                raise SearchIndexError(_VERIFY_ERROR)


def _row_frame(row: SearchDocument) -> dict:
    return {
        "index_version": row.index_version,
        "doc_type": str(row.doc_type),
        "document_key": row.document_key,
        "recording_id": _as_frame_value(row.recording_id),
        "transcript_id": _as_frame_value(row.transcript_id),
        "summary_id": _as_frame_value(row.summary_id),
        "segment_ordinal": _as_frame_value(row.segment_ordinal),
        "start_ms": _as_frame_value(row.start_ms),
        "end_ms": _as_frame_value(row.end_ms),
        "output_language": row.output_language,
        "title_text": row.title_text,
        "body_text": row.body_text,
        "aux_text": row.aux_text,
    }


def rebuild_index(*, using: str = "default", batch_size: int = DEFAULT_BATCH_SIZE) -> dict:
    """Atomically rebuild registry + FTS.

    One transaction: validate the registry schema, DROP and recreate the
    derived FTS table with the exact production schema (repairs missing /
    wrong-column / wrong-tokenizer tables), rebuild both structures in
    bounded batches, run COMPLETE rowid/content verification, and commit
    only on success. Any failure rolls back and leaves the previous
    index exactly as it was.
    """
    connection = connections[using]
    if connection.vendor != "sqlite":
        raise SearchIndexError(_SQLITE_ONLY_ERROR)
    if not fts5_trigram_available():
        raise SearchIndexError(_FTS_UNAVAILABLE_ERROR)
    if not _registry_schema_present(using=using):
        raise SearchIndexError(_REGISTRY_SCHEMA_ERROR)

    type_counts = {doc_type: 0 for doc_type in SearchDocType.values}
    total = 0
    with transaction.atomic(using=using):
        with connection.cursor() as cursor:
            _drop_fts(cursor)
            _create_fts(cursor)
            cursor.execute(f"DELETE FROM {_registry_table()}")
        for specs in iter_expected_document_batches(using=using, batch_size=batch_size):
            for chunk in _chunks(specs, INSERT_CHUNK_SIZE):
                documents = [
                    SearchDocument(
                        document_key=spec.document_key,
                        doc_type=spec.doc_type,
                        recording_id=spec.recording_id,
                        transcript_id=spec.transcript_id,
                        summary_id=spec.summary_id,
                        segment_ordinal=spec.segment_ordinal,
                        start_ms=spec.start_ms,
                        end_ms=spec.end_ms,
                        output_language=spec.output_language,
                        title_text=spec.title_text,
                        body_text=spec.body_text,
                        aux_text=spec.aux_text,
                        content_hash=spec.content_hash,
                        index_version=spec.index_version,
                    )
                    for spec in chunk
                ]
                created = SearchDocument.objects.using(using).bulk_create(documents)
                with connection.cursor() as cursor:
                    _insert_fts_batch(
                        cursor,
                        [
                            (doc.pk, doc.title_text, doc.body_text, doc.aux_text)
                            for doc in created
                        ],
                    )
                for doc in created:
                    type_counts[doc.doc_type] += 1
                total += len(created)
        _verify_rebuild(using, total)

    return {
        "result": "rebuilt",
        "index_version": INDEX_VERSION,
        "documents": {
            "total": total,
            "segments": type_counts[SearchDocType.SEGMENT],
            "summaries": type_counts[SearchDocType.SUMMARY],
            "recordings": type_counts[SearchDocType.RECORDING],
        },
    }


# ---------------------------------------------------------------------------
# Complete read-only status report
# ---------------------------------------------------------------------------


class _Tally:
    def __init__(self) -> None:
        self.counts = {category: 0 for category in CATEGORIES}
        self.keys: dict[str, list[str]] = {}
        self.truncated: dict[str, int] = {}

    def add(self, category: str, identifier: str) -> None:
        self.counts[category] += 1
        listed = self.keys.setdefault(category, [])
        if len(listed) < STATUS_KEY_LIMIT:
            listed.append(identifier)
        else:
            self.truncated[category] = self.truncated.get(category, 0) + 1


def _expected_registry_page_is_canonical(using: str, rows: list[SearchDocument]) -> set[int]:
    """Row ids (pks) of the registry page that map to an EXPECTED
    document, decided from the authoritative source with bounded queries
    (one per provenance family per page — never per row):

    - ``recording``: key matches ``recording:<own recording id>`` (the
      metadata doc is always generated for every Recording);
    - ``summary``: the Summary is active, on the active Transcript, in
      the ordinal-0 section;
    - ``segment``: the segment exists on an ACTIVE Transcript and its
      text is non-empty (``strip()`` eligibility).
    """
    expected_ids: set[int] = set()
    segment_candidates: dict[int, tuple[int, int]] = {}  # pk -> (transcript, ordinal)
    summary_candidates: dict[int, int] = {}  # pk -> summary_id
    for row in rows:
        if row.doc_type == SearchDocType.SEGMENT:
            if (
                row.transcript_id is not None
                and row.segment_ordinal is not None
                and row.document_key == f"segment:{row.transcript_id}:{row.segment_ordinal}"
            ):
                segment_candidates[row.pk] = (row.transcript_id, row.segment_ordinal)
        elif row.doc_type == SearchDocType.SUMMARY:
            if row.summary_id is not None and row.document_key == f"summary:{row.summary_id}":
                summary_candidates[row.pk] = row.summary_id
        elif row.doc_type == SearchDocType.RECORDING:
            if row.document_key == f"recording:{row.recording_id}":
                expected_ids.add(row.pk)
        # any other doc_type (or key/provenance mismatch from direct DB
        # tampering) can only be a forgery and is reported as an orphan
        # below

    if segment_candidates:
        pair_query = Q()
        for transcript_id, ordinal in sorted(set(segment_candidates.values())):
            pair_query |= Q(transcript_id=transcript_id, ordinal=ordinal)
        live_pairs = set()
        for transcript_id, ordinal, text in (
            TranscriptSegment.objects.using(using)
            .filter(pair_query, transcript__is_active=True)
            .values_list("transcript_id", "ordinal", "text")
        ):
            if text and text.strip():
                live_pairs.add((transcript_id, ordinal))
        for pk, pair in segment_candidates.items():
            if pair in live_pairs:
                expected_ids.add(pk)

    if summary_candidates:
        expected_summary_ids = set(
            Summary.objects.using(using)
            .filter(
                pk__in=list(set(summary_candidates.values())),
                is_active=True,
                transcript__is_active=True,
                section__ordinal=0,
            )
            .values_list("pk", flat=True)
        )
        for pk, summary_id in summary_candidates.items():
            if summary_id in expected_summary_ids:
                expected_ids.add(pk)
    return expected_ids


def _sweep_orphan_documents(using: str, tally: _Tally) -> None:
    """Registry-side orphan detection as ONE global keyset-paged sweep:
    memory is bounded per page and every registry row is classified with
    bounded provenance queries — a recording with millions of registry
    docs never loads them all.

    The FIRST page carries no lower-bound predicate and later pages use
    ``id__gt=last_seen``: SQLite permits zero and negative explicit
    primary keys, so a ``> 0`` start would silently skip tampered rows."""
    last_id: int | None = None
    while True:
        page_query = SearchDocument.objects.using(using)
        if last_id is not None:
            page_query = page_query.filter(id__gt=last_id)
        rows = list(page_query.order_by("id")[:INSERT_CHUNK_SIZE])
        if not rows:
            return
        last_id = rows[-1].pk
        expected_ids = _expected_registry_page_is_canonical(using, rows)
        for row in rows:
            if row.pk not in expected_ids:
                tally.add("orphan_document", row.document_key)


def _sweep_orphan_fts_rows(using: str, tally: _Tally) -> bool:
    """FTS-side orphan detection: EXACT total count via keyset-paged
    anti-join; only the identifier LIST is capped by the tally, never
    the count. The FIRST page carries no lower-bound predicate and later
    pages use ``rowid > last_seen`` — FTS5 accepts zero and negative
    rowids, so a ``> 0`` start would silently skip tampered rows.
    Returns False when the FTS table became unreadable."""
    connection = connections[using]
    try:
        last_rowid: int | None = None
        while True:
            sql = (
                f"SELECT f.rowid FROM {FTS_TABLE} f "
                f"LEFT JOIN {_registry_table()} d ON f.rowid = d.id "
                "WHERE d.id IS NULL"
            )
            params: list[int] = []
            if last_rowid is not None:
                sql += " AND f.rowid > %s"
                params.append(last_rowid)
            sql += " ORDER BY f.rowid LIMIT %s"
            params.append(INSERT_CHUNK_SIZE)
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                page = [row[0] for row in cursor.fetchall()]
            if not page:
                return True
            for row_id in page:
                tally.add("orphan_fts_row", f"rowid:{row_id}")
            last_rowid = page[-1]
            if len(page) < INSERT_CHUNK_SIZE:
                return True
    except Exception:
        return False


def build_status_report(
    *, using: str = "default", batch_size: int = DEFAULT_BATCH_SIZE
) -> dict:
    """Strictly read-only three-way comparison: authoritative source data
    vs ``SearchDocument`` vs the ACTUAL FTS row IDs and content. Never
    trusts registry hashes alone; never creates or modifies anything.

    Memory-bounded: expected documents arrive in fixed-size chunks and
    every comparison is a per-chunk set lookup; both orphan sweeps page
    with keyset pagination."""
    connection = connections[using]
    tally = _Tally()
    fts_state = {"state": "unknown", "category": None}

    if connection.vendor != "sqlite":
        tally.add("registry_schema_missing", "non-sqlite-connection")
        return _finish(tally, fts_state, {})

    registry_ok = _registry_schema_present(using=using)
    if not registry_ok:
        tally.add("registry_schema_missing", SearchDocument._meta.db_table)

    fts_info = inspect_fts_schema(using=using)
    fts_state = {"state": fts_info["state"], "category": fts_info["category"]}
    fts_usable = fts_info["state"] == "ok"
    if fts_info["state"] == "missing":
        tally.add("fts_missing", FTS_TABLE)
    elif fts_info["state"] == "broken":
        tally.add("fts_broken", fts_info["category"] or FTS_TABLE)

    counts = {"source_documents": 0, "registry_documents": 0, "fts_rows": None,
              "segments": 0, "summaries": 0, "recordings": 0, "legacy_und_variants": 0}

    if registry_ok:
        counts["registry_documents"] = SearchDocument.objects.using(using).count()
    if fts_usable:
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT count(*) FROM {FTS_TABLE}")
                counts["fts_rows"] = cursor.fetchone()[0]
        except Exception:
            fts_usable = False
            fts_state = {"state": "broken", "category": "fts_broken"}
            tally.add("fts_broken", FTS_TABLE)

    if fts_usable:
        # FTS rows with no registry row (cannot be attributed to a
        # document_key — reported by rowid, which is not indexed text).
        if not _sweep_orphan_fts_rows(using, tally):
            fts_usable = False
            fts_state = {"state": "broken", "category": "fts_broken"}
            tally.add("fts_broken", FTS_TABLE)

    if registry_ok:
        _sweep_orphan_documents(using, tally)

    if registry_ok:
        for specs in iter_expected_document_batches(using=using, batch_size=batch_size):
            counts["source_documents"] += len(specs)
            for spec in specs:
                counts["segments" if spec.doc_type == SearchDocType.SEGMENT else
                        "summaries" if spec.doc_type == SearchDocType.SUMMARY else "recordings"] += 1
                if spec.doc_type == SearchDocType.SUMMARY and spec.output_language == "und":
                    counts["legacy_und_variants"] += 1

            expected = {spec.document_key: spec for spec in specs}
            registry_by_key: dict[str, SearchDocument] = {
                row.document_key: row
                for row in SearchDocument.objects.using(using).filter(
                    document_key__in=list(expected)
                )
            }

            fts_content: dict[str, tuple] = {}
            if fts_usable:
                registry_ids = [row.pk for row in registry_by_key.values()]
                for chunk in _chunks(registry_ids, INSERT_CHUNK_SIZE):
                    placeholders = ", ".join(["%s"] * len(chunk))
                    try:
                        with connection.cursor() as cursor:
                            cursor.execute(
                                f"SELECT rowid, title_text, body_text, aux_text "
                                f"FROM {FTS_TABLE} WHERE rowid IN ({placeholders})",
                                chunk,
                            )
                            found = cursor.fetchall()
                    except Exception:
                        fts_usable = False
                        fts_state = {"state": "broken", "category": "fts_broken"}
                        tally.add("fts_broken", FTS_TABLE)
                        break
                    content_by_id = {row[0]: row for row in found}
                    for key, row in registry_by_key.items():
                        if row.pk in content_by_id:
                            fts_content[key] = content_by_id[row.pk]

            for key, spec in expected.items():
                row = registry_by_key.get(key)
                if row is None:
                    tally.add("missing_from_registry", key)
                    continue
                if row.index_version != INDEX_VERSION:
                    tally.add("version_mismatch", key)
                    continue
                if row.content_hash != spec.content_hash:
                    tally.add("stale_content", key)
                if fts_usable:
                    fts_row = fts_content.get(key)
                    if fts_row is None:
                        tally.add("missing_from_fts", key)
                    elif (fts_row[1], fts_row[2], fts_row[3]) != (
                        row.title_text,
                        row.body_text,
                        row.aux_text,
                    ) or compute_content_hash(_row_frame(row)) != row.content_hash:
                        tally.add("content_mismatch", key)

    healthy = fts_state["state"] == "ok" and not any(tally.counts.values())
    return _finish(tally, fts_state, counts, healthy=healthy)


def _finish(tally: _Tally, fts_state: dict, counts: dict, *, healthy: bool = False) -> dict:
    return {
        "healthy": bool(healthy),
        "index_version": INDEX_VERSION,
        "fts": fts_state,
        "counts": counts,
        "categories": dict(tally.counts),
        "keys": {k: v for k, v in tally.keys.items() if v},
        "keys_truncated": dict(tally.truncated),
    }
