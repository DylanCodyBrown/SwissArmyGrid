# REFACTOR.md: SwissArmyGrid in the Cosmic Python style

The Python code, rearranged after "Architecture Patterns with Python" (Percival and Gregory): a domain
with no I/O, a service layer of use cases that take their dependencies as arguments, adapters at the
edges, and one composition root. What the product does is unchanged: every route, MCP tool, `sag`
command, client function and file format is the same.

## The three rules, and how the layout meets them

1. **Cosmic Python.** Three layers, with imports pointing inwards only:

   | layer | folder(s) | may import | holds |
   |---|---|---|---|
   | domain | `domain/` | the standard library, `sqlglot` (text parsing only), other `domain` files | the model and its rules: steps and their SQL text, the SQL guard, generated code, chart specs, blades, relationships. No DuckDB, files, keyring, HTTP, processes or clock. |
   | service layer | `services/` | `domain`, other `services` | the use cases, one class or function each, given their ports (`services/ports.py`) as arguments |
   | adapters | `adapters/`, `server/`, `cli/`, `mcp/`, `client/` | anything | DuckDB, Arrow, files, the vault, websockets (`adapters/`); the HTTP door and everything that starts a process (`server/`); the agent's terminal and MCP doors (`cli/`, `mcp/`); the push client (`client/`) |

   `bootstrap.py` is the composition root: the one place that builds the concrete adapters and hands
   them to the services. `server/app.py` asks it for the services and gives each route module its own.

2. **No more than three deep.** Every module is `src/swissarmygrid/<file>.py` or
   `src/swissarmygrid/<folder>/<file>.py`; there are three layers. Entry points (HTTP routes, `sag`,
   MCP, the push client) are adapters, as the owner's rule says.

3. **One file per feature.** A feature has at most one file in each layer, named after it:
   `domain/<feature>.py`, `services/<feature>.py`, `adapters/<feature>.py`, `server/routes_<feature>.py`.
   A feature with nothing to say in a layer has no file there (recipes have no rules of their own, so
   no `domain/recipes.py`). Small things that belong together share a file rather than getting one
   each: `adapters/state_files.py` is everything written under the state folder (recipes, the audit
   log, the connection file, the model library).

`tests/test_architecture.py` checks all three: the depth, the import directions, the libraries each
layer may load, and that the plain client imports nothing but the standard library with the server's
packages blocked.

### Why `server/` as well as `adapters/`

Three things outside the layout pin `server/`:
- `python -m swissarmygrid.server` is how the VS Code extension, the README and the tests start the
  server, so `server/__main__.py` stays.
- `tests/test_client_install.py` asserts that nothing outside `server/` may start a process or open a
  browser. So the adapters that start one stay there: the marimo notebook (`server/notebook.py`), the
  blade runner and package install (`server/blade_runner.py`), the launcher and the lifecycle.
- The HTTP door is its own kind of adapter (a driving one). Keeping it apart from the driven adapters
  (`adapters/`) reads better than thirty files in one folder.

`client/`, `cli/` and `mcp/` keep their names for the same reason: `swissarmygrid.client` is public
(`from swissarmygrid.client import push`), and the three script entry points name them.

### The plain client

`swissarmygrid/__init__.py`, `client/*`, `cli/*` and the files they import
(`domain/blades.py`, `domain/catalogs.py`, `adapters/blade_run.py`) use the standard library only.
`domain/__init__.py`, `adapters/__init__.py` and `services/__init__.py` import nothing. The
architecture test imports each of them in a clean interpreter where `duckdb`, `pyarrow`, `pandas`,
`starlette`, `uvicorn`, `sqlglot`, `keyring`, `marimo`, `mcp`, `openpyxl` and `xlsxwriter` cannot be
imported.

## The service layer

One service class per feature (`services/<feature>.py`); each method is a use case. A service is
given the `Workspace` (`services/workspace.py`: the ports every use case shares, plus grid lookups
and the query runner that honours request generations) and whatever else it alone needs. In the
book's terms the workspace is the unit of work the handlers share.

A use case takes plain values (a grid, a parsed body, who is asking) and returns plain data. It ends
in its answer or in an error: a domain error (`StepError`, `DatasetError`, `BladeError`...), a
`LookupError` or `ValueError`, or one of four outcomes the workspace names: `Refused`, `NotFound`,
`TimedOut`, `Conflict`. `server/http.py` is the one place an error becomes a status code.

