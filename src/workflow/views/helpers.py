"""Shared helpers for the Step 4 web views."""

from __future__ import annotations

from django.http import HttpResponse
from django.shortcuts import render

from brainlib.config import AppConfig, ConfigError, load_config


def get_config() -> AppConfig:
    """Load the app configuration (falling back to the boot-time object)."""
    from brain import settings as django_settings

    try:
        return load_config()
    except ConfigError:
        return django_settings.BRAIN_CONFIG_OBJ


def rejection_response(request, message: str, code: str, status: int = 400) -> HttpResponse:
    """A friendly rejection page with a stable code — never a traceback."""
    return render(
        request,
        "400.html",
        {"message": message, "code": code},
        status=status,
    )


def conflict_response(request, holder_pid: str = "") -> HttpResponse:
    detail = f" (holder pid {holder_pid})" if holder_pid else ""
    return render(
        request,
        "409.html",
        {"detail": detail},
        status=409,
    )
