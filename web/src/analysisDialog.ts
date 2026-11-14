/**
 * analysisDialog.ts: the Analyze menu's forms (server: routes_analysis.py; the design in
 * Plans/analyze-menu.plan.md). Each opens from the menu or the Column row, filled from the selection,
 * and its result has Send to Sandbox, Open as grid and To Python (analyzeMenu.ts).
 *
 *   [FIT TEST]    openFitTest(columns): even, first digits, another grid, together -> POST fit-test
 *   [OUTLIERS]    openOutliers(column): amount (robust, quartiles, z-score; within each group) or
 *                 frequency -> POST outliers; Show these rows / Exclude these -> state.addStep
 *   [ODDS]        openOdds(column): each value's share and odds; with an outcome, its odds ratio
 *                 -> POST odds; Test together -> the fit test on the column and the outcome's column
 *   [PREDICTORS]  openPredictors(target): candidates ranked by Cramér's V -> POST predictors; a row's
 *                 Test together -> the fit test on the target and that column
 */
import { api, grid } from "./api";
import { amountCards, fitCards, frequencyCards, oddsCards, predictorsCards } from "./analysisCards";
import { datasetName, registerAnalysis, resultButtons, tableName } from "./analyzeMenu";
import { cheatSheet } from "./cheatSheets";
import { openRegression } from "./modelDialog";
import "./timeSeriesDialog"; // registers Time series and Forecast in the Analyze menu
import { button, clear, formatCount, h, isNumericType, markStaleOnChange, openDialog, toast } from "./dom";
import { selection, viewColumns } from "./gridView";
import { state } from "./state";

interface FitRow {
  value: string;
  observed: number;
  expected: number;
  residual: number;
}

interface FitResult {
  column?: string;
  statistic: number;
  df: number;
  p: number;
  n: number;
  effect_size: number;
  effect_name: string;
  effect: string;
  verdict: string;
  warnings: string[];
  categories?: FitRow[];
  conformity?: string;
  conformity_mad?: number;
  cells?: { a: string; b: string; observed: number; expected: number; residual: number }[];
}

interface Step {
  kind: string;
  params: Record<string, unknown>;
}

const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const fmt = (v: unknown): string => (v === null || v === undefined ? "(blank)" : typeof v === "number" ? number.format(v) : String(v));
const pText = (p: number): string => (p < 0.0001 ? "< 0.0001" : p.toFixed(4));
const percent = (v: number | null | undefined): string => (v === null || v === undefined ? "" : `${(100 * v).toFixed(v < 0.1 ? 1 : 0)}%`);

function tile(label: string, value: string, title?: string): HTMLElement {
  return h("div", { class: "stat", title }, h("div", { class: "label" }, label), h("div", { class: "value" }, value));
}

function select(label: string, options: [string, string][], value: string, onchange?: () => void): HTMLSelectElement {
  return h("select", { "aria-label": label, onchange }, ...options.map(([v, text]) => h("option", { value: v, selected: v === value }, text)));
}

function field(label: string, control: HTMLElement): HTMLElement {
  return h("label", { class: "analysis-field" }, h("span", {}, label), control);
}

/** The view's columns as options, optionally with a leading "(none)". */
function columnOptions(none?: string): [string, string][] {
  const all = viewColumns().map((c): [string, string] => [c.name, c.name]);
  return none === undefined ? all : [["", none], ...all];
}

function columnType(name: string): string {
  return viewColumns().find((c) => c.name === name)?.type ?? "";
}

/** A list of the view's columns to tick, with `ticked` ticked. */
function checklist(label: string, ticked: string[], skip: string[] = []): { element: HTMLElement; values: () => string[] } {
  const boxes = viewColumns()
    .filter((c) => !skip.includes(c.name))
    .map((c) => h("input", { type: "checkbox", value: c.name, checked: ticked.includes(c.name) }));
  const element = h(
    "fieldset",
    { class: "analysis-checks" },
    h("legend", {}, label),
    ...boxes.map((box) => h("label", {}, box, h("span", {}, box.value))),
  );
  return { element, values: () => boxes.filter((b) => b.checked).map((b) => b.value) };
}

