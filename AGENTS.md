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
    llm, tempcleanup, review, segmentation), a `views/` package
    (`recordings.py`, `actions.py`, `exports.py`, `review.py`,
    `tags.py`, `segmentation.py`, plus package-entry `home`/`health`/
    error views),
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
- Step 6.3 introduced NO new sync mechanism or writer: section-summary
  documents and section-tag aux are documents of the PARENT Recording
  and converge through this same per-recording reconciler (and the 5B.4
  embedding sync on the same callback); the new hooks only schedule the
  existing parent-recording sync from the section summarize/tag/
  segmentation writers (see the Step 6.3 bullet).

## Web actions (direct execution, no confirmation interstitial)

- EVERY mutating web workflow action — manual route, Confirm routing,
  Transcribe, summarize Generate/Retry/Regenerate (recording-level and
  section-level), Retry failed stage, and the segmented-version save —
  executes on the FIRST POST from its origin-page form. No confirmation
  interstitial exists or is rendered anywhere: the
  `action_confirm.html`/`segmentation_confirm.html` templates and
  `ActionConfirmForm` are removed, no payload carries a `confirmed`
  flag (the strict segmentation parser rejects it as an unknown
  field), and there is no second POST. In the `needs_review` web state
  the immediate manual-route form IS the recommended primary action —
  never blocked behind an audit-only "Confirm routing" step (choosing
  the current profile in that same form confirms the routing). A
  transcribed recording with an unverified automatic routing decision
  likewise surfaces NO recommended-action section: one-click Confirm
  routing is audit-only and is never rendered as a recommended action,
  while the routing metadata/history and the manual routing controls
  stay available through the collapsed Routing disclosure (choosing the
  current profile there confirms the routing).
- An active UNVERIFIED automatic `RoutingDecision` on a successfully
  transcribed Recording is audit-only and is NEVER an actionable Review
  category. It does not appear on the web Review page or its category
  totals, is excluded from the global Review badge distinct-recording
  count, is not reported as attention by `brain review` /
  `build_review_report` (the report may retain the empty `unverified`
  key for shape compatibility, but no query/population runs for it),
  and is excluded from Library `review=1` filtering and
  `LibraryItemCard.needs_attention` — all actionable-review surfaces
  agree. `routing_verified` stays `false` and every routing
  metadata/history/detail disclosure remains. Pre-transcription
  `ProcessingStatus.NEEDS_REVIEW` review behavior is unchanged. No
  migration, no existing-row mutation.
- The client-side pending UI is progressive enhancement in
  `src/static/workflow/app.js` (`form[data-action-form]`): the FIRST
  submit is never cancelled (an ordinary native POST navigation),
  repeated submit events on an already-submitted form are blocked, and
  while the request runs the enhancement disables and relabels ONLY
  the submit control, marks the form `aria-busy` and writes the
  optimistic pending message into the form's `[data-action-live]`
  `aria-live` region (the hidden segmentation form binds the visible
  external control/region via `data-action-control` /
  `data-action-live="<action>"`). Summarize and segmentation-save
  pending copy is TEMPLATE-OWNED via
  `data-pending-label`/`data-pending-message` (summarize modes differ);
  the route, confirm-routing, transcribe and retry templates carry no
  `data-pending-*` attributes and use the fixed per-action fallback map.
  Payload-bearing inputs are never
  disabled or mutated; there is no fetch/HTMX, no real progress
  reporting and no polling — the synchronous POST navigation IS the
  wait, and the AUTHORITATIVE result is the action's redirect plus the
  refreshed strictly-read-only GET (flash messages). A bfcache
  "back" (`pageshow` persisted) restores the original control label,
  enabled state, `aria-busy` and live text and re-allows submitting.
  Without JS every action form is a plain POST form; the CSP is
  unchanged. The obsolete confirmation-page machinery (the
  `initConfirmForms` in-flight anchor-disabling and its
  `[data-confirm-exempt]` escape hatch) is gone: navigation anchors are
  never disabled.
- Direct execution does NOT weaken the service-side safety contract:
  POST-only + CSRF; the opaque state fingerprint captured at render
  time travels on the executing POST — every recording-level action
  POST must carry EXACTLY ONE canonical lowercase 64-hex
  `state_fingerprint` digest (the SHA-256 of the deterministic bound-
  state JSON; missing/duplicate/empty/malformed/oversized/uppercase is
  one fixed sanitized friendly 400 BEFORE any lock/recovery/network/
  write) — a stale or duplicate submission with a canonical digest is
  still the safe under-lock no-op with zero DML; the exclusive pipeline
  lock +
  `recover_interruptions()` still guard mutations, eligibility,
  section/layout and source state are revalidated live before the
  write, and results stay versioned (transcript/summary/layout
  history) with prior actives kept on failure. Cheap pre-lock
  eligibility/validation probes reject obviously invalid submissions
  before any lock/network; execution under the lock remains
  authoritative.

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
- Config-owned tag definitions come from YAML `tags.allowed` and are
  synchronized explicitly; removed config-owned definitions are retired,
  never deleted. Custom tag definitions come from the web (see the next
  bullet). Model suggestions are
  versioned provenance. Manual/confirmed effective assignments are
  user-owned and must not be silently removed by regeneration.
- Tag definition provenance is explicit (`Tag.definition_origin`:
  `config` | `custom`, DB CHECK allowlist, migration 0010). Custom tags
  are global reusable definitions created ONLY through
  `workflow/services/tags.py:create_custom_tag_and_assign` (web POST
  only, `definition_origin=custom`, `is_configured=True`); `is_configured`
  stays the availability/retired state. `tags --sync` creates
  config-owned tags, updates/reactivates config tags as before, and —
  when a configured name normalizes to a custom tag's `name_key` —
  atomically PROMOTES that SAME row to config-owned (applying the
  configured name/description; pk, assignments, suggestions and history
  preserved); it retires ONLY absent config-owned tags, never custom
  tags. Custom creation validates exact-str/nonblank, control/newline
  rejection, `Tag.name`/`name_key` max lengths and ALL existing
  normalized-key collisions (retired rows included) with stable friendly
  errors, uses the shared manual-assignment semantics, runs under the
  local SQLite BUSY/LOCKED retry (outside atomic), takes no pipeline
  lock, and schedules exactly one recording search sync inside the
  successful transaction; concurrent duplicate creation never creates
  duplicates nor leaks `IntegrityError`.
- The + Add tag modal commits the COMPLETE desired active selection
  ATOMICALLY on Done via `workflow/services/tags.py:apply_tag_selection`
  (the ONLY bulk writer; the individual tag-add/tag-create/
  tag-confirm/tag-remove endpoints stay for compatibility but are not
  used inside the modal). Tapping an option toggles a checkbox LOCALLY —
  nothing mutates, redirects, or flashes a banner before Done; Done
  applies the whole set in one POST/transaction (the optional one new
  custom tag name is committed only by Done, inside the SAME
  transaction; a validation/collision failure rolls ALL selection
  changes back). The service validates bounded exact-integer IDs (bool/
  duplicates/missing rejected; size-capped against the real tag count
  with a hard limit), category-checks available vs retired
  (`is_configured=True` vs explicit `is_configured=False` opt-in), and
  applies exact ownership semantics: already-active-and-selected stays
  UNCHANGED (suggested stays suggested, confirmed/manual keep
  origin/provenance — Done never promotes); newly selected creates
  active manual; selected-inactive reactivates manual and clears
  suppression/source summary; active-but-unselected applies the exact
  user-removal suppression (`deactivated_by="user"`); inactive-and-
  unselected is untouched. Exactly ONE recording search sync is
  scheduled inside the transaction only when indexed tag membership
  changed; an unchanged Done is zero DML and zero callback. Runs under
  the local SQLite BUSY/LOCKED retry (outside atomic), takes no
  pipeline lock, returns safe counts only. Success (changed or
  unchanged) emits NO banner; invalid/error Done may show one sanitized
  error banner. The modal enhancer gives a panel with its own
  `[data-modal-commit]` Done submit a Cancel close control (never a
  second generated Done), and (re)opening resets staged checkbox/text
  state to the server-rendered initial values — no sessionStorage
  reopen marker exists any more.

