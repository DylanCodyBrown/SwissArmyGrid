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
| 8 | Company policy on sending client data to an AI service | Not checked | Gates privacy levels above 0. The default is level 0 (no cell values). |
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
  INT32"), so an agent below privacy level 2 gets a generic message instead
  (`tests/test_sql_step_and_selection.py`).
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
