/**
 * workbenchPage.ts: the Workbench, in a window of its own (Plans/workbench-and-connections.plan.md;
 * opened from the grid page's Workbench tab). [COMPOSITION]
 *
 *   [PANES]    RStudio's four (workbenchLayout.ts): the source, its console under it, Variables top
 *              right, Plots and Help under it; splitters; a status bar (kernel, environment, cursor)
 *   [LEFT]     the side bar (catalogRail.ts, Ctrl+B folds it): Connections and Blades (drag one into a
 *              cell or the script for its line; Add connection; a new blade), and the data explorer
 *              (each connection's catalog: drag a table or column for its name)
 *   [FILE]     New notebook, New script, Open…, Recent ▾, Save, Save as… (the file chooser: real paths)
 *   [TABS]     the open documents (a dot when changed); closing one stops its kernel
 *   [RUN]      the Python the kernel runs in (this server's own, or a project's environment), the
 *              kernel's state, Run all or Run file, Interrupt, Restart
 *   [DOC]      a notebook (workbenchNotebook.ts) or a script (workbenchScript.ts), each with its own
 *              console (workbenchConsole.ts) on its kernel; a blade being edited; a table from the explorer
 *   [COMMANDS] every action by one name (workbenchCommands.ts): the palette (Ctrl+Shift+P), the keys, and
 *              the agent's workbench_window tool
 *   [KEYS]     F5 runs (the file, or every cell) wherever the focus is, never reloading the window;
 *              Ctrl+S saves, Ctrl+O opens; Ctrl+Z outside an editor undoes a cell's delete or move;
 *              Ctrl+1 to Ctrl+4 (and F6) move between the panes; Ctrl+Alt+Page Up and Page Down switch
 *              documents; F1 on a name shows its help
 *   [RESTORE]  open documents and their unsaved text come back after a reload or a crashed tab (kept in
 *              this window's session storage, in the browser only: nothing is written to disk)
 *
 * ROUTING: /workbench/home, /open, /save, /pythons (routes_workbench.py); kernels in workbenchKernel.ts.
 */
import { handleAgentNotice, loadApprovals } from "./agentAccess";
import { api, ApiError, connect, onNotice, openedWith, tellFocus } from "./api";
import { type BladeKind, editBlade, showTemplatePicker } from "./bladeEditor";
import { mountRail, tableDetails, watchTheme } from "./catalogRail";
import { button, clear, h, toast } from "./dom";
import { chooseFile } from "./fileChooser";
import { effectiveTheme } from "./theme";
import { allCommands, answerAgent, type Command, openPalette, registerCommands, runCommand } from "./workbenchCommands";
import { consoleView, type ConsoleView } from "./workbenchConsole";
import { forgetCatalogs, pythonCompleter } from "./workbenchCompletion";
import { newEnvironment } from "./workbenchEnvironment";
import type { Fix } from "./workbenchErrors";
import { mountLayout } from "./workbenchLayout";
import { variablesPanel } from "./workbenchVariables";
import { Kernel } from "./workbenchKernel";
import { type CellData, notebookView, type NotebookView } from "./workbenchNotebook";
import { scriptView, type ScriptView } from "./workbenchScript";

watchTheme();

interface Doc {
  id: number;
  kind: "notebook" | "script" | "blade" | "table";
  title: string;
  path: string | null;
  modified: number | null;
  dirty: boolean;
  kernel: Kernel | null;
  python: string | null;
  element: HTMLElement;
  notebook?: NotebookView;
  script?: ScriptView;
  /** Its console, on its kernel (notebooks and scripts). */
  console?: ConsoleView;
}

interface PythonChoice {
  python: string;
  version: string;
  ipykernel: boolean;
  swissarmygrid: boolean;
}

const tabs = document.getElementById("wb-tabs") as HTMLElement;
const toolbar = document.getElementById("wb-toolbar") as HTMLElement;
// The four panes; Variables docked top right (the toolbar's Variables shows or hides the right column).
const layout = mountLayout(
  document.getElementById("wb-body") as HTMLElement,
  document.querySelector(".wb-layout > .cat-rail") as HTMLElement,
  document.querySelector(".wb-main") as HTMLElement,
  () => focusSource(),
  () => focusConsole(),
);
const variables = variablesPanel(() => {
  layout.toggle("right", false);
  drawToolbar();
});
layout.variables.append(variables.element);
const docHost = layout.source;
let cursor = { line: 1, column: 1 };
const docs: Doc[] = [];
let current: Doc | null = null;
let counter = 0;
let homeFolder = "";
let pythons: PythonChoice[] = [];
let connectionNames: string[] = [];
let bladeNames: string[] = [];
let codeNames: { name: string; connection: string; kind: string }[] = [];
let editingBlade = false;

const baseName = (path: string): string => path.split(/[\\/]/).pop() ?? path;

async function loadConnections(): Promise<void> {
  try {
    const every = (await api<{ blades: { name: string; connection?: string | null; code_name?: string | null }[] }>("/blades")).blades;
    connectionNames = every.filter((b) => b.connection).map((b) => b.name);
    bladeNames = every.filter((b) => !b.connection).map((b) => b.name);
    codeNames = every.filter((b) => b.code_name).map((b) => ({ name: b.code_name!, connection: b.name, kind: b.connection ?? "" }));
  } catch {
    connectionNames = [];
    bladeNames = [];
    codeNames = [];
  }
}

// ===== documents =====

const names = (): { connections: string[]; blades: string[]; codeNames: typeof codeNames } => ({ connections: connectionNames, blades: bladeNames, codeNames });
const hooks = {
  cursor: (line: number, column: number): void => {
    cursor = { line, column };
    drawStatus();
  },
  help: (): void => void showHelp(),
};

