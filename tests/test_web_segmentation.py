"""Step 6.1 web integration: transcript-page editor, save action, history.

Proves (per the approved Step 6.1 contract):

- the active Transcript page hosts the only editor: "Edit trim & splits"
  reveals scissors hooks, the bounded staged layout metadata is rendered
  via json_script, and the read-only segmentation fingerprint is embedded;
  historical transcript versions and any explicit ?layout= are read-only;
- ?layout= is recording/transcript parent-scoped (mismatch/unknown 404);
- GETs stay strictly SELECT-only with zero side effects; server pagination
  and stable segment anchors are retained; crop working visibility hides
  cropped rows by default with a Show-full toggle hook and an empty-page
  message; zero splits => zero topics; N splits => N+1 titles in the
  staged metadata; titles/segment text are never unescaped;
- the save route is POST-only + CSRF; the FIRST POST strictly parses the
  bounded payload, runs the shared READ-ONLY semantic validation and
  compares the submitted opaque fingerprint (stale/invalid => reject
  BEFORE any confirmation), then renders the autoescaped confirmation
  (no lock, no write); the CONFIRMED POST runs under the pipeline lock
  with a post-lock fingerprint comparison (stale => safe no-op, lock
  busy => 409), a no-op for unchanged payloads, sanitized failures, and a
  redirect that always targets the parent transcript; corrupt/oversized
  stored layouts fail closed;
- History owns a bounded newest-first table of segmented revisions with
  topic counts from ONE annotation (no N+1), safe read-only v+layout links
  and no title dumping.
"""

from __future__ import annotations

import json
import re

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from workflow.models import (
    ProcessingAttempt,
    Recording,
    Section,
    SegmentedVersion,
    Transcript,
)
from workflow.services.segmentation import (
    save_segmented_version,
    segmentation_fingerprint,
)
from factories import make_transcribed_recording

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


@pytest.fixture
def client():
    return Client()


def _transcript(count=10, sha="seg-web-1"):
    return make_transcribed_recording([f"segment {i}" for i in range(count)], sha=sha)


def _save(recording, transcript, start, end, splits=(), titles=()):
    return save_segmented_version(
        recording.pk, transcript.pk, start, end, list(splits), list(titles)
    )


def _editor_state(content: str) -> dict:
    """Parse the bounded staged-layout metadata from the page json_script."""
    match = re.search(
        r'<script id="segmentation-editor-state" type="application/json">(.*?)</script>',
        content,
        re.DOTALL,
    )
    assert match, "editor state json_script not rendered"
    return json.loads(match.group(1))


def _fingerprint_of_page(content: str) -> str:
    import html

    match = re.search(
        r'name="fingerprint" id="segmentation-fingerprint-input" value="([^"]*)"',
        content,
    )
    assert match, "fingerprint hidden input not rendered"
    # The attribute value is HTML-escaped by Django; the browser (and the
    # server on POST) decodes it back to the exact JSON string.
    return html.unescape(match.group(1))


# ---------------------------------------------------------------------------
# Transcript page: editor visibility / read-only rules / layout selector
# ---------------------------------------------------------------------------


class TestEditorVisibility:
    def test_active_transcript_renders_editor_hooks(self, client):
        recording, transcript, _ = _transcript()
        content = client.get(f"/recordings/{recording.pk}/transcript/").content.decode()
        assert "Edit trim &amp; splits" in content
        assert 'id="transcript-edit-toggle"' in content
        assert 'id="edit-mode-bar" hidden' in content
        assert 'id="segmentation-save-form"' in content
        assert 'id="boundary-action-dialog" hidden' in content
        assert 'id="segmentation-editor-state"' in content
        assert 'name="fingerprint"' in content
        # The crop-toggle is ALWAYS rendered (the JS editable classification
        # depends on it), even on the initial full/no-crop page.
        assert 'id="crop-view-toggle"' in content
        assert 'id="crop-view-bar" hidden' in content
        # Every rendered segment row carries its ordinal for the bounded
        # page-local editor.
        assert 'class="transcript-segment" id="segment-0" data-ordinal="0"' in content
        assert 'data-ordinal="9"' in content
        # No inline event handlers anywhere.
        for handler in re.findall(r"\bon(?:click|change|submit|load|error)\s*=", content):
            raise AssertionError(f"inline event handler found: {handler}")

    def test_initial_editor_prerequisites_complete(self, client):
        """Every element the JS ``editable`` classification requires must
        be present on the initial full/no-crop active-transcript page so
        that pressing Edit works immediately (no crop, no saved version):
        the crop bar/toggle, the edit toggle/bar, the staged-layout
        json_script, the save form and the opaque fingerprint."""
        recording, _t, _s = _transcript(sha="seg-init-prereq")
        content = client.get(f"/recordings/{recording.pk}/transcript/").content.decode()
        required_ids = [
            "crop-view-bar",
            "crop-view-msg",
            "crop-view-toggle",
            "transcript-edit-toggle",
            "segmentation-editor-state",
            "edit-mode-bar",
            "edit-status",
            "edit-clear-crop",
            "edit-reset",
            "edit-save",
            "segmentation-save-form",
            "segmentation-fingerprint-input",
            "segmentation-start-input",
            "segmentation-end-input",
            "boundary-action-dialog",
        ]
        for element_id in required_ids:
            assert f'id="{element_id}"' in content, element_id
        # The staged metadata is the FULL range with zero splits/titles: an
        # initial Edit reveals scissors over the whole transcript and a
        # zero-topic staged state (nothing to name, Save disabled).
        state = _editor_state(content)
        assert state["segment_count"] == 10
        assert state["start"] == 0
        assert state["end"] == 10
        assert state["splits"] == []
        assert state["titles"] == []
        # The crop bar stays hidden but the toggle exists (JS classification).
        assert 'id="crop-view-bar" hidden' in content

    def test_historical_transcript_is_read_only(self, client):
        from django.utils import timezone as tz

        from workflow.models import AttemptOutcome, AttemptStage, ProcessingAttempt

        recording, transcript, _ = _transcript(sha="seg-hist")
        attempt2 = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS, finished_at=tz.now(),
        )
        transcript2 = Transcript.objects.create(
            recording=recording, attempt=attempt2, text_normalized="new"
        )
        transcript.is_active = False
        transcript.superseded_at = tz.now()
        transcript.save()
        transcript2.is_active = True
        transcript2.activated_at = tz.now()
        transcript2.save()
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?v={transcript.pk}"
        ).content.decode()
        assert "HISTORICAL transcript version" in content
        assert "Edit trim &amp; splits" not in content
        assert "segmentation-editor-state" not in content
        assert "boundary-action-dialog" not in content
        assert "Trim &amp; split editing is only available on the active transcript." in content

    def test_explicit_layout_is_read_only_even_when_active(self, client):
        recording, transcript, _ = _transcript()
        _save(recording, transcript, 2, 8, [4], ["A", "B"])
        active = SegmentedVersion.objects.get(transcript=transcript, is_active=True)
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?layout={active.pk}"
        ).content.decode()
        assert "saved trim &amp; split revision" in content
        assert "Edit trim &amp; splits" not in content
        assert "segmentation-editor-state" not in content
        assert "boundary-action-dialog" not in content

    def test_layout_parent_scoped_404s(self, client):
        recording, transcript, _ = _transcript(sha="seg-scope-a")
        other, other_transcript, _ = _transcript(sha="seg-scope-b")
        other_version = _save(other, other_transcript, 0, 5, [], [])
        version = _save(recording, transcript, 0, 5, [], [])
        # Unknown layout id.
        assert client.get(
            f"/recordings/{recording.pk}/transcript/?layout=does-not-exist"
        ).status_code == 404
        # A layout that belongs to a DIFFERENT transcript of the same recording.
        assert client.get(
            f"/recordings/{recording.pk}/transcript/?layout={other_version.version_id}"
        ).status_code == 404
        # A layout of a different recording.
        assert client.get(
            f"/recordings/{other.pk}/transcript/?layout={version.version_id}"
        ).status_code == 404

    def test_unknown_transcript_version_still_404(self, client):
        recording, _t, _s = _transcript()
        assert client.get(f"/recordings/{recording.pk}/transcript/?v=999999").status_code == 404

    def _corrupt_active_layout(self, recording, transcript):
        """Make the active layout's stored state non-canonical (a section
        deleted from the middle of the partition)."""
        version = SegmentedVersion.objects.get(transcript=transcript, is_active=True)
        middle = version.sections.get(ordinal=2)
        middle.delete()
        return version

    def test_corrupt_active_layout_fails_closed_on_get(self, client):
        """A corrupt ACTIVE layout (implicit selection) renders a fixed
        unavailable notice with the full transcript and editing DISABLED —
        never a raw error and never a blind edit over corrupt state."""
        recording, transcript, _ = _transcript(sha="seg-corrupt-get")
        _save(recording, transcript, 1, 9, [3, 6], ["A", "B", "C"])
        self._corrupt_active_layout(recording, transcript)
        content = client.get(
            f"/recordings/{recording.pk}/transcript/"
        ).content.decode()
        assert "invalid and cannot be shown" in content
        assert "Edit trim &amp; splits" not in content
        assert "segmentation-editor-state" not in content
        assert 'name="fingerprint"' not in content
        # The full transcript is shown (no crop applied).
        assert 'data-ordinal="0"' in content
        assert 'data-ordinal="9"' in content

    def test_corrupt_explicit_layout_is_controlled_404(self, client):
        recording, transcript, _ = _transcript(sha="seg-corrupt-404")
        result = _save(recording, transcript, 1, 9, [3, 6], ["A", "B", "C"])
        version = SegmentedVersion.objects.get(pk=result.version_id)
        version.sections.get(ordinal=2).delete()
        response = client.get(
            f"/recordings/{recording.pk}/transcript/?layout={version.pk}"
        )
        assert response.status_code == 404
        assert b"Traceback" not in response.content
        assert b"IntegrityError" not in response.content

    def _single_section_layout(self, recording, transcript):
        """Corrupt a crop-only active version into the unrepresentable
        ONE-topic-Section shape (zero splits must store zero Sections)."""
        result = _save(recording, transcript, 2, 8, [], [])
        version = SegmentedVersion.objects.get(pk=result.version_id)
        Section.objects.create(
            transcript=transcript,
            segmented_version=version,
            ordinal=1,
            title="Lone",
            start_segment_ordinal=2,
            end_segment_ordinal_exclusive=8,
        )
        return version

    def test_single_section_active_layout_fails_closed_on_get(self, client):
        """A stored layout holding exactly ONE topic Section is not
        representable by the contract: the GET shows the fixed unavailable
        notice with editing DISABLED — never a heading for an invalid
        shape and never an edit over corrupt state."""
        recording, transcript, _ = _transcript(sha="seg-single-get")
        self._single_section_layout(recording, transcript)
        content = client.get(
            f"/recordings/{recording.pk}/transcript/"
        ).content.decode()
        assert "invalid and cannot be shown" in content
        assert "Edit trim &amp; splits" not in content
        assert "segmentation-editor-state" not in content
        assert 'name="fingerprint"' not in content
        # No topic heading is rendered for the lone invalid section.
        assert 'class="topic-heading"' not in content

    def test_single_section_explicit_layout_is_404(self, client):
        recording, transcript, _ = _transcript(sha="seg-single-404")
        version = self._single_section_layout(recording, transcript)
        response = client.get(
            f"/recordings/{recording.pk}/transcript/?layout={version.pk}"
        )
        assert response.status_code == 404
        assert b"Traceback" not in response.content

    def test_cross_transcript_section_is_layout_invalid(self, client):
        """A Section row whose transcript differs from its layout's
        transcript is corrupt stored state (SQLite cannot CHECK it): the
        shared canonical validator fails closed and the GET shows the
        fixed unavailable state."""
        recording, transcript, _ = _transcript(sha="seg-cross-sec")
        other, other_transcript, _ = _transcript(sha="seg-cross-sec2")
        result = _save(recording, transcript, 1, 9, [3, 6], ["A", "B", "C"])
        version = SegmentedVersion.objects.get(pk=result.version_id)
        moved = version.sections.get(ordinal=2)
        moved.transcript = other_transcript
        moved.save(update_fields=["transcript"])
        content = client.get(
            f"/recordings/{recording.pk}/transcript/"
        ).content.decode()
        assert "invalid and cannot be shown" in content
        assert "Edit trim &amp; splits" not in content

    def test_corrupt_active_layout_save_rejected_sanitized(self, client):
        """Saving over a corrupt active layout fails closed with the fixed
        sanitized layout_invalid category and zero DML."""
        recording, transcript, _ = _transcript(sha="seg-corrupt-save")
        _save(recording, transcript, 1, 9, [3, 6], ["A", "B", "C"])
        fingerprint = segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki")
        self._corrupt_active_layout(recording, transcript)
        # The read-only first-POST validation fails closed (the active
        # layout is no longer canonical).
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "1",
                "end_exclusive": "9",
                "split": ["3", "6"],
                "title": ["A", "B", "C"],
                "fingerprint": fingerprint,
            },
        )
        assert response.status_code == 400
        assert "revision is invalid" in response.content.decode()
        assert SegmentedVersion.objects.count() == 1

    def test_oversized_active_layout_fails_closed(self, client):
        """An oversized stored layout (more than MAX_TOPIC_SECTIONS rows)
        is bounded fail-closed: the GET never loads it and the page shows
        the fixed unavailable state; an explicit ?layout= is a 404."""
        from workflow.services.segmentation import MAX_TOPIC_SECTIONS

        recording, transcript, _ = _transcript(sha="seg-oversize")
        # A non-full crop creates a real revision (the full+zero initial
        # state would be a no-op).
        _save(recording, transcript, 1, 10, [], [])
        version = SegmentedVersion.objects.get(transcript=transcript, is_active=True)
        Section.objects.bulk_create(
            [
                Section(
                    transcript=transcript,
                    segmented_version=version,
                    ordinal=i,
                    title=f"t{i}",
                    start_segment_ordinal=i,
                    end_segment_ordinal_exclusive=i + 1,
                )
                for i in range(1, MAX_TOPIC_SECTIONS + 2)  # 201 rows > 200 cap
            ]
        )
        content = client.get(
            f"/recordings/{recording.pk}/transcript/"
        ).content.decode()
        assert "invalid and cannot be shown" in content
        assert client.get(
            f"/recordings/{recording.pk}/transcript/?layout={version.pk}"
        ).status_code == 404
        # The canonical validator caps at MAX+1 (never loads unbounded).
        from workflow.services.segmentation import canonical_layout_for_transcript
        from workflow.services.segmentation import SegmentationError

        with pytest.raises(SegmentationError) as exc:
            canonical_layout_for_transcript(version, transcript)
        assert exc.value.code == "layout_invalid"

    def test_get_is_select_only_and_side_effect_free(self, client):
        recording, transcript, _ = _transcript(sha="seg-select")
        _save(recording, transcript, 1, 4, [], [])
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(f"/recordings/{recording.pk}/transcript/")
        assert response.status_code == 200
        non_select = [
            q for q in ctx.captured_queries
            if not q["sql"].lstrip().upper().startswith("SELECT")
        ]
        assert non_select == []
        # Zero side effects: no rows created/changed by the GET.
        assert SegmentedVersion.objects.count() == 1
        assert Transcript.objects.get(pk=transcript.pk).pk == transcript.pk


