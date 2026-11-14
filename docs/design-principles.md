# Design principles

A reusable set of rules for building a local data app like SwissArmyGrid, so the next app starts from
these decisions instead of finding them again. Each rule is written for any new app; under it are why,
an example from SwissArmyGrid, and the owner's words where the rule came from them (with the date).
Quotes are exact, typing slips included; a word in square brackets is a clarification.

It is for a designer, a coding agent and a reviewer. Section 14 says the order to set a new app up;
section 15 is the checklist to run against every new screen.

## 1. Purpose first

**1.1 One job, one screen's worth of work.** Every feature should cut the switching between tools the
user would otherwise pan across.
- Example: the grid, the code (the Workbench) and the dashboard (the Sandbox) each sit on their own
  screen and stay in step through the server.
- The owner (2026-11-12): "I created SAG to make python to duckdb work easier instead of having to pan
  3 screens to work"

**1.2 Let each engine do what it is best at, and say which.** Decide once which part does which job,
and keep to it.
- Example: Python analyses; DuckDB SQL transforms. A transformation is always SQL over the user's own
  table, never a pandas translation of it.
- The owner (2026-11-12): "let SAG have the features that make python powerful like analysis and speed
  of data, and let duck db and sql do the transforming"

**1.3 What leaves the app must work where it lands.** Code, files and exports are checked exactly as
the user will use them.
- Example: Copy as Python gives one runnable line over the user's own variable, run and compared with
  the grid before it is copied.

## 2. Clicks and actions

**2.1 Three actions or fewer.** A task that takes more than three clicks or actions, and could take
fewer, is a defect.
- Example: the Key Finder runs as soon as its window opens; an empty diagram offers Find relationships,
  which opens the finder already running.
- The owner (2026-11-14): "if it takes more than 3 clicks or actions and can be reduced then it should
  considered as an improvement"

**2.2 Do the obvious work, let the user change it after.** When the app can work out what the user
means, it fills it in.
- Example: a blade's settings are read from its code and offered as one-click suggestions; a key vault
  lists its secret names by itself; the tie-out report suggests the totals.
- The owner (2026-11-13): "diff columns over top of each other simply by pulling two tables side by
  side and pushing a button"

**2.3 Direct gestures where the thing is, a named command as well.** Drag, right-click and keys are
shortcuts; every action also has a button or a palette entry.
- Example: drag a connection into a cell; right-click a cell to send it to the Sandbox; Script as on
  the catalog's right-click menu, also a command.
- The owner (2026-11-13): "you can even right click an individual cell and send that value to the
  sandbox"

**2.4 Make the hard part easy, not just possible.** A painful setup gets a form with choices, never a
page of instructions.
- Example: a notebook's own environment is made from library presets in a dialog and remembered; the
  error panel's fix adds the missing import or installs the package.
- The owner (2026-11-13): "make it super easy to create them. Like a set of combo boxes to import common
  libraries."

## 3. Placement

**3.1 A control sits beside the thing it controls.** If a feature takes extra steps because of where it
lives, it is in the wrong place.
- Example: the steps strip sits under the table's title; a card's actions sit on the card; a file
  field's Browse… sits beside the field.
- The owner (2026-11-14): "If it takes extra steps or a feature isn't conveniently located beside the
  thing that it's controlling or to features work better side by side on the screen then it should be
  considered as well"

**3.2 Features used together sit side by side.** A new mode of a tool goes next to that tool.
- Example: compare's diff mode beside cross compare; Tie-out in the Compare row.
- The owner (2026-11-13): "We could simply add another mode right beside cross compared"

**3.3 Group actions by what they act on, and name the group.** Application-wide, then the whole table,
then the selected column, then the selected cell; a row that only makes sense sometimes appears only
then.
- Example: the ribbon's Application, Table, Column and Cell rows; Compare only while two grids are side
  by side.
- The owner (2026-11-13): "Just put all those app level commands in another row above tables call
  Application"

