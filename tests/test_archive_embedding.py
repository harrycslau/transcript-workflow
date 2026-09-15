"""Archive/restore embedding-index regression (approved behavior).

Archival is a query-scope eligibility marker, never an index mutation:
archive/restore must leave the recording's ``EmbeddingDocument`` rows
physically present in the SAME compatible active generation, keep
``embedding-index status`` healthy, and cause ZERO embedding-network
calls. All network is mocked; no real HTTP/oMLX/user data.
"""

from __future__ import annotations

import pytest

from brainlib.config import EmbeddingConfig
from factories import make_config, make_transcribed_recording
from workflow.models import (
    EmbeddingDocument,
    EmbeddingGeneration,
    EmbeddingGenerationState,
)
from workflow.services import embedding_index as ei
from workflow.services import search_index as si
from workflow.services.archive import archive_recording, restore_recording
from workflow.services.embedding_client import EmbeddingBatch

# transaction=True: rebuild_embedding_index refuses to run inside a caller
# transaction (pytest-django's default outer transaction would trip it).
pytestmark = pytest.mark.django_db(transaction=True)


def _emb_config(tmp_path):
    return make_config(
        tmp_path,
        embedding=EmbeddingConfig(
            base_url="http://127.0.0.1:1/v1",
            model="test-embed-model",
            api_key_env="BRAIN_TEST_LLM_API_KEY",
            timeout_seconds=120,
            batch_size=32,
        ),
    )


def _embedder():
    def embed(config, texts):
        return [
            EmbeddingBatch(text=t, embedding=(0.5, 0.5, 0.5, 0.5)) for t in texts
        ]

    return embed


def _keys_for(recording, generation):
    keys = set(
        recording.search_documents.values_list("document_key", flat=True)
    )
    stored = set(
        EmbeddingDocument.objects.filter(
            generation=generation, document_key__in=keys
        ).values_list("document_key", flat=True)
    )
    return keys, stored


def test_archive_restore_keep_embedding_rows_and_status_healthy(tmp_path, monkeypatch):
    config = _emb_config(tmp_path)
    recording, _t, _s = make_transcribed_recording(
        ["archived embedding document"], sha="archive-embedding"
    )
    si.rebuild_index()
    ei.rebuild_embedding_index(config, embedder=_embedder())
    assert ei.build_embedding_status_report(config)["healthy"] is True

    generation = EmbeddingGeneration.objects.get(
        state=EmbeddingGenerationState.ACTIVE
    )
    source_keys, stored_keys = _keys_for(recording, generation)
    assert source_keys and stored_keys == source_keys

    # Track the ONLY embedding client entry point from here on: archive and
    # restore must never invoke it.
    calls = []
    monkeypatch.setattr(
        "workflow.services.embedding_client.embed_texts",
        lambda *a, **k: calls.append(1),
    )
    gen_count = EmbeddingGeneration.objects.count()
    doc_count = EmbeddingDocument.objects.count()

    archive_recording(recording)
    recording.refresh_from_db()
    assert recording.archived_at is not None
    assert _keys_for(recording, generation)[1] == source_keys
    assert EmbeddingGeneration.objects.count() == gen_count
    assert EmbeddingDocument.objects.count() == doc_count
    assert EmbeddingGeneration.objects.get(pk=generation.pk).state == EmbeddingGenerationState.ACTIVE
    assert ei.build_embedding_status_report(config)["healthy"] is True
    assert calls == []

    restore_recording(recording)
    recording.refresh_from_db()
    assert recording.archived_at is None
    assert _keys_for(recording, generation)[1] == source_keys
    assert EmbeddingGeneration.objects.count() == gen_count
    assert EmbeddingDocument.objects.count() == doc_count
    assert ei.build_embedding_status_report(config)["healthy"] is True
    assert calls == []