`tests/test_service_layer.py` runs use cases on fakes of the ports (pages, audit log, store, worker
pool, model library): no DuckDB, no files, no sockets, no HTTP.

A route module (`server/routes_<feature>.py`) is given its service and does three things: read the
request, call the use case, shape the answer. What stays in a route is what only HTTP knows: who is
asking (the `X-Actor` header, and the refusals that follow from it), body caps and content types,
streaming, cookies, and the order in which a request's problems are reported (a missing grid is
found before the body is read, as before).

## Ports

The ports several features share are in `services/ports.py`; a port only one feature uses sits beside
that feature's use cases. Two are in the domain, because compiling steps needs them.

| port | where | what the service layer needs | adapter |
|---|---|---|---|
| `SqlSession` | `ports.py` | run SQL and read rows; interrupt | a DuckDB cursor (`adapters/database.py` opens the locked database) |
| `Describe`, `SqlGrammar` | domain | the columns a query returns; DuckDB's own parse of a text, its keywords and table functions | `adapters/database.py` |
| `Datasets` | `ports.py` | the open datasets: get, all, replace, close, a session | `adapters/datasets.DatasetStore` |
| `Views` | `ports.py` | a grid's compiled view, windows, counts, the cached table | `adapters/views.ViewEngine` |
| `Jobs` | `ports.py` | run a function on the worker pool at a priority | `adapters/database.PriorityWorkers` |
| `Audit`, `Recipes` | `ports.py` | the audit log, saved recipes | `adapters/state_files.py` |
| `Notifier` | `ports.py` | tell the open pages; ask VS Code to show a grid | `adapters/live_updates.LiveUpdates` |
| `CodeVerifier` | `ports.py` | run copied code and compare it with the grid | `adapters/codegen.Verifier` |
| `TableReader` | `datasets.py` | bytes, files and pasted text to a table; a query's result as a table | the module `adapters/datasets.py` |
| `ExportWriter` | `export.py` | a result as CSV, Excel or Parquet bytes; save into the export folder | the module `adapters/export.py` |
| `Catalogs` | `catalogs.py` | loaded catalogs in the locked DuckDB | `adapters/catalogs.CatalogStore` |
| `Blades`, `BladeShelf` | `blades.py`, `agent.py` | saved blades, folders, favourites and secrets (the agent's door sees a narrower one) | `adapters/blades.BladeStore` |
| `ModelLibrary` | `relationships.py` | the relationship model files | `adapters/state_files.ModelFiles` |
| `Notebook` | `notebook.py` | add cells to the open marimo notebook | `server/notebook.NotebookControl` |
| plain functions | constructors | run a blade in a child process, install packages, which packages are installed, is this path one of our own files, the time now, the engine's error classes | `server/blade_runner.py`, `adapters/blade_run.py`, `adapters/state_files.py`, `bootstrap.py` |

## No message bus

Considered for the live-update broadcasts and the agent approvals, and left out:
- A broadcast is already one call on one port (`Notifier.broadcast`), and several use cases need its
  answer (`watched`: did a window take the grid?), which a publish-and-forget bus cannot give.
- An approval is a question that waits for one answer (a future, resolved by the user's click or a
  timeout). That is a rendezvous, not an event; it is one small class (`services/agent.Approvals`).
- The order of effects in a use case (store, notify, audit, ask VS Code) is part of the behaviour the
  tests pin. Handlers subscribed to events would hide that order.

## Feature map

| feature | was | domain | service layer | adapters and doors |
|---|---|---|---|---|
| datasets and loading | `engine/datasets.py`, `engine/loaders.py`, `server/routes_datasets.py` | `datasets.py` | `datasets.py` (`DatasetService`: push, file, paste, the "+" tab, list, close) | `adapters/datasets.py`, `server/routes_datasets.py` |
| grids and steps | `engine/steps.py`, `engine/describe.py`, `server/grids.py`, `server/selection.py`, `server/routes_grids.py` | `steps.py`, `grids.py` | `grids.py` (`GridService`) | `server/routes_grids.py` |
| views and windows | `engine/views.py` | (in `grids.py`: Window, POS, view_key) | (the `Views` port) | `adapters/views.py` |
| profiling | `engine/profile.py` | | `profile.py` (`Profiles`) | (in `routes_grids.py`) |
| the SQL box, the guard | `engine/sql_guard.py`, part of `routes_grids.py` | `sql_guard.py` | `sql.py` (`SqlBox`) | `adapters/database.py` (grammar), (in `routes_grids.py`) |
| export | `engine/export.py` | `export.py` | `export.py` (`Exports`) | `adapters/export.py`, (in `routes_grids.py`) |
| code generation | `codegen/*` | `codegen.py` | `codegen.py` (`CodeService`) | `adapters/codegen.py`, (in `routes_grids.py`) |
| charts and cards | `charts/spec.py`, `server/routes_cards.py` | `charts.py` | `charts.py` (`Cards`) | `server/routes_cards.py` |
| recipes, audit, info file | `engine/recipes.py`, `engine/audit.py`, `server/info_file.py` | | (in `grids.py`, `datasets.py`) | `adapters/state_files.py` |
| blades | `blade_core/*`, `server/blade_store.py`, `server/blade_runner.py`, `server/blade_templates.py`, half of `server/routes_catalog.py` | `blades.py` | `blades.py` (`BladeService`) | `adapters/blades.py`, `adapters/blade_run.py`, `server/blade_runner.py`, `server/blade_templates.py`, `server/routes_blades.py` |
| catalogs | `engine/catalog.py`, half of `server/routes_catalog.py` | `catalogs.py` | `catalogs.py` (`CatalogService`) | `adapters/catalogs.py`, `server/routes_catalog.py` |
| notebook | `server/notebook.py`, `server/routes_notebook.py` | | `notebook.py` (`NotebookCells`) | `server/notebook.py`, `server/routes_notebook.py` |
| agent door and approvals | `server/routes_agent.py`, `server/routes_context.py` | | `agent.py` (`AgentService`, `Approvals`, `ExportTickets`) | `server/routes_agent.py` |
| snapshots | `server/routes_snapshots.py` | | `snapshots.py` (`Snapshots`) | `server/routes_snapshots.py` |
| relationships, key finder, compare | `engine/relationships.py`, `engine/keyfinder.py`, `engine/compare.py`, `routes_models.py`, `routes_keyfinder.py`, `routes_compare.py` | `relationships.py` | `relationships.py` (`Models`, `KeyFinder`, `Compares`) | `adapters/state_files.py` (the library), `server/routes_relationships.py` |
| Python sessions (Open list) | `server/processes.py`, `server/routes_processes.py` | | `processes.py` (`ProcessRegistry`, `Sessions`) | `server/routes_processes.py` |
| live updates | `server/live_updates.py` | | (the `Notifier` port) | `adapters/live_updates.py` |
| server lifecycle | `server/lifecycle.py`, `server/launcher.py`, `server/__main__.py`, `server/security.py`, `engine/database.py`, `engine/workers.py`, `server/context.py` | | `workspace.py` | `server/*` unchanged, `server/http.py`, `adapters/database.py`, `bootstrap.py` |

## Migration order (one commit each, the whole suite green after each)

1. This plan and `tests/test_architecture.py` (the client check; the layer checks grow as files move).
2. The core: `domain/datasets.py`, `domain/steps.py`, `domain/sql_guard.py`, `domain/grids.py`;
   `adapters/database.py`, `adapters/datasets.py`, `adapters/views.py`.
3. Profiling, charts, code generation, export, relationships: pure parts to `domain/`, queries over
   the `SqlSession` port to `services/`, writers and the verifier to `adapters/`.
4. Blades and catalogs: `domain/blades.py`, `domain/catalogs.py`, `adapters/blades.py`,
   `adapters/catalogs.py`, `adapters/blade_run.py`; the client follows.
5. State files, live updates; `services/ports.py`; `bootstrap.py` replaces `server/context.Context`.
6. Use cases out of the routes, a feature at a time: datasets, grids, profile, SQL, export, code,
   cards, agent, catalogs, blades, notebook, relationships, processes, snapshots.
7. CLAUDE.md's layout; the architecture test at full strength.

## Progress

- [x] 0. Baseline: 1,371 tests; 1,370 passed and 1 failed in the full run (see "Left alone").
- [x] 1. Plan and architecture test.
- [x] 2. The core: `domain/datasets.py`, `domain/steps.py` (with the plain-language descriptions),
  `domain/sql_guard.py`, `domain/grids.py` (with selections, windows and view keys);
  `adapters/database.py` (the locked database, the worker pool, DuckDB's grammar),
  `adapters/datasets.py` (the store and the file loaders), `adapters/views.py`; the first ports.
  The domain no longer imports DuckDB: compiling takes `Describe` and `SqlGrammar` as arguments.
- [x] 3. Profiling (`services/profile.py`), charts (`domain/charts.py`, `services/charts.py`), code
  generation (`domain/codegen.py`, `services/codegen.py`, `adapters/codegen.py`), export
  (`domain/export.py`, `adapters/export.py`), relationships with the key finder and compare
  (`domain/relationships.py`, `services/relationships.py`). `codegen/` and `charts/` are gone.

- [x] 3b. Re-checked step 3 after the restart (1,394 passed), then merged the main line (release 1.6.0,
  blade settings): 1,457 passed.
- [x] 4. Blades and catalogs. `domain/blades.py` is a blade as text and as a record (its code, its
  packages, the portable file, its settings, `Blade` and `parse_blade`); `domain/catalogs.py` the
  catalog rules; `adapters/blades.py` the saved blades and the vault; `adapters/catalogs.py` the loaded
  catalogs in DuckDB; `adapters/blade_run.py` runs a blade in this Python and says what it has
  installed. `blade_core/` is gone; the client imports `domain/blades.py` and `adapters/blade_run.py`.
  The domain reads no clock, environment or installed packages: `parse_blade(..., now)`,
  `suggest_settings(..., environ)` and `needed(code, installed)` are given them. 1,462 tests.

- [x] 5. State files (`adapters/state_files.py`: the audit log, recipes, the connection file, the model
  library), live updates (`adapters/live_updates.py`), `services/workspace.py`, and `bootstrap.py` in
  place of `server/context.Context`; the HTTP helpers are `server/http.py`. `engine/` is gone. 1,465 tests.
- [x] 6a. Use cases out of the routes: datasets, grids, profiles, the SQL box, export, code, the agent
  door (with the current table), Python sessions. 1,471 tests.
- [x] 6b. The rest: cards, catalogs, blades (`server/routes_blades.py` split from `routes_catalog.py`),
  the notebook's cells, relationships (`server/routes_relationships.py` in place of three files),
  snapshots. No route module takes the composition root any more: each is given its service.
- [x] 7. CLAUDE.md's layout; the architecture test at full strength (the doors reach adapters only
  through `bootstrap.py`; no layer file over 1,500 lines; a door per service).

## Left alone, and why

- `server/blade_templates.py` (the 35 starter blades) stays in `server/`. It is plain data and would sit
  in the domain, but one starter's code (the 1Password vault) contains the word `subprocess`, and
  `tests/test_client_install.py` refuses that word in any file outside `server/`. The test is not
  weakened, so the file stays where the test allows it.

- `tests/e2e/test_notebook_page.py::test_phase_2_done_when_a_notebook_table_is_a_grid_and_its_steps_go_back`
  failed in the first full run on this machine, before any change (two SQL cells where the test
  expects one), and passes when its file is run alone: it is timing-sensitive here (Chromium in place
  of Edge). It is not touched.
- `cli/commands.py` (1,583 lines) is the one registry every MCP tool and `sag` command is generated
  from: names, descriptions and arguments. It is a little over the size guide, and splitting it would
  put the registry in several places for no gain; its text is part of the behaviour that must not
  change. It is untouched. `cli/`, `mcp/` and `client/` are otherwise as they were, apart from import
  paths: they are already thin entry points over HTTP.
- `server/notebook.py`, `server/blade_runner.py`, `server/lifecycle.py`, `server/launcher.py` and
  `server/__main__.py` stay in `server/` (not `adapters/`): each starts a process, and
  `tests/test_client_install.py` allows that only there. The services reach them through ports.
- A few statuses are still decided in a route, because only HTTP knows them or because the original
  body had no content type and the generic mapping would add one: 415 and 413 on a push, 422 for a
  failed blade run or install, 502/503/504 for the notebook and snapshots, and `DuckDB: ...` 400s on
  the SQL box and the agent's reads (the routes catch the engine's error class there, as before).
- `services/processes.py` holds `ProcessRegistry` (a model with its own rules: presence, load
  requests, who follows what) beside its use cases. It waits on asyncio events, so it is not in the
  domain; it touches no port.
- The domain uses `sqlglot` (the SQL guard and code generation parse text with it). It is a pure
  parser, the rule in the table above allows it, and replacing it would change behaviour.
- `web/` and `vscode-extension/` are untouched (two comments in `web/src` still name
  `blade_core/fill.py` and `blade_core/portable.py`; they are the main line's to change).
- `docs/verified.md`, `docs/ideas.md` and the plans name old paths (`engine/steps.py` and so on) in
  dated entries. They are a record of when things were verified or decided, so they are left as written.