**3.4 Each kind of thing has one home.** Pictures and charts gather in one place, code in another;
a second place for the same thing is removed.
- The owner (2026-11-14): "The bottom right pane isn't needed it goes to the sandbox."

**3.5 Panes stay where people expect them.** Copy the layout of the tools users already know.
- Example: the Workbench is RStudio's: source, console under it, Variables on the right, the side bar
  on the left.
- The owner (2026-11-14): "Vscode is the gold standard for a good idea here. RStudios ide might be
  closer to what we are after though so a good blend of the two. The three panel design with variables
  in r studio is nice"

**3.6 One line per row.** A toolbar row, the status bar and a form row never wrap into a stack: a row
that wraps moves everything under it.
- Example: when the ribbon runs out of room, the words of its last buttons fold to icons.

## 4. Windows

**4.1 Code always opens in its own browser window, never in the data page.** So it can sit on another
screen.
- The owner (2026-11-12): "Here is a rule. The terminal should be in its own browser tab always it is
  not in the same page as the grids / So it can be on a separate screen"

**4.2 The data page is for the data.** The table gets the whole page; editors and long panels open in
their own window.
- Example: the steps list opens in its own window from Edit steps; there is no SQL box on the grid page.

**4.3 Decide where each kind of work lives, once.**

| Place | Holds |
|---|---|
| The data page | the tab strip, the Application row, the ribbon, the strip under the title, the table, the status bar |
| The code window | notebooks and scripts, their consoles, Variables, the side bar, the error panel |
| The dashboard tab | resizable cards: charts, notes, tables, diagrams, pictures, results |
| The library page | saved code, catalogs and shared packs, with their save screen |
| Dialogs | one task each: a step, an analysis, an export, a connection, the settings |

**4.4 A dialog is one task.** One form per kind of thing, not one form with every field.
- The owner (2026-11-13): "Of course each analysis will need its own form to choose columns and input
  data from the grid"

## 5. Layout patterns

**5.1 The ribbon.** Rows stacked top to bottom, each starting with a caption cell: the row's name in
bold and, under it, what it is acting on ("Column: amount, 2 selected"). Buttons are an icon and a
word. A button with nothing to act on is disabled, its tooltip giving the reason. Rows: Application
(settings, connections, the library, the tour, zoom: on every tab), Table, Column, Cell, and a
contextual row (Compare) only when it applies. On a tab without a table the ribbon hides and the
Application row stays.

**5.2 The tab strip.** Fixed app tabs first (Sandbox, Diagram, Workbench), then one tab per open
document, then "+" to make a new one. The open tab is bold with a 2px accent line on its top edge.
Tabs are 32px high, 90 to 240px wide, long names cut with an ellipsis. Drag to reorder; drag one tab
onto another to show the two side by side.

**5.3 Side bars.** A side bar holds what you bring into the main area (connections, saved code, the
data explorer), on the left, opened and closed by one key (Ctrl+B). A results panel sits on the right
and starts collapsed in a window narrower than 1,100px.

**5.4 Panes with splitters.** Splitters can be dragged, focused and moved with the arrow keys, reset by
a double-click; the sizes are remembered.

**5.5 Dialogs.** A head (the title, a close button), a body that scrolls, and a footer with its buttons
on the right: Back first, then secondary actions (Test), then the one primary action last (Save). Width
720px by default, 1,200px for wide ones, never wider than the window less 32px. Closing a dialog that
asks a question answers no.

**5.6 Lists of rows.** Each row: the name, then its kind and a status word, then its actions on the
right as small icon buttons. An empty list says so in a few words ("No connections").

**5.7 Forms.** Labels above fields, in the muted colour. Fields flow in a grid (each at least 180px,
gap 10px); short fields share a row and a wide field (a path, a connection string) takes its own row
under them. Browse… sits beside its field. An on/off setting is a checkbox, never a yes/no text box. A
password field offers where the value comes from (the keyring, an environment variable, a vault).
Generated code sits underneath, folded, for the unusual case.

