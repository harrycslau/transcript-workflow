# Step 6 — Approved decisions and phase acceptance contracts (Step 6.0 outcome)

> **Status: APPROVED as the Step 6.0 outcome.** The user agreed in
> principle with the recommended direction and supplied the
> clarifications recorded here. These decisions are the accepted Step 6.0
> baseline and the design contract for phases 6.1+. This document is
> documentation only: no production code, schema, migration, command,
> config, or real file operation is implied or delivered.
>
> Two items remain **explicit, separate approval gates and are NOT
> approved here**: (1) any actual source **file deletion/move/trash/
> quarantine** (and any automation of it); (2) **installing/enabling/
> activating** any launchd schedule. See §10.
>
> The existing durable invariants (original-audio read-only safety, GET
> read-only, the shared `schedule_recording_sync` writer and embedding
> post-commit callback, pipeline locking for processing actions, POST-only
> mutation) are preserved and referenced, never weakened. The decisions
> below are approved Step 6.0 policy; the standing rules in `AGENTS.md`
> remain the durable runtime invariants.

## 1. Purpose

Step 6.0 is the decision/UX/acceptance gate for Step 6. This document
records the approved resolutions for every decision D1–D12, the approved
cross-cutting and per-phase acceptance contracts, and the clarifications
the user requested. It deliberately avoids final table/field names and
migration shapes beyond the recommended minimal direction in
`docs/step-6-plan.md` §8 (Phase 6.1); the exact shape is designed in 6.1.

## 2. Approved decisions (D1–D12)

### D1 / D8 — Trim scope and ordinal-0 default: **Option A — APPROVED**

