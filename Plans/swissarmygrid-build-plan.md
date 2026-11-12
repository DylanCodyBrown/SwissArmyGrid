# SwissArmyGrid — Build Plan

Name: **SwissArmyGrid**. A local server that shows any tabular
data in live browser grids, with built-in analysis, a step-based transformation panel
(think "Power Query applied steps"), code generation back to Python, charts, and an MCP
door so an agent can analyze alongside the user.

This plan is written to be handed to a coding agent. Read the whole thing, then build
milestone by milestone. Do not skip ahead.

---

## 0. Working agreements for the building agent

1. **Typed Python everywhere.** Type hints on every parameter and return type. Run a strict
   type checker (pyright or mypy strict) and a linter (ruff) as part of every milestone.
2. **Tests first for anything with semantics** (filters, steps, code generation). Write the
   golden test cases before the implementation.
3. **Small steps, one milestone at a time.** Finish a milestone's acceptance checks before
   starting the next. Report what passed and what was not verified.
4. **No scope creep.** Anything not in this plan goes in `docs/ideas.md`, not in the code.
5. **Ask the user before:** adding a dependency not listed here, enabling any server-side
   execution of agent-written code, writing outside the project folder, or making any
   network call other than package installation.
6. **No runtime downloads in the browser.** Vendor all frontend libraries into the project
   (install from npm, bundle, serve locally). Do not load scripts or styles from a public
   content delivery network; the work network may block them and it adds supply-chain risk.
7. **Mark every unverified assumption.** Section 12 lists things to verify against current
   documentation *before* relying on them. Record the result of each in `docs/verified.md`.
8. **Keep the browser light.** The browser draws and sends intents. The server does the work.
9. Prose in user-facing text and docs avoids unexplained acronyms.

---

## 1. Context and constraints

- Host: a robust local **Windows** machine. Server and browser normally run on the same
  machine. (If the browser is on another computer, the user forwards the port.)
- The user is a senior .NET/C#, SQL and Azure developer, new to some Python idioms. Clear,
  typed, well-commented Python matters more than cleverness.
- **Data is sensitive finance and client data.** Default posture: in memory, localhost only,
  token protected, nothing written to disk, nothing sent anywhere, no telemetry.
- Existing prototype files to start from (attach them to the build session):
  - `dfgrid.py` — renders a data frame as an AG Grid page and serves it locally. Includes
    `enable_inline()` for a Jupyter display hook. Use as a reference, not a base.
  - `swissarmygrid_client.py` — the push client, tested against a stand-in server. Its contract
    (section 5.1) is the contract the real server must honor.

## 2. Goals and non-goals

**Goals**
- Push any tabular object from any language into a named dataset; open any number of grids
  on a dataset; updates hot reload without losing sort, filters or column layout.
- Built-in analysis: per-column profile (distinct count, blanks, top values, statistics).
- A step list ("applied steps") with a pop-out panel; every step editable, reorderable,
  removable, previewable.
- A SQL box against the displayed table, with guard rails.
- "Copy as Python": code that reproduces the current view, matching the type of object that
  was pushed (DuckDB, pandas, or plain Python), verified against the grid's result.
- An MCP server exposing the same capabilities to an agent, with privacy levels.
- A side panel for charts and notes the agent (or user) adds beside the grid.
- Four themes: light, dark, high contrast light, high contrast dark.

**Non-goals (version 1)**
- Editing individual cells or writing back to source systems.
- Multi-user hosting, accounts, or any internet exposure.
- Joins in the point-and-click interface (the SQL box can join), computed columns beyond the
  listed transforms, regular expressions, window functions, nested filter groups.
- Quantile-based buckets.
- Server-side execution of agent-written Python (see milestone 9; gated and optional).

## 3. Key decisions already made