**5.8 Cards.** A dashboard of cards on a 12-column grid with 40px rows. Each card: a header with its
title on the left and icon actions on the right, resized from its corner (or the arrow keys), what it
draws refitted to its size.

**5.9 The status bar.** One line, 30px high, numbers in tabular figures: the row count, the selection's
count, sum and average, the memory meter. On a tab without a table it names the tab.

**5.10 The command palette.** Ctrl+Shift+P opens a filtered list of every command, each showing its
keys; Enter runs it. It reads the same list as the keys and the agent (see 8.3).

**5.11 Right-click menus.** What can be done at that spot, under a small muted label naming it. Every
entry is also somewhere else (a button or the palette).

## 6. The visual system

**6.1 One stylesheet of tokens.** Every colour is a named token defined once per theme; nothing is
hard-coded anywhere, charts and diagrams included. Tokens are named for their role, never their hue.

| Token | Role | Light | Dark |
|---|---|---|---|
| `--bg` | the page and inputs | `#ffffff` | `#121518` |
| `--surface` | bars and panels | `#eef1f4` | `#191d22` |
| `--surface-2` | captions, hover | `#e1e6eb` | `#21272d` |
| `--raised` | dialogs, cards, buttons | `#ffffff` | `#21272d` |
| `--fg` | text | `#1c2228` | `#e8ecef` |
| `--muted` | labels, secondary text | `#525c66` | `#a2acb6` |
| `--border` | lines between things | `#c8d0d8` | `#343c45` |
| `--border-strong` | inputs, hover, dialogs | `#7d8893` | `#707b86` |
| `--accent` | the brand and the main action | `#c8102e` | `#ff6b73` |
| `--accent-fg` | text on the accent | `#ffffff` | `#1c0a0c` |
| `--accent-soft` | a pressed or chosen fill | `#fbe6e9` | `#3d1c21` |
| `--focus` | the focus ring | `#38434e` | `#c4ccd4` |
| `--ok` | success, "go" | `#17722f` | `#3fb950` |
| `--warn` / `--warn-soft` | warnings | `#855800` / `#fff4d6` | `#d29922` / `#3a2e10` |
| `--error` / `--error-soft` | errors | `#a3141f` / `#fde8e7` | `#ff8a80` / `#3d1a19` |
| `--agent` / `--agent-soft` | what the agent did | `#6639ba` / `#efe9fb` | `#b392f0` / `#2c2244` |
| `--highlight`, `--changed`, `--selected`, `--row-hover` | data marks | `#fff1b8`, `#dcf4e3`, `#dbe3ec`, `#f2f4f7` | `#4a3f10`, `#133d24`, `#253342`, `#1d2228` |
| `--cat-1` to `--cat-8` | chart series | red, blue, green, magenta, olive, teal, violet, grey | lighter versions of the same |

**6.2 Four themes, following the system.** Light, dark, high contrast light, high contrast dark;
"system" is the default and follows the dark mode and "more contrast" settings; the user's choice is
remembered. Windows forced-colours mode maps the tokens to system colours. High contrast themes are
flat (no gradients), use 3px focus rings, black or white borders, and their own accent.