class TestEditorStateMetadata:
    def test_zero_split_has_zero_topics(self, client):
        recording, transcript, _ = _transcript()
        _save(recording, transcript, 2, 8, [], [])
        content = client.get(f"/recordings/{recording.pk}/transcript/").content.decode()
        state = _editor_state(content)
        assert state["segment_count"] == 10
        assert state["start"] == 2
        assert state["end"] == 8
        assert state["splits"] == []
        assert state["titles"] == []

    def test_n_splits_render_n_plus_one_titles(self, client):
        recording, transcript, _ = _transcript()
        _save(recording, transcript, 1, 9, [3, 6], ["A", "B", "C"])
        content = client.get(f"/recordings/{recording.pk}/transcript/").content.decode()
        state = _editor_state(content)
        assert state["splits"] == [3, 6]
        assert state["titles"] == ["A", "B", "C"]

    def test_editor_state_and_fingerprint_escape_user_titles(self, client):
        recording, transcript, _ = _transcript()
        _save(recording, transcript, 0, 10, [5], ["A", "<script>alert('x')</script>"])
        content = client.get(f"/recordings/{recording.pk}/transcript/").content.decode()
        assert "<script>alert('x')</script>" not in content
        state = _editor_state(content)
        # The json_script data block carries the escaped form only.
        assert state["titles"] == ["A", "<script>alert('x')</script>"]

    def test_fingerprint_is_opaque_sha256_and_binds_state(self, client):
        recording, transcript, _ = _transcript()
        result = _save(recording, transcript, 1, 7, [3], ["A", "B"])
        content = client.get(f"/recordings/{recording.pk}/transcript/").content.decode()
        raw = _fingerprint_of_page(content)
        # Opaque canonical 64-lowercase-hex SHA-256 — never a JSON blob
        # with titles/ids embedded in the hidden form value.
        assert re.fullmatch(r"[0-9a-f]{64}", raw), raw
        assert raw.startswith("{") is False
        assert '"' not in raw
        assert "A" not in raw and "B" not in raw
        assert result.version_id not in raw
        # Equals the freshly computed fingerprint for the same state.
        assert raw == segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki")
        # A different state produces a different fingerprint.
        _save(recording, transcript, 0, 10, [], [])
        assert (
            segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki") != raw
        )


class TestCropWorkingView:
    def test_cropped_rows_hidden_with_toggle_hook(self, client):
        recording, transcript, _ = _transcript()
        _save(recording, transcript, 2, 5, [], [])
        content = client.get(f"/recordings/{recording.pk}/transcript/").content.decode()
        # Retained rows visible; cropped rows carry the hidden attribute.
        for ordinal in (2, 3, 4):
            assert f'data-ordinal="{ordinal}"' in content
            assert f'id="segment-{ordinal}"' in content
            assert re.search(
                rf'id="segment-{ordinal}"[^>]*>', content
            ) and f'id="segment-{ordinal}"' in content
        # The toggle + message bar are server-rendered (no-JS readable).
        assert 'id="crop-view-bar"' in content
        assert 'id="crop-view-toggle"' in content
        assert "Show full transcript" in content
        assert "7 lines hidden" in content
        # Anchors and full pagination stay intact.
        assert 'id="segment-0"' in content
        assert 'id="segment-9"' in content

    def test_cropped_segments_carry_hidden_attribute(self, client):
        recording, transcript, _ = _transcript()
        _save(recording, transcript, 2, 5, [], [])
        content = client.get(f"/recordings/{recording.pk}/transcript/").content.decode()
        assert 'id="segment-0" data-ordinal="0" hidden' in content
        assert 'id="segment-1" data-ordinal="1" hidden' in content
        assert 'id="segment-5" data-ordinal="5" hidden' in content
        assert 'id="segment-9" data-ordinal="9" hidden' in content
        assert 'id="segment-2" data-ordinal="2">' in content
        assert 'id="segment-4" data-ordinal="4">' in content

    def test_page_with_no_working_rows_shows_compact_message(self, client):
        # 450 segments, default 200 per page. A crop ending at 200 means
        # page 2 (segments 200..399) has zero retained rows.
        recording, transcript, _ = make_transcribed_recording(
            [f"segment {i}" for i in range(450)], sha="seg-empty-page"
        )
        _save(recording, transcript, 0, 200, [], [])
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?page=2"
        ).content.decode()
        assert "none on this page" in content
        assert 'id="crop-view-toggle"' in content
        assert "Show full transcript" in content
        assert "450 segments" in content  # pagination over the full transcript

    def test_pagination_preserves_historical_and_layout_context(self, client):
        from django.utils import timezone as tz

        from workflow.models import AttemptOutcome, AttemptStage, ProcessingAttempt

        recording, transcript, _ = make_transcribed_recording(
            [f"segment {i}" for i in range(450)], sha="seg-paginate"
        )
        result = _save(recording, transcript, 0, 300, [100], ["A", "B"])
        # Active transcript + explicit layout: pagination keeps the layout.
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?layout={result.version_id}&page=1"
        ).content.decode()
        assert f"?layout={result.version_id}&amp;page=2" in content

        # Historical transcript + explicit layout: pagination keeps BOTH v
        # and layout so old anchors/links stay stable.
        attempt2 = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS, finished_at=tz.now(),
        )
        transcript2 = Transcript.objects.create(
            recording=recording, attempt=attempt2, text_normalized="new"
        )
        transcript.is_active = False
        transcript.superseded_at = tz.now()
        transcript.save()
        transcript2.is_active = True
        transcript2.activated_at = tz.now()
        transcript2.save()
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?v={transcript.pk}"
            f"&layout={result.version_id}&page=1"
        ).content.decode()
        assert (
            f"?v={transcript.pk}&amp;layout={result.version_id}&amp;page=2" in content
        )

    def test_no_crop_still_renders_toggle_for_editable_contract(self, client):
        """The crop bar stays hidden without a crop, but the toggle element
        is ALWAYS rendered so the editor's editable-page classification
        (which requires the toggle) works on the initial full/no-crop
        state — Edit must do something there."""
        recording, _t, _s = _transcript(sha="seg-nocrop")
        content = client.get(f"/recordings/{recording.pk}/transcript/").content.decode()
        assert 'id="crop-view-bar" hidden' in content
        assert 'id="crop-view-toggle"' in content
        # Editing is available in the initial state: the toggle + editor
        # bar + fingerprint + staged metadata are all present.
        assert "Edit trim &amp; splits" in content
        assert 'id="edit-mode-bar" hidden' in content
        assert 'name="fingerprint"' in content
        state = _editor_state(content)
        assert state["start"] == 0 and state["end"] == 10