function selected(): string[] {
  return selection.targetColumns();
}

// The menu's entries, in the order the plan lists them.
registerAnalysis("tag", "Odds…", () => void openOdds(selected()[0]));
registerAnalysis("fit", "Fit test…", () => void openFitTest(selected()));
registerAnalysis("profile", "Find predictors…", () => void openPredictors(selected()[0]));
registerAnalysis("outliers", "Outliers…", () => void openOutliers(selected()[0]));

// ===== the fit test =====

const EXPECTED: [string, string][] = [
  ["even", "Even: every value as likely"],
  ["benford", "First digit (Benford's law)"],
  ["benford2", "First two digits (Benford's law)"],
  ["grid", "Another grid's spread"],
  ["together", "Together: are the two related?"],
];

export async function openFitTest(columns: string[], expectedKind?: string): Promise<void> {
  const { body, foot, close } = openDialog("Fit test", true);
  const chosen = h("select", { multiple: true, size: 4, "aria-label": "Columns to test" }, ...columnOptions().map(([v, text]) => h("option", { value: v, selected: columns.includes(v) }, text)));
  const picked = (): string[] => [...chosen.selectedOptions].map((o) => o.value);
  const allNumbers = columns.length > 0 && columns.every((c) => isNumericType(columnType(c)));
  const guess = expectedKind ?? (columns.length === 2 && !allNumbers ? "together" : allNumbers ? "benford" : "even");
  const expected = select("Expected spread", EXPECTED, guess, () => (referenceRow.hidden = expected.value !== "grid"));
  const others = h("select", { "aria-label": "The grid to compare with" });
  const otherColumn = h("input", { type: "text", "aria-label": "Its column", placeholder: columns[0] ?? "", size: 14 });
  const referenceRow = h("div", { class: "analysis-controls", hidden: expected.value !== "grid" }, field("Grid", others), field("Column", otherColumn));
  void api<{ grids: { grid: string; dataset: string }[] }>("/grids").then(({ grids }) => {
    const current = state.info?.grid ?? "";
    others.append(...grids.filter((g) => g.grid !== current).map((g) => h("option", { value: g.grid }, g.dataset)));
    if (!others.options.length) others.append(h("option", { value: "", disabled: true }, "No other grids"));
  });
  const results = h("div", { class: "analysis-results" });
  const actions = h("span", { class: "analysis-actions" });
  const run = async (): Promise<void> => {
    clear(actions);
    const sent: Record<string, unknown> = { columns: picked(), expected: expected.value };
    if (!picked().length) {
      clear(results, h("p", { class: "hint" }, "Pick columns."));
      return;
    }
    if (expected.value === "grid") sent.reference = { grid: others.value, column: otherColumn.value.trim() || undefined };
    clear(results, h("p", { class: "hint" }, "Testing…"));
    try {
      const answer = await api<{ results?: FitResult[]; columns?: string[] } & FitResult>(`${grid}/fit-test`, "POST", sent);
      const list = answer.results ?? [{ ...answer, column: (answer.columns ?? []).join(" and ") }];
      clear(results, ...(answer.results ? answer.results.map((r) => fitCard(r)) : [fitCard(answer, answer.columns)]));
      clear(
        actions,
        ...resultButtons({
          analysis: "fit_test",
          body: sent,
          result: () => (answer.results ? answer : { results: [{ ...answer, column: (answer.columns ?? []).join(" and ") }] }),
          cards: () => (answer.results ? answer.results.flatMap((r) => fitCards(r, String(sent.expected))) : fitCards(answer, "together", answer.columns)),
          rows: list.length === 1 && list[0]?.categories
            ? () => ({ name: tableName(datasetName(), "fit", list[0]?.column ?? ""), rows: (list[0]?.categories ?? []).map((c) => ({ ...c })) })
            : undefined,
        }),
      );
    } catch (e) {
      clear(results, h("p", { class: "status-error" }, (e as Error).message));
    }
  };
  body.append(
    h("div", { class: "analysis-controls" }, field("Columns", chosen), field("Expected", expected), button("Test", () => void run(), { icon: "play", class: "primary" })),
    referenceRow,
    cheatSheet("fit_test", picked).element,
    results,
  );
  markStaleOnChange(body, results);
  foot.append(actions, button("Close", close));
  if (columns.length) await run();
}