**6.3 One brand colour, used for the main action.** The accent marks the brand and the main action
only (the title, the open tab's edge, the primary button, the first chart series). Structure is
neutral. Errors get their own deeper red, so the brand never reads as an error.
- Example: brushed steel greys with Swiss Army knife red.
- The owner (2026-11-13): "Why don't we design the default theme  in the app to be steel and Swiss army
  knife red"

**6.4 Readable by construction.** Text at least 4.5 to 1 against its background, interface parts 3 to 1,
high contrast themes 7 to 1. A test checks every token pair. No meaning by colour alone: blank,
changed, error and agent marks carry an icon or a word too.

**6.5 A dashboard may wear its own theme.** Offer a set of themes (twenty, colour-blind-safe and high
contrast first) and an import, applied to the dashboard and its exports without changing the app.
- The owner (2026-11-13): "colorblind and high contrast dark and light being the most important"

**6.6 Type.**
- Interface: "Segoe UI", system-ui, -apple-system, sans-serif, 13px, line height 1.45, all scaled by
  one zoom setting.
- Code and values: "Cascadia Code", Consolas, "Courier New", monospace, at 0.95em.
- Smaller text (captions, badges, counts) never under 11.5px. Labels 0.92em in the muted colour.
  Dialog titles 1.08em. Weight 600 for the primary button and the open tab, 650 for row captions.
- Numbers that line up (the status bar, counts) use tabular figures.

**6.7 Spacing, borders and corners.**
- Spacing steps: 2, 4, 6, 8, 10, 12px (6 and 8 most often); dialog body padding 14px, its head and foot
  10px by 14px.
- Controls 28px high (24px for small ones); inputs padded 3px by 8px.
- Borders 1px in `--border`; inputs in `--border-strong`. Focus: a 2px ring in `--focus`, 2px off the
  element, keyboard only.
- Corners: 6px on buttons and inputs, 8px on tabs (top corners) and toasts, 10px on cards, popovers and
  the palette, 12px on dialogs, round on badges.
- One shadow token for anything that floats (dialogs, popovers, toasts).

**6.8 Icons.** One set of line icons, drawn on a 24 grid with a 1.8 stroke in the text's colour, shown
at 15px and scaled with the zoom.
- Icon and word: every main command (ribbon, Application row, dialog footers).
- Icon only: small repeated actions on a card or a row (close, copy, delete, move, refresh, more), as a
  ghost button, always with a name for screen readers and a tooltip.
- Words fold to icons only when a row runs out of room.

**6.9 Buttons, and when each.**

| Kind | Looks | Use |
|---|---|---|
| Plain | raised fill, 1px border | most actions |
| Primary | accent fill, accent text, bold | the one main action of a dialog or form; at most one per place |
| Ghost | no fill, no border until hover | icon actions on cards, rows and headers |
| Danger | outlined in the error colour, never filled | delete, remove, discard |
| Go | success fill | Run, where green reads as "go" |
| Pressed toggle | accent fill with a check mark | an on/off button that is on |
| Disabled | faded to 55% | nothing to act on; the tooltip says why |

**6.10 Badges, hints and messages.**
- Badges: a small round-ended label with a 1px border in its own colour (agent, success, warning,
  error, each on its soft fill). Use them for a kind or a state ("Agent", "loaded").
- Hints: muted, 0.9em, a few words beside a field at most (the setting's key in brackets). Never a
  sentence of instructions.
- Error text: the error colour with a warning sign before it, next to what failed.
- Toasts: bottom right, above the status bar, a 4px left edge in the accent (in the error colour, with a
  warning sign, for an error); they say what happened in a word or two.
- Success: the success colour or a success badge; no celebration.

**6.11 Charts get care around the data.** Thin axis lines, quiet ticks with thousands separated, light
dotted grids, the legend across the top, labelled marker lines, hover boxes in the theme's colours, a
calm summary above with its first sentence as the headline. Series take the chart tokens; the alarm
colour is kept for alarms.
- The owner (2026-11-13): "the number line and canvas looked good but the crap around it was ugly /
  That's usually true for most ai generated charts. There is no love given to legends and labels"

**6.12 Hold at any width.** No sideways page scroll in a narrow window; a long name is cut at its end
with an ellipsis; one long value never pushes the other columns out.

## 7. Words and on-screen text

**7.1 Let the design speak for itself.** No help paragraphs, how-to hints, tips or explanations of what
a pane is. Labels, headings, counts and the layout carry the meaning. Empty states are a few words;
placeholders one or two; a ribbon caption shows what is selected and never tells the user to select.
- The owner (2026-11-14): "You need to thoroughly rid the app of over explanatory text and instructions
  that are in the gui. Let the design speak for itself"

**7.2 Text that stays.** Errors and warnings (shortened, never removed); questions about losing work or
allowing the agent; and explanations the user asks for, behind a button:
- a tour: one card per word, lighting up the real control, then a short game, behind a question mark;
- "Show the math": any shown result as a step-by-step proof;
- a "Data shape" sheet: a tiny made-up table in the shape an analysis needs, one that does not work, and
  a check of the table in front;
- the error panel (section 9).
- The owner (2026-11-13): "a fun interactive simple with this little words as possible tutorial on what
  all the terminology means"; "So any analysis values that are shown can be proven"; "a small mock-up
  instruction guide that simply shows a table and shows the format that data needs to be in"

**7.3 Tooltips are short and add something:** a keyboard shortcut ("Run file (F5)") or the reason a
button is disabled. A tooltip that repeats the label goes.

**7.4 Plain words, full words.** No abbreviation or acronym a newcomer would look up ("Diagram", not
"ERD"). Names people say as they are stay (SQL, CSV, PDF, Excel, Python).

**7.5 Buttons are verbs in sentence case:** "Add connection", "Send to Sandbox", "Show these rows". A
button that opens a form ends with "…" ("Filter…", "Browse…").

**7.6 Messages say what happened, in the user's words, once.** "Saved", "Sent to the agent"; "The
server is not answering", never "Failed to fetch", never stacked. A limit is refused in plain words
before it does harm ("bucket it first").

**7.7 Name new ideas with short, memorable words from one family,** chosen with the owner, and put every
one in the tour.
- Example: from the knife: blades (saved code that reaches outside Python), tangs (a saved set of
  relationships), packs.
- The owner (2026-11-12): "basically anytime you reach outside of python to load something, Boom thats
  a blade"

**7.8 The product's name is not on its screens.** Window titles name what is in front ("orders",
"Key Finder · orders").
- The owner (2026-11-13): "The words Swiss army grid should appear nowhere in the app."

**7.9 Numbers read well.** Identifiers get no thousands separators; chart ticks do; results are rounded
to four significant figures; plurals are right ("1 blade").

## 8. Interaction patterns

**8.1 Keyboard first.** Everything works without a mouse: the toolbar and the table are reachable by
Tab; Escape closes; arrow keys resize cards and splitters; every window has a command palette.

**8.2 Borrow keys people already know:** Excel's selection and copy, Jupyter's command mode (A, B, D D,
Z, Y, M, S), F5 and Ctrl+Enter to run, F1 for help, Ctrl+. for a fix.

**8.3 One list of commands feeds every door.** Each action is defined once, with an id, a title, its
keys and its arguments; the buttons, the keys, the palette and the agent all read that list, so they
never drift apart.

**8.4 Live, without reloading.** A change from anywhere (a new push, the agent, another window) shows
up at once over a socket, keeping the user's sort, scroll and layout; a dropped socket reconnects by
itself.

**8.5 Background work never makes the user wait, and says how it is going.** Slow work runs in a child
process or thread; the screen shows a status word (loading, loaded, not loaded, with the reason on
hover) and fills in as results land; only the newest answer is drawn.
- The owner (2026-11-14): "We should auto load the catalog In a separate thread so it can background
  load and feed the autocomplete"

**8.6 Nothing appears or asks unasked.** No sign-in window, prompt or pop-up from work the app started
by itself; nothing runs code until the user starts it.
- Example: a connection that signs in through a browser window loads its catalog on first use, never at
  start-up; a kernel starts on the first Run; copied code waits for the user to paste it.

**8.7 Every result can go three places.** To the dashboard, to a new table (when it is a table), or as
code for the user's own notebook; "Show the math" beside them. Anything that shows details (a profile, a
value, an overlay) can go to the dashboard.
- The owner (2026-11-13): "the results can go to the sandbox by choice or if the result is a grid then
  it can get sent to a grid but if the user wants just the python code to create the analysis they can
  also just choose to push the code to the terminal"

