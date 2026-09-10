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
    search_sync, search_query, search_web, semantic_query,
    search_fusion, library_metadata,
    variant_state, variant_view, web_actions, languages, langresolve,
    llm, tempcleanup, review), a `views/` package
    (`recordings.py`, `actions.py`, `exports.py`, `review.py`,
    `tags.py`, plus package-entry `home`/`health`/error views),
    `query.py` (Library list/annotations), `forms.py`, `middleware.py`,
    `context_processors.py`, `sqlite_unicode.py`, `templatetags/`,
    `migrations/`, and the Step-4/5A web templates under
    `src/templates/workflow/`. The full web UI (Step 4) plus the
    Step 5A.1 Library, 5A.4.2 keyword search and the Step 5C
    semantic/hybrid search (POST-only endpoint) are delivered.
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
  `retry`, `search-index rebuild`, `embedding-index rebuild`,
  `embedding-index repair`) must hold the
  exclusive `flock` at `data/temp/locks/pipeline.lock`
  (`workflow/services/pipeline_lock.py`); second process exits with
  code 3. Read-only commands (`status`, `review`, `transcripts`,
  `summaries`, `summary`, `tags`, `search-index status`,
  `embedding-index status`, `doctor`,
  `serve`) never lock.
- Run `recover_interruptions()` while holding the lock before new work
  in mutating commands; it is idempotent.
- DB constraints are the second concurrency layer: at most one
  unfinished `ProcessingAttempt` per (recording, stage), one active
  `Transcript` and one active `RoutingDecision` per recording, and one
  active `Summary` per (transcript, section, output_language) scope.

## Search index synchronization (Step 5A.3)

- `workflow/services/search_sync.py` is the ONLY incremental index
  writer: mutating services call `schedule_recording_sync([ids])`
  INSIDE their transaction and the work runs via
  `transaction.on_commit` — never on GET (web GETs stay strictly
  read-only) and never inside `reconcile_recording` (no recursion).
  The reconciler does not acquire the pipeline lock itself; callbacks
  triggered by pipeline/web actions normally run while the caller still
  holds that lock, while web tag edits run without it.
  `reconcile_recording` is the single per-recording writer: it
  recomputes the expected documents with the SHARED `search_index`
  builders (never a forked mapping), derives truth from the database,
  is idempotent (zero DML when converged) and reconciles registry + FTS
  in ONE transaction.
- Skipping requires FULL-field equality of every registry field AND
  the exact FTS text; `content_hash` alone is never trusted. FTS row
  states are explicit: correct ⇒ skip, text differs ⇒ UPDATE the
  existing rowid, FTS row missing ⇒ INSERT with the existing registry
  pk (never UPDATE alone), registry row missing ⇒ create + INSERT.
- An index failure NEVER fails or rolls back the authoritative
  operation. Reconciles run independently per recording; the ONLY log
  is one fixed aggregate warning per callback carrying the failure
  COUNT and nothing else (no ids, exception text, paths, SQL or
  indexed content). `search-index status` remains the detection
  mechanism and `rebuild` the authoritative repair; FTS rows orphaned
  by registry deletion/CASCADE are never removable per recording.
- Hooks fire only after COMMIT (a rolled-back transaction schedules
  nothing). Sync failures are nonfatal and non-automatic: the index
  stays detectably stale and converges via the next successful sync
  or a rebuild — never add automatic retries or background daemons.
- Step 5B.4 embedding synchronization rides the SAME post-commit
  callback (see the 5B.4 bullet below): a per-recording pre-reconcile
  key snapshot plus `embedding_sync.sync_recording_embeddings` run only
  after a successful search reconcile, with its own separate fixed
  aggregate warning (`embedding_index_sync_failed`); the search
  contract above is unchanged.

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

