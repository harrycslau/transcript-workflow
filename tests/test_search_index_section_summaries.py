"""Step 6.3 foundation tests: canonical ACTIVE topic-section summaries
become ``doc_type='summary'`` SearchDocuments of the PARENT Recording.

Proves on the CURRENT schema (no migration):

- the expected-document mapping indexes ACTIVE summary variants of topic
  Sections ONLY through the shared canonical-layout predicate (same SQL
  home the Step 6.2 Library projection compiles): historical layouts,
  malformed layouts (fail-closed), inactive transcripts and cross-parent
  forged Summary rows contribute nothing;
- stale registry rows for such summaries are detected as orphans and
  converge via reconcile/rebuild;
- section-summary aux text binds the Section's ACTIVE tag names
  (deterministic shared order); recording-scope tags never leak into a
  section doc and vice versa; a tag membership change converges via
  the normal stale/reconcile path;
- whole-recording (fixed ordinal-0) summary docs keep the OLD mapping
  (no tag aux change), and INDEX_VERSION is "2" (the migration-0008
  backfill mapping is version "1" and detectably stale until rebuild);
- the post-commit sync contract: segmentation saves, topic-section
  persistence and section tag mutations schedule exactly ONE
  parent-recording reconcile that converges the index end-to-end.
"""

from __future__ import annotations

import pytest
from django.utils import timezone as dj_timezone

from workflow.models import (
    AttemptStage,
    ProcessingAttempt,
    SearchDocument,
    Section,
    SegmentedVersion,
    Summary,
    Transcript,
)
from workflow import query as library_query
from workflow.services import search_index as si
from workflow.services import search_sync
from workflow.services.search_index import build_status_report, rebuild_index
from workflow.services.segmentation import (
    canonical_active_section_ids,
    save_segmented_version,
)
from workflow.services.summarize import persist_summary
from workflow.services.tags import add_manual_tag_section, remove_section_tag

from factories import make_summary_version, make_tag, make_transcribed_recording

pytestmark = pytest.mark.django_db


def split_recording(texts, splits, titles):
    """A transcribed recording with an active canonical topic layout;
    returns (recording, transcript, topic_sections by ordinal)."""
    recording, transcript, _fixed = make_transcribed_recording(texts)
    save_segmented_version(
        recording.pk, transcript.pk, 0, len(texts), splits, titles
    )
    layout = SegmentedVersion.objects.get(transcript=transcript, is_active=True)
    sections = list(
        Section.objects.filter(segmented_version=layout).order_by("ordinal")
    )
    return recording, transcript, sections


def registry_keys():
    return set(SearchDocument.objects.values_list("document_key", flat=True))


def registry_row(pk_or_summary):
    pk = getattr(pk_or_summary, "pk", pk_or_summary)
    return SearchDocument.objects.get(document_key=f"summary:{pk}")


def status():
    return build_status_report()


# ---------------------------------------------------------------------------
# Expected-document mapping (canonical layout inclusion)
# ---------------------------------------------------------------------------


