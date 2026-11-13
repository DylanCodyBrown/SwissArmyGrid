/**
 * stepsStrip.ts: the grid's steps in one line under its title (plan decision 13). [VIEW]
 *
 * The Table Transformations panel and the SQL box left the grid page, which gives the grid the whole
 * page. The ribbon adds steps. This strip says how many are on and offers:
 *   Undo (drop the last step) · Clear · Edit steps (the full panel, in a tab of its own) ·
 *   Copy as SQL / Copy as Python (only code the server ran and matched) · To Python (the same code,
 *   copied and queued for sag.paste() in Jupyter or IPython).
 */
import { api, gridPath, token } from "./api";
import { gridToPython } from "./toPython";
import { button, clear, h, toast } from "./dom";
import { state } from "./state";

let host: HTMLElement;

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
    button("To Python", () => void gridToPython(info.grid), {
      icon: "code",
      class: "ghost",
      title: "These steps as DuckDB code over your table, checked against the grid: copied, and sag.paste() puts it in your Jupyter cell or IPython prompt",
    }),
  );
  for (const btn of host.querySelectorAll<HTMLButtonElement>("button")) {
    const name = btn.getAttribute("aria-label");
    if ((name === "Undo the last step" || name === "Clear the steps") && last < 0) btn.disabled = true;
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
