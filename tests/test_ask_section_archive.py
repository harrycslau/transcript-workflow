"""Ask excludes individual-Section archive evidence (Step 6.4 refinement).

Proves the Ask special case:

- an archived topic Section's SUMMARY and SEGMENT evidence is excluded
  BEFORE top-K selection (the shared item mapping + canonical-layout
  predicate, never a post-retrieval drop), while sibling Sections and the
  ordinal-0 whole-recording variants stay admissible;
- the one source-health sweep / one query-embedding / one integrity
  traversal contract is unchanged;
- archiving a cited Section DURING the chat fails closed with the fixed
  sanitized concurrent-change failure (post-chat revalidation).
"""

from __future__ import annotations

import json

import pytest

from brainlib.config import EmbeddingConfig
from factories import (
    make_config,
    make_summary_version,
    make_transcribed_recording,
)
from workflow.models import Recording, Section, SegmentedVersion
from workflow.services import ask as ask_service
from workflow.services import embedding_index as ei
from workflow.services import search_index as si
from workflow.services import semantic_query as sq
from workflow.services.embedding_client import EmbeddingBatch
from workflow.services.segmentation import save_segmented_version

pytestmark = pytest.mark.django_db(transaction=True)

DIM = 4
LOCAL_LLM = "http://127.0.0.1:8000/v1"


def ask_config(tmp_path):
    from brainlib.config import LLMConfig

    return make_config(
        tmp_path,
        embedding=EmbeddingConfig(
            base_url="http://127.0.0.1:1/v1",
            model="test-embed-model",
            api_key_env="BRAIN_TEST_LLM_API_KEY",
            timeout_seconds=120,
            batch_size=32,
        ),
        llm=LLMConfig(
            provider="openai_compatible",
            base_url=LOCAL_LLM,
            model="test-chat-model",
            api_key_env="BRAIN_TEST_LLM_API_KEY",
            temperature=0.2,
            timeout_seconds=600,
        ),
    )


def keyword_embedder(keywords, dim=DIM, *, tracker=None):
    def embed(config, texts):
        if tracker is not None:
            tracker.append(list(texts))
        out = []
        for text in texts:
            vec = [0.0] + [0.01] * (dim - 1)
            for idx, keyword in enumerate(keywords):
                if keyword in text:
                    vec[idx] = 1.0
            out.append(EmbeddingBatch(text=text, embedding=tuple(vec)))
        return out

    return embed


def chat_json(answer, citations=(), *, calls=None):
    def chat(config, *, system_prompt, user_prompt, temperature, max_tokens):
        if calls is not None:
            calls.append({"user": user_prompt})
        return json.dumps(
            {"answer": answer, "citations": list(citations), "insufficient": False}
        )

    return chat


def build_split(tmp_path, sha):
    """A 4-segment recording split at marker 2 with ACTIVE summaries on
    BOTH topic Sections; only the FIRST Section's summary matches the
    ``alpha`` query, so it always ranks first while the sibling (lower
    score) still fits the per-Recording evidence cap. Healthy
    search+embedding indexes."""
    rec, transcript, fixed = make_transcribed_recording(
        ["gamma zero", "gamma one", "gamma two", "gamma three"], sha=sha
    )
    save_segmented_version(rec.pk, transcript.pk, 0, 4, [2], ["Alpha Topic", "Beta Topic"])
    layout = SegmentedVersion.objects.get(transcript=transcript, is_active=True)
    sections = list(Section.objects.filter(segmented_version=layout).order_by("ordinal"))
    section_summary = make_summary_version(
        rec,
        transcript,
        sections[0],
        title="alpha section title",
        overview="alpha section overview",
    )
    sibling_summary = make_summary_version(
        rec, transcript, sections[1], title="beta title", overview="beta overview"
    )
    si.rebuild_index()
    config = ask_config(tmp_path)
    ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha"]))
    return {
        "config": config,
        "rec": rec,
        "transcript": transcript,
        "fixed": fixed,
        "sibling_summary": sibling_summary,
        "sections": sections,
        "section_summary": section_summary,
    }


def _evidence_summary_ids(evidence):
    return {
        winner.match.summary_id
        for winner in evidence.matches
        if winner.match.doc_type == "summary"
    }


