"""Views for the Brain workflow app.

Home page requests must be fast and side-effect free: they never launch
MacWhisper, never query the oMLX /v1/models endpoint, and never expose
secrets. Full diagnostics live in ``brain doctor``.

The ``/health/`` endpoint returns stable, sanitized, machine-readable
statuses only. Raw exception text, absolute filesystem paths, secrets,
and SQL/traceback details never appear in responses; they may be
logged locally (never including API keys).
"""

from __future__ import annotations

import logging
import shutil

from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render

from brain import settings as django_settings
from brainlib import APP_NAME
from brainlib.config import AppConfig, ConfigError, load_config
from brainlib.paths import runtime_directories

logger = logging.getLogger(__name__)


def _lightweight_status(config: AppConfig) -> dict:
    """Cheap, bounded status for the home page.

    Only local filesystem existence checks are performed (no directory
    creation, no stat beyond ``is_dir``). Step 4 privacy policy: ABSOLUTE
    FILESYSTEM PATHS ARE NEVER PLACED IN TEMPLATE CONTEXT — storage is
    reported as label + availability only. Detailed local diagnostics
    (including paths) remain the domain of ``brain doctor`` in a
    terminal. External connectivity is intentionally NOT checked here.
    """
    storage = config.storage
    storage_status = [
        {"label": "Inbox", "status": "available" if storage.inbox.is_dir() else "missing"},
        {"label": "Database", "status": "available" if storage.database.parent.is_dir() else "missing"},
        {"label": "Transcripts", "status": "available" if storage.transcripts.is_dir() else "missing"},
        {"label": "Exports", "status": "available" if storage.exports.is_dir() else "missing"},
        {"label": "Logs", "status": "available" if storage.logs.is_dir() else "missing"},
        {"label": "Temporary storage", "status": "available" if storage.temp.is_dir() else "missing"},
    ]
    return {
        "app_name": APP_NAME,
        "version": django_settings.BRAIN_VERSION,
        "storage_status": storage_status,
        "macwhisper": {
            # Presence only — the configured command path is never rendered.
            "found": bool(shutil.which(config.macwhisper.command)),
        },
        "omlx": {
            "base_url": config.llm.base_url,
            "summary_model": config.llm.model or "(not configured)",
            "embedding_model": config.embedding.model or "(not configured)",
            "checked_on_page": False,
        },
        "initial_tags": [tag.name for tag in config.initial_tags],
        "configured_tags": [tag.name for tag in config.tags.allowed],
    }


def _pipeline_counts() -> dict[str, int]:
    """Lightweight DB aggregate for the status page (read-only, fast)."""
    from django.db.models import Count

    from workflow.models import ProcessingStatus, Recording, SummaryState

    counts = dict(Recording.objects.values_list("processing_status").annotate(total=Count("pk")))
    transcribed = Recording.objects.filter(processing_status=ProcessingStatus.TRANSCRIBED)
    return {
        "discovered": counts.get(ProcessingStatus.DISCOVERED, 0)
        + counts.get(ProcessingStatus.HASHING, 0)
        + counts.get(ProcessingStatus.ROUTING, 0),
        "needs_review": counts.get(ProcessingStatus.NEEDS_REVIEW, 0),
        "transcribed": counts.get(ProcessingStatus.TRANSCRIBED, 0),
        "failed": counts.get(ProcessingStatus.FAILED, 0),
        # Summarization counts (DB-only; never triggers oMLX work).
        "awaiting_summary": transcribed.filter(summary_status=SummaryState.MISSING).count(),
        "summary_failed": Recording.objects.filter(summary_status=SummaryState.FAILED).count(),
        "summarized": transcribed.filter(
            summary_status=SummaryState.CURRENT, resummarization_failed=False
        ).count(),
        "failed_resummarization": Recording.objects.filter(resummarization_failed=True).count(),
    }


def home(request):
    try:
        config = load_config()
    except ConfigError:
        config = django_settings.BRAIN_CONFIG_OBJ
    context = _lightweight_status(config)
    context["pipeline_counts"] = _pipeline_counts()
    return render(request, "workflow/home.html", context)


def _runtime_dirs_status(config: AppConfig) -> str:
    return "ok" if all(d.is_dir() for d in runtime_directories(config)) else "error"


def _database_status() -> str:
    """Probe the database. Returns a stable status; details go to logs only."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return "ok"
    except Exception:
        logger.exception("Health check: database query failed")
        return "error"


def health(request):
    """Structured health JSON with sanitized, stable statuses.

    - 200 ``ok``/``degraded``: application healthy; ``degraded`` means
      an optional external dependency (currently MacWhisper) is absent.
      The oMLX endpoint is never probed here, so its status is always
      ``not_checked``.
    - 503 ``unhealthy``: application-level failure (invalid config,
      missing runtime directories, database failure).

    Responses contain no raw exception text, filesystem paths, or
    secrets.
    """
    try:
        config: AppConfig = load_config()
    except ConfigError:
        logger.warning("Health check: configuration could not be loaded")
        return JsonResponse(
            {
                "status": "unhealthy",
                "application": {
                    "config": "error",
                    "runtime_directories": "unknown",
                    "database": "unknown",
                },
                "dependencies": {},
            },
            status=503,
        )

    dirs_status = _runtime_dirs_status(config)
    db_status = _database_status()
    payload = {
        "status": None,  # set below
        "version": django_settings.BRAIN_VERSION,
        "application": {
            "config": "ok",
            "runtime_directories": dirs_status,
            "database": db_status,
        },
        "dependencies": {
            "macwhisper": {
                "status": "available" if shutil.which(config.macwhisper.command) else "missing",
                # presence check only; no subprocess is launched here
            },
            "omlx": {
                "status": "not_checked",
                "base_url": config.llm.base_url,
            },
        },
    }

    if dirs_status != "ok" or db_status != "ok":
        payload["status"] = "unhealthy"
        return JsonResponse(payload, status=503)

    degraded = payload["dependencies"]["macwhisper"]["status"] != "available"
    payload["status"] = "degraded" if degraded else "ok"
    return JsonResponse(payload, status=200)


def error_404(request, exception=None):
    return render(request, "404.html", status=404)


def error_500(request):
    return render(request, "500.html", status=500)