# ---------------------------------------------------------------------------
# Topic headings: bounded autoescaped section titles in normal/read-only
# views (including explicit/historical layouts) + the off-page context
# heading for the section containing the first visible working row.
# ---------------------------------------------------------------------------


class TestTopicHeadings:
    def test_topic_headings_rendered_in_working_view(self, client):
        recording, transcript, _ = _transcript()
        _save(recording, transcript, 0, 10, [3, 6], ["Intro", "Body", "Close"])
        content = client.get(f"/recordings/{recording.pk}/transcript/").content.decode()
        # Headings sit at their section starts (before segments 3 and 6).
        assert '<div class="topic-heading" data-section-start="3"' in content
        assert '<div class="topic-heading" data-section-start="6"' in content
        assert "Topic</span>" in content
        # Section-start headings are NOT context headings.
        assert 'data-section-start="3"' in content and 'data-section-start="6"' in content

    def test_zero_splits_render_zero_headings(self, client):
        recording, transcript, _ = _transcript()
        _save(recording, transcript, 2, 8, [], [])
        content = client.get(f"/recordings/{recording.pk}/transcript/").content.decode()
        assert 'class="topic-heading"' not in content

    def test_headings_are_autoescaped(self, client):
        recording, transcript, _ = _transcript()
        _save(recording, transcript, 0, 10, [5], ["A", "<script>alert(1)</script>"])
        content = client.get(f"/recordings/{recording.pk}/transcript/").content.decode()
        assert "<script>alert(1)</script>" not in content
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content

    def test_headings_on_readonly_explicit_layout(self, client):
        recording, transcript, _ = _transcript(sha="seg-hdr-layout")
        result = _save(recording, transcript, 1, 9, [4], ["A", "B"])
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?layout={result.version_id}"
        ).content.decode()
        assert "read only" in content
        assert 'class="topic-heading" data-section-start="4"' in content
        assert "Edit trim &amp; splits" not in content

    def test_headings_escaped_on_readonly_explicit_layout(self, client):
        """Read-only explicit-layout headings are autoescaped exactly like
        the working view (titles are user content, never raw HTML)."""
        recording, transcript, _ = _transcript(sha="seg-hdr-layout-xss")
        result = _save(
            recording, transcript, 0, 10, [5],
            ["A", "<script>alert(1)</script>"],
        )
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?layout={result.version_id}"
        ).content.decode()
        assert "<script>alert(1)</script>" not in content
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content

    def test_headings_on_historical_transcript_with_layout(self, client):
        from django.utils import timezone as tz

        from workflow.models import AttemptOutcome, AttemptStage, ProcessingAttempt

        recording, transcript, _ = _transcript(sha="seg-hdr-hist")
        result = _save(recording, transcript, 1, 9, [4], ["Old A", "Old B"])
        attempt2 = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS, finished_at=tz.now(),
        )
        transcript2 = Transcript.objects.create(
            recording=recording, attempt=attempt2, text_normalized="new"
        )
        transcript.is_active = False
        transcript.superseded_at = tz.now()
        transcript.save()
        transcript2.is_active = True
        transcript2.activated_at = tz.now()
        transcript2.save()
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?v={transcript.pk}"
            f"&layout={result.version_id}"
        ).content.decode()
        assert "HISTORICAL transcript version" in content
        assert 'data-section-start="4"' in content
        assert "Old A" in content and "Old B" in content
        assert "Edit trim &amp; splits" not in content

    def test_headings_escaped_on_historical_transcript_with_layout(self, client):
        from django.utils import timezone as tz

        from workflow.models import AttemptOutcome, AttemptStage, ProcessingAttempt

        recording, transcript, _ = _transcript(sha="seg-hdr-hist-xss")
        result = _save(
            recording, transcript, 1, 9, [4],
            ["Old <b>A</b>", "<script>alert('x')</script>"],
        )
        attempt2 = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS, finished_at=tz.now(),
        )
        transcript2 = Transcript.objects.create(
            recording=recording, attempt=attempt2, text_normalized="new"
        )
        transcript.is_active = False
        transcript.superseded_at = tz.now()
        transcript.save()
        transcript2.is_active = True
        transcript2.activated_at = tz.now()
        transcript2.save()
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?v={transcript.pk}"
            f"&layout={result.version_id}"
        ).content.decode()
        assert "<script>alert('x')</script>" not in content
        assert "&lt;b&gt;A&lt;/b&gt;" in content
        # Django autoescaping also escapes the apostrophe (&#x27;).
        assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in content

    def test_context_heading_is_autoescaped(self, client):
        """The off-page context heading carries the same autoescaping as
        normal headings (it is the same user-owned section title)."""
        recording, transcript, _ = make_transcribed_recording(
            [f"segment {i}" for i in range(450)], sha="seg-hdr-ctx-xss"
        )
        _save(
            recording, transcript, 0, 400, [50, 300],
            ["Start", "<script>ctx</script>", "End"],
        )
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?page=2"
        ).content.decode()
        assert 'class="topic-heading topic-heading-context" data-section-start="50"' in content
        assert "<script>ctx</script>" not in content
        assert "&lt;script&gt;ctx&lt;/script&gt;" in content

    def test_context_heading_when_section_starts_off_page(self, client):
        """A topic section that starts on an EARLIER page but contains the
        first visible working row renders ONE context heading at the top of
        the visible run (bounded pagination over the full transcript)."""
        recording, transcript, _ = make_transcribed_recording(
            [f"segment {i}" for i in range(450)], sha="seg-hdr-context"
        )
        # Section [50, 300) starts on page 1; page 2 (200..399) begins
        # mid-section.
        _save(recording, transcript, 0, 400, [50, 300], ["Start", "Middle", "End"])
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?page=2"
        ).content.decode()
        # The section containing the first visible working row (200, inside
        # [50,300)) started earlier: one CONTEXT heading is rendered.
        assert 'class="topic-heading topic-heading-context" data-section-start="50"' in content
        assert "Middle" in content
        # The section starting on this page (300) is a normal heading.
        assert 'class="topic-heading" data-section-start="300"' in content
        # Exactly two heading elements on this page (context + section-300).
        assert content.count("data-section-start=") == 2

    def test_context_heading_only_for_first_visible_working_row(self, client):
        recording, transcript, _ = make_transcribed_recording(
            [f"segment {i}" for i in range(450)], sha="seg-hdr-ctx2"
        )
        # Crop [50, 400) with splits [100, 300]: page 2's first retained
        # row is 200 (inside [100,300)), which started on page 1; rows
        # 0..49 and 400..449 are cropped.
        _save(recording, transcript, 50, 400, [100, 300], ["A", "B", "C"])
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?page=2"
        ).content.decode()
        assert 'class="topic-heading topic-heading-context" data-section-start="100"' in content
        assert content.count("data-section-start=") == 2  # context + section-300


# ---------------------------------------------------------------------------
# Save action: POST-only / CSRF / confirmation / execution / fingerprint
# ---------------------------------------------------------------------------


