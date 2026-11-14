# Connections, the SQL cell and the Workbench

The owner's requests, 2026-11-13, in their words:

- "we need to go ahead and just branch connections from blades and just call them connections and
  include a app level overlay window for adding connections like the ones we are creating"
- "if the blade is a SQL connection, can we make it so it activates the jupyter SQL cell compatibility
  so you can just select that connection for the entire cell. like what the msofts sql server extension
  offers in vs code ... the same for workbooks and csv connectors ... and any other connection types
  that'll be worth it"
- "terminal and notebooks should be openable by clicking their tab in the main grid view ... merge
  them into one modal tab called workbench and you can open a python or a notebook, connection, data
  explorer, and blades panel on the left to drag thing in or create new blades ... with a place to save
  notebooks and open recent as well as python scripts in the terminal"

## Decisions (the owner's answers)

1. **Connections are databases and files.** A database blade (kind `sql`) and a file blade (a CSV,
   workbook, Parquet, JSON or folder, made from the file starters) are **connections**: listed, added
   and managed apart from blades. Vaults and secrets stay blades (a connection uses them for its
   password); analysis and other code blades stay blades.
2. **Adding a connection is a form per type, the code underneath.** An app-level window (the
   Application row's **Connections**): pick a type, fill in its fields (server, database, user; a file
   or folder), the password goes to the system keyring, **Test connection**, save. The Python it builds
   shows underneath, editable for the unusual case.
3. **The SQL cell.** `%%sql <connection>` runs the whole cell on that connection; `%sql_connect`
   chooses one for every later cell (like the SQL Server extension's connection picker).
4. **The Workbench is a tab that always opens in its own window** (the old rule 12 stands: code never
   sits in the grid page, so it can go on another screen). Its tab is in the grid page's tab bar,
   beside Sandbox and ERD; clicking it opens or focuses the Workbench window.
5. **An IPython kernel behind our own editor** (approved dependencies: `jupyter_client` in the server,
   `ipykernel` where the code runs). The kernel starts on the user's first Run, in a Python they
   choose (the server's own, or a project's environment). Notebooks are `.ipynb`, so Jupyter and VS
   Code open them too.
6. **Scripts are an editor and a Python console** (like Spyder or VS Code's interactive window): open,
   save and reopen `.py` files; Run sends the script or the selected lines to the kernel; the console
   below shows output and takes typed lines.

7. **The Workbench is a robust IDE** (the owner, 2026-11-14): completion as you type from the
   kernel (Python) and from the data explorer's catalog (SQL cells), help on hover.
8. **Connections keep every blade power:** a password from the keyring, an environment variable, or a
   key vault's entry (the vault blade runs first).
9. **Each notebook can have its own virtual environment,** made easily (where, the Python version,
   libraries by preset or group, imports added) and remembered for the notebook.

10. **Every connection is in every Workbench notebook and script by its name** (the owner's choice
    over a per-notebook checkbox, 2026-11-14): opened when a cell first uses the name, never written
    into the document.

## How it is built

**Connections share the blade store.** A connection is a blade the domain names a connection
(`is_connection`: kind `sql`, or a file starter's blade), so secrets, vaults, the catalog command,
Refresh, Test, folders, packs and the portable `.blade.py` file all work as they do. What changes is
where they are seen: `/connections` (the list, the types and their forms), the Application row's
Connections window, a Connections group in the Workbench's left panel, `sag.connection(name)` (the
same as `sag.blade(name)`), and the blades lists, which no longer show connections.

**The form per type** is the starter's code read by the settings reader (`domain/blades.py`: the
values each person fills in, written back with `/blades/fill`) plus its `secret("...")` names as
password fields. A path setting gets a file or folder picker. Test runs the unsaved code in a child
process with the typed password (nothing saved until Save).

**The SQL cell** (`client/sql_cell.py`, standard library only): what runs the SQL depends on what the
connection's code makes. A database connection runs it in its own SQL (any DB-API connection, DuckDB,
BigQuery, ClickHouse, Spark, SQLAlchemy). A file connection runs it in DuckDB in the user's Python: a
file is a table named after it, a workbook's sheets are tables, a folder's files are tables. A blade
that makes a DataFrame can be queried too. `%%sag` always works; `%%sql` only where no other extension
(JupySQL) has it. Tab completion in the cell is scoped to that connection.

**The Workbench** (`/workbench`, its own page):

- left panel: Connections, the data explorer (each connection's catalog), Blades; drag one into a cell
  or the script (its `sag.connection(...)` line, a table's `SELECT`, a blade's line), or create one;
- centre: tabs of open notebooks and scripts; a notebook is cells (code, SQL on a connection, markdown)
  run in the kernel; a script is an editor with the console under it;
- File: New notebook, New script, Open, Save, Save as, and **Recent** (the last files opened);
- the files live where the user saves them (default `~/SwissArmyGrid/notebooks`); recent paths are kept
  in the state folder (nothing else is written).

**Security.** The kernel runs only on the user's click, on their own computer, reached through the
server's token-guarded routes on 127.0.0.1. The agent cannot run code in it (milestone 9 stays unbuilt):
the kernel routes refuse the agent as the blade routes do.

**Completion** asks the kernel (`complete_request`, IPython with Jedi) for the cursor's line first, so
live objects answer (a DataFrame's columns), and the catalog search for a SQL cell's connection, kept
in the page. **Password sources** rename the secret in the code to the variable's name; a vault blade
mapping that name runs first, as for any blade. **Environments** are made with uv (else venv and pip),
ipykernel always; remembered per document in the state folder (a `.venv` beside it is found by itself);
a kernel in a Python without the package gets a copy of the plain client on its import path.

## Phases

1. Connections: the domain rule, `/connections`, the Connections window (form per type), the blade
   lists without connections, `sag.connection`.
2. The SQL cell on connections, its completion, docs.
3. The Workbench: the window and its tab, the kernel (start, run, interrupt, restart), notebooks and
   scripts (open, save, recent), the left panel with drag and drop.
