# AGENTS.md — durable repository instructions

Read this before any task. It contains the rules that hold across all
development phases of this project, not per-task status (see
`docs/project-status.md` for the current implementation handoff).

## Project purpose and principles

Brain is a **local-first, privacy-preserving transcript workflow** for
personal WAV recordings: discover → route language → transcribe via
MacWhisper (`mw` CLI) → (later) summarize/tag via a local oMLX
OpenAI-compatible endpoint → (later) search, summarize, split.

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
    routing, transcription, audiosamples, statemachine, pipeline,
    pipeline_lock), `views.py` (status page + `/health/`),
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
  (`ingest`, `route`, `transcribe`, `run`, `retry`) must hold the
  exclusive `flock` at `data/temp/locks/pipeline.lock`
  (`workflow/services/pipeline_lock.py`); second process exits with
  code 3. Read-only commands (`status`, `review`, `transcripts`,
  `doctor`, `serve`) never lock.
- Run `recover_interruptions()` while holding the lock before new work
  in mutating commands; it is idempotent.
- DB constraints are the second concurrency layer: at most one
  unfinished `ProcessingAttempt` per (recording, stage), one active
  `Transcript` and one active `RoutingDecision` per recording.

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
  recording's WAV keeps it `transcribed` + `audio_status=missing`
  with all history intact.

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

## Source-file and inbox safety

- Only ever READ user audio. Never move, rename, delete, truncate, or
  transcribe-with-side-effects files in `data/inbox`.
- Scanning and processing are restricted to the configured inbox;
  symlinks resolving outside it are never followed. Sources outside
  the current inbox are parked (`outside_current_inbox`) without file
  access.
- All temporary artifacts (routing samples, lock files) live under
  `data/temp` and are cleaned in `finally`.
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
- Never claim routing accuracy without evaluation against human
  confirmations.

## Migrations and DB constraints

- `0001`/`0002` are applied; add NEW migrations, never edit applied
  ones. Enforce invariants with DB constraints (partial uniques),
  not just application logic. Run `makemigrations --check` in
  verification.

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

- **Step 3** (next): structured JSON summaries rendered deterministically
  as Markdown/plain text, configurable multi-select tags from YAML,
  bounded chunking for long transcripts. No web UI beyond the existing
  status page.
- **Step 4**: full web interface, review queue, transcript/summary
  views, tag editing, manual routing controls.
- **Step 5**: FTS5 keyword search, local embeddings, semantic/hybrid
  search, Ask-with-citations.
- **Step 6**: user-initiated topic splitting, section-level
  summaries/tags, retention cleanup (deletion only after successful
  processing + retention delay), launchd scheduling.
- Do not implement features from a later step, and do not claim
  accuracy or completion without executable verification.
