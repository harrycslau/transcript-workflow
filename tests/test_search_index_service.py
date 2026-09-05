"""Service-level tests for the Step 5A.2 search index foundation.

Covered: hashing rules (null/empty/typed distinction, bound fields),
canonical mappings (segment/summary/recording), library_metadata
parity, document identity + shape constraints + CASCADE, atomic
rebuild (including failure AFTER destructive DDL and after partial
inserts), FTS schema repair, complete status categories, legacy `und`
reporting, and batch-bounded query counts (no N+1).
"""

from __future__ import annotations

import pytest
from django.db import connection, transaction
from django.db.models import Q
from django.db.utils import IntegrityError
from django.test.utils import CaptureQueriesContext

from factories import (
    make_summary_version,
    make_tag,
    make_tag_assignment,
    make_transcribed_recording,
)
from workflow.models import (
    AudioSource,
    SearchDocument,
    Summary,
    Transcript,
    TranscriptSegment,
)
from workflow.services import search_index as si

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _snapshot():
    docs = {
        row["document_key"]: (
            row["pk"],
            row["content_hash"],
            row["title_text"],
            row["body_text"],
            row["aux_text"],
        )
        for row in SearchDocument.objects.values("document_key", "pk", "content_hash",
                                                "title_text", "body_text", "aux_text")
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT f.rowid, f.title_text, f.body_text, f.aux_text FROM workflow_search_fts f"
        )
        fts = {row[0]: row[1:] for row in cursor.fetchall()}
    return docs, fts


def _fts_ddl():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='workflow_search_fts'"
        )
        row = cursor.fetchone()
    return row[0] if row else None


def _corpus(*, recordings: int, segments: int, summaries: bool = True):
    """Deterministic synthetic corpus (no audio, no network)."""
    import uuid

    made = []
    for i in range(recordings):
        texts = [f"recording {i} segment {j} content about topic{j}" for j in range(segments)]
        rec, transcript, section = make_transcribed_recording(texts, sha=f"sha-{uuid.uuid4().hex}")
        if summaries:
            make_summary_version(rec, transcript, section, title=f"Title {i}")
        made.append((rec, transcript, section))
    return made


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


class TestHashing:
    def _base(self):
        return dict(
            index_version="1", doc_type="segment", document_key="segment:1:0",
            recording_id="r", transcript_id="1", summary_id=None,
            segment_ordinal="0", start_ms="0", end_ms="1000", output_language="",
            title_text="", body_text="text", aux_text="",
        )

    def test_null_empty_and_value_frames_differ(self):
        fields = self._base()
        h_null = si.compute_content_hash(fields)
        h_empty = si.compute_content_hash({**fields, "summary_id": ""})
        h_value = si.compute_content_hash({**fields, "summary_id": "x"})
        assert len({h_null, h_empty, h_value}) == 3

    def test_every_hashed_field_changes_the_hash(self):
        base = self._base()
        changes = {
            "index_version": "2",
            "doc_type": "summary",
            "document_key": "segment:1:1",
            "recording_id": "r2",
            "transcript_id": "2",
            "summary_id": "s",
            "segment_ordinal": "1",
            "start_ms": "1",
            "end_ms": "2",
            "output_language": "en",
            "title_text": "t",
            "body_text": "b",
            "aux_text": "a",
        }
        base_hash = si.compute_content_hash(base)
        for key, value in changes.items():
            assert si.compute_content_hash({**base, key: value}) != base_hash, key

    def test_frame_is_length_prefixed(self):
        assert si.frame_parts([None, "", "abc"]) == b"NS0:S3:abc"


# ---------------------------------------------------------------------------
# Rebuild + status basics
# ---------------------------------------------------------------------------


