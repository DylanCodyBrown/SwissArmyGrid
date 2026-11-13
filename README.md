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
- **Steps** (like Power Query's applied steps): filter, sort, choose and rename columns, group
  by, distinct, keep first rows, clean text, bucket, categorize and SQL. The grid has the whole
  page: a strip under its title counts the steps and offers Undo, Clear, Copy as SQL / Python and
  To Python; **Edit steps** opens Table Transformations in a tab of its own, where each step can
  be edited, turned off or reordered, with row counts as it goes. A step list can be saved as a
  recipe.
- **Cell viewer:** double-click a cell. JSON objects and arrays show as tables you can open one
  layer at a time, markdown is formatted, and long text wraps.
- **Text matching** (the ribbon's Table row): Match case, Wildcards (`%` only, as in SQL LIKE),
  Trim, Empty = blank.
- **Column profile:** a popup from the Column row's Profile: statistics and value counts; click a
  value to filter on it. **Auto width** fits every column (Table row) or the selected ones.
- **SQL lives in your Python** (DuckDB SQL does the transforming): **To Python** hands a grid's steps
  to Jupyter or IPython as DuckDB code over your table by its own name, checked against the grid:
  copied, and `sag.paste()` puts it in the cell. A SQL step (Edit steps, Add a SQL step) is one
  guarded SELECT over the table entering it.
- **New table from a query:** the **+** after the last tab opens a SQL tray over a blank grid.
  A table name reads that table as its tab shows it (after its steps); Run turns the result into a
  table of its own, with every tool a pushed table has. The tray stays on the tab, so Run again
  rebuilds the table in place, and its own steps apply to the new rows.
- **Copy transformations as SQL or Python:** the view's steps, SQL steps included, as one DuckDB
  query over your own table by its variable name. **Copy as SQL** gives the query; **Copy as
  Python** gives `import duckdb` and one line, `orders_view = duckdb.sql("""...""").df()` (`.pl()`
  for Polars, `.to_arrow_table()` for Arrow). Longer pandas and plain-Python translations are
  there too. The copy is run exactly as copied, in a fresh namespace holding only your table,
  and compared with the grid before it's offered; only verified code gets the Copy buttons.
- **Relationships and tangs:** **Find relationships** works out how every open table joins, from
  the data: each join with its kind, the share of values that match, the orphans and why (names that
  don't match, a text column of numbers, two-column keys and second paths are handled; small number
  columns that overlap by accident are not suggested). Accept the joins, **Run** them to see which
  hold, and **Save as tang**: a `.tang` file with a name, description and tags, kept in your tangs or
  in a shared folder (the blade folders), where your coworkers' Relationships window lists it.
- **Packs:** blades and tangs a team keeps in a GitHub repository that is also a Claude Code plugin
  marketplace. Install a pack with `/plugin install` and its blades and tangs show up, marked with the
  pack; `/plugin marketplace update` brings the team's latest.
- **Analyze** (the Table row): analyses that hand off to each other, to help predict a metric. **Odds**:
  how often each value shows up, and with an outcome, each value's odds of it against the rest ("West:
  late 46% of the time, against 15% elsewhere: 4.85 times the odds"). **Find predictors**: every column
  tested against a target by chi-square, ranked by strength, with the pairs that say the same thing.
  **Fit test** and **Outliers** (below). **Show the math** on any result puts its proof in the Sandbox:
  each formula with the result's own numbers, worked to the values shown. Every result can go to the Sandbox, open as a grid, or go to
  the notebook as Python (`swissarmygrid.stats`, the same code the window ran). **Regression**: linear or
  logistic (odds ratios), effects in words with ranges, what-if predictions, predicted and miss columns,
  and Check the misses (outliers of what the model cannot explain). Each form has a **Data shape**
  cheat sheet: a made-up table in the shape it needs, one that does not work, and a check of your grid.
  **Time series**: a metric per period from a date column, its trend, its season in words ("December
  runs 31% above a typical month"), the periods that stand out (Show these rows), steps in the level and
  how much one period predicts the next. **Forecast**: trend, season and steps carried forward with 80%
  and 95% ranges, predictors each with their own future, and the miss it earns on held-back periods.
- **The Sandbox as a report:** results arrive as a readable note (with real tables) and a chart side by
  side; drag cards to arrange them, edit any note, see it as letter or landscape pages, **Print to PDF**,
  or **Export markdown** (one file, charts embedded as pictures, ready for an AI model). Right-click a
  cell or a header to send a value, a row, a profile or distinct values; every details window has
  Send to Sandbox.
- **Fit test and outliers:** select columns and ask whether their values spread as expected (chi-square
  against even shares, Benford's law on the first one or two digits, another grid's spread, or two
  columns together), with the verdict in words and an effect size; or find the outliers in a column, by
  amount (robust median fences, Tukey's quartiles or z-scores, within each vendor or month if you like)
  or by frequency (a typo seen once, an amount copied forty times). **Show these rows** adds a step
  that keeps just them; **Exclude these**, one that leaves them out.
- **Key Finder:** pick another grid (or every other grid) and get the likely join columns:
  each column pair that shares values, with the percent of each side's distinct values found in
  the other, whether each side is unique (so which one looks like the key), and a score. Columns
  whose longest value is over 128 characters are left out. Click a pair for sample shared values
  and the values an inner join would drop.
- **Export:** CSV, delimited text, Excel or Parquet, with provenance and formula neutralization.
- **Tabs, split and compare:** every grid is a tab in one window (they wrap onto more rows, up
  to 50). Drag a tab onto another to see two side by side; the key picker opens with the Key
  Finder already run, and compare keeps both grids aligned row by row (outer, anti or cross).
- **The Sandbox tab** (always first): a dashboard of cards for any table: Vega-Lite charts, notes,
  result tables, Mermaid and Graphviz diagrams, Plotly and ECharts figures, and images. Every card
  resizes from its corner on a 12-column grid, and what it draws fills it. Charts open large with
  wheel zoom, and clicking a bar filters the grid. The page loads nothing from the internet: figures
  that need outside maps or pictures say so instead.
- **Agent door (MCP):** the agent sees what the page shows; its exports wait for the user's
  Allow. Settings lists its activity and has a copyable prompt that installs the MCP in any agent.
- **Blades and the catalog** (its own tab, from **Blades and catalog** in the Application row): a **blade** is any
  Python that reaches outside Python to load something (a database connection, a CSV or workbook,
  an API, a vault). Save it once, then `conn = sag.blade("Warehouse")` in any Python. Starters for
  21 databases, 5 file patterns and 8 vaults; the save screen shows the packages a blade needs and
  installs missing ones. A blade can fill the **catalog**: every schema, table and column of its
  source with its type, as a searchable tree, readable by the agent so it can write SQL in the
  source's own dialect.
- **Jupyter and Python in the terminal** are where code goes: drag a blade, table or column from the
  blades page into a cell or an editor for its line, query or name.
- **Themes:** light, dark, high contrast light and high contrast dark, following the system by
  default; Windows forced colors are supported. Zoom with Ctrl+= and Ctrl+-.

The design is [Plans/swissarmygrid-build-plan.md](Plans/swissarmygrid-build-plan.md); what has
been checked against real versions is in [docs/verified.md](docs/verified.md); decisions and
open notes are in [docs/ideas.md](docs/ideas.md).

New here? The **Tour** button (the question mark in the Application row) explains every word, grid to
pack, by lighting up each one on the page, then plays a quick matching game.

## Install it: one package, two installs

From a release (each release carries the wheel, the VS Code extension and two installers), one line:

```powershell
irm https://github.com/DylanCodyBrown/SwissArmyGrid/releases/latest/download/install.ps1 | iex
```

```bash
curl -LsSf https://github.com/DylanCodyBrown/SwissArmyGrid/releases/latest/download/install.sh | sh
```

The installer sets up uv when it is missing, then downloads the release's wheel and installs the server
with its own Python. Give it a wheel path to install one you downloaded instead.

Or straight from PyPI, with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install "swissarmygrid[server]"   # the server, once per machine
uv pip install swissarmygrid              # the client, in any project's environment
```

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

## Blades and the catalog

A blade is saved Python that reaches outside Python to load something. Write it once on the blades
page (**Blades and catalog** in the Application row, or `/catalog`), then use it anywhere:

```python
import swissarmygrid as sag

conn = sag.blade("Warehouse")  # runs the saved code here and returns its variable
sag.blade("Azure vault")  # a vault blade: reads its secrets live, sets environment variables
sag.blade("Warehouse login")  # a secret blade: asks for values this Python does not have yet
sag.blades()  # every blade's name, favourites first
sag.blades(search="orders-function-app")  # by name, description or tag
sag.blades(tag="sales")
```

`sag.sandbox(fig, "Title", key="monthly")` puts a matplotlib, seaborn, Plotly, Altair or graphviz
figure, a picture, a DataFrame's first rows, Markdown text or a file in the Sandbox; the same `key`
replaces the card in place when the cell runs again.

- **Kinds:** SQL connection (with its dialect), other (any code), vault (Azure Key Vault, AWS,
  Google, HashiCorp, 1Password, a `.env` file, the operating system's vault: each secret mapped to an
  environment variable), secret (variable names only; the values are typed each session) and
  **analysis**: your own analysis, `def analyze(df, columns): ...` over the grid's rows as a pandas
  DataFrame, returning markdown, a table or a figure. It shows under **Your analyses** in the Analyze
  menu, runs in a child process like Refresh, and its result can go to the Sandbox. Three starters.
- **Secrets:** `secret("password")` in a blade's code. Its value is kept in the operating system's
  vault (Windows Credential Manager, macOS Keychain), never in the blade's file; an environment
  variable of the same name works too.
- **Packages:** the save screen reads the code's imports, names the pip packages (and calls such as
  `read_excel`, which needs openpyxl), and installs missing ones into SwissArmyGrid's Python on your
  click. `sag.blade()` turns a missing driver into the exact `uv pip install` command.
- **The catalog:** a blade's catalog command (SQL on its connection, or Python) returns one row per
  column of the source; **Refresh** runs it in a child process and loads it. Or load one directly:

```python
sag.load(conn.execute("SELECT * FROM INFORMATION_SCHEMA.COLUMNS").df(), name="Warehouse", dialect="sqlserver")
```

### Portable blades, blade folders, tags and favourites

A blade travels as one plain Python file, `<name>.blade.py`: its own code under a header, like a
script's inline metadata, that holds what the app needs (TOML):

```python
# /// swissarmygrid-blade
# format = 1
# name = "Orders app DB"
# kind = "sql"
# description = "The database the orders function app reads"
# tags = ["orders-function-app", "sales"]
# dialect = "postgres"
# variable = "conn"
# secrets = ["password"]
# ///
import psycopg

conn = psycopg.connect(host="db.example", user="reader", password=secret("password"))
```

- **Only names of secrets travel**, never their values: the person importing types their own.
- **Import** (the up-arrow above the blades, or drop `.blade.py` or `.zip` files on them) shows every
  blade first: its code, its description and tags, the secrets it asks for, and whether its name is
  taken (keep both, replace or skip). Importing runs nothing. **Export** gives one blade's file (on its
  save screen) or every blade as a zip.
- **Blade folders** (the folder button, or `SWISSARMYGRID_BLADE_FOLDERS`, paths separated as in `PATH`):
  folders searched, four levels deep, for Python files that *start* with the header. Ordinary Python
  files, virtual environments, `node_modules` and hidden folders are left alone, so blades can live in
  a Git repository beside other code. A folder blade saves back into its own file; removing it is
  yours to do in the folder. A blade kept by the app wins a name clash.
- **Description and tags** on the save screen. The search above the blades finds a blade by its name,
  its description or any tag, and the tag chips narrow the list: tag a blade with the app it serves
  and searching for that app finds it.
- **Favourites:** the star pins a blade to the top. Kept on this computer, never in the file.
- **Fill in your values.** A blade from someone else is written with their server, database, user or
  file path. SwissArmyGrid finds these settings in the code (read, never run): connection arguments
  such as `host=` and `dbname=`, the parts of a connection string (`Server=...;Database=...;`), and top-level
  values and paths (`path = Path(r"C:\data\orders.csv")`). The import review and the save screen show
  a field for each, mark the ones still holding an example value (`my-server`, `<your host>`, a
  starter's path) as **needs your value**, and suggest values from your other blades and from standard
  environment variables such as `PGHOST`. Whatever you type is written into exactly that place in the
  code. Passwords are never settings: they stay `secret("...")`, and the import review lets you type them
  straight into this computer's vault.

### Packs: blades and tangs from a Claude Code plugin marketplace

A **pack** is a Claude Code plugin with a `blades/` folder (`.blade.py` files) and a `tangs/` folder
(`.tang` files). A team keeps its packs in a GitHub repository that is a plugin marketplace
(`.claude-plugin/marketplace.json`); each person installs a pack once in Claude Code:

```
/plugin marketplace add <owner>/<repository>
/plugin install sales@team-blades
```

- **Installed packs** are found in Claude Code's plugin folder (`~/.claude`, or `CLAUDE_CONFIG_DIR`, or
  `SWISSARMYGRID_CLAUDE_DIR`; the cache follows `CLAUDE_CODE_PLUGIN_CACHE_DIR`). Their blades are on the
  blades page and their tangs in the Relationships window, marked **pack**, read-only: Claude Code
  replaces the installed copy on every update. **Save a copy** keeps your own; Test and Refresh the
  catalog work without saving. A pack switched off in Claude Code adds nothing.
- **Updates:** `/plugin marketplace update team-blades`. A pack with no `version` (as made here) is
  updated by every commit.
- **Making one:** **Packs** on the blades page, then **New marketplace**, or from a terminal:

  ```
  sag pack new ./team-blades --pack sales --owner "Sales team"
  ```

  It writes the marketplace, one pack and a README with the commands, and (from the page) adds the
  folder as a blade folder. Push it to GitHub.
- **Adding to one:** a clone of the repository in your blade folders is an **editable** pack. **Add to
  pack** copies chosen blades and tangs into it (files already there are kept unless you ask to
  replace them); then commit, open a pull request, and once it is merged everyone updates.
- **Trust:** a pack's blades are its maintainers' code, run on your computer when you use one, as with
  any plugin you install. Secrets never travel in a pack, only their names. The agent cannot list,
  make or fill packs.

**This repository is a marketplace too.** `/plugin marketplace add DylanCodyBrown/SwissArmyGrid`
offers two plugins: `swissarmygrid` (the user guide as a skill, and the agent tools, the
`swissarmygrid-mcp` server the installer puts on your computer; remove a copy you added by hand so the
tools are not listed twice) and `sample-store`, an example pack with a made-up blade and a tang holding
the ten relationships of `examples/sample_store.py` (push its tables with
`python examples/sample_store.py --push`, then **Run** the tang).

The agent reads catalogs with `catalog_list`, `catalog_schemas`, `catalog_tables`, `catalog_columns`
(with a first SELECT quoted for the source) and `catalog_search`.

**The agent can make blades for you.** `blades_create` proposes one: a window opens on the page with
the blade's whole file, code included, and it is saved only if you press **Allow** (the blades page,
and the grid page show it). `blades_list` gives it your blades' names,
descriptions and tags (so it can reuse one) and `blades_templates` the starters. It never sees an
existing blade's code or any secret, never sends a password (it writes `secret("password")` and you
type the value), and cannot change, remove or run a blade.

## Code to your Python

**To Python** (on an analysis result, or the grid's steps strip) hands code to Jupyter or Python in the
terminal: it is copied to the clipboard and queued, and in Jupyter or IPython

```python
sag.paste()  # this cell becomes the code (Jupyter), or it waits at the prompt (IPython)
```

puts the newest in place, its imports first. Nothing runs until you run it. A grid's steps go as DuckDB
code over your table by its own name, and only when the server ran that code and it matched the grid.
The queue lives in memory (the last 20); the agent can never send code to your Python.

Tables your Python makes still show in the grid page's Open list: importing `swissarmygrid` in Jupyter
or IPython starts the watcher (`sag.watch()` in a plain script).

**SQL completion.** In Jupyter and IPython, Tab inside SQL (a string with a SELECT, FROM or JOIN, or a
`%%sql` cell) offers the loaded catalogs' tables and columns; `orders.cu` Tab gives orders' columns.
It asks the running server and never starts one (`SWISSARMYGRID_COMPLETE=0` turns it off).
**Script as:** right-click a catalog table for SELECT \*, SELECT the columns, or pandas over the
blade's connection, copied and ready for `sag.paste()`.

## The agent commands: `sag` and the MCP server

Every action the page has is a named command in one registry (`swissarmygrid/cli/commands.py`), 95 of
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
  the whole tool list is about 5,000 tokens for 95 tools (a test holds it under a budget). `guide` and
  `commands` (the catalog) carry the detail. A command added
  to the registry becomes a tool with no change to the MCP server.
- `sag` is the same commands in a terminal (standard library only, in the plain wheel):
  `sag tabs_new_query top "SELECT ..."`, `sag steps_add g1 filter @step.json`,
  `sag agent_events --seconds 60`. `@file` reads a file and `-` reads standard input. `sag --help`
  prints the catalog.

`agent_events` listens to the server's live broadcasts (a grid changed and who changed it, a table
opened, an export approval asked for), so an agent can work alongside the user instead of polling.

`app_snapshot` returns a PNG of the page as the user sees it (the whole window, or the ribbon, grid,
steps strip or sandbox). The server draws nothing: it asks the open page, which draws itself with
html-to-image and uploads the picture, and the server hands it to the agent without writing it to disk.
The MCP returns it as image content; `sag app_snapshot` saves it and prints the path. It shows real
cell values, and each one is written to the audit log.

The ribbon's Column and Cell buttons act on the selected columns or cells, which only the browser
knows, so their commands take the column names or values as arguments.

`agent_read_grid` returns the grid's view as CSV. The agent picks `offset` and `limit` (up to
1,048,576 rows, an Excel sheet, per read), and every read starts with the view's total `row_count`,
which the step commands report too. An agent export always waits for the user's click: the export
route writes an agent's file only against the one-time ticket that click gives.

Left out on purpose, because the server refuses an agent or they are the user's to do: resolving an
approval, shutting the server down, the audit log,
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

## License

Licensed under either of the [Apache License, Version 2.0](LICENSE-APACHE) or the
[MIT License](LICENSE-MIT), at your option.

Unless you explicitly state otherwise, any contribution you intentionally submit for inclusion in
this project, as defined in the Apache-2.0 license, is dual licensed as above, without any additional
terms or conditions.