class TestCanonicalSectionSummariesIndexed:
    def test_section_variants_become_summary_docs(self):
        recording, transcript, sections = split_recording(
            ["a one", "b two", "c three"], [2], ["Alpha", "Beta"]
        )
        s1 = make_summary_version(
            recording, transcript, sections[0], title="S1 en",
            overview="First section overview.",
        )
        s2 = make_summary_version(
            recording, transcript, sections[1], title="S2 en",
            overview="Second section overview.", output_language="en",
        )
        fixed = make_summary_version(
            recording, transcript, Section.objects.get(
                transcript=transcript, segmented_version__isnull=True
            ),
            title="Whole en",
        )
        result = rebuild_index()
        assert result["result"] == "rebuilt"
        assert result["documents"]["summaries"] == 3
        keys = registry_keys()
        for summary in (s1, s2, fixed):
            assert f"summary:{summary.pk}" in keys
        report = status()
        assert report["healthy"] is True
        assert report["counts"]["summaries"] == 3

    def test_doc_fields_and_hash_use_existing_summary_mapping(self):
        recording, transcript, sections = split_recording(
            ["a one", "b two"], [1], ["Alpha", "Beta"]
        )
        summary = make_summary_version(
            recording, transcript, sections[0], title="S1",
            overview="Overview text.", people=["Alice"], topics=["grading"],
        )
        rebuild_index()
        row = registry_row(summary)
        assert row.doc_type == "summary"
        assert row.recording_id == recording.pk  # PARENT recording
        assert row.transcript_id == transcript.pk
        assert row.summary_id == summary.pk
        assert row.output_language == summary.output_language
        assert row.title_text == "S1"
        assert row.body_text == si.summary_body_text(summary)
        # No section tags -> aux is exactly the shared summary aux.
        assert row.aux_text == si.summary_aux_text(summary)
        # The hash equals a locally rebuilt spec (same contract).
        spec = si.make_spec(
            doc_type="summary",
            document_key=f"summary:{summary.pk}",
            recording_id=recording.pk,
            transcript_id=transcript.pk,
            summary_id=summary.pk,
            output_language=summary.output_language,
            title_text="S1",
            body_text=si.summary_body_text(summary),
            aux_text=si.summary_aux_text(summary),
        )
        assert row.content_hash == spec.content_hash

    def test_second_output_language_variant_indexed(self):
        recording, transcript, sections = split_recording(
            ["a one", "b two"], [1], ["Alpha", "Beta"]
        )
        en = make_summary_version(recording, transcript, sections[0])
        fi = make_summary_version(
            recording, transcript, sections[0], title="Fiksi",
            output_language="fi",
        )
        rebuild_index()
        keys = registry_keys()
        assert f"summary:{en.pk}" in keys
        assert f"summary:{fi.pk}" in keys
        assert registry_row(fi).output_language == "fi"
        assert status()["healthy"] is True


# ---------------------------------------------------------------------------
# Canonical-predicate exclusions (fail closed)
# ---------------------------------------------------------------------------


