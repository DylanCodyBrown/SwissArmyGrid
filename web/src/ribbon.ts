/**
 * ribbon.ts: the transformation toolbar, grouped the way Excel's ribbon is. [WIRE + EXPORT]
 *
 * Three groups, each named for what its tools act on, so it is always clear what a button
 * will change:
 *   TABLE   the whole view: text matching, filter, sort, choose columns, group, distinct, SQL,
 *           profile, Key Finder, export
 *   COLUMN  the selected column(s): click a header (or select cells) first. The group's
 *           caption names them. Sort, filter, rename, remove, keep, clean, bucket, categorize,
 *           group by, profile
 *   CELL    the selected cell(s) in one column: keep or exclude rows with those values, view
 *           the value, copy
 *   COMPARE  (only while two grids are side by side) the keys, outer / anti join, cross
 *           compare, stop: compare.ts lines the two grids up row for row
 * Every transformation still lands as a step in Table Transformations, so it can be turned
 * off, reordered, edited and exported as Python like any other.
 *
 * ROUTING: [TABLE TOOLS]   -> openTextMatching() / editStep(kind) / openTableProfile() /
 *                             openKeyFinderWindow() / openExport()
 *          [COLUMN TOOLS]  -> sortColumns() / editStep(kind, preset) / keepColumns() / onProfile()
 *          [CELL TOOLS]    -> filterByCells(keep) / viewCell() / copySelection()
 *          [COMPARE TOOLS] -> openCompareDialog() / changeCompare({mode, anti_side}) / stopCompare()
 *          [GROUP CAPTIONS]-> read-only, derived from gridView.selection + the active cell (+ the compare)
 *          (re-drawn on state "grid-selection" and "info")
 */
import { button, clear, h, isDateType, isNumericType, isTextType, openDialog, toast } from "./dom";
import { changeCompare, currentCompare, onCompareChange, stopCompare, type CompareInfo } from "./compare";
import { openFitTest, openOutliers } from "./analysisDialog";
import { analyzeButton } from "./analyzeMenu";
import { openCompareDialog } from "./compareDialog";
import { openTieOut } from "./tieOutDialog";
import { openExport } from "./exportDialog";
import { openKeyFinderWindow } from "./keyFinder";
import { autoSizeColumns, cellValue, copySelection, focusedCell, selectedValues, selection, viewCell, viewColumns } from "./gridView";
import { onSplitChange, splitPair } from "./panes";
import { openTableProfile } from "./profileView";
import { openRelationships } from "./relationshipsDialog";
import { state } from "./state";
import { editStep } from "./stepForms";
import { matchingSummary, openTextMatching } from "./textMatching";
import { gridZoom, onThemeChange, stepZoom } from "./theme";

let host: HTMLElement;
let onProfile: (column: string) => void = () => {};

export function mountRibbon(element: HTMLElement, openColumnProfile: (column: string) => void, popout = false): void {
  host = element;
  onProfile = openColumnProfile;
  state.on("grid-selection", render);
  state.on("info", render);
  onCompareChange(render);
  onSplitChange(render);
  onThemeChange(render); // a theme or zoom step can change what the rows draw
  if (popout) host.classList.add("popout-ribbon");
  render();
  // A window resize can make a row wrap (or room to spare): fold again, on width changes only.
  let width = 0;
  new ResizeObserver(() => {
    if (host.clientWidth === width) return;
    width = host.clientWidth;
    fold();
  }).observe(host);
}

/** Keep each row on one line: when its buttons would wrap, the words of its last buttons fold away,
 *  from the end, until it fits (menu buttons last). Every button stays where it is, with its icon, tooltip and name. */
function fold(): void {
  for (const tools of host.querySelectorAll<HTMLElement>(".ribbon-tools")) {
    const buttons = [...tools.querySelectorAll<HTMLElement>(":scope > .ribbon-btn")];
    for (const b of buttons) b.classList.remove("folded");
    const wraps = (): boolean => {
      const items = [...tools.children] as HTMLElement[];
      const first = items[0];
      const last = items[items.length - 1];
      return !!first && !!last && last.offsetTop > first.offsetTop + 4;
    };
    // From the end; a menu button (Analyze) keeps its word longest, as its icon alone says least.
    const order = [...buttons.filter((b) => !b.hasAttribute("aria-haspopup")).reverse(), ...buttons.filter((b) => b.hasAttribute("aria-haspopup")).reverse()];
    for (const b of order) {
      if (!wraps()) break;
      b.classList.add("folded");
    }
  }
}

/** The columns the Column tools act on: the selected ones, else the active cell's column. */
function targetColumns(): string[] {
  const chosen = selection.targetColumns();
  if (chosen.length) return chosen;
  if (selection.kind === "none") {
    const cell = focusedCell();
    if (cell) return [cell.column];
  }
  return [];
}

