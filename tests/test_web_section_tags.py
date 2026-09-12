"""Step 6.2 section-scoped tag editing (web).

Proves:

- parent + section scoped POST routes for bulk apply (modal Done),
  confirm suggested, and remove; the backend section tag services are
  the only writers;
- scope isolation: a section tag edit NEVER touches recording-scoped
  assignments and never schedules a recording search sync;
- the complete-selection modal semantics (create/reactivate/remove/
  unchanged no-op) work identically for a Section; success (changed or
  unchanged) emits NO banner for bulk Done;
- custom tag creation is global and config-compatible; only the
  assignment is section-scoped;
- suggested chips support Confirm; chips support Remove;
- historical sections are rejected (read-only) with stable sanitized
  errors; CSRF enforced; GET is a 405; the redirect always targets the
  SAME section detail page.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.test import Client

from workflow.models import (
    Section,
    Tag,
    TagAssignment,
    TagDeactivatedBy,
    TagOrigin,
)
from workflow.services.segmentation import save_segmented_version

from factories import make_tag, make_transcribed_recording

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


@pytest.fixture
def client():
    return Client()


def _split(sha="sec-tags-web", count=8):
    rec, transcript, _fixed = make_transcribed_recording(
        [f"segment {i}" for i in range(count)], sha=sha
    )
    result = save_segmented_version(
        rec.pk, transcript.pk, 0, count, [3], ["First topic", "Second topic"]
    )
    sections = list(
        Section.objects.filter(segmented_version_id=result.version_id).order_by("ordinal")
    )
    return rec, transcript, sections


class TestCsrfAndMethod:
    def test_get_is_405(self, client):
        rec, _t, sections = _split()
        for route in ("apply", "confirm", "remove"):
            if route == "apply":
                url = f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/apply/"
            else:
                tag = make_tag(f"T{route}")
                url = f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/{tag.pk}/{route}/"
            response = client.get(url)
            assert response.status_code == 405

    def test_post_without_csrf_rejected(self):
        rec, _t, sections = _split()
        csrf_client = Client(enforce_csrf_checks=True)
        response = csrf_client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/apply/",
            {"selected_tags": []},
        )
        assert response.status_code == 403


class TestBulkApply:
    def test_apply_creates_section_scoped_manual_assignment(self, client):
        rec, _t, sections = _split()
        tag = make_tag("Family")
        response = client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/apply/",
            {"selected_tags": [str(tag.pk)]},
        )
        assert response.status_code == 302
        assert response["Location"] == f"/recordings/{rec.pk}/sections/{sections[0].pk}/"
        assignment = TagAssignment.objects.get(section=sections[0], tag=tag)
        assert assignment.origin == TagOrigin.MANUAL
        assert assignment.is_active
        # No recording-scoped row is created.
        assert not TagAssignment.objects.filter(
            recording=rec, tag=tag, section__isnull=True
        ).exists()

    def test_apply_scope_isolation(self, client):
        rec, _t, sections = _split()
        tag = make_tag("Family")
        client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/apply/",
            {"selected_tags": [str(tag.pk)]},
        )
        client.post(
            f"/recordings/{rec.pk}/sections/{sections[1].pk}/tags/apply/",
            {"selected_tags": [str(tag.pk)]},
        )
        assert TagAssignment.objects.filter(section=sections[0], tag=tag).count() == 1
        assert TagAssignment.objects.filter(section=sections[1], tag=tag).count() == 1
        # Independent rows: deactivating one never affects the other.
        client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/{tag.pk}/remove/", {}
        )
        assert not TagAssignment.objects.get(section=sections[0], tag=tag).is_active
        assert TagAssignment.objects.get(section=sections[1], tag=tag).is_active

    def test_unchanged_done_emits_no_banner(self, client):
        rec, _t, sections = _split()
        tag = make_tag("Family")
        client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/apply/",
            {"selected_tags": [str(tag.pk)]},
        )
        # Repeat the same selection: unchanged => no success banner.
        response = client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/apply/",
            {"selected_tags": [str(tag.pk)]},
            follow=True,
        )
        content = response.content.decode()
        assert "Tag &#x27;" not in content
        assert "changed" not in content.lower() or "unchanged" in content.lower()

    def test_unselect_removes_with_user_suppression(self, client):
        rec, _t, sections = _split()
        tag = make_tag("Family")
        client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/apply/",
            {"selected_tags": [str(tag.pk)]},
        )
        client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/apply/",
            {"selected_tags": []},
        )
        assignment = TagAssignment.objects.get(section=sections[0], tag=tag)
        assert not assignment.is_active
        assert assignment.deactivated_by == TagDeactivatedBy.USER

    def test_custom_tag_create_is_global_assignment_section_scoped(self, client):
        rec, _t, sections = _split()
        response = client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/apply/",
            {"selected_tags": [], "new_tag_name": "My Custom"},
        )
        assert response.status_code == 302
        tag = Tag.objects.get(name_key="my custom")
        assert tag.definition_origin == Tag.DefinitionOrigin.CUSTOM
        assert tag.is_configured
        assert TagAssignment.objects.filter(section=sections[0], tag=tag).exists()
        # The same global definition is reusable on the sibling section.
        client.post(
            f"/recordings/{rec.pk}/sections/{sections[1].pk}/tags/apply/",
            {"selected_tags": [str(tag.pk)]},
        )
        assert TagAssignment.objects.filter(section=sections[1], tag=tag).count() == 1

    def test_duplicate_custom_name_is_sanitized(self, client):
        rec, _t, sections = _split()
        make_tag("Existing")
        response = client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/apply/",
            {"selected_tags": [], "new_tag_name": "Existing"},
            follow=True,
        )
        assert response.status_code == 200
        assert "already exists" in response.content.decode()
        assert not TagAssignment.objects.filter(section=sections[0]).exists()


class TestConfirmAndRemove:
    def test_confirm_suggestion(self, client):
        rec, _t, sections = _split()
        tag = make_tag("Family")
        TagAssignment.objects.create(
            recording=rec, section=sections[0], tag=tag,
            origin=TagOrigin.SUGGESTED, is_active=True,
        )
        response = client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/{tag.pk}/confirm/", {}
        )
        assert response.status_code == 302
        assert TagAssignment.objects.get(section=sections[0], tag=tag).origin == TagOrigin.CONFIRMED

    def test_confirm_inactive_suggestion_is_sanitized_error(self, client):
        rec, _t, sections = _split()
        tag = make_tag("Family")
        # Inactive row (suppressed).
        TagAssignment.objects.create(
            recording=rec, section=sections[0], tag=tag,
            origin=TagOrigin.SUGGESTED, is_active=False,
            deactivated_by="model",
        )
        response = client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/{tag.pk}/confirm/",
            {},
            follow=True,
        )
        assert response.status_code == 200
        assert "Only an active" in response.content.decode()
        assert TagAssignment.objects.get(section=sections[0], tag=tag).origin == TagOrigin.SUGGESTED

    def test_remove_section_tag(self, client):
        rec, _t, sections = _split()
        tag = make_tag("Family")
        TagAssignment.objects.create(
            recording=rec, section=sections[0], tag=tag, origin="manual", is_active=True,
        )
        response = client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/{tag.pk}/remove/", {}
        )
        assert response.status_code == 302
        assignment = TagAssignment.objects.get(section=sections[0], tag=tag)
        assert not assignment.is_active
        assert assignment.deactivated_by == TagDeactivatedBy.USER


class TestHistoricalRejection:
    def _historical_section(self):
        rec, transcript, sections = _split(sha="sec-tags-hist")
        section = sections[0]
        # Supersede the layout.
        save_segmented_version(rec.pk, transcript.pk, 0, transcript.segments.count())
        return rec, section

    def test_apply_rejected_for_historical_section(self, client):
        rec, section = self._historical_section()
        tag = make_tag("Family")
        response = client.post(
            f"/recordings/{rec.pk}/sections/{section.pk}/tags/apply/",
            {"selected_tags": [str(tag.pk)]},
            follow=True,
        )
        assert response.status_code == 200
        assert "not available for tag editing" in response.content.decode()
        assert not TagAssignment.objects.filter(section=section).exists()

    def test_confirm_rejected_for_historical_section(self, client):
        rec, section = self._historical_section()
        tag = make_tag("Family")
        TagAssignment.objects.create(
            recording=rec, section=section, tag=tag, origin=TagOrigin.SUGGESTED, is_active=True,
        )
        response = client.post(
            f"/recordings/{rec.pk}/sections/{section.pk}/tags/{tag.pk}/confirm/", {},
            follow=True,
        )
        assert response.status_code == 200
        assert "not available for tag editing" in response.content.decode()
        assert TagAssignment.objects.get(section=section, tag=tag).origin == TagOrigin.SUGGESTED

    def test_remove_rejected_for_historical_section(self, client):
        rec, section = self._historical_section()
        tag = make_tag("Family")
        TagAssignment.objects.create(
            recording=rec, section=section, tag=tag, origin="manual", is_active=True,
        )
        response = client.post(
            f"/recordings/{rec.pk}/sections/{section.pk}/tags/{tag.pk}/remove/", {},
            follow=True,
        )
        assert response.status_code == 200
        assert TagAssignment.objects.get(section=section, tag=tag).is_active

    def test_cross_recording_section_404(self, client):
        rec_a, _t, sections_a = _split(sha="sec-tags-cross-a")
        rec_b, _t_b, _s_b = _split(sha="sec-tags-cross-b")
        tag = make_tag("Family")
        response = client.post(
            f"/recordings/{rec_b.pk}/sections/{sections_a[0].pk}/tags/apply/",
            {"selected_tags": [str(tag.pk)]},
        )
        assert response.status_code == 404


class TestNoSync:
    def test_section_tag_mutations_schedule_no_recording_sync(self, client):
        """Section-scoped tag edits NEVER schedule a recording search
        sync (section tags are not indexed until Step 6.3): the apply
        POST performs no workflow_search_document writes."""
        from django.test.utils import CaptureQueriesContext

        rec, _t, sections = _split()
        tag = make_tag("Family")
        with CaptureQueriesContext(connection) as ctx:
            client.post(
                f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/apply/",
                {"selected_tags": [str(tag.pk)]},
            )
        assert not any("workflow_search_document" in q["sql"] for q in ctx.captured_queries)