- Migrations `0001`–`0009` define the current schema (0007 is the
  multilingual-summary migration: `Summary.output_language`,
  `SummaryVariantState`, transcript language-verification fields;
  intentionally irreversible — repair it in place, never add an 0008
  on top of an unapproved 0007; 0008 is the approved search-index
  migration: `SearchDocument` registry + `workflow_search_fts` FTS5
  trigram table, fully reversible with a separate-connection FTS5
  capability probe, under an approved plan; 0009 is the approved
  Step-5B.2 embedding-storage migration: `EmbeddingGeneration` +
  `EmbeddingDocument` (`workflow_embedding_generation` /
  `workflow_embedding_document`), SCHEMA-ONLY `CreateModel` operations,
  fully reversible, with no `RunPython`, no backfill, no network or
  embedding-client use, and no change to `SearchDocument`/source
  tables). Add NEW migrations, never
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
  **5A.3 (delivered)**: incremental per-recording synchronization from
  the authoritative source after committing changes
  (`workflow/services/search_sync.py`). **5A.4.1 (delivered)**:
  read-only keyword query engine `workflow/services/search_query.py`
  + `brain search` — literal plain-text terms (AND), quoted-phrase
  FTS matching with a Unicode-folded LIKE fallback for 1–2-codepoint
  terms, deterministic rebuild-stable bounded selection (document-key
  order, global + per-recording candidate bounds), per-Recording
  dedup, plain-text snippets with offset ranges, and a health split:
  the CLI runs the full `search-index status` sweep EXACTLY ONCE as a
  hard gate (missing/broken/stale ⇒ exit 1, never rebuild), while the
  engine itself only checks structural queryability. **5A.4.2a
  (delivered)**: Library keyword web search
  (`workflow/services/search_web.py` + the enabled top-bar field on
  `/recordings/`) — the FULL health sweep runs EXACTLY once per
  submitted search GET (NO health cache is permitted); filters scope
  the ENGINE candidate set via a `scope` Recording-QuerySet parameter
  compiled inside `search_query.py` into the innermost WHERE (before
  all window functions, so bounds/truncation/more-match counts are
  scope-honest); search sorting defaults/falls back to `relevance`
  (engine order, never a DB ORDER BY) through a structured
  `sort_error` channel that keeps valid filters; invalid-query and
  index-failure states clear the echoed query entirely. **5A.4.2b
  (delivered — completing Step 5A.4.2)**: snippets render through
  service-built plain-text fragments marked with semantic `<mark>`
  under one deterministic malformed-range policy (exact plain `str`,
  template autoescaping only — `mark_safe`/`SafeString`/generated HTML
  are forbidden here); a segment provenance chip links to the
  active-transcript page carrying its ordinal
  (`?page=ordinal//per_page+1#segment-<ordinal>`, 0-based ordinals,
  `web.transcript_segments_per_page`) ONLY after ONE bounded batch
  SELECT proves the indexed transcript is active AND owned by the SAME
  Recording — any doubt keeps the plain non-link chip; Card/Table
  parity through the `_search_snippet.html`/`_search_provenance.html`
  partials; external CSS only, CSP unchanged, search GETs stay
  strictly read-only. No further 5A sub-steps exist.
- **Pre-5B stability patch (delivered)**: the baseline-reproducible
  concurrent web-tag SQLite race in
  `TestConvergence::test_unlocked_tag_service_race_converges` is fixed
  by a LOCAL bounded retry in `workflow/services/tags.py` around ALL
  THREE unlocked web tag mutations (`add_manual_tag`,
  `confirm_suggestion`, `remove_tag`). The retry decorator lives
  OUTSIDE each wrapped function's `transaction.atomic` boundary, so
  every attempt runs in a fresh transaction (Django rolls the failed
  attempt back before the retry re-invokes). Retry applies ONLY to a
  Django `OperationalError` whose DIRECT cause is a
  `sqlite3.OperationalError` carrying an integer `sqlite_errorcode`
  whose primary error code — extended codes included, via `& 0xFF` —
  is SQLITE_BUSY or SQLITE_LOCKED; at most 3 total attempts with a
  FIXED 0.02-second delay between attempts; unrelated failures and
  exhausted contention re-raise immediately. Callback contract
  unchanged: `schedule_recording_sync` stays INSIDE the transaction —
  a rolled-back attempt discards any registered callback, and a
  successful commit of a mutation that schedules sync fires one
  callback; `confirm_suggestion`'s no-sync behavior (origin-only change,
  does not affect indexed content) is unchanged. Search-sync failures
  keep their separate nonfatal no-auto-retry policy — retries never
  cover post-commit sync work. Verification:
  16 deterministic focused tests plus independent repeatable runs
  (focused 50 passed; the real race test 75 consecutive invocations;
  full suite 1404 passed; no migration). Repeatable verification, not a
  proof against every possible SQLite contention.
- **Step 5B.1 — Embedding client (delivered)**: `workflow/services/embedding_client.py`
  is the ONLY /embeddings client (never refactored into `llm.py`).
  Public API: frozen `EmbeddingBatch(text, embedding)` and
  `embed_texts(config, texts, *, timeout=None, transport=None)`. One
  fixed production request `encoding_format='base64'`; NO runtime
  fallback, NO retries, exactly ONE HTTP request per call, and the
  client emits no logs. Endpoint = `base_url.rstrip('/') + '/embeddings'`,
  validated BEFORE transport (http/https only; no credentials/query/
  fragment; hostname exactly `localhost` or a literal loopback IP —
  everything else is `endpoint_not_local`). Model must be nonblank.
  Input: list/tuple of exact `str` only (never a bare str, never empty,
  never non-str elements, never empty strings; no coercion or
  truncation). Request body is deterministic UTF-8 JSON (sort_keys),
  bounded by hard caps: request 1 MiB, response 2 MiB, `MAX_DIMENSION`
  16384, hard max batch 128 (configured `batch_size` also enforced;
  config caps it at 128). Key is read at call time via
  `config.api_key_for(...)`; `Authorization` header only when set.
  Response validation: top-level `data` list, `model` str EXACTLY equal
  to the configured model, exact cardinality, item `index` exact int
  (never bool) forming a complete unique 0..n-1 set, item `embedding`
  exact str; strict base64 (padding never permissively normalized),
  decoded bytes nonempty and divisible by 4, little-endian IEEE-754
  float32 via `struct`, every value finite, consistent nonzero
  dimensions, dimension capped; vectors reordered by index into input
  order. Extra fields ignored. Errors are sanitized (never input,
  vector, body, headers, secret, SQL or path) with a stable taxonomy:
  `model_not_configured`, `endpoint_not_local`, `invalid_input`,
  `batch_too_large`, `request_too_large`, `endpoint_unavailable`,
  `timeout`, `http_error` (safe status only), `response_too_large`,
  and fine `EmbeddingInvalid.code` values `malformed_http_json`,
  `invalid_envelope`, `invalid_vector`, `dimension_mismatch`,
  `invalid_encoding`. Embedding config (`timeout_seconds` default 120,
  cap 600; `batch_size` default 32, cap 128) is validated at config
  parsing with positive-int checks that reject booleans; there is NO
  `max_input_characters` config. `brain doctor` checks the embedding
  model against ITS OWN `(base_url, api_key_env)` /v1/models result —
  exact equal pairs share one fetch, distinct pairs fetch independently
  (`check_models_for_config`); doctor NEVER calls `/embeddings` and no
  embedding probe command exists.