class TestSaveActionBasics:
    def test_get_is_405(self, client):
        recording, _t, _s = _transcript(sha="seg-get405")
        assert client.get(
            f"/recordings/{recording.pk}/transcript/save/"
        ).status_code == 405

    def test_post_without_csrf_rejected(self):
        recording, _t, _s = _transcript(sha="seg-csrf")
        csrf_client = Client(enforce_csrf_checks=True)
        response = csrf_client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {"transcript_id": str(_t.pk), "start": "0", "end_exclusive": "5"},
        )
        assert response.status_code == 403

    def test_confirmation_renders_without_lock_or_write(self, client, monkeypatch):
        recording, transcript, _ = _transcript(sha="seg-confirm")
        fingerprint = segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki")

        def busy(*args, **kwargs):
            raise AssertionError("the confirmation step must not take the pipeline lock")

        monkeypatch.setattr("workflow.services.web_actions.pipeline_lock", busy)
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "2",
                "end_exclusive": "8",
                "split": ["4"],
                "title": ["A", "B"],
                "fingerprint": fingerprint,
            },
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert "Save this trim &amp; split revision?" in content
        assert "segments 2–7" in content
        assert "1 split, 2 named sections" in content
        # No mutation happened.
        assert SegmentedVersion.objects.count() == 0
        # The confirmation carries the full hidden payload back.
        assert 'name="split" value="4"' in content
        assert 'name="title" value="A"' in content
        assert 'name="title" value="B"' in content
        assert 'name="confirmed" value="1"' in content

    def test_confirmation_escapes_topic_titles(self, client):
        recording, transcript, _ = _transcript(sha="seg-xss-confirm")
        fingerprint = segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki")
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "0",
                "end_exclusive": "10",
                "split": ["5"],
                "title": ["<script>alert(1)</script>", "&lt;b&gt;safe&lt;/b&gt;"],
                "fingerprint": fingerprint,
            },
        )
        content = response.content.decode()
        assert "<script>alert(1)</script>" not in content
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content
        assert SegmentedVersion.objects.count() == 0

    def test_stale_fingerprint_rejected_before_confirmation(self, client, monkeypatch):
        """The FIRST POST must reject a stale page BEFORE any confirmation:
        the submitted opaque fingerprint is compared against a freshly
        computed one — never silently re-synthesized. No lock, no DML."""
        recording, transcript, _ = _transcript(sha="seg-stale-first")
        stale = segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki")
        # State changes after the page was rendered.
        _save(recording, transcript, 1, 5, [], [])

        def no_lock(*args, **kwargs):
            raise AssertionError("no pipeline lock may be taken on the first POST")

        monkeypatch.setattr("workflow.services.web_actions.pipeline_lock", no_lock)
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "2",
                "end_exclusive": "8",
                "fingerprint": stale,
            },
        )
        assert response.status_code == 400
        assert "state changed" in response.content.decode().lower()
        # The page-render-time revision is untouched.
        assert SegmentedVersion.objects.filter(transcript=transcript).count() == 1
        assert SegmentedVersion.objects.get(transcript=transcript, is_active=True).revision == 1

    def test_semantically_invalid_payload_rejected_before_confirmation(
        self, client, monkeypatch
    ):
        """The FIRST POST validates semantics READ-ONLY (no lock/recovery/
        write): invalid ranges/splits/titles reject with 400 and never
        reach the confirmation page or any mutation."""
        recording, transcript, _ = _transcript(sha="seg-invalid-first")
        fingerprint = segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki")

        def no_lock(*args, **kwargs):
            raise AssertionError("no pipeline lock may be taken on the first POST")

        monkeypatch.setattr("workflow.services.web_actions.pipeline_lock", no_lock)
        base = {
            "transcript_id": str(transcript.pk),
            "fingerprint": fingerprint,
        }
        cases = [
            ({"start": "0", "end_exclusive": "0"}, "leave at least one segment"),
            ({"start": "5", "end_exclusive": "4"}, "leave at least one segment"),
            ({"start": "-1", "end_exclusive": "8"}, "malformed"),
            ({"start": "0", "end_exclusive": "11"}, "outside the transcript"),
            ({"start": "2", "end_exclusive": "8", "split": ["2"],
              "title": ["A", "B"]}, "inside the working range"),
            ({"start": "2", "end_exclusive": "8", "split": ["8"],
              "title": ["A", "B"]}, "inside the working range"),
            ({"start": "2", "end_exclusive": "8", "split": ["9"],
              "title": ["A", "B"]}, "outside the working range"),
            ({"start": "2", "end_exclusive": "8", "split": ["4", "4"],
              "title": ["A", "B", "C"]}, "duplicate split"),
            ({"start": "0", "end_exclusive": "10", "split": ["5"],
              "title": ["   ", "B"]}, "needs a name"),
            ({"start": "0", "end_exclusive": "10", "split": ["5"],
              "title": ["A\x00", "B"]}, "forbidden characters"),
            ({"start": "0", "end_exclusive": "10", "split": ["5"],
              "title": ["A", "x" * 256]}, "too long"),
        ]
        for overrides, expected in cases:
            data = dict(base)
            data.update(overrides)
            response = client.post(
                f"/recordings/{recording.pk}/transcript/save/", data
            )
            assert response.status_code == 400, overrides
            assert expected in response.content.decode().lower(), overrides
            assert "Save this trim &amp; split revision?" not in response.content.decode()
        assert SegmentedVersion.objects.count() == 0

    def test_malformed_payloads_rejected_without_write(self, client):
        recording, transcript, _ = _transcript(sha="seg-malformed")
        fingerprint = segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki")
        base = {"transcript_id": str(transcript.pk), "fingerprint": fingerprint}
        cases = [
            ({"start": "1.5", "end_exclusive": "5"}, "malformed"),
            ({"start": "true", "end_exclusive": "5"}, "malformed"),
            ({"start": "", "end_exclusive": "5"}, "malformed"),
            ({"start": "0", "end_exclusive": "5", "split": ["4", "x"]}, "malformed"),
            (
                {"start": "0", "end_exclusive": "5", "title": ["only-one"]},
                "topic count",
            ),
        ]
        for overrides, expected in cases:
            data = dict(base)
            data.update(overrides)
            response = client.post(
                f"/recordings/{recording.pk}/transcript/save/", data
            )
            assert response.status_code == 400, overrides
            assert expected in response.content.decode().lower(), overrides
        # Oversized structure: 300 splits exceeds the 199 bound.
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "0",
                "end_exclusive": "10",
                "split": [str(i) for i in range(300)],
                "fingerprint": fingerprint,
            },
        )
        assert response.status_code == 400
        assert "Too many topic sections." in response.content.decode()
        assert SegmentedVersion.objects.count() == 0

    def test_strict_parse_rejects_duplicates_unknowns_and_bad_fingerprint(
        self, client
    ):
        """The bounded parse is strict: duplicate singleton fields, unknown
        fields, malformed fingerprints, and a ``confirmed`` value other
        than exactly one ``1`` are rejected with no write."""
        recording, transcript, _ = _transcript(sha="seg-strict")
        fingerprint = segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki")
        base = {"transcript_id": str(transcript.pk), "fingerprint": fingerprint}
        # Duplicate singleton fields (the same name submitted twice).
        for field in ("transcript_id", "start", "end_exclusive", "fingerprint"):
            data = {
                "transcript_id": str(transcript.pk),
                "fingerprint": fingerprint,
                "start": "0",
                "end_exclusive": "5",
            }
            data[field] = [data[field], data[field]]
            response = client.post(
                f"/recordings/{recording.pk}/transcript/save/", data
            )
            assert response.status_code == 400, field
        # Unknown field.
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            dict(base, start="0", end_exclusive="5", evil="x"),
        )
        assert response.status_code == 400
        # Malformed fingerprints (non-hex, wrong length, uppercase, blank).
        for bad in ("zz" * 32, "a" * 63, "A" * 64, "", "x" * 64):
            response = client.post(
                f"/recordings/{recording.pk}/transcript/save/",
                dict(base, fingerprint=bad, start="0", end_exclusive="5"),
            )
            assert response.status_code == 400, bad
        # ``confirmed`` must be absent or exactly one ``1``.
        for bad_confirmed in ("2", "0", "yes", ""):
            response = client.post(
                f"/recordings/{recording.pk}/transcript/save/",
                dict(base, start="0", end_exclusive="5", confirmed=bad_confirmed),
            )
            assert response.status_code == 400, bad_confirmed
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            dict(base, start="0", end_exclusive="5", confirmed=["1", "1"]),
        )
        assert response.status_code == 400
        assert SegmentedVersion.objects.count() == 0

    def test_confirmed_without_fingerprint_rejected_before_lock(self, client, monkeypatch):
        """A confirmed POST without the required opaque fingerprint is
        rejected BEFORE the pipeline lock is ever taken."""
        recording, transcript, _ = _transcript(sha="seg-nofp")

        def no_lock(*args, **kwargs):
            raise AssertionError("the pipeline lock must not be taken without a fingerprint")

        monkeypatch.setattr("workflow.services.web_actions.pipeline_lock", no_lock)
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "0",
                "end_exclusive": "5",
                "confirmed": "1",
            },
        )
        assert response.status_code == 400
        assert "fingerprint" in response.content.decode().lower()
        assert SegmentedVersion.objects.count() == 0

    def test_missing_fingerprint_rejected_on_first_post(self, client, monkeypatch):
        """The FIRST POST also REQUIRES the opaque fingerprint: a missing
        value is rejected before any confirmation, lock, or DML."""
        recording, transcript, _ = _transcript(sha="seg-nofp-first")

        def no_lock(*args, **kwargs):
            raise AssertionError("no pipeline lock may be taken without a fingerprint")

        monkeypatch.setattr("workflow.services.web_actions.pipeline_lock", no_lock)
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "2",
                "end_exclusive": "8",
            },
        )
        assert response.status_code == 400
        assert "fingerprint" in response.content.decode().lower()
        assert "Save this trim &amp; split revision?" not in response.content.decode()
        assert SegmentedVersion.objects.count() == 0

    def test_non_active_transcript_rejected(self, client):
        from django.utils import timezone as tz

        from workflow.models import AttemptOutcome, AttemptStage, ProcessingAttempt

        recording, transcript, _ = _transcript(sha="seg-notactive")
        attempt2 = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS, finished_at=tz.now(),
        )
        transcript2 = Transcript.objects.create(
            recording=recording, attempt=attempt2, text_normalized="new"
        )
        transcript.is_active = False
        transcript.superseded_at = tz.now()
        transcript.save()
        transcript2.is_active = True
        transcript2.activated_at = tz.now()
        transcript2.save()
        fingerprint = segmentation_fingerprint(recording.pk, transcript2)
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "0",
                "end_exclusive": "5",
                "fingerprint": fingerprint,
            },
        )
        assert response.status_code == 400
        assert SegmentedVersion.objects.count() == 0

    def test_transcript_id_not_parent_scoped_rejected(self, client):
        recording, _t, _s = _transcript(sha="seg-other-rec")
        other, other_transcript, _ = _transcript(sha="seg-other-rec2")
        fingerprint = segmentation_fingerprint(other.pk, other_transcript)
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(other_transcript.pk),
                "start": "0",
                "end_exclusive": "5",
                "fingerprint": fingerprint,
            },
        )
        assert response.status_code == 400


