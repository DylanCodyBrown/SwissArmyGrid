/**
 * stepsStrip.ts: the grid's steps in one line under its title (plan decision 13). [VIEW]
 *
 * The Table Transformations panel and the SQL box left the grid page, which gives the grid the whole
 * page. The ribbon adds steps. This strip says how many are on and offers:
 *   Undo (drop the last step) · Clear · Edit steps (the full panel, in a tab of its own).
 *
 * The steps as code are the table's own read-only cell in the Workbench (docs/ideas.md, 2026-11-14):
 * Copy as SQL, Copy as Python and To Python left this strip, and were not moved anywhere.
 */
import { api, token } from "./api";
import { button, clear, h, toast } from "./dom";
import { state } from "./state";

let host: HTMLElement;

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
  // Opened from a connection with fewer rows than it holds: said plainly, beside the table, with the rest
  // one click away (the owner's call, 2026-11-14).
  const opened = info.opened;
  const loadAll = async (): Promise<void> => {
    if (!opened) return;
    toast(`Loading every row of ${opened.table}…`);
    try {
      const where = { database: opened.database, schema: opened.schema, table: opened.table, all: true };
      await api(`/connections/${encodeURIComponent(opened.connection)}/open`, "POST", where);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  clear(
    host,
    opened
      ? h(
          "span",
          { class: "opened-note", role: "status" },
          `First ${opened.shown.toLocaleString()} of ${opened.total.toLocaleString()} rows`,
          button("Load all rows", () => void loadAll(), { icon: "import", class: "ghost" }),
        )
      : null,
    h("span", { class: "steps-count", title: on.map((s, i) => `${i + 1}. ${s.description ?? s.kind}`).join("\n") || "No steps" }, label),
    info.step_error ? h("span", { class: "status-error" }, `Step ${info.step_error.index + 1}: ${info.step_error.message}`) : null,
    button("Undo the last step", () => void state.saveSteps(info.steps.slice(0, last)).catch((e: Error) => toast(e.message, "error")), {
      icon: "back",
      iconOnly: true,
      class: "ghost",
      title: last >= 0 ? `Undo: ${info.steps[last]?.description ?? info.steps[last]?.kind}` : "No step to undo",
    }),
    button("Clear the steps", () => void state.saveSteps([]).catch((e: Error) => toast(e.message, "error")), { icon: "trash", iconOnly: true, class: "ghost" }),
    button("Edit steps", openStepsWindow, { icon: "steps", class: "ghost" }),
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
