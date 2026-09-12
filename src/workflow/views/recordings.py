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
from django.urls import reverse
from django.views.decorators.http import require_POST

from workflow.models import (
    AttemptStage,
    AudioStatus,
    ProcessingStatus,
    Recording,
    Section,
    SegmentedVersion,
    Summary,
    SummaryState,
    Transcript,
)
from workflow.query import (
    ListFilters,
    RecordingCard,
    list_filters,
    recording_detail_queryset,
    section_duration_seconds,
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

# Section pks are Django/SQLite BigAutoFields (signed 64-bit integers).
# ``return_section`` values are validated as canonical positive decimal
# strings and capped to this range BEFORE any ORM use.
_MAX_BIGAUTOFIELD_PK = 2**63 - 1
_MAX_BIGAUTOFIELD_PK_DIGITS = len(str(_MAX_BIGAUTOFIELD_PK))


def _effective_view(request) -> str:
    """Explicit valid ``view=`` wins; otherwise the validated cookie;
    otherwise cards. Invalid values fall back safely, never an error.
    Reads the POST body for POST-only vector searches and the query
    string for ordinary GET requests."""
    source = request.POST if request.method == "POST" else request.GET
    view = (source.get("view") or "").strip().lower()
    if view in VALID_VIEWS:
        return view
    return _cookie_only_view(request)


def _cookie_only_view(request) -> str:
    """The validated view COOKIE only, or ``cards``. Used when a
    library-return token is present: the token is the SOLE carrier of
    state, so a raw ``view=`` query parameter must never affect rendering
    (nor the view cookie)."""
    cookie = (request.COOKIES.get(VIEW_COOKIE) or "").strip().lower()
    return cookie if cookie in VALID_VIEWS else "cards"


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

    # Safe Library-return token (Step 6.2a): when a ``lib_return``
    # parameter is present it is the SOLE carrier of the normal-Library
    # state (canonical validated filter/sort pairs + positive page +
    # cards/table view). A valid token completely defines the render and
    # the raw query string (including any ``q``) is ignored; an invalid/
    # forged/oversized token falls back to the PLAIN Library (default
    # filters, cookie/default only, page 1). Tokens never encode
    # search state and are never generated for search results.
    from workflow.services import library_return

    library_return_state = None
    library_return_present = "lib_return" in request.GET
    if library_return_present:
        library_return_state = library_return.decode_token(
            request.GET.get("lib_return"), config.timezone
        )

    if library_return_state is not None:
        # The token is the sole carrier of state: ``searching`` stays
        # False, ``filters``/``view``/``page_number`` come from the
        # validated token, and the raw query string (including any ``q``
        # or forged ``mode=``) is ignored.
        searching = False
        filters = library_return_state.filters
        view = library_return_state.view
        page_number = str(library_return_state.page)
    elif library_return_present:
        # An invalid/forged/oversized/non-canonical token: the Library
        # falls back to its PLAIN state (default filters, view from the
        # COOKIE only, page 1) — the token is the sole carrier, so the
        # rest of the raw query string (including any raw ``view=``) is
        # ignored entirely.
        searching = False
        filters = ListFilters()
        view = _cookie_only_view(request)
        page_number = None
    else:
        # A missing or blank/whitespace q is the NORMAL Library: no
        # validation, no health gate, no engine call — never an
        # "invalid query" state. A forged ``mode=semantic|hybrid`` on a
        # GET is IGNORED: GET stays strictly read-only keyword/library,
        # with zero embedding or network calls.
        searching = raw_q is not None and bool(raw_q.strip())
        # Search mode extends the sort contract (relevance default/fallback
        # through the structured sort_error channel); Library mode is the
        # historical parser.
        filters = list_filters(request.GET, config.timezone, allow_relevance=searching)
        view = _effective_view(request)
        page_number = request.GET.get("page")
    from workflow.models import Tag

    search = None
    if searching:
        from workflow.services import search_web

        search = search_web.run_web_search(
            raw_query=raw_q,
            filters=filters,
            timezone_name=config.timezone,
            page_number=page_number,
            per_page=config.web.recordings_per_page,
            segments_per_page=config.web.transcript_segments_per_page,
        )

    if search is not None:
        # Search results REPLACE the normal Library list on this same
        # page. The engine already applied filters, truncation flags
        # and ranking; sorting/pagination happened in the service —
        # this branch only renders. NO library-return token is generated
        # for search results (no search-origin support).
        context = _search_result_context(
            search=search,
            filters=filters,
            view=view,
            configured_tags=Tag.objects.filter(is_configured=True).order_by("name"),
        )
    else:
        filters_qs = filters.as_querystring()
        # Step 6.2 Library item projection: the normal overview unit is a
        # derived Library item (recording-backed or active-topic-section-
        # backed). Filters/pagination/order all happen database-side over
        # the UNION before hydration; search results are untouched.
        from workflow.query import (
            apply_item_sort,
            hydrate_library_items,
            library_item_count,
            library_item_queryset,
        )

        # The Library projection's active-topic-layout state is a LAZY
        # parameterized DB-side subquery embedded in both UNION branches —
        # no Python-side expansion, no per-render prepass, exact
        # database-side count/order/pagination.
        if filters.valid:
            queryset = library_item_queryset(filters, config.timezone)
            queryset = apply_item_sort(queryset, filters.sort)
            count_filters = filters
        else:
            queryset = library_item_queryset(ListFilters(), config.timezone)
            queryset = apply_item_sort(queryset, "newest")
            count_filters = ListFilters()
        paginator = Paginator(queryset, config.web.recordings_per_page)
        # The presentation projection carries subqueries irrelevant to a
        # COUNT; use the dedicated minimal count query (same branches,
        # same filters — DB-side, never a Python expansion).
        paginator.count = library_item_count(count_filters, config.timezone)
        page = paginator.get_page(page_number)
        cards = hydrate_library_items(page.object_list)

        base_qs = filters_qs + (f"&view={view}" if filters_qs else f"view={view}")
        # ONE server-signed library-return token per normal Library
        # render: encodes the effective (rendered) filter/sort state,
        # the positive page and the cards/table view. It is added to the
        # recording and section links so their breadcrumbs can return
        # here.
        library_return_token = library_return.make_token(
            count_filters, page.number, view
        )
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
            "library_return_token": library_return_token,
        }

    response = render(request, "workflow/recording_list.html", context)
    # A raw ``view=`` query parameter NEVER mutates the view cookie when a
    # library-return token is present: the token is the SOLE carrier of
    # state (its own encoded view), and an invalid token falls back to the
    # cookie/default only.
    if not library_return_present:
        explicit_view = _request_view_value(request)
        if explicit_view is not None:
            # Server-owned preference; explicit query param always wins
            # over the cookie, and a valid explicit value refreshes it.
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
    confirm_routing_available = decision is not None and not decision.routing_verified
    # The compact Routing trigger appears only where OPTIONAL route UI
    # belongs: manual profile selection is hidden when it is already the
    # prominent recommended action (needs-review without a confirmable
    # decision) so the same form is never duplicated.
    show_routing = route_eligible(recording) and (
        recording.processing_status != ProcessingStatus.NEEDS_REVIEW
        or confirm_routing_available
    )
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
        "confirm_routing_available": confirm_routing_available,
        "transcribe_available": recording.processing_status == ProcessingStatus.READY_TO_TRANSCRIBE,
        "summarize_mode": summarize_mode(recording),
        "retry_available": retry_eligible(recording),
        "show_routing": show_routing,
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


