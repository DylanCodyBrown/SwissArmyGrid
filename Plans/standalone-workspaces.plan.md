# SwissArmyGrid standalone: one local server, separate workspaces, marimo built in

Status: groundwork done (2026-11-12); marimo checked and chosen (2026-11-12). Phases 0-5 not started.

## Why

**What SAG is for (the owner's words):** SAG exists to make Python-to-DuckDB work easier, instead of
panning across three screens (the code, the SQL, the data). SAG brings what makes Python powerful,
analysis and fast data, and lets **DuckDB and SQL do the transforming**.

What stands in the way today: every notebook, script and VS Code window shares one workspace (one table
list keyed only by name, so two notebooks pushing `df` overwrite each other; one grid registry, one
DuckDB, one 50-tab cap; every live update to every page; SQL and the agent can read every table).
VS Code is the only thing that keeps the server running. And the copied code (Python or SQL) never
reliably transformed the source table back in the user's own environment.

The goal, per workspace, without panning:

- **the notebook** (marimo: Python cells and DuckDB SQL cells, with a terminal panel), in its own
  browser tab, on one screen, and
- **the grids** (SAG: scroll millions of rows, profile, Key Finder, compare, charts, relationships),
  in theirs, on another (decision 12);
- every transformation is a **DuckDB SQL query** over the user's own table, so the code is the
  transformation, not a translation of it;
- installed with one installer that sets up everything; VS Code optional;
- workspaces that save as a notebook file plus data snapshots;
- a Power BI style relationships view with optional autodetection.

## Decisions (made with the owner)

1. **One package, two installs.** `uv tool install "<wheel>[server]"` once per machine (the server, the
   `swissarmygrid` and `sag` commands, the MCP server, marimo); `uv pip install <wheel>` in any Python
   environment (the client: standard library only). `python build.py` builds the wheel with the page.
2. **One local server, many workspaces.** Still 127.0.0.1 only, token on every request.
3. **Each launch is its own workspace.** `sag.create_workspace()` from Python (or
   `swissarmygrid open <name>` from a terminal) starts the server if needed, creates a workspace, ties
   the calling Python process to it and opens it.
4. **The agent is MCP only.** The MCP server exposes everything, by workspace: every tool takes a
   workspace, and `workspaces_list` names them. The agent cannot run code in the notebook.
5. **Nothing runs Python until the user starts it.** The notebook's kernel starts on the user's click
   (or when the user opens a saved workspace and presses Run).
6. **New dependencies approved:** marimo (the notebook, decision 8), `pystray` and `Pillow` (tray icon),
   elkjs (diagram layout), jupytext (marimo's converter needs it for `# %%` scripts). Each is added in
   the phase that first uses it. (Dropped: xterm.js, `jupyter_client`/`ipykernel` as our own kernel,
   our own Monaco terminal; marimo replaces them.)
7. **The old notebook-model lookup goes.** It looked up a notebook's ERD models in a private service;
   its optional extra and all of its code are removed. The relationships view (phase 4) covers the need.
8. **marimo is the Python side, built in** (it replaces building our own terminal). Checked on
   2026-11-12 with marimo 0.25.2 (Apache-2.0, about 130 MB, Starlette and uvicorn like SAG):
   - `marimo convert` turns a `# %%` script into a notebook and renames reassigned variables
     (`df = df.dropna()` in a later cell becomes `df_1`), so ordinary scripts follow marimo's
     one-definition rule; a script with no markers becomes one cell, so SAG adds markers first;
   - `marimo edit --headless` has its own token (`--token-password`) and `--base-url`, so SAG can
     serve it under its own address in a tab of its own (decision 12);
   - `sag.push(cleaned)` in a marimo cell reached the SAG server as dataset `cleaned`;
   - its SQL cells run DuckDB over Python data frames by name: the transforming half of SAG's purpose.
   Not yet checked: its terminal panel on Windows (added for macOS and Linux first; Windows shells
   came later), and its Copilot setting's needs (decision 9).
9. **Copilot comes through marimo**, optional and per person: marimo's own AI completion setting, with
   the person's own GitHub account and Copilot subscription. SAG stores nothing. (Its requirements,
   such as Node.js, to be checked; the installer covers whatever it needs.)
10. **The installer installs everything the user needs.** One command, no other steps (phase 0).
11. **Division of labour: DuckDB SQL transforms, Python analyses.** Grid transformation steps are
    DuckDB SQL; the grid, profile, charts, Key Finder, compare and relationships are SAG's analysis on
    top. pandas stays for loading and for Python the user writes, not for generated transformations.
12. **The notebook and terminal always get their own browser tab, never the grids' page** (the
    owner's rule), so each can sit on its own screen. The grid page links to its workspace's notebook
    ("Open notebook", which opens or focuses that tab) and the notebook links back; the two stay in
    step through the server (a grid action writes its cell into the open notebook; running a cell
    refreshes the open grid), never by being framed in one page.
13. **The grid page is for the grid and analysis only.** No SQL box or transformations panel on it
    (they move to the notebook, decision 12), which gives the grid the whole page.
14. **Blades: saved Python, kept by SAG** (the owner, 2026-11-12). A blade is a fold-out tool on the Swiss Army knife (the owner named them): a store of saved code, so
    code is written once instead of retyped into every script. **Any time Python reaches outside itself to
    load something, that is a blade** (the owner's words): a database, an API, a CSV, a workbook. **Anything can be a blade**: the
    user highlights code in the notebook and presses **Save as blade** on its toolbar (until the
    notebook exists, pastes it into the page's form). The save screen names it, marks it a **SQL
    connection** (with its dialect) or **other** (an API client, a file path, any setup), names the
    variable it produces (found automatically: the last name the code assigns), and optionally **adds it
    to the catalog** with a SQL or Python command that returns the catalog table.
    `conn = sag.blade("Warehouse")` in any Python runs the saved code there and returns that variable;
    in the notebook a blade drags in as that line. Saved under `~/.swissarmygrid/blades/`,
    shared by every workspace on the machine. The agent never reads or writes a blade's code.
15. **Secrets live in the operating system's vault** (Windows Credential Manager, macOS Keychain) through
    `keyring` (new dependency, approved). Blade code says `secret("password")`; the file never
    holds the value.
15a. **Vault blades** (the owner, 2026-11-12): a third kind of blade beside SQL and other. **Add a vault**
    is a simple window: the vault's type (Azure Key Vault, AWS Secrets Manager, AWS Parameter Store,
    Google Secret Manager, HashiCorp Vault, 1Password, a `.env` file, the operating system's vault), its
    address, and a table mapping each secret in it to an environment variable (`warehouse-pwd` ->
    `WAREHOUSE_PASSWORD`), with a Test button. Running a vault blade (`sag.blade("Azure Vault")`, or before
    a blade that uses it) fetches the secrets live and sets those variables; SAG never stores their
    values. `secret("WAREHOUSE_PASSWORD")` in any blade reads the OS vault first, then the variables the
    vault blades set. The agent never sees a vault blade.
15b. **Secret blades** (the owner, 2026-11-12): a fourth kind. It saves only environment variable names
    (`WAREHOUSE_USER`, `WAREHOUSE_PASSWORD`), never their values. Using it asks only for the values this
    Python does not have yet (none on a second use; all again after the kernel restarts and wipes them), each in a hidden
    prompt: `getpass` in a terminal, a small window in the page or notebook) and sets it in that Python's
    environment only, so the values are gone when the kernel or process ends and are typed again next
    session. A catalog Refresh that needs them asks in the page and hands them to that one run. Lookup
    order for `secret("X")`: the blade's own saved secret (OS vault), then environment variables (set by
    vault and secret blades).
16. **Refresh runs on the SAG server**, on the user's click: the blade's code and catalog command run
    in a child process of the server's own Python (the uv tool environment, the one marimo uses), never
    inside the server and never for the agent. Drivers are added to that environment
    (`uv tool install --with pyodbc ...`; the installer can offer the common ones). The locked DuckDB
    database is untouched: only the catalog's rows come back.
17. **A catalog is names and types only, never data.** `sag.load(table, name=...)` takes a table with
    schema, table, column and type (any `INFORMATION_SCHEMA.COLUMNS` result works as it is) and makes a
    searchable explorer: Schema -> table/view -> column -> type. Catalogs live in memory (Refresh or
    `sag.load` brings them back after a restart). The agent reads them through
    `catalog_*` tools, to write SQL against the source in its dialect.
18. **The blades and catalog navigation sits left of the notebook** in the notebook's own tab
    (decision 12). Until phase 2 it is its own page, `/catalog`, the same component.

## Groundwork (done)

- The server, its page and the VS Code extension are one repository: the Python package at the root,
  `web/`, and `vscode-extension/`. The uploaded zip and .vsix are gone; the shipped model files
  (`src/swissarmygrid/models/*.json`) were left out.
- `build.py` builds the page and the one wheel (`--vsix` also packages the extension around it);
  `deploy_plugin.py` (the old private plugin deployment) is gone. Verified: the wheel installs as a uv tool,
  `swissarmygrid start` runs it, a client-only environment pushes all nine sample tables, and the
  page draws them in Chromium.
- `examples/sample_store.py`: a made-up nine-table store with an answer key (`TRUE_RELATIONSHIPS`) and
  traps (`DECOYS`, a two-column key, mismatched names and types, orphans, two paths to one table).
  `tests/test_sample_store.py` keeps the answer key true.
- The extension's path code follows the target platform's rules, so its tests pass on any machine.
- Two grid bugs fixed: the first data column was pinned, so it stayed stuck while the rest scrolled
  sideways (now only the row numbers stay); and AG Grid could read its own 42px default row height
  before the theme's CSS loaded, so row 1 was 32px and every later row 42px, with a gap (the row
  height is now given to the grid directly, and follows zoom).

