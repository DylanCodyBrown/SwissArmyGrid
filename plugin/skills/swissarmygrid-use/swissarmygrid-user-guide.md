# SwissArmyGrid user guide

SwissArmyGrid is a local data grid that runs on your own machine. You push a pandas DataFrame from Python, and it opens as a tab in a browser page where you can filter, sort, compare and profile the table without changing the source data. Every change you make is a step you can see, turn off or export as Python.

## Getting data in

Install it once with the installer in the repository's `installer/` folder (`install.ps1` on Windows, `install.sh` on macOS and Linux), or with `uv tool install "<wheel>[server]"`. After that, push a DataFrame from any script or notebook:

```python
import swissarmygrid as sag

sag.push(orders)
```

The grid is named after the variable, here `orders`. To choose the name yourself, pass `name=`. Names can be 1 to 64 letters, digits, `_` or `-`.

```python
sag.push(orders, name="orders", new_grid=True)
```

`new_grid=True` opens a new grid even when one with the same name is already open. Without it, pushing again refreshes the same grid, and its steps stay in place.

## The workspace

![The workspace: the tab bar, the title with its steps strip, the three ribbon rows, the grid and the status bar.](images/01-workspace.png)

The page has these parts:

- **Tab bar.** The Agent tab comes first and cannot be closed. Each open grid has its own tab after it, and the + after the last tab starts a new table from a query (see below).
- **Title.** The dataset name and its version. A new push to the same dataset raises the version. Beside it, the **steps strip**: how many steps the grid has, with Undo (the last step), Clear, Edit steps, Copy as SQL, Copy as Python and To notebook.
- **Application row.** Above the ribbon, on every tab: **Settings**, **Blades and catalog**, **Notebook** and **Tour** (the question mark): a one-minute tour of every word SwissArmyGrid uses, each shown on the page, then a quick game. On your first visit a **New here?** bubble sits beside it.
- **Ribbon.** Three rows, each named for what its buttons act on. **Table** works on the whole view. **Column** works on the selected column or columns. **Cell** works on the selected cells. Buttons that have nothing to act on are greyed out, and the caption under each row name says what it is looking at.
- **Grid.** The rows, with the column headers above them.
- **The grid has the whole page.** The steps themselves (Table Transformations) open in a tab of their own from Edit steps, and SQL is written in the notebook.
- **Status bar.** The row count, the selection summary and the memory meter.

### Settings

Open **Settings** from the Application row. It holds the settings for this browser (the theme, and whether column headers show their profile), the setup prompt for connecting an agent and the agent activity list described below.

![The Settings window: the header profile switch, the theme, the setup prompt with its copy button and the agent activity list.](images/02-preferences.png)

The "Connect an agent" box holds a prompt you can copy into Claude Code or another agent that has a shell. It tells that agent how to add the SwissArmyGrid MCP server.

## Working with a table

### Selecting cells, rows and columns

Selection works the way it does in a spreadsheet.

1. Drag across cells to select a block. The status bar shows the count, and the sum and average when the values are numbers.
2. Click a row number to select the row. Drag down the row numbers to select several rows. Hold Ctrl and click another row number to add it.
3. Click a column header to select the whole column. The Column row then names it in its caption.

![A block of cells selected across three columns, with the status bar showing the count, average and sum.](images/03-select-cells.png)

Ctrl+A selects everything, and Ctrl+C copies the selection. Click anywhere outside the grid, or press Escape, to clear the selection. The ribbon's buttons, menus and dialogs don't clear it, because they act on it.

Scrolling sideways is free: the view stops wherever you leave it, even in the middle of a column that is wider than the screen.

