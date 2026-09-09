"""Service tests for the Step 5B.3 embedding index status/rebuild/repair.

All network is mocked (fake embedder functions replace ``embed_texts``);
no real HTTP, no oMLX, no user data. Covers the exact v1 text mapping,
status purity (SELECT/PRAGMA only, search status exactly once, every
category, capped keys, active-only integrity, deterministic reports),
the rebuild algorithm (happy paths, multi-batch order/dimension,
empty-source probe, atomic supersession, same-contract rebuild, no
network in transaction, bounded writes, every injected failure, the
``PRAGMA data_version`` promotion guard, sanitized errors) and the
repair algorithm (all four classes, at-most-once embedding, no network
for orphan-only/healthy, durable partial progress, incompatible/missing
active, dimension mismatch, concurrent source change, final
convergence).
"""

from __future__ import annotations

import hashlib
import json
import struct

import pytest
from django.db import Error as DjangoDBError
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from brainlib.config import EmbeddingConfig
from factories import make_config, make_transcribed_recording
from workflow.models import (
    EmbeddingDocument,
    EmbeddingGeneration,
    EmbeddingGenerationState,
    Recording,
    SearchDocument,
    TranscriptSegment,
)
from workflow.services import embedding_index as ei
from workflow.services import search_index as si
from workflow.services.embedding_client import (
    EmbeddingBatch,
    EmbeddingError,
    EmbeddingHTTPError,
)
from workflow.services.vector_codec import (
    VectorCodecError,
    decode_vector,
    encode_vector,
)

