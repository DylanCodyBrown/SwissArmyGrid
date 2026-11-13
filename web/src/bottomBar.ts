/**
 * bottomBar.ts: the status bar (plan sections 6a and 8). [WIRE]
 *
 * ROUTING: [ROWS SHOWN]     -> read-only, from the grid's displayed range and row count
 *          [SELECTION]      -> read-only, from state.selection (Excel's Count, Average, Sum)
 *          [MEMORY METER]   -> read-only, from the budget; warns at 80% with an icon
 *          [DATASETS]       -> openDatasets(): every dataset with its size and a Close button
 *          [LIVE]           -> setLive()
 */
import { api, type Budget } from "./api";
import { $, button, clear, formatCount, h, toast } from "./dom";
import { displayedRange, shownTotal } from "./gridView";
import { onAgentView } from "./panes";
import { state } from "./state";

let rows: HTMLElement;
let selection: HTMLElement;
let meter: HTMLElement;
let live: HTMLElement;

export function mountBottomBar(host: HTMLElement): void {
  rows = h("span", { id: "status", role: "status" });
  selection = h("span", { "aria-live": "polite" });
  meter = h("span", { class: "meter" });
  live = h("span", { id: "live" });
  clear(host, rows, selection, h("span", { class: "grow" }), meter, button("Datasets", () => void openDatasets(), { class: "ghost", title: "Open datasets and their sizes" }), live, h("span", { class: "credit" }, "Created by Dylan Brown"));
  state.on("selection", render);
  state.on("info", render);
  onAgentView(render);
}

// What the status bar says when an app tab is in front: the grid's row count belongs to the grid.
const APP_TABS: Record<string, string> = {
  agent: "Sandbox",
  erd: "Relationship diagram of the open tables",
  query: "New table from a query",
};

function render(): void {
  const info = state.info;
  if (!info) return;
  const view = document.querySelector<HTMLElement>(".workspace")?.dataset.view ?? "grid";
  if (view !== "grid" && !(view === "query" && state.sqlResult)) {
    // The tab's name, then the grid behind it: a Sandbox chart's click filters that grid, and this says so.
    const total = shownTotal() || 0;
    const grid = total === info.row_count ? `${formatCount(total)} rows` : `${formatCount(total)} of ${formatCount(info.row_count)} rows`;
    rows.textContent = `${APP_TABS[view] ?? ""} · ${info.dataset}: ${grid}`;
    selection.textContent = "";
    renderMeter(info.budget);
    return;
  }
  if (state.sqlResult) {
    const r = state.sqlResult;
    rows.textContent = `Query result: ${formatCount(r.rows)} row${r.rows === 1 ? "" : "s"} \u00b7 ${r.milliseconds} ms${r.capped ? " \u00b7 stopped at the row cap" : ""}`;
    selection.textContent = "";
    renderMeter(info.budget);
    return;
  }
  if (document.querySelector(".workspace")?.getAttribute("data-panel") === "max") {
    const total = shownTotal() || 0;
    rows.textContent = `${formatCount(total)} rows in the view (${formatCount(info.row_count)} in ${info.dataset})`;
    selection.textContent = "";
    renderMeter(info.budget);
    return;
  }
  const [first, last] = displayedRange();
  const total = shownTotal() || 0;
  rows.textContent = total
    ? `Rows ${formatCount(Math.min(first + 1, total))}–${formatCount(Math.min(last + 1, total))} of ${formatCount(total)}${total !== info.row_count ? ` (${formatCount(info.row_count)} in ${info.dataset})` : ""}`
    : `0 rows (${formatCount(info.row_count)} in ${info.dataset})`;
  if (state.previewUpto !== null) rows.textContent += ` · previewing after step ${state.previewUpto}`;
  // Excel's status bar: Count is non-blank cells; Average and Sum cover the number cells.
  const s = state.selection;
  const n = (v: number): string => v.toLocaleString(undefined, { maximumFractionDigits: 4 });
  selection.textContent = s
    ? [
        s.kind === "rows" ? `${formatCount(s.rows)} row${s.rows === 1 ? "" : "s"} selected` : s.kind === "all" ? "All selected" : `${formatCount(s.cells)} cell${s.cells === 1 ? "" : "s"} selected`,
        `Count ${formatCount(s.count)}`,
        ...(s.mean !== null && s.sum !== null ? [`Average ${n(s.mean)}`, `Sum ${n(s.sum)}`] : []),
      ].join(" · ")
    : "";
  renderMeter(info.budget);
}

export function renderMeter(budget: Budget): void {
  const share = budget.total_bytes ? budget.used_bytes / budget.total_bytes : 0;
  meter.className = `meter ${budget.warn ? "warn" : ""}`;
  meter.title = budget.warn ? "Over 80% of the data budget is in use." : "Memory used by datasets";
  clear(
    meter,
    h("span", { class: "meter-bar", role: "meter", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.round(share * 100)), "aria-label": "Data budget used" }, h("span", { style: { width: `${Math.min(100, share * 100)}%` } })),
    `${budget.used} of ${budget.total} of data`,
  );
}

export function setLive(on: boolean): void {
  if (!live) return; // the pop-out windows have no status bar
  live.textContent = on ? "● live" : "○ not live: the server stopped or restarted; push again for a new link";
  live.className = on ? "" : "off";
}

async function openDatasets(): Promise<void> {
  document.querySelector(".popover")?.remove();
  const listing = await api<{ datasets: { name: string; rows: number; size: string; grids: string[]; version: number }[]; budget: Budget }>("/datasets");
  const pop = h("div", { class: "popover", role: "dialog", "aria-label": "Datasets" });
  const closePop = (): void => pop.remove();
  pop.append(
    h("div", { class: "code-toolbar", style: { justifyContent: "space-between" } }, h("strong", {}, "Datasets"), button("Close", closePop, { icon: "close", iconOnly: true, class: "ghost" })),
    h("p", { class: "hint" }, `${listing.budget.used} of ${listing.budget.total} in use · ${listing.budget.datasets} of ${listing.budget.max_datasets} datasets · ${listing.budget.grids} of ${listing.budget.max_grids} grids. Nothing is closed automatically.`),
    h(
      "table",
      { class: "small" },
      h("thead", {}, h("tr", {}, h("th", {}, "Dataset"), h("th", {}, "Rows"), h("th", {}, "Size"), h("th", {}, ""))),
      h(
        "tbody",
        {},
        ...listing.datasets.map((d) =>
          h(
            "tr",
            {},
            h("td", {}, d.name, d.name === state.info?.dataset ? h("span", { class: "hint" }, " (this grid)") : null),
            h("td", { class: "num" }, formatCount(d.rows)),
            h("td", { class: "num" }, d.size),
            h(
              "td",
              {},
              button(`Close ${d.name}`, () => {
                if (!confirm(`Close dataset ${d.name}? Its grids close too; nothing is saved.`)) return;
                void api(`/datasets/${encodeURIComponent(d.name)}`, "DELETE")
                  .then(() => { toast(`Closed ${d.name}.`); closePop(); })
                  .catch((e: Error) => toast(e.message, "error"));
              }, { icon: "trash", iconOnly: true, class: "ghost" }),
            ),
          ),
        ),
      ),
    ),
  );
  document.body.append(pop);
  const anchor = $(".statusbar").getBoundingClientRect();
  pop.style.right = "12px";
  pop.style.bottom = `${window.innerHeight - anchor.top + 6}px`;
  setTimeout(() => document.addEventListener("click", function away(e) {
    if (!pop.contains(e.target as Node)) { pop.remove(); document.removeEventListener("click", away); }
  }), 0);
}
