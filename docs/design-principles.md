# Design principles

These are the rules the owner has set while building SwissArmyGrid, written down so that every new
screen and feature follows them. They are for a designer, a coding agent, or a reviewer. Each rule is
short; under it are why, an example from the app, and the owner's own words where the rule came from
them (with the date the owner said it). Quotes are exact, typing slips included; a word in square
brackets is a clarification.

Where a rule here and an older document disagree, this document and `docs/ideas.md` win; the last
section lists what changed and what is still open.

## 1. What the app is for

**1.1 One screen's worth of work instead of three.** Every feature should cut the panning between the
code, the SQL and the data.
- Why: this is the reason the app exists.
- For example: the grid, the Workbench and the Sandbox each sit on their own screen and stay in step
  through the server, so nobody has to switch between three tools.
- The owner (2026-11-12): "I created SAG to make python to duckdb work easier instead of having to pan
  3 screens to work"

**1.2 Python analyses; DuckDB SQL transforms.** A transformation is always DuckDB SQL over the user's
own table, never a pandas translation of it.
- Why: the code the user copies must be the transformation itself and must run unchanged.
- For example: Copy as SQL gives one query; Copy as Python gives `duckdb.sql("""...""").df()` over the
  table by its own name, checked against the grid before it is copied.
- The owner (2026-11-12): "let SAG have the features that make python powerful like analysis and speed
  of data, and let duck db and sql do the transforming"

**1.3 Code that leaves the app must work where it lands.** Copied code is verified exactly as pasted,
on the user's variable, with nothing extra to define.
- The owner (2026-11-12), on the old copies: "never was the actual transformation script that would
  work to transform the source tbale for the grid back in the ENV"

## 2. Clicks and actions

**2.1 Three actions or fewer.** A task that takes more than three clicks or actions, and could take
fewer, is a defect.
- Why: the app is used all day by an analyst; every extra step is paid many times.
- For example: the Key Finder runs as soon as its window opens instead of waiting for a click; the empty
  Diagram tab's Find relationships opens the finder already running; a picture in the Workbench goes to
  the Sandbox with one click.
- The owner (2026-11-14): "if it takes more than 3 clicks or actions and can be reduced then it should
  considered as an improvement"

**2.2 One button for the common case.** When the app can work out what the user means, it does the
work and lets the user change it after.
- For example: the tie-out report lines two tables up and suggests the totals; blade settings are read
  from the code and offered as one-click suggestions; a vault lists its secret names by itself.
- The owner (2026-11-13): "diff columns over top of each other simply by pulling two tables side by
  side and pushing a button"

**2.3 Drag, right-click and keys are shortcuts, not the only way.** Offer the direct gesture where the
thing is (drag a connection into a cell, right-click a cell to send it), and keep a button or palette
command for the same action.
- For example: Script as is on the catalog's right-click menu; every Workbench action is also in the
  command palette (Ctrl+Shift+P).
- The owner (2026-11-13): "you can even right click an individual cell and send that value to the
  sandbox"

**2.4 Make the hard part easy, not just possible.** Where a setup is painful (an environment, a
missing import, a password), give it a form with choices, not a page of instructions.
- For example: a notebook's own environment is made from library presets in a dialog and remembered;
  the error panel's fix adds the missing import or installs the package.
- The owner (2026-11-13): "Our workbench should remember the virtual environment and make it super easy
  to create them. Like a set of combo boxes to import common libraries."

## 3. Placement

**3.1 A control sits beside the thing it controls.** If a feature takes extra steps because of where
it lives, it is in the wrong place.
- For example: the ribbon's Column row acts on the selected columns and its caption names them; the
  steps strip sits under the grid's title; a card's actions sit on the card.
- The owner (2026-11-14): "relative location to features is a thing. If it takes extra steps or a
  feature isn't conveniently located beside the thing that it's controlling or to features work better
  side by side on the screen then it should be considered as well"

**3.2 Features used together sit side by side.** A new mode of an existing tool goes next to that
tool, not in a new place.
- For example: compare's diff mode sits beside cross compare; Tie-out is in the Compare row; Fit test
  and Outliers are in the Column row and in the Analyze menu.
