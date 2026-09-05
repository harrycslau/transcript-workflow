"""Step 5A.2: Search index foundation — SearchDocument registry + FTS5 table.

Operation ordering (fully reversible, forward-atomic):

1. ``RunPython`` FTS5+trigram capability probe on a SEPARATE in-memory
   SQLite connection (always closed in ``finally``; never creates
   anything on the live database).
2. ``CreateModel`` SearchDocument (relational registry).
3. ``RunPython`` FTS ``CREATE``/``DROP``: creates the contentful FTS5
   table ``workflow_search_fts`` (rowid = SearchDocument.pk,
   tokenize='trigram'). DDL failures are converted to ONE fixed
   sanitized error with ``raise ... from None`` — SQL text, paths, raw
   exceptions and indexed content never surface. Reverse really drops
   the virtual table (plus its shadow tables).
4. ``RunPython`` backfill LAST: streams existing eligible data into the
   registry AND the FTS table in bounded batches. Reverse deletes the
   derived rows (harmless; the structures still exist at that point in
   the reverse ordering).

The WHOLE migration runs inside one transaction (Django's default
``atomic=True`` on SQLite): any failure — probe, DDL, or a mid-backfill
error — rolls back to exactly 0007 with no partial state. Reverse is
genuine (0008 -> 0007): both search structures are removed and canonical
source data is untouched; the derived index is regenerable via
``brain search-index rebuild`` anyway.

The canonicalization/hash/mapping helpers below are a migration-local
copy mirroring ``workflow.services.search_index`` (plus the
``library_metadata`` display-title chain and the ``langresolve`` default
policy) so this applied migration's semantics never drift with mutable
service code. The copies MUST NOT diverge: ``tests/test_search_index_migration.py``
proves migration-backfill and runtime-rebuild produce byte-identical
documents while ``INDEX_VERSION`` stays "1".
"""

from __future__ import annotations

import hashlib
import sqlite3
import unicodedata

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models

INDEX_VERSION = "1"
FTS_TABLE = "workflow_search_fts"
# Byte-identical to workflow.services.search_index.FTS_SCHEMA_SQL
# (asserted by tests).
FTS_SCHEMA_SQL = (
    "CREATE VIRTUAL TABLE workflow_search_fts "
    "USING fts5(title_text, body_text, aux_text, tokenize='trigram')"
)
FTS_DROP_SQL = "DROP TABLE IF EXISTS workflow_search_fts"
FTS_PROBE_SQL = "CREATE VIRTUAL TABLE brain_fts5_probe USING fts5(x, tokenize='trigram')"

BATCH_SIZE = 100
INSERT_CHUNK_SIZE = 500
TITLE_PLACEHOLDER = "Untitled recording"
DEFAULT_OUTPUT_LANGUAGE = "en"
ZH_HANT = "zh-Hant"

# Fixed sanitized messages; never interpolate SQL/paths/raw errors.
PROBE_ERROR = (
    "keyword search indexing requires SQLite FTS5 with the trigram tokenizer; "
    "this Python SQLite build does not provide it"
)
DDL_ERROR = "the search index FTS table could not be recreated on this SQLite connection"


# ---------------------------------------------------------------------------
# Capability probe + sanitized DDL (all reverses are real no-op-safe code)
# ---------------------------------------------------------------------------


def _noop(apps, schema_editor):
    return None


def _probe_fts5_trigram(apps, schema_editor):
    probe = None
    try:
        probe = sqlite3.connect(":memory:")
        probe.execute(FTS_PROBE_SQL)
    except Exception:
        raise RuntimeError(PROBE_ERROR) from None
    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass


def _execute_ddl(schema_editor, sql):
    schema_editor.execute(sql)


def _create_fts(apps, schema_editor):
    try:
        _execute_ddl(schema_editor, FTS_SCHEMA_SQL)
    except Exception:
        raise RuntimeError(DDL_ERROR) from None


def _drop_fts(apps, schema_editor):
    try:
        _execute_ddl(schema_editor, FTS_DROP_SQL)
    except Exception:
        raise RuntimeError(DDL_ERROR) from None


# ---------------------------------------------------------------------------
# Canonicalization + hashing (migration-local mirror of search_index)
# ---------------------------------------------------------------------------


def _nfc(value):
    return unicodedata.normalize("NFC", value or "")


def _frame(parts):
    out = bytearray()
    for part in parts:
        if part is None:
            out += b"N"
        else:
            data = part.encode("utf-8")
            out += b"S" + str(len(data)).encode("ascii") + b":" + data
    return bytes(out)