![A column selected from its header, with the Column row's tools now active.](images/04-select-column.png)

### Ribbon tools

The Table row holds the tools for the whole view: matching, Filter, Sort, Choose columns, Rename, Group by, Distinct rows, Keep first rows, Clean text, Auto width, Profile, Key Finder, Relationships, Export and the zoom control.

The Column row acts on the selected column. It has Sort A to Z and Z to A, Filter, Rename, Auto width, Remove, Keep only, Clean text, Bucket, Categorize, Group by, Profile, Fit test and Outliers. The Cell row keeps or excludes rows with the selected cell's value, shows the full value and copies.

### Filter steps

1. Click Filter in the Table row.
2. Choose a column, a condition and a value, then press Add step.
3. The steps strip counts it. **Edit steps** opens Table Transformations in a tab of its own, with every step in order and the row count before and after each.

![Table Transformations, in its own tab, with one filter step that keeps rows where region equals North (40 rows to 10).](images/05-filter-step.png)

Each step there has a checkbox to switch it off, a pencil to edit it and a trash icon to delete it; the grid follows at once. Turning a step off shows the grid without it, which is the quickest way to check what a step changed. **Undo** on the strip drops the last step.

### SQL: in the notebook

SQL is written where your Python is: the notebook tab (see The notebook). A grid's steps go there too. **To notebook** on the steps strip writes them as one marimo SQL cell over your table by its own name (`orders_view = mo.sql("""SELECT ... FROM orders WHERE ...""")`), runs it, and opens the notebook. A table that came from the notebook does this by itself: after each change its one SQL cell is rewritten and run, so the notebook is always the transformation, and running that cell gives the same rows as the grid.

**Copy as SQL** and **Copy as Python** on the strip give the same one query, or one line of DuckDB in Python, for any other Python. Both are run on a copy of your table and matched with the grid before they're copied.

A SQL step is still one step among the others: in Edit steps, **Add a SQL step** takes one SELECT over the table entering it, read as `grid` or by the table's name. Anything else is refused, and the strip names the broken step.

### A new table from a query

The + after the last tab opens a new tab with a query tray over a blank grid. Use it to build a table of your own from the tables you have open, such as a join of two of them.

1. Click + after the last tab. The new tab is named query_1, or the next free number. Type another name in the tray if you want one.
2. Write one SELECT. A table name reads that table as its tab shows it, after its steps, so a filter you added there applies here too. If a table is open in two tabs, its name reads the first one.
3. Press the green Run button, or Ctrl+Enter. The result becomes the table in this tab, with the ribbon, the steps strip and everything else a pushed table has.

The tray stays at the top of the tab. Change the query and press Run again to rebuild the table in place; the steps you added to it then apply to the new rows. The arrow at the left of the tray folds it down to one row. The table doesn't update by itself when the tables it reads change: press Run again for that.

### Profiles

Click a column header, then Profile in the Column row, to open a popup for that column. It shows the row count, blanks, distinct values, the shortest and longest values, and the most common values. Click a value to add a filter step for it.

![The profile popup for the product column: row count, blanks, distinct count and the most common values.](images/07-column-profile.png)

Profile in the Table row profiles every column at once, with the type, blank counts, distinct counts and the minimum value of each. Export profile (CSV) saves that table.

![The whole-view profile: one row per column with its type, blanks, distinct count and minimum.](images/08-table-profile.png)

### Fit test

Select one or more columns, then **Fit test** in the Column row. It asks: could the difference between what a column holds and what you expect be chance? (Pearson's chi-square goodness of fit.) Pick what you expect:

- **Even:** every value as likely (regions, weekdays, shifts).
- **First digit** or **first two digits (Benford's law):** in natural amounts (invoices, expenses, populations) about 30% start with 1 and under 5% with 9. Numbers someone made up rarely do. The result also gives Nigrini's conformity rating, from close conformity to nonconformity.
- **Another grid's spread:** this month's values against last month's, or a sample against the whole.
- **Together** (two columns): are they related? Region and late delivery, say.

Each column gets a verdict in words, the chi-square number, the p-value and an effect size. With many rows even a tiny difference is "certain", so read the effect (negligible, small, medium, large) to see whether it matters. The bars show each value's count, the tick the expected count, and values far off are shaded. A column with more than 200 different values needs bucketing first (or the Benford test, for amounts).

### Outliers

Select one column, then **Outliers** in the Column row:

- **Amount** (number columns): values far from the rest. **Robust** (the default) measures distance from the median, so a few wild values cannot drag the average along and hide themselves. **Quartile fences** is Tukey's box-plot rule; **mean and standard deviation** is the classic z-score. **How far out** sets the line (3.5, 1.5 and 3 by default). **Within each** judges every vendor, store or month on its own, so a normal amount for one is not hidden by another.
- **Frequency** (any column): values seen far more often or far more rarely than values usually are. A rare one is often a misspelling (Nrth among thousands of Norths); a common one, a copy or a default (the same amount forty times). Ordinary chance (990 against 1,000) is not flagged.

**Show these rows** adds a step that keeps just the outliers, with the fences written in it as numbers, so the grid shows them in their usual order; **Exclude these** adds one that leaves them out (rows with a blank stay). Undo either like any step. An outlier is a question, not proof: look at the rows before you decide.

### The Analyze menu

**Analyze** in the Table row opens analyses that hand off to each other, so you can go from "what is in this column" to "what predicts this metric":

- **Odds:** pick a column. Each value's share of the rows, "1 in N", and its odds on any one row. Add an **outcome** (late, a refund, an amount above a line) and each value gets its rate of it against every other value and an **odds ratio** with its 95% range: "West: late 46% of the time, against 15% elsewhere: 4.85 times the odds (4.16 to 5.65)". Rows whose range leaves out 1 are shaded: those values really differ. A star marks values with under 30 rows (rough). Number columns are cut into equal-sized ranges. **Test together** hands the column and the outcome to the fit test.
- **Find predictors:** pick a target (a number or a category). Every other column (or the ones you tick) is tested together with it by chi-square, numbers in equal-sized ranges, and ranked by strength (Cramér's V: none, weak, moderate, strong). Number pairs also get their straight-line correlation. Identifiers and dates are skipped with the reason, and two columns that say the same thing are marked, so a regression gets only one. **Test together** on a row opens the fit test for that pair.
- **Fit test** and **Outliers**, as above.

- **Regression:** a target and its predictors (Find predictors' **Regression with these** fills them in). A number target gets a linear regression: how much it moves with each predictor, the others held steady, with a 95% range, how much of it the model explains (R-squared) and its typical miss. A yes/no target (tick Yes/no and say what counts as yes: late, or above 1000) gets a logistic one, whose effects are odds ratios. Categories are compared with their commonest value. **What if** predicts for values you type, with the range a new row would fall in. **Add predicted column** and **Add miss column** add steps with the fitted formula; **Check the misses** adds the miss column and opens Outliers on it: the rows the model cannot explain. A regression shows what goes together, not what causes what.

- **Time series:** a date column, a metric and how to total it per period (sum, average, or a count of rows; day, week, month, quarter or year), optionally one line per value of a category. In words: the trend ("went from about 1,076 to 1,976 a month"), the season ("December runs 31% above a typical month"), the periods that stand out (a spike or a dip against their neighbours, with **Show these rows**), where the level stepped and stayed, and how much one period carries into the next. The chart marks each. **Forecast…** carries the same choices over.
- **Forecast:** how many periods ahead, and optional predictors (their average per period), each with its future: its last value, its own trend, or a value you give. The trend, the season and any step in the level go forward, with an 80% and a 95% range. It first holds back the last periods and forecasts them from the rest, and says how far off it was: the miss it earns, not one it fits. A forecast is a range: it assumes the past pattern goes on.

Each form has a **Data shape** panel: a made-up table in the shape the analysis needs (long: one row per order, the category and the outcome as columns), one that does not work and why (already counted, wide with one column per month, numbers stored as text), tips, and a check of your grid ("region: 4 values: fine", "this table looks wide"). It can go to the Sandbox too.

Every result has three buttons: **Send to Sandbox** (the result in words, as a note), **Open as grid** (the result's table as a new grid) and **To notebook** (the analysis as Python in the notebook: the grid's steps as checked SQL, then one `swissarmygrid.stats` call, the same code the window ran, so the numbers match).

### Auto width

Auto width in the Table row fits every column to its header and the rows on screen. The same button in the Column row fits only the selected columns.

![Columns fitted to their headers and the visible values after Auto width.](images/09-auto-width.png)

### Value viewer

Double-click a cell that holds a long text or a JSON value to open it in the value viewer. A JSON object shows as a table of keys and values, and the JSON tab shows the raw text. Long text shows its length and wraps.

![The value viewer showing a JSON object as a key and value table.](images/10-value-viewer-json.png)

![The value viewer showing a long text value with its character count.](images/11-value-viewer-text.png)

### Export and zoom

Export in the Table row writes the grid as CSV, text, Excel or Parquet. The file is built from the grid's steps, so it matches what you see. Choose a download or the export folder.

The zoom control in the Table row changes the text and row size of the grid. Ctrl+= and Ctrl+- step the zoom, and Ctrl+0 resets it to 100%.

## Comparing two tables

Two grids can sit side by side, and they can be lined up row for row on a key.

1. Drag one grid's tab onto the middle of another grid's tab. The two grids show side by side, and the key picker opens.
2. The picker lists the column pairs that share values, best first. Tick the pair that identifies the same row in both tables. To use a key made of two columns, tick both pairs.
3. If no pair fits, choose the columns in the "Or pick a pair yourself" row and press Add.
4. Press Compare.

![The key picker with the order_id pair ticked for orders and orders_v2.](images/13-split-key-picker.png)

The Key Finder runs when the picker opens. It ranks the pairs by the share of values that match on each side. Read the list before you trust the first row. In this sample, order_date ranked above order_id, because its values also match on both sides. Tick the key you mean. When the two tables have a known relationship (see Relationships below), its columns come first with a crown, already ticked.

The Key Finder also runs on its own from the Table row. It opens in a window of its own, titled "Possible Join Keys for" your table, so you can keep it beside the grids. Choose the table to compare with and press Find keys. Each row shows your column, the other table and its column, the relationship it looks like, the share of each table's values found in the other (headed by the table names) and the shared values as fractions. Click a row to see sample values that match and values that do not. Known relationships carry a crown.

Above the pairs, every run also shows the most distinct column combination in your table: the fewest columns that together tell every row apart, which is usually the table's primary key. It reads the first 400,000 rows of the grid as it stands after its steps. The search starts with the column that has the most distinct values, then adds whichever column tells the most remaining rows apart, one at a time. Each of those rows shows the share of rows the combination tells apart. The last row, marked with an accent edge, is the answer. Any other combination just as small that is also unique per row is listed under it. If no combination tells every row apart, the table repeats whole rows, and the answer says how many. The search adds one column at a time, so it finds a small key quickly, but a different key of the same size can exist. Check one you mean with a count in the notebook.

![The Key Finder window comparing orders with orders_v2, with the column pairs ranked by the share found on each side.](images/12-key-finder.png)

### Reading the aligned view

Once compared, the two grids always line up. Row N on the left and row N on the right share a key. When a side has no row for a key, a striped blank row sits opposite the row that has it. The two grids scroll together through one scrollbar in the centre.

![The two grids aligned on order_id. The striped row on orders_v2 is order 1005, which exists only in orders. The ribbon reports 38 matched, 2 only in orders and 2 only in orders_v2.](images/14-compare-aligned.png)

Cells that differ between the two sides are marked with a coloured bar. Click any row to highlight every row with the same key on both sides, so you can see the same order on the left and the right at once.

![A key group highlighted after a click. Order 1003 shows 129 on the left and 92 on the right, and the cells that differ are marked.](images/15-compare-key-group.png)

### Compare modes

The Compare row in the ribbon switches the mode.

- **Outer join** shows every key from both tables, as above.
- **Anti join** shows only the keys with no match on the other side. Choose both sides, only the left table or only the right table.
- **Cross compare** keeps each grid on its own rows. Click a row on one side to filter the other side to that key.
- **Tie-out…** answers "do these two tables add up the same?". Tick the columns to group by (a shared text column with few values, such as region, starts ticked) and the totals (every shared number column), and how far apart still counts as equal (an amount, or a percentage of the left total). The report has one row per group: rows on each side, each total on each side, the difference and its percentage, and **ties** or **off**. Groups that are off come first, the largest first, and a grand total ends it; a group on one side only is off by its whole total. Columns match by name ignoring letter case, and values meet the way compare keys do (" North" meets "North", 42 meets 42.0). Each side is read as its grid shows it, after its steps. **Click a group** to filter both grids to its rows. **Send to Sandbox** keeps it as a note and a table; **Download CSV** saves it. A grand total that ties can still hide groups whose differences cancel out: the words say so.
- The ribbon's counts (matched, only on one side) follow each grid's steps.
- **Stop compare** returns to two independent grids. The side-by-side view stays, with no keys.

## The Sandbox tab

The Sandbox tab is the first tab. It is one dashboard for every table, where charts, notes, result tables, diagrams (Mermaid and Graphviz), Plotly, ECharts and Vega-Lite figures and images live. Each card names the table it is drawn on; a card that belongs to no table (a note added here, or anything from Python) says where it came from, and stays when grids close.

**Resize a card** by dragging the grip in its bottom right corner: it snaps to the dashboard's twelve columns and rows, and the chart inside redraws to fill it. With the grip focused, the arrow keys do the same (left and right for width, up and down for height); a double-click returns the card's usual size. Sizes are kept in this browser.

To add a chart, press Add chart. Choose the table, the chart type, the column for the X axis, the aggregate (such as sum) and the column for the Y axis. To add a note, press Add note, give it a title and write its text in Markdown.

![The Sandbox with a bar chart of summed quantity by product and a note with a title and a short list.](images/16-analysis-sandbox.png)

Clicking a bar in a chart filters the grid to that value. Each card has a pin, a zoom button for a larger view, and a remove button.

### The Sandbox as a report

- **Move a card** by dragging its title bar onto another card (the left half puts it before, the right half after), or with Move earlier and Move later in its **⋯** menu.
- **Notes** are markdown: headings, **tables** (`| Region | Late |` rows under a `|---|---:|` rule; numbers line up right), lists, `code`, and `>` cautions. Double-click a note (or Edit note in its menu) to change it, an analysis's note too: add your own reading of the result.
- **Page view** shows the Sandbox as **letter** or **letter landscape** pages, edges drawn, cards in their order; a card never splits across pages, and **Start a new page here** (in a card's menu) begins a page with it. Notes grow on paper so no text is cut.
- **Print to PDF** prints exactly those pages: choose Save as PDF in the print dialog. A dark theme prints on light paper.
- **Export markdown** saves the whole Sandbox as one `.md` file: every note as its markdown, tables as markdown tables, every chart and diagram as a picture inside the file. Ready to keep, or to hand to an AI model to help read it.

### Saved dashboards

**Dashboards…** in the Sandbox's toolbar saves the Sandbox under a name: every card as it is drawn now, its size and place, a chart's points and a query table's rows included, so the dashboard opens even when its tables are gone. Saving again under the same name adds a version (the last 60 are kept); **History** lists them, newest first, with their cards, and **Open this version** brings one back. Opening replaces the cards on the Sandbox (it asks first). A chart or query whose table is open draws live on it; one whose table isn't shows what it showed when saved ("As saved") and turns live by itself once that table is pushed again. Nothing is saved unless you press Save; dashboards live in your SwissArmyGrid folder (`~/.swissarmygrid/dashboards`). An agent can list a dashboard's versions, never save, open or delete one.

### Sending things to the Sandbox

Anything that shows details can go to the Sandbox, as a readable note and, where there is one, a chart beside it:

- **Every Analyze result** (odds, fit test, find predictors, outliers, regression): **Send to Sandbox**.
- **Right-click a cell**: Send value to Sandbox, Send the row to Sandbox (each column and its value), Keep these, Exclude these, View value, Copy.
- **Right-click a column header**: Profile to Sandbox (its numbers, most common values and a histogram), Distinct values to Sandbox, Profile, and the Analyze entries for that column.
- **Send to Sandbox** also sits on the column profile, the whole-table profile, the value viewer and the Key Finder.

### From Python: sag.sandbox

`sag.sandbox(thing, "Title")` puts anything visual from your own Python in the Sandbox:

```python
sag.sandbox(fig)  # matplotlib or seaborn: a picture
sag.sandbox(plotly_fig, "Sales by month")  # Plotly: drawn live, in the theme
sag.sandbox(chart)  # Altair: drawn as Vega-Lite
sag.sandbox(dot)  # a graphviz Digraph
sag.sandbox(frame, "Top stores")  # a DataFrame or Arrow table: its first 100 rows
sag.sandbox("## Findings\n- North leads")  # Markdown: a note
sag.sandbox("chart.png")  # a file: a picture, .md, .mmd, .dot
sag.sandbox(text, kind="mermaid")  # Mermaid, DOT or ECharts text
sag.sandbox(fig, key="monthly", size=(8, 10))
```

`key=` names the card, so running the cell again replaces it where it is, at the size you gave it, instead of adding another. `size=(w, h)` is 2 to 12 columns by 3 to 30 rows. Nothing is drawn automatically: the script decides what is worth showing.

### How an agent sees the grids

An agent works on your tables through the SwissArmyGrid MCP server, which has one tool for each thing the grid can do (anything you can do with a button, the agent can do with a command, named for the part of the app it belongs to (`table_`, `column_`, `cell_`, `steps_`, `compare_`...), except the controls that are yours alone, such as approving an export). The plugin provides it, the setup prompt in Settings installs it in other agents, and the agent workspace has it already. The agent sees which table you have in front of you without asking: the page tells the grid server which tab is active, and each prompt carries a short summary of that table (its name, row count, columns and steps, never its values). An agent can also ask for it with the `current_table` tool.

## Relationships

Relationships, on the Table row, holds each table's primary key and the joins between tables. Once they are set:

- Primary-key columns carry a key mark in their header.
- Splitting two tables that have a known join pre-ticks its columns in the key picker, with a crown.
- The Key Finder marks known pairs with a crown.

**Find relationships** works out how the open tables join from their data. For each join it shows the tables and columns, its kind, a confidence bar and the evidence: the share of values that match, the orphans (values with no row on the other side), and why it thinks so. It handles names that don't match (`orders.customer_id` to `customers.cust_key`), a text column holding numbers, keys made of two columns, and two joins between the same tables (only one can be active; the second is marked inactive). Small number columns that happen to overlap, such as status codes and months, are not suggested. The confident joins are ticked: press **Accept the ticked joins**, or reject one so it isn't suggested again.

**Run against the open tables** checks every join in use: each one **holds**, **holds with orphans** (with a few of the orphan values), is **broken** (a column has gone, the key side repeats, or nothing matches) or is **not open**.

### Tangs: sharing relationships

A saved set of relationships is a **tang** (the part of a blade that anchors it into the handle, as a join anchors two tables). **Save as tang** keeps the joins in use, with a name, description, tags and, if you like, your name, as one `.tang` file:

- **In your tangs**, on this computer, or
- **In a shared folder**: the blade folders (a team share or a repository, set with Blade folders on the blades page). A tang saved there is in your coworkers' list the next time they open this window, marked **Shared**, with who saved it and when.

The list at the top shows every tang found, searchable by name, description, tag or author, with how many of its tables you have open. **Apply** uses one (merged into what you use now; the toast says if it changed a join you had). **Run** checks a tang against your tables before you trust it. **Import a tang** opens a `.tang` file: you see what it holds and what it would change before applying it, and can keep it in your tangs or share it. **Export** downloads the joins in use as a `.tang` file. Only `.tang` files are tangs: ordinary JSON files in those folders are left alone. Older model files (`~/.swissarmygrid/models`, `SWISSARMYGRID_MODELS`) still work.

Tangs also come in packs (see Blades and the catalog): an installed pack's tangs are listed marked **Pack**, and **Add to pack** shares yours through the team's repository.

Draw ERD adds a copy of the diagram to the Sandbox; the ERD tab always shows the joins in use. Tables are matched by the names you pushed them with, so push with the names the tang uses. An agent can find, run and explain joins for you; saving and sharing tangs is yours.

## The ERD tab

The ERD tab sits right after the Sandbox tab and can't be closed. It draws the keys and joins between the open tables as an entity relationship diagram: one box per table holding only its key columns (PK for a primary key, FK for a column a join uses), with crow's-foot lines showing one-to-many and one-to-one joins. A line's label is the join's note, such as an outlier or a caveat about how it matches; joins without one carry no text. In a star schema the central table is drawn in the middle, with its spokes above and below it.

![The ERD tab with no relationships yet, offering to import a file or apply a model.](images/18-erd-empty.png)

With no relationships for the open tables it says so and offers these ways in:

1. **Import a relationship file**: a model JSON file, such as one a colleague exported.
2. **Apply a shared model**: when a model in the shared library fits the open tables, it is listed with an Apply button.

![The ERD for orders, orders_v2 and customers, with the keys and the crow's-foot joins between them.](images/19-erd.png)

## What the agent may see

A connected agent sees everything the page shows: every table, its rows and columns (up to 1,048,576 rows per read, one Excel sheet), its steps and their values. An export by the agent always waits for your click: a window asks you to Allow or Deny it, and closing the window denies it.

Settings lists the agent's activity: what it asked for, newest first. It never includes cell values.

## Blades and the catalog

**Blades and catalog** in the Application row opens the blades page in a tab of its own, so it can sit on another screen.

- **A blade** is any Python that reaches outside Python to load something: a database connection, a CSV file, an Excel workbook, an API, a vault. Press **New blade**, pick a starter (21 databases, 5 file patterns, 8 vaults) or paste your own code, and save it. Then in any Python: `conn = sag.blade("Warehouse")`.
- **Passwords** go in the code as `secret("password")`. Type the value on the save screen: it is kept in Windows Credential Manager (or macOS Keychain), never in the blade.
- **Vault blades** read secrets from Azure Key Vault, AWS, Google, HashiCorp Vault, 1Password, a `.env` file or the operating system's vault, and set each one as an environment variable. **Secret blades** keep only variable names: you type the values once per session.
- **Packages:** the save screen lists the packages the code needs. **Install** adds missing ones to SwissArmyGrid; the copy button gives the command for your own Python.
- **The catalog:** tick **Add to the catalog** and give a query that lists the source's columns (the starters have one). **Refresh** (the play button) loads it: every schema, table and column with its type, as a tree you can search (`sales.orders` finds schema and table). Click a table for its columns and a first SELECT. Drag a blade into a script for its `sag.blade` line, or a table or column for its quoted name.
- **Find a blade:** the search box above the blades matches names, descriptions and tags, and the tag chips under it narrow the list. Give a blade a description and tags on its save screen (tag it with the app it serves, and searching for that app finds it). The star pins a favourite to the top.
- **Share a blade:** **Export** on its save screen downloads it as one `.blade.py` file (its code under a short header; passwords never go in it). The download arrow above the blades exports them all as a zip. To bring blades in, press the up arrow or drop the files on the blades: you see each blade's code first, and choose what happens when a name is taken. Importing runs nothing.
- **Fill in your values:** a blade's server, database, user and file path show as fields under **Settings** on its save screen and under **Fill in your values** when you import one. Example values are marked **needs your value**, and the values your other blades use are offered as buttons. A change is written straight into the blade's code.
- **Blade folders:** the folder button lists folders to search for `.blade.py` files (a team's Git repository, say). Only files that start with the blade header count. A blade from a folder shows **file**; saving it writes its file.
- **Packs:** a team can keep blades and tangs in a GitHub repository that is a Claude Code plugin marketplace. Install a pack once in Claude Code (`/plugin marketplace add <owner>/<repository>`, then `/plugin install <pack>@<marketplace>`) and its blades show here marked **pack**. They are read-only, since Claude Code replaces them on every update (`/plugin marketplace update`): **Save a copy** to change one; **Test** and **Refresh** work as they are. **Packs** above the blades lists what is installed, makes a **New marketplace** (also `sag pack new <folder>` in a terminal) and, for a clone of the repository in your blade folders, **Add to pack** copies your blades and tangs in, ready to commit.
- Your agent can read the catalog to write SQL against the source, and **make a blade for you**: a window shows you the blade's whole file, and it is saved only if you press **Allow**. It never sees your blades' code or any password; you type passwords on the blade's save screen.

![The blades page: three blades (a vault, a SQL connection and a secret blade) above the catalog tree, opened at sales.orders.customer_id, with the table's columns and a first SELECT on the right.](images/20-catalog.png)

![The save screen for a new PostgreSQL blade: its code, the variable it makes, its password field (kept in the computer's vault) and the missing psycopg package with an Install button.](images/21-blade-save.png)

## The notebook

**Notebook** in the Application row opens the notebook (marimo) in a tab of its own, with your blades and the catalog on the left. It starts the first time you open it.

- **Drag a blade** onto a cell to get its `sag.blade(...)` line. An empty cell takes it; otherwise it goes in a new cell just after.
- **Drag a table** from the catalog to get a SQL cell over that blade's connection, ready to run. **Drag a column** for its name.
- If a drop needs an import the notebook doesn't have yet, it's added in a new cell at the top and run.
- **Its tables are grids:** a table a cell makes shows in the grid page's Open list. Open it, and running the cell again refreshes the grid.
- **Save as blade:** highlight code in a cell and press **Save as blade**. The save screen opens with that code; a database driver in it makes it a SQL connection with the right dialect.

![The notebook: blades and the catalog on the left, the marimo notebook on the right with a blade's line dropped into a cell.](images/22-notebook.png)

## Limits

- All open tables together can hold 3 GB (never more than half of your computer's memory), and one table up to 1 GB, enough for a full Excel sheet of 1,048,576 rows on most tables.
- A query in the + tab shows up to 1,048,576 rows.
- Up to 50 tables and 50 grid tabs can be open at once.

## Tips

- Press Filter or Keep these first, then compare or chart. The step list keeps each change visible.
- Turn a step off with its checkbox to check what it did, then turn it back on.
- Read the Key Finder list before you pick a key. The top row is the best match, not always the column you mean.
- Use Pin as new grid for a SQL result you want to keep as its own table.
- The status bar shows the sum and average of the selected numbers, so a quick total needs no step.
