# Ideas and open notes

Out-of-scope ideas (plan section 0, rule 4), plus notes from the 2026-10-21 review of the plan
that haven't been folded back into it yet.

## Decisions made while building M0 and M1 (confirm or change)

- **Web framework: Starlette + uvicorn**, plus `websockets` for uvicorn's WebSocket support. The
  plan names no framework. The official MCP Python library is built on Starlette, which should
  help with M5.
- **Type checker: mypy strict.** The pip build of pyright downloads its engine from npm on first
  run, and the plan asks for no network calls beyond installing packages.
- **The grid page shell and static files need no token.** The plan says "token required on every
  request". A browser can't attach a header when it loads a page, so `/g/<id>` and `/static/` are
  public: they hold no data, only the page and the vendored library. The token goes in the URL
  fragment (`#token=`), which browsers never send to a server, and the page attaches it to every
  data request. Host and Origin are still checked on those paths.
- **WebSocket token as a subprotocol**, not a query parameter, so it never appears in the
  server's access log.
- **A request with no Origin header is allowed** (the push client never sends one); a request
  with a foreign Origin gets 403.
- **Separate caps for request body size and dataset size.** The plan's 6a measures the loaded
  Arrow size but also says to refuse on Content-Length. JSON is several times bigger than the
  same data in Arrow, so `max_push_body_bytes` (400 MiB) is separate from `max_dataset_bytes`.
- **Rows come back with the row count** (no separate `/count` route yet), and sort is a `sort=`
  query parameter. Both move to the step model in M3.
- **No thousands separators in grid numbers**, so identifier columns don't look like amounts.

## Decisions made while building M1-M8 (confirm or change)

- **Dependencies added under "build it out as robust as the plan":** sqlglot, mcp and
  xlsxwriter (the plan asked to check xlsxwriter first: BSD-2-Clause, and its in_memory mode is
  used so nothing goes to a temp folder); Monaco, Vega, Vega-Lite and vega-embed in the page
  (MIT and BSD-3, loaded only when first used); Playwright and openpyxl for tests only.
- **The step engine was built before M2**, since profiling, SQL, export and code generation
  all read the step view. Every grid change (header sort, chart click, agent edit) is a step.
- **Charts use a small declarative spec, not raw Vega-Lite** (charts/spec.py): strict
  validation is simple and the server can aggregate before drawing. The server then builds
  the Vega-Lite.
- **Generated code verification** runs only this package's own generated code, in process,
  with an import allowlist checked on the syntax tree. It is not a sandbox (that is M9). Since
  2026-11-12 it runs the code exactly as copied, in a namespace holding only the user's table
  (see "Copied code is one DuckDB query" below).
- **The approval endpoint trusts the browser.** Any process running as the same Windows user
  can read the connection file and approve its own request. The gate stops an agent that only
  has the MCP tools.
- **Group-by output is sorted by its keys** (blanks last), which matches pandas `sort=True`.
- **Header sort is the last word:** new steps go before the header-sort step.
- **The analysis panel starts collapsed in windows narrower than 1,100px** (VS Code's side
  browser, a laptop split).
- **The MCP server ships as a wheel in the plugin**, run with `uvx --from <wheel>`, so
  teammates need uv on their PATH.

## Decisions made on 2026-10-22 (confirm or change)

- **`%` is the only wildcard** (it was `*` and `?` in plan section 7), so a filter reads like SQL
  LIKE; `*`, `?` and `_` are literal.
- **Selection is Excel's, not checkboxes.** A header click now selects its column, so sorting
  moved to the arrow button in each header and to the ribbon's Column group.
- **Copy includes the header row** for whole rows, columns or everything, and not for a block of
  cells (Excel copies only cells; a header line helps when the whole table goes elsewhere).
  Copy stops at 200,000 rows and points to Export.
- **A SQL step reads only the table entering it**, never another dataset, so its generated
  Python needs no other data; joins stay a Pin-as-new-grid job. It names that table by the
  dataset's own name (2026-10-24, so the SQL pastes into the user's Python unchanged, where the
  table is a variable of that name) or `grid` (kept for older steps and recipes). In the SQL box
  a dataset name, the grid's own included, reads the rows as loaded, like the Python variable
  (0.6.0 briefly read the grid's view there instead, so Apply kept the box's rows; 0.6.1 went
  back so the box matches the notebook). So with earlier steps, the same text can give the box
  and the step different rows; with the SQL step first, the box, the step and the pasted Python
  all agree. A query with no defined order is sorted by every
  column (`ORDER BY ALL`).