def _section_summary_panel(variant) -> dict:
    """Section-scoped summary status presentation (Step 6.2).

    Derived ONLY from the selected Section variant's state — never from
    the parent Recording summary tuple (section summaries intentionally
    do not mutate that tuple). Pure read-only local presentation over
    the already-resolved :class:`~workflow.services.variant_view.VariantView`:

    - active selected summary, no regeneration failure => ok;
    - active selected summary + ``regeneration_failed`` => warn;
    - no active selected summary + failed variant state => danger;
    - no active selected summary otherwise => warn/neutral.

    Returns a ``{level, label, detail}`` dict driving the single
    Section summary status panel on the section detail page.
    """
    from workflow.models import SummaryVariantState

    if variant.summary is not None:
        regen_failed = (
            variant.variant_state is not None
            and variant.variant_state.regeneration_failed
        )
        if regen_failed:
            return {
                "level": "warn",
                "label": "Section re-summarization failed",
                "detail": (
                    "The current section summary was kept — retry is available "
                    "for this section variant."
                ),
            }
        if variant.resolved:
            return {
                "level": "ok",
                "label": "Section summary current",
                "detail": (
                    f"The {variant.resolved} summary for this section is current — "
                    "no action required."
                ),
            }
        return {
            "level": "ok",
            "label": "Section summary current",
            "detail": "The selected summary for this section is current — no action required.",
        }
    if (
        variant.variant_state is not None
        and variant.variant_state.status == SummaryVariantState.VariantStatus.FAILED
    ):
        return {
            "level": "danger",
            "label": "Section summary failed",
            "detail": (
                "The last generation attempt for this section variant failed — "
                "retry is available."
            ),
        }
    return {
        "level": "warn",
        "label": "Section summary not generated",
        "detail": (
            "No summary for this section/language variant yet — independent of "
            "the parent recording's summary status."
        ),
    }


