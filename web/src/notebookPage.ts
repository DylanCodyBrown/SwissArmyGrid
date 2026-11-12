/**
 * notebookPage.ts: the notebook tab: blades and the catalog left of marimo (plan, part 2). [COMPOSITION]
 *
 * marimo is served through SAG at /marimo (server/routes_notebook.py), so its frame is this page's
 * own origin. That is what makes the two notebook gestures work:
 * - Drag from the rail into a cell: a blade gives `conn = sag.blade("...")`, a table a SQL cell over
 *   its blade's connection, a column its quoted name. The drop lands where marimo's editor puts it;
 *   this page sees the drop too and asks the server to add any import it needs at the top of the
 *   notebook (a new cell, run at once), when the notebook does not have it yet.
 * - Save as blade: highlight code in a cell, press the button, and the blade save screen opens with
 *   that code, its kind and dialect guessed from its imports.
 */
import { ApiError, api, connect, onNotice, token } from "./api";
import { type BladeKind, type Template, editBlade, showTemplatePicker } from "./bladeEditor";
import { DROP_TYPE, mountRail, tableDetails, watchTheme } from "./catalogRail";
import { button, clear, h, openDialog, toast } from "./dom";

watchTheme();

const frame = document.querySelector<HTMLIFrameElement>(".nb-frame") as HTMLIFrameElement;
const status = document.querySelector<HTMLElement>(".nb-status") as HTMLElement;
const tools = document.querySelector<HTMLElement>(".nb-tools") as HTMLElement;
const liveNote = document.querySelector<HTMLElement>(".cat-live");
let notebookFile = "workspace.py";

// ===== Save as blade: the highlighted code =====

// The driver a blade imports says what kind of blade it is and how the agent should quote names.
const DRIVERS: [RegExp, string][] = [
  [/^(pyodbc|pymssql|mssql_python)$/, "sqlserver"],
  [/^(psycopg|psycopg2|pg8000|redshift_connector)$/, "postgres"],
  [/^(pymysql|MySQLdb|mysql|mariadb)$/, "mysql"],
  [/^(oracledb|cx_Oracle)$/, "oracle"],
  [/^sqlite3$/, "sqlite"],
  [/^duckdb$/, "duckdb"],
  [/^snowflake$/, "snowflake"],
  [/^(trino|prestodb)$/, "trino"],
  [/^(sqlalchemy|ibm_db|ibm_db_dbi|teradatasql|hdbcli|vertica_python|databricks|clickhouse_connect|google)$/, "other"],
];

export function guessKind(code: string): { kind: BladeKind; dialect: string } {
  const modules = [...code.matchAll(/^\s*(?:import|from)\s+([A-Za-z_][\w.]*)/gm)].map((m) => (m[1] ?? "").split(".")[0] ?? "");
  for (const module of modules) {
    for (const [pattern, dialect] of DRIVERS) if (pattern.test(module)) return { kind: "sql", dialect };
  }
  return { kind: "other", dialect: "other" };
}

/** What is highlighted in the notebook (marimo's editor keeps the page's own selection). */
function highlighted(): string {
  try {
    return frame.contentWindow?.getSelection()?.toString() ?? "";
  } catch {
    return ""; // not the same origin: the notebook did not load through SAG
  }
}

function saveAsBlade(): void {
  const code = highlighted().replace(/ /g, " ").trimEnd();
  if (!code.trim()) {
    toast("Highlight the code in a notebook cell first, then press Save as blade.", "error");
    return;
  }
  const { kind, dialect } = guessKind(code);
  const draft: Template = {
    id: "",
    label: "",
    group: "",
    kind,
    dialect,
    code,
    variable: "",
    catalog: null,
    vault_mapping: [],
    note: "From the notebook. Name it, check its kind, and save.",
  };
  editorDialog("Save as blade", (body, close) => void editBlade(body, null, draft, kind, (name) => rail.bladeSaved(name), close));
}

// ===== dialogs beside the notebook =====

function editorDialog(title: string, fill: (body: HTMLElement, close: () => void) => void): void {
  const { body, foot, close } = openDialog(title, true);
  foot.remove(); // the editor has its own buttons
  fill(body, () => {
    close();
    rail.editorClosed();
  });
}

