/**
 * modelDialog.ts: the Analyze menu's Regression (Plans/analyze-menu.plan.md, phase B; server:
 * routes_regression.py).
 *
 *   [FORM]        a target, what counts as yes (a yes/no target: logistic), the predictors
 *   [RESULT]      the verdict, the fit, each effect in a sentence with its range and p
 *   [WHAT-IF]     a field per predictor -> POST /regression/predict -> the estimate and its range
 *   [STEPS]       Add predicted column, Add miss column (a SQL step with the fitted numbers);
 *                 Check the misses: the miss column, then Outliers on it (the rows the model can't explain)
 *   [ACTIONS]     Send to Sandbox (a note and an effects chart), Open as grid (the coefficients), To Python
 */
import { regressionCards, type RegressionResult } from "./analysisCards";
import { openOutliers } from "./analysisDialog";
import { datasetName, registerAnalysis, resultButtons, showTheMath, tableName } from "./analyzeMenu";
import { api, grid } from "./api";
import { button, clear, formatCount, h, isNumericType, markStaleOnChange, openDialog, toast } from "./dom";
import { selection, viewColumns } from "./gridView";
import { state } from "./state";

const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });
const fmt = (v: unknown): string => (v === null || v === undefined ? "" : typeof v === "number" ? (Number.isFinite(v) ? number.format(v) : "∞") : String(v));
const pText = (p: number): string => (p < 0.0001 ? "< 0.0001" : p.toFixed(4));
const pct = (v: number): string => `${(100 * v).toFixed(0)}%`;

function field(label: string, control: HTMLElement): HTMLElement {
  return h("label", { class: "analysis-field" }, h("span", {}, label), control);
}

function tile(label: string, value: string, title?: string): HTMLElement {
  return h("div", { class: "stat", title }, h("div", { class: "label" }, label), h("div", { class: "value" }, value));
}

const OPS: [string, string][] = [
  ["values", "is one of"],
  ["gt", "is above"],
  ["ge", "is at least"],
  ["lt", "is below"],
  ["le", "is at most"],
];

registerAnalysis("fit", "Regression…", () => {
  const chosen = selection.targetColumns();
  void openRegression(chosen[0], chosen.slice(1));
});

