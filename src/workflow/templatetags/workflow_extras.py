"""Template filters for local-time display and small formatting helpers."""

from __future__ import annotations

from datetime import datetime

from django import template
from django.utils import timezone as dj_timezone

register = template.Library()


def _app_timezone_name() -> str:
    from brain import settings as django_settings

    return getattr(django_settings.BRAIN_CONFIG_OBJ, "timezone", "UTC")


@register.filter
def local_time(value, fmt: str = "Y-m-d H:i") -> str:
    """Render an aware datetime in the configured application timezone."""
    if not isinstance(value, datetime):
        return ""
    if dj_timezone.is_aware(value):
        from zoneinfo import ZoneInfo

        value = dj_timezone.localtime(value, ZoneInfo(_app_timezone_name()))
    mapping = {
        "Y": "%Y",
        "m": "%m",
        "d": "%d",
        "H": "%H",
        "i": "%M",
        "s": "%S",
    }
    out = ""
    for char in fmt:
        out += mapping.get(char, char)
    return value.strftime(out)


@register.filter
def duration(seconds) -> str:
    """Human-readable duration from seconds (None-safe)."""
    if seconds is None:
        return "unknown"
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "unknown"
    if total < 0:
        return "unknown"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


@register.filter
def sha_short(value) -> str:
    if not value:
        return ""
    return f"{value[:12]}…"


@register.filter
def mmss(ms) -> str:
    """Milliseconds as mm:ss (or h:mm:ss)."""
    if ms is None:
        return ""
    try:
        total = int(ms) // 1000
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"