class TestRebuildAndStatus:
    def test_rebuild_indexes_current_corpus_only(self):
        from django.utils import timezone as tz

        from workflow.models import AttemptOutcome, AttemptStage, ProcessingAttempt, Section

        rec, transcript, section = make_transcribed_recording(
            ["hello world", "  ", "second line"], sha="s-1"
        )
        make_summary_version(rec, transcript, section, title="Meeting", output_language="en")
        make_summary_version(rec, transcript, section, title="Kokous", output_language="fi")

        # A superseded transcript of another recording keeps a scope-active
        # Summary + segment — NEITHER may be indexed.
        rec2, t2, sec2 = make_transcribed_recording(["obsolete segment"], sha="s-3")
        superseded = make_summary_version(rec2, t2, sec2, title="SupersededDoc")
        attempt = ProcessingAttempt.objects.create(
            recording=rec2, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS, finished_at=tz.now(),
        )
        newt = Transcript.objects.create(recording=rec2, attempt=attempt)
        Section.objects.create(transcript=newt, ordinal=0)
        TranscriptSegment.objects.create(transcript=newt, ordinal=0, text="new talk",
                                         start_ms=0, end_ms=1)
        Transcript.objects.filter(pk=t2.pk).update(is_active=False, superseded_at=tz.now())

        rebuild = si.rebuild_index()
        keys = set(SearchDocument.objects.values_list("document_key", flat=True))
        assert f"segment:{transcript.pk}:0" in keys
        assert f"segment:{transcript.pk}:1" not in keys  # whitespace-only
        assert f"segment:{transcript.pk}:2" in keys
        summary_keys = [k for k in keys if k.startswith("summary:")]
        assert len(summary_keys) == 2  # en + fi variants of the CURRENT transcript
        assert f"recording:{rec.pk}" in keys
        assert f"segment:{t2.pk}:0" not in keys
        assert f"summary:{superseded.pk}" not in keys

        status = si.build_status_report()
        assert status["healthy"] is True
        assert status["counts"]["segments"] == rebuild["documents"]["segments"]

    def test_segment_documents_do_not_repeat_title_or_timestamps(self):
        rec, transcript, section = make_transcribed_recording(["one two three"], sha="t-1")
        seg = TranscriptSegment.objects.get(transcript=transcript, ordinal=0)
        seg.speaker = "Anna"
        seg.save(update_fields=["speaker"])
        make_summary_version(rec, transcript, section, title="Big Title")
        si.rebuild_index()
        doc = SearchDocument.objects.get(document_key=f"segment:{transcript.pk}:0")
        assert doc.title_text == ""
        assert doc.aux_text == "Anna"
        assert doc.start_ms is not None and doc.end_ms is not None
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT title_text, body_text, aux_text FROM workflow_search_fts WHERE rowid = %s",
                [doc.pk],
            )
            fts_title, fts_body, fts_aux = cursor.fetchone()
        assert fts_title == ""
        assert fts_body == "one two three"
        assert fts_aux == "Anna"
        assert str(doc.start_ms) not in fts_aux + fts_body

    def test_title_change_does_not_stale_segment_documents(self):
        rec, transcript, section = make_transcribed_recording(["alpha text"], sha="t-2")
        summary = make_summary_version(rec, transcript, section, title="Old Title")
        si.rebuild_index()
        segment_key = f"segment:{transcript.pk}:0"
        segment_hash_before = SearchDocument.objects.get(document_key=segment_key).content_hash
        recording_hash_before = SearchDocument.objects.get(
            document_key=f"recording:{rec.pk}"
        ).content_hash

        summary.title = "New Title"
        summary.save(update_fields=["title"])
        si.rebuild_index()
        assert SearchDocument.objects.get(document_key=segment_key).content_hash == segment_hash_before
        assert SearchDocument.objects.get(
            document_key=f"recording:{rec.pk}"
        ).content_hash != recording_hash_before

    def test_long_recording_title_owned_by_one_document(self):
        texts = [f"segment number {i} text" for i in range(30)]
        rec, transcript, section = make_transcribed_recording(texts, sha="t-3")
        make_summary_version(rec, transcript, section, title="UNIQUETITLEMARKER")
        si.rebuild_index()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM workflow_search_fts WHERE workflow_search_fts MATCH %s",
                ["UNIQUETITLEMARKER"],
            )
            matching = cursor.fetchone()[0]
        assert matching == 2  # the summary doc (title+body) and the recording doc — never 30+ segments
        segment_rows = SearchDocument.objects.filter(doc_type="segment")
        assert not any("UNIQUETITLEMARKER" in row.title_text for row in segment_rows)

    def test_whitespace_and_line_boundaries_preserved(self):
        rec, transcript, _section = make_transcribed_recording(
            ["  lead and  internal   spaces\nplus a line break  "], sha="t-4"
        )
        si.rebuild_index()
        doc = SearchDocument.objects.get(document_key=f"segment:{transcript.pk}:0")
        assert doc.body_text == "  lead and  internal   spaces\nplus a line break  "

    def test_timestamp_change_rehashes_without_fts_text_change(self):
        rec, transcript, _section = make_transcribed_recording(["timed text"], sha="t-5")
        si.rebuild_index()
        doc = SearchDocument.objects.get(document_key=f"segment:{transcript.pk}:0")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT title_text, body_text, aux_text FROM workflow_search_fts WHERE rowid = %s",
                [doc.pk],
            )
            fts_before = cursor.fetchone()
        hash_before = doc.content_hash

        TranscriptSegment.objects.filter(transcript=transcript, ordinal=0).update(end_ms=999999)
        si.rebuild_index()
        new_doc = SearchDocument.objects.get(document_key=f"segment:{transcript.pk}:0")
        assert new_doc.content_hash != hash_before  # timestamps bound into the hash
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT title_text, body_text, aux_text FROM workflow_search_fts WHERE rowid = %s",
                [new_doc.pk],
            )
            assert cursor.fetchone() == fts_before  # but FTS text is identical

    def test_rebuild_deterministic(self):
        _corpus(recordings=3, segments=2)
        si.rebuild_index()
        first = _snapshot()
        si.rebuild_index()
        second = _snapshot()
        # pks are internal and may change; identity + content + hash are stable
        assert {k: v[1:] for k, v in first[0].items()} == {k: v[1:] for k, v in second[0].items()}

    def test_rebuild_repairs_missing_fts_table(self):
        _corpus(recordings=1, segments=2)
        si.rebuild_index()
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_search_fts")
        assert si.build_status_report()["categories"]["fts_missing"] == 1
        si.rebuild_index()
        report = si.build_status_report()
        assert report["healthy"] is True
        assert "trigram" in _fts_ddl()

    def test_rebuild_repairs_wrong_tokenizer(self):
        _corpus(recordings=1, segments=2)
        si.rebuild_index()
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_search_fts")
            cursor.execute(
                "CREATE VIRTUAL TABLE workflow_search_fts "
                "USING fts5(title_text, body_text, aux_text, tokenize='porter')"
            )
        report = si.build_status_report()
        assert report["fts"] == {"state": "broken", "category": "fts_tokenizer"}
        si.rebuild_index()
        assert si.build_status_report()["healthy"] is True

    def test_rebuild_repairs_wrong_columns(self):
        _corpus(recordings=1, segments=2)
        si.rebuild_index()
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_search_fts")
            cursor.execute("CREATE VIRTUAL TABLE workflow_search_fts USING fts5(body_text)")
        report = si.build_status_report()
        assert report["fts"] == {"state": "broken", "category": "fts_columns"}
        si.rebuild_index()
        assert si.build_status_report()["healthy"] is True


