"""Recording list, detail, transcript and history views (Step 4).

GET rendering is strictly read-only: no subprocess, no network, no file
hashing, no database writes. Heavy fields (transcript text, raw model
JSON) are never loaded on the list page.
"""

from __future__ import annotations

from django.core.paginator import Paginator
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from workflow.models import (
    AttemptStage,
    AudioStatus,
    ProcessingStatus,
    Recording,
    Summary,
    SummaryState,
    Transcript,
)
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

# v6 detail/history bounds: exactly five active-transcript preview
# segments on the detail page; every potentially long History collection
# is capped at one local constant with a visible truncation notice.
DETAIL_PREVIEW_SEGMENTS = 5
HISTORY_LIMIT = 100


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


def _keyword_search_redirect(request, config):
    """Keyword POST → the canonical bookmarkable Library GET.

    A keyword submission from the unified top bar redirects (302) to the
    ordinary GET ``/recordings/?q=...`` page, preserving the canonical
    active ``filter_pairs`` and the effective view. A blank/whitespace
    query drops the query itself but still keeps the submitted filters
    and view (exactly the GET behaviour where a missing/blank ``q`` is
    the normal Library listing under those filters). The query text
    enters the URL only in this keyword case; semantic/hybrid never
    redirect.
    """
    import urllib.parse

    from django.urls import reverse

    raw_q = request.POST.get("q")
    has_query = raw_q is not None and bool(raw_q.strip())
    filters = list_filters(
        request.POST, config.timezone, allow_relevance=has_query
    )
    params: dict[str, list[str]] = {}
    if has_query:
        params["q"] = [raw_q.strip()]
    for name, value in filters.as_pairs():
        params.setdefault(name, []).append(value)
    params["view"] = [_effective_view(request)]
    return redirect(
        reverse("recordings") + "?" + urllib.parse.urlencode(params, doseq=True)
    )


@require_POST
def recording_search(request):
    """Unified POST-only Library search (Step 5C extended).

    ``require_POST`` makes every GET a 405 BEFORE any config, health,
    embedding or database work. The unified top bar POSTs every mode here:

    - ``keyword`` redirects to the canonical bookmarkable GET
      ``/recordings/?q=...`` (preserving active ``filter_pairs`` and the
      effective view); the query then lives only in that URL.
    - ``semantic``/``hybrid`` stay direct POST-only: the cheap
      mode/query/filter validation runs before the service (which owns
      the one health sweep and at most one localhost embedding request);
      invalid filters REJECT instead of widening to an unscoped search.
      The response re-renders the ordinary Library results template — no
      persistence, no redirect, no PRG; a browser refresh deliberately
      reruns the search. The query never appears in a URL, redirect, log
      or error.
    - any other mode is rejected by the vector service with a fixed
      message and zero embedding/network work.
    """
    config = get_config()
    view = _effective_view(request)
    from workflow.models import Tag
    from workflow.services import search_web

    mode = (request.POST.get("mode") or "").strip().lower()
    if mode == search_web.MODE_KEYWORD:
        return _keyword_search_redirect(request, config)
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


