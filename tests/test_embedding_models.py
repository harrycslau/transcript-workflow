"""Runtime DB-constraint tests for the Step 5B.2 embedding models.

Exercises the real models against the migrated test database: lifecycle
state/shape allowlist, chronology, one-active enforcement, duplicate
same-identity generations, document per-generation uniqueness, blank
field rejection, dimension bounds and FK CASCADE. DB-level enforcement
of the SAME schema is proven separately by the genuine MigrationExecutor
tests in ``test_embedding_migration.py`` (raw SQL on an isolated
database).

No network, no embedding-client calls, no real config.
"""

from __future__ import annotations

import struct
from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone as tz

from workflow.models import (
    EmbeddingDocument,
    EmbeddingGeneration,
    EmbeddingGenerationState,
)
from workflow.services.vector_codec import (
    VectorCodecError,
    decode_vector,
    encode_vector,
    validate_vector_blob,
)

pytestmark = pytest.mark.django_db


def now():
    return tz.now()


def make_generation(**overrides) -> EmbeddingGeneration:
    fields = {
        "model": "nomic-embed-text-v1.5",
        "dimensions": 768,
        "embedding_version": "1",
        "source_index_version": "1",
    }
    fields.update(overrides)
    return EmbeddingGeneration.objects.create(**fields)


def make_vector(dimensions=3):
    base = [0.25, -1.0, 2.0]
    repeated = (base * (dimensions + 2))[:dimensions]
    return encode_vector(repeated, dimensions=dimensions)


def make_document(generation, **overrides) -> EmbeddingDocument:
    fields = {
        "generation": generation,
        "document_key": "segment:abc:0",
        "source_content_hash": "a" * 64,
        "vector_blob": make_vector(generation.dimensions),
    }
    fields.update(overrides)
    return EmbeddingDocument.objects.create(**fields)


def _raises_integrity(call):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            call()


class TestEmbeddingGenerationStateChoices:
    def test_states_are_exact(self):
        assert [s.value for s in EmbeddingGenerationState] == [
            "building",
            "active",
            "superseded",
            "failed",
        ]


class TestEmbeddingGenerationBasics:
    def test_default_state_is_building_with_all_timestamps_null(self):
        gen = make_generation()
        assert gen.state == EmbeddingGenerationState.BUILDING
        for name in ("completed_at", "activated_at", "superseded_at", "failed_at"):
            assert getattr(gen, name) is None

    def test_duplicate_identity_generations_allowed(self):
        g1 = make_generation()
        g2 = make_generation()
        assert g1.pk != g2.pk
        assert EmbeddingGeneration.objects.count() == 2

    def test_same_contract_building_coexists_with_active(self):
        active = make_generation(
            state=EmbeddingGenerationState.ACTIVE,
            completed_at=now(),
            activated_at=now(),
        )
        assert active.state == EmbeddingGenerationState.ACTIVE
        # Same model/dimension/version contract may start a NEW building
        # generation while the old one is still active (same-contract
        # rebuild coexistence).
        building = make_generation()
        assert building.state == EmbeddingGenerationState.BUILDING
        assert EmbeddingGeneration.objects.count() == 2

    def test_one_active_enforced(self):
        make_generation(
            state=EmbeddingGenerationState.ACTIVE,
            completed_at=now(),
            activated_at=now(),
        )
        _raises_integrity(
            lambda: make_generation(
                state=EmbeddingGenerationState.ACTIVE,
                completed_at=now(),
                activated_at=now(),
            )
        )

    def test_multiple_non_active_states_allowed(self):
        make_generation()  # building
        make_generation(
            state=EmbeddingGenerationState.SUPERSEDED,
            completed_at=now(),
            activated_at=now(),
            superseded_at=now(),
        )
        make_generation(state=EmbeddingGenerationState.FAILED, failed_at=now())
        assert EmbeddingGeneration.objects.count() == 3