function makeDoc(kind: Doc["kind"], title: string, path: string | null, modified: number | null): Doc {
  const doc: Doc = { id: ++counter, kind, title, path, modified, dirty: false, kernel: null, python: null, element: h("div", { class: `wb-doc-body kind-${kind}` }) };
  if (kind === "notebook" || kind === "script") {
    const kernel = new Kernel(() => doc.path, () => doc.python);
    doc.kernel = kernel;
    kernel.onState(() => {
      if (doc === current) {
        drawToolbar();
        drawStatus();
      }
    });
    doc.console = consoleView(kernel, () => effectiveTheme(), {
      complete: pythonCompleter(kernel, names),
      help: (code, offset) => kernel.help(code, offset),
      f5: () => void runPrimary(doc),
      // A script's runs mark its lines; a line typed in the console has only the panel's own fixes.
      errors: (origin) =>
        doc.script?.errorHooks(origin) ?? {
          fix: (fix) => void (pageFix(doc)(fix) || doc.console?.note("Type the line again with the change.")),
          goTo: () => false,
          openGrid: (name) => void openGrid(doc, name),
        },
      hint: kind === "script" ? "F5 runs the file; Ctrl+Enter the statement at the cursor" : "A scratch prompt on this notebook's kernel",
    });
  }
  docs.push(doc);
  return doc;
}

/** The fixes only the window can make: install into the document's Python (in its console). */
const pageFix = (doc: Doc) => (fix: Fix): boolean => {
  if (fix.kind === "install" && fix.package && doc.console) {
    if (!/^[A-Za-z0-9._\-[\],=<>!~ ]+$/.test(fix.package)) return false;
    void doc.console.run({ code: `%pip install ${fix.package}` }, `%pip install ${fix.package}`).then(() => doc.console?.note(`When it says it is installed, run the code again (a restart is needed only if ${fix.package} was imported before).`));
    return true;
  }
  return false;
};