**8.8 Tools hand off to each other.** Separate tools with hand-off buttons, not one long guided path.
- Example: odds hands to the fit test, find predictors to regression, regression to forecast, the
  model's misses to outliers.

**8.9 A stale result says so.** When the inputs change, the old result dims under a short note until the
next run.

**8.10 The dashboard is the user's to arrange.** Cards drag, resize and take the user's own notes; it
prints page by page and exports to markdown with charts as pictures.

## 9. Errors

**9.1 Explain and fix the error where it happened.** Follow Visual Studio's Exception Helper: the error
and where, what it means in a sentence, one-click fixes, the failing expression's values at that moment,
the call stack with library frames folded, the raw traceback behind a toggle.
- The owner (2026-11-14): "Let's try to make the inline error traping a stack tracing the beat part of
  this app / I love the visual studio style hover error panel"

**9.2 Mark the error in the code too:** underline the failing expression, its message at the line's end,
the explanation on hover, the fixes on Ctrl+.

**9.3 An error can go to the agent** with a right-click, with the user's own question if they want.
- The owner (2026-11-14): "Let's add a right click an error or highlight code and right click to send
  to an agent."

**9.4 Nothing leaves the computer to explain an error.** The app writes the words itself.

## 10. The agent can do what the user can

**10.1 Every user action is an agent tool, with no prompts.** A new user action gets its command in the
command list (8.3), and so its agent tool, in the same change. A test fails for any route that has
neither a command nor a stated reason.
- The owner (2026-11-14): "every single thing that user can do the AIA [the agent] should be able to
  also do it for mcp"; asked how far: "Everything, no prompts"

