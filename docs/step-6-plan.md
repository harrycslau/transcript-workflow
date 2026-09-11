# Step 6 — Planning document (overall plan accepted; Step 6.0 complete; 6.1 planned)

> **Status: the overall Step 6 plan has been accepted by the user as the
> working baseline, and Step 6.0 is COMPLETE.** The approved Step 6.0
> decisions D1–D12 and the phase acceptance contracts live in
> `docs/step-6-decisions.md`; the Step 6 screens in `design/ui-prototype/`
> prototype those approved decisions, and this document's §8 plans their
> implementation.
> Step 6.1 is the next planned phase; §8 is its concrete implementation
> plan (structure/history only). No production Step 6 functionality is
> implemented yet.
>
> Two items remain explicit separate approval gates and are NOT approved:
> (1) any actual source file deletion/move/trash/quarantine (and its
> automation); (2) installing/enabling/activating any launchd schedule.
> See `docs/step-6-decisions.md` §10.
>
> The durable standing rules that already exist (original-audio read-only
> safety, GET read-only, the shared `schedule_recording_sync` writer +
> embedding post-commit callback, pipeline locking for processing actions,
> POST-only mutation for pipeline/web mutations) are preserved and
> referenced, never weakened by this plan.

## 1. Purpose

Step 6 is the user-initiated editing/summarization/retention layer built
on top of the delivered pipeline (Steps 1–5D):

- user-initiated **topic splitting**;
- **manual crop / trim** (non-destructive logical trim);
- section-level summaries and section-level tags;
- retention cleanup (deletion only after successful processing +
  retention delay), with a Keep-Audio override;
- missing-file reconciliation UI;
- launchd scheduling.

This document defines terminology, boundaries, the coherent phase
sequence, existing invariants and approved guardrails, the detailed 6.1
implementation plan, acceptance criteria per phase, data/index/concurrency
implications, testing expectations, and the resolved decision index.

## 2. Terminology (approved)

- **Trim (crop)** — selecting a *working range* of a recording's
  transcript. It never modifies source audio or the persisted full
  transcript, never changes the default whole-recording summary/search/Ask
  scope, and never becomes the implicit default (Option A, D1/D8). The
  saved crop is the normal Transcript working presentation (cropped rows
  hidden by default, with a "Show full transcript" toggle). A trim is one
  half of a segmented version (§3).
- **Segmented version** — one immutable transcript-bound working layout
  revision containing (1) a non-destructive logical trim/working range and
  (2) **zero or more** topic sections (zero when no splits; N+1 when splits
  exist) that partition ONLY that selected range. Range + topics are saved
  together, atomically, as one revision.
- **Split (topic splitting)** — dividing the retained range into named
  topic sections (each a contiguous segment range). Splits create sections:
  N splits inside the retained range yield exactly N+1 topic sections; a
  crop-only version has zero. Sections enable section-level summaries and
  tags in 6.2.
- **Fixed ordinal-0 section** — the existing whole-recording `Section`
  created once per successful transcript. It is outside segmented versions,
  never edited or versioned, and keeps driving the ordinal-0 defaults.
- Canonical ranges are `[start_segment_ordinal, end_segment_ordinal_exclusive)`;
  the UI may display inclusive segment labels and timestamps.

The non-negotiable baseline for trim is **non-destructive logical trim**:
we never modify or overwrite the original audio. A *derived trimmed-audio
export* is a separate, deferred, not-approved feature (D2). Actual source
deletion under retention requires a separate explicit approval gate.

## 3. Current-state anchors (facts, not proposals)

- `Section` carries `transcript`/`ordinal`/`title`/`start_ms`/`end_ms`;
  ordinal 0 is the whole-recording section, created once per successful
  transcript. `Section` is currently created by the pipeline only.
- `Summary` and `SummaryVariantState` already scope by `section`
  (`Summary.section`, one active per
  (transcript, section, output_language)); recording-level defaults are
  derived from the active transcript's ordinal-0 section. So section-level
  summaries have a natural extension point already (6.2).