| Decision | Choice | Reason |
|---|---|---|
| Engine | Native **DuckDB**, in process, in memory | Fast, no database to install, handles big tables, great SQL |
| Server language | **Python** | DuckDB, pandas, Arrow are native; installs with `uv` or `pip` |
| Browser role | Thin: draws visible rows, sends intents | Keeps the page light with millions of rows |
| Grid component | **AG Grid Community**, vendored | Free edition; see section 12 for feature limits |
| Row loading | Server supplies row windows on demand (infinite row model) | Browser never holds the full table |
| Wire format for rows | Apache Arrow slices (JSON acceptable for the first cut) | Compact and fast |
| Live updates | WebSocket notifications carrying dataset versions | Hot reload |
| Data in DuckDB | Ordinary in-memory tables, not `TEMP` tables | Temp tables may be connection scoped (verify) |
| File access | The server loads files in Python; DuckDB itself has external access disabled | Lets SQL from the user or agent be locked down |
| Code verification | Generated code is run on the host and compared to the grid result | Never ship unverified code |
| Charts | Declarative specs (Vega-Lite via Altair, or Plotly JSON) with server-side aggregation | Safe; no code execution; light browser |
| Agent code execution | Off by default; milestone 9, sandboxed and approved per run | Highest risk feature |

## 4. Architecture

One Python process, two doors, one shared registry.

```
 Python / notebooks / scripts ──┐                       ┌── Browser grids (many)
 Visual Studio Code extension ──┼─► PUSH + GRID door ───┤
 command line `grid open x.csv` ┘   (HTTP + WebSocket)  └── Side panel (steps, charts)
                                         │
                                  Shared registry
                       datasets · grids · steps · charts · audit log
                                         │
                                    DuckDB (in memory)
                                         │
 Agent (MCP client) ──► MCP door (stdio launcher or streamable HTTP) ──┘
```

**Concepts**
- **Dataset**: a named table with a version number and a `source_kind`
  (`pandas`, `polars`, `duckdb`, `records`, `csv_file`, `json_file`, `arrow`, ...).
- **Grid**: a view of a dataset with its own id, its own step list and settings. Many grids
  can share one dataset. A grid's address is `/g/<id>`.
- **Step list**: ordered operations applied to a dataset to produce the grid's view
  (section 7). Steps are data, not code.
- **Chart / note card**: an item in the side panel, tied to a dataset (and optionally a grid).

**Process rules**
- Bind to `127.0.0.1` only. Random free port unless configured.
- On start, write `~/.swissarmygrid/server.json` with `{"port": ..., "token": ..., "max_dataset_bytes": ...}` (file
  permissions as restrictive as the operating system allows). Delete it on clean shutdown.
- Clients find the server through that file and may start it if it is not running.
- One shared DuckDB connection with a **cursor per request**. A bounded worker pool runs
  queries; interactive grid queries get priority over profiling and agent queries. A newer
  request for the same grid cancels the older one.

## 5. Contracts

### 5.1 Push (must match the existing client)

```
PUT /datasets/{name}?new_grid=0|1
Authorization: Bearer <token>
Content-Type: application/json   (orient="split": {"columns": [...], "data": [[...], ...]})
→ 200 {"grid_url": "http://127.0.0.1:<port>/g/<id>", "dataset": "<name>", "version": <int>}
```
- Missing or wrong token → 401. Disallowed origin or host header → 403. Dataset larger than
  the limit (section 6a) → 413 with a plain-language message in the response body.
- Same name again replaces the table, bumps `version`, and notifies every grid on it.
- `new_grid=1` also creates an additional grid on the dataset.
- Add an Arrow variant (`Content-Type: application/vnd.apache.arrow.stream`) in milestone 1
  or 2 and make the client prefer it when `pyarrow` is installed.
- Optional header `X-Source-Kind` (`pandas`, `duckdb`, `records`, ...) so code generation
  matches the input. Update the client to send it.
- Names are restricted to `[A-Za-z0-9_-]{1,64}`; reject anything else.

### 5.2 Grid data

```
GET /grids/{id}/rows?start=&end=&gen=     → Arrow slice for the visible window
GET /grids/{id}/count?gen=                → filtered row count
GET /grids/{id}/profile?column=           → column profile (section 8)
POST /grids/{id}/steps                    → replace or edit the step list
WS  /ws                                   → {"type":"dataset_updated","dataset":"orders","version":4}
```
`gen` is a request generation number so the server can drop stale work and the browser can
ignore stale responses.

## 6. Security requirements (musts)

1. Localhost only. Token required on every request including WebSocket upgrade.
2. Check `Origin` and `Host` headers on every request to defend against pages in the user's
   browser calling the local server.
