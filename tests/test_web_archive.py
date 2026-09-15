"""Reversible archive web surface.

POST-only archive/restore on the FIRST POST (no confirmation interstitial),
the opaque fingerprint binding the archived state (stale cross-state forms
are safe no-ops), readable archived detail with a Restore action and no
ordinary mutating forms, forged mutations rejected service-side, the
read-only archived listing, and the section removal-discoverability link.
"""

from __future__ import annotations

import pytest
from django.test import Client

from factories import make_transcribed_recording
from workflow.models import Recording, Section, SegmentedVersion
from workflow.services.archive import archive_recording
from workflow.services.segmentation import (
    save_segmented_version,
    segmentation_fingerprint,
)
from workflow.services.web_actions import state_fingerprint

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


@pytest.fixture
def client():
    return Client()


def _fingerprint(recording):
    return state_fingerprint(recording)


class TestArchiveActions:
    def test_active_detail_offers_archive_danger_zone(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="web-arch-1")
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert "Archive from Brain" in content
        assert "/archive/" in content
        assert "This hides the recording from the Library, search, Ask and Review." in content
        assert "restore it later" in content
        # Verbose retained-audio/history sentence removed from the Danger zone.
        assert "source-file deletion" not in content

    def test_first_archive_post_executes_then_restore(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="web-arch-2")
        response = client.post(
            f"/recordings/{recording.pk}/archive/",
            {"fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 302
        recording.refresh_from_db()
        assert recording.archived_at is not None
        detail = client.get(response.headers["Location"]).content.decode()
        assert "Archived." in detail
        assert "/restore/" in detail
        assert "/archive/" not in detail

        response = client.post(
            f"/recordings/{recording.pk}/restore/",
            {"fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 302
        recording.refresh_from_db()
        assert recording.archived_at is None
        assert "Archive from Brain" in client.get(
            response.headers["Location"]
        ).content.decode()

    def test_archived_detail_hides_ordinary_mutating_forms(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="web-arch-3")
        archive_recording(recording)
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        for route in ("/route/", "/transcribe/", "/retry/", "/summarize/"):
            assert route not in content
        assert "/tags/apply/" not in content
        assert "/archive/" not in content
        assert "/restore/" in content

    def test_stale_cross_state_fingerprint_is_safe_noop(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="web-arch-4")
        active_fingerprint = _fingerprint(recording)
        # Another tab archives first; the stale active-state archive form
        # must not re-archive or change anything.
        archive_recording(recording)
        recording.refresh_from_db()
        stamp = recording.archived_at
        response = client.post(
            f"/recordings/{recording.pk}/archive/",
            {"fingerprint": active_fingerprint},
        )
        assert response.status_code == 302
        recording.refresh_from_db()
        assert recording.archived_at == stamp
        # And a pre-restore form is stale after a restore.
        restore_fingerprint = _fingerprint(recording)
        client.post(
            f"/recordings/{recording.pk}/restore/",
            {"fingerprint": restore_fingerprint},
        )
        recording.refresh_from_db()
        assert recording.archived_at is None
        response = client.post(
            f"/recordings/{recording.pk}/restore/",
            {"fingerprint": restore_fingerprint},
        )
        assert response.status_code == 302
        recording.refresh_from_db()
        assert recording.archived_at is None

    def test_missing_or_duplicate_fingerprint_rejected(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="web-arch-5")
        assert client.post(f"/recordings/{recording.pk}/archive/", {}).status_code == 400
        assert (
            client.post(
                f"/recordings/{recording.pk}/archive/",
                {"fingerprint": [_fingerprint(recording), _fingerprint(recording)]},
            ).status_code
            == 400
        )
        recording.refresh_from_db()
        assert recording.archived_at is None

    def test_get_is_405(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="web-arch-6")
        assert client.get(f"/recordings/{recording.pk}/archive/").status_code == 405


class TestForgedMutationsRejected:
    def test_archived_tag_apply_rejected(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="web-arch-7")
        archive_recording(recording)
        response = client.post(
            f"/recordings/{recording.pk}/tags/apply/", {}, follow=True
        )
        body = response.content.decode()
        assert "archived" in body.lower()
        assert "Traceback" not in body

    def test_archived_segmentation_save_rejected(self, client):
        recording, transcript, _s = make_transcribed_recording(
            ["a", "b"], sha="web-arch-8"
        )
        fingerprint = segmentation_fingerprint(
            recording.pk, transcript, timezone_name="Europe/Helsinki"
        )
        archive_recording(recording)
        response = client.post(
            f"/recordings/{recording.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "0",
                "end_exclusive": "2",
                "fingerprint": fingerprint,
            },
        )
        assert response.status_code == 302
        assert SegmentedVersion.objects.filter(transcript=transcript).count() == 0

    def test_forged_pipeline_actions_rejected_on_archived(self, client):
        from workflow.models import RoutingDecision

        recording, _t, _s = make_transcribed_recording(["x"], sha="web-arch-forge")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status="needs_review"
        )
        recording.refresh_from_db()
        archive_recording(recording)
        recording.refresh_from_db()
        fingerprint = _fingerprint(recording)
        response = client.post(
            f"/recordings/{recording.pk}/route/",
            {"profile": "european", "fingerprint": fingerprint},
        )
        assert response.status_code == 400
        assert "archived" in response.content.decode().lower()
        assert RoutingDecision.objects.filter(recording=recording).count() == 0


class TestArchivedListing:
    def test_library_links_and_archived_page_lists_only_archived(self, client):
        active, _t, _s = make_transcribed_recording(["visible"], sha="web-arch-9a")
        archived, _t2, _s2 = make_transcribed_recording(["hidden"], sha="web-arch-9b")
        archive_recording(archived)

        library = client.get("/recordings/").content.decode()
        assert "/recordings/archived/" in library

        page = client.get("/recordings/archived/")
        assert page.status_code == 200
        content = page.content.decode()
        assert f"/recordings/{archived.pk}/" in content
        assert f"/recordings/{active.pk}/" not in content

    def test_archived_page_empty_state(self, client):
        content = client.get("/recordings/archived/").content.decode()
        assert "Archived items" in content
        assert "No archived items." in content


class TestSectionRemovalDiscoverability:
    def _active_split(self, sha):
        recording, transcript, _fixed = make_transcribed_recording(
            ["a", "b", "c", "d"], sha=sha
        )
        result = save_segmented_version(
            recording.pk, transcript.pk, 0, 4, [2], ["First", "Second"]
        )
        sections = list(
            Section.objects.filter(segmented_version_id=result.version_id).order_by(
                "ordinal"
            )
        )
        return recording, transcript, sections

    def test_active_section_has_edit_remove_link(self, client):
        recording, _t, sections = self._active_split("web-sec-1")
        content = client.get(
            f"/recordings/{recording.pk}/sections/{sections[0].pk}/"
        ).content.decode()
        assert "Edit/remove section" in content
        # The redundant second paragraph/link and its verbose copy are gone.
        assert content.count("Edit/remove section") == 1
        assert "merges this section with its neighbor" not in content

    def test_archived_section_has_no_removal_link(self, client):
        recording, _t, sections = self._active_split("web-sec-2")
        archive_recording(recording)
        content = client.get(
            f"/recordings/{recording.pk}/sections/{sections[0].pk}/"
        ).content.decode()
        assert "Edit/remove section" not in content
        assert "Archived." in content
        assert "/summarize/" not in content
        assert "/tags/apply/" not in content
