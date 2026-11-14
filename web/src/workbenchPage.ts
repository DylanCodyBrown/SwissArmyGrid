/**
 * workbenchPage.ts: the Workbench, in a window of its own (Plans/workbench-and-connections.plan.md;
 * opened from the grid page's Workbench tab). [COMPOSITION]
 *
 *   [LEFT]     the rail (catalogRail.ts): Connections and Blades (drag one into a cell or the script for
 *              its line; Add connection; a new blade), and the data explorer (each connection's catalog:
 *              drag a table or column for its name)
 *   [FILE]     New notebook, New script, Open…, Recent ▾, Save, Save as… (the file chooser: real paths)
 *   [TABS]     the open documents (a dot when changed); closing one stops its kernel
 *   [RUN]      the Python the kernel runs in (this server's own, or a project's environment), the
 *              kernel's state, Run all or Run file, Interrupt, Restart
 *   [DOC]      a notebook (workbenchNotebook.ts) or a script with its console (workbenchScript.ts); a
 *              blade being edited; a table from the explorer
 *
 * ROUTING: /workbench/home, /open, /save, /pythons (routes_workbench.py); kernels in workbenchKernel.ts.
 */
import { handleAgentNotice, loadApprovals } from "./agentAccess";
import { api, ApiError, connect, onNotice, openedWith } from "./api";
import { type BladeKind, editBlade, showTemplatePicker } from "./bladeEditor";
import { mountRail, tableDetails, watchTheme } from "./catalogRail";
import { button, clear, h, toast } from "./dom";
import { chooseFile } from "./fileChooser";
import { effectiveTheme } from "./theme";
import { forgetCatalogs } from "./workbenchCompletion";
import { newEnvironment } from "./workbenchEnvironment";
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
}

interface PythonChoice {
  python: string;
  version: string;
  ipykernel: boolean;
  swissarmygrid: boolean;
}

const tabs = document.getElementById("wb-tabs") as HTMLElement;
const docHost = document.getElementById("wb-doc") as HTMLElement;
const toolbar = document.getElementById("wb-toolbar") as HTMLElement;
// The Variables overlay, beside the document (the toolbar's Variables shows or hides it).
const variables = variablesPanel();
(document.getElementById("wb-body") as HTMLElement).append(variables.element);
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

function makeDoc(kind: Doc["kind"], title: string, path: string | null, modified: number | null): Doc {
  const doc: Doc = { id: ++counter, kind, title, path, modified, dirty: false, kernel: null, python: null, element: h("div", { class: `wb-doc-body kind-${kind}` }) };
  if (kind === "notebook" || kind === "script") {
    doc.kernel = new Kernel(() => doc.path, () => doc.python);
    doc.kernel.onState(() => { if (doc === current) drawToolbar(); });
  }
  docs.push(doc);
  return doc;
}

const markChanged = (doc: Doc) => (): void => {
  if (!doc.dirty) {
    doc.dirty = true;
    drawTabs();
  }
};

function openNotebook(path: string | null, cells: CellData[], modified: number | null): void {
  const doc = makeDoc("notebook", path ? baseName(path) : `Untitled notebook ${counter + 1}`, path, modified);
  doc.notebook = notebookView(cells, doc.kernel!, () => connectionNames, () => effectiveTheme(), markChanged(doc), () => bladeNames, () => codeNames);
  doc.element.append(doc.notebook.element);
  show(doc);
}

function openScript(path: string | null, text: string, modified: number | null): void {
  const doc = makeDoc("script", path ? baseName(path) : `Untitled script ${counter + 1}`, path, modified);
  doc.script = scriptView(text, doc.kernel!, () => effectiveTheme(), markChanged(doc), () => ({ connections: connectionNames, blades: bladeNames, codeNames }));
  doc.element.append(doc.script.element);
  show(doc);
}

function show(doc: Doc): void {
  current = doc;
  variables.show(doc.kernel);
  docHost.replaceChildren(doc.element);
  drawTabs();
  drawToolbar();
  document.title = `${doc.title} · Workbench`;
}

