/**
 * analysisDialog.ts: the Column row's Fit test and Outliers windows (server: routes_analysis.py).
 *
 *   [FIT TEST]   openFitTest(columns): expected spread (even, first digits, another grid, together)
 *                -> POST /grids/{id}/fit-test; per column: the verdict in words, chi-square, p, the
 *                effect size, and each value's observed count against the expected (a bar and a tick)
 *   [OUTLIERS]   openOutliers(column): amount (robust, quartiles, z-score; within each group) or
 *                frequency -> POST /grids/{id}/outliers; Show these rows -> state.addStep(result.step)
 */
import { api, grid } from "./api";
import { button, clear, formatCount, h, isNumericType, openDialog, toast } from "./dom";
import { viewColumns } from "./gridView";
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

function tile(label: string, value: string, title?: string): HTMLElement {
  return h("div", { class: "stat", title }, h("div", { class: "label" }, label), h("div", { class: "value" }, value));
}

function select(label: string, options: [string, string][], value: string, onchange?: () => void): HTMLSelectElement {
  const el = h("select", { "aria-label": label, onchange }, ...options.map(([v, text]) => h("option", { value: v, selected: v === value }, text)));
  return el;
}

function field(label: string, control: HTMLElement): HTMLElement {
  return h("label", { class: "analysis-field" }, h("span", {}, label), control);
}

// ===== the fit test =====

const EXPECTED: [string, string][] = [
  ["even", "Even: every value as likely"],
  ["benford", "First digit (Benford's law)"],
  ["benford2", "First two digits (Benford's law)"],
  ["grid", "Another grid's spread"],
  ["together", "Together: are the two related?"],
];

export async function openFitTest(columns: string[]): Promise<void> {
  if (!columns.length) return;
  const { body, foot, close } = openDialog(`Fit test: ${columns.join(", ")}`, true);
  const types = new Map(viewColumns().map((c) => [c.name, c.type]));
  const allNumbers = columns.every((c) => isNumericType(types.get(c) ?? ""));
  const expected = select("Expected spread", EXPECTED, columns.length === 2 && !allNumbers ? "together" : allNumbers ? "benford" : "even", () => {
    referenceRow.hidden = expected.value !== "grid";
  });
  const others = h("select", { "aria-label": "The grid to compare with" });
  const otherColumn = h("input", { type: "text", "aria-label": "Its column", placeholder: columns[0] ?? "", size: 14 });
  const referenceRow = h("div", { class: "analysis-controls", hidden: true }, field("Grid", others), field("Column", otherColumn));
  void api<{ grids: { grid: string; dataset: string }[] }>("/grids").then(({ grids }) => {
    const current = decodeURIComponent(grid.split("/").pop() ?? "");
    others.append(...grids.filter((g) => g.grid !== current).map((g) => h("option", { value: g.grid }, g.dataset)));
    if (!others.options.length) others.append(h("option", { value: "", disabled: true }, "No other grid is open"));
  });
  const results = h("div", { class: "analysis-results" });
  const run = async (): Promise<void> => {
    clear(results, h("p", { class: "hint" }, "Testing…"));
    const sent: Record<string, unknown> = { columns, expected: expected.value };
    if (expected.value === "grid") sent.reference = { grid: others.value, column: otherColumn.value.trim() || undefined };
    try {
      const answer = await api<{ results?: FitResult[]; expected: string } & FitResult>(`${grid}/fit-test`, "POST", sent);
      clear(results, ...(answer.results ? answer.results.map((r) => fitCard(r)) : [fitCard(answer, columns)]));
    } catch (e) {
      clear(results, h("p", { class: "status-error" }, (e as Error).message));
    }
  };
  body.append(
    h("div", { class: "analysis-controls" }, field("Expected", expected), button("Test", () => void run(), { icon: "play", class: "primary" })),
    referenceRow,
    h(
      "p",
      { class: "hint" },
      "Chi-square goodness of fit: could the differences between what the column holds and what you expect be chance? Read the effect too: with many rows, small differences are certain but may not matter.",
    ),
    results,
  );
  foot.append(button("Close", close, { class: "primary" }));
  await run();
}

