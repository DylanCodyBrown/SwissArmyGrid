/**
 * sqlBox.ts: the SQL box (plan sections 6 and 10). [WIRE]
 *
 * A dataset name, this grid's own included, reads that table as loaded: the same rows as the
 * user's Python variable of that name, so the SQL copies over unchanged. `grid` reads this grid's
 * view after its steps. The server's guard
 * allows only one SELECT / WITH query (or CREATE TABLE <new> AS SELECT to pin), enforces a time
 * limit and a 10,000-row cap. Results replace the grid until "Back to table".
 *
 * "Apply as step" turns the query into a SQL transformation step in the Table Transformations
 * panel: later steps build on it, and it is in the exported Python. A step reads only the table
 * entering it, by the dataset's name or `grid` (so with earlier steps, a step's rows can differ
 * from the box's preview of the same text); joining another dataset stays a Pin-as-new-grid job.
 *
 * ROUTING: [EDITOR]        Monaco, column autocomplete; Ctrl+Enter -> run()
 *          [RUN / CANCEL]  -> run() / cancel()   (cancel interrupts the query on the server)
 *          [APPLY AS STEP] -> applyAsStep()      ("Update step N" while editing a SQL step)
 *          [HISTORY]       -> pick() ; entries tagged user or agent
 *          [RESULT BAR]    -> backToTable() / pinResult() / applyAsStep()
 *          [RESULT TABLE]  -> read-only, the query's rows (client-side AG Grid)
 */
import { createGrid, type GridApi } from "ag-grid-community";
import { api, grid } from "./api";
import { $, button, clear, formatCount, h, toast } from "./dom";
import { state } from "./state";
import { effectiveTheme, gridTheme, onThemeChange } from "./theme";
import type { SqlEditor } from "./lazy/sqlEditor";

interface SqlResult {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  capped: boolean;
  cap: number;
  milliseconds: number;
  pinned?: string;
  grid_url?: string;
}

const history: { sql: string; by: "user" | "agent"; when: string }[] = [];
let editor: SqlEditor | null = null;
let fallback: HTMLTextAreaElement | null = null;
let box: HTMLElement;
let status: HTMLElement;
let historyPicker: HTMLSelectElement;
let controller: AbortController | null = null;
let resultGrid: GridApi | null = null;
let lastSql = "";
/** The SQL step being edited (its index), or null when Apply adds a new step. */
let editing: number | null = null;
let applyButton: HTMLButtonElement | null = null;

export function mountSqlBox(host: HTMLElement): void {
  box = host;
  box.hidden = true;
  onThemeChange(() => {
    editor?.setTheme(effectiveTheme());
    resultGrid?.setGridOption("theme", gridTheme());
  });
}

export function isSqlOpen(): boolean {
  return Boolean(box && !box.hidden);
}

export function rememberAgentQuery(sql: string): void {
  history.unshift({ sql, by: "agent", when: new Date().toLocaleTimeString() });
  renderHistory();
}

/** Open the SQL box on an existing SQL step, so Apply updates that step. */
export async function openSqlForStep(index: number): Promise<void> {
  const step = state.info?.steps[index];
  if (!step || step.kind !== "sql") return;
  if (box.hidden) await toggleSqlBox();
  editing = index;
  setText(String(step.params.sql ?? ""));
  syncApply();
  clear(status, `Editing step ${index + 1}. Here `, h("code", {}, "grid"), ` is the table entering step ${index + 1}, and `, h("code", {}, tableName()), ` the rows as loaded; once saved, the step reads the table entering it by either name. Update step ${index + 1} saves it.`);
  editor?.focus();
}

/** This grid's dataset name: the table as loaded, the same name and rows as in the user's Python. */
function tableName(): string {
  return state.info?.dataset ?? "grid";
}

function syncApply(): void {
  if (!applyButton) return;
  const label = editing === null ? "Apply as step" : `Update step ${editing + 1}`;
  applyButton.querySelector(".btn-label")!.textContent = label;
  applyButton.setAttribute("aria-label", label);
  applyButton.title = editing === null
    ? `Add this query to the Table Transformations as a SQL step (it reads only the table entering it, as ${tableName()} or grid)`
    : `Save this query as step ${editing + 1}`;
}

