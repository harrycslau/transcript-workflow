# Brain — transcript workflow

A local-first workflow for transcribing recordings (via MacWhisper Pro's
`mw` CLI), summarizing and tagging transcripts (via a configurable oMLX
OpenAI-compatible endpoint), and storing/searching the results locally.

## What is available now

Steps 1–4 are implemented. The app can:

- `data/inbox` discovery, file-stability tracking, SHA-256 hashing, and
  content-based deduplication (the same audio at several paths becomes
  one recording with multiple sources)
- sample-based language routing (beginning/middle/end windows) with
  configurable routing profiles and an oMLX classifier
- automatic high-confidence routing with full transcription
  (`routing_verified = false` until a human confirms)
- low-confidence/ambiguous results → Needs Review, with CLI manual
  override and retry
- transcribe with MacWhisper and preserve transcript history
- summarize with the configured local oMLX model, including bounded
  long-transcript chunking
- suggest configurable tags while preserving manual tag decisions
- browse recordings, transcripts, summaries, history, tags, and the
  review queue in a local web interface

Keyword/semantic search is planned for Step 5. Manual topic splitting,
scheduling, and retention deletion are planned for Step 6. **The app does
not currently delete, move, or modify audio files.**

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

If this is your first time using the app with real recordings, follow the
Chinese [first real-audio walkthrough](docs/first-real-audio-test.md). It
uses a small copied test batch and explains what to click when routing or
processing needs attention.

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
| Routing profile model not installed            | WARN              | 0         |
| afinfo/afconvert missing (routing needs review)| WARN              | 0         |
| Legacy `macwhisper.model` key in use           | WARN              | 0         |

Model verification states: a configured model is PASS only when a valid,
non-empty `/v1/models` list was retrieved and contains it; it is a WARN
when absent from the list, when the endpoint reports an explicit empty
list, or when the list could not be retrieved/validated. Malformed oMLX
responses (invalid JSON, non-object payloads, missing/non-list `data`)
are always warnings, never crashes; invalid entries inside `data` are
ignored. `brain doctor` never prints API keys and never fails on missing
optional external tools; a non-zero exit means a genuine required
failure (config, storage, or database).

### Pipeline commands

All pipeline commands accept `--json` for stable machine-readable
output. Mutating commands (`ingest`, `route`, `transcribe`, `run`,
`retry`) hold an exclusive lock under `data/temp/locks/`; a second
pipeline process exits with code **3** while one is already running.
Interrupted attempts are recovered at the start of each mutating
command (while the lock is held). Transcription timeouts are bounded:
`cli_timeout_seconds` is the hard maximum cap; short audio gets a small
minimum allowance and longer audio scales with duration
(`min(cap, max(minimum, duration-scaled))`).

```sh
uv run brain ingest          # discover and register stable new WAV files
uv run brain route           # auto-route pending recordings
uv run brain transcribe      # transcribe recordings with an approved profile
uv run brain run             # ingest -> route -> transcribe -> summarize
uv run brain status --json   # counts and failures
uv run brain review --json   # recordings needing human attention
uv run brain retry <id>      # explicitly retry a failed recording
uv run brain transcripts <id>
```

### Routing profiles and the routing policy

Routing profiles live under `macwhisper.routing.profiles` in
`config/config.yaml` (see `config/config.example.yaml`). Default
profiles: `cantonese` (apple:zh-HK), `mandarin` (apple:zh-CN),
`european` (parakeet-pro:nvidia_parakeet-v3; Finnish, English, and
Finnish–English mixtures all route here), and `european_small`
(installed but manual-only by default).

- **High confidence** (classifier confidence ≥
  `confidence_threshold`, evidence consistent): the profile is chosen
  automatically and fully transcribed. The routing decision is stored
  as `automatic` with `routing_verified = false` — an unverified
  automatic transcription you can later confirm or correct.
- **Low confidence / ambiguous / classifier unavailable**: the
  recording enters **Needs Review**; no full transcription runs until a
  human chooses a profile.
- **Manual override**:
  `brain route <id> --confirm` verifies the active automatic decision
  without retranscribing; `brain route <id> --profile <name>` selects a
  profile manually (add `--transcribe-now` to transcribe immediately in
  the same lock). Selecting a different profile on a transcribed
  recording schedules a retranscription (`ready_to_transcribe`); the
  old transcript stays active until the new one succeeds. Selecting the
  already-active profile on a transcribed recording marks the decision
  verified without retranscribing. Manual decisions are never
  overwritten by the automatic router, and retranscription preserves
  previous transcript versions.
- **Failures and recovery**: a failed initial transcription leaves the
  recording `failed` (only `brain retry` reactivates it — `brain run`
  never retries automatically). A failed *re*transcription keeps the
  active transcript and sets a queryable retranscription-failure marker
  (visible in `brain review` / `brain status`); `brain retry` retries
  it explicitly. Interrupted runs (process death) are recovered
  automatically at the start of the next mutating command: unfinished
  attempts are marked `interrupted` and in-flight states return to a
  safe point; recovered counts appear in `--json` output.

Language routing is heuristic. Cantonese-vs-Mandarin distinction relies
on colloquial vocabulary evidence plus the oMLX classifier; near-ties
always go to Needs Review. No routing-accuracy claims are made until
evaluated against human confirmations on real recordings.

The legacy `macwhisper.model` key still loads (mapped to a warned,
manual-only `legacy` profile when non-blank); migrate to
`macwhisper.routing.profiles`. The loader never modifies your
`config/config.yaml`.

### Web server

```sh
uv run brain serve                     # http://127.0.0.1:8787
uv run brain serve --host 127.0.0.1 --port 9000
```

- Binds to localhost only by default; no browser is opened.
- Missing configuration or runtime-directory setup failures print a
  concise error and exit with code 1 (no traceback, no Django startup).
- `GET /` — minimal status page (app version, storage availability,
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

## Current limitations

- No keyword or semantic search yet (Step 5).
- No manual topic splitting, scheduling, or retention deletion yet
  (Step 6).
- Automatic Cantonese-vs-Mandarin routing is heuristic and unverified —
  ambiguous evidence always lands in Needs Review.
- Router confidence is an uncalibrated score, not a probability.
- `european_small` is manual-only; retention deletion is **not active**
  — audio is never deleted by the app.
- `brain run` is a local CLI loop; scheduling (launchd) arrives in
  Step 6.

## Project layout

```
config/config.example.yaml   # committed example configuration
data/                        # runtime data (gitignored, created on demand)
src/brainlib/                # core library: config, paths, diagnostics, CLI
src/brain/                   # Django project (settings, urls, wsgi/asgi)
src/workflow/                # Django app (models, services, views, migrations)
src/manage.py                # conventional Django entry point
tests/                       # pytest suite (incl. sanitized MacWhisper fixtures)
```