/** The one column the Cell tools act on, with the rows they cover, or a reason they can't. */
function targetCells(): { column: string; single: { row: number } | null } | string {
  if (selection.kind === "cells") {
    if (selection.columns.length !== 1) return "Select cells in one column.";
    const [a, b] = selection.rows[0]!;
    return { column: selection.columns[0]!, single: a === b ? { row: a } : null };
  }
  if (selection.kind === "none") {
    const cell = focusedCell();
    return cell ? { column: cell.column, single: { row: cell.row } } : "Click a cell first.";
  }
  return "Select cells, not rows or columns.";
}

/** One toolbar row: its label (and what it acts on) in a fixed column at the left, its tools
 * to the right, so the three rows line up like a form. */
function group(name: string, caption: string, ...tools: (HTMLElement | null)[]): HTMLElement {
  return h(
    "div",
    { class: "ribbon-group", role: "toolbar", "aria-label": `${name} tools: ${caption}` },
    h("div", { class: "ribbon-caption", title: caption }, h("strong", {}, name), h("span", {}, caption)),
    h("div", { class: "ribbon-tools" }, ...tools),
  );
}

function tool(label: string, icon: string, action: () => void, options: { title?: string; disabled?: string | false; pressed?: boolean } = {}): HTMLButtonElement {
  const b = button(label, action, { icon, class: "ribbon-btn", title: options.disabled || options.title || label, pressed: options.pressed });
  if (options.disabled) b.disabled = true;
  return b;
}

/** Grid zoom, on the Application row: - 100% +  (Ctrl+= / Ctrl+- / Ctrl+0 work anywhere). Built once;
 *  its percentage follows every zoom step (a zoom step notifies the theme listeners). */
export function zoomControl(): HTMLElement {
  const percent = (): string => `${Math.round(gridZoom() * 100)}%`;
  const out = h("output", { "aria-live": "polite", title: "Grid zoom (Ctrl+0 resets)" }, percent());
  onThemeChange(() => { out.textContent = percent(); });
  return h(
    "span",
    { class: "zoom-control ribbon-zoom", role: "group", "aria-label": "Grid zoom" },
    h("span", { class: "zoom-label", "aria-hidden": "true" }, "Zoom"),
    button("Zoom out", () => stepZoom(-1), { icon: "minus", iconOnly: true, title: "Zoom out (Ctrl+-)" }),
    out,
    button("Zoom in", () => stepZoom(1), { icon: "plus", iconOnly: true, title: "Zoom in (Ctrl+=)" }),
  );
}

