/**
 * cellMenu.ts: the grid's right-click menus (Plans/sandbox-report.plan.md, S4). gridView.ts says where
 * the click was; this says what can be done there.
 *
 *   [CELL]    Send value to Sandbox, Send the row to Sandbox, Keep these, Exclude these, View value, Copy
 *   [HEADER]  Profile to Sandbox, Distinct values to Sandbox, Profile, and the Analyze entries for it
 */
import { oddsCards, rowCards, valueCards, type OddsCardInput } from "./analysisCards";
import { openFitTest, openOdds, openOutliers, openPredictors } from "./analysisDialog";
import { sendCards } from "./analyzeMenu";
import { api, grid } from "./api";
import { showMenu, toast, type MenuItem as Item } from "./dom";
import { cellValue, copySelection, onCellMenu, onHeaderMenu, selection, viewCell, viewColumns, type CellClick } from "./gridView";
import { openColumnProfile, profileToSandbox } from "./profileView";
import { filterByCells, keepColumns, sortColumns } from "./ribbon";
import { editStep } from "./stepForms";
import { state } from "./state";

const dataset = (): string => state.info?.dataset ?? "";

/** The row's values in the view's column order (the grid keeps a few of its own fields beside them). */
function rowValues(click: CellClick): Record<string, unknown> {
  return Object.fromEntries(viewColumns().map((c) => [c.name, click.data[c.name] ?? null]));
}

async function distinctToSandbox(column: string): Promise<void> {
  try {
    const counted = await api<OddsCardInput>(`${grid}/odds`, "POST", { column });
    await sendCards(oddsCards(counted).map((card) => ({ ...card, title: card.title.replace(/^Odds: /, "Distinct values of ") })));
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

export function mountContextMenus(): void {
  onCellMenu((click) => {
    const where = `${click.column}, row ${(click.row + 1).toLocaleString()}`;
    showMenu(click.x, click.y, where, [
      ["image", "Send value to Sandbox", () => void cellValue(click.row, click.column).then((value) => sendCards(valueCards(value, where, dataset())))],
      ["rows", "Send the row to Sandbox", () => void sendCards(rowCards(rowValues(click), click.row, dataset()))],
      "-",
      ["filter", "Keep these", () => void filterByCells(true)],
      ["exclude", "Exclude these", () => void filterByCells(false)],
      "-",
      ["eye", "View value", () => viewCell()],
      ["copy", "Copy", () => void copySelection()],
    ]);
  });
  onHeaderMenu((column, event) => {
    const chosen = selection.targetColumns();
    // The column's own tools first, beside it (they lived only in the ribbon, rows above); then the
    // analyses. On several selected columns, the tools act on all of them.
    const cols = chosen.includes(column) ? chosen : [column];
    const others = viewColumns().map((c) => c.name).filter((n) => !cols.includes(n));
    showMenu(event.clientX, event.clientY, column, [
      ["sortup", "Sort A→Z", () => void sortColumns(cols, "asc")],
      ["sortdown", "Sort Z→A", () => void sortColumns(cols, "desc")],
      ["filter", "Filter…", () => editStep("filter", undefined, { column })],
      ["rename", "Rename…", () => editStep("rename", undefined, { only: cols })],
      ["trash", "Remove", () => void keepColumns(others)],
      ["columns", "Keep only", () => void keepColumns(cols)],
      ["group", "Group by…", () => editStep("group_by", undefined, { keys: cols })],
      "-",
      ["image", "Profile to Sandbox", () => void profileToSandbox(column)],
      ["tag", "Distinct values to Sandbox", () => void distinctToSandbox(column)],
      ["profile", "Profile…", () => openColumnProfile(column)],
      "-",
      ["tag", "Odds…", () => void openOdds(column)],
      ["fit", "Fit test…", () => void openFitTest(chosen.length ? chosen : [column])],
      ["outliers", "Outliers…", () => void openOutliers(column)],
      ["profile", "Find what predicts it…", () => void openPredictors(column)],
    ]);
  });
}
