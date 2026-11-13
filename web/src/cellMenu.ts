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
import { h, icon, toast } from "./dom";
import { cellValue, copySelection, onCellMenu, onHeaderMenu, selection, viewCell, viewColumns, type CellClick } from "./gridView";
import { openColumnProfile, profileToSandbox } from "./profileView";
import { filterByCells } from "./ribbon";
import { state } from "./state";

type Item = [string, string, () => void] | "-";

/** A menu at the pointer, kept inside the window; arrow keys move, Escape closes. */
function showMenu(x: number, y: number, label: string, items: Item[]): void {
  document.querySelector(".menu")?.remove();
  const menu = h(
    "div",
    { class: "menu context-menu", role: "menu", "aria-label": label },
    h("div", { class: "menu-label" }, label),
    ...items.map((item) =>
      item === "-"
        ? h("div", { class: "menu-rule", role: "separator" })
        : h("button", { type: "button", role: "menuitem", onclick: () => { menu.remove(); item[2](); } }, icon(item[0]), item[1]),
    ),
  );
  document.body.append(menu);
  menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - menu.offsetHeight - 8))}px`;
  (menu.querySelector("button") as HTMLButtonElement | null)?.focus();
  setTimeout(() => {
    const away = (event: Event): void => {
      if (!menu.contains(event.target as Node)) {
        menu.remove();
        document.removeEventListener("mousedown", away);
        document.removeEventListener("contextmenu", away);
      }
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("contextmenu", away);
  });
  menu.addEventListener("keydown", (event) => {
    const buttons = [...menu.querySelectorAll<HTMLButtonElement>("button")];
    const at = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === "Escape") menu.remove();
    else if (event.key === "ArrowDown") buttons[(at + 1) % buttons.length]?.focus();
    else if (event.key === "ArrowUp") buttons[(at - 1 + buttons.length) % buttons.length]?.focus();
    else return;
    event.preventDefault();
  });
}

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
    showMenu(event.clientX, event.clientY, column, [
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
