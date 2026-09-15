"""Step 6.2 section summary action (web, POST-only, direct execution).

Proves:

- the single (first and only) POST performs the cheap pre-lock guards
  (generation selector, live active-topic-section ownership/canonical
  layout validation, strict fingerprint/mode input contract) with NO
  pipeline lock, NO network, NO write, then executes under the global
  pipeline lock + recovery, re-validates the live section, compares the
  opaque section action fingerprint (stale => safe no-op), re-derives
  the mode (mismatch => safe no-op) and calls ``summarize_section_one``;
- modes: first / retry_summary / regenerate per selected variant; the
  resolved output language drives the mode;
- success/failure messaging is section-scoped and stable sanitized;
- busy lock => friendly 409; GET is a 405;
- the action NEVER changes the Recording-level summary tuple and NEVER
  schedules a recording search sync; historical sections are rejected
  before any lock; the redirect always targets the section detail page
  with the validated read selector;
- the two-step confirmation protocol is GONE: no ``confirmed`` field,
  no ``action_confirm.html`` rendering for section summaries, and the
  section variant action form carries the direct-action hooks
  (``data-action-form`` marker + ``aria-live`` status region).
"""

from __future__ import annotations

import re as _re
from pathlib import Path

import pytest
from django.test import Client
from django.utils import timezone as dj_timezone

from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    ProcessingAttempt,
    Recording,
    Section,
    Summary,
    SummaryState,
    SummaryVariantState,
)
from workflow.services.segmentation import save_segmented_version
from workflow.services.web_actions import section_state_fingerprint

from factories import make_transcribed_recording

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


@pytest.fixture
def client():
    return Client()


def _split(sha="sec-sum-web", count=8):
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


def _post_url(recording, section):
    return f"/recordings/{recording.pk}/sections/{section.pk}/summarize/"


def _make_section_summary(rec, transcript, section, *, output_language="en"):
    from factories import make_summary_version

    summary = make_summary_version(
        rec, transcript, section, title=f"S {output_language}",
        output_language=output_language,
    )
    SummaryVariantState.objects.create(
        transcript=transcript, section=section,
        output_language=output_language, status="current",
    )
    return summary


def _fake_summarize(monkeypatch, recording, *, output_language="en", captured=None):
    """Patch ``summarize_section_one`` (the real service is exercised by
    the section-summary service tests); record what it was called with."""

    def fake(config, section, regenerate=False, **kwargs):
        if captured is not None:
            captured["section_id"] = section.pk
            captured["regenerate"] = regenerate
            captured.update({k: v for k, v in kwargs.items() if k == "target_language"})
        return {
            "recording_id": recording.pk, "section_id": section.pk,
            "result": "summarized", "output_language": output_language,
        }

    monkeypatch.setattr("workflow.services.summarize.summarize_section_one", fake)


class TestCsrfAndMethod:
    def test_get_is_405(self, client):
        rec, _t, sections = _split()
        response = client.get(_post_url(rec, sections[0]))
        assert response.status_code == 405

    def test_post_without_csrf_rejected(self):
        rec, _t, sections = _split()
        csrf_client = Client(enforce_csrf_checks=True)
        response = csrf_client.post(
            _post_url(rec, sections[0]),
            {"language": "default", "mode": "first",
             "fingerprint": section_state_fingerprint(rec, sections[0])},
        )
        assert response.status_code == 403


