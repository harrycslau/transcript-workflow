"""Ask-with-citations web view (Step 5D).

GET renders the form and does ZERO health/embedding/chat work and no
writes. POST on the same endpoint executes the read-only Ask:
invalid input is rejected before any health/network work, operational
failures render one sanitized ``unavailable`` state with the submitted
question cleared, and insufficiency is a successful explicit state.
The question is never placed in a URL, log or error, and every rendered
value is autoescaped plain data — the view builds no HTML.
"""

from __future__ import annotations

from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from brainlib.config import ConfigError
from workflow.services import ask as ask_service
from workflow.views.helpers import get_config

STATE_FORM = "form"
STATE_INVALID = "invalid"
STATE_UNAVAILABLE = "unavailable"


def _render(request, *, state, question="", result=None, message=None):
    return render(
        request,
        "workflow/ask.html",
        {
            "state": state,
            "question": question,
            "result": result,
            "message": message,
            # Application-owned truncation note (content-free); empty unless
            # any supplied evidence was excerpted or budget-dropped.
            "evidence_note": (
                ask_service.EVIDENCE_TRUNCATED_NOTE
                if result is not None and result.evidence_truncated
                else ""
            ),
        },
    )


@require_http_methods(["GET", "POST"])
def ask_view(request):
    if request.method != "POST":
        return _render(request, state=STATE_FORM)
    return _handle_post(request)


def _handle_post(request):
    raw = request.POST.get("question")
    # Cheap validation BEFORE config/health/network work. The rejected
    # text is never echoed.
    try:
        question = ask_service.validate_question(raw)
    except ask_service.AskInputError as exc:
        return _render(request, state=STATE_INVALID, message=str(exc))

    try:
        config = get_config()
        # Resolve the production seams at call time so tests can patch
        # them cleanly.
        from workflow.services import embedding_client, llm

        result = ask_service.ask_question(
            question,
            config=config,
            embedder=embedding_client.embed_texts,
            chat=llm.chat_completion,
        )
    except ConfigError as exc:
        # Every failure is already a fixed sanitized message (AskError or
        # the semantic retrieval's stable errors); the question is
        # cleared so it appears nowhere in the response.
        return _render(request, state=STATE_UNAVAILABLE, message=str(exc))

    return _render(request, state=result.state, question=result.question, result=result)
