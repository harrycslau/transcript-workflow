"""Read-only copy/export endpoints (Step 4).

UTF-8 responses with correct Content-Type; Content-Disposition filenames
are derived from the recording's SHA-256 prefix and the format only —
never from titles or source filenames — so header injection is not
possible. Exports never mutate state and never touch the filesystem,
MacWhisper, or the network. Historical versions are identified via the
``version`` query parameter and always resolved through the parent
Recording (cross-recording access is a 404).
"""

from __future__ import annotations

import json

from django.http import HttpResponse, HttpResponseBadRequest
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET

from workflow.models import Recording, Summary, Transcript
from workflow.services.rendering import render_markdown, render_text, summary_to_dict


def _summary_for(request, recording: Recording) -> tuple[Summary, bool]:
    version = request.GET.get("version")
    language = (request.GET.get("language") or "").strip()
    if version:
        summary = get_object_or_404(
            Summary.objects.select_related("transcript", "section"),
            pk=version,
            recording_id=recording.pk,
        )
        return summary, True
    if language:
        # Same selector rules as the read pages: the four standard
        # selectors plus concrete languages that already exist. Unknown
        # selectors and unresolvable targets are friendly 404s — never
        # a silent fallback to the default-language summary.
        from workflow.services.variant_view import build_variant_view

        variant = build_variant_view(recording, language)
        if variant.error:
            raise Http404(
                f"No summary variant '{language}' exists for this recording."
            )
        if not variant.resolved:
            raise Http404(
                "The summary in the original language is not available yet: "
                "the source language has not been determined. Generate it "
                "from the recording page."
            )
        summary = variant.summary
        if summary is None:
            raise Http404(
                f"No summary in '{variant.resolved}' exists for this recording yet."
            )
        return summary, False
    summary = recording.current_summary()
    if summary is None:
        raise Http404("No current summary for this recording")
    return summary, False


def is_summary_current(summary: Summary) -> bool:
    """Currency is DERIVED (active summary of the active transcript's
    whole-recording section) — never inferred from ``is_active`` alone,
    because old-transcript summaries stay active in their own scope."""
    return bool(
        summary.is_active
        and summary.transcript.is_active
        and summary.section.ordinal == 0
    )


def _response(content: str, content_type: str, filename: str) -> HttpResponse:
    response = HttpResponse(content, content_type=f"{content_type}; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@require_GET
def summary_export(request, recording_id):
    recording = get_object_or_404(Recording, pk=recording_id)
    fmt = (request.GET.get("format") or "markdown").strip().lower()
    if fmt not in ("markdown", "text", "json"):
        return HttpResponseBadRequest("format must be markdown, text or json")
    summary, historical = _summary_for(request, recording)
    current = is_summary_current(summary)
    sha = recording.sha256[:12]

    if fmt == "json":
        payload = summary_to_dict(summary)
        payload["is_active_in_scope"] = summary.is_active
        payload["is_current_for_recording"] = current
        payload["historical"] = not current
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        return _response(content, "application/json", f"brain-summary-{sha}.json")

    content = render_markdown(summary) if fmt == "markdown" else render_text(summary)
    if not current:
        header = (
            f"Historical summary (version {summary.ordinal}) — not the current summary of this "
            f"recording.\nTranscript version: {summary.transcript_id}\n\n"
        )
        content = header + content
    ext = "md" if fmt == "markdown" else "txt"
    ctype = "text/markdown" if fmt == "markdown" else "text/plain"
    return _response(content, ctype, f"brain-summary-{sha}.{ext}")


@require_GET
def transcript_export(request, recording_id):
    recording = get_object_or_404(Recording, pk=recording_id)
    fmt = (request.GET.get("format") or "text").strip().lower()
    if fmt not in ("text", "timestamped"):
        return HttpResponseBadRequest("format must be text or timestamped")
    version = request.GET.get("version")
    if version:
        transcript = get_object_or_404(
            Transcript.objects.prefetch_related("segments"),
            pk=version,
            recording_id=recording.pk,
        )
        historical = True
    else:
        transcript = recording.transcripts.filter(is_active=True).first()
        if transcript is None:
            return HttpResponseBadRequest("No active transcript for this recording")
        historical = False
    sha = recording.sha256[:12]
    suffix = f"-v{transcript.pk}" if historical else ""

    if fmt == "timestamped":
        lines = []
        for segment in transcript.segments.all().order_by("ordinal"):
            start_ms = segment.start_ms or 0
            minutes, seconds = divmod(start_ms // 1000, 60)
            prefix = f"[{minutes:02d}:{seconds:02d}] "
            speaker = f"{segment.speaker}: " if segment.speaker else ""
            lines.append(f"{prefix}{speaker}{segment.text}")
        content = "\n".join(lines) + ("\n" if lines else "")
    else:
        content = transcript.text_normalized or ""
        if not content:
            texts = [s.text for s in transcript.segments.all().order_by("ordinal")]
            content = "\n".join(texts)
            if content:
                content += "\n"
    return _response(content, "text/plain", f"brain-transcript-{sha}{suffix}.txt")