function render(): void {
  if (!host || !state.info) return;
  const columns = viewColumns();
  const type = (name: string): string => columns.find((c) => c.name === name)?.type ?? "";
  const cols = targetColumns();
  const one = cols.length === 1 ? cols[0]! : null;
  const noColumn = cols.length ? false : "Select a column first.";
  const needOne = one ? false : cols.length ? "Select one column." : "Select a column first.";
  const textCols = cols.filter((c) => isTextType(type(c)));
  const cells = targetCells();
  const cellReason = typeof cells === "string" ? cells : false;
  const colCaption = cols.length ? (cols.length <= 2 ? cols.join(", ") : `${cols.slice(0, 2).join(", ")} +${cols.length - 2}`) : "";
  const cellCaption = typeof cells === "string" ? "" : cells.single ? `row ${cells.single.row + 1} of ${cells.column}` : `${selection.rowCount(Infinity)} cells of ${cells.column}`;

  clear(
    host,
    group(
      "Table",
      "",
      tool(`Matching: ${matchingSummary()}`, "case", () => openTextMatching(), { title: "How text compares" }),
      tool("Filter", "filter", () => editStep("filter")),
      tool("Sort", "sortup", () => editStep("sort")),
      tool("Choose columns", "columns", () => editStep("columns")),
      tool("Rename", "rename", () => editStep("rename")),
      tool("Group by", "group", () => editStep("group_by")),
      tool("Distinct rows", "keep", () => editStep("distinct")),
      tool("Keep first rows", "rows", () => editStep("limit")),
      tool("Clean text", "clean", () => editStep("transform")),
      tool("Auto width", "columns", () => autoSizeColumns()),
      tool("Profile", "profile", () => void openTableProfile()),
      analyzeButton(),
      tool("Key Finder", "zoom", () => openKeyFinderWindow(), { title: "Find likely join keys" }),
      tool("Relationships", "crown", () => openRelationships(), { title: "Keys and joins" }),
      tool("Export", "export", () => openExport()),
    ),
    group(
      "Column",
      colCaption,
      tool("Sort A→Z", "sortup", () => void sortColumns(cols, "asc"), { disabled: noColumn }),
      tool("Sort Z→A", "sortdown", () => void sortColumns(cols, "desc"), { disabled: noColumn }),
      tool("Filter…", "filter", () => editStep("filter", undefined, { column: one }), { disabled: needOne }),
      tool("Rename…", "rename", () => editStep("rename"), { disabled: noColumn }),
      tool("Auto width", "columns", () => autoSizeColumns(cols), { disabled: noColumn }),
      tool("Remove", "trash", () => void keepColumns(columns.map((c) => c.name).filter((n) => !cols.includes(n))), { disabled: noColumn, title: "Remove selected columns" }),
      tool("Keep only", "columns", () => void keepColumns(cols), { disabled: noColumn }),
      tool("Clean text…", "clean", () => editStep("transform", undefined, { columns: textCols }), { disabled: noColumn || (textCols.length ? false : "Select a text column.") }),
      tool("Bucket…", "bucket", () => editStep("bucket", undefined, { column: one }), { disabled: needOne || (isNumericType(type(one ?? "")) || isDateType(type(one ?? "")) ? false : "Number or date columns only.") }),
      tool("Categorize…", "tag", () => editStep("categorize", undefined, { column: one }), { disabled: needOne }),
      tool("Group by…", "group", () => editStep("group_by", undefined, { keys: cols }), { disabled: noColumn }),
      tool("Profile", "profile", () => one && onProfile(one), { disabled: needOne }),
      tool("Fit test…", "fit", () => void openFitTest(cols), { disabled: noColumn, title: "Chi-square test" }),
      tool("Outliers…", "outliers", () => one && void openOutliers(one), { disabled: needOne }),
    ),
    group(
      "Cell",
      cellCaption,
      tool("Keep these", "filter", () => void filterByCells(true), { disabled: cellReason }),
      tool("Exclude these", "exclude", () => void filterByCells(false), { disabled: cellReason, title: "Blank rows drop too" }),
      tool("View value", "eye", () => viewCell(), { disabled: typeof cells === "string" || !cells.single ? "Select one cell." : false }),
      tool("Copy", "copy", () => void copySelection(), { disabled: selection.kind === "none" && !focusedCell() ? "Select something first." : false, title: "Copy (Ctrl+C)" }),
    ),
    compareGroup(),
  );
  fold();
}

/** The Compare row: only while two grids are side by side. */
function compareGroup(): HTMLElement | null {
  const pair = splitPair();
  if (!pair) return null;
  const cmp = currentCompare();
  const noKeys = cmp ? false : "Pick keys first.";
  const keys = cmp ? cmp.keys.map(([l, r]) => (l === r ? l : `${l} = ${r}`)).join(" + ") : "";
  // The label column holds the keys; the counts go at the end of the row, where they fit.
  const caption = cmp ? keys : "no keys yet";
  const note = cmp?.mode === "diff" && cmp.differ
    ? diffNote(cmp, pair[0].dataset, pair[1].dataset)
    : cmp
      ? h("span", { class: "ribbon-note", role: "status" }, `${cmp.counts.matched.toLocaleString()} matched · ${cmp.counts.left_only.toLocaleString()} only in ${pair[0].dataset} · ${cmp.counts.right_only.toLocaleString()} only in ${pair[1].dataset}`)
      : h("span", { class: "ribbon-note" }, `${pair[0].dataset} and ${pair[1].dataset} side by side`);
  const anti = cmp?.mode === "anti";
  return group(
    "Compare",
    caption,
    tool(cmp ? "Keys…" : "Pick keys…", "columns", () => openCompareDialog(pair[0], pair[1]), {}),
    tool("Outer join", "rows", () => void changeCompare({ mode: "outer" }), { disabled: noKeys, pressed: cmp?.mode === "outer" }),
    tool("Anti join", "exclude", () => void changeCompare({ mode: "anti" }), { disabled: noKeys, pressed: anti }),
    ...(anti
      ? ([
          ["both", "Both sides"],
          ["left", `Only in ${pair[0].dataset}`],
          ["right", `Only in ${pair[1].dataset}`],
        ] as const).map(([side, label]) => tool(label, "filter", () => void changeCompare({ anti_side: side }), { pressed: cmp?.anti_side === side }))
      : []),
    tool("Cross compare", "zoom", () => void changeCompare({ mode: "cross" }), { disabled: noKeys, pressed: cmp?.mode === "cross" }),
    tool("Diff", "check", () => void changeCompare({ mode: "diff", diff: { column: null } }), { disabled: noKeys, pressed: cmp?.mode === "diff" }),
    cmp?.mode === "diff" ? tool("How values compare…", "settings", () => openDiffOptions(cmp)) : null,
    tool("Tie-out…", "check", () => cmp && void openTieOut(cmp, pair), { disabled: noKeys }),
    tool("Stop compare", "close", () => void stopCompare(), { disabled: noKeys }),
    note,
  );
}