- **Generated pandas and plain Python run a SQL step in DuckDB** (the plain target then needs
  duckdb, and says so in its header). There is no faithful pure-Python translation of SQL.
- **`read_grid` keeps the privacy caps** (20 rows at level 1, 200 at level 2) and the approval
  click; the agent's `limit` works inside them. Say if level 2 should allow more per read.
  (Superseded 2026-11-12: the privacy levels were removed; see below.)
- **The ribbon names what each tool acts on** (Table, Column, Cell, with the target in the
  caption); a Column or Cell tool is disabled, with the reason in its tooltip, until something
  it can act on is selected.

## Key Finder decisions (2026-10-22, confirm or change)

- **Both sides are grid views**, each after its own steps, so a grid can be filtered down
  before looking for its join. The target can be the grid's own dataset (a self-join such as
  parent_id to id); a column is never paired with itself.
- **Values compare as normalized text:** 42, 42.0 and '42' meet; a timestamp at midnight meets
  its date; Trim (on) and Ignore case (off) apply to text. Columns longer than 128 characters,
  true/false, lists, structs and binary columns are left out and listed with the reason.
- **Score** = (0.7 x overlap coefficient + 0.3 x the lower of the two percent-found figures) x
  min(1, log10(shared) / 3). The second term breaks the classic tie between a real reference
  and an unrelated running counter (an order_id of 1..6,000 contains every customer id of
  1..2,000, but covers them only a third of the way). The last factor keeps Y/N flags and small
  code lists below real keys.
- **Single columns only.** Composite keys (two columns that join only together) are a later
  step: the same inverted-index join over column pairs, with the candidate count kept small.
- The Key Finder is a natural companion to compare (below): a found pair can become the
  compare keys.

## The "+" tab and the window race (2026-10-23, confirm or change)

- **A table name in the "+" tab reads its tab's view**, after that tab's steps (Dylan's choice
  over the raw rows). Two tabs on one dataset: the name reads the first (oldest) grid, the one a
  re-push reuses. A dataset with no tab open reads its rows as loaded.
- **The result is stored, not live.** Run makes a dataset (source kind `sql_result`, with the
  query kept on `DatasetInfo.query`); a change to a table it reads doesn't rebuild it. Run again
  does, in place, keeping the tab's own steps.
- **Run again rebuilds only a table a query made.** `replace` on `POST /datasets/{name}/query`
  is refused for a pushed or loaded dataset, and without `replace` any name in use is refused, so
  a "+" tab can never overwrite another table by picking its name. A query can't read the table
  it builds.
- **The tray is the base data; the SQL box stays a step.** Both open the same Monaco editor, and
  each editor now keeps its own autocomplete names.
- **A push told to open a window holds the next pushes back** until that window connects, or 15
  seconds, so a script pushing three tables opens one window. The MCP's `open_grid` uses the same
  rule.
- Open: the pin path (`CREATE TABLE ... AS` in the SQL box) still fetches with
  `execute(q).to_arrow_table()`, which copies the rows once more than `cursor.sql(q)` (the "+"
  route uses the latter). Worth switching; left alone to keep this change to the "+" tab.
- Open: a closed "+" tab's Monaco editor isn't disposed (panes.ts has no pane-closed hook). Small,
  but it grows with every query tab closed in one page session.

## Copied code is one DuckDB query over the user's table (2026-11-12, confirm or change)

The owner's report: copied SQL and Python "never was the actual transformation script that would
work to transform the source table back in the ENV". Reproduced: the DuckDB copy only defined
`transform(con, rel)` (pyarrow, a `__sag_ord` column, a table registered as `source`), so pasting
it computed nothing and calling it on the user's DataFrame failed; the pandas and plain copies
also only defined `transform()` with helpers and imports. Verification ran them against an Arrow
relation, not the user's variable, so they were marked verified anyway.

