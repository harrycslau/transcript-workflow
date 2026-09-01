# Project status — implementation handoff (end of Step 4)

This file reflects the repository as of the end of Step 4 plus the
post-incident routing/transcription fixes. It is a snapshot, not a
durable instruction file; `AGENTS.md` holds the standing rules.

## Post-incident fixes (routing + MP3/M4A transcription)

Real incident: an MP3 with overwhelming Cantonese evidence
(zh-HK marker score 9.61 vs 0.0, CJK ratio 0.826, three non-silent
windows) landed in `uncertain` solely because the oMLX classifier
output was invalid; afterwards, full transcription with
`apple:zh-HK --speakers` failed and only the stderr progress line was
stored, hiding the real error. Synthetic validation on MacWhisper
14.8 (1480) proved the stable signature: `apple:zh-HK` rejects
`--speakers` ("does not support speaker detection (diarization)") on
WAV/M4A/AAC-in-renamed-.mp3 — the incident's transcription could never
have succeeded with diarization requested. (Note: the synthetic "MP3"
fixture contained AAC data under an `.mp3` name because macOS afconvert
has no MP3 encoder, so NATIVE MP3 direct input was NOT proven by that
test. With normalization on the default path, native MP3 is converted
to PCM WAV before full transcription, so direct MP3 support is not
required.)

- **Heuristic auto-route gate** (`macwhisper.routing.heuristic_auto_route`):
  used ONLY when the classifier is invalid or unavailable. All
  independent conditions must hold for exactly one enabled Chinese
  family (chinese family verdict, unambiguous zh verdict, min CJK
  ratio 0.60, min marker score 4.0, dominance ratio 3.0 over opposing
  scores, opposing ceiling 0.5, ≥2 non-silent windows). Reason codes
  `auto_confident_heuristic_classifier_invalid` /
  `auto_confident_heuristic_classifier_unavailable`; `ready_to_
  transcribe=True` from the gate, but `_apply_outcome` still applies
  `routing.auto_transcribe` (false ⇒ Needs Review). Scores are
  uncalibrated evidence, never probabilities; no European gate. The
  incident evidence passes the default gate.
- **Classifier request state machine** (finite, no loops): one
  structured request (`response_format` json_schema); one plain
  request ONLY after an explicit HTTP 400/422 response_format/json_schema
  capability rejection; one repair request ONLY after an HTTP-successful
  schema-invalid response. Restricted parser tolerates pure JSON, one
  fence, or one closed bounded `<think>...</think>` block followed by
  the object; rejects commentary/multiple objects. Bounded diagnostics
  (call count, capability, stable validation categories) stored in
  evidence; response bodies/prompts never persisted.
- **Transcription stderr**: `Error:` line + up to two diagnostic lines
  selected (progress line ignored), path-sanitized, 300-char cap at
  persistence AND rendering; stable categories
  (`mw_connection_failure`, `mw_speakers_failure`, `mw_input_unreadable`,
  `mw_nonzero_exit`).
- **Input normalization** (`macwhisper.normalize_input`, default true):
  non-PCM-WAV sources (MP3/M4A) are converted to a temporary 16 kHz
  mono PCM WAV under `data/temp/transcription/<recording>/attempt_<n>/`;
  original read-only; temp removed in `finally` AND by the
  interruption-aware orphan sweeper.
- **Orphan temp cleanup** (`workflow/services/tempcleanup.py`): runs in
  `recover_interruptions` under the pipeline lock; deletes ONLY
  validated `<uuid>/attempt_<int>` dirs under the bounded
  `data/temp/{routing,transcription}` namespaces with no matching
  unfinished attempt; symlinks/invalid names never followed; counts in
  recovery `--json`.
- **Provenance**: migration `0006` adds nullable
  `ProcessingAttempt.context_json` (normalization facts, per-run
  outcomes, speakers fallback); `cli_args_json` shape unchanged.
- **Speakers fallback** (`macwhisper.speakers_fallback`, default
  **false**): one automatic `--no-speakers` retry within the same
  attempt ONLY on the validated stable diarization signature; both runs
  recorded; degradation visibly reported in CLI result, web flash, and
  attempt history.
- Router version bumped to "2"; heuristic gate settings fingerprinted
  into evidence for later evaluation against human corrections.
- Suite after this round: 785 passing (baseline 689 + 96 new across the
  heuristic gate, classifier state machine/parser, normalization,
  speakers fallback, orphan sweep, config validation, and surfacing);
  Django check clean; `makemigrations --check` clean; `git diff --check`
  clean.

