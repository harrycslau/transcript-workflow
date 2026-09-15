"""Reversible individual-Section archive web surface + Archived items.

POST-only archive/restore on the FIRST POST (no confirmation
interstitial), the opaque Section fingerprint binding the Section archive
state (stale/duplicate forms are safe no-ops), readable archived Section
detail with a Restore action and no ordinary summary/tag forms, the
combined bounded Archived-items table (Recording + Section, parent/child
dedup), the compact Danger-zone copy/buttons, and the single
``Edit/remove section`` link regression.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.utils import timezone

from factories import make_transcribed_recording
from workflow.models import Recording, Section, SegmentedVersion
from workflow.services.archive import archive_recording, archive_section
from workflow.services.segmentation import SegmentationError, save_segmented_version
from workflow.services.web_actions import (
    section_restore_fingerprint,
    section_state_fingerprint,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


@pytest.fixture
def client():
    return Client()


def _active_split(sha):
    recording, transcript, _fixed = make_transcribed_recording(
        ["a", "b", "c", "d"], sha=sha
    )
    result = save_segmented_version(
        recording.pk, transcript.pk, 0, 4, [2], ["First", "Second"]
    )
    sections = list(
        Section.objects.filter(segmented_version_id=result.version_id).order_by("ordinal")
    )
    return recording, transcript, sections


def _fingerprint(recording, section):
    return section_state_fingerprint(recording, section)


def _restore_fingerprint(recording, section):
    return section_restore_fingerprint(recording, section)


class TestSectionArchiveActions:
    def test_active_section_has_danger_zone_archive_button(self, client):
        recording, _t, sections = _active_split("web-sec-arch-1")
        content = client.get(
            f"/recordings/{recording.pk}/sections/{sections[0].pk}/"
        ).content.decode()
        assert "Archive section" in content
        assert f"/sections/{sections[0].pk}/archive/" in content
        assert "btn-sm" in content
        assert "This hides the section from the Library, search and Ask." in content
        assert "trim &amp; split layout is untouched" not in content

    def test_first_archive_post_executes_then_restore(self, client):
        recording, _t, sections = _active_split("web-sec-arch-2")
        section = sections[0]
        response = client.post(
            f"/recordings/{recording.pk}/sections/{section.pk}/archive/",
            {"fingerprint": _fingerprint(recording, section)},
        )
        assert response.status_code == 302
        section.refresh_from_db()
        assert section.archived_at is not None

        detail = client.get(response.headers["Location"]).content.decode()
        assert "Section archived." in detail
        assert f"/sections/{section.pk}/restore/" in detail
        assert f"/sections/{section.pk}/archive/" not in detail
        # No ordinary summary/tag actions.
        assert "/summarize/" not in detail
        assert "/tags/apply/" not in detail

        response = client.post(
            f"/recordings/{recording.pk}/sections/{section.pk}/restore/",
            {"fingerprint": _restore_fingerprint(recording, section)},
        )
        assert response.status_code == 302
        section.refresh_from_db()
        assert section.archived_at is None
        assert "Archive section" in client.get(
            response.headers["Location"]
        ).content.decode()

    def test_stale_cross_state_fingerprint_is_safe_noop(self, client):
        recording, _t, sections = _active_split("web-sec-arch-3")
        section = sections[1]
        active_fingerprint = _fingerprint(recording, section)
        # Another tab archives first; the stale active-state form is a no-op.
        archive_section(recording, section)
        section.refresh_from_db()
        stamp = section.archived_at
        response = client.post(
            f"/recordings/{recording.pk}/sections/{section.pk}/archive/",
            {"fingerprint": active_fingerprint},
        )
        assert response.status_code == 302
        section.refresh_from_db()
        assert section.archived_at == stamp

    def test_missing_or_duplicate_fingerprint_rejected(self, client):
        recording, _t, sections = _active_split("web-sec-arch-4")
        section = sections[0]
        assert (
            client.post(
                f"/recordings/{recording.pk}/sections/{section.pk}/archive/", {}
            ).status_code
            == 400
        )
        assert (
            client.post(
                f"/recordings/{recording.pk}/sections/{section.pk}/archive/",
                {
                    "fingerprint": [
                        _fingerprint(recording, section),
                        _fingerprint(recording, section),
                    ]
                },
            ).status_code
            == 400
        )
        section.refresh_from_db()
        assert section.archived_at is None

    def test_get_is_405_and_get_never_mutates(self, client):
        recording, _t, sections = _active_split("web-sec-arch-5")
        section = sections[0]
        assert (
            client.get(
                f"/recordings/{recording.pk}/sections/{section.pk}/archive/"
            ).status_code
            == 405
        )
        client.get(f"/recordings/{recording.pk}/sections/{section.pk}/")
        section.refresh_from_db()
        assert section.archived_at is None

    def test_parent_archived_suppresses_section_controls(self, client):
        recording, _t, sections = _active_split("web-sec-arch-6")
        section = sections[0]
        archive_recording(recording)

        detail = client.get(
            f"/recordings/{recording.pk}/sections/{section.pk}/"
        ).content.decode()
        assert f"/sections/{section.pk}/archive/" not in detail
        assert f"/sections/{section.pk}/restore/" not in detail

        # Defense-in-depth: a forged POST is a safe no-op.
        response = client.post(
            f"/recordings/{recording.pk}/sections/{section.pk}/archive/",
            {"fingerprint": _fingerprint(recording, section)},
        )
        assert response.status_code == 302
        section.refresh_from_db()
        assert section.archived_at is None


class TestHistoricalArchivedSectionRestore:
    def test_archive_then_supersede_then_restore_from_historical_detail(self, client):
        recording, transcript, sections = _active_split("web-sec-hist-restore")
        target = sections[0]
        archive_section(recording, target)
        # Supersede the active layout: the archived Section becomes historical.
        save_segmented_version(
            recording.pk, transcript.pk, 0, 4, [1], ["Gamma", "Delta"]
        )
        target.refresh_from_db()
        assert target.segmented_version.is_active is False
        # The ordinary summary/archive fingerprint still fails closed for
        # the historical target (never weakened by the dedicated path).
        with pytest.raises(SegmentationError):
            section_state_fingerprint(recording, target)

        content = client.get(
            f"/recordings/{recording.pk}/sections/{target.pk}/"
        ).content.decode()
        assert "Section archived." in content
        assert f"/sections/{target.pk}/restore/" in content
        # Otherwise read-only: no ordinary actions at all.
        assert "/summarize/" not in content
        assert "/tags/apply/" not in content
        assert f"/sections/{target.pk}/archive/" not in content

        response = client.post(
            f"/recordings/{recording.pk}/sections/{target.pk}/restore/",
            {"fingerprint": _restore_fingerprint(recording, target)},
        )
        assert response.status_code == 302
        target.refresh_from_db()
        assert target.archived_at is None

        content = client.get(
            f"/recordings/{recording.pk}/sections/{target.pk}/"
        ).content.decode()
        assert "Section archived." not in content
        assert f"/sections/{target.pk}/restore/" not in content

    def test_stale_restore_fingerprint_is_safe_noop(self, client):
        recording, transcript, sections = _active_split("web-sec-hist-stale")
        target = sections[0]
        archive_section(recording, target)
        save_segmented_version(
            recording.pk, transcript.pk, 0, 4, [1], ["Gamma", "Delta"]
        )
        target.refresh_from_db()
        stale = _restore_fingerprint(recording, target)
        # Another tab restores first; the stale historical form is a no-op.
        from workflow.services.archive import restore_section

        restore_section(recording, target)
        response = client.post(
            f"/recordings/{recording.pk}/sections/{target.pk}/restore/",
            {"fingerprint": stale},
        )
        assert response.status_code == 302
        target.refresh_from_db()
        assert target.archived_at is None


class TestArchivedSectionMutationsRejected:
    def test_forged_section_summarize_rejected(self, client):
        recording, _t, sections = _active_split("web-sec-arch-mut-1")
        section = sections[0]
        archive_section(recording, section)
        response = client.post(
            f"/recordings/{recording.pk}/sections/{section.pk}/summarize/",
            {
                "fingerprint": _fingerprint(recording, section),
                "mode": "first",
                "language": "default",
            },
        )
        assert response.status_code in (302, 400)
        assert "Traceback" not in response.content.decode()

    def test_forged_section_tag_apply_rejected(self, client):
        recording, _t, sections = _active_split("web-sec-arch-mut-2")
        section = sections[0]
        archive_section(recording, section)
        response = client.post(
            f"/recordings/{recording.pk}/sections/{section.pk}/tags/apply/",
            {},
            follow=True,
        )
        assert "Traceback" not in response.content.decode()


class TestSectionLinkRegression:
    def test_single_edit_remove_link_and_no_verbose_copy(self, client):
        recording, _t, sections = _active_split("web-sec-link-1")
        content = client.get(
            f"/recordings/{recording.pk}/sections/{sections[0].pk}/"
        ).content.decode()
        assert content.count("Edit/remove section") == 1
        assert "Section in transcript" not in content
        assert "merges this section with its neighbor" not in content
        assert "Full transcript" in content
        assert "History" in content


class TestArchivedItemsTable:
    def test_recording_and_section_rows_without_parent_duplicate(self, client):
        rec, _t, sections = _active_split("web-sec-table-1")
        archive_section(rec, sections[0])
        other, _t2, _s2 = make_transcribed_recording(["hidden"], sha="web-sec-table-2")
        archive_recording(other)

        content = client.get("/recordings/archived/").content.decode()
        assert "Archived items" in content
        # The archived Section appears; the split parent Recording does NOT
        # (no parent + child duplicate).
        assert f"/sections/{sections[0].pk}/" in content
        assert f'href="/recordings/{other.pk}/"' in content
        assert f'href="/recordings/{rec.pk}/"' not in content
        # Sibling is not archived, so it is absent.
        assert f"/sections/{sections[1].pk}/" not in content
        assert "Duration" in content and "Type / context" in content

    def test_parent_archived_child_not_duplicated(self, client):
        rec, _t, sections = _active_split("web-sec-table-3")
        archive_section(rec, sections[0])
        # Parent archived AFTER the child: only the recording row appears.
        archive_recording(rec)
        content = client.get("/recordings/archived/").content.decode()
        assert f'href="/recordings/{rec.pk}/"' in content
        assert f"/sections/{sections[0].pk}/" not in content

    def test_global_order_is_newest_archived_first(self, client):
        first, _t1, _s1 = make_transcribed_recording(["one"], sha="web-sec-table-4a")
        second, _t2, _s2 = make_transcribed_recording(["two"], sha="web-sec-table-4b")
        old = timezone.now() - timezone.timedelta(days=2)
        Recording.objects.filter(pk=first.pk).update(archived_at=old)
        Recording.objects.filter(pk=second.pk).update(archived_at=timezone.now())
        content = client.get("/recordings/archived/").content.decode()
        assert content.index(f"/recordings/{second.pk}/") < content.index(
            f"/recordings/{first.pk}/"
        )

    def test_query_count_is_bounded_across_row_count(self, client):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def query_count():
            with CaptureQueriesContext(connection) as ctx:
                client.get("/recordings/archived/")
            return len(ctx.captured_queries)

        a, _t1, _s1 = make_transcribed_recording(["one"], sha="web-sec-table-q1")
        archive_recording(a)
        one = query_count()
        b, _t2, _s2 = make_transcribed_recording(["two"], sha="web-sec-table-q2")
        archive_recording(b)
        two = query_count()
        # Hydration is batched (constant query count), never per-row.
        assert two == one

    def test_limit_sentinel_truncates(self, client, monkeypatch):
        from workflow.views import recordings as recordings_view

        monkeypatch.setattr(recordings_view, "ARCHIVED_LIMIT", 1)
        a, _t1, _s1 = make_transcribed_recording(["one"], sha="web-sec-table-5a")
        b, _t2, _s2 = make_transcribed_recording(["two"], sha="web-sec-table-5b")
        archive_recording(a)
        archive_recording(b)
        content = client.get("/recordings/archived/").content.decode()
        assert "most recently archived items" in content


class TestRecordingDangerZoneCopy:
    def test_concise_copy_and_compact_buttons(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="web-sec-danger")
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert "This hides the recording from the Library, search, Ask and Review." in content
        assert "restore it later" in content
        assert "source-file deletion" not in content
        assert 'class="btn-sm danger-button"' in content