3. Dataset names and file paths are validated; no path traversal.
4. DuckDB is started with external access disabled (verify exact settings) and its
   configuration locked, so no SQL can read files, write files, reach the network, install
   or load extensions. File loading and exporting happen in trusted Python code.
5. The SQL box and agent SQL pass a guard: parse with `sqlglot`; allow only `SELECT` and
   `WITH` queries and `CREATE TABLE <new name> AS SELECT`; the source dataset is read-only.
   Enforce a time limit, a row cap on results, and a memory limit.
6. DuckDB spilling to disk can write sensitive data to a temp folder. Set the temp
   directory deliberately or disable spilling; document the choice.
7. In memory by default. Saving a dataset to disk is an explicit user action.
8. Cell contents are untrusted. Never put cell text into prompts or tool descriptions as
   instructions. Return it to agents only as clearly delimited data.
9. Audit log (local file, no cell values): timestamp, actor (`user`, `agent`), action,
   dataset, query text, number of rows returned.
10. No telemetry, no outbound calls, no auto-update checks.

## 6a. Resource limits (hard caps)

Every limit is enforced by the server, set in one settings file, and reported in plain
language. "Size" means in-memory size after loading (the Arrow buffer size), not file size or
row count. One mebibyte (MiB) is 1,048,576 bytes; messages say MiB so the unit is never
ambiguous.

| Limit | Default | Behavior when exceeded |
|---|---|---|
| One dataset (a single push or load) | 100 MiB | Rejected, HTTP 413, plain-language message |
| All datasets and pinned results together | 3 GiB (1 GiB before 0.4.0), never above 50% of physical memory | New loads refused; nothing is evicted silently |
| Datasets open | 50 | Refuse the next one |
| Grids open | 20 | Refuse the next one |
| Rows per window request | 1,000 | Clamp or reject |
| Displayed cell text | 500 characters | Truncate in the grid; full value in a detail view and in exports |
| SQL box result rows | 10,000 | Stop and say so |
| Agent query result rows | By privacy level (level 2: 200) | Stop and say so |
| Chart points | 5,000 | Aggregate harder |
| Single response size | 8 MiB | Reject |
| Export | Not bound by the dataset cap; streams | Configurable maximum |

**Rules**
- **Enforce early.** Check `Content-Length` before reading. Count bytes while streaming and
  abort as soon as the limit is passed, without buffering the rest. For Arrow and Parquet,
  measure the decoded size and guard against files that expand far beyond their compressed
  size (decompression bombs).
- **Clear errors.** Example: "Dataset 'orders' is 143.2 MiB; the limit is 100 MiB. Filter,
  sample, or select fewer columns before pushing, or raise the limit in settings."
- **Never silently truncate or sample.** Offer an explicit "load the first N rows" action
  that the user chooses.
- **Total budget:** when full, refuse the load and show every dataset with its size and a
  close button. "Close the least recently used" is an explicit action, never automatic.
  Results pinned as new grids count against the budget. Step views are lazy and do not.
  Caches (pages, profiles) have their own size cap and may be evicted freely.
- **Raising limits** only through settings, with an absolute ceiling of 50% of physical
  memory, read at startup.
- **Working memory is separate from the dataset cap.** Sorts, joins and group-bys need memory
  beyond the data itself. Set DuckDB's memory limit to the total data budget plus headroom
  (start at 2 times). Because spilling to disk is off by default, a query that exceeds it must
  fail cleanly with a message and leave the server running. Verify how DuckDB behaves.
- **Visibility:** a memory meter in the status bar ("412 MiB of 1 GiB of data"), a datasets
  list with sizes, and a warning at 80% of any limit.
- **Client pre-check:** the server writes `max_dataset_bytes` into the connection file. The
  push client estimates the size and fails before sending anything. The server remains the
  authority and always re-checks.
- **Tests:** a push just below, exactly at, and just above the limit; a streamed upload that
  exceeds the limit aborts early; a compressed file that expands past the limit is rejected;
  the total budget refuses the next load; the 51st dataset is refused; a window request over
  1,000 rows is clamped or rejected; a long cell is truncated on screen but intact in the
  detail view and exports; a query exceeding working memory fails cleanly and the server stays up.