def _hash(fields):
    payload = _frame(
        [
            fields["index_version"],
            fields["doc_type"],
            fields["document_key"],
            fields["recording_id"],
            fields["transcript_id"],
            fields["summary_id"],
            fields["segment_ordinal"],
            fields["start_ms"],
            fields["end_ms"],
            fields["output_language"],
            fields["title_text"],
            fields["body_text"],
            fields["aux_text"],
        ]
    )
    return hashlib.sha256(payload).hexdigest()


def _as_str(value):
    return None if value is None else str(value)


def _doc(fields):
    """Build stored model fields + content_hash. Only the hash FRAMING
    stringifies numeric IDs/timestamps; stored columns keep their raw
    values (mirror of search_index.make_spec)."""
    stored = dict(fields)
    stored.setdefault("transcript_id", None)
    stored.setdefault("summary_id", None)
    stored.setdefault("segment_ordinal", None)
    stored.setdefault("start_ms", None)
    stored.setdefault("end_ms", None)
    stored.setdefault("output_language", "")
    stored.setdefault("title_text", "")
    stored.setdefault("aux_text", "")
    stored["index_version"] = INDEX_VERSION
    framed = dict(stored)
    framed["doc_type"] = str(framed["doc_type"])
    for name in ("recording_id", "transcript_id", "summary_id",
                 "segment_ordinal", "start_ms", "end_ms"):
        framed[name] = _as_str(framed[name])
    stored["content_hash"] = _hash(framed)
    return stored


# ---------------------------------------------------------------------------
# Field mappings + metadata chain (mirrors search_index + library_metadata)
# ---------------------------------------------------------------------------


def _list_item_text(item):
    if isinstance(item, dict):
        text = item.get("text", "")
        return text if isinstance(text, str) else ""
    return item if isinstance(item, str) else ""


def _summary_body(summary):
    parts = [_nfc(summary.overview).strip("\r\n")]
    for item in summary.key_points or []:
        text = _nfc(_list_item_text(item)).strip("\r\n")
        if text:
            parts.append(text)
    for item in summary.action_items or []:
        text = _nfc(_list_item_text(item)).strip("\r\n")
        if text:
            parts.append(text)
    return "\n".join(part for part in parts if part)


def _summary_aux(summary):
    parts = []
    for field_values in (summary.people, summary.organizations, summary.topics):
        if not field_values:
            continue
        for value in field_values:
            if isinstance(value, str) and _nfc(value).strip():
                parts.append(_nfc(value).strip())
    return "\n".join(parts)


def _is_chinese_family(code):
    return code.split("-", 1)[0].lower() in ("zh", "yue", "cmn")


def _default_output_language(transcript, active_decision):
    """Migration-local mirror of langresolve.resolve_default_language."""
    observed = transcript.language_observed or ""
    if observed and transcript.language_observed_verified_by == "user":
        return ZH_HANT if _is_chinese_family(observed) else DEFAULT_OUTPUT_LANGUAGE
    if active_decision is not None:
        chinese_route = active_decision.route_suggestion in ("cantonese", "mandarin")
        if active_decision.routing_verified and chinese_route:
            return ZH_HANT
        if active_decision.method == "automatic" and chinese_route:
            return ZH_HANT
    if observed:
        return ZH_HANT if _is_chinese_family(observed) else DEFAULT_OUTPUT_LANGUAGE
    return DEFAULT_OUTPUT_LANGUAGE


def _display_title(summaries, default_language, sources):
    """Migration-local mirror of the library_metadata display-title chain."""
    any_title = None
    default_title = None
    for row in summaries:  # pre-ordered by (recording, ordinal)
        title = _nfc(row.title)
        if title and any_title is None:
            any_title = title
        if title and default_language and row.output_language == default_language:
            if default_title is None:
                default_title = title
        if any_title is not None and default_title is not None:
            break
    preferred_filename = None
    for source in sorted(sources, key=lambda s: (0 if s.is_canonical else 1, s.first_seen_at, s.pk)):
        if source.original_filename:
            preferred_filename = _nfc(source.original_filename)
            break
    filenames = []
    seen = set()
    for source in sorted(sources, key=lambda s: (0 if s.is_canonical else 1, s.first_seen_at, s.pk)):
        name = _nfc(source.original_filename).strip()
        if name and name not in seen:
            seen.add(name)
            filenames.append(name)
    title = default_title or any_title or preferred_filename or TITLE_PLACEHOLDER
    return title, "\n".join(filenames)


def _active_tag_names(assignments):
    rows = sorted(
        assignments, key=lambda a: (a.tag.name_key, a.tag.name)
    )
    return [_nfc(a.tag.name) for a in rows]