class TestEmbeddingGenerationLifecycleShape:
    def _accepted(self, state, **timestamps):
        gen = make_generation(state=state, **timestamps)
        assert gen.state == state
        return gen

    def test_accepted_lifecycle_shapes(self):
        t = now()
        self._accepted(EmbeddingGenerationState.BUILDING)
        self._accepted(
            EmbeddingGenerationState.ACTIVE, completed_at=t, activated_at=t
        )
        self._accepted(
            EmbeddingGenerationState.SUPERSEDED,
            completed_at=t,
            activated_at=t,
            superseded_at=t,
        )
        self._accepted(EmbeddingGenerationState.FAILED, failed_at=t)

    @pytest.mark.parametrize(
        "state,timestamps",
        [
            (EmbeddingGenerationState.BUILDING, {"failed_at": now()}),
            (EmbeddingGenerationState.BUILDING, {"activated_at": now()}),
            (EmbeddingGenerationState.BUILDING, {"completed_at": now()}),
            (EmbeddingGenerationState.BUILDING, {"superseded_at": now()}),
            (EmbeddingGenerationState.ACTIVE, {}),
            (EmbeddingGenerationState.ACTIVE, {"activated_at": now()}),
            (EmbeddingGenerationState.ACTIVE, {"completed_at": now()}),
            (
                EmbeddingGenerationState.ACTIVE,
                {"completed_at": now(), "activated_at": now(), "failed_at": now()},
            ),
            (
                EmbeddingGenerationState.ACTIVE,
                {"completed_at": now(), "activated_at": now(), "superseded_at": now()},
            ),
            (EmbeddingGenerationState.SUPERSEDED, {}),
            (
                EmbeddingGenerationState.SUPERSEDED,
                {"completed_at": now(), "activated_at": now()},
            ),
            (
                EmbeddingGenerationState.SUPERSEDED,
                {
                    "completed_at": now(),
                    "activated_at": now(),
                    "superseded_at": now(),
                    "failed_at": now(),
                },
            ),
            (EmbeddingGenerationState.FAILED, {}),
            (EmbeddingGenerationState.FAILED, {"failed_at": now(), "completed_at": now()}),
            (EmbeddingGenerationState.FAILED, {"failed_at": now(), "activated_at": now()}),
            (EmbeddingGenerationState.FAILED, {"failed_at": now(), "superseded_at": now()}),
            # Unknown states are rejected by the shape CHECK (the lifecycle
            # shape doubles as the explicit DB state allowlist).
            ("mystery", {}),
            ("mystery", {"failed_at": now()}),
            ("paused", {"completed_at": now(), "activated_at": now()}),
        ],
    )
    def test_rejected_lifecycle_shapes(self, state, timestamps):
        _raises_integrity(lambda: make_generation(state=state, **timestamps))


class TestEmbeddingGenerationChronology:
    def test_active_completed_after_activated_rejected(self):
        t0 = now()
        t1 = t0 + timedelta(seconds=5)
        _raises_integrity(
            lambda: make_generation(
                state=EmbeddingGenerationState.ACTIVE,
                completed_at=t1,
                activated_at=t0,
            )
        )

    def test_active_equal_timestamps_accepted(self):
        t = now()
        gen = make_generation(
            state=EmbeddingGenerationState.ACTIVE, completed_at=t, activated_at=t
        )
        assert gen.completed_at <= gen.activated_at

    def test_superseded_activated_after_superseded_rejected(self):
        t0 = now()
        t1 = t0 + timedelta(seconds=5)
        _raises_integrity(
            lambda: make_generation(
                state=EmbeddingGenerationState.SUPERSEDED,
                completed_at=t0,
                activated_at=t1,
                superseded_at=t0,
            )
        )

    def test_superseded_out_of_order_rejected(self):
        t0 = now()
        t1 = t0 + timedelta(seconds=1)
        t2 = t1 + timedelta(seconds=1)
        # completed_at after activated_at
        _raises_integrity(
            lambda: make_generation(
                state=EmbeddingGenerationState.SUPERSEDED,
                completed_at=t2,
                activated_at=t1,
                superseded_at=t2,
            )
        )

    def test_superseded_ordered_timestamps_accepted(self):
        t0 = now()
        t1 = t0 + timedelta(seconds=1)
        t2 = t1 + timedelta(seconds=1)
        gen = make_generation(
            state=EmbeddingGenerationState.SUPERSEDED,
            completed_at=t0,
            activated_at=t1,
            superseded_at=t2,
        )
        assert gen.completed_at <= gen.activated_at <= gen.superseded_at


