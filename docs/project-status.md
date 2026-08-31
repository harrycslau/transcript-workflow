# Project status — implementation handoff (end of Step 3)

This file reflects the repository as of the end of Step 3. It is a
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

## Step 3 — delivered

### Summarization

- `Summary` model (migration `0003`): versioned structured summaries
  belonging to a `Transcript` (+ its whole-recording `Section`,
  NOT NULL; Step 6 section-level summaries reuse the scope). Constraints:
  `uniq_summary_ordinal (recording, ordinal)` and
  `uniq_active_summary_in_scope (transcript, section, is_active)`.
  The "current summary of a recording" is DERIVED: the active Summary
  of the active Transcript (`Recording.current_summary()`). Old
  transcripts' summaries stay `is_active=True` forever (historically
  valid, not current). Canonical storage is the validated structured
  payload; Markdown/plain text is rendered deterministically
  (`services/rendering.py`).
- oMLX client (`services/llm.py`): OpenAI-compatible chat completions,
  httpx only, env-indirect API key (never stored/logged), 2 MiB
  streamed-response cap, strict envelope validation, sanitized error
  taxonomy (`endpoint_unavailable`, `timeout`, `http_error`,
  `response_too_large`, `malformed_http_json`, `invalid_envelope`,
  `malformed_model_json`, `schema_validation`, `input_too_large`).
- Chunking (`services/chunking.py`): the ENTIRE transcript is
  deterministically chunked on segment boundaries (code-point-safe
  hard split for oversized segments, trailing-segment overlap). No
  truncation. Pre-flight checks against `max_total_characters` and
  `max_chunk_count` finish a durable `ProcessingAttempt` with
  `error_code=input_too_large` (measured input chars, computed chunk
  count, limits fingerprint) and ZERO HTTP calls; no Summary is
  created. `max_input_characters` is the per-REQUEST cap, enforced on
  the fully serialized payload (scaffolding + JSON escaping included)
  for every map/sub-reduce/final-reduce call; oversized reduce inputs
  use deterministic hierarchical reduction (clean `input_too_large`
  failure if even a single intermediate cannot fit). Short transcripts
  use exactly one call.
- Map stage: bounded intermediate `{overview, key_points}` per chunk;
  reduce stage merges intermediates (chronological) into the final
  schema (`title`, `overview`, `key_points`, `action_items` with
  `owner`/`due_date` null-safe, `people`, `organizations`, `topics`,
  `suggested_tags`, `language`). Strict validation: booleans rejected
  where strings expected, bounded counts/lengths, fenced JSON
  tolerated. Summary language follows the speaker's dominant language.
- Persistence (`services/summarize.py:persist_summary`) is the single
  atomic path: enforces `section.transcript_id == transcript_id` and
  `transcript.recording_id == recording_id`, deactivates only the same
  transcript's active whole-recording summary, activates the new one.
- Provenance on Summary: attempt, model, base_url, prompt/parser
  version, config fingerprint, chunk_count, input_characters,
  `input_truncated=False`, `limits_used`, generation mode, raw
  suggested tags (incl. rejected names).

### State, failure, retry

- `Recording.summary_status` (`not_ready|missing|current|failed`) is
  orthogonal to `processing_status`; failed summarization never makes
  transcription look failed. `resummarization_failed` +
  `last_failed_attempt` mark failed regenerations (current summary kept).
- `brain run` = recovery → ingest → route → transcribe → summarize;
  automatic summarization only for `missing` (never-attempted) — no
  auto-retry loop. `brain summarize ID` / `brain retry ID` are explicit;
  `brain summarize ID --regenerate` forces a new version.
- `recover_interruptions` closes unfinished summarization attempts and
  reconciles summary state idempotently (interrupted first attempt →
  `missing`; interrupted regeneration → `current` + retryable warning).

### Tags

- YAML `tags.allowed` (legacy `initial_tags` seeds it with a doctor
  WARN). `Tag` rows sync by NFC+casefold `name_key`: created, updated,
  RETIRED (never deleted) on removal, reactivated on re-add; display
  name preserved from first sync.
- `SummaryTagSuggestion` records per-summary-version provenance;
  `TagAssignment` (unique per recording+tag, partial unique on active)
  holds effective assignments with origin `suggested|manual|confirmed`
  and `source_summary`. Regeneration refreshes only `suggested` rows;
  manual assignments are never touched. Unconfigured suggestions are
  recorded as rejected, never persisted; `Unknown` is dropped when any
  real tag is suggested.
- `brain tags` is genuinely read-only; `brain tags --sync` mutates
  under the pipeline lock; summarization syncs inside the locked path.

### CLI (Step 3 additions)

`summarize [ID] [--regenerate]`, `summaries ID`,
`summary ID [--format markdown|text|json]` (copy-friendly Markdown by
default), `tags [--sync]`; `status`/`review` and the home page gained
summary counts (`awaiting_summary`, `summary_failed`, `summarized`,
`failed_resummarization`). Exit codes unchanged (0/1/2/3).

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

- 440 tests passing (`pytest`); no real MacWhisper, oMLX, network,
  ffmpeg, or user audio; "must not happen" mocks raise.
- Verified: `manage.py check`, `makemigrations --check`, fresh-process
  CLI config errors (no traceback), `git diff --check`.
- Sanitized MacWhisper fixtures: `tests/fixtures/macwhisper/`.

## Known limitations

- Cantonese↔Mandarin auto-routing accuracy unproven (needs labelled
  real recordings; zh-ambiguity defaults to Needs Review).
- Router confidence is uncalibrated.
- No per-chunk retry memoization (a retry re-runs the bounded map+reduce);
  `confirmed` tag origin and tag editing UI are Step 4.
- Summarization retry of oversized inputs requires a config change
  (`input_too_large` never auto-recovers); no partial summaries.
- No retention deletion (never deletes audio); unverified-routing
  eligibility for future retention is an open Step 6 policy decision.
- `audioop` deprecation (Python 3.13 removal; revisit before upgrade).
- Parked recordings (missing/out-of-inbox sources) wait for the next
  ingest/run; no proactive notification.

## Step 3–6 roadmap (agreed)

- **Step 4 (next)**: full web interface, review queue, transcript/summary
  views, tag editing/filtering, manual routing controls.
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
- Summaries belong to a Transcript (+ whole-recording Section); the
  recording's current summary is derived from the active transcript.
- Summarization state is orthogonal to `processing_status`
  (`summary_status` + `resummarization_failed`).
- Chunking never truncates: whole transcript chunked on segment
  boundaries; `max_total_characters`/`max_chunk_count` failures are
  clean, durable, zero-HTTP pre-flight failures.
- Per-request cap (`max_input_characters`) enforced on the fully
  serialized payload; hierarchical reduce for oversized intermediates.
- Tags are configurable multi-select from YAML (`initial_tags` seeds
  `tags.allowed` with a WARN); manual tags protected from AI overwrite;
  removed tags retired, never deleted.
- `brain tags` read-only; `brain tags --sync` / summarization mutate
  only under the pipeline lock.
- Full web UI (Step 4), semantic search (Step 5), manual splitting /
  retention cleanup / scheduling (Step 6) are deferred.
