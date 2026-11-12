# CLAUDE.md — SwissArmyGrid

A local Python server (Starlette + uvicorn + in-memory DuckDB) that shows tabular data in live
AG Grid pages, with steps, profiling, SQL, export, verified code generation, charts and an MCP
door for agents. It runs only on the user's machine. This repository is the whole product: the Python
package at the root, its page in `web/`, and the optional VS Code extension in `vscode-extension/`.

One package, two installs (`python build.py` builds the wheel, page inside): the plain wheel is a
standard-library-only push client for any Python environment; the `[server]` extra is the grid server,
installed once per machine as a uv tool (`uv tool install "<wheel>[server]"`), started by `swissarmygrid`.

- **What SAG is for:** Python-to-DuckDB work on one screen instead of three. SAG brings Python's
  power (analysis, fast data); **DuckDB SQL does the transforming**. Generated transformations are
  DuckDB SQL over the user's own table, never a pandas translation.
- **Where it is going:** [Plans/standalone-workspaces.plan.md](Plans/standalone-workspaces.plan.md):
  a one-command installer that sets up everything; one local server with separate workspaces
  (`sag.create_workspace()`); marimo built in (each workspace is a marimo notebook, grid actions
  write DuckDB SQL cells). **Rule: the notebook/terminal always opens in its own browser tab, never in
  the grid page**, so it can sit on another screen; saved workspaces; a tray icon; and a Power BI style
  relationships view with autodetection. VS Code is optional.
- **The original plan:** [Plans/swissarmygrid-build-plan.md](Plans/swissarmygrid-build-plan.md).
  Follow its section 0 working agreements: typed Python, tests first for semantics, ask before
  adding a dependency the plan doesn't list, never build milestone 9 (agent code sandbox)
  without asking.
- **What exists:** milestones M0-M8, blades and the catalog, and plan phase 2 (the marimo notebook tab,
  its tables as grids, grid steps written back as SQL cells). Not built: M9 (sandbox), the VS Code extension and the
  Jupyter display hook (the plan's "parallel extras").
- **Record verification results** in [docs/verified.md](docs/verified.md); decisions and open
  notes go in [docs/ideas.md](docs/ideas.md).
- **Data is sensitive.** Use only made-up data in tests, examples and screenshots. Keep the
  posture: 127.0.0.1 only, token on every data route, DuckDB locked (`engine/database.py`),
  nothing written to disk except explicit exports, saved recipes and the audit log.

## Layout

```
src/swissarmygrid/
  settings.py            every limit (plan 6a) in one frozen dataclass
  engine/                DuckDB setup (database), datasets + limits, steps -> SQL (steps),
                         view cache + windows (views), workers, profile, sql_guard, export,
                         loaders, recipes, audit, describe
  codegen/               duckdb_code, python_code (pandas + plain), verify, generate
  charts/spec.py         chart spec validation, server-side aggregation, Vega-Lite, Altair
  server/                app (composition), context (services + helpers), routes_datasets,
                         routes_grids, routes_agent, routes_cards, routes_snapshots (app_snapshot: relays the
                         agent's request to the open page, which draws itself with web/src/snapshot.ts and
                         uploads the PNG; nothing is kept), security (guard), lifecycle
                         (ports, single instance, clean exit), info_file, grids, live_updates
  client/push_client.py  push(), ensure_server()
  client/blades_client.py sag.load (a catalog), sag.blade (run a saved blade here), sag.blades
  blade_core/            blades, standard library only: code (what code makes, asks for, imports; read
                         with ast), run (run a blade, its catalog command, a vault), packages (pip names)
  engine/catalog.py      catalogs: a source's names and types, tree queries and search (in the locked DuckDB)
  server/blade_store.py  saved blades (~/.swissarmygrid/blades, one JSON each), secrets via keyring;
                         blade_runner (Refresh/Test in a child process), blade_templates (35 starters),
                         routes_catalog (/catalogs for everyone, /blades refuses the agent)
  server/notebook.py     the notebook: marimo started on first use (child process, token on stdin),
                         NotebookControl joins the open notebook as a kiosk to add cells (imports, drops);
                         routes_notebook serves it at /marimo (HTTP + websockets, notebook cookie)
  cli/                   the agent command layer: commands.py (the one registry of every page action,
                         named by app area: tabs_, table_, column_, cell_, steps_...), api.py (HTTP + event socket as the
                         agent), main.py (the `sag` terminal command)
  mcp/server.py          the stdio MCP server: one typed tool per command, generated from the registry (92 of them).
                         It holds no logic; add a command in cli/commands.py and it appears
web/src/                 main.ts (composition) + one module per panel; lazy/ holds Monaco and Vega;
                         catalogRail.ts (blades + catalog navigation) is shared by the blades page
                         (catalogPage.ts, static/catalog.html) and the notebook tab (notebookPage.ts,
                         static/notebook.html: the rail left of marimo); bladeEditor.ts is the save screen.
                         The grid page has no SQL box and no steps panel (decision 13): stepsStrip.ts is the
                         strip under the title; stepsPanel.ts shows only in its own window (?panel=steps)
web/static/              index.html, grid.css (all theme tokens), build/ (gitignored)
tests/                   unit + HTTP tests, golden/ (shared cases), e2e/ (Edge via Playwright)
tools/                   screenshots.py, vscode_check.py
plugin/                  the plugin template (skills/swissarmygrid-use: SKILL.md, guide)
examples/sample_store.py made-up nine-table store with a relationship answer key (test bed for autodetection)
build.py                 build the page (release) and the one wheel; --vsix also packages the extension
installer/               install.sh and install.ps1 (uv, then the server as a uv tool); build.py stamps the version
.github/workflows/       release.yml: a v* tag builds, tests the installers on Linux and Windows, publishes
tools/smoke_install.py   start the installed server, push the sample store, check it all arrived (used by releases)
vscode-extension/        the optional VS Code extension (TypeScript): starts the server, shows grids in VS Code
```

## Commands

See [README.md](README.md). Before a commit: `pytest -q` (includes the browser tests),
`ruff check .`, `ruff format --check .`, `mypy`, and `npm run typecheck` in `web/`. Run
Python as `.venv\Scripts\python -m ...`: `uv run` re-syncs first and fails while a server
started from `swissarmygrid.exe` is running.

## Gotchas

- A step change that adds or removes columns must purge the grid's row cache (not just
  refresh), or old rows draw blank in the new column. `gridView.refresh()` handles this.
- AG Grid renders only on-screen columns; tests that look for a new column's header rely on
  `ensureColumnVisible` (done when a step adds a column).
- Starlette's TestClient sends `Host: testserver` on WebSockets; pass `Host: 127.0.0.1:<port>`.
- The e2e fixture gives each test its own grid (`new_grid=True`) and closes it; a plain re-push
  would reuse the first grid with every earlier test's steps.
- `npm run build` writes source maps (development); `build.py` uses `build:release`
  without them, and the wheel bundles `web/static` as `swissarmygrid/web_static`.
- DuckDB quirks the engine depends on are listed in docs/verified.md (LIKE escape, case
  mapping, date_trunc type, parameterized GROUP BY aliases).