def _validated_section_return(request, recording, config):
    """Read-only validation of Section-origin return parameters.

    ``return_section`` must be a canonical positive ASCII decimal integer
    (no sign, whitespace, Unicode digits, zero or leading zeros) naming a
    readable canonical TOPIC Section of ``recording`` (active or
    historical; fixed, malformed and cross-parent sections fail closed).
    The value is explicitly length- and value-capped to the signed 64-bit
    Django/SQLite BigAutoField range BEFORE any ORM use, so an oversized
    or overflowing value can never reach a query. ``lib_return`` is
    accepted ONLY when it decodes as a valid library-return token AND the
    Section is valid. Returns ``(section, lib_return_token)`` where
    ``section`` is None (and the token ``""``) for any invalid/missing
    input — callers then retain the plain Recording-origin breadcrumb and
    echo nothing unsafe. SELECTs only; the destination is always
    constructed by the caller from ``reverse('section-detail', ...)`` —
    arbitrary client URLs are never accepted.
    """
    from workflow.services import library_return
    from workflow.services.segmentation import (
        SegmentationError,
        canonical_layout_for_transcript,
    )

    section = None
    raw_section = request.GET.get("return_section")
    if (
        raw_section is not None
        and raw_section.isascii()
        and raw_section.isdecimal()
        and len(raw_section) <= _MAX_BIGAUTOFIELD_PK_DIGITS
    ):
        pk = int(raw_section)
        # Canonical positive decimal only: the exact ``str`` round-trip
        # rejects zero and leading zeros, and the value must fit a signed
        # 64-bit BigAutoField BEFORE it reaches the ORM.
        if 0 < pk <= _MAX_BIGAUTOFIELD_PK and str(pk) == raw_section:
            section = (
                Section.objects.select_related("transcript", "segmented_version")
                .filter(pk=pk, transcript__recording=recording)
                .first()
            )
        if section is not None:
            # Only canonical readable TOPIC sections qualify: the fixed
            # whole-recording section, malformed layouts and cross-parent
            # rows fail closed to the plain Recording-origin breadcrumb.
            if section.segmented_version_id is None:
                section = None
            else:
                try:
                    canonical = canonical_layout_for_transcript(
                        section.segmented_version, section.transcript
                    )
                except SegmentationError:
                    section = None
                else:
                    if not any(
                        sec["ordinal"] == section.ordinal for sec in canonical["sections"]
                    ):
                        section = None

    if section is None:
        # An invalid/oversized/overflow/missing ``return_section`` rejects
        # the whole Section-origin return: ``lib_return`` is never echoed
        # either.
        return None, ""

    lib_return = ""
    raw_lib_return = request.GET.get("lib_return")
    if raw_lib_return and library_return.decode_token(
        raw_lib_return, config.timezone
    ) is not None:
        lib_return = raw_lib_return
    return section, lib_return


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

    # Safe Library-return token (Step 6.2a): an optional server-signed
    # token from an originating normal-Library render is validated and,
    # when valid, restores the originating Library state through the
    # top-left breadcrumb. Absent/invalid/forged/oversized tokens leave
    # the plain Library breadcrumb — never echoed, never trusted, never
    # reconstructed from raw query parameters. GET stays strictly
    # read-only.
    from workflow.services import library_return

    library_return_url = reverse("recordings")
    raw_lib_return = request.GET.get("lib_return")
    if raw_lib_return and library_return.decode_token(
        raw_lib_return, config.timezone
    ) is not None:
        library_return_url = library_return.return_url(raw_lib_return)

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
    from workflow.models import Tag, TagOrigin

    tag_choices = Tag.objects.filter(is_configured=True).order_by("name")
    retired_tag_choices = Tag.objects.filter(is_configured=False).order_by("name")
    # Read-only presentation list for the add-tag editor, derived ONLY
    # from the card's already-prefetched active assignments (no extra
    # queries, no N+1): per option the tag itself, whether it is
    # currently assigned (disables the button), and whether that active
    # assignment is a model suggestion (drives the concise visible
    # "(suggested)" label). A manual/confirmed assignment stays bare.
    active_by_tag = {assignment.tag_id: assignment for assignment in card.active_tags}

    def _tag_options(tags):
        return [
            {
                "tag": tag,
                "assigned": tag.pk in active_by_tag,
                "suggested": (
                    active_by_tag[tag.pk].origin == TagOrigin.SUGGESTED
                    if tag.pk in active_by_tag
                    else False
                ),
            }
            for tag in tags
        ]

    tag_options = _tag_options(tag_choices)
    retired_tag_options = _tag_options(retired_tag_choices)

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
        "tag_options": tag_options,
        "retired_tag_options": retired_tag_options,
        "default_output_language": variant.default_language,
        "selected_language": variant.requested,
        "library_return_url": library_return_url,
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
    # A section summary belongs to a topic Section (Step 6.2); the
    # historical Summary detail route stays usable and identifies the
    # owning Section accurately (with a canonical parent check).
    is_section_summary = summary.section_id is not None and summary.section.segmented_version_id is not None
    section_detail_url = None
    if is_section_summary:
        from workflow.services.segmentation import (
            SegmentationError,
            canonical_layout_for_transcript,
        )

        try:
            canonical_layout_for_transcript(
                summary.section.segmented_version, summary.section.transcript
            )
            section_detail_url = reverse(
                "section-detail", args=[recording.pk, summary.section.pk]
            )
        except SegmentationError:
            # Malformed/cross-parent stored layout: no readable section
            # detail link, but the summary itself stays readable.
            section_detail_url = None
    context = {
        "card": card,
        "recording": recording,
        "summary": summary,
        "is_section_summary": is_section_summary,
        "section_detail_url": section_detail_url,
        # Currency is derived, never inferred from is_active: an
        # old-transcript summary may still be active in its own scope.
        "is_current": (
            summary.is_active
            and summary.transcript.is_active
            and summary.section_id is not None
            and summary.section.ordinal == 0
            and summary.section.segmented_version_id is None
            and summary.transcript.recording_id == recording.pk
        ),
        "actions": _action_availability(config, recording),
    }
    return render(request, "workflow/summary_detail.html", context)