## Step 4 — delivered

### Web interface (server-rendered, local-first)

- Pages: dashboard `/` (existing status page), `/recordings/`
  (paginated, filterable), `/recordings/<id>/` (detail/working view),
  `/recordings/<id>/summary/` (current summary),
  `/recordings/<id>/summaries/<sid>/` (historical summary),
  `/recordings/<id>/transcript/` (segment-paginated; `?v=` historical),
  `/recordings/<id>/history/`, `/tags/`, `/review/`.
- List contract (`workflow/query.py`): `effective_at` annotation
  (`Coalesce(recorded_at, discovered_at)`) used consistently for
  ordering, local-day/range filtering and display; explicit
  `Prefetch(..., to_attr=...)` contract (current summary row, active
  tags, active routing decision, sources) consumed via
  `RecordingCard`; query count proven constant as row count grows;
  transcript text never loaded on the list. Filters: local calendar
  day (DST-correct via ZoneInfo), from/to range, multi-tag with
  explicit `tag_match=all|any` (AND default), processing/summary
  status, audio present/missing, has-summary, review union; invalid
  values render friendly messages.
- Detail page: structured summary rendered from validated fields only
  (never Markdown/HTML-trusted), provenance collapsed, transcript
  segment pages bounded by `web.transcript_segments_per_page`
  (default 200), copy controls (no-JS export links + JS clipboard
  button, non-destructive failure), sanitized attempt table.
- Summary versioning on the web: pages/exports distinguish
  `is_active_in_scope` (row field) from `is_current_for_recording`
  (derived: active summary of the active transcript's ordinal-0
  section). Historical summaries are labelled and exported with a
  banner; a scope-active old-transcript summary is NEVER presented as
  current.
- Exports (`views/exports.py`): summary Markdown/text/JSON and
  transcript text/timestamped, UTF-8, sanitized `brain-<sha>-` style
  filenames (header-injection safe), `?version=` selects historical
  versions resolved through the parent Recording (cross-recording
  access = 404), read-only.

### Tags (Step 4 semantics)

- Migration `0005`: `TagAssignment.deactivated_by` ("" | "user" |
  "model") + `RunPython` backfill (legacy inactive rows → "model") +
  `chk_tagassignment_deactivation_state` CheckConstraint: active rows
  must carry "", inactive rows must carry "user"/"model" (both invalid
  combinations are test-proven to be rejected by SQLite).
- Semantics: manual add is idempotent and user-owned; confirm upgrades
  a suggested assignment to `confirmed` (user-owned, survives
  re-summarization, provenance kept); remove/reject sets
  `deactivated_by="user"` — a SUPPRESSION. Re-summarization
  (`_materialize_tags`) deactivates dropped suggestions with
  `deactivated_by="model"`, reactivates only non-suppressed rows, and
  always appends `SummaryTagSuggestion` provenance — user-suppressed
  tags stay suppressed while suggestions remain visible.
  Retired tags are excluded from the add selector unless the explicit
  "include retired tags" opt-in is set.
- The partial active-unique constraint is acknowledged redundant beside
  unique(recording, tag); races are handled via transactions +
  row-locking + idempotent re-select, never via the redundant
  constraint.

### Web actions (POST only, two-step confirmation)

- `workflow/services/web_actions.py`: every action acquires the SAME
  global pipeline `flock` (busy → friendly 409 page), runs
  `recover_interruptions()` (stage-aware), re-derives eligibility from
  current DB state, and compares a state fingerprint
  (`processing_status`, summary markers, current-summary ordinal,
  newest attempt id) captured at form render; mismatch = safe no-op
  "state changed". Synchronous execution; no queues.
- Eligibility matrix: route (routing/needs_review/ready_to_transcribe/
  transcribed/routing-failed; transcribing and other states ineligible;
  same-profile = idempotent verify, NO new decision row; different
  profile on ready_to_transcribe appends and stays ready; on
  transcribed keeps the active transcript until retranscription
  succeeds), confirm-routing (idempotent, targets the active decision),
  transcribe (only ready_to_transcribe; duplicate POSTs can never
  retranscribe — fingerprint/eligibility reject), summarize
  (server-derived first/retry/regenerate wording), retry (failed /
  retranscription_failed / summary_failed / resummarization_failed).