- **Step 5B.2 — Embedding storage foundation (delivered)**: versioned
  embedding storage with a SEPARATE generation-based architecture,
  migration 0009 (schema-only, depends on 0008, fully reversible, no
  backfill/network/embedding-client use). `EmbeddingGeneration`
  (db_table `workflow_embedding_generation`; BigAutoField id) records
  `model` (exact configured/canonical model string), `dimensions`
  (1..16384), `embedding_version` (the FULL embedding implementation
  contract — deterministic text preparation plus vector mapping, to be
  versioned by Step 5B.3 — never merely the codec/client) and, on a
  SEPARATE axis, `source_index_version` (binding the `SearchDocument`
  index contract); no `EMBEDDING_VERSION` constant exists in 5B.2
  because the production mapping/version is not implemented until 5B.3.
  `EmbeddingDocument` (db_table
  `workflow_embedding_document`) stores per-vector rows whose
  `document_key` is COPIED independently from
  `SearchDocument.document_key` — NEVER an FK or pk dependency on
  `SearchDocument` — with `source_content_hash` provenance and the raw
  `vector_blob`. Model/dimension/version changes create a NEW
  generation; the prior `active` generation remains usable until a later
  atomic promotion supersedes it. Duplicate generations with an
  identical (model, dimensions, embedding_version, source_index_version)
  identity are ALLOWED (no unique identity tuple) so a same-contract
  rebuild can coexist with the old active generation. States are exactly
  `building`/`active`/`superseded`/`failed`; at most one `active`
  (partial unique `uniq_active_embedding_generation`); a lifecycle-shape
  CHECK doubles as the explicit state allowlist (building has all
  lifecycle timestamps null; active = completed_at+activated_at set with
  superseded_at/failed_at null; superseded adds superseded_at with
  failed_at null; failed = failed_at set and the other three null; a
  FAILED building generation may retain bounded partial
  `EmbeddingDocument` rows for diagnosis/cleanup/resumption, but they
  are unusable because the generation is never active);
  chronology CHECKs `completed_at <= activated_at` and
  `activated_at <= superseded_at`; dimensions bounds and non-empty
  identity fields are DB CHECKs. `EmbeddingDocument` is unique per
  (generation, document_key) — the same key MAY appear in many
  generations — key/hash/vector must be non-empty, and NO redundant
  `dimensions` column exists (the generation row is the cross-table
  truth; SQLite CHECKs cannot reference the generation row, so
  codec/writers/status validate the equality; no model `save` override
  fakes DB enforcement). Vectors are portable raw little-endian
  IEEE-754 float32 BLOBs. `workflow/services/vector_codec.py`
  (pure stdlib) is the codec and the ONE runtime home of
  `MAX_DIMENSION = 16384`; `embedding_client.MAX_DIMENSION` remains
  available by import from it. Public API: `encode_vector(values, *,
  dimensions) -> bytes`, `decode_vector(blob, *, dimensions) ->
  tuple[float, ...]`, `validate_vector_blob(blob, *, dimensions) ->
  None`; dimensions are exact int 1..MAX (bool/0/negative/over-cap
  rejected); encode accepts only list/tuple of exact int/float (bool
  rejected), cardinality exactly `dimensions`, finite before packing,
  overflow/struct failures sanitized, packed result verified finite;
  the raw encoding is exactly `struct.pack(f"<{dimensions}f", ...)` —
  no header/pickle/JSON; decode accepts exact bytes of exactly
  `dimensions * 4` bytes and rejects non-finite values; errors are
  sanitized `VectorCodecError(ValueError)` with stable codes
  `invalid_dimension`/`invalid_values`/`invalid_blob`, never containing
  values, blob content, or paths. At the 5B.2 delivery, 5B.3 (bounded
  status/rebuild/repair) and 5B.4 (incremental synchronization) were
  NOT implemented and no `search_index`/`search_sync`/CLI/web changes
  were made (5B.3 has since been delivered — see the 5B.3 bullet
  below; Step 5C has since been delivered too — see the Step 5C
  bullet below). Verification (historical 5B.2 state): the full suite then
  passed — **1632 collected and 1632 passed** (the Step 5B.1
  full-suite state was 1499; the 5B.2 delta is the 133 new
  5B.2 tests below), with the only warning the known `audioop`
  deprecation; `manage.py check` and `makemigrations --check` are
  clean. Supporting focused detail: 72 pure codec tests
  (`tests/test_vector_codec.py`), 52 runtime model-constraint tests
  (`tests/test_embedding_models.py`), 9 genuine MigrationExecutor
  tests (`tests/test_embedding_migration.py`) plus the updated
  migration-readiness and unchanged embedding-client tests (the
  5B.2 focused set: 240 passed). No commit or real-database migration
  is claimed.