export async function toggleSqlBox(): Promise<void> {
  if (!box.hidden) {
    box.hidden = true;
    editing = null;
    return;
  }
  box.hidden = false;
  if (box.childElementCount) {
    editor?.focus();
    return;
  }
  const editorHost = h("div", { class: "sql-editor" });
  status = h("span", { class: "sql-status", role: "status" }, "Ctrl+Enter runs. ", h("code", {}, tableName()), " is the table as loaded (as in your Python), ", h("code", {}, "grid"), " this view after its steps; any other dataset by name.");
  historyPicker = h("select", { "aria-label": "Query history", onchange: () => pick() });
  applyButton = button("Apply as step", () => void applyAsStep(), { icon: "check" });
  syncApply();
  box.append(
    editorHost,
    h(
      "div",
      { class: "sql-actions" },
      button("Run", () => void run(), { icon: "play", class: "primary", title: "Run (Ctrl+Enter)" }),
      button("Cancel", () => void cancel(), { icon: "stop" }),
      applyButton,
      historyPicker,
      status,
    ),
  );
  renderHistory();
  const initial = `SELECT *\nFROM ${tableName()}`;
  try {
    const m = (await import(/* external */ "/static/build/lazy/sqlEditor.js" as string)) as typeof import("./lazy/sqlEditor");
    ensureMonacoCss();
    editor = m.createSqlEditor(editorHost, initial, () => void run(), effectiveTheme());
    editor.setNames(
      (state.info?.columns ?? []).map((c) => c.name),
      ["grid", ...(await api<{ datasets: { name: string }[] }>("/datasets")).datasets.map((d) => d.name)],
    );
    editor.focus();
  } catch {
    // Monaco did not load (it is optional): a plain text box does the same job.
    fallback = h("textarea", { "aria-label": "SQL query. Ctrl+Enter runs it.", spellcheck: "false", class: "mono" });
    fallback.value = initial;
    fallback.addEventListener("keydown", (e) => { if (e.ctrlKey && e.key === "Enter") void run(); });
    editorHost.append(fallback);
    fallback.focus();
  }
}

export function ensureMonacoCss(): void {
  if (document.querySelector("link[data-monaco]")) return;
  document.head.append(h("link", { rel: "stylesheet", href: "/static/build/lazy/sqlEditor.css", "data-monaco": "true" }));
}

function text(): string {
  return editor?.value() ?? fallback?.value ?? "";
}

function renderHistory(): void {
  if (!historyPicker) return;
  clear(historyPicker, h("option", { value: "" }, history.length ? "History…" : "No history yet"));
  history.forEach((entry, i) => historyPicker.append(h("option", { value: String(i) }, `${entry.by === "agent" ? "[agent] " : ""}${entry.when}  ${entry.sql.replace(/\s+/g, " ").slice(0, 80)}`)));
}

function setText(sql: string): void {
  editor?.setValue(sql);
  if (fallback) fallback.value = sql;
}

function pick(): void {
  const entry = history[Number(historyPicker.value)];
  if (entry) setText(entry.sql);
}

/** Save the query as a SQL step (new, or the one being edited). A step that does not apply
 * is taken back out, and the reason shows here, so the user fixes it in the editor. */
async function applyAsStep(): Promise<void> {
  const sql = text().trim();
  const info = state.info;
  if (!sql || !info) return;
  const before = info.steps;
  try {
    let index: number;
    if (editing !== null) {
      index = editing;
      const steps = [...before];
      steps[index] = { ...steps[index]!, params: { sql } };
      await state.saveSteps(steps);
    } else {
      const headerAt = before.findIndex((s) => s.params.header);
      index = headerAt >= 0 ? headerAt : before.length;
      await state.addStep({ kind: "sql", params: { sql } });
    }
    const broken = state.info?.step_error;
    if (broken && broken.index === index) {
      await state.saveSteps(before);
      status.textContent = `Not applied: ${broken.message}`;
      status.className = "sql-status error";
      return;
    }
    toast(editing !== null ? `Updated step ${index + 1}.` : `Applied as step ${index + 1} in Table Transformations.`);
    editing = null;
    syncApply();
    clear(status, `Step ${index + 1} saved. Ctrl+Enter runs another query.`);
    status.className = "sql-status";
    backToTable();
  } catch (e) {
    status.textContent = (e as Error).message;
    status.className = "sql-status error";
  }
}

