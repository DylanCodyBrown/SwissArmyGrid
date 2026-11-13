# Verified (plan section 17)

What has actually been checked, against which version, and how. Anything not listed here is
still an assumption. Last updated 2026-10-23.

| # | Item | Status | Result |
|---|---|---|---|
| 1 | AG Grid Community | Verified, 36.2.0 | The infinite row model (`IDatasource.getRows` with `startRow`, `endRow`, `sortModel`; `successCallback(rows, lastRow)`) works with server windows. `refreshInfiniteCache()` refetches in place keeping sort and scroll; `purgeInfiniteCache()` is needed when the column set changes, or stale rows briefly show the new column as blank. Custom header components (`IHeaderComp`, `progressSort`), `rowSelection: {mode: "multiRow"}`, `ensureColumnVisible`, `setColumnsVisible` all work. Themes use the theming API; color params accept CSS variables (`"var(--bg)"`), so one theme object follows all four page themes. Columns outside the viewport are not rendered (column virtualization). Filters: AG Grid's own filter UI is not used; filters are server steps. MIT license. To size its scroll area, AG Grid measures the tallest div the browser allows by growing a bare div appended to `<body>`; in a flex-column body that div shrinks, AG Grid reads 1,000,000px and switches every table over 31,250 rows to "stretched" scrolling (a wheel notch skipped hundreds of rows). `grid.css` keeps direct children of `body` from shrinking. |
| 2 | DuckDB | Verified, 2.0.0 | `enable_external_access=false` blocks `read_csv`, `COPY`, `ATTACH`, `INSTALL` and reading Python variables by name; explicitly registered Arrow tables still load. `lock_configuration=true` blocks later `SET`. `temp_directory=''` turns spilling off; an over-memory query fails with `OutOfMemoryException` and the connection keeps working. `trim(s, chars)`, `ltrim`/`rtrim` accept a character list. Backslashes are literal in strings. `LIKE ... ESCAPE '!'` works, but a backslash escape character cannot escape itself (`'a\b' LIKE 'a\\b' ESCAPE '\'` is false), so the engine uses `!`. `lower()`/`upper()` map one character to one (`upper('ß')` = `ẞ`, `lower('İ')` = `i`, `upper('ﬁ')` = `ﬁ`; Python gives `SS`, `i̇`, `FI`); the generators copy DuckDB. `date_trunc` returns TIMESTAMP even for DATE input (cast back). `date_trunc('week')` starts on Monday. `stddev_samp`, `quantile_cont` (linear) and `median` match pandas. `rowid` of a freshly created table is dense 0..n-1. A GROUP BY alias whose expression contains `?` parameters can fail to bind ("must appear in the GROUP BY clause"); histogram queries inline their two numbers and group by position. Default `memory_limit` was 25 GiB; the server sets twice the data budget (at least 256 MiB). |
| 3 | MCP library | Verified, mcp 2.3.0 | `mcp.server.mcpserver.MCPServer` with `@server.tool(description=...)` and `run("stdio")`; transports stdio, sse, streamable-http. The launcher is a separate stdio process calling the grid server over HTTP, so no routes are mounted into the MCP app. |
| 4 | Arrow data exchange interface | Verified | The push client converts pandas (`Table.from_pandas`), Polars and other objects with `__arrow_c_stream__` (`pa.table(obj)`), DuckDB relations (`to_arrow_table()`) and lists of dicts, with pyarrow 26. Dates keep their type end to end. |
| 5 | sqlglot | Verified, 30.24.0 | Parses the DuckDB dialect for the SQL box subset; WITH clauses live under the `with_` argument (not `with`) in this version; table functions parse as `exp.Table` with a function `this`. |
| 6 | VS Code | Verified, 1.143.0 | "Simple Browser: Show" opens the integrated browser as a real Chromium page (not an iframe); the grid, WebSocket, panel and title all work. Older versions use a sandboxed iframe and append `?id=&vscodeBrowserReqId=`; the page reads the grid id from the path and the token from the fragment, so both work. `tools/vscode_check.py` reproduces the check. Debugger-variable menu: not checked (VS Code extension not built). |
| 7 | Licenses and sizes | Verified | AG Grid Community MIT; Monaco 0.57 MIT; Vega 6.4, Vega-Lite 6.5, vega-embed 7.3 BSD-3-Clause; xlsxwriter 3.3 BSD-2-Clause; DuckDB MIT; pyarrow Apache-2.0; sqlglot MIT; Starlette and uvicorn BSD-3. Bundles: main 1.2 MB (loads with the page), charts 0.84 MB and the SQL editor 4.5 MB (each loads on first use). The release wheel is 7.2 MiB unpacked. Check the company's open-source policy before wider sharing. |
| 8 | Company policy on sending client data to an AI service | Not checked | A connected agent sees cell values (the privacy levels were removed on 2026-11-12), so check this before connecting an agent to client data. |
| 9 | Windows sandbox options | Not checked | Milestone 9 is not built. |
| 10 | Excel export | Verified | xlsxwriter `in_memory` mode (constant_memory would write temp files, against the no-disk default); `strings_to_formulas=False` writes `=1+1` as a string cell; integers over 15 digits stay exact as text; the row limit (1,048,576) and cell limit (32,767) are enforced with messages; round trips read back with openpyxl. CSV formula neutralization prefixes an apostrophe. Parquet round trips keep types, nulls, time zones and provenance metadata (pyarrow, snappy or zstd). |
| 11 | Resource limits | Verified | See item 2 for memory. `Content-Length` is checked first and the body is counted while streaming (Starlette `request.stream()`); Arrow and Parquet decode batch by batch so a 16 MB table compressed to under 200 KB is refused at 1 MB. `pyarrow.Table.nbytes` measures size cheaply. |
| 12 | uvicorn and asyncio sockets | Verified, 2026-11-12 | A listening socket made with the default protocol (0) is not recognised as TCP by asyncio, which then never turns Nagle off on accepted connections: every request on a kept-alive connection (how the page fetches windows while scrolling) waited about 40 ms for the client's delayed ACK. `server/lifecycle.py` creates the listener with `IPPROTO_TCP` and sets `TCP_NODELAY`: `/health` 44 ms → 0.33 ms, a 100-row window 48 ms → 4 ms. |