- **The DuckDB copy is the default for every source**, and is the query itself: one statement over
  the table by its variable name (the dataset name). Copy as SQL gives the query; Copy as Python
  gives `import duckdb` and `orders_view = duckdb.sql("""...""").df()` (`.pl()` for Polars,
  `.to_arrow_table()` for Arrow, the relation for DuckDB, `.df()` otherwise). This is the plan's
  "Grids pushed from outside" bullet under Code-behind.
- **Built from the engine's own step pieces** (engine/steps.py helpers), not a translation. Row
  order without the hidden column: DuckDB's order preservation, plus `row_number() OVER ()` as the
  last sort key, because ORDER BY is not stable (docs/verified.md). Remove duplicates numbers its
  input in a subquery (`_row`), the one helper column, inside the query only.
- **Verification runs exactly the copied text** with `exec` in a fresh namespace holding only the
  user's variable, rebuilt as what was pushed, and reads back `<name>_view`. It is in process (a
  fresh interpreter per check took about 0.6 s, too slow for the golden suite); the tests also run
  every case in a fresh `python -I` (tests/test_copied_code.py). `duckdb.sql` uses DuckDB's default
  connection, so the server locks that connection down on first use (no file system, no extension
  downloads, no spilling, memory limit, then lock_configuration) and runs one check at a time.
- **pandas is not a server dependency**, so in an installed server `.df()` and the pandas
  translation could not be run. The DuckDB check then runs the same query over an Arrow table and
  says the final `.df()` was not run; the pandas translation reports "Not verified". Open: add
  pandas to the `[server]` extra (a dependency decision for the owner).
- **pandas and plain Python stay as secondary targets** ("longer translation" in the picker), and
  now end with `orders_view = transform(orders)` so they too transform the user's variable. The
  plain one reads a list of dicts, so it cannot run on a pushed DataFrame; the tab says what each
  option reads.
- Open: a pandas frame pushed with a non-default index arrives with that index as a column
  (`reset_index()` in the push client), which the user's own frame does not have, so the copy's
  `SELECT *` shows one column fewer than the grid. A list of dicts, a file or the clipboard has no
  variable DuckDB can read; the copy expects a DataFrame of that name and says so.

## The Sandbox (2026-11-13, the owner's request; page part done, server part after the refactor)

"The agent tab should be renamed to sandbox ... every single image output to land in the sandbox ...
more visual analytics languages ... dynamically size those graphs ... like a container ... savable
dashboards that ... open up with the workspace." The owner's choices: images come from Python (the
agent's or the user's draws the figure and sends the picture; SwissArmyGrid never runs agent code, so
no milestone 9); Plotly, Graphviz (DOT) and Apache ECharts besides Vega-Lite and Mermaid; the page part
first, the server part once the Cosmic Python refactor has landed.

- **Done (page):** the tab is Sandbox; a 12-column dashboard with 40px rows, each card resized from its
  corner grip (or the arrow keys on it), what it draws refitted to the card; renderers for Plotly
  4.2.0, ECharts 6.1.0 and Graphviz (@viz-js/viz 3.31.0), each loaded only when its first card draws;
  image cards (inline PNG, JPEG, GIF, WebP or SVG in an <img>, which never runs or loads anything);
  Plotly and ECharts take the theme's chart colours unless a figure sets its own.
- **Safety:** the grid page now has a Content Security Policy: only its own scripts, no inline
  handlers, nothing fetched from another host. Figures are cleaned besides: Graphviz and Mermaid SVG
  through svgSafe (no scripts, handlers, animations, embedded HTML or outside links); Plotly refuses map
  traces (they fetch tiles) and keeps only inline layout images; ECharts drops images, links, the
  graphic layer and the data view, and draws tooltips as text.
- **Done (server, 2026-11-13):** cards of no table (`POST /cards`: notes, figures, pictures, a table of
  its own rows; badged Python, Agent or Sandbox; they stay when grids close; the Sandbox's Add note
  makes one); `key` replaces a card in place, keeping its size and order; `sag.sandbox` (duck-typed,
  standard library only); a `vegalite` card for Altair, **refused from the agent** (Vega compiles its
  expressions and the page allows that; the agent has the checked `chart`), its data inline (any url or
  href refused, so it fetches nothing); `sandbox_formats` and `sandbox_add` with `file`, `key`, `size`
  and its own rows.
