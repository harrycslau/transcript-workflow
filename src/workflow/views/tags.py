"""Tag overview and tag-editing views (Step 4).

All mutations are POST + CSRF and go through
:mod:`workflow.services.tags` (the same ownership/locking semantics as
the rest of the pipeline). Every response is a POST→redirect→GET with a
flash message; HTMX-free by design so plain non-JS submissions behave
identically.
"""

from __future__ import annotations

from django.contrib import messages as dj_messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from workflow.models import Recording, Tag, TagAssignment
from workflow.query import tag_overview
from workflow.services.tags import TagOperationError, add_manual_tag, confirm_suggestion, remove_tag
from workflow.views.helpers import get_config
from workflow.forms import TagAddForm


def tag_list(request):
    context = {"tag_rows": tag_overview()}
    return render(request, "workflow/tag_list.html", context)


def _recording_or_404(recording_id: str) -> Recording:
    return get_object_or_404(Recording, pk=recording_id)


def _tag_or_404(tag_id: int) -> Tag:
    try:
        return Tag.objects.get(pk=tag_id)
    except (Tag.DoesNotExist, ValueError):
        raise Http404("Tag not found") from None


@require_POST
def tag_add(request, recording_id):
    recording = _recording_or_404(recording_id)
    config = get_config()
    tags = list(Tag.objects.order_by("name"))
    form = TagAddForm(
        data=request.POST,
        configured=[tag for tag in tags if tag.is_configured],
        retired=[tag for tag in tags if not tag.is_configured],
    )
    if not form.is_valid():
        raw_pk = (request.POST.get("tag") or "").strip()
        raw_tag = None
        if raw_pk.isdigit():
            raw_tag = Tag.objects.filter(pk=int(raw_pk)).first()
        if raw_tag is not None and not raw_tag.is_configured:
            dj_messages.error(
                request,
                f"Tag '{raw_tag.name}' is retired and no longer configured. Tick 'include retired "
                "tags' if you deliberately want to restore it.",
            )
        else:
            dj_messages.error(request, "Choose a valid tag to add.")
        return redirect("recording-detail", recording_id)
    try:
        result = add_manual_tag(
            recording,
            form.cleaned_data["tag_obj"],
            include_retired=form.cleaned_data.get("include_retired", False),
        )
    except TagOperationError as exc:
        dj_messages.error(request, exc.message)
        return redirect("recording-detail", recording_id)
    tag_name = form.cleaned_data["tag_obj"].name
    if result["created"]:
        dj_messages.success(request, f"Tag '{tag_name}' added.")
    elif result["promoted"]:
        dj_messages.success(
            request,
            f"Tag '{tag_name}' is now manual and will survive future re-summarization.",
        )
    elif result["reactivated"]:
        dj_messages.success(request, f"Tag '{tag_name}' restored as a manual tag.")
    else:
        dj_messages.info(
            request,
            f"Tag '{tag_name}' is already a user-owned tag — nothing changed.",
        )
    return redirect("recording-detail", recording_id)


def _assignment_or_404(recording: Recording, tag_id: int) -> tuple[TagAssignment, Tag]:
    try:
        tag = Tag.objects.get(pk=tag_id)
    except (Tag.DoesNotExist, ValueError):
        raise Http404("Tag not found") from None
    assignment = TagAssignment.objects.filter(recording=recording, tag=tag).first()
    if assignment is None:
        raise Http404("No tag assignment for this recording")
    return assignment, tag


@require_POST
def tag_confirm(request, recording_id, tag_id):
    recording = _recording_or_404(recording_id)
    _assignment, tag = _assignment_or_404(recording, tag_id)
    try:
        result = confirm_suggestion(recording, tag)
    except TagOperationError as exc:
        dj_messages.error(request, exc.message)
        return redirect("recording-detail", recording_id)
    if result["already_confirmed"]:
        dj_messages.info(request, f"Tag '{tag.name}' was already confirmed — nothing changed.")
    else:
        dj_messages.success(request, f"Tag '{tag.name}' confirmed. It is now user-owned and survives re-summarization.")
    return redirect("recording-detail", recording_id)


@require_POST
def tag_remove(request, recording_id, tag_id):
    recording = _recording_or_404(recording_id)
    _assignment, tag = _assignment_or_404(recording, tag_id)
    result = remove_tag(recording, tag)
    if result["removed"]:
        dj_messages.success(
            request,
            f"Tag '{tag.name}' removed. Future model suggestions for it stay visible but will "
            "not restore it automatically.",
        )
    else:
        dj_messages.info(request, f"Tag '{tag.name}' was not active — nothing changed.")
    return redirect("recording-detail", recording_id)
