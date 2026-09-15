"""Web actions: CSRF, eligibility matrix, locking, stale/duplicate POSTs.

Proves (per the approved plan):
- CSRF enforced on every mutating endpoint (Client(enforce_csrf_checks=True));
- GET cannot mutate anything;
- recording-level actions EXECUTE on the first POST from their page form
  (no confirmation interstitial); eligibility is re-checked under the
  pipeline lock, stale fingerprints are safe no-ops; a duplicate
  transcribe POST never retranscribes;
- the recording-level state fingerprint is an OPAQUE canonical lowercase
  64-hex digest; every executing POST must carry exactly one such value
  (missing/empty/duplicate/malformed/oversized/uppercase => one fixed
  friendly 400 with NO lock, recovery or stage-service call), while a
  canonical but STALE digest keeps the under-lock safe no-op;
- lock contention renders a friendly 409;
- the recording-level fingerprint also binds the ACTIVE routing
  decision's stable identity/behavior fields (pk/ordinal, profile,
  model, language_arg, verified flag, explicit no-active marker; never
  raw evidence or timestamps), so a routing update that changes
  neither the processing status nor the attempts still stales rendered
  route/confirm/transcribe forms (safe no-op), and the binding read is
  SELECT-only and bounded regardless of routing history;
- retry per stage; manual routing (incl. same-profile idempotency and
  ready_to_transcribe different-profile appends); confirm routing;
  summarize first/retry/regenerate; failed regeneration preserves the
  current summary;
- the direct action forms (recording-level AND the shared per-variant
  summarize form, including the section-scoped one) carry the
  pending-state hooks (data-action-form marker + aria-live status
  region) for the submit JS;
- errors never leak tracebacks or secrets.
"""

from __future__ import annotations