## Phase 0: the installer

One command per platform, from a release:

- Windows: `irm <release>/install.ps1 | iex`; macOS and Linux: `curl -LsSf <release>/install.sh | sh`.
- It installs uv when missing (uv's own installer), then `uv tool install --force
  "<wheel>[server]"`: the server, marimo, DuckDB, pyarrow, pandas, jupytext and everything else in
  one isolated environment, on a pinned Python that uv fetches itself (no system Python needed).
- It adds a Start menu entry (Windows) and offers "Start with Windows" for the tray (phase 1).
- It prints the one line for the client: `uv pip install <wheel>` in a project's environment, or
  nothing at all when the user works only in SAG's built-in notebook.
- `swissarmygrid doctor` checks the install (versions, the page, marimo, the tray, ports) and says
  what to fix in plain words; the installer runs it last.
- Re-running the installer updates in place; `swissarmygrid uninstall` removes the tool, shortcuts
  and state folder (asking before it deletes saved workspaces: it never does by default).
- Done when: on a clean Windows machine with nothing installed, the one command ends with the tray
  icon showing and `swissarmygrid doctor` all green.

## Phase 1: workspaces, and a server that stands alone

- `Workspace` owns what is global today: `DatasetStore`, `GridRegistry`, the view cache, Python
  sessions, live-update sockets, limits. A DuckDB schema per workspace (`ws_<id>`): SQL names resolve
  inside the caller's workspace only. One memory budget is shared across workspaces.
- Addresses: `/w/<workspace>/...` for every page and API route; the page learns its workspace from
  its address. Live updates go only to that workspace's pages.
- Client: `sag.create_workspace(name=None) -> Workspace`; `sag.push` goes to the process's workspace
  (the last created one, or `SWISSARMYGRID_WORKSPACE`). Without one, `push` creates one named after
  the script or notebook.
- CLI: `swissarmygrid start [--background]`, `open [workspace]` (opens the browser with the token),
  `workspaces`, `stop`.
- Tray icon (`swissarmygrid tray`; the owner's spec, 2026-11-12): a click opens the last workspace; a
  **right click shows what is active**: every open workspace by name, with how many tables it holds and
  whether its notebook is running, each one opening that workspace's tab (and its notebook) when chosen;
  then **New workspace** (asks for a name, creates it, opens its tab), the blades page, Restart, Start
  with Windows (a Startup folder shortcut), and Stop and quit. The list is read fresh each time the menu
  opens. A server started by the tray or the command line never stops on its own; one started by VS Code
  still ends with VS Code. Not built yet: the user guide described a tray before it existed, and that
  section was removed until it does.
- MCP: every tool takes `workspace`; `workspaces_list`, `workspace_create`, `workspace_close`.
- Done when: two Python processes each `create_workspace()` and push a table named `df`, both grids
  stay correct, each page sees only its own tables, and the agent sees both by workspace.

## Phase 2: marimo built in, in its own tab

- **One marimo server** for all workspaces, started by SAG on first use: `marimo edit <workspaces
  folder> --headless`, on 127.0.0.1, with a token SAG generates (`--token-password-file`) and a base
  URL under SAG (`/notebook/`), so it is reached through SAG's own address and token. If marimo's
  server cannot sit behind SAG cleanly, SAG opens it on its own port, still token-protected.
- **Each workspace is a marimo notebook file** (`<workspace>.py` in the workspaces folder), always in
  its **own browser tab** (decision 12), with marimo's own terminal panel for shell commands. The grid
  page's "Open notebook" opens that tab or brings it forward (one tab per workspace, by a named
  window); the tray and `swissarmygrid open --notebook` open it too. Never framed in the grid page.
- **The notebook's tables become grids.** A small marimo-aware hook in the client (the watcher reads
  `__main__`, which marimo does not use) reports each cell's data frames to the workspace; opening one
  shows it as a grid, and re-running the cell refreshes it. `sag.push` keeps working as checked.
- **Robustness:** SAG supervises the marimo server like the VS Code extension supervises SAG today
  (start on demand, health check, restart if it dies, a clear message if it will not start); a
  workspace's kernel can be interrupted and restarted from the panel; the page reconnects after a
  reload (marimo already reattaches to a running session, as checked).
- Done when: with no VS Code anywhere, `df = pd.read_csv(...)` in the workspace's notebook shows `df`
  in the grid list, opening it shows the grid, and editing the cell refreshes it. **Done 2026-11-12
  (1.2.1):** the watcher starts in a marimo kernel and reads marimo's own variables
  (tests/test_notebook_grids.py runs it against a real kernel). Still to come in this phase: the
  code-behind (grid actions write SQL cells) and, with it, removing the grid page's SQL box and
  Table Transformations panel.

### Code-behind: every grid transformation is a DuckDB SQL cell

The reported bug: "Copy transformations as Python" and the SQL steps never gave back code that
transforms the source table in the user's environment (stray imports, not the actual transformation).
Generating code after the fact, from steps run in SAG's own DuckDB, is a translation that drifts.

With the notebook built in, the direction reverses (decision 11):

- A grid action (filter, sort, choose and rename columns, group by, distinct, bucket, SQL...) writes a
  **marimo SQL cell**: one DuckDB query over the user's own table by its variable name, producing a
  new variable (`orders_filtered = mo.sql(f"SELECT ... FROM orders WHERE ...")`). It runs in the
  user's kernel, against the user's data; the grid shows the result.
- The notebook therefore *is* the transformation script, by construction. Editing the SQL in the cell
  re-runs it and the grid follows (marimo's reactivity re-runs what depends on it).
- A step list compiles to one query (SAG's step compiler already produces DuckDB SQL), so "copy" gives
  a single runnable statement: `duckdb.sql("""...""")` over `orders` works in any Python with DuckDB.
- **Decided (owner, 2026-11-12): the grid page's SQL box and Table Transformations panel go**, so the
  grid gets the whole page. Transforming lives in the notebook's tab (decision 12), as DuckDB SQL cells;
  the grid page keeps analysis (grid, profile, charts, Key Finder, compare, relationships). The panel is
  removed in the same release that brings the notebook, since until then it is the only way to
  transform a grid.
- Grids pushed from outside (a VS Code notebook, a script) keep server-side steps; their "Copy as SQL"
  becomes that same single query over the source table's name, and "Copy as Python" the one-line
  DuckDB call around it: no imports beyond `duckdb`, nothing else.

### Converting Python and notebooks

- **Any `.py` to a marimo notebook:** SAG splits it into cells first when it has no `# %%` markers
  (standard library `ast`: the module docstring as a markdown cell, imports together, each function or
  class with its decorators and leading comments, top-level statements grouped by blank lines), then
  `marimo convert` (which renames reassigned variables). Files with markers go straight through.
- **`.ipynb` to marimo:** `marimo convert`. **marimo to `.ipynb`:** `marimo export ipynb`, so a
  workspace can always go back to Jupyter.
- In the page: "Open a Python file or notebook" in the workspace menu; from a terminal:
  `swissarmygrid convert <file>`.
- Tested on made-up scripts with reassignment, functions, a `__main__` block, comments, a syntax error
  (one cell and a plain message), and round trips through `.ipynb`.

## Blades and the catalog explorer (decisions 14-18)

**Part 1 (now, release 1.1.0; needs no notebook):**
- Engine `engine/catalog.py`: normalize a loaded table (column names matched case-insensitively:
  `table_schema`/`schema`/`owner`, `table_name`/`table`, `column_name`/`column`, `data_type`/`type`,
  optional `table_catalog`/`database`, `table_type`/`kind`, `is_nullable`, `ordinal_position`,
  `description`/`comment`/`remarks`); one DuckDB table per catalog in the locked database, hidden from
  grids and the SQL box; tree queries (schemas, tables of a schema, columns of a table) and search
  (every word in schema.table.column; `a.b` matches schema.table or table.column; exact before
  starts-with before contains). Limits in `Settings`.
- Blade store `server/blades.py`: one JSON file per blade (name, kind `sql` or `other`,
  dialect, code, the variable it produces, and the optional catalog command: SQL run on that variable
  through DB-API, or Python whose `catalog` variable or last expression is the table); secrets through
  `keyring`; a starting catalog query per dialect (INFORMATION_SCHEMA, SQLite's pragmas, Oracle's
  ALL_TAB_COLUMNS).
- Starter blades `server/blade_templates.py` (the owner, 2026-11-12): "New blade from a template" for the
  20 most used databases (SQL Server, PostgreSQL, MySQL, MariaDB, Oracle, SQLite, DuckDB, Snowflake,
  BigQuery, Redshift, Databricks, Azure SQL / Synapse, Db2, Teradata, Trino/Presto, ClickHouse,
  Sybase/SAP ASE, SAP HANA, Vertica, Access via ODBC) and common file patterns (a CSV file, an Excel
  workbook, a Parquet file, a folder of files). Each fills in the driver import, placeholders,
  `secret("password")`, the variable, and a catalog command: the dialect's catalog query, or for files
  Python that lists sheets or files as tables and their headers as columns. Templates are a starting
  point the user edits; the driver note says what to install.
- Packages a blade needs `server/blade_packages.py` (the owner, 2026-11-12): the save screen reads the
  code's imports (and calls that pull in an extra package, such as `read_excel` -> openpyxl), maps each
  import to its pip name (`snowflake.connector` -> `snowflake-connector-python`, `psycopg` ->
  `psycopg[binary]`, ...), and says which are missing from SAG's environment. **Install** adds them there
  on the user's click (`uv pip install --python <SAG's Python>`, never for the agent); a copy button gives
  the same `uv pip install ...` for the user's own environment. `sag.blade()` turns an ImportError into
  that exact command.
- Refresh `server/blade_runner.py`: a child process (`python -I`) gets the code and secrets on
  stdin, runs the code, takes its variable, runs the catalog command, and returns rows as JSON; a time
  limit, plain errors.
- Routes `server/routes_catalog.py`: `/catalogs` (list, load, tree, search, remove) and `/blades`
  (list, get, save, remove, refresh, python). Blade routes refuse the agent.
- Client: `sag.load(table, name, dialect=None)`, `sag.blade(name)` (runs the saved Python in the
  caller's Python, secrets fetched from the server), `sag.blades()`.
- MCP: `catalog_list`, `catalog_schemas`, `catalog_tables`, `catalog_columns` (with a SELECT quoted in
  the source's dialect), `catalog_search`. Results are names and types, wrapped as untrusted data.
- Page `/catalog` (its own tab): Blades (new, edit, refresh, copy `sag.blade`, remove; drag
  gives the `sag.blade` line) above Catalog (picker, search, keyboard tree, column details; drag gives
  the quoted name). The grid page links to it.
- Done when: a SQLite blade saved in the page refreshes into a searchable catalog, `sag.blade`
  returns a working connection in a fresh Python, `sag.load(information_schema_frame, name=...)` shows
  a 500,000-column catalog instantly, and the agent finds a column and gets a correct SELECT.

**Part 2 (done 2026-11-12, release 1.2.0, with the first piece of phase 2: marimo started by SAG on
first use and served at /marimo in its own tab):** the same navigation left of the notebook; **Save as blade** on the
notebook's toolbar takes the highlighted code into the save screen. Dropping a blade into a script
**adds any import it needs at the top when the script does not have it yet** (the owner, 2026-11-12):
`import swissarmygrid as sag` for the `sag.blade(...)` line, or the blade's own imports when its code is
dropped in full; an import already there (in any form) is never repeated. Drag a blade into a cell
(`conn = sag.blade(...)`), a table (a SQL cell over the blade's connection), a column (its quoted name).

## Phase 3: save and open a workspace

A saved workspace is a folder (`<name>.sagw/`):

- `notebook.py`: the workspace's marimo notebook, which is the Python and SQL that built it;
- `data/<table>.parquet`: a snapshot of each table;
- `workspace.json`: tabs, grids and their steps, relationships, the diagram layout;
- `environment.txt`: the Python version and package versions, so the notebook can be re-run.

Opening loads the snapshots (fast and exact); "Run notebook" rebuilds from `notebook.py` for fresh
data. (Proposed default; to confirm.)

## Phase 4: the relationships view (Power BI's Model view, near enough)

Replaces the Mermaid ERD tab.

- Tables as draggable cards listing their columns; lines with `1` and `*` at the ends, a filter
  direction arrow, dashed when inactive. Laid out automatically with elkjs; positions saved.
- Drag a column onto another to create a relationship; double-click a line to edit it (columns,
  cardinality, filter direction, active), with a preview of matched and unmatched rows.
- Manage relationships list; zoom, fit, and "Auto layout" again.
- Relationships drive the grid: key columns marked in headers, compare keys pre-ticked (as today),
  joins written as DuckDB SQL cells (decision 11), and later cross-filtering between related grids.

## Phase 5: relationship autodetection (optional, and very good)

Built on Key Finder (`engine/keyfinder.py`). Each candidate pair gets a confidence and a reason.

- Candidates: name similarity (`orders.customer_id` and `customers.cust_key`, `*_id` to a table's
  key), compatible types (text "17" meets the number 17), composite keys up to three columns.
- Evidence: the one side is unique; the many side's values are contained in it (near 100%, allowing
  orphans); distinct counts make sense.
- Penalties: low-cardinality columns that overlap by accident (status codes, years, quantities,
  true/false flags, line numbers).
- Ambiguity: a second path between the same tables is added inactive, as Power BI does.
- Optional: on push, suggestions appear for the user to accept; nothing is applied silently.
- Scored against `examples/sample_store.py`: find all of `TRUE_RELATIONSHIPS` (both calendar paths,
  one inactive; the two-column returns key) and none of `DECOYS`.

## Security notes

- With the notebook built in, SAG can run code, not just read data. Only the page holding the
  workspace's token reaches marimo (its own token, generated by SAG, never shown elsewhere); never the
  MCP (decision 4); nothing runs before the user starts it (decision 5). Host and Origin checks
  (`server/security.py`) stay on every SAG route, and marimo listens on 127.0.0.1 only.
- Saved workspaces hold data snapshots: they are written only where the user saves them.
- Copilot (decision 9) is the one feature that sends anything off the machine, only while the person
  is signed in; it sees the cell text and names the editor sends, never what SAG holds in its grids.
