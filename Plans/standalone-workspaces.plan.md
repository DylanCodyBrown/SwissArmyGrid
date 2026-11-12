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
- a Power BI style relationships view with optional autodetection, replacing the FI-Atlas ERD.

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
7. **The FI-Atlas ERD feature goes.** The `atlas` extra is already removed; the code behind it goes in
   phase 4, when the relationships view replaces it.
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

## Groundwork (done)

- The server, its page and the VS Code extension are one repository: the Python package at the root,
  `web/`, and `vscode-extension/`. The uploaded zip and .vsix are gone; the team's model files
  (`src/swissarmygrid/models/*.json`) were left out.
- `build.py` builds the page and the one wheel (`--vsix` also packages the extension around it);
  `deploy_plugin.py` (the company marketplace) is gone. Verified: the wheel installs as a uv tool,
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
- Tray icon (`swissarmygrid tray`): Open, the workspace list, Restart, Start with Windows (a Startup
  folder shortcut), Stop and quit. A server started by the tray or the command line never stops on its
  own; one started by VS Code still ends with VS Code.
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
  in the grid list, opening it shows the grid, and editing the cell refreshes it.

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
- **Proposed (owner, 2026-11-12; to confirm): retire the grid page's SQL box and Table Transformations
  panel** once the notebook writes transformations as SQL cells: transforming then lives in the
  notebook's tab (decision 12), and the grid page keeps analysis (grid, profile, charts, Key Finder,
  compare, relationships). Until the notebook exists the panel stays, since it is the only way to
  transform a grid today.
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

## Phase 3: save and open a workspace

A saved workspace is a folder (`<name>.sagw/`):

- `notebook.py`: the workspace's marimo notebook, which is the Python and SQL that built it;
- `data/<table>.parquet`: a snapshot of each table;
- `workspace.json`: tabs, grids and their steps, relationships, the diagram layout;
- `environment.txt`: the Python version and package versions, so the notebook can be re-run.

Opening loads the snapshots (fast and exact); "Run notebook" rebuilds from `notebook.py` for fresh
data. (Proposed default; to confirm.)

## Phase 4: the relationships view (Power BI's Model view, near enough)

Replaces the Mermaid ERD tab and the FI-Atlas model features.

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
