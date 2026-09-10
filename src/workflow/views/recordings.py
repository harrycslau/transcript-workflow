"""Recording list, detail, transcript and history views (Step 4).

GET rendering is strictly read-only: no subprocess, no network, no file
hashing, no database writes. Heavy fields (transcript text, raw model
JSON) are never loaded on the list page.
"""

from __future__ import annotations

from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

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
    otherwise cards. Invalid values fall back safely, never an error.
    Reads the POST body for POST-only vector searches and the query
    string for ordinary GET requests."""
    source = request.POST if request.method == "POST" else request.GET
    view = (source.get("view") or "").strip().lower()
    if view in VALID_VIEWS:
        return view
    cookie = (request.COOKIES.get(VIEW_COOKIE) or "").strip().lower()
    if cookie in VALID_VIEWS:
        return cookie
    return "cards"


def _request_view_value(request):
    """The explicit ``view=`` value from the request's own method body,
    or ``None`` when absent/invalid (used only for the cookie decision)."""
    source = request.POST if request.method == "POST" else request.GET
    view = (source.get("view") or "").strip().lower()
    return view if view in VALID_VIEWS else None


def _search_result_context(*, search, filters, view, configured_tags):
    """Shared render context for keyword GET and vector POST results.

    Keyword results keep their GET ``search_qs``/``base_qs`` navigation;
    vector results carry ``filter_pairs``/``search_mode`` so the template
    renders POST-only navigation (the query never enters a URL)."""
    from workflow.services import search_web

    filter_messages = list(filters.errors)
    if filters.sort_error:
        filter_messages.append(filters.sort_error)
    search_qs = ""
    if not search.is_vector and search.echo_query:
        import urllib.parse

        search_qs = urllib.parse.urlencode({"q": search.echo_query})
    filters_qs = filters.as_querystring()
    if search.is_vector:
        # No GET navigation for vector results: the query is never
        # encoded into a URL; every control is a POST form.
        base_qs = ""
    else:
        base_parts = [part for part in (search_qs, filters_qs) if part]
        base_qs = "&".join(base_parts + [f"view={view}"])
    return {
        "searching": True,
        "search": search,
        # None on invalid/index/unavailable states: the rejected query is
        # never echoed anywhere in the response.
        "search_query": search.echo_query,
        "search_qs": search_qs,
        "note_unscoped": (
            search_web.NOTE_UNSCOPED_FILTERS if search.unscoped_filters else ""
        ),
        "filters": filters,
        "filter_errors": filter_messages,
        "filters_qs": filters_qs,
        "filter_pairs": filters.as_pairs(),
        "base_qs": base_qs,
        "effective_view": view,
        "vector_mode": search.is_vector,
        "search_mode": search.mode,
        "show_month_headings": filters.sort in ("newest", "oldest"),
        "configured_tags": configured_tags,
    }


def recording_list(request):
    config = get_config()
    raw_q = request.GET.get("q")
    # A missing or blank/whitespace q is the NORMAL Library: no
    # validation, no health gate, no engine call — never an
    # "invalid query" state. A forged ``mode=semantic|hybrid`` on a GET
    # is IGNORED: GET stays strictly read-only keyword/library, with zero
    # embedding or network calls.
    searching = raw_q is not None and bool(raw_q.strip())
    # Search mode extends the sort contract (relevance default/fallback
    # through the structured sort_error channel); Library mode is the
    # historical parser.
    filters = list_filters(request.GET, config.timezone, allow_relevance=searching)
    view = _effective_view(request)
    from workflow.models import Tag

    search = None
    if searching:
        from workflow.services import search_web

        search = search_web.run_web_search(
            raw_query=raw_q,
            filters=filters,
            timezone_name=config.timezone,
            page_number=request.GET.get("page"),
            per_page=config.web.recordings_per_page,
            segments_per_page=config.web.transcript_segments_per_page,
        )

    if search is not None:
        # Search results REPLACE the normal Library list on this same
        # page. The engine already applied filters, truncation flags
        # and ranking; sorting/pagination happened in the service —
        # this branch only renders.
        context = _search_result_context(
            search=search,
            filters=filters,
            view=view,
            configured_tags=Tag.objects.filter(is_configured=True).order_by("name"),
        )
    else:
        filters_qs = filters.as_querystring()
        queryset = recording_list_queryset()
        if filters.valid:
            queryset = apply_filters(queryset, filters, config.timezone)
        paginator = Paginator(queryset, config.web.recordings_per_page)
        page = paginator.get_page(request.GET.get("page"))
        cards = [RecordingCard(recording) for recording in page.object_list]

        base_qs = filters_qs + (f"&view={view}" if filters_qs else f"view={view}")
        context = {
            "searching": False,
            "search_query": None,
            "search_qs": "",
            "cards": cards,
            "page": page,
            "filters": filters,
            "filter_errors": filters.errors,
            "filters_qs": filters_qs,
            "filter_pairs": filters.as_pairs(),
            "base_qs": base_qs,
            "effective_view": view,
            "vector_mode": False,
            "search_mode": "",
            "show_month_headings": filters.sort in ("newest", "oldest"),
            "configured_tags": Tag.objects.filter(is_configured=True).order_by("name"),
        }

    response = render(request, "workflow/recording_list.html", context)
    explicit_view = _request_view_value(request)
    if explicit_view is not None:
        # Server-owned preference; explicit query param always wins over
        # the cookie, and a valid explicit value refreshes it.
        response.set_cookie(
            VIEW_COOKIE,
            explicit_view,
            max_age=VIEW_COOKIE_MAX_AGE,
            samesite="Lax",
            path="/",
            httponly=True,
            secure=request.is_secure(),
        )
    return response


@require_POST
def recording_search(request):
    """Dedicated POST-only semantic/hybrid Library search (Step 5C).

    ``require_POST`` makes every GET a 405 BEFORE any config, health,
    embedding or database work. The cheap mode/query/filter validation
    runs before the service (which owns the one health sweep and at most
    one localhost embedding request); invalid filters REJECT instead of
    widening to an unscoped search. The response re-renders the ordinary
    Library results template — no persistence, no redirect, no PRG; a
    browser refresh deliberately reruns the search. The query never
    appears in a URL, redirect, log or error.
    """
    config = get_config()
    view = _effective_view(request)
    from workflow.models import Tag
    from workflow.services import search_web

    mode = (request.POST.get("mode") or "").strip().lower()
    raw_q = request.POST.get("q")
    filters = list_filters(request.POST, config.timezone, allow_relevance=True)
    search = search_web.run_web_vector_search(
        mode=mode,
        raw_query=raw_q,
        filters=filters,
        timezone_name=config.timezone,
        page_number=request.POST.get("page"),
        per_page=config.web.recordings_per_page,
        config=config,
        segments_per_page=config.web.transcript_segments_per_page,
    )
    context = _search_result_context(
        search=search,
        filters=filters,
        view=view,
        configured_tags=Tag.objects.filter(is_configured=True).order_by("name"),
    )
    response = render(request, "workflow/recording_list.html", context)
    explicit_view = _request_view_value(request)
    if explicit_view is not None:
        response.set_cookie(
            VIEW_COOKIE,
            explicit_view,
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