## Reversible archive (never source deletion)

- `Recording.archived_at` (nullable, migration 0014) is the ONLY
  Recording archive state; `Section.archived_at` (nullable, migration
  0015) is the ONLY individual TOPIC-Section archive state. Archive is
  REVERSIBLE and is never source-file, row, layout, index or history
  deletion: audio, transcripts, summaries, tags, attempts, routing
  decisions, `SegmentedVersion` revisions, SearchDocuments and
  EmbeddingDocuments all stay physically stored and internally healthy.
  Restore clears the field.
- `workflow/services/archive.py` is the ONE archive/restore service:
  transactional, idempotent (unchanged = zero DML), re-fetches the exact
  Recording/Section, sets ONLY `archived_at` (+ `updated_at` for a
  Recording), never touches files/network/index rows/history, returns
  safe counts only, and logs nothing. Recording archive refuses while ANY
  unfinished `ProcessingAttempt` exists (the mutating web path runs
  `recover_interruptions` first); Recording restore is always safe.
- Section archive is a visibility/eligibility marker on an individual
  TOPIC Section, NOT layout deletion: it NEVER merges ranges and NEVER
  mutates/supersedes a `SegmentedVersion`. Only a canonical topic Section
  of the active transcript's active valid layout whose parent Recording
  is NOT archived may be NEWLY archived (fixed, historical, cross-parent
  and archived-parent targets refuse with stable categories). Restore is
  deliberately PERMISSIVE: it clears an existing marker even when the
  Section later became historical (that is the ONLY mutation; the layout
  is untouched and the Section stays otherwise read-only). The web
  Restore path covers that case through a DEDICATED opaque
  ``section_restore_fingerprint`` (Recording id + archived state, Section
  id + own archived state, transcript/layout ids + active/historical
  state, ordinal) that does NOT require the layout/transcript to still be
  active and never weakens ``section_state_fingerprint`` (which stays
  canonical-active-only for ordinary summary/archive actions).
- The SHARED canonical-layout validator keeps treating an archived
  Section as structurally PRESENT (archive is eligibility, not topology),
  so parent suppression stays based on the FULL canonical layout: an
  archived Section creates an intentional hidden item, never a
  resurrected parent Recording. Ineligibility is centralized, never
  forked: `workflow.query._section_base_queryset` excludes
  `Section.archived_at` (so the Library item projection/count/identity
  UNION, the search engines' one-column item-key scope and the stale-key
  hydration `library_items_by_keys` all exclude it while siblings remain
  and the parent stays suppressed).
- Web: `POST /recordings/<pk>/archive/`+`/restore/` and
  `POST /recordings/<pk>/sections/<id>/archive/`+`/restore/` execute on
  the FIRST POST (no confirmation interstitial) under the exclusive
  pipeline lock + `recover_interruptions`. The recording-level opaque
  `state_fingerprint` binds the archived state; the Section-level opaque
  `section_state_fingerprint` binds the Section's OWN archived state (in
  addition to the recording archived state and the canonical
  layout/section identity) — a pre-archive form can never execute after
  an archive and vice versa (canonical but stale ⇒ the safe under-lock
  no-op; lock busy ⇒ friendly 409; missing/duplicate/malformed ⇒ friendly
  400 before any lock). An archived individual Section detail stays
  directly readable with a Section-archived banner + Restore and offers
  NO ordinary summary/tag actions (service-side guards reject forged
  ones); an archived parent Recording suppresses every Section
  archive/restore control.
- Ineligibility is centralized, never forked: `workflow.query.filter_only`
  excludes archived Recordings (Library item projection/count/identity
  UNION and the recording-scope search queryset) and the Section branch
  excludes archived parents and archived Sections, so keyword/semantic/
  hybrid search (web AND CLI) and the stale-key revalidation
  (`library_items_by_keys`) can never return or hydrate an archived item.
  `review.build_review_report` and the global Review badge exclude
  archived; the pipeline work selectors (`route_pending`,
  `transcribe_ready`, `summarize_pending`, `run_pipeline`) and explicit
  `manual_route`/`confirm_routing`/`route_one`/`transcribe_one`/`retry`/
  `summarize_one`/`summarize_section_one` refuse or skip archived
  Recordings; the tag serialization boundary and
  `save_segmented_version` reject archived writes. Read-only
  detail/history/export routes stay available for restore/audit.
- Ask: `retrieve_semantic_evidence` gained the bounded
  `exclude_archived_sections` predicate. It excludes ARCHIVED Section
  item keys (the SHARED `search_query._item_key_case` mapping plus the
  SHARED canonical-layout predicate, never forked) BEFORE top-K
  selection, while the ordinal-0 whole-recording variants stay
  admissible (a whole-recording document under a split derives a NULL
  item key and is not matched by the exclusion — the full Library item
  scope would over-exclude it, so it is deliberately not reused). Ask
  still passes the canonical archived-excluding Recording scope and
  keeps the one-sweep/one-embedding/one-traversal contract; post-chat
  revalidation fails closed when a cited Section's summary OR a Segment
  inside an archived Section's range appears, so an archived-section
  citation is impossible.
- Archive/restore schedule NO search/embedding synchronization, delete
  NO registry/FTS/vector rows and change NO index mapping/version; the
  search and embedding indexes stay internally healthy (a rebuild still
  includes archived documents internally) — ineligibility is a
  query-scope concern. Rescan/ingest deduplication keeps the SAME
  Recording/AudioSource (never a second content identity) and never
  clears `archived_at`.
- The read-only `/recordings/archived/` page (linked from the normal
  Library, titled `Archived items`) renders ONE bounded deterministic
  table over `workflow.query.archived_item_queryset`: archived Recording
  rows PLUS independently archived canonical ACTIVE topic Sections whose
  parent Recording is NOT archived (never parent + child duplicates),
  ordered globally by `archived_at` descending then canonical item key,
  hard limit+1 sentinel, hydrated by the SAME batched
  `hydrate_library_items` contract (no N+1). Columns are Archived /
  Title / Duration / Type-context. No search/`ListFilters`/return-token
  expansion, no network, no writes.