**10.2 Two exceptions.** The agent cannot answer its own approval requests, and no answer it gets holds
a password's value.

**10.3 The agent knows what it is looking at.** Tools say what exists, what each format takes and what
data can be used; their names follow the screen's areas (`tabs_`, `table_`, `column_`, `cell_`,
`steps_`...) so the list reads like the screen; the agent can take a picture of the user's screen.
- The owner (2026-11-13): "the agents will need to understand what they are looking at what's available
  and what data they can plot with"

**10.4 What the agent does is visible and recorded:** live on the user's screen, marked in the agent's
colour, listed in the activity log (never with cell values). Data handed to the agent is marked as data,
never instructions.

## 11. Privacy and safety

**11.1 Local only.** Listen on 127.0.0.1; a token on every data route (kept out of logs); check Host and
Origin; no telemetry, no update checks, no outbound calls.

**11.2 Lock the engine.** The query engine cannot read or write files, reach the network or load
extensions, and does not spill to disk; the app's own code does file work.

**11.3 Nothing on disk without a click,** except a short, written list (exports, saved recipes and
dashboards, saved code and shared files, connections, user-saved documents, the audit log). Everything
else stays in memory.

**11.4 Passwords are never shown back or sent to the agent.** Keep them in the system keyring, an
environment variable or a key vault; code refers to them by name (`secret("WAREHOUSE_PASSWORD")`).

**11.5 Pages fetch nothing.** Bundle every library; a content security policy blocks other hosts; clean
every drawing of scripts and outside links.

**11.6 Made-up data only** in tests, examples and screenshots.
- The owner (2026-11-12): "you will need to stage your own data for testing here"

**11.7 Secure by default; the user ticks to loosen.**
- Example: Trust server certificate is a checkbox, off by default.

## 12. Connections, secrets and shared files

**12.1 Anything that reaches outside the app is a connection,** added with a form per type, tested
before saving, in one Connections window; key vaults are connections too.
- The owner (2026-11-14): "Key vaults should be another connection not a blade"

**12.2 Connections are simply there.** Every connection is available in every notebook and script by
its name, with no setup lines.

**12.3 Map the hybrid way.** Show the source's own names; a box left empty keeps the name, a typed one
maps it; by default load everything under its own name, so local code reads the same names as the cloud.
- The owner (2026-11-14): "you can choose to map those to environment variables or you can just leave
  them as is and they will map into the same name as the name in the vault"