- Tags are **recording-level only** today. Section-level tags are new (6.2).
- Search indexes the active transcript's segments (whole transcript) and
  the whole-recording summary. Embedding sync and Ask reuse those
  documents. Any change to the searchable scope flows through
  `schedule_recording_sync` post-commit and the embedding post-commit
  callback; web GETs stay strictly read-only. **6.1 changes no indexed
  content and schedules no sync.**
- Mutating pipeline processing actions run under the exclusive `flock` at
  `data/temp/locks/pipeline.lock`; bounded web tag mutations run unlocked
  under the existing local SQLite BUSY/LOCKED retry. Segmented-version save
  uses the pipeline lock (approved, G3).

## 4. Invariants and approved design guardrails

### 4a. Existing durable invariants (preserved)

- **E1 — Original-audio safety.** The user's source audio is sacred and
  read-only. No Step 6 feature modifies, overwrites, truncates, or
  in-place-edits an original file under `data/inbox`. Logical trim only
  *selects*; it never rewrites. Retention-driven source deletion is a
  separate explicit approval gate with robust safeguards (D9/§15).
- **E2 — Source audio and full transcript retained under logical trim.**
- **E3 — Search/embedding/Ask stay consistent through the shared sync
  writer.** All searchable-scope changes flow through
  `schedule_recording_sync` inside the successful mutation transaction
  (post-commit), and the embedding post-commit callback follows. GET stays
  strictly read-only. 6.1 introduces no searchable-scope change.
- **E4 — Existing ordinal-0 behavior.** The active transcript's ordinal-0
  section remains the derivation basis for the recording-level default
  summary and the whole-recording search document (D1/D8 Option A).

### 4b. Approved design guardrails

- **G1 — Summary provenance integrity.** Section-boundary changes never
  mutate boundaries in place under existing summaries: segmented versions
  are immutable, and a change creates a new revision while prior
  summaries/variants stay bound to their revision (D3/D5).
- **G2 — Chosen active scope.** Ordinal 0 stays the complete,
  whole-recording section; default summary/search/Ask remain
  full-recording. The saved crop is the normal Transcript working
  presentation (cropped rows hidden by default, full transcript one toggle
  away); trim/segmented versions are explicitly selected working layouts
  (D1/D8 Option A).
- **G3 — Concurrency for segmented-version mutations.** Segmented-version
  save is POST-only, under the pipeline lock, with stale-state protection,
  and no new background daemons/retries. Section-tag edits (6.2) follow
  the established bounded tag-mutation pattern instead.

## 5. Scope and non-goals (approved)

**In scope (Step 6):**

1. Non-destructive manual **trim** as half of a segmented version
   (Option A) — Phase 6.1.
2. **Topic splitting** as an immutable/versioned segmented version — Phase 6.1.
3. **Section-level summaries** — Phase 6.2.
4. **Section-level tags** — Phase 6.2.
5. **Retention cleanup** (Keep-Audio override, missing-file
   reconciliation UI) — Phase 6.4.
6. **launchd scheduling** — Phase 6.5.

**Non-goals:**

- Modifying/overwriting original audio in any form (never — E1).
- Derived trimmed-audio *export* / playback changes in 6.1 (D2; deferred,
  not approved).
- New audio tooling beyond the current `afinfo`/`afconvert` — no ffmpeg
  dependency (D2).
- Changing the default whole-recording summary derivation or the
  search/Ask index scope (D1/D8 Option A).
- Section editing that mutates boundaries under existing summaries (G1).
- Automatic/background retry daemons for sync (stays manual/no-retry).
- Automatic summary/variant/suggestion/section-tag carry-forward into a
  new segmented version (explicitly conservative; §8, D3/D5/B).

## 6. Phase sequence

- 6.1 (segmented versions: trim + topic layout/history) is the foundation;
  everything else depends on it.
- 6.2 (section summaries/tags) depends on 6.1's topic `Section` rows.
- 6.3 (search/embedding/Ask integration) depends on 6.1's structure and,
  for section-level search, on 6.2's section summaries.
- 6.4 (retention + Rescan) is largely orthogonal to 6.1–6.3 but shares the
  mutating-action/concurrency plumbing.
