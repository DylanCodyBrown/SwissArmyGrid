/**
 * analyzeMenu.ts: the Table row's Analyze menu, and the actions every analysis result shares
 * (Plans/analyze-menu.plan.md).
 *
 *   [ANALYZE ▾]        Odds, Fit test, Find predictors, Outliers: each opens its form, filled from the
 *                      selected columns (analysisDialog.ts)
 *   [SEND TO SANDBOX]  a report-style note and its chart, side by side (analysisCards.ts; POST cards)
 *   [OPEN AS GRID]     a tabular result as a new grid (PUT /datasets/{name}?new_grid=1, JSON rows)
 *   [TO PYTHON]        the analysis as Python for Jupyter or IPython: POST /grids/{id}/analysis-code,
 *                      then copied and queued for sag.paste() (toPython.ts)
 */
import { api, grid } from "./api";
import { analysisBlades, openBladesPage, runBladeAnalysis } from "./bladeAnalysis";
import { toPython } from "./toPython";
import type { SandboxCard } from "./analysisCards";
import { button, h, icon, toast } from "./dom";
import { state } from "./state";

export interface ResultActions {
  /** The analysis's name for the server (odds, predictors, outliers, fit_test) and the request it ran. */
  analysis: string;
  body: Record<string, unknown>;
  /** The result as Sandbox cards: a report-style note and its chart (analysisCards.ts). */
  cards: () => SandboxCard[];
  /** The result as rows, for a new grid (and the name to give it). */
  rows?: () => { name: string; rows: Record<string, unknown>[] };
  /** The result as the server sent it, for Show the math (its proof is worked from these numbers). */
  result?: () => unknown;
}

/** The menu's entries: [icon, label, open]. analysisDialog.ts fills it, to keep the imports one-way. */
const entries: [string, string, () => void][] = [];

export function registerAnalysis(iconName: string, label: string, open: () => void): void {
  entries.push([iconName, label, open]);
}

export function analyzeButton(): HTMLButtonElement {
  const btn = button("Analyze", (event) => openMenu(event), {
    icon: "fit",
    class: "ribbon-btn",
  });
  btn.setAttribute("aria-haspopup", "menu");
  btn.append(h("span", { class: "menu-caret", "aria-hidden": "true" }, "▾"));
  return btn;
}

function openMenu(event: MouseEvent): void {
  document.querySelector(".menu")?.remove();
  const anchor = (event.currentTarget as HTMLElement).getBoundingClientRect();
  const menu = h(
    "div",
    { class: "menu", role: "menu", "aria-label": "Analyze" },
    ...entries.map(([name, label, run]) =>
      h("button", { type: "button", role: "menuitem", onclick: () => { menu.remove(); run(); } }, icon(name), label),
    ),
  );
  document.body.append(menu);
  menu.style.top = `${anchor.bottom + 4}px`;
  menu.style.left = `${Math.min(anchor.left, window.innerWidth - menu.offsetWidth - 8)}px`;
  (menu.querySelector("button") as HTMLButtonElement | null)?.focus();
  // Your own analyses (analysis blades), under the built-in ones.
  void analysisBlades().then((mine) => {
    if (!menu.isConnected) return;
    menu.append(
      h("div", { class: "menu-rule", role: "separator" }),
      h("div", { class: "menu-label" }, "Your analyses"),
      ...mine.map((blade) => h("button", { type: "button", role: "menuitem", title: blade.description || blade.name, onclick: () => { menu.remove(); void runBladeAnalysis(blade); } }, icon("cog"), blade.name)),
      h("button", { type: "button", role: "menuitem", onclick: () => { menu.remove(); openBladesPage(); } }, icon("plus"), mine.length ? "Make another…" : "Make your own…"),
    );
  });
  setTimeout(() =>
    document.addEventListener("click", function away(e) {
      if (!menu.contains(e.target as Node)) {
        menu.remove();
        document.removeEventListener("click", away);
      }
    }),
  );
  menu.addEventListener("keydown", (e) => {
    const items = [...menu.querySelectorAll<HTMLButtonElement>("button")];
    const at = items.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === "Escape") menu.remove();
    else if (e.key === "ArrowDown") items[(at + 1) % items.length]?.focus();
    else if (e.key === "ArrowUp") items[(at - 1 + items.length) % items.length]?.focus();
    else return;
    e.preventDefault();
  });
}

/** A name a grid (and a Python variable) can carry: letters, digits and underscores. */
export function tableName(...parts: string[]): string {
  return parts.map((p) => p.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "")).filter(Boolean).join("_") || "result";
}

/** The buttons under a result: Send to Sandbox, Open as grid (when it has rows), To Python. */
export function resultButtons(actions: ResultActions): HTMLButtonElement[] {
  const out = [
    button("Send to Sandbox", () => void toSandbox(actions), { icon: "image" }),
  ];
  if (actions.rows) out.push(button("Open as grid", () => void toGrid(actions), { icon: "rows" }));
  if (actions.result) {
    const result = actions.result;
    out.push(button("Show the math", () => void showTheMath(actions.analysis, result()), { icon: "steps", title: "Step by step, to the Sandbox" }));
  }
  out.push(button("To Python", () => void toYourPython(actions), { icon: "code", title: "Copied for sag.paste()" }));
  return out;
}

/** The proof of a result (domain/proofs.py), as a note in the Sandbox beside the result's cards. */
export async function showTheMath(analysis: string, result: unknown): Promise<void> {
  try {
    const proof = await api<{ title: string; markdown: string }>("/analysis-proof", "POST", { analysis, result, dataset: datasetName() });
    await api(`${grid}/cards`, "POST", { kind: "note", title: proof.title, body: proof.markdown, size: { w: 8, h: 14 } });
    toast("In the Sandbox: the math, step by step.");
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

/** Add cards to the Sandbox, filed with the grid in front; says what arrived. */
export async function sendCards(cards: SandboxCard[]): Promise<void> {
  try {
    for (const card of cards) await api(`${grid}/cards`, "POST", card);
    toast(`In the Sandbox: ${cards.length === 1 ? "a note" : "a note and its chart"}.`);
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

async function toSandbox(actions: ResultActions): Promise<void> {
  try {
    await sendCards(actions.cards());
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

async function toGrid(actions: ResultActions): Promise<void> {
  if (!actions.rows) return;
  const { name, rows } = actions.rows();
  if (!rows.length) {
    toast("Nothing to put in a grid.");
    return;
  }
  try {
    // The JSON push takes pandas' "split" shape: the column names, then each row's values in order.
    const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))];
    const data = rows.map((row) => columns.map((c) => row[c] ?? null));
    await api(`/datasets/${encodeURIComponent(name)}?new_grid=1`, "PUT", { columns, data });
    toast(`Opened as ${name}.`);
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

async function toYourPython(actions: ResultActions): Promise<void> {
  try {
    const made = await api<{ code: string; imports: string[] }>(`${grid}/analysis-code`, "POST", { analysis: actions.analysis, body: actions.body });
    await toPython(made.code, made.imports, `${actions.analysis.replace(/_/g, " ")} on ${datasetName()}`);
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

/** The dataset this grid shows, for naming result grids. */
export function datasetName(): string {
  return state.info?.dataset ?? "table";
}