def recording_transcript(request, recording_id):
    config, recording, card = _detail_base(request, recording_id)

    # Section-origin return (Step 6.2 follow-up): a validated
    # ``return_section`` (a readable canonical topic Section of this
    # recording) labels the breadcrumb ``← Section`` pointing back to the
    # exact Section detail; an invalid/missing marker keeps the plain
    # ``← Recording overview``. The separately validated ``lib_return``
    # token rides the back link and pagination verbatim. GET stays
    # strictly read-only.
    section_return, lib_return = _validated_section_return(request, recording, config)
    section_return_url = ""
    if section_return is not None:
        section_return_url = reverse(
            "section-detail", args=[recording.pk, section_return.pk]
        )
        if lib_return:
            section_return_url = f"{section_return_url}?lib_return={lib_return}"

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

    # Read-only layout selector (Step 6.1): any explicit ?layout= must
    # belong to the SELECTED transcript (parent-scoped; mismatch/unknown =>
    # 404) and is ALWAYS read-only, even when it is the currently active
    # layout. The default (no layout) shows the current ACTIVE layout of
    # the selected transcript; only the active transcript + implicit
    # current layout is editable. Selected layouts are read through the
    # shared bounded canonical validator (fail-closed: malformed stored
    # state is a controlled 404 for explicit layouts / an unavailable
    # notice for the implicit active one — never a raw error).
    from workflow.services.segmentation import (
        SegmentationError,
        canonical_layout_for_transcript,
    )

    layout_param = request.GET.get("layout")
    explicit_layout = None
    if layout_param:
        explicit_layout = get_object_or_404(
            SegmentedVersion, pk=layout_param, transcript=transcript
        )
    active_layout = (
        SegmentedVersion.objects.filter(transcript=transcript, is_active=True).first()
    )
    layout = explicit_layout if explicit_layout is not None else active_layout

    layout_error = False
    layout_unavailable_note = ""
    canonical = None
    if layout is not None:
        try:
            canonical = canonical_layout_for_transcript(layout, transcript)
        except SegmentationError:
            if explicit_layout is not None:
                raise Http404("Trim & split revision not available for this transcript") from None
            layout_error = True
            layout = None

    paginator = Paginator(transcript.segments.order_by("ordinal"), config.web.transcript_segments_per_page)
    page = paginator.get_page(request.GET.get("page"))
    model_id = transcript.attempt.model_id if transcript.attempt_id is not None else ""

    editable = transcript.is_active and explicit_layout is None and not layout_error

    # Working range of the selected layout (full transcript when none).
    working_start = 0
    working_end = paginator.count
    if canonical is not None:
        working_start = canonical["start"]
        working_end = canonical["end_exclusive"]
    page_has_working_rows = any(
        working_start <= segment.ordinal < working_end for segment in page.object_list
    )
    has_crop = working_start > 0 or working_end < paginator.count

    # Bounded presentation: autoescaped topic headings at section starts,
    # plus ONE context heading when the section containing the first
    # visible working row started on an earlier page. Zero splits => zero
    # headings. Rendered for BOTH the working view and read-only layout
    # views (historical transcript / explicit layout).
    sections = canonical["sections"] if canonical is not None else ()
    page_ordinals = [segment.ordinal for segment in page.object_list]
    first_visible_index = None
    for index, segment in enumerate(page.object_list):
        if working_start <= segment.ordinal < working_end:
            first_visible_index = index
            break
    context_section = None
    if first_visible_index is not None and sections:
        first_ordinal = page.object_list[first_visible_index].ordinal
        for sec in sections:
            if sec["start"] <= first_ordinal < sec["end"]:
                if sec["start"] not in page_ordinals:
                    context_section = sec
                break
    transcript_items = []
    for index, segment in enumerate(page.object_list):
        if context_section is not None and index == first_visible_index:
            transcript_items.append({"kind": "context_heading", "section": context_section})
        for sec in sections:
            if sec["start"] == segment.ordinal:
                transcript_items.append({"kind": "heading", "section": sec})
                break
        transcript_items.append({"kind": "segment", "segment": segment})
    if layout_error:
        layout_unavailable_note = (
            "The stored trim & split revision is invalid and cannot be shown. "
            "The full transcript is displayed instead."
        )

    # Complete staged layout metadata ONLY (range/splits/titles + the
    # temporary-title flags AND the bounded server-derived temporary
    # titles map, capped at MAX_TOPIC_SECTIONS) is client-side and
    # submitted; transcript text never is.
    editor_state = None
    fingerprint = ""
    if editable:
        from workflow.services.segmentation import (
            MAX_TOPIC_SECTIONS,
            derive_temporary_section_title,
            segmentation_fingerprint,
        )

        fingerprint = segmentation_fingerprint(
            recording.pk, transcript, timezone_name=config.timezone
        )
        if canonical is not None:
            splits = list(canonical["splits"])
            titles = list(canonical["titles"])
            title_is_temporary = [
                sec["title_is_temporary"] for sec in canonical["sections"]
            ]
        else:
            splits = []
            titles = []
            title_is_temporary = []
        # Bounded SERVER-authoritative temporary titles: ``temporary_titles``
        # maps a 0-based index (ordinal - 1) to the exact derived
        # ``Segment N of YYYYMMDDHHMM`` for that canonical ordinal, so the
        # editor can visibly prefill new split-created sections without
        # ever deriving a timestamp from the browser clock.
        temporary_titles = [
            derive_temporary_section_title(recording, ordinal, config.timezone)
            for ordinal in range(1, MAX_TOPIC_SECTIONS + 1)
        ]
        editor_state = {
            "segment_count": paginator.count,
            "start": working_start,
            "end": working_end,
            "splits": splits,
            "titles": titles,
            "title_is_temporary": title_is_temporary,
            "temporary_titles": temporary_titles,
        }

    crop_view_message = ""
    if has_crop:
        hidden_count = working_start + (paginator.count - working_end)
        noun = "line" if hidden_count == 1 else "lines"
        if page_has_working_rows:
            crop_view_message = (
                f"Saved working view — {hidden_count} {noun} hidden. "
                "The full transcript remains available."
            )
        else:
            crop_view_message = (
                f"Saved working view — {hidden_count} {noun} hidden and none on this page. "
                "The full transcript remains available."
            )
            if editable:
                crop_view_message += " Use Edit trim & splits → Clear crop to restore the whole transcript."

    # Pagination stays over the FULL transcript (old anchors/search/Ask
    # links stay stable); historical/layout context is preserved.
    pagination_parts = []
    if not transcript.is_active:
        pagination_parts.append(f"v={transcript.pk}")
    if explicit_layout is not None:
        pagination_parts.append(f"layout={explicit_layout.pk}")
    if section_return is not None:
        # Only the VALIDATED Section-origin parameters ride pagination so
        # the back link keeps working after paging; invalid values are
        # never echoed.
        pagination_parts.append(f"return_section={section_return.pk}")
        if lib_return:
            pagination_parts.append(f"lib_return={lib_return}")
    pagination_base_qs = "&".join(pagination_parts)

    context = {
        "card": card,
        "recording": recording,
        "recording_title": card.title,
        "transcript": transcript,
        "is_active_version": transcript.is_active,
        "section_return": section_return,
        "section_return_url": section_return_url,
        "page_obj": page,
        "segment_count": paginator.count,
        "transcript_model": model_id,
        "transcript_language": transcript.language_observed,
        "transcript_duration": recording.duration_seconds,
        # Export URL fragment preserving the historical version (the
        # active transcript uses the plain export URL). ``&`` is
        # autoescaped in the template, decoded by browsers/JS.
        "version_query": f"&version={transcript.pk}" if not transcript.is_active else "",
        # Step 6.1 editor / working-view context.
        "editable": editable,
        "explicit_layout": explicit_layout,
        "layout": layout,
        "layout_error": layout_error,
        "layout_unavailable_note": layout_unavailable_note,
        "working_start": working_start,
        "working_end": working_end,
        "page_has_working_rows": page_has_working_rows,
        "has_crop": has_crop,
        "crop_view_message": crop_view_message,
        "transcript_items": transcript_items,
        "editor_state": editor_state,
        "fingerprint": fingerprint,
        "pagination_base_qs": pagination_base_qs,
    }
    return render(request, "workflow/recording_transcript.html", context)