export async function openRegression(target?: string, predictors: string[] = [], outcomeValues?: string): Promise<void> {
  const { body, foot, close } = openDialog("Regression", true);
  const columns = viewColumns();
  const which = h("select", { "aria-label": "Target" }, ...columns.map((c) => h("option", { value: c.name, selected: c.name === target }, c.name)));
  const op = h("select", { "aria-label": "Yes means" }, ...OPS.map(([v, t]) => h("option", { value: v }, t)));
  const yes = h("input", { type: "text", size: 14, placeholder: "Value", "aria-label": "Yes value", value: outcomeValues ?? "" });
  const yesNo = h("input", { type: "checkbox", "aria-label": "A yes/no target" });
  const outcomeRow = h("span", { class: "analysis-inline" }, op, yes);
  const boxes = (): HTMLInputElement[] => [...pool.querySelectorAll<HTMLInputElement>("input")];
  const pool = h("fieldset", { class: "analysis-checks" });
  const sync = (): void => {
    const numeric = isNumericType(columns.find((c) => c.name === which.value)?.type ?? "");
    if (!numeric) yesNo.checked = true;
    yesNo.disabled = !numeric;
    outcomeRow.hidden = !yesNo.checked;
    const ticked = new Set(boxes().filter((b) => b.checked).map((b) => b.value));
    if (!ticked.size) predictors.forEach((p) => ticked.add(p));
    clear(
      pool,
      h("legend", {}, "Predictors"),
      ...columns.filter((c) => c.name !== which.value).map((c) => {
        const box = h("input", { type: "checkbox", value: c.name, checked: ticked.has(c.name) });
        return h("label", {}, box, h("span", {}, c.name));
      }),
    );
  };
  which.addEventListener("change", sync);
  yesNo.addEventListener("change", sync);
  if (outcomeValues) yesNo.checked = true;
  sync();
  const results = h("div", { class: "analysis-results" });
  const actions = h("span", { class: "analysis-actions" });
  const run = async (): Promise<void> => {
    const sent: Record<string, unknown> = { target: which.value, predictors: boxes().filter((b) => b.checked).map((b) => b.value) };
    if (yesNo.checked) {
      const text = yes.value.trim();
      if (!text) {
        clear(results, h("p", { class: "hint" }, "Enter the yes value."));
        return;
      }
      sent.outcome = op.value === "values" ? { column: which.value, values: text.split(",").map((v) => v.trim()).filter(Boolean) } : { column: which.value, op: op.value, value: Number(text) };
    }
    clear(results, h("p", { class: "hint" }, "Fitting…"));
    clear(actions);
    try {
      const answer = await api<RegressionResult>(`${grid}/regression`, "POST", sent);
      clear(results, resultView(answer), whatIf(answer));
      clear(
        actions,
        button("Add predicted column", () => void add(answer.steps.predicted), { icon: "plus" }),
        answer.steps.miss ? button("Add miss column", () => void add(answer.steps.miss), { icon: "plus" }) : null,
        answer.steps.miss ? button("Check the misses", () => void checkMisses(answer), { icon: "outliers", title: "Outliers of the miss column" }) : null,
        ...resultButtons({
          analysis: "regression",
          body: sent,
          result: () => answer,
          cards: () => regressionCards(answer),
          rows: () => ({ name: tableName(datasetName(), "regression", answer.target), rows: answer.coefficients.map((c) => ({ term: c.term, coefficient: c.coefficient, low: c.low, high: c.high, p: c.p, ...(c.odds_ratio !== undefined ? { odds_ratio: c.odds_ratio } : {}), effect: c.words })) }),
        }),
      );
    } catch (e) {
      clear(results, h("p", { class: "status-error" }, (e as Error).message));
    }
  };
  const add = async (step: { kind: string; params: Record<string, unknown> } | null): Promise<void> => {
    if (!step) return;
    await state.addStep(step).then(() => toast("Added as a step."), (e: Error) => toast(e.message, "error"));
  };
  const checkMisses = async (answer: RegressionResult): Promise<void> => {
    if (!answer.steps.miss) return;
    try {
      await state.addStep(answer.steps.miss);
      close();
      await new Promise((resolve) => setTimeout(resolve, 500)); // the grid draws its new column first
      await openOutliers(`miss_${answer.target}`);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  body.append(
    h("div", { class: "analysis-controls" }, field("Target", which), h("label", { class: "analysis-field" }, h("span", {}, "Yes/no"), yesNo), outcomeRow, button("Fit", () => void run(), { icon: "play", class: "primary" })),
    pool,
    results,
  );
  markStaleOnChange(body, results);
  foot.append(actions, button("Close", close));
  if (target && predictors.length) await run();
}

function resultView(r: RegressionResult): HTMLElement {
  const linear = r.kind === "linear";
  const tiles = linear
    ? [
        tile("R-squared", fmt(r.fit.r2), "Share explained"),
        tile("Adjusted", fmt(r.fit.adj_r2), "Corrected for predictor count"),
        tile("Typical miss", fmt(r.fit.rmse), "In the target's units"),
        tile("Rows used", formatCount(r.n)),
      ]
    : [
        tile("Right", pct(r.fit.accuracy ?? 0), "Correct at 50%"),
        tile("Guessing", pct(r.fit.majority ?? 0), "Always the commoner value"),
        tile("McFadden R-squared", fmt(r.fit.pseudo_r2), "0.2 to 0.4: a good fit"),
        tile("Rows used", formatCount(r.n)),
      ];
  return h(
    "div",
    {},
    h("div", { class: "analysis-summary" }, h("p", { class: "analysis-headline" }, r.verdict)),
    h("div", { class: "stat-grid" }, ...tiles),
    r.left_out ? h("p", { class: "hint" }, `${formatCount(r.left_out)} row${r.left_out === 1 ? " has" : "s have"} a blank in a used column and ${r.left_out === 1 ? "was" : "were"} left out.`) : null,
    ...(r.warnings ?? []).map((w) => h("p", { class: "hint warning-text" }, w)),
    h(
      "table",
      { class: "small" },
      h("thead", {}, h("tr", {}, h("th", {}, "Term"), h("th", {}, "In words"), h("th", { class: "num" }, linear ? "Effect" : "Odds ratio"), h("th", { class: "num" }, "95% range"), h("th", { class: "num" }, "p"))),
      h(
        "tbody",
        {},
        ...r.coefficients.slice(1).map((c) =>
          h(
            "tr",
            { class: c.p < 0.05 ? "odds-up" : "" },
            h("td", {}, c.term),
            h("td", {}, c.words),
            h("td", { class: "num" }, fmt(linear ? c.coefficient : c.odds_ratio)),
            h("td", { class: "num" }, `${fmt(c.low)} to ${fmt(c.high)}`),
            h("td", { class: "num" }, pText(c.p)),
          ),
        ),
      ),
    ),
    Object.keys(r.references).length ? h("p", { class: "hint" }, `Compared with: ${Object.entries(r.references).map(([k, v]) => `${k} = ${v}`).join(", ")}.`) : null,
  );
}

/** A field per predictor (numbers start at their mean, categories at their reference) and Predict. */
function whatIf(r: RegressionResult): HTMLElement {
  const fields: Record<string, HTMLInputElement | HTMLSelectElement> = {};
  for (const column of r.predictors) {
    const terms = r.model.terms.filter((t) => t.column === column);
    const first = terms[0];
    if (first?.kind === "number") {
      fields[column] = h("input", { type: "number", step: "any", value: String(Math.round((first.mean ?? 0) * 100) / 100), "aria-label": column, size: 8 });
    } else {
      const levels = [r.references[column] ?? "", ...terms.filter((t) => t.kind === "level").map((t) => t.level ?? "")];
      fields[column] = h("select", { "aria-label": column }, ...levels.map((v) => h("option", { value: v }, v)));
    }
  }
  const answer = h("p", { class: "analysis-headline" });
  const predict = async (): Promise<void> => {
    const values = Object.fromEntries(Object.entries(fields).map(([k, f]) => [k, f instanceof HTMLInputElement ? Number(f.value) : f.value]));
    try {
      const p = await api<{ estimate: number; low: number; high: number }>("/regression/predict", "POST", { model: r.model, values });
      clear(
        answer,
        r.kind === "linear"
          ? `${r.target}: about ${fmt(p.estimate)} (a new row would fall between ${fmt(p.low)} and ${fmt(p.high)}, 95% of the time). `
          : `Chance of ${r.outcome}: ${pct(p.estimate)} (between ${pct(p.low)} and ${pct(p.high)}). `,
        button("Show the math", () => void showTheMath("predict", p), { icon: "steps", class: "ghost", title: "Term by term, to the Sandbox" }),
      );
    } catch (e) {
      answer.textContent = (e as Error).message;
    }
  };
  return h(
    "section",
    { class: "analysis-card what-if" },
    h("h3", { class: "section-title" }, "What if"),
    h("div", { class: "analysis-controls" }, ...Object.entries(fields).map(([k, f]) => field(k, f)), button("Predict", () => void predict(), { icon: "play" })),
    answer,
  );
}
