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

Keyword search is available on the CLI (`brain search`) and in the
Library's web UI (`/recordings/`): scoped filters before ranking,
highlighted `<mark>` snippets and segment jump links
(Step 5A.4.2 complete). Semantic/hybrid search and Ask-with-citations
are planned for Step 5B. Manual topic splitting, scheduling, and
retention deletion are planned for Step 6. **The app does not currently
delete, move, or modify audio files.**

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
- The index is kept current automatically (Step 5A.3 post-commit
  per-recording synchronization) and is queried read-only by
  `brain search` (Step 5A.4.1, below) and by the Library's top-bar
  keyword search (Step 5A.4.2a/b, below).

### Keyword search (Step 5A.4.1)

```sh
uv run brain search "budget meeting"              # AND-combined keywords
uv run brain search "budget" --limit 20 --json
```

- Plain-text queries only: the input is NFC-normalized and split into
  whitespace-separated terms combined with AND. Quotes, `%`, `_`, `\`
  and FTS operator syntax are literal user text, never executed as
  raw FTS MATCH syntax. Matching is case-insensitive via Unicode
  folding; diacritics are NOT folded (`a` is not `ä`). Terms of 1–2
  codepoints (which the trigram tokenizer cannot match at all — short
  CJK included) use an escaped Unicode-aware `LIKE` fallback; longer
  terms use safely quoted FTS phrases.
- Strictly read-only: searching never locks and never rebuilds,
  repairs or synchronizes anything. It first runs the full read-only
  integrity check EXACTLY once; a missing, broken or stale index is a
  clean exit 1 pointing at `brain search-index rebuild` — never
  partially-served stale results. The reusable query engine itself is
  separate from that gate; the web search (Step 5A.4.2a/b, below) chose
  the strictest policy instead of any health cache: the same full
  sweep runs exactly once per submitted search.
- Results are deduplicated to ONE entry per Recording with
  deterministic, rebuild-stable ranking. Candidate selection is
  bounded globally and per Recording; the per-Recording bound keeps
  Summary/metadata candidates before segment floods, and ANY overflow
  of either bound sets `truncated` (your ranking is then a best-of-
  bounded-prefix approximation for the affected Recording, never a
  silent lie). `more_recordings_matched` is exact unless the global
  bound actually cut the fetch, in which case it is null. Snippets
  are plain text with structured highlight offsets (no HTML): the
  window is hard-capped around the FIRST individual match — repeated
  or tiling matches can never blow it past the cap; a match crossing
  the window edge is clipped but stays highlighted, and highlights
  cover whole source characters even when a match ends partway
  through a casefold expansion like `ﬃ` or `ß`.
  Each result records WHERE the best match came from: Recording
  metadata, a Summary language variant (`--json` shows
  `output_language`) or a transcript segment (with timestamp).
- Exit codes: **0** searched (also with zero results), **1** config or
  missing/broken/stale index, **2** malformed query or bad `--limit`.

### Library keyword search (Step 5A.4.2a/b)

The top-bar search field on `/recordings/` runs the same read-only
engine without leaving the Library:

- One submitted search runs the FULL index integrity sweep EXACTLY
  once (no health cache): a missing, broken or stale index is a
  friendly page pointing at `brain search-index status` / `rebuild` —
  never partially-served stale results, and the web layer never
  repairs anything itself.
- Active Library filters (dates, tags) scope the search itself: they
  narrow the engine's candidate population BEFORE relevance ranking,
  result bounds and pagination, so lower-ranked in-filter matches are
  never starved out by an out-of-filter flood. Invalid scope filters
  run the search unscoped, labelled honestly; an invalid sort falls
  back to Relevance and keeps every valid filter.
- Sorting: Relevance (the engine's deterministic comparator order —
  default and fallback in search mode) or any of the four Library
  sorts applied across the whole returned match set before pagination.
- The query, view mode and filters persist across pagination, view
  toggles and filter edits via plain links/form state; everything
  works without JavaScript.
- Privacy: an invalid query or an index-failure page NEVER echoes the
  rejected text back; error messages are fixed, actionable texts.
- Strictly read-only: a search GET never writes, locks, rebuilds or
  synchronizes; result cards are batch-fetched (no per-result
  queries).
- Highlights: snippet match ranges render as semantic `<mark>`
  elements built from plain-text fragments (server-side, autoescaped;
  no raw HTML is ever generated). Card and Table views show identical
  highlights and provenance.
- Segment jump links: when the match is a transcript segment, its
  provenance chip links straight to the transcript page containing it
  (`?page=N#segment-<ordinal>`, using the configured
  `web.transcript_segments_per_page`). The link is created only after
  one bounded batch query proves the indexed transcript is still the
  ACTIVE one AND belongs to the same recording — stale or mismatched
  provenance keeps a plain label, never a wrong jump.
- Accessibility: the result count is a polite status region (never a
  per-row live region), segment chips are keyboard-focusable links,
  the anchored transcript paragraph gets a landing highlight, and
  everything still works with no JavaScript and the same strict CSP.

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
  Keyword search exists on the CLI (`brain search`, Step 5A.4.1) and
  in the Library itself: the top-bar field runs a keyword search over
  the same read-only engine, with `<mark>` highlights and transcript
  jump links (Steps 5A.4.2a/b) — see the Library keyword search
  section below.
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

- Library web search is keyword-only (Step 5A.4.2 complete with
  highlights, jump links and styling/a11y): semantic search/hybrid
  ranking and Ask-with-citations are later Step 5B work. Keyword
  matching is substring-style (FTS5
  trigrams + a Unicode-folded LIKE fallback for 1–2-codepoint terms),
  not stemmed or word-tokenized.
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
