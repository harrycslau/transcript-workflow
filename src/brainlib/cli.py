"""``brain`` console entry point.

Commands:
  brain doctor               Run system diagnostics (exit 1 on FAIL results).
  brain serve [--host H] [--port P]
                             Start the local Django development server
                             (default http://127.0.0.1:8787, no browser).

Django is initialized through Python APIs (``django.setup()`` +
``call_command``); ``manage.py`` remains only as the conventional
development entry point.
"""

from __future__ import annotations

import argparse
import os
import sys

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


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
    # Validate configuration and set up runtime directories before booting
    # Django so failures produce concise user-facing errors, not tracebacks.
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

    args = parser.parse_args(argv)

    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "serve":
        return cmd_serve(args.host, args.port)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
