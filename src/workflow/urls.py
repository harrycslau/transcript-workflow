"""Workflow app URL routes (Step 4).

All mutating endpoints are POST-only. Child Summary/Transcript objects
are always resolved through the parent Recording in the views, so a
mismatched URL is a 404 — never cross-recording access.
"""

from django.urls import path

from workflow.views import actions, ask, exports, recordings, review, segmentation, tags
from workflow import views

urlpatterns = [
    path("", views.redirect_to_recordings, name="home"),
    path("status/", views.home, name="status"),
    path("health/", views.health, name="health"),

    # Ask with citations (Step 5D). GET renders the form and does zero
    # health/embedding/chat work; POST executes the read-only Ask.
    path("ask/", ask.ask_view, name="ask"),

    path("recordings/", recordings.recording_list, name="recordings"),
    # Dedicated POST-only semantic/hybrid Library search (Step 5C). GET is
    # a 405 with no config/health/network/DB work; the query never enters
    # a URL.
    path("recordings/search/", recordings.recording_search, name="recording-search"),
    path("recordings/<uuid:recording_id>/", recordings.recording_detail, name="recording-detail"),
    path("recordings/<uuid:recording_id>/summary/", recordings.recording_summary, name="recording-summary"),
    # Summary ids are model CharField(36) primary keys (UUID-producing by
    # default, but manually supplied/legacy non-UUID values are valid), so
    # the converter is ``str`` — the parent-scoped lookup below still
    # enforces the recording boundary.
    path(
        "recordings/<uuid:recording_id>/summaries/<str:summary_id>/",
        recordings.summary_detail,
        name="summary-detail",
    ),
    path("recordings/<uuid:recording_id>/transcript/", recordings.recording_transcript, name="recording-transcript"),
    path("recordings/<uuid:recording_id>/history/", recordings.recording_history, name="recording-history"),

    # Step 6.1 segmented-version save (POST-only, two-step confirmation).
    path(
        "recordings/<uuid:recording_id>/transcript/save/",
        segmentation.action_segmentation_save,
        name="action-segmentation-save",
    ),

    # Step 6.2 topic-section detail (read route) and its summary action
    # (POST-only, two-step confirmation). Section pks are BigAutoField
    # ints; the parent Recording scope is enforced in the views.
    path(
        "recordings/<uuid:recording_id>/sections/<int:section_id>/",
        recordings.section_detail,
        name="section-detail",
    ),
    path(
        "recordings/<uuid:recording_id>/sections/<int:section_id>/summarize/",
        actions.action_section_summarize,
        name="action-section-summarize",
    ),

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
    # Section summary export (current selected variant only).
    path(
        "recordings/<uuid:recording_id>/sections/<int:section_id>/summary/export/",
        exports.section_summary_export,
        name="section-summary-export",
    ),

    # Tag mutations (POST).
    path("recordings/<uuid:recording_id>/tags/apply/", tags.tag_apply, name="tag-apply"),
    path("recordings/<uuid:recording_id>/tags/add/", tags.tag_add, name="tag-add"),
    path("recordings/<uuid:recording_id>/tags/create/", tags.tag_create, name="tag-create"),
    path("recordings/<uuid:recording_id>/tags/<int:tag_id>/confirm/", tags.tag_confirm, name="tag-confirm"),
    path("recordings/<uuid:recording_id>/tags/<int:tag_id>/remove/", tags.tag_remove, name="tag-remove"),
    path(
        "recordings/<uuid:recording_id>/sections/<int:section_id>/tags/apply/",
        tags.section_tag_apply,
        name="section-tag-apply",
    ),
    path(
        "recordings/<uuid:recording_id>/sections/<int:section_id>/tags/<int:tag_id>/confirm/",
        tags.section_tag_confirm,
        name="section-tag-confirm",
    ),
    path(
        "recordings/<uuid:recording_id>/sections/<int:section_id>/tags/<int:tag_id>/remove/",
        tags.section_tag_remove,
        name="section-tag-remove",
    ),

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
