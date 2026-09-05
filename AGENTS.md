# AGENTS.md — durable repository instructions

Read this before any task. It contains the rules that hold across all
development phases of this project, not per-task status (see
`docs/project-status.md` for the current implementation handoff).

## Project purpose and principles

Brain is a **local-first, privacy-preserving transcript workflow** for
personal audio recordings (WAV/MP3/M4A): discover → route language → transcribe via
MacWhisper (`mw` CLI) → summarize/tag via a local oMLX
OpenAI-compatible endpoint → (later) search and split.

Non-negotiable principles:

- Everything stays on this machine. No cloud services, no telemetry.
- Audio and transcripts never leave the machine; the oMLX endpoint is
  localhost.
- The user's audio files are sacred: read-only to this software.

## Architecture

- Python 3.12+ (`.python-version`), managed with `uv`; `uv sync` first.
- Django 5.2 LTS, SQLite, Django templates (HTMX comes later).
- `src/` layout:
  - `src/brainlib/` — Django-independent core: `config.py` (YAML + env
    loading/validation), `paths.py`, `diagnostics.py` (doctor checks,
    PASS/WARN/FAIL), `cli.py` (`brain` entry point, argparse +
    `django.setup()` via Python APIs).
  - `src/brain/` — Django project (`settings.py` reads the shared
    config loader; localhost-only; `DEBUG=True` local dev).
  - `src/workflow/` — Django app: `models.py`, `services/` (ingest,
    routing, transcription, summarize, chunking, rendering, tags,
    audiosamples, statemachine, pipeline, pipeline_lock, search_index,
    library_metadata), `views.py`
    (status page + `/health/`),
    `migrations/`.
  - `src/manage.py` — conventional entry point only; the CLI is
    `brain` (`brainlib.cli:main`).

## Configuration and secrets

- Config file is REQUIRED: `config/config.yaml` (override with
  `BRAIN_CONFIG`). Code defaults supplement omitted keys; the selected
  YAML file itself must exist.
- Never modify the user's `config/config.yaml`; only
  `config/config.example.yaml` is committed. Legacy
  `macwhisper.model` stays backward-compatible with a doctor WARN.
- Secrets live only in the environment / `.env` (e.g.
  `BRAIN_LLM_API_KEY` named by `api_key_env` keys). Never put secrets
  in YAML, code, logs, errors, argv, or database records. When logging
  is needed, log categories/counts, never values.
- Relative storage paths resolve against the project root
  (`pyproject.toml` location). Runtime data lives under `data/`
  (gitignored) and is created on demand — never elsewhere.
- File-derived timestamps are timezone-aware (`timezone` config key,
  default `Europe/Helsinki`).

## Pipeline locking and concurrency

- SQLite has no advisory locks. All mutating pipeline commands
  (`ingest`, `route`, `transcribe`, `summarize`, `tags --sync`, `run`,
  `retry`, `search-index rebuild`) must hold the
  exclusive `flock` at `data/temp/locks/pipeline.lock`
  (`workflow/services/pipeline_lock.py`); second process exits with
  code 3. Read-only commands (`status`, `review`, `transcripts`,
  `summaries`, `summary`, `tags`, `search-index status`, `doctor`,
  `serve`) never lock.
- Run `recover_interruptions()` while holding the lock before new work
  in mutating commands; it is idempotent.
- DB constraints are the second concurrency layer: at most one
  unfinished `ProcessingAttempt` per (recording, stage), one active
  `Transcript` and one active `RoutingDecision` per recording, and one
  active `Summary` per (transcript, section) scope.

## Content identity, versioning, active-record invariants

- `Recording` = content identity (SHA-256, unique). `AudioSource` =
  each observed file path (casefolded `path_identity` unique);
  several sources can share one Recording.
- Transcripts are versioned: every successful transcription creates a
  new `Transcript` (+ segments + exactly one whole-recording
  `Section`), linked to its `ProcessingAttempt`. At most one active
  transcript per recording (partial unique constraint); retranscription
  deactivates the old one atomically and preserves it forever.
- `RoutingDecision` is append-only history; confirming updates the
  active decision in place (`routing_verified`, `verified_at/by`);
  profile changes append a new decision.
- `processing_status` (pipeline) and `audio_status`
  (present/missing) are orthogonal; deleting a transcribed
  recording's source audio keeps it `transcribed` + `audio_status=missing`
  with all history intact.