- **Done (2026-11-13): saved dashboards and their history**, with one change from the design below,
  **to confirm**: nothing is saved automatically. The rule is that nothing is written to disk except
  explicit exports, recipes and the audit log, and an automatic save or a daily snapshot would write
  chart points and table rows without a click. So a dashboard is saved on **Save** (Dashboards… in
  the Sandbox); saving the same name again adds a version (the last 60); History opens any version.
  Each card keeps what it showed ("frozen"), so a dashboard opens without its tables; a chart or query
  relinks to a grid on its table whenever one is open, by the table's name. The agent lists and reads
  (`sandbox_dashboards`); saving (it writes to disk), opening (it replaces the screen) and deleting are
  the user's. Still open: automatic save if wanted, a read-only look back ("Viewing 10 November"),
  comparing two versions (cards added, removed, changed), and one dashboard per workspace once
  workspaces exist.
- **Done (2026-11-13): data-bound figures.** A Plotly or ECharts card on a grid may carry a SELECT; the
  server runs it on the grid's view (its table's name reads the view, as in a SQL step; first 5,000
  rows) and fills the figure: Plotly trace fields that name a column, ECharts' `dataset.source`.
- **Next (server, after the refactor), the owner's follow-up:** "a sag command to push anything to the
  sandbox ... the script can push anything that is visual"; "the MCPs should be fully aware of what
  tools are available to build graphs and charts"; "an agent can push rendered markdown ... resizable
  like a text box"; "no gaps in MCP tools ... the agents will need to understand what they are looking
  at, what's available and what data they can plot with". The design:
  - **`sag.sandbox(thing, title=None, *, key=None, size=None, kind=None)`** in Python (standard library
    only; it duck-types what it is given): a matplotlib or seaborn figure (`savefig`, PNG), a Plotly
    figure (`to_json`), an Altair chart (`to_dict`, drawn as Vega-Lite), a graphviz `Digraph`/`Source`
    (`.source`), a PIL image, PNG/JPEG/SVG bytes or a file path, a DataFrame (a table card, first rows),
    Markdown text (a note), and Mermaid, DOT or ECharts text with `kind=`. Explicit, not every image:
    the script decides what is worth showing. **`key=`** names a card, so running a cell again
    replaces its card instead of adding another. Returns the card's id. `sag sandbox ...` in the
    terminal does the same for files.
  - **Cards not tied to a table:** a card from Python or the agent may belong to no grid ("Python").
  - **The agent's tools:** `sandbox_formats` (every card kind, its exact format, limits, a small
    example of each, and how sizing works: what it can build); `sandbox_add` for every kind, Markdown
    notes included (an image from a file path the agent saved, since the MCP server runs on the same
    computer); `sandbox_update`; `sandbox_layout` (sizes and order, to arrange a dashboard);
    `sandbox_list` describing each card (kind, title, size, the table it uses, a short summary of its
    content, never a whole image) so the agent understands what is on screen; `app_snapshot` of the
    Sandbox to see it.
  - **What data it can plot with:** the existing table tools (open tables, columns and types,
    profiles, `agent_query`) plus **data-bound figures**: a Plotly or ECharts card may name a table and
    a query; the server runs it (capped at 5,000 rows, as charts are) and fills the figure (ECharts'
    `dataset.source`, Plotly columns by name). Big data never passes through the agent, and the card
    follows the table's steps.
  - **Sizes on the server**, so a dashboard is the same in every browser; notes size like a text box.
  - **Saved dashboards:** the Sandbox is saved to disk as it changes and reopens when SwissArmyGrid
    starts; named dashboards save and open (`sandbox_save`, `sandbox_open`); once workspaces exist
    (plan phase 1), each workspace opens with its own. A card drawn from a table that is not open
    says so, and draws when the table is pushed again.
  - **Sandbox history and time travel** (the owner: "make the entire sandbox have a history where you
    can save snapshots of the sandbox at a period in time and you can time [travel]"): a snapshot is
    the whole Sandbox frozen, every card's size and place and the data it showed (a chart's computed
    points, a result table's rows, notes, figures and images as they were), so it opens later even
    when its tables are gone. Named snapshots on demand ("Save snapshot"), and one automatic snapshot
    a day (the day's first change); automatic ones trimmed to the last 60, named ones kept until
    deleted; files beside the saved dashboards. A timeline across the Sandbox steps back and forward
    read-only ("Viewing 10 November 2026, 09:14 · Back to now · Restore this"); Restore makes that
    moment the live Sandbox (cards reconnect to their tables when open, else keep their frozen
    picture) after snapshotting the current one first. Comparing two moments marks cards added,
    removed or changed. Agent: list, take and read snapshots; restoring is the user's click.

## Steel and Swiss army knife red (2026-11-13, the owner's request; confirm or change)

"Design the default theme in the app to be steel and Swiss army knife red." The light and dark themes
(and so "system", the default) are now brushed-steel greys with one red: `#c8102e` in light (5.9:1 on
white, white text on it 5.9:1), `#ff6b73` in dark (with dark text on red buttons).

- **Red is the brand and the main action:** the table's title, the open tab's top edge, primary and
  pressed buttons, the first chart series, and a thin marker line on the active row and column.
- **Steel is everything else:** surfaces, borders, the focus ring (dark steel in light, light steel in
  dark, so it never reads as an error), and where you are in the grid (steel fill, red line).
- **Errors keep a deeper red of their own** (`#a3141f`), always with words or an icon; danger buttons are
  outlined, primary ones solid.
- The tab bar has a slight brushed-steel gradient (`--steel-sheen`); the high contrast themes stay flat
  and unchanged. No cross, flag or other mark: colours only.
- **Open:** keep the old blue as a "Classic" theme in Settings if anyone misses it; the user guide's
  screenshots still show blue.

## Packs: blades and tangs from a Claude Code plugin marketplace (2026-11-13, the owner's idea; built)

"Use the Claude marketplace to source blades and tangs, so they could be stored in a GitHub repo and
collaborated with." From the Claude Code docs (code.claude.com/docs/en/plugins/: create-marketplace,
marketplace-reference, manifest-reference, install, loading, org):

- A marketplace is a repository with `.claude-plugin/marketplace.json` (name, owner, plugins); each
  plugin is copied as a unit into `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` on install
  (the root can be moved with `CLAUDE_CODE_PLUGIN_CACHE_DIR`), so folders Claude Code does not
  interpret, such as `blades/` and `tangs/`, travel with it. `claude plugin list --json` and
  `installed_plugins.json` give the install paths; `enabledPlugins` in the settings says which are on.
- Updates: `/plugin marketplace update` fetches the repository; with no `version` in the plugin, every
  new commit is an update (its version is the commit), with one, the string must change.
- Team-wide: a project's `.claude/settings.json` `extraKnownMarketplaces` makes the marketplace known;
  each person still installs once; only managed settings install automatically.

The design:
- **A pack** is a plugin with `blades/` and `tangs/` (and, if wanted, the SwissArmyGrid skill and MCP
  server). SwissArmyGrid finds installed, enabled packs and lists their blades and tangs with the pack
  and version; read-only (the installed copy is replaced on update): Save a copy into your own blades or
  a blade folder.
- **Collaboration is Git:** clone the marketplace repository, add the clone as a blade folder (already
  works), save blades and tangs there, commit, pull request, merge; teammates update.
- **`sag pack new`** scaffolds a marketplace repository (marketplace.json, one pack, README with the
  commands); **Add to pack** copies chosen blades and tangs into a pack folder. SwissArmyGrid's own
  repository may become a marketplace with an example pack on the made-up store.
- **Trust:** a pack blade is the marketplace maintainers' code, trusted as installing any plugin is;
  it runs only when used, and always shows its pack.
- **Built as designed**, and this repository is now a marketplace (`swissarmygrid`: the skill and the
  agent tools; `sample-store`: the example pack). Decisions made on the way: the pack routes and the
  Packs window are the user's (the agent cannot list or change packs); Add to pack writes only into a
  pack inside the blade folders, keeps existing files unless asked to replace, and New marketplace
  refuses a folder that is not empty; packs carry no `version`, so every commit is an update.
- **Open:** Claude Code's `installed_plugins.json` is not documented, so it is read loosely, with a
  scan of the cache as the fallback; check it again when Claude Code changes. A project's
  `extraKnownMarketplaces` could be offered from the Packs window so a team's repository suggests the
  marketplace by itself.

## Tangs and the relationship finder (2026-11-13, the owner's requests; confirm or change)

"The hardest part of my job is trying to determine how two tables relate to each other"; "a name for
relationships that are saved"; "a different file format ... so they're not confused with other json
files and you can run them in the application to make sure they work"; "if I discover a relationship
my coworkers have no way of knowing: share relationships in the same way we can share blades".

- **The name is the owner's: tangs.** A saved set of relationships is a tang (the part of a blade that
  anchors it into the handle). The `.tang` file is JSON that must start with
  `"swissarmygrid": "tang", "format": 1`, so no other JSON file is taken for one, and a newer format is
  refused by name.
