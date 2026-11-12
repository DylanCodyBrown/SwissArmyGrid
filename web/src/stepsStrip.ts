/**
 * stepsStrip.ts: the grid's steps in one line under its title (plan decision 13). [VIEW]
 *
 * The Table Transformations panel and the SQL box left the grid page, which gives the grid the whole
 * page; transforming lives in the notebook, as DuckDB SQL cells (decision 11). The ribbon still adds
 * steps. This strip says how many are on and offers:
 *   Undo (drop the last step) · Clear · Edit steps (the full panel, in a tab of its own) ·
 *   Copy as SQL / Copy as Python (only code the server ran and matched) · To notebook (a SQL cell).
 * A grid that came from the notebook keeps its steps there by itself: after each change its one SQL
 * cell is rewritten and run (POST /grids/{id}/notebook-cell), so the notebook is the transformation.
 */
import { ApiError, api, gridPath, token, type StepJson } from "./api";
import { button, clear, h, toast } from "./dom";
import { state } from "./state";

let host: HTMLElement;
const written = new Map<string, string>(); // grid id -> the steps last written to the notebook
const announced = new Set<string>();
let writeTimer = 0;

function copy(text: string, what: string): void {
  void navigator.clipboard.writeText(text).then(
    () => toast(`Copied ${what}.`),
    () => toast("The browser did not allow copying.", "error"),
  );
}

interface Code {
  code: string;
  sql: string;
  verified: boolean;
  difference: string | null;
  note: string | null;
}

async function copyCode(target: "duckdb", part: "sql" | "code"): Promise<void> {
  const info = state.info;
  if (!info) return;
  try {
    const answer = await api<Code>(`${gridPath(info.grid)}/code?target=${target}`);
    if (!answer.verified) {
      toast(`Not copied: the code did not match the grid (${answer.difference ?? answer.note ?? "unchecked"}).`, "error");
      return;
    }
    copy(part === "sql" ? answer.sql : answer.code, part === "sql" ? "the SQL" : "the Python");
  } catch (error) {
    toast((error as Error).message, "error");
  }
}

function openNotebook(): void {
  window.open(`/notebook#token=${encodeURIComponent(token)}`, "swissarmygrid-notebook")?.focus();
}

/** The steps as a SQL cell in the notebook. `quiet` for the automatic write after a change. */
async function toNotebook(quiet: boolean): Promise<void> {
  const info = state.info;
  if (!info) return;
  try {
    const kept = await api<{ variable: string; ran: boolean }>(`${gridPath(info.grid)}/notebook-cell`, "POST");
    written.set(info.grid, signature(info.steps));
    if (!quiet || !announced.has(info.grid)) {
      announced.add(info.grid);
      toast(`In the notebook as ${kept.variable}${kept.ran ? "" : " (it runs when the notebook is open)"}.`);
    }
  } catch (error) {
    // Quiet writes say nothing when the notebook isn't open; a click always says why.
    if (!quiet || !(error instanceof ApiError && error.status === 409)) toast((error as Error).message, "error");
  }
}

function signature(steps: StepJson[]): string {
  return JSON.stringify(steps.map((s) => [s.kind, s.params, s.enabled !== false]));
}

function openStepsWindow(): void {
  const info = state.info;
  if (!info) return;
  const page = `/g/${encodeURIComponent(info.grid)}`;
  const opened = window.open(`${page}?panel=steps#token=${encodeURIComponent(token)}`, `steps-${page}`, "width=440,height=820");
  if (!opened) toast("The browser blocked the steps window.", "error");
}

function render(): void {
  const info = state.info;
  if (!info) {
    clear(host);
    return;
  }
  const on = info.steps.filter((s) => s.enabled !== false && !s.params.header);
  const last = info.steps.length - 1;
  const label = on.length === 0 ? "No steps" : on.length === 1 ? "1 step" : `${on.length} steps`;
  clear(
    host,
    h("span", { class: "steps-count", title: on.map((s, i) => `${i + 1}. ${s.description ?? s.kind}`).join("\n") || "No steps yet: the ribbon adds them" }, label),
    info.step_error ? h("span", { class: "status-error" }, `Step ${info.step_error.index + 1}: ${info.step_error.message}`) : null,
    button("Undo the last step", () => void state.saveSteps(info.steps.slice(0, last)).catch((e: Error) => toast(e.message, "error")), {
      icon: "back",
      iconOnly: true,
      class: "ghost",
      title: last >= 0 ? `Undo: ${info.steps[last]?.description ?? info.steps[last]?.kind}` : "No step to undo",
    }),
    button("Clear the steps", () => void state.saveSteps([]).catch((e: Error) => toast(e.message, "error")), { icon: "trash", iconOnly: true, class: "ghost" }),
    button("Edit steps", openStepsWindow, { icon: "steps", class: "ghost", title: "Every step, to edit, turn off or reorder (in a tab of its own)" }),
    button("Copy as SQL", () => void copyCode("duckdb", "sql"), { icon: "sql", class: "ghost", title: "These steps as one DuckDB query over your table, checked against the grid" }),
    button("Copy as Python", () => void copyCode("duckdb", "code"), { icon: "copy", class: "ghost", title: "One line of DuckDB in Python over your table, checked against the grid" }),
    button(info.notebook ? "Kept in the notebook" : "To notebook", () => void toNotebook(false).then(openNotebook), {
      icon: "code",
      class: "ghost",
      title: info.notebook
        ? "This table came from the notebook: its steps are kept there as a SQL cell. Open it."
        : "Write these steps into the notebook as a SQL cell over this table",
    }),
  );
  for (const btn of host.querySelectorAll<HTMLButtonElement>("button")) {
    const name = btn.getAttribute("aria-label");
    if ((name === "Undo the last step" || name === "Clear the steps") && last < 0) btn.disabled = true;
  }
  // A notebook table's steps follow it back to the notebook, a moment after the last change.
  if (info.notebook && written.get(info.grid) !== signature(info.steps)) {
    window.clearTimeout(writeTimer);
    writeTimer = window.setTimeout(() => void toNotebook(true), 800);
  }
}

export function mountStepsStrip(element: HTMLElement): void {
  host = element;
  host.classList.add("steps-strip");
  host.setAttribute("role", "group");
  host.setAttribute("aria-label", "Steps");
  state.on("info", render);
}

/** For the grid page's tab switches: the strip shows the grid in front. */
export function refreshStepsStrip(): void {
  if (host) render();
}