# transaction=True: public rebuild/repair refuse to run while the caller
# is inside a SQLite transaction, and pytest-django's default outer
# transaction would otherwise trip that fixed precondition.
pytestmark = pytest.mark.django_db(transaction=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def emb_config(tmp_path, **overrides):
    return make_config(
        tmp_path,
        embedding=EmbeddingConfig(
            base_url="http://127.0.0.1:1/v1",
            model=overrides.pop("model", "test-embed-model"),
            api_key_env="BRAIN_TEST_LLM_API_KEY",
            timeout_seconds=120,
            batch_size=overrides.pop("batch_size", 32),
        ),
    )


def build_source(*, recordings=1, segments=1):
    for i in range(recordings):
        make_transcribed_recording(
            [f"record {i} segment {j} content" for j in range(segments)],
            sha=f"emb-{i}-{recordings}x{segments}",
        )
    si.rebuild_index()


def make_embedder(dim=4, *, fail_calls=(), dims_by_call=None, guard_no_txn=False,
                  tracker=None, mutate_call=None, fill=0.1):
    state = {"calls": 0}

    def embed(config, texts):
        state["calls"] += 1
        if tracker is not None:
            tracker.append(list(texts))
        if guard_no_txn:
            assert connection.in_atomic_block is False, "network inside a transaction"
        if state["calls"] in fail_calls:
            raise EmbeddingHTTPError(503)
        if mutate_call is not None and state["calls"] in mutate_call:
            mutate_call[state["calls"]]()
        call_dim = dim
        if dims_by_call and state["calls"] in dims_by_call:
            call_dim = dims_by_call[state["calls"]]
        return [
            EmbeddingBatch(text=t, embedding=tuple([fill] * call_dim)) for t in texts
        ]

    embed.state = state
    return embed


def _missing_count(config):
    """Total current SearchDocument keys lacking an active vector."""
    gen = active_generation()
    active_keys = set(
        EmbeddingDocument.objects.filter(generation=gen).values_list(
            "document_key", flat=True
        )
    )
    return sum(
        1
        for key in SearchDocument.objects.values_list("document_key", flat=True)
        if key not in active_keys
    )


def active_generation():
    return EmbeddingGeneration.objects.get(state=EmbeddingGenerationState.ACTIVE)


def active_snapshot(gen=None):
    gen = gen or active_generation()
    return {
        "state": gen.state,
        "completed_at": gen.completed_at,
        "activated_at": gen.activated_at,
        "superseded_at": gen.superseded_at,
        "failed_at": gen.failed_at,
        "documents": sorted(
            (d.document_key, d.source_content_hash, d.vector_blob)
            for d in EmbeddingDocument.objects.filter(generation=gen)
        ),
    }


# ---------------------------------------------------------------------------
# v1 text mapping
# ---------------------------------------------------------------------------


class TestPrepareText:
    def test_exact_v1_format(self):
        rec, transcript, _s = make_transcribed_recording(["alpha beta"], sha="txt-1")
        si.rebuild_index()
        doc = SearchDocument.objects.get(document_key=f"segment:{transcript.pk}:0")
        text = ei.prepare_document_text(doc)
        assert text.startswith("brain-embedding-v1\n")
        lines = text.split("\n")
        assert len(lines) == 5
        assert lines[0] == "brain-embedding-v1"
        assert lines[1] == "doc_type:7:segment"
        assert lines[2] == "title_text:0:"
        assert lines[3] == "body_text:10:alpha beta"
        assert lines[4] == "aux_text:0:"

    def test_version_constant_binds_mapping(self):
        assert ei.EMBEDDING_VERSION == "1"
        assert ei.EMBEDDING_VERSION != si.INDEX_VERSION or ei.EMBEDDING_VERSION == "1"

    def test_deterministic(self):
        build_source(recordings=1, segments=2)
        docs = list(SearchDocument.objects.order_by("document_key"))
        assert ei.prepare_document_text(docs[0]) == ei.prepare_document_text(docs[0])

    def test_all_fields_bound_no_truncation(self):
        rec, transcript, _s = make_transcribed_recording(
            ["  lead and   internal\nline break  "], sha="txt-2"
        )
        si.rebuild_index()
        doc = SearchDocument.objects.get(document_key=f"segment:{transcript.pk}:0")
        assert ei.prepare_document_text(doc) == (
            "brain-embedding-v1\n"
            "doc_type:7:segment\n"
            "title_text:0:\n"
            "body_text:34:  lead and   internal\nline break  \n"
            "aux_text:0:"
        )

    def test_empty_fields_included_and_nonempty_payload(self):
        build_source(recordings=1, segments=1)
        doc = SearchDocument.objects.get(doc_type="recording")
        text = ei.prepare_document_text(doc)
        assert "title_text:0:" not in text  # recording doc has a title
        assert "aux_text:0:" in text
        assert text.strip()

    def test_doc_type_changes_payload(self):
        build_source(recordings=1, segments=1)
        seg = SearchDocument.objects.get(doc_type="segment")
        rec = SearchDocument.objects.get(doc_type="recording")
        assert ei.prepare_document_text(seg) != ei.prepare_document_text(rec)

    def test_length_prefix_keeps_duplicate_labels_unambiguous(self):
        rec, transcript, _s = make_transcribed_recording(["x"], sha="txt-3")
        si.rebuild_index()
        doc = SearchDocument.objects.get(document_key=f"segment:{transcript.pk}:0")
        doc.body_text = "body_text:99:forged"
        text = ei.prepare_document_text(doc)
        assert "body_text:19:body_text:99:forged" in text

    def test_exact_v1_fixture(self):
        class FakeDoc:
            doc_type = "segment"
            title_text = ""
            body_text = "hello\nworld"
            aux_text = "speaker"

        assert ei.prepare_document_text(FakeDoc()) == (
            "brain-embedding-v1\n"
            "doc_type:7:segment\n"
            "title_text:0:\n"
            "body_text:11:hello\nworld\n"
            "aux_text:7:speaker"
        )

    def test_hostile_fields_rejected_without_calling_str(self):
        class HostileBody:
            doc_type = "segment"
            title_text = ""
            body_text = object()

            def __str__(self):  # pragma: no cover
                raise AssertionError("__str__ must never be invoked")

            def __repr__(self):  # pragma: no cover
                raise AssertionError("__repr__ must never be invoked")

        with pytest.raises(ei.EmbeddingIndexError, match="malformed text fields"):
            ei.prepare_document_text(HostileBody())

    def test_hostile_doc_type_none_and_str_subclass_rejected(self):
        class HostileDocType:
            doc_type = 123
            title_text = ""
            body_text = "x"
            aux_text = ""

        with pytest.raises(ei.EmbeddingIndexError, match="malformed text fields"):
            ei.prepare_document_text(HostileDocType())

        class HostileNone:
            doc_type = "segment"
            title_text = None
            body_text = "x"
            aux_text = ""

        with pytest.raises(ei.EmbeddingIndexError, match="malformed text fields"):
            ei.prepare_document_text(HostileNone())

        class StrSub(str):
            pass

        class HostileSubclass:
            doc_type = "segment"
            title_text = ""
            body_text = StrSub("x")  # not an EXACT str
            aux_text = ""

        with pytest.raises(ei.EmbeddingIndexError, match="malformed text fields"):
            ei.prepare_document_text(HostileSubclass())

    def test_hostile_attributes_missing_rejected(self):
        class MissingAux:
            doc_type = "segment"
            title_text = ""
            body_text = "x"

        with pytest.raises(ei.EmbeddingIndexError, match="malformed text fields"):
            ei.prepare_document_text(MissingAux())


# ---------------------------------------------------------------------------
# Snapshot framing (item 4)
# ---------------------------------------------------------------------------


class TestSnapshotFrame:
    def test_length_prefixed_utf8(self):
        key = "héllo→世界"
        frame = ei._snapshot_frame(key, "a" * 64)
        key_bytes = key.encode("utf-8")
        assert frame == (
            b"S"
            + str(len(key_bytes)).encode("ascii")
            + b":"
            + key_bytes
            + b"S64:"
            + b"a" * 64
        )
        # byte length, not codepoint length
        assert len(key_bytes) != len(key)

    def test_no_boundary_shift_collision(self):
        # adjacent pairs can never be confused with a shifted split
        assert ei._snapshot_frame("ab", "c") + ei._snapshot_frame("de", "f") != (
            ei._snapshot_frame("ab", "cd") + ei._snapshot_frame("e", "f")
        )
        assert ei._snapshot_frame("ab", "c") + ei._snapshot_frame("de", "f") != (
            ei._snapshot_frame("abc", "d") + ei._snapshot_frame("e", "f")
        )
        # hash-like text that resembles framing cannot alias a boundary
        assert ei._snapshot_frame("k", "S3:abc") + ei._snapshot_frame("k2", "h") != (
            ei._snapshot_frame("k", "S3:abcS2:k2")
        )
        # NUL / newline / unicode keys stay unambiguous
        assert ei._snapshot_frame("a\0b", "c") + ei._snapshot_frame("d", "e") != (
            ei._snapshot_frame("a", "b\0c") + ei._snapshot_frame("d", "e")
        )
        assert ei._snapshot_frame("x\n y", "z") != ei._snapshot_frame("x", "\n yz")

    def test_deterministic_and_shared_between_snapshot_kinds(self):
        pairs = [
            ("seg\u0000ment:1:0", "a" * 64),
            ("记录:2", "b" * 64),
            ("plain key", "c" * 64),
        ]
        source = hashlib.sha256()
        generation = hashlib.sha256()
        for key, content_hash in pairs:
            source.update(ei._snapshot_frame(key, content_hash))
            # generation rows carry the copied source content_hash under
            # the same field: byte-identical framing
            generation.update(ei._snapshot_frame(key, content_hash))
        assert source.hexdigest() == generation.hexdigest()
        again = hashlib.sha256()
        for key, content_hash in pairs:
            again.update(ei._snapshot_frame(key, content_hash))
        assert again.hexdigest() == source.hexdigest()

    def test_hostile_values_rejected(self):
        with pytest.raises(ei.EmbeddingIndexError, match="malformed document identity"):
            ei._snapshot_frame(123, "a" * 64)
        with pytest.raises(ei.EmbeddingIndexError, match="malformed document identity"):
            ei._snapshot_frame("k", None)
        with pytest.raises(ei.EmbeddingIndexError, match="malformed document identity"):
            ei._snapshot_frame("k", b"not a str")

    def test_snapshot_update_hostile_row_sanitized(self):
        class HostileDoc:
            document_key = 123
            content_hash = "a" * 64

        with pytest.raises(ei.EmbeddingIndexError, match="malformed document identity"):
            ei._snapshot_update(hashlib.sha256(), [HostileDoc()])


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestStatusBasics:
    def test_healthy_after_rebuild(self, tmp_path):
        build_source(recordings=2, segments=2)
        config = emb_config(tmp_path)
        assert ei.rebuild_embedding_index(config, embedder=make_embedder())["healthy"]
        report = ei.build_embedding_status_report(config)
        assert report["healthy"] is True
        assert report["categories"] == {c: 0 for c in ei.CATEGORIES}
        assert report["schema"]["present"] is True
        assert report["model"]["configured"] is True
        active = report["active_generation"]
        assert active["model"] == "test-embed-model"
        assert active["dimensions"] == 4
        assert active["embedding_version"] == ei.EMBEDDING_VERSION
        assert active["source_index_version"] == si.INDEX_VERSION
        assert report["counts"]["current_search_documents"] >= 6
        assert report["counts"]["active_documents"] == report["counts"]["current_search_documents"]
        assert report["source_index"]["healthy"] is True

    def test_status_is_pure_read_only_no_lock_no_network(self, tmp_path, monkeypatch):
        build_source(recordings=1, segments=2)
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        before = active_snapshot()

        def forbidden(*args, **kwargs):
            raise AssertionError("status must not lock/network/repair")

        monkeypatch.setattr("workflow.services.pipeline_lock.pipeline_lock", forbidden)
        monkeypatch.setattr(ei, "embed_texts", forbidden)
        with CaptureQueriesContext(connection) as ctx:
            report = ei.build_embedding_status_report(config)
        writes = [
            q["sql"] for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith(
                ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "REPLACE")
            )
        ]
        assert writes == []
        assert report["healthy"] is True
        assert active_snapshot() == before

    def test_search_status_called_exactly_once(self, tmp_path, monkeypatch):
        build_source(recordings=1, segments=1)
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        calls = {"n": 0}
        real = si.build_status_report

        def spy(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(si, "build_status_report", spy)
        ei.build_embedding_status_report(config)
        assert calls["n"] == 1

    def test_deterministic_report(self, tmp_path):
        build_source(recordings=2, segments=2)
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        first = ei.build_embedding_status_report(config)
        second = ei.build_embedding_status_report(config)
        assert first == second

    def test_model_not_configured_is_unhealthy_no_network(self, tmp_path, monkeypatch):
        build_source(recordings=1, segments=1)
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        blank_config = emb_config(tmp_path, model="")

        def forbidden(*args, **kwargs):
            raise AssertionError("no network for a blank model")

        monkeypatch.setattr(ei, "embed_texts", forbidden)
        report = ei.build_embedding_status_report(blank_config)
        assert report["healthy"] is False
        assert report["categories"]["model_not_configured"] == 1
        assert report["counts"]["active_documents"] == report["counts"]["current_search_documents"]

    def test_model_identity_is_exact_with_padding(self, tmp_path):
        # The configured model is the EXACT canonical string: blankness is
        # tested with strip() only; the request, stored generation, status
        # identity and expected model are never stripped/canonicalized.
        build_source(recordings=1, segments=1)
        config = emb_config(tmp_path, model="  padded-embed-model  ")
        seen = {}

        def embed(config, texts):
            seen["request_model"] = config.embedding.model
            return [EmbeddingBatch(text=t, embedding=(0.1, 0.1, 0.1, 0.1)) for t in texts]

        result = ei.rebuild_embedding_index(config, embedder=embed)
        assert seen["request_model"] == "  padded-embed-model  "
        gen = active_generation()
        assert gen.model == "  padded-embed-model  "
        assert result["model"] == "  padded-embed-model  "
        report = ei.build_embedding_status_report(config)
        assert report["healthy"] is True
        assert report["active_generation"]["model"] == "  padded-embed-model  "
        assert report["expected"]["model"] == "  padded-embed-model  "
        assert report["model"]["name"] == "  padded-embed-model  "

    def test_status_helper_failure_sanitized(self, tmp_path, monkeypatch):
        build_source(recordings=1, segments=2)
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=make_embedder())

        def boom(*args, **kwargs):
            raise RuntimeError("CANARY-STATUS-HELPER-SECRET")

        monkeypatch.setattr(ei, "_active_page_invalid", boom)
        with pytest.raises(ei.EmbeddingIndexError) as excinfo:
            ei.build_embedding_status_report(config)
        assert "CANARY-STATUS-HELPER-SECRET" not in str(excinfo.value)

    def test_status_db_failure_sanitized(self, tmp_path, monkeypatch):
        build_source(recordings=1, segments=2)
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=make_embedder())

        def boom(*args, **kwargs):
            raise DjangoDBError("CANARY-STATUS-DB-SECRET")

        monkeypatch.setattr(ei, "_iter_search_document_pages", boom)
        with pytest.raises(ei.EmbeddingIndexError) as excinfo:
            ei.build_embedding_status_report(config)
        assert "CANARY-STATUS-DB-SECRET" not in str(excinfo.value)

    def test_status_schema_introspection_failure_sanitized(self, tmp_path, monkeypatch):
        build_source(recordings=1, segments=2)
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=make_embedder())

        def boom(*args, **kwargs):
            raise RuntimeError("CANARY-INTROSPECTION-SECRET")

        monkeypatch.setattr(connection.introspection, "table_names", boom)
        with pytest.raises(ei.EmbeddingIndexError, match="could not be computed") as excinfo:
            ei.build_embedding_status_report(config)
        assert "CANARY-INTROSPECTION-SECRET" not in str(excinfo.value)

    def test_schema_helpers_genuine_absence_vs_failure(self, tmp_path, monkeypatch):
        build_source(recordings=1, segments=1)
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        # genuine absence returns False (never misreported as failure)
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_embedding_document")
            cursor.execute("DROP TABLE workflow_embedding_generation")
        try:
            assert ei._embedding_schema_present(using="default") is False
            assert ei._search_registry_present(using="default") is True
        finally:
            with connection.schema_editor() as editor:
                editor.create_model(EmbeddingGeneration)
                editor.create_model(EmbeddingDocument)
        # introspection/query failure PROPAGATES (never swallowed to False)
        def boom(*args, **kwargs):
            raise DjangoDBError("CANARY-INTROSPECTION-DB-SECRET")

        monkeypatch.setattr(connection.introspection, "table_names", boom)
        with pytest.raises(DjangoDBError, match="CANARY-INTROSPECTION-DB-SECRET"):
            ei._embedding_schema_present(using="default")
        with pytest.raises(DjangoDBError, match="CANARY-INTROSPECTION-DB-SECRET"):
            ei._search_registry_present(using="default")

    def test_no_active_generation(self, tmp_path):
        build_source(recordings=1, segments=1)
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        EmbeddingGeneration.objects.all().delete()
        report = ei.build_embedding_status_report(config)
        assert report["healthy"] is False
        assert report["categories"]["no_active_generation"] == 1
        assert report["active_generation"] is None
        assert report["counts"]["missing_document"] == 0