- **Shared like blades, through the same folders:** the blade folders are searched for `.tang` files
  (four levels deep, hidden folders and environments skipped), so one team folder carries both. Save as
  tang writes to the person's own tangs or one of those folders, nowhere else. Older model JSON still
  reads.
- **Who and when:** a tang keeps its saved time and, if the person writes it, who saved it; never read
  from the computer. Each join keeps its evidence ("Found 2026-11-13: 91% of values in customers, 201
  orphans").
- **Running a tang** checks each join in the open tables (each dataset as pushed, before a grid's
  steps): holds, holds with orphans, broken, not open; a column's type change since saving is noted.
- **The finder's rules** (domain/relationships.py): the key side unique; 80% of the other side's values
  in it; names that point at the key; a key with under 50 values needs a name match; two counters from 1
  need coverage; floats and booleans are never keys; two-column keys searched among key-like columns
  only (a price is the most varied column of an order line); implied joins dropped; of two joins
  between the same tables, the first column's is active. Scored on the made-up store's answer key: all
  ten, no decoys.
- **Rejected suggestions** are remembered while the server runs, not across restarts.
- **Open:** keys of three columns found by containment alone; foreign keys read from a loaded catalog;
  the relationships view of plan phase 4 (draggable tables).

## Fill in your values (2026-11-12, the owner's request; confirm or change)

"Have blades auto detect ... the variables that are defined inside of the blades, so when somebody imports
a new blade it will suggest what variables to enter."

- **Read from the code, not declared in the header.** Any blade works, with no extra writing by its
  author; a header list would go stale the moment the code changed.
- **What counts:** connection keyword arguments (`host=`, `port=`, `dbname=`, `user=`, `account=`,
  `warehouse=`, `http_path=`, `vault_url=`, ...); the known parts of a connection string (Server,
  Database, UID, DSN, DBQ, Driver, Port, HOSTNAME...); top-level names given a literal or `Path(...)`; a
  path passed to `connect`, `read_csv` and the like. Never anything named like a password, token or key.
- **Placeholders:** `my-`/`my_`/`your-`, `example`, `<...>`, `changeme`, and a starter's own example path.
- **Suggestions:** the same setting in the user's other blades (most used first), then a short list of
  standard environment variables (`PGHOST`, `SNOWFLAKE_ACCOUNT`, `AWS_REGION`...), never a password one.
- **Writing back** replaces only that value's characters; a raw string stays raw; a backslash in a plain
  connection string is written doubled; a value that would break the code (a `;` in a connection string,
  a line break) is refused with a plain message.
- **Open:** suggestions from a loaded catalog's server name; settings for the agent's `blades_create`
  (it already lists them in the approval window).

## The agent makes blades (2026-11-12, the owner's request; confirm or change)

"Make it possible for the agents through MCP to create blades for you." Three commands:
`blades_list`, `blades_templates` and `blades_create`.

- **A proposal, saved on Allow.** It uses the same approval window as an agent export, now also on the
  blades page and the notebook tab. The window shows the blade's whole `.blade.py` file (the code
  included), what it imports that isn't installed, and that its code runs whenever the blade is used.
  Saving runs nothing. An unanswered request is declined after 3 minutes.