- 6.5 (launchd) is an operations layer on top of 6.4 and the existing
  pipeline.

## 7. Phase 6.0 — Decisions / UX prototype / acceptance contracts (COMPLETE)

Goal: resolve the prioritized decision log (D1–D12) and produce a UX
prototype plus acceptance contracts that phases 6.1+ must satisfy.

Outcome (complete):

- D1–D12 resolved and recorded as **approved** in
  `docs/step-6-decisions.md`, including the requested clarifications on
  segmented-version semantics, retranscription continuation, retention
  timer, Rescan, and scheduling.
- Step 6 screens updated in `design/ui-prototype/` to the approved behavior
  (fictional, static): trim/split editing is integrated into the active
  Transcript screen, while the separate Trim & sections screen is removed;
  retention and Rescan UX placement is deferred to 6.4.
- Per-phase acceptance contracts written (this document + decisions doc).

No production schema, migration, or runtime implementation was part of 6.0.

## 8. Phase 6.1 — Segmented versions: logical trim + topic layout/history (implementation plan)

**Bounded to structure/history only.** 6.1 creates and versions the
segmented-version structure and shows its history. It does not generate
section summaries or section tags, does not change the search/embedding
index, does not touch the ordinal-0 defaults, and adds no CLI.

### 8.1 Objective

Deliver crop/split editing **integrated into the existing active
Transcript route/template** (no separate editor screen or route; historical
transcript versions are read-only), plus a confirm-then-execute save that
atomically creates one new immutable segmented version (crop range plus
zero-or-more topic sections). The saved crop becomes the normal Transcript
working presentation — hiding irrelevant lead-in/trailing lines by default
with a "Show full transcript" toggle — and, when splits exist, the retained
range bounds those explicit sections for 6.2. A read-only revision history
is owned by the History page. A new active transcript simply has no
segmented version ("Not segmented").

### 8.2 Behavior

- A segmented version belongs to one `Transcript`; it stores the crop/
  working range `[start_segment_ordinal, end_segment_ordinal_exclusive)` and
  **zero or more** topic sections. Zero splits ⇒ zero topic sections; one or
  more splits ⇒ N+1 sections that exactly partition the retained range and
  require topics.
- The editor is on the **active Transcript page**. Pressing **Edit trim &
  splits** only reveals small scissors controls on the **inter-segment**
  divider lines (no panel, no modal; no transcript start/end scissors).
  Clicking a scissors opens a small accessible
  action dialog summarizing the boundary/time with **Split here** (interior
  topic split), **Crop from here** (start becomes the boundary), **Crop to
  here** (end-exclusive becomes the boundary) or **Remove split** (only when
  already a split). Empty crops, outside-range actions, endpoint (start/end)
  splits and duplicate splits are disabled/rejected.
- The staged payload is the crop range plus the **sorted split markers**
  plus topic labels (none when there are no splits); the service validates
  and materializes the exhaustive `Section` rows from the range + split
  markers (zero rows for crop-only). There are no independent arbitrary
  section start/end selects and no automatic topic inference.
- Historical transcript versions are read-only; only the active transcript
  can be trimmed/split.
- Boundaries are segment-aligned. The editor stages changes; nothing is
  written until a confirmed POST executes under the pipeline lock.
- **Clear crop** restores the full range `[0, segment_count)` and creates no
  sections by itself; a crop-only state clears to zero topic sections.
  Sections exist only where the user placed splits.
- Saving creates a **new active revision**; the prior active revision is
  superseded and remains readable. Prior revisions and any summaries/
  variants bound to them stay historical.
- Saving a payload identical to the current active segmented version is a
  **no-op**: zero DML, no new revision, no callback.
- Topic titles: exact `str`, nonblank after `strip()`, controls/newlines
  rejected, bounded by the existing `Section.title` max length (255).
- Topic count is bounded by the actual segment count and a hard defensive
  cap chosen during implementation (no config key).
- The service verifies layout/Section transcript ownership and that, when
  splits exist, the topic partition exactly covers `[start, end_exclusive)`.
