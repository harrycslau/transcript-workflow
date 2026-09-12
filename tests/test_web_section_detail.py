"""Step 6.2 section detail page + section summary export (web).

Proves the approved read contract:

- the route is parent-scoped: unknown/cross-recording/fixed/non-topic/
  malformed-layout Sections are controlled 404s;
- an ACTIVE topic Section renders the topic H1, parent context/links,
  range + transcript jump link, the section-scoped tags editor, the
  complete selected summary variant, provenance, and a bounded transcript
  preview from ONLY its canonical segment range;
- a HISTORICAL topic Section (superseded layout / historical transcript)
  is readable but NEVER actionable (no tags editor, no action form);
- GETs are strictly SELECT-only: no writes, no network, no subprocess;
- unknown concrete read variants are friendly 404s (never a silent
  fallback);
- the section summary export endpoint is parent-scoped, canonical-only,
  and serves markdown/text/json for the current selected variant;
  ownership/malformed/language failures are controlled 404s.
"""

from __future__ import annotations

import re

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.test import Client

from workflow.models import (
    ProcessingAttempt,
    Recording,
    Section,
    SegmentedVersion,
    Summary,
    SummaryState,
    SummaryVariantState,
    Transcript,
)
from workflow.services.segmentation import save_segmented_version

from factories import (
    make_summary_version,
    make_tag,
    make_transcribed_recording,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


@pytest.fixture
def client():
    return Client()


def _transcript(count=8, sha="sec-web-detail"):
    return make_transcribed_recording(
        [f"segment {i}" for i in range(count)], sha=sha
    )


def _split(recording, transcript, start=0, end=None, splits=(), titles=()):
    if end is None:
        end = transcript.segments.count()
    result = save_segmented_version(
        recording.pk, transcript.pk, start, end, list(splits), list(titles)
    )
    return list(
        Section.objects.filter(segmented_version_id=result.version_id).order_by("ordinal")
    )


def _url(recording, section):
    return f"/recordings/{recording.pk}/sections/{section.pk}/"


class TestActiveSection:
    def test_renders_topic_title_parent_context_range_and_links(self, client):
        rec, transcript, _fixed = _transcript()
        sections = _split(rec, transcript, splits=[3], titles=["First topic", "Second topic"])
        section = sections[0]
        content = client.get(_url(rec, section)).content.decode()
        assert f"<h1 class=\"detail-title\">{section.title}</h1>" in content
        assert section.title == "First topic"
        assert "segments 0–2" in content  # canonical range label
        assert (
            f'href="/recordings/{rec.pk}/transcript/?page=1&amp;return_section={section.pk}#segment-0"'
            in content
        )
        assert f'href="/recordings/{rec.pk}/history/?return_section={section.pk}"' in content
        assert f'href="/recordings/{rec.pk}/"' in content  # parent recording link
        assert "action-section-summarize" in content or "Generate" in content
        assert f'/recordings/{rec.pk}/sections/{section.pk}/tags/apply/' in content

    def test_tags_editor_and_action_present_for_active_section(self, client):
        rec, transcript, _fixed = _transcript()
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        make_tag("Family")
        content = client.get(_url(rec, sections[0])).content.decode()
        assert 'id="tag-editor"' in content
        assert 'name="selected_tags"' in content
        assert 'id="new_tag_name"' in content or 'name="new_tag_name"' in content
        assert f'/recordings/{rec.pk}/sections/{sections[0].pk}/summarize/' in content

    def test_preview_only_from_section_range_and_bounded(self, client):
        rec, transcript, _fixed = _transcript(count=12)
        # Second section covers segments 3..7; the preview must contain
        # exactly its segments, bounded to DETAIL_PREVIEW_SEGMENTS (5).
        sections = _split(rec, transcript, splits=[3, 8], titles=["A", "B", "C"])
        section = sections[1]
        content = client.get(_url(rec, section)).content.decode()
        assert "segment 3" in content
        assert "segment 7" in content
        assert "segment 2" not in content
        assert "segment 8" not in content
        # Bounded preview: the page preview shows the first 5 of the 5
        # range segments; a wider section still shows only 5.
        sections_wide = _split(
            rec, transcript, start=0, end=12, splits=[1, 6], titles=["A", "B", "C"]
        )
        content = client.get(_url(rec, sections_wide[1])).content.decode()
        assert "segment 1" in content
        assert "segment 5" in content  # ordinal 5 = the 5th range segment
        assert "segment 6" not in content  # the 6th is NOT previewed
        assert f"segment{{ segment_count|pluralize }}" not in content

    def test_section_default_summary_rendered(self, client):
        rec, transcript, _fixed = _transcript()
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        make_summary_version(
            rec, transcript, sections[0], title="Section summary title",
            overview="Section overview text.", output_language="en",
        )
        content = client.get(_url(rec, sections[0])).content.decode()
        assert "Section summary title" in content
        assert "Section overview text." in content

    def test_summary_export_markdown(self, client):
        rec, transcript, _fixed = _transcript()
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        make_summary_version(rec, transcript, sections[0], title="Sec S", output_language="en")
        response = client.get(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/summary/export/?format=markdown"
        )
        assert response.status_code == 200
        assert "Sec S" in response.content.decode()
        assert response["Content-Type"].startswith("text/markdown")
        assert 'attachment; filename="brain-section-summary-' in response["Content-Disposition"]

    def test_summary_export_text_and_json(self, client):
        rec, transcript, _fixed = _transcript()
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        make_summary_version(rec, transcript, sections[0], title="Sec S", output_language="en")
        text = client.get(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/summary/export/?format=text"
        )
        assert text.status_code == 200
        assert text["Content-Type"].startswith("text/plain")
        payload = client.get(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/summary/export/?format=json"
        ).json()
        assert payload["title"] == "Sec S"
        assert payload["section_id"] == sections[0].pk

    def test_export_current_selected_variant_only(self, client):
        rec, transcript, _fixed = _transcript()
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        make_summary_version(rec, transcript, sections[0], title="EN S", output_language="en")
        make_summary_version(
            rec, transcript, sections[0], title="ZH S", output_language="zh-Hant"
        )
        zh = client.get(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/summary/export/"
            f"?format=markdown&language=zh-Hant"
        )
        assert "ZH S" in zh.content.decode()
        en = client.get(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/summary/export/"
            f"?format=markdown&language=en"
        )
        assert "EN S" in en.content.decode()

    def test_export_unknown_language_is_404(self, client):
        rec, transcript, _fixed = _transcript()
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        make_summary_version(rec, transcript, sections[0], title="S", output_language="en")
        response = client.get(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/summary/export/"
            f"?format=markdown&language=xx"
        )
        assert response.status_code == 404

    def test_export_unresolved_original_is_404(self, client):
        rec, transcript, _fixed = _transcript()
        transcript.language_observed = ""
        transcript.save(update_fields=["language_observed"])
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        response = client.get(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/summary/export/"
            f"?format=markdown&language=original"
        )
        assert response.status_code == 404

    def test_export_invalid_format_bad_request(self, client):
        rec, transcript, _fixed = _transcript()
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        response = client.get(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/summary/export/?format=html"
        )
        assert response.status_code == 400


class TestReadOnlyAndOwnership:
    def test_get_is_select_only(self, client):
        rec, transcript, _fixed = _transcript()
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        make_summary_version(rec, transcript, sections[0], title="S", output_language="en")
        with CaptureQueriesContext(connection) as ctx:
            client.get(_url(rec, sections[0]))
        assert ctx.captured_queries, "expected at least one query"
        for q in ctx.captured_queries:
            assert q["sql"].strip().upper().startswith("SELECT"), q["sql"]

    def test_unknown_section_404(self, client):
        rec, _t, _s = _transcript(sha="sec-404-unknown")
        response = client.get(f"/recordings/{rec.pk}/sections/999999/")
        assert response.status_code == 404

    def test_cross_recording_section_404(self, client):
        rec_a, t_a, _ = _transcript(sha="sec-404-cross-a")
        rec_b, _t_b, _ = _transcript(sha="sec-404-cross-b")
        sections = _split(rec_a, t_a, splits=[3], titles=["A", "B"])
        response = client.get(f"/recordings/{rec_b.pk}/sections/{sections[0].pk}/")
        assert response.status_code == 404

    def test_fixed_whole_recording_section_404(self, client):
        rec, _t, fixed = _transcript(sha="sec-404-fixed")
        response = client.get(f"/recordings/{rec.pk}/sections/{fixed.pk}/")
        assert response.status_code == 404

    def test_malformed_single_section_layout_404(self, client):
        rec, transcript, _fixed = _transcript(sha="sec-404-malformed")
        # A lone topic Section is corrupt stored state (not a valid
        # layout) — the section detail must fail closed with a 404.
        version = SegmentedVersion.objects.create(
            transcript=transcript, revision=1, start_segment_ordinal=0,
            end_segment_ordinal_exclusive=8, is_active=True,
            activated_at=transcript.activated_at or transcript.created_at,
        )
        section = Section.objects.create(
            transcript=transcript, segmented_version=version, ordinal=1,
            title="Only topic", start_segment_ordinal=0, end_segment_ordinal_exclusive=8,
        )
        response = client.get(f"/recordings/{rec.pk}/sections/{section.pk}/")
        assert response.status_code == 404

    def test_export_cross_recording_404(self, client):
        rec_a, t_a, _ = _transcript(sha="sec-export-cross-a")
        rec_b, _t_b, _ = _transcript(sha="sec-export-cross-b")
        sections = _split(rec_a, t_a, splits=[3], titles=["A", "B"])
        response = client.get(
            f"/recordings/{rec_b.pk}/sections/{sections[0].pk}/summary/export/?format=markdown"
        )
        assert response.status_code == 404


class TestHistoricalSection:
    def _historical(self):
        rec, transcript, _fixed = _transcript(sha="sec-hist")
        sections = _split(rec, transcript, splits=[3], titles=["Old A", "Old B"])
        # Supersede: a new crop-only full-range layout (zero topics).
        save_segmented_version(rec.pk, transcript.pk, 0, transcript.segments.count())
        return rec, transcript, sections[0]

    def test_historical_readable_but_not_actionable(self, client):
        rec, transcript, section = self._historical()
        content = client.get(_url(rec, section)).content.decode()
        assert section.title in content
        assert "historical" in content
        # Read-only: no tags editor, no summary action form, no
        # fingerprint, no confirm/remove tag forms.
        assert 'id="tag-editor"' not in content
        assert "action-section-summarize" not in content
        assert 'name="fingerprint"' not in content
        assert "section-tag-confirm" not in content

    def test_historical_export_still_works_when_canonical(self, client):
        rec, transcript, section = self._historical()
        make_summary_version(rec, transcript, section, title="Old summary", output_language="en")
        response = client.get(
            f"/recordings/{rec.pk}/sections/{section.pk}/summary/export/?format=markdown"
        )
        assert response.status_code == 200
        assert "Old summary" in response.content.decode()

    def test_historical_transcript_version_readable_but_not_actionable(self, client):
        rec, transcript, _fixed = _transcript(sha="sec-hist-tx")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        section = sections[0]
        # Retranscription: new active transcript; old transcript + layout
        # become historical.
        from workflow.models import AttemptOutcome, AttemptStage

        attempt = ProcessingAttempt.objects.create(
            recording=rec,
            stage=AttemptStage.TRANSCRIPTION,
            ordinal=2,
            outcome=AttemptOutcome.SUCCESS,
        )
        transcript.is_active = False
        transcript.save(update_fields=["is_active"])
        new_transcript = Transcript.objects.create(
            recording=rec, attempt=attempt, text_normalized="new"
        )
        Section.objects.create(transcript=new_transcript, ordinal=0, title="Full")
        new_transcript.is_active = True
        new_transcript.save(update_fields=["is_active"])
        content = client.get(_url(rec, section)).content.decode()
        assert section.title in content
        assert "historical" in content
        assert 'id="tag-editor"' not in content
        assert "action-section-summarize" not in content

    def test_unknown_language_read_is_friendly_404(self, client):
        rec, transcript, _fixed = _transcript(sha="sec-lang-404")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        response = client.get(_url(rec, sections[0]) + "?language=xx")
        assert response.status_code == 404

    def test_concrete_existing_read_language_renders(self, client):
        rec, transcript, _fixed = _transcript(sha="sec-lang-conc")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        make_summary_version(
            rec, transcript, sections[0], title="FI S",
            overview="Finnish overview.", output_language="fi",
        )
        response = client.get(_url(rec, sections[0]) + "?language=fi")
        assert response.status_code == 200
        # The concrete variant's content stays readable; its title is NOT
        # repeated inside the summary body (the H1 is the stored/display
        # title because no DEFAULT summary exists for this section).
        assert "Finnish overview." in response.content.decode()
        assert "FI S" not in response.content.decode()
        assert 'class="summary-title"' not in response.content.decode()


class TestHistoricalSectionTagsReadOnly:
    """Fix: historical section detail shows its existing ACTIVE section
    tags read-only — the chips render with NO mutation controls."""

    def _historical(self):
        rec, transcript, _fixed = _transcript(sha="sec-hist-tags-none")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        save_segmented_version(rec.pk, transcript.pk, 0, transcript.segments.count())
        return rec, transcript, sections[0]

    def _historical_with_tag(self):
        from workflow.models import TagAssignment, TagOrigin

        rec, transcript, _fixed = _transcript(sha="sec-hist-tags")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        section = sections[0]
        tag = make_tag("Family")
        TagAssignment.objects.create(
            recording=rec, section=section, tag=tag,
            origin=TagOrigin.SUGGESTED, is_active=True,
        )
        save_segmented_version(rec.pk, transcript.pk, 0, transcript.segments.count())
        return rec, section, tag

    def test_historical_section_shows_tags_read_only(self, client):
        rec, section, tag = self._historical_with_tag()
        content = client.get(_url(rec, section)).content.decode()
        # The tag chip renders (read-only)...
        assert tag.name in content
        # ...with NO mutation controls and NO editor.
        assert 'id="tag-editor"' not in content
        assert "section-tag-confirm" not in content
        assert "section-tag-remove" not in content
        assert 'name="selected_tags"' not in content

    def test_historical_section_without_tags_shows_no_tag_section(self, client):
        rec, _transcript, section = self._historical()
        content = client.get(_url(rec, section)).content.decode()
        assert 'aria-label="Tags"' not in content

    def test_historical_section_does_not_query_global_tag_choices(self, client):
        """Fix: a historical section renders ONLY its assigned tags
        read-only — the global configured/retired Tag choices queries are
        skipped entirely (no standalone ``FROM "workflow_tag"`` query)."""
        from django.test.utils import CaptureQueriesContext

        rec, _transcript, section = self._historical()
        with CaptureQueriesContext(connection) as ctx:
            client.get(_url(rec, section))
        standalone_tag_queries = [
            q for q in ctx.captured_queries if 'FROM "workflow_tag"' in q["sql"]
        ]
        assert not standalone_tag_queries

    def test_active_section_still_queries_global_tag_choices(self, client):
        from django.test.utils import CaptureQueriesContext

        rec, transcript, _fixed = _transcript(sha="sec-act-tags-q")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        with CaptureQueriesContext(connection) as ctx:
            client.get(_url(rec, sections[0]))
        assert any('FROM "workflow_tag"' in q["sql"] for q in ctx.captured_queries)


class TestHistoricalSectionJumpLinks:
    """Fix: transcript jump links on historical sections preserve the
    historical transcript + layout (?v=...&layout=...); an active section
    keeps the plain current link."""

    def test_historical_layout_jump_link_preserves_layout(self, client):
        rec, transcript, _fixed = _transcript(sha="sec-jump-layout")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        section = sections[0]
        version = section.segmented_version
        save_segmented_version(rec.pk, transcript.pk, 0, transcript.segments.count())
        content = client.get(_url(rec, section)).content.decode()
        # The jump link opens the SECTION'S OWN (superseded) revision.
        # (``&`` is autoescaped to ``&amp;`` in the rendered HTML.)
        assert (
            f'href="/recordings/{rec.pk}/transcript/?page=1&amp;layout={version.pk}'
            f"&amp;return_section={section.pk}#segment-0" in content
        )
        # The active transcript gets no ?v=, and the plain current link is
        # NOT used for the historical section.
        assert "?page=1&amp;v=" not in content
        assert f'href="/recordings/{rec.pk}/transcript/?page=1#segment-0"' not in content

    def test_historical_transcript_jump_link_preserves_version_and_layout(self, client):
        from workflow.models import AttemptOutcome, AttemptStage

        rec, transcript, _fixed = _transcript(sha="sec-jump-tx")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        section = sections[0]
        version = section.segmented_version
        # Supersede the section's revision on the SAME transcript, then
        # retranscribe so the whole transcript becomes historical.
        save_segmented_version(rec.pk, transcript.pk, 0, transcript.segments.count())
        attempt = ProcessingAttempt.objects.create(
            recording=rec, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS,
        )
        transcript.is_active = False
        transcript.save(update_fields=["is_active"])
        new_transcript = Transcript.objects.create(
            recording=rec, attempt=attempt, text_normalized="new"
        )
        Section.objects.create(transcript=new_transcript, ordinal=0, title="Full")
        new_transcript.is_active = True
        new_transcript.save(update_fields=["is_active"])
        content = client.get(_url(rec, section)).content.decode()
        # Historical transcript AND superseded revision: ?v= + &layout=
        # both preserved so the jump opens EXACTLY this revision.
        assert (
            f'href="/recordings/{rec.pk}/transcript/?page=1&amp;v={transcript.pk}'
            f"&amp;layout={version.pk}&amp;return_section={section.pk}#segment-0" in content
        )

    def test_active_section_jump_link_is_plain_current_link(self, client):
        rec, transcript, _fixed = _transcript(sha="sec-jump-active")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        content = client.get(_url(rec, sections[0])).content.decode()
        assert (
            f'href="/recordings/{rec.pk}/transcript/?page=1&amp;return_section={sections[0].pk}#segment-0"'
            in content
        )
        assert "?page=1&amp;v=" not in content
        assert "?page=1&amp;layout=" not in content


class TestHistoricalSectionVariantScope:
    """Fix: historical canonical section pages/exports resolve their own
    summaries, variant states, source language and variants against the
    Section's OWN transcript — never the recording's active transcript."""

    def _historical_layout(self, sha="sec-hist-var"):
        rec, transcript, _fixed = _transcript(sha=sha)
        sections = _split(rec, transcript, splits=[3], titles=["Old A", "Old B"])
        section = sections[0]
        save_segmented_version(rec.pk, transcript.pk, 0, transcript.segments.count())
        return rec, transcript, section

    def test_historical_section_default_language_uses_its_own_transcript(self, client):
        rec, transcript, section = self._historical_layout()
        transcript.language_observed = "yue"
        transcript.save(update_fields=["language_observed"])
        from workflow.services.variant_view import build_variant_view

        # The section scope resolves from the section's OWN transcript
        # (here still the active one — the divergence case is covered by
        # the historical-transcript test below).
        section_view = build_variant_view(rec, "default", section=section)
        assert section_view.default_language == "zh-Hant"
        assert section_view.source_language == "yue"

    def test_historical_section_exposes_its_own_concrete_variant(self, client):
        rec, transcript, section = self._historical_layout()
        make_summary_version(
            rec, transcript, section, title="Old FI",
            overview="Finnish overview.", output_language="fi",
        )
        response = client.get(_url(rec, section) + "?language=fi")
        assert response.status_code == 200
        # The concrete variant's content stays readable; its title is NOT
        # repeated inside the summary body (only the DEFAULT summary
        # drives the H1, and no default exists for this section).
        assert "Finnish overview." in response.content.decode()
        assert "Old FI" not in response.content.decode()
        # Read-only: no action form on the historical page.
        assert "action-section-summarize" not in response.content.decode()

    def test_historical_transcript_section_uses_its_own_source_language(self, client):
        from workflow.models import AttemptOutcome, AttemptStage

        rec, transcript, _fixed = _transcript(sha="sec-hist-tx-var")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        section = sections[0]
        make_summary_version(rec, transcript, section, title="Old summary", output_language="en")
        # The old transcript is Cantonese; retranscription creates a new
        # English active transcript.
        transcript.language_observed = "yue"
        transcript.save(update_fields=["language_observed"])
        attempt = ProcessingAttempt.objects.create(
            recording=rec, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS,
        )
        transcript.is_active = False
        transcript.save(update_fields=["is_active"])
        new_transcript = Transcript.objects.create(
            recording=rec, attempt=attempt, text_normalized="new",
            language_observed="en",
        )
        Section.objects.create(transcript=new_transcript, ordinal=0, title="Full")
        new_transcript.is_active = True
        new_transcript.save(update_fields=["is_active"])
        from workflow.services.variant_view import build_variant_view

        section_view = build_variant_view(rec, "default", section=section)
        assert section_view.default_language == "zh-Hant"  # the OLD transcript
        assert section_view.source_language == "yue"
        whole_view = build_variant_view(rec, "default")
        assert whole_view.default_language == "en"  # the ACTIVE transcript
        # The historical section's own concrete variant stays readable;
        # its title is NOT repeated inside the summary body (the H1 is
        # the stored/display title — no DEFAULT summary exists for this
        # section, whose default language is zh-Hant).
        response = client.get(_url(rec, section) + "?language=en")
        assert response.status_code == 200
        assert "Old summary" not in response.content.decode()
        assert "Discussed grading plans." in response.content.decode()

    def test_historical_section_exposes_no_action_selector_or_mode(self, client):
        """Fix: a historical/non-actionable section scope exposes NO
        generation action selector or mode — on the view AND on every tab
        option."""
        rec, transcript, _fixed = _transcript(sha="sec-hist-nosel")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        historical = sections[1]
        # Supersede the layout: the section becomes historical.
        save_segmented_version(rec.pk, transcript.pk, 0, transcript.segments.count())
        from workflow.services.variant_view import build_variant_view

        view = build_variant_view(rec, "default", section=historical)
        assert view.action_mode is None
        assert view.action_selector is None
        for option in view.options:
            assert option.action_selector is None
        # A concrete existing read variant is also action-free.
        make_summary_version(
            rec, transcript, historical, title="Old FI", output_language="fi"
        )
        view_fi = build_variant_view(rec, "fi", section=historical)
        assert view_fi.action_mode is None
        assert view_fi.action_selector is None

    def test_active_section_still_exposes_action_selectors(self, client):
        """The active-section scope keeps its generation action selectors."""
        rec, transcript, _fixed = _transcript(sha="sec-act-sel")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        from workflow.services.variant_view import build_variant_view

        view = build_variant_view(rec, "default", section=sections[0])
        assert view.action_mode is not None
        assert view.action_selector == "default"
        assert any(o.action_selector is not None for o in view.options)

    def test_historical_unresolved_original_shows_read_only_guidance(self, client):
        """Fix: the unresolved-Original empty state on a historical section
        must NOT instruct the user to Generate (no action exists)."""
        rec, transcript, _fixed = _transcript(sha="sec-hist-uo")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        section = sections[0]
        # Unknown source language => Original is unresolved.
        transcript.language_observed = ""
        transcript.save(update_fields=["language_observed"])
        save_segmented_version(rec.pk, transcript.pk, 0, transcript.segments.count())
        content = client.get(_url(rec, section) + "?language=original").content.decode()
        assert "Use the Generate action" not in content
        assert "this language cannot be generated" in content

    def test_active_unresolved_original_shows_generate_guidance(self, client):
        rec, transcript, _fixed = _transcript(sha="sec-act-uo")
        transcript.language_observed = ""
        transcript.save(update_fields=["language_observed"])
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        content = client.get(_url(rec, sections[0]) + "?language=original").content.decode()
        assert "Use the Generate action" in content

    def test_historical_transcript_section_export_uses_section_transcript(self, client):
        """Fix 6: the section summary export inherits the historical
        variant fix — a concrete variant of a historical-transcript
        section resolves against that section's OWN transcript."""
        from workflow.models import AttemptOutcome, AttemptStage

        rec, transcript, _fixed = _transcript(sha="sec-hist-exp-var")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        section = sections[0]
        make_summary_version(rec, transcript, section, title="Old export", output_language="fi")
        # Retranscription makes the section's transcript historical while
        # a NEW active transcript (without that variant) exists.
        attempt = ProcessingAttempt.objects.create(
            recording=rec, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS,
        )
        transcript.is_active = False
        transcript.save(update_fields=["is_active"])
        new_transcript = Transcript.objects.create(
            recording=rec, attempt=attempt, text_normalized="new"
        )
        Section.objects.create(transcript=new_transcript, ordinal=0, title="Full")
        new_transcript.is_active = True
        new_transcript.save(update_fields=["is_active"])
        response = client.get(
            f"/recordings/{rec.pk}/sections/{section.pk}/summary/export/"
            f"?format=markdown&language=fi"
        )
        assert response.status_code == 200
        assert "Old export" in response.content.decode()
        # Cross-recording parent scoping stays a 404 on the export.
        rec_b, _t_b, _s_b = _transcript(sha="sec-hist-exp-other")
        response = client.get(
            f"/recordings/{rec_b.pk}/sections/{section.pk}/summary/export/"
            f"?format=markdown&language=fi"
        )
        assert response.status_code == 404


class TestSectionStatusPanel:
    """Objective A: the Section detail status panel is Section-scoped and
    derived ONLY from the selected VariantView/Section summary state —
    never the parent Recording summary tuple (section summaries
    intentionally do not mutate that tuple)."""

    def _section_with_summary(self, sha="sec-status"):
        rec, transcript, _fixed = _transcript(sha=sha)
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        make_summary_version(
            rec, transcript, sections[0], title="Sec S", output_language="en"
        )
        # make_summary_version flips the parent tuple to CURRENT; force it
        # back to MISSING so the Section panel is provably independent.
        Recording.objects.filter(pk=rec.pk).update(summary_status=SummaryState.MISSING)
        return rec, transcript, sections[0]

    def _current_variant_state(self, transcript, section, *, regen=False):
        return SummaryVariantState.objects.create(
            transcript=transcript, section=section, output_language="en",
            status="current", regeneration_failed=regen,
        )

    def test_panel_is_section_scoped_label(self, client):
        rec, transcript, section = self._section_with_summary()
        self._current_variant_state(transcript, section)
        content = client.get(_url(rec, section)).content.decode()
        assert "Section summary status" in content
        assert "inherited from the parent recording" not in content
        assert "Recording status" not in content

    def test_active_summary_current_ok(self, client):
        rec, transcript, section = self._section_with_summary()
        self._current_variant_state(transcript, section)
        content = client.get(_url(rec, section)).content.decode()
        assert 'class="status-panel status-panel-ok"' in content
        assert "Section summary current" in content
        assert "no action required" in content
        # The misleading parent missing text never appears.
        assert "An active transcript exists but the current summary is missing" not in content

    def test_active_summary_with_regen_failure_warn_kept(self, client):
        rec, transcript, section = self._section_with_summary(sha="sec-status-regen")
        self._current_variant_state(transcript, section, regen=True)
        content = client.get(_url(rec, section)).content.decode()
        assert 'class="status-panel status-panel-warn"' in content
        assert "Section re-summarization failed" in content
        assert "current section summary was kept" in content

    def test_failed_state_no_summary_danger(self, client):
        rec, transcript, _fixed = _transcript(sha="sec-status-fail")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        SummaryVariantState.objects.create(
            transcript=transcript, section=sections[0],
            output_language="en", status="failed",
        )
        content = client.get(_url(rec, sections[0])).content.decode()
        assert 'class="status-panel status-panel-danger"' in content
        assert "Section summary failed" in content
        assert "retry is available" in content

    def test_no_summary_warn_section_variant_scoped(self, client):
        rec, transcript, _fixed = _transcript(sha="sec-status-none")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        content = client.get(_url(rec, sections[0])).content.decode()
        assert 'class="status-panel status-panel-warn"' in content
        assert "Section summary not generated" in content
        # Explicitly this Section/language variant, not the parent recording.
        assert "section/language variant" in content
        assert "An active transcript exists but the current summary is missing" not in content

    def test_parent_recording_summary_state_never_used(self, client):
        """The parent Recording summary tuple (CURRENT/FAILED/MISSING) is
        irrelevant: the panel follows the Section variant state only."""
        rec, transcript, section = self._section_with_summary(sha="sec-status-parent")
        Recording.objects.filter(pk=rec.pk).update(summary_status=SummaryState.FAILED)
        content = client.get(_url(rec, section)).content.decode()
        assert 'class="status-panel status-panel-ok"' in content
        assert "Section summary current" in content
        # The parent "Summary failed" label/detail is never shown here.
        assert "Summary failed" not in content
        assert "retry is available" not in content

    def test_unresolved_original_panel_is_not_generated(self, client):
        rec, transcript, _fixed = _transcript(sha="sec-status-uo")
        transcript.language_observed = ""
        transcript.save(update_fields=["language_observed"])
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        content = client.get(_url(rec, sections[0]) + "?language=original").content.decode()
        assert "Section summary not generated" in content
        assert "status-panel-warn" in content
        # The existing unresolved-Original empty-state presentation stays.
        assert "source language is not known" in content


class TestSectionOriginReturnLinks:
    """Objective B: the three Section-detail links (History, Section in
    transcript, Full transcript) carry a server-owned ``return_section``
    marker plus the already validated ``lib_return`` token when present —
    so History/transcript breadcrumbs can point back to this exact
    Section."""

    def _token(self):
        from workflow.query import ListFilters
        from workflow.services import library_return

        return library_return.make_token(ListFilters(), 1, "cards")

    def test_all_three_links_carry_return_section(self, client):
        rec, transcript, _fixed = _transcript(sha="sec-return-links")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        section = sections[0]
        content = client.get(_url(rec, section)).content.decode()
        assert f'href="/recordings/{rec.pk}/history/?return_section={section.pk}"' in content
        assert f'href="/recordings/{rec.pk}/transcript/?return_section={section.pk}"' in content
        assert (
            f'href="/recordings/{rec.pk}/transcript/?page=1&amp;return_section={section.pk}#segment-0"'
            in content
        )

    def test_valid_lib_return_propagates_to_all_links(self, client):
        rec, transcript, _fixed = _transcript(sha="sec-return-token")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        section = sections[0]
        token = self._token()
        content = client.get(_url(rec, section) + f"?lib_return={token}").content.decode()
        q = f"return_section={section.pk}&amp;lib_return={token}"
        assert f'href="/recordings/{rec.pk}/history/?{q}"' in content
        assert f'href="/recordings/{rec.pk}/transcript/?{q}"' in content
        assert (
            f'href="/recordings/{rec.pk}/transcript/?page=1&amp;return_section={section.pk}'
            f"&amp;lib_return={token}#segment-0" in content
        )

    def test_forged_lib_return_not_echoed_on_section_links(self, client):
        rec, transcript, _fixed = _transcript(sha="sec-return-badtok")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        section = sections[0]
        content = client.get(_url(rec, section) + "?lib_return=forged").content.decode()
        # The Section marker is always present; the forged token is never echoed.
        assert f'href="/recordings/{rec.pk}/history/?return_section={section.pk}"' in content
        assert "lib_return=" not in content

    def test_follow_history_link_shows_section_breadcrumb(self, client):
        rec, transcript, _fixed = _transcript(sha="sec-return-flow-hist")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        section = sections[0]
        content = client.get(_url(rec, section)).content.decode()
        match = re.search(
            rf'href="(/recordings/{rec.pk}/history/\?return_section={section.pk})"', content
        )
        assert match, "history link not found"
        page = client.get(match.group(1)).content.decode()
        assert (
            f'<a href="/recordings/{rec.pk}/sections/{section.pk}/">&larr; Section</a>'
            in page
        )
        assert "&larr; Recording overview" not in page

    def test_follow_section_in_transcript_link_shows_section_breadcrumb(self, client):
        rec, transcript, _fixed = _transcript(sha="sec-return-flow-jump")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        section = sections[0]
        content = client.get(_url(rec, section)).content.decode()
        match = re.search(
            rf'href="(/recordings/{rec.pk}/transcript/\?page=1&amp;return_section={section.pk}#segment-0)"',
            content,
        )
        assert match, "transcript jump link not found"
        # The rendered href is HTML-escaped (&amp;); decode before following.
        import html as html_module

        page = client.get(html_module.unescape(match.group(1))).content.decode()
        assert (
            f'<a href="/recordings/{rec.pk}/sections/{section.pk}/">&larr; Section</a>'
            in page
        )
        assert "&larr; Recording overview" not in page

    def test_follow_full_transcript_link_shows_section_breadcrumb(self, client):
        rec, transcript, _fixed = _transcript(sha="sec-return-flow-full")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        section = sections[0]
        content = client.get(_url(rec, section)).content.decode()
        match = re.search(
            rf'href="(/recordings/{rec.pk}/transcript/\?return_section={section.pk})"', content
        )
        assert match, "full transcript link not found"
        page = client.get(match.group(1)).content.decode()
        assert (
            f'<a href="/recordings/{rec.pk}/sections/{section.pk}/">&larr; Section</a>'
            in page
        )
        assert "&larr; Recording overview" not in page

    def test_follow_history_link_preserves_valid_lib_return(self, client):
        rec, transcript, _fixed = _transcript(sha="sec-return-flow-tok")
        sections = _split(rec, transcript, splits=[3], titles=["A", "B"])
        section = sections[0]
        token = self._token()
        content = client.get(_url(rec, section) + f"?lib_return={token}").content.decode()
        match = re.search(
            rf'href="(/recordings/{rec.pk}/history/\?return_section={section.pk}&amp;lib_return={token})"',
            content,
        )
        assert match, "history link with token not found"
        import html as html_module

        page = client.get(html_module.unescape(match.group(1))).content.decode()
        assert (
            f'<a href="/recordings/{rec.pk}/sections/{section.pk}/?lib_return={token}">'
            "&larr; Section</a>" in page
        )
        assert "&larr; Recording overview" not in page