class TestInvalidPayloadBeforeLock:
    """Every cheap guard rejects with a friendly 400 BEFORE any pipeline
    lock, recovery, network or write."""

    def test_missing_fingerprint_rejected(self, client):
        rec, _t, sections = _split(sha="sec-val-nofp")
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "default", "mode": "first"},
        )
        assert response.status_code == 400

    def test_duplicate_fingerprint_rejected(self, client):
        rec, _t, sections = _split(sha="sec-val-dupfp")
        fp = section_state_fingerprint(rec, sections[0])
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "default", "mode": "first", "fingerprint": [fp, fp]},
        )
        assert response.status_code == 400

    def test_malformed_fingerprint_rejected(self, client):
        rec, _t, sections = _split(sha="sec-val-badfp")
        for bad in ("not-a-hex-fingerprint", "z" * 64, "0" * 63):
            response = client.post(
                _post_url(rec, sections[0]),
                {"language": "default", "mode": "first", "fingerprint": bad},
            )
            assert response.status_code == 400, bad

    def test_missing_mode_rejected(self, client):
        rec, _t, sections = _split(sha="sec-val-nomode")
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "default",
             "fingerprint": section_state_fingerprint(rec, sections[0])},
        )
        assert response.status_code == 400

    def test_duplicate_mode_rejected(self, client):
        rec, _t, sections = _split(sha="sec-val-dupmode")
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "default", "mode": ["first", "first"],
             "fingerprint": section_state_fingerprint(rec, sections[0])},
        )
        assert response.status_code == 400

    def test_invalid_mode_rejected(self, client):
        rec, _t, sections = _split(sha="sec-val-badmode")
        for bad in ("banana", ""):
            response = client.post(
                _post_url(rec, sections[0]),
                {"language": "default", "mode": bad,
                 "fingerprint": section_state_fingerprint(rec, sections[0])},
            )
            assert response.status_code == 400, bad

    def test_uppercase_mode_accepted_and_normalized(self, client, monkeypatch):
        """Modes are normalized like fingerprints: an uppercase action
        mode is accepted and lowercased before the service comparison."""
        rec, transcript, sections = _split(sha="sec-val-uppermode")
        _make_section_summary(rec, transcript, sections[0])
        fp = section_state_fingerprint(rec, sections[0])
        captured = {}
        _fake_summarize(monkeypatch, rec, captured=captured)
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "default", "mode": "REGENERATE", "fingerprint": fp},
        )
        assert response.status_code == 302
        assert captured["regenerate"] is True

    def test_uppercase_fingerprint_accepted_and_normalized(self, client, monkeypatch):
        rec, transcript, sections = _split(sha="sec-val-upper")
        fp = section_state_fingerprint(rec, sections[0]).upper()
        captured = {}
        _fake_summarize(monkeypatch, rec, captured=captured)
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "default", "mode": "first", "fingerprint": fp},
        )
        assert response.status_code == 302
        assert captured["section_id"] == sections[0].pk

    def test_rejections_happen_before_any_lock_or_network(self, client, monkeypatch):
        """Malformed inputs are rejected in the view even when a lock
        acquisition would explode — the service is never reached."""
        rec, _t, sections = _split(sha="sec-val-nolock")

        def no_lock(*args, **kwargs):
            raise AssertionError("pipeline lock must not be acquired")

        monkeypatch.setattr("workflow.services.web_actions.pipeline_lock", no_lock)
        for payload in (
            {"language": "default", "mode": "first"},
            {"language": "default", "mode": "first", "fingerprint": "not-hex"},
            {"language": "default",
             "fingerprint": section_state_fingerprint(rec, sections[0])},
            {"language": "xx", "mode": "first",
             "fingerprint": section_state_fingerprint(rec, sections[0])},
        ):
            response = client.post(_post_url(rec, sections[0]), payload)
            assert response.status_code == 400

    def test_validates_language(self, client):
        rec, _t, sections = _split()
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "xx", "mode": "first",
             "fingerprint": section_state_fingerprint(rec, sections[0])},
        )
        assert response.status_code == 400
        assert "Only default, English, Traditional Chinese and Original" in response.content.decode()

    def test_historical_section_rejected_before_lock(self, client, monkeypatch):
        rec, transcript, sections = _split()
        fingerprint = section_state_fingerprint(rec, sections[0])
        # Supersede the layout so the section becomes historical.
        save_segmented_version(rec.pk, transcript.pk, 0, transcript.segments.count())

        def no_lock(*args, **kwargs):
            raise AssertionError("no pipeline lock may be taken for a historical section")

        monkeypatch.setattr("workflow.services.web_actions.pipeline_lock", no_lock)
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "default", "mode": "first", "fingerprint": fingerprint},
        )
        assert response.status_code == 400
        assert "read-only" in response.content.decode()
        assert Summary.objects.filter(transcript=transcript, section=sections[0]).count() == 0

    def test_cross_recording_404(self, client):
        rec_a, _t, sections_a = _split(sha="sec-sum-cross-a")
        rec_b, _t_b, _s_b = _split(sha="sec-sum-cross-b")
        response = client.post(
            f"/recordings/{rec_b.pk}/sections/{sections_a[0].pk}/summarize/",
            {"language": "default", "mode": "first", "fingerprint": "0" * 64},
        )
        assert response.status_code == 404

    def test_invalid_payload_renders_no_confirmation_page(self, client):
        """The removed two-step protocol never resurfaces: no invalid
        submission renders a 200 confirmation interstitial."""
        rec, _t, sections = _split(sha="sec-val-noconf")
        cases = [
            {"language": "default", "mode": "first"},
            {"language": "xx", "mode": "first",
             "fingerprint": section_state_fingerprint(rec, sections[0])},
        ]
        for payload in cases:
            response = client.post(_post_url(rec, sections[0]), payload)
            assert response.status_code == 400, payload
            assert not any(
                "action_confirm" in (t.name or "") for t in response.templates
            ), payload
            assert b"are you sure" not in response.content
            assert b'name="confirmed"' not in response.content


