# SwissArmyGrid

A local server that shows any tabular data in live browser grids, with analysis built in. You
push a DataFrame from Python and get a URL; open it in Chrome, Edge or VS Code's built-in
browser. Everything stays on your machine: 127.0.0.1 only, a token on every data request, data
in memory, nothing sent anywhere.

What a grid page does:
- **Grid:** scroll millions of rows (the server sends only the rows on screen). Each header
  shows its distinct count, its blank percentage and a small histogram; its arrow button sorts
  on the server. The horizontal scrollbar is always on screen.
- **Excel-style selection:** click or drag the row numbers to select rows, a header to select its
  column, drag across cells for a block, the corner (or Ctrl+A) for everything; Shift extends,
  Ctrl adds. The status bar shows Count, Average and Sum over the whole selection, loaded or not,
  and Ctrl+C copies it as tab-separated text.
- **Ribbon:** the transformation tools grouped by what they act on: **Table** (filter, sort,
  columns, group by, distinct, SQL, profile, export), **Column** (sort, filter, rename, remove,
  keep, clean, bucket, categorize, group by and profile, for the selected columns) and **Cell**
  (keep or exclude rows with the selected values, view, copy).
- **Table Transformations** (like Power Query's applied steps): filter, sort, choose and rename
  columns, group by, distinct, keep first rows, clean text, bucket, categorize and SQL. Each
  step can be edited, turned off, reordered or previewed (changed cells are marked), with row
  counts as it goes. A step list can be saved as a recipe, and the panel pops out into its own
  window.
- **Cell viewer:** double-click a cell. JSON objects and arrays show as tables you can open one
  layer at a time, markdown is formatted, and long text wraps.
- **Text matching** (the ribbon's Table row): Match case, Wildcards (`%` only, as in SQL LIKE),
  Trim, Empty = blank.
- **Column profile:** a popup from the Column row's Profile: statistics and value counts; click a
  value to filter on it. **Auto width** fits every column (Table row) or the selected ones.
- **SQL box:** Monaco editor; a dataset's name reads it as loaded (the same name and rows as in
  your Python, so the SQL copies over unchanged), and `grid` reads this grid's view after its
  steps. Guarded: one read-only query, a time limit and a row cap. **Apply as step** keeps a
  query over this grid as a SQL transformation step, which reads the table entering it by
  either name; pin any result (joins included) as a new grid.
- **New table from a query:** the **+** after the last tab opens a SQL tray over a blank grid.
  A table name reads that table as its tab shows it (after its steps); Run turns the result into a
  table of its own, with every tool a pushed table has. The tray stays on the tab, so Run again
  rebuilds the table in place, and its own steps apply to the new rows.
- **Copy transformations as Python:** DuckDB, pandas or plain-Python code that reproduces the
  view, SQL steps included. It is run against the grid before it's offered, and only verified
  code gets a Copy button.
- **Key Finder:** pick another grid (or every other grid) and get the likely join columns:
  each column pair that shares values, with the percent of each side's distinct values found in
  the other, whether each side is unique (so which one looks like the key), and a score. Columns
  whose longest value is over 128 characters are left out. Click a pair for sample shared values
  and the values an inner join would drop.
- **Export:** CSV, delimited text, Excel or Parquet, with provenance and formula neutralization.
- **Tabs, split and compare:** every grid is a tab in one window (they wrap onto more rows, up
  to 50). Drag a tab onto another to see two side by side; the key picker opens with the Key
  Finder already run, and compare keeps both grids aligned row by row (outer, anti or cross).
- **The Agent tab** (always first): the **Analysis Sandbox** (charts, notes and result tables;
  charts open large with wheel zoom, and clicking a bar filters the grid). The cards fill the width of the screen.
- **Agent door (MCP):** one privacy level for every table, set in Settings (3 = everything,
  the default; 2, 1 and 0 narrow it, with approve-before-rows and column masks), an activity log,
  and a copyable prompt that installs the MCP in any agent.
- **Themes:** light, dark, high contrast light and high contrast dark, following the system by
  default; Windows forced colors are supported. Zoom with Ctrl+= and Ctrl+-.

The design is [Plans/swissarmygrid-build-plan.md](Plans/swissarmygrid-build-plan.md); what has
been checked against real versions is in [docs/verified.md](docs/verified.md); decisions and
open notes are in [docs/ideas.md](docs/ideas.md).

## Install it: one package, two installs

From a release (each release carries the wheel, the VS Code extension and two installers): download
`install.ps1` (Windows) or `install.sh` (macOS, Linux) with the wheel, then run

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 .\swissarmygrid-<version>-py3-none-any.whl
```

The installer sets up uv when it is missing, then the server with its own Python. Without a wheel path
it downloads the release's wheel itself (needs the GitHub CLI signed in while the repository is private).

Or by hand: build it with `python build.py`, then:

```bash
uv tool install --force "swissarmygrid-<version>-py3-none-any.whl[server]"   # the server, once per machine
uv pip install "swissarmygrid-<version>-py3-none-any.whl"                    # the client, in any Python environment
```

The server install gives the `swissarmygrid` command (start, stop, status), the `sag` agent
command and `swissarmygrid-mcp`. The client install is standard library only, so it goes into any
project's environment without touching that project's own packages:

```python
import swissarmygrid as sag

sag.push(orders)  # a grid named "orders" opens; push again to refresh it in place
```

Visual Studio Code is optional: the extension in `vscode-extension/` starts the server when VS
Code opens and shows the grids in a VS Code tab, but `swissarmygrid start` runs the same server
without it. Where this is going (workspaces, `sag.create_workspace()`, an in-app Python terminal,
saved workspaces, a relationships view): [Plans/standalone-workspaces.plan.md](Plans/standalone-workspaces.plan.md).

Grids open as tabs in one window, the workspace (`/workspace`): the first push opens it, and
every later push adds its grid as a tab there (or brings its tab forward) instead of opening a new
window. That holds for pushes made while the window is still loading, too: they wait for it (up to
15 seconds) rather than each opening one. Close a tab with its ×, middle-click, or Delete; drag
tabs to reorder them.

With pyarrow present, pushes keep their column types. A push opens the browser only when no page
already shows that grid.

## One-time setup for working on SwissArmyGrid itself

```bash
uv sync                      # Python environment in .venv
cd web
npm install                  # AG Grid, Monaco, Vega, esbuild, TypeScript (local, no CDN)
npm run build                # bundles src/ into static/build/
cd ..
```

Made-up test data: `python examples/sample_store.py --push` pushes a nine-table store with known
relationships (and deliberate traps for autodetection) to the running server.

## Run it

```powershell
.venv\Scripts\python -m swissarmygrid.server            # start (reuses the last port if free)
.venv\Scripts\python -m swissarmygrid.server --port 8790  # exactly this port, or a clear error
.venv\Scripts\python -m swissarmygrid.server status
.venv\Scripts\python -m swissarmygrid.server stop
```

`uv run swissarmygrid` works too, but while a server started that way is running, `uv` can't
update the environment (Windows locks the launcher). `python -m` avoids that.

From a notebook or script (the client starts a server if none is running):

```python
import swissarmygrid as sag

orders = load_orders()  # a pandas or Polars frame, Arrow table, DuckDB relation, or list of dicts
sag.push(orders)  # the dataset (and the browser tab) is named "orders"; the grid opens
sag.push(orders)  # again: every open grid on it refreshes in place, keeping its sort
sag.push(orders, name="q3", new_grid=True)
url = sag.push(orders, open=False)  # no browser, just the link
```

`SWISSARMYGRID_OPEN=0` turns the browser off everywhere (the tests set it).

Or the demo data:

```powershell
.venv\Scripts\python examples\push_demo.py --rows 1000000
```

Open the printed URL, including the `#token=` part. In VS Code: Ctrl+Shift+P, then
**Simple Browser: Show**, then paste it.

## The agent commands: `sag` and the MCP server

Every action the page has is a named command in one registry (`swissarmygrid/cli/commands.py`), 88 of
them. A command is named for the part of the app it works on, then the action, so a flat list sorts
into the app's layout: `tabs_new_query` (the + tab), `table_filter` and `table_group_by` (the ribbon's
Table row), `column_keep` and `column_bucket` (the Column row), `cell_exclude` (the Cell row),
`steps_add` and `steps_apply_sql` (Table Transformations), `compare_create`, `sandbox_add`,
`relationships_set`, `erd_draw`, and `agent_read_grid` / `agent_events` for what only an agent has. The
registry is ordered the same way, and `commands` returns the catalog grouped by area. Two front ends
run it:

- `swissarmygrid-mcp` is a stdio MCP server that registers one typed tool per command, generated from
  the registry, so the model sees `steps_add(grid, kind, params, index?)` with its real arguments
  rather than composing a generic call. Descriptions are one line each and the schemas are slimmed, so
  the whole tool list is about 4,800 tokens for 88 tools (a test holds it under a budget). `guide` and
  `commands` (the catalog, with what the user's privacy level allows) carry the detail. A command added
  to the registry becomes a tool with no change to the MCP server.
- `sag` is the same commands in a terminal (standard library only, in the plain wheel):
  `sag tabs_new_query top "SELECT ..."`, `sag steps_add g1 filter @step.json`,
  `sag agent_events --seconds 60`. `@file` reads a file and `-` reads standard input. `sag --help`
  prints the catalog.

`agent_events` listens to the server's live broadcasts (a grid changed and who changed it, a table
opened, an approval asked for), so an agent can work alongside the user instead of polling.

`app_snapshot` returns a PNG of the page as the user sees it (the whole window, or the ribbon, grid,
steps panel or sandbox). The server draws nothing: it asks the open page, which draws itself with
html-to-image and uploads the picture, and the server hands it to the agent without writing it to disk.
The MCP returns it as image content; `sag app_snapshot` saves it and prints the path. It shows real
cell values at every privacy level, so each one is written to the audit log, and Settings says so.

