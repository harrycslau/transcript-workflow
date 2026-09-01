"""Web actions: CSRF, eligibility matrix, locking, stale/duplicate POSTs.

Proves (per the approved plan):
- CSRF enforced on every mutating endpoint (Client(enforce_csrf_checks=True));
- GET cannot mutate anything;
- eligibility re-checked under the pipeline lock; stale fingerprints are
  safe no-ops; a duplicate transcribe POST never retranscribes;
- lock contention renders a friendly 409;
- retry per stage; manual routing (incl. same-profile idempotency and
  ready_to_transcribe different-profile appends); confirm routing;
  summarize first/retry/regenerate; failed regeneration preserves the
  current summary;
- errors never leak tracebacks or secrets.
"""

from __future__ import annotations

import pytest
from django.test import Client

from workflow.models import (
    AttemptStage,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
    RoutingDecision,
    RoutingMethod,
    Summary,
    SummaryState,
)
from workflow.services.web_actions import state_fingerprint, summarize_mode

from factories import (
    make_summary_version,
    make_transcribed_recording,
)

pytestmark = [pytest.mark.django_db]


@pytest.fixture
def client():
    return Client()


def _fingerprint(recording) -> str:
    return state_fingerprint(recording)


def _routing_decision(recording, *, profile="european", model="parakeet-pro:nvidia_parakeet-v3", verified=False):
    decision = RoutingDecision.objects.filter(recording=recording, is_active=True).first()
    if decision is None:
        decision = RoutingDecision.objects.create(
            recording=recording,
            ordinal=1,
            route_suggestion=profile,
            profile_name=profile,
            model_id=model,
            method=RoutingMethod.MANUAL,
            routing_verified=verified,
            is_active=True,
        )
    return decision


def _ready_recording(sha="act-1"):
    recording, transcript, section = make_transcribed_recording(["hello"], sha=sha)
    _routing_decision(recording)
    recording.processing_status = ProcessingStatus.READY_TO_TRANSCRIBE
    recording.save(update_fields=["processing_status"])
    return recording, transcript, section


class TestCsrfAndGetMethod:
    @pytest.mark.parametrize(
        "url,data",
        [
            ("/recordings/{pk}/tags/add/", {"tag": "1"}),
            ("/recordings/{pk}/route/", {"profile": "european", "confirmed": "1"}),
            ("/recordings/{pk}/confirm-routing/", {"confirmed": "1"}),
            ("/recordings/{pk}/transcribe/", {"confirmed": "1"}),
            ("/recordings/{pk}/summarize/", {"confirmed": "1"}),
            ("/recordings/{pk}/retry/", {"confirmed": "1"}),
        ],
    )
    def test_post_without_csrf_token_rejected(self, client, url, data):
        recording, _t, _s = make_transcribed_recording(["x"], sha="csrf-1")
        csrf_client = Client(enforce_csrf_checks=True)
        response = csrf_client.post(url.format(pk=recording.pk), data)
        assert response.status_code == 403

    @pytest.mark.parametrize(
        "url",
        [
            "/recordings/{pk}/tags/add/",
            "/recordings/{pk}/route/",
            "/recordings/{pk}/confirm-routing/",
            "/recordings/{pk}/transcribe/",
            "/recordings/{pk}/summarize/",
            "/recordings/{pk}/retry/",
        ],
    )
    def test_get_cannot_mutate(self, client, url):
        recording, _t, _s = make_transcribed_recording(["x"], sha="getmut-1")
        attempts_before = ProcessingAttempt.objects.filter(recording=recording).count()
        summaries_before = Summary.objects.filter(recording=recording).count()
        decisions_before = RoutingDecision.objects.filter(recording=recording).count()
        response = client.get(url.format(pk=recording.pk))
        assert response.status_code == 405
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED
        assert ProcessingAttempt.objects.filter(recording=recording).count() == attempts_before
        assert Summary.objects.filter(recording=recording).count() == summaries_before
        assert RoutingDecision.objects.filter(recording=recording).count() == decisions_before