async function run(): Promise<void> {
  const sql = text().trim();
  if (!sql) return;
  controller?.abort();
  controller = new AbortController();
  clear(status, "Running…");
  status.className = "sql-status";
  history.unshift({ sql, by: "user", when: new Date().toLocaleTimeString() });
  history.splice(30);
  renderHistory();
  try {
    const result = await api<SqlResult>(`${grid}/sql`, "POST", editing === null ? { sql } : { sql, upto: editing }, controller.signal);
    if (result.pinned) {
      // The new grid arrives as a tab (the server announces it) and comes to the front.
      clear(status, `Pinned as dataset ${result.pinned}; it opened as a tab.`);
      return;
    }
    lastSql = sql;
    clear(status, `${formatCount(result.row_count)} row${result.row_count === 1 ? "" : "s"} in ${result.milliseconds} ms${result.capped ? ` · stopped at the ${formatCount(result.cap)}-row cap` : ""}`);
    showResult(result);
    state.sqlResult = { rows: result.row_count, milliseconds: result.milliseconds, capped: result.capped };
    state.emit("selection");
  } catch (e) {
    if ((e as Error).name === "AbortError") return;
    status.textContent = (e as Error).message;
    status.className = "sql-status error";
  }
}

async function cancel(): Promise<void> {
  controller?.abort();
  try {
    await api(`${grid}/cancel`, "POST");
    status.textContent = "Cancelled.";
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

/** The active grid's pane: a result shows in it, in place of that grid. */
function activePane(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".pane.active");
}

function showResult(result: SqlResult): void {
  const pane = activePane();
  if (!pane) return;
  backToTable();
  const bar = h(
    "div",
    { class: "result-bar", id: "sql-result-bar" },
    h("strong", {}, "Query result"),
    h("span", {}, `${formatCount(result.row_count)} ${result.row_count === 1 ? "row" : "rows"}`),
    h("span", { class: "spacer" }),
    button("Pin as new grid", () => void pinResult(), { icon: "pin" }),
    button(editing === null ? "Apply as step" : `Update step ${editing + 1}`, () => void applyAsStep(), { icon: "check", title: "Keep this query as a SQL step in Table Transformations" }),
    button("Back to table", backToTable, { icon: "back", class: "primary" }),
  );
  const host = h("div", { id: "sql-result-grid", style: { flex: "1", minHeight: "0" } });
  pane.querySelector<HTMLElement>(".grid-host")!.hidden = true;
  pane.append(bar, host);
  resultGrid = createGrid(host, {
    theme: gridTheme(),
    columnDefs: result.columns.map((name, i) => ({
      headerName: name,
      colId: `c${i}`,
      type: result.rows.some((r) => typeof r[i] === "number") ? "rightAligned" : undefined,
      valueGetter: (p: { data?: unknown[] }) => p.data?.[i],
      valueFormatter: (p: { value: unknown }) => (p.value === null || p.value === undefined ? "(blank)" : String(p.value)),
      cellClassRules: { "cell-blank": (p: { value: unknown }) => p.value === null },
    })),
    rowData: result.rows,
    defaultColDef: { resizable: true, sortable: true, minWidth: 90 },
  });
}

export function backToTable(): void {
  if (state.sqlResult) {
    state.sqlResult = null;
    state.emit("selection");
  }
  document.getElementById("sql-result-bar")?.remove();
  document.getElementById("sql-result-grid")?.remove();
  resultGrid?.destroy();
  resultGrid = null;
  for (const host of document.querySelectorAll<HTMLElement>(".pane .grid-host")) host.hidden = false;
}

async function pinResult(): Promise<void> {
  const name = prompt("Name for the new dataset (letters, digits, _ or -):", `${state.info?.dataset ?? "result"}_query`);
  if (!name) return;
  try {
    const result = await api<SqlResult>(`${grid}/sql`, "POST", { sql: `CREATE TABLE ${name} AS ${lastSql}` });
    // The new grid arrives as a tab (the server announces it) and comes to the front.
    toast(`Pinned as ${result.pinned}; it opened as a tab.`);
  } catch (e) {
    toast((e as Error).message, "error");
  }
}