**ERD models that go with a notebook.** Relationship models can be kept in FI-Atlas (`dbo.sag_model`, with history) and
tied to notebooks (`dbo.sag_notebook_model`); `fi_clients.sag_models` reads and writes them and the FI Client Explorer's
SAG Models view edits them. When a notebook's watcher starts, its own Python (the one with fi-clients and the user's
sign-in) looks up that notebook's models once, in a background thread (`client/model_offers.py`), and hands them to the
server, which holds them with the session and tells the page (`models_offered`). The page asks which to apply
(`web/src/modelOffer.ts`); Apply merges them into the current model. The notebook is named by `folder/file` in lower case,
the same on every machine. If fi-clients is missing or the sign-in fails, the lookup reports why (the Open list shows
it) and nothing else changes; `SWISSARMYGRID_MODELS=0` turns it off. `sag.import_erds()` does the same on request, and
the agent commands `relationships_offered`, `relationships_apply_offered` and `relationships_dismiss_offered` see and
answer the offers. `tools/live_atlas_check.py` is the one check that uses the real FI-Atlas end to end.

The ribbon's Column and Cell buttons act on the selected columns or cells, which only the browser
knows, so their commands take the column names or values as arguments.

The commands that return cells straight from the page (`row_read`, `cell_view`, `table_profile`, a
SELECT in `steps_run_sql`, `compare_rows`, the header and selection statistics) answer an agent only at
privacy level 3; below it the server refuses them and the catalog marks them unavailable.
`agent_read_grid` and `agent_query` are the agent's door for values at every level.

`agent_read_grid` returns the grid's view as CSV. The agent picks `offset` and `limit`, and every read
starts with the view's total `row_count`, which the step commands report too. The privacy level caps a
read: level 0 gives the count and the column names only, level 1 gives 20 masked rows, level 2 gives
200 and level 3 (the default) gives up to 1,048,576 (an Excel sheet) with no masks. Below level 3,
rows wait for the user's click unless they allowed automatic reads.

Left out on purpose, because the server refuses an agent or they are the user's to do: changing the
privacy level, resolving an approval, shutting the server down, the audit log,
pushing table contents (an agent passes a path to `datasets_load`), and opening a table from
a Python session. Zoom, column widths and the theme live in the browser, so no command reaches them.

The plugin's `UserPromptSubmit` hook runs `swissarmygrid-context`, which adds a short summary of
the table in front (grid, row count, columns and types, steps; never cell values) to each prompt,
the way an IDE hands Claude the open file. It prints nothing when no grid page is open.
Build the one wheel (the page inside it) with `python build.py`; `python build.py --vsix`
packages the VS Code extension around it too.

## Iterate on the page

```powershell
cd web
npm run watch        # rebuilds on save; reload the tab
npm run typecheck
```

## Checks (run before a commit)

```powershell
.venv\Scripts\python -m pytest -q                 # 300+ tests, including the browser ones
.venv\Scripts\python -m pytest -q tests\e2e       # just the browser tests (Edge, headless)
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
.venv\Scripts\python -m mypy
cd web; npm run typecheck
```

`tools\screenshots.py` photographs every screen into `docs\screenshots\`, and
`tools\vscode_check.py <url> <png>` checks a grid inside VS Code's browser.
