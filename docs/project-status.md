# Project status — implementation handoff (Step 5A.4.2 complete)

This file reflects the repository through Step 5A.4.2: Step 4, the
post-incident routing/transcription fixes, the multilingual summary
corrective round, the production Library UI (Step 5A.1), the search
index foundation (Step 5A.2), incremental index synchronization
(Step 5A.3), the read-only keyword search backend + CLI
(Step 5A.4.1) and the Library keyword web search in both its service
round (5A.4.2a) and its rendering/accessibility round (5A.4.2b).
It is a snapshot, not a durable instruction file; `AGENTS.md` holds
the standing rules.

## Handoff audit — 2026-09-09

A documentation-only review of the repository as of HEAD `8c215f8`
(`feat(docs): update AGENTS.md and project-status.md with stability
patch details and outline for Steps 5B–5D`) on `main`, tracking
`origin/main`. The sections below the audit remain the detailed
delivery snapshot.

**Observed facts**

- 20 commits, no Git tags or releases, and no visible CI
  configuration (no `.github/`, GitLab, or CircleCI files).
- Python 3.12 / `uv` (Hatchling build backend) / Django 5.2 LTS /
  SQLite / minimal dependencies; no frameworks beyond the approved set.
- Implementation is complete through Step 5A.4.2b (Step 4 web UI,
  5A.1 Library, 5A.2 index foundation, 5A.3 incremental sync,
  5A.4.1 keyword backend + CLI, 5A.4.2a/b Library keyword web search
  with highlights and jump links).
- The current baseline command collected 1388 tests and produced
  **1387 passed** plus the one known failure:
  `TestConvergence::test_unlocked_tag_service_race_converges` (the
  SQLite table-lock race). `manage.py check` and
  `makemigrations --check` passed.
- Observed cleanup debt (not fixed here): the unreachable `return
  None` after `return "first"` in
  `workflow/services/web_actions.py:summarize_mode`, and the noted
  `audioop` risk on Python 3.13.

**Inference / next steps**

- Immediate next step is the pre-5B bounded SQLite lock/busy retry
  patch for the documented concurrent web-tag race, then 5B embeddings
  foundation, 5C semantic/hybrid search, 5D Ask-with-citations, and
  Step 6.
- No claim is made here about the real user database's migration state
  (`0007`/`0008` application is not reported). Local config values and
  secrets are intentionally omitted from this handoff.

> The older "1388 passing" text in the Tests section below is a
> delivery snapshot from the Step 5A.4.2b round, not the current audit
> result (which is 1387 passed + the one known race failure above).

## Step 5A.4.2b — Library Search Rendering & Accessibility (delivered)

- **Snippets as fragments** (`search_web.snippet_fragments`): the ONE
  deterministic malformed-range policy — invalid snippet/text/matches
  shape renders NO snippet (non-`str` text is never coerced), a
  non-list `matches` yields one plain fragment, individually malformed
  ranges are ignored, surviving ranges are clamped/sorted/merged, and
  the fragments ALWAYS partition the exact text as exact plain `str`
  (str SUBCLASSES — trusted-wrapper objects whose `__str__` returns
  themselves, or hostile subclasses raising in `__iter__`/`__str__` —
  are rebuilt via the unbound base-str conversion, which runs NO
  subclass hook; non-str is never coerced). The template
  wraps `mark=True` pieces in semantic `<mark>` under autoescaping
  only: `mark_safe`/`SafeString`/generated HTML are forbidden here
  (contract-scanned by tests). The engine snippet builder is untouched.
- **Segment jump links**: the provenance chip links to the
  active-transcript page carrying the indexed ordinal
  (`?page=ordinal//segments_per_page+1#segment-<ordinal>`, 0-based
  ordinals, `web.transcript_segments_per_page` threaded from config via
  `run_web_search(segments_per_page=…)`). ONE bounded batch SELECT per
  page validates the `(transcript_id, recording_id)` PAIR: the
  Transcript pk (integer BigAutoField — strict positive-int
  validation; Recording pks stay UUID strings) must be active AND
  owned by the result's own Recording. Inactive, missing,
  foreign-owned or malformed provenance keeps the plain non-link chip
  — never a guessed link, never a 500. URL fragments are browser-side:
  tests assert the generated href AND separately GET the path+query to
  prove the target page carries `id="segment-<ordinal>"` (the ONLY
  transcript-markup change).
- **Parity & presentation**: `_search_snippet.html` (fragment loop) and
  `_search_provenance.html` (chip/span-or-link) are the shared
  partials; Card and Table keep their own wrappers/cells and show
  identical marks and hrefs (identical engine results guarantee
  identical rendering). Search results header gained a static
  `role="status"`; the page-frame polite `aria-live` container stays
  the ONLY live region (no per-row announcements). Restrained external
  CSS only: `mark`, `a.match-chip`, `.transcript-segment:target`
  landing state + `scroll-margin-top`, mobile stacked-cell wrapping;
  CSP middleware untouched; search GETs remain strictly read-only
  (the link-validation SELECT is constant-cost, proven by the
  existing no-N+1 page-cost equalities plus a 4-vs-1 link-count test).

## Step 5A.4.2a — Library Keyword Web Search (delivered)

- **Service** `workflow/services/search_web.py` — the ONE home of the
  Library search flow, strictly read-only: validate → FULL health
  sweep EXACTLY once per submitted search GET (`preflight_full_health`;
  NO health cache — safe cross-layer invalidation is not provable) →
  scoped engine call → search-aware sorting → pagination → ONE
  prefetch-contracted card fetch for the page window. Blank/whitespace
  `q` is the normal Library (no validation, no gate, no engine).
