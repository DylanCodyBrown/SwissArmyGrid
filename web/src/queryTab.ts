/**
 * queryTab.ts: the "+" tab: a new table from one SELECT over the open tables. [WIRE + EXPORT]
 *
 * The "+" after the last tab opens a draft: a SQL tray over a blank grid. A table name in the
 * query reads that table as its tab shows it, after its steps (a table with no tab open reads its
 * rows as loaded). Run stores the result as a new dataset (POST /datasets/{name}/query), and the
 * draft becomes an ordinary grid tab on it, with every tool a pushed table has.
 *
 * That tab keeps the tray, so the query can change and Run again: the table is rebuilt in place
 * and the tab's own steps apply to the new rows. The tray is the table's source, like a view
 * definition; the steps are everything after it, and the SQL box in Table Transformations is one
 * more step on top.
 *
 * ROUTING: [TRAY TOGGLE]  -> setOpen()                  (one row, or the editor too)
 *          [TABLE NAME]   -> panes.renameDraft()        (the draft only; a built table keeps its name)
 *          [RUN]          -> runDraft() / runAgain()    (Ctrl+Enter in the editor too)
 *          [STATUS]       -> report(): the rows built, or the server's reason
 *          [EDITOR]       Monaco (a text box if it fails to load); autocomplete -> refreshNames()
 *          [BLANK GRID]   -> read-only, empty until the draft's first Run
 */
import { createGrid, type GridApi } from "ag-grid-community";
import { api } from "./api";
import { button, clear, formatCount, h, icon, toast } from "./dom";
import { contextOf, currentDraft, finishDraft, gridList, onAgentView, onPaneCreated, onPaneDropped, renameDraft, viewingQuery, type Context } from "./panes";
import { ensureMonacoCss } from "./sqlBox";
import { effectiveTheme, gridTheme, onThemeChange } from "./theme";
import type { SqlEditor } from "./lazy/sqlEditor";

/** What POST /datasets/{name}/query answers. */
interface Built {
  grid: string;
  dataset: string;
  version: number;
  rows: number;
}

interface Tray {
  element: HTMLElement;
  editor: SqlEditor;
  report: (message: string, error?: boolean) => void;
  busy: (on: boolean) => void;
}

const NAME = /^[A-Za-z0-9_-]{1,64}$/;

let host: HTMLElement;
let draftTray: Promise<Tray> | null = null;
let nameInput: HTMLInputElement;
let blankGrid: GridApi | null = null;
let drawnSerial = 0;
// Monaco has one theme for every editor, so any editor can switch it.
let lastEditor: SqlEditor | null = null;
// Each built table's tray, by grid id, so a closed tab frees its editor.
const trays = new Map<string, Promise<Tray>>();

export function mountQueryTab(element: HTMLElement): void {
  host = element;
  onAgentView(() => { if (viewingQuery()) void showDraft(); });
  onPaneCreated(attachTray);
  onPaneDropped((ctx) => dropTray(ctx.id));
  onThemeChange(() => {
    lastEditor?.setTheme(effectiveTheme());
    blankGrid?.setGridOption("theme", gridTheme());
  });
}

// ===== THE DRAFT: a tray over a blank grid, until its first Run =====

async function showDraft(): Promise<void> {
  const draft = currentDraft();
  if (!draft) return;
  draftTray ??= buildDraft();
  const tray = await draftTray;
  if (drawnSerial !== draft.serial) {
    // A new draft: its own name, and a fresh query (a closed draft's text is not carried over).
    drawnSerial = draft.serial;
    nameInput.value = draft.name;
    tray.editor.setValue(firstQuery());
    tray.report(hint());
  }
  tray.editor.focus();
  await refreshNames(tray.editor);
}