- **What the agent still can't do:** read an existing blade's code, catalog command or secret status
  (`blades_list` gives names, kinds, descriptions, tags and the `sag.blade` line); send a secret's value
  (refused; the code says `secret("password")`); choose a blade folder (ignored, so a blade lands in the
  app); replace, change, remove, refresh, test or run a blade. A taken name is refused before the user is
  asked.
- **Open:** letting the agent edit a blade it made (behind the same Allow) and offering "save into a blade
  folder" in the approval window.

## Portable blades (2026-11-12, the owner's requests; confirm or change)

"A way to import a blade ... a format to make them portable", "blade folders that the app could search",
"a format or frontmatter" to tell blades from other Python, "descriptions and tags", and "pin blades
to favorite".

- **The frontmatter is a comment block**, `# /// swissarmygrid-blade` ... `# ///` holding TOML, the
  same shape as Python's inline script metadata (PEP 723), so a blade file stays a runnable, lintable
  Python file and Git diffs read well. It must open the file: a header further down does not count,
  so a file that merely mentions the marker is never taken for a blade.
- **`.blade.py` is the suggested name, not the mark.** In a blade folder any `.py` file that starts with
  the header is a blade; the name is only for people.
- **Secrets travel as names only.** An imported blade asks for its values on its save screen.
- **Folder blades are the folder's.** Saving writes the file back (the header is rewritten, the code
  kept); the app never deletes one. A blade saved in the app wins a name clash with a folder blade,
  and the clash shows in import as "already exists".