class TestSaveExecution:
    def test_confirmed_save_creates_revision_and_redirects(self, client):
        recording, transcript, _ = _transcript(sha="seg-save-ok")
        fingerprint = segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki")
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "2",
                "end_exclusive": "8",
                "split": ["4"],
                "title": ["Intro", "Main"],
                "fingerprint": fingerprint,
                "confirmed": "1",
            },
        )
        assert response.status_code == 302
        assert response.url == f"/recordings/{recording.pk}/transcript/"
        version = SegmentedVersion.objects.get(transcript=transcript, is_active=True)
        assert version.revision == 1
        assert (version.start_segment_ordinal, version.end_segment_ordinal_exclusive) == (2, 8)
        sections = list(version.sections.order_by("ordinal"))
        assert [(s.title, s.start_segment_ordinal, s.end_segment_ordinal_exclusive)
                for s in sections] == [("Intro", 2, 4), ("Main", 4, 8)]
        # Fixed ordinal-0 section untouched.
        assert transcript.sections.filter(
            ordinal=0, segmented_version__isnull=True
        ).count() == 1

    def test_confirmed_save_is_noop_for_unchanged_payload(self, client):
        recording, transcript, _ = _transcript(sha="seg-noop")
        _save(recording, transcript, 2, 8, [4], ["Intro", "Main"])
        fingerprint = segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki")
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "2",
                "end_exclusive": "8",
                "split": ["4"],
                "title": ["Intro", "Main"],
                "fingerprint": fingerprint,
                "confirmed": "1",
            },
        )
        assert response.status_code == 302
        assert SegmentedVersion.objects.filter(transcript=transcript).count() == 1
        assert SegmentedVersion.objects.filter(
            transcript=transcript, is_active=True
        ).count() == 1

    def test_initial_full_zero_save_is_noop(self, client):
        recording, transcript, _ = _transcript(sha="seg-noop0")
        fingerprint = segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki")
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "0",
                "end_exclusive": "10",
                "fingerprint": fingerprint,
                "confirmed": "1",
            },
        )
        assert response.status_code == 302
        assert SegmentedVersion.objects.count() == 0

    def test_stale_fingerprint_is_safe_noop(self, client):
        recording, transcript, _ = _transcript(sha="seg-stale")
        stale = segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki")
        # Another process saves a revision after the page was rendered.
        _save(recording, transcript, 1, 5, [], [])
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "2",
                "end_exclusive": "8",
                "fingerprint": stale,
                "confirmed": "1",
            },
        )
        assert response.status_code == 302
        # Zero DML: still exactly the one revision from the other save.
        assert SegmentedVersion.objects.filter(transcript=transcript).count() == 1
        version = SegmentedVersion.objects.get(transcript=transcript, is_active=True)
        assert (version.start_segment_ordinal, version.end_segment_ordinal_exclusive) == (1, 5)

    def test_lock_busy_returns_409(self, client, monkeypatch):
        recording, transcript, _ = _transcript(sha="seg-lockbusy")
        fingerprint = segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki")
        from workflow.services.pipeline_lock import PipelineBusy

        def busy(config):
            raise PipelineBusy("777")

        monkeypatch.setattr("workflow.services.web_actions.pipeline_lock", busy)
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "0",
                "end_exclusive": "5",
                "fingerprint": fingerprint,
                "confirmed": "1",
            },
        )
        assert response.status_code == 409
        assert "Another pipeline process is active" in response.content.decode()
        assert SegmentedVersion.objects.count() == 0

    def test_save_schedules_no_search_sync(self, client):
        from workflow.models import SearchDocument

        recording, transcript, _ = _transcript(sha="seg-nosync")
        fingerprint = segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki")
        before = SearchDocument.objects.count()
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "0",
                "end_exclusive": "8",
                "split": ["4"],
                "title": ["A", "B"],
                "fingerprint": fingerprint,
                "confirmed": "1",
            },
        )
        assert response.status_code == 302
        assert SearchDocument.objects.count() == before

    def test_second_revision_supersedes_prior(self, client):
        recording, transcript, _ = _transcript(sha="seg-rev2")
        _save(recording, transcript, 1, 5, [], [])
        fingerprint = segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki")
        client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "2",
                "end_exclusive": "9",
                "split": ["5"],
                "title": ["A", "B"],
                "fingerprint": fingerprint,
                "confirmed": "1",
            },
        )
        versions = list(
            SegmentedVersion.objects.filter(transcript=transcript).order_by("revision")
        )
        assert [v.revision for v in versions] == [1, 2]
        assert versions[0].is_active is False
        assert versions[0].superseded_at is not None
        assert versions[1].is_active is True
        assert versions[1].superseded_at is None

    def test_confirmed_save_service_rejection_is_sanitized(self, client, monkeypatch):
        """A confirmed-POST failure inside the locked execution yields a
        fixed sanitized failure, a safe parent redirect, and zero DML —
        never a raw exception/SQL/ids page."""
        from workflow.services.segmentation import SegmentationError

        recording, transcript, _ = _transcript(sha="seg-svc-reject")
        fingerprint = segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki")

        def failing_save(*args, **kwargs):
            raise SegmentationError("storage_error")

        monkeypatch.setattr(
            "workflow.services.segmentation.save_segmented_version", failing_save
        )
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "4",
                "end_exclusive": "8",
                "split": ["5"],
                "title": ["A", "B"],
                "fingerprint": fingerprint,
                "confirmed": "1",
            },
        )
        assert response.status_code == 302
        assert response.url == f"/recordings/{recording.pk}/transcript/"
        assert SegmentedVersion.objects.count() == 0
        body = client.get(response.headers["Location"]).content.decode()
        assert "Saving failed — try again." in body
        assert "Traceback" not in body
        assert "IntegrityError" not in body

    def test_fingerprint_failure_after_lock_is_sanitized(self, client, monkeypatch):
        """A SegmentationError from the POST-LOCK fingerprint recomputation
        (e.g. a concurrent process corrupting the active layout) must be
        caught into a fixed sanitized ActionOutcome — a 302 redirect, never
        a raw 500 — with zero segmentation DML."""
        from workflow.services.segmentation import SegmentationError

        recording, transcript, _ = _transcript(sha="seg-fp-fail")
        fingerprint = segmentation_fingerprint(recording.pk, transcript, timezone_name="Europe/Helsinki")

        def boom(*args, **kwargs):
            raise SegmentationError("layout_invalid")

        monkeypatch.setattr(
            "workflow.services.segmentation.segmentation_fingerprint", boom
        )
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "2",
                "end_exclusive": "8",
                "split": ["4"],
                "title": ["A", "B"],
                "fingerprint": fingerprint,
                "confirmed": "1",
            },
        )
        assert response.status_code == 302
        assert response.url == f"/recordings/{recording.pk}/transcript/"
        # Zero segmentation DML: nothing created/superseded.
        assert SegmentedVersion.objects.count() == 0
        assert Section.objects.filter(segmented_version__isnull=False).count() == 0
        # The redirect target carries the fixed sanitized message (the GET
        # recomputes the fingerprint for the editable page, so restore the
        # real implementation before following it).
        monkeypatch.undo()
        body = client.get(response.headers["Location"]).content.decode()
        assert "revision is invalid" in body
        assert "Traceback" not in body


# ---------------------------------------------------------------------------
# History: bounded newest-first segmented revisions table
# ---------------------------------------------------------------------------


def _history_rows(content: str):
    table = re.search(
        r'<caption>Segmented working-layout revisions for this recording</caption>'
        r'.*?</table>',
        content,
        re.DOTALL,
    )
    if not table:
        return []
    return re.findall(r"<tr>.*?</tr>", table.group(0), re.DOTALL)


class TestSegmentedHistory:
    def test_history_table_renders_without_titles(self, client):
        recording, transcript, _ = _transcript(sha="seg-hist1")
        _save(recording, transcript, 2, 8, [4], ["Intro", "Main"])
        content = client.get(f"/recordings/{recording.pk}/history/").content.decode()
        assert "Trim &amp; split revisions" in content
        rows = _history_rows(content)
        assert len(rows) == 2  # header + 1 data row
        assert "#1" in rows[1]
        assert "segments 2–7" in rows[1]
        assert "2" in rows[1]  # topic count annotation
        # Titles are NEVER dumped into History.
        assert "Intro" not in content
        assert "Main" not in content
        # Parent-scoped read-only link: v + layout.
        version = SegmentedVersion.objects.get(transcript=transcript, is_active=True)
        assert (
            f'href="/recordings/{recording.pk}/transcript/?v={transcript.pk}&amp;layout={version.pk}"'
            in rows[1]
        )

    def test_newest_first_across_transcripts(self, client):
        recording, transcript, _ = _transcript(sha="seg-hist-new")
        _save(recording, transcript, 1, 4, [], [])
        # A second revision supersedes the first.
        _save(recording, transcript, 2, 6, [], [])
        content = client.get(f"/recordings/{recording.pk}/history/").content.decode()
        rows = _history_rows(content)
        assert len(rows) == 2 + 1  # header + 2 rows
        # Newest (revision 2, active) first; revision 1 superseded second.
        assert ">#2</a>" in rows[1]
        assert "superseded" not in rows[1]
        assert ">#1</a>" in rows[2]
        assert "superseded" in rows[2]

    def test_history_bounded_with_truncation_notice(self, client):
        from django.utils import timezone as tz

        recording, transcript, _ = _transcript(sha="seg-hist-flood")
        now = tz.now()
        for i in range(1, 106):
            SegmentedVersion.objects.create(
                transcript=transcript,
                revision=i,
                start_segment_ordinal=0,
                end_segment_ordinal_exclusive=10,
                is_active=(i == 105),
                activated_at=now,
                superseded_at=None if i == 105 else now,
                created_at=now,
            )
        content = client.get(f"/recordings/{recording.pk}/history/").content.decode()
        rows = _history_rows(content)
        # Header + exactly 100 data rows (the oldest 5 are gone).
        assert len(rows) == 101
        assert "Only the most recent 100 trim &amp; split revisions are shown." in content
        assert ">#105</a>" in rows[1]
        assert ">#6</a>" in rows[100]
        assert ">#5</a>" not in content

    def test_history_query_uses_one_annotation_no_n_plus_one(self, client):
        recording, transcript, _ = _transcript(sha="seg-hist-n1")
        for i in range(1, 11):
            _save(recording, transcript, 0, max(1, 10 - i), [], [])
        with CaptureQueriesContext(connection) as ctx:
            client.get(f"/recordings/{recording.pk}/history/")
        # Bounded: the segmented-revisions query is one annotated SELECT
        # (topic counts never trigger per-row queries).
        segmented_sql = [
            q["sql"]
            for q in ctx.captured_queries
            if "workflow_segmentedversion" in q["sql"]
            and "COUNT" in q["sql"].upper()
        ]
        assert len(segmented_sql) == 1

    def test_no_segmented_versions_shows_empty_state(self, client):
        recording, _t, _s = _transcript(sha="seg-hist-empty")
        content = client.get(f"/recordings/{recording.pk}/history/").content.decode()
        assert "No trim &amp; split revisions yet." in content
        assert _history_rows(content) == []


