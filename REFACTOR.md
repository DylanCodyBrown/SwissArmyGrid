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
   them to the services. `server/app.py` asks it for the services and mounts the routes.

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

## Ports (`services/ports.py`, and two in the domain)

| port | what the service layer needs | adapter |
|---|---|---|
| `SqlSession` | run SQL and read rows; interrupt | a DuckDB cursor (`adapters/database.py` opens the locked database) |
| `Describe`, `SqlGrammar` (domain) | the columns a query returns; DuckDB's own parse of a text, its keywords and table functions | `adapters/database.py` |
| `Datasets` | the open datasets: get, all, replace, close, a session | `adapters/datasets.DatasetStore` |
| `Views` | a grid's compiled view, windows, counts, the cached table | `adapters/views.ViewEngine` |
| `Jobs` | run a function on the worker pool at a priority | `adapters/database.PriorityWorkers` |
| `TableReader` | bytes, files and pasted text to a table | `adapters/datasets` (Arrow, CSV, Excel, Parquet) |
| `Audit`, `Recipes`, `ModelLibrary` | the audit log, saved recipes, model files | `adapters/state_files.py` |
| `Notifier` | tell the open pages; ask VS Code to show a grid | `adapters/live_updates.LiveUpdates` |
| `ExportWriter` | a result as CSV, Excel or Parquet bytes; save into the export folder | `adapters/export.py` |
| `CodeVerifier` | run copied code and compare it with the grid | `adapters/codegen.py` |
| `Catalogs` | loaded catalogs in the locked DuckDB | `adapters/catalogs.CatalogStore` |
| `Blades`, `BladeRunner`, `PackageInstaller` | saved blades and their secrets; run one in a child process; install packages | `adapters/blades.BladeStore` (files, keyring), `server/blade_runner.py` |
| `Notebook` | marimo: start, add cells | `server/notebook.py` |
| `Clock` | seconds, and the time of day for a stamp | `time` in `bootstrap.py` |

## No message bus

Considered for the live-update broadcasts and the agent approvals, and left out:
- A broadcast is already one call on one port (`Notifier.broadcast`), and several use cases need its
  answer (`watched`: did a window take the grid?), which a publish-and-forget bus cannot give.
- An approval is a question that waits for one answer (a future, resolved by the user's click or a
  timeout). That is a rendezvous, not an event; it is one small class (`services/agent.Approvals`).
- The order of effects in a use case (store, notify, audit, ask VS Code) is part of the behaviour the
  tests pin. Handlers subscribed to events would hide that order.

## Feature map

| feature | was | domain | service layer | adapters |
|---|---|---|---|---|
| datasets and loading | `engine/datasets.py`, `engine/loaders.py`, `server/routes_datasets.py` | `datasets.py` (Column, DatasetInfo, names, errors) | `datasets.py` (store a table, load, paste, close, budget) | `adapters/datasets.py` (store, Arrow, files), `server/routes_datasets.py` |
| grids and steps | `engine/steps.py`, `engine/describe.py`, `server/grids.py`, `server/selection.py`, `server/routes_grids.py` | `steps.py` (steps to SQL, in words), `grids.py` (Grid, registry, selection, view keys) | `grids.py` (lookup, rows, steps, step ops, highlights, recipes) | `server/routes_grids.py` |
| views and windows | `engine/views.py` | (in `grids.py`: Window, POS, view_key) | (through the `Views` port) | `adapters/views.py` |
| profiling | `engine/profile.py` | | `profile.py` | |
| SQL guard and queries | `engine/sql_guard.py`, parts of three route files | `sql_guard.py` | `sql.py` (SQL box, the "+" tab, agent query) | `adapters/database.py` (grammar) |
| export | `engine/export.py` | `export.py` (options, provenance) | `export.py` | `adapters/export.py` |
| code generation | `codegen/*` | `codegen.py` | `codegen.py` | `adapters/codegen.py` (verify) |
| charts and cards | `charts/spec.py`, `server/routes_cards.py` | `charts.py` | `charts.py` | `server/routes_cards.py` |
| recipes, audit, info file | `engine/recipes.py`, `engine/audit.py`, `server/info_file.py` | | (in `grids.py`, `datasets.py`) | `adapters/state_files.py` |
| blades | `blade_core/*`, `server/blade_store.py`, `server/blade_runner.py`, `server/blade_templates.py`, half of `server/routes_catalog.py` | `blades.py` (code, packages, portable file, settings, Blade) | `blades.py` | `adapters/blades.py`, `adapters/blade_run.py`, `server/blade_runner.py`, `server/blade_templates.py`, `server/routes_blades.py` |
| catalogs | `engine/catalog.py`, half of `server/routes_catalog.py` | `catalogs.py` | `catalogs.py` | `adapters/catalogs.py`, `server/routes_catalog.py` |
| notebook | `server/notebook.py`, `server/routes_notebook.py` | | `notebook.py` | `server/notebook.py`, `server/routes_notebook.py` |
| agent door and approvals | `server/routes_agent.py`, `server/routes_context.py` | | `agent.py` (approvals, tickets, reads, current table) | `server/routes_agent.py` |
| snapshots | `server/routes_snapshots.py` | | `snapshots.py` | `server/routes_snapshots.py` |
| relationships, key finder, compare | `engine/relationships.py`, `engine/keyfinder.py`, `engine/compare.py`, three route files | `relationships.py` | `relationships.py` | `server/routes_relationships.py` |
| Python sessions (Open list) | `server/processes.py`, `server/routes_processes.py` | | `processes.py` | `server/routes_processes.py` |
| live updates | `server/live_updates.py` | | (the `Notifier` port) | `adapters/live_updates.py` |
| server lifecycle | `server/lifecycle.py`, `server/launcher.py`, `server/__main__.py`, `server/security.py`, `engine/database.py`, `engine/workers.py` | | | `server/*` unchanged, `adapters/database.py` |

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

## Left alone, and why

- `server/blade_templates.py` (the 35 starter blades) stays in `server/`. It is plain data and would sit
  in the domain, but one starter's code (the 1Password vault) contains the word `subprocess`, and
  `tests/test_client_install.py` refuses that word in any file outside `server/`. The test is not
  weakened, so the file stays where the test allows it.

- `tests/e2e/test_notebook_page.py::test_phase_2_done_when_a_notebook_table_is_a_grid_and_its_steps_go_back`
  failed in the first full run on this machine, before any change (two SQL cells where the test
  expects one), and passes when its file is run alone: it is timing-sensitive here (Chromium in place
  of Edge). It is not touched.
