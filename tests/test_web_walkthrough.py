"""Synthetic Step 4 browser walkthrough (plan section 16).

One end-to-end pass over the web UI with the Django test client:
list -> filter -> detail -> transcript pages -> exports -> tag lifecycle
-> suppression survives re-summarization -> mocked retry/regenerate ->
lock conflict -> GET purity. Removed after the run.
"""
import json

import pytest
from django.test import Client

from workflow.models import (
    ProcessingAttempt, ProcessingStatus, Recording, RoutingDecision,
    RoutingMethod, Summary, SummaryState, TagAssignment, TagDeactivatedBy, TagOrigin,
)
from workflow.services.web_actions import state_fingerprint, summarize_mode
from factories import make_summary_version, make_tag, make_transcribed_recording

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


def test_walkthrough():
    c = Client()

    # 1. List + filter by date and tags.
    recording, transcript, section = make_transcribed_recording(["hello world"], sha="walk-1")
    family = make_tag("Family")
    academic = make_tag("Academic")
    for tag in (family, academic):
        TagAssignment.objects.create(recording=recording, tag=tag, origin=TagOrigin.SUGGESTED)
    RoutingDecision.objects.create(
        recording=recording, ordinal=1, route_suggestion="european",
        profile_name="european", model_id="parakeet-pro:nvidia_parakeet-v3",
        method=RoutingMethod.AUTOMATIC, routing_verified=False, is_active=True,
    )
    make_summary_version(recording, transcript, section)
    assert c.get("/recordings/").status_code == 200
    r = c.get("/recordings/?tag=Family&tag=Academic&tag_match=all")
    assert str(recording.pk) in r.content.decode()

    # 2. Detail + paginated transcript.
    detail = c.get(f"/recordings/{recording.pk}/")
    assert detail.status_code == 200
    long_texts = [f"seg {i}" for i in range(450)]
    long_rec, long_t, _ = make_transcribed_recording(long_texts, sha="walk-long")
    page = c.get(f"/recordings/{long_rec.pk}/transcript/?page=2")
    assert "seg 200" in page.content.decode() and "seg 0<" not in page.content.decode()

    # 3. Exports (markdown + json + transcript).
    md = c.get(f"/recordings/{recording.pk}/summary/export/?format=markdown")
    assert md.status_code == 200 and md["Content-Type"].startswith("text/markdown")
    js = c.get(f"/recordings/{recording.pk}/summary/export/?format=json")
    payload = json.loads(js.content.decode())
    assert payload["is_current_for_recording"] is True
    assert payload["is_active_in_scope"] is True
    assert c.get(f"/recordings/{recording.pk}/transcript/export/?format=timestamped").status_code == 200

    # 4. Tag lifecycle: confirm -> remove (suppress) -> suggestion stays suppressed.
    c.post(f"/recordings/{recording.pk}/tags/{family.pk}/confirm/", {})
    assert TagAssignment.objects.get(recording=recording, tag=family).origin == TagOrigin.CONFIRMED
    c.post(f"/recordings/{recording.pk}/tags/{academic.pk}/remove/", {})
    suppressed = TagAssignment.objects.get(recording=recording, tag=academic)
    assert suppressed.deactivated_by == TagDeactivatedBy.USER

    # 5. Synthetic re-summarization suggests the suppressed tag again.
    from workflow.services.summarize import _materialize_tags
    from workflow.models import SummaryTagSuggestion
    from django.utils import timezone as tz
    Summary.objects.filter(transcript=transcript, section=section, is_active=True).update(
        is_active=False, superseded_at=tz.now())
    new_summary = make_summary_version(recording, transcript, section, title="Walk v2")
    _materialize_tags(recording, new_summary, [family, academic])
    assert SummaryTagSuggestion.objects.filter(summary=new_summary, tag=academic).count() == 1
    assert TagAssignment.objects.get(recording=recording, tag=academic).deactivated_by == TagDeactivatedBy.USER
    assert TagAssignment.objects.get(recording=recording, tag=academic).is_active is False
    assert TagAssignment.objects.get(recording=recording, tag=family).is_active is True  # confirmed survives

    # 6. Retry/regenerate with mocked services.
    from workflow.services.web_actions import ActionOutcome
    Recording.objects.filter(pk=recording.pk).update(resummarization_failed=True)
    assert summarize_mode(recording) == "regenerate"
    fp = state_fingerprint(recording)
    from workflow.views import actions as actions_mod
    real_exec = actions_mod.execute_web_action
    seen = {}
    def fake_exec(config, rec, action, **kwargs):
        seen["action"] = action
        return ActionOutcome(ok=True, result="summarized", message="Summary generated.")
    actions_mod.execute_web_action = fake_exec
    try:
        r = c.post(f"/recordings/{recording.pk}/summarize/",
                   {"confirmed": "1", "mode": "regenerate", "fingerprint": fp})
    finally:
        actions_mod.execute_web_action = real_exec
    assert r.status_code == 302 and seen["action"] == "summarize"

    # 7. Lock conflict -> 409.
    from workflow.services.pipeline_lock import PipelineBusy
    real_lock = __import__("workflow.services.web_actions", fromlist=["pipeline_lock"]).pipeline_lock
    def busy(config):
        raise PipelineBusy("999")
    import workflow.services.web_actions as wa
    wa.pipeline_lock = busy
    try:
        r = c.post(f"/recordings/{recording.pk}/confirm-routing/",
                   {"confirmed": "1", "fingerprint": fp})
    finally:
        wa.pipeline_lock = real_lock
    assert r.status_code == 409

    # 8. GET purity: no writes from any read page.
    before = (Recording.objects.count(), ProcessingAttempt.objects.count(),
              TagAssignment.objects.count(), Summary.objects.count())
    for url in ("/", "/recordings/", "/tags/", "/review/",
                f"/recordings/{recording.pk}/", f"/recordings/{recording.pk}/history/"):
        assert c.get(url).status_code == 200
    after = (Recording.objects.count(), ProcessingAttempt.objects.count(),
             TagAssignment.objects.count(), Summary.objects.count())
    assert before == after