Ordinal 0 remains the complete, whole-recording section and is **fixed**:
it is never part of segmented versions, is never edited or versioned, and
keeps driving the recording-level default summary, search, embeddings and
Ask. Trim never changes these full-recording defaults. The Transcript
*view* is not part of those defaults: a saved crop becomes the normal
working presentation on the Transcript page (cropped rows hidden by
default, with a small "Show full transcript" toggle), while the persisted
full transcript data and source audio remain complete. See §3 for the
approved segmented-version semantics (which supersede the earlier "trim
decoupled from layout, independent Apply/Reset lifecycle" wording).

### D2 — Logical-only vs derived audio export: **logical trim only — APPROVED**

Phase 6.1 ships logical (non-destructive) trim only. No generated/derived
audio file, no audio export, no playback change, and no new audio tool in
the current Step 6 baseline. The existing `afinfo`/`afconvert` baseline is
unchanged and ffmpeg is not a dependency. A derived export, if ever
wanted, is a separate deferred feature with its own tooling evaluation
(not approved).

### D3 / D5 — Section layout edit/version behavior: **immutable/versioned — APPROVED**

A segmented version is edited as an immutable snapshot: saving creates a
**new active revision**; prior revisions, and any summaries/variants bound
to them, remain as history. Boundaries are never mutated in place under
existing summaries. Range and topic sections are saved **together,
atomically, as one revision** (§3). Final table/field names are designed
in 6.1; the recommended minimal direction is in
`docs/step-6-plan.md` §8.

### D4 — Section boundary alignment: **segment ordinals only — APPROVED**

Boundaries may only be placed *between transcript segments* (inter-segment
lines only; never at the transcript's very start or end). Canonical
membership is the **segment ordinal**; timestamps are display-only. No
partial segments are allowed. Topic sections exist only when at least one
split exists: then N+1 explicit sections exhaustively partition the
retained range and every retained segment belongs wholly to exactly one of
them. A crop-only version has **zero** topic sections, so no segment
belongs to a topic section. Canonical ranges are specified as
`[start_segment_ordinal, end_segment_ordinal_exclusive)` to remove
inclusive ambiguity; the UI may display inclusive segment labels and
timestamps.

### D6 — Section tag relationship to recording tags: **orthogonal — APPROVED**

Section tags use the existing **global tag definitions**, but section
assignments are orthogonal to recording assignments. There is no
inference and no promotion/demotion between the two scopes. Existing
provenance semantics (suggested/confirmed/manual, config/custom,
user-removal suppression) are preserved within each scope. Section tag
edits follow the established bounded tag-mutation pattern (local SQLite
BUSY/LOCKED retry, no pipeline lock) and are implemented in 6.2, not 6.1.

### D7 — Section-level search documents: **index active-layout summaries — APPROVED**

Phase 6.3 indexes summaries that belong to the **active segmented
version**, reusing the existing summary document identity/type where
feasible. Bare section boundaries are not documents and are never
indexed. Ask may use section-summary prose as evidence; metadata/tags
never become evidence. Any index mapping or version impact must be handled
explicitly in 6.3, never silently.

### D9 — Retention eligibility, delay clock, audit, delete mode: **eligibility reporting first — APPROVED**

The first 6.4 delivery is **eligibility reporting + recording-level Keep
Audio + dry run only**. There is **no** source deletion, move, trash, or
quarantine in that delivery. The approved conservative candidate policy:
a recording is retention-eligible only when it has

- exactly one present source that is inside the current inbox;
- verified routing;
- an active successful transcript;
- a current active ordinal-0 **default** summary;
- no unfinished attempts and no active retranscription/resummarization
  failure.

The approved retention-timer policy is in §5. Any actual source
relocation/deletion, and any automation of it, remains a **separate
explicit user-approval gate** with revalidation and an append-only audit
trail (§10). Hard deletion cannot be reversed and would be a distinct
delete mode requiring separate approval.

### D10 — Scheduler command/frequency/destructive cleanup opt-in: **disabled launchd template — APPROVED**

Provide a proposed, **disabled** launchd template/instructions for a daily
`brain run`. launchd owns timing and environment. No new app scheduling
config key and no in-app daemon is added. Installing/enabling/activating
the template on the machine requires explicit user action and is a
**separate approval gate** (§10). No destructive retention command is
scheduled.

### D11 — Segmented-version partition semantics: **splits create sections; crop-only has zero — APPROVED**

A segmented version's topic sections exist only when at least one split
exists: with N splits inside the retained range there are exactly N+1
topic sections, exhaustive, ordered and non-overlapping across **exactly
the selected working range** (not the whole transcript) — every retained
segment in `[start, end_exclusive)` belongs to exactly one topic section,
and every topic section is a nonempty contiguous sub-range. A crop-only
version (zero splits) has **zero** topic sections. Gaps, overlaps, empty
sections and out-of-order boundaries are rejected atomically. Segments
outside the selected range belong to no topic section in that segmented
version; they remain intact and accessible in the full transcript. The
fixed ordinal-0 full-recording section is outside segmented versions and
is never counted as a topic section.

### D12 — Missing-file reconciliation behavior: **status + one global Rescan — APPROVED**

Phase 6.4 provides audio status plus an explicit **POST-only Rescan
inbox** action under the pipeline lock. The exact semantics are in §6. No
manual relink is offered; existing ingest/hash identity handles
rediscovery/attachment after hash proof. GET remains strictly read-only.

## 3. Trim / segmented version semantics (approved clarification A)

- A **segmented version** is one immutable transcript-bound working layout
  revision containing (1) a non-destructive logical trim/working range and
  (2) **zero or more** topic sections. A crop with no splits has zero topic
  sections; one or more splits produce N+1 topic sections that exhaustively
  partition ONLY that selected range.
- Trim's 6.1 purpose is the **saved cropped working Transcript view**: the
  retained range hides irrelevant lead-in/trailing lines on the Transcript
  page by default (cropped rows hidden, with a small "Show full transcript"
  toggle), without modifying source audio or the persisted full transcript.
  If splits exist, the retained range also bounds those explicit topic
  sections for 6.2 section summaries/tags.
- Segments outside the selected range remain intact and accessible in the
  full transcript; they simply belong to no topic section in that
  segmented version (in a crop-only version, no retained segment belongs to
  a topic section either, because there are none).
- The ordinal-0 existing `Section` remains fixed full-recording and outside
  segmented versions. Default summary/search/embedding/Ask stay
  full-recording; persisted transcript data stays complete. Trim never
  changes these defaults; the Transcript working presentation follows the
  active segmented version's crop (see the purpose bullet above).
- Trim structure/history is delivered in **6.1** together with topic
  layout revisions. It has **no derived audio/playback/export effect** in
  6.1. The cropped working Transcript view is active in 6.1;
  section-level summarization/tagging over the retained range activates in
  **6.2**. No derived audio in the current Step 6 baseline.
- Because range + topics are one immutable segmented version, save them
  **atomically as one new revision**. **Clear crop** restores the full
  transcript as the range; it creates no sections by itself (sections exist
  only where the user placed splits), so a crop-only state clears to zero
  topic sections with nothing to name. The prior prototype/docs claim that
  trim and layout have independent Apply/Reset lifecycles is removed.
- Boundaries remain segment-aligned; canonical ranges are
  `[start_segment_ordinal, end_segment_ordinal_exclusive)` to remove
  inclusive ambiguity. UI may display inclusive segment labels/timestamps.

## 4. Retranscription and continuation (approved clarification B)

- Successful retranscription creates the new Transcript + fresh ordinal-0
  full section exactly as today. It creates **no segmented version** and
  does not copy trim/topic boundaries, because segment ordinals/timestamps
  may differ. Old transcript segmented versions remain historical/readable.
  The new active transcript shows "Not segmented" until the user creates
  one.
- Failed retranscription leaves the current active transcript and active
  segmented version untouched.
- Recording-level tags continue unchanged across retranscription (existing
  behavior).
- For every newly saved segmented version, all topic `Section` rows are
  new immutable scopes. No section summary, `SummaryVariantState`, model
  suggestion, or section-tag assignment is automatically copied — not even
  for visually identical boundaries/title. Prior data remains historical
  on the prior version. New topics start with summary variants
  missing/not generated and no section tags. This is conservative and
  explicit; any future carry-forward is a separate user action/decision.
- Saving an unchanged payload is a **no-op**, not a new revision.
- 6.1 only creates structure/history; it does not implement section
  summaries/tags. 6.2 implements explicit per-section generation/tagging;
  there is **no automatic generation on version save or retranscription**.
  6.3 later indexes only active segmented-version summaries. Existing
  recording-level defaults/search remain untouched.

## 5. Retention timer policy (approved clarification C)

- The eligibility clock is based on activation of the **current active
  ordinal-0 DEFAULT-language Summary**.
- A successful regeneration that replaces that default summary
  **resets/restarts the retention timer** at the replacement summary's
  activation time.
- Failed regeneration does **not** reset it because the current summary is
  preserved. Non-default variant generation, section summary generation,
  and section/tag/layout/trim edits do **not** reset it.
- Successful retranscription makes the recording ineligible until the new
  transcript gets a current default summary; that summary's activation
  starts a fresh timer.
- This is approved policy for future 6.4 design. No deletion is
  implemented. Actual file deletion/move/trash/quarantine remains a
  separate explicit user approval (§10).

## 6. Rescan exact semantics (approved clarification D)

- Rescan is **ONE GLOBAL inbox operation**, not per-recording. It is one
  POST-only, CSRF-protected action under the pipeline lock; it runs
  recovery then **exactly one normal `run_ingest(config)` pass, not
  `run_pipeline`**.
- It reconciles all known sources and recursively discovers supported
  WAV/MP3/M4A in the configured inbox using the existing
  containment/symlink, stability, pre/post-hash and SHA-256 identity rules.
- New/still-changing files may only become observing/waiting and need a
  later scan after the stability window. Same content at a new path
  attaches as another `AudioSource` to the same Recording; the old moved
  path remains historical/missing. Changed content at the same path
  detaches/re-enters observation and is never silently attached to the old
  Recording. Outside-current-inbox sources are left unchanged and never
  accessed.
- It updates aggregate source/Recording audio presence but does **not**
  route/transcribe/summarize. Existing post-commit search/embedding sync
  hooks from ingest remain the only index path; there is no web-specific
  index writer.
- There is **no manual relink**. Use "rediscovered/attached after hash
  proof", never "relink".
- The UI result is aggregate sanitized counts only: no paths, hashes, IDs,
  raw exceptions or content. Lock busy uses the existing friendly 409. GET
  does no work. A two-step confirmation is the approved/recommended 6.4
  interaction, not a 6.1 deliverable: **Rescan itself remains 6.4, not
  6.1**.
- **6.4 UX placement is deferred.** The approved policy above is retained
  in full, but no retention / Keep Audio / Rescan screen or control is
  prototyped now; the exact 6.4 UI placement (including the single global
  Rescan inbox control, not per-item buttons) is designed when 6.4 is
  implemented. The prototype has no Rescan control.

## 7. Scheduling (approved clarification E)

- Preparing a **disabled** launchd template/instructions can remain
  planned.
- Installing/enabling/activating any schedule is a **separate explicit
  approval/user action** (§10). No destructive command is scheduled.

## 7a. Transcript-page crop & split editing (final approved feedback refinement — supersedes F)

This is the **final approved feedback refinement** for the 6.1 editor. It
**supersedes the prior derived-section/editor-panel presentation** (feedback
refinement F): there is no large editor panel, no fixed ordinal-0 card, no
"derived topic sections" list, no section ranges/summaries/tags/variants or
provenance in the editor, and no inline revision-history list on Transcript.
The approved interaction is:

- **One editor location.** Crop/split editing exists **only on the active
  Transcript page**. There is no separate "Trim & sections" screen/link,
  route or screen.
- **Active vs historical.** The active transcript gets an **Edit trim &
  splits** control; historical transcript versions are read-only.
- **Edit toggle reveals scissors only.** Pressing **Edit trim & splits**
  does **not** show a panel or a modal. It immediately reveals a small
  scissors control on each **inter-segment** divider line between
  timestamped transcript rows (never at the transcript's very start or
  end), preserving the clean transcript layout. Pressing it again
  (**Done editing**) hides the scissors and discards unsaved staging.
- **Scissors ask for the action.** Clicking a scissors opens a small
  accessible action dialog/popover (the toggle itself never opens one) that
  summarizes the boundary/time and exposes only the valid actions:
  - **Split here** — an interior split that starts a new user-visible
    section.
  - **Crop from here** — sets the working start to this boundary,
    trimming/hiding the content above it.
  - **Crop to here** — sets the end-exclusive to this boundary,
    trimming/hiding the content below it.
  - **Remove split** — only when that divider is already a split.
  - **Cancel**.
  Invalid actions are disabled or omitted. Empty crops, any action on a
  boundary outside the current working range, an endpoint (start/end) split,
  and a duplicate split are rejected.
- **Crop hides, never dims.** Cropped-away transcript rows and irrelevant
  boundary controls are **hidden** from the staged edited transcript (not
  dimmed and not shown as a derived-range configuration). A compact staged
  notice reports counts such as "N lines cropped above/below" with
  **Reset** and **Clear crop**. The full transcript and source audio remain
  retained and recoverable; after a fictional save the normal Transcript
  shows the saved cropped working view (cropped rows hidden by default)
  with an explicit small **Show full transcript** toggle. Ordinal-0
  summary/search/embedding/Ask stay full-recording.
- **Crop-only = no sections.** A crop with no splits is valid and has **zero
  topic sections/topics**; nothing needs naming. One or more splits partition
  the retained range into N+1 visible spans, and each span gets an **inline
  topic input** in the transcript flow where the user names the topic. There
  is **no automatic/derived topic inference** and no separate section-card
  list, ranges, summaries, tags, variants or provenance in this editor.
- **Removing the final split** removes the topic inputs/section records; the
  crop remains. **Clear crop** restores the full transcript without creating
  sections (sections are created only by splits).
- **Canonical state.** Canonical state stays half-open
  `[start, end_exclusive)`; timestamps are display-only. Cropping removes any
  split markers that fall outside the retained range. The fixed ordinal-0
  section and the default summary/search/embedding/Ask scope stay
  full-recording; the Transcript working view follows the saved crop.
- **One confirmed immutable revision.** Saving commits one new revision
  containing the crop range plus the optional explicit splits/topics.
  Confirmation is required; an unchanged payload is a no-op; **Reset**
  restores the current active version; prior revisions/history are preserved.
  New split-created sections start without summaries/tags in later 6.2; no
  carry-forward. Save/Reset controls stay compact (a small bar), not a large
  editor panel. **History belongs on History**, not as a large list on
  Transcript.
- **6.4 UX deferred.** The retention / Keep Audio / Rescan **screen** and
  control are removed from the prototype; their approved policies (D9, D12,
  §5, §6) are retained, and 6.4 UX placement is deferred to 6.4. Retention /
  Rescan policies and the separate deletion/schedule approval gates are
  unchanged.

## 8. Cross-cutting acceptance contracts (approved)

These apply to every Step 6 phase:

- **Original audio never modified/overwritten** (E1). No Step 6 feature
  writes to `data/inbox`.
- **Full transcript + source audio retained under logical trim** (E2).
- **Saved crop = normal Transcript working view**: cropped rows are hidden
  by default with a "Show full transcript" toggle; the fixed ordinal-0
  default summary, search, embeddings and Ask stay full-recording, and the
  persisted full transcript data stays complete (final refinement, §7a).
- **Ordinal-0 behavior unchanged** unless a future decision explicitly
  changes it (E4, D1/D8 Option A).
- **6.1 crop/split editing lives only on the active Transcript page**;
  historical transcript versions are read-only and there is no separate
  editor screen/route (final refinement, §7a).
- **Summary provenance never corrupted by boundary changes**: segmented
  versions are immutable; saving creates a new active revision and prior
  summaries stay bound to their revision (D3/D5).
- **Search/embedding/Ask stay consistent** through
  `schedule_recording_sync` post-commit + the embedding post-commit
  callback (E3). GETs stay strictly read-only.
- **Segmented-version save is POST-only, under the pipeline lock, with a
  stale-state fingerprint.** Section **tag** edits instead follow the
  existing bounded tag-mutation pattern (local SQLite BUSY/LOCKED retry,
  no pipeline lock), not the save contract.
- **No background daemons or automatic retries**; sync failures stay
  nonfatal and detectably stale via `search-index status` /
  `embedding-index status`.
- **No new framework/dependency.**

## 9. Phase acceptance contracts (approved)

### Phase 6.1 — Segmented versions: logical trim + topic layout/history

Bounded to structure/history only. The concrete implementation plan is
`docs/step-6-plan.md` §8.

- A segmented version stores a working range `[start, end_exclusive)` plus
  **zero or more** topic sections (zero when no splits; N+1 when splits
  exist), partitioning exactly that range; save is one atomic new revision
  (A, D3/D5, D11).
- The editor is integrated into the **active Transcript route/template**
  (no separate editor screen or route); historical transcript versions are
  read-only (final refinement, §7a).
- The edit toggle only reveals scissors on the inter-segment divider lines
  (no panel, no modal; no transcript start/end scissors); clicking a
  scissors opens a small action dialog with
  Split here / Crop from here / Crop to here / Remove split (final
  refinement, §7a).
- The saved crop becomes the normal Transcript working presentation
  (cropped rows hidden by default, "Show full transcript" toggle); the
  fixed ordinal-0 default summary, search, embeddings and Ask stay
  full-recording, and the persisted full transcript data stays complete
  (D1/D8, final refinement, §7a).
- The save payload is the crop range plus sorted split markers plus topic
  labels (empty when no splits); the service materializes the exhaustive
  `Section` rows from the range + split markers, so no client-supplied
  section ranges are accepted and there is no automatic topic inference
  (final refinement, §7a).
- Empty crops, outside-range actions, endpoint-split and duplicate-split
  actions are rejected (final refinement, §7a).
- Clear crop restores the full range and creates no sections by itself (a
  crop-only state clears to zero topic sections); sections exist only where
  the user placed splits (final refinement, §7a).
- Boundaries are between segment ordinals; membership is by ordinal;
  timestamps are display-only; no partial segments (D4).
- Save is POST-only, under the pipeline lock, with a stale-state
  fingerprint; it creates a new active revision and preserves prior
  revisions and their summaries (D3/D5).
- Gaps/overlaps/empty/out-of-order ranges are rejected atomically (D11).
- The fixed ordinal-0 full-recording section is not part of segmented
  versions and is never counted as a topic section (D1/D8, D11).
- Retranscription creates no segmented version and copies no boundaries;
  failed retranscription leaves the active segmented version untouched;
  no summary/variant/suggestion/section-tag carry-forward; unchanged save
  is a no-op (B).
- 6.1 does not implement section summaries/tags and does not change the
  search/embedding index or the ordinal-0 defaults; no search sync on save
  (D2, D7, B).
- New schema arrives as a NEW migration after 0010, never edits to applied
  ones, with `makemigrations --check` and MigrationExecutor coverage.

### Phase 6.2 — Section summaries and section tags

- Section summaries scope exactly as the existing
  (transcript, section, output_language) contract; ordinal-0 derivation is
  unchanged (E4).
- Section tags use global definitions but are orthogonal to recording
  tags; no inference/promotion; provenance semantics preserved (D6).
- Per-section generation/tagging is explicit; no automatic generation on
  version save or retranscription (B).
- Generation follows the pipeline-action lock/stale-state contract;
  section tag edits follow the bounded tag-mutation pattern (D6, §8).
- Mutating actions are POST-only.

### Phase 6.3 — Search / embedding / Ask integration

- Keyword/semantic/hybrid/Ask keep full-recording behavior (Option A).
- Section summaries belonging to the active segmented version are indexed
  using the existing summary document identity/type where feasible; bare
  boundaries are never documents; section-summary prose may be Ask
  evidence but metadata/tags never are (D7).
- Only active segmented-version summaries are indexed; recording-level
  defaults/search are untouched (B).
- All scope changes flow through `schedule_recording_sync` post-commit and
  the embedding post-commit callback (E3).
- Any index mapping/version impact is handled explicitly; sync failures
  stay nonfatal, no-auto-retry, detectably stale (E3).
- Web GETs stay strictly read-only.

### Phase 6.4 — Retention + Keep Audio + missing-file reconciliation

- First delivery is eligibility reporting + recording-level Keep Audio +
  dry run only; no source deletion/move/trash/quarantine (D9).
- Eligibility follows the approved conservative policy (D9); the delay
  clock follows §5 (default-summary activation; successful regeneration
  resets it; failed regeneration and non-default/section/tag/layout/trim
  edits do not; retranscription restarts it once a new default summary
  exists) and compares timezone-aware instants.
- Any actual source relocation/deletion and its automation is a separate
  explicit user-approval gate with revalidation and append-only audit
  (D9, E1, §10).
- Missing-file behavior is status + the one global POST-only Rescan under
  the pipeline lock; no manual relink; GET read-only; aggregate sanitized
  counts only; two-step confirmation recommended at implementation (D12,
  §6).

### Phase 6.5 — launchd scheduling

- A disabled launchd template/instructions for daily `brain run`; launchd
  owns timing/environment; no new app config key or daemon (D10).
- Installing/enabling/activating requires explicit user action; no
  destructive retention command is scheduled (D9/D10, §10).
- No new framework/dependency.

## 9a. Step 6.2a — Safe Library return, temporary split titles, derived display title, section duration (delivered)

A bounded follow-up to 6.2 delivered in the working tree (migration
0013). It changes NO search/index/Ask contract and **Step 6.3 remains
unimplemented**.

- **Safe Library return.** ONE small server-signed token per normal
  Library render encoding ONLY canonical validated normal-Library
  state: the `ListFilters.as_pairs()` filter/sort pairs (stably
  de-duplicated so a redundant duplicate tag never survives), a bounded
  positive `page`, and the `cards`/`table` view. Destination is always
  `reverse('recordings')`. A `lib_return` parameter is the SOLE carrier
  of state: a valid token ignores the raw query string including any
  `q` and any raw `view=` (which never mutates the view cookie), and an
  invalid/forged/oversized/non-canonical token falls back to the plain
  Library using the view cookie/default only while ignoring all raw
  query state; a duplicate pair rejects the whole token on decode. The
  token rides the normal-Library recording AND section links
  (recording-backed title links in card/table, the section-card parent
  Recording link and the section title links) and propagates verbatim
  through section-detail tabs, the section summary confirmation/
  execution redirects (including the confirmation CANCEL link, built
  only from the already validated token) and the section tag redirects;
  `recording_detail` validates an optional `lib_return` through the
  shared decoder and, when valid, restores the originating page/state
  through its top-left `← Library` breadcrumb
  (`library_return.return_url`); absent/invalid/forged tokens leave
  the plain `reverse('recordings')` breadcrumb and are never echoed.
  Direct section links
  still work; search results never generate a token (no search-origin
  support).
- **Temporary split titles.** New editor-created ranges get the
  server-authoritative `Segment N of YYYYMMDDHHMM` (N = the 1-based
  canonical section ordinal; timestamp = `recorded_at` else
  `discovered_at` in the configured timezone). The editor JSON carries a
  bounded server-generated `temporary_titles` list (index = ordinal −
  1) so new split-created sections are visibly prefilled immediately —
  never from the browser clock — and a carried-over temporary section
  whose ordinal changed regenerates its server title. The editor
  payload carries bounded exact flags; the parser/service validate
  cardinality/type and the WRITER requires a new True flag's non-blank
  title to EXACTLY equal the current server-derived value (a custom
  title can never be claimed temporary), while the no-op comparison
  uses the raw submitted payload so an unchanged layout stays a
  zero-DML no-op even after effective-timestamp drift. READ-side
  validation (canonical service reads and the Library SQL predicate)
  requires a True flag to carry the exact canonical shape
  `Segment <ordinal> of <12 ASCII digits>` and never compares a stored
  title to the current timestamp (titles are immutable creation-time
  metadata). Blank True-flag titles are filled at save; editing a title
  makes it custom; existing exact ranges preserve title/provenance;
  layouts stay immutable; migration 0013 adds
  `Section.title_is_temporary` (Boolean, default False, existing rows
  custom) — additive and fully reversible.
- **Derived display title.** `Section.title` is NEVER mutated during
  summary generation. Whenever an active DEFAULT-language section
  Summary exists its `Summary.title` is the Section's ONE user-facing
  title — it supersedes BOTH a stored temporary title AND a manually
  entered custom title in presentation (display override only; the
  custom title stays layout metadata/provenance). The Section detail
  page renders that title exactly once (H1/page title) — the
  `_summary_body.html` embedded title paragraph is suppressed there
  (`suppress_title=True`, h3 hierarchy retained) while Recording Detail
  keeps its embedded title paragraph. Without a default Summary the
  stored `Section.title` is used. The Library's derived SQL expression
  (default Summary title first, stored title fallback) drives both the
  rendered title and Title A–Z/Z–A ordering; optional variant titles
  never replace the H1 and switching tabs never changes the page
  identity. This supersedes the earlier "custom titles always win"
  display invariant, but NOT custom ownership/storage.
- **Section duration.** Library section items project an approximate
  duration from the canonical range's usable segment span, safe unknown
  when unavailable/nonpositive; recording items keep the recording
  duration; no N+1/unbounded reads; the duration renders in the normal
  card/table and on section detail.
- **Explicit 6.3 requirement (recorded, NOT implemented).** The normal
  Library tag filters ALREADY suppress a valid split parent and use the
  active Section items (the derived projection). The user requires
  **Step 6.3 to mirror that Library replacement**: a valid active split
  layout must yield the active Section search/filter results with the
  parent recording result SUPPRESSED — never parent + section
  duplicates — while historical/malformed layouts fail closed. The
  current keyword/semantic/hybrid/Ask stack remains
  whole-recording-only and unchanged; 6.3 is not implemented.
- **Section-scoped summary status + Section-origin returns (bug-fix
  delivery).** The Section detail status panel is Section-scoped
  (`_section_summary_panel` over the selected `VariantView`) — derived
  ONLY from the Section variant state (current / regeneration_failed /
  failed / not-generated, explicitly this section/language variant),
  never the parent Recording summary tuple; the misleading parent
  "summary missing" text and the `(inherited from the parent recording)`
  note are gone and the panel label is `Section summary status`. The
  three Section-detail links (History, Section in transcript, Full
  transcript) carry a server-owned bounded `return_section=<Section pk>`
  marker plus the already validated `lib_return` token when present;
  History and transcript GETs validate `return_section` as an exact
  positive ASCII-decimal integer naming a readable canonical topic
  Section of the URL recording (active or historical; fixed/malformed/
  cross-parent fail closed) and label the breadcrumb `← Section`
  pointing to the exact Section detail (with a valid `lib_return` if
  supplied) — the plain `← Recording overview` is retained otherwise and
  nothing unsafe is echoed. Transcript pagination preserves only the
  validated return parameters; History internal `v`/`layout` links,
  recording-origin pages, action flows and search are NOT broadened;
  GETs stay strictly read-only.

## 10. Separate unresolved approval gates

These are explicitly **not approved** by Step 6.0 and remain separate,
explicit user decisions:

1. **Actual source file deletion / move / trash / quarantine** (and any
   automation of it). 6.4's first delivery is reporting + Keep Audio + dry
   run only. Any real relocation/deletion requires its own approval, with
   revalidation and an append-only audit trail, and a chosen delete mode
   (hard deletion is irreversible).
2. **Installing/enabling/activating any schedule** (launchd or otherwise).
   Only a disabled template/instructions may be prepared.
3. (Deferred, also not approved) a **derived trimmed-audio export** and its
   tooling (D2).

## 11. What this document is not

- Not production code, schema, migration, command, config, or a real file
  operation.
- Not an approval of source deletion or schedule activation (§10).
- The Step 6.0 decisions here are the approved design contract for 6.1+;
  they do not themselves change any standing runtime invariant in
  `AGENTS.md`.