- Summaries are versioned structured data belonging to a Transcript and
  Section. The current Recording summary is derived from the active
  Transcript; summaries on old Transcripts remain historically active.
  A replacement Summary becomes active only after complete successful
  generation. Markdown/plain text are deterministic renderings, not the
  canonical stored representation.
- Tags are defined by YAML `tags.allowed` and synchronized explicitly;
  removed definitions are retired, never deleted. Model suggestions are
  versioned provenance. Manual/confirmed effective assignments are
  user-owned and must not be silently removed by regeneration.

## Multilingual summary variants (standing invariants)

- Language policy has ONE home: `workflow/services/languages.py`
  (canonical BCP-47 casing: primary lowercase, script Titlecase, region
  uppercase; `zh|yue|cmn[*]` → `zh-Hant` for default AND original
  output; malformed values never persisted). Output resolution lives in
  `workflow/services/langresolve.py`. Never re-implement normalization
  elsewhere; migration-local copies must mirror semantics explicitly.
- `workflow/services/variant_state.py:reconcile_variant_state` is the
  ONLY runtime writer of `SummaryVariantState` and the Recording-level
  default summary tuple after an active transcript exists. It derives
  truth from the DB (never trusts caller declarations), verifies
  transcript∈recording and section∈transcript, validates the
  output-language identity at its boundary (canonical; Chinese-family
  output only as `zh-Hant`; `und` is a migration-only marker), and
  updates Recording-level fields ONLY when the variant is the currently
  derived default of the ACTIVE transcript's ordinal-0 section.
  Regeneration-failure recency is by attempt ordinal, never wall-clock
  alone. The ONE documented initialization exception: the
  transcription-activation hook sets the new active transcript's
  default `summary_status=missing` (no reconciler event exists yet).
- Recovery is exact-scope only: detection attempts
  (`language_detection`) and provenance-less legacy attempts change no
  summary state (stable diagnostics surfaced instead); forged
  provenance and malformed/noncanonical `resolved` identities are
  rejected without writes. Never infer scope from the currently active
  transcript.
- Generation requests accept only `default`, `original`, `en`,
  `zh-Hant`. Read/display/export may additionally accept a concrete
  `output_language` that already exists; unknown concrete languages are
  a friendly 404 — never an uncaught exception and never a silent
  fallback to the default. Web action forms submit a DERIVED generation
  selector (`variant.action_selector`: a Finnish tab regenerates via
  `original`; unrepresentable concrete tabs are read-only with no
  action) and carry a separately validated read `return_language` for
  the redirect — the four-selector allowlist is never weakened.
- The action-state fingerprint binds EVERY stable input that determines
  language resolution (active transcript id, canonical source language
  and its verifier, resolved default and Original output languages with
  an explicit unresolved marker) in addition to recording state,
  attempts and variant languages — a source-language correction
  invalidates rendered confirmations even when no attempt is created
  and the action mode is unchanged. Fingerprinting is read-only
  (SELECTs only).
- Actions return to their origin page via a server-owned allowlist
  token (`return_view`: `detail | summary`) carried through the
  confirmation interstitial; missing/invalid/forged values fall back to
  recording detail. Arbitrary client URLs are never accepted.
- Summary `output_language` is the variant identity key
  (`uniq_active_summary_in_output_language`); `Summary.language` is the
  canonicalized detected source language, not the variant key —
  `validate_final_payload` applies one deterministic provenance rule:
  a known canonical Transcript source is AUTHORITATIVE (the model's
  empty/contradictory/malformed value is ignored; a user-verified
  Transcript language is never displaced), while a genuinely unknown
  source accepts a canonicalized model value (empty stays the
  unknown-source case; malformed values raise a stable
  schema-validation error with the normal invalid-output retry).
  `Summary.language` and `Transcript.language_observed` always agree.
  Tags are materialized only from the default variant.
- Explicit-Original detection is bounded (max two calls; retry only on
  invalid output) and every failure creates a durable finished attempt
  whose stable category (`endpoint_unavailable`, `timeout`,
  `http_error`, `request_too_large`, `response_too_large`,
  `source_language_unknown`) is surfaced unchanged through
  `summarize_one`, the CLI and the web layer — the attempt remains the
  source of truth, and no variant state exists until a concrete output
  language does.
- Web GET requests must remain strictly read-only (no detection, no
  network, no subprocess, no writes); unresolved Original is a status,
  never resolved by side effects on GET.

## Failure, retry, recovery

- Failed initial routing/transcription → `processing_status=failed`
  with `failure_stage`; only `brain retry` reactivates. `brain run`
  never auto-retries `failed` records.
