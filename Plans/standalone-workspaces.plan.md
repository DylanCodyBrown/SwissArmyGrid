# SwissArmyGrid standalone: one local server, separate workspaces

Status: groundwork done (2026-11-12). Phases 1-5 (and 2b) not started.

## Why

Today every notebook, script and VS Code window shares one workspace: one table list keyed only by
name (two notebooks pushing `df` overwrite each other), one grid registry, one DuckDB, one 50-tab cap,
every live update broadcast to every page, and SQL and the agent can read every table. VS Code is
also the only thing that keeps the server running. The goal is a product that stands on its own:

- installed with uv, started from any Python with one call;
- each launch is its own workspace, with its own scoped grids;
- an in-app Python terminal, so VS Code is optional;
- GitHub Copilot suggestions (ghost text) in the terminal and the SQL query editors;
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
   (the terminal's kernel), elkjs (diagram layout), and `@github/copilot-language-server` (Copilot
   suggestions). Each is added in the phase that first uses it, not before. (xterm.js was dropped: see
   decision 8.)
7. **The FI-Atlas ERD feature goes.** The `atlas` extra (fi-clients from a private repository) is
   already removed; the code behind it goes in phase 4, when the relationships view replaces it.
8. **The terminal is a Monaco input with an output log, not a character terminal** (like VS Code's
   Python Interactive window). A character terminal (xterm.js) cannot show inline suggestions or hold
   an editable multi-line command; Monaco, already the SQL editor, can. It is not a shell: full-screen
   terminal programs do not run in it, and it is for Python.
9. **Copilot is optional and per person.** Off until the person signs in with their own GitHub account
   (GitHub's device-code sign-in) and their own Copilot subscription; nothing is shared or stored by
   SwissArmyGrid. Without it everything works as before. The suggestion source is behind one interface,
   so another model can replace Copilot later.

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
  (`jupyter_client`) with the watcher on, bound to that workspace.
- On the page (decision 8): a Monaco editor for the input (Python highlighting, completion of the
  workspace's variable and column names, multi-line editing; Enter runs, Shift+Enter adds a line, Up
  recalls history) above which a log shows each command with its output, errors and rich results.
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

## Phase 2b: GitHub Copilot in the SQL editors and the terminal

The SQL query editors (the SQL box, a SQL step, the "+" tab's query tray) can take it before the
terminal exists, since they are Monaco already: they are the first test of it.

- **The server runs Copilot.** It starts `@github/copilot-language-server` (the Language Server
  Protocol server GitHub publishes for editors) once, on first sign-in, and relays to it; the page
  never talks to GitHub. Its standalone build is preferred, so a machine needs no Node.js; if that
  cannot be used, Node.js becomes a requirement of this feature only.
- **Sign-in:** Settings, "Sign in to GitHub Copilot": the server asks the language server for a device
  code, the page shows the code and the GitHub page to enter it on, and the session stays signed in
  on that machine (the language server keeps it, as in any editor). "Sign out" ends it.
- **Suggestions:** Monaco's inline completions provider asks the server, which sends
  `textDocument/inlineCompletion` with the editor's text as a document (`.sql` or `.py`), plus
  context Copilot cannot see otherwise: the workspace's table names, their columns and types (never
  cell values), and for the terminal the commands run so far. Tab accepts; Escape dismisses. Shown
  and accepted suggestions are reported back (`textDocument/didShowCompletion`, the acceptance
  command), as the protocol asks.
- **Privacy:** what goes to Copilot is the editor's text and those names, nothing else; Settings says
  so next to the sign-in. Off for a workspace with one switch.
- **To check before it ships to anyone else:** GitHub's terms for the language server (the "GitHub
  Terms for Additional Products and Features") for use in a local web page, and, at work, whether
  the employer allows Copilot to see query text and column names.
- Done when: signed in, typing `SELECT region, sum(` in the SQL box over the sample store offers a
  ghost-text completion that names real columns, Tab accepts it, and signed out nothing is sent.

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
- Copilot (phase 2b) is the one feature that sends anything off the machine: the editor's text and
  table and column names, only while the person is signed in, never cell values.
