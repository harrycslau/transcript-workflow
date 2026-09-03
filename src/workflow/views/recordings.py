"""Recording list, detail, transcript and history views (Step 4).

GET rendering is strictly read-only: no subprocess, no network, no file
hashing, no database writes. Heavy fields (transcript text, raw model
JSON) are never loaded on the list page.
"""

from __future__ import annotations

from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, render

from workflow.models import ProcessingStatus, Recording, Summary, SummaryState, Transcript
from workflow.query import (
    ListFilters,
    RecordingCard,
    apply_filters,
    list_filters,
    recording_detail_queryset,
    recording_list_queryset,
)
from workflow.views.helpers import get_config
from workflow.services.web_actions import attempt_summary_for_display

VIEW_COOKIE = "brain_view_pref"
VALID_VIEWS = ("cards", "table")
VIEW_COOKIE_MAX_AGE = 31536000  # one year


def _effective_view(request) -> str:
    """Explicit valid ``view=`` wins; otherwise the validated cookie;
    otherwise cards. Invalid values fall back safely, never an error."""
    view = (request.GET.get("view") or "").strip().lower()
    if view in VALID_VIEWS:
        return view
    cookie = (request.COOKIES.get(VIEW_COOKIE) or "").strip().lower()
    if cookie in VALID_VIEWS:
        return cookie
    return "cards"


def recording_list(request):
    config = get_config()
    filters = list_filters(request.GET, config.timezone)
    queryset = recording_list_queryset()
    if filters.valid:
        queryset = apply_filters(queryset, filters, config.timezone)
    paginator = Paginator(queryset, config.web.recordings_per_page)
    page = paginator.get_page(request.GET.get("page"))
    cards = [RecordingCard(recording) for recording in page.object_list]
    from workflow.models import Tag

    view = _effective_view(request)
    filters_qs = filters.as_querystring()
    base_qs = filters_qs + (f"&view={view}" if filters_qs else f"view={view}")

    context = {
        "cards": cards,
        "page": page,
        "filters": filters,
        "filter_errors": filters.errors,
        "filters_qs": filters_qs,
        "base_qs": base_qs,
        "effective_view": view,
        "show_month_headings": filters.sort in ("newest", "oldest"),
        "configured_tags": Tag.objects.filter(is_configured=True).order_by("name"),
    }
    response = render(request, "workflow/recording_list.html", context)
    if request.GET.get("view") in VALID_VIEWS:
        # Server-owned preference; explicit query param always wins over
        # the cookie, and a valid explicit value refreshes it.
        response.set_cookie(
            VIEW_COOKIE,
            view,
            max_age=VIEW_COOKIE_MAX_AGE,
            samesite="Lax",
            path="/",
            httponly=True,
            secure=request.is_secure(),
        )
    return response


def _detail_base(request, recording_id):
    recording = recording_detail_queryset(recording_id).first()
    if recording is None:
        raise Http404("Recording not found")
    config = get_config()
    card = RecordingCard(recording)
    return config, recording, card


def _action_availability(config, recording) -> dict:
    from workflow.services.web_actions import (
        retry_eligible,
        route_eligible,
        state_fingerprint,
        summarize_mode,
    )

    profiles = sorted(config.macwhisper.routing.profiles.values(), key=lambda p: p.name)
    decision = recording.routing_decisions.filter(is_active=True).first()
    return {
        "fingerprint": state_fingerprint(recording),
        "route_eligible": route_eligible(recording),
        "route_profiles": [
            {
                "name": profile.name,
                "model": profile.model,
                "language": profile.language,
                "manual_only": profile.manual_only,
            }
            for profile in profiles
        ],
        "confirm_routing_available": decision is not None and not decision.routing_verified,
        "transcribe_available": recording.processing_status == ProcessingStatus.READY_TO_TRANSCRIBE,
        "summarize_mode": summarize_mode(recording),
        "retry_available": retry_eligible(recording),
    }