async function buildDraft(): Promise<Tray> {
  nameInput = h("input", {
    type: "text",
    class: "query-name mono",
    "aria-label": "Name of the new table",
    spellcheck: "false",
    maxlength: "64",
    oninput: () => renameDraft(nameInput.value.trim()),
  });
  const tray = await createTray("", () => void runDraft(), hint(), nameInput);
  const gridHost = h("div", { class: "query-blank" });
  clear(
    host,
    // ===== QUERY TRAY =====
    tray.element,
    // ===== BLANK GRID =====
    gridHost,
  );
  blankGrid = createGrid<unknown[]>(gridHost, {
    theme: gridTheme(),
    columnDefs: [],
    rowData: [],
    // Fixed text: no data reaches this template.
    overlayNoRowsTemplate: "<span>Write a query in the tray and Run it: the result becomes this table.</span>",
  });
  return tray;
}

async function runDraft(): Promise<void> {
  const tray = await draftTray!;
  const name = nameInput.value.trim();
  if (!NAME.test(name)) {
    tray.report("Name the new table with 1 to 64 letters, digits, _ or -.", true);
    nameInput.focus();
    return;
  }
  const built = await build(tray, name, false);
  if (!built) return;
  toast(`Built ${built.dataset}: ${rowsText(built.rows)}.`);
  await finishDraft(built.grid, built.dataset);
}

// ===== A BUILT TABLE'S PANE: its tray, to change the query and Run again =====

function attachTray(ctx: Context): void {
  const slot = ctx.pane!.querySelector<HTMLElement>(".query-slot")!;
  let loaded: string | null = null; // the query the tray last took from the server
  const sync = async (): Promise<void> => {
    const query = ctx.state.info?.query ?? null;
    if (query === null) {
      // Not a query's table (or a push under its name replaced it): no tray.
      clear(slot);
      dropTray(ctx.id);
      loaded = null;
      return;
    }
    const tray = trays.get(ctx.id);
    if (!tray) {
      loaded = query;
      const pending = createTray(query, () => void pending.then((t) => runAgain(ctx, t)), hint(ctx.dataset));
      trays.set(ctx.id, pending);
      const created = await pending;
      if (trays.get(ctx.id) === pending) slot.append(created.element);
      await refreshNames(created.editor);
      return;
    }
    // Rebuilt (here, or from another tab on the same table): show the query that built it.
    if (query !== loaded) {
      loaded = query;
      const current = await tray;
      if (current.editor.value() !== query) current.editor.setValue(query);
    }
  };
  ctx.state.onOwn("info", () => void sync());
  void sync();
}

/** Free a tray's editor: its tab closed, or its table is no longer a query's. */
function dropTray(id: string): void {
  void trays.get(id)?.then((tray) => tray.editor.dispose());
  trays.delete(id);
}

async function runAgain(ctx: Context, tray: Tray): Promise<void> {
  // The server tells every page (dataset_updated); main.ts reloads this grid, steps and all.
  await build(tray, ctx.dataset, true);
}

// ===== THE TRAY =====

/** A bar (collapse, the draft's name, the green Run, the status) over the SQL editor. */
async function createTray(initial: string, onRun: () => void, firstStatus: string, name?: HTMLInputElement): Promise<Tray> {
  const element = h("section", { class: "query-tray", "data-open": "true", "aria-label": "The query that builds this table" });
  const editorHost = h("div", { class: "sql-editor" });
  const status = h("span", { class: "sql-status", role: "status" }, firstStatus);
  const run = button("Run", onRun, { icon: "play", class: "run", title: "Build the table from this query (Ctrl+Enter)" });
  const toggle = button("Hide the query", () => setOpen(element.dataset.open !== "true"), { icon: "up", iconOnly: true, class: "ghost" });
  toggle.setAttribute("aria-expanded", "true");
  const setOpen = (open: boolean): void => {
    const label = open ? "Hide the query" : "Show the query";
    element.dataset.open = String(open);
    toggle.replaceChildren(icon(open ? "up" : "down"));
    toggle.setAttribute("aria-label", label);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.title = label;
  };
  element.append(h("div", { class: "query-tray-bar" }, toggle, h("strong", {}, name ? "New table" : "Query"), name ?? null, run, status), editorHost);
  const editor = await openEditor(editorHost, initial, onRun);
  lastEditor = editor;
  return {
    element,
    editor,
    report: (message, error = false) => {
      status.textContent = message;
      status.className = error ? "sql-status error" : "sql-status";
    },
    busy: (on) => {
      run.disabled = on;
      if (on) {
        status.textContent = "Running…";
        status.className = "sql-status";
      }
    },
  };
}