- A failed *re*transcription keeps the active transcript, sets
  `retranscription_failed` + `last_failed_attempt` (queryable, surfaced
  by review/status); success clears them.
- Pre-stage source validation (`validate_source_for_processing`) is
  mandatory before routing/transcription: explicit outcome
  (valid/missing/changed/outside_inbox/no_source); missing or changed
  or out-of-inbox sources park the recording cleanly — never process
  changed content under the old SHA-256, never spawn MacWhisper on an
  unverified path, never create a fake failure attempt for a deleted
  file.
- Summarization state is orthogonal to `processing_status`. A new active
  Transcript receives one automatic summary attempt. Failed or
  interrupted summarization requires explicit retry; `brain run` must
  never auto-retry it. Failed regeneration preserves the current
  Summary and records `resummarization_failed` + `last_failed_attempt`.
- Recovery is stage-aware: routing/transcription interruption must not
  alter summary eligibility or failure markers. Only a recovered
  summarization attempt receives summary interruption reconciliation.
- Summarization uses the complete stored Transcript, never the source audio. Long
  input is deterministically chunked and hierarchically reduced; it is
  never silently truncated. Every request and response is bounded.

## Source-file and inbox safety

- Only ever READ user audio. Never move, rename, delete, truncate, or
  transcribe-with-side-effects files in `data/inbox`.
- Scanning and processing are restricted to the configured inbox;
  symlinks resolving outside it are never followed. Sources outside
  the current inbox are parked (`outside_current_inbox`) without file
  access.
- All temporary artifacts (routing samples, lock files) live under
  `data/temp` and are cleaned in `finally`.
- Full transcription of non-PCM-WAV input (MP3/M4A) uses a temporary
  normalized 16 kHz mono PCM WAV under
  `data/temp/transcription/<recording>/attempt_<n>/` (config
  `macwhisper.normalize_input`, default true). The original source is
  opened read-only; provenance lives in `ProcessingAttempt.context_json`
  (never transcript text, prompts, or secrets) while `cli_args_json`
  keeps its historical argv shape.
- `finally` cleanup does not run on SIGKILL: orphan temp dirs are swept
  during recovery ONLY inside the strictly bounded
  `data/temp/{routing,transcription}` namespaces, only for
  pattern-validated `<uuid>/attempt_<int>` dirs with no matching
  unfinished attempt; symlinks and invalid names are never followed,
  and no path is ever taken blindly from the database.
- MacWhisper stderr is never stored raw: meaningful `Error:` lines are
  selected (progress lines ignored), sanitized, capped (300 chars) at
  persistence AND rendering, with a stable error category kept separate
  from the free-text detail.
- Speakers fallback (`macwhisper.speakers_fallback`, default false)
  may retry ONCE with `--no-speakers` only on a stable, synthetic
  -validated diarization-specific failure signature; both runs stay in
  `context_json` and the degraded (no speaker labels) result is
  visibly reported. Requested diarization is never silently dropped.
- `mw` is invoked with argv arrays only, per-run `--model` overrides,
  bounded timeouts (`cli_timeout_seconds` is a hard cap:
  `min(cap, max(minimum, duration-scaled))`), `--format json` output
  validated before any DB write, stdout size-capped. Never pass
  `--overwrite` against user files, never change the globally selected
  model (`mw models` ▸ marker must not move).

## Routing rules (until superseded)

- Profiles live in `macwhisper.routing.profiles`; `cantonese`,
  `mandarin`, `european` are required and auto-selectable when routing
  is enabled; `language: null` means "omit `--language`" (parakeet
  rejects `multilingual`; validated on MacWhisper 14.7.1).
- High confidence + consistent evidence → automatic route,
  `routing_verified=false`; low confidence / zh-ambiguity /
  classifier-unavailable → `needs_review`. Script ratio is weak
  evidence only; never decisive for Cantonese-vs-Mandarin.
- Heuristic auto-route gate (classifier invalid/unavailable ONLY):
  conservative, configurable, multiple independent conditions (family
  verdict, unambiguous zh verdict, CJK ratio, marker minimum, dominance
  ratio, opposing-score ceiling, non-silent coverage) must ALL hold for
  exactly one enabled Chinese family; Cantonese and Mandarin have
  independent thresholds and kill switches; no European gate. Gate
  scores are heuristic evidence, NOT calibrated probabilities.
  `ready_to_transcribe` from the gate still passes through
  `_apply_outcome`, so `routing.auto_transcribe=false` ⇒ Needs Review.
  Reason codes `auto_confident_heuristic_classifier_invalid` /
  `auto_confident_heuristic_classifier_unavailable`; `routing_verified`
  stays false for audit; gate config is fingerprinted into evidence.