## Performance (plan section 14), measured 2026-10-21

`tests/test_performance.py`, 1,000,000 rows x 10 columns (78.6 MiB), through the full app on
this machine:

| Measure | Result | Target |
|---|---|---|
| Push (Arrow) | 0.15 s | |
| First window | 6 ms | 300 ms |
| Sort, then the first window | 320 ms | 500 ms |
| Filter step, then the first window | 117 ms | 500 ms |
| Profile of one column | 93 ms | 1 s |
| Jump to row 500,000 after the filter | 4 ms | |

A chart over 1,000,000 rows sends at most 5,000 points (`tests/test_cards.py`).

## Other checks

- uvicorn 0.54 on Windows serves from a socket bound and listening beforehand
  (`Server.run(sockets=[...])`).
- `SO_EXCLUSIVEADDRUSE` stops a second socket from binding the server's port even with
  `SO_REUSEADDR` (`tests/test_lifecycle.py`).
- On Windows the venv's `python.exe` is a launcher that starts the real interpreter as a child,
  so `Popen.pid` is not the server's pid; the connection file records the real one.
- Playwright 1.64 drives the installed Edge (`channel="msedge"`) with no browser download.
- `uv run` re-syncs the environment first, which fails while a server started from
  `swissarmygrid.exe` holds that file; run the server with `python -m swissarmygrid.server`
  during development.
- DuckDB keeps an ordered input's order through `row_number() OVER ()` (checked on 3,000,000
  rows with 8 threads: an `ORDER BY` subquery and a filtered ordered table, five runs each, no
  row out of place). A SQL step relies on this to number its rows; a query with no defined
  order (GROUP BY, DISTINCT, joins, windows, set operations, CTEs) is wrapped in `ORDER BY ALL`
  first, so the grid and the generated code agree run after run.