function fitCard(r: FitResult, pair?: string[]): HTMLElement {
  const fits = r.p >= 0.05;
  const tiles = [
    tile("Chi-square", fmt(r.statistic)),
    tile("Degrees of freedom", formatCount(r.df)),
    tile("p-value", pText(r.p), "The chance of differences at least this big if the expectation were true"),
    tile("Rows", formatCount(r.n)),
    tile(r.effect_name, `${fmt(r.effect_size)} (${r.effect})`, "How big the difference is, whatever the row count: 0.1 small, 0.3 medium, 0.5 large"),
  ];
  if (r.conformity) tiles.push(tile("Benford conformity", r.conformity, `Mean absolute deviation of the digit shares: ${fmt(r.conformity_mad)} (Nigrini)`));
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
        h("span", { class: "num", title: "Standardized residual: (seen - expected) / sqrt(expected); beyond 2 is notable" }, `${r.residual > 0 ? "+" : ""}${fmt(r.residual)}`),
      ),
    ),
    rows.length > shown.length ? h("p", { class: "hint" }, `and ${formatCount(rows.length - shown.length)} more values`) : null,
  );
}

function cellTable(cells: NonNullable<FitResult["cells"]>, pair: string[]): HTMLElement {
  return h(
    "table",
    { class: "small" },
    h("caption", { class: "hint" }, "The combinations furthest from independence"),
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
  ["robust", "Robust: distance from the median"],
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
}

export async function openOutliers(column: string): Promise<void> {
  const { body, foot, close } = openDialog(`Outliers in ${column}`, true);
  const columns = viewColumns();
  const numeric = isNumericType(columns.find((c) => c.name === column)?.type ?? "");
  const look = select("Look at", [["amount", "Amount: values far from the rest"], ["frequency", "Frequency: values seen too often or too rarely"]], numeric ? "amount" : "frequency", () => sync());
  if (!numeric) look.options[0]!.disabled = true;
  const method = select("Method", METHODS, "robust", () => (threshold.placeholder = DEFAULT[method.value] ?? ""));
  const threshold = h("input", { type: "number", min: "0.1", step: "0.1", size: 5, placeholder: DEFAULT.robust, "aria-label": "How far out counts" });
  const by = select("Within each", [["", "(the whole column)"], ...columns.filter((c) => c.name !== column).map((c): [string, string] => [c.name, c.name])], "");
  const amountOnly = [field("Method", method), field("How far out", threshold), field("Within each", by)];
  const sync = (): void => amountOnly.forEach((f) => (f.hidden = look.value !== "amount"));
  const results = h("div", { class: "analysis-results" });
  let step: Step | null = null;
  const show = button("Show these rows", () => void showRows(), { icon: "filter", class: "primary", title: "Add a step that keeps only these rows (undo it like any step)" });
  show.disabled = true;
  const showRows = async (): Promise<void> => {
    if (!step) return;
    await state.addStep(step).then(close, (e: Error) => toast(e.message, "error"));
  };
  const run = async (): Promise<void> => {
    clear(results, h("p", { class: "hint" }, "Looking…"));
    show.disabled = true;
    const sent: Record<string, unknown> = { column, look: look.value };
    if (look.value === "amount") Object.assign(sent, { method: method.value, threshold: threshold.value || undefined, by: by.value || undefined });
    try {
      const answer = await api<AmountResult | FrequencyResult>(`${grid}/outliers`, "POST", sent);
      step = answer.step;
      show.disabled = !step;
      clear(results, answer.look === "amount" ? amountView(answer, column) : frequencyView(answer));
    } catch (e) {
      clear(results, h("p", { class: "status-error" }, (e as Error).message));
    }
  };
  sync();
  body.append(h("div", { class: "analysis-controls" }, field("Look at", look), ...amountOnly, button("Look", () => void run(), { icon: "play" })), results);
  foot.append(show, button("Close", close));
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
          h("caption", { class: "hint" }, "The furthest out first. Score: how many spreads from the middle."),
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
          h("caption", { class: "hint" }, "Rarer values are often typos; commoner ones, copies or defaults."),
          h("thead", {}, h("tr", {}, h("th", {}, "Value"), h("th", { class: "num" }, "Rows"), h("th", {}, ""), h("th", { class: "num" }, "Score"))),
          h("tbody", {}, ...r.outliers.map((o) => h("tr", {}, h("td", {}, o.value), h("td", { class: "num" }, formatCount(o.count)), h("td", { class: "hint" }, o.score < 0 ? "rarer" : "commoner"), h("td", { class: "num" }, fmt(o.score))))),
        )
      : null,
  );
}