- **No carry-forward**: no section summary, `SummaryVariantState`, model
  suggestion, or section-tag assignment is copied into a new segmented
  version, even for visually identical boundaries/titles.
- The fixed ordinal-0 full-recording `Section` is outside segmented
  versions, untouched, and never counted as a topic section.
- Existing ordinal-0 defaults (default summary/search/embedding/Ask) are
  unchanged; the Transcript working presentation follows the active
  segmented version's crop (cropped rows hidden by default, full transcript
  one toggle away). **No search sync is scheduled on 6.1 save** because no
  currently indexed content/default changes.

### 8.3 Likely minimal model/migration shape (recommended; exact names chosen in 6.1)

Reuse the existing `Section` model so 6.2 summaries can naturally FK to
topic sections; do not create a parallel topic-section model.

- New transcript-bound layout parent (working name `SegmentedVersion`; the
  pk is used for recording-scoped read-only historical URLs):
  - `transcript` FK (`on_delete=CASCADE`, related_name e.g. `segmented_versions`);
  - `revision` positive integer, unique per transcript;
  - `start_segment_ordinal` `PositiveIntegerField`;
  - `end_segment_ordinal_exclusive` `PositiveIntegerField`;
  - concrete lifecycle: `is_active` bool, `activated_at` (non-null once
    saved), nullable `superseded_at`, `created_at`. In one transaction,
    saving supersedes the prior active row first (`is_active=False`,
    `superseded_at=now`) and then creates the NEW active row
    (`is_active=True`, `activated_at=now`, `superseded_at=NULL`); rollback
    restores the prior active row if any later write fails.
  - lifecycle shape CHECK: active ⇒ `activated_at` set and `superseded_at`
    NULL; historical ⇒ `is_active` false, `activated_at` set and
    `superseded_at` set.
  - chronology CHECK: `activated_at <= superseded_at` when superseded.
  - one active per transcript (partial unique on `transcript` where
    `is_active=True`); unique `(transcript, revision)`.
- Add to `Section`:
  - nullable `segmented_version` FK to the layout parent (`NULL` = legacy/
    fixed ordinal-0 section), recommended `on_delete=PROTECT` (immutable
    history; accidental deletion blocked);
  - nullable canonical `start_segment_ordinal` /
    `end_segment_ordinal_exclusive` fields (null for ordinal-0).
- Legacy `Section.start_ms`/`end_ms` are display-only and are NOT canonical
  for topic sections; canonical membership is the new
  `start_segment_ordinal` / `end_segment_ordinal_exclusive` fields, and
  display timestamps are derived from segment data.
- Section shape rules (two mutually exclusive alternatives, so ordinal-0
  can never collide with topic sections):
  - **Fixed section** (`segmented_version IS NULL`): `ordinal` must be 0 and
    both canonical segment-ordinal fields must be NULL. Ordinal 0 is unique
    per transcript (partial unique where `segmented_version IS NULL`),
    preserving the legacy whole-recording section.
  - **Topic section** (`segmented_version IS NOT NULL`): `ordinal` must be
    >= 1 and both canonical segment-ordinal fields must be non-NULL; the
    range must be nonempty (`end_segment_ordinal_exclusive >
    start_segment_ordinal`). Ordinal is unique within a layout
    (`(segmented_version, ordinal)` partial unique where
    `segmented_version IS NOT NULL`).
  - The existing `uniq_section_ordinal` constraint is replaced by these two
    conditional constraints in the same new migration.
- Same-row shape CHECK (as feasible in Django/SQLite): one `CheckConstraint`
  on `Section` enforces the two alternatives — either
  `segmented_version IS NULL AND ordinal = 0 AND start_segment_ordinal IS NULL AND end_segment_ordinal_exclusive IS NULL`,
  or
  `segmented_version IS NOT NULL AND ordinal >= 1 AND start_segment_ordinal IS NOT NULL AND end_segment_ordinal_exclusive IS NOT NULL AND end_segment_ordinal_exclusive > start_segment_ordinal` —
  so a row can never be half-fixed/half-topic.