- Section "deletion" is discoverability only: an ACTIVE canonical topic
  Section detail's upper link is `Edit/remove section` (the single link;
  the duplicate paragraph and its verbose copy are gone) pointing to its
  location in the existing transcript editor, where removing a
  surrounding split/crop merges it with its neighbor and saved revisions
  remain in History. There is NO section-delete service/model/migration
  and historical/fixed/malformed/archived sections get no removal
  control. Section archive is the separate, reversible visibility marker
  described above.

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
  an explicit unresolved marker) and the ACTIVE routing decision's
  stable identity/behavior (decision pk/ordinal, profile_name,
  model_id, language_arg, routing_verified; explicit no-active marker;
  never raw evidence, confidence, verifier or timestamps) in addition
  to recording state, attempts and variant languages — a
  source-language correction or a routing update that changes neither
  the processing status nor an attempt invalidates rendered action
  forms even when no attempt is created and the action mode is
  unchanged. The value is OPAQUE: a canonical
  lowercase 64-hex SHA-256 digest over the exact deterministic bound-
  state JSON bytes (ids, languages and the raw JSON never appear in
  it). Fingerprinting is read-only (SELECTs only).
- Actions return to their origin page via a server-owned allowlist
  token (`return_view`: `detail | summary`) carried by the executing
  action form and honored on the redirect (there is no confirmation
  interstitial — see the Web actions section); missing/invalid/forged
  values fall back to recording detail. Arbitrary client URLs are
  never accepted.
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
- Optional static `summarization.models` (exact nonblank strings, ≤32
  entries of ≤255 chars, no stripping/canonicalization, duplicates/
  bool/non-list/non-str/control rejected as sanitized `ConfigError`);
  effective choices are always exact `llm.model` first, then configured
  alternatives in order, de-duplicated by exact identity, omitted/empty
  ⇒ `(llm.model,)`. The web Retry/Regenerate forms (recording and
  section, shared `_variant_action.html`) render a `<select name="model">`
  with a `<label>` reading `Retry:`/`Regenerate:` and the exact default
  selected; initial Generate renders no selector and is default-only.
  The executing POST is strictly parsed (exactly one value, exact
  effective-allowlist membership) BEFORE lock/recovery/network/write —
  one fixed friendly `invalid_model` 400 for missing/duplicate/blank/
  oversized/unknown — and re-validated at the service boundary. A
  selection affects ONLY that one operation: it is threaded through
  source-language detection, map/reduce/final/repair/fallback, the
  durable attempt (`model_id`/`cli_args_json`) and `Summary.model_id`
  and the config fingerprint, with `config.llm.model` as the unchanged
  default for every existing service/CLI/batch caller. The effective
  allowlist/default are bound into the recording and section opaque
  action fingerprints (config choice changes stale forms). `AppConfig`
  is never mutated; GET performs no model discovery/network and no
  migration or new CLI feature exists.

## Summarization oMLX request contract (reliability patch)

- Summarization map/reduce/final and single calls send a deterministic
  OpenAI-compatible `response_format` (`json_schema`, `strict: true`):
  `brain_summary_map` for map/non-final-reduce and `brain_summary_final`
  for final/single. The schemas encode the intended canonical shape and
  the representable structural bounds (counts, string lengths, minLength
  nonblank strings, key-point levels, nullable-but-required action
  `owner`/`due_date`, empty-list semantics); every declared property is
  listed in `required` for strict structured outputs. Compatibility
  parsing and local semantic validation remain authoritative whether or
  not the server enforces the grammar — historical null/missing
  collection tolerance, whitespace stripping, canonical BCP-47 language
  normalization and key-point hierarchy rules are NOT encoded in the
  schema. Allowed tag names are NOT encoded as an enum, so the existing
  unknown-suggestion `rejected` behavior is unchanged.
  `PROMPT_IMPLEMENTATION_VERSION` is `3`.
- The serialized request-size gate measures the whole payload including
  the schema and any repair prompt.
- `finish_reason` is validated strictly: `length` is the stable
  `output_truncated` category (raised BEFORE content parsing), `stop`/
  absent is normal, and any other reason is a fixed sanitized
  `invalid_envelope` whose raw value is never surfaced.
- Finite request state machine (no loops): one structured `json_schema`
  request, then EXACTLY ONE repair request when the HTTP-successful
  output is invalid (malformed envelope/JSON, schema_validation,
  language_mismatch, output_truncated). An explicit HTTP 400/422
  response_format/json_schema capability rejection of the structured
  request instead allows one plain attempt plus at most one plain repair
  — hard max 3 calls, only on that explicit path. Endpoint/timeout/
  other-HTTP/response-size failures are never retried. The repair prompt
  repeats the exact required shape, names only an allowlisted stable
  category, never contains the rejected output, and asks for compact
  output when truncated; a repeated `length` after one compact repair is
  terminal `output_truncated`. The last specific error code is preserved.
- Every HTTP 200 — including one carrying an oMLX Warning (recognized
  or otherwise) that `response_format` was not enforced — is treated
  identically: local validation is authoritative and the same single
  repair request applies; there is NO Warning classifier and no warning
  condition triggers a plain fallback. Plain fallback exists ONLY for an
  explicit HTTP 400/422 response_format/json_schema capability
  rejection. Raw header text and bounded error-body samples are never
  inspected for behavior, persisted, logged, returned, or otherwise
  surfaced.
- `_reduce_layer` carries its internal `_split_allowed` termination
  guard: a merged request that still exceeds the cap after its halves
  were already reduced fails cleanly with `input_too_large` instead of
  re-reducing the same pair without bound.

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
- `brain run` honors `macwhisper.file_stable_seconds`: a newly
  discovered or changed source must stay unchanged for that window
  before it is hashed. `brain run --now` is the explicit one-pass
  operator escape hatch that bypasses ONLY that persisted
  stability-window eligibility check (newly discovered, already
  observing, retry-failed and freshly detached changed sources hash
  immediately). It never weakens the inbox/symlink boundary, the
  `_hash_source` before/after stat validation, SHA-256
  identity/deduplication, or `validate_source_for_processing`; a file
  changing during hashing is still deferred. `brain ingest` remains
  stability-aware and the service defaults are
  `respect_stability_window=True`.
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