- The owner (2026-11-13): "We could simply add another mode right beside cross compared"

**3.3 Rows are named for what they act on.** The ribbon has an Application row (app-wide), then
Table, Column and Cell, and Compare only while two grids are side by side.
- Why: it is always clear what a button will change.
- For example: Settings, Connections, Blades and catalog, Tour and Zoom are in the Application row
  above Table, on every tab; the Analyze menu is in the Table row.
- The owner (2026-11-13): "Just put all those app level commands in another row above tables call
  Application"; "I'm thinking these analysis can live in a drop-down combo box that lives in the same
  bar as the table row of tools"

**3.4 Each kind of thing has one home.** Pictures and charts gather in the Sandbox; code lives in the
Workbench; connections in the Connections window; a second place for the same thing is removed.
- For example: the Workbench lost its bottom-right picture pane; its pictures have Send to Sandbox.
- The owner (2026-11-14): "The bottom right pane isn't needed it goes to the sandbox."

**3.5 Panes stay in the same places.** In the Workbench: the source, its console under it, Variables
on the right, the side bar (connections, blades, the data explorer) on the left.
- The owner (2026-11-14): "RStudios ide might be closer to what we are after though so a good blend of
  the two. The three panel design with variables in r studio is nice"

**3.6 One line per row.** A toolbar row, the status bar and a step form's fields never wrap into a
stack; when space runs out, the last buttons fold to icons.
- Why: a row that wraps moves everything under it.
- For example: the browser tests check that the status bar keeps its height when a cell is selected
  and that a step form's fields share a row.
- The owner (2026-11-12): "Why is the table header on the same line as other stuff"

## 4. Windows: what lives where

**4.1 Code always opens in its own browser window, never in the grid page.** The Workbench tab opens
or focuses that window.
- Why: so the code can sit on another screen beside the grids.
- The owner (2026-11-12): "Here is a rule. The terminal should be in its own browser tab always it is
  not in the same page as the grids / So it can be on a separate screen"; asked again (2026-11-13):
  "Its own window, always"

**4.2 The grid page is for the grid and its analysis.** No SQL box and no steps panel on it; the grid
has the whole page. The steps open in their own window (Edit steps).

**4.3 What lives where.**

| Place | Holds |
|---|---|
| Grid page | the tab bar (Sandbox, Diagram, Workbench, one tab per grid, the + query tab), the Application row, the ribbon, the steps strip, the grid, the status bar |
| Workbench window | notebooks, scripts, their consoles, Variables, the side bar; the error panel |
| Sandbox tab | the dashboard of resizable cards: charts, notes, tables, diagrams, pictures, analysis results |
| Blades page | blades, catalogs and packs, with the save screen |
| Dialogs and windows | a form per task: a step, an analysis, export, compare, the Key Finder, relationships, Connections, Settings |

**4.4 A dialog is one task.** Each analysis has its own form; each connection type has its own form
with its code underneath.
- The owner (2026-11-13): "Of course each analysis will need its own form to choose columns and input
  data from the grid"

## 5. Let the design speak for itself

**5.1 No help paragraphs, how-to hints or explanations on the screen.** Labels, headings, counts and
the layout carry the meaning.
- For example: empty states are a few words ("No connections", "Nothing open"); placeholders are one or
  two words; the ribbon shows what is selected and no longer tells the user to select something.
- The owner (2026-11-14): "You need to thoroughly rid the app of over explanatory text and instructions
  that are in the gui. Let the design speak for itself"

**5.2 Tooltips are short and add something.** A tooltip may hold a keyboard shortcut ("Run file (F5)")
or the reason a button is disabled; one that repeats the button's name goes.

**5.3 What stays, and why.** Text stays when the user must act on it or asked for it:
- errors and warnings, shortened but never removed;
- questions about losing work, and the agent's approval questions;
- **the tour**: the owner asked for it, behind the question mark, one card per word, then a game;
- **Show the math**: a step-by-step proof of any analysis value, on a button, sent to the Sandbox;
- **the Data shape sheets**: a small made-up table in the shape an analysis needs, one that does not
  work and why, and a check of the grid in front;