- **Step 5B.3 — Embedding index status/rebuild/repair (delivered)**: all
  three bounded operations live in `workflow/services/embedding_index.py`
  (name preferred; no new schema/migration/config keys, no 5B.4
  synchronization, no `search_sync` hooks, no semantic retrieval, no
  web/GET changes). `EMBEDDING_VERSION='1'` is the FULL production
  embedding mapping contract — deterministic text preparation
  (`prepare_document_text`, ONE pure helper consuming a SearchDocument
  row) plus the vector mapping (`vector_codec.encode_vector`) — on a
  SEPARATE axis from `search_index.INDEX_VERSION`. The v1 text format is
  documented exactly (version marker line + four length-prefixed labelled
  fields `doc_type`/`title_text`/`body_text`/`aux_text` in fixed order,
  empty fields included, never truncated/coerced); ANY change to the
  format/field order/vector encoding MUST bump `EMBEDDING_VERSION`.
  SearchDocument rows are the immediate source (never reconstructed);
  their `content_hash`/`index_version` provenance is copied into
  `EmbeddingDocument.source_content_hash`/`source_index_version`.
  `prepare_document_text` validates all four fields are EXACT `str`
  (malformed rows raise one fixed sanitized error; a hostile value's
  arbitrary `__str__` is never invoked). The configured embedding model
  is the EXACT configured/canonical string (5B.1 sends it verbatim):
  blankness is tested with `.strip()` only and the
  stored/expected/comparison identity is NEVER stripped or
  canonicalized. Errors are sanitized `EmbeddingIndexError` (subclasses
  `ConfigError`, CLI exit 1 without traceback; fixed messages only,
  guidance names commands only); exception `.code` interpolation is
  ALLOWLISTED (known stable 5B.1 embedding/codec codes are surfaced,
  unknown or hostile custom-subclass codes map to a fixed generic
  category — never echoed verbatim); no service logs.
- `build_embedding_status_report(config, *, using='default')` is
  strictly read-only (SELECT/PRAGMA only; no lock/network/repair/write).
  It runs the COMPLETE `search_index.build_status_report(using=...)`
  EXACTLY once and surfaces an unhealthy source index as the
  `source_index_unhealthy` category (never rebuilt). Stable categories
  (never renamed): `schema_missing`, `source_index_unhealthy`,
  `model_not_configured`, `no_active_generation`, `model_mismatch`,
  `embedding_version_mismatch`, `source_index_version_mismatch`,
  `missing_document`, `stale_content`, `orphan_document`,
  `invalid_vector`. Key samples are capped at 20 per category with exact
  `keys_truncated` omitted counts; streams/keyset-pages in fixed-size
  batches (no N+1, no unbounded accumulation). Malicious oversized
  vector BLOBs are classified by SQLite `length()` FIRST; only exact
  `dimensions*4`-byte blobs are fetched/decoded, in bounded chunks.
  A structurally-valid but zero-norm (all-zero finite float32) vector is
  a usability-layer `invalid_vector` (exact component-wise zero test —
  no tolerance, never a threshold; a tiny nonzero subnormal stays
  usable) — fail closed, never skipped or approximated.
  Active-only integrity determines usability: failed/building/superseded
  generations are counted but their documents never make a healthy
  active generation unhealthy. There is NO configured dimensions value:
  status cannot detect a same-name server dimension change (active
  dimensions are validated by DB bounds and vector blobs; rebuild/repair
  endpoint results detect dimension changes). Healthy iff source index
  healthy, model configured, exactly one compatible active generation
  and zero missing/stale/orphan/invalid active documents. A blank model
  is the stable `model_not_configured` category with no network. Any
  structural/query/blob failure inside the status internals raises a
  fixed sanitized `EmbeddingIndexError` (never misleading healthy/count
  output, never a traceback); a genuinely missing embedding schema
  keeps the normal `schema_missing` report. Schema introspection is
  failure-honest: genuine table ABSENCE returns False, while an
  introspection/query FAILURE propagates to the status boundary and
  becomes `_STATUS_ERROR` — never misreported as `schema_missing`.
