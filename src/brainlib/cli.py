"""``brain`` console entry point.

Commands:
  brain doctor               Run system diagnostics (exit 1 on FAIL results).
  brain serve [--host H] [--port P]
                             Start the local Django development server
                             (default http://127.0.0.1:8787, no browser).
  brain ingest|route|transcribe|run|retry|status|review|transcripts
                             Pipeline commands (Step 2), each with --json.

Exit codes: 0 success/warnings; 1 config or setup error; 2 usage error;
3 another pipeline process holds the lock. Django is initialized through
Python APIs; ``manage.py`` remains only as the conventional entry point.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from django.core.exceptions import ImproperlyConfigured  # safe without configured settings

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

# Exit code used when another pipeline process holds the pipeline lock.
EXIT_BUSY = 3


def _print_doctor_report(results) -> int:
    from brainlib.diagnostics import FAIL

    width = max(len(r.name) for r in results) + 2
    print("brain doctor")
    print("-" * (width + 40))
    for result in results:
        print(f"{result.name:<{width}} {result.status:<5} {result.detail}")
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    summary = ", ".join(f"{counts.get(s, 0)} {s.lower()}" for s in ("PASS", "WARN", "FAIL") if counts.get(s))
    print("-" * (width + 40))
    print(summary)
    return 1 if any(r.status == FAIL for r in results) else 0


def cmd_doctor() -> int:
    from brainlib import diagnostics

    results, _exit_code = diagnostics.run_doctor()
    return _print_doctor_report(results)


def cmd_serve(host: str, port: int) -> int:
    # Validate configuration before booting Django so a missing or broken
    # config produces a concise user-facing error rather than a traceback.
    from brainlib.config import ConfigError, load_config
    from brainlib.paths import ensure_runtime_dirs

    try:
        config = load_config()
        ensure_runtime_dirs(config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: cannot set up runtime directories: {exc}", file=sys.stderr)
        return 1

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "brain.settings")

    import django
    from django.core.management import call_command
    from django.core.management.base import CommandError

    django.setup()
    print(f"Starting Brain at http://{host}:{port}/ (Ctrl+C to stop)")
    try:
        call_command("runserver", f"{host}:{port}", use_reloader=True)
    except CommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


# ---------------------------------------------------------------------------
# Pipeline command helpers
# ---------------------------------------------------------------------------


def _setup_django() -> None:
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "brain.settings")
    django.setup()


def _emit(payload, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        _emit_human(payload)


def _emit_human(payload) -> None:
    if isinstance(payload, dict) and "error" in payload:
        print(f"error: {payload['error']}", file=sys.stderr)
        return
    if not isinstance(payload, dict):
        print(payload)
        return
    for key, value in payload.items():
        if isinstance(value, list):
            print(f"{key}:")
            for item in value:
                print(f"  - {item}")
        elif isinstance(value, dict):
            print(f"{key}:")
            for sub_key, sub_value in value.items():
                print(f"  {sub_key}: {sub_value}")
        else:
            print(f"{key}: {value}")


def _pipeline_command(args, work):
    """Shared runner for mutating pipeline commands.

    ``work(config)`` runs while the pipeline lock is held. Handles
    Django setup, config loading, lock contention, and error mapping:
    ConfigError -> exit 1, PipelineBusy -> exit 3.
    """
    from brainlib.config import ConfigError, load_config

    try:
        # Validate/load config BEFORE Django setup: in a fresh process,
        # brain.settings re-raises config problems as ImproperlyConfigured
        # during import, which must not escape as a traceback.
        config = load_config()
        _setup_django()
        from workflow.services.pipeline import PipelineBusy, pipeline_lock
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ImproperlyConfigured as exc:
        # Only settings-import failures land here; the message is the
        # sanitized config error already used by CLI output.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        with pipeline_lock(config):
            recovery = _recover(config)
            payload = work(config)
            if isinstance(payload, dict) and recovery.get("recovered_attempts"):
                payload = {"recovery": recovery, **payload}
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except PipelineBusy as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BUSY
    _emit(payload, getattr(args, "json", False))
    return 0


def _recover(config) -> dict:
    """Run the interruption-recovery pass (lock is held by the caller)."""
    from workflow.services.pipeline import recover_interruptions

    return recover_interruptions(config)


def _read_only_command(args, work) -> int:
    from brainlib.config import ConfigError, load_config

    try:
        config = load_config()
        _setup_django()
        payload = work(config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ImproperlyConfigured as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _emit(payload, getattr(args, "json", False))
    return 0


def cmd_ingest(args) -> int:
    def work(config):
        from workflow.services.pipeline import run_ingest

        return run_ingest(config)

    return _pipeline_command(args, work)


def cmd_route(args) -> int:
    if args.recording_id and (args.profile or args.confirm):
        def work(config):
            from brainlib.config import ConfigError
            from workflow.models import Recording
            from workflow.services.pipeline import (
                confirm_routing,
                manual_route,
                transcribe_ready,
            )

            recording = Recording.objects.filter(pk=args.recording_id).first()
            if recording is None:
                raise ConfigError(f"recording not found: {args.recording_id}")
            if args.profile:
                payload = manual_route(recording, args.profile)
                if args.transcribe_now and payload.get("status") == "ready_to_transcribe":
                    # Same lock scope: transcribe immediately after routing.
                    payload["transcription"] = transcribe_ready(config, [args.recording_id])
                return payload
            return confirm_routing(recording)

        return _pipeline_command(args, work)

    def work(config):
        from workflow.services.pipeline import route_pending

        return {"routed": route_pending(config)}

    return _pipeline_command(args, work)


def cmd_transcribe(args) -> int:
    ids = [args.recording_id] if args.recording_id else None

    def work(config):
        from workflow.services.pipeline import transcribe_ready

        return {"transcribed": transcribe_ready(config, ids)}

    return _pipeline_command(args, work)


def cmd_run(args) -> int:
    def work(config):
        from workflow.services.pipeline import run_pipeline

        return run_pipeline(config)

    return _pipeline_command(args, work)


def cmd_retry(args) -> int:
    def work(config):
        from brainlib.config import ConfigError
        from workflow.models import Recording
        from workflow.services.pipeline import retry as retry_recording

        recording = Recording.objects.filter(pk=args.recording_id).first()
        if recording is None:
            raise ConfigError(f"recording not found: {args.recording_id}")
        return retry_recording(config, recording)

    return _pipeline_command(args, work)


def cmd_status(args) -> int:
    def work(config):
        from django.db.models import Count

        from workflow.models import ProcessingAttempt, ProcessingStatus, Recording

        counts = dict(
            Recording.objects.values_list("processing_status").annotate(total=Count("pk"))
        )
        failed_retranscriptions = Recording.objects.filter(
            processing_status=ProcessingStatus.TRANSCRIBED, retranscription_failed=True
        ).count()
        failures = list(
            Recording.objects.filter(processing_status=ProcessingStatus.FAILED).values_list("pk", "failure_stage")
        )
        recent_errors = list(
            ProcessingAttempt.objects.exclude(error_code="").order_by("-started_at")
            .values_list("recording_id", "stage", "error_code")[:20]
        )
        return {
            "counts": {status: counts.get(status, 0) for status, _ in ProcessingStatus.choices},
            "failed_retranscriptions": failed_retranscriptions,
            "failures": [{"recording_id": pk, "stage": stage} for pk, stage in failures],
            "recent_errors": [
                {"recording_id": pk, "stage": stage, "error_code": code} for pk, stage, code in recent_errors
            ],
        }

    return _read_only_command(args, work)


def cmd_review(args) -> int:
    def work(config):
        from workflow.models import ProcessingStatus, Recording

        needs_review = []
        for recording in Recording.objects.filter(processing_status=ProcessingStatus.NEEDS_REVIEW):
            decision = recording.routing_decisions.filter(is_active=True).first()
            needs_review.append(
                {
                    "recording_id": recording.pk,
                    "kind": "needs_review_before_transcription",
                    "suggested_route": decision.route_suggestion if decision else None,
                    "confidence": decision.confidence if decision else None,
                    "reason_code": decision.reason_code if decision else None,
                }
            )
        unverified = []
        retranscription_failed = []
        for recording in Recording.objects.filter(processing_status=ProcessingStatus.TRANSCRIBED):
            decision = recording.routing_decisions.filter(is_active=True).first()
            if recording.retranscription_failed:
                retranscription_failed.append(
                    {
                        "recording_id": recording.pk,
                        "kind": "failed_retranscription",
                        "attempt_id": recording.last_failed_attempt_id,
                        "route": decision.route_suggestion if decision else None,
                    }
                )
            if decision is not None and not decision.routing_verified:
                unverified.append(
                    {
                        "recording_id": recording.pk,
                        "kind": "transcribed_routing_unverified",
                        "route": decision.route_suggestion,
                        "confidence": decision.confidence,
                        "profile": decision.profile_name,
                    }
                )
        failed = [
            {"recording_id": pk, "kind": f"failed_{stage}"}
            for pk, stage in Recording.objects.filter(processing_status=ProcessingStatus.FAILED).values_list(
                "pk", "failure_stage"
            )
        ]
        return {
            "needs_review": needs_review,
            "unverified": unverified,
            "failed_retranscription": retranscription_failed,
            "failed": failed,
        }

    return _read_only_command(args, work)


def cmd_transcripts(args) -> int:
    def work(config):
        from brainlib.config import ConfigError
        from workflow.models import Recording

        recording = Recording.objects.filter(pk=args.recording_id).first()
        if recording is None:
            raise ConfigError(f"recording not found: {args.recording_id}")
        versions = [
            {
                "transcript_id": t.pk,
                "is_active": t.is_active,
                "created_at": t.created_at.isoformat(),
                "attempt_id": t.attempt_id,
                "parser_version": t.parser_version,
                "segment_count": t.segments.count(),
            }
            for t in recording.transcripts.all()
        ]
        return {"recording_id": recording.pk, "transcripts": versions}

    return _read_only_command(args, work)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="brain",
        description="Local-first transcript workflow",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Run system diagnostics")

    serve = subparsers.add_parser("serve", help="Start the local web server")
    serve.add_argument("--host", default=DEFAULT_HOST, help=f"Bind address (default {DEFAULT_HOST})")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (default {DEFAULT_PORT})")

    def add_pipeline_command(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--json", action="store_true", help="Machine-readable JSON output")
        return sub

    add_pipeline_command("ingest", "Discover and register stable new WAV files")

    route = add_pipeline_command("route", "Route recordings automatically, or manually route one")
    route.add_argument("recording_id", nargs="?", help="Recording to route manually")
    route.add_argument("--profile", help="Routing profile for manual routing")
    route.add_argument(
        "--confirm", action="store_true", help="Verify the active automatic decision (no retranscription)"
    )
    route.add_argument(
        "--transcribe-now",
        action="store_true",
        help="After manual routing, immediately run transcription for this recording (same lock)",
    )

    transcribe = add_pipeline_command("transcribe", "Transcribe recordings with an approved routing profile")
    transcribe.add_argument("recording_id", nargs="?", help="Transcribe a single recording")

    add_pipeline_command("run", "Compose ingest -> route -> transcribe")
    add_pipeline_command("status", "Summarize counts and failures")
    add_pipeline_command("review", "List recordings needing human attention")

    retry_cmd = add_pipeline_command("retry", "Retry a failed recording or failed retranscription")
    retry_cmd.add_argument("recording_id", help="Recording to retry")

    transcripts = add_pipeline_command("transcripts", "List transcript versions of a recording")
    transcripts.add_argument("recording_id", help="Recording to inspect")

    args = parser.parse_args(argv)

    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "serve":
        return cmd_serve(args.host, args.port)
    if args.command == "ingest":
        return cmd_ingest(args)
    if args.command == "route":
        return cmd_route(args)
    if args.command == "transcribe":
        return cmd_transcribe(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "review":
        return cmd_review(args)
    if args.command == "retry":
        return cmd_retry(args)
    if args.command == "transcripts":
        return cmd_transcripts(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