- **Engine scope** (the one real engine change):
  `search_recordings(..., scope=<Recording QuerySet>)`. The engine
  validates model/unsliced, clears caller ordering, forces a
  single-column PK selection and compiles on the same DB alias
  (`_compile_scope`) — callers pass QuerySets, never SQL. The
  predicate lands in the INNERMOST matched-set WHERE, BEFORE every
  window function, so ranking, both bounds, `truncated` and
  `more_recordings_matched` are computed over the in-scope population
  only (proven against brute-force ground truth and the 240-flood
  regression: the lower-ranked in-scope match is never starved;
  default-bound unscoped runs legitimately answer "top 200 of 241,
  truncated=false, 41 more" while the scoped run sees only its own
  matches). `scope=None` is byte-identical to the 5A.4.1 engine.
- **Sorting contract**: search mode parses sorts with
  `allow_relevance=True` — `relevance` (engine comparator order,
  NEVER a DB ORDER BY) is the default AND the structured fallback for
  invalid sorts via `ListFilters.sort_error` (a bad sort keeps every
  valid filter; only invalid SCOPE filters fall back to a labelled
  unscoped search). `ListFilters` gained `sort_default`/`sort_error`/
  `scope_valid`; `apply_filters` split into `filter_only` +
  `apply_sort` so `search_scope_queryset` reuses the EXACT Library
  filter predicates; `as_querystring()` omits the sort iff it equals
  the mode default.
- **States**: `ok` (zero results included; query echoed only via
  autoescaping), `invalid` (fixed bound messages) and `index`
  (sanitized `search-index status`/`rebuild` guidance). invalid and
  index states set `echo_allowed=False` — the rejected text appears
  NOWHERE in the response (proven by canary tests).
- **Persistence without JavaScript**: the top-bar form submits `q`;
  the filter form carries a hidden `q`; pagination/view-toggle/
  filter links carry the merged `base_qs` (q + filters + view).
  "Clear search" drops only `q`; "Clear all" drops everything.
- **Purity & cost**: GET-only SELECT/PRAGMA (CaptureQueriesContext)
  with raise-guards on sync/rebuild/lock; identical query totals for
  40 vs 5 results and page 1 vs page 2 (no N+1); zero-result pages
  skip the row fetch. No new URLs, config keys, migrations or schema.
- **Scope compilation (review rounds 2–3)**: an EMPTY scope is VALID
  in EVERY empty-query form — `Recording.objects.none()` (flagged) and
  `filter(pk__in=[])` (compiler-proven only): `as_sql()` raises
  `django.core.exceptions.EmptyResultSet`, which is caught and answered
  with the same provably-empty subquery through the normal zero-result
  path, never `EmptyResultSet` leaking and never the sanitized error
  (the private `query.is_empty()` pre-check was removed — the compiler
  is the single emptiness authority). Every other compilation failure
  maps to the sanitized `SearchIndexError(_QUERY_FAILED_ERROR) from
  None`: SQL, params, paths, indexed content, the query and the
  underlying exception text never escape the message or the formatted
  traceback (proven with sentinel texts injected at the Django
  compiler seam).
- **Note fidelity (review round 2)**: `NOTE_SORT_WINDOW` accompanies
  non-relevance sorting ONLY when the returned winner set is known
  incomplete (`more_recordings_matched > 0` or `None`); pure
  candidate-bound truncation with the full winner set present shows
  only the truncation note.
- **Logging privacy (review round 2)**: caplog-tested (DEBUG on the
  `workflow` tree) — the query canary appears in NO log record for
  invalid-query, fts_missing/broken/stale and sanitized engine-failure
  states; successful pages echo the query only through template
  autoescaping.
- **Delivered later in Step 5A.4.2b** (see the section above):
  `<mark>` highlight fragments, segment jump links and the
  styling/a11y polish. **Still NOT implemented (Step 5B)**:
  Semantic/Hybrid controls (FORBIDDEN — tests assert their absence),
  any health cache or background maintenance.

## Step 5A.4.1 — Keyword Search Backend + CLI (delivered)

- **Service** `workflow/services/search_query.py` — a strictly
  READ-ONLY query engine over the 5A.2/5A.3 index: SELECTs only, never
  takes the pipeline lock, never rebuilds, repairs, synchronizes or
  writes (proven: forbidden lock/rebuild/sync patches, a
  CaptureQueriesContext that only ever sees SELECT/PRAGMA, and an
  unchanged `build_status_report` after searches). No schema change —
  all queries read the existing registry + `workflow_search_fts`.
- **Two explicit health layers** (so 5A.4.2 can later plug a safe
  cached-health policy into interactive web search without touching
  the engine):
  - `preflight_full_health()` runs the FULL read-only
    `build_status_report()` EXACTLY once; ANY unhealthy category
    hard-fails (no results) with a stable message naming the stable
    categories (registry / `fts_missing` / `fts_broken:<sub>` /
    stale list) plus `search-index status` / `rebuild` — never query
    text, indexed content, keys, paths or SQL. `brain search` calls it
    once; test-proven call-count 1.
  - `search_recordings()` NEVER runs the sweep (test patches
    `build_status_report` to raise and the engine still answers); it
    only STRUCTURALLY checks queryability (registry table, FTS
    schema/tokenizer via the 5A.2 inspector, SQLite window-function
    support) and maps query-time SQL failures to fixed sanitized
    errors. It therefore serves stale indexes by design — honesty is
    the caller-policy layer's job.