class TestDirectExecution:
    """The section form's FIRST (and only) POST executes the action."""

    def test_first_generation_executes_and_redirects(self, client, monkeypatch):
        rec, transcript, sections = _split()
        fingerprint = section_state_fingerprint(rec, sections[0])
        captured = {}
        _fake_summarize(monkeypatch, rec, captured=captured)
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "default", "mode": "first", "fingerprint": fingerprint},
        )
        assert response.status_code == 302
        assert captured["regenerate"] is False
        assert response["Location"] == f"/recordings/{rec.pk}/sections/{sections[0].pk}/"
        # The action never touches the Recording-level summary tuple.
        rec.refresh_from_db()
        assert rec.summary_status in (
            SummaryState.MISSING, SummaryState.CURRENT,
        )
        assert rec.summary_status != SummaryState.FAILED

    def test_regenerate_executes(self, client, monkeypatch):
        rec, transcript, sections = _split()
        _make_section_summary(rec, transcript, sections[0])
        fingerprint = section_state_fingerprint(rec, sections[0])
        captured = {}
        _fake_summarize(monkeypatch, rec, captured=captured)
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "default", "mode": "regenerate", "fingerprint": fingerprint},
        )
        assert response.status_code == 302
        assert captured["regenerate"] is True

    def test_retry_after_failed_variant_executes(self, client, monkeypatch):
        rec, transcript, sections = _split()
        SummaryVariantState.objects.create(
            transcript=transcript, section=sections[0],
            output_language="en", status="failed",
        )
        fingerprint = section_state_fingerprint(rec, sections[0])
        captured = {}
        _fake_summarize(monkeypatch, rec, captured=captured)
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "default", "mode": "retry_summary", "fingerprint": fingerprint},
        )
        assert response.status_code == 302
        assert captured["regenerate"] is False

    def test_original_unresolved_is_first_generation(self, client, monkeypatch):
        rec, transcript, sections = _split()
        transcript.language_observed = ""
        transcript.save(update_fields=["language_observed"])
        fingerprint = section_state_fingerprint(rec, sections[0])
        captured = {}
        _fake_summarize(monkeypatch, rec, captured=captured)
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "original", "mode": "first", "fingerprint": fingerprint},
        )
        assert response.status_code == 302
        assert captured["target_language"] == "original"

    def test_stale_fingerprint_is_safe_noop(self, client, monkeypatch):
        rec, _t, sections = _split()

        def fake(config, section, regenerate=False, **kwargs):
            raise AssertionError("must not run on stale fingerprint")

        monkeypatch.setattr("workflow.services.summarize.summarize_section_one", fake)
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "default", "mode": "first", "fingerprint": "0" * 64},
        )
        assert response.status_code == 302
        # Follow the redirect: a warning banner, no summary was created.
        detail = client.get(response["Location"]).content.decode()
        assert "nothing was run" in detail

    def test_mode_mismatch_is_safe_noop(self, client, monkeypatch):
        rec, transcript, sections = _split()
        _make_section_summary(rec, transcript, sections[0])
        fingerprint = section_state_fingerprint(rec, sections[0])

        def fake(config, section, regenerate=False, **kwargs):
            raise AssertionError("mode mismatch must not run the service")

        monkeypatch.setattr("workflow.services.summarize.summarize_section_one", fake)
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "default", "mode": "first", "fingerprint": fingerprint},
        )
        assert response.status_code == 302
        assert "nothing was run" in client.get(response["Location"]).content.decode()

    def test_failure_surfaces_section_scoped_message(self, client, monkeypatch):
        rec, _t, sections = _split()
        fingerprint = section_state_fingerprint(rec, sections[0])

        def failing(config, section, regenerate=False, **kwargs):
            return {
                "recording_id": rec.pk, "section_id": section.pk,
                "result": "failed", "error_code": "endpoint_unavailable",
            }

        monkeypatch.setattr("workflow.services.summarize.summarize_section_one", failing)
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "default", "mode": "first", "fingerprint": fingerprint},
        )
        assert response.status_code == 302
        detail = client.get(response["Location"]).content.decode()
        assert "Section summarization failed" in detail
        assert "endpoint_unavailable" in detail
        assert "Traceback" not in detail

    def test_redirect_keeps_validated_return_language(self, client, monkeypatch):
        rec, transcript, sections = _split()
        _make_section_summary(rec, transcript, sections[0], output_language="fi")
        fingerprint = section_state_fingerprint(rec, sections[0])
        _fake_summarize(monkeypatch, rec)
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "original", "mode": "regenerate",
             "return_language": "fi", "fingerprint": fingerprint},
        )
        assert response.status_code == 302
        assert response["Location"].endswith(f"/sections/{sections[0].pk}/?language=fi")

    def test_invalid_return_language_falls_back(self, client, monkeypatch):
        rec, _t, sections = _split()
        fingerprint = section_state_fingerprint(rec, sections[0])
        _fake_summarize(monkeypatch, rec)
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "default", "mode": "first",
             "return_language": "xx", "fingerprint": fingerprint},
        )
        assert response.status_code == 302
        assert response["Location"] == f"/recordings/{rec.pk}/sections/{sections[0].pk}/"

    def test_no_recording_tuple_or_sync_changes(self, client, monkeypatch):
        """The executed section summary must never change the Recording
        tuple; the ACTION layer itself performs no search-index work."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        rec, transcript, sections = _split()
        rec.summary_status = SummaryState.MISSING
        rec.save(update_fields=["summary_status"])
        fingerprint = section_state_fingerprint(rec, sections[0])

        # The fake service returns success WITHOUT creating anything: the
        # real ``summarize_section_one`` never touches the Recording
        # tuple (proven by the section-summary service tests, which also
        # pin the single parent-recording sync schedule); this test
        # asserts the ACTION layer itself adds no index writes.
        _fake_summarize(monkeypatch, rec)
        with CaptureQueriesContext(connection) as ctx:
            response = client.post(
                _post_url(rec, sections[0]),
                {"language": "default", "mode": "first", "fingerprint": fingerprint},
            )
        assert response.status_code == 302
        rec.refresh_from_db()
        # Recording-level tuple untouched (stays MISSING).
        assert rec.summary_status == SummaryState.MISSING
        assert not rec.resummarization_failed
        # No recording search-sync callback / no SearchDocument writes.
        assert not any("workflow_search_document" in q["sql"] for q in ctx.captured_queries)


class TestServiceBoundaryFailSafe:
    """The section-summarize SERVICE boundary fails safe (safe no-op)
    when the expected fingerprint or mode is absent or invalid, and
    verifies section parent recording ownership — all BEFORE any
    pipeline lock, recovery, network or write."""

    def _run(self, monkeypatch, recording, section, *, fp, mode):
        from workflow.views.helpers import get_config
        from workflow.services.web_actions import execute_section_summarize

        def no_lock(*args, **kwargs):
            raise AssertionError("pipeline lock must not be acquired")

        monkeypatch.setattr("workflow.services.web_actions.pipeline_lock", no_lock)
        return execute_section_summarize(
            get_config(), recording, section,
            requested_mode=mode,
            expected_fingerprint=fp,
            language="default",
        )

    def test_absent_fingerprint_is_safe_noop(self, monkeypatch):
        rec, _t, sections = _split(sha="sec-bnd-nofp")
        outcome = self._run(
            monkeypatch, rec, sections[0], fp=None, mode="first"
        )
        assert outcome.result == "state_changed"
        assert "nothing was run" in outcome.message

    def test_malformed_fingerprint_is_safe_noop(self, monkeypatch):
        rec, _t, sections = _split(sha="sec-bnd-badfp")
        for bad in ("not-hex", "z" * 64, "0" * 63):
            outcome = self._run(monkeypatch, rec, sections[0], fp=bad, mode="first")
            assert outcome.result == "state_changed"

    def test_absent_mode_is_safe_noop(self, monkeypatch):
        rec, _t, sections = _split(sha="sec-bnd-nomode")
        fp = section_state_fingerprint(rec, sections[0])
        outcome = self._run(monkeypatch, rec, sections[0], fp=fp, mode=None)
        assert outcome.result == "state_changed"

    def test_invalid_mode_is_safe_noop(self, monkeypatch):
        rec, _t, sections = _split(sha="sec-bnd-badmode")
        fp = section_state_fingerprint(rec, sections[0])
        for bad in ("banana", ""):
            outcome = self._run(monkeypatch, rec, sections[0], fp=fp, mode=bad)
            assert outcome.result == "state_changed"

    def test_cross_parent_section_is_safe_noop(self, monkeypatch):
        """The service boundary verifies section parent recording
        ownership: a section of ANOTHER recording never runs."""
        rec_a, _t, sections_a = _split(sha="sec-bnd-cross-a")
        rec_b, _t_b, sections_b = _split(sha="sec-bnd-cross-b")
        fp = section_state_fingerprint(rec_b, sections_b[0])
        outcome = self._run(monkeypatch, rec_a, sections_b[0], fp=fp, mode="first")
        assert outcome.result == "state_changed"


class TestFingerprintInvalidation:
    def test_attempt_variant_and_source_language_invalidate(self):
        rec, transcript, sections = _split(sha="sec-fp-inv")
        fp = section_state_fingerprint(rec, sections[0])
        # A new section summary attempt invalidates.
        ProcessingAttempt.objects.create(
            recording=rec, stage=AttemptStage.SUMMARIZATION, ordinal=1,
            context_json={
                "language": {
                    "transcript_id": transcript.pk,
                    "section_id": sections[0].pk,
                    "resolved": "en",
                }
            },
            outcome=AttemptOutcome.SUCCESS,
        )
        assert section_state_fingerprint(rec, sections[0]) != fp
        fp = section_state_fingerprint(rec, sections[0])
        # A source-language correction invalidates (language resolution).
        transcript.language_observed = "zh-HK"
        transcript.save(update_fields=["language_observed"])
        assert section_state_fingerprint(rec, sections[0]) != fp
        fp = section_state_fingerprint(rec, sections[0])
        # A title change invalidates.
        Section.objects.filter(pk=sections[0].pk).update(title="Renamed")
        assert section_state_fingerprint(rec, sections[0]) != fp
        fp = section_state_fingerprint(rec, sections[0])
        # A variant-state change invalidates.
        SummaryVariantState.objects.create(
            transcript=transcript, section=sections[0],
            output_language="en", status="current",
        )
        assert section_state_fingerprint(rec, sections[0]) != fp

    def test_latest_attempt_mutation_invalidates(self):
        """Recovery mutates the LATEST mutable attempt (outcome/
        error_code/finished_at); the bounded fingerprint must still
        detect it without materializing the attempt history."""
        rec, transcript, sections = _split(sha="sec-fp-latest")
        section = sections[0]
        attempt = ProcessingAttempt.objects.create(
            recording=rec, stage=AttemptStage.SUMMARIZATION, ordinal=1,
            context_json={
                "language": {
                    "transcript_id": transcript.pk,
                    "section_id": section.pk,
                    "resolved": "en",
                }
            },
            outcome=AttemptOutcome.RUNNING,
        )
        fp = section_state_fingerprint(rec, section)
        attempt.outcome = AttemptOutcome.INTERRUPTED
        attempt.error_code = "process_interrupted"
        attempt.finished_at = dj_timezone.now()
        attempt.save(update_fields=["outcome", "error_code", "finished_at"])
        assert section_state_fingerprint(rec, section) != fp

    def test_fingerprint_is_opaque_sha256_hex(self):
        """The section action fingerprint is a truly opaque canonical
        64-hex SHA-256 — no ids, titles, or raw JSON in the value."""
        rec, transcript, sections = _split(sha="sec-fp-opaque")
        fp = section_state_fingerprint(rec, sections[0])
        assert _re.fullmatch(r"[0-9a-f]{64}", fp)
        # A 64-hex value can never carry non-hex identifiers: the UUID
        # recording id (dashes), the topic title (spaces), or raw JSON
        # markers are all provably absent.
        assert "-" not in fp
        assert " " not in fp
        assert "{" not in fp and '"' not in fp
        assert sections[0].title not in fp
        assert str(rec.pk) not in fp  # UUID contains dashes — provably absent

    def test_fingerprint_query_count_bounded_against_attempt_history(self):
        """The fingerprint binds a bounded aggregate + the LATEST mutable
        attempt — the query count never grows with the append-only attempt
        history."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        rec, transcript, sections = _split(sha="sec-fp-bound")
        section = sections[0]

        def count():
            with CaptureQueriesContext(connection) as ctx:
                section_state_fingerprint(rec, section)
            return len(ctx.captured_queries)

        small = count()
        for i in range(40):
            ProcessingAttempt.objects.create(
                recording=rec, stage=AttemptStage.SUMMARIZATION, ordinal=i + 1,
                context_json={
                    "language": {
                        "transcript_id": transcript.pk,
                        "section_id": section.pk,
                        "resolved": "en",
                    }
                },
                outcome=AttemptOutcome.SUCCESS,
                finished_at=dj_timezone.now(),
            )
        assert count() == small

    def test_fingerprint_state_cap_fails_closed(self):
        """An unusual current-scope-state row count (variant states per
        output language) exceeds the defensive cap and fails closed with
        the stable sanitized ``section_state_too_large`` category."""
        from workflow.services.segmentation import SegmentationError

        rec, transcript, sections = _split(sha="sec-fp-cap")
        section = sections[0]
        SummaryVariantState.objects.bulk_create(
            [
                SummaryVariantState(
                    transcript=transcript, section=section,
                    output_language=f"xx-{i:02d}", status="missing",
                )
                for i in range(65)
            ]
        )
        with pytest.raises(SegmentationError) as excinfo:
            section_state_fingerprint(rec, section)
        assert excinfo.value.code == "section_state_too_large"

    def test_fingerprint_cross_recording_section_fails_closed(self):
        """Defense-in-depth parent ownership — the fingerprint of a Section
        belonging to ANOTHER recording fails closed with the stable
        sanitized ``section_not_in_recording`` category (never a
        cross-recording fingerprint)."""
        from workflow.services.segmentation import SegmentationError

        rec_a, _t, sections_a = _split(sha="sec-fp-own-a")
        rec_b, _t_b, sections_b = _split(sha="sec-fp-own-b")
        # rec_b's section is a LIVE topic section (would pass the
        # canonical validator); the ownership check must still reject it
        # for rec_a.
        with pytest.raises(SegmentationError) as excinfo:
            section_state_fingerprint(rec_a, sections_b[0])
        assert excinfo.value.code == "section_not_in_recording"

    def test_fingerprint_is_select_only(self, client):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        rec, _t, sections = _split(sha="sec-fp-sel")
        with CaptureQueriesContext(connection) as ctx:
            section_state_fingerprint(rec, sections[0])
        assert ctx.captured_queries
        for q in ctx.captured_queries:
            assert q["sql"].strip().upper().startswith("SELECT"), q["sql"]


