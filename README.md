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

# 3. Create the application database schema
#    (REQUIRED before doctor/run/serve; the app never migrates itself)
uv run python src/manage.py migrate

# 4. Optionally create .env for secrets
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
  inbox/        # WAV, MP3, and M4A recordings to process
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
| SQLite FTS5 + trigram missing                  | WARN              | 0         |
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
uv run brain ingest          # discover stable WAV, MP3, and M4A files
uv run brain route           # auto-route pending recordings
uv run brain transcribe      # transcribe recordings with an approved profile
uv run brain run             # ingest -> route -> transcribe -> summarize
uv run brain status --json   # counts and failures
uv run brain review --json   # recordings needing human attention
uv run brain retry <id>      # explicitly retry a failed recording
uv run brain transcripts <id>
```

### Search index (Step 5A.2 foundation)

```sh
uv run brain search-index status    # read-only health check (no lock)
uv run brain search-index rebuild   # atomic rebuild (takes the pipeline lock)
```

The keyword-search foundation is a relational `SearchDocument` registry
plus one contentful SQLite FTS5 table (`workflow_search_fts`,
`tokenize='trigram'`, rowid = registry pk). Indexed documents: every
non-empty segment of the active Transcript, every current
whole-recording Summary language variant (active Transcript only;
legacy `und` variants are indexed and reported as legacy), and one
deterministic metadata document per Recording (Library display title,
source filenames, active tags — never paths or secrets).

- `status` is strictly read-only and never locks. It compares the
  authoritative source data, the registry AND the actual FTS rows
  (IDs and content), never hashes alone. Exit **0** only when fully
  healthy; **1** for not built / stale / inconsistent /
  missing-or-broken FTS. `--json` reports stable categories, counts and
  document keys — never indexed text.
- `rebuild` runs in ONE transaction (validate schema → drop/recreate
  the derived FTS table → rebuild registry + FTS in bounded batches →
  complete verification → commit). Any failure rolls back and leaves
  the previous index byte-identical; a missing / wrong-schema /
  wrong-tokenizer FTS table is repaired by a rebuild.
- Keeping the index current after data changes is Step 5A.3; query
  parsing/ranking (`brain search <query>`) is Step 5A.4. The Library
  search field stays disabled until then.

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
- **Heuristic fallback** (classifier invalid or unavailable only): when
  the deterministic evidence is overwhelming and internally consistent —
  configured under `macwhisper.routing.heuristic_auto_route` — the
  Chinese-family profile (Cantonese or Mandarin, each with independent
  thresholds and a kill switch) is chosen automatically. The gate
  requires ALL of: Chinese family verdict, unambiguous zh verdict,
  minimum CJK ratio, minimum marker score, dominance over opposing
  marker scores, a low absolute ceiling on opposing scores, and enough
  non-silent sample coverage. These thresholds are heuristic evidence,
  **not calibrated probabilities**. With `auto_transcribe: true` (the
  default) such recordings transcribe without confirmation
  (`routing_verified = false` for audit); with `auto_transcribe: false`
  they wait in Needs Review like every other automatic route. European
  speech has no heuristic gate — it always needs the classifier or a
  human.
- **Low confidence / ambiguous / classifier unavailable with weak
  evidence**: the recording enters **Needs Review**; no full
  transcription runs until a human chooses a profile.
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
  attempts are marked `interrupted`, in-flight states return to a
  safe point, and orphaned temp files (e.g. normalized audio after a
  SIGKILL) are swept from the bounded `data/temp` namespaces; recovered
  counts appear in `--json` output.

### Non-WAV input (MP3/M4A) and speaker labels

- With `macwhisper.normalize_input: true` (default), MP3/M4A sources
  are converted to a temporary 16 kHz mono PCM WAV under `data/temp`
  before full transcription. The original file is read-only — never
  moved, renamed, or deleted — and the temp copy is removed after the
  attempt. Provenance (source format, normalization, speaker-fallback
  runs) is stored in the attempt history.
- `macwhisper.speakers: true` requests speaker detection. Some models
  do not support diarization (validated on MacWhisper 14.8: the apple
  zh models reject `--speakers` with a stable error). The error is now
  extracted and stored verbatim-sanitized instead of the progress line.
  With `macwhisper.speakers_fallback: true` (default **false**), one
  automatic `--no-speakers` retry runs after that specific failure and
  the degraded (no speaker labels) result is visibly reported; both
  runs stay in the attempt context.

Language routing is heuristic. Cantonese-vs-Mandarin distinction relies
on colloquial vocabulary evidence plus the oMLX classifier; near-ties
always go to Needs Review. No routing-accuracy claims are made until
evaluated against human confirmations on real recordings.

The legacy `macwhisper.model` key still loads (mapped to a warned,
manual-only `legacy` profile when non-blank); migrate to
`macwhisper.routing.profiles`. The loader never modifies your
`config/config.yaml`.

### Summaries in multiple languages

Summaries are versioned per **output language** ("variants"). The
variant identity is the canonical `Summary.output_language` code
(`en`, `fi`, `zh-Hant`, …), stored on the Summary; a language-aware
variant state tracks whether each variant is `current`, `missing`, or
`failed` for the active transcript.

Language policy (single source: `workflow/services/languages.py`):

- Canonical BCP-47 casing: primary subtag lowercase, script Titlecase,
  region uppercase (`en`, `fi`, `en-US`, `zh-HK`, `yue-HK`, `zh-Hant`).
  Malformed codes are never persisted.
- Chinese-family sources (`zh`, `yue`, `cmn` with any subtags) resolve
  every **default** and **original** output to `zh-Hant` (Traditional
  Chinese). Non-Chinese sources default to an English summary.
- The source language on a transcript is provenance-tracked
  (`language_observed_verified_by`: LLM detection, routing, or user).

Commands:

```sh
uv run brain summarize <id> --language default    # derived default
uv run brain summarize <id> --language original   # source language
uv run brain summarize <id> --language en         # explicit English
uv run brain summarize <id> --language zh-Hant    # explicit Traditional Chinese
uv run brain summary <id> --language fi           # display an existing variant
uv run brain transcript-language <id>             # view detected source language
uv run brain transcript-language <id> --set yue   # correct it (atomic, locked)
```

Generation accepts only the four selectors (`default`, `original`,
`en`, `zh-Hant`). An `original` request with an unknown source language
performs a bounded local detection first (at most two model calls, and
only one for endpoint/HTTP/timeout/size failures; the failure category
— `endpoint_unavailable`, `timeout`, `http_error`, `request_too_large`,
`response_too_large`, `source_language_unknown` — is stored durably on
the attempt and surfaced unchanged by the CLI and web). Read/display
and export additionally accept any concrete language that already
exists for the recording (e.g. `fi` after an Original generation);
from a concrete tab whose language an approved selector produces (the
Finnish example), the Regenerate action submits `original` and returns
to the `fi` tab. Variants no selector can produce remain readable and
exportable but offer no generation action. The summary source language
reported by the model is canonicalized before storage; malformed codes
are treated as invalid model output (one retry). Tags are materialized
only from the default variant; other variants never overwrite
Recording-level default state.

The web interface mirrors this on the recording detail AND summary
pages: language tabs (Default / English / Traditional Chinese /
Original plus existing variants such as Finnish), per-variant state,
Generate/Retry/Regenerate actions with language-preserving
confirmation, and language-preserving Copy/Markdown/text/JSON export
links. Action confirmations bind every input that determines language
resolution (a source-language correction invalidates an already-open
confirmation), and actions return to the page they originated from
(detail or summary) with the selected language. All GET requests are
strictly read-only.

Source-language provenance follows one deterministic rule: a known
canonical Transcript source language is authoritative for the
generated Summary (the model's empty or contradictory answer is
ignored); only when the transcript source is genuinely unknown may the
model's canonicalized value fill it in. `Summary.language` and
`Transcript.language_observed` always agree.

### Web server

```sh
uv run brain serve                     # http://127.0.0.1:8787
uv run brain serve --host 127.0.0.1 --port 9000
```

- Binds to localhost only by default; no browser is opened.
- Missing configuration or runtime-directory setup failures print a
  concise error and exit with code 1 (no traceback, no Django startup).
- `GET /` — redirects to the Library (`/recordings/`).
- `GET /recordings/` — the Library: browse recordings as cards or a
  responsive table, with from/to date, tag and sort filters (Newest,
  Oldest, Title A–Z, Title Z–A) and month headings for chronological
  sorts. Card/Table preference is remembered via a server-owned
  `view=`-overridable cookie; everything works without JavaScript.
  Keyword search is coming in a later Step 5 substep (the FTS5 index
  foundation and `brain search-index` exist; the search field stays a
  disabled placeholder until queries are wired).
- `GET /status/` — the status page (app version, storage availability,
  MacWhisper/oMLX configuration, selected models, pipeline counts).
  Page loads run only lightweight local checks; they never launch
  MacWhisper or query the oMLX endpoint — use `brain doctor` for full
  diagnostics.
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

- No keyword or semantic search UI yet (Step 5A.2 delivered only the
  persistent FTS5 index foundation; querying and incremental
  synchronization are later Step 5 substeps).
- No manual topic splitting, scheduling, or retention deletion yet
  (Step 6).
- Automatic Cantonese-vs-Mandarin routing is heuristic and unverified —
  ambiguous evidence always lands in Needs Review. The heuristic
  fallback thresholds are uncalibrated evidence, not probabilities.
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