# ---------------------------------------------------------------------------
# Rendered-page verification of the editor JS's data contracts: the
# scissors-placement rule applied to the ACTUAL rendered page ordinals and
# the exact-range topic-title mapping the JS rebuilds on every staged edit.
# The app.js source-strings for both rules are asserted separately below.
# ---------------------------------------------------------------------------


class TestPageBoundaryScissors:
    """The JS creates one scissors per rendered segment ordinal o with
    ``0 < o < SEGMENT_COUNT`` (the full transcript count from the server
    metadata). Applied to the rendered pages this means: the divider before
    a page's FIRST segment is present whenever the boundary is interior
    (page > 1), while the transcript's absolute start (0) and absolute end
    (SEGMENT_COUNT) never get a scissors."""

    @staticmethod
    def _scissors_boundaries(content: str, segment_count: int) -> list[int]:
        ordinals = [int(o) for o in re.findall(r'data-ordinal="(\d+)"', content)]
        # Mirror of app.js: ``if (o <= 0 || o >= SEGMENT_COUNT) return;``
        return [o for o in ordinals if o > 0 and o < segment_count]

    def test_page1_has_scissors_after_first_and_never_at_start(self, client):
        recording, _t, _s = make_transcribed_recording(
            [f"segment {i}" for i in range(450)], sha="seg-scissors-p1"
        )
        content = client.get(f"/recordings/{recording.pk}/transcript/").content.decode()
        boundaries = self._scissors_boundaries(content, 450)
        assert boundaries[0] == 1
        assert 0 not in boundaries
        assert boundaries[-1] == 199

    def test_page2_scissors_before_first_segment_interpage_boundary(self, client):
        """The divider BETWEEN pages (before the page's first segment) gets
        a scissors on the later page: ordinal 200 is interior to the whole
        transcript (0 < 200 < 450), so the boundary before segment 200 is
        editable from page 2 even though no scissors sits at the end of
        page 1."""
        recording, _t, _s = make_transcribed_recording(
            [f"segment {i}" for i in range(450)], sha="seg-scissors-p2"
        )
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?page=2"
        ).content.decode()
        boundaries = self._scissors_boundaries(content, 450)
        assert boundaries[0] == 200  # inter-page boundary before segment 200
        assert boundaries[-1] == 399

    def test_last_page_never_has_transcript_end_scissors(self, client):
        recording, _t, _s = make_transcribed_recording(
            [f"segment {i}" for i in range(450)], sha="seg-scissors-last"
        )
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?page=3"
        ).content.decode()
        boundaries = self._scissors_boundaries(content, 450)
        assert 450 not in boundaries  # absolute transcript end is omitted
        assert boundaries[-1] == 449

    def test_saved_crop_endpoints_never_receive_scissors(self, client):
        """With a saved crop [2, 8) on a 10-segment transcript the JS hides
        every scissors at or outside the staged endpoints (b <= start or
        b >= end): boundaries 2 and 8 are never actionable."""
        recording, transcript, _ = _transcript(sha="seg-scissors-crop")
        _save(recording, transcript, 2, 8, [], [])
        content = client.get(f"/recordings/{recording.pk}/transcript/").content.decode()
        boundaries = self._scissors_boundaries(content, 10)
        assert boundaries == [1, 2, 3, 4, 5, 6, 7, 8, 9]
        staged = _editor_state(content)
        visible = [
            b for b in boundaries
            if not (b <= staged["start"] or b >= staged["end"])
        ]
        assert visible == [3, 4, 5, 6, 7]


class TestExactTitleMappingContract:
    """The JS preserves a topic title ONLY by an exact canonical
    [start,end) range match, independent of index; changed/new ranges
    start blank. These tests execute the same bounded algorithm the JS
    source implements (see the source-string test below) over the exact
    staged-edit scenarios the editor produces."""

    @staticmethod
    def _derive_sections(start, end, splits):
        interior = sorted(s for s in splits if start < s < end)
        if not interior:
            return []
        bounds = [start] + interior + [end]
        return [
            {"start": bounds[i], "end": bounds[i + 1]}
            for i in range(len(bounds) - 1)
        ]

    @staticmethod
    def _rebuild_titles(staged, prev_sections, prev_titles):
        sections = TestExactTitleMappingContract._derive_sections(
            staged["start"], staged["end"], staged["splits"]
        )
        by_range = {
            f"{sec['start']}:{sec['end']}": prev_titles[i] or ""
            for i, sec in enumerate(prev_sections)
        }
        return [by_range.get(f"{sec['start']}:{sec['end']}", "") for sec in sections]

    def _assert_scenario(self, staged, prev_sections, prev_titles, expected):
        titles = self._rebuild_titles(staged, prev_sections, prev_titles)
        assert titles == expected, titles

    def test_inserting_a_split_blanks_both_new_ranges_only(self):
        staged = {"start": 0, "end": 10, "splits": [4, 6], "titles": ["A", "B", "C"]}
        prev = self._derive_sections(0, 10, [4, 6])
        staged["splits"] = [4, 5, 6]
        # [0,4) and [6,10) are unchanged and keep their titles; the split
        # changed [4,6) into [4,5)+[5,6), both blank.
        self._assert_scenario(staged, prev, ["A", "B", "C"], ["A", "", "", "C"])

    def test_split_before_later_section_keeps_it_by_range_not_index(self):
        """A split inserted BEFORE an unchanged later section must not
        displace its title: [4,7) keeps 'B' and [7,10) keeps 'C' even
        though their indices shift."""
        staged = {"start": 0, "end": 10, "splits": [4, 7], "titles": ["A", "B", "C"]}
        prev = self._derive_sections(0, 10, [4, 7])
        staged["splits"] = [2, 4, 7]
        self._assert_scenario(staged, prev, ["A", "B", "C"], ["", "", "B", "C"])

    def test_removing_a_split_blanks_the_merged_section(self):
        staged = {"start": 0, "end": 10, "splits": [4, 6], "titles": ["A", "B", "C"]}
        prev = self._derive_sections(0, 10, [4, 6])
        staged["splits"] = [6]
        # The merged [0,6) no longer exactly matches [0,4)/[4,6); [6,10)
        # is unchanged and keeps its title.
        self._assert_scenario(staged, prev, ["A", "B", "C"], ["", "C"])

    def test_crop_from_blanks_changed_prefix_only(self):
        staged = {"start": 0, "end": 10, "splits": [4], "titles": ["A", "B"]}
        prev = self._derive_sections(0, 10, [4])
        staged["start"] = 2
        # [4,10) is unchanged (keeps B); [2,4) is a changed range (blank).
        self._assert_scenario(staged, prev, ["A", "B"], ["", "B"])

    def test_crop_to_blanks_changed_suffix_only(self):
        staged = {"start": 0, "end": 10, "splits": [4], "titles": ["A", "B"]}
        prev = self._derive_sections(0, 10, [4])
        staged["end"] = 6
        # [0,4) unchanged (keeps A); [4,6) is changed (blank).
        self._assert_scenario(staged, prev, ["A", "B"], ["A", ""])


