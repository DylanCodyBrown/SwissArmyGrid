# VS Code extension: the server's one home, and push buttons on files

> **Superseded (the owner, 2026-11-14):** "The goal is to install everything with UV. The vscode extension
> just adds the mcp tools as vscode tools and adds the button to open Swiss army grid the same the icon
> tray does." The extension is now only that: an MCP server definition provider for the uv tool
> install's `swissarmygrid.mcp.server`, and Open SwissArmyGrid (start the server if it is not running,
> open the workspace in a window of its own). It no longer starts the server with VS Code, carries or
> updates a server, pushes files or shows grids in a VS Code tab. The installers stay uv only. What
> follows is the earlier design, kept for its reasons.

Status (2026-10-23): planned, not built. Waiting on Dylan's review of the open questions at the end.

## What was asked

1. VS Code starts the grid server when it opens. No extension icon, no panels: one status bar item
   that restarts the server.
2. A button at the top of a data file pushes it to the grid: CSV, JSON and every other type the
   server reads, plus Excel (`.xlsx`).
3. The Python import (`sag.push`) relies on the server VS Code runs, never a stray local process,
   and nothing else in the framework starts the server from somewhere else.

## Today: the server starts from whichever install asks first

| Caller | How it starts a server | Which Python runs it |
|---|---|---|
| `sag.push` in a project | `ensure_server(start=True)`, then `server_command()` | `SWISSARMYGRID_SERVER`; else the project's own Python if it happens to have duckdb, uvicorn, starlette and sqlglot; else the uv tool install; else a `swissarmygrid` on PATH |
| `swissarmygrid-mcp` (the plugin, run by uvx) | `_call` calls `ensure_server(start=True)` | uvx's cached environment: it has the `[server]` extra, so rule 2 of `server_command()` picks it |
| `swissarmygrid` / `python -m swissarmygrid.server` | started by hand | the uv tool install, or the repo's `.venv` during development |
| the prompt hook (`swissarmygrid-context`) | never: it only reads the connection file | n/a |
| `setup.ps1` | never: it stops the server before reinstalling | n/a |

`lifecycle.py` keeps it to one server at a time, so nothing crashes. But the first caller decides
which version and which packages run:

- After a plugin update, uvx holds the new wheel while the uv tool install keeps the old one until
  setup runs again. Which version you get depends on whether Claude Code or a push came first.
- A project environment that started the server has no `mcp` or `pywinpty`, so the Agent tab's
  terminal fails in it.

## Design

### 1. One owner

- The extension starts the server, always from the uv tool install:
  `<uv tool dir>\swissarmygrid\Scripts\python.exe -m swissarmygrid.server`. That is the install
  the setup skill makes and updates, so the server's version is always the plugin's.
- Nothing else starts one. `ensure_server` becomes connect-only: `push()` and the MCP use the
  server named in `~/.swissarmygrid/server.json`, or fail with "SwissArmyGrid isn't running. Start
  it from the SwissArmyGrid item in VS Code's status bar." `server_command()` and the detached
  `Popen` go. `push(start_server=...)` stays one version as an ignored argument, so existing calls
  don't break.
- Starting by hand stays, for development and for use outside VS Code (`swissarmygrid`, or
  `python -m swissarmygrid.server` in the repo). That is a deliberate start, not a stray one.
- The connection file records who started the server (`started_by`: `vscode` or `cli`) and its
  executable. `/health` already reports the version and process id.

### 2. The status bar item, the only UI

- Running, stopped, or "another copy": a server from a different install or version, such as the
  repo's `.venv`. The tooltip shows the version, port and who started it.
- Click restarts: `POST /shutdown`, wait for the connection file to go, start from the uv tool
  install.
- On startup (`onStartupFinished`): a healthy server is adopted; otherwise the extension starts one.
  With several VS Code windows open, the first starts it and the rest adopt it (a second start
  already stops at `AlreadyRunningError`).

### 3. Grids open inside VS Code

- The push client stops opening the system browser. The extension holds a WebSocket to the server
  as the opener. When a push or the MCP's `open_grid` finds nothing on screen, the server tells the
  opener once (the window-grace rule from the 2026-10-23 fix still applies), and the extension shows
  the workspace with `simpleBrowser.show`. That command works in VS Code 1.143 (docs/verified.md),
  but it is built in, not a stable API, so the fallback is `vscode.env.openExternal`, the system
  browser.
- Setting `swissarmygrid.openIn`: `vscode` (default) or `browser`.

### 4. Push buttons on files

- An `editor/title` button (top right of the editor) on `.csv .tsv .txt .json .jsonl .ndjson
  .parquet .arrow .feather .xlsx`, and the same command on the Explorer's right-click menu. VS Code
  opens Parquet, Arrow and Excel files as binary, and the title button may not show for them there:
  check, and rely on the Explorer menu if not.
- The command calls `POST /datasets/{name}/load` with the file's path. The dataset name is the file
  name without its extension, with other characters replaced by `_`.
- Excel: the server reads `.xlsx` with openpyxl in read-only mode, row by row, under the dataset
  size cap; the first row is the header. A workbook with several sheets opens a picker
  (multi-select), and each chosen sheet becomes a dataset named `<file>_<sheet>`.

### 5. Shipped through the plugin

- The `.vsix` ships in the plugin folder (`Claude Plugins/swissarmygrid/vscode/`), built by
  `deploy_plugin.py` beside the wheel. The setup skill installs it with
  `code --install-extension <vsix> --force` after the server. Teammates keep one update path:
  update the plugin, run setup.
- Source: a new `vscode/` folder here (TypeScript bundled with esbuild, like `web/`).

## New dependencies (asking first, per section 0 of the build plan)

- `openpyxl` in the `[server]` extra, for `.xlsx` (MIT; already a dev dependency here).
- In `vscode/`: `@types/vscode` and `@vscode/vsce` (packaging), plus esbuild and TypeScript, which
  `web/` already uses.

## Milestones (ship E1 to E3 in one plugin version)

Removing the other starters before the extension exists would leave teammates with no server, so
the order is: build the new owner, then take the old ones away, in the same release.

1. **E1, the extension starts and owns the server.** Activation, adopt or start, the status bar
   item, restart. *Done when:* a fresh VS Code window starts the server from the uv tool install;
   a second window adopts it; restart replaces a `.venv` server with the uv tool one.
2. **E2, connect-only clients.** `ensure_server` stops starting servers; push and MCP give the
   message; the connection file records `started_by`. *Done when:* a test confirms no module in
   `src/` except `server/__main__.py` starts a server process, and a push with no server raises the
   message.
3. **E3, the opener.** Grids open in the Simple Browser; the client no longer calls
   `webbrowser.open`. *Done when:* three quick pushes with nothing open show one VS Code tab.
4. **E4, file buttons and Excel.** *Done when:* each listed type loads from the title button or
   the Explorer menu, and a two-sheet workbook offers both sheets.
5. **E5, packaging.** `deploy_plugin.py` builds the `.vsix`; setup installs it; the setup and use
   guides say so.

## Open questions

1. **Closing VS Code.** Keep the server running (today's behavior; the tray icon stops it, so
   closing a window never loses loaded data), or stop it with the last window? Proposed: keep it
   running.
2. **Outside VS Code** (a scheduled script, Claude Code in a plain terminal). With nothing else
   allowed to start a server, those calls fail with the message until someone starts it with
   `swissarmygrid`. Proposed: yes, by design.