- **the error panel**: what went wrong in plain words, and its fixes.
- The owner (2026-11-13), on the tour: "a fun interactive simple with this little words as possible
  tutorial on what all the terminology means and how to use the app"; on Show the math: "So any
  analysis values that are shown can be proven"; on the Data shape: "a small mock-up instruction guide
  that simply shows a table and shows the format that data needs to be in"

**5.4 Explanations live in the guide, not on the screen.** The README, the user guide and the agent
guide explain; the screens do not.

**5.5 The product's name is not on its screens.** Page titles name the table in front ("orders",
"Key Finder · orders").
- The owner (2026-11-13): "The words Swiss army grid should appear nowhere in the app."

## 6. Words

**6.1 Plain words, full words.** No abbreviations or acronyms a newcomer would have to look up.
- For example: the "ERD" tab became "Diagram" and its heading "Relationship diagram"; "Fit test"
  rather than a statistics term on the button.
- Well-known format and product names stay as people say them: SQL, CSV, PDF, Excel, Python.
- Source: the build plan's working agreement 9 ("user-facing text and docs avoids unexplained
  acronyms").

**6.2 Buttons are verbs, in sentence case.** "Add connection", "Send to Sandbox", "Show these rows",
"Run again", "Copy as SQL". A button that opens a form ends with "…" ("Filter…", "Browse…").

**6.3 Messages say what happened, in a word or two.** "Saved", "Sent to the agent", "Restarted". No
advice in a toast.

**6.4 Messages are the user's words, never the machine's.** One plain message, said once.
- For example: when the server stops answering, the page shows "server is not answering" once, never
  "Failed to fetch", and never stacked.

**6.5 New ideas get short, memorable names, chosen with the owner.** The names come from the knife:
blades (saved code that reaches outside Python), tangs (a saved set of relationships), packs, the
Sandbox, the Workbench. Every name is in the tour.
- The owner (2026-11-12): "basically anytime you reach outside of python to load something, Boom thats
  a blade"

**6.6 Numbers read well.** Grid numbers have no thousands separators (an identifier must not look
like an amount); chart ticks do; results are rounded to four significant figures; plurals are right
("1 blade").

## 7. The look

