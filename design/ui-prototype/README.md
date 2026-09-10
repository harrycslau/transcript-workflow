# Brain — UI Design Prototype (v4)

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
- Click a recording to enter Recording Detail
- **Back to Library** preserves query, filters, and scroll position
- View preference (Card/Table) is saved to `localStorage`
- Press `/` to focus search from anywhere
- **Technical details** on the Recording Detail page collapse/expand with a native `<details>` element (no JavaScript)

## What changed from v3

### 1. Recording Detail is now an overview page (proposal)

The v3 detail page duplicated the full Summary, Transcript and History
inside three tabs. v4 replaces the tabs with a **single scannable
overview** that gets you to status, actions and the full documents in a
few seconds:

1. **Compact header** — title, date, duration, status badge, and
   explicit links to the **Full summary / Transcript / History** pages.
2. **Status / next-action panel** — one green panel stating the current
   health and the single next action ("Transcribed · Summary current ·
   No pending actions"). Problems (needs review, failed stage, missing
   audio, pending regeneration) would replace the green panel and name
   the concrete next step.
3. **Compact tags** — the active tags plus the add-tag control, kept
   tight beside the header.
4. **Summary preview** — the Overview paragraph, exactly **3 key
   points** and **2 action items**, then "Open full summary". The full
   variant tabs, copy/export controls and provenance stay on the full
   summary page.
5. **Transcript preview** — exactly **5 segments**, then "Open
   transcript — 187 segments".
6. **Technical details** — a native collapsed `<details>` block with
   Recording ID, SHA-256, source file, routing, transcription model,
   summary model and summary provenance. IDs/hashes are hidden by
   default so the page reads like a document, not a database row.
7. **No attempts table and no full history** — History is one link, not
   a duplicated table. All the historical detail remains reachable.

The action bar stays sticky at the bottom ("Summary current · No
pending actions" + Regenerate), so the expensive action is always one
tap away without scrolling.

## Rationale

- **Decision first.** The detail page's primary job is answering "is
  this recording OK, and what should I do next?" Tabs force the user to
  hunt across duplicated content before that question is answered.
- **One document at a time.** The full Summary and Transcript are
  already long. Previewing them on the same page as the status
  duplicates content and doubles the scrolling; links keep every full
  document one click away.
- **History is secondary.** Attempts tables matter during debugging, not
  during daily review. A link preserves access without dominating the
  page.
- **Technical disclosure without noise.** IDs, hashes and model strings
  are real privacy/audit data but visually loud. A native collapsed
  block keeps them available without front-loading them.

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
Recording Detail (overview):
  compact header + Full summary / Transcript / History links
  status / next-action panel
  compact tags
  Summary preview (overview + 3 key points + 2 actions → open full)
  Transcript preview (5 segments → open transcript)
  Technical details (native <details>: IDs/hash/source/routing/model)
  sticky action bar (Regenerate)
```

Full documents live on their own pages (Summary with variant tabs and
exports; Transcript with segment pagination; History with attempts).

## Visual system

Same as v3 — warm, calm, personal palette. The new overview adds only a
green status panel, a detail-links row and the native technical-details
block; no new tokens or component styles beyond those.

## Accessibility considerations (unchanged + v4 additions)

- View toggle uses `role="radiogroup"` with `role="radio"` and `aria-checked`
- Table rows are keyboard-accessible (native `<tr>` focusability)
- Filter chips are `<button>` elements, not `<div onclick>`
- `/` shortcut focuses search; `Escape` closes dropdown
- Status badges use text + dot (not colour alone)
- Active filter chips have explicit remove buttons with `aria-label`
- Detail links carry explicit `aria-label`s ("Open the full summary",
  "Open the full transcript (187 segments)")
- Technical details use a native `<details>/<summary>` — keyboard and
  screen-reader disclosure semantics for free
- Status panel renders as `role="status"` so screen readers announce it

## Questions for approval

1. **Preview sizes.** The overview shows 3 key points, 2 action items
   and 5 transcript segments. Are these the right bounds? Should the
   summary preview include the full overview paragraph, or a clamped
   version with "more"?

2. **Action placement.** The sticky bottom action bar keeps Regenerate
   one tap away. Should the next-action also appear inside the status
   panel (e.g. a "Regenerate" button next to "No pending actions")?
   Or should actions move into the header row?

3. **Technical disclosure.** IDs, hashes, model names and routing are
   collapsed inside a native `<details>` block. Should any field be
   visible by default (e.g. transcription model in the header meta),
   and is the summary-provenance summary line the right level of
   detail?

4. **Tags.** The overview keeps the full compact tag row with add-tag.
   Should suggested tags be visually distinct here (as in the Library),
   and should the "Confirm suggestion" action appear inline on the
   overview or stay on the full summary page?

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