def section_detail(request, recording_id, section_id):
    """Step 6.2 topic-section detail (read route).

    Parent-scoped: the Section must belong to ``recording_id``; a
    mismatch/unknown pk is a controlled 404. Only TOPIC Sections are
    readable here (the whole-recording fixed Section belongs to the
    recording detail page); a cross-parent, non-topic, or malformed-
    layout Section is a controlled 404.

    - an ACTIVE topic Section (of the ACTIVE transcript's ACTIVE layout)
      is editable/actionable: section-scoped tags editor, per-variant
      summary action, opaque action fingerprint;
    - a HISTORICAL topic Section (superseded layout, or a layout of a
      historical transcript) is readable ONLY when parent ownership and
      the canonical layout hold — never actionable.

    GET is strictly read-only: SELECTs only, no network, subprocess,
    detection, or writes. The H1 is the topic title; parent Recording
    title/source/date/status is context/provenance; the bounded
    transcript preview comes from ONLY the Section's canonical segment
    range.
    """
    config, recording, card = _detail_base(request, recording_id)
    section = get_object_or_404(
        Section.objects.select_related("transcript", "segmented_version"),
        pk=section_id,
        transcript__recording=recording,
    )
    if section.segmented_version_id is None:
        raise Http404("Section not available")

    # Safe Library-return token (Step 6.2a): the server-signed token from
    # the originating normal-Library render is validated and propagated
    # verbatim — the breadcrumb link, the variant tabs and every action
    # form keep the originating page/state. Invalid/forged tokens simply
    # leave the plain Library breadcrumb (no error, no fallback page).
    from workflow.services import library_return

    library_return_token = ""
    if "lib_return" in request.GET:
        decoded = library_return.decode_token(
            request.GET.get("lib_return"), config.timezone
        )
        if decoded is not None:
            library_return_token = request.GET["lib_return"]
    library_return_url = (
        library_return.return_url(library_return_token)
        if library_return_token
        else reverse("recordings")
    )

    from workflow.services.segmentation import (
        SegmentationError,
        canonical_layout_for_transcript,
        range_label,
    )

    try:
        canonical = canonical_layout_for_transcript(
            section.segmented_version, section.transcript
        )
    except SegmentationError:
        raise Http404("Section not available") from None
    if not any(sec["ordinal"] == section.ordinal for sec in canonical["sections"]):
        raise Http404("Section not available")

    active_transcript = recording.transcripts.filter(is_active=True).first()
    is_active_section = (
        active_transcript is not None
        and section.transcript_id == active_transcript.pk
        and section.segmented_version.is_active
    )

    # Read-only variant view-model for the Section scope: default/
    # original/en/zh-Hant generation selectors plus existing concrete
    # read variants; unknown concrete languages are a friendly 404.
    from workflow.services.variant_view import build_variant_view

    requested_language = request.GET.get("language", "default")
    variant = build_variant_view(recording, requested_language, section=section)
    if variant.error:
        raise Http404(
            f"No summary variant '{requested_language}' exists for this section."
        )

    # Bounded transcript preview from ONLY the Section's canonical range.
    preview_segments = list(
        section.transcript.segments.filter(
            ordinal__gte=section.start_segment_ordinal,
            ordinal__lt=section.end_segment_ordinal_exclusive,
        ).order_by("ordinal")[:DETAIL_PREVIEW_SEGMENTS]
    )
    segment_count = section.end_segment_ordinal_exclusive - section.start_segment_ordinal

    # Stable user-facing title (Step 6.2a): NEVER mutate ``Section.title``
    # during summary generation. The active DEFAULT-language section
    # Summary's title is the page title whenever one exists — it supersedes
    # BOTH a stored temporary title AND a manually entered custom title in
    # presentation (a display override only; the stored custom title stays
    # layout metadata/provenance). Without a default Summary the stored
    # title is used. The title is derived from the DEFAULT variant only —
    # switching tabs never changes the page identity (H1/page title).
    display_title = section.title
    if variant.default_summary is not None:
        display_title = variant.default_summary.title

    # Approximate section duration: (latest usable end_ms - earliest usable
    # start_ms) / 1000 where the two endpoints are selected INDEPENDENTLY
    # over the canonical range — the earliest NON-NULL start_ms and the
    # latest NON-NULL end_ms (a start-only first segment and an end-only
    # last segment still yield a span) — via ONE bounded aggregate. None
    # when unavailable or nonpositive ("unknown").
    section_duration = section_duration_seconds(section)

    # Variant tab query fragment (Step 6.2a): the validated library-return
    # token rides every language tab so the breadcrumb keeps the
    # originating page/state; recording-detail pages leave it empty.
    variant_tab_query = f"lib_return={library_return_token}" if library_return_token else ""

    # Section-origin return marker: every link leaving this Section page
    # (History, Section in transcript, Full transcript) carries the
    # server-owned ``return_section=<pk>`` plus the already validated
    # ``lib_return`` token when present, so the History/transcript
    # breadcrumbs can point back to this exact Section. The value is
    # always the Section's own bounded integer pk — never a URL.
    return_query = f"return_section={section.pk}"
    if library_return_token:
        return_query += f"&lib_return={library_return_token}"
    history_url = f"{reverse('recording-history', args=[recording.pk])}?{return_query}"
    full_transcript_url = (
        f"{reverse('recording-transcript', args=[recording.pk])}?{return_query}"
    )

    # Transcript jump link: the active-transcript page at the page
    # containing the Section's first segment, anchored to that segment.
    # An ACTIVE section keeps the plain current link; a HISTORICAL
    # section (historical transcript and/or superseded layout) preserves
    # its own revision via ?v=...&layout=... so the jump opens EXACTLY
    # the section's transcript + layout, never the current one.
    transcript_jump_page = section.start_segment_ordinal // config.web.transcript_segments_per_page + 1
    jump_parts = [f"page={transcript_jump_page}"]
    if not section.transcript.is_active:
        jump_parts.append(f"v={section.transcript_id}")
    if not section.segmented_version.is_active:
        jump_parts.append(f"layout={section.segmented_version_id}")
    jump_parts.append(f"return_section={section.pk}")
    if library_return_token:
        jump_parts.append(f"lib_return={library_return_token}")
    transcript_jump_url = (
        f"{reverse('recording-transcript', args=[recording.pk])}"
        f"?{'&'.join(jump_parts)}#segment-{section.start_segment_ordinal}"
    )

    from workflow.models import Tag, TagAssignment, TagOrigin

    active_section_tags = list(
        TagAssignment.objects.filter(section=section, is_active=True)
        .select_related("tag")
        .order_by("tag__name")
    )
    # The global configured/retired tag choices only feed the ACTIVE
    # section's tag editor. A HISTORICAL section renders ONLY its assigned
    # tags read-only (the template never opens the editor), so the global
    # Tag queries are skipped entirely for historical pages.
    if is_active_section:
        tag_choices = Tag.objects.filter(is_configured=True).order_by("name")
        retired_tag_choices = Tag.objects.filter(is_configured=False).order_by("name")
        active_by_tag = {assignment.tag_id: assignment for assignment in active_section_tags}

        def _tag_options(tags):
            return [
                {
                    "tag": tag,
                    "assigned": tag.pk in active_by_tag,
                    "suggested": (
                        active_by_tag[tag.pk].origin == TagOrigin.SUGGESTED
                        if tag.pk in active_by_tag
                        else False
                    ),
                }
                for tag in tags
            ]

        tag_options = _tag_options(tag_choices)
        retired_tag_options = _tag_options(retired_tag_choices)
    else:
        tag_options = []
        retired_tag_options = []

    section_actions = {}
    if is_active_section:
        from workflow.services.web_actions import section_state_fingerprint

        section_actions["fingerprint"] = section_state_fingerprint(recording, section)

    # Read-only parent routing decision (context/provenance only).
    routing_decision = recording.routing_decisions.filter(is_active=True).first()

    context = {
        "card": card,
        "recording": recording,
        "section": section,
        "canonical": canonical,
        "is_active_section": is_active_section,
        "range_label": range_label(
            section.start_segment_ordinal, section.end_segment_ordinal_exclusive
        ),
        "display_title": display_title,
        "section_duration": section_duration,
        "transcript_jump_url": transcript_jump_url,
        "history_url": history_url,
        "full_transcript_url": full_transcript_url,
        "preview_segments": preview_segments,
        "segment_count": segment_count,
        "variant": variant,
        "current_summary": variant.summary,
        "default_summary": variant.default_summary,
        "section_actions": section_actions,
        "tag_options": tag_options,
        "retired_tag_options": retired_tag_options,
        "active_section_tags": active_section_tags,
        "routing_decision": routing_decision,
        "default_output_language": variant.default_language,
        "selected_language": variant.requested,
        "section_status": _section_summary_panel(variant),
        "library_return_url": library_return_url,
        "library_return_token": library_return_token,
        "variant_tab_query": variant_tab_query,
    }
    return render(request, "workflow/section_detail.html", context)


