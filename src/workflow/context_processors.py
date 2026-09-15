"""Template context helpers for the global navigation.

``nav_context`` supplies the Review badge count to the base template on
every rendered page. It is deliberately bounded: at most one COUNT query,
no network, subprocess or filesystem work, and a safe fallback to 0 when
the database is unavailable (error pages extend ``base.html`` too).
"""

from __future__ import annotations

import logging

from django.db.models import Q

from workflow.models import AudioStatus, ProcessingStatus, Recording, SummaryState

logger = logging.getLogger(__name__)


def review_badge_count() -> int:
    """Distinct Recordings appearing in ANY Review category.

    Matches the Review page's categories (incl. awaiting summary, failed
    summary, failed re-summarization and missing audio). An active
    UNVERIFIED automatic routing decision on a transcribed Recording is
    audit-only (see AGENTS.md) and is deliberately NOT a Review category.
    Overlapping categories are counted once via ``distinct()``; the whole
    union is one bounded query.
    """
    return (
        Recording.objects.filter(
            Q(processing_status=ProcessingStatus.NEEDS_REVIEW)
            | Q(processing_status=ProcessingStatus.FAILED)
            | Q(retranscription_failed=True)
            | Q(resummarization_failed=True)
            | Q(summary_status=SummaryState.FAILED)
            | Q(
                processing_status=ProcessingStatus.TRANSCRIBED,
                summary_status=SummaryState.MISSING,
            )
            | Q(audio_status=AudioStatus.MISSING)
        )
        .distinct()
        .count()
    )


def nav_context(request):
    try:
        count = review_badge_count()
    except Exception:
        # Stable category only — never exception text, SQL, paths or
        # database content in logs or rendered output.
        logger.warning("nav_context: review badge count unavailable")
        count = 0
    return {"review_count": count}