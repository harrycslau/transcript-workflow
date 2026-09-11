# Brain — UI Design Prototype (v6)

> **This is a design prototype, not production code.**
> All data is fictional. No real transcripts, recordings, or database content is used.

## How to view

```
open design/ui-prototype/index.html
```

**Key interactions:**
- Type in the search bar — Library switches to search results
- Use the **Hybrid** dropdown to change search mode
- Click filter chips (Work, Personal, etc.) to filter
- Click the **grid/table icon** toggle in Row 1 to switch between Card and Table views
- Sort by Newest, Oldest, Title A–Z, Title Z–A, or Duration
- Click a recording to enter Recording Detail, which contains the **complete summary**
- On Recording Detail, switch **language variant tabs** (visual state only) and use
  **Copy Markdown**, **Download .md**, **Plain text**, and **Regenerate summary**
- Open the separate **Transcript** or **History** screens from the detail header
- **Routing** in the detail header opens a modal chooser to reroute and
  retranscribe with a different routing profile, or confirm the current routing
  (prototype-only, static — nothing is scheduled or polled)
- **+ Add tag** opens the tag editor dialog: filter configured tags in compact
  rows, toggle assignments, and create prototype-only custom tags with
  **Create and add**
- Each separate screen has a **Back to Recording overview** control
- **Back to Library** preserves query, filters, and scroll position
- **Copy Markdown** / **Copy transcript** use the clipboard when available
- **Regenerate summary** opens the existing confirmation dialog
- View preference (Card/Table) is saved to `localStorage`
- Press `/` to focus search from anywhere
- **Technical details** / **Summary provenance** collapse with native `<details>` elements (no JavaScript)

## What changed from v5

### 1. The complete summary lives on Recording Detail

v5 split the full summary onto its own screen and showed only a bounded
preview on the overview. Feedback from daily use: the summary is the
reason to open a recording, so the Recording Detail page itself now
contains the **complete summary** in a Markdown-like document flow:

- **Overview**, **full nested Key points**, **Action items**,
  **People &amp; organizations** and **Topics** render as one document,
  no preview bounds and no "open full" step.
- **Language variant tabs** and the variant status note sit above the
  document and switch visual state as before.
- A contextual utility row next to the Summary heading carries
  **Copy Markdown**, **Download .md**, **Plain text** and
  **Regenerate summary** (the existing confirmation dialog).
- An optional collapsed **Summary provenance** block keeps
  variant/model/version/generation details available without noise.

The separate Summary screen markup is removed entirely.

### 2. Transcript and History stay separate

The Transcript and History screens are unchanged in behaviour and keep
their **Back to Recording overview** controls. The transcript preview
on Recording Detail stays exactly as before: 5 segments plus a link to
the separate Transcript screen. History remains one link/screen with
responsive captioned tables.

### 3. Two native disclosures, no duplicated fields

Recording Detail now has two collapsed `<details>` blocks that partition
the metadata instead of duplicating it:

- **Technical details** — recording/routing/transcription-specific:
  Recording ID, SHA-256, source file, discovery, routing profile and
  transcription model.
- **Summary provenance** — summary-specific: variant, summary model,
  summary version, generation, generated timestamp and status.

## Rationale

- **Daily usability.** The summary is the document users read most; it
  belongs on the page they open, not one click away. Removing the
  preview/full-summary split eliminates a navigation hop for the common
  case while keeping the page a coherent document.
- **Contextual actions.** Copy/export/Regenerate sit next to the
  Summary heading, not pinned to the viewport — no sticky bottom bar.
- **Long documents stay separate.** Transcript and History are long and
  audit-oriented; separate screens keep Recording Detail scannable.
- **History is tabular.** Attempts and versions are audit rows, so a
  captioned table (with horizontal scroll on small screens) is the
  right, accessible form.
- **Technical disclosure without noise.** IDs, hashes and model strings
  stay collapsed in native `<details>` blocks, partitioned by concern so
  no field appears twice.

## This iteration's prototype-only proposals

Two prototype-only interactions were added on Recording Detail. Both are static
design proposals and neither implies that a backend for them exists today.

### 1. Routing chooser

A compact **Routing** trigger sits next to the Transcript and History
links in the Recording Detail header. It opens an accessible modal chooser that
clearly shows the current routing (european profile · auto · verified) and lets
the user pick a routing profile:

- Choosing a **different profile** (cantonese / mandarin) schedules a
  retranscription with that profile. The chooser explains that a new transcript
  version is created **only after** the retranscription succeeds, and that the
  current transcript and full history remain preserved.
- Choosing the **current profile** keeps the already-confirmed routing — no
  processing change and no retranscription.

The primary button demonstrates **Continue to confirmation**, which updates the
dialog to a prototype confirmation state with copy tailored to the selected
profile, followed by a static "confirmation recorded" state. The prototype
explicitly does **not** simulate background or polling behaviour — no work is
scheduled in the background.

### 2. Tag editor dialog + custom tag creation

The inert **+ Add tag** button now opens a tag editor dialog containing:

- a **search/filter textbox** over configured tags, with compact selectable rows
  that use concise labels — `Work`, `Meeting (suggested)`, `Research`,
  `Personal`, `Language`, `Idea` — and update the detail tag row when toggled.
  No visible state labels or legend are shown; the confirmed/suggested
  distinction stays available to assistive technology via `aria-label` on each
  checkbox;