class TestAskExcludesArchivedSection:
    def test_archived_section_summary_excluded_before_topk(self, tmp_path):
        corpus = build_split(tmp_path, "asksecarch-topk")
        rec = corpus["rec"]
        archived_section = corpus["sections"][0]
        sibling = corpus["sections"][1]

        # Before archive the target summary IS admitted (proves the
        # exclusion below is meaningful, not an empty corpus).
        target_summary = corpus["section_summary"]
        sibling_summary = corpus["sibling_summary"]
        baseline = sq.retrieve_semantic_evidence(
            "alpha",
            config=corpus["config"],
            embedder=keyword_embedder(["alpha"]),
            scope=Recording.objects.all(),
            exclude_archived_sections=True,
        )
        baseline_ids = _evidence_summary_ids(baseline)
        assert target_summary.pk in baseline_ids
        assert sibling_summary.pk in baseline_ids

        from workflow.services.archive import archive_section

        archive_section(rec, archived_section)

        evidence = sq.retrieve_semantic_evidence(
            "alpha",
            config=corpus["config"],
            embedder=keyword_embedder(["alpha"]),
            scope=Recording.objects.all(),
            exclude_archived_sections=True,
        )
        summary_ids = _evidence_summary_ids(evidence)
        assert target_summary.pk not in summary_ids
        # The sibling Section's summary is retained.
        assert sibling_summary.pk in summary_ids
        assert sibling is not None

    def test_legacy_path_without_exclusion_would_still_see_it(self, tmp_path):
        # Documents are retained; only the shared item scope excludes them.
        # Without the exclusion the archived Section's summary is still a
        # candidate (the archive is an eligibility marker, not deletion).
        corpus = build_split(tmp_path, "asksecarch-legacy")
        rec = corpus["rec"]
        archived_section = corpus["sections"][0]
        from workflow.services.archive import archive_section

        archive_section(rec, archived_section)
        legacy = sq.retrieve_semantic_evidence(
            "alpha",
            config=corpus["config"],
            embedder=keyword_embedder(["alpha"]),
            scope=Recording.objects.all(),
        )
        assert corpus["section_summary"].pk in _evidence_summary_ids(legacy)

    def test_one_sweep_and_one_embedding_unchanged(self, tmp_path, monkeypatch):
        corpus = build_split(tmp_path, "asksecarch-contract")
        from workflow.services.archive import archive_section

        archive_section(corpus["rec"], corpus["sections"][0])
        sweeps = {"n": 0}
        real_sweep = si.build_status_report

        def counting_sweep(*args, **kwargs):
            sweeps["n"] += 1
            return real_sweep(*args, **kwargs)

        monkeypatch.setattr(si, "build_status_report", counting_sweep)
        tracker = []
        result = ask_service.ask_question(
            "alpha",
            config=corpus["config"],
            embedder=keyword_embedder(["alpha"], tracker=tracker),
            chat=chat_json("Answer [C1].", ["C1"]),
        )
        assert result.state in (ask_service.STATE_ANSWERED, ask_service.STATE_INSUFFICIENT)
        assert sweeps["n"] == 1
        assert len(tracker) == 1  # exactly one query embedding request

    def test_archive_during_chat_fails_closed(self, tmp_path):
        corpus = build_split(tmp_path, "asksecarch-race")
        rec = corpus["rec"]
        archived_section = corpus["sections"][0]
        from workflow.services.archive import archive_section

        def mutate_then_answer(config, *, system_prompt, user_prompt, temperature, max_tokens):
            archive_section(rec, archived_section)
            return json.dumps(
                {"answer": "Cited [C1].", "citations": ["C1"], "insufficient": False}
            )

        # C1 is the first Section's summary (the only alpha-matching
        # evidence), so the cited Section is archived mid-chat.
        with pytest.raises(ask_service.AskError) as excinfo:
            ask_service.ask_question(
                "alpha",
                config=corpus["config"],
                embedder=keyword_embedder(["alpha"]),
                chat=mutate_then_answer,
            )
        assert excinfo.value.code == ask_service.ASK_CONCURRENT_CHANGE

    def test_archived_section_segment_evidence_excluded(self, tmp_path):
        # Segment texts DO carry the keyword here, so the first Section's
        # segments would otherwise be candidates; they must be excluded
        # BEFORE top-K too.
        rec, transcript, fixed = make_transcribed_recording(
            ["alpha zero", "alpha one", "alpha two", "alpha three"],
            sha="asksecarch-segment",
        )
        save_segmented_version(
            rec.pk, transcript.pk, 0, 4, [2], ["Alpha Topic", "Beta Topic"]
        )
        layout = SegmentedVersion.objects.get(transcript=transcript, is_active=True)
        sections = list(Section.objects.filter(segmented_version=layout).order_by("ordinal"))
        si.rebuild_index()
        config = ask_config(tmp_path)
        ei.rebuild_embedding_index(config, embedder=keyword_embedder(["alpha"]))

        from workflow.services.archive import archive_section

        archive_section(rec, sections[0])
        evidence = sq.retrieve_semantic_evidence(
            "alpha",
            config=config,
            embedder=keyword_embedder(["alpha"]),
            scope=Recording.objects.all(),
            exclude_archived_sections=True,
        )
        archived_range = (
            sections[0].start_segment_ordinal,
            sections[0].end_segment_ordinal_exclusive,
        )
        for winner in evidence.matches:
            if winner.match.doc_type != "segment":
                continue
            ordinal = winner.match.segment_ordinal
            assert not (archived_range[0] <= ordinal < archived_range[1])
