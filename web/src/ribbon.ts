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
import { button, clear, h, isDateType, isNumericType, isTextType, toast } from "./dom";
import { changeCompare, currentCompare, onCompareChange, stopCompare } from "./compare";
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
let inPopout = false;

export function mountRibbon(element: HTMLElement, openColumnProfile: (column: string) => void, popout = false): void {
  host = element;
  onProfile = openColumnProfile;
  state.on("grid-selection", render);
  state.on("info", render);
  onCompareChange(render);
  onSplitChange(render);
  onThemeChange(render); // a zoom step notifies too, so the Table row's percentage follows
  inPopout = popout;
  if (popout) host.classList.add("popout-ribbon");
  render();
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
    if (selection.columns.length !== 1) return "Select cells in one column to filter by their values.";
    const [a, b] = selection.rows[0]!;
    return { column: selection.columns[0]!, single: a === b ? { row: a } : null };
  }
  if (selection.kind === "none") {
    const cell = focusedCell();
    return cell ? { column: cell.column, single: { row: cell.row } } : "Click a cell first.";
  }
  return "The Cell tools work on cells; you have whole rows or columns selected.";
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

/** Grid zoom, on the Table row: - 100% +  (Ctrl+= / Ctrl+- / Ctrl+0 work anywhere). */
function zoomControl(): HTMLElement {
  return h(
    "span",
    { class: "zoom-control ribbon-zoom", role: "group", "aria-label": "Grid zoom" },
    h("span", { class: "zoom-label", "aria-hidden": "true" }, "Zoom"),
    button("Zoom out", () => stepZoom(-1), { icon: "minus", iconOnly: true, title: "Zoom out (Ctrl+-)" }),
    h("output", { "aria-live": "polite", title: "Grid zoom (Ctrl+0 resets)" }, `${Math.round(gridZoom() * 100)}%`),
    button("Zoom in", () => stepZoom(1), { icon: "plus", iconOnly: true, title: "Zoom in (Ctrl+=)" }),
  );
}

function render(): void {
  if (!host || !state.info) return;
  const columns = viewColumns();
  const type = (name: string): string => columns.find((c) => c.name === name)?.type ?? "";
  const cols = targetColumns();
  const one = cols.length === 1 ? cols[0]! : null;
  const noColumn = cols.length ? false : "Select a column first: click its header.";
  const needOne = one ? false : cols.length ? "Select one column." : "Select a column first: click its header.";
  const textCols = cols.filter((c) => isTextType(type(c)));
  const cells = targetCells();
  const cellReason = typeof cells === "string" ? cells : false;
  const colCaption = cols.length ? (cols.length <= 2 ? cols.join(", ") : `${cols.slice(0, 2).join(", ")} +${cols.length - 2}`) : "select a column";
  const cellCaption = typeof cells === "string" ? "select a cell" : cells.single ? `row ${cells.single.row + 1} of ${cells.column}` : `${selection.rowCount(Infinity)} cells of ${cells.column}`;

  clear(
    host,
    group(
      "Table",
      "the whole view",
      tool(`Matching: ${matchingSummary()}`, "case", () => openTextMatching(), { title: "How this table's filters and rules compare text: case, wildcards (%), trim, empty = blank" }),
      tool("Filter", "filter", () => editStep("filter"), { title: "Keep rows that match a condition" }),
      tool("Sort", "sortup", () => editStep("sort"), { title: "Sort by up to three columns" }),
      tool("Choose columns", "columns", () => editStep("columns")),
      tool("Rename", "rename", () => editStep("rename")),
      tool("Group by", "group", () => editStep("group_by"), { title: "Group rows and add totals, counts or averages" }),
      tool("Distinct rows", "keep", () => editStep("distinct"), { title: "Drop duplicate rows (the first one stays)" }),
      tool("Keep first rows", "rows", () => editStep("limit")),
      tool("Clean text", "clean", () => editStep("transform"), { title: "Trim or change the case of text columns" }),
      tool("Auto width", "columns", () => autoSizeColumns(), { title: "Fit every column to its header and the rows on screen" }),
      tool("Profile", "profile", () => void openTableProfile(), { title: "Profile every column" }),
      analyzeButton(),
      tool("Key Finder", "zoom", () => openKeyFinderWindow(), { title: "Find the columns of other grids that share values with this one (likely join keys)" }),
      tool("Relationships", "crown", () => openRelationships(), { title: "Primary keys and joins: apply a shared model, import or export one, draw an ERD" }),
      tool("Export", "export", () => openExport(), { title: "Export to CSV, text, Excel or Parquet" }),
      inPopout ? null : zoomControl(),
    ),
    group(
      "Column",
      colCaption,
      tool("Sort A→Z", "sortup", () => void sortColumns(cols, "asc"), { disabled: noColumn, title: "Sort ascending by the selected column(s)" }),
      tool("Sort Z→A", "sortdown", () => void sortColumns(cols, "desc"), { disabled: noColumn, title: "Sort descending by the selected column(s)" }),
      tool("Filter…", "filter", () => editStep("filter", undefined, { column: one }), { disabled: needOne }),
      tool("Rename…", "rename", () => editStep("rename"), { disabled: noColumn }),
      tool("Auto width", "columns", () => autoSizeColumns(cols), { disabled: noColumn, title: "Fit the selected column(s) to their header and the rows on screen" }),
      tool("Remove", "trash", () => void keepColumns(columns.map((c) => c.name).filter((n) => !cols.includes(n))), { disabled: noColumn, title: "Remove the selected column(s) from the view" }),
      tool("Keep only", "columns", () => void keepColumns(cols), { disabled: noColumn, title: "Keep only the selected column(s)" }),
      tool("Clean text…", "clean", () => editStep("transform", undefined, { columns: textCols }), { disabled: noColumn || (textCols.length ? false : "Select a text column.") }),
      tool("Bucket…", "bucket", () => editStep("bucket", undefined, { column: one }), { disabled: needOne || (isNumericType(type(one ?? "")) || isDateType(type(one ?? "")) ? false : "Bucket works on number and date columns."), title: "Add a column grouping values into ranges" }),
      tool("Categorize…", "tag", () => editStep("categorize", undefined, { column: one }), { disabled: needOne, title: "Add a column of labels from rules" }),
      tool("Group by…", "group", () => editStep("group_by", undefined, { keys: cols }), { disabled: noColumn }),
      tool("Profile", "profile", () => one && onProfile(one), { disabled: needOne, title: "Profile this column: a one-time popup" }),
      tool("Fit test…", "fit", () => void openFitTest(cols), { disabled: noColumn, title: "Chi-square: do the selected columns' values spread as expected (even, Benford's first digits, another grid)? Two columns: are they related?" }),
      tool("Outliers…", "outliers", () => one && void openOutliers(one), { disabled: needOne, title: "Find values far from the rest, or seen too often or too rarely" }),
    ),
    group(
      "Cell",
      cellCaption,
      tool("Keep these", "filter", () => void filterByCells(true), { disabled: cellReason, title: "Keep only rows with the selected value(s) in this column" }),
      tool("Exclude these", "exclude", () => void filterByCells(false), { disabled: cellReason, title: "Drop rows with the selected value(s) in this column (blank rows drop too, as in SQL)" }),
      tool("View value", "eye", () => viewCell(), { disabled: typeof cells === "string" || !cells.single ? "Select one cell." : false, title: "Open the full value (JSON, markdown and long text are formatted)" }),
      tool("Copy", "copy", () => void copySelection(), { disabled: selection.kind === "none" && !focusedCell() ? "Select something first." : false, title: "Copy the selection (Ctrl+C)" }),
    ),
    compareGroup(),
  );
}

