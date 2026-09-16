"""Global Library actions: Run now and Open inbox.

Both controls render directly in the standard base header
(``base.html``) on every normal page, as the leftmost controls in the
top-right group (``Run now``, ``Open inbox``, then ``Ask``, ``Review``,
``Status``). Each is a plain POST form, CSRF-protected, executes on the
FIRST POST (no confirmation interstitial) and redirects back to the
plain Library (PRG). Neither renders a live region: the only pending
feedback is the submit-button label changing while the native POST is
in flight (``app.js`` disables and relabels the submit control). A
successful Open inbox dispatch flashes nothing; a failure keeps one
fixed sanitized error message.

- ``run_now`` is the exact web equivalent of ``brain run --now``. A
  read-only migration-readiness inspection runs BEFORE any lock, then
  the exclusive pipeline lock is held while
  ``run_pipeline(config, respect_stability_window=False)`` runs one full
  ingest -> route -> transcribe -> summarize pass. ``run_pipeline``
  performs interruption recovery itself, so recovery is never
  duplicated here. Busy lock renders the existing friendly 409.
- ``open_inbox`` launches Finder on the server-loaded configured inbox
  only. It takes no pipeline lock, runs no recovery, touches no ORM and
  runs no migration preflight; it accepts no client path/destination.

Nothing is leaked: raw exception text, paths, audio/transcript content,
SQL, model output, secrets and the raw pipeline report (whose ingest
section contains paths) are never surfaced, flashed or logged.
"""

from __future__ import annotations

from django.contrib import messages as dj_messages
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from brainlib.migrations import RECOVERY_COMMAND
from workflow.services.open_inbox import OpenInboxError, open_inbox
from workflow.services.pipeline import run_pipeline
from workflow.services.pipeline_lock import PipelineBusy, pipeline_lock
from workflow.views.helpers import conflict_response, get_config, rejection_response

RUN_NOW_SUCCESS_MESSAGE = "Run now finished. The Library has been refreshed."

# One fixed sanitized message: the exception and the raw pipeline report
# are never included.
RUN_NOW_FAILURE_MESSAGE = (
    "Run now could not be completed. Open the status page (or run "
    "`brain doctor`) and try again."
)

# One fixed sanitized message for every Open inbox failure.
OPEN_INBOX_FAILURE_MESSAGE = (
    "The inbox folder could not be opened. Check that it exists and that "
    "Finder is available, then try again."
)

# One fixed actionable message naming the exact recovery command; never
# the underlying exception or migration details beyond the command.
MIGRATIONS_PENDING_MESSAGE = (
    "The database schema is out of date, so nothing was run. Apply pending "
    f"migrations first, then retry: {RECOVERY_COMMAND}"
)
MIGRATIONS_UNVERIFIABLE_MESSAGE = (
    "The database schema could not be verified, so nothing was run. Apply "
    f"pending migrations first, then retry: {RECOVERY_COMMAND}"
)


def _migration_preflight_error() -> str | None:
    """Read-only migration-readiness inspection.

    Returns a fixed actionable message when migrations are pending or the
    migration state cannot be inspected, else ``None``. Strictly
    read-only (SELECT/PRAGMA only) and must run BEFORE any lock,
    recovery, ORM pipeline work, file access or network call.
    """
    from brainlib.migrations import MigrationInspectionError, unapplied_migrations

    try:
        pending = unapplied_migrations()
    except MigrationInspectionError:
        return MIGRATIONS_UNVERIFIABLE_MESSAGE
    if pending:
        return MIGRATIONS_PENDING_MESSAGE
    return None


@require_POST
def run_now(request):
    """``brain run --now`` as a direct Library action (POST only).

    GET is a 405 with zero work (``require_POST``). Pending/uninspectable
    migrations are a fixed actionable 400 BEFORE even reloading config
    from disk and before any lock or pipeline call.
    """
    migration_error = _migration_preflight_error()
    if migration_error is not None:
        return rejection_response(request, migration_error, "migrations_pending")
    config = get_config()
    try:
        with pipeline_lock(config):
            # ``run_pipeline`` performs recover_interruptions internally;
            # never call recovery again here. The raw report is discarded
            # (it contains ingest paths and must not be surfaced).
            run_pipeline(config, respect_stability_window=False)
    except PipelineBusy as exc:
        return conflict_response(request, exc.holder_pid)
    except Exception:
        # Sanitized: never surface or log the exception or pipeline report.
        dj_messages.error(request, RUN_NOW_FAILURE_MESSAGE)
        return redirect("recordings")
    dj_messages.success(request, RUN_NOW_SUCCESS_MESSAGE)
    return redirect("recordings")


@require_POST
def open_inbox_view(request):
    """Open the configured inbox in Finder (POST only, no lock).

    A successful dispatch has NO flash message: the POST simply redirects
    back to the Library. A failure keeps the one fixed sanitized error.
    """
    config = get_config()
    try:
        open_inbox(config.storage.inbox)
    except OpenInboxError:
        dj_messages.error(request, OPEN_INBOX_FAILURE_MESSAGE)
        return redirect("recordings")
    return redirect("recordings")