class TestPageBoundaryTopicInputs:
    """Behavioral mirror of the editor's edit-mode input-placement rule for
    the page-start boundary: a split or crop-to exactly at the first
    segment of page >1 changes BOTH adjacent sections, so BOTH topic inputs
    (N+1 for N splits) must be exposed — the preceding section's context
    input directly before the current section's input, at most one."""

    @staticmethod
    def _derive_sections(start, end, splits):
        interior = sorted(s for s in splits if start < s < end)
        if not interior:
            return []
        bounds = [start] + interior + [end]
        return [
            {"start": bounds[i], "end": bounds[i + 1]}
            for i in range(len(bounds) - 1)
        ]

    @classmethod
    def _render_topic_inputs(cls, page_ordinals, staged):
        """Mirror of the JS renderTranscriptFlow input placement: returns
        the ordered list of exposed topic inputs as
        ``(index, start, end)`` in DOM order."""
        sections = cls._derive_sections(staged["start"], staged["end"], staged["splits"])
        if not sections:
            return []
        # Segment visibility while editing: el.hidden = (o < start || o >= end).
        visible = [o for o in page_ordinals if staged["start"] <= o < staged["end"]]
        first_visible = visible[0] if visible else None
        inputs = []
        for idx, sec in enumerate(sections):
            anchor = next((o for o in page_ordinals if o == sec["start"]), None)
            if anchor is not None:
                inputs.append((idx, sec["start"], sec["end"]))
                if (
                    idx > 0
                    and sec["start"] == page_ordinals[0]
                    and sections[idx - 1]["end"] == page_ordinals[0]
                ):
                    # Adjacent preceding section ended at the page's first
                    # ordinal: insert its context input BEFORE the current.
                    prev = sections[idx - 1]
                    inputs.insert(len(inputs) - 1, (idx - 1, prev["start"], prev["end"]))
            elif first_visible is not None and sec["start"] <= first_visible < sec["end"]:
                inputs.append((idx, sec["start"], sec["end"]))
        return inputs

    def test_split_at_page_start_exposes_both_inputs(self):
        """First split at the first segment of page 2 (boundary 200):
        sections [0,200) and [200,450) — BOTH topic inputs are exposed,
        the preceding section's context input before the current one."""
        page = list(range(200, 400))  # page 2 of a 450-segment transcript
        staged = {"start": 0, "end": 450, "splits": [200]}
        inputs = self._render_topic_inputs(page, staged)
        assert inputs == [(0, 0, 200), (1, 200, 450)], inputs

    def test_crop_to_at_page_start_exposes_both_inputs(self):
        """Crop-to at the page-start boundary (range [0,300) with a split at
        200): the retained section ending at 200 is changed and must be
        nameable alongside the section starting at 200."""
        page = list(range(200, 400))
        staged = {"start": 0, "end": 300, "splits": [200]}
        inputs = self._render_topic_inputs(page, staged)
        assert inputs == [(0, 0, 200), (1, 200, 300)], inputs

    def test_at_most_one_adjacent_context_input(self):
        """A section that ends BEFORE the page's first ordinal is not
        adjacent and gets no context input: with splits [100,300] on page 2
        only the spanning [100,300) context input and the on-page [300,450)
        input are exposed (never the earlier [0,100) section)."""
        page = list(range(200, 400))
        staged = {"start": 0, "end": 450, "splits": [100, 300]}
        inputs = self._render_topic_inputs(page, staged)
        assert inputs == [(1, 100, 300), (2, 300, 450)], inputs

    def test_normal_on_page_split_is_unaffected(self):
        """A split on a page whose first ordinal is 0 never triggers the
        adjacent-preceding rule; both inputs anchor at their section starts."""
        page = list(range(0, 10))
        staged = {"start": 0, "end": 10, "splits": [4]}
        inputs = self._render_topic_inputs(page, staged)
        assert inputs == [(0, 0, 4), (1, 4, 10)], inputs

    def test_cropped_page_start_ordinal_is_ignored(self):
        """When the page's first ordinal is CROPPED (outside the staged
        range) no section starts there and the adjacent rule never fires;
        inputs anchor at their visible section starts."""
        page = list(range(200, 400))
        staged = {"start": 250, "end": 450, "splits": [300]}
        inputs = self._render_topic_inputs(page, staged)
        assert inputs == [(0, 250, 300), (1, 300, 450)], inputs


# ---------------------------------------------------------------------------
# Static JS editor contract (no browser JS harness exists in the project;
# the rendering-level "external-only JS / no inline handlers" guarantees
# are covered by the security tests). Mirrors the small app.js contract
# tests in test_web_detail.py.
# ---------------------------------------------------------------------------


class TestStaticEditorJsContract:
    """The Step 6.1 editor JS is external, CSP-safe, and never persists
    staged layout data. It reads the server json_script via textContent
    (never innerHTML), drives visibility through the hidden attribute,
    opens the boundary action dialog only from a scissors click, keeps
    focus/Escape in the dialog, and submits the staged payload through
    the native hidden-input form POST (never fetch/XHR)."""

    def _editor_source(self) -> str:
        from pathlib import Path

        from django.contrib.staticfiles import finders

        path = finders.find("workflow/app.js")
        assert path is not None
        return Path(path).read_text(encoding="utf-8")

    def test_editor_init_is_wired_and_metadata_comes_from_json_script(self):
        source = self._editor_source()
        assert "initSegmentationControls" in source
        # The editor only activates when the bounded json_script block
        # exists on the page (editable active transcript).
        assert 'document.getElementById("segmentation-editor-state")' in source
        # The staged metadata is parsed from the script's text content —
        # never innerHTML, never a second network/DB round trip.
        assert "JSON.parse(stateEl.textContent)" in source

    def test_no_browser_persistence_and_no_inner_html(self):
        source = self._editor_source()
        # No drafts are persisted anywhere (task: no browser persistence).
        assert "localStorage" not in source
        assert "sessionStorage" not in source
        # All values are written via DOM text/value properties.
        assert ".innerHTML" not in source
        assert "document.write" not in source

    def test_scissors_and_visibility_contract(self):
        source = self._editor_source()
        # Scissors are one compact button per inter-segment divider line,
        # created in JS, hidden until editing (hidden attribute driven).
        assert 'btn.className = "boundary-scissors"' in source
        assert 'btn.setAttribute("data-boundary", String(boundary))' in source
        assert 'btn.setAttribute("aria-haspopup", "dialog")' in source
        assert 'btn.hidden = true' in source
        # Visible only while editing AND strictly inside the staged crop
        # (no action is valid at the crop endpoints).
        assert "btn.hidden = !editMode || b <= staged.start || b >= staged.end" in source
        # Cropped rows are hidden (never merely dimmed) in both views.
        assert 'el.hidden = showFull ? false : (o < range.start || o >= range.end)' in source

    def test_page_boundary_scissors_js_contract(self):
        """A scissors is created before the page's FIRST segment whenever
        that boundary is interior to the WHOLE transcript (ordinal > 0 and
        < total segment count), so a pagination boundary before page >1
        always has a scissors — while the transcript's absolute start/end
        stay omitted."""
        source = self._editor_source()
        editor = source[source.index("function initSegmentationControls()"):]
        assert "if (o <= 0 || o >= SEGMENT_COUNT) return;" in editor
        assert "makeScissors(o, el)" in editor
        assert "container.insertBefore(sc, el)" in editor
        # SEGMENT_COUNT is the FULL transcript count from the server
        # metadata (never the page-local last ordinal).
        assert "SEGMENT_COUNT = initialState.segment_count;" in editor

    def test_rebuild_titles_exact_range_contract(self):
        """A topic title AND its temporary-title flag are preserved ONLY
        by an exact canonical [start,end) range match, independent of
        index — a split inserted before an unchanged later section never
        drops its title/flag, and there is no left-prefix heuristic/
        inference. A brand-new range is visibly PREFILLED with the
        server-authoritative title for its canonical ordinal (picked from
        the bounded server-rendered list, never derived from the browser
        clock); a carried-over temporary section whose ordinal changed
        regenerates its server title instead of retaining a mismatched
        "Segment N"."""
        source = self._editor_source()
        editor = source[
            source.index("function rebuildTitles(prevSections, prevTitles, prevFlags)")
            :
        ]
        assert "byRange[sec.start + \":\" + sec.end]" in editor
        assert "flagByRange[sec.start + \":\" + sec.end]" in editor
        assert "ordinalByRange[sec.start + \":\" + sec.end]" in editor
        assert "titles.push(byRange[key])" in editor
        # New ranges: server-prefilled title + True temporary flag.
        assert "titles.push(serverTemporaryTitle(ordinal))" in editor
        assert "flags.push(true)" in editor
        # Ordinal-change regeneration for carried temporary sections.
        assert "titles[titles.length - 1] = serverTemporaryTitle(ordinal)" in editor
        # The server list is the source (never the browser clock).
        editor_full = source[source.index("function initSegmentationControls()"):]
        assert "initialState.temporary_titles" in editor_full
        assert "Date" not in editor_full.split("serverTemporaryTitle")[0]
        # No index-based carry-over and no prefix heuristic.
        assert "prevSections[i]" not in editor
        assert "sec.end <= prev.end" not in editor

    def test_page_boundary_exposes_both_topic_inputs_js_contract(self):
        """A split or crop-to exactly at the page-start boundary (the first
        segment of page >1) changes BOTH adjacent sections: the editor must
        expose the preceding section's context input (its end equals the
        first visible ordinal) directly BEFORE the current section's input,
        at most once."""
        source = self._editor_source()
        editor = source[source.index("function initSegmentationControls()"):]
        # The current section input is captured so the adjacent preceding
        # context input can be inserted directly before it.
        assert "var currentInput = insertTopicInput(anchor, idx, sec);" in editor
        # The adjacent-preceding rule fires only when the previous section
        # ends exactly at the page's first ordinal.
        assert "sec.start === ordinals[0]" in editor
        assert "sections[idx - 1].end === ordinals[0]" in editor
        assert "insertTopicInput(anchor, idx - 1, sections[idx - 1], currentInput)" in editor
        # insertTopicInput accepts the optional before-element and returns
        # the row so the adjacent input lands BEFORE the current one.
        assert "function insertTopicInput(anchor, index, sec, beforeEl)" in editor
        assert "beforeEl.parentNode.insertBefore(row, beforeEl)" in editor
        assert "return row;" in editor
        # The containing-section rule is unchanged (one context input when
        # a section started on an earlier page and spans the first visible
        # working row).
        assert "sec.start <= firstVisible && firstVisible < sec.end" in editor

    def test_editor_hides_server_headings_and_restores(self):
        """Edit mode hides the server-rendered topic headings (the inline
        inputs replace them); exit/reset restores them. Read-only pages
        never enter edit mode, so their headings stay visible."""
        source = self._editor_source()
        editor = source[source.index("function initSegmentationControls()"):]
        assert 'container.querySelectorAll(".topic-heading")' in editor
        assert "el.hidden = editMode;" in editor

    def test_dialog_opens_only_from_scissors_and_is_accessible(self):
        source = self._editor_source()
        # The dialog is opened ONLY by a scissors click, never by the
        # edit toggle itself.
        assert "if (editMode) openBoundaryDialog(boundary, btn)" in source
        # Accessible dialog: role/aria-modal + focus first enabled + Escape
        # + Tab containment + focus restored to the trigger on close.
        assert '"role", "dialog"' in source
        assert '"aria-modal", "true"' in source
        assert "focusFirstEnabled()" in source
        assert 'event.key === "Escape"' in source
        assert 'event.key !== "Tab"' in source
        assert "dialogTrigger.focus()" in source
        # Only valid actions are enabled/visible per boundary.
        assert "splitBtn.disabled = !canSplit(b)" in source
        assert "removeBtn.hidden = !canRemoveSplit(b)" in source

    def test_save_submits_native_form_post(self):
        source = self._editor_source()
        editor = source[source.index("function initSegmentationControls()"):]
        # Save writes the bounded staged metadata into hidden inputs and
        # submits the server-rendered form (the server renders the
        # confirmation BEFORE any mutation; no fetch/XHR anywhere in the
        # editor).
        assert "saveForm.appendChild(input)" in editor
        assert "saveForm.submit()" in editor
        assert "fetch(" not in editor
        assert "XMLHttpRequest" not in editor
        # The dirty-state leave warning is a browser-owned dialog and a
        # real submit suppresses it; nothing is ever persisted.
        assert (
            'saveForm.addEventListener("submit", function () { allowLeave = true; })'
            in editor
        )
        assert "beforeunload" in editor


