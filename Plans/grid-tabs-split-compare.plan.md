# Grid tabs, split view and compare

Status (2026-10-22): T1 tabs, T2 split (shared panels) and T3 compare built. Making a split opens
the key picker (compareDialog.ts) with the Key Finder already run between the two grids; ticked pairs
plus Compare line the grids up, Close leaves the split without keys. The ribbon's Compare row switches
outer / anti (both, left only, right only) / cross compare and stops. Server: engine/compare.py +
routes_compare.py (the mapping is a per-compare table, rebuilt when either grid changes).
Known limits: Excel-style selection stats, copy and export act on a grid's own view positions, not
the aligned compare rows; header sorts don't reorder a compare (it is always in key order).

## What was asked

1. **Tabs.** New grids open as tabs across the top of one window, not as new browser windows.
   Tabs can be moved (reordered), closed and organized.
2. **Split view.** Dragging one tab onto another puts the two grids side by side. Clicking into
   either grid makes the side panels (Table Transformations, analysis panel, SQL box, ribbon,
   status bar) show that grid.
3. **Compare.** A Compare group on the ribbon, live while the view is split: define the primary
   key columns, pick a comparison type, and the two grids line their rows up by key and sort.
   The comparison types:
   - **anti join:** the rows whose key has no match on the other side;
   - **outer join:** every key from both sides, lined up row for row;
   - **cross compare:** click a row on one side and the other side shows every row with the same
     key, and the other way round.

## Why this needs a plan first

The page is built around one grid. `api.ts` reads the grid id from the URL once (`grid`), and
`state`, `gridView`, `selection`, the steps panel, the SQL box, the analysis panel and the
status bar are all single module-level objects bound to it. Tabs need several grids in one page,
each with its own state, and split view needs two of them drawn at once. That is a refactor of
how the page is put together, not a new widget.

## Design

### Page shape

```
[tab bar: orders × | q3 × | +]                      <- new, top of the page (circled area)
[title, toolbar, ribbon]                            <- follow the active grid
[steps | grid pane A | grid pane B | analysis]     <- B only while split
[status bar]                                        <- follows the active grid
```

### Client: one context per grid

- A `GridContext` class holds what is now module-level per grid: the API base path, the view
  state (today's `PageState`), the `GridSelection`, and the AG Grid instance (today's
  `gridView` as a class). Moving to this is the bulk of the work; the five-step file shape
  stays.
- A small `workspace` module owns the open tabs, their order, which one is active, and the
  split (`left`, `right`, `active pane`). Panels subscribe to "active grid changed" and re-bind
  to that context; they don't hold a grid of their own.
- The tab bar: click activates, the × closes the tab (the grid closes on the server, the
  dataset stays), drag reorders, and dropping a tab on another tab (or on a pane edge) splits.
  Dropping it back on the tab bar un-splits. The order and the split are remembered per
  browser (localStorage); the tab list itself comes from the server's open grids.

### Server: tabs instead of windows

- The push client opens a browser only when no workspace page is connected. When one is, the
  server broadcasts `grid_opened` and the page adds a tab and focuses it. (The server already
  counts WebSocket clients, so it can answer that.)
- `/g/<id>` keeps working as a direct link: it opens the workspace with that tab active.

### Compare

**The rule (Dylan, 2026-10-22): the two grids are always aligned.** Row N on the left and row N on
the right are the same key (or one is a blank placeholder where that key has no match), and the
two grids scroll together, so a person reads straight across, left to right and back, and sees
every row that matches. Every comparison type keeps this.

**Keys come from the Key Finder too (Dylan, 2026-10-22).** Each Key Finder pair gets "Use as keys":
it assigns that column pair as the two tables' keys, and compare uses them. The keys can still be
set by hand in the Compare row.

- **Key mapping:** pick key columns on the left and their partners on the right (same name by
  default, so an `id` to `id` compare needs no setup). With two grids split, the Key Finder
  (built 2026-10-22) runs between them and its top pair is offered as the keys. Types must be comparable; text keys
  follow the Matching switches (case, trim).
- **A compare is a server-side view over both grids' current views** (each after its own
  steps), joined on the keys and positioned like any other view, so it scrolls millions of
  rows the same way. Both panes read windows from it, row for row, so the rows line up.
- **Sort:** by the keys, then by the left side's own order.
- **Cell differences** in a matched row are marked, like the changed-cell marks in preview.
- Compare never changes either grid's steps. Turning it off puts both grids back as they were.

## Milestones

- **T1 tabs (built 2026-10-22, differently from the design above):** `/workspace` is one window
  with a tab per open grid, and each tab holds that grid's own page (`/g/<id>`) in an iframe. No
  `GridContext` refactor was needed: every page module still serves one grid, and a hidden tab
  stays loaded. A push opens the workspace once; after that the server's `grid_shown` notice adds
  the tab and brings it forward, so no new window opens. Tabs close (the grid closes, the dataset
  stays), drag to reorder, and the order and active tab are remembered per browser. The theme
  follows across tabs through the browser's storage event.
  **Cost, for T2:** with iframes, two grids side by side each bring their own ribbon and panels,
  not the one shared set this plan describes ("clicking into one grid changes the other panels").
  T2 either accepts per-pane panels or does the `GridContext` refactor then.
- **T2 split (built 2026-10-22, shared panels, Dylan's choice):** the iframe workspace from T1 is
  gone. One page holds every open grid: `gridView.ts` is a factory (`createGridView`), one view
  and one `PageState` per grid; `panes.ts` holds the tab bar, a pane per grid and the split. The
  panels read the active grid through live bindings (`api.grid`, `state.state`) and redraw on
  `activate()`. Make a split by dropping a tab on another tab's middle, or dragging a tab down
  onto the grid (the half picks the side); a tab's left edge reorders. Click into a pane to make
  it active; "Close this side" goes back to one grid. `/workspace` and `/g/<id>` are the same page.
- **T3 compare:** key mapping, the server compare view, anti / outer / cross. Golden-style
  tests on small made-up tables for each type, including blank keys and duplicate keys.

## Open questions (asked one at a time, in this order)

1. ~~How a compare is displayed~~ **Decided 2026-10-22: two aligned grids.** Each side keeps
   its own columns and steps; rows line up by key across the panes, a blank placeholder row
   stands in where a key has no match, and differing cells are marked.
2. ~~Anti join sides~~ **Decided 2026-10-22: both, with a switch.** By default both sides'
   unmatched rows show (each beside a blank placeholder); a switch narrows to Left only or
   Right only.
3. ~~Cross compare~~ **Decided 2026-10-22: filter to the matches.** Clicking a row filters the
   other grid to the rows with that key (a temporary filter, never a saved step), with a banner
   naming the key and a Clear button; it works in both directions.
4. ~~Duplicate keys~~ **Decided 2026-10-22: every pairing, as SQL.** 3 left rows and 2 right
   rows of one key give 6 aligned rows. Safeguard (my addition, to confirm in review): the
   compare first counts its output rows and asks before running one bigger than both sides
   together, naming the keys with the most duplicates.
5. ~~Tabs after a restart~~ **Decided 2026-10-22: restore when the data returns.** The
   workspace remembers its tabs (dataset names, order, the split, each grid's steps as a recipe)
   in the state folder; after a restart a tab reappears when its dataset is pushed again, with
   its steps. No data is written to disk, only names and steps.

All five questions are answered; the plan is ready for review before T1 starts.