- `rebuild_embedding_index(config, *, using='default', embedder=embed_texts)`:
  the CLI holds the pipeline lock; the service NEVER acquires it AND
  refuses to run while already inside a caller SQLite transaction (fixed
  precondition checked BEFORE any embedder invocation; HTTP is never
  called with `connection.in_atomic_block` True). The configured model
  is stored/returned EXACTLY (blankness via `.strip()` only);
  `embedding.batch_size` must be a positive integer ≤ 128 (a manually
  constructed over-cap config is a fixed sanitized error, zero network).
  Preflights full source search-index health EXACTLY once before any
  network/write (unhealthy ⇒ stable error guiding `brain search-index
  status`/`rebuild`, zero embedding mutation/network). Streams ALL
  current SearchDocuments deterministically by `document_key` in batches
  ≤ `config.embedding.batch_size` (no DB cursor or
  transaction held over HTTP). For a nonempty source the FIRST real
  batch discovers the returned dimension and only THEN a `building`
  generation is created in a short transaction and that first batch
  persisted (no extra dimension probe); an EMPTY source uses the one
  fixed non-sensitive `SYNTHETIC_DIMENSION_PROBE` and creates an empty
  building generation (documented/tested). One HTTP request per batch,
  no retries; exact client cardinality/text pairing and one consistent
  returned dimension across every batch are validated; a zero-norm
  returned vector is rejected with one fixed sanitized error BEFORE any
  write (never persisted, never promoted); vectors encoded
  with the codec. Each persisted batch is ONE bounded short transaction
  that rechecks every source key/content_hash before inserting
  (changed/missing source aborts/fails the generation — never false
  provenance) and retains bounded partial rows on later failure.
  A deterministic rolling sha256 snapshot over the exact ordered
  `(document_key, content_hash)` framing is accumulated while
  processing; the frame is ONE documented length-prefixed UTF-8
  encoding (`S<len(key)>:<key>S<len(hash)>:<hash>`, byte lengths, no
  raw delimiters, both values exact `str` validated) shared
  byte-identically by the source snapshot and the generation-integrity
  snapshot, so boundary-shifted/unicode/delimiter values cannot
  collide. Before promotion: a fresh complete source health sweep
  PLUS complete current SearchDocument key/hash snapshot equality PLUS
  target generation integrity (exact key/hash set and usable vectors —
  dimensions, finiteness and nonzero norm) in bounded reads. The final-validation →
  promotion race (including unlocked web-tag/SearchDocument commits) is
  closed with `PRAGMA data_version`: capture before final validation,
  and in the SHORT promotion transaction acquire the SQLite write
  reservation via a harmless no-op update of the building generation,
  re-read `data_version` and require equality — any other connection's
  commit fails conservatively (no network and no full scan in that
  transaction; same-connection mutation cannot race this single-threaded
  operation). Under that same transaction the CURRENT active generation
  id is re-read and must EXACTLY equal the captured `prior_active_id`
  (including None); the prior active's supersede UPDATE must affect
  exactly one row. Promotion is ONE short transaction: supersede the old
  active FIRST (`state=superseded`, `superseded_at=now`), then activate
  the target (`completed_at=activated_at=now`); any failure rolls the
  whole promotion back with the old active unchanged; the DB partial
  unique is the final guard. HEALTH IS ESTABLISHED BY THE PRE-PROMOTION
  VALIDATION AND THE WRITE-LOCK GUARD AT THE PROMOTION COMMIT — after
  promotion succeeds the verified result is returned directly and there
  are NO post-promotion failure points (no post-promotion status sweep;
  tests prove actual health with an independent status report). Any
  failure AFTER generation creation
  marks it `failed` (best-effort short transaction, `failed_at`,
  bounded partial docs kept) and NEVER touches the old active; a
  pre-generation first-call failure records nothing (dimensions
  unknown); a swallowed mark-failed failure leaves a detectable
  `building` generation without masking the original sanitized failure
  (status reports history, only active usability matters); SIGKILL may
  also leave `building`. DB failures are wrapped in fixed sanitized
  messages; ALL unexpected `Exception` failures (not
  KeyboardInterrupt/SystemExit) are converted to one fixed sanitized
  error — the source preflight, schema validation and (for repair) the
  active-compatibility queries run INSIDE the same public sanitizing
  boundary, so unexpected DB/search-helper exceptions there can never
  leak raw to the CLI. Returns safe counts only (generation
  id/model/dimensions, documents, batches, prior active id, verified
  healthy).