function openEditor(name: string | null): void {
  if (name !== null) {
    editorDialog(`Blade: ${name}`, (body, close) => void editBlade(body, name, null, "other", (saved) => rail.bladeSaved(saved), close));
    return;
  }
  editorDialog("New blade", (body, close) =>
    void showTemplatePicker(body, (template, kind) => void editBlade(body, null, template, kind, (saved) => rail.bladeSaved(saved), close)),
  );
}

const rail = mountRail({
  bladesHost: document.getElementById("blades") as HTMLElement,
  catalogHost: document.getElementById("catalog") as HTMLElement,
  editBlade: openEditor,
  isEditing: () => false, // the editor is a dialog: nothing behind it can be clicked
  showTable: (view) => {
    const { body } = openDialog(view.table, true);
    body.append(...tableDetails(view));
  },
  notebook: true,
});

// ===== drops into the notebook: a cell, and the imports it needs at the top =====

interface Dropped {
  code: string;
  imports: string[];
}

function droppedOf(event: DragEvent): Dropped | null {
  const raw = event.dataTransfer?.getData(DROP_TYPE);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Dropped;
    return typeof parsed.code === "string" && Array.isArray(parsed.imports) ? parsed : null;
  } catch {
    return null;
  }
}

/** A blade or a table dropped on the notebook: the server puts its code into the cell it was dropped
 * on (when empty) or a new cell after it, and the imports it needs in a cell at the top. Placed by
 * the server through marimo's own document changes, so it lands the same whatever the editor does
 * with a drop. A column (a name inside a line) stays an ordinary text drop. */
async function placeDrop(dropped: Dropped, cell: string | null): Promise<void> {
  try {
    const answer = await api<{ cell: string; added: string[] }>("/notebook/drop", "POST", {
      code: dropped.code,
      imports: dropped.imports,
      cell,
      file: notebookFile,
    });
    toast(answer.added.length ? `Added at the top of the notebook: ${answer.added.join("; ")}` : "Added to the notebook.");
  } catch (error) {
    toast((error as Error).message, "error");
  }
}

function watchDrops(): void {
  const inner = frame.contentDocument;
  if (!inner) return;
  // Allow the drop anywhere in the notebook, not only over an editor.
  inner.addEventListener(
    "dragover",
    (event: DragEvent) => {
      if (event.dataTransfer?.types.includes(DROP_TYPE)) event.preventDefault();
    },
    true,
  );
  inner.addEventListener(
    "drop",
    (event: DragEvent) => {
      const dropped = droppedOf(event);
      if (!dropped) return;
      event.preventDefault();
      event.stopPropagation(); // the server places it; the editor must not paste it a second time
      const cell = (event.target as Element | null)?.closest?.("[data-cell-id]")?.getAttribute("data-cell-id") ?? null;
      void placeDrop(dropped, cell);
    },
    true,
  );
}

// ===== start =====

async function start(): Promise<void> {
  clear(status, "Starting the notebook...");
  status.hidden = false;
  try {
    const session = await api<{ src: string; file: string }>("/notebook/session", "POST");
    notebookFile = session.file;
    frame.addEventListener("load", () => {
      status.hidden = true;
      frame.hidden = false;
      watchDrops();
    });
    frame.src = session.src;
  } catch (error) {
    const message = error instanceof ApiError ? error.message : (error as Error).message;
    clear(
      status,
      h("p", {}, "The notebook did not start."),
      h("pre", { class: "code-box" }, message),
      button("Try again", () => void start(), { class: "primary" }),
    );
  }
}

tools.append(
  button("Save as blade", saveAsBlade, { icon: "save", class: "primary", title: "Save the highlighted code as a blade" }),
  button("Blades page", () => window.open(`/catalog#token=${encodeURIComponent(token)}`, "swissarmygrid-catalog")?.focus(), {
    icon: "key",
    class: "ghost",
  }),
  button("Grids", () => window.open(`/workspace#token=${encodeURIComponent(token)}`, "swissarmygrid-workspace")?.focus(), {
    icon: "columns",
    class: "ghost",
  }),
);
void start();
void rail.loadBlades().then(() => rail.loadCatalogs());
connect((live) => {
  if (liveNote) liveNote.textContent = live ? "" : "Not connected to the server";
}, { notebook: "1" });
onNotice((notice) => {
  if (notice.type === "catalogs_changed") void rail.loadCatalogs();
  if (notice.type === "blades_changed") void rail.loadBlades();
});