def _status_panel(recording: Recording, routing_decision) -> dict:
    """Composite read-only status/next-action presentation (v6).

    Pure function over persisted state — SELECTs only, never writes,
    never touches files/network. Returns a ``{level, label, detail}``
    dict where ``level`` is one of ``ok``/``warn``/``danger``/
    ``running``/``neutral`` and drives the single status panel on the
    recording detail page.
    """
    from workflow.services.web_actions import unfinished_attempt_stage

    stage = unfinished_attempt_stage(recording)
    if stage is not None:
        return {
            "level": "running",
            "label": "Running",
            "detail": f"A {stage} attempt is currently running.",
        }
    if recording.processing_status in (
        ProcessingStatus.HASHING,
        ProcessingStatus.ROUTING,
        ProcessingStatus.TRANSCRIBING,
    ):
        return {
            "level": "running",
            "label": "Running",
            "detail": "A pipeline stage is currently running.",
        }
    if recording.processing_status == ProcessingStatus.FAILED:
        return {
            "level": "danger",
            "label": "Failed",
            "detail": "Routing or transcription failed — retry is available.",
        }
    if recording.retranscription_failed:
        return {
            "level": "warn",
            "label": "Retranscription failed",
            "detail": "The existing transcript stays active — retry is available.",
        }
    if recording.processing_status == ProcessingStatus.NEEDS_REVIEW:
        return {
            "level": "warn",
            "label": "Routing needs review",
            "detail": "Confirm the routing profile or route manually before transcription.",
        }
    if recording.processing_status == ProcessingStatus.READY_TO_TRANSCRIBE:
        return {
            "level": "warn",
            "label": "Ready to transcribe",
            "detail": "Transcription has not started yet.",
        }
    if recording.processing_status == ProcessingStatus.DISCOVERED:
        return {
            "level": "neutral",
            "label": "Discovered",
            "detail": "Waiting for the next pipeline run to route it.",
        }
    if recording.summary_status == SummaryState.FAILED:
        return {
            "level": "danger",
            "label": "Summary failed",
            "detail": "The last summarization attempt failed — retry is available.",
        }
    if recording.resummarization_failed:
        return {
            "level": "warn",
            "label": "Re-summarization failed",
            "detail": "The current summary was kept — retry is available.",
        }
    if recording.audio_status == AudioStatus.MISSING:
        return {
            "level": "warn",
            "label": "Audio missing",
            "detail": "The source audio file is no longer present.",
        }
    if recording.summary_status == SummaryState.MISSING:
        return {
            "level": "warn",
            "label": "Summary not generated",
            "detail": "An active transcript exists but the current summary is missing.",
        }
    if (
        recording.processing_status == ProcessingStatus.TRANSCRIBED
        and routing_decision is not None
        and not routing_decision.routing_verified
    ):
        return {
            "level": "warn",
            "label": "Transcribed — routing unverified",
            "detail": "Confirm the automatic routing decision.",
        }
    return {
        "level": "ok",
        "label": "Transcribed",
        "detail": "Summary current — no action required.",
    }


def _format_confidence(value) -> str | None:
    """Two-decimal label for a routing confidence score, or None when
    absent/unusable. Safe bounded formatting — never raw evidence."""
    if value is None:
        return None
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return None


def _routing_rows_for_display(recording: Recording, limit: int) -> tuple[list[dict], bool]:
    """Allowlisted routing-history rows (v6).

    Exposes ONLY safe fields: timestamp, profile, method, verification,
    model, a bounded confidence label and the stable reason code. The
    query projects exactly those allowlisted columns via ``.only(...)``
    so the raw ``evidence`` JSON is never loaded, and the adapter never
    renders it.
    """
    decisions = list(
        recording.routing_decisions.order_by("-ordinal")
        .only(
            # "recording" (the FK column) must be loaded: a deferred FK
            # triggers a refresh_from_db per row during iteration.
            "recording",
            "created_at",
            "profile_name",
            "method",
            "routing_verified",
            "model_id",
            "confidence",
            "reason_code",
        )[: limit + 1]
    )
    truncated = len(decisions) > limit
    rows = [
        {
            "created_at": decision.created_at,
            "profile_name": decision.profile_name,
            "method": decision.method,
            "verified_label": "yes" if decision.routing_verified else "no",
            "model_id": decision.model_id,
            "confidence": _format_confidence(decision.confidence),
            "reason_code": decision.reason_code,
        }
        for decision in decisions[:limit]
    ]
    return rows, truncated


