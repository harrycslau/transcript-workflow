"""System diagnostics for Brain (used by ``brain doctor`` and the health endpoint).

Exit-code semantics:

- FAIL: malformed/missing config, unwritable database location,
  SQLite connection failure -> ``brain doctor`` exits 1.
- WARN (exit stays 0): missing/unavailable MacWhisper, unreachable
  oMLX server, invalid oMLX /v1/models responses (bad JSON, unexpected
  payload shape), blank model configuration, configured model absent
  from a reachable /v1/models response, configured model that could
  not be verified, missing SQLite FTS5.

Secrets are never included in results: only the *name* of the env var
holding a key, and whether it is set. oMLX response bodies,
authorization headers, and API keys never appear in diagnostics.

Model verification states (see :func:`check_models`):
- blank model                      -> WARN "not configured"
- list retrieved, model present    -> PASS
- list retrieved, model absent     -> WARN "not in /v1/models"
- list could not be retrieved      -> WARN "could not be verified"
- reachable endpoint, empty list   -> WARN (never PASS)
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from typing import Callable

import httpx

from brainlib.config import AppConfig, ConfigError

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

MW_TIMEOUT_SECONDS = 10
OMLX_TIMEOUT_SECONDS = 5

# Verification states for configured model names.
MODELS_VERIFIED = "verified"  # /v1/models retrieved successfully and non-empty
MODELS_EMPTY = "empty"  # reachable endpoint returned a valid, empty model list
MODELS_UNVERIFIED = "unverified"  # list could not be retrieved or parsed


class OmlxPayloadError(Exception):
    """The oMLX /v1/models response could not be parsed into model IDs.

    Never carries response bodies or secrets; messages are static
    schema descriptions safe for diagnostics output.
    """


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str

    @property
    def is_fatal(self) -> bool:
        return self.status == FAIL


def check_python_version() -> CheckResult:
    import sys

    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 12):
        return CheckResult("Python version", PASS, f"Python {version}")
    return CheckResult("Python version", FAIL, f"Python 3.12+ required, found {version}")


def check_runtime_dirs(config: AppConfig) -> CheckResult:
    """Validate every configured runtime directory (not only new ones).

    Collects all failures before reporting; does not stop at the first
    invalid path.
    """
    from brainlib.paths import ensure_runtime_dirs, is_writable_dir, runtime_directories

    try:
        ensure_runtime_dirs(config)
    except OSError as exc:
        return CheckResult("Runtime directories", FAIL, f"cannot create: {exc}")

    problems: list[str] = []
    for directory in runtime_directories(config):
        if not directory.exists():
            problems.append(f"missing: {directory}")
        elif not directory.is_dir():
            problems.append(f"not a directory: {directory}")
        elif not is_writable_dir(directory):
            problems.append(f"not writable: {directory}")
    if problems:
        return CheckResult("Runtime directories", FAIL, "; ".join(problems))
    return CheckResult("Runtime directories", PASS, "all configured directories present and writable")


def check_database_location(config: AppConfig) -> CheckResult:
    from brainlib.paths import is_writable_dir

    parent = config.storage.database.parent
    if not is_writable_dir(parent):
        return CheckResult("Database location", FAIL, f"parent directory not writable: {parent}")
    return CheckResult("Database location", PASS, str(parent))


def check_sqlite_connection(config: AppConfig) -> CheckResult:
    try:
        conn = sqlite3.connect(config.storage.database)
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return CheckResult("SQLite connection", FAIL, str(exc))
    return CheckResult("SQLite connection", PASS, str(config.storage.database))


def check_fts5() -> CheckResult:
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE fts5_probe USING fts5(text)")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return CheckResult("SQLite FTS5", WARN, f"unavailable: {exc}")
    return CheckResult("SQLite FTS5", PASS, "available")


def check_macwhisper(
    config: AppConfig,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> CheckResult:
    """Check MacWhisper presence and ``mw version``. Never fatal in Step 1."""
    runner = runner or subprocess.run
    command = config.macwhisper.command
    resolved = shutil.which(command) or (command if command.startswith("/") else shutil.which(command))
    if not resolved:
        return CheckResult("MacWhisper", WARN, f"executable not found: {command}")

    try:
        result = runner(
            [resolved, "version"],
            capture_output=True,
            text=True,
            timeout=MW_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return CheckResult("MacWhisper", WARN, f"executable not found: {command}")
    except subprocess.TimeoutExpired:
        return CheckResult("MacWhisper", WARN, f"'{command} version' timed out after {MW_TIMEOUT_SECONDS}s")
    except OSError as exc:
        return CheckResult("MacWhisper", WARN, f"failed to run '{command} version': {exc}")

    if result.returncode != 0:
        stderr = (result.stderr or "").strip().splitlines()
        detail = stderr[0] if stderr else f"exit code {result.returncode}"
        return CheckResult("MacWhisper", WARN, f"'{command} version' failed: {detail}")
    version_line = (result.stdout or "").strip().splitlines()
    detail = version_line[0] if version_line else "no version output"
    return CheckResult("MacWhisper", PASS, detail)


def fetch_omlx_models(
    base_url: str,
    api_key_env: str,
    timeout: float = OMLX_TIMEOUT_SECONDS,
) -> list[str]:
    """Fetch model IDs from an OpenAI-compatible /v1/models endpoint.

    Raises :class:`httpx.HTTPError`-family exceptions on connectivity
    problems and :class:`OmlxPayloadError` for invalid JSON or an
    unexpected payload shape. Exception messages are static schema
    descriptions; response bodies, headers, and secrets are never
    included. Invalid entries inside ``data`` are ignored safely.

    Works with base URLs that include or omit a trailing slash.
    """
    import os

    # Secrets are read from the environment by name and only used for the
    # request header; they are never logged or returned.
    headers: dict[str, str] = {}
    secret = os.environ.get(api_key_env, "").strip()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    response = httpx.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=timeout)
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError:
        raise OmlxPayloadError("response is not valid JSON") from None
    if not isinstance(payload, dict):
        raise OmlxPayloadError("response is not a JSON object")
    if "data" not in payload:
        raise OmlxPayloadError("response is missing 'data'")
    data = payload["data"]
    if not isinstance(data, list):
        raise OmlxPayloadError("'data' is not a list")

    # Invalid entries (non-objects, missing/null IDs) are ignored safely.
    return sorted(
        str(item.get("id"))
        for item in data
        if isinstance(item, dict) and item.get("id") is not None
    )


def check_omlx(
    base_url: str,
    api_key_env: str,
    fetcher: Callable[..., list[str]] | None = None,
) -> tuple[CheckResult, list[str], str]:
    """Check oMLX reachability. Returns (result, model_ids, verification_state).

    Unreachable servers and invalid responses yield WARN with an empty
    model list and :data:`MODELS_UNVERIFIED`; a reachable endpoint with
    a valid but empty model list yields WARN and :data:`MODELS_EMPTY`.
    Never raises and never includes secrets or response bodies in the
    detail string.
    """
    fetcher = fetcher or fetch_omlx_models
    try:
        models = fetcher(base_url, api_key_env)
    except httpx.HTTPError as exc:
        reason = type(exc).__name__
        return CheckResult("oMLX endpoint", WARN, f"unreachable at {base_url} ({reason})"), [], MODELS_UNVERIFIED
    except OmlxPayloadError as exc:
        return CheckResult("oMLX endpoint", WARN, f"reachable at {base_url}, but /models returned an invalid payload ({exc})"), [], MODELS_UNVERIFIED
    except OSError as exc:
        return CheckResult("oMLX endpoint", WARN, f"unreachable at {base_url} ({exc})"), [], MODELS_UNVERIFIED

    if not models:
        return CheckResult("oMLX endpoint", WARN, f"reachable at {base_url}, but /models returned no model IDs"), [], MODELS_EMPTY
    return (
        CheckResult("oMLX endpoint", PASS, f"reachable at {base_url}, {len(models)} model(s) available"),
        models,
        MODELS_VERIFIED,
    )


def check_models(
    summary_model: str,
    embedding_model: str,
    state: str,
    available_models: list[str],
) -> list[CheckResult]:
    """Validate configured model names against an explicit verification state.

    States:
    - ``MODELS_VERIFIED``: /v1/models retrieved; configured names are
      checked for membership.
    - ``MODELS_EMPTY``: endpoint reachable but reported no models;
      configured names must not be reported as PASS.
    - ``MODELS_UNVERIFIED``: the list could not be retrieved or parsed;
      configured names are reported as unverifiable warnings.
    """
    results: list[CheckResult] = []

    def check_one(label: str, model: str) -> CheckResult:
        if not model.strip():
            return CheckResult(label, WARN, "no model configured (blank)")
        if state == MODELS_VERIFIED:
            if model in available_models:
                return CheckResult(label, PASS, model)
            return CheckResult(label, WARN, f"'{model}' not in /v1/models")
        if state == MODELS_EMPTY:
            return CheckResult(label, WARN, f"'{model}' configured, but the endpoint reports no models")
        return CheckResult(label, WARN, f"'{model}' configured, but could not be verified (no valid /v1/models response)")

    results.append(check_one("Summary model", summary_model))
    results.append(check_one("Embedding model", embedding_model))
    return results


def redact_secret_check(config: AppConfig) -> CheckResult:
    """Report whether the configured LLM API key env var is set - name only."""
    env_name = config.llm.api_key_env
    import os

    if os.environ.get(env_name, "").strip():
        return CheckResult("LLM API key", PASS, f"set via ${env_name}")
    return CheckResult("LLM API key", WARN, f"not set (${env_name} is empty)")


def run_doctor() -> tuple[list[CheckResult], int]:
    """Run all diagnostics. Returns (results, exit_code).

    Exit code is 1 only for genuine required failures (FAIL results).
    """
    results: list[CheckResult] = [check_python_version()]

    try:
        config: AppConfig | None = None
        from brainlib.config import load_config

        config = load_config()
    except ConfigError as exc:
        results.append(CheckResult("Configuration", FAIL, str(exc)))
        results.append(CheckResult("Everything else", FAIL, "skipped: configuration invalid"))
        return results, 1

    from brainlib import __version__

    results.append(CheckResult("Configuration", PASS, f"loaded from {config.config_path}"))
    results.append(CheckResult("Application version", PASS, f"Brain {__version__}"))
    results.append(check_runtime_dirs(config))
    results.append(check_database_location(config))
    results.append(check_sqlite_connection(config))
    results.append(check_fts5())
    results.append(check_macwhisper(config))
    results.append(redact_secret_check(config))

    omlx_result, models, state = check_omlx(config.llm.base_url, config.llm.api_key_env)
    results.append(omlx_result)
    results.extend(check_models(config.llm.model, config.embedding.model, state, models))

    exit_code = 1 if any(r.is_fatal for r in results) else 0
    return results, exit_code