class TestEmbeddingGenerationDimensionsAndIdentity:
    @pytest.mark.parametrize("dimensions", [0, -1, 16385, 2**40])
    def test_dimension_bounds_rejected(self, dimensions):
        _raises_integrity(lambda: make_generation(dimensions=dimensions))

    @pytest.mark.parametrize("dimensions", [1, 384, 1024, 16384])
    def test_dimension_bounds_accepted(self, dimensions):
        gen = make_generation(dimensions=dimensions)
        assert gen.dimensions == dimensions

    @pytest.mark.parametrize("field", ["model", "embedding_version", "source_index_version"])
    def test_blank_identity_rejected(self, field):
        _raises_integrity(lambda: make_generation(**{field: ""}))


class TestEmbeddingDocument:
    def test_document_created_and_vector_roundtrips(self):
        gen = make_generation(dimensions=3)
        blob = encode_vector([0.5, -1.0, 2.0], dimensions=3)
        doc = make_document(gen, vector_blob=blob)
        fresh = EmbeddingDocument.objects.get(pk=doc.pk)
        assert fresh.embedded_at is not None
        assert decode_vector(fresh.vector_blob, dimensions=gen.dimensions) == (
            0.5,
            -1.0,
            2.0,
        )

    def test_document_key_can_reference_nonexistent_search_document(self):
        # document_key is a COPIED identity, never an FK: a key with no
        # SearchDocument row is storable (rebuild/delete-tolerant).
        gen = make_generation(dimensions=1)
        doc = make_document(gen, document_key="segment:no-such-transcript:0")
        assert EmbeddingDocument.objects.filter(pk=doc.pk).exists()

    def test_duplicate_key_within_generation_rejected(self):
        gen = make_generation(dimensions=1)
        make_document(gen, document_key="segment:a:0")
        _raises_integrity(lambda: make_document(gen, document_key="segment:a:0"))

    def test_same_key_in_two_generations_allowed(self):
        g1 = make_generation(dimensions=1)
        g2 = make_generation(dimensions=1)
        make_document(g1, document_key="segment:a:0")
        make_document(g2, document_key="segment:a:0")
        assert EmbeddingDocument.objects.count() == 2

    def test_blank_key_rejected(self):
        gen = make_generation(dimensions=1)
        _raises_integrity(lambda: make_document(gen, document_key=""))

    def test_blank_source_hash_rejected(self):
        gen = make_generation(dimensions=1)
        _raises_integrity(lambda: make_document(gen, source_content_hash=""))

    def test_empty_vector_blob_rejected(self):
        gen = make_generation(dimensions=1)
        _raises_integrity(lambda: make_document(gen, vector_blob=b""))

    def test_generation_cascade_deletes_documents(self):
        gen = make_generation(dimensions=1)
        make_document(gen, document_key="segment:a:0")
        make_document(gen, document_key="segment:a:1")
        gen.delete()
        assert EmbeddingDocument.objects.count() == 0

    def test_no_redundant_dimensions_column(self):
        field_names = {f.name for f in EmbeddingDocument._meta.concrete_fields}
        assert "dimensions" not in field_names
        assert {"generation", "document_key", "source_content_hash", "vector_blob"} <= field_names

    def test_document_with_blob_short_for_dimensions_rejected_by_no_column_check(
        self,
    ):
        # Cross-table dimension equality is intentionally NOT a DB CHECK
        # (SQLite cannot reference the generation row); the codec is the
        # writer-side enforcer. A mis-length blob is storable at the DB
        # level and must be caught by codec validation.
        gen = make_generation(dimensions=4)
        doc = make_document(gen, vector_blob=struct.pack("<2f", 1.0, 2.0))
        with pytest.raises(VectorCodecError):
            validate_vector_blob(doc.vector_blob, dimensions=gen.dimensions)