- Classifier requests are a finite state machine (max: one structured
  request; one plain request only after an explicit HTTP 400/422
  response_format/json_schema capability rejection; one repair request
  only after an HTTP-successful schema-invalid response). No loops.
  Bounded stable validation categories only in evidence — never raw
  model output, prompts, or response bodies.
- Never claim routing accuracy without evaluation against human
  confirmations.

## Migrations and DB constraints

- Migrations `0001`–`0008` define the current schema (0007 is the
  multilingual-summary migration: `Summary.output_language`,
  `SummaryVariantState`, transcript language-verification fields;
  intentionally irreversible — repair it in place, never add an 0008
  on top of an unapproved 0007; 0008 is the approved search-index
  migration: `SearchDocument` registry + `workflow_search_fts` FTS5
  trigram table, fully reversible with a separate-connection FTS5
  capability probe, under an approved plan). Add NEW migrations, never
  edit existing/applied ones. Enforce invariants with DB constraints
  (partial uniques, check constraints), not just application logic.
  Run `makemigrations --check` in verification. 0007's data migration
  validates the pre-migration invariant first (corrupted legacy data
  fails clearly and atomically — never a silent winner selection) and
  must be covered by genuine `MigrationExecutor` tests on isolated
  databases, including a real reverse attempt.
- Migration readiness is enforced, never assumed: `brain doctor`
  carries a `Database migrations` check (read-only via Django's
  `MigrationExecutor`; see `brainlib/migrations.py`), and every ORM
  CLI command plus `brain serve` runs a shared schema preflight
  BEFORE any lock/recovery/ORM/file/network work. Pending migrations
  mean exit 1 with a concise actionable message naming
  `uv run python src/manage.py migrate` — never a traceback, never
  automatic migration.

## Testing and verification

- Tests: `UV_CACHE_DIR=/private/tmp/transcript-workflow-uv-cache uv run pytest`
  (no real MacWhisper, oMLX, network, ffmpeg, or user audio; mocks that
  raise for "must not happen" operations; fixtures in
  `tests/fixtures/macwhisper/`).
- Standard verification:
  ```
  UV_CACHE_DIR=/private/tmp/transcript-workflow-uv-cache uv run pytest
  UV_CACHE_DIR=/private/tmp/transcript-workflow-uv-cache uv run python src/manage.py check
  UV_CACHE_DIR=/private/tmp/transcript-workflow-uv-cache uv run python src/manage.py makemigrations --check
  git diff --check && git status --short
  ```
- CLI commands support `--json`; errors go to stderr with documented
  exit codes (0 ok, 1 config/setup error, 2 usage, 3 lock busy).

## Prohibited actions

- Do not modify `config/config.yaml` (prove with SHA-256 when relevant).
- Do not process, move, or delete real files in `data/inbox`.
- Do not expose API keys or transcript contents in logs, errors, or
  exception messages.
- Do not alter the global MacWhisper model selection.
- Do not commit unless the user explicitly asks.
- Do not add frameworks beyond the approved set (no LangChain, Celery,
  Redis, Docker, SPAs, vector DBs).

## Scope boundaries

- **Step 3** (complete): structured summaries rendered deterministically
  as Markdown/plain text, configurable multi-select tags from YAML, and
  bounded whole-transcript chunking/hierarchical reduction. The web UI
  remains the minimal status page.
- **Step 4**: full web interface, review queue, transcript/summary
  views, tag editing, manual routing controls.
- **Step 5**: FTS5 keyword search, local embeddings, semantic/hybrid
  search, Ask-with-citations. **5A.2 (delivered)**: persistent
  `SearchDocument` registry + FTS5 trigram table, reversible migration
  0008, atomic `search-index rebuild`, read-only `search-index status`.
  **5A.3 (not implemented)**: incremental synchronization from the
  authoritative source after pipeline changes. **5A.4+ (not
  implemented)**: query parsing, ranking, highlighting, `brain search`,
  web search — do not implement them as part of index maintenance.
- **Step 6**: user-initiated topic splitting, section-level
  summaries/tags, retention cleanup (deletion only after successful
  processing + retention delay), launchd scheduling.
- Do not implement features from a later step, and do not claim
  accuracy or completion without executable verification.