- **Parked idea:** file-backed datasets (a Parquet file scanned in place) could exceed the cap
  without using memory. It needs a careful security review, because DuckDB's external access
  is disabled.

## 7. Step list and operations (version 1)

A **step** is a typed record: `{id, kind, params, enabled}`. The grid's view is the
dataset passed through enabled steps in order. Each step is also a DuckDB view over the
previous one, which makes "preview at step N" cheap.

**Reference semantics** (all three code generators must reproduce these exactly):
- SQL null semantics: a blank never equals anything, including another blank.
- Sorting: blanks last, ascending and descending.
- Group by keeps the blank group. (Pandas drops it by default; its generator must override.)
- Text comparison is case sensitive unless the grid's case setting is off, in which case
  both sides are lowercased (simple lowercase, not full Unicode case folding).
- Sample standard deviation; quantiles with linear interpolation.
- Weeks start on Monday.

**Operations**
- Filters: equals, not equals, in list, not in list, is blank, is not blank; text: contains,
  starts with, ends with; numbers: less than, greater than, between; dates: before, after,
  between; booleans: true, false. Combination: AND across columns, OR within one column's list.
- Sort (multiple columns). Columns: select, hide, reorder, rename.
- Group by: count of rows, count of non-blank, distinct count, sum, min, max, mean.
- Distinct rows. Limit.
- Column transforms (one column, several, or every text column): trim left, trim right,
  trim both (spaces, tabs, line breaks), upper case, lower case.
- **Bucket** (always a new column): numbers by fixed width or custom edge list (left-closed by
  default with a toggle; out of range becomes an "Other" label; blanks stay blank); dates by
  day, week, month, quarter, year.
- **Categorize** (always a new column): ordered rules, first match wins, plus an unmatched
  label. Rule types: wildcard (`*` and `?` only), equals one of a list, contains, starts with,
  number range. Show the unmatched count and warn on rows matching more than one rule.

**Toolbar settings** (per grid, saved with the steps and reflected in generated code):
case sensitive, wildcards on or off, ignore leading and trailing spaces, treat empty text as
blank.

**Wildcard translation:** `*` and `?` only. Translate to `LIKE` with an escape character
(escape literal `%`, `_`, and the escape character itself). Plain Python uses
`fnmatch.fnmatchcase` (not `fnmatch.fnmatch`, which normalizes case on Windows) after
rejecting or escaping `[`.

**Recipes:** a grid's step list and settings save to a small file with no data. Pushing a
new version of a dataset with the same columns reapplies the recipe automatically. If a
column is missing, mark the first broken step and stop at it.

## 8. Profiling

Computed in DuckDB on the filtered rows, cached by `(dataset version, step list hash)`.
- Every column: row count, blank count and percent, distinct count, all-unique flag, top
  values with counts.
- Numbers: min, max, mean, median, quartiles, standard deviation, sum, zero and negative
  counts, histogram bins.
- Text: shortest and longest length, empty strings, values with leading or trailing spaces,
  groups of values that differ only by case.
- Dates: earliest, latest, counts by month.
- Whole table: duplicate rows.
Surface in column headers (distinct count, blank percent, tiny histogram), a click-through
side panel with value counts (clicking a value adds an equals filter step), a bottom bar
(rows shown of total; count, sum and average of selected cells), and a whole-table
Profile view that can be exported.

## 9. Code generation and verification

- Record `source_kind` at push. Generate Python matching it:
  - `duckdb` → DuckDB relation or SQL code.
  - `pandas` → pandas code.
  - `records` / file kinds → plain Python (`csv`, `json`, list comprehensions, `fnmatch`).
- Generators are driven by the neutral step list, never by the SQL text. SQL typed in the SQL
  box is parsed into steps with `sqlglot`. If any part cannot map to steps, generate the
  DuckDB form with a one-line reason. Never silently produce different behavior.
- Output is a typed function, for example
  `def transform(orders: pd.DataFrame) -> pd.DataFrame`, with a header comment recording
  source columns, types and row count at generation time. No data is embedded.