class TestSectionReturnBreadcrumb:
    """Objective B: the transcript breadcrumb labels ``← Section`` when a
    validated ``return_section`` (a readable canonical topic Section of
    this recording) is supplied; the separately validated ``lib_return``
    token rides the back link and pagination. Anything invalid keeps the
    plain ``← Recording overview`` and is never echoed. Existing
    ``v``/``layout`` identity is untouched."""

    def _split(self, sha="seg-sec-return", count=10, splits=(3,), titles=("A", "B")):
        recording, transcript, _ = _transcript(count=count, sha=sha)
        _save(recording, transcript, 0, count, list(splits), list(titles))
        sections = list(
            Section.objects.filter(transcript=transcript)
            .exclude(segmented_version__isnull=True)
            .order_by("ordinal")
        )
        return recording, transcript, sections

    def _token(self):
        from workflow.query import ListFilters
        from workflow.services import library_return

        return library_return.make_token(ListFilters(), 1, "cards")

    def test_section_origin_breadcrumb(self, client):
        recording, _transcript, sections = self._split()
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?return_section={sections[0].pk}"
        ).content.decode()
        assert (
            f'<a href="/recordings/{recording.pk}/sections/{sections[0].pk}/">'
            "&larr; Section</a>" in content
        )
        assert "&larr; Recording overview" not in content

    def test_section_origin_breadcrumb_preserves_valid_lib_return(self, client):
        recording, _transcript, sections = self._split(sha="seg-sec-return-tok")
        token = self._token()
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?return_section={sections[0].pk}"
            f"&lib_return={token}"
        ).content.decode()
        assert (
            f'<a href="/recordings/{recording.pk}/sections/{sections[0].pk}/?lib_return={token}">'
            "&larr; Section</a>" in content
        )
        assert "&larr; Recording overview" not in content

    def test_invalid_lib_return_dropped_section_kept(self, client):
        recording, _transcript, sections = self._split(sha="seg-sec-return-badtok")
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?return_section={sections[0].pk}"
            "&lib_return=forged"
        ).content.decode()
        assert (
            f'<a href="/recordings/{recording.pk}/sections/{sections[0].pk}/">'
            "&larr; Section</a>" in content
        )
        assert "lib_return=" not in content

    def test_malformed_return_section_falls_back_to_parent(self, client):
        recording, _transcript, sections = self._split(sha="seg-sec-return-bad")
        for bad in (
            "abc",
            "-3",
            "2.5",
            "0",
            "00",
            "01",
            f"0{sections[0].pk}",  # leading-zero form of a REAL Section pk
            "9223372036854775808",  # 2**63: one past max signed 64-bit
            "18446744073709551616",  # 2**64
            "9" * 100,  # far beyond the BigAutoField digit/length cap
            "99999999999999999999",
            "",
            "٢",
        ):
            response = client.get(
                f"/recordings/{recording.pk}/transcript/?return_section={bad}"
            )
            assert response.status_code == 200, bad
            content = response.content.decode()
            assert "&larr; Recording overview" in content, bad
            assert "&larr; Section" not in content, bad
            assert "return_section" not in content, bad

    def test_noncanonical_return_section_drops_valid_lib_return(self, client):
        """An invalid/oversized ``return_section`` rejects the WHOLE
        Section-origin return: even a valid ``lib_return`` token is never
        echoed (breadcrumb, pagination or links)."""
        recording, _transcript, sections = self._split(
            sha="seg-sec-return-drop-token", count=410
        )
        token = self._token()
        for bad in (
            f"0{sections[0].pk}",  # leading-zero form of a REAL Section pk
            "9223372036854775808",  # one past max signed 64-bit
            "9" * 100,
        ):
            response = client.get(
                f"/recordings/{recording.pk}/transcript/?return_section={bad}"
                f"&lib_return={token}&page=1"
            )
            assert response.status_code == 200, bad
            content = response.content.decode()
            assert "&larr; Recording overview" in content, bad
            assert "&larr; Section" not in content, bad
            assert "return_section" not in content, bad
            assert "lib_return" not in content, bad
            assert token not in content, bad

    def test_cross_recording_return_section_falls_back_to_parent(self, client):
        recording, _transcript, _sections = self._split(sha="seg-sec-return-cross-a")
        _other, _t_other, sections_b = self._split(sha="seg-sec-return-cross-b")
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?return_section={sections_b[0].pk}"
        ).content.decode()
        assert "&larr; Recording overview" in content
        assert "&larr; Section" not in content
        assert "return_section" not in content

    def test_fixed_section_return_falls_back_to_parent(self, client):
        recording, _tx, fixed = _transcript(count=4, sha="seg-sec-return-fixed")
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?return_section={fixed.pk}"
        ).content.decode()
        assert "&larr; Recording overview" in content
        assert "&larr; Section" not in content

    def test_direct_recording_origin_transcript_unchanged(self, client):
        recording, _transcript, _sections = self._split(sha="seg-sec-return-plain")
        content = client.get(f"/recordings/{recording.pk}/transcript/").content.decode()
        assert "&larr; Recording overview" in content
        assert "&larr; Section" not in content
        assert "return_section" not in content
        assert "lib_return=" not in content

    def test_historical_layout_keeps_layout_identity_and_back_link(self, client):
        recording, transcript, _sections = self._split(sha="seg-sec-return-hist")
        section = Section.objects.filter(transcript=transcript).exclude(
            segmented_version__isnull=True
        ).order_by("ordinal").first()
        version = section.segmented_version
        # Supersede the revision: the section becomes historical.
        _save(recording, transcript, 0, transcript.segments.count())
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?layout={version.pk}"
            f"&return_section={section.pk}"
        ).content.decode()
        # The back link points to the HISTORICAL section detail.
        assert (
            f'<a href="/recordings/{recording.pk}/sections/{section.pk}/">'
            "&larr; Section</a>" in content
        )
        # The historical revision is still identified (read-only note).
        assert "saved trim &amp; split revision" in content

    def test_historical_transcript_keeps_v_and_layout_identity(self, client):
        from workflow.models import AttemptOutcome, AttemptStage

        recording, transcript, _sections = self._split(sha="seg-sec-return-vlayout")
        section = Section.objects.filter(transcript=transcript).exclude(
            segmented_version__isnull=True
        ).order_by("ordinal").first()
        version = section.segmented_version
        # Retranscription: the transcript (and its layout) become historical.
        attempt = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS,
        )
        transcript.is_active = False
        transcript.save(update_fields=["is_active"])
        new_transcript = Transcript.objects.create(
            recording=recording, attempt=attempt, text_normalized="new"
        )
        Section.objects.create(transcript=new_transcript, ordinal=0, title="Full")
        new_transcript.is_active = True
        new_transcript.save(update_fields=["is_active"])
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?v={transcript.pk}"
            f"&layout={version.pk}&return_section={section.pk}"
        ).content.decode()
        # Existing v/layout identity unchanged + back to the historical Section.
        assert (
            f'<a href="/recordings/{recording.pk}/sections/{section.pk}/">'
            "&larr; Section</a>" in content
        )
        assert "HISTORICAL transcript version" in content
        assert "saved trim &amp; split revision" in content

    def test_pagination_preserves_validated_section_return(self, client):
        recording, _transcript, sections = self._split(
            sha="seg-sec-return-page", count=410
        )
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?return_section={sections[0].pk}"
        ).content.decode()
        assert "Page 1 of 3" in content
        assert (
            f'href="?return_section={sections[0].pk}&amp;page=2"'
            in content
        )
        # The back link keeps working after paging.
        page2 = client.get(
            f"/recordings/{recording.pk}/transcript/?page=2"
            f"&return_section={sections[0].pk}"
        ).content.decode()
        assert (
            f'<a href="/recordings/{recording.pk}/sections/{sections[0].pk}/">'
            "&larr; Section</a>" in page2
        )

    def test_pagination_preserves_lib_return_with_section(self, client):
        recording, _transcript, sections = self._split(
            sha="seg-sec-return-page-tok", count=410
        )
        token = self._token()
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?return_section={sections[0].pk}"
            f"&lib_return={token}"
        ).content.decode()
        assert (
            f'href="?return_section={sections[0].pk}&amp;lib_return={token}&amp;page=2"'
            in content
        )
        page2 = client.get(
            f"/recordings/{recording.pk}/transcript/?page=2"
            f"&return_section={sections[0].pk}&lib_return={token}"
        ).content.decode()
        assert (
            f'<a href="/recordings/{recording.pk}/sections/{sections[0].pk}/?lib_return={token}">'
            "&larr; Section</a>" in page2
        )

    def test_pagination_does_not_echo_invalid_return_params(self, client):
        recording, _transcript, _sections = self._split(
            sha="seg-sec-return-badpage", count=410
        )
        content = client.get(
            f"/recordings/{recording.pk}/transcript/?page=1"
            "&return_section=abc&lib_return=forged"
        ).content.decode()
        assert "&larr; Recording overview" in content
        assert "&larr; Section" not in content
        assert "return_section" not in content
        assert "lib_return" not in content
        # Plain pagination continues.
        assert 'href="?page=2"' in content