function fitCard(r: FitResult, pair?: string[]): HTMLElement {
  const fits = r.p >= 0.05;
  const tiles = [
    tile("Chi-square", fmt(r.statistic)),
    tile("Degrees of freedom", formatCount(r.df)),
    tile("p-value", pText(r.p), "Chance by luck alone"),
    tile("Rows", formatCount(r.n)),
    tile(r.effect_name, `${fmt(r.effect_size)} (${r.effect})`, "0.1 small, 0.3 medium, 0.5 large"),
  ];
  if (r.conformity) tiles.push(tile("Benford conformity", r.conformity, `Mean absolute deviation: ${fmt(r.conformity_mad)}`));
  const title = pair ? `${pair[0]} and ${pair[1]}` : (r.column ?? "");
  return h(
    "section",
    { class: "analysis-card" },
    h("h3", { class: "section-title" }, title),
    h("p", { class: `analysis-verdict ${fits ? "ok" : "off"}` }, r.verdict),
    h("div", { class: "stat-grid" }, ...tiles),
    ...r.warnings.map((w) => h("p", { class: "hint warning-text" }, w)),
    r.categories ? fitBars(r.categories) : null,
    r.cells ? cellTable(r.cells, pair ?? ["", ""]) : null,
  );
}

/** Each value: its count as a bar, the expected count as a tick, and how far apart they are. */
function fitBars(rows: FitRow[]): HTMLElement {
  const shown = rows.slice(0, 40);
  const top = Math.max(1, ...shown.map((r) => Math.max(r.observed, r.expected)));
  return h(
    "div",
    { class: "fit-bars", role: "list", "aria-label": "Observed count (bar) against expected (tick)" },
    h("div", { class: "fit-row head", "aria-hidden": "true" }, h("span", {}, "Value"), h("span", {}, "Seen (bar) and expected (tick)"), h("span", {}, "Seen"), h("span", {}, "Expected"), h("span", {}, "Off by")),
    ...shown.map((r) =>
      h(
        "div",
        { class: `fit-row ${Math.abs(r.residual) >= 2 ? "far" : ""}`, role: "listitem" },
        h("span", { class: "fit-value", title: r.value }, r.value),
        h(
          "span",
          { class: "fit-track" },
          h("span", { class: "fit-bar", style: { width: `${(100 * r.observed) / top}%` } }),
          h("span", { class: "fit-tick", style: { left: `${(100 * r.expected) / top}%` } }),
        ),
        h("span", { class: "num" }, formatCount(r.observed)),
        h("span", { class: "num" }, fmt(r.expected)),
        h("span", { class: "num", title: "Standardized residual" }, `${r.residual > 0 ? "+" : ""}${fmt(r.residual)}`),
      ),
    ),
    rows.length > shown.length ? h("p", { class: "hint" }, `and ${formatCount(rows.length - shown.length)} more values`) : null,
  );
}

function cellTable(cells: NonNullable<FitResult["cells"]>, pair: string[]): HTMLElement {
  return h(
    "table",
    { class: "small" },
    h("caption", { class: "hint" }, "Furthest from independence"),
    h("thead", {}, h("tr", {}, h("th", {}, pair[0] ?? ""), h("th", {}, pair[1] ?? ""), h("th", { class: "num" }, "Seen"), h("th", { class: "num" }, "Expected"), h("th", { class: "num" }, "Off by"))),
    h(
      "tbody",
      {},
      ...cells.slice(0, 15).map((c) =>
        h("tr", {}, h("td", {}, c.a), h("td", {}, c.b), h("td", { class: "num" }, formatCount(c.observed)), h("td", { class: "num" }, fmt(c.expected)), h("td", { class: "num" }, `${c.residual > 0 ? "+" : ""}${fmt(c.residual)}`)),
      ),
    ),
  );
}

