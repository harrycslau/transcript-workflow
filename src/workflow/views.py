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

    Only local filesystem checks (path existence) are performed.
    External connectivity is intentionally NOT checked here.
    """
    storage = config.storage
    paths = {
        "inbox": storage.inbox,
        "database": storage.database,
        "transcripts": storage.transcripts,
        "exports": storage.exports,
        "logs": storage.logs,
        "temp": storage.temp,
    }
    return {
        "app_name": APP_NAME,
        "version": django_settings.BRAIN_VERSION,
        "paths": paths,
        "macwhisper": {
            "command": config.macwhisper.command,
            "found": bool(shutil.which(config.macwhisper.command)),
        },
        "omlx": {
            "base_url": config.llm.base_url,
            "summary_model": config.llm.model or "(not configured)",
            "embedding_model": config.embedding.model or "(not configured)",
            "checked_on_page": False,
        },
        "initial_tags": [tag.name for tag in config.initial_tags],
    }


def home(request):
    try:
        config = load_config()
    except ConfigError:
        config = django_settings.BRAIN_CONFIG_OBJ
    context = _lightweight_status(config)
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