def recording_detail(request, recording_id):
    config, recording, card = _detail_base(request, recording_id)
    transcript = recording.transcripts.filter(is_active=True).first()
    transcript_segment_count = transcript.segments.count() if transcript is not None else 0
    per_page = config.web.transcript_segments_per_page
    segments = []
    segment_pages = 0
    if transcript is not None:
        paginator = Paginator(transcript.segments.order_by("ordinal"), per_page)
        first_page = paginator.get_page(1)
        segments = list(first_page.object_list)
        segment_pages = paginator.num_pages
    summaries = recording.summaries.order_by("-ordinal").only(
        "id", "recording", "transcript", "ordinal", "title", "is_active", "created_at",
        "transcript", "section",
    )
    from workflow.models import Tag

    tag_choices = Tag.objects.filter(is_configured=True).order_by("name")
    retired_tag_choices = Tag.objects.filter(is_configured=False).order_by("name")

    # Read-only variant view-model: resolves the requested selector and
    # provides the selected summary, variant state, action mode and all
    # tab options. Unknown concrete languages are a friendly 404.
    from workflow.services.variant_view import build_variant_view

    variant = build_variant_view(recording, request.GET.get("language", "default"))
    if variant.error:
        raise Http404(
            f"No summary variant '{request.GET.get('language')}' exists for this recording."
        )

    context = {
        "card": card,
        "recording": recording,
        "transcript": transcript,
        "transcript_segment_count": transcript_segment_count,
        "segments": segments,
        "segment_pages": segment_pages,
        "summaries": summaries,
        "variant": variant,
        # The displayed summary is the SELECTED variant's summary —
        # never an arbitrary active row.
        "current_summary": variant.summary,
        "default_summary": variant.default_summary,
        "attempts": attempt_summary_for_display(recording, limit=8),
        "actions": _action_availability(config, recording),
        "routing_decision": card.active_route,
        "tag_choices": tag_choices,
        "retired_tag_choices": retired_tag_choices,
        "default_output_language": variant.default_language,
        "selected_language": variant.requested,
    }
    return render(request, "workflow/recording_detail.html", context)


def recording_summary(request, recording_id):
    config, recording, card = _detail_base(request, recording_id)
    from workflow.services.variant_view import build_variant_view

    variant = build_variant_view(recording, request.GET.get("language", "default"))
    if variant.error:
        raise Http404(
            f"No summary variant '{request.GET.get('language')}' exists for this recording."
        )
    context = {
        "card": card,
        "recording": recording,
        "variant": variant,
        "summary": variant.summary,
        "selected_language": variant.requested,
        "default_output_language": variant.default_language,
        "actions": _action_availability(config, recording),
    }
    return render(request, "workflow/recording_summary.html", context)


def summary_detail(request, recording_id, summary_id):
    config, recording, card = _detail_base(request, recording_id)
    summary = get_object_or_404(
        Summary.objects.select_related("transcript", "section"),
        pk=summary_id,
        recording_id=recording.pk,
    )
    context = {
        "card": card,
        "recording": recording,
        "summary": summary,
        # Currency is derived, never inferred from is_active: an
        # old-transcript summary may still be active in its own scope.
        "is_current": (
            summary.is_active
            and summary.transcript.is_active
            and summary.section_id is not None
            and summary.section.ordinal == 0
            and summary.transcript.recording_id == recording.pk
        ),
        "actions": _action_availability(config, recording),
    }
    return render(request, "workflow/summary_detail.html", context)


def recording_transcript(request, recording_id):
    config, recording, card = _detail_base(request, recording_id)
    version = request.GET.get("v")
    if version:
        try:
            transcript = recording.transcripts.get(pk=version)
        except (Transcript.DoesNotExist, ValueError):
            raise Http404("Transcript version not found for this recording") from None
    else:
        transcript = recording.transcripts.filter(is_active=True).first()
        if transcript is None:
            raise Http404("No active transcript for this recording")
    paginator = Paginator(transcript.segments.order_by("ordinal"), config.web.transcript_segments_per_page)
    page = paginator.get_page(request.GET.get("page"))
    context = {
        "card": card,
        "recording": recording,
        "transcript": transcript,
        "is_active_version": transcript.is_active,
        "page_obj": page,
        "segment_count": paginator.count,
    }
    return render(request, "workflow/recording_transcript.html", context)


def recording_history(request, recording_id):
    config, recording, card = _detail_base(request, recording_id)
    from django.db.models import Count

    transcripts = recording.transcripts.order_by("-created_at").annotate(
        segment_count=Count("segments")
    )
    summaries = recording.summaries.order_by("-ordinal").select_related("transcript", "section")
    attempts = attempt_summary_for_display(recording, limit=20)
    context = {
        "card": card,
        "recording": recording,
        "transcripts": transcripts,
        "summaries": summaries,
        "attempts": attempts,
    }
    return render(request, "workflow/recording_history.html", context)
