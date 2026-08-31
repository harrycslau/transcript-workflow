# Project status — implementation handoff (end of Step 2)

This file reflects the repository as of the end of Step 2. It is a
snapshot, not a durable instruction file; `AGENTS.md` holds the
standing rules.

## Step 1 — delivered

- `uv`-managed project (Python 3.12, Django 5.2 LTS, SQLite).
- `brainlib` core: `config.py` (required YAML config `config/config.yaml`,
  `BRAIN_CONFIG` override, code defaults, `.env` secret loading,
  strict validation with concise errors), `paths.py` (runtime dir
  creation, non-destructive writability probe via `tempfile.mkstemp`),
  `diagnostics.py` (`brain doctor` PASS/WARN/FAIL checks; FAIL only
  for config/storage/database problems), `cli.py` (`brain` entry point,
  config errors concise on stderr — no tracebacks even in fresh
  processes).
- Django project `brain` (localhost-only, settings read the shared
  loader) + `workflow` app; minimal status page (`/`) and JSON
  `/health/` (200 ok/degraded, 503 unhealthy; sanitized statuses; no
  subprocesses or network on page loads).
- `brain serve` (default `127.0.0.1:8787`, `--host/--port`, no browser).
- Runtime layout under `data/` (inbox, database, transcripts, exports,
  logs, temp) — gitignored, created on demand.

## Step 2 — delivered

### Database models (`src/workflow/models.py`, migrations `0001`+`0002`)

- `Recording` — content identity (unique `sha256`), `duration_seconds`,
  `recorded_at` (filename-derived, timezone-aware), `processing_status`,
  `audio_status`, `failure_stage`, `retranscription_failed`,
  `last_failed_attempt`.
- `AudioSource` — observed file paths (unique casefolded
  `path_identity`), stability tracking (`file_size/mtime/stable_since`),
  `discovery_state` (observing/hashing/hashed/failed), presence,
  `is_canonical`.
- `RoutingDecision` — append-only history; `method`
  automatic/manual, `confidence` (router score, not a calibrated
  probability), bounded `evidence` JSON, `routing_verified`,
  `verified_at/by`, one `is_active` per recording (partial unique).
- `ProcessingAttempt` — immutable per-attempt provenance (stage,
  ordinal, safe argv JSON, `mw_version`, outcome, sanitized errors);
  partial unique: one unfinished attempt per (recording, stage).
- `Transcript` (versioned; partial unique: one active per recording) →
  `TranscriptSegment` (unique transcript+ordinal) → `Section`
  (unique transcript+ordinal; Step 2 creates exactly one
  whole-recording Section).

### Pipeline states (`services/statemachine.py`)

`discovered → hashing → routing → {needs_review | ready_to_transcribe}
→ transcribing → transcribed`, plus `failed`. `audio_status`
(present/missing) is orthogonal. `brain run` never auto-retries
`failed`; only `brain retry` reactivates. A failed retranscription
keeps the active transcript and sets `retranscription_failed`.

### Services (`src/workflow/services/`)

- `pipeline_lock.py` — `flock` at `data/temp/locks/pipeline.lock`;
  mutating commands hold it, contention exits 3; recovery runs under
  the lock.
- `ingest.py` — case-insensitive WAV discovery restricted to the
  configured inbox (symlinks out are ignored), persisted stability
  observations, verified SHA-256 (size/mtime re-checked around
  hashing), content-identity dedup (one Recording, many AudioSources),
  canonical-source selection (deterministic), missing/reappeared
  reconciliation, content replacement at an existing path → detach +
  rehash; out-of-inbox sources parked `outside_current_inbox`.
- `audiosamples.py` — duration via stdlib `wave`/`afinfo` (plain text,
  not JSON), `afconvert` to 16 kHz mono PCM, beginning/middle/end
  windows (15 s) merged chronologically into one composite WAV, per
  -window silence detection (all-silent ⇒ silent), cleanup helpers.
- `routing.py` — sample routing: candidates `cantonese` (apple:zh-HK),
  `mandarin` (apple:zh-CN), `european` (parakeet-pro:nvidia_parakeet
  -v3) transcribe the composite with `--no-speakers`; deterministic
  heuristics (Cantonese/Mandarin colloquial markers; script ratio is
  weak evidence only) + strict oMLX classifier
  (`{route, confidence, reason_code, evidence}`; envelope and schema
  validated; HTTP/connectivity → unavailable, malformed → invalid; both
  → `needs_review`). Stable reason codes: `auto_confident`,
  `low_confidence`, `zh_ambiguous`, `candidates_disagree`,
  `classifier_unavailable`, `classifier_invalid`, `sampling_failed`,
  `routing_disabled`, `contradictory_evidence`, `too_short`,
  `silent_audio`.
