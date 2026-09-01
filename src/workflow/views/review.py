"""Review dashboard (Step 4).

Read-only: renders the shared review report (the same builder the CLI
uses). No lock, no recovery, no processing on GET. Groups link to the
recording detail pages and show stable codes only.
"""

from __future__ import annotations

from django.shortcuts import render

from workflow.services.review import build_review_report
from workflow.views.helpers import get_config

_GROUP_META = [
    ("needs_review", "Routing needs review", "needs_review"),
    ("unverified", "Transcribed with unverified automatic routing", "unverified"),
    ("failed_retranscription", "Failed retranscription (transcript kept)", "retranscription_failed"),
    ("failed", "Pipeline failures", "failed"),
    ("awaiting_summary", "Awaiting first summary", "awaiting_summary"),
    ("failed_summary", "First summary failed", "summary_failed"),
    ("failed_resummarization", "Re-summarization failed (current summary kept)", "resummarization_failed"),
    ("missing_audio", "Missing audio", "missing_audio"),
]


def review(request):
    get_config()  # fail fast on broken config, same as other pages
    report = build_review_report()
    recording_ids: set[str] = set()
    for key, _label, _slug in _GROUP_META:
        for item in report.get(key, []):
            recording_ids.add(item["recording_id"])
    from workflow.models import Recording

    recordings = {
        recording.pk: recording
        for recording in Recording.objects.filter(pk__in=recording_ids).only(
            "id", "sha256", "recorded_at", "discovered_at"
        )
    }
    groups = []
    for key, label, slug in _GROUP_META:
        items = []
        for item in report.get(key, []):
            entry = dict(item)
            recording = recordings.get(item["recording_id"])
            entry["recording"] = recording
            entry["display_code"] = (
                item.get("error_code")
                or item.get("reason_code")
                or item.get("kind")
            )
            items.append(entry)
        groups.append({"key": key, "label": label, "slug": slug, "items": items, "count": len(items)})
    context = {"groups": groups, "total": sum(group["count"] for group in groups)}
    return render(request, "workflow/review.html", context)