class TestExclusions:
    def test_superseded_layout_summaries_drop_out(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["Alpha", "Beta"]
        )
        old = make_summary_version(recording, transcript, sections[0])
        rebuild_index()
        assert f"summary:{old.pk}" in registry_keys()

        # A NEW layout supersedes the old revision (new sections carry no
        # summaries at all).
        save_segmented_version(recording.pk, transcript.pk, 0, 3, [1], ["X", "Y"])
        rebuild_index()
        keys = registry_keys()
        assert f"summary:{old.pk}" not in keys
        report = status()
        assert report["healthy"] is True
        # The old summary row itself stays (history is never deleted).
        assert Summary.objects.filter(pk=old.pk).exists()

    def test_malformed_layout_excluded_and_stale_row_detected(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["Alpha", "Beta"]
        )
        summary = make_summary_version(recording, transcript, sections[1])
        rebuild_index()
        assert status()["healthy"] is True

        # Corrupt stored state: a temporary flag carrying an arbitrary
        # custom title violates the canonical SHAPE rule -> the whole
        # layout fails closed on EVERY canonical read (SQL twin parity).
        Section.objects.filter(pk=sections[1].pk).update(
            title="Totally custom", title_is_temporary=True
        )
        report = status()
        assert report["healthy"] is False
        assert report["categories"].get("orphan_document", 0) >= 1
        assert f"summary:{summary.pk}" in report["keys"]["orphan_document"]

        rebuild_index()
        assert f"summary:{summary.pk}" not in registry_keys()
        assert status()["healthy"] is True

    def test_inactive_transcript_excluded(self):
        recording, transcript, sections = split_recording(
            ["a", "b"], [1], ["Alpha", "Beta"]
        )
        summary = make_summary_version(recording, transcript, sections[0])
        rebuild_index()
        assert f"summary:{summary.pk}" in registry_keys()

        Transcript.objects.filter(pk=transcript.pk).update(
            is_active=False, superseded_at=dj_timezone.now()
        )
        rebuild_index()
        assert f"summary:{summary.pk}" not in registry_keys()
        assert status()["healthy"] is True

    def test_cross_parent_forged_summary_excluded(self):
        """A Summary row naming a VALID canonical section but the OTHER
        recording's transcript is never expected content — and a registry
        row carrying that forged provenance converges to an orphan."""
        rec_a, transcript_a, sections_a = split_recording(["a", "b"], [1], ["A", "B"])
        rec_b, transcript_b, _ = split_recording(["c", "d"], [1], ["C", "D"])
        forged = make_summary_version(
            rec_a, transcript_b, sections_a[0], title="Forged"
        )
        rebuild_index()
        assert f"summary:{forged.pk}" not in registry_keys()
        assert status()["healthy"] is True

        # Even a manually planted registry row with the forged row's own
        # provenance is not canonical (bounded page validation re-reads
        # the authoritative source with the cross-parent defense).
        spec = si.make_spec(
            doc_type="summary",
            document_key=f"summary:{forged.pk}",
            recording_id=forged.recording_id,
            transcript_id=forged.transcript_id,
            summary_id=forged.pk,
            output_language=forged.output_language,
            title_text="Forged",
            body_text="body",
            aux_text="",
        )
        SearchDocument.objects.create(
            document_key=spec.document_key, doc_type=spec.doc_type,
            recording_id=spec.recording_id, transcript_id=spec.transcript_id,
            summary_id=spec.summary_id, output_language=spec.output_language,
            title_text=spec.title_text, body_text=spec.body_text,
            aux_text=spec.aux_text, content_hash=spec.content_hash,
            index_version=si.INDEX_VERSION,
        )
        report = status()
        assert report["healthy"] is False
        assert f"summary:{forged.pk}" in report["keys"]["orphan_document"]
        search_sync.reconcile_recording(rec_a.pk)
        assert f"summary:{forged.pk}" not in registry_keys()
        assert status()["healthy"] is True


# ---------------------------------------------------------------------------
# Section tag aux binding
# ---------------------------------------------------------------------------