- `repair_embedding_index(config, *, using='default', embedder=embed_texts)`:
  refuses to run while already inside a caller SQLite transaction
  (fixed precondition, zero embedder calls when rejected); preflights
  source search-index health before any mutation/network and
  REQUIRES exactly the current active generation with an EXACT match of
  the configured model, `EMBEDDING_VERSION` and `INDEX_VERSION` —
  otherwise a stable rebuild-required error (a
  building/failed/superseded/incompatible generation is never chosen);
  no remote dimension probe when no embedding work is needed. Reconciles
  the active generation against the current SearchDocuments via the same
  bounded two-stream merge: missing/stale/invalid current keys (invalid
  now includes stored zero-norm vectors) are
  re-embedded at most once (HTTP outside transactions; returned
  dimensions must equal the active generation's or the batch fails
  requiring rebuild BEFORE any write, and a zero-norm returned vector is
  rejected with one fixed sanitized error BEFORE any write — it never
  replaces an existing vector) and active-generation orphans are
  deleted in bounded pages with an in-transaction absence recheck. Each
  short write transaction re-reads source key/hash AND active-generation
  compatibility before the upsert; changed rows remain unresolved and
  never receive false provenance. Partial batch progress is durable;
  failures do NOT mark the active generation failed and do not roll back
  earlier batches. Ends with a fresh status; success only when
  converged/healthy; unresolved/failure raises a sanitized error and the
  user inspects `brain embedding-index status` (no automatic retry).
  No network when only deleting orphans or already healthy. ALL
  unexpected `Exception` failures are converted to one fixed sanitized
  error (never a raw traceback).
- **CLI**: `brain embedding-index status|rebuild|repair [--json]`,
  symmetric with `search-index`. status: schema preflight, read-only, no
  pipeline lock/recovery; exit 0 healthy, 1 unhealthy. rebuild/repair go
  through the shared `_pipeline_command` (schema preflight BEFORE
  lock/recovery, exclusive pipeline lock, `recover_interruptions`, exit
  3 busy, errors exit 1); the CLI resolves `embed_texts` from the module
  at call time so tests mock it without touching the client. Human and
  JSON output are sanitized; error guidance names commands only; no
  probe command.
- **Verification (historical 5B.3 state, independently confirmed)**: the
  full suite passes —
  **1746 collected and 1746 passed** with the only warning the known
  `audioop` deprecation; `manage.py check` and `makemigrations --check`
  (no migration) are clean. Supporting focused detail: the 5B.3 focused
  set (`tests/test_embedding_index_service.py` 86 +
  `tests/test_embedding_index_cli.py` 23 = **109 passed**) plus the
  search-index/client/codec/embedding-model/embedding-migration/CLI/
  migration-readiness/search-sync regressions (**455 passed**). The
  historical full-suite states are 1632 (Step 5B.2), 1499 (Step 5B.1)
  and 1404 (pre-5B stability patch). No commit, real-database
  migration, or real embedding network call is claimed; all tests are
  mocked/network-free.
- **Step 5B — Local Embeddings Foundation**: **5B.1 (delivered)** the
  bounded local /embeddings client; **5B.2 (delivered)** versioned
  embedding storage (generations + documents + vector codec + migration
  0009); **5B.3 (delivered)** bounded embedding-index
  status/rebuild/repair (above); **5B.4 (delivered — see the dedicated
  bullet below)** incremental embedding synchronization. **Step 5C
  semantic/hybrid retrieval is now delivered too** (see the Step 5C
  bullet below) and **Step 5D Ask with Citations is delivered too**
  (see the Step 5D bullet below); the next planned work is **Step 6**.
- **Step 5B.4 — Incremental embedding synchronization (delivered)**:
  `workflow/services/embedding_sync.py` is the ONLY incremental
  `EmbeddingDocument` writer; `embedding_index.py` keeps the explicit
  status/rebuild/repair. `search_sync.schedule_recording_sync` remains
  the sole authoritative post-commit trigger and its per-recording
  callback now: (1) BEFORE the search reconciliation captures the
  recording's current SearchDocument keys (keys only) into a
  connection-local SQLite TEMP table (`brain_embedding_removed_keys`,
  `INSERT...SELECT`, bounded application memory, dropped in `finally`;
  a capture failure NEVER stops the search reconciliation — it counts an
  embedding failure and leaves stale state detectable); (2) runs
  `reconcile_recording`; (3) ONLY on search success invokes
  `embedding_sync.sync_recording_embeddings` (search failure suppresses
  the unsafe embedding step and stays separately logged). The worker
  reuses the EXACT 5B.3 mapping contract (`prepare_document_text`,
  `EMBEDDING_VERSION`, `embedding_client.embed_texts`,
  `vector_codec.encode_vector`; SearchDocument is the immediate source,
  never reconstructed) and deletes removed-key vectors FIRST
  (network-free, bounded pages, short transactions rechecking source
  absence and active compatibility), then streams the recording's
  current SearchDocuments in deterministic `document_key` keyset pages
  no larger than the validated `embedding.batch_size` (hard max 128),
  classifies correct/missing/stale/invalid per page with the SHARED
  5B.3 length-first vector validation (invalid includes zero-norm
  vectors), embeds ONLY missing/stale/invalid
  rows (one HTTP request per non-empty batch, outside all DB
  transactions) and upserts through the shared short-transaction batch
  writer that re-reads every source key/hash and the same active
  generation identity — concurrent source change or active promotion
  produces no false provenance/write; a zero-norm endpoint vector is
  rejected with the shared fixed sanitized error before any upsert
  (the existing vector stays). Config is loaded FRESH inside the
  callback via `brainlib.config.load_config` (only when an active
  generation exists); no active generation or an incompatible active
  generation is a normal no-op (zero network/DML/log; status/rebuild is
  the remedy). Failures never escape or alter the authoritative/search
  operation; per-recording independent; the ONLY embedding failure log
  is one fixed aggregate warning per callback
  (`category=embedding_index_sync_failed`, count only). No automatic
  retries, no pipeline lock in the sync, no HTTP while
  `connection.in_atomic_block`; a prior failed deletion can leave an
  unattributable embedding orphan after its SearchDocument is gone —
  status detects it (`orphan_document`) and explicit
  `brain embedding-index repair`/`rebuild` removes it, exactly like
  orphan FTS rows (no global callback sweep, retry, queue, daemon or
  background job). Verification (independently confirmed): full suite
  **1783 collected and 1783
  passed** (the 5B.3 state was 1746; the 5B.4 delta is the 37 tests in
  `tests/test_embedding_index_sync.py`), only the known `audioop`
  warning; `manage.py check`, `makemigrations --check` (no migration)
  and `git diff --check` clean. No commit, real-database migration, or
  real embedding network call is claimed; all tests are mocked/network-free.
