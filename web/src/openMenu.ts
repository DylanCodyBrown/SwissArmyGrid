/**
 * openMenu.ts: the Open dialog: the tables in your running Python sessions. [WIRE + EXPORT]
 *
 * A Python session whose watcher is on (swissarmygrid imported in a notebook, or sag.watch() in a
 * script) tells the server which tables it holds: names and sizes, never rows. This dialog lists them,
 * session by session. Open asks that session to push the table, and from then on the table follows its
 * variable: run the cell again and the tab refreshes in place. Reload sends it again, for changes made
 * in place that the watcher cannot see.
 *
 * ROUTING: [OPEN BUTTON]   beside the "+" in the tab bar -> showOpenDialog()
 *          [SESSION]       -> read-only, derived from GET /processes (redrawn on processes_changed)
 *          [OPEN / RELOAD] -> askToOpen(session, table)      POST /processes/{id}/open
 *          [OPEN ALL]      -> askToOpen() for each table not open yet
 *          [ERROR TOAST]   process_error notice -> the table that did not open, and why
 */
import { api, onNotice, type Notice } from "./api";
import { button, formatCount, h, openDialog, toast } from "./dom";
import { showOffer, type SessionOffer } from "./modelOffer";

interface TableEntry {
  name: string;
  kind: string;
  rows: number | null;
  columns: number | null;
  open: boolean;
}

interface Session {
  id: string;
  label: string;
  pid: number;
  tables: TableEntry[];
  /** The ERD models this notebook is associated with in FI-Atlas, once its Python has looked them up. */
  models: SessionOffer | null;
}

const KIND_LABELS: Record<string, string> = {
  dataframe: "DataFrame",
  polars: "Polars",
  arrow: "Arrow",
  relation: "DuckDB relation",
  table: "DuckDB table",
};

/** Redraws the open dialog; null while no dialog is open. */
let redraw: (() => void) | null = null;

/** Listen for the server's notices about sessions. Called once, by the workspace page. */
export function mountOpenMenu(): void {
  onNotice((notice: Notice) => {
    if (notice.type === "processes_changed") redraw?.();
    if (notice.type === "process_error") {
      toast(`${notice.name ?? "A table"} did not open from ${notice.label || "Python"}: ${notice.message ?? "no reason given"}`, "error");
    }
  });
}

/** The button beside the "+": opens the list. */
export function openMenuButton(): HTMLButtonElement {
  return button("Open a table from Python", () => showOpenDialog(), {
    icon: "rows",
    iconOnly: true,
    class: "ghost tab-new",
    title: "Open a table from a running Python session",
  });
}

async function askToOpen(session: Session, table: TableEntry, pressed?: HTMLButtonElement): Promise<void> {
  if (pressed) {
    pressed.disabled = true;
    // A Reload of a table that is already open changes nothing the list shows, so give the button back.
    setTimeout(() => { pressed.disabled = false; }, 2000);
  }
  try {
    await api(`/processes/${encodeURIComponent(session.id)}/open`, "POST", { name: table.name });
  } catch (e) {
    toast(`Could not ask ${session.label} for ${table.name}: ${(e as Error).message}`, "error");
    if (pressed) pressed.disabled = false;
  }
}

function sizeOf(table: TableEntry): string {
  const rows = table.rows === null ? "?" : formatCount(table.rows);
  const columns = table.columns === null ? "?" : formatCount(table.columns);
  return `${rows} × ${columns}`;
}

function tableRow(session: Session, table: TableEntry): HTMLElement {
  return h(
    "tr",
    {},
    h("td", {}, h("strong", {}, table.name)),
    h("td", {}, KIND_LABELS[table.kind] ?? table.kind),
    h("td", { class: "num", title: "rows × columns" }, sizeOf(table)),
    h("td", {}, table.open ? h("span", { class: "open-state" }, "open") : ""),
    h(
      "td",
      {},
      button(table.open ? "Reload" : "Open", (event) => void askToOpen(session, table, event.currentTarget as HTMLButtonElement), {
        class: table.open ? "ghost" : "primary",
        title: table.open ? `Send ${table.name} again from ${session.label}` : `Load ${table.name} from ${session.label} into the grid`,
      }),
    ),
  );
}

function sessionBlock(session: Session): HTMLElement {
  const closed = session.tables.filter((t) => !t.open);
  return h(
    "section",
    { class: "open-session" },
    h(
      "header",
      {},
      h("strong", {}, session.label),
      h("span", { class: "hint" }, `process ${session.pid}`),
      closed.length > 1
        ? button("Open all", () => closed.forEach((t) => void askToOpen(session, t)), { class: "ghost", title: `Open all ${closed.length} tables in this session` })
        : null,
      session.models?.models.length
        ? button(`Models (${session.models.models.length})`, () => showOffer(session, true), { class: "ghost", icon: "crown", title: "The ERD models associated with this notebook: apply them again" })
        : null,
    ),
    // Why a notebook has no models to offer, in one quiet line (fi-clients missing, not signed in, no table yet).
    session.models?.error ? h("p", { class: "hint", role: "status" }, `Models: ${session.models.error}`) : null,
    session.tables.length
      ? h("table", { class: "kf-table small" }, h("tbody", {}, ...session.tables.map((t) => tableRow(session, t))))
      : h("p", { class: "hint" }, "No tables among this session's variables yet."),
  );
}

function showOpenDialog(): void {
  const { dialog, body } = openDialog("Open a table from Python", true);
  body.classList.add("open-dialog");
  const status = h("p", { class: "hint", role: "status" }, "Looking for Python sessions…");
  const list = h("div", { class: "open-sessions" });
  body.append(status, list);

  const draw = (sessions: Session[]): void => {
    status.textContent = sessions.length
      ? ""
      : "No Python session is offering tables yet. In a notebook, importing swissarmygrid starts it. In a script, call sag.watch() once.";
    list.replaceChildren(...sessions.map(sessionBlock));
  };
  const load = async (): Promise<void> => {
    try {
      draw((await api<{ processes: Session[] }>("/processes")).processes);
    } catch (e) {
      status.textContent = `Could not read the Python sessions: ${(e as Error).message}`;
    }
  };

  redraw = () => void load();
  dialog.addEventListener("close", () => { redraw = null; });
  void load();
}