async function openGrid(doc: Doc, name: string): Promise<void> {
  if (!doc.kernel?.id) return;
  try {
    await api(`/workbench/kernels/${doc.kernel.id}/grid`, "POST", { name });
    toast(`${name} is open as a grid.`);
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

/** A frame in another file: open it in the Workbench at that line. */
async function openAt(path: string, line: number): Promise<void> {
  await open(path);
  const doc = docs.find((d) => d.path === path);
  if (doc?.script) setTimeout(() => doc.script?.reveal(line), 300);
}

/** F5 and the toolbar's main Run: the file, or every cell. */
async function runPrimary(doc: Doc | null): Promise<void> {
  if (doc?.script) await doc.script.runFile();
  else if (doc?.notebook) await doc.notebook.runAll();
}

function focusSource(): void {
  if (current?.script) current.script.focus();
  else if (current?.notebook) current.notebook.commandMode();
  else (docHost.querySelector("button, input, [tabindex]") as HTMLElement | null)?.focus();
}

function focusConsole(): void {
  current?.console?.focus();
}

async function showHelp(): Promise<void> {
  const text = (await (current?.script?.helpAtCursor() ?? current?.notebook?.helpAtCursor())) ?? "";
  layout.help("Help on the name at the cursor", text);
}

const markChanged = (doc: Doc) => (): void => {
  if (!doc.dirty) {
    doc.dirty = true;
    drawTabs();
  }
  remember();
};

function openNotebook(path: string | null, cells: CellData[], modified: number | null): Doc {
  const doc = makeDoc("notebook", path ? baseName(path) : `Untitled notebook ${counter + 1}`, path, modified);
  doc.notebook = notebookView(cells, doc.kernel!, () => connectionNames, () => effectiveTheme(), markChanged(doc), () => bladeNames, () => codeNames, { ...hooks, fix: pageFix(doc), openGrid: (name) => void openGrid(doc, name) });
  doc.element.append(doc.notebook.element);
  show(doc);
  return doc;
}

function openScript(path: string | null, text: string, modified: number | null): Doc {
  const doc = makeDoc("script", path ? baseName(path) : `Untitled script ${counter + 1}`, path, modified);
  const file = {
    path: () => doc.path,
    // Run file runs what is on disk: a changed file with a path is saved first (an untitled one runs
    // from a private copy).
    saveFirst: async (): Promise<boolean> => (!doc.path || !doc.dirty ? true : save(doc)),
  };
  doc.script = scriptView(text, doc.kernel!, doc.console!, () => effectiveTheme(), markChanged(doc), names, file, { ...hooks, fix: pageFix(doc), openFile: (path, line) => void openAt(path, line), openGrid: (name) => void openGrid(doc, name) });
  doc.element.append(doc.script.element);
  show(doc);
  return doc;
}

function show(doc: Doc): void {
  current = doc;
  variables.show(doc.kernel);
  docHost.replaceChildren(doc.element);
  layout.console.replaceChildren(doc.console?.element ?? h("p", { class: "hint wb-pane-empty" }, "A notebook or script has its console here."));
  cursor = { line: 1, column: 1 };
  drawTabs();
  drawToolbar();
  drawStatus();
  document.title = `${doc.title} · Workbench`;
  remember();
}

function close(doc: Doc): void {
  if (doc.dirty && !confirm(`${doc.title} has changes that are not saved. Close it anyway?`)) return;
  doc.kernel?.stop();
  docs.splice(docs.indexOf(doc), 1);
  remember();
  if (doc.kind === "blade") editingBlade = false;
  if (current === doc) {
    const next = docs[docs.length - 1];
    if (next) show(next);
    else showWelcome();
  } else drawTabs();
}

function drawTabs(): void {
  clear(
    tabs,
    ...docs.map((doc) =>
      h(
        "div",
        { class: `tab-item ${doc === current ? "active" : ""}`, role: "tab", "aria-selected": String(doc === current), tabindex: doc === current ? "0" : "-1", title: doc.path ?? doc.title, onclick: () => show(doc) },
        h("span", { class: "tab-label" }, `${doc.dirty ? "● " : ""}${doc.title}`),
        button(`Close ${doc.title}`, (e) => { e.stopPropagation(); close(doc); }, { icon: "close", iconOnly: true, class: "tab-close" }),
      ),
    ),
  );
}

// ===== files =====

async function open(path?: string): Promise<void> {
  const chosen = path ?? (await chooseFile({ title: "Open a notebook or script", mode: "file", suffixes: [".ipynb", ".py"], start: current?.path ?? homeFolder }));
  if (!chosen) return;
  const already = docs.find((d) => d.path === chosen);
  if (already) return show(already);
  try {
    const found = await api<{ path: string; kind: "notebook" | "script"; modified: number; cells?: CellData[]; text?: string; python: string | null }>("/workbench/open", "POST", { path: chosen });
    if (found.kind === "notebook") openNotebook(found.path, found.cells ?? [], found.modified);
    else openScript(found.path, found.text ?? "", found.modified);
    // Its own environment, as remembered (or the .venv beside it): the picker shows it, Run uses it.
    if (found.python && current) {
      current.python = found.python;
      if (!pythons.some((p) => p.python === found.python)) pythons.push({ python: found.python, version: "", ipykernel: true, swissarmygrid: true });
      drawToolbar();
    }
    void refreshHome();
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

/** Save a document (true: it is saved). */
async function save(doc: Doc | null, as = false): Promise<boolean> {
  if (!doc || (doc.kind !== "notebook" && doc.kind !== "script")) return false;
  let path = doc.path;
  if (!path || as) {
    const suggested = doc.path ? baseName(doc.path) : doc.kind === "notebook" ? "analysis.ipynb" : "script.py";
    path = await chooseFile({ title: doc.kind === "notebook" ? "Save the notebook" : "Save the script", mode: "save", start: doc.path ?? homeFolder, name: suggested });
    if (!path) return false;
    const ending = doc.kind === "notebook" ? ".ipynb" : ".py";
    if (!path.toLowerCase().endsWith(ending)) path += ending;
  }
  const body = doc.kind === "notebook" ? { cells: doc.notebook!.cells() } : { text: doc.script!.text() };
  const send = async (force: boolean): Promise<void> => {
    const saved = await api<{ path: string; modified: number }>("/workbench/save", "POST", { path, ...body, modified: as ? null : doc.modified, force });
    const first = doc.path === null;
    doc.path = saved.path;
    doc.modified = saved.modified;
    if (first && doc.python) void api("/workbench/environment", "POST", { path: saved.path, python: doc.python }).catch(() => undefined);
    doc.title = baseName(saved.path);
    doc.dirty = false;
    drawTabs();
    toast(`Saved ${saved.path}.`);
    remember();
    void refreshHome();
  };
  try {
    await send(false);
    return true;
  } catch (e) {
    if (e instanceof ApiError && e.status === 409 && confirm(`${e.message}\n\nReplace the file on disk with this one?`)) {
      return send(true).then(
        () => true,
        (err: Error) => (toast(err.message, "error"), false),
      );
    }
    toast((e as Error).message, "error");
    return false;
  }
}

let recent: { path: string; exists: boolean }[] = [];
async function refreshHome(): Promise<void> {
  try {
    const home = await api<{ folder: string; recent: { path: string; exists: boolean }[] }>("/workbench/home");
    homeFolder = home.folder;
    recent = home.recent;
  } catch {
    /* the server will say so on the next action */
  }
}

function recentMenu(anchor: HTMLElement): void {
  document.querySelector(".menu")?.remove();
  const box = anchor.getBoundingClientRect();
  const menu = h(
    "div",
    { class: "menu", role: "menu", "aria-label": "Recent files" },
    ...(recent.length
      ? recent.map((r) => h("button", { type: "button", role: "menuitem", disabled: !r.exists, title: r.exists ? r.path : `${r.path} (not there any more)`, onclick: () => { menu.remove(); void open(r.path); } }, baseName(r.path), h("span", { class: "hint" }, `  ${r.path}`)))
      : [h("div", { class: "menu-label" }, "Nothing opened yet.")]),
  );
  document.body.append(menu);
  menu.style.top = `${box.bottom + 4}px`;
  menu.style.left = `${Math.min(box.left, innerWidth - menu.offsetWidth - 8)}px`;
  (menu.querySelector("button:not(:disabled)") as HTMLButtonElement | null)?.focus();
  setTimeout(() => document.addEventListener("mousedown", function away(e) { if (!menu.contains(e.target as Node)) { menu.remove(); document.removeEventListener("mousedown", away); } }));
  menu.addEventListener("keydown", (e) => { if (e.key === "Escape") menu.remove(); });
}

// ===== toolbar =====

const STATE_WORDS: Record<string, string> = { off: "not started (starts on the first Run)", starting: "starting…", idle: "ready", busy: "running…" };

function folderOf(path: string | null): string {
  if (!path) return homeFolder;
  const at = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return at > 0 ? path.slice(0, at) : homeFolder;
}

/** Run in another Python from the next Run on (a running kernel is stopped first). */
function useEnvironment(doc: Doc, python: string): void {
  doc.python = python;
  if (!pythons.some((p) => p.python === python)) pythons.push({ python, version: "", ipykernel: true, swissarmygrid: true });
  if (doc.kernel?.id) {
    doc.kernel.stop();
    toast("The kernel stopped: the next Run starts in the chosen Python.");
  }
  if (doc.path) void api("/workbench/environment", "POST", { path: doc.path, python }).catch((e: Error) => toast(e.message, "error"));
  drawToolbar();
}

function pythonPicker(doc: Doc): HTMLElement {
  const select = h("select", { "aria-label": "Environment", title: "The Python this document's code runs in (remembered for it)" }) as HTMLSelectElement;
  const own = pythons[0];
  const label = (p: PythonChoice): string => {
    // An environment by its last two folders (project/.venv), the whole path in the option's title.
    const folder = p.python.replace(/[\\/](bin[\\/]python[\d.]*|Scripts[\\/]python\.exe)$/i, "").split(/[\\/]/).filter(Boolean);
    const where = p === own ? "This app's Python" : folder.slice(-2).join("/");
    return `${where}${p.version ? ` (${p.version})` : ""}${p.ipykernel ? "" : ", no ipykernel"}`;
  };
  for (const p of pythons) select.append(h("option", { value: p.python, title: p.python, selected: (doc.python ?? own?.python) === p.python }, label(p)));
  select.append(h("option", { value: "__new" }, "New environment…"), h("option", { value: "__other" }, "Another Python…"));
  select.addEventListener("change", async () => {
    if (select.value === "__new") {
      const made = await newEnvironment({ folder: folderOf(doc.path), document: doc.path, appVersion: own?.version ?? "" });
      if (made) {
        useEnvironment(doc, made.python);
        if (made.imports.length && doc.notebook) doc.notebook.add("code", made.imports.join("\n"), true);
      }
      drawToolbar();
      return;
    }
    if (select.value === "__other") {
      const path = await chooseFile({ title: "Choose a Python (python.exe in a project's .venv\\Scripts, or bin/python)", mode: "file", start: folderOf(doc.path) });
      if (path) useEnvironment(doc, path);
      drawToolbar();
      return;
    }
    useEnvironment(doc, select.value);
  });
  return select;
}

function drawToolbar(): void {
  const doc = current;
  const file = [
    button("Side bar", () => {
      layout.toggle("rail");
      drawToolbar();
    }, { icon: "panel", iconOnly: true, pressed: layout.state().rail, title: "Show or hide the side bar: connections, blades, the data explorer (Ctrl+B)" }),
    button("New notebook", () => openNotebook(null, [], null), { icon: "plus" }),
    button("New script", () => openScript(null, "", null), { icon: "code" }),
    button("Open…", () => void open(), { icon: "folder", title: "Open a notebook or script (Ctrl+O)" }),
    button("Recent", (e) => recentMenu(e.currentTarget as HTMLElement), { icon: "more" }),
    button("Save", () => void save(current), { icon: "save", title: "Save (Ctrl+S; Save as is Ctrl+Shift+S)" }),
  ];
  const palette = button("Commands", () => openPalette(), { icon: "steps", title: "Every command, with its keys (Ctrl+Shift+P)" });
  const right = button("Variables", () => {
    if (layout.toggle("right")) void variables.refresh();
    drawToolbar();
  }, { icon: "columns", pressed: layout.state().right, title: "Variables, Plots and Help on the right" });
  if (!doc || (doc.kind !== "notebook" && doc.kind !== "script")) {
    clear(toolbar, h("div", { class: "ribbon-tools" }, ...file), h("div", { class: "ribbon-tools" }, right, palette));
    return;
  }
  const kernel = doc.kernel!;
  const run =
    doc.kind === "notebook"
      ? [
          button("Python", () => doc.notebook!.add("code"), { icon: "plus", title: "Add a Python cell (B in command mode)" }),
          button("SQL", () => doc.notebook!.add("sql"), { icon: "sql", title: "Add a SQL cell, on a connection" }),
          button("Markdown", () => doc.notebook!.add("markdown"), { icon: "edit", title: "Add a Markdown cell" }),
          button("Run all", () => void doc.notebook!.runAll(), { icon: "play", class: "primary", title: "Run every cell, stopping at an error (F5)" }),
        ]
      : [
          button("Run file", () => void doc.script!.runFile(), { icon: "play", class: "primary", title: "Run the file as Python would (F5)" }),
          button("Run statement", () => void doc.script!.runSelection(), { icon: "play", title: "The whole statement at the cursor, then the next (Ctrl+Enter); or the selection" }),
        ];
  clear(
    toolbar,
    h("div", { class: "ribbon-tools" }, ...file),
    h(
      "div",
      { class: "ribbon-tools" },
      ...run,
      button("Interrupt", () => void kernel.interrupt(), { icon: "stop", title: "Stop the running code, and the runs waiting behind it" }),
      button("Restart", () => void restartKernel(kernel), { icon: "back", title: "A fresh Python: every variable is gone" }),
      pythonPicker(doc),
      right,
      palette,
    ),
  );
}

async function restartKernel(kernel: Kernel): Promise<boolean> {
  const did = await kernel.restart();
  toast(did ? "Restarted: every variable is gone." : "Nothing to restart: the kernel starts on the first Run.");
  return did;
}

/** The status bar: the kernel, the runs waiting, the environment, the cursor. */
function drawStatus(): void {
  const doc = current;
  const kernel = doc?.kernel;
  const python = doc?.python ?? pythons[0]?.python ?? "";
  const env = !doc?.python || doc.python === pythons[0]?.python ? "This app's Python" : python.replace(/[\\/](bin[\\/]python[\d.]*|Scripts[\\/]python\.exe)$/i, "").split(/[\\/]/).filter(Boolean).slice(-2).join("/");
  clear(
    layout.status,
    kernel
      ? h("span", { class: `wb-state ${kernel.state}` }, h("span", { class: "wb-dot", "aria-hidden": "true" }), `Kernel: ${STATE_WORDS[kernel.state]}`, kernel.running > 1 ? ` (${kernel.running - 1} more waiting)` : "")
      : h("span", { class: "hint" }, doc ? "No kernel: nothing here runs code" : "Nothing open"),
    kernel ? h("span", { title: python }, env) : null,
    doc?.kind === "script" || doc?.kind === "notebook" ? h("span", {}, `Line ${cursor.line}, column ${cursor.column}`) : null,
    doc ? h("span", {}, doc.kind === "notebook" ? "Notebook" : doc.kind === "script" ? "Python script" : doc.kind === "blade" ? "Blade" : "Explorer") : null,
    h("span", { class: "spacer" }),
    h("span", { class: "hint" }, "Ctrl+Shift+P: every command"),
  );
}

// ===== the left panel: connections, blades, the data explorer =====

function bladeDoc(name: string | null): void {
  const doc = makeDoc("blade", name ?? "New blade", null, null);
  editingBlade = true;
  const done = (): void => { editingBlade = false; close(doc); };
  const saved = (savedName: string): void => { editingBlade = false; rail.bladeSaved(savedName); void loadConnections(); };
  if (name !== null) void editBlade(doc.element, name, null, "other" as BladeKind, saved, done);
  else
    void showTemplatePicker(doc.element, (template, kind) => {
      void editBlade(doc.element, null, template, kind, saved, done);
    });
  show(doc);
}

const rail = mountRail({
  bladesHost: document.getElementById("blades") as HTMLElement,
  catalogHost: document.getElementById("catalog") as HTMLElement,
  editBlade: bladeDoc,
  isEditing: () => editingBlade,
  showTable: (view) => {
    let doc = docs.find((d) => d.kind === "table");
    if (!doc) doc = makeDoc("table", "Explorer", null, null);
    doc.title = view.table;
    clear(doc.element, ...tableDetails(view));
    show(doc);
  },
  connectionsByName: true,
  showSchema: (label, catalog) => {
    let doc = docs.find((d) => d.kind === "table");
    if (!doc) doc = makeDoc("table", "Explorer", null, null);
    doc.title = label;
    clear(doc.element, h("h2", {}, label), h("p", { class: "hint" }, `A schema in ${catalog}. Open it to see its tables and views.`));
    show(doc);
  },
});

// ===== restore after a reload =====

interface Remembered {
  kind: "notebook" | "script";
  path: string | null;
  modified: number | null;
  dirty: boolean;
  python: string | null;
  text?: string;
  cells?: CellData[];
}
const SESSION_KEY = "workbench-documents";
let rememberTimer: number | null = null;

/** Keep the open documents in this window's session storage (the browser's, never a file), so a
 *  reload or a crashed tab brings them back with their unsaved text. */
function rememberNow(): void {
  if (rememberTimer !== null) window.clearTimeout(rememberTimer);
  rememberTimer = null;
  const kept = (withOutputs: boolean): Remembered[] =>
    docs
      .filter((d) => d.notebook || d.script)
      .map((d) => ({
        kind: d.notebook ? "notebook" : "script",
        path: d.path,
        modified: d.modified,
        dirty: d.dirty,
        python: d.python,
        // A saved document comes back from its file; only changed ones keep their text here.
        ...(d.dirty || !d.path ? (d.notebook ? { cells: d.notebook.cells().map((c) => (withOutputs ? c : { ...c, outputs: [] })) } : { text: d.script!.text() }) : {}),
      }));
  const at = current ? docs.filter((d) => d.notebook || d.script).indexOf(current) : -1;
  try {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify({ docs: kept(true), current: at }));
  } catch {
    try {
      sessionStorage.setItem(SESSION_KEY, JSON.stringify({ docs: kept(false), current: at })); // too big with outputs
    } catch {
      /* storage refused: a reload starts empty, as before */
    }
  }
}

function remember(): void {
  if (rememberTimer === null) rememberTimer = window.setTimeout(rememberNow, 800);
}

/** The documents open before a reload (false: there were none). */
async function restore(): Promise<boolean> {
  type Saved = { docs: Remembered[]; current: number } | null;
  const read = (): Saved => {
    try {
      return JSON.parse(sessionStorage.getItem(SESSION_KEY) ?? "null") as Saved;
    } catch {
      return null;
    }
  };
  const saved = read();
  if (!saved?.docs?.length) return false;
  const opened: Doc[] = [];
  for (const r of saved.docs) {
    if (r.path && !r.dirty) {
      await open(r.path); // fresh from its file
      const reopened = docs.find((d) => d.path === r.path);
      if (reopened) opened.push(reopened);
      continue;
    }
    const doc = r.kind === "notebook" ? openNotebook(r.path, r.cells ?? [], r.modified) : openScript(r.path, r.text ?? "", r.modified);
    doc.python = r.python;
    if (r.python && !pythons.some((p) => p.python === r.python)) pythons.push({ python: r.python, version: "", ipykernel: true, swissarmygrid: true });
    doc.dirty = r.dirty;
    opened.push(doc);
  }
  const back = opened[saved.current] ?? opened[opened.length - 1];
  if (back) show(back);
  drawTabs();
  return opened.length > 0;
}

// ===== start =====

function showWelcome(): void {
  current = null;
  variables.show(null);
  layout.console.replaceChildren(h("p", { class: "hint wb-pane-empty" }, "A notebook or script has its console here."));
  drawTabs();
  drawToolbar();
  drawStatus();
  document.title = "Workbench";
  clear(
    docHost,
    h(
      "div",
      { class: "wb-welcome" },
      h("h1", {}, "Workbench"),
      h("p", {}, "Notebooks and Python scripts, run in a Python on this computer. Drag a connection, a blade or a table from the left into a cell or the script."),
      h("div", { class: "row-actions" }, button("New notebook", () => openNotebook(null, [], null), { icon: "plus", class: "primary" }), button("New script", () => openScript(null, "", null), { icon: "code" }), button("Open…", () => void open(), { icon: "folder" })),
      recent.length ? h("h3", {}, "Recent") : null,
      recent.length ? h("ul", { class: "wb-recent" }, ...recent.map((r) => h("li", {}, h("button", { type: "button", class: "link", disabled: !r.exists, onclick: () => void open(r.path) }, baseName(r.path)), h("span", { class: "hint" }, ` ${r.path}`)))) : null,
      h("p", { class: "hint" }, "A SQL cell runs on a connection: its own SQL for a database, DuckDB over a file's tables. The same works in Jupyter: %%sql <connection>."),
    ),
  );
}

// ===== commands: the palette, the keys and the agent (workbench_window) =====

const isCode = (): boolean => !!current?.notebook || !!current?.script;
const isScript = (): boolean => !!current?.script;
const isNotebook = (): boolean => !!current?.notebook;
const notebook = (): NotebookView => {
  if (!current?.notebook) throw new Error("The document in front is not a notebook.");
  return current.notebook;
};
const script = (): ScriptView => {
  if (!current?.script) throw new Error("The document in front is not a script.");
  return current.script;
};
const kernelOf = (): Kernel => {
  if (!current?.kernel) throw new Error("The document in front runs no code.");
  return current.kernel;
};
const num = (args: Record<string, unknown>, name: string): number | undefined => {
  const value = args[name];
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "number" || !Number.isInteger(value)) throw new Error(`${name} is a whole number.`);
  return value;
};
const text = (args: Record<string, unknown>, name: string): string | undefined => {
  const value = args[name];
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "string") throw new Error(`${name} is text.`);
  return value;
};
const kindArg = (args: Record<string, unknown>): "code" | "sql" | "markdown" => {
  const kind = text(args, "kind") ?? "code";
  if (kind !== "code" && kind !== "sql" && kind !== "markdown") throw new Error('kind is "code", "sql" or "markdown".');
  return kind;
};
const step = (by: 1 | -1): void => {
  if (!current || docs.length < 2) return;
  show(docs[(docs.indexOf(current) + by + docs.length) % docs.length]!);
};

/** What the window holds: for the agent's "documents" (and the palette's nothing). */
function documents(): unknown {
  return {
    documents: docs.map((d, index) => ({
      index,
      kind: d.kind,
      title: d.title,
      path: d.path,
      unsaved_changes: d.dirty,
      in_front: d === current,
      kernel: d.kernel ? { id: d.kernel.id, state: d.kernel.state, runs_waiting: Math.max(0, d.kernel.running - 1) } : null,
      python: d.python,
      selected_cell: d.notebook ? d.notebook.selected() : null,
    })),
    panes: layout.state(),
    cursor: current?.script || current?.notebook ? cursor : null,
  };
}

const COMMANDS: Command[] = [
  // File
  { id: "file.newNotebook", group: "File", title: "New notebook", run: () => void openNotebook(null, [], null) },
  { id: "file.newScript", group: "File", title: "New script", run: () => void openScript(null, "", null) },
  { id: "file.open", group: "File", title: "Open a notebook or script…", keys: "Ctrl+O", args: "path: the file (without it, the file chooser opens)", run: (a) => open(text(a, "path")) },
  { id: "file.save", group: "File", title: "Save", keys: "Ctrl+S", when: isCode, whyNot: "nothing that saves is in front", run: () => save(current) },
  { id: "file.saveAs", group: "File", title: "Save as…", keys: "Ctrl+Shift+S", when: isCode, whyNot: "nothing that saves is in front", run: () => save(current, true) },
  { id: "file.close", group: "File", title: "Close the document", args: "index: a document (default: the one in front)", when: () => docs.length > 0, whyNot: "nothing is open", run: (a) => {
    const at = num(a, "index");
    const doc = at === undefined ? current : docs[at];
    if (!doc) throw new Error(`There is no document ${at}.`);
    close(doc);
  } },
  // Documents
  { id: "documents", agentOnly: true, group: "Documents", title: "What is open", run: () => documents() },
  { id: "doc.read", agentOnly: true, group: "Documents", title: "The front document's text or cells", when: isCode, whyNot: "no notebook or script is in front", run: () => (current?.notebook ? { kind: "notebook", cells: current.notebook.cells() } : { kind: "script", text: script().text() }) },
  { id: "doc.show", group: "Documents", title: "Show a document", args: "index, or path", run: (a) => {
    const at = num(a, "index");
    const path = text(a, "path");
    const doc = at !== undefined ? docs[at] : docs.find((d) => d.path === path);
    if (!doc) throw new Error("No open document matches.");
    show(doc);
  } },
  { id: "doc.next", group: "Documents", title: "Next document", keys: "Ctrl+Alt+PageDown", run: () => step(1) },
  { id: "doc.previous", group: "Documents", title: "Previous document", keys: "Ctrl+Alt+PageUp", run: () => step(-1) },
  // Run
  { id: "run.primary", group: "Run", title: "Run the file, or every cell", keys: "F5", when: isCode, whyNot: "no notebook or script is in front", run: () => runPrimary(current) },
  { id: "run.statement", group: "Run", title: "Run the statement at the cursor, then the next", keys: "Ctrl+Enter", when: isScript, whyNot: "the document in front is not a script", run: () => script().runSelection() },
  { id: "run.toHere", group: "Run", title: "Run from the top to the cursor", when: isScript, whyNot: "the document in front is not a script", run: () => script().runToHere() },
  { id: "run.fromHere", group: "Run", title: "Run from the cursor to the end", when: isScript, whyNot: "the document in front is not a script", run: () => script().runFromHere() },
  { id: "run.cell", group: "Run", title: "Run the selected cell", keys: "Ctrl+Enter", args: "index: a cell, from 0 (default: the selected one)", when: isNotebook, whyNot: "the document in front is not a notebook", run: (a) => notebook().runCell(num(a, "index")) },
  { id: "run.above", group: "Run", title: "Run the cells above", when: isNotebook, whyNot: "the document in front is not a notebook", run: () => notebook().runAbove() },
  { id: "run.below", group: "Run", title: "Run this cell and the ones below", when: isNotebook, whyNot: "the document in front is not a notebook", run: () => notebook().runBelow() },
  { id: "run.interrupt", group: "Run", title: "Interrupt (and cancel the runs waiting)", when: isCode, whyNot: "no notebook or script is in front", run: () => kernelOf().interrupt() },
  { id: "run.restart", group: "Run", title: "Restart the kernel", when: isCode, whyNot: "no notebook or script is in front", run: () => restartKernel(kernelOf()) },
  // Cells
  { id: "cell.insertAbove", group: "Cell", title: "Insert a cell above", keys: "A", args: "kind: code, sql or markdown; source", when: isNotebook, whyNot: "the document in front is not a notebook", run: (a) => notebook().insert("above", kindArg(a), text(a, "source") ?? "") },
  { id: "cell.insertBelow", group: "Cell", title: "Insert a cell below", keys: "B", args: "kind: code, sql or markdown; source", when: isNotebook, whyNot: "the document in front is not a notebook", run: (a) => notebook().insert("below", kindArg(a), text(a, "source") ?? "") },
  { id: "cell.select", group: "Cell", title: "Select a cell", args: "index (from 0); edit: true to put the cursor in it", when: isNotebook, whyNot: "the document in front is not a notebook", run: (a) => notebook().select(num(a, "index") ?? 0, a.edit === true) },
  { id: "cell.delete", group: "Cell", title: "Delete the cell", keys: "D D", args: "index (default: the selected one)", when: isNotebook, whyNot: "the document in front is not a notebook", run: (a) => notebook().remove(num(a, "index")) },
  { id: "cell.undo", group: "Cell", title: "Undo a cell's delete or move", keys: "Z", when: isNotebook, whyNot: "the document in front is not a notebook", run: () => notebook().undo() },
  { id: "cell.moveUp", group: "Cell", title: "Move the cell up", when: isNotebook, whyNot: "the document in front is not a notebook", run: () => notebook().move(-1) },
  { id: "cell.moveDown", group: "Cell", title: "Move the cell down", when: isNotebook, whyNot: "the document in front is not a notebook", run: () => notebook().move(1) },
  { id: "cell.toCode", group: "Cell", title: "Make it a Python cell", keys: "Y", when: isNotebook, whyNot: "the document in front is not a notebook", run: () => notebook().setKind("code") },
  { id: "cell.toMarkdown", group: "Cell", title: "Make it a Markdown cell", keys: "M", when: isNotebook, whyNot: "the document in front is not a notebook", run: () => notebook().setKind("markdown") },
  { id: "cell.toSql", group: "Cell", title: "Make it a SQL cell", keys: "S", when: isNotebook, whyNot: "the document in front is not a notebook", run: () => notebook().setKind("sql") },
  { id: "cell.duplicate", group: "Cell", title: "Duplicate the cell", when: isNotebook, whyNot: "the document in front is not a notebook", run: () => notebook().duplicate() },
  { id: "cell.clearOutput", group: "Cell", title: "Clear the cell's output", args: "index (default: the selected one)", when: isNotebook, whyNot: "the document in front is not a notebook", run: (a) => notebook().clearOutput(num(a, "index")) },
  { id: "cell.clearAll", group: "Cell", title: "Clear every output", when: isNotebook, whyNot: "the document in front is not a notebook", run: () => notebook().clearAll() },
  { id: "cell.toggleOutput", group: "Cell", title: "Hide or show the cell's output", keys: "O", when: isNotebook, whyNot: "the document in front is not a notebook", run: () => notebook().toggleOutput() },
  // Console
  { id: "console.focus", group: "Console", title: "Go to the console", keys: "Ctrl+2", when: isCode, whyNot: "no notebook or script is in front", run: () => layout.focus("console") },
  { id: "console.run", group: "Console", title: "Run code in the console", args: "code", when: isCode, whyNot: "no notebook or script is in front", run: (a) => {
    const code = text(a, "code");
    if (!code?.trim()) throw new Error("Send the code to run.");
    return current!.console!.run({ code }, code.split("\n").map((l, i) => `${i ? "..." : ">>>"} ${l}`).join("\n"));
  } },
  { id: "console.read", agentOnly: true, group: "Console", title: "What the console shows", when: isCode, whyNot: "no notebook or script is in front", run: () => current!.console!.text() },
  { id: "console.clear", group: "Console", title: "Clear the console", when: isCode, whyNot: "no notebook or script is in front", run: () => current!.console!.clear() },
  // View
  { id: "palette", group: "View", title: "Show every command", keys: "Ctrl+Shift+P", run: () => openPalette() },
  { id: "view.source", group: "View", title: "Go to the source", keys: "Ctrl+1", run: () => layout.focus("source") },
  { id: "view.variables", group: "View", title: "Go to Variables", keys: "Ctrl+3", run: () => layout.focus("variables") },
  { id: "view.lower", group: "View", title: "Go to Plots and Help", keys: "Ctrl+4", run: () => layout.focus("lower") },
  { id: "view.nextPane", group: "View", title: "Next pane", keys: "F6", run: () => layout.cycle(1) },
  { id: "view.previousPane", group: "View", title: "Previous pane", keys: "Shift+F6", run: () => layout.cycle(-1) },
  { id: "view.toggleSidebar", group: "View", title: "Show or hide the side bar", keys: "Ctrl+B", run: () => { layout.toggle("rail"); drawToolbar(); } },
  { id: "view.toggleRight", group: "View", title: "Show or hide Variables, Plots and Help", run: () => { if (layout.toggle("right")) void variables.refresh(); drawToolbar(); } },
  { id: "view.toggleConsole", group: "View", title: "Show or hide the console", run: () => layout.toggle("console") },
  { id: "view.standard", group: "View", title: "Layout: standard (four panes)", run: () => { layout.setLayout("standard"); drawToolbar(); } },
  { id: "view.focus", group: "View", title: "Layout: the source alone", keys: "Ctrl+Shift+1", run: () => { layout.setLayout("focus"); drawToolbar(); } },
  { id: "view.wide", group: "View", title: "Layout: wide data (the right column wider)", run: () => { layout.setLayout("wide"); drawToolbar(); } },
  { id: "view.plots", group: "View", title: "Show Plots", run: () => { layout.toggle("right", true); layout.showTab("plots"); drawToolbar(); } },
  { id: "view.variablesRefresh", group: "View", title: "Read the variables again", when: isCode, whyNot: "no notebook or script is in front", run: () => variables.refresh().then(() => variables.snapshot()) },
  // Help
  { id: "help.atCursor", group: "Help", title: "Help on the name at the cursor", keys: "F1", when: isCode, whyNot: "no notebook or script is in front", run: () => showHelp() },
  { id: "commands", agentOnly: true, group: "Help", title: "Every command and its arguments", run: () => allCommands().map((c) => ({ id: c.id, title: c.title, group: c.group, keys: c.keys ?? null, args: c.args ?? null, available: c.when ? c.when() : true })) },
];
registerCommands(COMMANDS);

/** The keys the window takes wherever the focus is (before an editor or the browser sees them). */
const WINDOW_KEYS: Record<string, string> = {
  F5: "run.primary",
  F1: "help.atCursor",
  F6: "view.nextPane",
  "Shift+F6": "view.previousPane",
  "Ctrl+Shift+P": "palette",
  "Ctrl+S": "file.save",
  "Ctrl+Shift+S": "file.saveAs",
  "Ctrl+O": "file.open",
  "Ctrl+B": "view.toggleSidebar",
  "Ctrl+1": "view.source",
  "Ctrl+2": "console.focus",
  "Ctrl+3": "view.variables",
  "Ctrl+4": "view.lower",
  "Ctrl+Shift+1": "view.focus",
  "Ctrl+Alt+PageDown": "doc.next",
  "Ctrl+Alt+PageUp": "doc.previous",
};

function keyName(e: KeyboardEvent): string {
  const key = /^Digit\d$/.test(e.code) ? e.code.slice(5) : e.key.length === 1 ? e.key.toUpperCase() : e.key;
  return [e.ctrlKey || e.metaKey ? "Ctrl" : "", e.altKey ? "Alt" : "", e.shiftKey ? "Shift" : "", key].filter(Boolean).join("+");
}

document.addEventListener(
  "keydown",
  (e) => {
    const id = WINDOW_KEYS[keyName(e)];
    if (!id) return;
    // F5 would reload the window (stopping every kernel), F1 open the browser's help: the window's own.
    e.preventDefault();
    e.stopPropagation();
    if (document.querySelector(".wb-palette-shade") && id !== "palette") return;
    void runCommand(id).catch((error: Error) => toast(error.message, "error"));
  },
  true,
);
document.addEventListener("keydown", (e) => {
  // Ctrl+Z outside an editor: a notebook's cell delete or move comes back (inside, the editor's own undo).
  const inEditor = (e.target as HTMLElement | null)?.closest?.(".monaco-editor, input, textarea, select");
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === "z" && !inEditor && current?.notebook) {
    if (current.notebook.undo()) e.preventDefault();
  }
});
window.addEventListener("beforeunload", (e) => {
  if (docs.some((d) => d.dirty)) e.preventDefault();
});
window.addEventListener("pagehide", () => {
  rememberNow();
  for (const d of docs) d.kernel?.stop(true);
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  for (const d of docs) {
    (d.notebook ?? d.script)?.setTheme(effectiveTheme());
    d.console?.setTheme(effectiveTheme());
  }
});