- **Step 5C — Semantic and Hybrid Search (delivered)**: the read-only
  engines are `workflow/services/semantic_query.py` (pure query
  validation/`prepare_query_text`, `SEMANTIC_QUERY_VERSION="1"` on a
  SEPARATE axis from the document `EMBEDDING_VERSION`, numerically-safe
  cosine, the exact grouped per-recording top-K primitive
  `select_semantic_winners`, and the scoped `semantic_search` engine
  plus the reusable `semantic_rank`/`SemanticSnapshot`/`embed_query_vector`
  snapshot entry points) and `workflow/services/search_fusion.py`
  (pure RRF + the one-sweep `hybrid_search`), with
  `search_query.CompiledScope`/`compile_scope` as the shared immutable
  compiled-scope value. Strictly read-only: SELECT/PRAGMA plus EXACTLY
  ONE localhost embedding request, no lock/rebuild/repair/sync, no
  logs, no retries, no keyword-only fallback.
  - **Deterministic traversal**: the complete active-generation corpus
    is read in deterministic `(recording_id, document_key)` order
    (keyset-paged, exact `.only()` projection, title/body/aux TextFields
    never loaded), in-scope documents are scored, and ONE Recording's
    best document (per-recording comparator: cosine desc, doc-type rank
    summary<recording<segment, document_key, recording id) is finished
    BEFORE its single provenance-only winner enters the global top-K
    heap (≤ 200) — the best-last regression (a Recording's best
    document sorted LAST inside its group decides K membership) is
    proven. Brute-force corpus-linear but bounded-memory: every in-scope
    vector decoded exactly once (shared `embedding_index._classify_active_page`,
    length-first), the heap/results retain metadata only (never K
    vectors), excerpts are a winner-only bounded SUBSTR fetch.
  - **Integrity/concurrency**: exactly ONE complete source health sweep
    (`search_index.build_status_report`) + ONE global active-generation
    integrity traversal + ONE query embedding per search (ZERO for an
    empty scope/corpus), `PRAGMA data_version` guarded before/after, and
    a final complete active-identity re-read; ANY mismatch or ANY global
    integrity defect (missing/stale/orphan/wrong-length/non-finite/zero
    — in-scope or out) fails closed with a fixed sanitized error and no
    partial results; requires exactly one compatible ACTIVE generation
    (exact model/`EMBEDDING_VERSION`/`INDEX_VERSION`). Zero-norm STORED
    vectors fail closed as the role-appropriate `invalid_document_vector`
    in queries and are `invalid_vector` in `embedding-index status`/
    repair; a zero QUERY vector is `invalid_query_vector`.
  - **Hybrid fusion**: exactly ONE source health sweep, ONE integrity
    traversal and ONE query embedding with a SHARED
    `CompiledScope`/`SemanticSnapshot` (the Recording scope QuerySet is
    compiled EXACTLY ONCE via the exact keyword compiler; both
    components consume the SAME immutable value, and the keyword
    component calls `search_recordings(compiled_scope=...)` directly —
    never `preflight_full_health`, no keyword re-gate). Both components
    run at depth 200 (`HYBRID_DEPTH`); fusion is pure RRF (k=60,
    one-based ranks, `1/(60+rank)` over PRESENT components — absence is
    returned-depth, never a corpus nonmatch), deterministic order RRF
    desc, presence count desc, min present rank, max present rank,
    canonical recording id; `truncated` = either component truncated;
    `more_recordings_matched` is exact `len(fused)-final_count` ONLY
    when both component populations are proved complete, else null; no
    weights/raw-score normalization; presentation prefers keyword
    evidence (highlights preserved) with an `evidence` block
    (keyword_rank/semantic_rank/semantic_cosine/rrf_score).
  - **Modes/CLI/web**: `brain search QUERY --mode keyword|semantic|hybrid`
    (default keyword with byte-for-byte parity; semantic/hybrid own the
    one-sweep/one-embed contract; exit 2 usage before health, exit 1
    sanitized, no lock/recovery/write). The Library keyword GET
    (`/recordings/?q=...`) is unchanged and strictly read-only (a forged
    `mode=` on GET is ignored — GETs never embed/network); semantic/
    hybrid web search is POST-only at `/recordings/search/` (GET = 405
    with zero work, CSRF-protected), the query never enters a URL,
    invalid scope filters REJECT (never widened to unscoped), every
    service failure is ONE stable `unavailable` state with the query
    cleared, and navigation (pagination/sort/filter/view) is POST-only
    with hidden server-validated state; filters, provenance, snippets
    and segment jump links are shared with keyword search.
  - **Verification (independently confirmed)**: full suite **2103
    collected and 2103 passed** (the 5B.4 state was 1783; the 5C delta
    is 320 tests — the five new Step 5C test files
    `tests/test_semantic_query.py` (69), `tests/test_semantic_search.py`
    (42), `tests/test_search_fusion.py` (43),
    `tests/test_search_cli_modes.py` (32) and `tests/test_web_search_modes.py`
    (40) = 310 tests, plus 10 additions across
    `tests/test_embedding_index_service.py`, `tests/test_embedding_index_sync.py`
    and `tests/test_migration_readiness.py`), only the known `audioop`
    warning; `manage.py check`, `makemigrations --check` (no migration)
    and `git diff --check` clean. No commit, real-database migration, or
    real embedding network call is claimed; all tests are mocked/network-free.