- **Favourites are per computer** (`blade_pins.json`), never in the shared file, so one person's pins
  don't travel to a team folder.
- **Open:** a tag-only view (group the list by tag) and a "pull from Git" button for a folder are not
  built; the search and tag chips cover finding a blade for now.

## Privacy levels removed (2026-11-12, the owner's decision)

"Remove the privacy levels from the app." The agent now always sees everything, as level 3 gave.

- **Removed:** levels 0 to 3 and their picker in Settings ("What the agent may see"); column masks
  and the column-lineage tracer that existed only for them (server/masking.py, the context's
  `derived_columns`); the level-0 checks (server/level_zero.py, `ctx.agent_made`,
  `Grid.agent_steps`); step-value hiding (server/step_privacy.py); row-read approvals ("Run row
  reads without asking me"); engine error text hidden by level (the EngineErrorRedaction
  middleware); the PIVOT refusal; `min_level` in the command registry and the catalog's
  `available` flag; `/agent/policy` and the `agent_policy` command; `privacy_level` and `masked`
  in agent results.
- **Kept, since they are security rather than per-level privacy:** an agent export still waits for
  the user's click and is written only against the one-time ticket that click gives
  (`routes_agent.ExportTickets`); the approval popup and `/agent/approvals`; the token on every
  data route and the Host/Origin guard; `X-Actor: agent`; the agent activity list (the audit log);
  `<untrusted-data>` wrapping of values in MCP results; an agent never gets the token (URLs come
  back without it); every `/blades` route refuses the agent; the routes that were always the
  user's alone (approving, `/workspace/active`, the snapshot upload, the Key Finder's value
  samples, shutdown, the audit log).
- **Reads are capped at 1,048,576 rows** (one Excel sheet), as level 3 was.

## Agent privacy hardening (2026-11-12, superseded the same day: the levels were removed)

- **Approved agent exports are masked.** The file follows `/agent/read`: masks traced to the source
  columns, level 1 text masking, the level's per-read row cap (20 / 200), and level 0 exports only
  `scope=profile`. One click is one read's worth of rows, since the agent can read the file back.