function close(doc: Doc): void {
  if (doc.dirty && !confirm(`${doc.title} has changes that are not saved. Close it anyway?`)) return;
  doc.kernel?.stop();
  docs.splice(docs.indexOf(doc), 1);
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

async function save(doc: Doc | null, as = false): Promise<void> {
  if (!doc || (doc.kind !== "notebook" && doc.kind !== "script")) return;
  let path = doc.path;
  if (!path || as) {
    const suggested = doc.path ? baseName(doc.path) : doc.kind === "notebook" ? "analysis.ipynb" : "script.py";
    path = await chooseFile({ title: doc.kind === "notebook" ? "Save the notebook" : "Save the script", mode: "save", start: doc.path ?? homeFolder, name: suggested });
    if (!path) return;
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
    void refreshHome();
  };
  try {
    await send(false);
  } catch (e) {
    if (e instanceof ApiError && e.status === 409 && confirm(`${e.message}\n\nReplace the file on disk with this one?`)) {
      await send(true).catch((err: Error) => toast(err.message, "error"));
    } else toast((e as Error).message, "error");
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
    const where = p === own ? "This app's Python" : p.python.replace(/[\\/](bin[\\/]python|Scripts[\\/]python\.exe)$/i, "");
    return `${where}${p.version ? ` (${p.version})` : ""}${p.ipykernel ? "" : ", no ipykernel"}`;
  };
  for (const p of pythons) select.append(h("option", { value: p.python, selected: (doc.python ?? own?.python) === p.python }, label(p)));
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
    button("New notebook", () => openNotebook(null, [], null), { icon: "plus" }),
    button("New script", () => openScript(null, "", null), { icon: "code" }),
    button("Open…", () => void open(), { icon: "folder" }),
    button("Recent", (e) => recentMenu(e.currentTarget as HTMLElement), { icon: "more" }),
    button("Save", () => void save(current), { icon: "save" }),
    button("Save as…", () => void save(current, true), { icon: "save" }),
  ];
  if (!doc || (doc.kind !== "notebook" && doc.kind !== "script")) {
    clear(toolbar, h("div", { class: "ribbon-tools" }, ...file));
    return;
  }
  const kernel = doc.kernel!;
  const run =
    doc.kind === "notebook"
      ? [
          button("Python", () => doc.notebook!.add("code"), { icon: "plus", title: "Add a Python cell" }),
          button("SQL", () => doc.notebook!.add("sql"), { icon: "sql", title: "Add a SQL cell (on a connection)" }),
          button("Markdown", () => doc.notebook!.add("markdown"), { icon: "edit", title: "Add a Markdown cell" }),
          button("Run all", () => void doc.notebook!.runAll(), { icon: "play", class: "primary" }),
        ]
      : [
          button("Run file", () => void doc.script!.runFile(), { icon: "play", class: "primary", title: "Run the whole script (F5)" }),
          button("Run selection", () => void doc.script!.runSelection(), { icon: "play", title: "The selected lines, or the cursor's line (Ctrl+Enter)" }),
        ];
  clear(
    toolbar,
    h("div", { class: "ribbon-tools" }, ...file),
    h(
      "div",
      { class: "ribbon-tools" },
      ...run,
      button("Interrupt", () => void kernel.interrupt(), { icon: "stop", title: "Stop the running code (as Ctrl+C would)" }),
      button("Variables", (e) => {
        const open = variables.toggle();
        (e.currentTarget as HTMLElement).setAttribute("aria-pressed", String(open));
      }, { icon: "columns", pressed: !variables.element.hidden, title: "What the kernel holds now: your variables, the connections, the environment" }),
      button("Restart", () => void kernel.restart().then(() => toast("Restarted: every variable is gone.")), { icon: "back", title: "A fresh Python: every variable is gone" }),
      pythonPicker(doc),
      h("span", { class: `wb-state ${kernel.state}`, role: "status" }, STATE_WORDS[kernel.state]),
    ),
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

// ===== start =====

function showWelcome(): void {
  current = null;
  variables.show(null);
  drawTabs();
  drawToolbar();
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

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    void save(current, e.shiftKey);
  } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "o") {
    e.preventDefault();
    void open();
  }
});
window.addEventListener("beforeunload", (e) => {
  if (docs.some((d) => d.dirty)) e.preventDefault();
});
window.addEventListener("pagehide", () => {
  for (const d of docs) d.kernel?.stop(true);
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  for (const d of docs) (d.notebook ?? d.script)?.setTheme(effectiveTheme());
});

// The left panel's lists, and kept current: a blade or connection saved anywhere, a catalog refreshed.
void rail.loadBlades().then(() => rail.loadCatalogs());
connect((live) => {
  if (live) void loadApprovals(); // an agent's request (a new blade, say) asks here too
}, { catalog: "1" });
onNotice((notice) => {
  handleAgentNotice(notice);
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
  const wanted = openedWith.get("open"); // the address as opened (the page clears it once the token is read)
  if (wanted) void open(wanted);
});