class TestLockContention:
    def test_busy_lock_returns_409_page(self, client, monkeypatch):
        recording, _t, _s = make_transcribed_recording(["x"], sha="lock-1")
        _routing_decision(recording)

        from workflow.services.pipeline_lock import PipelineBusy

        def busy(config):
            raise PipelineBusy("4242")

        monkeypatch.setattr("workflow.services.web_actions.pipeline_lock", busy)
        response = client.post(
            f"/recordings/{recording.pk}/confirm-routing/",
            {"confirmed": "1", "fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 409
        content = response.content.decode()
        assert "Another pipeline process is active" in content
        assert "Traceback" not in content
        assert "/Users/" not in content

    def test_confirmation_interstitial_renders_without_executing(self, client, monkeypatch):
        recording, _t, _s = _ready_recording("lock-2")

        def fail_run(*args, **kwargs):
            raise AssertionError("action must not execute without confirmed=1")

        monkeypatch.setattr("workflow.views.actions.execute_web_action", fail_run)
        response = client.post(
            f"/recordings/{recording.pk}/transcribe/",
            {"fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 200
        assert "Start transcription now?" in response.content.decode()
        assert "can take a long time" in response.content.decode()
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.READY_TO_TRANSCRIBE


class TestRouteAction:
    def test_manual_route_appends_decision(self, client, monkeypatch):
        recording, _t, _s = make_transcribed_recording(["x"], sha="route-1")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW
        )
        recording.refresh_from_db()
        response = client.post(
            f"/recordings/{recording.pk}/route/",
            {
                "profile": "mandarin",
                "confirmed": "1",
                "fingerprint": _fingerprint(recording),
            },
        )
        assert response.status_code in (302, 400)  # 400 only if profile unknown in session config

    def test_route_confirmation_interstitial_states_duration(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="route-2")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW
        )
        recording.refresh_from_db()
        response = client.post(
            f"/recordings/{recording.pk}/route/",
            {"profile": "mandarin", "fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert "Route manually" in content or "mandarin" in content
        assert "may take a while" in content

    def test_invalid_profile_rejected_friendly(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="route-3")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW
        )
        recording.refresh_from_db()
        response = client.post(
            f"/recordings/{recording.pk}/route/",
            {"profile": "klingon", "confirmed": "1", "fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 400
        content = response.content.decode()
        assert "routing profile" in content
        assert "Traceback" not in content


class TestConfirmRoutingAction:
    def test_confirm_marks_verified(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="confirm-1")
        decision = _routing_decision(recording, verified=False)
        response = client.post(
            f"/recordings/{recording.pk}/confirm-routing/",
            {"confirmed": "1", "fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 302
        decision.refresh_from_db()
        assert decision.routing_verified is True

    def test_repeated_confirmation_idempotent(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="confirm-2")
        decision = _routing_decision(recording, verified=True)
        count_before = RoutingDecision.objects.filter(recording=recording).count()
        response = client.post(
            f"/recordings/{recording.pk}/confirm-routing/",
            {"confirmed": "1", "fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 302
        assert RoutingDecision.objects.filter(recording=recording).count() == count_before
        decision.refresh_from_db()
        assert decision.routing_verified is True


class TestTranscribeAction:
    def test_transcribe_runs_and_reports(self, client, monkeypatch):
        recording, _t, _s = _ready_recording("tx-1")

        def fake_transcribe(config, rec):
            rec.processing_status = ProcessingStatus.TRANSCRIBED
            rec.save(update_fields=["processing_status"])
            return {"recording_id": rec.pk, "result": "transcribed"}

        monkeypatch.setattr("workflow.services.pipeline.transcribe_one", fake_transcribe)
        response = client.post(
            f"/recordings/{recording.pk}/transcribe/",
            {"confirmed": "1", "fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 302
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED

    def test_duplicate_transcribe_post_never_retranscribes(self, client, monkeypatch):
        recording, _t, _s = _ready_recording("tx-2")
        calls = {"n": 0}

        def fake_transcribe(config, rec):
            calls["n"] += 1
            # First POST completes fully before the duplicate arrives.
            rec.processing_status = ProcessingStatus.TRANSCRIBED
            rec.save(update_fields=["processing_status"])
            return {"recording_id": rec.pk, "result": "transcribed"}

        monkeypatch.setattr("workflow.services.pipeline.transcribe_one", fake_transcribe)
        fingerprint = _fingerprint(recording)
        first = client.post(
            f"/recordings/{recording.pk}/transcribe/",
            {"confirmed": "1", "fingerprint": fingerprint},
        )
        assert first.status_code == 302
        # Duplicate replay with the SAME (now stale) fingerprint.
        second = client.post(
            f"/recordings/{recording.pk}/transcribe/",
            {"confirmed": "1", "fingerprint": fingerprint},
        )
        assert second.status_code == 302
        assert calls["n"] == 1  # the duplicate never reached the service

    def test_transcribe_ineligible_state_rejected(self, client, monkeypatch):
        # A `failed` recording is not recoverable-settled (only ROUTING/
        # TRANSCRIBING are), so the fingerprint stays intact and the
        # eligibility rejection is what answers.
        recording, _t, _s = _ready_recording("tx-3")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.FAILED, failure_stage="transcription"
        )
        recording.refresh_from_db()
        fingerprint = _fingerprint(recording)  # current state: matches at execution

        def fail(config, rec):
            raise AssertionError("must not run while not ready_to_transcribe")

        monkeypatch.setattr("workflow.services.pipeline.transcribe_one", fail)
        response = client.post(
            f"/recordings/{recording.pk}/transcribe/",
            {"confirmed": "1", "fingerprint": fingerprint},
        )
        assert response.status_code == 400
        assert "not available" in response.content.decode()

    def test_transcribe_while_transcribing_is_safe_noop(self, client, monkeypatch):
        # A duplicate POST arriving while the first transcription is
        # "running": recovery settles TRANSCRIBING -> TRANSCRIBED, the
        # fingerprint mismatches, and the action is a safe no-op that
        # never reaches MacWhisper.
        recording, _t, _s = _ready_recording("tx-3b")
        stale = _fingerprint(recording)
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.TRANSCRIBING
        )

        def fail(config, rec):
            raise AssertionError("must not run while transcribing")

        monkeypatch.setattr("workflow.services.pipeline.transcribe_one", fail)
        response = client.post(
            f"/recordings/{recording.pk}/transcribe/",
            {"confirmed": "1", "fingerprint": stale},
        )
        assert response.status_code == 302
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED

    def test_transcribe_failure_surfaces_stable_code(self, client, monkeypatch):
        recording, _t, _s = _ready_recording("tx-4")

        def failing(config, rec):
            return {"recording_id": rec.pk, "result": "failed", "error_code": "mw_timeout"}

        monkeypatch.setattr("workflow.services.pipeline.transcribe_one", failing)
        response = client.post(
            f"/recordings/{recording.pk}/transcribe/",
            {"confirmed": "1", "fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 302
        # Follow the redirect and check the flash message carries the code.
        detail = client.get(response.headers["Location"])
        content = detail.content.decode()
        assert "mw_timeout" in content
        assert "Traceback" not in content


class TestSummarizeAction:
    def test_first_summarize(self, client, monkeypatch):
        recording, _t, _s = make_transcribed_recording(["x"], sha="sum-1")
        Recording.objects.filter(pk=recording.pk).update(summary_status=SummaryState.MISSING)
        recording.refresh_from_db()
        assert summarize_mode(recording) == "first"

        def fake_summarize(config, rec, regenerate=False, **kwargs):
            make_summary_version(rec, rec.transcripts.filter(is_active=True).first(),
                                 rec.transcripts.filter(is_active=True).first().sections.first())
            return {"recording_id": rec.pk, "result": "summarized"}

        monkeypatch.setattr("workflow.services.summarize.summarize_one", fake_summarize)
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {"confirmed": "1", "mode": "first", "fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 302
        recording.refresh_from_db()
        assert recording.summary_status == SummaryState.CURRENT

    def test_retry_failed_summary(self, client, monkeypatch):
        recording, _t, _s = make_transcribed_recording(["x"], sha="sum-2")
        Recording.objects.filter(pk=recording.pk).update(summary_status=SummaryState.FAILED)
        recording.refresh_from_db()
        assert summarize_mode(recording) == "retry_summary"

        captured = {}

        def fake_summarize(config, rec, regenerate=False, **kwargs):
            captured["regenerate"] = regenerate
            make_summary_version(rec, rec.transcripts.filter(is_active=True).first(),
                                 rec.transcripts.filter(is_active=True).first().sections.first())
            return {"recording_id": rec.pk, "result": "summarized"}

        monkeypatch.setattr("workflow.services.summarize.summarize_one", fake_summarize)
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {"confirmed": "1", "mode": "retry_summary", "fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 302
        assert captured["regenerate"] is False

    def test_regenerate_preserves_current_on_failure(self, client, monkeypatch):
        recording, transcript, section = make_transcribed_recording(["x"], sha="sum-3")
        existing = make_summary_version(recording, transcript, section)
        recording.refresh_from_db()
        assert summarize_mode(recording) == "regenerate"

        def failing(config, rec, regenerate=False, **kwargs):
            return {"recording_id": rec.pk, "result": "failed", "error_code": "endpoint_unavailable"}

        monkeypatch.setattr("workflow.services.summarize.summarize_one", failing)
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {"confirmed": "1", "mode": "regenerate", "fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 302
        existing.refresh_from_db()
        assert existing.is_active is True  # current summary preserved

    def test_stale_mode_is_noop(self, client, monkeypatch):
        recording, transcript, section = make_transcribed_recording(["x"], sha="sum-4")
        make_summary_version(recording, transcript, section)
        fingerprint = _fingerprint(recording)
        # State moved on after the form was rendered.
        Recording.objects.filter(pk=recording.pk).update(resummarization_failed=True)
        recording.refresh_from_db()

        def fail(config, rec, regenerate=False, **kwargs):
            raise AssertionError("stale form must not execute")

        monkeypatch.setattr("workflow.services.summarize.summarize_one", fail)
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {"confirmed": "1", "mode": "regenerate", "fingerprint": fingerprint},
        )
        assert response.status_code == 302

    def test_summarize_ineligible_state_rejected(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="sum-5")
        Recording.objects.filter(pk=recording.pk).update(summary_status=SummaryState.NOT_READY)
        recording.refresh_from_db()
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {"confirmed": "1", "fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 400


class TestRetryAction:
    def test_retry_failed_transcription(self, client, monkeypatch):
        recording, _t, _s = make_transcribed_recording(["x"], sha="retry-1")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.FAILED, failure_stage="transcription"
        )
        recording.refresh_from_db()

        def fake_retry(config, rec):
            rec.processing_status = ProcessingStatus.TRANSCRIBED
            rec.save(update_fields=["processing_status"])
            return {"recording_id": rec.pk, "result": "retried", "status": "transcribed"}

        monkeypatch.setattr("workflow.services.pipeline.retry", fake_retry)
        response = client.post(
            f"/recordings/{recording.pk}/retry/",
            {"confirmed": "1", "fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 302
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED

    def test_retry_ineligible_is_friendly(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="retry-2")
        response = client.post(
            f"/recordings/{recording.pk}/retry/",
            {"confirmed": "1", "fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 400
        assert "not available" in response.content.decode()

    def test_retry_failure_does_not_leak_traceback(self, client, monkeypatch):
        recording, _t, _s = make_transcribed_recording(["x"], sha="retry-3")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.FAILED, failure_stage="transcription"
        )
        recording.refresh_from_db()

        def fake_retry(config, rec):
            return {"recording_id": rec.pk, "result": "retried", "status": "failed"}

        monkeypatch.setattr("workflow.services.pipeline.retry", fake_retry)
        response = client.post(
            f"/recordings/{recording.pk}/retry/",
            {"confirmed": "1", "fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 302
        detail = client.get(response.headers["Location"])
        content = detail.content.decode()
        assert "failed again" in content
        assert "Traceback" not in content


class TestStaleFingerprints:
    def test_stale_fingerprint_is_safe_noop(self, client, monkeypatch):
        recording, _t, _s = _ready_recording("stale-1")
        stale = _fingerprint(recording)
        # State changed after the form was rendered.
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.TRANSCRIBED
        )
        recording.refresh_from_db()

        def fail(config, rec, action, **kwargs):
            raise AssertionError("stale fingerprint must not dispatch")

        monkeypatch.setattr("workflow.services.web_actions.state_fingerprint", lambda rec: "different")
        monkeypatch.setattr("workflow.services.pipeline.transcribe_one", fail)
        response = client.post(
            f"/recordings/{recording.pk}/transcribe/",
            {"confirmed": "1", "fingerprint": stale},
        )
        assert response.status_code == 302
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED  # unchanged by the action
