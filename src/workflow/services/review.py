"""Review-report builder shared by the CLI (`brain review`) and the web
review dashboard (Step 4).

Returns the same machine-readable dict the CLI has always emitted; the
web view renders it. Read-only: no lock, no recovery, no side effects.
Values are stable and sanitized (recording ids, stable reason/error
codes, confidence scores) — never raw exception text, prompts, or
filesystem paths.

Query budget is CONSTANT regardless of how many Recordings match: the
two per-category loops use filtered ``Prefetch(..., to_attr=...)`` for
active RoutingDecisions and ``select_related("last_failed_attempt")``
for retranscription failure codes; every other group is a single
aggregate/values query. No related-manager access inside per-row
loops.
"""

from __future__ import annotations

from django.db.models import Prefetch

from workflow.models import (
    ProcessingStatus,
    Recording,
    RoutingDecision,
    SummaryState,
)


def _active_decisions_prefetch() -> Prefetch:
    return Prefetch(
        "routing_decisions",
        queryset=RoutingDecision.objects.filter(is_active=True).only(
            "id", "recording", "route_suggestion", "profile_name",
            "confidence", "reason_code", "routing_verified",
        ),
        to_attr="active_decisions",
    )


def _first_active(recording: Recording) -> RoutingDecision | None:
    decisions = getattr(recording, "active_decisions", [])
    return decisions[0] if decisions else None


def build_review_report() -> dict:
    """Group recordings that need attention into stable categories."""
    needs_review = []
    needs_review_qs = Recording.objects.filter(
        processing_status=ProcessingStatus.NEEDS_REVIEW
    ).prefetch_related(_active_decisions_prefetch())
    for recording in needs_review_qs:
        decision = _first_active(recording)
        needs_review.append(
            {
                "recording_id": recording.pk,
                "kind": "needs_review_before_transcription",
                "suggested_route": decision.route_suggestion if decision else None,
                "confidence": decision.confidence if decision else None,
                "reason_code": decision.reason_code if decision else None,
            }
        )

    unverified = []
    retranscription_failed = []
    transcribed_qs = (
        Recording.objects.filter(processing_status=ProcessingStatus.TRANSCRIBED)
        .select_related("last_failed_attempt")
        .prefetch_related(_active_decisions_prefetch())
    )
    for recording in transcribed_qs:
        decision = _first_active(recording)
        if recording.retranscription_failed:
            failed_attempt = recording.last_failed_attempt  # select_related: no query
            retranscription_failed.append(
                {
                    "recording_id": recording.pk,
                    "kind": "failed_retranscription",
                    "attempt_id": failed_attempt.pk if failed_attempt is not None else None,
                    "error_code": failed_attempt.error_code if failed_attempt is not None else "",
                    "route": decision.route_suggestion if decision else None,
                }
            )
        if decision is not None and not decision.routing_verified:
            unverified.append(
                {
                    "recording_id": recording.pk,
                    "kind": "transcribed_routing_unverified",
                    "route": decision.route_suggestion,
                    "confidence": decision.confidence,
                    "profile": decision.profile_name,
                }
            )

    failed = [
        {"recording_id": pk, "kind": f"failed_{stage}"}
        for pk, stage in Recording.objects.filter(processing_status=ProcessingStatus.FAILED).values_list(
            "pk", "failure_stage"
        )
    ]
    awaiting_summary = [
        {"recording_id": pk, "kind": "awaiting_summary"}
        for pk in Recording.objects.filter(
            processing_status=ProcessingStatus.TRANSCRIBED, summary_status=SummaryState.MISSING
        ).values_list("pk", flat=True)
    ]
    failed_summary = [
        {"recording_id": pk, "kind": "failed_summary", "error_code": code or "unknown"}
        for pk, code in Recording.objects.filter(summary_status=SummaryState.FAILED).values_list(
            "pk", "last_failed_attempt__error_code"
        )
    ]
    failed_resummarization = [
        {"recording_id": pk, "kind": "failed_resummarization", "attempt_id": attempt_id}
        for pk, attempt_id in Recording.objects.filter(resummarization_failed=True).values_list(
            "pk", "last_failed_attempt_id"
        )
    ]
    missing_audio = [
        {"recording_id": pk, "kind": "missing_audio"}
        for pk in Recording.objects.filter(audio_status="missing").values_list("pk", flat=True)
    ]
    return {
        "needs_review": needs_review,
        "unverified": unverified,
        "failed_retranscription": retranscription_failed,
        "failed": failed,
        "awaiting_summary": awaiting_summary,
        "failed_summary": failed_summary,
        "failed_resummarization": failed_resummarization,
        "missing_audio": missing_audio,
    }