- Layout parent CHECKs: range `end_segment_ordinal_exclusive >
  start_segment_ordinal` and `start_segment_ordinal >= 0`.
- Cross-table ownership (a `Section`'s transcript equals its layout's
  transcript) cannot be a SQLite CHECK; the service enforces it along with
  the exact `[start, end_exclusive)` partition of the selected range
  whenever topic sections exist (zero `Section` rows for crop-only).
- Migration is **new, after 0010**, additive + constraint replacement,
  fully reversible; no data migration is needed (existing ordinal-0 rows
  migrate with layout `NULL`, satisfying the fixed-section alternative).
  No final migration number is claimed.
- Implementation must also review/update existing ordinal-0 queries
  (default-summary derivation, variant state, chunking, rendering,
  exports, search builders) to include `segmented_version IS NULL` where
  appropriate as defense-in-depth, preserving current behavior.

### 8.4 Constraints

- Original audio read-only; full transcript retained (E1/E2).
- Boundaries only between segments (inter-segment lines only); membership
  by ordinal; no partial segments. Topic sections exist only when at least
  one split exists: N splits ⇒ N+1 sections; a crop-only version has zero
  sections (D4, D11).
- When sections exist, the topic partition covers exactly the retained
  range; contiguous, ordered, nonempty; gaps/overlaps/empty/out-of-order
  rejected atomically (D11).
- One active segmented version per transcript; immutable revisions.
- Titles validated as in §8.2.
- No new config key; no new dependency; no new framework.

### 8.5 Service boundary

- A new service module (working name `workflow/services/segmentation.py`)
  is the **ONLY writer** of segmented versions and topic `Section` rows.
- It validates exact input (bounded exact-integer IDs, ranges, split
  markers, topic labels), verifies transcript/recording ownership and the
  exact partition whenever topic sections exist (zero `Section` rows for
  crop-only), and performs the create-new-revision + supersede-old-active
  in **one transaction**. It **materializes the exhaustive topic `Section`
  rows from the crop range + sorted split markers** (zero rows when there
  are no splits; no client-supplied section ranges, no automatic topic
  inference), computes the canonical payload first and short-circuits to a
  no-op when unchanged.
- The service **does not acquire the pipeline lock** and does **not**
  schedule search/embedding sync. The calling web action layer acquires
  the lock, runs recovery, re-derives eligibility, and compares the
  fingerprint (mirroring `workflow/services/web_actions.py`).
- Returns safe counts/identifiers only; no raw exceptions, paths, or
  transcript content.

### 8.6 Forms / routes / views / templates / history

- **Editor GET** — the active Transcript route/template (no separate editor
  route or screen), strictly read-only (no detection/network/write): renders
  the transcript with scissors divider controls and the active segmented
  version (or "Not segmented"). Historical transcript versions are
  read-only. Server-rendered initial values + hidden state fingerprint.
- **Confirmation** — POST-only confirm step carrying the hidden
  fingerprint and staged payload, following the existing confirmation
  interstitial pattern; then the confirmed POST executes.
- **Execution** — under the pipeline lock + `recover_interruptions` +
  fingerprint comparison; stale/lock-busy safe outcomes (existing friendly
  409 for lock busy; "state changed" no-op for fingerprint mismatch).
- **History** — the History page owns a read-only list of prior revisions;
  each historical segmented version is viewable read-only (recording-scoped),
  never editable. Transcript does not render the revision list.
- Templates reuse the existing v6 detail/page patterns and the prototype
  structure; no new frontend framework. Section titles and other user
  values render with normal autoescaping only (no generated HTML).
- **No CLI for 6.1.**

### 8.7 Lock / fingerprint

- Save is POST-only under the exclusive pipeline lock, with
  `recover_interruptions()` while holding it (existing pattern).
- A read-only segmented-version fingerprint binds the active transcript
  id, segment count, active segmented-version id/revision and current
  topic structure so a stale or duplicate form cannot save against a state
  the user did not see. Fingerprinting performs SELECTs only.

### 8.8 Retranscription hooks / behavior