// ===== outliers =====

const METHODS: [string, string][] = [
  ["robust", "Robust (median)"],
  ["iqr", "Quartile fences (Tukey)"],
  ["zscore", "Mean and standard deviation"],
];
const DEFAULT: Record<string, string> = { robust: "3.5", iqr: "1.5", zscore: "3" };

interface AmountResult {
  look: "amount";
  by: string | null;
  rows: number;
  low: number;
  high: number;
  groups: number;
  groups_flagged: number;
  groups_unjudged: number;
  fences: { group: string | null; rows: number; low_fence: number | null; high_fence: number | null; median: number; low: number; high: number }[];
  outliers: { group: string | null; value: number; score: number }[];
  step: Step | null;
  exclude_step: Step | null;
  step_note?: string;
}

interface FrequencyResult {
  look: "frequency";
  distinct: number;
  typical: number;
  rarer: number;
  commoner: number;
  judged: boolean;
  outliers: { value: string; count: number; score: number }[];
  step: Step | null;
  exclude_step: Step | null;
}

export async function openOutliers(column?: string): Promise<void> {
  const { body, foot, close } = openDialog("Outliers", true);
  const which = select("Column", columnOptions(), column ?? viewColumns()[0]?.name ?? "", () => {
    const numeric = isNumericType(columnType(which.value));
    look.options[0]!.disabled = !numeric;
    if (!numeric) look.value = "frequency";
    sync();
  });
  const look = select("Look at", [["amount", "Amount"], ["frequency", "Frequency"]], isNumericType(columnType(which.value)) ? "amount" : "frequency", () => sync());
  look.options[0]!.disabled = !isNumericType(columnType(which.value));
  const method = select("Method", METHODS, "robust", () => (threshold.placeholder = DEFAULT[method.value] ?? ""));
  const threshold = h("input", { type: "number", min: "0.1", step: "0.1", size: 5, placeholder: DEFAULT.robust, "aria-label": "How far out counts" });
  const by = select("Within each", columnOptions("(the whole column)"), "");
  const amountOnly = [field("Method", method), field("How far out", threshold), field("Within each", by)];
  const sync = (): void => amountOnly.forEach((f) => (f.hidden = look.value !== "amount"));
  const results = h("div", { class: "analysis-results" });
  const actions = h("span", { class: "analysis-actions" });
  let steps: { show: Step | null; exclude: Step | null } = { show: null, exclude: null };
  const add = async (step: Step | null): Promise<void> => {
    if (step) await state.addStep(step).then(close, (e: Error) => toast(e.message, "error"));
  };
  const show = button("Show these rows", () => void add(steps.show), { icon: "filter", class: "primary", title: "Keep only these rows" });
  const exclude = button("Exclude these", () => void add(steps.exclude), { icon: "exclude", title: "Leave these rows out" });
  const run = async (): Promise<void> => {
    clear(results, h("p", { class: "hint" }, "Looking…"));
    clear(actions);
    show.disabled = exclude.disabled = true;
    const sent: Record<string, unknown> = { column: which.value, look: look.value };
    if (look.value === "amount") Object.assign(sent, { method: method.value, threshold: threshold.value ? Number(threshold.value) : undefined, by: by.value || undefined });
    try {
      const answer = await api<AmountResult | FrequencyResult>(`${grid}/outliers`, "POST", sent);
      steps = { show: answer.step, exclude: answer.exclude_step };
      show.disabled = !answer.step;
      exclude.disabled = !answer.exclude_step;
      const view = answer.look === "amount" ? amountView(answer, which.value) : frequencyView(answer);
      clear(results, view);
      clear(
        actions,
        ...resultButtons({
          analysis: "outliers",
          body: sent,
          result: () => (answer),
          cards: () => (answer.look === "amount" ? amountCards({ ...answer, column: which.value, method: String(sent.method ?? "robust") }) : frequencyCards({ ...answer, column: which.value })),
          rows: () => ({
            name: tableName(datasetName(), "outliers", which.value),
            rows: answer.look === "amount"
              ? answer.outliers.map((o) => ({ ...(answer.by ? { [answer.by]: o.group } : {}), [which.value]: o.value, score: o.score }))
              : answer.outliers.map((o) => ({ [which.value]: o.value, rows: o.count, score: o.score })),
          }),
        }),
      );
    } catch (e) {
      clear(results, h("p", { class: "status-error" }, (e as Error).message));
    }
  };
  sync();
  body.append(
    h("div", { class: "analysis-controls" }, field("Column", which), field("Look at", look), ...amountOnly, button("Look", () => void run(), { icon: "play" })),
    cheatSheet("outliers", () => [which.value, by.value].filter(Boolean)).element,
    results,
  );
  markStaleOnChange(body, results);
  foot.append(actions, show, exclude, button("Close", close));
  await run();
}