- `manual_route` (CLI + web shared): eligibility restricted to the
  matrix above with clean ConfigError otherwise; same-profile on ANY
  eligible status verifies in place without appending.
- First POST renders a confirmation interstitial (duration, what is
  preserved on failure, retry-vs-summarize-vs-regenerate wording);
  second POST (`confirmed=1`, CSRF) executes; POST→redirect→GET with
  flash messages.

### Review dashboard

- Shared builder `workflow/services/review.py` used by BOTH
  `brain review` (CLI JSON unchanged plus additive `missing_audio`
  group and `error_code` on failed-retranscription rows) and
  `/review/`; groups: needs-review, unverified automatic routing,
  failed retranscription, pipeline failures, awaiting summary, failed
  summary, failed re-summarization, missing audio. Stable sanitized
  codes only; GET purity and bounded queries test-proven.

### Web configuration

- `web:` config section (`recordings_per_page` 25,
  `transcript_segments_per_page` 200): strictly validated positive
  ints, booleans rejected; documented in `config/config.example.yaml`.

### Security / accessibility

- Middleware: SecurityMiddleware (nosniff), CommonMiddleware, CSRF,
  MessageMiddleware (CookieStorage — signed message cookie,
  HttpOnly, SameSite=Lax, flags inherited from `SESSION_COOKIE_*`;
  no session table), XFrameOptions DENY, and a strict
  Content-Security-Policy (`default-src 'self'`, no inline scripts,
  static `app.js` only). CSRF test-proven with
  `Client(enforce_csrf_checks=True)`; child objects always resolved
  through the parent Recording; no secrets/paths/tracebacks on any
  page (home page's local storage-path display is pre-existing Step 1
  behaviour for the owner's own machine).
- `docs` warning: `brain serve --host 0.0.0.0` exposes private
  transcripts to the network; keep 127.0.0.1.

### Tests

- 638 passing (495 pre-existing + 143 new across list, detail,
  tags, actions, exports, review, security, walkthrough, config).
  Includes query-count invariance (5 vs 40 rows), DST day filters,
  CSRF 403s, lock 409, duplicate-transcribe non-retranscription,
  suppression survival, escaping, Unicode exports, GET purity with
  subprocess/httpx raise-guards.

## Migration readiness (post-Step-4 hardening)

- `brainlib/migrations.py`: read-only inspection via Django's
  `MigrationExecutor`/`MigrationRecorder` (graph leaf plan +
  explicit `check_consistent_history`); stable sanitized categories
  for unavailable table / inconsistent history; never applies
  migrations, never shells out to manage.py.
- `brain doctor` gained a `Database migrations` check (after the
  SQLite connection check): PASS `all migrations applied`, or FAIL
  with pending labels (`workflow.0003_...` style) and the recovery
  command. Inspection failure is FAIL with a stable category; raw
  exception text never appears.
- Shared CLI schema preflight (`_require_applied_migrations`) runs in
  every ORM command (run/ingest/route/transcribe/summarize/retry/
  status/review/transcripts/summaries/summary/tags, read-only and
  `--sync` alike) BEFORE lock acquisition/recovery/ORM/file/network
  work: pending migrations -> exit 1, concise actionable stderr with
  `uv run python src/manage.py migrate`, no traceback, no rows, no
  locks, no subprocess/network/inbox access, never auto-migrates.
- `brain serve` checks migration readiness after `django.setup()` and
  before binding: pending migrations -> exit 1, same message, the
  runserver machinery is never started.
- Exit codes unchanged: 0 ok, 1 config/setup (incl. pending
  migrations), 2 usage, 3 lock busy.

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
  conditional `uniq_active_summary_in_scope (transcript, section)` for
  rows where `is_active=True`.
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
  reconciles summary state idempotently and by stage. An interrupted
  first attempt becomes `failed`; an interrupted regeneration remains
  `current` with a retryable warning. Both require explicit retry.
  Unrelated routing/transcription recovery never changes summary
  eligibility or summary failure markers.

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
- `ingest.py` — case-insensitive WAV/MP3/M4A discovery restricted to the
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
  route → transcribe → summarize), `manual_route` (different profile on a
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

- 495 tests passing (`pytest`); no real MacWhisper, oMLX, network,
  ffmpeg, or user audio; "must not happen" mocks raise.
- Verified: `manage.py check`, `makemigrations --check`, fresh-process
  CLI config errors (no traceback), stage-aware cross-stage recovery,
  error/secret hygiene, and `git diff --check`.
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