# ---------------------------------------------------------------------------
# Backfill (genuinely streaming: docs are buffered to INSERT_CHUNK_SIZE
# and flushed immediately, so a single recording with millions of
# segments never accumulates an unbounded list; the forward transaction
# makes the whole thing atomic)
# ---------------------------------------------------------------------------


def _backfill(apps, schema_editor):
    alias = schema_editor.connection.alias
    Recording = apps.get_model("workflow", "Recording")
    Transcript = apps.get_model("workflow", "Transcript")
    TranscriptSegment = apps.get_model("workflow", "TranscriptSegment")
    Summary = apps.get_model("workflow", "Summary")
    AudioSource = apps.get_model("workflow", "AudioSource")
    TagAssignment = apps.get_model("workflow", "TagAssignment")
    RoutingDecision = apps.get_model("workflow", "RoutingDecision")
    SearchDocument = apps.get_model("workflow", "SearchDocument")

    buffer: list[dict] = []

    def flush(force: bool = False) -> None:
        while buffer and (force or len(buffer) >= INSERT_CHUNK_SIZE):
            take = INSERT_CHUNK_SIZE if not force else len(buffer)
            chunk = buffer[:take]
            del buffer[:take]
            created = SearchDocument.objects.using(alias).bulk_create(
                [SearchDocument(**fields) for fields in chunk]
            )
            with schema_editor.connection.cursor() as cursor:
                _insert_fts_rows(
                    cursor,
                    [
                        (doc.pk, doc.title_text, doc.body_text, doc.aux_text)
                        for doc in created
                    ],
                )

    def add_doc(fields: dict) -> None:
        buffer.append(_doc(fields))
        flush()

    last_pk = None
    while True:
        sweep = Recording.objects.using(alias).order_by("pk")
        if last_pk is not None:
            sweep = sweep.filter(pk__gt=last_pk)
        recordings = list(sweep[:BATCH_SIZE])
        if not recordings:
            break
        last_pk = recordings[-1].pk
        rec_ids = [rec.pk for rec in recordings]

        transcripts = list(
            Transcript.objects.using(alias).filter(recording__in=rec_ids, is_active=True)
        )
        transcript_id_by_rec = {t.recording_id: t.pk for t in transcripts}
        rec_id_by_transcript = {t.pk: t.recording_id for t in transcripts}
        decisions = {
            d.recording_id: d
            for d in RoutingDecision.objects.using(alias).filter(
                recording__in=rec_ids, is_active=True
            )
        }
        default_language_by_rec = {
            t.recording_id: _default_output_language(t, decisions.get(t.recording_id))
            for t in transcripts
        }

        summaries_by_rec = {}
        for summary in (
            Summary.objects.using(alias)
            .filter(
                recording__in=rec_ids,
                is_active=True,
                transcript__is_active=True,
                section__ordinal=0,
            )
            .order_by("recording_id", "ordinal")
        ):
            summaries_by_rec.setdefault(summary.recording_id, []).append(summary)

        sources_by_rec = {}
        for source in AudioSource.objects.using(alias).filter(recording__in=rec_ids):
            sources_by_rec.setdefault(source.recording_id, []).append(source)

        tags_by_rec = {}
        for assignment in (
            TagAssignment.objects.using(alias)
            .filter(recording__in=rec_ids, is_active=True)
            .select_related("tag")
        ):
            tags_by_rec.setdefault(assignment.recording_id, []).append(assignment)

        segments = (
            TranscriptSegment.objects.using(alias)
            .filter(transcript__in=list(transcript_id_by_rec.values()))
            .order_by("transcript_id", "ordinal")
            .iterator(chunk_size=INSERT_CHUNK_SIZE)
        )
        for segment in segments:
            if not segment.text or not segment.text.strip():
                continue
            add_doc(
                {
                    "doc_type": "segment",
                    "document_key": f"segment:{segment.transcript_id}:{segment.ordinal}",
                    "recording_id": rec_id_by_transcript[segment.transcript_id],
                    "transcript_id": segment.transcript_id,
                    "segment_ordinal": segment.ordinal,
                    "start_ms": segment.start_ms,
                    "end_ms": segment.end_ms,
                    "body_text": _nfc(segment.text),
                    "aux_text": _nfc(segment.speaker),
                }
            )

        for recording in recordings:
            title, filename_body = _display_title(
                summaries_by_rec.get(recording.pk, []),
                default_language_by_rec.get(recording.pk),
                sources_by_rec.get(recording.pk, []),
            )
            add_doc(
                {
                    "doc_type": "recording",
                    "document_key": f"recording:{recording.pk}",
                    "recording_id": recording.pk,
                    "title_text": title,
                    "body_text": filename_body,
                    "aux_text": "\n".join(
                        _active_tag_names(tags_by_rec.get(recording.pk, []))
                    ),
                }
            )

        for rows in summaries_by_rec.values():
            for summary in rows:
                add_doc(
                    {
                        "doc_type": "summary",
                        "document_key": f"summary:{summary.pk}",
                        "recording_id": summary.recording_id,
                        "transcript_id": summary.transcript_id,
                        "summary_id": summary.pk,
                        "output_language": summary.output_language,
                        "title_text": _nfc(summary.title),
                        "body_text": _summary_body(summary),
                        "aux_text": _summary_aux(summary),
                    }
                )

    flush(force=True)