function amountView(r: AmountResult, column: string): HTMLElement {
  const total = r.low + r.high;
  const head = total
    ? `${formatCount(total)} of ${formatCount(r.rows)} values look out of place: ${formatCount(r.high)} high, ${formatCount(r.low)} low.`
    : `Nothing stands out among ${formatCount(r.rows)} values.`;
  const one = !r.by ? r.fences[0] : undefined;
  return h(
    "div",
    {},
    h("p", { class: `analysis-verdict ${total ? "off" : "ok"}` }, head),
    one && one.low_fence !== null ? h("p", {}, `Usual range: ${fmt(one.low_fence)} to ${fmt(one.high_fence)} (median ${fmt(one.median)}).`) : null,
    r.by
      ? h(
          "p",
          { class: "hint" },
          `Judged within each ${r.by}: ${formatCount(r.groups_flagged)} of ${formatCount(r.groups)} have outliers.`,
          r.groups_unjudged ? ` ${formatCount(r.groups_unjudged)} have fewer than 5 values and were not judged.` : "",
        )
      : null,
    r.by && r.fences.length
      ? h(
          "table",
          { class: "small" },
          h("thead", {}, h("tr", {}, h("th", {}, r.by), h("th", { class: "num" }, "Values"), h("th", {}, "Usual range"), h("th", { class: "num" }, "Low"), h("th", { class: "num" }, "High"))),
          h("tbody", {}, ...r.fences.map((f) => h("tr", {}, h("td", {}, fmt(f.group)), h("td", { class: "num" }, formatCount(f.rows)), h("td", {}, `${fmt(f.low_fence)} to ${fmt(f.high_fence)}`), h("td", { class: "num" }, formatCount(f.low)), h("td", { class: "num" }, formatCount(f.high))))),
        )
      : null,
    r.outliers.length
      ? h(
          "table",
          { class: "small" },
          h("thead", {}, h("tr", {}, r.by ? h("th", {}, r.by) : null, h("th", { class: "num" }, column), h("th", { class: "num" }, "Score"))),
          h("tbody", {}, ...r.outliers.map((o) => h("tr", {}, r.by ? h("td", {}, fmt(o.group)) : null, h("td", { class: "num" }, fmt(o.value)), h("td", { class: "num" }, `${o.score > 0 ? "+" : ""}${fmt(o.score)}`)))),
        )
      : null,
    r.step_note ? h("p", { class: "hint" }, r.step_note) : null,
  );
}

function frequencyView(r: FrequencyResult): HTMLElement {
  const total = r.rarer + r.commoner;
  const head = !r.judged
    ? `Only ${formatCount(r.distinct)} different values: too few to judge.`
    : total
      ? `${formatCount(total)} of ${formatCount(r.distinct)} values stand out: ${formatCount(r.rarer)} rarer and ${formatCount(r.commoner)} commoner than the usual ${fmt(r.typical)} rows per value.`
      : `Nothing stands out among ${formatCount(r.distinct)} values (usually ${fmt(r.typical)} rows each).`;
  return h(
    "div",
    {},
    h("p", { class: `analysis-verdict ${total ? "off" : "ok"}` }, head),
    r.outliers.length
      ? h(
          "table",
          { class: "small" },
          h("thead", {}, h("tr", {}, h("th", {}, "Value"), h("th", { class: "num" }, "Rows"), h("th", {}, ""), h("th", { class: "num" }, "Score"))),
          h("tbody", {}, ...r.outliers.map((o) => h("tr", {}, h("td", {}, o.value), h("td", { class: "num" }, formatCount(o.count)), h("td", { class: "hint" }, o.score < 0 ? "rarer" : "commoner"), h("td", { class: "num" }, fmt(o.score))))),
        )
      : null,
  );
}