- Migrations `0001`–`0015` define the current schema (0007 is the
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
  tables; 0010 is the approved tag-provenance migration:
  `Tag.definition_origin` (`config`/`custom`, default `config`; existing
  rows migrate as config) plus the `chk_tag_definition_origin_allowlist`
  DB CHECK — additive and fully reversible, no data migration or
  `RunPython`; 0011 is the approved Step-6.1 segmented-version
  migration: the new immutable `SegmentedVersion` layout parent plus
  nullable `Section` `segmented_version` ownership FK and canonical
  half-open range fields, with the global `(transcript, ordinal)` section
  unique replaced by the conditional `uniq_section_ordinal_fixed` /
  `uniq_section_ordinal_topic` constraints and the
  `chk_section_shape_segmentation` fixed-vs-topic same-row CHECK, plus
  the layout revision/active/lifecycle/chronology/range CHECKs — additive
  and fully reversible (no data migration; reverse deterministically
  renumbers topic ordinals before restoring the global unique); 0012 is
  the approved Step-6.2 section-tag migration: the nullable
  `TagAssignment.section` ownership FK (PROTECT) plus the conditional
  `uniq_tag_assignment_recording` (`section IS NULL`, unique
  `(recording, tag)`) / `uniq_tag_assignment_section` (`section IS NOT
  NULL`, unique `(section, tag)`) uniques replacing the old
  recording-only constraints — additive and fully reversible, no data
  migration (existing rows stay recording-scoped; the reverse deletes
  section-only assignment rows before restoring the old global
  `(recording, tag)` unique, preserving recording assignments); 0013 is
  the approved Step-6.2a temporary-split-title migration: the nullable
  `Section.title_is_temporary` Boolean (default False, existing rows
  migrate as custom) — additive and fully reversible with no data
  migration; 0014 is the approved reversible-Recording-archive migration:
  the nullable `Recording.archived_at` timestamp (existing rows migrate as
  active/NULL) — additive and fully reversible with no data migration
  or `RunPython`; 0015 is the approved reversible-individual-Section-
  archive migration: the nullable `Section.archived_at` timestamp
  (existing rows migrate as unarchived/NULL), depends on 0014 and never
  edits it — additive and fully reversible with no data migration or
  `RunPython`). Add NEW
  migrations, never
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
- **Step 4 v6 Recording Detail / Transcript / History redesign
  (delivered)**: the approved v6 document-oriented production redesign
  is implemented (`design/ui-prototype/` remains the approved design
  source). Detail: stable default-language `RecordingCard.title` h1
  (variants change summary content, never page identity), one composite
  read-only status/next-action panel, compact tag chips with a native
  `<details>` editor, complete selected summary with contextual
  copy/export actions plus a collapsed Summary-provenance disclosure,
  EXACTLY five active-transcript preview segments plus the accurate
  total/open link, and a collapsed Technical-details disclosure; the
  recent-attempts table is removed (History owns audit data). Transcript:
  back-to-overview, title/context metadata, copy/plain/timestamped
  downloads via the existing export URLs, retained pagination,
  `id="segment-<ordinal>"` anchors and `?v=` historical versions
  (`select_related("attempt")`). History: captioned responsive tables
  (routing, processing attempts, transcript versions, summary versions,
  source info); every collection bounded by `HISTORY_LIMIT = 100`
  (limit+1 sentinel) with visible truncation notices and no N+1; routing
  rows project ONLY allowlisted fields (never raw `evidence`); attempts
  stay sanitized via `attempt_summary_for_display`; sources show safe
  original filenames only (never paths); audio status is
  present/missing only. All GETs remain strictly read-only; the
  standalone summary route and historical summary/transcript routes
  stay operational; multilingual generation semantics, tags and exports
  are unchanged.
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
   scope-honest) — since Step 6.3 the web layer instead passes the
   normal Library's one-column `item_key` UNION as the engine's
   `item_scope` (see the Step 6.3 bullet; the Recording-scope engine
   API is retained and unchanged); search sorting defaults/falls back to `relevance`
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
  below; Steps 5B.4, 5C, 5D and 6.3 have since been delivered too —
  see their bullets below). Verification (historical 5B.2 state): the full suite then
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
   (see the Step 5D bullet below); **Step 6.0, Step 6.1, Step 6.2,
   Step 6.2a and Step 6.3 are delivered** (see the Step 6 bullets
   below) and the next planned work is
   **Step 6.4** (retention/Keep-Audio/Rescan; real source-file
   deletion/move and any launchd install remain explicit approval
   gates).
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
    sanitized, no lock/recovery/write). The web UI has ONE query input:
    the global top bar carries a native Keyword/Semantic/Hybrid selector
    and a submit button, and POSTs every mode (with CSRF) to
    `/recordings/search/`. Keyword POST redirects (302) to the canonical
    bookmarkable GET `/recordings/?q=...`, preserving the canonical
    active `filter_pairs` and the effective view on Library contexts (a
    blank query drops only the query text, keeping filters/view; from
    non-Library pages the search is global, with no hidden Library
    state); the direct keyword GET stays supported and strictly
    read-only (a forged `mode=` on GET is ignored — GETs never
    embed/network). Semantic/hybrid execute and navigate POST-only at
    `/recordings/search/` (GET = 405 with zero work, CSRF-protected),
    their query never enters a URL, redirect, log or error; invalid
    scope filters REJECT (never widened to unscoped), every service
    failure is ONE stable `unavailable` state with the query cleared
    (never leaked back into the top-bar input), and navigation
    (pagination/sort/filter/view) is POST-only with hidden
    server-validated state; filters, provenance, snippets and segment
    jump links are shared with keyword search. The current vector mode
     is pre-selected in the top bar on rendered results, Keyword
     otherwise. Mobile uses a two-row fixed header: brand + navigation on
     the first row and the search form on a full-width second row.
     Since Step 6.3 EVERY mode (CLI and web, keyword included) runs
     LIBRARY-ITEM mode over the normal Library item scope — split
     recordings answer their Sections with the parent suppressed (see
     the Step 6.3 bullet); the unscoped/per-Recording engines and the
     historical parity output remain available and unchanged.
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
  consuming `semantic_search()` (which dedups to one winner per unit —
  per Recording in the legacy mode, per Library item in Step 6.3 item
  mode — and permits metadata). Evidence admits only `segment` and `summary`
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
- **Step 6** (overall plan accepted; **Step 6.0 complete** — see
  `docs/step-6-plan.md` and the approved `docs/step-6-decisions.md`):
  user-initiated topic splitting and manual **crop/trim**
  (non-destructive logical trim selecting a working interval/range while
  retaining source audio and the full transcript; any derived audio export
  is a separate deferred feature), section-level summaries/tags, retention
  cleanup (deletion only after successful processing + retention delay),
  missing-file reconciliation, launchd scheduling. **Approved Step 6.0
  rules**: ordinal-0 stays full-recording/fixed and trim never changes the
  full-recording defaults (ordinal-0 default summary, search, embeddings,
  Ask) — a saved crop becomes the normal Transcript working presentation
  (cropped rows hidden by default, "Show full transcript" toggle) while the
  persisted full transcript data stays complete; a segmented
  version is one immutable transcript-bound revision holding a working
  range plus **zero or more** topic sections (zero splits ⇒ zero sections;
  N+1 splits ⇒ N+1 sections that exactly partition the retained range and
  require topics), saved
  atomically (clear crop restores the full range without creating
  sections); boundaries are segment ordinals
  with canonical `[start, end_exclusive)`; retranscription creates no
  segmented version and copies no boundaries/summaries/tags (an unchanged
  save is a no-op); successful regeneration of the current default summary
  restarts the retention timer (failed regeneration and
  non-default/section/tag/layout/trim edits do not; retranscription
  restarts it once a new default summary exists); Rescan is ONE global
  POST-only `run_ingest` pass (no manual relink). **Approved UX refinement**:
  6.1 crop/split editing lives only on the active Transcript page
  ("Edit trim & splits"; historical transcript versions are read-only, no
  separate editor screen/route). Pressing Edit only reveals small scissors
  controls on the inter-segment divider lines (no panel, no modal; no
  transcript start/end scissors);
  clicking a scissors opens a small accessible action dialog with Split
  here, Crop from here (start becomes the boundary), Crop to here
  (end-exclusive becomes the boundary) or Remove split (only when already a
  split) — empty crops, outside-range actions, endpoint splits and
  duplicate splits are rejected. Cropped rows are hidden (never merely
  dimmed) while editing and in the saved working view, with a compact
  crop/split status plus Reset/Clear crop and a small "Show full
  transcript" toggle; crop-only has zero topic sections, and splits create
  N+1 sections that the user names via inline topic inputs — no automatic
  or derived topic inference, no section-card/ranges/summaries/tags/
  provenance in the editor. The staged payload is the crop range + sorted
  split markers + topic labels and the service materializes the exhaustive
  `Section` rows (zero for crop-only; no independent section start/end
  selects). History owns the revision list, not Transcript. The
  retention/Keep Audio/Rescan **screen** is removed from the
  prototype (6.4 UX placement deferred; its approved policies are retained).
  **Step 6.1 (segmented versions: logical trim + topic layout/history),
  Step 6.2 (section-level summaries/tags + derived Library items),
  Step 6.2a (safe Library return + display refinements) and Step 6.3
  (section content in the search/embedding/Ask stack) are delivered** —
  see the dedicated invariant bullets below; the next planned phase is
  **Step 6.4** (retention/Keep-Audio/Rescan, missing-file
  reconciliation). Actual source file deletion/move/trash/quarantine (6.4) and
  installing/enabling any schedule (6.5) remain separate explicit approval
  gates.