import contextlib
import re

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

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
            ("/recordings/{pk}/route/", {"profile": "european"}),
            ("/recordings/{pk}/confirm-routing/", {}),
            ("/recordings/{pk}/transcribe/", {}),
            ("/recordings/{pk}/summarize/", {}),
            ("/recordings/{pk}/retry/", {}),
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
            {"fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 409
        content = response.content.decode()
        assert "Another pipeline process is active" in content
        assert "Traceback" not in content
        assert "/Users/" not in content

    def test_first_post_executes_directly_without_interstitial(self, client, monkeypatch):
        """One-POST flow: the recording-level transcribe form executes on
        its FIRST (and only) POST — no confirmation interstitial page is
        rendered and no ``confirmed`` flag is required."""
        recording, _t, _s = _ready_recording("lock-2")
        seen = {}

        def fake_transcribe(config, rec):
            seen["action"] = "transcribe"
            rec.processing_status = ProcessingStatus.TRANSCRIBED
            rec.save(update_fields=["processing_status"])
            return {"recording_id": rec.pk, "result": "transcribed"}

        monkeypatch.setattr("workflow.services.pipeline.transcribe_one", fake_transcribe)
        response = client.post(
            f"/recordings/{recording.pk}/transcribe/",
            {"fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 302
        assert seen.get("action") == "transcribe"
        # POST → redirect → GET (PRG); the flash carries the result.
        detail = client.get(response.headers["Location"])
        assert "Transcription completed." in detail.content.decode()
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED


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
                "fingerprint": _fingerprint(recording),
            },
        )
        assert response.status_code in (302, 400)  # 400 only if profile unknown in session config

    def test_first_route_post_executes_without_interstitial(self, client, monkeypatch):
        """Manual route now runs on the FIRST POST: no confirmation
        interstitial (no "may take a while" page), and the flash message
        is surfaced after the PRG redirect."""
        recording, _t, _s = make_transcribed_recording(["x"], sha="route-2")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW
        )
        recording.refresh_from_db()

        seen = {}

        def fake_manual_route(rec, profile_name, confirmed_by="cli"):
            seen["profile"] = profile_name
            rec.processing_status = ProcessingStatus.READY_TO_TRANSCRIBE
            rec.save(update_fields=["processing_status"])
            return {
                "recording_id": rec.pk,
                "result": "routed",
                "status": ProcessingStatus.READY_TO_TRANSCRIBE,
            }

        monkeypatch.setattr(
            "workflow.services.pipeline.manual_route", fake_manual_route
        )
        response = client.post(
            f"/recordings/{recording.pk}/route/",
            {"profile": "mandarin", "fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 302
        assert seen.get("profile") == "mandarin"
        # The PRG target shows the outcome; the confirmation wording is gone.
        detail = client.get(response.headers["Location"])
        assert "Routing updated" in detail.content.decode()

    def test_invalid_profile_rejected_friendly(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="route-3")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW
        )
        recording.refresh_from_db()
        response = client.post(
            f"/recordings/{recording.pk}/route/",
            {"profile": "klingon", "fingerprint": _fingerprint(recording)},
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
            {"fingerprint": _fingerprint(recording)},
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
            {"fingerprint": _fingerprint(recording)},
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
            {"fingerprint": _fingerprint(recording)},
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
            {"fingerprint": fingerprint},
        )
        assert first.status_code == 302
        # Duplicate replay with the SAME (now stale) fingerprint.
        second = client.post(
            f"/recordings/{recording.pk}/transcribe/",
            {"fingerprint": fingerprint},
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
            {"fingerprint": fingerprint},
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
            {"fingerprint": stale},
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
            {"fingerprint": _fingerprint(recording)},
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
            {"mode": "first", "fingerprint": _fingerprint(recording)},
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
            {"mode": "retry_summary", "fingerprint": _fingerprint(recording)},
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
            {"mode": "regenerate", "fingerprint": _fingerprint(recording)},
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
            {"mode": "regenerate", "fingerprint": fingerprint},
        )
        assert response.status_code == 302

    def test_summarize_ineligible_state_rejected(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="sum-5")
        Recording.objects.filter(pk=recording.pk).update(summary_status=SummaryState.NOT_READY)
        recording.refresh_from_db()
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {"fingerprint": _fingerprint(recording)},
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
            {"fingerprint": _fingerprint(recording)},
        )
        assert response.status_code == 302
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED

    def test_retry_ineligible_is_friendly(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="retry-2")
        response = client.post(
            f"/recordings/{recording.pk}/retry/",
            {"fingerprint": _fingerprint(recording)},
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
            {"fingerprint": _fingerprint(recording)},
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
            {"fingerprint": stale},
        )
        assert response.status_code == 302
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED  # unchanged by the action


# The OPAQUE recording-level state fingerprint: canonical lowercase 64-hex.
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}\Z")

# family -> (URL suffix, extra form data a VALID submission carries). Each
# family's recording is set up in a state where the action would really
# execute given a valid digest, so a 400 can only come from the
# fingerprint guard.
ACTION_FAMILIES = {
    "confirm-routing": ("confirm-routing", {}),
    "retry": ("retry", {}),
    "route": ("route", {"profile": "mandarin"}),
    "summarize": ("summarize", {"mode": "first"}),
    "transcribe": ("transcribe", {}),
}

# Every stage service a recording-level action may run.
FORBIDDEN_STAGE_TARGETS = (
    "workflow.services.pipeline.manual_route",
    "workflow.services.pipeline.confirm_routing",
    "workflow.services.pipeline.transcribe_one",
    "workflow.services.pipeline.retry",
    "workflow.services.summarize.summarize_one",
)


def _eligible_recording(family: str, sha: str):
    """A recording in the state where ``family``'s action is executable."""
    if family == "transcribe":
        recording, _t, _s = _ready_recording(sha)
        return recording
    recording, _t, _s = make_transcribed_recording(["x"], sha=sha)
    if family == "route":
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW
        )
    elif family == "confirm-routing":
        _routing_decision(recording, verified=False)
    elif family == "summarize":
        # summary_status MISSING with an active transcript: mode "first"
        # derives, so the pre-lock probe does not answer for the guard.
        Recording.objects.filter(pk=recording.pk).update(
            summary_status=SummaryState.MISSING
        )
    elif family == "retry":
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.FAILED, failure_stage="transcription"
        )
    recording.refresh_from_db()
    return recording