# ---------------------------------------------------------------------------
# Atomic rebuild — failure after DDL / after partial inserts
# ---------------------------------------------------------------------------


class TestRebuildAtomicity:
    def test_failure_after_partial_inserts_restores_previous_index_exactly(self, monkeypatch):
        _corpus(recordings=3, segments=2)
        si.rebuild_index(batch_size=1)
        before_docs, before_fts = _snapshot()
        before_ddl = _fts_ddl()

        calls: list[int] = []
        real = si._insert_fts_batch

        def flaky(cursor, rows):
            if not calls:
                real(cursor, rows)  # first chunk lands (partial state!)
                calls.append(len(rows))
                raise RuntimeError("synthetic failure after partial inserts")
            real(cursor, rows)

        monkeypatch.setattr(si, "_insert_fts_batch", flaky)
        with pytest.raises(RuntimeError, match="synthetic failure after partial inserts"):
            si.rebuild_index(batch_size=1)

        after_docs, after_fts = _snapshot()
        assert after_docs == before_docs  # pks, hashes, text — byte-identical
        assert after_fts == before_fts
        assert _fts_ddl() == before_ddl

    def test_failure_after_destructive_ddl_restores_schema_and_rows(self, monkeypatch):
        _corpus(recordings=2, segments=2)
        si.rebuild_index()
        before_docs, before_fts = _snapshot()
        before_ddl = _fts_ddl()

        def explode(cursor, rows):
            raise RuntimeError("synthetic failure right after DDL")

        monkeypatch.setattr(si, "_insert_fts_batch", explode)
        with pytest.raises(RuntimeError, match="right after DDL"):
            si.rebuild_index()

        after_docs, after_fts = _snapshot()
        assert after_docs == before_docs
        assert after_fts == before_fts
        assert _fts_ddl() == before_ddl  # DROP/CREATE inside the transaction rolled back

    def test_verification_mismatch_rolls_back(self, monkeypatch):
        _corpus(recordings=1, segments=2)
        si.rebuild_index()
        before_docs, before_fts = _snapshot()

        def broken_insert(cursor, rows):
            # insert FTS rows with corrupted content but valid rowids
            corrupted = [(r[0], r[1], r[2] + "-CORRUPTED", r[3]) for r in rows]
            real = object.__getattribute__(si, "_insert_fts_batch")
            cursor.executemany(
                "INSERT INTO workflow_search_fts (rowid, title_text, body_text, aux_text) "
                "VALUES (%s, %s, %s, %s)",
                corrupted,
            )

        monkeypatch.setattr(si, "_insert_fts_batch", broken_insert)
        with pytest.raises(si.SearchIndexError):
            si.rebuild_index()

        after_docs, after_fts = _snapshot()
        assert after_docs == before_docs
        assert after_fts == before_fts

    def test_probe_uses_separate_memory_connection(self, monkeypatch):
        assert si.fts5_trigram_available() is True


