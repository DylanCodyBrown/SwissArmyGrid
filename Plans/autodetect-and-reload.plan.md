# Autodetect and reload: `SOD = trino.execute(...)` is all you write

Status (2026-10-23): steps 1-5 built and shipped in 0.5.0; step 6 (docs and skills) and the
"Added while building" items are next. Replaces the push-first workflow in
0.4.5. Owner: whoever picks up SwissArmyGrid (the 0.4.5 session is finished).

## What was asked

1. Write the query line and nothing else. The table shows up in the grid's **Open** list by its
   variable name (`SOD`). Click it and the server loads it into its own DuckDB.
2. Once a table is open, re-running the cell refreshes it in place. A table nobody opened loads nothing.
3. `sag.push` stops being the way in. It stays as a quiet fallback, out of every doc, example and skill step.
4. A toolbar button in VS Code that opens the grids.
5. Works at every Python level where it can: notebooks, the interactive window, scripts, the REPL.

## What it is not (decided, and why)

- **Not shared memory.** The server and your Python are two processes. Sharing a Python environment
  shares packages, not memory, so a table is still copied to the server when you open it. Measured for
  1M rows: the stream is 48 ms, the server's DuckDB load is about 1 s. Fine at the sizes we use.
- **Not persistence.** The server keeps data in memory only, on purpose (plan section 6). Keeping tables
  across a restart would be an explicit Save writing Parquet to the user's folder. Not in this change.
- **Not "load on detection".** Detecting a table sends the server its name and size, never the rows.
  Rows leave your process only when you click Open (or when an open table follows its variable).
  Everything the server holds is readable through the agent door, so loading stays a choice.

## Design

### One mechanism for every level: scan the top-level variables of `__main__`

A notebook kernel's variables, a script's module-level variables and a REPL's are all
`sys.modules["__main__"].__dict__` (checked with IPython: it is the same dict as `user_ns`). A small
daemon thread in the user's Python reads that dict, so there is no per-host code and no Jupyter API.