def _insert_fts_rows(cursor, rows):
    cursor.executemany(
        f"INSERT INTO {FTS_TABLE} (rowid, title_text, body_text, aux_text) "
        "VALUES (%s, %s, %s, %s)",
        rows,
    )


def _unbackfill(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {FTS_TABLE}")
        cursor.execute("DELETE FROM workflow_search_document")


class Migration(migrations.Migration):

    dependencies = [
        ("workflow", "0007_summary_multilingual"),
    ]

    operations = [
        migrations.RunPython(_probe_fts5_trigram, _noop),
        migrations.CreateModel(
            name="SearchDocument",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("document_key", models.TextField(db_index=True, help_text="Stable identity: recording:<id> | segment:<transcript_id>:<ordinal> | summary:<id>", unique=True)),
                ("doc_type", models.CharField(choices=[("segment", "Transcript Segment"), ("summary", "Current Summary Variant"), ("recording", "Recording Metadata")], max_length=16)),
                ("segment_ordinal", models.PositiveIntegerField(blank=True, null=True)),
                ("start_ms", models.BigIntegerField(blank=True, null=True)),
                ("end_ms", models.BigIntegerField(blank=True, null=True)),
                ("output_language", models.CharField(blank=True, default="", max_length=32)),
                ("title_text", models.TextField(blank=True, default="")),
                ("body_text", models.TextField()),
                ("aux_text", models.TextField(blank=True, default="")),
                ("content_hash", models.CharField(help_text="sha256 over the length-prefixed canonical frame", max_length=64)),
                ("index_version", models.CharField(help_text="Indexer/schema version that produced content_hash", max_length=16)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("recording", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="search_documents", to="workflow.recording")),
                ("summary", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="search_documents", to="workflow.summary")),
                ("transcript", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="search_documents", to="workflow.transcript")),
            ],
            options={
                "ordering": ["document_key"],
                "db_table": "workflow_search_document",
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("doc_type", "segment")),
                        fields=("transcript", "segment_ordinal"),
                        name="uniq_search_doc_segment",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("doc_type", "summary")),
                        fields=("summary",),
                        name="uniq_search_doc_summary",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("doc_type", "recording")),
                        fields=("recording",),
                        name="uniq_search_doc_recording",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("doc_type", "segment"), _negated=True),
                            models.Q(
                                ("doc_type", "segment"),
                                ("output_language", ""),
                                ("segment_ordinal__isnull", False),
                                ("summary__isnull", True),
                                ("transcript__isnull", False),
                            ),
                            _connector="OR",
                        ),
                        name="chk_search_doc_shape_segment",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("doc_type", "summary"), _negated=True),
                            models.Q(
                                ("doc_type", "summary"),
                                ("end_ms__isnull", True),
                                ("segment_ordinal__isnull", True),
                                ("start_ms__isnull", True),
                                ("summary__isnull", False),
                                ("transcript__isnull", False),
                            ),
                            _connector="OR",
                        ),
                        name="chk_search_doc_shape_summary",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("doc_type", "recording"), _negated=True),
                            models.Q(
                                ("doc_type", "recording"),
                                ("end_ms__isnull", True),
                                ("output_language", ""),
                                ("segment_ordinal__isnull", True),
                                ("start_ms__isnull", True),
                                ("summary__isnull", True),
                                ("transcript__isnull", True),
                            ),
                            _connector="OR",
                        ),
                        name="chk_search_doc_shape_recording",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("doc_type", "segment"),
                            ("doc_type", "summary"),
                            ("doc_type", "recording"),
                            _connector="OR",
                        ),
                        name="chk_search_doc_type_allowed",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("doc_type", "summary"), _negated=True),
                            models.Q(("output_language", ""), _negated=True),
                            _connector="OR",
                        ),
                        name="chk_search_doc_summary_output_language",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("content_hash", ""), _negated=True),
                            models.Q(("index_version", ""), _negated=True),
                        ),
                        name="chk_search_doc_hash_fields_present",
                    ),
                ],
            },
        ),
        migrations.RunPython(_create_fts, _drop_fts),
        migrations.RunPython(_backfill, _unbackfill, elidable=False),
    ]
