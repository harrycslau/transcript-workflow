"""Recording detail / summary / transcript / history pages (Step 4).

Proves:
- current summary rendered from structured fields, escaped;
- transcript content escaped; model-provided HTML never trusted;
- long transcript bounded (segments paginated per config);
- current vs historical versions correctly labelled (scope-active
  old-transcript summaries are HISTORICAL, not current);
- missing summary/transcript states render friendly empty states;
- failure/retry warnings surfaced;
- GET purity: no subprocess, no network, no writes, no hashing;
- bounded query count on detail.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.db import connection

from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    AudioStatus,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
    RoutingMethod,
    RoutingDecision,
    Summary,
    SummaryState,
    Transcript,
)

from factories import make_summary_version, make_tag, make_tag_assignment, make_transcribed_recording

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


@pytest.fixture
def client():
    return Client()


def _summary_recording():
    recording, transcript, section = make_transcribed_recording(["hello world"], sha="detail-1")
    summary = make_summary_version(recording, transcript, section)
    return recording, transcript, section, summary


class TestDetailPage:
    def test_detail_renders_structured_summary(self, client):
        recording, _t, _s, summary = _summary_recording()
        response = client.get(f"/recordings/{recording.pk}/")
        assert response.status_code == 200
        content = response.content.decode()
        assert summary.title in content
        assert summary.overview in content
        assert "Point one" in content  # key point
        assert "Do a thing" in content  # action item
        assert "Alice" in content  # person

    def test_summary_fields_escaped_not_raw_html(self, client):
        recording, transcript, section = make_transcribed_recording(["x"], sha="xss-1")
        summary = make_summary_version(
            recording, transcript, section,
            title="<script>alert(1)</script>",
            overview="Overview with <img src=x onerror=alert(2)> payload",
        )
        response = client.get(f"/recordings/{recording.pk}/")
        content = response.content.decode()
        assert "<script>alert(1)</script>" not in content
        assert "&lt;script&gt;" in content
        assert "<img src=x onerror" not in content

    def test_transcript_content_escaped(self, client):
        recording, transcript, section = make_transcribed_recording(
            ["<script>alert('transcript')</script>"], sha="xss-2"
        )
        make_summary_version(recording, transcript, section)
        response = client.get(f"/recordings/{recording.pk}/")
        content = response.content.decode()
        assert "<script>alert('transcript')</script>" not in content
        assert "&lt;script&gt;" in content

    def test_identity_and_status_fields(self, client):
        recording, _t, _s, _summary = _summary_recording()
        response = client.get(f"/recordings/{recording.pk}/")
        content = response.content.decode()
        assert recording.sha256[:12] in content
        assert "transcribed" in content
        assert "detail-1" in content  # source filename placeholder not present; sha shown

    def test_missing_summary_state_friendly(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="nosum-1")
        Recording.objects.filter(pk=recording.pk).update(summary_status=SummaryState.MISSING)
        response = client.get(f"/recordings/{recording.pk}/summary/")
        assert response.status_code == 200
        content = response.content.decode()
        assert "No summary yet" in content

    def test_failed_summary_state_friendly(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="failsum-1")
        Recording.objects.filter(pk=recording.pk).update(summary_status=SummaryState.FAILED)
        response = client.get(f"/recordings/{recording.pk}/summary/")
        content = response.content.decode()
        assert "failed" in content
        assert "Retry" in content

    def test_missing_transcript_state_friendly(self, client):
        recording = Recording.objects.create(sha256="notrans-1")
        response = client.get(f"/recordings/{recording.pk}/")
        assert response.status_code == 200
        assert "No transcript yet" in response.content.decode()

    def test_retranscription_failed_warning_surfaced(self, client):
        recording, transcript, section = make_transcribed_recording(["a"], sha="retx-1")
        make_summary_version(recording, transcript, section)
        attempt = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.NONZERO_EXIT, finished_at=None, error_code="mw_exit_1",
        )
        Recording.objects.filter(pk=recording.pk).update(
            retranscription_failed=True, last_failed_attempt=attempt
        )
        response = client.get(f"/recordings/{recording.pk}/")
        content = response.content.decode()
        assert "Retranscription failed" in content

    def test_unknown_recording_is_404(self, client):
        response = client.get("/recordings/00000000-0000-0000-0000-000000000000/")
        assert response.status_code == 404

    def test_detail_query_count_bounded(self, client):
        recording, transcript, section = make_transcribed_recording(["a"], sha="qc-1")
        make_summary_version(recording, transcript, section)
        for i in range(3):
            make_tag_assignment(recording, make_tag(f"QCTag{i}"))
        with CaptureQueriesContext(connection) as ctx:
            client.get(f"/recordings/{recording.pk}/")
        assert len(ctx.captured_queries) < 40, f"detail page issued {len(ctx.captured_queries)} queries"


class TestTranscriptPagination:
    def test_long_transcript_bounded_per_page(self, client):
        texts = [f"segment {i}" for i in range(450)]
        recording, transcript, _s = make_transcribed_recording(texts, sha="long-1")
        response = client.get(f"/recordings/{recording.pk}/transcript/")
        assert response.status_code == 200
        content = response.content.decode()
        # Default config: 200 segments per page. First page shows 0..199.
        assert "segment 0" in content
        assert "segment 199" in content
        assert "segment 200" not in content
        assert "450 segments" in content
        assert "Page 1 of 3" in content

    def test_transcript_page_2(self, client):
        texts = [f"segment {i}" for i in range(450)]
        recording, _t, _s = make_transcribed_recording(texts, sha="long-2")
        response = client.get(f"/recordings/{recording.pk}/transcript/?page=2")
        content = response.content.decode()
        assert "segment 200" in content
        assert "segment 0<" not in content

    def test_invalid_page_clamped_not_500(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="long-3")
        response = client.get(f"/recordings/{recording.pk}/transcript/?page=999")
        assert response.status_code == 200

    def test_detail_page_exactly_five_preview_segments(self, client):
        texts = [f"segment {i}" for i in range(450)]
        recording, _t, _s = make_transcribed_recording(texts, sha="long-4")
        response = client.get(f"/recordings/{recording.pk}/")
        content = response.content.decode()
        # Exactly the first 5 active-transcript segments are previewed on
        # the detail page; the transcript page owns the rest.
        for i in range(5):
            assert f"segment {i}" in content
        assert "segment 5" not in content
        assert "450 segments" in content  # accurate total + open link
        assert "Open transcript" in content

    def test_unknown_transcript_version_404(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="long-5")
        response = client.get(f"/recordings/{recording.pk}/transcript/?v=999999")
        assert response.status_code == 404


class TestSummaryVersions:
    def test_current_summary_page(self, client):
        recording, _t, _s, summary = _summary_recording()
        response = client.get(f"/recordings/{recording.pk}/summary/")
        assert response.status_code == 200
        content = response.content.decode()
        assert summary.title in content
        assert "current summary" in content

    def test_historical_summary_labelled_not_current(self, client):
        recording, transcript, section = make_transcribed_recording(["v1"], sha="hist-1")
        old_summary = make_summary_version(recording, transcript, section, title="V1 summary")
        # Create a second transcript (retranscription) + its own summary.
        attempt2 = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS, finished_at=None,
        )
        transcript2 = Transcript.objects.create(recording=recording, attempt=attempt2, text_normalized="v2")
        from workflow.models import Section

        section2 = Section.objects.create(transcript=transcript2, ordinal=0)
        from django.utils import timezone as tz

        # Retire the old transcript first, then promote the new one — the
        # partial unique constraint allows only one active transcript.
        transcript.is_active = False
        transcript.superseded_at = tz.now()
        transcript.save()
        transcript2.is_active = True
        transcript2.activated_at = tz.now()
        transcript2.save()
        new_summary = make_summary_version(recording, transcript2, section2, title="V2 summary")

        # Old summary is STILL active in its own scope — but historical.
        old_summary.refresh_from_db()
        assert old_summary.is_active is True

        response = client.get(f"/recordings/{recording.pk}/summaries/{old_summary.pk}/")
        assert response.status_code == 200
        content = response.content.decode()
        assert "HISTORICAL" in content
        assert "V1 summary" in content
        assert "not the current summary" in content
        # History table also labels it historical-for-recording.
        response = client.get(f"/recordings/{recording.pk}/history/")
        content = response.content.decode()
        assert "V1 summary" in content
        assert "historical for this recording" in content
        assert "V2 summary" in content
        assert "current" in content

    def test_cross_recording_summary_is_404(self, client):
        rec_a, _t, _s, summary_a = _summary_recording()
        rec_b, _t2, _s2 = make_transcribed_recording(["b"], sha="other-1")
        response = client.get(f"/recordings/{rec_b.pk}/summaries/{summary_a.pk}/")
        assert response.status_code == 404

    def test_history_lists_attempts_sanitized(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="hist-2")
        ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.NONZERO_EXIT, error_code="mw_exit_1",
            error_message="Transcribing x.mp3... | Error: boom", finished_at=None,
        )
        response = client.get(f"/recordings/{recording.pk}/history/")
        content = response.content.decode()
        assert "mw_exit_1" in content
        # Bounded sanitized detail is rendered (re-sanitized at the
        # rendering boundary), never raw stderr.
        assert "Transcribing x.mp3... | Error: boom" in content

    def test_history_error_detail_rendered_sanitized(self, client):
        """Rendering-boundary sanitization: unsafe content in a historical
        row (e.g. written by older versions) is sanitized for display."""
        recording, _t, _s = make_transcribed_recording(["a"], sha="hist-3")
        ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.NONZERO_EXIT, error_code="mw_exit_1",
            error_message="failed on /Users/harry/secret/inbox/file.wav", finished_at=None,
        )
        response = client.get(f"/recordings/{recording.pk}/history/")
        content = response.content.decode()
        assert "/Users/harry/secret/inbox/file.wav" not in content
        assert "&lt;path&gt;" in content  # escaped <path> replacement


class TestSummaryHeadingHierarchy:
    """The shared _summary_body partial is context-aware: Recording
    Detail embeds it in detail mode (styled title paragraph + h3
    sections under its Summary h2), while the standalone current and
    historical summary pages default to h2 sections under their own h1
    and never repeat the title paragraph."""

    def test_detail_embedded_mode_h3_under_summary_h2(self, client):
        recording, _t, _s, _summary = _summary_recording()
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert '<h2 class="overview-block-title" id="summary-title">Summary</h2>' in content
        assert '<p class="summary-title">' in content  # title as styled paragraph
        for heading in ("Overview", "Key points", "Action items", "People", "Topics"):
            assert f"<h3>{heading}</h3>" in content, heading
        assert "<h4>" not in content

    def test_standalone_current_summary_h2_no_title_repeat(self, client):
        recording, _t, _s, summary = _summary_recording()
        response = client.get(f"/recordings/{recording.pk}/summary/")
        assert response.status_code == 200
        content = response.content.decode()
        assert "<h1>" in content  # page h1 carries the title
        for heading in ("Overview", "Key points", "Action items", "People", "Topics"):
            assert f"<h2>{heading}</h2>" in content, heading
        assert "<h3>" not in content  # no embedded-mode h3 sections
        assert '<p class="summary-title">' not in content  # no title repeat
        assert summary.title in content  # title still present via the h1

    def test_historical_summary_h2_no_title_repeat(self, client):
        recording, transcript, _s = make_transcribed_recording(["v1"], sha="hier-hist")
        section = transcript.sections.get(ordinal=0)
        old = make_summary_version(recording, transcript, section, title="V1 summary")
        # Retire the transcript: the summary stays active in its own scope
        # but is historical for the recording.
        transcript.is_active = False
        transcript.save()
        response = client.get(f"/recordings/{recording.pk}/summaries/{old.pk}/")
        assert response.status_code == 200
        content = response.content.decode()
        for heading in ("Overview", "Key points", "Action items", "People", "Topics"):
            assert f"<h2>{heading}</h2>" in content, heading
        assert "<h3>" not in content
        assert '<p class="summary-title">' not in content
        assert "V1 summary" in content  # title via the page h1


class TestDetailTagEditorMarkup:
    """The compact + Add tag editor and concise wrapping tag chips
    (production integration): real server forms, client-only filter,
    retired nested disclosure, create form, and no verbose state labels."""

    def test_chips_are_compact_inline_wrapping_and_concise(self, client):
        recording, _t, _s, _summary = _summary_recording()
        manual = make_tag("Work")
        make_tag_assignment(recording, manual, origin="manual")
        suggested = make_tag("Meeting")
        make_tag_assignment(recording, suggested, origin="suggested")
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        # One wrapping chip container (flex + wrap), each chip inline.
        assert 'class="tag-chips"' in content
        assert "flex-wrap" not in content  # layout lives in external CSS only
        assert 'class="tag-chip tag-manual"' in content
        assert 'class="tag-chip tag-suggested"' in content
        # Concise visible text: plain name for manual, "(suggested)" only
        # for the suggested chip — no verbose origin/legend labels.
        assert "Work" in content
        assert "Meeting" in content
        assert '<span class="tag-chip-suggested">(suggested)</span>' in content
        assert "(manual)" not in content
        assert "(confirmed)" not in content
        assert "(assigned)" not in content
        # Confirm/Remove semantics stay intact.
        assert "/tags/{}/confirm/".format(suggested.pk) in content
        assert "/tags/{}/remove/".format(manual.pk) in content

    def test_add_editor_renders_one_bulk_form_with_checkboxes(self, client):
        recording, _t, _s, _summary = _summary_recording()
        work = make_tag("Work")
        make_tag_assignment(recording, work, origin="manual")
        retired = make_tag("OldTopic", configured=False)
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        # + Add tag enhanced details; no old select disclosure.
        assert '<details class="enhanced-details tag-editor" id="tag-editor">' in content
        assert "+ Add tag" in content
        assert 'id="id_tag_add"' not in content
        # Client-only filter input.
        assert 'id="tag-filter-input"' in content
        # ONE bulk form posting to tag-apply with repeated checkbox fields
        # and the optional new-tag input — no per-option submit buttons,
        # no separate create form, no hidden include_retired flag.
        assert 'action="/recordings/{}/tags/apply/"'.format(recording.pk) in content
        assert 'name="selected_tags"' in content
        assert 'name="selected_retired_tags"' in content
        assert 'name="new_tag_name"' in content
        assert 'name="tag"' not in content
        assert 'name="include_retired"' not in content
        assert "/tags/create/" not in content
        assert "/tags/add/" not in content
        # The already-assigned available option's checkbox is checked; the
        # retired option (no assignment) stays unchecked.
        assert 'value="{}" checked'.format(work.pk) in content
        assert 'value="{}">'.format(retired.pk) in content
        # Retired tags live in a small nested disclosure.
        assert '<details class="retired-tags">' in content
        assert "Retired tags" in content
        # One Done submit (the only mutating submit control in the modal).
        assert ">Done</button>" in content
        assert "Create and add" not in content

    def test_editor_options_show_concise_labels_only_suggested_suffixed(self, client):
        """The editor options carry the same concise visible text as the
        chips: bare names, with '(suggested)' ONLY for an actively
        suggested definition. The checkbox block's initial checked state
        equals the authoritative active assignments."""
        recording, _t, _s, _summary = _summary_recording()
        manual = make_tag("Work")
        make_tag_assignment(recording, manual, origin="manual")
        suggested = make_tag("Meeting")
        make_tag_assignment(recording, suggested, origin="suggested")
        free = make_tag("Research")
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        # Concise option text: plain names for manual/plain options.
        assert "Research" in content
        assert "Work" in content
        # Exactly one editor option is suffixed: the actively suggested
        # one (the chips use their own tag-chip-suggested class).
        assert content.count("tag-option-suggested") == 1
        assert '<span class="tag-option-suggested">(suggested)</span>' in content
        # Initial checked state == authoritative active assignments.
        assert 'value="{}" checked'.format(manual.pk) in content
        assert 'value="{}" checked'.format(suggested.pk) in content
        assert 'value="{}">'.format(free.pk) in content
        # Tapping is local: options are checkbox labels, never submit
        # controls; deselecting an assigned block just unchecks it.
        assert '<label class="tag-option" data-tag-name="Meeting">' in content
        assert 'type="checkbox"' in content
        assert 'class="tag-option-button"' not in content

    def test_detail_get_is_select_only_with_tag_editor(self, client):
        recording, _t, _s, _summary = _summary_recording()
        make_tag_assignment(recording, make_tag("SelectOnly"))
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(f"/recordings/{recording.pk}/")
        assert response.status_code == 200
        non_select = [
            q for q in ctx.captured_queries
            if not q["sql"].lstrip().upper().startswith("SELECT")
        ]
        assert non_select == []


class TestEnhancedDetailsOverlay:
    """Routing / + Add tag enhanced details: the native <details> no-JS
    fallback stays fully usable with the real forms, the first useful
    control per editor is explicitly marked for overlay focus, and the
    app.js overlay contract is browser-independent (no <dialog>/showModal)
    with cache-busted static URLs."""

    def test_tag_filter_and_route_select_marked_for_focus(self, client):
        recording, _t, _s, _summary = _summary_recording()
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        # The two "first useful control" markers: the tag filter input
        # and the routing profile select.
        assert content.count("data-modal-focus") == 2
        assert 'id="tag-filter-input"' in content
        assert 'id="id_route_profile_routing"' in content
        assert 'name="profile"' in content

    def test_app_js_custom_overlay_contract(self):
        from django.contrib.staticfiles import finders

        path = finders.find("workflow/app.js")
        assert path is not None
        source = Path(path).read_text(encoding="utf-8")
        # Custom overlay instead of native <dialog>: role=dialog panel
        # inside a fixed backdrop, shown/hidden via the hidden attribute.
        assert 'className = "modal-overlay"' in source
        assert 'className = "modal-panel"' in source
        assert 'setAttribute("role", "dialog")' in source
        assert 'setAttribute("aria-modal", "true")' in source
        assert 'setAttribute("aria-labelledby", titleId)' in source
        assert 'setAttribute("hidden", "")' in source
        assert 'removeAttribute("hidden")' in source
        # Summary click intercepts the native expansion.
        assert 'addEventListener("click"' in source
        assert "event.preventDefault()" in source
        # No native dialog dependency; never innerHTML from data.
        assert "showModal" not in source
        assert 'createElement("dialog")' not in source
        assert ".innerHTML" not in source
        # Closing: Done (routing, no commit control), Escape, backdrop;
        # focus restored to the trigger. A panel with its own
        # [data-modal-commit] Done submit gets Cancel instead.
        assert 'closeButton.textContent = commitControl ? "Cancel" : "Done";' in source
        assert 'event.key === "Escape"' in source
        assert 'event.target === overlay' in source
        assert 'trigger.focus()' in source
        # Tab containment.
        assert 'event.key !== "Tab"' in source

    def test_app_js_bulk_modal_contract(self):
        """The tag modal stages changes locally: NO sessionStorage reopen
        marker exists any more (nothing submits before Done); a panel with
        its own [data-modal-commit] Done submit gets a Cancel close button
        instead of a second generated Done; (re)opening resets the staged
        checkbox/text state to the server-rendered initial values."""
        from django.contrib.staticfiles import finders

        path = finders.find("workflow/app.js")
        assert path is not None
        source = Path(path).read_text(encoding="utf-8")
        # Reopen-marker behaviour is entirely removed.
        assert "sessionStorage" not in source
        assert "TAG_REOPEN_KEY" not in source
        assert "consumeTagReopenMarker" not in source
        # Commit panels: the generated close control is Cancel (routing,
        # which has no commit control, keeps Done).
        assert 'panel.querySelector("[data-modal-commit]")' in source
        assert 'closeButton.textContent = commitControl ? "Cancel" : "Done";' in source
        # Staged-state reset on (re)open: form.reset() restores the
        # server-rendered checkbox defaults, the filter is cleared, and
        # filtered options are un-hidden.
        assert "resetStagedState" in source
        assert "form.reset()" in source
        assert '.tag-filter-input' in source
        assert "option.hidden = false" in source
        # The bulk form still submits normally (no AJAX/interception).
        assert 'data-modal-commit' in source
        # Never build DOM content from user/config values with innerHTML.
        assert ".innerHTML" not in source

    def test_routing_summary_matches_sibling_detail_links(self):
        """The Routing <summary> reads like the Transcript/History anchors
        (same colour, size/weight, underline, line-height and focus ring)
        and keeps the native disclosure marker hidden — never button
        chrome."""
        from django.contrib.staticfiles import finders

        path = finders.find("workflow/base.css")
        assert path is not None
        css = Path(path).read_text(encoding="utf-8")
        rule = css[css.index(".routing-editor summary.detail-link {") : css.index(
            ".routing-editor summary.detail-link::-webkit-details-marker"
        )]
        assert "font-size: 0.8125rem" in rule
        assert "font-weight: 500" in rule
        assert "line-height: 1.5" in rule
        assert "color: var(--color-accent)" in rule
        assert "text-decoration: underline" in rule
        # Native disclosure marker stays hidden.
        assert ".routing-editor summary.detail-link::-webkit-details-marker { display: none; }" in css
        assert ".routing-editor summary.detail-link::marker { content: none; }" in css
        # Focus ring matches the global anchor focus ring.
        assert ".routing-editor summary.detail-link:focus-visible {" in css
        assert "outline: 2px solid var(--accent)" in css
        # Hover keeps the link colour (sibling anchors have no distinct
        # hover state) — never button chrome.
        assert ".routing-editor summary.detail-link:hover { color: var(--color-accent); }" in css
        # No button chrome on the summary.
        assert "border:" not in rule
        assert "background:" not in rule
        assert "border-radius:" not in rule

    def test_versioned_static_urls_on_detail(self, client):
        recording, _t, _s, _summary = _summary_recording()
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'href="/static/workflow/base.css?v=3"' in content
        assert 'src="/static/workflow/app.js?v=3" defer' in content


class TestBulkTagEditorMarkup:
    """The + Add tag modal (production UX correction): ONE bulk form with
    checkbox blocks posting only to tag-apply, initial checked state =
    authoritative active assignments, local-only tapping (no per-option
    submits), a single Done submit, no-JS native fallback, no
    sessionStorage marker, asset v3, and the routing-overflow CSS rules."""

    def _editor(self, client, recording):
        body = client.get(f"/recordings/{recording.pk}/").content.decode()
        start = body.index('id="tag-editor"')
        return body[start : body.index("</section>", start)]

    def test_one_bulk_form_only_done_submit(self, client):
        recording, _t, _s, _summary = _summary_recording()
        editor = self._editor(client, recording)
        # Exactly ONE form, posting to tag-apply.
        assert editor.count("<form") == 1
        assert 'action="/recordings/{}/tags/apply/"'.format(recording.pk) in editor
        # The ONLY mutating submit control is the Done button; there are no
        # per-option submit buttons and no separate add/create forms.
        assert editor.count('type="submit"') == 1
        assert ">Done</button>" in editor
        assert "Create and add" not in editor
        assert 'name="tag"' not in editor
        assert 'class="tag-option-button"' not in editor
        assert "/tags/add/" not in editor
        assert "/tags/create/" not in editor

    def test_initial_checked_state_equals_authoritative_active_set(self, client):
        recording, _t, _s, _summary = _summary_recording()
        manual = make_tag("Work")
        make_tag_assignment(recording, manual, origin="manual")
        suggested = make_tag("Meeting")
        make_tag_assignment(recording, suggested, origin="suggested")
        free = make_tag("Research")
        retired_active = make_tag("OldTopic", configured=False)
        make_tag_assignment(recording, retired_active, origin="confirmed")
        editor = self._editor(client, recording)
        for pk in (manual.pk, suggested.pk, retired_active.pk):
            assert 'value="{}" checked'.format(pk) in editor, pk
        assert 'value="{}">'.format(free.pk) in editor
        # Suggested visible label only on the suggested option.
        assert "Meeting" in editor
        assert '<span class="tag-option-suggested">(suggested)</span>' in editor
        assert "(manual)" not in editor
        assert "(confirmed)" not in editor

    def test_tapping_is_local_checkbox_toggle(self, client):
        recording, _t, _s, _summary = _summary_recording()
        assigned = make_tag("Assigned")
        make_tag_assignment(recording, assigned, origin="manual")
        editor = self._editor(client, recording)
        # Compact inline wrapping blocks: label wrapping a real checkbox
        # and a sibling span — tapping toggles the checkbox locally, and
        # tapping a selected block again deselects it (no submission).
        assert '<label class="tag-option" data-tag-name="Assigned">' in editor
        assert '<input type="checkbox" name="selected_tags" value="{}" checked>'.format(assigned.pk) in editor
        assert '<span class="tag-option-label">Assigned</span>' in editor
        # No submit is wired to the individual option.
        assert "tag-option-button" not in editor

    def test_retired_options_are_explicit_nested_opt_in(self, client):
        recording, _t, _s, _summary = _summary_recording()
        retired = make_tag("OldTopic", configured=False)
        editor = self._editor(client, recording)
        assert '<details class="retired-tags">' in editor
        assert "Retired tags" in editor
        assert 'name="selected_retired_tags"' in editor
        assert 'value="{}">'.format(retired.pk) in editor

    def test_new_tag_input_is_inside_the_same_form(self, client):
        recording, _t, _s, _summary = _summary_recording()
        editor = self._editor(client, recording)
        # The optional new-tag text input lives inside the ONE bulk form
        # and is committed only by Done.
        assert 'id="id_new_tag_name"' in editor
        assert 'name="new_tag_name"' in editor
        assert 'maxlength="64"' in editor
        assert editor.index("tag-selection-form") < editor.index("id_new_tag_name")
        assert editor.index("id_new_tag_name") < editor.index(">Done</button>")

    def test_no_js_fallback_is_usable(self, client):
        """Without JS the enhanced-details remains a plain native
        disclosure whose real checkbox form, text input and Done submit
        work as an ordinary POST form."""
        recording, _t, _s, _summary = _summary_recording()
        make_tag("Fallback")
        editor = self._editor(client, recording)
        assert 'method="post"' in editor
        assert 'name="csrfmiddlewaretoken"' in editor
        assert 'type="checkbox"' in editor
        assert 'type="text"' in editor
        assert 'type="submit"' in editor
        assert ">Done</button>" in editor

    def test_detail_page_has_no_reopen_marker_data(self, client):
        recording, _t, _s, _summary = _summary_recording()
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert "sessionStorage" not in content
        assert "data-reopen" not in content

    def test_routing_overflow_css_rules(self):
        from django.contrib.staticfiles import finders

        path = finders.find("workflow/base.css")
        assert path is not None
        css = Path(path).read_text(encoding="utf-8")
        # .modal-panel / .routing-panel: border-box + min-width 0 so a
        # long route option can never force horizontal overflow.
        for selector in (".modal-panel {", ".routing-panel {"):
            block = css[css.index(selector) : css.index("}", css.index(selector))]
            assert "box-sizing: border-box" in block, selector
            assert "min-width: 0" in block, selector
        # .route-form stacks cleanly and its select shrinks.
        form_block = css[css.index(".route-form {") : css.index("}", css.index(".route-form {"))]
        assert "box-sizing: border-box" in form_block
        assert "min-width: 0" in form_block
        assert "width: 100%" in form_block
        assert "flex-direction: column" in form_block
        select_block = css[css.index(".route-form select {") : css.index("}", css.index(".route-form select {"))]
        assert "max-width: 100%" in select_block
        assert "width: 100%" in select_block
        assert "min-width: 0" in select_block
        # Checkbox-block selection styling: `input:checked + span` (no
        # per-row layout, no :has dependency).
        assert "input:checked + .tag-option-label" in css
        assert "input:focus-visible + .tag-option-label" in css


class TestStatusPanel:
    """The single composite status/next-action panel covers the real
    state matrix: healthy, failed/retry, retranscription-failed,
    ready-to-transcribe, needs-review, summary failed / regeneration
    failed / missing, missing audio, running, and unverified routing."""

    def _status(self, client, recording):
        response = client.get(f"/recordings/{recording.pk}/")
        assert response.status_code == 200
        content = response.content.decode()
        match = re.search(
            r'<div class="status-panel status-panel-(\w+)"[^>]*>\s*'
            r'<span class="status-panel-label">Status</span>\s*'
            r'<span class="status-panel-main">(.*?)</span>',
            content,
            re.DOTALL,
        )
        assert match, "status panel not found"
        return match.group(1), match.group(2)

    def test_healthy_no_action_required(self, client):
        recording, _t, _s, _summary = _summary_recording()
        level, detail = self._status(client, recording)
        assert level == "ok"
        assert "no action required" in detail

    def test_failed_offers_retry(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-fail")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.FAILED, failure_stage="transcription"
        )
        recording.refresh_from_db()
        level, detail = self._status(client, recording)
        assert level == "danger"
        assert "retry is available" in detail

    def test_retranscription_failed_keeps_current(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-retx")
        Recording.objects.filter(pk=recording.pk).update(retranscription_failed=True)
        level, detail = self._status(client, recording)
        assert level == "warn"
        assert "existing transcript stays active" in detail

    def test_ready_to_transcribe(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-ready")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.READY_TO_TRANSCRIBE
        )
        level, detail = self._status(client, recording)
        assert level == "warn"
        assert "Ready to transcribe" in detail

    def test_needs_review(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-review")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW
        )
        level, detail = self._status(client, recording)
        assert level == "warn"
        assert "Routing needs review" in detail

    def test_summary_failed(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-sumfail")
        Recording.objects.filter(pk=recording.pk).update(summary_status=SummaryState.FAILED)
        level, detail = self._status(client, recording)
        assert level == "danger"
        assert "summarization attempt failed" in detail

    def test_resummarization_failed_keeps_summary(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-regen")
        Recording.objects.filter(pk=recording.pk).update(resummarization_failed=True)
        level, detail = self._status(client, recording)
        assert level == "warn"
        assert "current summary was kept" in detail

    def test_missing_audio(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-audio")
        Recording.objects.filter(pk=recording.pk).update(audio_status=AudioStatus.MISSING)
        level, detail = self._status(client, recording)
        assert level == "warn"
        assert "Audio missing" in detail

    def test_summary_missing(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-nosum")
        Recording.objects.filter(pk=recording.pk).update(summary_status=SummaryState.MISSING)
        level, detail = self._status(client, recording)
        assert level == "warn"
        assert "Summary not generated" in detail

    def test_running_attempt(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="st-run")
        ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.RUNNING, finished_at=None,
        )
        level, detail = self._status(client, recording)
        assert level == "running"
        assert "currently running" in detail

    def test_unverified_routing_warns(self, client):
        recording, _t, _s, _summary = _summary_recording()
        RoutingDecision.objects.create(
            recording=recording, ordinal=1, route_suggestion="european",
            profile_name="european", model_id="m", method=RoutingMethod.AUTOMATIC,
            routing_verified=False, is_active=True,
        )
        level, detail = self._status(client, recording)
        assert level == "warn"
        assert "routing unverified" in detail


class TestDetailActionsPresentation:
    """Recording Detail action presentation (v6): a compact recommended
    action for states that genuinely need attention and a collapsed
    native `<details>` for advanced processing actions. All business
    semantics (route/confirm/transcribe/retry) stay unchanged; this only
    re-organises which form is prominent."""

    def test_healthy_has_no_large_actions_section(self, client):
        recording, _t, _s, _summary = _summary_recording()
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        # No large generic Actions section, no suggested recommended action.
        assert 'aria-label="Actions"' not in content
        assert 'aria-label="Recommended action"' not in content

    def test_healthy_routing_disclosure_collapsed_with_route_form(self, client):
        recording, _t, _s, _summary = _summary_recording()
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        # Compact native Routing disclosure (no `open`), beside the
        # Transcript/History links — the standalone "Advanced processing
        # actions" block is gone.
        assert "Advanced processing actions" not in content
        assert '<details class="enhanced-details routing-editor" id="routing-editor">' in content
        assert "Routing" in content
        # It retains the manual route form and its fingerprint, posting
        # to the existing action-route endpoint (server confirmation
        # interstitial + lock + revalidate unchanged).
        assert '/route/' in content
        assert 'id="id_route_profile_routing"' in content
        assert 'name="profile"' in content
        assert 'name="fingerprint"' in content
        # The concise retranscription explanation is present.
        assert "schedules a retranscription" in content
        assert "created only when the retranscription succeeds" in content

    def test_needs_review_with_confirmable_decision_confirm_only_prominent(self, client):
        """With an active unverified routing decision the immediate
        recommended action is ONLY 'Confirm routing'; manual profile
        selection moves to the collapsed Routing disclosure."""
        recording, _t, _s = make_transcribed_recording(["a"], sha="pa-nr-conf")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW
        )
        recording.refresh_from_db()
        RoutingDecision.objects.create(
            recording=recording, ordinal=1, route_suggestion="european",
            profile_name="european", model_id="m", method=RoutingMethod.AUTOMATIC,
            routing_verified=False, is_active=True,
        )
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'aria-label="Recommended action"' in content
        assert "Confirm routing" in content
        # Manual profile selection is NOT prominent; it lives in Routing.
        assert 'id="id_route_profile"' not in content
        assert 'id="id_route_profile_routing"' in content
        assert 'name="fingerprint"' in content

    def test_needs_review_without_decision_manual_route_only_no_duplicate(self, client):
        """With no confirmable decision the manual route is the single
        prominent action and is NOT duplicated in the Routing
        disclosure."""
        recording, _t, _s = make_transcribed_recording(["a"], sha="pa-nr-man")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW
        )
        recording.refresh_from_db()
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'aria-label="Recommended action"' in content
        assert 'id="id_route_profile"' in content
        assert "Confirm routing" not in content
        # The Routing disclosure must not duplicate the prominent manual
        # form (no trigger, no form).
        assert 'id="routing-editor"' not in content
        assert 'id="id_route_profile_routing"' not in content
        assert 'name="fingerprint"' in content

    def test_ready_to_transcribe_prominent_transcribe(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="pa-ready")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.READY_TO_TRANSCRIBE
        )
        recording.refresh_from_db()
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'aria-label="Recommended action"' in content
        assert "Transcribe now" in content

    def test_failed_prominent_retry(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="pa-fail")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.FAILED
        )
        recording.refresh_from_db()
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'aria-label="Recommended action"' in content
        assert "Retry failed stage" in content

    def test_retranscription_failed_prominent_retry(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="pa-retx")
        Recording.objects.filter(pk=recording.pk).update(retranscription_failed=True)
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'aria-label="Recommended action"' in content
        assert "Retry failed stage" in content

    def test_transcribed_unverified_routing_prominent_confirm(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="pa-unv")
        RoutingDecision.objects.create(
            recording=recording, ordinal=1, route_suggestion="european",
            profile_name="european", model_id="m", method=RoutingMethod.AUTOMATIC,
            routing_verified=False, is_active=True,
        )
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'aria-label="Recommended action"' in content
        assert "Confirm routing" in content
        assert "Transcribe now" not in content

    def test_running_state_no_prominent_action(self, client):
        recording, _t, _s = make_transcribed_recording(["a"], sha="pa-run")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.TRANSCRIBING
        )
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'aria-label="Recommended action"' not in content

    def test_summary_failure_relies_on_contextual_summary_action(self, client):
        """A summary-only failure keeps its retry in the Summary heading;
        it must NOT surface a generic duplicate 'Retry failed stage' in
        the primary action area."""
        recording, _t, _s, _summary = _summary_recording()
        Recording.objects.filter(pk=recording.pk).update(summary_status=SummaryState.FAILED)
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'aria-label="Recommended action"' not in content
        assert "Retry failed stage" not in content
        # Contextual retry stays in the Summary heading / variant action
        # (here a current summary exists, so the action is "Regenerate").
        assert "/summarize/" in content

    def test_summary_failed_without_current_uses_contextual_retry(self, client):
        """A genuine summary failure (no current summary) keeps its retry
        in the Summary heading and never surfaces a generic duplicate
        'Retry failed stage' in the primary action area."""
        recording, _t, _s = make_transcribed_recording(["a"], sha="pa-sumfail")
        Recording.objects.filter(pk=recording.pk).update(summary_status=SummaryState.FAILED)
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'aria-label="Recommended action"' not in content
        assert "Retry failed stage" not in content
        # The contextual variant action renders the Retry button.
        assert "Retry" in content
        assert "/summarize/" in content


class TestConfirmFormProgressiveEnhancement:
    """Small static contract for the shared .confirm-form enhancement:
    it must attach a submit listener (not click-only), guard repeated
    submits, disable and relabel the button, and never alter the no-JS
    POST path. Kept small/maintainable because no browser JS harness
    exists; rendering-level 'external-only JS / no inline handlers' is
    already covered by the security tests."""

    def test_app_js_confirm_form_contract(self):
        from django.contrib.staticfiles import finders

        path = finders.find("workflow/app.js")
        assert path is not None
        source = Path(path).read_text(encoding="utf-8")
        assert ".confirm-form" in source
        assert 'addEventListener("submit"' in source
        assert "event.preventDefault()" in source  # guards repeated submits
        assert "button.disabled = true" in source
        assert 'setAttribute("aria-disabled", "true")' in source
        assert 'textContent = "Running…"' in source
        assert 'setAttribute("aria-busy", "true")' in source


class TestNestedKeyPoints:
    """Structured key points render as real nested <ol>/<li> semantics
    with an h1/h2/h3 hierarchy; historical strings and malformed rows
    stay safely readable flat items, always autoescaped."""

    def _detail_with_points(self, client, points, sha="kp-1"):
        recording, transcript, section = make_transcribed_recording(["x"], sha=sha)
        make_summary_version(recording, transcript, section, key_points=points)
        response = client.get(f"/recordings/{recording.pk}/")
        assert response.status_code == 200
        return response.content.decode()

    def test_nested_structured_points_render_nested_lists(self, client):
        content = self._detail_with_points(
            client,
            [
                {"text": "First level one", "level": 1},
                {"text": "Nested under first", "level": 2},
                {"text": "Deeper under first", "level": 3},
                {"text": "Second level one", "level": 1},
                {"text": "Nested under second", "level": 2},
            ],
        )
        compact = re.sub(r"\s+<", "<", content)
        # Real <ol>/<li> nesting: top list + one nested list per item
        # with children, browser numbering reproduces 1. / 1.1 / 1.1.1.
        kp = content[content.find("Key points"):content.find("Action items")]
        assert kp.count("<ol") == 4
        assert kp.count("</ol>") == 4
        assert kp.count("<li>") == 5
        assert (
            '<li>First level one<ol class="key-points"><li>Nested under first'
            '<ol class="key-points"><li>Deeper under first</li></ol></li></ol></li>'
            '<li>Second level one<ol class="key-points"><li>Nested under second</li></ol></li>'
        ) in compact

    def test_key_point_text_is_autoescaped(self, client):
        content = self._detail_with_points(
            client, [{"text": "<img src=x onerror=1>", "level": 1}], sha="kp-xss"
        )
        assert "<img src=x" not in content
        assert "&lt;img" in content

    def test_historical_strings_and_malformed_rows_stay_readable(self, client):
        content = self._detail_with_points(
            client,
            [
                "Plain historical point",
                {"text": "Level zero point", "level": 0},
                {"text": "Orphaned deeper point", "level": 2},
                {"text": 12345},   # non-str text: never coerced
                42,                # non-dict rows are skipped
                None,
            ],
            sha="kp-hist",
        )
        compact = re.sub(r"\s+<", "<", content)
        assert "<li>Plain historical point</li>" in compact
        assert "<li>Level zero point</li>" in compact
        assert "<li>Orphaned deeper point</li>" in compact
        assert "<li>12345</li>" not in compact
        # Only the single top-level list — no nesting for fallback rows.
        assert content.count("<ol") == 1

    def test_section_headings_are_h3(self, client):
        recording, transcript, section = make_transcribed_recording(["x"], sha="kp-h")
        make_summary_version(recording, transcript, section)
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        # Detail hierarchy: h1 (title) -> h2 (Summary) -> h3 sections;
        # the summary title is a styled paragraph, never a heading.
        for heading in ("Overview", "Key points", "Action items", "People", "Topics"):
            assert f"<h3>{heading}</h3>" in content, heading
        assert "<h4>" not in content
        assert '<p class="summary-title">' in content


class TestTranscriptScreen:
    def test_segment_anchors_present(self, client):
        recording, _t, _s = make_transcribed_recording(["a", "b"], sha="anchor-1")
        response = client.get(f"/recordings/{recording.pk}/transcript/")
        content = response.content.decode()
        assert 'id="segment-0"' in content
        assert 'id="segment-1"' in content

    def test_meta_context_and_export_links(self, client):
        recording, transcript, _s = make_transcribed_recording(["a"], sha="meta-1")
        transcript.language_observed = "fi"
        transcript.save(update_fields=["language_observed"])
        recording.attempts.filter(stage=AttemptStage.TRANSCRIPTION).update(model_id="parakeet-v3")
        response = client.get(f"/recordings/{recording.pk}/transcript/")
        content = response.content.decode()
        assert "1 segments" in content
        assert "language: fi" in content
        assert "model: parakeet-v3" in content
        assert "version: #" in content
        # Copy button reuses the existing export-URL mechanism; plain and
        # timestamped downloads stay available.
        assert "data-copy-url" in content
        assert "transcript/export/?format=text" in content
        assert "transcript/export/?format=timestamped" in content

    def test_transcript_page_queries_bounded_no_lazy_attempt(self, client):
        """The transcript is fetched with select_related('attempt'), so
        metadata rendering never triggers an incidental per-page query."""
        recording, _t, _s = make_transcribed_recording(["a"] * 10, sha="qc-tx")
        recording.attempts.filter(stage=AttemptStage.TRANSCRIPTION).update(model_id="parakeet-v3")
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(f"/recordings/{recording.pk}/transcript/")
        assert response.status_code == 200
        assert len(ctx.captured_queries) < 20, len(ctx.captured_queries)

    def test_historical_version_banner_and_versioned_exports(self, client):
        from django.utils import timezone as tz

        recording, transcript, _s = make_transcribed_recording(["old"], sha="histv-1")
        attempt2 = ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.SUCCESS, finished_at=tz.now(),
        )
        transcript2 = Transcript.objects.create(recording=recording, attempt=attempt2, text_normalized="new")
        transcript.is_active = False
        transcript.superseded_at = tz.now()
        transcript.save()
        transcript2.is_active = True
        transcript2.activated_at = tz.now()
        transcript2.save()
        response = client.get(f"/recordings/{recording.pk}/transcript/?v={transcript.pk}")
        content = response.content.decode()
        assert "HISTORICAL transcript version" in content
        assert f"version={transcript.pk}" in content  # export links keep version=
        assert "old" in content  # the historical text renders


class TestDetailRedesign:
    def test_no_recent_attempts_table_on_detail(self, client):
        """Audit data lives on History; the detail page must not render a
        recent-attempts table or its raw attempt details."""
        recording, _t, _s, _summary = _summary_recording()
        ProcessingAttempt.objects.create(
            recording=recording, stage=AttemptStage.TRANSCRIPTION, ordinal=2,
            outcome=AttemptOutcome.NONZERO_EXIT, error_code="mw_exit_1",
            error_message="raw stderr line", finished_at=None,
        )
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert "Recent attempts" not in content
        assert "mw_exit_1" not in content

    def test_collapsed_provenance_and_technical_details(self, client):
        recording, _t, _s, _summary = _summary_recording()
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'class="technical-details"' in content
        assert "Summary provenance" in content
        assert "Technical details" in content
        # JSON export stays available, but secondary (inside the
        # collapsed provenance block).
        assert "format=json" in content

    def test_detail_get_is_select_only(self, client):
        recording, _t, _s, _summary = _summary_recording()
        make_tag_assignment(recording, make_tag("DetailSelect"))
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(f"/recordings/{recording.pk}/")
        assert response.status_code == 200
        non_select = [
            q for q in ctx.captured_queries
            if not q["sql"].lstrip().upper().startswith("SELECT")
        ]
        assert non_select == []


class TestGetPurity:
    def test_detail_get_makes_no_writes(self, client):
        recording, _t, _s, _summary = _summary_recording()
        before_attempts = ProcessingAttempt.objects.count()
        before_recordings = Recording.objects.count()
        client.get(f"/recordings/{recording.pk}/")
        assert ProcessingAttempt.objects.count() == before_attempts
        assert Recording.objects.count() == before_recordings

    def test_list_get_makes_no_writes(self, client):
        _summary_recording()
        before = Recording.objects.count()
        client.get("/recordings/")
        assert Recording.objects.count() == before

    def test_export_get_makes_no_writes(self, client):
        recording, _t, _s, _summary = _summary_recording()
        before = Recording.objects.count()
        client.get(f"/recordings/{recording.pk}/summary/export/?format=markdown")
        assert Recording.objects.count() == before

    def test_detail_get_no_routing_or_hash_side_effects(self, client):
        recording, _t, _s, _summary = _summary_recording()
        before_decisions = RoutingDecision.objects.count()
        client.get(f"/recordings/{recording.pk}/")
        assert RoutingDecision.objects.count() == before_decisions