def recording_history(request, recording_id):
    config, recording, card = _detail_base(request, recording_id)

    # Section-origin return (Step 6.2 follow-up): a validated
    # ``return_section`` (a readable canonical topic Section of this
    # recording) labels the breadcrumb ``← Section`` pointing back to the
    # exact Section detail; an invalid/missing marker keeps the plain
    # ``← Recording overview``. The separately validated ``lib_return``
    # token rides the back link verbatim. GET stays strictly read-only.
    section_return, lib_return = _validated_section_return(request, recording, config)
    section_return_url = ""
    if section_return is not None:
        section_return_url = reverse(
            "section-detail", args=[recording.pk, section_return.pk]
        )
        if lib_return:
            section_return_url = f"{section_return_url}?lib_return={lib_return}"

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

    # Step 6.1: segmented working-layout revisions, newest first, bounded
    # with the same limit+1 sentinel. Topic counts come from ONE annotation
    # (no N+1); titles are NEVER dumped into History. Rows carry the
    # parent-scoped read-only link (?v= + &layout=).
    segmented = list(
        SegmentedVersion.objects.filter(transcript__recording=recording)
        .select_related("transcript")
        .annotate(topic_count=Count("sections"))
        .order_by("-activated_at", "-created_at", "-revision")[: HISTORY_LIMIT + 1]
    )
    segmented_truncated = len(segmented) > HISTORY_LIMIT
    segmented = segmented[:HISTORY_LIMIT]
    from workflow.services.segmentation import range_label

    segmented_rows = [
        {
            "pk": version.pk,
            "revision": version.revision,
            "activated_at": version.activated_at,
            "is_active": version.is_active,
            "superseded_at": version.superseded_at,
            "range_label": range_label(
                version.start_segment_ordinal, version.end_segment_ordinal_exclusive
            ),
            "topic_count": version.topic_count,
            "transcript_id": version.transcript_id,
            "transcript_active": version.transcript.is_active,
        }
        for version in segmented
    ]

    context = {
        "card": card,
        "recording": recording,
        "section_return": section_return,
        "section_return_url": section_return_url,
        "transcripts": transcripts,
        "transcripts_truncated": transcripts_truncated,
        "summaries": summaries,
        "summaries_truncated": summaries_truncated,
        "attempts": attempts,
        "attempts_truncated": attempts_truncated,
        "routing_rows": routing_rows,
        "routing_truncated": routing_truncated,
        "segmented_rows": segmented_rows,
        "segmented_truncated": segmented_truncated,
        "history_limit": HISTORY_LIMIT,
    }
    return render(request, "workflow/recording_history.html", context)