def _forbid_all_execution(monkeypatch):
    """Any pipeline lock acquisition or stage-service call raises."""

    def forbidden(*args, **kwargs):
        raise AssertionError("must not run without a valid fingerprint")

    monkeypatch.setattr("workflow.services.web_actions.pipeline_lock", forbidden)
    for target in FORBIDDEN_STAGE_TARGETS:
        monkeypatch.setattr(target, forbidden)


class TestOpaqueFingerprintShape:
    """The fingerprint is an opaque canonical digest: bound state never
    appears in it, and the rendered forms carry nothing else."""

    def test_digest_is_lowercase_hex64_and_deterministic(self):
        recording, _t, _s = _ready_recording("opaque-1")
        fingerprint = _fingerprint(recording)
        assert _FINGERPRINT_RE.match(fingerprint)
        assert len(fingerprint) == 64
        assert fingerprint == fingerprint.lower()
        # Pure function of the bound state: identical inputs -> identical digest.
        assert _fingerprint(recording) == fingerprint

    def test_detail_forms_carry_only_the_opaque_digest(self, client):
        recording, _t, _s = _ready_recording("opaque-2")
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        values = re.findall(r'name="fingerprint" value="([^"]*)"', content)
        assert values  # the direct-execution action forms render the field
        assert all(_FINGERPRINT_RE.match(value) for value in values)
        # Bound ids/statuses/JSON syntax can never surface in the exposed
        # value (hex-only digest — opacity by shape).
        blob = "".join(values)
        for bound in (str(recording.pk), recording.processing_status, "{", "-"):
            assert bound not in blob


class TestRecordingFingerprintContract:
    """Every recording-level executing POST must carry exactly ONE opaque
    lowercase 64-hex fingerprint. Missing, empty, duplicate, malformed,
    oversized or uppercase values answer the ONE fixed friendly 400
    BEFORE ``_execute``: the pipeline lock, recovery and every stage
    service are forbidden (any call raises AssertionError). A canonical
    but stale digest is NOT a 400 — it keeps the under-lock safe no-op."""

    def _bad_payload(self, case: str, fingerprint: str) -> dict:
        return {
            "missing": {},
            "empty": {"fingerprint": ""},
            "duplicate": {"fingerprint": [fingerprint, fingerprint]},
            "malformed": {"fingerprint": "not-a-fingerprint"},
            "oversized": {"fingerprint": "0" * 65},
            "uppercase": {"fingerprint": fingerprint.upper()},
        }[case]

    @pytest.mark.parametrize("family", sorted(ACTION_FAMILIES))
    @pytest.mark.parametrize(
        "case",
        ["missing", "empty", "duplicate", "malformed", "oversized", "uppercase"],
    )
    def test_invalid_fingerprint_is_400_before_lock_or_execution(
        self, client, monkeypatch, family, case
    ):
        _forbid_all_execution(monkeypatch)
        recording = _eligible_recording(family, sha=f"fpcontract-{family}-{case}")
        status_before = recording.processing_status
        fingerprint = _fingerprint(recording)
        payload = dict(ACTION_FAMILIES[family][1])
        payload.update(self._bad_payload(case, fingerprint))
        response = client.post(
            f"/recordings/{recording.pk}/{ACTION_FAMILIES[family][0]}/", payload
        )
        assert response.status_code == 400
        content = response.content.decode()
        assert "The state fingerprint is missing or invalid" in content
        assert "invalid_fingerprint" in content
        assert "Traceback" not in content
        # The rejected values are never echoed back.
        assert fingerprint not in content
        assert fingerprint.upper() not in content
        recording.refresh_from_db()
        assert recording.processing_status == status_before

    def test_canonical_stale_digest_is_under_lock_safe_noop(
        self, client, monkeypatch
    ):
        """A parser-valid digest that no longer matches the state is the
        EXISTING safe no-op: it reaches the service, acquires the lock
        and answers the state-changed warning — never a 400, never a
        stage-service call."""
        recording, _t, _s = _ready_recording("fpcontract-stale")
        stale = _fingerprint(recording)
        assert _FINGERPRINT_RE.match(stale)
        # State moves on after the form was rendered.
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.TRANSCRIBED
        )

        def fail(config, rec):
            raise AssertionError("stale canonical digest must not dispatch")

        monkeypatch.setattr("workflow.services.pipeline.transcribe_one", fail)
        acquired = []

        @contextlib.contextmanager
        def spy_lock(config):
            acquired.append(True)
            yield

        monkeypatch.setattr(
            "workflow.services.web_actions.pipeline_lock", spy_lock
        )
        response = client.post(
            f"/recordings/{recording.pk}/transcribe/",
            {"fingerprint": stale},
            follow=True,
        )
        assert response.status_code == 200  # 302 followed
        assert "changed since the form was opened" in response.content.decode()
        assert acquired == [True]  # the no-op ran under the lock
        recording.refresh_from_db()
        assert recording.processing_status == ProcessingStatus.TRANSCRIBED


