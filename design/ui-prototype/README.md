# Brain — UI Design Prototype (v3)

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

## What changed from v2

### 1. Filter layout: two rows, no caption

The `FILTER` label is removed. Controls are self-explanatory in two rows:

- **Row 1**: From date, To date, Sort by dropdown, Card/Table view toggle
- **Row 2**: Tags label + tag chips

No bordered card wraps the filters — they sit directly in the content flow with a subtle bottom border separator.

### 2. Recording count: no sort duplication

The count reads `24 recordings` (or `1 recording` for singular). The Sort dropdown already communicates the active ordering — the count line does not repeat it.

### 3. Month grouping: sort-aware

Month headings (e.g., "September 2026") appear **only** for chronological sorts:
- Newest first (descending)
- Oldest first (ascending)

For non-chronological sorts (Title A–Z, Title Z–A, Duration), month headings are hidden and the list renders as a continuous ungrouped sequence.

When a search query is active and sorting by relevance, month headings are not shown.

### 4. Card / Table view toggle

A segmented toggle beside the Sort control switches between:

- **Cards** (default): Rich cards with title, date, duration, excerpt, tags, language dots, and status badge
- **Table**: Compact rows with columns for Date & time, Title + overview, Duration, Tags, Languages, and Status

Table mode requirements met:
- Entire row is clickable → Recording Detail
- Long titles and tags truncate/wrap gracefully
- Status uses text + colour (not colour alone)
- Comfortable row height, restrained separators
- Overview line beneath title (shows matched excerpt when searching)
- Month group headers shown only for chronological sorts in table mode too
- On mobile (<768px), table is hidden and cards are shown regardless of preference
- The selected desktop preference is preserved (not lost on mobile fallback)

View preference is stored in `localStorage` under `brain-view-mode`, not in any database.

### 5. Mobile filter behavior

On mobile:
- Filter Row 1 (dates, sort, toggle) and Row 2 (tags) are hidden
- A single "Filters" button opens a drawer with all controls
- Active filter chips remain visible above the results even when the drawer is closed
- Clearing filters works from both the drawer and the visible chips

### 6. Prototype coverage

The prototype demonstrates:

1. Cards sorted by Newest — month headings visible (September, August groups)
2. Cards sorted by Title A–Z — month headings hidden, continuous list
3. Table sorted by Newest — month group separators in table rows
4. Table sorted by Title A–Z — no month grouping, alphabetical order
5. Search results with highlighted excerpts, no month headings
6. Mobile filter drawer with active chip persistence

## Information architecture

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
Recording Detail → Summary / Transcript / History tabs
```

## Visual system

Same as v2 — warm, calm, personal palette. No changes to tokens, typography, or component styles beyond the filter and table additions.

## Current production behaviours preserved

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

## Accessibility considerations

- View toggle uses `role="radiogroup"` with `role="radio"` and `aria-checked`
- Table rows are keyboard-accessible (native `<tr>` focusability)
- Filter chips are `<button>` elements, not `<div onclick>`
- `/` shortcut focuses search; `Escape` closes dropdown
- Month group separators in table use `<td colspan>` for screen reader context
- Status badges use text + dot (not colour alone)
- Active filter chips have explicit remove buttons with `aria-label`

## Questions for approval

1. **Table columns**: The prototype shows Date, Title+overview, Duration, Tags, Languages, Status. Should any column be added, removed, or reordered?

2. **Overview line in table**: Each row shows a one-line truncated summary excerpt. In search mode, it highlights matched keywords. Is this useful, or too noisy for a dense table?

3. **Sort options**: The prototype offers Newest, Oldest, Title A–Z, Title Z–A, and Duration. Are these the right set, or should others (e.g., Date discovered, processing status) be added?

4. **Month group separator style**: In table mode, month groups appear as a full-width muted row spanning all columns. Is this the right visual treatment, or would a simpler approach work better?

5. **Filter Row 2 placement**: Tags sit in a second row below dates/sort. Should they instead be inline in Row 1 (compact but potentially crowded), or in a third row?
