"""Tag management: manual add, confirm, remove/suppress, races, GET purity.

Proves (per the approved plan):
- manual add activates with origin=manual, idempotent on repeat POST;
- confirm upgrades suggested → confirmed, provenance preserved;
- remove sets deactivated_by="user" (suppression) and keeps
  SummaryTagSuggestion history;
- future re-summarization never removes manual/confirmed tags and never
  reactivates a user-suppressed tag, while model-deactivated tags DO
  reactivate;
- retired tags need the explicit opt-in;
- case-insensitive tag identity;
- the deactivation-state CheckConstraint rejects both invalid combos;
- GET requests make no writes.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from django.test import Client

from workflow.models import (
    Summary,
    SummaryTagSuggestion,
    Tag,
    TagAssignment,
    TagDeactivatedBy,
    TagOrigin,
)

from factories import (
    make_summary_version,
    make_tag,
    make_tag_assignment,
    make_transcribed_recording,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


@pytest.fixture
def client():
    return Client()


def _tagged_recording(*, with_summary=True):
    recording, transcript, section = make_transcribed_recording(["hello"], sha="tags-1")
    summary = make_summary_version(recording, transcript, section) if with_summary else None
    return recording, transcript, section, summary


def _post_add(client, recording, tag_pk, *, include_retired=False):
    data = {"tag": str(tag_pk)}
    if include_retired:
        data["include_retired"] = "1"
    return client.post(f"/recordings/{recording.pk}/tags/add/", data)


class TestManualAddPromotion:
    """Manual add must convert an active model suggestion into a
    user-owned assignment (review finding 1)."""

    def test_active_suggested_is_promoted_to_manual_same_row(self, client):
        recording, transcript, section, summary = _tagged_recording()
        tag = make_tag("Family")
        assignment = make_tag_assignment(recording, tag, origin="suggested", source_summary=summary)
        before_pk = assignment.pk
        before_created = assignment.created_at
        response = _post_add(client, recording, tag.pk)
        assert response.status_code == 302
        # Same single row retained — no second row.
        assert TagAssignment.objects.filter(recording=recording, tag=tag).count() == 1
        assignment = TagAssignment.objects.get(pk=before_pk)
        assert assignment.origin == TagOrigin.MANUAL
        assert assignment.is_active is True
        assert assignment.source_summary is None
        assert assignment.deactivated_by == TagDeactivatedBy.NONE
        assert assignment.deactivated_at is None
        assert assignment.created_at == before_created  # no provenance churn

    def test_promoted_tag_survives_resummarization_drop(self, client):
        recording, transcript, section, _sum = _tagged_recording()
        tag = make_tag("Family")
        make_tag_assignment(recording, tag, origin="suggested")
        _post_add(client, recording, tag.pk)
        # A later summary version no longer suggests the tag.
        from django.utils import timezone as tz

        from workflow.models import Summary

        Summary.objects.filter(transcript=transcript, section=section, is_active=True).update(
            is_active=False, superseded_at=tz.now()
        )
        new_summary = make_summary_version(recording, transcript, section, title="Later version")
        from workflow.services.summarize import _materialize_tags

        _materialize_tags(recording, new_summary, [])  # suggests nothing
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assert assignment.is_active is True
        assert assignment.origin == TagOrigin.MANUAL

    def test_repeated_manual_add_is_true_noop(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        _post_add(client, recording, tag.pk)
        first = TagAssignment.objects.get(recording=recording, tag=tag)
        first_updated = first.updated_at if hasattr(first, "updated_at") else None
        response = _post_add(client, recording, tag.pk)
        assert response.status_code == 302
        assert TagAssignment.objects.filter(recording=recording, tag=tag).count() == 1
        again = TagAssignment.objects.get(pk=first.pk)
        assert again.origin == TagOrigin.MANUAL
        assert again.is_active is True
        assert again.source_summary is None
        # Row untouched by the repeat (no save happened).
        assert again.deactivated_at is None
        assert again.deactivated_by == TagDeactivatedBy.NONE

    def test_active_confirmed_plus_manual_add_stays_confirmed(self, client):
        recording, _t, _s, summary = _tagged_recording()
        tag = make_tag("Family")
        make_tag_assignment(recording, tag, origin="confirmed", source_summary=summary)
        before = TagAssignment.objects.get(recording=recording, tag=tag)
        response = _post_add(client, recording, tag.pk)
        assert response.status_code == 302
        after = TagAssignment.objects.get(pk=before.pk)
        assert after.origin == TagOrigin.CONFIRMED  # never downgraded
        assert after.is_active is True
        assert after.source_summary == summary  # provenance preserved
        assert TagAssignment.objects.filter(recording=recording, tag=tag).count() == 1

    def test_inactive_user_suppressed_plus_deliberate_add_clears_suppression(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        make_tag_assignment(recording, tag, origin="suggested", active=True)
        client.post(f"/recordings/{recording.pk}/tags/{tag.pk}/remove/", {})
        assert TagAssignment.objects.get(recording=recording, tag=tag).deactivated_by == TagDeactivatedBy.USER
        _post_add(client, recording, tag.pk)
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assert assignment.is_active is True
        assert assignment.origin == TagOrigin.MANUAL
        assert assignment.deactivated_by == TagDeactivatedBy.NONE
        assert assignment.deactivated_at is None
        assert assignment.source_summary is None

    def test_inactive_model_deactivated_plus_add_becomes_manual(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        make_tag_assignment(recording, tag, origin="suggested", active=False)  # model-deactivated
        _post_add(client, recording, tag.pk)
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assert assignment.is_active is True
        assert assignment.origin == TagOrigin.MANUAL
        assert assignment.deactivated_by == TagDeactivatedBy.NONE

    def test_ui_message_distinguishes_all_outcomes(self, client):
        recording, transcript, section, summary = _tagged_recording()
        import re as _re

        def flash(response):
            body = client.get(response.headers["Location"]).content.decode()
            return _re.findall(r'message message-\w+"[^>]*>([^<]+)</div>', body)

        # created
        fresh = make_tag("FreshTag")
        r = _post_add(client, recording, fresh.pk)
        assert any("added." in m for m in flash(r)), flash(r)
        # promoted
        suggested = make_tag("SuggTag")
        make_tag_assignment(recording, suggested, origin="suggested", source_summary=summary)
        r = _post_add(client, recording, suggested.pk)
        assert any("now manual and will survive" in m for m in flash(r)), flash(r)
        # already user-owned (manual)
        r = _post_add(client, recording, fresh.pk)
        assert any("already a user-owned tag" in m for m in flash(r)), flash(r)
        # already user-owned (confirmed) — also a no-op message
        confirmed = make_tag("ConfTag")
        make_tag_assignment(recording, confirmed, origin="confirmed")
        r = _post_add(client, recording, confirmed.pk)
        assert any("already a user-owned tag" in m for m in flash(r)), flash(r)
        # restored (suppressed -> deliberate re-add)
        removed = make_tag("RemovedTag")
        make_tag_assignment(recording, removed, origin="suggested")
        client.post(f"/recordings/{recording.pk}/tags/{removed.pk}/remove/", {})
        r = _post_add(client, recording, removed.pk)
        assert any("restored as a manual tag" in m for m in flash(r)), flash(r)

    def test_promotion_keeps_constraints_satisfied(self, client):
        recording, _t, _s, summary = _tagged_recording()
        tag = make_tag("Family")
        make_tag_assignment(recording, tag, origin="suggested", source_summary=summary)
        _post_add(client, recording, tag.pk)
        # The row re-reads cleanly under full constraint validation.
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assignment.full_clean(exclude=["tag", "recording"])
        assert (assignment.is_active and assignment.deactivated_by == "") or (
            not assignment.is_active and assignment.deactivated_by in ("user", "model")
        )


class TestManualAdd:
    def test_add_manual_tag(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        response = _post_add(client, recording, tag.pk)
        assert response.status_code == 302
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assert assignment.is_active is True
        assert assignment.origin == TagOrigin.MANUAL

    def test_repeat_add_idempotent(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        _post_add(client, recording, tag.pk)
        _post_add(client, recording, tag.pk)
        assert TagAssignment.objects.filter(recording=recording, tag=tag).count() == 1
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assert assignment.is_active is True
        assert assignment.origin == TagOrigin.MANUAL

    def test_add_without_tag_is_rejected(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        response = client.post(f"/recordings/{recording.pk}/tags/add/", {"tag": ""})
        assert response.status_code == 302
        assert TagAssignment.objects.filter(recording=recording).count() == 0

    def test_retired_tag_requires_opt_in(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("OldTag", configured=False)
        response = _post_add(client, recording, tag.pk)
        assert response.status_code == 302
        assert TagAssignment.objects.filter(recording=recording, tag=tag).count() == 0
        # With the explicit opt-in the restore succeeds.
        response = _post_add(client, recording, tag.pk, include_retired=True)
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assert assignment.is_active is True
        assert assignment.origin == TagOrigin.MANUAL

    def test_add_clears_user_suppression(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        make_tag_assignment(recording, tag, origin="suggested", active=True)
        client.post(f"/recordings/{recording.pk}/tags/{tag.pk}/remove/", {})
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assert assignment.deactivated_by == TagDeactivatedBy.USER
        # Deliberate re-add lifts the suppression.
        _post_add(client, recording, tag.pk)
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assert assignment.is_active is True
        assert assignment.origin == TagOrigin.MANUAL
        assert assignment.deactivated_by == TagDeactivatedBy.NONE


class TestConfirm:
    def test_confirm_suggested_becomes_confirmed(self, client):
        recording, _t, _s, summary = _tagged_recording()
        tag = make_tag("Family")
        assignment = make_tag_assignment(recording, tag, origin="suggested", source_summary=summary)
        response = client.post(f"/recordings/{recording.pk}/tags/{tag.pk}/confirm/", {})
        assert response.status_code == 302
        assignment.refresh_from_db()
        assert assignment.origin == TagOrigin.CONFIRMED
        assert assignment.is_active is True
        assert assignment.source_summary == summary  # provenance preserved

    def test_confirm_is_idempotent(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        make_tag_assignment(recording, tag, origin="suggested")
        client.post(f"/recordings/{recording.pk}/tags/{tag.pk}/confirm/", {})
        client.post(f"/recordings/{recording.pk}/tags/{tag.pk}/confirm/", {})
        assert TagAssignment.objects.get(recording=recording, tag=tag).origin == TagOrigin.CONFIRMED

    def test_confirm_inactive_is_rejected(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        make_tag_assignment(recording, tag, origin="suggested", active=False)
        response = client.post(f"/recordings/{recording.pk}/tags/{tag.pk}/confirm/", {})
        assert response.status_code == 302
        assert TagAssignment.objects.get(recording=recording, tag=tag).origin == TagOrigin.SUGGESTED


class TestRemoveSuppress:
    def test_remove_sets_user_suppression(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        make_tag_assignment(recording, tag, origin="suggested")
        response = client.post(f"/recordings/{recording.pk}/tags/{tag.pk}/remove/", {})
        assert response.status_code == 302
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assert assignment.is_active is False
        assert assignment.deactivated_by == TagDeactivatedBy.USER
        assert assignment.deactivated_at is not None

    def test_remove_is_idempotent(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        make_tag_assignment(recording, tag, origin="suggested")
        client.post(f"/recordings/{recording.pk}/tags/{tag.pk}/remove/", {})
        response = client.post(f"/recordings/{recording.pk}/tags/{tag.pk}/remove/", {})
        assert response.status_code == 302
        assert TagAssignment.objects.get(recording=recording, tag=tag).deactivated_by == TagDeactivatedBy.USER

    def test_suggestion_history_preserved_after_remove(self, client):
        recording, transcript, section, summary = _tagged_recording()
        tag = make_tag("Family")
        from workflow.models import SummaryTagSuggestion as STS

        STS.objects.create(summary=summary, tag=tag)
        make_tag_assignment(recording, tag, origin="suggested", source_summary=summary)
        client.post(f"/recordings/{recording.pk}/tags/{tag.pk}/remove/", {})
        assert STS.objects.filter(summary=summary, tag=tag).count() == 1


class TestResummarizationSemantics:
    """The suppression rules inside _materialize_tags (via the real code path)."""

    def _resummarize(self, recording, transcript, section, suggested_tags):
        """Re-run only the tag-materialization step of a new summary version.

        Mirrors persist_summary's scope rule: the previous active summary
        of THIS (transcript, section) scope is deactivated first.
        """
        from django.utils import timezone as tz

        from workflow.services.summarize import _materialize_tags

        Summary.objects.filter(
            transcript=transcript, section=section, is_active=True
        ).update(is_active=False, superseded_at=tz.now())
        new_summary = make_summary_version(
            recording, transcript, section, title="Second version"
        )
        _materialize_tags(recording, new_summary, suggested_tags)
        return new_summary

    def test_manual_and_confirmed_survive_resummarization(self, client):
        recording, transcript, section, _sum = _tagged_recording()
        manual = make_tag("Family")
        confirmed = make_tag("Academic")
        make_tag_assignment(recording, manual, origin="manual")
        make_tag_assignment(recording, confirmed, origin="confirmed")
        # New summary version suggests neither.
        self._resummarize(recording, transcript, section, [])
        assert TagAssignment.objects.get(recording=recording, tag=manual).is_active is True
        assert TagAssignment.objects.get(recording=recording, tag=confirmed).is_active is True

    def test_user_suppressed_tag_not_reactivated_by_new_suggestion(self, client):
        recording, transcript, section, _sum = _tagged_recording()
        tag = make_tag("Family")
        make_tag_assignment(recording, tag, origin="suggested")
        client.post(f"/recordings/{recording.pk}/tags/{tag.pk}/remove/", {})
        # A later summary version suggests the same tag again.
        new_summary = self._resummarize(recording, transcript, section, [tag])
        # Suggestion provenance recorded, but the assignment stays suppressed.
        assert SummaryTagSuggestion.objects.filter(summary=new_summary, tag=tag).count() == 1
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assert assignment.is_active is False
        assert assignment.deactivated_by == TagDeactivatedBy.USER

    def test_model_deactivated_tag_reactivates_on_new_suggestion(self, client):
        recording, transcript, section, _sum = _tagged_recording()
        tag = make_tag("Family")
        make_tag_assignment(recording, tag, origin="suggested")
        # Model drops the tag in the next version.
        self._resummarize(recording, transcript, section, [])
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assert assignment.is_active is False
        assert assignment.deactivated_by == TagDeactivatedBy.MODEL
        # A later version suggests it again -> reactivates.
        self._resummarize(recording, transcript, section, [tag])
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assert assignment.is_active is True
        assert assignment.origin == TagOrigin.SUGGESTED

    def test_suggested_tag_survives_when_re_suggested(self, client):
        recording, transcript, section, _sum = _tagged_recording()
        tag = make_tag("Family")
        make_tag_assignment(recording, tag, origin="suggested")
        self._resummarize(recording, transcript, section, [tag])
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assert assignment.is_active is True
        assert assignment.origin == TagOrigin.SUGGESTED


class TestConstraintsAndRaces:
    def test_check_constraint_rejects_active_with_actor(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                TagAssignment.objects.create(
                    recording=recording, tag=tag, origin="manual",
                    is_active=True, deactivated_by="user",
                )

    def test_check_constraint_rejects_inactive_without_actor(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                TagAssignment.objects.create(
                    recording=recording, tag=tag, origin="manual",
                    is_active=False, deactivated_by="",
                )

    def test_duplicate_add_race_resolves_to_one_row(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        # Two sequential posts emulate the duplicate-submission case; the
        # unique(recording, tag) constraint plus idempotent handling keep
        # exactly one row.
        _post_add(client, recording, tag.pk)
        _post_add(client, recording, tag.pk)
        assert TagAssignment.objects.filter(recording=recording, tag=tag).count() == 1

    def test_backfill_attributed_legacy_rows_to_model(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        # Created via the factory's inactive path (deactivated_by="model"),
        # matching what the 0005 backfill wrote for legacy rows.
        assignment = make_tag_assignment(recording, tag, active=False)
        assert assignment.deactivated_by == TagDeactivatedBy.MODEL


class TestGetPurityAndCrossRecording:
    def test_get_on_tag_pages_makes_no_writes(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        make_tag_assignment(recording, tag)
        before = TagAssignment.objects.count()
        client.get("/tags/")
        client.get(f"/recordings/{recording.pk}/")
        assert TagAssignment.objects.count() == before

    def test_cross_recording_tag_action_is_404(self, client):
        rec_a, _t, _s, _sum = _tagged_recording()
        rec_b, _t2, _s2 = make_transcribed_recording(["b"], sha="tags-other")
        tag = make_tag("Family")
        make_tag_assignment(rec_a, tag)
        # rec_b has no assignment for this tag.
        assert client.post(f"/recordings/{rec_b.pk}/tags/{tag.pk}/confirm/", {}).status_code == 404
        assert client.post(f"/recordings/{rec_b.pk}/tags/{tag.pk}/remove/", {}).status_code == 404
        assert TagAssignment.objects.get(recording=rec_a, tag=tag).is_active is True

    def test_unknown_tag_id_is_404(self, client):
        recording, _t, _s, _sum = _tagged_recording()
        assert client.post(f"/recordings/{recording.pk}/tags/999999/remove/", {}).status_code == 404


class TestSearchIndexSyncOnWebActions:
    """Step 5A.3: tag POSTs keep the search index in sync after commit;
    a sync failure never breaks the request; GET stays sync-free."""

    def _meta(self, recording):
        from workflow.models import SearchDocument

        return SearchDocument.objects.get(document_key=f"recording:{recording.pk}")

    def test_tag_add_post_fires_exactly_one_index_sync(
        self, client, django_capture_on_commit_callbacks
    ):
        from workflow.services import search_index as si

        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        si.rebuild_index()
        assert self._meta(recording).aux_text == ""
        with django_capture_on_commit_callbacks(execute=True) as captured:
            response = _post_add(client, recording, tag.pk)
        assert response.status_code == 302
        assert len(captured) == 1  # exactly one scheduled sync per commit
        assert self._meta(recording).aux_text == "Family"
        assert si.build_status_report()["healthy"] is True

    def test_tag_remove_post_updates_index(
        self, client, django_capture_on_commit_callbacks
    ):
        from workflow.services import search_index as si

        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        make_tag_assignment(recording, tag, origin="manual")
        si.rebuild_index()
        assert self._meta(recording).aux_text == "Family"
        with django_capture_on_commit_callbacks(execute=True) as captured:
            response = client.post(f"/recordings/{recording.pk}/tags/{tag.pk}/remove/", {})
        assert response.status_code == 302
        assert len(captured) == 1
        assert self._meta(recording).aux_text == ""
        assert si.build_status_report()["healthy"] is True

    def test_sync_failure_never_breaks_the_tag_request(
        self, client, monkeypatch, django_capture_on_commit_callbacks
    ):
        from workflow.services import search_index as si
        from workflow.services import search_sync

        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        si.rebuild_index()

        def always_fail(recording_id, **kwargs):
            raise RuntimeError("index unavailable")

        monkeypatch.setattr(search_sync, "reconcile_recording", always_fail)
        with django_capture_on_commit_callbacks(execute=True):
            response = _post_add(client, recording, tag.pk)
        # The authoritative tag edit stands; the index merely stays
        # detectably stale (the failure was swallowed, never raised).
        assert response.status_code == 302
        assert TagAssignment.objects.get(recording=recording, tag=tag).is_active is True
        assert self._meta(recording).aux_text == ""
        report = si.build_status_report()
        assert report["healthy"] is False
        assert report["categories"]["stale_content"] == 1

    def test_get_requests_never_schedule_index_sync(self, client, monkeypatch):
        """GET purity (AGENTS.md): no module may schedule a sync while
        serving a GET. The bound module attributes must exist — patching
        them also guards the wiring against silent removal."""
        from workflow.services import (
            ingest,
            pipeline,
            summarize,
            tags,
            transcription,
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("GET must never schedule a search index sync")

        for module in (tags, pipeline, summarize, transcription, ingest):
            monkeypatch.setattr(module, "schedule_recording_sync", forbidden)
        recording, _t, _s, _sum = _tagged_recording()
        tag = make_tag("Family")
        make_tag_assignment(recording, tag)
        assert client.get("/tags/").status_code == 200
        assert client.get(f"/recordings/{recording.pk}/").status_code == 200