**7.1 Steel and Swiss Army knife red.** Brushed-steel greys for the structure, one red for the brand
and the main action (the title, the open tab's edge, primary buttons, the first chart series). Errors
keep their own deeper red, always with words or an icon. No cross, flag or other mark: colours only.
- The owner (2026-11-13): "Why don't we design the default theme  in the app to be steel and Swiss army
  knife red"

**7.2 Every colour is a token.** Colours are defined once per theme in `web/static/grid.css`; nothing
is hard-coded, generated charts included. Light, dark, high contrast light and high contrast dark
follow the system by default; Windows forced-colours mode works.

**7.3 Readable by construction.** Text at least 4.5 to 1 against its background, interface parts 3 to
1, high contrast themes 7 to 1 with thick focus outlines. Blank, changed, error and agent-versus-user
marks carry an icon or text as well as colour.

**7.4 The Sandbox wears its own theme.** Twenty themes, colour-blind-safe and high contrast light and
dark first; a theme can be imported; it applies to the Sandbox and its exports without changing the
grid.
- The owner (2026-11-13): "the user can select from 20 basic themes l, colorblind and high contrast
  dark and light being the most important"

**7.5 Charts get care around the data.** Thin axes, quiet ticks, light dotted grids, the legend across
the top, labelled marker lines, hover boxes in the theme's colours, a calm summary box above.
- The owner (2026-11-13): "I saw your time series the number line and canvas looked good but the crap
  around it was ugly / That's usually true for most ai generated charts. There is no love given to
  legends and labels"; "it should actually be very clearly readable and look decent don't skimp on that
  part"

**7.6 Layout holds at any width.** No sideways page scroll in a narrow window; a long name is cut at its
end with an ellipsis; a long value never pushes the other columns out.

## 8. Keyboard

**8.1 Everything works without a mouse.** The toolbar and grid are reachable by keyboard; cards and
splitters resize with the arrow keys; Escape closes; the tour moves with the arrow keys.

**8.2 Borrow the keys people already know.** Excel's selection and copy; Jupyter's command mode (A, B,
D D, Z, Y, M, S); F5 and Ctrl+Enter as in RStudio and Visual Studio Code; F1 for help; Ctrl+. for a fix.
- The owner (2026-11-14): "Vscode is the gold standard for a good idea here."

**8.3 Every window action has one name.** The palette, the keys and the agent's `workbench_window` tool
run the same command.

## 9. Results and hand-offs

**9.1 Every result can go three places.** Send to Sandbox (a card), Open as grid (when it is a table),
To Python (the code, for the user's notebook). Show the math sits beside them.
- The owner (2026-11-13): "the results can go to the sandbox by choice or if the result is a grid then
  it can get sent to a grid but if the user wants just the python code to create the analysis they can
  also just choose to push the code to the terminal"

**9.2 Tools hand off to each other.** Separate tools with hand-off buttons, not one guided path: odds
to the fit test, find predictors to regression, regression to forecast, the model's misses to outliers.
- The owner (2026-11-13): "four is separate with handoff buttons"

**9.3 Anything that shows details can go to the Sandbox.** Profiles, distinct values, a cell's value,
any overlay.
- The owner (2026-11-13): "Basically any overlay that we have created to see details about a table or a
  column or a cell can be pushed to the sandbox"

**9.4 The Sandbox is the user's to arrange.** Cards drag, resize and take the user's own notes; the
whole Sandbox prints page by page (letter or landscape) and exports to markdown with charts as pictures.

**9.5 A stale result says so.** When the choices change, the old result dims under a note until the
next run.

## 10. Errors

**10.1 An error is explained and fixed where it happened.** The Workbench's error panel follows Visual
Studio's Exception Helper: the error and where it happened, what it means in a sentence, one-click
fixes, the failing expression's values at that moment, the call stack with library frames folded,
Python's own traceback behind a toggle.
- The owner (2026-11-14): "Let's try to make the inline error traping a stack tracing the beat part of
  this app / I love the visual studio style hover error panel"

**10.2 The error is also in the code.** The failing expression is underlined, its message sits at the
line's end, the hover explains, Ctrl+. offers the same fixes.

**10.3 An error can go to the agent.** Right-click an error or selected code, Send to the agent.
- The owner (2026-11-14): "Let's add a right click an error or highlight code and right click to send
  to an agent."

**10.4 Nothing leaves the computer to explain an error.** The words are written by the app itself; no
web service sees the user's code or data.

**10.5 A disabled button says why.** Its tooltip gives the reason ("Number or date columns only.").

**10.6 A limit is refused in plain words, before it does harm.** "bucket it first" for a fit test over
200 values; a value that would break a blade's code is refused with a plain message.

## 11. The golden rule: the agent can do what the user can

**11.1 Every user action is an agent tool, with no prompts.** A new user action gets its command in
`src/swissarmygrid/cli/commands.py` (and so a tool through the agent door, the Model Context Protocol
server) in the same change. `tests/test_cli.py` fails for a route with no command and no stated reason.
- The owner (2026-11-14): "And remember the Golden rule every single thing that user can do the AIA
  [the agent] should be able to also do it for mcp"; asked how far: "Everything, no prompts"

**11.2 The exceptions.** The agent cannot answer its own approval requests, and no answer it gets holds
a password's value (`/blades/{name}/python` stays refused). The agent's exports still wait for the
user's Allow (open: see section 16).

**11.3 The agent knows what it is looking at.** Tools say what exists, what formats a card takes and
what data can be plotted; the command names follow the screen's areas (`tabs_`, `table_`, `column_`,
`cell_`, `steps_`, `sandbox_`...), so the tool list reads like the screen.
- The owner (2026-11-13): "We also want to make sure there's no gaps in MCB tools ... the agents will
  need to understand what they are looking at what's available and what data they can plot with"

**11.4 What the agent does shows up live and is recorded.** Its changes appear on the user's screen as
they happen; its actions are in the agent activity list (never cell values).

## 12. Privacy and safety

**12.1 Local only.** The server listens on 127.0.0.1; a token on every data route (in the address's
fragment, never logged); Host and Origin checked; no telemetry, no update checks, no outbound calls.

**12.2 DuckDB is locked.** No SQL can read or write files, reach the network or load extensions;
spilling to disk is off. Files are read and written by the app's own Python.

**12.3 Nothing is written to disk without a click,** except the listed things: explicit exports, saved
recipes, saved dashboards and themes, the audit log, blades, tangs, connections, and Workbench files
saved by the user (with its recent list and environments). Catalogs, the Sandbox's live state and the
To Python queue stay in memory.
- For example: saved dashboards are saved on Save, not automatically, for this reason.

**12.4 Passwords are never shown back or sent to the agent.** They live in the system keyring, an
environment variable or a key vault; code says `secret("WAREHOUSE_PASSWORD")`; a vault's values reach
only the kernel's environment, never a page, the agent or the log (names only).

**12.5 Cell contents are data, never instructions.** Values reach the agent inside `<untrusted-data>`.

**12.6 Pages fetch nothing.** No runtime downloads: every library is bundled with the app; a content
security policy blocks other hosts; figures are cleaned (no scripts, no outside links, no map tiles).

**12.7 Made-up data only** in tests, examples and screenshots.
- The owner (2026-11-12): "you will need to stage your own data for testing here"

## 13. Defaults, sign-in and background work

**13.1 Secure by default; the user ticks to loosen.** For example: Trust server certificate is a
checkbox, off by default; a self-signed server needs the tick.
- The owner (2026-11-14): "Yea we need a trust server cert checkbox"

**13.2 No sign-in window out of nowhere.** Work the app starts by itself never opens a browser sign-in:
a connection that signs in with a browser window or a device code loads its catalog on first use, not
at start-up.

**13.3 Background work never makes the user wait.** Slow work runs in a child process or thread; the
screen fills in as results land; only the newest answer is drawn.
- For example: catalogs load two at a time in the background; the last tab clicked wins over a slow
  first load; the Key Finder keeps only its newest search's answer.
- The owner (2026-11-14): "We should auto load the catalog In a separate thread so it can background
  load and feed the autocomplete"

**13.4 Nothing runs Python until the user starts it.** A Workbench kernel starts on the first Run;
To Python waits for `sag.paste()`; a blade's code runs only when it is used.

**13.5 The browser draws; the server works.** The page shows the visible rows and sends intents; the
server does the computing. Heavy page libraries load the first time they are needed.

**13.6 One command installs everything.** uv installs the server once per computer; Visual Studio Code
is optional and only adds the agent tools and an Open button.
- The owner (2026-11-14): "The goal is to install everything with UV."

## 14. Connections, vaults, catalogs, blades and tangs

**14.1 Connections are databases, files and key vaults.** Each is added with a form per type, the code
underneath, tested before saving, in the Application row's Connections window.
- The owner (2026-11-14): "Key vaults should be another connection not a blade"

**14.2 Every connection is in every notebook and script by its name.** No setup lines, no checkbox.
- The owner chose (2026-11-14): "All connections, always"

**14.3 A password can come from anywhere the user keeps it:** the keyring, an environment variable or
a key vault, chosen in the form. Sign-in choices sit in the form (Windows, user name and password,
multi-factor), and switching keeps what was typed.
- The owner (2026-11-14): "Do all of those connections allow env variables to be used instead of
  entering?"

**14.4 Vaults map the hybrid way.** The vault's form lists its secret names; a box left empty keeps the
vault's own name, a typed name maps it; by default every secret loads under its own name, so local code
uses the same names as the cloud.
- The owner (2026-11-14): "whenever you add a ball [vault] it gives you a list of the secrets names and
  you can choose to map those to environment variables or you can just leave them as is and they will
  map into the same name as the name in the vault"; "That means you can develop your lambda or Azure
  function apps with a vault and use the same exact credentials that you'll be using in the cloud on
  your local machine"

**14.5 Catalogs load themselves** (names and types only, never data), feed completion as they land,
and say loading, loaded or not loaded (with the reason on hover).

**14.6 Read the code, don't ask the author to declare it.** A blade's settings, imports and packages
are found in its code; secrets travel as names only.
- The owner (2026-11-12): "also we need to detect imports and pips and make it easy for the user to add
  that to the env"

**14.7 Shared things are files people can pass around.** Blades (`.blade.py`, a header the app
recognises), tangs (`.tang`, a format no other file is mistaken for, runnable in the app to check its
joins), packs (a Git repository as a Claude Code marketplace).
- The owner (2026-11-13): "The problem is if I discover a relationship my coworkers have no way of
  knowing"

## 15. Engineering rules that shape the product

**15.1 Typed Python, strict checks.** Type hints everywhere; `mypy` strict, `ruff`, and `npm run
typecheck` before a commit.

**15.2 Tests first for behaviour.** Golden cases before the code for anything with meaning (filters,
steps, generated code, analyses); browser tests for what the user sees.

**15.3 The architecture is checked.** Three layers (domain, services, adapters), imports inwards, no
deeper than `src/swissarmygrid/<folder>/<file>.py`, one file per feature per layer
(`REFACTOR.md`, `tests/test_architecture.py`). The plain client uses the standard library only.

**15.4 Ask before adding a dependency** the plan does not list; prefer the standard library (the
statistics are the app's own).

**15.5 Verify, then record.** Anything relied on from a library or DuckDB is checked against the real
version and recorded in `docs/verified.md`.

**15.6 Ask the owner before designing a big feature,** with choices and a recommended one; record the
answer in `docs/ideas.md` as "the owner's call".
- The owner (2026-11-13): "You should probably talk to me first before designing anything"

**15.7 Cut what is heavy and little used.** For example, marimo was built in, then stripped.
- The owner (2026-11-13): "It's heavy and not well adopted."

## 16. Changed rules and open questions

Older text still says otherwise in places; follow the newer rule.

- **The agent runs code now.** The standalone plan's decision 4 ("the agent cannot run code in the
  notebook") and "the agent never reads a blade's code" were replaced by the golden rule (2026-11-14).
  The README (lines 250 and 458) and the user guide still say the agent can never run code in a kernel
  or send code to the user's Python; `python_send` and the Workbench tools now let it.
- **Vaults** were blades (2026-11-12, and the Workbench plan's decision 1), then connections
  (2026-11-14). Secret blades (values typed again every session) remain as blades.
- **Privacy levels** were removed (2026-11-12): the agent sees what the user sees.
- **Open:** should the agent's exports drop their Allow, under "no prompts"? Should the Sandbox save
  itself and keep a daily history, as first designed, given the no-disk rule? Should inline
  suggestions from an AI model (the owner mentioned GitHub's code completion) be offered, given that
  nothing leaves the computer?

## Reviewer checklist

Run this against every new screen or feature.

1. Can each task on it be done in three actions or fewer? If not, can it be shorter?
2. Does each control sit beside what it controls, and does a new mode sit beside its tool?
3. Is it in the right row or window (Application, Table, Column, Cell; grid page, Workbench, Sandbox,
   blades page)? Does code stay out of the grid page?
4. Is there any help paragraph, how-to hint, long placeholder or tooltip that repeats a label? Remove
   it, unless it is an error, a warning, a question about losing work, or the user asked for it.
5. Are the words plain and full, with no unexplained abbreviations? Are buttons verbs in sentence case,
   with "…" when they open a form? Do messages say what happened, once?
6. Does the product's name appear on the screen? It should not.
7. Are all colours tokens? Does it read well in light, dark, both high contrast themes and forced
   colours, with marks that carry an icon or words as well as colour?
8. Does every row stay on one line? Does it hold at a narrow width without sideways scrolling?
9. Does it work from the keyboard alone?
10. Can its results go to the Sandbox, a grid and To Python, and can its numbers be shown with the math?
11. Does an error say what happened in plain words, where, and offer the fix? Does a disabled button say
    why?
12. Does the agent have the same action as a command in `cli/commands.py`, in the same change?
13. Does it write anything to disk without a click, show a password back, or send anything off the
    computer? It must not.
14. Is the default the safe one? Could it open a sign-in window by itself, or make the user wait?
15. Are there tests first for its behaviour, a browser test for what the user sees, made-up data only,
    and a note in `docs/verified.md` or `docs/ideas.md` for what was checked or decided?