- **Step 6.1 — Segmented versions (delivered)**: `SegmentedVersion` rows
  are IMMUTABLE transcript-bound working-layout revisions holding a
  canonical half-open crop `[start_segment_ordinal,
  end_segment_ordinal_exclusive)` plus **zero or more** topic `Section`
  rows — zero splits ⇒ zero topic Sections; N splits ⇒ exactly N+1
  exhaustive named topic Sections that partition the retained range.
  The fixed ordinal-0 whole-recording `Section` stays OUTSIDE all
  revisions (never edited/versioned) and keeps driving the ordinal-0
  defaults (default summary/search/embeddings/Ask).
  `workflow/services/segmentation.py` is the ONLY writer/validator of
  `SegmentedVersion` and topic `Section` rows: exact bounded input
  validation (no coercion), sorted-canonicalized split markers, one
  transaction that supersedes the prior active revision and creates the
  new one, ZERO-DML no-op for unchanged payloads, fail-closed
  `layout_invalid` on malformed stored state, an OPAQUE read-only
  `segmentation_fingerprint` (SELECTs only), and fixed sanitized
   `SegmentationError` categories; it never acquires the pipeline lock,
   logs nothing and touches no network/files, and since Step 6.3 an
   actual REAL layout change schedules exactly ONE post-commit
   parent-recording search sync (see the Step 6.3 bullet; at the 6.1
   delivery it scheduled no sync because no section content was
   indexed yet). At most ONE active layout per transcript (partial
  unique); `(transcript, revision)` unique; a new active transcript gets
  NO layout ("Not segmented") — retranscription copies no
  boundaries/summaries/tags. Editing lives ONLY on the active Transcript
  page ("Edit trim & splits"): pressing Edit reveals small scissors on
  the inter-segment divider lines, staged changes stay PAGE-LOCAL and
  must be SAVED BEFORE NAVIGATION (browser dirty-leave warning; NO
  browser draft persistence); the saved crop is the normal working
  presentation (cropped rows hidden, "Show full transcript" toggle). Save
  executes on the FIRST POST from the editor (no confirmation page —
  see the Web actions section) under the pipeline
  lock + `recover_interruptions` guarded by the opaque stale fingerprint
  (stale/duplicate submissions are safe no-ops; lock busy is the friendly
  409). GET stays strictly read-only; historical transcript versions and
   explicit layouts are read-only via `?v=<transcript>&layout=<version>`;
   History owns a bounded revision list (`HISTORY_LIMIT`). At the 6.1
   delivery save scheduled no search/embedding sync (no indexed content
   changed yet); since Step 6.3 a real layout change schedules exactly
   ONE post-commit parent-recording search sync because the layout
   decides which topic-section summaries are canonical index content
   (see the Step 6.3 bullet). Migration
  0011 (reversible, no data migration) adds `SegmentedVersion` plus
  nullable `Section` ownership/range fields and replaces the global
  `(transcript, ordinal)` unique with the two conditional section-shape
  constraints (fixed vs topic) and the layout range/revision/
  lifecycle/chronology CHECKs; existing ordinal-0 queries gained
  `segmented_version IS NULL` defense-in-depth filters.