- (2026-11-12, DuckDB 2.0.0, pandas 3.0.6) For the copied query, on a 3,000,000-row pandas frame
  read by name with 8 threads, three runs each: `row_number() OVER ()` numbers the frame's rows in
  order; `ORDER BY k` alone is **not** stable (ties came out of order every run), while
  `ORDER BY k, row_number() OVER ()` matched pandas' stable sort; an ordered CTE keeps its order
  through a later CTE's WHERE and projection, and through LIMIT. A window function is allowed in
  ORDER BY. `SET disabled_filesystems = 'LocalFileSystem'` on the default connection blocks
  `read_csv`, `COPY`, `ATTACH` and `https://` reads but still reads Python variables by name
  (unlike `enable_external_access=false`), and `lock_configuration` then stops it being undone.
  DuckDB 2.0 relations have no `fetchone()`, and `.arrow()` returns a RecordBatchReader, so the
  copy uses `.to_arrow_table()` for Arrow pushes (tests/test_copied_code.py,
  tests/test_golden_codegen.py).
- DuckDB conversion errors quote the cell value ("Could not convert string 'North' to
  INT32"). Until the privacy levels were removed (2026-11-12) an agent below level 2 got a
  generic message instead; now everyone gets DuckDB's own text.
- Headless Edge shares the real Windows clipboard with the desktop, so the copy test captures
  what the page writes instead of reading the clipboard.
- AG Grid Community 36 has no range selection; the Excel-style selection is drawn with cell
  classes, and AG Grid's own Ctrl+A / Ctrl+C handling is pre-empted with a capture-phase key
  listener.
- (2026-10-23) A grid view's SQL, with its own WITH clause, works as a CTE inside another query
  once per table read (the "+" tab joins two tabs' views; `tests/test_query_tables.py`).
  `cursor.sql(q).to_arrow_table()` skips the extra copy that `execute(q).to_arrow_table()` makes,
  and `cursor.interrupt()` still stops it (the timeout test). AG Grid 36 shows its no-rows overlay
  with zero columns (the "+" tab's blank grid).
- (2026-10-23) Three pushes in a row with no window open opened three windows: each landed before
  the first window's page had connected its WebSocket. Reproduced with the real client and server
  (`webbrowser.open` stubbed), fixed in `LiveUpdates.shown_or_opening`, then one window.
- (2026-10-24) A SQL step's text runs unchanged in the user's Python: `duckdb.sql("SELECT ... FROM
  orders")` finds a local pandas frame named `orders` by DuckDB's replacement scan and returns the
  grid's rows in the grid's order (`tests/test_sql_step_and_selection.py`). The generated pandas
  and plain code register the step input under the name the query reads, and verify.
- (2026-10-24) `count(DISTINCT row(a, b))` counts a blank as a value of its own (`row(c)` gave 4
  where `count(DISTINCT c)` gave 3), so the Key Finder's most distinct combination treats NULL as
  GROUP BY does. One greedy round over 49 two-column combinations of a 400,000-row, 50-column
  table took 0.76 s, so the whole build (at most 8 rounds) finishes in seconds.
- (2026-10-24) `test_lifecycle.py::test_start_status_second_start_and_stop` timed out once in a
  full run (its 10 s start wait, under the browser tests' load) and passed alone, 11 of 11.
- (2026-11-12) SQL guard escapes, DuckDB 2.0.0 and sqlglot 30.24: a CTE name passed the table
  check anywhere in the query, but DuckDB scopes CTEs, so `SELECT * FROM (WITH ds_x AS (SELECT 1)
  SELECT 1), ds_x` read the raw table ds_x (also vc_ cache tables, duckdb_tables, duckdb_columns,
  pg_settings, sqlite_master). A non-recursive CTE's own name in its body is the real table
  (`WITH ds_x AS (SELECT * FROM ds_x)`), and so is a WITH RECURSIVE self-reference outside the
  recursive side of a UNION. `LATERAL json_execute_serialized_sql(json_serialize_sql('...'))` ran
  any SELECT, because sqlglot keeps a LATERAL function out of its Table nodes. The guard now renames
  every CTE to `__sag_cte_<n>` by scope, refuses every table-only function, and checks DuckDB's own
  parse (`json_serialize_sql`) of the rewritten query (`tests/test_sql_guard.py`).
- (2026-11-12) Engine hardening, DuckDB 2.0.0: DuckDB matches table and column names without
  case, so "ds_orders" is also the table of a dataset "Orders" (a push of "orders" replaced it)
  and a CTE or subquery renames a second "y"/"Y" to "y_1"; a column named rowid hides the rowid
  pseudo-column (every window came back empty). `CAST('...+05:00' AS TIMESTAMP)` drops the
  offset; TIMESTAMPTZ keeps the instant. GROUP BY binds a column before a select alias of the
  same name (profiles now group by position). A DROP while another cursor's query reads the
  table does not disturb that query (MVCC); a lookup between them fails, hence the view cache's
  leases. Fetching a TIMESTAMP WITH TIME ZONE value into Python (`fetchall`) needs pytz, which
  the server's dependencies do not include: such a column fails every window and profile
  (to_arrow_table does not need it). Not fixed: adding pytz is a dependency decision.
- (2026-11-12) Measured on 1,000,000 rows x 10 columns (`tests/test_performance.million`), at
  the engine, with a filter and a sort step, before -> after: column profile 589 -> 44 ms, text
  column profile 634 -> 65 ms, table profile 1556 -> 112 ms, header numbers 296 -> 79 ms (they
  read the view cache, `ViewEngine.reading`, instead of every step and the final sort); Key
  Finder pairs against 333,334 rows about 1.7-2.2 s -> 1.0-1.2 s; SQL box `SELECT * FROM grid
  LIMIT 100` 94 -> 6 ms; CSV export 6.2 -> 2.1 s (byte for byte the same); Excel export 50 ->
  45.6 s (xlsxwriter's own writes and close are the rest). `tests/test_performance.py` after:
  first window 13 ms, sort 247 ms, filter 51 ms, profile 39 ms.
- (2026-11-12) Agent tool sweep (`tests/test_tool_sweep.py`): all 88 MCP tools through the MCP
  server's `call_tool`, at levels 0-3, with sensible and hostile arguments, against the sample
  store on a real port: 706 cases pass in about 75 s. Found and fixed on the way: 500s from a
  malformed step's description and from sqlglot's TokenError (an unclosed quote), a raw exception
  from `table_export` options, the column tracer counting `rowid` in `SELECT *` (every stepped grid
  untraced), and DuckDB error text at level 2 quoting a hidden column. sqlglot 30.24.0 qualifies a
  whole-row reference (`to_json(t)`) as `exp.TableColumn` and `#1` as `exp.PositionalColumn`;
  `server/level_zero.py` treats both as untraceable.
- (2026-11-12) Excel export, xlsxwriter 3.3.0, 1,000,000 rows x 10 columns (ints, floats,
  text, a date, a timestamp, a boolean), at the engine: 59.0 -> 21.9 s. Half the time was
  xlsxwriter's per-cell write calls and half its row-by-row XML loop at close(); the data sheet's
  cells are now written as XML directly (`export._SheetData`) and xlsxwriter writes every other
  part. Every part of the file but docProps/core.xml (its creation time) is byte for byte the same
  as with the per-cell calls (`tests/test_export_hostile.py`, and the 1,000,000-row file compared
  part by part). Of the 21.9 s, zip's deflate of the 389 MB sheet XML takes 9 s; level 1 would
  take 2 s but make the file 30% larger (56 -> 73 MB), so the default level stays. Format indexes
  depend on the order xlsxwriter first writes each format (row by row), and shared strings on
  the order of write calls (header, then each batch column by column); both are kept. A sheet
  with 16,385+ columns used to lose the extra columns silently (xlsxwriter returns -1 past XFD);
  it is refused now. DuckDB 2.0.0's ORDER BY over a very wide result is slow whatever the row
  count: `SELECT <4,000 columns> FROM t ORDER BY rowid` takes 2.4 s on 3 rows (SELECT * without
  the ORDER BY 0.09 s), and 16,390 columns took 47 s for every view request (rows and exports).
- (2026-11-12) Copy as Python (Altair), Altair 6.3.0 with vl-convert, pandas, DuckDB 1.4 in a
  clean virtual environment: the copy put column names into Altair's shorthand (`"col:on:N"` is a
  syntax error there, `a.b` reads a nested field, a `"` ended the Python string) and handed Altair
  the whole frame, which it refuses over 5,000 rows (MaxRowsError). It now runs the grid's own
  chart query over the frame with DuckDB (`charts.spec.chart_query`) and encodes the plain columns
  x, y, y2, color, bin_start and bin_end. All 24 chart kinds and options of the probe table (dates,
  time zones, 1850-9999, nulls, unicode, HUGEINT sums, +-1e300) ran and rendered to SVG, from a
  DuckDB `.df()` frame and from `pd.read_parquet` (dates as objects); on a 1,000,000-row frame a
  900-bar chart took 0.7 s and a scatter 0.2 s (5,000 sampled points). The tests run the copy with
  a recording stand-in for altair (not a dependency) and compare its points with the card's.
- (2026-11-12) Catalogs (engine/catalog.py), DuckDB 2.0.0: a 500,000-column catalog (40 schemas,
  20,000 tables) loads in 0.6 s; its schemas list in 18 ms, one schema's tables in 4 ms, one table's
  columns in 2 ms, a word search in 45 ms and a dotted (schema.table) search in 24 ms. DuckDB 2.0
  refuses `unregister` (ResourceInUseException) on a cursor whose last result was read with
  `fetchone()` and not to the end: the engine reads with `fetchall()` before touching the cursor
  again.
- (2026-11-12) Blades: Python's ModuleNotFoundError names only the top level it could not find
  (`snowflake` for `import snowflake.connector`), so the blade runner maps it back to the import as
  written before naming the pip package. `sqlite3.connect(path)` makes a new, empty database when the
  path is wrong, so the SQLite starter opens `file:...?mode=ro` with `uri=True`, which fails plainly.
  keyring 25 on a Linux container with no Secret Service gives `keyring.backends.fail.Keyring`; the
  blade store says plainly that secrets need a vault and suggests a secret or vault blade instead. A
  module attribute and a subpackage of the same name collide (`sag.blades` was the `blades` package
  once it was imported), so the package is `blade_core`.
- (2026-11-12) The notebook behind SAG, marimo 0.25.2 (server/notebook.py, routes_notebook.py):
  `marimo edit <folder> --headless --base-url /marimo --token-password-file - --no-skew-protection`
  starts in about 2 s and is served through SAG's own address (HTTP with `http.client` on worker
  threads, `read1` so server-sent events flow; websockets relayed with `websockets`). marimo accepts
  `Authorization: Bearer <token>`, so the proxy adds it and the browser never sees marimo's token. Skew
  protection needs a `Marimo-Server-Token` header the proxy cannot know, and SAG serves marimo's own
  page anyway, so it is off. A second viewer may join a running notebook as a kiosk
  (`/marimo/ws?session_id=<new>&file=<nb>&kiosk=true`): it is sent `kernel-ready` with the cell ids
  and codes, and a `create-cell` document change it posts to `/api/document/transaction` (with
  `Marimo-Session-Id` = its own id) appears in the person's open notebook and is saved. A kiosk may
  not run cells ("This connection is read-only for this action"); `/api/home/running_notebooks`
  gives the notebook's own session id, which may.
- (2026-11-12) Portable blades, release 1.4.0 (blade_core/portable.py, server/blade_store.py,
  routes_catalog.py, web/src/bladeLibrary.ts): every one of the 35 starters writes to a `.blade.py`
  file and reads back to the same blade, through `tomllib` on Python 3.12; names, descriptions and
  commands with quotes, backslashes, `'''` and newlines survive (a multi-line literal string when it
  is safe, a JSON-escaped basic string otherwise). A header that does not open the file is not a blade,
  so a module that mentions the marker in a docstring is left alone. Import of a file whose code writes
  a marker file never ran it, neither in review nor on Import. A blade folder skips `.venv`, `venv`,
  `node_modules`, `site-packages`, `__pycache__`, hidden folders, build and dist, and stops four levels
  down. Saving a folder blade rewrites its own file. In Chrome for Testing 153 through Playwright: the search
  matches a tag and a description, tag chips narrow and widen the list, a pinned blade comes first and
  stays first after a reload, a dropped name clash offers "keep both" first and saves "Bakery DB 2",
  and Export downloads `orders_app_db.blade.py`. Full suite: 1,356 passed.
- (2026-11-12) The agent makes blades, release 1.5.0 (routes_agent `/agent/blades`, commands
  `blades_list`, `blades_templates`, `blades_create`): a proposal waits for the user's Allow and is
  saved only then; the approval shows the blade's whole `.blade.py` file and the packages it imports that
  are missing. Refused before the user is asked: a secret's value, a taken name (in any letter case),
  code that is not Python or makes no variable, an unknown kind. A `folder` or `path` from the agent is
  ignored (the blade is kept by the app; the blade folder stayed empty). `blades_list` carries no code,
  catalog command or secret names. In Chrome for Testing 153: the approval window opens on the blades
  page with the code, and Allow saves it into the list. The tool list is 95 tools, 21,040 characters
  (cap 24,000). Full suite: 1,371 passed.
- (2026-11-12) Fill in your values, release 1.6.0 (blade_core/fill.py, `/blades/inspect` settings,
  `/blades/fill`, import `fill`): settings are found in 33 of the 35 starters (the 1Password and blank
  starters have none). Every setting of every starter, changed alone, is written back with every
  other setting and the rest of the code unchanged, and is found again at the same place. ast's columns
  are UTF-8 byte offsets: a setting after "Café" in a comment is written at the right place. Lines are
  counted on newlines only (str.splitlines also splits on form feeds). A raw-string path stays raw; a
  value a raw string cannot end with (a trailing backslash) is written as an escaped string; a backslash
  in a plain connection string (`db01\SQLEXPRESS`) is written doubled and found again. Refused: `;` or
  quotes in a connection-string part, a line break, a word for a number, a stale setting id. Passwords,
  tokens and keys are never settings, and no password environment variable is ever suggested. In Chrome
  for Testing 153: the import review marks host, dbname and user as needing a value, a suggestion button
  fills the field, and Import writes the values into the saved blade's code; on the save screen a
  changed setting rewrites the code box. Full suite: 1,431 passed.
- (2026-11-13) The Sandbox, page part (aiPanel.ts, lazy/plotly.ts, lazy/echarts.ts, lazy/graphviz.ts,
  lazy/svgSafe.ts, the grid page's Content Security Policy). In Chrome for Testing 153: Plotly 4.2.0,
  ECharts 6.1.0 and Graphviz (@viz-js/viz 3.31.0) cards draw, and the page requested nothing outside
  127.0.0.1 while a Plotly figure named an outside picture, an ECharts option an outside symbol and
  graphic image, and a DOT node a javascript: URL (the SVG kept no outside link). A Plotly map trace,
  text that is not JSON and an image that is not inline say so instead of drawing. With the policy, an
  injected <img onerror> handler does not run; the whole suite passes under it (Monaco, snapshots,
  Vega, the live socket). Dragging a card's corner grip grows it on the grid and the chart redraws
  taller; the arrow keys resize it and the size is kept after a reload. A card not resized fills its
  row; a small Mermaid diagram keeps its own size. A drawn Vega chart resizes in place (view.width /
  height), so a click while the page settles lands on the live marks. Full suite: 1,439 passed.
- (2026-11-13) The Cosmic Python refactor merged (fast-forward, 13 commits): 1,502 passed here too,
  ruff, format, mypy (strict, 150 files) and the TypeScript check clean; the 1.6.0 wheel built from it
  installs as a uv tool and passes the smoke test (the plain client, the server, the notebook).
- (2026-11-13) Tangs and the relationship finder (domain/relationships.py, services/relationships.py,
  adapters/state_files.py, relationshipsDialog.ts). On examples/sample_store.py the finder returns
  exactly the answer key's ten joins (the two-column returns -> order_lines key, customers.home_store
  text to stores.store_id number, orders.ship_date's second path to calendar inactive) and none of the
  six decoys, over HTTP and in Chrome for Testing 153. The greedy key search alone picked unit_price
  (a float, the most varied column) into order_lines' key; it now searches key-like columns only. A run
  of the accepted joins: nine hold, orders -> customers holds with orphans (90.9% match, 201 values).
  A tang saved into a shared (blade) folder is listed, as Shared with its author, tags and ten joins,
  by a second server pointed at the same folder, and applies there. Plain JSON, another program's
  JSON, a newer format and a malformed tang are refused with their reason. Full suite: 1,547 passed.
- (2026-11-13) Packs (domain/packs.py, adapters/packs.py, services/packs.py, routes_packs.py, the
  Packs window in bladeLibrary.ts). This repository's `.claude-plugin/marketplace.json` passes
  `claude plugin validate .` (Claude Code 2.1.323), with warnings only for the plugins' missing
  `version`, left out on purpose so every commit is an update. The sample-store pack copied into a
  made-up Claude folder's plugin cache and listed in its `installed_plugins.json` is found as
  `sample-store@swissarmygrid`, with its blade (source pack, read-only) and its tang (where pack, ten
  joins); run against the pushed sample store, nine hold and orders -> customers holds with orphans
  (90.9%), the same as the tang saved by hand. A plugin switched off in `enabledPlugins` adds nothing;
  a plugin with no blades or tangs is not a pack. In Chrome for Testing 153, a pack blade opens
  read-only with Save a copy, and New marketplace plus Add to pack write a marketplace that takes
  saved blades.
- (2026-11-13) The tour (tour.ts), in Chrome for Testing 153, light and dark: the New here? bubble
  shows on a first visit and never again once the tour is opened (localStorage, so per browser); each
  card's spotlight surrounds its control (the grid's tab, Relationships, Blades and catalog) or the
  card sits in the middle when the control is off screen; the arrow keys and Escape work; five game
  rounds end on the score knife. Full suite: 1,563 passed (one older test listed the Application
  row's buttons exactly and now lists the four row buttons).
- (2026-11-13) The fit test and outliers (domain/analysis.py, services/analysis.py,
  routes_analysis.py, analysisDialog.ts). The chi-square p-value matches the textbook critical values
  (3.841 with 1 degree of freedom is 0.05, 11.07 with 5 is 0.05, 15.51 with 8 is 0.05) to 1e-9, and an
  even degree of freedom's closed form (100 with 10: 5.45e-17). DuckDB's `mad()` is the median absolute
  deviation (unscaled) and `printf('%.14e', x)` gives the first significant digit correctly rounded
  (9.9999999999999999 is 1.0e+01). On made-up invoices: log-uniform amounts fit Benford (p 0.68, close
  conformity) and evenly typed ones do not (p < 0.0001, nonconformity, w 0.62); region against late
  is related (Cramér's V 0.88); the three planted unit costs are the only outliers within vendor V2,
  and Show these rows keeps exactly rows 30, 1030 and 2030 in the grid's order; frequency finds the one
  misspelled region. In Chrome for Testing 153, light and dark.
- (2026-11-13) The Analyze menu, phase A (Plans/analyze-menu.plan.md): odds, find predictors,
  swissarmygrid.stats, the three result actions. domain/stats.py matches printed tables: t (2.228 with
  10 degrees of freedom, 2.0096 with 49, 3.169 with 10 at 0.01), F (4.103 for 2 and 10, 2.5336 for 5
  and 30) and normal quantiles to 1e-12. Odds by hand: 20 of 100 against 10 of 100 is an odds ratio of
  2.25, Woolf's range 0.9943 to 5.0916. On made-up orders (West late 40% against 10%, office a copy of
  region): West's odds ratio 4.85 (4.16 to 5.65), clearly above 1; predictors of amount rank units and
  price strong and the categories none, skip the order number as an identifier; predictors of late put
  region moderate, size weak, and office as saying the same as region (Cramér's V 1.0). The notebook
  code (the steps as checked SQL, then the stats call) run on a plain pandas DataFrame gives the
  window's results for all four analyses, to 1e-9 (sums in another order differ in the last digit).
  A JSON push takes pandas' split shape, not records (Open as grid sends that). In Chrome for Testing
  153: odds, Test together, find predictors, Open as grid, Send to Sandbox and Exclude these.
- (2026-11-13) The Sandbox as a report and the regression (Plans/sandbox-report.plan.md,
  Plans/analyze-menu.plan.md phase B). Cards keep the server-side order, size and new-page flag; Plotly,
  ECharts, Graphviz and image cards are accepted from the server (they were refused before). Page view
  printed through Chromium's own print engine gives landscape letter pages on light paper with the
  charts drawn and no controls; notes measured at their page width grow so no table row is cut. The
  markdown export embeds Plotly figures as PNG (Plotly.toImage) and Mermaid drawings as PNG or, with HTML
  labels, as SVG. Playwright's simulated mouse drag does not raise HTML drag events in the Sandbox's
  scrolled panel; the tests fire the browser's own DragEvents (and use Move earlier). Regression on
  made-up orders with planted effects: linear recovers units 3.005 (3), West +19.93 (+20), East -4.88
  (-5), equal to numpy's least squares to 1e-9; logistic recovers West's odds ratio 4.52 (e^1.5 = 4.48)
  and units 0.021 (0.02) in 5 rounds; what-if gives 99.8 (100) and a chance of 0.498 (0.5). pandas'
  missing numbers arrive as NaN, which DuckDB does not count as blank: rows with a non-finite number
  in a used column are left out and counted. The Data shape check finds a wide table (jan_, feb_,
  mar_ sales), identifiers and Benford's range (on the values above zero). t quantiles match the
  printed table (2.228 for 10 degrees of freedom).
- (2026-11-13) Time series and forecast (domain/timeseries.py). On made-up monthly sales (trend of 10 a
  month, December +34%, February -20%, a 60% spike in June 2021, a +300 step in January 2023), over 200
  seeds with and without the step: the step found within a month of January 2023 every time it was
  planted and never otherwise, the spike always found, 2 of 200 series with one false flag. December
  reads +31%, February -20%. The forecast with the step built in: trend 10.5 a month, next month 2,036
  against a true level of about 2,020, held-back miss 3% (4% without the step). Show these rows keeps
  exactly the four sales of June 2021 in the grid's order. The notebook code reproduces the window's
  words for both.
- (2026-11-13) Catalog completion against real IPython 9.18.1 (a throwaway environment with the plain
  client): with a running server and a made-up catalog, `shell.Completer.complete()` on
  `conn.sql("SELECT * FROM ord` gave order_lines and orders, on `pd.read_sql("""\nSELECT orders.cu`
  gave orders.customer_id, and on `x = ord` (not SQL) nothing. IPython 9 still runs
  `Completer.custom_matchers` (the first matcher API), which ipykernel uses for Jupyter too.