- `transcription.py` — safe argv (no shell, per-run `--model`, no
  `--overwrite`), timeout `min(cap, max(minimum, duration-scaled))`
  with `cli_timeout_seconds` as hard cap, stdout capped, strict
  MacWhisper JSON validation (ms timestamps; booleans/negative/non
  -finite/out-of-order rejected, slight overlap tolerated), atomic
  versioned transcript persistence.
- `pipeline.py` — orchestration (`run_pipeline` = recovery → ingest →
  route → transcribe), `manual_route` (different profile on a
  transcribed recording ⇒ pending retranscription; same profile ⇒
  verify without retranscribing), `confirm_routing`, `retry`,
  `recover_interruptions` (unfinished attempts → `interrupted`; orphan
  in-flight states → safe points; idempotent; counts surfaced in
  `--json`), `validate_source_for_processing` (explicit outcome; used
  immediately before routing/transcription).

### CLI (`src/brainlib/cli.py`)

`doctor`, `serve`, `ingest`, `route` (`--profile`, `--confirm`,
`--transcribe-now`), `transcribe`, `run`, `status`, `review`,
`retry`, `transcripts` — all pipeline commands support `--json`.
Exit codes: 0 ok/warnings, 1 config/setup error (concise stderr, no
traceback, verified in fresh subprocesses), 2 usage, 3 lock contention.

### Routing policy

- High confidence (≥ `confidence_threshold`, evidence consistent) →
  automatic profile + full transcription, `routing_verified=false`
  (unverified automatic transcription; human confirmation later).
- `auto_transcribe: false` ⇒ automatic suggestions always wait in
  `needs_review`.
- Low confidence / zh-ambiguity / candidates disagree / classifier
  unavailable or invalid / sampling failure / silent / too short ⇒
  `needs_review`.
- `european_small` profile is manual-only. Cantonese-vs-Mandarin
  accuracy is explicitly NOT claimed until evaluated on real labelled
  recordings.
- MacWhisper facts (validated live on 14.7.1): parakeet rejects
  `--language multilingual` ⇒ all profiles use `language: null`;
  JSON output is `{"segments":[{start/end in ms, id, text, words[]}],
  "text"}`; fixture in `tests/fixtures/macwhisper/parakeet_json.json`.

## Web behaviour (minimal)

`/` — status page (version, storage paths, MacWhisper presence without
spawning, oMLX config without network calls, pipeline counts).
`/health/` — sanitized JSON, 200 ok/degraded, 503 unhealthy. Full UI is
Step 4.

## Tests and verification status

- 307 tests passing (`pytest`); no real MacWhisper, oMLX, network,
  ffmpeg, or user audio; "must not happen" mocks raise.
- Verified: `manage.py check`, `makemigrations --check`, fresh-process
  CLI config errors (no traceback), `git diff --check`.
- Sanitized MacWhisper fixtures: `tests/fixtures/macwhisper/`.

## Known limitations

- Cantonese↔Mandarin auto-routing accuracy unproven (needs labelled
  real recordings; zh-ambiguity defaults to Needs Review).
- Router confidence is uncalibrated.
- No summaries/tags/search yet; `initial_tags` is config-only.
- No retention deletion (never deletes audio); unverified-routing
  eligibility for future retention is an open Step 6 policy decision.
- `audioop` deprecation (Python 3.13 removal; revisit before upgrade).
- Parked recordings (missing/out-of-inbox sources) wait for the next
  ingest/run; no proactive notification.

## Step 3–6 roadmap (agreed)

- **Step 3 (next)**: structured JSON summaries rendered deterministically
  as Markdown/plain text; configurable multi-select tags from YAML;
  bounded chunking for long transcripts; oMLX endpoint/model
  configurable. No web UI beyond the status page.
- **Step 4**: full web interface, review queue, transcript/summary
  views, tag editing/filtering, manual routing controls.
- **Step 5**: FTS5 keyword search, local embeddings, semantic/hybrid
  search, Ask-with-citations.
- **Step 6**: user-initiated topic splitting, section-level
  summaries/tags, retention cleanup (only after successful processing
  + retention delay; Keep-Audio override), missing-file reconciliation
  UI, launchd scheduling.

### Step 3 decisions

- Summaries are stored canonically as **structured JSON** (never only
  prose), rendered deterministically to Markdown/plain text.
- Tags are configurable multi-select from YAML (`initial_tags` becomes
  the seed); manual tags protected from AI overwrite later.
- oMLX endpoint/model stay configurable; keys remain env-only.
- Long transcripts require bounded chunking before summarization.
- Full web UI (Step 4), semantic search (Step 5), manual splitting /
  retention cleanup / scheduling (Step 6) are deferred.