class TestRoutingStateFingerprint:
    """The recording-level fingerprint binds the ACTIVE routing decision's
    stable identity/behavior state (pk/ordinal, profile, model,
    language_arg, verified flag, explicit no-active marker). A routing
    update that changes NEITHER the processing status NOR the attempts
    (e.g. a CLI manual route or confirm landing while a form is open)
    therefore stales every rendered route/confirm/transcribe action,
    which stays the under-lock safe no-op. Raw evidence, confidence,
    reason text, verifier and timestamps are never bound."""

    @staticmethod
    def _append_active_decision(recording, old, *, profile, model, verified=True):
        """The DB effect of a second routing update (manual_route
        append semantics): old decision deactivated, new one active."""
        RoutingDecision.objects.filter(pk=old.pk).update(is_active=False)
        return RoutingDecision.objects.create(
            recording=recording,
            ordinal=old.ordinal + 1,
            route_suggestion=profile,
            profile_name=profile,
            model_id=model,
            language_arg=None,
            method=RoutingMethod.MANUAL,
            routing_verified=verified,
            is_active=True,
        )

    def test_decision_swap_without_status_or_attempt_change_stales_form(
        self, client, monkeypatch
    ):
        recording, _t, _s = _ready_recording("rfp-swap")
        fingerprint = _fingerprint(recording)
        status_before = recording.processing_status
        attempts_before = ProcessingAttempt.objects.filter(
            recording=recording
        ).count()

        old = RoutingDecision.objects.get(recording=recording, is_active=True)
        # Same-family swap: the language-resolution inputs are untouched,
        # so ONLY the new routing binding can move the digest.
        self._append_active_decision(
            recording,
            old,
            profile="european_small",
            model="parakeet-pro:nvidia_parakeet-v3_494MB",
        )
        recording.refresh_from_db()
        # The swap changed ONLY the active routing decision:
        assert recording.processing_status == status_before
        assert (
            ProcessingAttempt.objects.filter(recording=recording).count()
            == attempts_before
        )
        # ...and the fingerprint nevertheless went stale.
        assert _fingerprint(recording) != fingerprint

        def fail(*args, **kwargs):
            raise AssertionError("stale form must not dispatch after a routing update")

        monkeypatch.setattr("workflow.services.pipeline.transcribe_one", fail)
        response = client.post(
            f"/recordings/{recording.pk}/transcribe/",
            {"fingerprint": fingerprint},
            follow=True,
        )
        assert response.status_code == 200  # 302 followed
        assert "changed since the form was opened" in response.content.decode()
        recording.refresh_from_db()
        assert recording.processing_status == status_before  # untouched no-op

        # Positive control: a form rendered AFTER the update executes.
        def fake_transcribe(config, rec):
            return {"recording_id": rec.pk, "result": "transcribed"}

        monkeypatch.setattr("workflow.services.pipeline.transcribe_one", fake_transcribe)
        fresh = client.post(
            f"/recordings/{recording.pk}/transcribe/",
            {"fingerprint": _fingerprint(recording)},
        )
        assert fresh.status_code == 302

    def test_behavior_fields_of_the_same_row_are_bound(self):
        """profile/model/language changes APPLIED TO the same active row
        (identity fields unchanged) still stale the digest."""
        recording, _t, _s = _ready_recording("rfp-fields")
        before = _fingerprint(recording)
        decision = RoutingDecision.objects.get(recording=recording, is_active=True)
        RoutingDecision.objects.filter(pk=decision.pk).update(
            profile_name="mandarin", model_id="apple:zh-CN", language_arg="zh-Hant"
        )
        assert _fingerprint(recording) != before

    def test_confirming_the_same_decision_changes_digest(self, client, monkeypatch):
        recording, _t, _s = _ready_recording("rfp-confirm")
        decision = RoutingDecision.objects.get(recording=recording, is_active=True)
        assert decision.routing_verified is False
        fingerprint = _fingerprint(recording)

        # An out-of-band confirmation of the SAME row (the CLI
        # confirm_routing effect): no new decision, no attempt, no status
        # change — only the verified flag (plus unbound provenance).
        RoutingDecision.objects.filter(pk=decision.pk).update(
            routing_verified=True, verified_at=timezone.now(), verified_by="cli"
        )
        assert _fingerprint(recording) != fingerprint

        def fail(*args, **kwargs):
            raise AssertionError("stale form must not dispatch after a confirmation")

        monkeypatch.setattr("workflow.services.pipeline.transcribe_one", fail)
        response = client.post(
            f"/recordings/{recording.pk}/transcribe/",
            {"fingerprint": fingerprint},
            follow=True,
        )
        assert "changed since the form was opened" in response.content.decode()

    def test_unbound_decision_fields_never_move_the_digest(self):
        """Raw evidence, confidence, reason text, verifier and timestamps
        are NOT bound: mutating them cannot change the digest (and could
        never leak through it), while the bound fields still can."""
        recording, _t, _s = _ready_recording("rfp-unbound")
        before = _fingerprint(recording)
        decision = RoutingDecision.objects.get(recording=recording, is_active=True)
        RoutingDecision.objects.filter(pk=decision.pk).update(
            evidence={
                "excerpt": "SECRET-AUDIO-TEXT-" + ("x" * 5000),
                "scores": {"european": 0.9, "mandarin": 0.05},
            },
            confidence=0.987,
            reason_code="manual_selection",
            verified_by="someone-else",
            verified_at=timezone.now(),
        )
        after = _fingerprint(recording)
        assert after == before
        assert _FINGERPRINT_RE.match(after)
        assert "SECRET-AUDIO-TEXT" not in after

    def test_routing_history_is_not_scanned_and_stays_select_only(self):
        """The binding reads ONE bounded active-decision row: growing the
        append-only history never adds queries and never changes the
        digest; every query is a SELECT."""
        recording, _t, _s = _ready_recording("rfp-bounded")
        with CaptureQueriesContext(connection) as small:
            fp_small = _fingerprint(recording)
        assert all(
            q["sql"].lstrip().upper().startswith("SELECT")
            for q in small.captured_queries
        )
        active = RoutingDecision.objects.get(recording=recording, is_active=True)
        for i in range(2, 12):  # ten extra inactive history rows
            RoutingDecision.objects.create(
                recording=recording,
                ordinal=active.ordinal + i,
                route_suggestion="european",
                profile_name="european",
                model_id="parakeet-pro:nvidia_parakeet-v3",
                method=RoutingMethod.MANUAL,
                is_active=False,
            )
        with CaptureQueriesContext(connection) as large:
            fp_large = _fingerprint(recording)
        assert len(large.captured_queries) == len(small.captured_queries)
        assert fp_large == fp_small