def recording_detail(request, recording_id):
    config, recording, card = _detail_base(request, recording_id)
    transcript = recording.transcripts.filter(is_active=True).first()
    transcript_segment_count = 0
    preview_segments: list = []
    if transcript is not None:
        transcript_segment_count = transcript.segments.count()
        # Exactly DETAIL_PREVIEW_SEGMENTS active-transcript segments; the
        # accurate total and the full-transcript link are rendered around
        # them (the transcript page owns pagination and anchors).
        preview_segments = list(
            transcript.segments.order_by("ordinal")[:DETAIL_PREVIEW_SEGMENTS]
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
        "preview_segments": preview_segments,
        "variant": variant,
        # The displayed summary is the SELECTED variant's summary —
        # never an arbitrary active row.
        "current_summary": variant.summary,
        "default_summary": variant.default_summary,
        "actions": _action_availability(config, recording),
        "routing_decision": card.active_route,
        "status": _status_panel(recording, card.active_route),
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
    # Metadata rendering (model, language, ...) reads the attempt row, so
    # the transcript is fetched with select_related("attempt") on BOTH the
    # active and the historical (?v=) path — no incidental lazy query.
    transcripts_qs = recording.transcripts.select_related("attempt")
    if version:
        try:
            transcript = transcripts_qs.get(pk=version)
        except (Transcript.DoesNotExist, ValueError):
            raise Http404("Transcript version not found for this recording") from None
    else:
        transcript = transcripts_qs.filter(is_active=True).first()
        if transcript is None:
            raise Http404("No active transcript for this recording")
    paginator = Paginator(transcript.segments.order_by("ordinal"), config.web.transcript_segments_per_page)
    page = paginator.get_page(request.GET.get("page"))
    model_id = transcript.attempt.model_id if transcript.attempt_id is not None else ""
    context = {
        "card": card,
        "recording": recording,
        "recording_title": card.title,
        "transcript": transcript,
        "is_active_version": transcript.is_active,
        "page_obj": page,
        "segment_count": paginator.count,
        "transcript_model": model_id,
        "transcript_language": transcript.language_observed,
        "transcript_duration": recording.duration_seconds,
        # Export URL fragment preserving the historical version (the
        # active transcript uses the plain export URL). ``&`` is
        # autoescaped in the template, decoded by browsers/JS.
        "version_query": f"&version={transcript.pk}" if not transcript.is_active else "",
    }
    return render(request, "workflow/recording_transcript.html", context)


def recording_history(request, recording_id):
    config, recording, card = _detail_base(request, recording_id)

    # Bounded queries: every potentially long collection is fetched with
    # a limit+1 sentinel row so the truncation notice is exact without
    # separate count queries, and there is no N+1 (segment counts come
    # from ONE annotated query; attempt rows are pre-sanitized).
    transcripts = list(
        recording.transcripts.order_by("-created_at")
        .annotate(segment_count=Count("segments"))[: HISTORY_LIMIT + 1]
    )
    transcripts_truncated = len(transcripts) > HISTORY_LIMIT
    transcripts = transcripts[:HISTORY_LIMIT]

    summaries = list(
        recording.summaries.order_by("-ordinal")
        .select_related("transcript", "section")[: HISTORY_LIMIT + 1]
    )
    summaries_truncated = len(summaries) > HISTORY_LIMIT
    summaries = summaries[:HISTORY_LIMIT]

    attempts = attempt_summary_for_display(recording, limit=HISTORY_LIMIT)
    attempts_truncated = recording.attempts.count() > HISTORY_LIMIT
    # One-pass segment counts for transcription attempts (from the
    # already-fetched transcript rows — never a per-attempt query).
    segments_by_attempt = {t.attempt_id: t.segment_count for t in transcripts}
    for row in attempts:
        if row["stage"] == AttemptStage.TRANSCRIPTION:
            row["segment_count"] = segments_by_attempt.get(row["id"])

    routing_rows, routing_truncated = _routing_rows_for_display(recording, HISTORY_LIMIT)

    context = {
        "card": card,
        "recording": recording,
        "transcripts": transcripts,
        "transcripts_truncated": transcripts_truncated,
        "summaries": summaries,
        "summaries_truncated": summaries_truncated,
        "attempts": attempts,
        "attempts_truncated": attempts_truncated,
        "routing_rows": routing_rows,
        "routing_truncated": routing_truncated,
        "history_limit": HISTORY_LIMIT,
    }
    return render(request, "workflow/recording_history.html", context)