class TestSectionTagAux:
    def _indexed(self):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["Alpha", "Beta"]
        )
        summary = make_summary_version(recording, transcript, sections[0])
        rebuild_index()
        return recording, transcript, sections, summary

    def test_active_section_tag_names_bind_aux_in_order(self):
        recording, _t, sections, summary = self._indexed()
        work = make_tag("Work")
        alpha = make_tag("Alpha tag")
        add_manual_tag_section(sections[0], work)
        add_manual_tag_section(sections[0], alpha)
        search_sync.reconcile_recording(recording.pk)
        row = registry_row(summary)
        base = si.summary_aux_text(summary)
        # Deterministic name_key order, appended after the summary parts.
        assert row.aux_text == f"{base}\nAlpha tag\nWork"
        assert status()["healthy"] is True

    def test_scope_isolation_both_directions(self):
        recording, _t, sections, summary = self._indexed()
        section_tag = make_tag("SectionOnly")
        recording_tag = make_tag("RecordingOnly")
        add_manual_tag_section(sections[0], section_tag)
        from factories import make_tag_assignment

        make_tag_assignment(recording, recording_tag, origin="manual")
        rebuild_index()
        section_row = registry_row(summary)
        metadata_row = SearchDocument.objects.get(
            document_key=f"recording:{recording.pk}"
        )
        assert "sectiononly" in section_row.aux_text.lower()
        assert "recordingonly" not in section_row.aux_text.lower()
        assert "recordingonly" in metadata_row.aux_text.lower()
        assert "sectiononly" not in metadata_row.aux_text.lower()

    def test_inactive_section_tag_dropped_via_stale_and_reconcile(self):
        recording, _t, sections, summary = self._indexed()
        tag = make_tag("Work")
        add_manual_tag_section(sections[0], tag)
        rebuild_index()
        assert "work" in registry_row(summary).aux_text.lower()

        remove_section_tag(sections[0], tag)
        # The aux changed but the registry still holds the old hash.
        report = status()
        assert report["healthy"] is False
        assert f"summary:{summary.pk}" in report["keys"]["stale_content"]

        result = search_sync.reconcile_recording(recording.pk)
        assert result["updated"] == 1
        assert "work" not in registry_row(summary).aux_text.lower()
        assert status()["healthy"] is True

    def test_second_section_unaffected_by_first_section_tags(self):
        recording, transcript, sections, summary = self._indexed()
        other = make_summary_version(recording, transcript, sections[1])
        rebuild_index()
        add_manual_tag_section(sections[0], make_tag("Work"))
        rebuild_index()
        assert "work" in registry_row(summary).aux_text.lower()
        assert "work" not in registry_row(other).aux_text.lower()

    def test_cross_parent_tag_assignment_fails_closed(self):
        """A malformed section-scoped TagAssignment whose denormalized
        ``recording`` is a DIFFERENT Recording never contributes to the
        Section doc (nor to the foreign Recording's metadata doc) — the
        aux load consumes only assignments whose recording equals the
        Section transcript's own recording; a legitimate same-parent
        assignment still binds."""
        from workflow.models import TagAssignment

        recording, transcript, sections, summary = self._indexed()
        foreign, _ft, _ffixed = make_transcribed_recording(["foreign"])
        forged = make_tag("Forged")
        TagAssignment.objects.create(
            recording=foreign, section=sections[0], tag=forged,
            origin="manual", is_active=True,
        )
        # A genuine assignment of the SAME tag object on the same Section
        # through the SERVICE stays untouched by the defense.
        legit = make_tag("Legit")
        add_manual_tag_section(sections[0], legit)
        rebuild_index()
        section_row = registry_row(summary)
        assert "legit" in section_row.aux_text.lower()
        assert "forged" not in section_row.aux_text.lower()
        # The foreign Recording's metadata doc sees neither assignment.
        foreign_meta = SearchDocument.objects.get(
            document_key=f"recording:{foreign.pk}"
        )
        assert "forged" not in foreign_meta.aux_text.lower()
        assert "legit" not in foreign_meta.aux_text.lower()
        # Deterministic and healthy: the malformed row is invisible to
        # the mapping (rebuild and per-recording converge identically).
        assert status()["healthy"] is True
        search_sync.reconcile_recording(recording.pk)
        assert registry_row(summary).aux_text == section_row.aux_text
        assert status()["healthy"] is True


# ---------------------------------------------------------------------------
# Version + shared-predicate + determinism
# ---------------------------------------------------------------------------


class TestVersionAndSharing:
    def test_index_version_bumped(self):
        assert si.INDEX_VERSION == "2"

    def test_predicate_has_one_home(self):
        # The Library projection and the search index consume the SAME
        # segmentation-owned SQL text and parameters (never a fork).
        sql, params = canonical_active_section_ids()
        assert params == [200, 255]  # MAX_TOPIC_SECTIONS / MAX_TOPIC_TITLE_LENGTH
        assert str(library_query._VALID_SECTIONS_SQL_RAW.sql) == sql
        assert si._SECTION_IDS_SQL == sql

    def test_version_1_backfill_row_is_detectably_stale(self):
        """Migration-0008-style registry rows (index_version "1") are
        reported as version_mismatch and only an explicit rebuild
        converges them — never a silent upgrade."""
        recording, _transcript, _fixed = make_transcribed_recording(["a"])
        row = si.make_spec(
            doc_type="recording",
            document_key=f"recording:{recording.pk}",
            recording_id=recording.pk,
            title_text="x", body_text="", aux_text="",
        )
        SearchDocument.objects.create(
            document_key=row.document_key, doc_type=row.doc_type,
            recording_id=row.recording_id,
            title_text=row.title_text, body_text=row.body_text,
            aux_text=row.aux_text, content_hash=row.content_hash,
            index_version="1",
        )
        report = status()
        assert report["healthy"] is False
        assert f"recording:{recording.pk}" in report["keys"]["version_mismatch"]
        rebuild_index()
        assert SearchDocument.objects.get(
            document_key=f"recording:{recording.pk}"
        ).index_version == "2"
        assert status()["healthy"] is True

    def test_rebuild_is_deterministic_over_section_docs(self):
        recording, transcript, sections = split_recording(["a", "b"], [1], ["A", "B"])
        summary = make_summary_version(recording, transcript, sections[0])
        first = rebuild_index()
        assert first["result"] == "rebuilt"
        assert status()["healthy"] is True
        hash1 = registry_row(summary.pk).content_hash
        rebuild_index()
        hash2 = registry_row(summary.pk).content_hash
        assert hash1 == hash2
        assert status()["healthy"] is True