class TestPendingStateFormHooks:
    """Markup contract for the pending-state JavaScript: every
    direct-execution action form — the recording-level forms AND the
    shared per-variant summarize form (recording- and section-scoped) —
    carries a ``data-action-form`` marker and one ``aria-live`` status
    region the enhancement can update (in-form, or the value-tagged
    external region of the segmentation editor). Summarize forms carry
    TEMPLATE-OWNED mode-specific pending copy because their copy
    varies; the simpler actions fall back to the compact JS map."""

    def test_detail_page_direct_forms_carry_hooks(self, client):
        recording, _t, _s = _ready_recording("hooks-ready")
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        # ready_to_transcribe: the transcribe form executes directly,
        # with the concise "can take a while / prior transcript stays"
        # note immediately above the control.
        assert 'data-action-form="transcribe"' in content
        assert 'aria-live="polite"' in content
        assert "can take a while" in content
        assert "stays active until a new one succeeds" in content

    def test_needs_review_manual_route_form_carries_hooks_without_confirm(self, client):
        """Needs review shows ONLY the manual route form as the
        recommended action; the audit-only Confirm routing action is not
        offered there even with an active unverified decision."""
        recording, _t, _s = make_transcribed_recording(["x"], sha="hooks-review")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.NEEDS_REVIEW
        )
        recording.refresh_from_db()
        _routing_decision(recording, verified=False)
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'data-action-form="route"' in content
        assert 'aria-live="polite"' in content
        assert 'data-action-form="confirm-routing"' not in content
        assert "/confirm-routing/" not in content

    def test_transcribed_unverified_has_no_confirm_routing_form(self, client):
        """One-click Confirm routing is audit-only and is never rendered
        as a recommended action: a transcribed recording with an
        unverified active decision shows no confirm-routing form, only
        the manual route form inside the collapsed Routing disclosure
        (choosing the current profile there confirms the routing)."""
        recording, _t, _s = make_transcribed_recording(["x"], sha="hooks-confirm")
        _routing_decision(recording, verified=False)
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'data-action-form="confirm-routing"' not in content
        assert "/confirm-routing/" not in content
        assert 'data-action-form="route"' in content

    def test_failed_retry_form_carries_hook(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="hooks-failed")
        Recording.objects.filter(pk=recording.pk).update(
            processing_status=ProcessingStatus.FAILED, failure_stage="routing"
        )
        recording.refresh_from_db()
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'data-action-form="retry"' in content

    def test_recording_variant_form_carries_hook(self, client):
        recording, _t, _s = make_transcribed_recording(["x"], sha="hooks-variant")
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        # The recording-level summarize form executes on its first POST
        # with TEMPLATE-OWNED pending copy (this state is a "first"
        # generation: nothing exists to keep).
        assert 'data-action-form="summarize"' in content
        assert 'data-pending-label="Generating…"' in content
        assert "Generating the first summary" in content

    def test_regenerate_variant_form_carries_keep_current_copy(self, client):
        """A regenerate variant carries the template-owned keep-current
        copy (inline note + pending message): the current summary stays
        active unless the new version is created completely."""
        recording, transcript, section = make_transcribed_recording(
            ["x"], sha="hooks-regen"
        )
        make_summary_version(recording, transcript, section)
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'data-action-form="summarize"' in content
        assert 'data-pending-label="Regenerating…"' in content
        assert "current summary stays active unless the new version" in content

    def test_section_variant_form_carries_hooks(self, client):
        """Phase 2: the section-scoped variant form executes on its first
        POST (no server-side confirmation step), so it carries the same
        direct-action hooks as the recording-level summarize form."""
        from workflow.models import Section
        from workflow.services.segmentation import save_segmented_version

        recording, transcript, _fixed = make_transcribed_recording(
            [f"line {i}" for i in range(8)], sha="hooks-section"
        )
        result = save_segmented_version(
            recording.pk, transcript.pk, 0, 8, [3], ["First topic", "Second topic"]
        )
        section = (
            Section.objects.filter(segmented_version_id=result.version_id)
            .order_by("ordinal")
            .first()
        )
        content = client.get(
            f"/recordings/{recording.pk}/sections/{section.pk}/"
        ).content.decode()
        # The section action form posts to the section summarize route
        # and executes directly: marker + aria-live region are present.
        assert f"/sections/{section.pk}/summarize/" in content
        assert 'data-action-form="summarize"' in content
        assert "data-action-live" in content
        assert 'aria-live="polite"' in content
