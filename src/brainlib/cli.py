"""``brain`` console entry point.

Commands:
  brain doctor               Run system diagnostics (exit 1 on FAIL results).
  brain serve [--host H] [--port P]
                             Start the local Django development server
                             (default http://127.0.0.1:8787, no browser).
  brain ingest|route|transcribe|run|retry|status|review|transcripts
                             Pipeline commands (Step 2), each with --json.
  brain summarize [ID] [--regenerate]
  brain summaries ID | brain summary ID [--format markdown|text|json]
  brain tags [--sync]        Summarization, rendering, and tag commands (Step 3).
  brain search-index status  Read-only search index health (no lock; exit 1
                             when the index is not built, stale, inconsistent
                             or the FTS table is missing/broken).
  brain search-index rebuild Atomically rebuild registry + FTS (mutating;
                             takes the pipeline lock).

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
    # Schema preflight before starting the server: never bind and serve
    # pages that would crash against a database behind the code's
    # migrations. Read-only check; never applies migrations.
    try:
        _require_applied_migrations()
    except Exception as exc:
        # _require_applied_migrations raises ConfigError carrying a
        # concise, sanitized, actionable message (with the recovery
        # command); anything else must not leak raw details either.
        from brainlib.config import ConfigError

        if isinstance(exc, ConfigError):
            print(f"error: {exc}", file=sys.stderr)
            return 1
        raise
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


def _require_applied_migrations() -> None:
    """Fail cleanly (ConfigError -> exit 1, no traceback) when the
    configured database is missing required migrations.

    Runs BEFORE any lock acquisition, recovery, ORM work, file access,
    or external contact. Strictly read-only (Django migration APIs);
    never applies migrations. Raises ConfigError so the existing
    command handlers print one concise actionable error including the
    exact recovery command.
    """
    from brainlib.config import ConfigError
    from brainlib.migrations import (
        MigrationInspectionError,
        RECOVERY_COMMAND,
        summarize_pending,
        unapplied_migrations,
    )

    try:
        pending = unapplied_migrations()
    except MigrationInspectionError as exc:
        raise ConfigError(
            f"database migration state could not be verified ({exc.category}). "
            f"Apply pending migrations first, then retry:\n  {RECOVERY_COMMAND}"
        ) from None
    if pending:
        raise ConfigError(
            f"database schema is out of date ({summarize_pending(pending)}). "
            f"Apply pending migrations first, then retry:\n  {RECOVERY_COMMAND}"
        )


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
        # Schema preflight BEFORE the lock, recovery, or any ORM/file/
        # external work: a database behind the code's migrations must
        # fail cleanly instead of crashing mid-pipeline.
        _require_applied_migrations()
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
        # Schema preflight: read-only commands also touch ORM models,
        # which crash on a database behind the code's migrations.
        _require_applied_migrations()
        payload = work(config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ImproperlyConfigured as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _emit(payload, getattr(args, "json", False))
    return 0


def cmd_transcript_language(args) -> int:
    """View or set the detected source language of a transcript."""
    from brainlib.config import ConfigError, load_config

    def _read_work(config):
        from workflow.models import Recording

        recording = Recording.objects.filter(pk=args.recording_id).first()
        if recording is None:
            raise ConfigError(f"recording not found: {args.recording_id}")
        transcript = recording.transcripts.filter(is_active=True).first()
        if transcript is None:
            raise ConfigError(f"no active transcript for recording {args.recording_id}")
        return {
            "recording_id": recording.pk,
            "transcript_id": transcript.pk,
            "language_observed": transcript.language_observed or "(not detected)",
            "verified_by": transcript.language_observed_verified_by or "(unknown)",
            "verified_at": (
                transcript.language_observed_verified_at.isoformat()
                if transcript.language_observed_verified_at
                else "(never)"
            ),
        }

    def _set_work(config):
        from workflow.models import Recording
        from workflow.services.summarize import set_transcript_language

        recording = Recording.objects.filter(pk=args.recording_id).first()
        if recording is None:
            raise ConfigError(f"recording not found: {args.recording_id}")
        return set_transcript_language(recording, args.set_language.strip())

    if args.set_language:
        return _pipeline_command(args, _set_work)
    return _read_only_command(args, _read_work)


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

        from workflow.models import ProcessingAttempt, ProcessingStatus, Recording, SummaryState

        counts = dict(
            Recording.objects.values_list("processing_status").annotate(total=Count("pk"))
        )
        failed_retranscriptions = Recording.objects.filter(
            processing_status=ProcessingStatus.TRANSCRIBED, retranscription_failed=True
        ).count()
        transcribed = Recording.objects.filter(processing_status=ProcessingStatus.TRANSCRIBED)
        summary = {
            "awaiting_summary": transcribed.filter(summary_status=SummaryState.MISSING).count(),
            "summary_failed": Recording.objects.filter(summary_status=SummaryState.FAILED).count(),
            "summarized": transcribed.filter(
                summary_status=SummaryState.CURRENT, resummarization_failed=False
            ).count(),
            "failed_resummarization": Recording.objects.filter(resummarization_failed=True).count(),
        }
        failures = list(
            Recording.objects.filter(processing_status=ProcessingStatus.FAILED).values_list("pk", "failure_stage")
        )
        recent_errors = list(
            ProcessingAttempt.objects.exclude(error_code="").order_by("-started_at")
            .values_list("recording_id", "stage", "error_code")[:20]
        )
        return {
            "counts": {status: counts.get(status, 0) for status, _ in ProcessingStatus.choices},
            "summary": summary,
            "failed_retranscriptions": failed_retranscriptions,
            "failures": [{"recording_id": pk, "stage": stage} for pk, stage in failures],
            "recent_errors": [
                {"recording_id": pk, "stage": stage, "error_code": code} for pk, stage, code in recent_errors
            ],
        }

    return _read_only_command(args, work)


def cmd_review(args) -> int:
    def work(config):
        from workflow.services.review import build_review_report

        return build_review_report()

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


def cmd_summarize(args) -> int:
    def work(config):
        from brainlib.config import ConfigError
        from workflow.models import Recording
        from workflow.services.summarize import summarize_one, summarize_pending

        if args.recording_id:
            recording = Recording.objects.filter(pk=args.recording_id).first()
            if recording is None:
                raise ConfigError(f"recording not found: {args.recording_id}")
            return summarize_one(
                config, recording,
                target_language=getattr(args, "language", "default") or "default",
                regenerate=args.regenerate,
            )
        return summarize_pending(config)

    return _pipeline_command(args, work)


def cmd_summaries(args) -> int:
    def work(config):
        from brainlib.config import ConfigError
        from workflow.models import Recording

        recording = Recording.objects.filter(pk=args.recording_id).first()
        if recording is None:
            raise ConfigError(f"recording not found: {args.recording_id}")
        current = recording.current_summary()
        versions = [
            {
                "summary_id": s.pk,
                "ordinal": s.ordinal,
                "is_active": s.is_active,
                "is_current": current is not None and s.pk == current.pk,
                "output_language": s.output_language,
                "language": s.language,
                "title": s.title,
                "model_id": s.model_id,
                "prompt_version": s.prompt_version,
                "chunk_count": s.chunk_count,
                "input_characters": s.input_characters,
                "input_truncated": s.input_truncated,
                "generation_mode": s.generation_mode,
                "attempt_id": s.attempt_id,
                "created_at": s.created_at.isoformat(),
            }
            for s in recording.summaries.all()
        ]
        return {"recording_id": recording.pk, "summaries": versions}

    return _read_only_command(args, work)


def cmd_summary(args) -> int:
    from brainlib.config import ConfigError, load_config

    fmt = "json" if getattr(args, "json", False) else args.format

    def work(config):
        from brainlib.config import ConfigError
        from workflow.models import Recording
        from workflow.services.rendering import render_markdown, render_text, summary_to_dict

        recording = Recording.objects.filter(pk=args.recording_id).first()
        if recording is None:
            raise ConfigError(f"recording not found: {args.recording_id}")
        language = getattr(args, "language", None)
        if language:
            from workflow.services.langresolve import resolve_output_language
            from workflow.services.variant_view import existing_variant_languages
            from brainlib.config import ConfigError as _ConfigError

            transcript = recording.transcripts.filter(is_active=True).first()
            if transcript is None:
                raise ConfigError(f"no active transcript for recording {args.recording_id}")
            if language in ("default", "original", "en", "zh-Hant"):
                output_language = resolve_output_language(transcript, language)
            elif language in existing_variant_languages(recording, transcript):
                # Read-only access to an existing concrete variant.
                output_language = language
            else:
                raise _ConfigError(
                    f"cannot resolve language '{language}' for this recording"
                )
            if not output_language:
                raise ConfigError(f"cannot resolve language '{language}' for this recording")
            summary = recording.current_summary(output_language=output_language)
        else:
            summary = recording.current_summary()
        if summary is None:
            lang_msg = f" in language '{language}'" if language else ""
            raise ConfigError(f"no current summary{lang_msg} for recording {args.recording_id}")
        if fmt == "markdown":
            return render_markdown(summary)
        if fmt == "text":
            return render_text(summary)
        return summary_to_dict(summary)

    try:
        config = load_config()
        _setup_django()
        # Schema preflight (cmd_summary predates the shared command
        # runners): no ORM work on a database behind the code's
        # migrations.
        _require_applied_migrations()
        payload = work(config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ImproperlyConfigured as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _emit(payload, isinstance(payload, dict))
    return 0


def _tags_payload() -> dict:
    from workflow.models import Tag

    tags = [
        {
            "name": tag.name,
            "name_key": tag.name_key,
            "description": tag.description,
            "is_configured": tag.is_configured,
            "active_assignments": tag.assignments.filter(is_active=True).count(),
        }
        for tag in Tag.objects.order_by("name")
    ]
    return {
        "tags": tags,
        "configured": sum(1 for t in tags if t["is_configured"]),
        "retired": sum(1 for t in tags if not t["is_configured"]),
    }


def cmd_tags(args) -> int:
    if getattr(args, "sync", False):
        def work(config):
            from workflow.services.tags import sync_tags

            return {"sync": sync_tags(config), **_tags_payload()}

        # --sync mutates the database and therefore takes the pipeline lock.
        return _pipeline_command(args, work)

    def work(config):
        # Genuinely read-only: no synchronization, no writes.
        return _tags_payload()

    return _read_only_command(args, work)


def cmd_search_index(args) -> int:
    """``brain search-index status|rebuild`` (Step 5A.2).

    status: strictly read-only, never takes the pipeline lock; exit 0
    only when the index is fully healthy, 1 for not-built/stale/
    inconsistent/missing-or-broken FTS. rebuild: mutating — takes the
    pipeline lock via the shared runner (contention exits 3).
    """
    if args.action == "rebuild":
        def work(config):
            from workflow.services.search_index import rebuild_index

            return rebuild_index()

        return _pipeline_command(args, work)

    from brainlib.config import ConfigError, load_config

    try:
        config = load_config()
        _setup_django()
        _require_applied_migrations()
        from workflow.services.search_index import build_status_report

        payload = build_status_report()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ImproperlyConfigured as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _emit(payload, getattr(args, "json", False))
    return 0 if payload.get("healthy") else 1


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

    add_pipeline_command("ingest", "Discover and register stable WAV, MP3, and M4A files")

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

    summarize = add_pipeline_command(
        "summarize", "Summarize eligible recordings, or (re)summarize one recording"
    )
    summarize.add_argument("recording_id", nargs="?", help="Summarize a single recording explicitly")
    summarize.add_argument(
        "--regenerate",
        action="store_true",
        help="Create a new summary version even when a current summary exists",
    )
    summarize.add_argument(
        "--language",
        choices=["default", "original", "en", "zh-Hant"],
        default="default",
        help="Target output language (default: auto-detect from source)",
    )

    summaries = add_pipeline_command("summaries", "List summary versions of a recording")
    summaries.add_argument("recording_id", help="Recording to inspect")

    summary = add_pipeline_command("summary", "Print the current summary of a recording")
    summary.add_argument("recording_id", help="Recording to print")
    summary.add_argument(
        "--format",
        choices=["markdown", "text", "json"],
        default="markdown",
        help="Output format (default markdown; copy-friendly for other LLMs)",
    )
    summary.add_argument(
        "--language",
        choices=["default", "original", "en", "zh-Hant"],
        default=None,
        help="Show summary in a specific language variant",
    )

    transcript_lang_cmd = add_pipeline_command(
        "transcript-language", "View or set the detected source language of a transcript"
    )
    transcript_lang_cmd.add_argument("recording_id", help="Recording to inspect or correct")
    transcript_lang_cmd.add_argument(
        "--set",
        dest="set_language",
        metavar="LANGUAGE",
        help="Set the source language explicitly (e.g. en, fi, zh-HK)",
    )

    tags_cmd = add_pipeline_command("tags", "List configured and retired tags (read-only)")
    tags_cmd.add_argument(
        "--sync",
        action="store_true",
        help="Synchronize tags with the YAML configuration (mutating; takes the pipeline lock)",
    )

    search_index_cmd = subparsers.add_parser(
        "search-index", help="Inspect or rebuild the keyword-search index (Step 5A.2)"
    )
    search_index_sub = search_index_cmd.add_subparsers(dest="action", required=True)
    status_cmd = search_index_sub.add_parser(
        "status", help="Read-only index health (no lock; exit 1 unless fully healthy)"
    )
    status_cmd.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    rebuild_cmd = search_index_sub.add_parser(
        "rebuild", help="Atomically rebuild the index (mutating; takes the pipeline lock)"
    )
    rebuild_cmd.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    args = parser.parse_args(argv)

    if args.command == "summarize" and args.regenerate and not args.recording_id:
        parser.error("--regenerate requires a RECORDING_ID")
    if args.command == "summarize" and args.language != "default" and not args.recording_id:
        parser.error("--language requires a RECORDING_ID")

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
    if args.command == "summarize":
        return cmd_summarize(args)
    if args.command == "summaries":
        return cmd_summaries(args)
    if args.command == "summary":
        return cmd_summary(args)
    if args.command == "tags":
        return cmd_tags(args)
    if args.command == "transcript-language":
        return cmd_transcript_language(args)
    if args.command == "search-index":
        return cmd_search_index(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