- No new transcription hook and no change to transcription behavior:
  successful retranscription already creates the new Transcript + fresh
  ordinal-0 full `Section`. Ordinal-0 sections keep layout `NULL` and null
  canonical range fields.
- The new active transcript has **no** segmented version ("Not segmented");
  trim/topic boundaries are not copied (ordinals/timestamps may differ).
- Old transcript segmented versions remain historical/readable.
- Failed retranscription leaves the active transcript and active segmented
  version untouched.
- Recording-level tags continue unchanged across retranscription.
- Any minimal compatibility edit needed to keep ordinal-0 creation valid
  under the new conditional constraints is behavior-preserving and covered
  by regression tests. As defense-in-depth, existing ordinal-0 queries are
  reviewed/updated to filter `segmented_version IS NULL` where appropriate
  (see §8.3), preserving current behavior.

### 8.9 No-op semantics

- Canonicalize the staged payload (crop range + sorted split markers +
  ordered topic labels, empty when no splits).
- If it exactly equals the current active segmented version, return
  "unchanged": zero DML, no new revision, no supersede, no callback.
- Only a real change creates a new revision.

### 8.10 Tests / verification

- Model/constraint tests: one active layout per transcript, unique
  revision, the Section shape CHECK (fixed vs topic alternatives — ordinal
  0 with NULL canonical fields vs ordinal >= 1 with non-NULL canonical
  fields and topic end > start), conditional section-ordinal uniqueness,
  layout range CHECKs, ownership rules.
- Service tests: atomic create-new-revision + supersede, exact
  `[start, end_exclusive)` partition validation, title validation (blank,
  control/newline, max-length), crop-only = zero sections and clear-crop
  rule, no-op, no carry-forward, transcript/recording ownership rejection.
- Migration tests: genuine `MigrationExecutor` forward and reverse on
  isolated databases (existing ordinal-0 rows stay layout `NULL` and
  satisfy the fixed-section shape).
- Web tests: editor GET strictly read-only; POST-only + CSRF; confirmation;
  lock busy → 409; stale fingerprint → safe no-op; history read-only; no
  search/embedding sync scheduled on save.
- Regression tests: ordinal-0 creation and the existing
  summary/transcript/history routes unaffected (including ordinal-0 queries
  that gain a `segmented_version IS NULL` filter); no index changes.
- Standard checks: full `pytest` suite, `manage.py check`,
  `makemigrations --check`, `git diff --check`.

### 8.11 Rollout / migration

- One NEW migration after 0010: new layout table + additive nullable
  `Section` columns + replacement of `uniq_section_ordinal` with the
  conditional constraints. Reversible. No backfill/data migration.
- Existing ordinal-0 sections continue to work with layout `NULL`; no
  behavior change for recordings that never create a segmented version.
- No config, dependency, or index change.

### 8.12 Explicit exclusions

- No section summaries, `SummaryVariantState`, or section tags (6.2).
- No search/embedding index change or sync callback on save (6.3).
- No derived audio, playback, or export (D2).
- No separate trim/sections editor screen or route; editing lives on the
  active Transcript page and historical transcript versions stay read-only.
- No Rescan/retention/Keep Audio UX or scheduling (6.4/6.5); the 6.4 UX
  placement is deferred while its approved policies are retained.
- No manual relink; no source deletion.
- No change to ordinal-0 defaults (default summary/search/embedding/Ask) or
  to the persisted full transcript data; the Transcript working view
  follows the active segmented version's crop.
- No CLI.
- No automatic carry-forward of any prior section data.

### 8.13 Acceptance criteria

- A confirmed save creates exactly one new immutable segmented version
  (crop range + sorted splits + zero-or-more topic sections) atomically;
  the prior active revision is superseded and remains readable.
- The editor is integrated into the active Transcript route/template; there
  is no separate editor route, and historical transcript versions are
  read-only.
- An unchanged payload is a no-op (zero DML, no new revision).
- Clear crop restores the full range and creates no sections by itself;
  crop-only states save with zero topic sections.
- Invalid partitions (gap/overlap/empty/out-of-order, out-of-range) are
  rejected atomically with a stable sanitized error.
