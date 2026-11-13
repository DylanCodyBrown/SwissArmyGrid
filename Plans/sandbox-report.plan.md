# The Sandbox as a report: readable pushes, pages, PDF and markdown, push anything, cheat sheets

The owner's brief (2026-11-13): each analysis comes with "a small mock-up instruction guide that simply
shows a table and shows the format that data needs to be in", a cheat sheet. Pushes to the Sandbox "should
actually be very clearly readable and look decent; don't skimp on that part". "The sandbox is a modular
container where the user can drag, drop and resize anything", and add their own markdown. "Output the
entire sandbox to PDF, letter or landscape, page by page", or "to markdown, to use with an AI model", the
charts embedded as PNG. "Any overlay we have created to see details about a table, a column or a cell can
be pushed to the sandbox; you can even right-click an individual cell and send that value."

## The owner's decisions

1. **Page view** for the PDF: a switch shows the Sandbox as letter or landscape pages with their edges
   drawn; the analyst arranges cards onto pages; the PDF is what they see.
2. **Cheat sheets show and check**: the mock tables, and a line about whether the grid fits. No reshape
   step.
3. **This work comes before the regression** (Plans/analyze-menu.plan.md, phase B), so the regression's
   results land in a finished Sandbox.

## What exists, and what is missing

Cards (note, table, chart, diagram, Graphviz, Plotly, ECharts, image) on a 12-column board, resized from
the corner; notes added by hand; one card at a time downloads as HTML. Missing: moving cards by drag,
markdown tables and headings in notes, any whole-Sandbox export, right-click menus anywhere, and a working
Plotly or ECharts card from the server (the server keeps their figure under `spec`, the page reads `body`).

## The pieces

### S1. Readable pushes

- Notes render real markdown: headings, tables (GitHub style, numbers right-aligned), bold, code,
  lists, quotes, rules.
- An analysis push is two cards side by side: a **note** (a heading, the verdict, a table of the key
  rows, the key numbers in bold, the cautions) and a **chart** (Plotly, drawn in the theme's colors):
  - odds: each value's odds ratio as a dot with its 95% range, on a log scale, a line at 1;
  - fit test: seen against expected bars per value (Benford: the digit curve);
  - find predictors: strength bars, ranked, redundant ones marked;
  - outliers: the values as a strip with the fences, the outliers in the accent color.
- The server keeps a Plotly or ECharts card's figure as the page draws it.

### S2. The board and its pages

- Drag a card by its title bar to move it; drop between cards. The order is kept on the server.
- **Page view** (letter or landscape): the board is drawn as pages; cards flow onto them in order and
  never split; a card can **Start a new page**. **Print to PDF** prints exactly those pages (the browser's
  own Save as PDF: no new dependency), one page per sheet, charts redrawn at print size.

### S3. Markdown export

One `.md` file: each card in order, notes as their markdown, tables as markdown tables, charts and
diagrams as PNG images embedded in the file (`![title](data:image/png;base64,...)`), images as they are.
Headed with the workspace, the date and the grids each card came from. Ready to hand to an AI model.

### S4. Push anything

- **Send to Sandbox** on every overlay that shows details: the column profile, the whole-table profile,
  the value viewer, the status bar's selection numbers, the Key Finder, a compare's summary.
- **Right-click a cell**: Send value to Sandbox, Keep these, Exclude these, View value, Copy.
- **Right-click a column header**: Profile to Sandbox, Distinct values to Sandbox, and the Analyze
  entries for that column (Odds, Fit test, Outliers).
- Each push is a readable note (and a chart where the overlay has one: the histogram, the top values).

### S5. Cheat sheets

Each analysis form has a **Data shape** panel: a tiny mock table in the shape that works, one in a shape
that does not and why (wide against long; numbers that should be bucketed; an identifier as a category),
and a line about the grid itself ("region has 4 values: fine", "this table looks wide: one column per
month; these analyses want one row per month"). The check runs on the server from the columns' types
and counts. The cheat sheet can be sent to the Sandbox too.

## Order of work

S1, S2, S3, S4, S5; each with tests (domain and HTTP first, then the browser), docs and a verification
note. Then the regression.
