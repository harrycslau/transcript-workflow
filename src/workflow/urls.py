"""Workflow app URL routes (Step 4).

All mutating endpoints are POST-only. Child Summary/Transcript objects
are always resolved through the parent Recording in the views, so a
mismatched URL is a 404 — never cross-recording access.
"""

from django.urls import path

from workflow.views import actions, exports, recordings, review, tags
from workflow import views

urlpatterns = [
    path("", views.home, name="home"),
    path("health/", views.health, name="health"),

    path("recordings/", recordings.recording_list, name="recordings"),
    path("recordings/<uuid:recording_id>/", recordings.recording_detail, name="recording-detail"),
    path("recordings/<uuid:recording_id>/summary/", recordings.recording_summary, name="recording-summary"),
    path(
        "recordings/<uuid:recording_id>/summaries/<uuid:summary_id>/",
        recordings.summary_detail,
        name="summary-detail",
    ),
    path("recordings/<uuid:recording_id>/transcript/", recordings.recording_transcript, name="recording-transcript"),
    path("recordings/<uuid:recording_id>/history/", recordings.recording_history, name="recording-history"),

    path("tags/", tags.tag_list, name="tags"),

    path("review/", review.review, name="review"),

    # Exports (GET, read-only).
    path(
        "recordings/<uuid:recording_id>/summary/export/", exports.summary_export, name="summary-export"
    ),
    path(
        "recordings/<uuid:recording_id>/transcript/export/",
        exports.transcript_export,
        name="transcript-export",
    ),

    # Tag mutations (POST).
    path("recordings/<uuid:recording_id>/tags/add/", tags.tag_add, name="tag-add"),
    path("recordings/<uuid:recording_id>/tags/<int:tag_id>/confirm/", tags.tag_confirm, name="tag-confirm"),
    path("recordings/<uuid:recording_id>/tags/<int:tag_id>/remove/", tags.tag_remove, name="tag-remove"),

    # Pipeline actions (POST, two-step confirmation).
    path("recordings/<uuid:recording_id>/route/", actions.action_route, name="action-route"),
    path(
        "recordings/<uuid:recording_id>/confirm-routing/",
        actions.action_confirm_routing,
        name="action-confirm-routing",
    ),
    path("recordings/<uuid:recording_id>/transcribe/", actions.action_transcribe, name="action-transcribe"),
    path("recordings/<uuid:recording_id>/summarize/", actions.action_summarize, name="action-summarize"),
    path("recordings/<uuid:recording_id>/retry/", actions.action_retry, name="action-retry"),
]