- a **Create a new tag** textbox with a **Create and add** action that appends a
  manual tag chip to the detail tag row, rejects blank and duplicate entries, and
  reports the outcome in a small status message.

Custom-tag creation is **explicitly labelled as a prototype proposal** (one short
muted note: "Custom tags are a prototype feature.") — this README is a static
design document. Production has since adopted the interaction: custom tags are
global reusable definitions created via the recording detail page
(`workflow/services/tags.py:create_custom_tag_and_assign`), and a custom tag
whose normalized name later appears in YAML `tags.allowed` is promoted to
config-owned by `tags --sync` on the same row (assignments/history preserved).

## Information architecture (proposal)

```
Top bar: [Brain] [Search + mode dropdown] [Review badge] [Status]
              |
              v
Filter area: Row 1 (dates, sort, view toggle) | Row 2 (tags)
              |
              v
Results: Cards or Table — same data, same filters
              |
              v
Recording Detail:
  compact header + Transcript / History / Routing links
  status / next-action panel ("No action required")
  compact tags (+ Add tag → tag editor dialog)
  Summary (complete document flow: variant tabs + status, utility
    actions, overview, nested key points, action items, people &
    organizations, topics; collapsed Summary provenance)
  Transcript preview (5 segments → open transcript)
  Technical details (native <details>: IDs/hash/source/routing/model)
              |
              +--> Transcript (metadata, exports, fuller transcript)
              +--> History    (routing/transcription/summary tables, source info)
```

## Visual system

Same warm, calm, personal palette as v3/v4/v5. v6 keeps the
document-flow typography (`.doc-flow`, `.doc-list` counters,
`.doc-bullets`), the variant-tab and utility-action styles, and the
responsive history tables. No new tokens or component styles beyond the
reuse of existing full-screen utilities on Recording Detail; the
obsolete Summary-screen markup and dead selectors are removed.

## Accessibility considerations (v6)

- View toggle uses `role="radiogroup"` with `role="radio"` and `aria-checked`
- Filter chips are `<button>` elements, not `<div onclick>`
- `/` shortcut focuses search; `Escape` closes dropdown
- Status badges use text + dot (not colour alone)
- Active filter chips have explicit remove buttons with `aria-label`
- Screen navigation reuses the shared `data-screen` handling
- Language variant tabs use `role="tablist"` / `role="tab"` with `aria-selected`
- History tables use `<caption>`, `<th scope="col">` and a horizontally
  scrollable wrapper on small screens
- Technical details and summary provenance use native
  `<details>/<summary>` for keyboard and screen-reader semantics
- Status panel renders as `role="status"` so screen readers announce it
- Summary headings use a semantic `<h2>`/`<h3>` hierarchy under the
  recording `<h1>`, and nested numbering comes from real list
  semantics, not decorative text
- Both new dialogs (Routing, Tag editor) use `role="dialog"`,
  `aria-modal="true"` and `aria-labelledby`, are hidden with the native
  `hidden` attribute (no inline JS/styles), and share one small local
  dialog helper: opening moves focus into the dialog, `Escape`, backdrop
  click and Cancel close it, closing returns focus to the trigger, and
  `Tab` is trapped within the dialog. The routing chooser uses a real
  `<fieldset>/<legend>` with radio inputs; the tag editor uses labelled
  checkbox rows and a `role="status"` live region for its status message
- Mobile layout stacks both dialogs using the existing tokens and
  breakpoints (full-width action buttons, stacked create row)

## Questions for approval

1. **Complete summary on the detail page.** Is the complete summary (five
   sections plus variant tabs and utility actions) the right content
   for Recording Detail, or should any section be collapsed by default
   while staying on the same page?

2. **Utility action placement.** Copy Markdown, Download .md, Plain
   text and Regenerate sit in the Summary heading row. Is that the
   right contextual home, or should the actions live in a toolbar
   between the variant tabs and the document?

3. **Two disclosures vs one.** Does splitting Technical details
   (recording/routing/transcription) and Summary provenance
   (summary-specific) read clearly, or should they be merged into a
   single collapsed block with grouped labels?

4. **Transcript preview bounds.** The detail page still shows exactly
   5 transcript segments and links to the separate Transcript screen.
   Is that the right bound now that the summary is complete on the same
   page?

5. **Routing trigger placement.** Should the trigger stay next to
   Transcript/History as prototyped, or sit adjacent to the status
   panel? If status-adjacent, the interactive control must remain
   outside `role="status"`.

6. **Custom tag definitions.** Should production move from
   configuration-owned tag definitions to user-created custom tag
   definitions (stored per recording, as prototyped), or stay
   configuration-owned? Custom-tag creation requires this product and
   backend decision.

## Current production behaviours preserved (for the proposal)

- Default / Original / English / Traditional Chinese generation
- Concrete existing variants readable
- Only approved generation selectors creating variants
- Current summary retained after failed regeneration
- Explicit confirmation before regeneration (modal)
- Manual tag editing
- Date and tag browsing
- Copy-friendly Markdown/plain-text exports
- Read-only GET pages
- Processing and failure states
- localhost-only operation