- **Step values at level 0.** Every agent view of steps (tabs_get, view-state, context, the step
  commands' answers) shows the same redacted form (server/step_privacy.py). Chosen over refusing
  the steps_* commands at level 0: the server applies one change by index or id
  (`POST /grids/{id}/step-ops`), so the commands never read values back. A redacted list posted back
  unchanged keeps the user's values; a half-edited one is refused.
- **Level 0 SQL rule** (server/level_zero.py): a non-number column (text, date, boolean, list,
  struct, JSON, anything untraced) may only be counted, grouped by without being returned, tested
  with IS NULL, or passed through. Functions, casts, comparisons, sorts, set operations and
  PIVOT/UNPIVOT on it are refused in agent SQL, steps, table cards and charts. Number-ness follows
  the data: a number the user computed from text (`length(client)`) is not a number column.
- **Open, by choice:** sort steps on text are allowed at level 0 (they change only what the user
  sees; with a limit step they reveal row order, not values), though `ORDER BY text` in agent SQL
  is refused. `table_matching` switches change how the user's own hidden filter values match, so the
  row count can move. The Key Finder's "longest value is N characters" equals the profile's
  `longest`, which level 0 already shows. Key Finder and compare skip agent-made tables and grids
  with an agent SQL, bucket or categorize step, since they can hold values the agent chose.

## Next: grid tabs, split view and compare (to plan)

Asked for on 2026-10-22: grids open as tabs in one window instead of new browser windows (move,
close, reorder), dragging one tab onto another splits the view into two grids side by side (the
side panels follow whichever grid was clicked last), and a Compare group on the ribbon lines the
two grids up by primary keys for an anti join, an outer join or a cross compare. This changes
the page from one grid per page to several, so it gets its own plan before any code.

## Not built (yet)

- Milestone 9, the agent Python sandbox: the plan says ask first.
- The VS Code extension ("View in grid" on a file or a debugger variable) and the Jupyter
  display hook: the plan lists them as extras after M4.
- A SQL box query that joins another dataset can't be a step (see above); pinning it works,
  but the pinned grid's Python starts from the pinned result.
- Splitting an export across several Excel sheets: an export over the row limit is refused
  with a message instead.

## Known gaps in the first M1 slice (most now closed)

- JSON push loses types: dates arrive as ISO text and decimals as strings. The Arrow push format
  fixes this and should come next.
- No `gen` request numbers yet; AG Grid drops stale blocks on its own for now.
- The dataset (50) and grid (20) count limits, and the 500-character cell truncation, aren't
  enforced yet.
- No Content-Security-Policy header on the page. Worth adding; AG Grid's theming API injects
  style tags, so it needs a nonce or `style-src 'unsafe-inline'`.
- The bundle registers `AllCommunityModule` (1.1 MB). Register only the modules in use.

## Review notes on the plan itself

- Section 0 rule 7 and the M0 check say "section 12" for the verify list; it is section 17.
- The push client's size pre-check uses pandas `memory_usage(deep=True)`, which overstates text
  columns compared with the Arrow size the server measures, so it can refuse frames the server
  would accept. Measure with pyarrow when it's installed.
- The push client doesn't validate or escape the dataset name before putting it in the URL, uses
  a 10-second timeout that a large push can exceed, and reports a stale connection file (left by
  a crashed server) as a raw connection error.
- Four implementations per operation (engine + DuckDB, pandas and plain-Python generators) is a
  lot of work for M4 and M6. Consider shipping the DuckDB generator first and adding pandas only
  when someone needs it.

## The fit test and outliers (2026-11-13, the owner's request; built)

"A chi-square goodness of fit I can quickly use on a set of columns, and a quick outlier deviation
analysis where I can find the liars within a column, by frequency or amount."

- **Where:** the Column row (Fit test on the selected columns, Outliers on one), and the agent tools
  `column_fit_test` and `column_outliers`. Domain math and query text in domain/analysis.py; no new
  dependency (the chi-square p-value is the regularized incomplete gamma, tested against textbook
  critical values).
- **Fit test:** even, Benford first digit and first two digits (with Nigrini's mean absolute
  deviation ranges), another grid's column, the caller's shares, and two columns together
  (independence, Cramér's V). Every result gives an effect size in words beside p, because with
  many rows any difference is significant. More than 200 values: refused, with "bucket it first".
- **Outliers by amount:** robust (median and median absolute deviation, Iglewicz and Hoaglin's 3.5;
  falls back to the mean absolute deviation when over half the values are equal), Tukey's quartile
  fences (1.5), or z-scores (3); optionally within each group of another column (groups under 5
  values are not judged).
- **Outliers by frequency:** a value is flagged only when its count is unusual on two scales, and
  its score is the weaker: against the other values' counts on a log scale (natural popularity
  differs by a few times), and against counting noise (Anscombe's square-root transform, where chance
  is one unit wide). Neither alone worked: the log scale alone flagged 990 among values seen 1,000
  times; noise alone missed a typo seen twice among values seen 500 to 2,000 times. Checked on four
  made-up shapes in tests/test_analysis.py.
- **Show these rows** is a SQL step with the fences written as numbers (frozen when found), not a
  live subquery: a plain `SELECT * FROM grid WHERE ...` keeps the grid's row order, while a subquery
  would make the guard sort by every column. Pushing new data does not move the fences; run Outliers
  again for new ones.
- **Open:** a Sandbox card for a fit test's bars; a "Flag as a column" choice (a categorize step)
  beside Show these rows; outliers by date (gaps, a burst on one day).