class TestLockContention:
    def test_busy_lock_returns_409(self, client, monkeypatch):
        from workflow.services.pipeline_lock import PipelineBusy

        rec, _t, sections = _split()
        fingerprint = section_state_fingerprint(rec, sections[0])

        def busy(*args, **kwargs):
            raise PipelineBusy(12345)

        monkeypatch.setattr(
            "workflow.services.web_actions.execute_section_summarize",
            busy,
        )
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "default", "mode": "first", "fingerprint": fingerprint},
        )
        assert response.status_code == 409


class TestConfirmationProtocolRemoved:
    """Phase 2: no action anywhere renders a confirmation interstitial."""

    def test_confirmation_templates_deleted(self):
        from django.conf import settings

        template_dirs = [Path(p) for p in settings.TEMPLATES[0]["DIRS"]]
        for name in ("workflow/action_confirm.html", "workflow/segmentation_confirm.html"):
            assert not any((base / name).exists() for base in template_dirs), name

    def test_recording_level_actions_never_render_confirmation(self, client):
        rec, _t, _s = _split(sha="sec-shared-1")
        cases = [
            ("/route/", {"profile": "mandarin"}),
            ("/confirm-routing/", {}),
            ("/transcribe/", {}),
            ("/summarize/", {"language": "default"}),
            ("/retry/", {}),
        ]
        for suffix, extra in cases:
            response = client.post(
                f"/recordings/{rec.pk}{suffix}",
                {"fingerprint": "0" * 64, **extra},
            )
            # Stale fingerprint => safe no-op redirect; never a 200
            # confirmation page and never a rendered cancel link.
            assert response.status_code == 302, suffix
            assert not any(
                "action_confirm" in (template.name or "")
                for template in response.templates
            ), suffix
            assert b"Back to section" not in response.content, suffix
            assert b"Yes, run it" not in response.content, suffix

    def test_section_action_post_never_renders_confirmation(self, client, monkeypatch):
        rec, transcript, sections = _split(sha="sec-shared-2")
        fingerprint = section_state_fingerprint(rec, sections[0])
        _fake_summarize(monkeypatch, rec)
        response = client.post(
            _post_url(rec, sections[0]),
            {"language": "default", "mode": "first", "fingerprint": fingerprint},
        )
        # Executes (302 redirect) — no 200 interstitial, no confirmed field.
        assert response.status_code == 302
        assert not any(
            "action_confirm" in (template.name or "") for template in response.templates
        )
        assert b"are you sure" not in response.content
        assert b"confirmed" not in response.content

    def test_segmentation_save_never_renders_confirmation(self, client):
        from workflow.services.segmentation import segmentation_fingerprint

        from factories import make_transcribed_recording

        recording, transcript, _ = make_transcribed_recording(
            [f"segment {i}" for i in range(10)], sha="sec-shared-3"
        )
        fingerprint = segmentation_fingerprint(
            recording.pk, transcript, timezone_name="Europe/Helsinki"
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
            },
        )
        assert response.status_code == 302
        assert not any(
            "segmentation_confirm" in (template.name or "")
            for template in response.templates
        )
        assert b"Save this trim" not in response.content