# ---------------------------------------------------------------------------
# Status categories (surgical corruption)
# ---------------------------------------------------------------------------


class TestStatusCategories:
    def _setup(self):
        rec, transcript, section = make_transcribed_recording(["content one", "content two"], sha="c-1")
        make_summary_version(rec, transcript, section, title="Doc title")
        si.rebuild_index()
        return rec, transcript, section

    def test_stale_content_detected(self):
        _rec, transcript, _section = self._setup()
        TranscriptSegment.objects.filter(transcript=transcript, ordinal=0).update(text="edited source")
        report = si.build_status_report()
        assert report["healthy"] is False
        assert report["categories"]["stale_content"] == 1
        assert f"segment:{transcript.pk}:0" in report["keys"]["stale_content"]

    def test_missing_from_registry(self):
        _rec, transcript, _section = self._setup()
        SearchDocument.objects.filter(document_key=f"segment:{transcript.pk}:0").delete()
        report = si.build_status_report()
        assert report["categories"]["missing_from_registry"] == 1
        assert report["categories"]["orphan_fts_row"] == 1

    def test_orphan_document_detected(self):
        rec, _transcript, _section = self._setup()
        other, _t, _s = make_transcribed_recording(["later recording"], sha="c-ghost")
        SearchDocument.objects.create(
            document_key=f"segment:{other.pk}:99", doc_type="segment",
            recording=other, transcript=_t, segment_ordinal=99,
            title_text="", body_text="ghost", aux_text="",
            content_hash="0" * 64, index_version=si.INDEX_VERSION,
        )
        report = si.build_status_report()
        assert report["categories"]["orphan_document"] == 1

    def test_missing_from_fts(self):
        _rec, transcript, _section = self._setup()
        doc = SearchDocument.objects.get(document_key=f"segment:{transcript.pk}:0")
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM workflow_search_fts WHERE rowid = %s", [doc.pk])
        report = si.build_status_report()
        assert report["categories"]["missing_from_fts"] == 1

    def test_content_mismatch_fts_vs_registry(self):
        _rec, transcript, _section = self._setup()
        doc = SearchDocument.objects.get(document_key=f"segment:{transcript.pk}:0")
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE workflow_search_fts SET body_text = body_text || ' X' WHERE rowid = %s",
                [doc.pk],
            )
        report = si.build_status_report()
        assert report["categories"]["content_mismatch"] == 1

    def test_content_mismatch_registry_hash_selfcheck(self):
        _rec, transcript, _section = self._setup()
        doc = SearchDocument.objects.get(document_key=f"segment:{transcript.pk}:0")
        # aligned tamper: registry text AND fts text changed consistently,
        # but the stored hash no longer matches the registry's own fields
        SearchDocument.objects.filter(pk=doc.pk).update(body_text="tampered aligned")
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE workflow_search_fts SET body_text = %s WHERE rowid = %s",
                ["tampered aligned", doc.pk],
            )
        report = si.build_status_report()
        assert report["categories"]["content_mismatch"] == 1

    def test_version_mismatch(self):
        _rec, transcript, _section = self._setup()
        SearchDocument.objects.filter(document_key=f"segment:{transcript.pk}:0").update(
            index_version="999"
        )
        report = si.build_status_report()
        assert report["categories"]["version_mismatch"] == 1

    def test_fts_missing_and_broken_states(self):
        self._setup()
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_search_fts")
        report = si.build_status_report()
        assert report["healthy"] is False
        assert report["categories"]["fts_missing"] == 1
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE VIRTUAL TABLE workflow_search_fts USING fts5(title_text, body_text, aux_text)"
            )
        report = si.build_status_report()
        assert report["categories"]["fts_broken"] == 1
        assert report["fts"]["category"] == "fts_tokenizer"

    def test_legacy_und_variant_counted(self):
        rec, transcript, section = make_transcribed_recording(["und text"], sha="c-und")
        make_summary_version(rec, transcript, section, title="UND title", output_language="und")
        si.rebuild_index()
        report = si.build_status_report()
        assert report["counts"]["legacy_und_variants"] == 1
        assert report["healthy"] is True
        doc = SearchDocument.objects.get(doc_type="summary")
        assert doc.output_language == "und"

    def test_orphan_fts_row_count_exact_beyond_the_key_limit(self):
        # review finding: the COUNT must be exact past STATUS_KEY_LIMIT;
        # only the identifier LIST (and therefore keys_truncated) is capped
        self._setup()
        base = max(SearchDocument.objects.values_list("pk", flat=True)) + 100
        with connection.cursor() as cursor:
            for offset in range(25):
                cursor.execute(
                    "INSERT INTO workflow_search_fts (rowid, title_text, body_text, aux_text)"
                    " VALUES (%s, 'orphan row', 'orphan row', '')",
                    [base + offset],
                )
        report = si.build_status_report()
        assert si.STATUS_KEY_LIMIT == 20
        assert report["categories"]["orphan_fts_row"] == 25
        assert len(report["keys"]["orphan_fts_row"]) == si.STATUS_KEY_LIMIT
        assert report["keys_truncated"]["orphan_fts_row"] == 5
        assert (
            report["counts"]["fts_rows"]
            == report["counts"]["registry_documents"] + 25
        )

    def test_status_never_writes(self):
        _corpus(recordings=1, segments=1)
        si.rebuild_index()
        before_docs, before_fts = _snapshot()
        with CaptureQueriesContext(connection) as ctx:
            si.build_status_report()
        writes = [
            q["sql"] for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "DROP"))
        ]
        assert writes == []
        assert (_snapshot()[0] == before_docs) and (_snapshot()[1] == before_fts)

    def test_report_json_contains_no_indexed_text(self):
        rec, transcript, _section = make_transcribed_recording(
            ["super-secret-transcript-content"], sha="c-json"
        )
        TranscriptSegment.objects.filter(transcript=transcript).update(text="super-secret-transcript-content")
        si.rebuild_index()
        TranscriptSegment.objects.filter(transcript=transcript, ordinal=0).update(text="changed now")
        report = si.build_status_report()
        import json

        assert not report["healthy"]
        rendered = json.dumps(report)
        assert "super-secret-transcript-content" not in rendered