**12.4 Sign-in choices live in the form,** and switching keeps what was typed (Windows, user name and
password, multi-factor).

**12.5 Read the code instead of asking the author to declare it:** settings, imports and packages are
found in the code; secrets travel as names only.

**12.6 Shared things are files people can pass around,** in a format no other file is mistaken for, that
the app can open and check, kept in folders or a Git repository the team already uses.
- The owner (2026-11-13): "if I discover a relationship my coworkers have no way of knowing"

## 13. Engineering rules that shape the product

- **Typed code, strict checks:** type hints everywhere; strict type checking and a linter before each
  commit.
- **Tests first for behaviour;** browser tests for what the user sees, including the rules above (one
  line rows, keyboard reach, themes, plain messages).
- **Layers checked by a test:** domain, services and adapters, imports pointing inwards, no deeper than
  `package/folder/file`, one file per feature per layer.
- **Ask before adding a dependency;** prefer the standard library; cut what is heavy and little used.
- **Verify, then record:** anything relied on from a library is checked against its real version and
  written down (`docs/verified.md`).
- **Ask the owner before designing a big feature,** with choices and a recommended one, and record the
  answer (`docs/ideas.md`).
  - The owner (2026-11-13): "You should probably talk to me first before designing anything"
- **The browser draws; the server works.** Heavy page libraries load the first time they are needed.
- **One command installs everything;** an editor is optional.

## 14. Starting a new app

Set things up in this order, so every later screen inherits the rules instead of fixing them.

1. **Tokens and themes.** The stylesheet of tokens (6.1), the four themes and forced colours (6.2), the
   contrast test (6.4), the type, spacing, corner and icon rules (6.6 to 6.8). No screen before this.
2. **The building blocks.** One helper each for a button (its label is also its screen-reader name), an
   icon, a field, a dialog with head, body and footer, a toast, a menu; the button kinds of 6.9.
3. **The shell.** Windows decided (4.3); the tab strip, the Application row, the ribbon's rows with
   captions and folding, the status bar, side bars and splitters.
4. **The command list.** One registry of every action (id, title, keys, arguments) that feeds the
   buttons, the keys, the palette and the agent tools, with the test that every route has a command.
5. **The safety posture.** Local address, token, Host and Origin checks, a locked engine, the list of
   what may be written to disk.
6. **Features,** each with tests first, its command in the list, its result going the three places
   (8.7), and the checklist below run before it ships.

## 15. Reviewer checklist

Run this against every new screen or feature.

1. Can each task be done in three actions or fewer? If not, can it be shorter?
2. Does each control sit beside what it controls, and a new mode beside its tool?
3. Is it in the right row and window? Does code stay out of the data page?
4. Is there any explaining text: a help paragraph, a how-to hint, a long placeholder, a tooltip that
   repeats a label? Remove it, unless it is an error, a warning, a question, or something the user asked
   for.
5. Are the words plain and full? Buttons verbs in sentence case, "…" when they open a form? Messages say
   what happened, once?
6. Is the product's name on the screen? It should not be.
7. Are all colours tokens? Does it read well in light, dark, both high contrast themes and forced
   colours, with marks that carry an icon or a word as well as colour?
8. One primary button at most, the main action, last in the footer? Icon-only buttons only for small
   repeated actions, each with a name?
9. Does every row stay on one line, and the screen hold at a narrow width?
10. Can it be used from the keyboard alone, and is every action in the palette?
11. Can the agent do it through a command, in the same change?
12. Do slow parts run in the background with a status word? Does anything appear or ask unasked?
13. Do its results go to the dashboard, a table and code, and can its numbers be shown with the math?
14. Does an error say what happened, where, and offer the fix? Does a disabled button say why?
15. Does it write to disk without a click, show a password, or send anything off the computer? It must not.
16. Tests first, made-up data in every test and screenshot, and what was checked or decided written down?
