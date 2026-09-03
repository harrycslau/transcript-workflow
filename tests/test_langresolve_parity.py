"""Parity tests: the ORM default-output-language expression in
``workflow.services.langresolve`` must agree with the Python
``resolve_default_language`` for every documented evidence path, and
must not issue per-recording queries.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from workflow.models import (
    Recording,
    RoutingDecision,
    RoutingMethod,
    Transcript,
)
from workflow.services.langresolve import (
    default_output_language_expression,
    resolve_default_language,
)

from factories import make_transcribed_recording

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


def _expression_default_languages(recordings: list[Recording]) -> dict[str, str]:
    """Resolve the ORM expression for each recording via one query over
    their active transcripts."""
    qs = (
        Transcript.objects.filter(recording__in=recordings, is_active=True)
        .annotate(_dl=default_output_language_expression())
        .values_list("recording_id", "_dl")
    )
    return {recording_id: dl for recording_id, dl in qs}


def _make_routing(recording, *, verified, method, suggestion):
    RoutingDecision.objects.create(
        recording=recording,
        ordinal=1,
        route_suggestion=suggestion,
        profile_name=suggestion,
        model_id="test-model",
        method=method,
        routing_verified=verified,
        is_active=True,
    )


def test_parity_all_evidence_paths():
    expected: list[tuple[Recording, str]] = []

    # Unknown-language fallback -> en
    rec, t, s = make_transcribed_recording(["x"], sha="par-1")
    expected.append((rec, "en"))

    # Transcript-observed non-Chinese -> en
    rec, t, s = make_transcribed_recording(["x"], sha="par-2")
    t.language_observed = "en"
    t.save()
    expected.append((rec, "en"))

    # Transcript-observed Chinese (cmn-CN, yue-HK, zh-Hant) -> zh-Hant
    for sha, lang in (("par-3", "cmn-CN"), ("par-4", "yue-HK"), ("par-5", "zh-Hant")):
        rec, t, s = make_transcribed_recording(["x"], sha=sha)
        t.language_observed = lang
        t.save()
        expected.append((rec, "zh-Hant"))

    # User-corrected Chinese -> zh-Hant
    rec, t, s = make_transcribed_recording(["x"], sha="par-6")
    t.language_observed = "zh-HK"
    t.language_observed_verified_by = "user"
    t.save()
    expected.append((rec, "zh-Hant"))

    # User-corrected non-Chinese -> en
    rec, t, s = make_transcribed_recording(["x"], sha="par-7")
    t.language_observed = "fi"
    t.language_observed_verified_by = "user"
    t.save()
    expected.append((rec, "en"))

    # Verified Chinese routing (overrides observed non-Chinese) -> zh-Hant
    rec, t, s = make_transcribed_recording(["x"], sha="par-8")
    t.language_observed = "en"
    t.save()
    _make_routing(rec, verified=True, method=RoutingMethod.AUTOMATIC, suggestion="cantonese")
    expected.append((rec, "zh-Hant"))

    # Automatic (unverified) Chinese routing -> zh-Hant
    rec, t, s = make_transcribed_recording(["x"], sha="par-9")
    _make_routing(rec, verified=False, method=RoutingMethod.AUTOMATIC, suggestion="mandarin")
    expected.append((rec, "zh-Hant"))

    # European routing decisions never force Chinese output -> en
    rec, t, s = make_transcribed_recording(["x"], sha="par-10")
    _make_routing(rec, verified=True, method=RoutingMethod.AUTOMATIC, suggestion="european")
    expected.append((rec, "en"))

    recordings = [r for r, _ in expected]
    py_results = {
        r.pk: resolve_default_language(r.transcripts.get(is_active=True)) for r in recordings
    }
    expr_results = _expression_default_languages(recordings)

    assert set(expr_results) == set(py_results)
    for rec, want in expected:
        assert py_results[rec.pk] == want, f"python mismatch for {rec.pk}"
        assert expr_results[rec.pk] == want, f"expression mismatch for {rec.pk}"


def test_expression_is_database_side_constant_query():
    recordings = []
    for index in range(10):
        rec, t, s = make_transcribed_recording(["x"], sha=f"parq-{index}")
        t.language_observed = "zh-HK"
        t.save()
        recordings.append(rec)
    with CaptureQueriesContext(connection) as ctx:
        _expression_default_languages(recordings)
    # Exactly one query for all ten recordings.
    assert len(ctx.captured_queries) == 1