- **Plain-text query contract** (raw MATCH syntax is never reachable):
  NFC + outer strip, split into whitespace-separated LITERAL terms
  combined with AND at document level; `"`, `%`, `_`, `\`, `*`, `AND`,
  `NEAR(...)` are user text (3+ codepoints → ONE quoted FTS phrase per
  term with `"` doubled; 1–2 codepoints — impossible for the trigram
  tokenizer, short CJK included — → escaped Unicode-aware LIKE with
  `ESCAPE '\'` after `\`/`%`/`_` escaping; mixed short/long queries
  AND both predicate kinds). Unicode folding is ONE contract:
  `sqlite_unicode.fold_text` (NFC + per-codepoint casefold) is both
  the Python ranking/snippet fold and the `brain_fold` SQL function,
  so selection and highlight offsets can never disagree on Unicode
  semantics. Documented asymmetry: the trigram tokenizer is
  case-insensitive but does not fold diacritics
  (`kaytossa` ≠ `käytössä`), and its case folding differs from
  `fold_text` only on rare case-expansion pairs.
- **Deterministic bounded selection** (never FTS rowid, never natural
  order): a registry JOIN ranks candidates with `ROW_NUMBER() OVER
  (PARTITION BY recording_id ORDER BY <doc-type priority
  summary<recording<segment>, document_key)` — the per-recording bound
  therefore can NEVER evict the higher-priority Summary/metadata
  candidate a segment flood would otherwise push out by document-key
  order (regression-proven), and a global + PER-RECORDING bound keeps
  floods from starving other recordings (both proven against corpora
  larger than the caps). `truncated` is TRUE for ANY overflow of
  EITHER bound — detected from exact `COUNT(*) OVER ()` /
  `COUNT(*) OVER (PARTITION BY recording_id)` window counts, never
  inferred from kept-row counts. Guarantee under `truncated=true`: the
  matched-Recording set and per-Recording ranking stay exact while the
  global fetch was not cut; a per-recording-trimmed Recording's best
  document is the best of its bounded priority prefix, and if the
  global fetch WAS cut whole Recordings may be missing.
  `more_recordings_matched` is EXACT while the fetch was not cut and
  `null` (unknown) when the global bound cut rows — never guessed.
  `document_key` is rebuild-stable; identical payloads across rebuilds
  are test-proven.
- **Dedup + ranking**: best document per Recording; comparator
  (worst satisfied field rank title<body<aux, doc-type rank
  summary<recording<segment, fold-occurrences desc, first offset,
  document_key). A row selected by SQLite but not locatable by the
  Python fold (pathological divergence) is kept, ranked last and never
  highlighted wrongly (unit-proven).
- **Snippets**: plain text + offset ranges (never HTML). The window
  is anchored on the FIRST INDIVIDUAL mapped match — never on a
  globally merged range (repeated text and tiling multi-term matches
  chain into unbounded merges, which once produced over-cap
  snippets); displayed ranges are clipped to the window and merged
  ONLY among themselves, so every returned range is non-empty,
  ordered, non-overlapping and inside the returned text, and `…`
  ellipsis markers are included in the returned text with offsets
  relative to it. The content window is HARD-capped at
  `SNIPPET_MAX_CODEPOINTS` UNCONDITIONALLY (returned text ≤ cap + 2
  ellipsis characters; no oversized defensive fallback exists): the
  anchor is fully visible whenever it fits the cap, and a
  pathological anchor that does not fit is shown CLIPPED from its
  start — the bound wins. The window clamps at both text ends and
  shifts left near the end of the text. Proven: 256-codepoint term
  mid-text and at the tail, 1000 repeated characters, two tiling
  terms chained past the cap, a match clipped at the window boundary
  (stays non-empty and highlighted), a synthetic over-cap anchor (cap
  beats full-match visibility), plus the exact inclusive
  start/end source-character fold→NFC mapping for casefold
  EXPANSIONS (`ﬃ`→`ffi`, `ß`→`ss` yield whole-source-character ranges,
  never zero-length slices — the old `spans[end]` endpoint mapping
  produced zero-length ranges there), Latin diacritics, CJK and
  astral emoji.
- **Provenance** per result: `metadata` | `summary` (+`output_language`
  — language variants are separate documents; the best variant wins
  the dedup) | `segment` (+ordinal/start/end), plus the matched fields
  and occurrence counts. The Library display title (canonical
  recording metadata document) rides along; `brain search` reads it,
  the 5A.4.2 web layer can link with the recording id.
- **brain_fold registration is search-specific and failure-isolated**
  (`workflow/sqlite_unicode.py`): the connection signal registers the
  collation FIRST (unchanged contract) and the fold function only
  best-effort — a fold failure can never break pages, ORM access or
  the Title collation; it resurfaces ONLY at search use-time as the
  separate `ensure_fold_function` error (idempotent retry, sanitized,
  collation-failure messages never relabeled). MATCH-only searches
  never require it at all. Test-proven for first connection,
  reconnect, idempotency and isolation.
- **CLI**: `brain search "QUERY" [--limit N] [--json]` — read-only
  (schema preflight, no lock). Order: cheap input validation
  (`validate_query`: empty/256-codepoint/8-term/limit 1–200 bounds —
  exit 2, BEFORE the sweep) → full health gate EXACTLY once (exit 1
  clean stderr for missing/broken/stale) → engine (exit 0 even with
  zero results). Human output: numbered results, provenance labels
  (`[summary · en]`, `[segment 3 @ 1:15]`, `[metadata]`),
  `«highlight»`-decorated bounded snippets, truthful truncation/more
  notes; `--json` = the structured payload (query echo is the
  normalized query only — no raw text in errors).
- **Review round 1 corrections** (three findings, all regression-
  proven): hidden per-recording truncation (priority-aware window
  ordering + exact COUNT-based overflow detection), zero-length
  highlight ranges at casefold-expansion endpoints (exact inclusive
  start/end source-character spans), and the unstretched snippet
  window (hard cap with left-shift instead of extension).
- **Review round 2 correction** (regression-proven): the snippet
  window must anchor on the FIRST INDIVIDUAL mapped match, not on a
  globally merged range — repeated text (1000×`a` under `a`) and
  tiling terms chain adjacent matches into one over-cap merge that
  the defensive branch then emitted whole; displayed ranges are now
  clipped into the window and merged only among themselves, and the
  defensive over-cap fallback now caps the window (clip) instead of
  bypassing it.
- **Verification**: full suite **1261 passed** (+92 over the 1169
  baseline: 86 in the two new search files, 6 in
  `test_sqlite_unicode.py` for fold registration isolation),
  `manage.py check`, `makemigrations --check` clean (NO new
  migration), `git diff --check` clean. No real audio, network,
  MacWhisper or oMLX; `config/config.yaml` and the real `data/`
  untouched.
- **Known pre-existing flake** (not introduced here, reproduced
  ~1-in-6 on the pristine baseline):
  `tests/test_search_index_sync.py::TestConvergence::test_unlocked_tag_service_race_converges`
  is a genuine thread race against SQLite write serialization and may
  fail sporadically in any run.
- **Deliberately NOT implemented**: web search (subsequently delivered
  as Step 5A.4.2a — see the section above),
  ranking beyond the deterministic comparator, stemming/word
  tokenization, embeddings/semantic/hybrid search,
  Ask-with-citations. At that round the Library search field was
  still the disabled placeholder.

## Step 5A.3 — Incremental Search Index Synchronization (delivered)

- **Service** `workflow/services/search_sync.py` — the ONLY incremental
  writer, with exactly two entry points:
  `schedule_recording_sync(ids)` (the only trigger; called INSIDE
  mutating services' transactions, runs via `transaction.on_commit` —
  immediate under autocommit, NEVER on GET; ids deduplicated per call)
  and `reconcile_recording(recording_id)` (the one per-recording
  writer; idempotent; returns counts only: inserted / updated /
  fts_repaired / deleted / skipped / recording_missing).
- `reconcile_recording` derives truth FROM THE DATABASE inside ONE
  transaction using the SHARED 5A.2 builders
  (`iter_expected_documents_for_recording_ids` — the same
  `_expected_documents_for_rows` mapping as the global sweep, never a
  forked mapping) — never trusting caller declarations. Phase 1 deletes
  every non-canonical registry row of that recording (and its FTS row,
  FTS-first) using the SHARED canonical-row validation, which this
  round strengthened: segment rows must match the ACTIVE transcript's
  OWN recording, summary rows must agree on recording, transcript AND
  output_language — cross-recording / mismatched-provenance forgeries
  are rejected (and reported `orphan_document`) by status and the
  reconciler alike. Phase 2 upserts expected docs with a FULL-field
  comparison (all registry fields + the exact FTS text read back;
  `content_hash` alone is NEVER trusted — a tampered field carrying the
  old hash is still repaired, proven for `index_version`, provenance
  and title tampering).
- **Explicit FTS row states** (never ambiguous): registry+FTS correct
  ⇒ skip; text differs ⇒ UPDATE the existing rowid; FTS row MISSING ⇒
  INSERT with the EXISTING registry pk as rowid (proven by spying that
  the missing row goes through INSERT statements only, never UPDATE);
  registry row missing ⇒ create it, then INSERT its FTS row.
- **Self-healing boundary (proven by two distinct tests)**: reconcile
  repairs everything attributable to the recording, but an FTS row
  whose registry row is gone (direct registry deletion, or an
  authoritative `Summary` delete whose FK CASCADE removed the registry
  row) has NO attribution from FTS alone — it is NEVER removed per
  recording; both scenarios converge to healthy only via
  `brain search-index rebuild` (the authoritative repair), with
  `orphan_fts_row` detecting them meanwhile.
- **Hook coverage** (`schedule_recording_sync` at every authoritative
  commit that changes indexed content): `transcription._persist_transcript`;
  `summarize.persist_summary`, the Explicit-Original DETECTION save, the
  late post-persistence source-language save, and
  `set_transcript_language`; `pipeline._apply_outcome`, `manual_route`,
  `confirm_routing`, `_mark_source_missing`,
  `_recalculate_recording_sources` and the DIRECT canonical-promotion
  branch of `validate_source_for_processing`; `tags.add_manual_tag` and
  `remove_tag` (NOT `confirm_suggestion` — an origin-only change does
  not affect indexed content); `ingest._attach_hashed_source`,
  `_detach_for_rehash` and the reappear branch of `ingest()`.
  Rolled-back transactions produce no sync (proven with
  `set_rollback(True)`). Rollback contract proven end-to-end: the
  early detection save syncs the metadata title chain even when the
  later generation attempt fails (seeded en + zh-Hant variants;
  detection `yue` flips the derived default; generation failure leaves
  the index healthy).
- **Failure contract (proven)**: reconcile failures roll back their own
  transaction completely (no half-written registry/FTS pair); a sync
  failure NEVER escapes into the CLI or web request flow (proven via
  CLI exit code 0 with sync failing, and web tag POST 302 with sync
  failing); recordings inside one callback fail INDEPENDENTLY; the ONLY
  log is one fixed aggregate warning per callback:
  `search index post-commit sync failed category=search_index_sync_failed count=%d`
  — a count and nothing else (no ids, exception text, paths, SQL or
  indexed content; proven by matching the exact captured log line).
  `search-index status` remains the detection mechanism and `rebuild`
  the repair; the index stays detectably stale and converges on the
  next successful sync (proven; no automatic-retry pretending — each
  commit gets its own sync, deliberately never coalesced across
  commits).
- **Concurrency truth, no magic**: pipeline/web-action commits happen
  under the flock so their hooks run locked; web tag edits have NO
  flock and rely on SQLite writer serialization. A real, baseline-
  reproducible race remains in concurrent tag mutations:
  `TestConvergence::test_unlocked_tag_service_race_converges` can fail
  with `database table is locked: workflow_tagassignment` (observed
  roughly 1-in-6 runs). Fix this in a dedicated bounded-retry stability
  patch before Step 5B; do not describe the path as proven to converge
  until that test is reliably green. `reconcile_recording` never
  registers a sync (no recursion) and never takes the pipeline lock.
- **Bounded**: per-recording streaming reuses the 5A.2 chunk bound (one
  recording with 1200 segments syncs in ≤ 100-row pages, proven by
  spying INSERT batch sizes); per-recording spec sets equal the rebuild
  sweep's (parity test).
- **Verification**: full suite **1169 passed** (+37 over the 1132
  baseline: 33 in `tests/test_search_index_sync.py`, 4 web tests in
  `tests/test_web_tags.py` — including GET-purity guards that patch the
  bound `schedule_recording_sync` in every hooked service),
  `manage.py check`, `makemigrations --check` clean (NO new migration —
  5A.3 changes no schema), `git diff --check` clean. No real audio,
  network, MacWhisper or oMLX; `config/config.yaml` and the real
  `data/` untouched.

## Step 5A.2 — Search Index Foundation (delivered, review-corrected)

- **Schema (migration 0008, fully reversible)**: relational registry
  `workflow_search_document` (`SearchDocument` model: `document_key`
  globally unique — `recording:<id>`, `segment:<transcript_id>:<ordinal>`,
  `summary:<id>`; `content_hash` + `index_version`; exact searchable
  title/body/aux text columns; provenance FKs `recording` (CASCADE) /
  `transcript` (nullable, CASCADE) / `summary` (nullable, CASCADE) —
  **no section FK**; DB CheckConstraints: `doc_type` limited to
  `segment|summary|recording` (`chk_search_doc_type_allowed`), summary
  documents require a non-empty `output_language`
  (`chk_search_doc_summary_output_language`), per-type column-shape
  checks, hash-field presence; per-type partial uniques; db_table pinned
  to `workflow_search_document`) + one **contentful** FTS5 table
  `workflow_search_fts` (`rowid` = registry pk — a plain integer pk,
  `title_text`/`body_text`/`aux_text`, `tokenize='trigram'`; the table
  stores its own text; no `content=''`, no external content). PK/rowid
  are internal and may change between rebuilds; only `document_key` is
  stable.
- **Migration 0008** (`0008_search_index.py`): separate-connection
  in-memory FTS5+trigram probe FIRST (failure → fixed
  `RuntimeError(PROBE_ERROR)`: "keyword search indexing requires SQLite
  FTS5 with the trigram tokenizer; this Python SQLite build does not
  provide it", original exception never shown); `CreateModel`
  (Django-managed, reversible, constraints byte-identical to the model —
  enforced by `makemigrations --check`); raw DDL create/drop of the FTS
  table (conversion with `raise ... from None`, fixed `DDL_ERROR`, no
  path/SQL echo); `RunPython` backfill/unbackfill. Backfill STREAMS:
  documents are buffered to `INSERT_CHUNK_SIZE` (500) and flushed
  immediately — bounded memory even for one recording with millions of
  segments — in exactly the service's global order, mirroring the
  service **byte-identically** (NFC, framing hash, body/aux composition,
  display title, filenames, tags, default-language policy) —
  parity-tested against `rebuild_index()` (same corpus, same hashes).
  Forward and reverse run under the default `MigrationExecutor`
  atomicity; reverse leaves zero `workflow_search*` tables and no
  source-table changes.
- **Service** `workflow/services/search_index.py` (`SearchIndexError`
  subclasses `brainlib.config.ConfigError` → safe stderr/exit 1):
  `INDEX_VERSION="1"`; content hash over length-prefixed UTF-8 framing
  of index version, doc type, key, provenance IDs,
  ordinal/start/end/output-language/exact texts (IDs stringified only in
  framing — stored FK columns keep their raw values); NFC before hashing
  and FTS insertion.
  `iter_expected_document_batches()` streams **fixed-size document
  chunks** (`chunk_size` = `INSERT_CHUNK_SIZE` = 500; `batch_size` = 100
  Recordings per deterministic PK sweep; bounded queries per sweep, no
  N+1, no unbounded accumulation). Canonical mappings:
  - `segment` (every non-empty-by-`strip()` segment of the ACTIVE
    Transcript; stored text NOT stripped): title = EMPTY (the Recording
    metadata doc owns the searchable title), body = the COMPLETE segment
    text (NFC, internal whitespace/line boundaries preserved), aux = the
    speaker or empty; ordinal/start/end are registry+hash provenance
    only, never FTS text;
  - `summary` (current whole-recording variants: `Summary.is_active` ∧
    `transcript.is_active` ∧ `section__ordinal=0` — a query eligibility
    filter, not a SearchDocument FK): title = the Summary title, body =
    overview + key-point texts + action-item texts (model order,
    newline-joined), aux = people/organizations/topics values; legacy
    `output_language="und"` variants stay as stored and are counted as
    `legacy_und_variants`;
  - `recording` (one per Recording): title = the Library display-title
    chain, body = deterministic source FILENAMES only (never paths),
    aux = sorted active tag names; never secrets, diagnostics or
    rejected model suggestions.
- **`rebuild_index()`**: ONE atomic transaction — registry-schema
  validation → DROP/CREATE derived FTS table → streamed bounded-batch
  inserts (bulk_create + `executemany`, each ≤ 500) → complete
  in-transaction verification (exact registry/FTS counts, bidirectional
  rowid↔registry join with zero gaps, per-row content-hash recomputation
  and byte-exact FTS↔registry text comparison, streamed in 500-row
  pages) → commit. Any failure rolls back; the previous index stays
  byte-identical (proven by injected mid-backfill and post-DDL failures
  with snapshot comparison). Missing / wrong-schema / wrong-tokenizer
  FTS tables are repaired by a rebuild (proven by direct-table surgery).
- **`build_status_report()`**: strictly read-only (guarded at the
  connection level; never creates/repairs); bounded memory (per-chunk
  `document_key__in` registry lookups; keyset-paged registry-orphan and
  FTS-orphan sweeps whose first page is unbounded so zero/negative
  explicit ids and rowids are never skipped). Compares authoritative source data, registry AND
  actual FTS rows (not hashes alone). Stable categories:
  `registry_schema_missing`, `fts_missing`, `fts_broken` (with
  `fts_columns`/`fts_tokenizer` sub-diagnostics in the report's
  `fts.category`), `missing_from_registry`, `orphan_document`,
  `missing_from_fts`, `orphan_fts_row`, `content_mismatch`,
  `stale_content`, `version_mismatch`; identifier lists capped at
  `STATUS_KEY_LIMIT`=20 with `keys_truncated` reporting the TRUE omitted
  count (orphan FTS counts themselves are exact, proven with 25
  orphans), never indexed text; health recomputed from categories.
- **CLI**: `brain search-index status [--json]` (read-only, NEVER locks;
  exit 0 only when fully healthy, else 1) and `brain search-index
  rebuild [--json]` (`_pipeline_command`: schema preflight → lock →
  recover → rebuild; exit 3 on lock contention; 2 on usage). JSON
  reports categories/counts/keys only. `brain doctor`'s FTS check now
  probes trigram (`PASS` detail "available (trigram)"; WARN semantics
  unchanged — 5A.2 needs no embedding-model gate).
- **Shared Library metadata helpers**: new pure `workflow/services/library_metadata.py`
  (`display_title_from_recording`, `display_title_from_parts`,
  `summary_title_parts`, `preferred_source_filename`,
  `deterministic_source_filenames`, `active_tag_names`,
  `source_sort_key`, `TITLE_PLACEHOLDER`); `workflow/query.py`
  `RecordingCard.title` fallback and the migration backfill share this
  semantic contract; `workflow/query.py`'s SQL annotations stay
  authoritative for the Library page.
- **Query bounds**: rebuild and status issue identical READ patterns for
  small and 15× corpora (proven 2 vs 32 recordings in one sweep); writes
  are always bounded batches (test proves ≤ 1 INSERT statement per 20
  documents). The streaming bound is proven both ways: the generator
  never yields more than 500 docs and the migration never flushes more
  than 500 per INSERT batch, each for ONE recording with 1200 segments,
  with complete coverage and deterministic order asserted.
- **Verification**: full suite **1132 passed** (72 new: 47 service, 12
  migration-executor forward/reverse/atomicity/probe/streaming, 13 CLI;
  plus the migration-readiness incident-mirror list extended to the new
  leaf), `manage.py check` clean, `makemigrations --check` clean, no
  whitespace errors. Real `data/database/brain.sqlite3` untouched
  (verified read-only again after this round; still at 0007, migration
  0008 never applied to it); no real audio, network, MacWhisper or oMLX
  use; `config/config.yaml` never modified.
- **Review corrections applied in place** (this round): genuine
  chunk-streaming in generator + migration backfill (previously
  accumulated all docs per sweep before chunking inserts); exact
  `orphan_fts_row` totals beyond the key cap (previously capped by the
  probe query's `LIMIT`); `doc_type` and summary-`output_language` DB
  CheckConstraints (model + migration 0008 mirror); both keyset sweeps
  now start with an UNBOUNDED first page and continue with
  `> last_seen` (SQLite/FTS5 accept zero and negative explicit
  ids/rowids — a `> 0` start could have hidden corrupted rows behind a
  healthy report; proven with forged pks/rowids at `-1`/`0` and
  forced multi-page traversal); this section rewritten against the
  actual code.
- **Deliberately NOT implemented** (as of 5A.2): query
  parsing/ranking/highlighting/`brain search <query>` (Step 5A.4), web
  search UI, embeddings. The Library search field remains the disabled
  placeholder. Incremental synchronization arrived with Step 5A.3 (see
  the section above).

## Step 5A.1 — Production Library UI (delivered)

- **Routing**: `/` redirects to the Library (`/recordings/`); the
  diagnostics/status page moved to `/status/`; `/review/` and
  `/health/` unchanged.
- **Library**: server-rendered, no-JS Card and Table views. Only the
  selected representation is rendered (one DOM; the same table stacks
  into readable rows on small screens via CSS `data-label`). Two-row
  filters (From/To/Sort in row 1, Tags in row 2; no FILTER caption, no
  Day/status/audio/has-summary/review/tag-match controls). Sort by
  Newest/Oldest/Title A–Z/Z–A with sort-aware month headings. Search is
  a disabled "Search coming soon" placeholder — no FTS or real search
  exists yet.
- **Title contract**: one annotated `display_title` (active
  default-language whole-recording Summary → deterministic active
  Summary → canonical AudioSource filename → placeholder) drives both
  rendering and Title ordering. Title sorting is Unicode-aware
  (NFC + casefold) via a SQLite collation registered on every connection
  (`workflow/sqlite_unicode.py`), database-side before pagination, PK
  tie-break. The card adapter exposes a single `display_summary` (the
  prefetched Summary matching the derived default output language) so
  title and overview always share a language.
- **View preference**: server-owned `brain_view_pref` cookie
  (HttpOnly, SameSite=Lax, 1 year) with explicit validated `view=`
  precedence; the filter form carries a hidden effective-view field so
  Card/Table survives filtering without cookies; "Clear all" clears
  filters/sort but preserves the effective view.
- **Top bar**: Brain/Library, disabled search field, Review with a
  distinct-recordings count badge (one guarded COUNT DISTINCT query in
  `workflow/context_processors.py`), Status.
- **Language policy**: `CHINESE_FAMILY_PRIMARIES` is the single source
  in `languages.py`; `langresolve.default_output_language_expression()`
  mirrors `resolve_default_language()` in SQL (parity-tested) and is
  used by the Library without per-recording queries.
- No migrations, no new dependencies, strict CSP preserved. **Step 4 is
  complete; Step 5A.1 is complete after the corrective pass.** Step
  5A.2 Search Index Foundation has since been delivered (see the
  section at the top).

## Multilingual summary variants (corrective round — delivered)

The multilingual-summary work was reassigned and corrected; the previous
round's completion claims did not match repository evidence. What is now
in place (all verified by genuine workflow tests):

- **Language policy** (`workflow/services/languages.py`): single
  canonicalization — BCP-47 canonical casing (primary lowercase, script
  Titlecase, region uppercase; e.g. `en`, `en-US`, `zh-HK`, `zh-Hant`);
  Chinese family `zh|yue|cmn[*]` → `zh-Hant` for default AND original
  output; malformed values never persisted. `workflow/services/langresolve.py`
  holds default/original resolution; `summarize` re-exports for
  compatibility. The migration 0007 keeps a documented local copy that
  mirrors these semantics for historical models.
- **Variant-state reconciler** (`workflow/services/variant_state.py`):
  the ONLY writer of `SummaryVariantState` and the Recording-level
  default summary tuple. Identity/event-driven: derives truth from the
  DB inside one transaction (locks the Recording row, verifies
  transcript∈recording and section∈transcript, queries the exact active
  Summary and the newest exact matching finished failed attempt).
  `current` ⇔ active Summary for the exact scope; `failed` ⇔ no active
  Summary + matching failure; `regeneration_failed` requires a matching
  failure with attempt ordinal NEWER than the Summary's attempt
  (ordinal, not wall-clock). Recording-level fields update only when
  the variant is the currently derived default of the ACTIVE
  transcript's ordinal-0 section.
- **Recovery** (`recover_interruptions`): exact-scope only. Detection
  attempts (`language_detection`) and provenance-less legacy attempts
  change NOTHING (stable diagnostics `detection_attempts` /
  `legacy_scope_unknown` in the recovery report); forged provenance
  (foreign transcript/section) is rejected without writes. Idempotent.
- **Manual source-language correction** (`brain transcript-language
  ID --set CODE`): one outer pipeline lock (no nesting), atomic, full
  state matrix incl. `current + regeneration_failed` (recency by
  attempt ordinal). Contention exits 3 without mutation.
- **Detection**: bounded (max 2 calls; invalid output retries once;
  endpoint/HTTP/timeout/request-too-large/response-too-large never
  retry); `request_too_large` is a distinct durable category
  (`input_too_large` outcome), never conflated with invalid output;
  complete safe provenance (`transcript_id`, `section_id`,
  `requested: "original"`, `language_detection: true`), no fake
  resolved language, no content/secrets.
- **Generation selectors vs read selectors**: generation accepts only
  `default`, `original`, `en`, `zh-Hant`. Read/display/export
  additionally accept concrete languages that already exist. The
  view-model derives each tab's `action_selector`: a Finnish tab
  regenerates via `original` (redirect returns to the `fi` tab through
  a separately validated `return_language`); concrete tabs no selector
  can produce are read-only (no action). Unknown concrete languages →
  friendly 404 (pages and exports); exports never silently fall back
  to the default language.
- **Final-payload source language**: `validate_final_payload`
  canonicalizes the model-reported source language through the
  language policy; empty is allowed only as the unknown-source case;
  malformed codes raise a stable schema-validation error (the normal
  invalid-output retry applies). `Summary.language` and
  `Transcript.language_observed` receive the same canonical value.
- **Reconciler identity validation**: the reconciler rejects
  malformed, noncanonical, source-style Chinese (`yue`, `zh-HK`, …)
  and `und` output identities with a stable scope error and zero
  writes; recovery surfaces a distinct `invalid_output_identity`
  diagnostic. The reconciler is the single runtime writer of summary
  lifecycle state; the one initialization exception is the
  transcription-activation hook setting the new transcript's default
  `summary_status=missing`.
- **Detection categories surfaced**: `_detect_source_language_with_attempt`
  returns a structured result; `summarize_one` returns the durable
  attempt's actual stable category (`endpoint_unavailable`, `timeout`,
  `http_error`, `request_too_large`, `response_too_large`,
  `source_language_unknown`) — verified end-to-end through
  `summarize_one` with mocked LLM calls.
- **Web** (`workflow/services/variant_view.py`): one read-only variant
  view-model consumed by detail, summary, exports and the POST action
  layer — requested selector vs resolved language, selected Summary +
  variant state + action mode + derived generation selector, unresolved-
  Original status, all tab options (Default/English/Traditional
  Chinese/Original + existing concrete variants such as Finnish).
  Language preserved through confirmation and POST→redirect→GET;
  actions return to their origin page via a server-owned
  `return_view` allowlist token (`detail | summary`); the action-state
  fingerprint binds every language-resolution input (transcript id,
  canonical source language + verifier, resolved default and Original
  outputs with an explicit unresolved marker), so a source-language
  correction invalidates rendered confirmations even with no new
  attempt and an unchanged action mode; summary history shows output
  language; GET remains strictly read-only (test-proven).
- **Source-language provenance**: one deterministic rule in
  `validate_final_payload(…, source_language=…)` — a known canonical
  Transcript source is authoritative for `Summary.language` (the
  model's empty/contradictory/malformed answer is ignored); a genuinely
  unknown source accepts a canonicalized model value (empty stays the
  unknown case; malformed → stable schema-validation error with the
  normal invalid-output retry). `Summary.language` and
  `Transcript.language_observed` always agree (test-proven end to
  end, including after Original detection).
- **Migration 0007** (repaired in place, no 0008): invariant validation
  FIRST (corrupted legacy data fails clearly and atomically — no
  winner selection, no historical-row deactivation); constraint swap;
  data backfill LAST; reversible-check via `Migration.unapply`
  pre-check proves `IrreversibleError` before any mutation. Covered by
  genuine `MigrationExecutor` tests on isolated per-test SQLite
  databases (historical 0006 fixtures, forward + reverse).
- Tests: 985 passing (final corrective pass: +19 focused regression
  tests binding the confirmation fingerprint to language resolution,
  authoritative source-language provenance, and validated return-view
  redirects). `manage.py check`,
  `makemigrations --check`, `git diff --check` clean.

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

- Pages at the time: dashboard `/` (existing status page — since moved
  to `/status/`, with `/` now redirecting to `/recordings/`), `/recordings/`
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

## Web behaviour (minimal) — historical snapshot (Step 1)

At that time `/` was the status page (version, storage paths, MacWhisper
presence without spawning, oMLX config without network calls, pipeline
counts) and `/health/` was sanitized JSON (200 ok/degraded, 503
unhealthy), with "full UI = Step 4".

**Current routing (see the top-of-file handoff):** `/` redirects to the
Library `/recordings/`, the diagnostics/status page lives at
`/status/`, and the full web interface (Step 4) plus the Step 5A.1
Production Library UI are delivered.

## Tests and verification status

- Current: **1388 tests passing** (Step 5A.4.2b search rendering &
  accessibility: +59 over the 1329 baseline — fragment-policy units,
  engine-real `<mark>` renders (CJK, emoji, `ﬃ`/`ß` casefold
  expansions), split XSS coverage (escaped query echo AND escaped
  indexed payload while valid marks render), pair-validated jump-link
  pages incl. malformed transcript-id/ordinal and cross-recording
  regressions, boundary anchors proven by fetching the path+query
   without the fragment, Card/Table parity, batch-cost, CSP and
   accessibility tests; +9 SafeString-hardening regressions in the
   review round — a trusted-wrapper str (whose ``__str__`` returns the
   SAME object) and a hostile subclass (``__iter__``/``__str__`` raise)
   through every ``matches`` shape, asserting the builder never raises,
   every fragment is exact built-in ``str`` with content preserved, and
   both the plain and marked render paths stay fully autoescaped, plus
   a production scan banning safe-string machinery in the service);
   Step 5A.4.2a Library keyword web
  search: +68 over the 1261 baseline — 9 engine-scope cases incl. the
  240-flood ground-truth regression + 43 web-search cases in the first
  round; +3 engine and +11 web cases in the review round 2: empty /
  sanitized scope compilation, note fidelity, caplog privacy; +1
  engine and +1 web case in the final round: compiler-discovered
  empty scope `filter(pk__in=[])`); Step
  5A.4.1 keyword search backend +
  CLI + snippet-bound review rounds: +92 over the 1169 baseline); earlier snapshots
  recorded 495 (Step 3), 985/992 (Step 4 + multilingual corrective),
  1056/1059 (Step 5A.1), 1132 (Step 5A.2 incl. review corrections),
  1169 (Step 5A.3). No real MacWhisper,
  oMLX, network, ffmpeg, or user audio; "must not happen" mocks raise.
  One pre-existing, baseline-reproducible flake (~1-in-6):
  `test_search_index_sync.py::TestConvergence::test_unlocked_tag_service_race_converges`
  (genuine thread race against SQLite write serialization).
- Verified: `manage.py check`, `makemigrations --check`, fresh-process
  CLI config errors (no traceback), stage-aware cross-stage recovery,
  error/secret hygiene, and `git diff --check`.
- Sanitized MacWhisper fixtures: `tests/fixtures/macwhisper/`.

## Known limitations

- Cantonese↔Mandarin auto-routing accuracy unproven (needs labelled
  real recordings; zh-ambiguity defaults to Needs Review).
- Router confidence is uncalibrated.
- No per-chunk retry memoization (a retry re-runs the bounded map+reduce).
- Summarization retry of oversized inputs requires a config change
  (`input_too_large` never auto-recovers); no partial summaries.
- No retention deletion (never deletes audio); unverified-routing
  eligibility for future retention is an open Step 6 policy decision.
- `audioop` deprecation (Python 3.13 removal; revisit before upgrade).
- Parked recordings (missing/out-of-inbox sources) wait for the next
  ingest/run; no proactive notification.
- **Library web search is keyword-only (Step 5A.4.2 COMPLETE)**:
  `brain search` remains the CLI entry point and `/recordings/` the
  web entry; highlights, segment jump links and the search-row
  styling/a11y polish are delivered. Local embeddings, semantic/hybrid
  search and Ask-with-citations are the later **Steps 5B–5D**. Keyword
  matching is substring-style (trigrams +
  Unicode-folded LIKE fallback), not stemmed. Index staleness after
  abnormal process death between commit and callback is repaired by
  `brain search`'s full health gate REFUSING to serve (exit 1), with
  `search-index status` as the detailed detector and `rebuild` the
  repair (the sync contract never promises an out-of-band watchdog).

## Step 3–6 roadmap (agreed)

- **Step 4 — delivered**: full web interface, review queue,
  transcript/summary views, tag editing/filtering, manual routing
  controls.
- **Step 5 — in progress**: Step 5A.1 Production Library UI, Step 5A.2
  Search Index Foundation and **Step 5A.3 Incremental Index
  Synchronization are delivered**, and **Step 5A.4.1 keyword search
  backend + CLI is delivered** (registry + FTS5 trigram table,
  reversible migration 0008, atomic `brain search-index rebuild`,
  read-only `brain search-index status`, per-recording after-commit
  sync via `workflow/services/search_sync.py` hooks, literal
  plain-text `brain search` with deterministic ranking, per-Recording
  dedup, bounded snippets and the separated full-health gate) and
  **Step 5A.4.2 web search is COMPLETE** (5A.4.2a scoped service +
  states, no health cache; 5A.4.2b highlights, segment jump links,
  Card/Table parity and accessibility). **Step 5A is complete.**
  Before starting the next feature phase, complete one narrowly scoped
  **stability patch** for the documented concurrent web-tag SQLite lock
  race (bounded retry of lock/busy failures only, fresh transaction per
  attempt, finite backoff, no retry of unrelated database failures).
- **Step 5B — Local Embeddings Foundation**: use the configured local
  oMLX embedding endpoint/model; add versioned embedding storage and
  provenance (model, dimensions, source content hash/index version);
  provide bounded status/rebuild/repair commands and incremental sync.
  Keep this phase to index production and integrity — no semantic-search
  UI or Ask feature yet.
- **Step 5C — Semantic and Hybrid Search**: implement bounded semantic
  retrieval and deterministic keyword+semantic fusion, preserving
  recording deduplication, tag/date scope, provenance and stale/index-
  unavailable states; expose Keyword/Semantic/Hybrid modes in CLI and
  the Library web UI.
- **Step 5D — Ask with Citations**: retrieve bounded local evidence,
  call only the local LLM, and produce answers whose citations map to
  real retrieved transcript segments or summaries (including working
  transcript jump links). Never invent citations; surface insufficient
  evidence clearly. Add CLI and web flows; persistence of question/
  answer history is out of the initial scope unless separately approved.
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