/** The Compare row: only while two grids are side by side. */
function compareGroup(): HTMLElement | null {
  const pair = splitPair();
  if (!pair) return null;
  const cmp = currentCompare();
  const noKeys = cmp ? false : "Pick the keys first (Keys…).";
  const keys = cmp ? cmp.keys.map(([l, r]) => (l === r ? l : `${l} = ${r}`)).join(" + ") : "";
  // The label column holds the keys; the counts go at the end of the row, where they fit.
  const caption = cmp ? keys : "no keys yet";
  const note = cmp
    ? h("span", { class: "ribbon-note", role: "status" }, `${cmp.counts.matched.toLocaleString()} matched · ${cmp.counts.left_only.toLocaleString()} only in ${pair[0].dataset} · ${cmp.counts.right_only.toLocaleString()} only in ${pair[1].dataset}`)
    : h("span", { class: "ribbon-note" }, `${pair[0].dataset} and ${pair[1].dataset} side by side`);
  const anti = cmp?.mode === "anti";
  return group(
    "Compare",
    caption,
    tool(cmp ? "Keys…" : "Pick keys…", "columns", () => openCompareDialog(pair[0], pair[1]), { title: "Choose the columns that identify the same row in both tables (the Key Finder suggests them)" }),
    tool("Outer join", "rows", () => void changeCompare({ mode: "outer" }), { disabled: noKeys, pressed: cmp?.mode === "outer", title: "Every key from both tables, lined up row for row" }),
    tool("Anti join", "exclude", () => void changeCompare({ mode: "anti" }), { disabled: noKeys, pressed: anti, title: "Only the rows whose key has no match on the other side" }),
    ...(anti
      ? ([
          ["both", "Both sides"],
          ["left", `Only in ${pair[0].dataset}`],
          ["right", `Only in ${pair[1].dataset}`],
        ] as const).map(([side, label]) => tool(label, "filter", () => void changeCompare({ anti_side: side }), { pressed: cmp?.anti_side === side }))
      : []),
    tool("Cross compare", "zoom", () => void changeCompare({ mode: "cross" }), { disabled: noKeys, pressed: cmp?.mode === "cross", title: "Each grid shows its own rows; click a row to see the rows with its key on the other side" }),
    tool("Tie-out…", "check", () => cmp && void openTieOut(cmp, pair), { disabled: noKeys, title: "Do the two tables add up the same, group by group? Totals side by side, ties or off" }),
    tool("Stop compare", "close", () => void stopCompare(), { disabled: noKeys, title: "Back to two independent grids (the split stays)" }),
    note,
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
  if (hasBlank && present.length) toast("Blank cells in the selection are not part of this filter.");
  await state.addStep({ kind: "filter", params }).catch((e: Error) => toast(e.message, "error"));
}