// ===== odds =====

interface OddsValue {
  value: string;
  rows: number;
  share: number;
  one_in: number;
  odds_against: number;
  outcome_rows?: number;
  rate?: number;
  rest_rate?: number | null;
  odds_ratio?: number;
  low?: number;
  high?: number;
  clear?: boolean;
  small?: boolean;
}

interface OddsResult {
  column: string;
  bucketed: boolean;
  blanks: number;
  rows: number;
  outcome: string | null;
  rate?: number;
  values: OddsValue[];
  together?: { verdict: string; effect_size?: number; effect?: string };
}

const OPS: [string, string][] = [
  ["values", "is one of"],
  ["gt", "is above"],
  ["ge", "is at least"],
  ["lt", "is below"],
  ["le", "is at most"],
  ["eq", "equals"],
];

export async function openOdds(column?: string): Promise<void> {
  const { body, foot, close } = openDialog("Odds", true);
  const which = select("Column", columnOptions(), column ?? viewColumns()[0]?.name ?? "");
  const buckets = h("input", { type: "number", min: "2", max: "20", size: 4, placeholder: "auto", "aria-label": "Buckets for a number column" });
  const outcomeColumn = select("Outcome column", columnOptions("(no outcome)"), "");
  const op = select("Outcome test", OPS, "values");
  const outcomeValue = h("input", { type: "text", size: 16, placeholder: "Value", "aria-label": "Outcome value" });
  const outcomeRow = h("span", { class: "analysis-inline" }, op, outcomeValue);
  const sync = (): void => {
    outcomeRow.hidden = !outcomeColumn.value;
  };
  outcomeColumn.addEventListener("change", sync);
  const results = h("div", { class: "analysis-results" });
  const actions = h("span", { class: "analysis-actions" });
  const run = async (): Promise<void> => {
    const sent: Record<string, unknown> = { column: which.value };
    if (buckets.value) sent.buckets = Number(buckets.value);
    if (outcomeColumn.value) {
      const text = outcomeValue.value.trim();
      if (!text) {
        clear(results, h("p", { class: "hint" }, "Enter the outcome value."));
        return;
      }
      sent.outcome = op.value === "values"
        ? { column: outcomeColumn.value, values: text.split(",").map((v) => v.trim()).filter(Boolean) }
        : { column: outcomeColumn.value, op: op.value, value: Number(text) };
    }
    clear(results, h("p", { class: "hint" }, "Counting…"));
    clear(actions);
    try {
      const answer = await api<OddsResult>(`${grid}/odds`, "POST", sent);
      clear(results, oddsView(answer));
      const handoffs = answer.outcome
        ? [button("Test together", () => { close(); void openFitTest([answer.column, outcomeColumn.value], "together"); }, { icon: "fit", title: "Chi-square test" })]
        : [];
      clear(
        actions,
        ...handoffs,
        ...resultButtons({
          analysis: "odds",
          body: sent,
          result: () => (answer),
          cards: () => oddsCards(answer),
          rows: () => ({ name: tableName(datasetName(), "odds", answer.column), rows: answer.values.map((v) => ({ ...v })) }),
        }),
      );
    } catch (e) {
      clear(results, h("p", { class: "status-error" }, (e as Error).message));
    }
  };
  sync();
  body.append(
    h("div", { class: "analysis-controls" }, field("Column", which), field("Buckets", buckets), field("Outcome", outcomeColumn), outcomeRow, button("Count", () => void run(), { icon: "play", class: "primary" })),
    cheatSheet("odds", () => [which.value, outcomeColumn.value].filter(Boolean)).element,
    results,
  );
  markStaleOnChange(body, results);
  foot.append(actions, button("Close", close));
  await run();
}