- Titles are validated as in §8.2; topic count is bounded.
- The fixed ordinal-0 section is never part of a segmented version and is
  never counted as a topic section.
- Retranscription creates no segmented version and copies no boundaries;
  failed retranscription leaves the active segmented version untouched; no
  summary/variant/suggestion/section-tag carry-forward occurs.
- Editor GET is strictly read-only; save is POST-only, pipeline-locked,
  fingerprint-guarded; lock busy is a friendly 409; stale state is a safe
  no-op.
- The saved crop becomes the normal Transcript working presentation
  (cropped rows hidden by default, "Show full transcript" toggle); no
  ordinal-0 default (default summary/search/embedding/Ask), persisted full
  transcript data, or source audio changes; no search/embedding sync is
  scheduled on save.
- `makemigrations --check` is clean and the new migration is reversible
  with MigrationExecutor coverage.

## 9. Later phases (approved acceptance contracts)

### Phase 6.2 — Section summaries and section tags

- Section summaries scope exactly as the existing
  (transcript, section, output_language) contract; ordinal-0 derivation is
  unchanged.
- Section tags use global definitions but are orthogonal to recording
  tags; no inference/promotion; provenance semantics preserved (D6).
- Per-section generation/tagging is explicit; no automatic generation on
  version save or retranscription.
- Generation follows the pipeline-action lock/stale-state contract;
  section tag edits follow the bounded tag-mutation pattern; mutating
  actions are POST-only.

### Phase 6.3 — Search / embedding / Ask integration

- Keyword/semantic/hybrid/Ask keep full-recording behavior (Option A).
- Section summaries belonging to the active segmented version are indexed
  using the existing summary document identity/type where feasible; bare
  boundaries are never documents; section-summary prose may be Ask evidence
  but metadata/tags never are (D7).
- Only active segmented-version summaries are indexed; recording-level
  defaults/search are untouched.
- All scope changes flow through `schedule_recording_sync` post-commit and
  the embedding post-commit callback; any index mapping/version impact is
  explicit; sync failures stay nonfatal, no-auto-retry, detectably stale.
- Web GETs stay strictly read-only.

### Phase 6.4 — Retention + Keep Audio + missing-file reconciliation

- First delivery is eligibility reporting + recording-level Keep Audio +
  dry run only; no source deletion/move/trash/quarantine (D9).
- Eligibility follows the approved conservative policy; the delay clock is
  the current active ordinal-0 default summary's activation; successful
  regeneration resets it; failed regeneration and non-default/section/tag/
  layout/trim edits do not; successful retranscription restarts it once a
  new default summary exists. Timezone-aware instants.
- Rescan is ONE global POST-only, CSRF-protected action under the pipeline
  lock: recovery then exactly one `run_ingest(config)` pass (not
  `run_pipeline`), existing containment/stability/hash identity, aggregate
  sanitized counts only, no manual relink, GET read-only; a two-step
  confirmation is the implementation recommendation.
- Any actual source relocation/deletion and its automation is a separate
  explicit user-approval gate with revalidation and append-only audit.

### Phase 6.5 — launchd scheduling

- A **disabled** launchd template/instructions for daily `brain run`;
  launchd owns timing/environment; no new app config key or daemon.
- Installing/enabling/activating requires explicit user action; no
  destructive retention command is scheduled.
- No new framework/dependency.

## 10. Cross-phase acceptance criteria

- Original audio never modified/overwritten (E1).
- Source audio + full transcript retained under logical trim (E2).
- The saved crop is the normal Transcript working presentation (cropped
  rows hidden by default, "Show full transcript" toggle); ordinal-0 default
  summary/search/Ask stay full-recording (D1/D8).
- Summary provenance never corrupted by boundary changes (immutable
  segmented versions, G1).
- Existing ordinal-0 behavior unchanged (E4/D1/D8 Option A).
- Search/embedding/Ask consistent with the effective scope through the
  shared sync writer (E3); 6.1 introduces no scope change.
- Segmented-version save POST-only, pipeline-locked, stale-state guarded
  (G3); no background daemons.