class TestStatusCategories:
    def _setup(self, tmp_path):
        build_source(recordings=1, segments=2)
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        return config

    def test_schema_missing(self, tmp_path):
        config = self._setup(tmp_path)
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_embedding_document")
            cursor.execute("DROP TABLE workflow_embedding_generation")
        try:
            report = ei.build_embedding_status_report(config)
            assert report["categories"]["schema_missing"] == 1
            assert report["healthy"] is False
            assert report["schema"]["present"] is False
        finally:
            # transaction=True commits the DROP for real: restore the
            # Django-managed embedding tables for subsequent tests.
            with connection.schema_editor() as editor:
                editor.create_model(EmbeddingGeneration)
                editor.create_model(EmbeddingDocument)

    def test_source_index_unhealthy(self, tmp_path):
        config = self._setup(tmp_path)
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE workflow_search_fts")
        try:
            report = ei.build_embedding_status_report(config)
            assert report["categories"]["source_index_unhealthy"] == 1
            assert report["healthy"] is False
        finally:
            # transaction=True commits the DROP for real: restore the
            # search FTS virtual table via the canonical rebuild.
            si.rebuild_index()

    def test_model_mismatch(self, tmp_path):
        config = self._setup(tmp_path)
        EmbeddingGeneration.objects.filter(state=EmbeddingGenerationState.ACTIVE).update(
            model="different-model"
        )
        report = ei.build_embedding_status_report(config)
        assert report["categories"]["model_mismatch"] == 1
        assert report["healthy"] is False

    def test_embedding_version_mismatch(self, tmp_path):
        config = self._setup(tmp_path)
        EmbeddingGeneration.objects.filter(state=EmbeddingGenerationState.ACTIVE).update(
            embedding_version="2"
        )
        report = ei.build_embedding_status_report(config)
        assert report["categories"]["embedding_version_mismatch"] == 1
        assert report["healthy"] is False

    def test_source_index_version_mismatch(self, tmp_path):
        config = self._setup(tmp_path)
        EmbeddingGeneration.objects.filter(state=EmbeddingGenerationState.ACTIVE).update(
            source_index_version="9"
        )
        report = ei.build_embedding_status_report(config)
        assert report["categories"]["source_index_version_mismatch"] == 1
        assert report["healthy"] is False

    def test_missing_document(self, tmp_path):
        config = self._setup(tmp_path)
        make_transcribed_recording(["new recording text"], sha="missing-doc")
        si.rebuild_index()
        report = ei.build_embedding_status_report(config)
        assert report["categories"]["missing_document"] >= 1
        assert report["counts"]["missing_document"] >= 1
        assert report["healthy"] is False

    def test_stale_content(self, tmp_path):
        config = self._setup(tmp_path)
        rec, transcript, _s = make_transcribed_recording(["stale seed"], sha="stale-doc")
        si.rebuild_index()
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        TranscriptSegment.objects.filter(transcript=transcript, ordinal=0).update(
            text="edited source"
        )
        si.rebuild_index()
        report = ei.build_embedding_status_report(config)
        assert report["categories"]["stale_content"] == 1
        assert report["counts"]["stale_content"] == 1
        assert f"segment:{transcript.pk}:0" in report["keys"]["stale_content"]

    def test_orphan_document(self, tmp_path):
        config = self._setup(tmp_path)
        gen = active_generation()
        EmbeddingDocument.objects.create(
            generation=gen,
            document_key="segment:ghost:0",
            source_content_hash="a" * 64,
            vector_blob=encode_vector([0.1] * gen.dimensions, dimensions=gen.dimensions),
        )
        report = ei.build_embedding_status_report(config)
        assert report["categories"]["orphan_document"] == 1
        assert report["keys"]["orphan_document"] == ["segment:ghost:0"]

    def test_invalid_vector_wrong_length(self, tmp_path):
        config = self._setup(tmp_path)
        gen = active_generation()
        doc = EmbeddingDocument.objects.filter(generation=gen).first()
        EmbeddingDocument.objects.filter(pk=doc.pk).update(vector_blob=b"\x00\x00")
        report = ei.build_embedding_status_report(config)
        assert report["categories"]["invalid_vector"] == 1
        assert report["keys"]["invalid_vector"] == [doc.document_key]

    def test_invalid_vector_nonfinite(self, tmp_path):
        config = self._setup(tmp_path)
        gen = active_generation()
        doc = EmbeddingDocument.objects.filter(generation=gen).first()
        bad = struct.pack(f"<{gen.dimensions}f", *([1.0] * (gen.dimensions - 1) + [float("nan")]))
        EmbeddingDocument.objects.filter(pk=doc.pk).update(vector_blob=bad)
        report = ei.build_embedding_status_report(config)
        assert report["categories"]["invalid_vector"] == 1
        assert report["healthy"] is False

    def test_oversized_blob_classified_via_length_without_decode(self, tmp_path):
        config = self._setup(tmp_path)
        gen = active_generation()
        doc = EmbeddingDocument.objects.filter(generation=gen).first()
        # A malicious oversized blob must be classified by DB length
        # alone — never loaded/decoded.
        EmbeddingDocument.objects.filter(pk=doc.pk).update(
            vector_blob=b"\x00" * (1024 * 1024)
        )
        report = ei.build_embedding_status_report(config)
        assert report["categories"]["invalid_vector"] == 1
        assert report["counts"]["invalid_vector"] == 1

    def test_keys_capped_with_exact_omitted_count(self, tmp_path):
        config = self._setup(tmp_path)
        for i in range(25):
            make_transcribed_recording([f"cap recording {i}"], sha=f"cap-{i}")
        si.rebuild_index()
        total_missing = _missing_count(config)
        assert total_missing == 50  # segment + recording metadata doc per recording
        report = ei.build_embedding_status_report(config)
        assert ei.KEY_LIMIT == 20
        assert report["counts"]["missing_document"] == 50
        assert len(report["keys"]["missing_document"]) == 20
        assert report["keys_truncated"]["missing_document"] == 30

    def test_active_only_integrity(self, tmp_path):
        config = self._setup(tmp_path)
        now = timezone.now()
        for state in (EmbeddingGenerationState.SUPERSEDED, EmbeddingGenerationState.FAILED):
            gen = EmbeddingGeneration.objects.create(
                model="test-embed-model", dimensions=4,
                embedding_version="1", source_index_version=si.INDEX_VERSION,
                state=state,
                completed_at=now if state != EmbeddingGenerationState.FAILED else None,
                activated_at=now if state != EmbeddingGenerationState.FAILED else None,
                superseded_at=now if state == EmbeddingGenerationState.SUPERSEDED else None,
                failed_at=now if state == EmbeddingGenerationState.FAILED else None,
            )
            EmbeddingDocument.objects.create(
                generation=gen, document_key="segment:junk:0",
                source_content_hash="0" * 64,
                vector_blob=b"\x00\x00",  # malformed: irrelevant for non-active
            )
        EmbeddingGeneration.objects.create(
            model="test-embed-model", dimensions=4,
            embedding_version="1", source_index_version=si.INDEX_VERSION,
        )  # building
        report = ei.build_embedding_status_report(config)
        assert report["generations"]["superseded"] == 1
        assert report["generations"]["failed"] == 1
        assert report["generations"]["building"] == 1
        assert report["generations"]["active"] == 1
        assert report["healthy"] is True  # garbage in non-active generations is irrelevant

    def test_report_contains_no_document_text(self, tmp_path):
        build_source(recordings=1, segments=1)
        rec, transcript, _s = make_transcribed_recording(
            ["SUPER-SECRET-EMBEDDING-CONTENT"], sha="secret-1"
        )
        si.rebuild_index()
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        TranscriptSegment.objects.filter(transcript=transcript, ordinal=0).update(
            text="SUPER-SECRET-EMBEDDING-CONTENT changed"
        )
        si.rebuild_index()
        report = ei.build_embedding_status_report(config)
        assert "SUPER-SECRET-EMBEDDING-CONTENT" not in json.dumps(report)