function oddsView(r: OddsResult): HTMLElement {
  const withOutcome = Boolean(r.outcome);
  const top = r.values[0];
  const verdict = withOutcome
    ? `${r.outcome}: ${percent(r.rate)} of ${formatCount(r.rows)} rows. ${r.together?.verdict ?? ""}`
    : `${formatCount(r.values.length)} values over ${formatCount(r.rows)} rows${r.blanks ? ` (${formatCount(r.blanks)} blank, left out)` : ""}.`;
  const headline = withOutcome && top?.clear ? `${top.value}: ${r.outcome} ${percent(top.rate)} of the time, against ${percent(top.rest_rate)} elsewhere: ${fmt(top.odds_ratio)} times the odds.` : null;
  return h(
    "div",
    {},
    h("p", { class: `analysis-verdict ${withOutcome && top?.clear ? "off" : "ok"}` }, verdict),
    headline ? h("p", { class: "analysis-headline" }, headline) : null,
    r.bucketed ? h("p", { class: "hint" }, `${r.column} in equal-sized ranges.`) : null,
    h(
      "table",
      { class: "small odds-table" },
      h(
        "thead",
        {},
        h(
          "tr",
          {},
          h("th", {}, r.column),
          h("th", { class: "num" }, "Rows"),
          h("th", { class: "num" }, "Share"),
          h("th", { class: "num", title: "On any one row" }, "1 in"),
          withOutcome ? h("th", { class: "num" }, "Outcome rate") : null,
          withOutcome ? h("th", { class: "num" }, "Elsewhere") : null,
          withOutcome ? h("th", { class: "num", title: "With its 95% range" }, "Odds ratio") : null,
        ),
      ),
      h(
        "tbody",
        {},
        ...r.values.slice(0, 60).map((v) =>
          h(
            "tr",
            { class: v.clear ? (v.odds_ratio! > 1 ? "odds-up" : "odds-down") : "" },
            h("td", {}, v.value),
            h("td", { class: "num" }, formatCount(v.rows)),
            h("td", { class: "num" }, percent(v.share)),
            h("td", { class: "num" }, fmt(v.one_in)),
            withOutcome ? h("td", { class: "num" }, percent(v.rate), v.small ? h("span", { class: "hint", title: "Under 30 rows: rough" }, " *") : null) : null,
            withOutcome ? h("td", { class: "num" }, percent(v.rest_rate)) : null,
            withOutcome ? h("td", { class: "num" }, `${fmt(v.odds_ratio)} (${fmt(v.low)} to ${fmt(v.high)})`) : null,
          ),
        ),
      ),
    ),
    withOutcome ? h("p", { class: "hint" }, "* Under 30 rows: rough.") : null,
  );
}

// ===== find predictors =====

interface Candidate {
  column: string;
  kind: string;
  bucketed: boolean;
  v: number;
  strength: string;
  p: number;
  rows: number;
  correlation: number | null;
  note: string | null;
  same_as?: string;
}

interface PredictorsResult {
  target: string;
  target_bucketed: boolean;
  candidates: Candidate[];
  skipped: { column: string; reason: string }[];
  redundant: { keep: string; drop: string; v: number }[];
}

