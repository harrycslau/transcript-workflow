# Brain — transcript workflow

A local-first workflow for transcribing recordings (via MacWhisper Pro's
`mw` CLI), summarizing and tagging transcripts (via a configurable oMLX
OpenAI-compatible endpoint), and storing/searching the results locally.

## Step 1 feature boundary

Step 1 is the project foundation only:

- Django project skeleton with a minimal home/status page and a JSON
  health endpoint
- `brain doctor` system diagnostics CLI
- YAML configuration with `.env` secret support
- Runtime directory scaffolding (`data/`), ignored by Git
- Automated test suite

**Not yet implemented:** transcription, summarization/tagging, database
models (including tags), search (keyword or semantic), the local web UI
beyond the status page, and automatic audio retention deletion
(`retention` is configuration-only and inactive).

## Prerequisites

- macOS with Python **3.12+** (managed automatically by `uv`)
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- MacWhisper Pro's `mw` CLI (optional; its absence is reported as a
  warning by `brain doctor`)
- An oMLX (or any OpenAI-compatible) server (optional; unavailability
  is reported as a warning)

## Setup

```sh
# 1. Install dependencies (creates .venv, fetches Python 3.12 if needed)
uv sync

# 2. Create your local configuration
cp config/config.example.yaml config/config.yaml

# 3. Optionally create .env for secrets
cp .env.example .env
```

### Configuration

- The app reads `config/config.yaml` by default; the path can be
  overridden with `BRAIN_CONFIG=/path/to/config.yaml`.
- `config/config.yaml` and `.env` are **not** committed to Git; edit
  `config/config.example.yaml` only for shared defaults.
- Relative storage paths resolve against the **project root** (the
  directory containing `pyproject.toml`).
- Secrets (e.g. `BRAIN_LLM_API_KEY`) come from the environment or `.env`,
  named by each section's `api_key_env`. API keys are never logged or
  displayed.
- A missing or malformed configuration produces a concise error for
  both `brain doctor` and `brain serve`.
- Code defaults supplement omitted optional settings, but the YAML file
  itself is required.

### Runtime layout

```
data/
  inbox/        # WAV recordings to process (scanned in a later step)
  database/     # SQLite database (brain.sqlite3)
  transcripts/  # transcript output
  exports/      # exported notes
  logs/         # application logs
  temp/         # scratch space
```

All directories are created safely on demand. The entire `data/` tree is
ignored by Git, as are `.env`, `config/config.yaml`, SQLite sidecar
files, audio files, transcripts, logs, Python caches, and virtualenvs.

## Usage

### Diagnostics

```sh
uv run brain doctor
```

Checks and their outcomes:

| Check                                          | Result            | Exit code |
| ---------------------------------------------- | ----------------- | --------- |
| Config missing/malformed/invalid               | FAIL              | 1         |
| Database parent not writable                   | FAIL              | 1         |
| SQLite connection failure                      | FAIL              | 1         |
| SQLite FTS5 missing                            | WARN              | 0         |
| MacWhisper missing / `mw version` fails        | WARN              | 0         |
| oMLX endpoint unreachable                      | WARN              | 0         |
| oMLX invalid/invalid-shape /v1/models response | WARN              | 0         |
| Blank summary/embedding model configuration    | WARN              | 0         |
| Configured model absent from reachable /models | WARN              | 0         |
| Model not verifiable (no valid /models data)   | WARN              | 0         |

Model verification states: a configured model is PASS only when a valid,
non-empty `/v1/models` list was retrieved and contains it; it is a WARN
when absent from the list, when the endpoint reports an explicit empty
list, or when the list could not be retrieved/validated. Malformed oMLX
responses (invalid JSON, non-object payloads, missing/non-list `data`)
are always warnings, never crashes; invalid entries inside `data` are
ignored. `brain doctor` never prints API keys and never fails on missing
optional external tools; a non-zero exit means a genuine required
failure (config, storage, or database).

### Web server

```sh
uv run brain serve                     # http://127.0.0.1:8787
uv run brain serve --host 127.0.0.1 --port 9000
```

- Binds to localhost only by default; no browser is opened.
- Missing configuration or runtime-directory setup failures print a
  concise error and exit with code 1 (no traceback, no Django startup).
- `GET /` — minimal status page (app version, storage paths,
  MacWhisper/oMLX configuration, selected models). Page loads run only
  lightweight local checks; they never launch MacWhisper or query the
  oMLX endpoint — use `brain doctor` for full diagnostics.
- `GET /health/` — structured JSON with stable, sanitized statuses.
  Returns **200** (`ok`, or `degraded` when an optional dependency such
  as MacWhisper is absent) and **503** (`unhealthy`) for
  application-level failures (config invalid, runtime directories
  missing, database failure). Responses never contain raw exception
  text, filesystem paths, SQL errors, tracebacks, or secrets (internal
  details are logged locally instead). The endpoint never launches
  MacWhisper, queries `/models`, or exposes secrets.

### Tests

```sh
uv run pytest
```

Tests are fully self-contained: they use a temporary configuration,
mocked subprocess/HTTP calls, and never require MacWhisper, oMLX,
network access, or real audio.

## Current limitations (Step 1)

- No transcription, summarization, tagging, or search yet.
- `initial_tags` is validated configuration only; no tag models exist.
- Retention deletion is **not active** — audio files are never touched.
- The home page is a status page only; HTMX-driven UI comes later.
- Database migrations exist but there are no models yet.

## Project layout

```
config/config.example.yaml   # committed example configuration
data/                        # runtime data (gitignored, created on demand)
src/brainlib/                # core library: config, paths, diagnostics, CLI
src/brain/                   # Django project (settings, urls, wsgi/asgi)
src/workflow/                # Django app (views, templates, migrations)
src/manage.py                # conventional Django entry point
tests/                       # pytest suite
```