- **Step 5D — Ask with Citations (delivered)**: read-only Ask whose
  answers cite ONLY actually retrieved evidence. `workflow/services/ask.py`
  is the ONLY Ask orchestrator; the document-level evidence surface
  lives in `workflow/services/semantic_query.py`
  (`retrieve_semantic_evidence` / `select_semantic_evidence`) and reuses
  the EXACT Step 5C contracts — it is deliberately NOT implemented by
  consuming `semantic_search()` (which dedups one winner per Recording
  and permits metadata). Evidence admits only `segment` and `summary`
  SearchDocuments (metadata NEVER evidence), allows several documents
  per Recording, and uses deterministic hardcoded bounds (12 total, 3
  per Recording; no AskConfig/YAML keys). Exactly ONE source health
  sweep, at most/exactly ONE query embedding when an eligible corpus
  exists (ZERO for an empty one), ONE complete global
  active-generation integrity traversal, and the same
  active-generation/version/`PRAGMA data_version` concurrency
  protections as Step 5C. Ask never writes, locks,
  rebuilds/repairs/syncs, persists history or logs content.
  CLI: `brain ask QUESTION [--json]` — read-only schema preflight, no
  pipeline lock/recovery; exit 2 invalid question before health/network,
  exit 1 sanitized operational/index/embedding/LLM failure, exit 0 for
  an answer OR the fixed insufficient-evidence result. Web: `/ask/` —
  GET renders the form with ZERO health/embedding/chat work and no
  writes; POST executes Ask (CSRF; the question never enters a URL; no
  persistence/PRG; PUT/DELETE/PATCH are 405 before any work).
  Chat contract: the LLM base URL is validated at the Ask boundary
  (http/https, no credentials/query/fragment, hostname exactly
  `localhost` or a literal loopback IP → fixed sanitized
  `endpoint_not_local`, zero transport); the prompt treats source text
  as untrusted quoted evidence and requires a strict structured JSON
  object with exactly `answer`/`citations`/`insufficient`; citation ids
  are server-owned `C1..Cn` and only retrieved ids are accepted, with
  declared/inline set equality, no duplicates and no unknown
  citation-looking bracket tokens; a sufficient answer needs at least
  one citation; ONE retry, ONLY for HTTP-successful malformed/schema/
  citation output (endpoint/timeout/HTTP/request/response-size failures
  never retry); model-declared insufficiency uses a fixed
  application-owned message. Evidence is bounded (per-document chars,
  total evidence chars, serialized request chars, max output tokens,
  answer chars, citation count — all hardcoded); per-document
  excerpting and total-budget tail drops are explicitly marked and
  surfaced (`evidence_truncated` + one content-free application note).
  Selected evidence is revalidated AFTER the chat
  (key/content_hash/provenance plus Transcript/Segment or Summary
  ownership/existence); any change is a fixed sanitized
  concurrent-change failure, never an answer. Stable links: transcript
  `/recordings/<recording>/transcript/?v=<transcript-id>&page=<ordinal
  //segments_per_page+1>#segment-<ordinal>` and exact summary version
  `/recordings/<recording>/summaries/<summary-id>/`; the summary route
  converter is `<str:summary_id>` (CharField(36) primary keys), parent
  ownership still enforced. Answers render as plain autoescaped
  fragments plus server-owned citation links — never model HTML or
  model URLs. No history persistence.
- **Step 6**: user-initiated topic splitting, section-level
  summaries/tags, retention cleanup (deletion only after successful
  processing + retention delay), launchd scheduling.
- Do not implement features from a later step, and do not claim
  accuracy or completion without executable verification.