- **Verification:** run the generated code (in the sandbox from milestone 9, or a restricted
  in-process runner with a fixed allowlist for generated code only — decide with the user) on
  the same data, and compare with the grid result: same columns, same rows, same order where
  order is defined. Show "verified identical" or the first differing rows. Only verified code
  gets the plain copy button.
- **Golden tests are shared:** one data-driven table of `(input table, step list, expected
  output)` cases runs through the DuckDB engine and all three generators. Include blanks,
  empty strings, case variants, padded spaces, integers versus floats, dates and week
  boundaries, wildcard characters, `ß` and other Unicode edge cases.
- No operation ships until it has all four implementations and tests.

## 10. Browser requirements

- Thin client in TypeScript, bundled with a light bundler (Vite or esbuild). No heavy
  framework required; keep dependencies minimal.
- AG Grid Community with the infinite row model; sort and filter sent to the server as
  steps. Debounce filter typing (about 200 ms); ignore stale responses by `gen`.
- Layout: grid, collapsible steps panel (pop-out window allowed, synced through the
  browser's window messaging), side panel for charts and notes, bottom bar, toolbar:
  `Aa` `Wildcards` `Trim` `Empty = blank` | `+ Bucket` `+ Categorize` | `SQL` `Profile`
  `Copy as Python` `Export`.
- Steps panel: numbered plain-language steps; enable or disable, drag to reorder, edit in
  place, delete, insert; row counts between steps ("12,000 → 11,420"); click a step to
  preview the data at that point and highlight changed cells; per-step errors; a code tab.
- SQL box: Monaco editor with column name autocomplete, run with Ctrl+Enter, cancel button,
  row count and time, history tagged `user` or `agent`. Results replace the grid with a
  "back to table" button; "pin as new grid" creates a dataset from the result.
- Export to delimited text, comma-separated values and Excel: see section 10a.
- Clipboard: paste tab-separated text from Excel to create a dataset; copy selected cells as
  tab-separated text.
- Charts are never sent raw rows (see section 11).

## 10a. Export (text file, comma-separated values, Excel)

A first-class feature, not an afterthought. Files are written only by trusted server code.

**Formats:** delimited text file (`.txt` with a chosen delimiter, including tab and pipe),
comma-separated values (`.csv`), Excel (`.xlsx`), and Parquet (`.parquet`).

**What can be exported (scope selector):** the current view (all rows after the steps),
selected rows only, selected columns only, the whole source dataset ignoring steps, and the
Profile table. Export uses the grid's own step list, so the file always matches the grid.

**Delimited text and comma-separated values options:**
- Delimiter: comma, tab, pipe, semicolon, or one custom character. Header row on or off.
- Quoting: only when needed (default) or always. Cells containing the delimiter, quotes or
  line breaks must be quoted and escaped correctly.
- Line endings: Windows style by default on Windows, with an option for Unix style.
- Encoding: UTF-8 by default, plus "UTF-8 with byte order mark" so Excel opens accented
  characters correctly. Other encodings only on request.
- Blank representation: empty (default) or custom text.
- Dates in ISO format by default; numbers with a decimal point and no thousands separators.

**Excel options and behavior:**
- Real typed cells: numbers as numbers, dates as dates, text as text. Text columns stay text,
  so identifiers with leading zeros are never turned into numbers.
- Integers longer than 15 digits are written as text, because Excel numbers lose precision
  past that.
- Sheet name, bold header row, frozen header row, auto filter, sensible column widths.
- Optional second sheet "Provenance" containing the source, the applied steps in plain
  language, the export time and the row count. It contains no data and is on by default.
- Excel limits: 1,048,576 rows per sheet and 32,767 characters per cell. Refuse or split
  across sheets with a clear message, and warn before truncating any cell.
- Use a constant-memory writer so large exports do not exhaust memory.

**Parquet options and behavior:**
- Preserves column types exactly (integers, decimals, dates, timestamps, booleans, text), which
  makes it the best format for handing data to other tools such as Databricks or Python code.
  Excel cannot open it, so offer Parquet alongside, not instead of, comma-separated values.
- Written by trusted Python code with `pyarrow` (DuckDB's own file writing stays disabled).
  Write in batches (row groups) so large exports use constant memory.
- Compression: Snappy by default for the widest compatibility, with Zstandard as an option.
- Timestamps: record whether a column has a time zone, and never silently shift times.
- File metadata: store the same provenance as the Excel sheet (source, applied steps in plain
  language, export time, row count) as custom Parquet key-value metadata. No data in it.
- Optional later: partitioned export, a folder of files split by one chosen column.
- Formula neutralization does not apply to Parquet.

**Spreadsheet formula injection:** text values beginning with `=`, `+`, `-`, `@`, a tab or a
carriage return can be run as formulas when a file is opened in Excel. By default, neutralize
them in text columns only (typed numbers are untouched) and write Excel text as string cells.
Provide a visible setting to turn this off, with a warning.

**Delivery:**
- Default: the file streams to the browser as a download, so the user chooses where it lands.
  The server does not write to arbitrary paths.
- Optional "save to export folder" into one configured folder. File names are validated (no
  path separators, safe characters, correct extension) and an existing file is never
  overwritten without asking.
- Large exports run in a lower-priority worker, show progress, and can be cancelled.
- A confirmation shows "Exported 11,420 rows × 8 columns to orders.csv".

**Governance:**
- Every export writes an audit log entry: actor, dataset, format, row count, file name,
  settings. Never cell values. Exports are the main way client data leaves the system.
- The MCP `export` tool writes only into the configured export folder, never as a browser
  download, and requires the user's approval click.

**Dependencies:** `pyarrow` (also needed for the Arrow push format) for Parquet, plus one Excel
writer library. Prefer a write-only streaming writer
(`xlsxwriter`); `openpyxl` is the alternative. Verify its license and streaming mode first and
ask the user before adding it.

**Tests:** round trip every format (export, read back with an independent reader, compare
values, types and row order). Cases: commas, quotes and line breaks inside cells, leading
zeros, very long text, Unicode, blanks versus empty strings, dates and times, integers over 15
digits, formula-looking text, a table exceeding the Excel row limit, and an empty result. For
Parquet also check that column types survive the round trip (read back with `pyarrow` and
pandas and compare schemas), that blanks stay null, that timestamps keep their time zone
status, and that the provenance metadata is present and contains no cell values.

## 11. Charts and the side panel

- Cards for charts, markdown notes, small result tables. Each card records its source
  (dataset, version, steps) and shows its spec, with pin, export (image or HTML), and
  "copy as Python" (Altair code for the same chart).
- Agents provide a **declarative specification** (Vega-Lite, or Plotly JSON). No agent code
  is executed. Validate every specification against the schema and reject unknown keys.
- The server aggregates before sending: histogram bins, group counts, binned or sampled
  scatter points. Cap points per chart (hundreds to a few thousand).
- Cards redraw when their dataset updates. Selecting in a chart adds filter steps to the
  grid (cross-filtering), and grid filters can drive chart highlighting.
- Colors come from theme tokens; categorical palettes checked for contrast and color
  blindness; never rely on color alone (use shapes, patterns, or direct labels).

## 12. MCP server

- Official Python MCP library. Provide a **stdio launcher** that connects to the running
  server (starting it if needed) and, if supported, a streamable HTTP endpoint on
  localhost with the token. Verify transports and how to mount extra routes before deciding.
- **Tools:** `list_datasets`, `describe_dataset` (schema, counts, profile), `profile_dataset`,
  `query` (guarded read-only SQL, row cap), `load_file`, `open_grid`, `update_grid`
  (filters, sort, highlights), `get_view_state` (filters, sort, selection; no cell values
  unless the privacy level allows), `save_result`, `export`, `add_chart`, `update_chart`,
  `add_note`, `remove_item`.
- Agents pass names and file paths, never table contents. Pushes do not go through MCP.
- **Privacy levels per dataset** (default 0):
  - 0: schema, counts and profile only.
  - 1: also small masked samples.
  - 2: full rows on datasets the user explicitly approves.
  Raw rows beyond the cap need an approval click in the grid. Column masking (hide or hash)
  applies at levels 1 and 2.
- Agent actions are visible in the grid (tagged queries, highlights) and written to the
  audit log. No tool can delete or overwrite source data, write files outside a sandbox
  folder, or reach the network.
- Default behavior: aggregate queries within the allowed level run automatically; queries
  returning raw rows wait for a click. The user can change this.

## 13. Themes and accessibility

- Four themes: light, dark, high contrast light, high contrast dark. All colors are style
  variables defined once as tokens. No hard-coded colors anywhere, including generated charts.
- Follow the system by default (dark mode and "more contrast" preferences) with a saved
  manual override. Support Windows forced-colors mode and test it separately.
- When embedded in Visual Studio Code, follow its theme (it exposes theme classes for the
  four variants; verify names).
- Contrast: text at least 4.5 to 1, interface elements at least 3 to 1; high contrast themes
  aim for 7 to 1, with thick always-visible focus outlines and strong borders. No reliance on
  zebra striping. Blank, changed, error and agent-versus-user markers carry an icon or text
  as well as color.
- Monaco's built-in light, dark and high contrast themes; AG Grid light and dark Quartz
  themes with our overrides.
- Test: a script that checks every token pair's contrast ratio; screenshots per theme;
  keyboard-only and screen-reader passes on the grid.
- Define tokens in milestone 1, harden in milestone 8.

## 14. Performance targets (measure and report, do not guess)

- 1,000,000 rows × 10 numeric or short-text columns (roughly inside the 100 MiB default; raise
  the cap in settings for larger tests): first window visible in under 300 ms; scrolling smooth.
- Applying a filter or sort on that table: result window under 500 ms.
- Profile of one column on that table: under 1 second.
- Server memory: DuckDB memory limit configurable, default leaves headroom for the machine.
- Rapid typing in a filter box never queues more than one pending query per grid.

## 15. Project layout (suggested)

```
swissarmygrid/
  pyproject.toml            # uv project; strict typing, ruff, pytest
  src/swissarmygrid/
    server/                 # web app, auth, origin checks, websocket, info file
    engine/                 # DuckDB setup, datasets, steps to SQL, profiling, guard
    codegen/                # duckdb.py, pandas.py, plain.py, verify.py
    mcp/                    # tools, privacy levels, audit log, stdio launcher
    charts/                 # spec validation, aggregation
    client/                 # push client package (importable from user notebooks)
  web/                      # TypeScript app, vendored libraries, theme tokens
  tests/
    golden/                 # shared cases for engine and all generators
  docs/
    verified.md             # results of the "verify" list
    ideas.md                # parked ideas
```

## 16. Milestones and acceptance checks

**M0 — Scaffold.** `uv` project, strict type checking, linter, pytest, a health route, the
info file, token and origin checks with tests (wrong token 401, wrong origin 403).
*Done when:* all checks pass and the section 12 verification list is started.

**M1 — Core: push, live multi-grid.** In-memory DuckDB with external access locked;
`PUT /datasets/{name}`; client updated (source kind header, optional Arrow); grid page with
vendored AG Grid, infinite row model, server-side sort and simple filters; row count; WebSocket
version notices; hot reload that preserves sort, filters and column layout; multiple grids per
dataset; theme tokens defined with light and dark working.
*Done when:* the existing client pushes a frame and a grid opens; pushing again refreshes the
grid in place; two grids on one dataset update together; a 1,000,000-row frame meets the
performance targets (report numbers).

**M2 — Profiling.** Header stats, column side panel, click-a-value filter, bottom bar
selection stats, Profile view.
*Done when:* profile values match pandas on the same data in tests (including blanks).

**M3 — Steps, toolbar, SQL box.** Step list model with the version 1 operations (without
transforms), the steps panel with preview and row counts, toolbar settings, recipes, the SQL
box with the guard, pin as new grid, and export to text, comma-separated values and Excel
(section 10a).
*Done when:* every guard rule has a failing-input test (file read, network, `COPY`,
`ATTACH`, extension load, multiple statements, writing to the source); recipes reapply after a
re-push; column drift is reported at the right step; every export test in section 10a
passes, including the row limit, formula-injection, and leading-zero cases; each export
appears in the audit log.

**M4 — Code generation and verification.** Neutral steps to DuckDB, pandas and plain Python;
`sqlglot` mapping from the SQL box; source kind detection; verification harness; shared golden
tests; "Copy as Python".
*Done when:* every golden case passes through all four paths with identical results; an
unmappable query falls back to the DuckDB form with a stated reason; no copy button appears
for unverified code.

**M5 — MCP server.** Stdio launcher, tools, privacy levels, approval flow for raw rows, audit
log.
*Done when:* an MCP client can describe, profile, query and open a grid with filters; level 0
returns no cell values; a malicious cell value (text that looks like an instruction) is returned
only as delimited data; every agent action appears in the audit log.

**M6 — Transforms.** Trim, upper, lower; bucket; categorize with live unmatched and overlap
checks. Add generators and golden tests for each as they are built.
*Done when:* each transform passes the shared golden tests, including Unicode and boundary
cases.

**M7 — Side panel and charts.** Cards, chart specification validation, server-side
aggregation, redraw on update, cross-filtering, Altair export.
*Done when:* a chart over a 1,000,000-row table sends no more than a few thousand points;
malformed specifications are rejected; charts follow the theme.

**M8 — Themes and accessibility hardening.** All four themes across every component,
forced-colors check, contrast script, keyboard and screen-reader pass.
*Done when:* the contrast script passes for every token pair and each theme has screenshots
reviewed by the user.

**M9 — Optional: agent Python sandbox.** Only if the user approves after M5–M8. Separate
process per run, timeout and memory limits, read-only data snapshot as an Arrow file, no
network, no access to the user's files, credentials or network shares, output limited to image
or specification bytes, code shown and approved before the first run. On Windows, evaluate a
container (Docker Desktop or the Windows Subsystem for Linux) versus a low-privilege account
with firewall rules. *Do not build this without asking.*

Parallel extras after M4: Visual Studio Code extension ("View in grid" on a file or a
debugger variable), Jupyter display hook, drag and drop, installable offline page.

## 17. Verify before relying on it (record results in `docs/verified.md`)

1. **AG Grid Community:** the infinite row model, the filter model shape, which filter types
   exist in the free edition, and the light and dark theme classes for the pinned version.
   Distinct-value checklist filters, row grouping, pivoting and side tool panels are, as far
   as known, Enterprise features; do not depend on them.
2. **DuckDB:** the settings that disable external access and lock configuration; whether
   `trim` accepts a character list; `LIKE ... ESCAPE` behavior; whether temporary tables are
   connection scoped; quantile and standard deviation function semantics; how memory limit and
   temp directory interact with spilling.
3. **MCP library:** supported transports, and how to mount extra web routes next to the MCP
   endpoint.
4. **Arrow data exchange interface:** which minimum versions of pandas, Polars, PyArrow and
   DuckDB expose it, for accepting "anything tabular".
5. **sqlglot:** how well it parses the DuckDB dialect for the subset in section 7.
6. **Visual Studio Code:** theme class names for webviews and whether the debugger variables
   context menu action works as expected.
7. **Licenses and sizes:** Monaco, any chart libraries, and every dependency (no copyleft
   surprises). The user should check the company's open-source policy.
8. **Company policy:** whether sending client data to an AI service is allowed at all. This
   gates the MCP privacy levels above level 0.
9. **Windows sandbox options** for milestone 9.
10. **Excel export:** the chosen writer's license and streaming mode, Excel's current row,
    column and cell limits, how Excel treats a byte order mark in comma-separated values
    files, and how Excel handles text cells that begin with `=`, `+`, `-` or `@`. For Parquet,
    check how the tools the user cares about (Databricks, Power BI, pandas, DuckDB) read the
    chosen compression and timestamp types.
11. **Resource limits:** how DuckDB's memory limit counts in-memory tables versus working
    memory, what happens when a query exceeds it with spilling disabled, how to measure
    Arrow buffer size cheaply, and whether the web framework lets a streamed upload be
    aborted mid-body.

## 18. Open questions for the user

1. Can the host run containers (Docker Desktop or the Windows Subsystem for Linux), or only
   plain processes?
2. Should the agent's raw-row queries always wait for approval, or run automatically on
   datasets the user has opted in?
3. Should SQL box results replace the grid view (with a way back), or always open a new grid?
4. Is Windows High Contrast mode used by the team, or are the four in-app themes enough?
5. Where should saved recipes and the audit log live (project folder, user folder, or a
   shared location)?