# ---------------------------------------------------------------------------
# Post-commit sync end-to-end (new Step 6.3 callers)
# ---------------------------------------------------------------------------


class TestSyncEndToEnd:
    def test_segmentation_save_converges_index(self, django_capture_on_commit_callbacks):
        recording, transcript, sections = split_recording(
            ["a", "b", "c"], [2], ["Alpha", "Beta"]
        )
        old = make_summary_version(recording, transcript, sections[0])
        rebuild_index()
        assert f"summary:{old.pk}" in registry_keys()

        with django_capture_on_commit_callbacks(execute=True):
            save_segmented_version(
                recording.pk, transcript.pk, 0, 3, [1], ["X", "Y"]
            )
        keys = registry_keys()
        assert f"summary:{old.pk}" not in keys
        assert status()["healthy"] is True

    def test_segmentation_noop_schedules_no_callback(self, django_capture_on_commit_callbacks):
        recording, transcript, _sections = split_recording(
            ["a", "b", "c"], [2], ["Alpha", "Beta"]
        )
        with django_capture_on_commit_callbacks() as callbacks:
            result = save_segmented_version(
                recording.pk, transcript.pk, 0, 3, [2], ["Alpha", "Beta"]
            )
        assert result.created is False
        assert callbacks == []

    def test_persist_summary_topic_path_converges_index(
        self, django_capture_on_commit_callbacks
    ):
        recording, transcript, sections = split_recording(
            ["a", "b"], [1], ["Alpha", "Beta"]
        )
        attempt = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.SUMMARIZATION, ordinal=1,
            model_id="m",
        )
        payload = {
            "title": "Section title",
            "overview": "Section overview.",
            "key_points": [],
            "action_items": [],
            "people": [],
            "organizations": [],
            "topics": [],
            "language": "en",
            "suggested": [],
            "rejected": [],
        }
        with django_capture_on_commit_callbacks(execute=True):
            summary = persist_summary(
                recording=recording, transcript=transcript, section=sections[0],
                attempt=attempt, payload=payload, output_language="en",
                is_default=True, model_id="m", base_url="u", prompt_version="1",
                fingerprint="f", chunk_count=1, input_characters=1,
                limits_used={}, generation_mode="manual",
            )
        row = registry_row(summary)
        assert row.title_text == "Section title"
        assert status()["healthy"] is True

    def test_section_tag_add_schedules_parent_reconcile(
        self, django_capture_on_commit_callbacks
    ):
        recording, _t, sections = split_recording(["a", "b"], [1], ["Alpha", "Beta"])
        summary = make_summary_version(recording, _t, sections[0])
        rebuild_index()
        assert "work" not in registry_row(summary).aux_text.lower()

        with django_capture_on_commit_callbacks(execute=True):
            add_manual_tag_section(sections[0], make_tag("Work"))
        assert "work" in registry_row(summary).aux_text.lower()
        assert status()["healthy"] is True