- No new framework/dependency.

## 11. Data / index / concurrency implications

- **Schema.** The 6.1 shape is in §8.3. Any migration is a NEW migration,
  never an edit to an existing/applied one, covered by genuine
  MigrationExecutor tests plus `makemigrations --check`.
- **Search/embedding index.** 6.1 changes no indexed content and schedules
  no sync. Later phases that add section-level documents route through the
  existing per-recording reconcile (`schedule_recording_sync` post-commit +
  embedding callback).
- **Concurrency.** Segmented-version save uses the pipeline lock, POST-only,
  with stale-state protection. Pipeline processing actions hold the flock
  today; unlocked web tag mutations rely on SQLite write serialization plus
  the existing local BUSY/LOCKED retry.
- **GETs.** Strictly read-only; no detection, network, subprocess, or
  writes.

## 12. Testing / verification expectations

- All tests remain mocked/network-free; no real MacWhisper/oMLX, ffmpeg,
  or user audio.
- Per-phase: focused deterministic tests for the new service/web behavior,
  plus regressions for the shared sync/lock/stale-state contracts.
- Data-layout tests use genuine MigrationExecutor tests on isolated
  databases, including reverse attempts.
- Standard checks: `UV_CACHE_DIR=/private/tmp/transcript-workflow-uv-cache uv run pytest`,
  `manage.py check`, `makemigrations --check`, `git diff --check`.
- Docs-only/prototype changes do not require the full suite; `git diff
  --check` plus static prototype checks suffice until implementation begins.

## 13. Migration expectations

- No migration is implied by this planning document itself.
- 6.1 arrives as one new migration after 0010 (layout parent + nullable
  `Section` ownership/range fields + conditional uniqueness/CHECKs),
  reversible, with the standard verification.
- The existing ordinal-0 behavior (E4) and G1 guide the design.
- Any retention source-deletion feature that touches existing models is a
  separate approval gate and likewise a new migration.

## 14. Resolved decision index (D1–D12)

All D1–D12 are **resolved/approved** in
`docs/step-6-decisions.md`; the detailed rationale and the requested
clarifications (segmented-version semantics, retranscription continuation,
retention timer, Rescan, scheduling) live there. Summary:

| Decision | Resolution |
| --- | --- |
| D1/D8 | Option A: ordinal 0 stays full-recording/fixed; trim never changes full-recording defaults (saved crop = Transcript working view) |
| D2 | Logical trim only in 6.1; no derived audio/export; no new tool |
| D3/D5 | Immutable/versioned segmented versions; range + topics saved atomically as one revision |
| D4 | Inter-segment segment-ordinal boundaries only; canonical half-open `[start, end_exclusive)` |
| D6 | Section tags orthogonal to recording tags; no inference/promotion |
| D7 | 6.3 indexes active-segmented-version summaries; boundaries never documents |
| D9 | 6.4 first delivery = eligibility reporting + Keep Audio + dry run; deletion is a separate gate |
| D10 | Disabled launchd template; activation is a separate gate |
| D11 | Splits create N+1 sections partitioning the retained range; crop-only has zero sections |
| D12 | Status + ONE global POST-only Rescan; no manual relink; GET read-only |

## 15. Explicitly deferred / not approved

- Derived trimmed-audio export and its tooling (D2).
- Any actual source deletion/move/trash/quarantine and its automation
  (D9) — separate explicit approval gate.
- Installing/enabling/activating any schedule (D10) — separate explicit
  user action.
- Exact 6.1 table/field names beyond the recommended shape in §8.3.

## 16. Notes for reviewers

- The overall plan is accepted as the working baseline; Step 6.0 is
  complete and its decisions are approved in
  `docs/step-6-decisions.md`.
- Step 6.1 is planned (this document §8), bounded to structure/history
  only; no production code/schema/migration/config is delivered.
- The Step 6.0 round (this document + `docs/step-6-decisions.md` + the
  Step 6 screens in `design/ui-prototype/` + the prototype README) is
  documentation/prototype only: no production code, schema, migration,
  command, config, or real file operations.