class TestSectionFormDirectHooks:
    """The section-scoped variant form now executes directly, so it
    carries the same markup hooks as the recording-level form,
    including the TEMPLATE-OWNED mode-specific pending copy."""

    def test_section_variant_form_carries_hooks(self, client):
        rec, _t, sections = _split(sha="sec-hooks")
        content = client.get(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/"
        ).content.decode()
        assert f"/sections/{sections[0].pk}/summarize/" in content
        assert 'data-action-form="summarize"' in content
        assert 'data-action-live' in content
        assert 'aria-live="polite"' in content
        # Fresh section (no summary yet): "first" mode template copy.
        assert 'data-pending-label="Generating…"' in content
        assert "Generating the first summary" in content


class TestRealSectionGenerationIntegration:
    """Objective A integration: run single-POST Section generation through
    the REAL web/service orchestration (``execute_section_summarize``,
    the pipeline lock/recovery and ``summarize_section_one``) with ONLY
    the LLM boundary stubbed, then follow the redirect and prove the
    Section-scoped status panel is honest while the parent Recording
    summary tuple may remain MISSING."""

    def _integration_config(self, tmp_path):
        from brainlib.config import LLMConfig

        from factories import make_config

        return make_config(
            tmp_path,
            llm=LLMConfig(
                provider="openai_compatible",
                base_url="http://127.0.0.1:1/v1",
                model="test-model",
                api_key_env="BRAIN_TEST_LLM_API_KEY",
                temperature=0.2,
                timeout_seconds=600,
            ),
        )

    def test_generation_section_panel_current(self, client, tmp_path, monkeypatch):
        from factories import final_summary_json

        rec, transcript, sections = _split(sha="sec-integration")
        section = sections[0]
        # The parent Recording starts with no whole-recording summary.
        assert rec.summary_status == SummaryState.MISSING

        # Stub ONLY the LLM boundary: the real section service, the real
        # web orchestration, the pipeline lock and recovery all run.
        def fake_chat(config, **kwargs):
            return final_summary_json(
                title="Integration Section Summary",
                overview="Integration overview text.",
            )

        monkeypatch.setattr(
            "workflow.services.summarize.llm_service.chat_completion", fake_chat
        )
        monkeypatch.setattr(
            "workflow.views.actions.get_config",
            lambda: self._integration_config(tmp_path),
        )

        fingerprint = section_state_fingerprint(rec, section)
        response = client.post(
            _post_url(rec, section),
            {"language": "default", "mode": "first", "fingerprint": fingerprint},
        )
        assert response.status_code == 302
        detail = client.get(response["Location"]).content.decode()

        # Active Summary with the exact section/transcript/output language.
        summary = Summary.objects.get(
            transcript=transcript, section=section, output_language="en", is_active=True
        )
        assert summary.title == "Integration Section Summary"
        # Linked successful attempt.
        assert summary.attempt is not None
        assert summary.attempt.outcome == AttemptOutcome.SUCCESS
        # Current Section-scoped variant state.
        variant_state = SummaryVariantState.objects.get(
            transcript=transcript, section=section, output_language="en"
        )
        assert variant_state.status == SummaryVariantState.VariantStatus.CURRENT
        # The generated summary renders on the section detail page.
        assert "Integration Section Summary" in detail
        assert "Integration overview text." in detail
        # The parent Recording tuple MAY remain missing while the Section
        # panel says current and never shows the misleading parent text.
        rec.refresh_from_db()
        assert rec.summary_status == SummaryState.MISSING
        assert "Section summary current" in detail
        assert "An active transcript exists but the current summary is missing" not in detail
        assert "inherited from the parent recording" not in detail