# ---------------------------------------------------------------------------
# Rebuild
# ---------------------------------------------------------------------------


class TestRebuild:
    def test_happy_path_single_batch(self, tmp_path):
        build_source(recordings=1, segments=2)
        config = emb_config(tmp_path)
        tracker = []
        result = ei.rebuild_embedding_index(
            config, embedder=make_embedder(dim=4, tracker=tracker)
        )
        assert result["result"] == "rebuilt"
        assert result["healthy"] is True
        assert result["dimensions"] == 4
        assert result["documents"] == SearchDocument.objects.count()
        assert result["batches"] == 1
        gen = active_generation()
        assert gen.model == "test-embed-model"
        assert gen.dimensions == 4
        assert gen.embedding_version == ei.EMBEDDING_VERSION
        assert gen.source_index_version == si.INDEX_VERSION
        assert EmbeddingDocument.objects.filter(generation=gen).count() == result["documents"]
        assert ei.build_embedding_status_report(config)["healthy"] is True

    def test_multi_batch_order_and_dimension(self, tmp_path):
        build_source(recordings=2, segments=2)  # 6 docs with batch_size=2 -> 3 batches
        config = emb_config(tmp_path, batch_size=2)
        tracker = []
        result = ei.rebuild_embedding_index(
            config, embedder=make_embedder(dim=3, tracker=tracker)
        )
        assert result["batches"] == 3
        assert all(len(b) <= 2 for b in tracker), tracker
        gen = active_generation()
        keys = list(
            EmbeddingDocument.objects.filter(generation=gen)
            .order_by("document_key")
            .values_list("document_key", flat=True)
        )
        assert keys == sorted(
            SearchDocument.objects.values_list("document_key", flat=True)
        )
        for d in EmbeddingDocument.objects.filter(generation=gen):
            assert len(decode_vector(d.vector_blob, dimensions=3)) == 3
        assert ei.build_embedding_status_report(config)["healthy"] is True

    def test_empty_source_synthetic_probe(self, tmp_path):
        si.rebuild_index()  # zero recordings -> zero SearchDocuments, healthy
        config = emb_config(tmp_path)
        tracker = []
        result = ei.rebuild_embedding_index(
            config, embedder=make_embedder(dim=7, tracker=tracker)
        )
        assert tracker == [[ei.SYNTHETIC_DIMENSION_PROBE]]
        gen = active_generation()
        assert gen.dimensions == 7
        assert EmbeddingDocument.objects.filter(generation=gen).count() == 0
        assert result["documents"] == 0
        assert ei.build_embedding_status_report(config)["healthy"] is True

    def test_prior_active_atomic_supersession(self, tmp_path):
        build_source(recordings=1, segments=2)
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=make_embedder(dim=4))
        old = active_generation()
        old_snapshot = active_snapshot(old)
        result = ei.rebuild_embedding_index(config, embedder=make_embedder(dim=4))
        old.refresh_from_db()
        assert old.state == EmbeddingGenerationState.SUPERSEDED
        assert old.superseded_at is not None
        assert old_snapshot["documents"]  # history retained
        new = active_generation()
        assert new.pk != old.pk
        assert result["prior_active_generation"] == old.pk
        assert ei.build_embedding_status_report(config)["healthy"] is True

    def test_same_contract_rebuild_coexists(self, tmp_path):
        build_source(recordings=1, segments=2)
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        assert EmbeddingGeneration.objects.count() == 2
        assert active_generation().state == EmbeddingGenerationState.ACTIVE
        assert ei.build_embedding_status_report(config)["healthy"] is True

    def test_batch_writes_bounded(self, tmp_path, monkeypatch):
        build_source(recordings=2, segments=2)
        config = emb_config(tmp_path, batch_size=2)
        sizes = []
        real = ei._persist_batch

        def spy(generation, page, embedded, dimensions, using):
            sizes.append(len(page))
            return real(generation, page, embedded, dimensions, using)

        monkeypatch.setattr(ei, "_persist_batch", spy)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        assert sizes == [2, 2, 2]

    def test_source_unhealthy_zero_work(self, tmp_path):
        build_source(recordings=1, segments=1)
        rec, transcript, _s = make_transcribed_recording(["stale"], sha="pref-1")
        TranscriptSegment.objects.filter(transcript=transcript, ordinal=0).update(
            text="changed without rebuild"
        )
        config = emb_config(tmp_path)
        with pytest.raises(ei.EmbeddingIndexError, match="search index is not healthy"):
            ei.rebuild_embedding_index(config, embedder=make_embedder())
        assert EmbeddingGeneration.objects.count() == 0

    def test_first_call_failure_creates_no_generation(self, tmp_path):
        build_source(recordings=1, segments=2)
        config = emb_config(tmp_path)
        with pytest.raises(ei.EmbeddingIndexError, match="embedding request failed"):
            ei.rebuild_embedding_index(config, embedder=make_embedder(fail_calls=(1,)))
        assert EmbeddingGeneration.objects.count() == 0
        assert EmbeddingDocument.objects.count() == 0

    def test_mid_call_failure_marks_generation_failed_old_active_unchanged(
        self, tmp_path
    ):
        build_source(recordings=2, segments=2)
        config = emb_config(tmp_path, batch_size=2)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        old = active_generation()
        old_snapshot = active_snapshot(old)
        with pytest.raises(ei.EmbeddingIndexError, match="embedding request failed"):
            ei.rebuild_embedding_index(
                config, embedder=make_embedder(fail_calls=(2,))
            )
        failed = EmbeddingGeneration.objects.exclude(pk=old.pk).get()
        assert failed.state == EmbeddingGenerationState.FAILED
        assert failed.failed_at is not None
        # bounded partial docs retained (first batch of 2 persisted)
        assert EmbeddingDocument.objects.filter(generation=failed).count() == 2
        assert active_snapshot(old) == old_snapshot
        assert old.state == EmbeddingGenerationState.ACTIVE

    def test_dimension_change_across_batches(self, tmp_path):
        build_source(recordings=2, segments=2)
        config = emb_config(tmp_path, batch_size=2)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        old = active_generation()
        old_snapshot = active_snapshot(old)
        with pytest.raises(ei.EmbeddingIndexError, match="dimensions are inconsistent"):
            ei.rebuild_embedding_index(
                config, embedder=make_embedder(dims_by_call={2: 8})
            )
        failed = EmbeddingGeneration.objects.exclude(pk=old.pk).get()
        assert failed.state == EmbeddingGenerationState.FAILED
        assert EmbeddingDocument.objects.filter(generation=failed).count() == 2
        assert active_snapshot(old) == old_snapshot

    def test_source_change_before_batch_write_aborts(self, tmp_path):
        build_source(recordings=2, segments=2)
        config = emb_config(tmp_path, batch_size=2)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        old = active_generation()
        old_snapshot = active_snapshot(old)

        def mutate_all():
            SearchDocument.objects.update(content_hash="f" * 64)

        with pytest.raises(ei.EmbeddingIndexError, match="changed while building"):
            ei.rebuild_embedding_index(
                config, embedder=make_embedder(mutate_call={2: mutate_all})
            )
        failed = EmbeddingGeneration.objects.exclude(pk=old.pk).get()
        assert failed.state == EmbeddingGenerationState.FAILED
        assert EmbeddingDocument.objects.filter(generation=failed).count() == 2
        assert active_snapshot(old) == old_snapshot

    def test_source_change_before_final_validation(self, tmp_path, monkeypatch):
        build_source(recordings=2, segments=2)
        config = emb_config(tmp_path, batch_size=2)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        old = active_generation()
        old_snapshot = active_snapshot(old)
        real = ei._validate_before_promotion
        mutated = {"done": False}

        def flaky(generation, snapshot, dimensions, using):
            if not mutated["done"]:
                # A canonical source row arrives after all batches were
                # written but before final validation: the fresh source
                # key/hash snapshot no longer equals the processing one.
                make_transcribed_recording(["late arrival"], sha="late-arrival")
                si.rebuild_index()
                mutated["done"] = True
            return real(generation, snapshot, dimensions, using)

        monkeypatch.setattr(ei, "_validate_before_promotion", flaky)
        with pytest.raises(ei.EmbeddingIndexError, match="changed during building"):
            ei.rebuild_embedding_index(config, embedder=make_embedder())
        failed = EmbeddingGeneration.objects.exclude(pk=old.pk).get()
        assert failed.state == EmbeddingGenerationState.FAILED
        assert active_snapshot(old) == old_snapshot

    def test_data_version_promotion_gap_fails_conservatively(self, tmp_path, monkeypatch):
        build_source(recordings=2, segments=2)
        config = emb_config(tmp_path, batch_size=2)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        old = active_generation()
        old_snapshot = active_snapshot(old)
        calls = {"n": 0}
        real = ei._pragma_data_version

        def flaky(using):
            calls["n"] += 1
            if calls["n"] == 1:
                return 1
            return 2  # another connection committed after the pre-validation read

        monkeypatch.setattr(ei, "_pragma_data_version", flaky)
        with pytest.raises(ei.EmbeddingIndexError, match="changed concurrently"):
            ei.rebuild_embedding_index(config, embedder=make_embedder())
        assert calls["n"] >= 2
        failed = EmbeddingGeneration.objects.exclude(pk=old.pk).get()
        assert failed.state == EmbeddingGenerationState.FAILED
        assert active_snapshot(old) == old_snapshot

    def test_promotion_failure_rolls_back_old_active_unchanged(self, tmp_path, monkeypatch):
        from django.utils import timezone as tz

        build_source(recordings=1, segments=2)
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        old = active_generation()
        old_snapshot = active_snapshot(old)

        def broken_promote(generation, prior_active_id, data_version_before, using):
            now = tz.now()
            with transaction.atomic(using=using):
                if prior_active_id is not None:
                    EmbeddingGeneration.objects.using(using).filter(
                        pk=prior_active_id, state=EmbeddingGenerationState.ACTIVE
                    ).update(state=EmbeddingGenerationState.SUPERSEDED, superseded_at=now)
                raise DjangoDBError("synthetic promotion failure")

        monkeypatch.setattr(ei, "_promote", broken_promote)
        with pytest.raises(ei.EmbeddingIndexError, match="database operation failed"):
            ei.rebuild_embedding_index(config, embedder=make_embedder())
        # the whole promotion transaction rolled back: old active unchanged
        assert active_snapshot(old) == old_snapshot
        assert old.state == EmbeddingGenerationState.ACTIVE
        failed = EmbeddingGeneration.objects.exclude(pk=old.pk).get()
        assert failed.state == EmbeddingGenerationState.FAILED

    def test_errors_never_contain_content_or_raw_details(self, tmp_path):
        rec, transcript, _s = make_transcribed_recording(
            ["SUPER-SECRET-EMBEDDING-VALUE"], sha="secret-rebuild"
        )
        si.rebuild_index()
        config = emb_config(tmp_path)
        with pytest.raises(ei.EmbeddingIndexError) as excinfo:
            ei.rebuild_embedding_index(
                config, embedder=make_embedder(fail_calls=(1,))
            )
        text = str(excinfo.value)
        assert "SUPER-SECRET-EMBEDDING-VALUE" not in text
        assert "Traceback" not in text
        assert "http_error" in text

    def test_no_network_inside_transaction(self, tmp_path):
        # transaction=True (module-wide): the pytest outer transaction
        # must not create false positives for the in_atomic_block guard.
        build_source(recordings=2, segments=2)
        config = emb_config(tmp_path, batch_size=2)
        embedder = make_embedder(guard_no_txn=True)
        result = ei.rebuild_embedding_index(config, embedder=embedder)
        assert result["healthy"] is True
        assert embedder.state["calls"] == 3

    def test_precondition_rejects_caller_transaction_zero_embedder_calls(self, tmp_path):
        build_source(recordings=1, segments=1)
        config = emb_config(tmp_path)

        def forbidden(config, texts):
            raise AssertionError("no embedder call inside a caller transaction")

        with transaction.atomic():
            with pytest.raises(ei.EmbeddingIndexError, match="database transaction"):
                ei.rebuild_embedding_index(config, embedder=forbidden)

    def test_batch_size_over_hard_max_rejected_zero_embedder_calls(self, tmp_path):
        build_source(recordings=1, segments=1)
        config = emb_config(tmp_path, batch_size=200)

        def forbidden(config, texts):
            raise AssertionError("no embedding work for an invalid batch_size")

        with pytest.raises(ei.EmbeddingIndexError, match="batch_size"):
            ei.rebuild_embedding_index(config, embedder=forbidden)

    def test_unexpected_embedder_failure_sanitized_and_generation_failed(self, tmp_path):
        build_source(recordings=2, segments=2)
        config = emb_config(tmp_path, batch_size=2)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        old = active_generation()
        old_snapshot = active_snapshot(old)
        state = {"calls": 0}

        def explode(config, texts):
            state["calls"] += 1
            if state["calls"] >= 2:
                raise RuntimeError("CANARY-UNEXPECTED-REBUILD-SECRET")
            return [EmbeddingBatch(text=t, embedding=(0.1, 0.1, 0.1, 0.1)) for t in texts]

        with pytest.raises(ei.EmbeddingIndexError, match="failed unexpectedly") as excinfo:
            ei.rebuild_embedding_index(config, embedder=explode)
        assert "CANARY-UNEXPECTED-REBUILD-SECRET" not in str(excinfo.value)
        failed = EmbeddingGeneration.objects.exclude(pk=old.pk).get()
        assert failed.state == EmbeddingGenerationState.FAILED
        assert active_snapshot(old) == old_snapshot

    def test_no_post_promotion_status_sweep_and_independent_health(self, tmp_path, monkeypatch):
        build_source(recordings=1, segments=2)
        config = emb_config(tmp_path)
        calls = {"status": 0}
        real = si.build_status_report

        def spy(*args, **kwargs):
            calls["status"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(si, "build_status_report", spy)
        result = ei.rebuild_embedding_index(config, embedder=make_embedder())
        # Exactly two sweeps: the preflight and the pre-promotion
        # validation. There is NO post-promotion sweep.
        assert calls["status"] == 2
        assert result["healthy"] is True
        # Health is proven independently by a fresh status report.
        assert ei.build_embedding_status_report(config)["healthy"] is True

    def test_promotion_active_identity_race(self, tmp_path, monkeypatch):
        build_source(recordings=1, segments=2)
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        old = active_generation()
        old_snapshot = active_snapshot(old)
        # Simulate a concurrent active appearing after prior capture: the
        # captured prior id (None) no longer matches the current active.
        monkeypatch.setattr(ei, "_active_generation_id", lambda using: None)
        with pytest.raises(ei.EmbeddingIndexError, match="concurrently"):
            ei.rebuild_embedding_index(config, embedder=make_embedder())
        failed = EmbeddingGeneration.objects.exclude(pk=old.pk).get()
        assert failed.state == EmbeddingGenerationState.FAILED
        assert active_snapshot(old) == old_snapshot

    def test_supersede_active_requires_exactly_one_row(self, tmp_path):
        build_source(recordings=1, segments=1)
        config = emb_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        old = active_generation()
        ei._supersede_active(old.pk, timezone.now(), "default")
        old.refresh_from_db()
        assert old.state == EmbeddingGenerationState.SUPERSEDED
        # already superseded -> zero rows affected -> promotion race
        with pytest.raises(ei.EmbeddingIndexError, match="concurrently"):
            ei._supersede_active(old.pk, timezone.now(), "default")

    def test_mark_failed_failure_leaves_detectable_building_generation(self, tmp_path, monkeypatch):
        build_source(recordings=2, segments=2)
        config = emb_config(tmp_path, batch_size=2)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        old = active_generation()
        old_snapshot = active_snapshot(old)

        def broken_now():
            raise RuntimeError("mark-failed write broke")

        monkeypatch.setattr(ei, "_now", broken_now)
        with pytest.raises(ei.EmbeddingIndexError, match="embedding request failed"):
            ei.rebuild_embedding_index(config, embedder=make_embedder(fail_calls=(2,)))
        # the best-effort mark-failed failure was swallowed: the original
        # sanitized failure propagated and the generation stays building
        # (detectable by status; only active usability matters).
        building = EmbeddingGeneration.objects.filter(
            state=EmbeddingGenerationState.BUILDING
        ).first()
        assert building is not None
        assert EmbeddingDocument.objects.filter(generation=building).count() == 2
        assert active_snapshot(old) == old_snapshot

    def test_preflight_exception_sanitized_no_generation(self, tmp_path, monkeypatch):
        build_source(recordings=1, segments=1)
        config = emb_config(tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError("CANARY-REBUILD-PREFLIGHT-SECRET")

        monkeypatch.setattr(si, "build_status_report", boom)
        with pytest.raises(ei.EmbeddingIndexError, match="failed unexpectedly") as excinfo:
            ei.rebuild_embedding_index(config, embedder=make_embedder())
        assert "CANARY-REBUILD-PREFLIGHT-SECRET" not in str(excinfo.value)
        assert EmbeddingGeneration.objects.count() == 0

    def test_schema_check_exception_sanitized_no_generation(self, tmp_path, monkeypatch):
        build_source(recordings=1, segments=1)
        config = emb_config(tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError("CANARY-REBUILD-SCHEMA-SECRET")

        monkeypatch.setattr(ei, "_embedding_schema_present", boom)
        with pytest.raises(ei.EmbeddingIndexError, match="failed unexpectedly") as excinfo:
            ei.rebuild_embedding_index(config, embedder=make_embedder())
        assert "CANARY-REBUILD-SCHEMA-SECRET" not in str(excinfo.value)
        assert EmbeddingGeneration.objects.count() == 0

    def test_hostile_embedding_code_allowlisted(self, tmp_path):
        build_source(recordings=1, segments=1)
        config = emb_config(tmp_path)

        class HostileError(EmbeddingError):
            code = "CANARY-HOSTILE-EMBEDDING-CODE"

        def hostile(config, texts):
            raise HostileError()

        with pytest.raises(ei.EmbeddingIndexError) as excinfo:
            ei.rebuild_embedding_index(config, embedder=hostile)
        text = str(excinfo.value)
        assert "CANARY-HOSTILE-EMBEDDING-CODE" not in text
        assert "(embedding_error)" in text  # fixed generic category

    def test_hostile_codec_code_allowlisted(self, tmp_path, monkeypatch):
        build_source(recordings=1, segments=1)
        config = emb_config(tmp_path)

        class HostileCodecError(VectorCodecError):
            def __init__(self):
                super().__init__("CANARY-HOSTILE-CODEC-CODE", "hostile")

        def broken_encode(values, *, dimensions):
            raise HostileCodecError()

        monkeypatch.setattr(ei, "encode_vector", broken_encode)
        with pytest.raises(ei.EmbeddingIndexError) as excinfo:
            ei.rebuild_embedding_index(config, embedder=make_embedder())
        text = str(excinfo.value)
        assert "CANARY-HOSTILE-CODEC-CODE" not in text
        # _persist_batch already collapses codec failures to the fixed
        # sanitized vector message (no code echoed at all)
        assert text == "embedding vectors could not be encoded"

    def test_code_allowlist_helpers(self):
        # known stable codes stay surfaced
        assert "embedding request failed (http_error)" in ei._request_error("http_error")
        assert ei._vector_error("invalid_values") == (
            "embedding vectors could not be encoded (invalid_values)"
        )
        # unknown/hostile codes map to the fixed generic category
        assert "(embedding_error)" in ei._request_error("CANARY-HOSTILE-EMBEDDING-CODE")
        assert ei._request_error(12345) == ei._request_error("embedding_error")
        assert ei._vector_error("CANARY-HOSTILE-CODEC-CODE") == (
            "embedding vectors could not be encoded (vector_codec_error)"
        )
        assert ei._vector_error(None) == ei._vector_error("vector_codec_error")

    def test_known_stable_codes_still_surfaced(self, tmp_path):
        build_source(recordings=1, segments=1)
        config = emb_config(tmp_path)
        with pytest.raises(ei.EmbeddingIndexError) as excinfo:
            ei.rebuild_embedding_index(
                config, embedder=make_embedder(fail_calls=(1,))
            )
        assert "(http_error)" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


class TestRepair:
    def _build(self, tmp_path):
        build_source(recordings=1, segments=2)
        config = emb_config(tmp_path, batch_size=2)
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        return config

    def test_repair_all_four_classes(self, tmp_path):
        config = self._build(tmp_path)
        # missing: new recording source
        rec, transcript, section = make_transcribed_recording(
            ["brand new recording text"], sha="repair-missing"
        )
        # stale: existing source edited
        stale_rec, stale_transcript, _s = make_transcribed_recording(
            ["stale repair text"], sha="repair-stale"
        )
        si.rebuild_index()
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        TranscriptSegment.objects.filter(transcript=stale_transcript, ordinal=0).update(
            text="stale repair text EDITED"
        )
        si.rebuild_index()
        # invalid: corrupt an active vector
        gen = active_generation()
        victim = EmbeddingDocument.objects.filter(generation=gen).first()
        EmbeddingDocument.objects.filter(pk=victim.pk).update(vector_blob=b"\x00\x00")
        # orphan: bogus active vector
        EmbeddingDocument.objects.create(
            generation=gen, document_key="segment:ghost-repair:0",
            source_content_hash="a" * 64,
            vector_blob=encode_vector([0.1] * gen.dimensions, dimensions=gen.dimensions),
        )

        before = dict(
            (d.document_key, d.vector_blob)
            for d in EmbeddingDocument.objects.filter(generation=gen)
        )
        result = ei.repair_embedding_index(config, embedder=make_embedder())
        assert result["result"] == "repaired"
        assert result["healthy"] is True
        assert ei.build_embedding_status_report(config)["healthy"] is True
        gen.refresh_from_db()
        assert gen.state == EmbeddingGenerationState.ACTIVE
        # orphan deleted
        assert not EmbeddingDocument.objects.filter(
            generation=gen, document_key="segment:ghost-repair:0"
        ).exists()
        # invalid vector repaired
        assert EmbeddingDocument.objects.get(
            generation=gen, pk=victim.pk
        ).vector_blob == encode_vector([0.1] * 4, dimensions=4)
        # missing + stale now present with correct hashes
        current = dict(SearchDocument.objects.values_list("document_key", "content_hash"))
        for d in EmbeddingDocument.objects.filter(generation=gen):
            assert current[d.document_key] == d.source_content_hash

    def test_each_work_row_embedded_at_most_once(self, tmp_path):
        config = self._build(tmp_path)
        # 3 stale rows
        for i in range(3):
            make_transcribed_recording(
                [f"stale multi {i}"], sha=f"once-{i}"
            )
        si.rebuild_index()
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        gen = active_generation()
        for i in range(3):
            rec = Recording.objects.get(
                sha256=f"once-{i}"
            )
            TranscriptSegment.objects.filter(transcript__recording=rec).update(
                text=f"stale multi {i} edited"
            )
        si.rebuild_index()
        # 2 orphans
        for i in range(2):
            EmbeddingDocument.objects.create(
                generation=gen, document_key=f"segment:orphan-once:{i}",
                source_content_hash="b" * 64,
                vector_blob=encode_vector([0.1] * gen.dimensions, dimensions=gen.dimensions),
            )
        tracker = []
        result = ei.repair_embedding_index(
            config, embedder=make_embedder(tracker=tracker)
        )
        assert result["embedded"] == 3
        flat = [t for batch in tracker for t in batch]
        assert len(flat) == 3  # each stale row exactly once; orphans never embedded
        assert len(set(flat)) == 3
        assert "segment:orphan-once:0" not in " ".join(flat)
        assert result["deleted_orphans"] == 2
        assert ei.build_embedding_status_report(config)["healthy"] is True

    def test_only_orphan_no_network(self, tmp_path):
        config = self._build(tmp_path)
        gen = active_generation()
        for i in range(3):
            EmbeddingDocument.objects.create(
                generation=gen, document_key=f"segment:orphan-only:{i}",
                source_content_hash="c" * 64,
                vector_blob=encode_vector([0.1] * gen.dimensions, dimensions=gen.dimensions),
            )

        def forbidden(config, texts):
            raise AssertionError("no embedding work needed for orphan-only repair")

        result = ei.repair_embedding_index(config, embedder=forbidden)
        assert result["deleted_orphans"] == 3
        assert result["embedded"] == 0
        assert ei.build_embedding_status_report(config)["healthy"] is True

    def test_already_healthy_no_network_no_writes(self, tmp_path):
        config = self._build(tmp_path)
        before = active_snapshot()

        def forbidden(config, texts):
            raise AssertionError("already healthy: no embedding work")

        result = ei.repair_embedding_index(config, embedder=forbidden)
        assert result["embedded"] == 0
        assert result["deleted_orphans"] == 0
        assert result["healthy"] is True
        assert active_snapshot() == before

    def test_partial_progress_durable_on_later_failure(self, tmp_path):
        config = self._build(tmp_path)
        for i in range(4):
            make_transcribed_recording([f"partial repair {i}"], sha=f"partial-{i}")
        si.rebuild_index()
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        gen = active_generation()
        for i in range(4):
            rec = Recording.objects.get(
                sha256=f"partial-{i}"
            )
            TranscriptSegment.objects.filter(transcript__recording=rec).update(
                text=f"partial repair {i} EDITED"
            )
        si.rebuild_index()
        before = dict(
            (d.document_key, d.vector_blob)
            for d in EmbeddingDocument.objects.filter(generation=gen)
        )
        # 4 stale rows, batch_size=2 -> embedder call 1 (batch 1) succeeds
        # with a DIFFERENT fill (detectable durable write), call 2 (batch
        # 2) fails: batch 1 durable, batch 2 not written.
        with pytest.raises(ei.EmbeddingIndexError, match="embedding request failed"):
            ei.repair_embedding_index(
                config, embedder=make_embedder(fail_calls=(2,), fill=0.9)
            )
        gen.refresh_from_db()
        assert gen.state == EmbeddingGenerationState.ACTIVE  # never marked failed
        after = dict(
            (d.document_key, d.vector_blob)
            for d in EmbeddingDocument.objects.filter(generation=gen)
        )
        # exactly 2 rows were replaced (the durable batch 1)
        changed = [k for k in before if k in after and after[k] != before[k]]
        assert len(changed) == 2, changed

    def test_missing_or_incompatible_active_zero_work(self, tmp_path):
        config = self._build(tmp_path)
        gen = active_generation()

        def forbidden(config, texts):
            raise AssertionError("no work allowed")

        EmbeddingGeneration.objects.all().delete()
        with pytest.raises(ei.EmbeddingIndexError, match="no active embedding generation"):
            ei.repair_embedding_index(config, embedder=forbidden)

        # restore a compatible active, then make it incompatible
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        EmbeddingGeneration.objects.filter(state=EmbeddingGenerationState.ACTIVE).update(
            model="incompatible-model"
        )
        with pytest.raises(ei.EmbeddingIndexError, match="incompatible"):
            ei.repair_embedding_index(config, embedder=forbidden)

        EmbeddingGeneration.objects.filter(state=EmbeddingGenerationState.ACTIVE).update(
            model="test-embed-model", embedding_version="2"
        )
        with pytest.raises(ei.EmbeddingIndexError, match="incompatible"):
            ei.repair_embedding_index(config, embedder=forbidden)

        # building/failed/superseded are never chosen
        EmbeddingGeneration.objects.filter(state=EmbeddingGenerationState.ACTIVE).update(
            embedding_version="1"
        )
        active = active_generation()
        for state in (EmbeddingGenerationState.BUILDING, EmbeddingGenerationState.FAILED):
            EmbeddingGeneration.objects.create(
                model="test-embed-model", dimensions=active.dimensions,
                embedding_version="1", source_index_version=si.INDEX_VERSION,
                state=state,
                failed_at=timezone.now()
                if state == EmbeddingGenerationState.FAILED else None,
            )
        # the compatible active still drives repair successfully
        assert ei.repair_embedding_index(config, embedder=make_embedder())["healthy"] is True

    def test_dimension_mismatch_zero_batch_write(self, tmp_path):
        config = self._build(tmp_path)
        for i in range(2):
            make_transcribed_recording([f"dim mismatch {i}"], sha=f"dimm-{i}")
        si.rebuild_index()
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        gen = active_generation()
        for i in range(2):
            rec = Recording.objects.get(
                sha256=f"dimm-{i}"
            )
            TranscriptSegment.objects.filter(transcript__recording=rec).update(
                text=f"dim mismatch {i} EDITED"
            )
        si.rebuild_index()
        before = dict(
            (d.document_key, d.vector_blob)
            for d in EmbeddingDocument.objects.filter(generation=gen)
        )
        with pytest.raises(ei.EmbeddingIndexError, match="different dimension"):
            ei.repair_embedding_index(config, embedder=make_embedder(dim=9))
        after = dict(
            (d.document_key, d.vector_blob)
            for d in EmbeddingDocument.objects.filter(generation=gen)
        )
        assert after == before  # nothing written with the wrong dimension

    def test_concurrent_source_change_not_falsely_updated(self, tmp_path):
        config = self._build(tmp_path)
        rec, transcript, _s = make_transcribed_recording(
            ["UNIQUE-REPAIR-ROW"], sha="race-row"
        )
        si.rebuild_index()
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        gen = active_generation()
        TranscriptSegment.objects.filter(transcript=transcript, ordinal=0).update(
            text="UNIQUE-REPAIR-ROW EDITED"
        )
        si.rebuild_index()
        key = f"segment:{transcript.pk}:0"
        old_blob = EmbeddingDocument.objects.get(generation=gen, document_key=key).vector_blob

        def mutate_source():
            # Another connection changes the source after classification
            # and embedding, before the write transaction.
            SearchDocument.objects.filter(document_key=key).update(
                content_hash="d" * 64
            )

        with pytest.raises(ei.EmbeddingIndexError, match="changed while repairing"):
            ei.repair_embedding_index(
                config, embedder=make_embedder(mutate_call={1: mutate_source})
            )
        # the row was never given false provenance: old vector unchanged
        assert (
            EmbeddingDocument.objects.get(generation=gen, document_key=key).vector_blob
            == old_blob
        )
        assert gen.state == EmbeddingGenerationState.ACTIVE

    def test_repair_final_convergence_after_healthy_embedded_but_orphan(self, tmp_path):
        config = self._build(tmp_path)
        gen = active_generation()
        EmbeddingDocument.objects.create(
            generation=gen, document_key="segment:lone-orphan:0",
            source_content_hash="a" * 64,
            vector_blob=encode_vector([0.1] * gen.dimensions, dimensions=gen.dimensions),
        )
        result = ei.repair_embedding_index(config, embedder=make_embedder())
        assert result["healthy"] is True
        assert result["deleted_orphans"] == 1
        assert ei.build_embedding_status_report(config)["healthy"] is True

    def test_repair_requires_source_health_preflight(self, tmp_path):
        config = self._build(tmp_path)
        rec, transcript, _s = make_transcribed_recording(["x"], sha="repair-pref")
        TranscriptSegment.objects.filter(transcript=transcript, ordinal=0).update(
            text="changed without rebuild"
        )
        with pytest.raises(ei.EmbeddingIndexError, match="search index is not healthy"):
            ei.repair_embedding_index(config, embedder=make_embedder())
        assert EmbeddingGeneration.objects.count() >= 1  # nothing mutated or created

    def test_repair_precondition_rejects_caller_transaction(self, tmp_path):
        config = self._build(tmp_path)

        def forbidden(config, texts):
            raise AssertionError("no embedder call inside a caller transaction")

        with transaction.atomic():
            with pytest.raises(ei.EmbeddingIndexError, match="database transaction"):
                ei.repair_embedding_index(config, embedder=forbidden)

    def test_repair_unexpected_failure_sanitized_active_unchanged(self, tmp_path):
        config = self._build(tmp_path)
        make_transcribed_recording(["repair unexpected"], sha="repair-unexp")
        si.rebuild_index()
        ei.rebuild_embedding_index(config, embedder=make_embedder())
        gen = active_generation()
        rec = Recording.objects.get(sha256="repair-unexp")
        TranscriptSegment.objects.filter(transcript__recording=rec).update(
            text="repair unexpected EDITED"
        )
        si.rebuild_index()
        before = dict(
            (d.document_key, d.vector_blob)
            for d in EmbeddingDocument.objects.filter(generation=gen)
        )

        def explode(config, texts):
            raise RuntimeError("CANARY-UNEXPECTED-REPAIR-SECRET")

        with pytest.raises(ei.EmbeddingIndexError, match="failed unexpectedly") as excinfo:
            ei.repair_embedding_index(config, embedder=explode)
        assert "CANARY-UNEXPECTED-REPAIR-SECRET" not in str(excinfo.value)
        gen.refresh_from_db()
        assert gen.state == EmbeddingGenerationState.ACTIVE  # never marked failed
        after = dict(
            (d.document_key, d.vector_blob)
            for d in EmbeddingDocument.objects.filter(generation=gen)
        )
        assert after == before  # nothing written

    def test_repair_merge_across_page_boundaries(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ei, "_STATUS_PAGE_SIZE", 2)
        config = self._build(tmp_path)
        gen = active_generation()
        # Orphans interleaved with missing keys so the two-stream merge
        # crosses several page boundaries while mutations happen.
        for i in range(3):
            EmbeddingDocument.objects.create(
                generation=gen, document_key=f"segment:orphan-page:{i}",
                source_content_hash="a" * 64,
                vector_blob=encode_vector(
                    [0.1] * gen.dimensions, dimensions=gen.dimensions
                ),
            )
        for i in range(2):
            make_transcribed_recording([f"missing page {i}"], sha=f"mispg-{i}")
        si.rebuild_index()
        tracker = []
        result = ei.repair_embedding_index(
            config, embedder=make_embedder(tracker=tracker)
        )
        assert result["deleted_orphans"] == 3
        assert result["embedded"] == 4  # 2 recordings x (segment + recording doc)
        # No newly repaired row was skipped or deleted during streaming.
        current_keys = set(
            SearchDocument.objects.values_list("document_key", flat=True)
        )
        active_keys = set(
            EmbeddingDocument.objects.filter(generation=gen).values_list(
                "document_key", flat=True
            )
        )
        assert current_keys <= active_keys
        assert ei.build_embedding_status_report(config)["healthy"] is True

    def test_repair_preflight_exception_sanitized(self, tmp_path, monkeypatch):
        config = self._build(tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError("CANARY-REPAIR-PREFLIGHT-SECRET")

        monkeypatch.setattr(si, "build_status_report", boom)
        with pytest.raises(ei.EmbeddingIndexError, match="failed unexpectedly") as excinfo:
            ei.repair_embedding_index(config, embedder=make_embedder())
        assert "CANARY-REPAIR-PREFLIGHT-SECRET" not in str(excinfo.value)
        assert active_generation().state == EmbeddingGenerationState.ACTIVE

    def test_repair_schema_check_exception_sanitized(self, tmp_path, monkeypatch):
        config = self._build(tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError("CANARY-REPAIR-SCHEMA-SECRET")

        monkeypatch.setattr(ei, "_embedding_schema_present", boom)
        with pytest.raises(ei.EmbeddingIndexError, match="failed unexpectedly") as excinfo:
            ei.repair_embedding_index(config, embedder=make_embedder())
        assert "CANARY-REPAIR-SCHEMA-SECRET" not in str(excinfo.value)
        assert active_generation().state == EmbeddingGenerationState.ACTIVE

    def test_repair_active_compat_exception_sanitized(self, tmp_path, monkeypatch):
        config = self._build(tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError("CANARY-REPAIR-ACTIVE-SECRET")

        monkeypatch.setattr(ei, "_require_compatible_active", boom)
        with pytest.raises(ei.EmbeddingIndexError, match="failed unexpectedly") as excinfo:
            ei.repair_embedding_index(config, embedder=make_embedder())
        assert "CANARY-REPAIR-ACTIVE-SECRET" not in str(excinfo.value)
        assert active_generation().state == EmbeddingGenerationState.ACTIVE