A variable is a **table** when its class is one of: a pandas DataFrame, a pyarrow Table, a Polars
DataFrame, a DuckDB relation, or anything whose *class* has `__arrow_c_stream__` (a query
client's `Result` once it gains it). The check is on the class, never the instance, so objects with a magic
`__getattr__` (mocks, lazy proxies) are not poked. Skipped: names starting with `_`, IPython's own
names, lists of dicts (any config list would match), and function locals.

Tables inside a **DuckDB connection** found among the variables are listed too (`duckdb_tables()` over a
cursor, which never touches the user's connection), because `to_duck(table, con)` and
`con.execute("CREATE TABLE ...")` put tables there, not in variables.

### Client (`client/watcher.py`, standard library only)

- `watch()` starts two daemon threads, once per process; `unwatch()` stops them.
  - **Scanner**, every 0.5 s: build the entries `{name, kind, rows, columns, fingerprint}`; POST them to
    the server when they changed, and as a heartbeat every 10 s. The reply names the tables the
    server holds from this process (`following`). For each followed table whose fingerprint changed
    (a rerun assigned a new object, or a connection table's size changed) it pushes the table again
    without bringing its tab to the front.
  - **Listener**: long-polls `GET /processes/<id>/requests?wait=25`; for each `load` request it pushes
    that variable, as the dataset of the same name.
- Both threads back off 3 s when no server answers, and pick up again when one appears. Neither raises.
- **Start**: `import swissarmygrid` starts it when IPython is running (notebook, interactive window,
  `ipython`), because that import is already the user's decision to use the grid. In a plain script
  call `sag.watch()`. `SWISSARMYGRID_WATCH=0` turns it off everywhere, `=1` forces it on. The server,
  the MCP and pytest are not IPython, so they never start one.
- The session label shown in the list: the notebook file (`__vsc_ipynb_file__` when VS Code sets it),
  else the script's file name, else `Python <pid>`.
- Data goes through the existing `push()` path with two internal arguments: `_process` (header
  `X-Process`) and `_focus=False` for a follow.

### Server

- `server/processes.py`: `ProcessRegistry` of live sessions. A session expires 30 s after its last
  report. It also records which datasets each session loaded (`X-Process` on a push), so the list can say
  "open" only for the session that owns the dataset, and a close or a replacement by another session
  removes it from `following`.
- Routes (`server/routes_processes.py`), all behind the existing token guard:

  | Route | For |
  |---|---|
  | `POST /processes/{id}/sync` | the scanner: label, pid, entries; replies `following` |
  | `GET /processes/{id}/requests?wait=N` | the listener: long-poll for load requests |
  | `GET /processes` | the page: sessions with their tables and open state |
  | `POST /processes/{id}/open` | the page: queue a load of one table (`reload` re-queues it) |

  The agent (`X-Actor: agent`) can list sessions but not open anything: loading a table from a running
  Python session is the user's action.
- `PUT /datasets/{name}` accepts `?focus=0` (skip `grid_shown` and the opener) and `X-Process`.
- A `processes_changed` notice goes to the pages when a session or its table list changes.
- The uvicorn access log drops `/processes/` lines, or the heartbeats would fill server.log.

### Page (`web/src/openMenu.ts`)

An **Open** button beside the "+" in the tab bar. It opens a dialog in the existing dialog style:
sessions as groups, each table as a row (name, kind, rows × columns, state) with **Open** or **Reload**.
It refreshes on `processes_changed`. Opening waits for the dataset to arrive and the existing
`grid_shown` notice adds the tab.

### Extension (`VSCode-Extensions/swissarmygrid`)

`swissarmygrid.showGrids` gets a toolbar entry: `editor/title` for Python files and `notebook/toolbar`
for notebooks, icon `$(table)`. Grids keep opening in VS Code's own browser (Dylan: fine).

### The query client library

`Result` gains `__arrow_c_stream__` (and `to_arrow()`), with the typed column logic `to_duck` already
uses, so `sag.push(result)`, `duckdb.sql("SELECT * FROM result")`, `pandas` and Polars read it directly.
`to_duck`, `df()` and `dicts()` stay.

### Docs and skills

README, the setup skill and its guide, and the examples lead with "assign a table and open it from the
list". `push` moves to a "Fallback" paragraph that names when it is still needed.

## Limits to state in the docs

- Function locals are not seen; a script that has exited has nothing left to open (use `push`, or keep
  it alive); a debugger breakpoint freezes the scanner thread.
- Marimo is untested.
- A change made in place (`df.loc[...] = ...`) keeps the same object, so it does not auto-refresh.
  **Reload** in the list covers it.
- Two sessions with a variable of the same name share one dataset name; opening the second replaces the first.

## Build order and tests

1. **Server registry and routes**, tests first (`tests/test_processes.py`): reports, expiry, requests and
   long-poll, ownership, agent refused, token required, access-log filter.
2. **Client scanner**, tests first (`tests/test_watch.py`): the table rules on made-up namespaces, then
   an end-to-end run against a real server: detect, open, rerun follows, close stops following.
3. **Page**: `openMenu.ts`, typecheck, a Playwright test for the dialog.
4. **Extension** toolbar entry, tests, new `.vsix`.
5. **Query client** Arrow interface, tests, tag.
6. **Docs and skills**.
7. Gates: `pytest -q`, `ruff check .`, `ruff format --check .`, `mypy`, `npm run typecheck`.
8. Install on Dylan's machine (uv tool + extension) and try `SOD = trino.execute(...)` for real.

## Added while building (Dylan, 2026-10-23)

1. **Remove the Agent tab's terminal.** Claude is already in VS Code, so the grid does not need to
   launch one. Goes: the pty websocket in `routes_terminal.py`, `web/src/agentTerminal.ts` and
   `lazy/terminal.ts`, the `@xterm` packages, `pywinpty`/`ptyprocess` in the `[server]` extra, the agent
   command setting, and the agent-workspace folder with the guides copied into it
   (`workspace_docs`, which also removes the force-include that stops a clean checkout from running
   `uv sync`). The Analysis Sandbox, the MCP door and the prompt hook (`routes_context.py`) stay.
   Its own commit, so it can be reverted alone.
2. **Grid tools for GitHub Copilot, without MCP.** The extension registers them with the Language Model
   Tools API (`vscode.lm.registerTool` and `contributes.languageModelTools`, VS Code 1.95+), in its own
   process. Claude Code keeps using the MCP server. Both call the same grid routes as the agent
   (`X-Actor: agent`), so the one privacy policy and the approval prompts apply to Copilot too. First
   set, read-only: list datasets, describe, profile, query, read rows, open a grid. Whatever a tool
   returns goes to Copilot's model, so check that Copilot is approved for this data first.
3. **One server per VS Code workspace.** Its own port and its own tabs and memory. The server already
   allows it (its lock is named after the connection file's path and `SWISSARMYGRID_INFO` moves that
   file); the extension and the client need a per-workspace connection file and a way for a Python
   process to find its own. Needs a plan of its own. It isolates data, not Python environments.

4. **The grid follows VS Code's theme.** The Settings theme picker and the stored choice go. The
   extension sends the active theme kind (light, dark, high contrast light, high contrast dark: the grid
   already has exactly these four) over its existing socket; the server keeps the latest and tells
   every page, including a page that connects later. Outside VS Code the page falls back to the
   system's light or dark. This matches the theme *kind*, not VS Code's exact palette: the grid sits in
   a browser frame that cannot read VS Code's color variables.

## Deferred, in this order

1. **Add to notebook**: write the grid's verified Python back as a notebook cell
   (`NotebookEdit.insertCells`, undoable, never writes the file).
2. **Starting the watcher in scripts without a line**: the extension injects it for terminals and
   debug launches; kernels may not pick it up. Test before building.
3. **Persistence**: an explicit Save to Parquet in the user's folder, read by both sides.
4. **Memory-mapped exchange** for tables over about a million rows.
5. **Debugger feeder** through the debug protocol (also reaches function locals).