# ---------------------------------------------------------------------------
# Identity, constraints, CASCADE
# ---------------------------------------------------------------------------


class TestConstraints:
    def test_document_key_unique(self):
        rec, _t, _s = make_transcribed_recording(["x"], sha="k-1")
        SearchDocument.objects.create(
            document_key="recording:dup", doc_type="recording", recording=rec,
            body_text="a", content_hash="1" * 64, index_version="1",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            SearchDocument.objects.create(
                document_key="recording:dup", doc_type="recording", recording=rec,
                body_text="b", content_hash="2" * 64, index_version="1",
            )

    def test_partial_uniques_per_type(self):
        rec, transcript, _s = make_transcribed_recording(["x"], sha="k-2")
        seg = TranscriptSegment.objects.get(transcript=transcript, ordinal=0)
        common = dict(doc_type="segment", recording=rec, transcript=transcript,
                      segment_ordinal=0, body_text="a", content_hash="a" * 64,
                      index_version="1")
        SearchDocument.objects.create(document_key="segment:1", **common)
        with pytest.raises(IntegrityError), transaction.atomic():
            SearchDocument.objects.create(document_key="segment:2", **common)

    def test_shape_constraints(self):
        rec, transcript, section = make_transcribed_recording(["x"], sha="k-3")
        summary = make_summary_version(rec, transcript, section)
        # recording doc must not carry transcript provenance
        with pytest.raises(IntegrityError), transaction.atomic():
            SearchDocument.objects.create(
                document_key="recording:bad", doc_type="recording", recording=rec,
                transcript=transcript, body_text="a", content_hash="1" * 64, index_version="1",
            )
        # summary doc must not carry timestamps
        with pytest.raises(IntegrityError), transaction.atomic():
            SearchDocument.objects.create(
                document_key="summary:bad", doc_type="summary", recording=rec,
                transcript=transcript, summary=summary, start_ms=5,
                output_language="en", body_text="a", content_hash="1" * 64, index_version="1",
            )
        # segment doc requires transcript
        with pytest.raises(IntegrityError), transaction.atomic():
            SearchDocument.objects.create(
                document_key="segment:bad", doc_type="segment", recording=rec,
                segment_ordinal=0, body_text="a", content_hash="1" * 64, index_version="1",
            )

    def test_hash_fields_required(self):
        rec, _t, _s = make_transcribed_recording(["x"], sha="k-4")
        with pytest.raises(IntegrityError), transaction.atomic():
            SearchDocument.objects.create(
                document_key="recording:nohash", doc_type="recording", recording=rec,
                body_text="a", content_hash="", index_version="1",
            )

    def test_doc_type_is_database_check_constrained(self):
        # Django choices alone are not a DB constraint; the CHECK is
        rec, _t, _s = make_transcribed_recording(["x"], sha="cc-1")
        with pytest.raises(IntegrityError), transaction.atomic():
            SearchDocument.objects.create(
                document_key="bogus:1", doc_type="bogus", recording=rec,
                body_text="a", content_hash="1" * 64, index_version="1",
            )
        # the three legitimate values still pass
        SearchDocument.objects.create(
            document_key="recording:cc-ok", doc_type="recording", recording=rec,
            body_text="a", content_hash="1" * 64, index_version="1",
        )

    def test_summary_document_requires_output_language(self):
        rec, transcript, section = make_transcribed_recording(["x"], sha="cc-2")
        summary = make_summary_version(rec, transcript, section)
        common = dict(doc_type="summary", recording=rec, transcript=transcript,
                      summary=summary, body_text="a", content_hash="a" * 64,
                      index_version="1")
        with pytest.raises(IntegrityError), transaction.atomic():
            SearchDocument.objects.create(
                document_key="summary:nolang", output_language="", **common
            )
        # a concrete language (including the legacy "und" marker) passes
        SearchDocument.objects.create(
            document_key="summary:withlang", output_language="en", **common
        )

    def test_cascade_deletes_never_block_source_deletion(self):
        rec, transcript, section = make_transcribed_recording(["cascading"], sha="k-5")
        summary = make_summary_version(rec, transcript, section)
        si.rebuild_index()
        assert SearchDocument.objects.filter(recording=rec).count() == 3
        # deleting a Summary removes its derived doc without protection
        summary.delete()
        assert not SearchDocument.objects.filter(document_key=f"summary:{summary.pk}").exists()
        # deleting the Transcript removes segment docs (source delete never
        # blocked by derived index rows)
        transcript.delete()
        assert SearchDocument.objects.filter(doc_type="segment").count() == 0
        # deleting the Recording removes ALL remaining derived docs
        rec.attempts.all().delete()
        rec.delete()
        assert not SearchDocument.objects.exists()


# ---------------------------------------------------------------------------
# library_metadata parity (SQL annotation vs Python helper)
# ---------------------------------------------------------------------------


class TestLibraryMetadataParity:
    def _queryset(self):
        from workflow.query import recording_list_queryset

        return recording_list_queryset()

    def test_display_title_chain_parity(self):
        # 1. default-language summary wins
        rec1, t1, s1 = make_transcribed_recording(["a"], sha="p-1")
        make_summary_version(rec1, t1, s1, title="EN title", output_language="en")
        make_summary_version(rec1, t1, s1, title="FI title", output_language="fi")
        # 2. no default-language row -> lowest ordinal
        rec2, t2, s2 = make_transcribed_recording(["b"], sha="p-2")
        make_summary_version(rec2, t2, s2, title="Only FI", output_language="fi")
        # 3. no summary -> preferred filename
        rec3, _t3, _s3 = make_transcribed_recording([], sha="p-3")
        AudioSource.objects.create(recording=rec3, path="/x/z.wav", path_identity="/x/z.wav",
                                   original_filename="z.wav", is_canonical=True)
        # 4. nothing -> placeholder
        rec4, _t4, _s4 = make_transcribed_recording([], sha="p-4")

        from workflow.services.library_metadata import display_title_from_recording

        expected = {
            rec1.pk: "EN title",
            rec2.pk: "Only FI",
            rec3.pk: "z.wav",
            rec4.pk: "Untitled recording",
        }
        for rec in self._queryset():
            assert rec.display_title == expected[rec.pk]
            assert display_title_from_recording(rec) == expected[rec.pk]

    def test_zh_default_variant_preferred_in_chain(self):
        rec, transcript, section = make_transcribed_recording(["中文"], sha="p-zh")
        from workflow.models import ProcessingAttempt, RoutingDecision

        attempt = ProcessingAttempt.objects.get(pk=transcript.attempt_id)
        RoutingDecision.objects.create(
            recording=rec, ordinal=1, route_suggestion="cantonese", profile_name="cantonese",
            model_id="apple:zh-HK", method="automatic", routing_verified=True, is_active=True,
        )
        make_summary_version(rec, transcript, section, title="EN variant", output_language="en")
        make_summary_version(rec, transcript, section, title="中文标题", output_language="zh-Hant")
        rec.refresh_from_db()
        for rec_obj in self._queryset():
            if rec_obj.pk == rec.pk:
                assert rec_obj.display_title == "中文标题"
                from workflow.services.library_metadata import display_title_from_recording

                assert display_title_from_recording(rec_obj) == "中文标题"

    def test_recording_document_uses_helper_and_filenames_only(self):
        rec, _t, _s = make_transcribed_recording([], sha="p-doc")
        AudioSource.objects.create(recording=rec, path="/secret/dir/one.wav",
                                   path_identity="/secret/dir/one.wav", original_filename="one.wav")
        AudioSource.objects.create(recording=rec, path="/secret/dir/two.m4a",
                                   path_identity="/secret/dir/two.m4a", original_filename="two.m4a")
        tag = make_tag("Work")
        make_tag_assignment(rec, tag)
        si.rebuild_index()
        doc = SearchDocument.objects.get(document_key=f"recording:{rec.pk}")
        assert doc.title_text == "one.wav"
        assert doc.body_text == "one.wav\ntwo.m4a"
        assert "/secret" not in doc.body_text and "/" not in doc.body_text
        assert doc.aux_text == "Work"


# ---------------------------------------------------------------------------
# Query bounds: batch-bounded, never row-proportional
# ---------------------------------------------------------------------------


class TestQueryBounds:
    def _count(self, fn):
        with CaptureQueriesContext(connection) as ctx:
            fn()
        return len(ctx.captured_queries)

    def _reads(self, fn):
        with CaptureQueriesContext(connection) as ctx:
            fn()
        return [
            q["sql"]
            for q in ctx.captured_queries
            if not q["sql"].lstrip().upper().startswith("INSERT")
        ]

    def test_rebuild_reads_constant_small_vs_large(self):
        # READS are batch-bounded (no N+1): identical read patterns for a
        # small and a 15x corpus in one batch. WRITES are bounded chunks
        # (bulk_create driver batching), so they are excluded and counted
        # separately below.
        _corpus(recordings=2, segments=3)
        si.rebuild_index()
        small_reads = self._reads(lambda: si.rebuild_index(batch_size=1000))
        SearchDocument.objects.all().delete()
        made = _corpus(recordings=30, segments=3)
        with CaptureQueriesContext(connection) as ctx:
            si.rebuild_index(batch_size=1000)
        large_reads = [
            q["sql"] for q in ctx.captured_queries
            if not q["sql"].lstrip().upper().startswith("INSERT")
        ]
        inserts = [
            q["sql"] for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith("INSERT")
        ]
        assert len(large_reads) == len(small_reads), (len(small_reads), len(large_reads))
        # every corpus document is written in INSERT-batches, never per row
        total_docs = SearchDocument.objects.count()
        assert total_docs > 30
        assert len(inserts) <= total_docs / 20, (len(inserts), total_docs)

    def test_status_queries_constant_small_vs_large(self):
        _corpus(recordings=2, segments=3)
        si.rebuild_index()
        small = self._count(lambda: si.build_status_report(batch_size=1000))
        _corpus(recordings=30, segments=3)
        si.rebuild_index(batch_size=1000)
        large = self._count(lambda: si.build_status_report(batch_size=1000))
        assert large == small, (small, large)

    def test_status_batches_reduce_queries_for_many_small_batches(self):
        # sanity: batching actually happens (per-batch overhead > 0)
        _corpus(recordings=4, segments=1)
        si.rebuild_index()
        many_batches = self._count(lambda: si.build_status_report(batch_size=1))
        one_batch = self._count(lambda: si.build_status_report(batch_size=1000))
        assert many_batches > one_batch


# ---------------------------------------------------------------------------
# Bounded streaming (a single recording must never accumulate an
# unbounded document list — every yielded chunk is capped)
# ---------------------------------------------------------------------------


class TestBoundedStreaming:
    def test_generator_never_yields_more_than_the_chunk_size(self):
        _corpus(recordings=1, segments=1200, summaries=False)
        sizes: list[int] = []
        flat: list[str] = []
        for batch in si.iter_expected_document_batches(batch_size=1):
            assert 1 <= len(batch) <= si.INSERT_CHUNK_SIZE
            sizes.append(len(batch))
            flat.extend(spec.document_key for spec in batch)
        assert len(sizes) >= 3, sizes  # 1200 segments >> the 500 chunk bound
        assert sum(sizes) == 1201  # 1200 segments + 1 metadata document
        assert len(set(flat)) == 1201  # complete coverage, no duplicates
        ordinals = [int(key.split(":")[2]) for key in flat if key.startswith("segment:")]
        assert ordinals == list(range(1200))  # deterministic order preserved
        assert flat[-1].startswith("recording:")  # metadata doc stays last

    def test_rebuild_of_one_large_recording_inserts_only_bounded_chunks(self, monkeypatch):
        _corpus(recordings=1, segments=1200, summaries=False)
        real = si._insert_fts_batch
        sizes: list[int] = []

        def spy(cursor, rows):
            sizes.append(len(rows))
            return real(cursor, rows)

        monkeypatch.setattr(si, "_insert_fts_batch", spy)
        result = si.rebuild_index(batch_size=1)
        assert result["documents"]["total"] == 1201
        assert sizes and max(sizes) <= si.INSERT_CHUNK_SIZE, sizes
        assert len(sizes) >= 3
        # status over the same oversized recording stays bounded AND exact
        report = si.build_status_report(batch_size=1)
        assert report["healthy"] is True
        assert report["counts"]["source_documents"] == 1201


class TestNonPositiveIdSweeps:
    # SQLite (and FTS5) accept ZERO and NEGATIVE explicit ids/rowids; both
    # keyset sweeps must include them (no > 0 lower bound on the first
    # page) or corruption hides behind a healthy report.

    _FORGED_COLUMNS = (
        "id, document_key, doc_type, recording_id, output_language,"
        " title_text, body_text, aux_text, content_hash, index_version, created_at"
    )

    def _forge_registry_row(self, pk, key):
        rec, _t, _s = make_transcribed_recording(["x"], sha=f"neg-{pk}")
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO workflow_search_document ({self._FORGED_COLUMNS})"
                " VALUES (%s, %s, 'recording', %s, '', '', 'forged', '',"
                " '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',"
                " '1', '2026-01-01 00:00:00')",
                [pk, key, rec.pk],
            )
        return rec

    def test_orphan_fts_rows_at_zero_and_negative_rowids(self, monkeypatch):
        rec, transcript, _section = make_transcribed_recording(["content"], sha="neg-fts")
        si.rebuild_index()
        base = max(SearchDocument.objects.values_list("pk", flat=True))
        with connection.cursor() as cursor:
            for rowid in (-1, 0, base + 500):
                cursor.execute(
                    "INSERT INTO workflow_search_fts (rowid, title_text, body_text, aux_text)"
                    " VALUES (%s, 'orphan', 'orphan', '')",
                    [rowid],
                )
        # force multi-page traversal across the -1/0/positive boundary
        monkeypatch.setattr(si, "INSERT_CHUNK_SIZE", 1)
        report = si.build_status_report()
        assert report["healthy"] is False
        assert report["categories"]["orphan_fts_row"] == 3
        listed = report["keys"]["orphan_fts_row"]
        assert len(listed) == 3
        assert len(set(listed)) == 3  # no duplicates across pages
        assert set(listed) == {"rowid:-1", "rowid:0", f"rowid:{base + 500}"}
        assert "orphan_fts_row" not in report["keys_truncated"]

    def test_orphan_registry_rows_at_zero_and_negative_pks(self, monkeypatch):
        _rec, _transcript, _section = make_transcribed_recording(["content"], sha="neg-doc")
        si.rebuild_index()
        # forged rows at explicit pks -1/0 for a DIFFERENT recording than
        # the rebuilt one (the partial unique on (transcript, ordinal) and
        # the key shape keep them orphans either way)
        for pk in (-1, 0):
            self._forge_registry_row(pk, f"recording:forged-{pk}")
            assert SearchDocument.objects.filter(pk=pk).exists()  # raw -1/0 pk landed
        monkeypatch.setattr(si, "INSERT_CHUNK_SIZE", 2)
        report = si.build_status_report()
        assert report["healthy"] is False
        assert report["categories"]["orphan_document"] == 2
        listed = report["keys"]["orphan_document"]
        assert len(listed) == 2
        assert len(set(listed)) == 2  # no duplicates across pages
        assert set(listed) == {"recording:forged--1", "recording:forged-0"}

    def test_sweeps_terminate_without_duplicates_on_mixed_sign_ids(self, monkeypatch):
        # every legitimate row plus negative/zero/pinned-positive orphans,
        # traversed page by page: exact totals, single listing, termination
        _rec, _transcript, _section = make_transcribed_recording(["a", "b"], sha="neg-mix")
        si.rebuild_index()
        legit = SearchDocument.objects.count()
        base = max(SearchDocument.objects.values_list("pk", flat=True))
        with connection.cursor() as cursor:
            for rowid in (-7, -1, 0, base + 40, base + 41):
                cursor.execute(
                    "INSERT INTO workflow_search_fts (rowid, title_text, body_text, aux_text)"
                    " VALUES (%s, 'orphan', 'orphan', '')",
                    [rowid],
                )
        monkeypatch.setattr(si, "INSERT_CHUNK_SIZE", 2)
        report = si.build_status_report()
        assert report["healthy"] is False
        assert report["categories"]["orphan_fts_row"] == 5
        listed = report["keys"]["orphan_fts_row"]
        assert len(listed) == len(set(listed)) == 5
        assert report["counts"]["registry_documents"] == legit  # untouched