- **Step 6.2 — Section-level summaries and tags + derived Library items
  (delivered)**: topic `Section` rows of an active split layout are now
  first-class Library items and carry their own multilingual summaries
  and tags. Step 6.3 (section content in the
  search/embedding/Ask stack, Library-item search everywhere) has since
  been DELIVERED — see its dedicated bullet below; the whole-recording-
  only limitations that existed at the 6.2 delivery no longer hold. 6.4
  (retention/Keep-Audio/Rescan) and 6.5 (launchd) remain later approval
  gates. Migration 0012 is additive and fully reversible; NO
  real-database migration is claimed and the work stays uncommitted in
  the working tree.
  - **Derived Library item projection** (`workflow/query.py`): the
    Library overview unit is a DERIVED item, never a persisted model —
    there is NO `LibraryItem` model. Any Recording with no active topic
    Sections (unprocessed, unsplit active transcript, or a crop-only
    active layout) yields exactly ONE recording-backed item; an active
    transcript/layout with N canonical topic Sections (N ≥ 2) yields
    exactly those N section-backed items and REPLACES its recording-
    backed item in the normal Library; historical layouts are absent and
    retranscription (no new segmented version) naturally returns to one
    recording item. The projection is a read-only DB UNION of
    same-shaped recording/section branches (`library_item_queryset`):
    filters, count, ordering and pagination all happen database-side
    BEFORE hydration — never a Python expansion of all recordings. The
    "which recordings are replaced / which Sections are valid" state is
    ONE lazy parameterized read-only SQL canonical-layout predicate
    (a RawSQL subquery shared by both branches — exactly two fixed
    parameters, never a growing `IN (...)` list, never a Python id set)
    that fail-closes on every canonical rule (ACTIVE version +
    transcript, contiguous segment ordinals 0..count-1 with a nonempty
    transcript, range inside `[0, count)`, topic count in
    2..`MAX_TOPIC_SECTIONS`, cross-parent rejection, section ordinals
    exactly 1..N with an exhaustive contiguous partition, exact-string
    bounded nonblank control-free titles). Each page is hydrated by
    `hydrate_library_items` in bounded batched queries (no N+1) into
    `LibraryItemCard` adapters (section items carry their section-scoped
    active tags, default-variant Summary, variant state and language
    set). Since Step 6.3 the canonical-layout predicate has ONE home —
    `segmentation.canonical_layout_predicate()` (with the
    `canonical_hidden_recording_ids`/`canonical_active_section_ids`
    SELECT wrappers) as the SQL twin of the shared Python validator —
    consumed by this projection AND the search-index mapping/canonical
    validation, the engines' item-key SQL and Ask, never forked
    anywhere; the Recording metadata-aux tag projection still filters
    recording-scope assignments only (`section__isnull=True`) — section
    tags are indexed solely in their own section-summary documents.
  - **Section summaries** (`workflow/services/summarize.py:
    summarize_section_one`): an EXPLICIT per-section action (POST-only,
    executed on the FIRST POST from the section-detail form — no
    confirmation interstitial, see the Web actions section — under the
    shared pipeline lock + `recover_interruptions`, guarded by the
    OPAQUE bounded `section_state_fingerprint` — SELECT-only;
    stale/duplicate submissions are safe no-ops, lock busy is the
    friendly 409). While the request runs, the shared `app.js`
    direct-action enhancement disables/relabels the submit control and
    shows the optimistic `aria-live` pending state; in-flight
    navigation anchors are no longer disabled (the removed
    confirmation-page `data-confirm-exempt`/`Back to section` escape
    hatch and the `initConfirmForms` anchor-disabling machinery do not
    exist). The
    target must be a TOPIC Section of the ACTIVE transcript's ACTIVE
    layout (shared `segmentation.require_active_topic_section`); fixed,
    historical, cross-parent and malformed-layout targets are stable
    sanitized `SegmentationError` categories; historical sections are
    readable but offer no ordinary summary/tag action (an archived
    historical Section still offers the dedicated Restore). Input is ALL
    and ONLY the Section's canonical segment range
    `[start, end_exclusive)` —
    deterministic full stored text, never source audio, exact-count
    defense-in-depth — chunked by the existing bounded path. Output
    variants/versioning reuse the EXACT multilingual machinery: one
    active Summary per (transcript, section, output_language),
    section-scoped `SummaryVariantState`, generation selectors
    default/original/en/zh-Hant, exact-scope attempt provenance and
    interruption recovery. The target section/layout is captured at the
    start AND revalidated at persistence (`section_layout_changed`
    failure — never a write to read-only history). `persist_summary`
    itself re-locks the parent Recording FIRST and then the authoritative
    target Section (the same order as `archive_section`) and rechecks
    BOTH archive markers before any Summary/tag/variant DML — a parent
    archive raises the stable archive error, a Section archive the stable
    `section_archived` segmentation error — so an archive introduced
    after generation but before persistence writes nothing and schedules
    no sync. A section summary
    NEVER changes the Recording-level default tuple (`summary_status`,
    `resummarization_failed`, `last_failed_attempt`), processing status
    or the whole-recording summary; since Step 6.3 every successful
    activation schedules exactly ONE post-commit parent-recording
    search sync (canonical topic-section summaries are indexed
    documents of the parent Recording — see the Step 6.3 bullet).
  - **Section tags** (`workflow/services/tags.py` section-scoped
    mutations + migration 0012): nullable `TagAssignment.section`
    ownership FK (PROTECT) with MUTUALLY EXCLUSIVE conditional uniques —
    recording scope (`section IS NULL`, unique `(recording, tag)`;
    existing rows migrate unchanged) vs section scope (`section IS NOT
    NULL`, unique `(section, tag)`); `recording` stays a REQUIRED
    denormalized parent for both scopes, so a recording and its sections
    hold independent assignments of the same tag. All active section
    writes are SERVICE-ONLY (`add_manual_tag_section`,
    `confirm_section_suggestion`, `remove_section_tag`,
    `apply_section_tag_selection` — the atomic complete-selection Done —
    and `create_custom_tag_and_assign_section`), sharing the exact
    validation/retired-opt-in/origin/suppression/custom-collision
    semantics, running under the local SQLite BUSY/LOCKED retry (outside
    atomic) and taking no pipeline lock; since Step 6.3 a mutation
    schedules exactly ONE parent-recording search sync inside its
    transaction ONLY when the Section's ACTIVE tag-name set actually
    changes (created/reactivated/removed — the names are indexed inside
    the section-summary documents' aux), while an origin-only
    `confirm_section_suggestion`, an unchanged Done and every failure
    schedule nothing; historical sections
    are read-only. Only the DEFAULT variant's section summary
    materializes section-scoped suggestions.
  - **Verification (independently confirmed, historical state)**: full
    suite **2722 passed** (the Step 6.1 state was 2507 — historical;
    the Step 6.2 delta is **215 tests**: the prior 207 Step 6.2 tests
    plus **8 UI-refinement tests** — 3 section-confirmation, 4 normal
    Library-table, 1 search-table scoping), only the known `audioop`
    warning; `manage.py check`,
    `makemigrations --check` (0012) and `git diff --check` clean; the
    focused Step 6.1/6.2 section set is **326 passed**. No
    real-database migration is claimed; the work and its tests are in
    the working tree, uncommitted.
- **Step 6.2a — Safe Library return, temporary split titles, derived
  display title, section duration (delivered)**: a bounded follow-up to
  6.2 that improves the section Library/detail UX; the search/index/Ask
  contract was unchanged at its delivery and the explicit 6.3
  requirement recorded at the end of this bullet has since been
  FULFILLED by the delivered Step 6.3 (see the Step 6.3 bullet).
  Migration **0013**
  is additive and fully reversible; NO real-database migration is
  claimed and the work stays uncommitted in the working tree.
  - **Safe Library return** (`workflow/services/library_return.py`):
    ONE small server-signed (Django HMAC signer, salt-scoped, URL-safe
    base64) token per NORMAL Library render encoding ONLY the canonical
    validated normal-Library state: the `ListFilters.as_pairs()`
    filter/sort pairs (STABLY DE-DUPLICATED so a redundant duplicate
    tag value never survives), a bounded positive `page`, and the
    `cards`/`table` view. The destination is ALWAYS
    `reverse('recordings')`; a `lib_return` parameter present on the
    Library is the SOLE carrier of state (a valid token ignores the raw
    query string, including any `q` and any raw `view=` — which also
    never mutates the view cookie), and an invalid/forged/oversized/
    non-canonical token decodes to `None` so the Library falls back to
    its PLAIN state using the view COOKIE/default only while ignoring
    all raw query state. Decoding re-validates every pair through the
    shared `list_filters(..., allow_relevance=False)` and requires an
    exact canonical round-trip — a smuggled search query, `relevance`
    sort, unknown, redundant or DUPLICATE pair rejects the WHOLE token.
    The token is
    added to the normal-Library recording AND section links
    (recording-backed title links in card/table, the section-card parent
    Recording link and the section title links) and propagated verbatim
    through section-detail tabs, the section summary executing form and
    its execution redirect (the token travels only on the validated
    action form and is echoed back only from the already validated
    value — the removed confirmation page and its CANCEL link no
    longer exist) and the section tag redirects;
    `recording_detail` validates an optional `lib_return` through the
    shared decoder and, when valid, restores the originating page/state
    through its top-left `← Library` breadcrumb
    (`library_return.return_url`); absent/invalid/forged tokens leave
    the plain `reverse('recordings')` breadcrumb and are never echoed.
    Direct section links
    still work
    and search results never generate a token (no search-origin
    support). GETs stay strictly read-only.
  - **Temporary split titles** (`workflow/services/segmentation.py` +
    migration 0013 `Section.title_is_temporary`, Boolean default False,
    existing rows custom): new editor-created ranges get the
    server-authoritative `Segment N of YYYYMMDDHHMM` (N = the 1-based
    canonical Section ordinal; timestamp = `recorded_at` else
    `discovered_at` rendered in the CONFIGURED timezone). The editor
    JSON carries a BOUNDED server-generated `temporary_titles` list
    (index = ordinal − 1, capped at `MAX_TOPIC_SECTIONS`) so new
    split-created sections are visibly PREFILLED immediately — the
    browser clock is never used — and a carried-over temporary section
    whose canonical ordinal CHANGED in a revised layout regenerates the
    appropriate server title instead of retaining a mismatched
    `Segment N`; ANY input event makes a title custom. The editor
    payload carries bounded exact flags; the parser validates
    cardinality and exact `0`/`1` values and the WRITER boundaries
    require a new True-flag title to be blank OR EXACTLY equal the
    CURRENT server-derived value (`title_flag_forgery`/`title_flag_count_mismatch`
    — a custom title can never be claimed temporary); the no-op
    comparison uses the RAW submitted payload, so an unchanged active
    layout stays a ZERO-DML no-op even when the stored temporary titles
    were derived under an older effective timestamp. READ-side
    validation (canonical service reads, the fingerprint, the Library
    SQL validity predicate) requires a True temporary flag to carry the
    EXACT canonical SHAPE `Segment <ordinal> of <12 ASCII digits>` — an
    arbitrary custom title on a temporary row is corrupt stored state
    and fails closed (`layout_invalid`); reads NEVER compare a stored
    temporary title to the CURRENT timestamp (titles are immutable
    creation-time metadata). Existing exact ranges preserve
     title/provenance, segmentation remains the ONLY writer, and layouts
     stay immutable (a flag-only change creates a new revision). At the
     6.2a delivery a save scheduled no sync; since Step 6.3 a real
     layout change schedules exactly ONE post-commit parent-recording
     search sync (see the Step 6.3 bullet).
  - **Derived display title** (`workflow/query.py` +
    `views/recordings.py:section_detail`): `Section.title` is NEVER
    mutated during summary generation. Whenever an active
    DEFAULT-language section Summary exists, its `Summary.title` is the
    Section's ONE user-facing title — it supersedes BOTH a stored
    temporary title AND a manually entered custom title in presentation
    (a display override only; the custom title stays layout
    metadata/provenance, no prompt/new persistence is needed). The
    Section detail page renders that title exactly once (H1/page
    title): `_summary_body.html` suppresses only its embedded title
    paragraph on Section detail (`suppress_title=True`, h3 hierarchy
    retained) while Recording Detail and the standalone summary pages
    keep their existing title rendering. Without a default Summary the
    stored `Section.title` is used. The Library projection's derived
    SQL expression (default Summary title first, stored title fallback)
    drives BOTH the rendered title and the Title A–Z/Z–A ordering (they
    can never diverge), and the section detail H1 is derived from the
    DEFAULT variant only — optional variant titles never replace it and
    switching tabs never changes the page identity.
  - **Section duration** (`workflow/query.py`): section Library items
    project an approximate duration — `(latest usable end_ms - earliest
    usable start_ms) / 1000` where the two endpoints are selected
    INDEPENDENTLY over the canonical range (the earliest NON-NULL
    `start_ms` and the latest NON-NULL `end_ms`; a start-only first
    segment and an end-only last segment still yield a span) — as one
    bounded scalar subquery per projected page row
    (never N+1/unbounded); recording items keep the recording's own
    duration. `LibraryItemCard.duration_seconds` is safe `None`
    ("unknown") when unavailable/nonpositive and renders in the normal
    card/table; the section detail page always shows the same bounded
    aggregate (rendering the literal `unknown` when unavailable).
  - **Explicit 6.3 requirement (FULFILLED by the delivered Step 6.3)**:
    the normal Library tag filters already suppress a valid split
    parent and use the active Section items (the derived projection);
    Step 6.3 mirrors that Library replacement across the whole
    keyword/semantic/hybrid/Ask stack: a valid active split layout
    yields the active Section search/filter results with the parent
    recording result SUPPRESSED — never parent + section duplicates —
    while historical/malformed layouts fail closed to the single
    Recording item (see the Step 6.3 bullet).
  - **Section-scoped summary status + Section-origin returns (bug-fix
    delivery)**: the Section detail status panel is Section-scoped
    (`_section_summary_panel` over the selected `VariantView`) — derived
    ONLY from the Section variant state (`Section summary current` /
    `Section re-summarization failed` with the kept-summary note /
    `Section summary failed` / `Section summary not generated`,
    explicitly this section/language variant), never the parent
    Recording summary tuple; the misleading parent "summary missing"
    text and the `(inherited from the parent recording)` note are gone
    and the panel label is `Section summary status`. The three
    Section-detail links (History, Section in transcript, Full
    transcript) carry a server-owned bounded `return_section=<Section pk>`
    marker plus the already validated `lib_return` token when present;
    History and transcript GETs validate `return_section` as an exact
    positive ASCII-decimal integer naming a readable canonical topic
    Section of the URL recording (active or historical; fixed/malformed/
    cross-parent fail closed) and label the breadcrumb `← Section`
    pointing to the exact Section detail (with a valid `lib_return` if
    supplied) — the plain `← Recording overview` is retained otherwise
    and nothing unsafe is echoed. Transcript pagination preserves only
    the validated return parameters; History internal `v`/`layout`
    links, recording-origin pages, action flows and search are NOT
    broadened; GETs stay strictly read-only.
  - **Verification (independently confirmed, historical state)**: full
    suite **2857 collected and 2857 passed** (the Step 6.2a state was
    2819 — historical; the bug-fix delta is **38 tests** added across
    `tests/test_web_section_detail.py` (14), `tests/test_web_history.py`
    (8), `tests/test_web_segmentation.py` (13),
    `tests/test_web_section_summarize.py` (1) and
    `tests/test_temporary_section_titles.py` (2); the focused Step
    6.2a/6.2 web set is **255 passed**
    (`tests/test_library_return_token.py`,
    `tests/test_temporary_section_titles.py`,
    `tests/test_section_title_migration_0013.py`,
    `tests/test_web_segmentation.py`, `tests/test_web_library.py`)),
    only the known `audioop` warning; `manage.py check`,
    `makemigrations --check` (0013) and `git diff --check` clean. No
    real-database migration is claimed; the work and its tests are in
    the working tree, uncommitted.
- **Step 6.3 — Section content in the search/embedding/Ask stack,
  Library-item search everywhere (delivered)**: completes the recorded
  6.3 requirement — every search surface (keyword/semantic/hybrid, web
  AND CLI) searches the NORMAL Library items, so a valid active split
  layout answers its Section items with the parent Recording SUPPRESSED
  (never parent + section duplicates) while unsplit/crop-only/
  historical/malformed recordings fail closed to their single Recording
  item. **NO new migration at that delivery** (0013 was then the head;
  the version bump is code-only; the later archive work adds 0014).
  - **Index v2**: `search_index.INDEX_VERSION` is `"2"`. The canonical
    summary set is now the whole-recording ordinal-0 variants PLUS
    every ACTIVE variant of a topic Section of the fully canonical
    ACTIVE layout of the ACTIVE Transcript (the SHARED `segmentation`
    canonical-layout SQL predicate plus the `section__transcript=F
    ("transcript")` cross-parent defense; keyed `summary:<id>` like any
    variant, owned by the PARENT Recording). Migration 0008's backfill
    deliberately still mirrors the historical version-1 whole-recording-
    only mapping — after an upgrade the index is DETECTABLY stale
    (`version_mismatch` categories, never rebuilt implicitly) until an
    explicit `brain search-index rebuild`. The canonical-registry
    validation accepts an ordinal-0 fixed OR a canonical topic-section
    summary row; a layout that became historical/malformed de-eligibles
    its rows, which converge to orphans through the existing
    status/reconcile machinery.
  - **Summary docs bind tags aux**: a section-summary document's
    `aux_text` is the shared people/organizations/topics parts followed
    by the Section's ACTIVE tag names in the shared deterministic order
    (`library_metadata.active_tag_names`) — a section tag membership
    change therefore flows through the normal content-hash
    `content_mismatch`/`stale_content` detection with NO separate tag
    bookkeeping; the Recording metadata document still carries ONLY
    recording-scope tag names.
  - **Segment mapping / suppression (ONE SQL derivation)**:
    `search_query._item_key_case` mirrors the Library item-union
    contract over the SHARED canonical-layout SQL: no canonical split
    ⇒ every document of the Recording is `r:<recording_id>`; a
    canonical split ⇒ an ACTIVE topic-section Summary maps to its own
    `s:<section_id>`, a Segment maps to the valid Section owning its
    ordinal range (half-open `[start, end_exclusive)`), and anything
    the layout cannot own — the parent metadata document, the fixed
    whole-recording Summary, cropped-out Segments, stale/malformed/
    cross-parent rows — derives `NULL` and is EXCLUDED (fail closed;
    the suppressed parent never appears beside its Sections). In item
    mode the per-unit candidate bound, `truncated`, the winner dedup
    and the matched counts are all LIBRARY-ITEM truths.
  - **Item scope plumbing**: `workflow.query.
    library_item_key_queryset` is the one-column, unsliced, unordered
    `item_key` UNION of the normal Library identity (same branch
    helpers/filters as the full projection — it can never diverge from
    the projected rows); `search_query.compile_item_scope` validates
    the exact built shape (UNION combinator, exactly one `item_key`
    column on the combined query AND every branch) into an immutable
    `CompiledItemScope` (a compiler-proven empty UNION answers the same
    provably-empty subquery; every other failure is the fixed
    sanitized index failure; hybrid compiles EXACTLY ONCE and shares
    the SAME value with both components). The engines take
    `item_scope=`/`compiled_item_scope=`, mutually exclusive with the
    UNCHANGED Recording `scope=`/`CompiledScope` API (exactly one
    eligibility mechanism per call, fixed usage errors otherwise). The
    derived key is restricted to the compiled scope in the middle
    WHERE — BEFORE the window functions — so bounds/counts stay
    scope-honest. Payloads gain the ADDITIVE
    `item_key`/`item_kind`/`section_id` fields and
    `more_items_matched` beside the SAME-VALUE compatibility alias
    `more_recordings_matched`; item-mode payloads carry the explicit
    `item_mode: true` flag (OMITTED outside item mode, so the legacy
    payload stays byte-identical) and presentation takes its unit from
    THAT FLAG unconditionally — never inferred from the returned rows.
    Semantic item mode keeps the one-sweep/one-integrity-traversal/
    one-embedding/bounded-page contracts and the contiguous
    `(recording_id, document_key)` traversal (items are
    Recording-exclusive, grouped via bounded per-Recording bests);
    hybrid fuses/dedups/tie-breaks on the item identity with a strict
    fusion-boundary canonicalizer (only canonical `r:<UUID>`/positive
    canonical `s:<id>` honoured; anything else falls back to the
    parent `r:` identity — never a crash or a fabricated Section id).
  - **Web/CLI replacement**: `search_web` keyword GET and
    semantic/hybrid POST run item mode with the filtered Library item
    scope; an invalid keyword filter set falls back to the UNFILTERED
    canonical item scope (still item mode — never widened to whole-
    Recording or unscoped-engine mode) with the SAME filters driving
    hydration; invalid vector filters still REJECT. Winners are keyed
    by `item_key` (two Sections of one Recording are distinct winners;
    segment jump links are resolved per winner key), the four Library
    sorts re-order the winner set with the EXACT normal-Library
    `apply_item_sort` (a Section sorts by its own derived display
    title; unique `item_key` tie-break), and relevance stays engine
    order. The page window hydrates ONLY through
    `workflow.query.library_items_by_keys` — the one read-only entry
    revalidating every engine key against the normal-Library identity
    under the same scope filters (bounded per-branch IN, malformed
    keys skipped, stale/replaced/deleted/filtered-out keys silently
    dropped — a stale result can never resurrect an item and nothing
    is fabricated) — and the templates render the SAME item card/table
    as the normal Library (Section rows link to the Section detail;
    search-origin Section links carry NO `lib_return` token). `brain
    search` runs EVERY mode over the UNFILTERED canonical item scope
    (cheap validation before health preserved; scope construction runs
    no query) and enriches Section winners with ONE bounded additive
    hydration (`section_title`/`parent_title`/`section_range`; zero
    extra queries when no Section won, engine fields never replaced,
    no fabrication); exit codes, read-only guarantees and the
    historical unsplit output line stay unchanged.
  - **Sync hooks (no new mechanism)**: section content converges as
    documents of the PARENT Recording through the sole
    `search_sync.schedule_recording_sync` post-commit writer (the 5B.4
    embedding sync rides the same callback, so section-summary vectors
    follow). The new/changed hooks: a REAL segmentation layout change
    schedules exactly ONE parent-recording sync INSIDE the save
    transaction (the zero-DML no-op path and a rollback schedule
    nothing; `search_sync` is imported lazily inside `segmentation` to
    keep the import DAG acyclic: `search_sync → search_index →
    segmentation`); every successful section-summary activation
    (`persist_summary`, BOTH shapes) schedules exactly ONE
    parent-recording sync; section tag writes schedule one parent sync
    ONLY when the Section's ACTIVE tag-name set actually changes
    (create/reactivate/remove; an origin-only
    `confirm_section_suggestion`, an unchanged Done and failures
    schedule nothing). No per-Section callback, writer, queue or
    daemon exists anywhere.
  - **Ask — section summaries as PROSE-ONLY evidence**: Ask now admits
    ACTIVE variants of canonical topic Sections (same SHARED predicate
    plus the cross-parent defense) alongside the unchanged ordinal-0
    whole-recording variants, with the same one-sweep/one-embedding/
    one-integrity-traversal and document-level bounds (metadata still
    never evidence). Post-chat revalidation includes the canonical
    ACTIVE section/layout ownership, so a concurrent layout change is
    the fixed sanitized concurrent-change failure, never an answer. A
    section summary's PROMPT evidence is summary PROSE only (body, else
    title) — it never falls back to `aux_text`, so its indexed tag
    names, layout titles and segment boundaries are never prompt
    evidence (unlike a whole-recording summary, which keeps the
    historical aux fallback). Citations carry the server-owned
    `section_id` (null otherwise) and reuse the EXACT existing
    summary-version route; the Ask contract is otherwise unchanged.
  - **Rebuild sequence after the upgrade**: `brain search-index
    rebuild` FIRST (materializes the version-2 mapping), THEN `brain
    embedding-index rebuild` — the old active generation carries
    `source_index_version` "1" (status reports
    `source_index_version_mismatch`; `repair` requires an EXACT
    INDEX_VERSION match, so a mismatch means rebuild) and
    semantic/hybrid/Ask fail closed until a compatible active
    generation exists. Incremental sync keeps both indexes current
    afterwards.
  - **Verification (independently confirmed, current state)**: full
    suite **3100 collected and 3100 passed** (the Step 6.2a bug-fix
    state was 2857 — historical; the 6.3 delta is **243 tests**: the
    four new Step 6.3 files `tests/test_search_index_section_
    summaries.py`, `tests/test_semantic_item_search.py`,
    `tests/test_search_fusion_items.py` and `tests/test_search_cli_items.py`
    (134 in the new files) plus 109 additions across the search-web/
    search-modes/search-CLI, ask, library-items, segmentation, section
    summarize/tags, migration and semantic regressions), only the
    known `audioop` warning; `manage.py check`,
    `makemigrations --check` (NO new migration; 0013 still the head)
    and `git diff --check` clean. No commit and no real-database
    migration is claimed; the work and its tests are in the working
    tree, uncommitted.
- Do not implement features from a later step, and do not claim
  accuracy or completion without executable verification.