/** Diff mode's summary: how many matched rows differ, a button per changed column (only its rows),
 * and the rows on one side only. */
function diffNote(cmp: CompareInfo, left: string, right: string): HTMLElement {
  const d = cmp.differ!;
  const focus = cmp.diff.column;
  const columns = Object.entries(d.columns).map(([name, count]) =>
    button(`${name} ${count.toLocaleString()}`, () => void changeCompare({ diff: { column: focus === name ? null : name } }), {
      class: "ghost diff-column",
      pressed: focus === name,
      title: focus === name ? "Show all differences" : undefined,
    }),
  );
  return h(
    "span",
    { class: "ribbon-note diff-note", role: "status" },
    h("span", {}, `${d.differ.toLocaleString()} of ${d.matched.toLocaleString()} matched rows differ`),
    ...columns,
    h("span", {}, `${d.left_only.toLocaleString()} only in ${left} · ${d.right_only.toLocaleString()} only in ${right}`),
    button("Every row", () => void changeCompare({ mode: "outer" }), { class: "ghost" }),
  );
}

/** How diff mode compares two cells: a tolerance for numbers, trimming and case for text, columns left out. */
function openDiffOptions(cmp: CompareInfo): void {
  const { body, foot, close } = openDialog("How values compare");
  const o = cmp.diff;
  const tolerance = h("input", { type: "number", min: "0", step: "any", value: String(o.tolerance), size: 8, "aria-label": "Numbers are equal within" });
  const percent = h("select", { "aria-label": "Tolerance as" }, h("option", { value: "", selected: !o.percent }, "(an amount)"), h("option", { value: "1", selected: o.percent }, "% of the left value"));
  const trim = h("input", { type: "checkbox", checked: o.trim, "aria-label": "Ignore spaces at either end" });
  const ignoreCase = h("input", { type: "checkbox", checked: o.ignore_case, "aria-label": "Ignore letter case" });
  const compared = [...new Set([...(cmp.differ?.compared ?? []), ...o.skip])];
  const keep = compared.map((name) => h("input", { type: "checkbox", value: name, checked: !o.skip.includes(name), "aria-label": `Compare ${name}` }));
  body.append(
    h("div", { class: "analysis-controls" }, h("label", { class: "analysis-field" }, h("span", {}, "Numbers are equal within"), tolerance), percent),
    h("label", { class: "check-row" }, trim, "Text: ignore spaces at either end"),
    h("label", { class: "check-row" }, ignoreCase, "Text: ignore letter case"),
  );
  if (compared.length) body.append(h("fieldset", { class: "tie-out-pick" }, h("legend", {}, "Columns compared"), ...keep.map((b) => h("label", {}, b, b.value))));
  foot.append(
    button("Apply", () => {
      void changeCompare({ diff: { tolerance: Number(tolerance.value) || 0, percent: percent.value === "1", trim: trim.checked, ignore_case: ignoreCase.checked, skip: keep.filter((b) => !b.checked).map((b) => b.value) } });
      close();
    }, { class: "primary" }),
    button("Cancel", close),
  );
}

async function sortColumns(cols: string[], direction: "asc" | "desc"): Promise<void> {
  await state.addStep({ kind: "sort", params: { keys: cols.map((c) => [c, direction]) } }).catch((e: Error) => toast(e.message, "error"));
}

async function keepColumns(keep: string[]): Promise<void> {
  if (!keep.length) {
    toast("That would remove every column.", "error");
    return;
  }
  await state.addStep({ kind: "columns", params: { keep } }).catch((e: Error) => toast(e.message, "error"));
}

/** Keep (or exclude) rows whose value in the column is one of the selected cells' values. */
export async function filterByCells(keep: boolean): Promise<void> {
  const cells = targetCells();
  if (typeof cells === "string") return;
  const column = viewColumns().find((c) => c.name === cells.column);
  if (!column) return;
  const values = cells.single ? [await cellValue(cells.single.row, cells.column)] : selectedValues(cells.column);
  const present = values.filter((v) => v !== null && v !== undefined);
  const hasBlank = present.length < values.length;
  let params: Record<string, unknown>;
  if (!present.length) params = { column: column.name, op: keep ? "blank" : "not_blank" };
  else if (typeof present[0] === "boolean" && present.length === 1) params = { column: column.name, op: keep === present[0] ? "true" : "false" };
  else if (present.length === 1) params = { column: column.name, op: keep ? "eq" : "ne", value: present[0] };
  else params = { column: column.name, op: keep ? "in" : "not_in", values: present };
  if (hasBlank && present.length) toast("Blank cells left out of the filter.");
  await state.addStep({ kind: "filter", params }).catch((e: Error) => toast(e.message, "error"));
}