// The left panel's lists, and kept current: a blade or connection saved anywhere, a catalog refreshed.
void rail.loadBlades().then(() => rail.loadCatalogs());
connect((live) => {
  if (!live) return;
  void loadApprovals(); // an agent's request (a new blade, say) asks here too
  if (document.hasFocus()) tellFocus();
}, { catalog: "1", workbench: "1" });
// The agent's workbench_window goes to the Workbench window the user was last in.
window.addEventListener("focus", tellFocus);
onNotice((notice) => {
  handleAgentNotice(notice);
  // The agent's workbench_window: a command run here, as the palette would, and answered.
  if (notice.type === "workbench_command") void answerAgent(notice as unknown as { id: string; command: string; args?: Record<string, unknown> });
  if (notice.type === "catalogs_changed") {
    void rail.loadCatalogs();
    forgetCatalogs(); // completion asks the catalogs again
  }
  if (notice.type === "blades_changed") {
    void rail.loadBlades();
    void loadConnections();
  }
});

void Promise.all([
  refreshHome(),
  loadConnections(),
  api<{ pythons: PythonChoice[] }>("/workbench/pythons").then((r) => (pythons = r.pythons), () => undefined),
]).then(() => {
  showWelcome();
  void restore().then(() => {
    const wanted = openedWith.get("open"); // the address as opened (the page clears it once the token is read)
    if (wanted) void open(wanted);
  });
});
