# SwissArmyGrid standalone: one local server, separate workspaces

Status: groundwork done (2026-11-12). Phases 1-5 not started.

## Why

Today every notebook, script and VS Code window shares one workspace: one table list keyed only by
name (two notebooks pushing `df` overwrite each other), one grid registry, one DuckDB, one 50-tab cap,
every live update broadcast to every page, and SQL and the agent can read every table. VS Code is
also the only thing that keeps the server running. The goal is a product that stands on its own:

- installed with uv, started from any Python with one call;
- each launch is its own workspace, with its own scoped grids;
- an in-app Python terminal, so VS Code is optional;
- workspaces that save, together with the Python that made them;
- a Power BI style relationships view, with optional autodetection, replacing the FI-Atlas ERD.

## Decisions (made with the owner)

1. **One package, two installs.** `uv tool install "<wheel>[server]"` once per machine (the server,
   the `swissarmygrid` and `sag` commands, the MCP server); `uv pip install <wheel>` in any Python
   environment (the client: standard library only). `python build.py` builds the wheel with the page
   inside it.
2. **One local server, many workspaces.** Still 127.0.0.1 only, token on every request.
3. **Each launch is its own workspace.** `sag.create_workspace()` from Python (or
   `swissarmygrid open <name>` from a terminal) starts the server if needed, creates a workspace, ties
   the calling Python process to it and opens it.
4. **The agent is MCP only.** The MCP server exposes everything, by workspace: every tool takes a
   workspace, and `workspaces_list` names them. The agent cannot type into the Python terminal.
5. **The Python terminal is off until the user launches it** from the page. Nothing runs Python on the
   user's behalf before that click.
6. **New dependencies approved:** `pystray` and `Pillow` (tray icon), `jupyter_client` and `ipykernel`
   (the terminal's kernel), xterm.js (the terminal on the page), elkjs (diagram layout). Each is added
   in the phase that first uses it, not before.
7. **The FI-Atlas ERD feature goes.** The `atlas` extra (fi-clients from a private repository) is
   already removed; the code behind it goes in phase 4, when the relationships view replaces it.

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

## Phase 2: the Python terminal

- A terminal panel per workspace, off until the user clicks it. The click starts an IPython kernel
  (`jupyter_client`) with the watcher on, bound to that workspace. xterm.js draws it on the page.
- Every table a command creates appears in the Open list; a setting auto-opens new ones as grids.
  Reassigning a variable refreshes its open grid (the watcher already does this).
- The kernel's history is kept per workspace: it becomes the saved code in phase 3.
- The kernel's Python is the server's own environment by default; a setting names another Python.
- Done when: in a page with no VS Code anywhere, `df = pd.read_csv(...)` in the terminal shows a grid.

### Code-behind: grid actions write their code into the terminal

The owner's reported bug: "Copy transformations as Python" and the SQL transformations never gave
back code that works in the user's environment; it carried imports and was not the transformation
that turns the source table into the grid's view. Generating code *after the fact*, from steps run in
the server's DuckDB, is a translation that can drift from what the user's own data needs.

With a terminal per workspace the direction reverses. A grid action (filter, sort, rename, group by,
a SQL step...) **writes the pandas or DuckDB statement into the terminal and runs it there**, against
the user's own variable, producing the next variable. The grid shows the result. So:

- the terminal's history *is* the transformation script, by construction: nothing to copy back that
  could disagree with what ran;
- the user can edit a generated line and run it again, and the grid follows;
- a step that fails, fails in the user's own Python, with the real error.

The user chooses pandas or DuckDB SQL per workspace (DuckDB runs the same SQL the grid runs today).
Workspaces without a terminal (a notebook pushing from VS Code) keep today's server-side steps, and
their copied code gets fixed separately (task: the copied code must be one runnable function or
statement over the user's own variable, with no stray imports).

## Phase 3: save and open a workspace

A saved workspace is a folder (`<name>.sagw/`):

- `code.py`: the Python that built it (the terminal's history, and the source of scripts that pushed
  to it, where the watcher can see it);
- `data/<table>.parquet`: a snapshot of each table;
- `workspace.json`: tabs, grids and their steps, relationships, the diagram layout;
- `environment.txt`: the Python version and package versions, so the code can be re-run.

Opening loads the snapshots (fast and exact); "Re-run code" rebuilds from `code.py` for fresh data.
(Proposed default; to confirm.)

## Phase 4: the relationships view (Power BI's Model view, near enough)

Replaces the Mermaid ERD tab and the FI-Atlas model features.

- Tables as draggable cards listing their columns; lines with `1` and `*` at the ends, a filter
  direction arrow, dashed when inactive. Laid out automatically with elkjs; positions saved.
- Drag a column onto another to create a relationship; double-click a line to edit it (columns,
  cardinality, filter direction, active), with a preview of matched and unmatched rows.
- Manage relationships list; zoom, fit, and "Auto layout" again.
- Relationships drive the grid: key columns marked in headers, compare keys pre-ticked (as today),
  and later cross-filtering between related grids.

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

- The terminal makes the local server able to run code, not just read data. Only the page with the
  workspace's token can start or type into it; never the MCP (decision 4); never before the click
  (decision 5). Host and Origin checks (`server/security.py`) stay on every route.
- Saved workspaces hold data snapshots: they are written only where the user saves them.