export async function openPredictors(target?: string): Promise<void> {
  const { body, foot, close } = openDialog("Find predictors", true);
  const which = select("Target", columnOptions(), target ?? viewColumns()[0]?.name ?? "", () => rebuild());
  const buckets = h("input", { type: "number", min: "2", max: "20", size: 4, placeholder: "4", "aria-label": "Buckets for number columns" });
  let pool = checklist("Candidates", [], [which.value]);
  const poolSlot = h("div", {}, pool.element);
  const rebuild = (): void => {
    pool = checklist("Candidates", [], [which.value]);
    clear(poolSlot, pool.element);
  };
  rebuild();
  const results = h("div", { class: "analysis-results" });
  const actions = h("span", { class: "analysis-actions" });
  const run = async (): Promise<void> => {
    const sent: Record<string, unknown> = { target: which.value };
    if (pool.values().length) sent.candidates = pool.values();
    if (buckets.value) sent.buckets = Number(buckets.value);
    clear(results, h("p", { class: "hint" }, "Testing…"));
    clear(actions);
    try {
      const answer = await api<PredictorsResult>(`${grid}/predictors`, "POST", sent);
      clear(results, predictorsView(answer, (column) => { close(); void openFitTest([answer.target, column], "together"); }));
      // The strong and moderate candidates, one of each redundant pair: the regression's predictors.
      const picks = answer.candidates.filter((c) => (c.strength === "strong" || c.strength === "moderate") && !c.same_as).map((c) => c.column);
      clear(
        actions,
        picks.length ? button("Regression with these", () => { close(); void openRegression(answer.target, picks); }, { icon: "fit", class: "primary", title: `A regression of ${answer.target} on ${picks.join(", ")}` }) : null,
        ...resultButtons({
          analysis: "predictors",
          body: sent,
          result: () => (answer),
          cards: () => predictorsCards(answer),
          rows: () => ({ name: tableName(datasetName(), "predictors", answer.target), rows: answer.candidates.map((c) => ({ column: c.column, kind: c.kind, strength: c.strength, cramers_v: c.v, p: c.p, correlation: c.correlation, same_as: c.same_as ?? null, note: c.note })) }),
        }),
      );
    } catch (e) {
      clear(results, h("p", { class: "status-error" }, (e as Error).message));
    }
  };
  body.append(
    h("div", { class: "analysis-controls" }, field("Target", which), field("Buckets", buckets), button("Find", () => void run(), { icon: "play", class: "primary" })),
    poolSlot,
    cheatSheet("predictors", () => [which.value, ...pool.values()]).element,
    results,
  );
  markStaleOnChange(body, results);
  foot.append(actions, button("Close", close));
  if (target) await run();
}

function predictorsView(r: PredictorsResult, together: (column: string) => void): HTMLElement {
  const strong = r.candidates.filter((c) => c.strength === "strong" || c.strength === "moderate");
  const head = strong.length
    ? `${strong.map((c) => c.column).join(", ")} ${strong.length === 1 ? "goes" : "go"} with ${r.target}.`
    : `No column goes clearly with ${r.target}.`;
  return h(
    "div",
    {},
    h("p", { class: `analysis-verdict ${strong.length ? "off" : "ok"}` }, head),
    r.target_bucketed ? h("p", { class: "hint" }, `${r.target} in equal-sized ranges.`) : null,
    h(
      "table",
      { class: "small" },
      h("thead", {}, h("tr", {}, h("th", {}, "Column"), h("th", {}, "Strength"), h("th", { class: "num", title: "0 none, 1 decides it" }, "Cramér's V"), h("th", { class: "num", title: "Number columns only" }, "Correlation"), h("th", {}, ""), h("th", {}, ""))),
      h(
        "tbody",
        {},
        ...r.candidates.map((c) =>
          h(
            "tr",
            { class: `strength-${c.strength}` },
            h("td", {}, c.column, c.bucketed ? h("span", { class: "hint" }, " (in ranges)") : null),
            h("td", {}, h("span", { class: "strength-bar", style: { "--v": String(Math.min(1, c.v / 0.5)) } }), c.strength),
            h("td", { class: "num" }, fmt(c.v)),
            h("td", { class: "num" }, c.correlation === null ? "" : fmt(c.correlation)),
            h("td", { class: "hint" }, c.same_as ? `says the same as ${c.same_as}` : (c.note ?? "")),
            h("td", {}, button("Test together", () => together(c.column), { class: "ghost small-btn", title: `Fit test: ${r.target} and ${c.column} together` })),
          ),
        ),
      ),
    ),
    r.skipped.length ? h("p", { class: "hint" }, "Skipped: ", r.skipped.map((s) => `${s.column} (${s.reason})`).join("; "), ".") : null,
  );
}