/** Monaco when it loads (it is optional), else a plain text box that does the same job. */
async function openEditor(editorHost: HTMLElement, initial: string, onRun: () => void): Promise<SqlEditor> {
  try {
    const m = (await import(/* external */ "/static/build/lazy/sqlEditor.js" as string)) as typeof import("./lazy/sqlEditor");
    ensureMonacoCss();
    return m.createSqlEditor(editorHost, initial, onRun, effectiveTheme());
  } catch {
    const box = h("textarea", { "aria-label": "SQL query. Ctrl+Enter runs it.", spellcheck: "false", class: "mono" });
    box.value = initial;
    box.addEventListener("keydown", (e) => { if (e.ctrlKey && e.key === "Enter") onRun(); });
    editorHost.append(box);
    return {
      value: () => box.value,
      setValue: (text) => { box.value = text; },
      focus: () => box.focus(),
      setTheme: () => undefined,
      setNames: () => undefined,
      dispose: () => box.remove(),
    };
  }
}

/** POST the tray's query; the tray says Running…, then the rows built or the server's reason. */
async function build(tray: Tray, name: string, replace: boolean): Promise<Built | null> {
  const sql = tray.editor.value().trim();
  if (!sql) return null;
  tray.busy(true);
  try {
    const built = await api<Built>(`/datasets/${encodeURIComponent(name)}/query`, "POST", { sql, replace });
    tray.report(`${rowsText(built.rows)} in ${built.dataset} (version ${built.version}).`);
    return built;
  } catch (e) {
    tray.report((e as Error).message, true);
    return null;
  } finally {
    tray.busy(false);
  }
}

// ===== names: the open tables, for the first query, the hint and autocomplete =====

function openTables(): string[] {
  return [...new Set(gridList().map((g) => contextOf(g.id)?.dataset).filter((d): d is string => !!d))];
}

/** A new draft starts on the first open table. */
function firstQuery(): string {
  const first = openTables()[0];
  return first ? `SELECT *\nFROM ${/^[A-Za-z_][A-Za-z0-9_]*$/.test(first) ? first : `"${first}"`}` : "";
}

/** `own`: a built table's own name, which its query can't read. */
function hint(own?: string): string {
  const tables = openTables().filter((t) => t !== own);
  return tables.length
    ? `Ctrl+Enter runs. Read a table by name, as its tab shows it: ${tables.join(", ")}.`
    : "Ctrl+Enter runs. No tables are open yet: push one, or SELECT from range(10).";
}

/** Autocomplete: every dataset's name, and the columns of the open tabs as they show them. */
async function refreshNames(editor: SqlEditor): Promise<void> {
  try {
    const { datasets } = await api<{ datasets: { name: string }[] }>("/datasets");
    const open = gridList().map((g) => contextOf(g.id)).filter((c): c is Context => !!c);
    // A tab never opened has no description yet; load it (nothing redraws: it isn't active).
    await Promise.all(open.filter((c) => !c.state.info).map((c) => c.state.load()));
    const columns = new Set(open.flatMap((c) => (c.state.info?.columns ?? []).map((column) => column.name)));
    editor.setNames([...columns], datasets.map((d) => d.name));
  } catch {
    // Autocomplete is a convenience; the query runs without it.
  }
}

function rowsText(rows: number): string {
  return `${formatCount(rows)} ${rows === 1 ? "row" : "rows"}`;
}
