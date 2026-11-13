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
  posture: 127.0.0.1 only, token on every data route, DuckDB locked (`adapters/database.py`),
  nothing written to disk except explicit exports, saved recipes and dashboards, and the audit log.

## Layout

The Python is arranged after "Architecture Patterns with Python" (Cosmic Python); [REFACTOR.md](REFACTOR.md)
has the rules and why. Three layers, imports pointing inwards, nothing deeper than
`src/swissarmygrid/<folder>/<file>.py`, one file per feature in each layer.
`tests/test_architecture.py` checks all of it.

```
src/swissarmygrid/
  settings.py            every limit (plan 6a) in one frozen dataclass
  stats.py               public: the Analyze menu's analyses on your own table (what notebook code calls)
  bootstrap.py           the composition root: builds the adapters, hands them to the services

  domain/                the model and its rules. Standard library and sqlglot only; no I/O, no clock
    datasets.py          Column, DatasetInfo, names, the dataset errors
    steps.py             steps -> SQL, each step in plain words, view settings
    grids.py             Grid, the registry, request generations, selections, windows, view keys
    sql_guard.py         what a SQL box, "+" tab or agent query may say
    codegen.py           generated code: DuckDB, pandas, plain Python
    charts.py            chart spec validation, the aggregate query's text, Vega-Lite, Altair
    export.py            export options, provenance
    relationships.py     keys, the Key Finder's scoring, compare, relationship models, the ERD; tangs
                         (the .tang file: a saved, shared set of relationships) and the relationship
                         finder's rules (judge, suggest, implied joins, paths between tables)
    catalogs.py          catalog rules: dialects, names, which source column is read as what
    blades.py            a blade as text and as a record: what its code makes, asks for and imports
                         (read with ast), its pip packages, the portable .blade.py file (a
                         `# /// swissarmygrid-blade` TOML header, zips), its settings (found with ast,
                         placeholders, suggestions, written back), Blade, parse_blade, Template
    stats.py             the shared statistics: chi-square, t, F and normal distributions, effect sizes
    analysis.py          the Analyze menu's math and query text: the fit test (Benford, Nigrini), outliers
                         (amount fences, frequency on two scales), odds (ratios, Woolf's range), buckets,
                         find predictors' rules, the Data shape check, and the notebook code (one
                         swissarmygrid.stats call)
    regression.py        linear and logistic regression: the terms as SQL, the sums, Cholesky, ranges,
                         what-if predictions, the model as SQL (predicted and miss columns)
    timeseries.py        periods, the seasonal index, anomalies against neighbours, steps in the level,
                         memory, growth; the forecast (trend, steps, season, predictors, held-back check)
    tieout.py            the tie-out report: shared columns, the two GROUP BYs joined, ties or off
    packs.py             a pack (a Claude Code plugin with blades/ and tangs/): names, the new marketplace's
                         files, Claude Code's install record read loosely, which plugins are switched off

  services/              the use cases, one service class per feature, given their ports
    ports.py             the shared ports: SqlSession, Views, Datasets, Jobs, Audit, Notifier, Recipes
    workspace.py         Workspace (the ports every use case shares, grid lookups and queries) and the
                         outcomes Refused, NotFound, TimedOut, Conflict
    datasets.py          every way data arrives (push, file, paste, the "+" tab's query), list, close
    grids.py             rows, counts, steps and step changes, highlights, the selection, recipes
    profile.py           column and table profiles, kept by view key
    analysis.py          the Analyze menu on a grid's view: run_* functions over any cursor (the same code
                         swissarmygrid.stats runs on a DataFrame), the Data shape check, the notebook code
    regression.py        regression on a grid's view (run_regression, over any cursor) and what-if
    timeseries.py        time series and forecast on a grid's view (run_time_series, run_forecast)
    sql.py               the SQL box
    export.py            CSV, Excel, Parquet: a download, or saved (an agent's only with a ticket)
    codegen.py           copy as code: generate, then verify what the user will paste
    charts.py            the side panel's cards: charts, notes, tables, diagrams, cross-filtering
    agent.py             the agent door: approvals, export tickets, its reads, the current table,
                         listing and proposing blades (saved on the user's Allow)
    catalogs.py          load, remove, the tree, search
    blades.py            save, inspect, fill, refresh, test, install, favourites, folders, export, import
    notebook.py          cells SwissArmyGrid adds to the open marimo notebook
    relationships.py     the Key Finder, compares, the current model and its library
    tieout.py            the tie-out report on a compare's two grids
    processes.py         Python sessions offering tables: the registry and the Open list
    snapshots.py         a PNG of the page, drawn by the page (app_snapshot); nothing is kept
    packs.py             list packs, make a marketplace, add saved blades and tangs to an editable pack

  adapters/              the driven edge: DuckDB, Arrow, files, the vault, websockets
    database.py          the locked DuckDB, the priority worker pool, DuckDB's own SQL grammar
    datasets.py          the dataset store and its limits; Arrow, CSV, Excel, Parquet, JSON loaders
    views.py             the view cache and windows
    catalogs.py          loaded catalogs: tree queries and search in the locked DuckDB
    blades.py            saved blades (~/.swissarmygrid/blades, one JSON each), blade folders
                         (.blade.py files found by their header), favourites; secrets via keyring
    blade_run.py         run a blade, its catalog command, a vault, in this Python (standard library
                         only: sag.blade uses it too); which packages this Python has
    codegen.py           runs copied code and compares it with the grid
    export.py            CSV, Excel and Parquet writers; the export folder
    state_files.py       the audit log, recipes, saved dashboards, the connection file, the model library
    live_updates.py      the pages' and VS Code windows' sockets
    packs.py             installed packs in Claude Code's plugin folder, editable ones in the blade
                         folders; writing a new marketplace and a pack's files

  server/                the HTTP door, and everything that starts a process
    app.py               asks bootstrap for the services, mounts the routes behind the guard
    http.py              reading requests, shaping answers, each error as its status
    routes_*.py          one per feature: read the request, call the use case, shape the answer
                         (datasets, grids, analysis, regression, timeseries, tieout, agent, cards, catalog, blades, notebook, relationships,
                         processes, snapshots, packs). /catalogs is for everyone; /blades and /packs refuse
                         the agent, whose own door is /agent/blades in routes_agent.py
    security.py          the guard: 127.0.0.1, the token, Host and Origin
    lifecycle.py, launcher.py, __main__.py   ports, single instance, clean exit; `swissarmygrid`
    notebook.py          marimo started on first use (child process, token on stdin); NotebookControl
                         joins the open notebook as a kiosk to add cells
    blade_runner.py      Refresh/Test in a child process; installing packages
    blade_templates.py   the 35 starter blades

  client/push_client.py  push(), ensure_server()
  client/blades_client.py sag.load (a catalog), sag.blade (run a saved blade here), sag.blades
  client/sandbox_client.py sag.sandbox: a figure, picture, table or text as a Sandbox card (duck-typed)
  cli/                   the agent command layer: commands.py (the one registry of every page action,
                         named by app area: tabs_, table_, column_, cell_, steps_...), api.py (HTTP + event socket as the
                         agent), main.py (the `sag` terminal command; `sag pack new` runs here, no server)
  mcp/server.py          the stdio MCP server: one typed tool per command, generated from the registry (110 of them).
                         It holds no logic; add a command in cli/commands.py and it appears
web/src/                 main.ts (composition) + one module per panel; lazy/ holds Monaco, Vega, Mermaid,
                         Graphviz, Plotly and ECharts (each loads when first needed; svgSafe cleans drawings);
                         aiPanel.ts is the Sandbox tab (a 12-column dashboard of resizable cards);
                         analyzeMenu.ts is the Table row's Analyze menu and every result's Send to Sandbox,
                         Open as grid and To notebook; analysisDialog.ts is its forms (odds, fit test, find
                         predictors, outliers; Fit test and Outliers are also in the Column row);
                         modelDialog.ts the regression and what-if; timeSeriesDialog.ts time series and
                         forecast (Plotly charts in the window); cheatSheets.ts each form's Data shape;
                         analysisCards.ts every result as Sandbox cards (a report-style note and a chart);
                         sandboxDashboards.ts saving and opening the Sandbox as named dashboards;
                         sandboxPages.ts Page view, Print to PDF and moving cards; sandboxExport.ts the
                         markdown export; cellMenu.ts the right-click menus on cells and headers;
                         tour.ts is the first-time tour (the Application row's question mark: a
                         spotlight card per word, then a matching game);
                         catalogRail.ts (blades + catalog navigation) is shared by the blades page
                         (catalogPage.ts, static/catalog.html) and the notebook tab (notebookPage.ts,
                         static/notebook.html: the rail left of marimo); bladeEditor.ts is the save screen;
                         bladeLibrary.ts is import (review first), export and the blade folders dialog.
                         The grid page has no SQL box and no steps panel (decision 13): stepsStrip.ts is the
                         strip under the title; stepsPanel.ts shows only in its own window (?panel=steps)
web/static/              index.html, grid.css (all theme tokens), build/ (gitignored)
tests/                   unit + HTTP tests, golden/ (shared cases), e2e/ (Edge via Playwright),
                         test_architecture.py (the layers, the depth, the plain client)
tools/                   screenshots.py, vscode_check.py
plugin/                  the swissarmygrid plugin: skills/swissarmygrid-use (SKILL.md, guide), .mcp.json
.claude-plugin/          marketplace.json: this repository is a Claude Code marketplace (plugin/ and the pack)
examples/sample_store.py made-up nine-table store with a relationship answer key (test bed for autodetection)
examples/packs/          sample-store: an example pack (a made-up blade, the store's tang)
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
