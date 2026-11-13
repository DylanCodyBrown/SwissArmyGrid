/**
 * analysisCards.ts: an analysis result as Sandbox cards that read as a report (Plans/sandbox-report.plan.md,
 * S1): a note (a title, the verdict, a table of the key rows, the key numbers in bold, the cautions) and a
 * Plotly chart beside it, the two sized to share one row. Charts name the theme's colors ("theme:accent"),
 * which lazy/plotly.ts resolves when it draws, so they look right in every theme.
 */

export interface SandboxCard {
  kind: "note" | "plotly";
  title: string;
  body: string;
  size: { w: number; h: number };
}

const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const whole = new Intl.NumberFormat("en-US");
export const fmt = (v: unknown): string => (v === null || v === undefined ? "" : typeof v === "number" ? (Number.isInteger(v) ? whole.format(v) : number.format(v)) : String(v));
const pct = (v: number | null | undefined): string => (v === null || v === undefined ? "" : `${(100 * v).toFixed(v < 0.1 ? 1 : 0)}%`);
const pText = (p: number): string => (p < 0.0001 ? "< 0.0001" : p.toFixed(4));
/** A cell's text: a bar inside a value would end the cell early. */
const cell = (v: unknown): string => fmt(v).replace(/\|/g, "\\|").replace(/\n/g, " ");

export function mdTable(head: string[], rows: unknown[][], right: boolean[] = []): string {
  const rule = head.map((_, i) => (right[i] ? "---:" : "---"));
  return [`| ${head.join(" | ")} |`, `| ${rule.join(" | ")} |`, ...rows.map((r) => `| ${r.map(cell).join(" | ")} |`)].join("\n");
}

/** A note and its chart, sized to sit side by side: the note five columns, the chart seven. */
function pair(title: string, markdown: string, figure: Record<string, unknown> | null): SandboxCard[] {
  const lines = markdown.split("\n").length;
  const h = Math.max(7, Math.min(18, Math.ceil((lines * 23 + 60) / 52)));
  const cards: SandboxCard[] = [{ kind: "note", title, body: markdown, size: { w: figure ? 5 : 12, h } }];
  if (figure) cards.push({ kind: "plotly", title: `${title} (chart)`, body: JSON.stringify(figure), size: { w: 7, h } });
  return cards;
}

const BASE_LAYOUT = { margin: { l: 56, r: 16, t: 12, b: 48 }, font: { size: 12 }, hovermode: "closest" };

// ===== the fit test =====

export interface FitCardInput {
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
  categories?: { value: string; observed: number; expected: number; residual: number }[];
  conformity?: string;
  conformity_mad?: number;
  cells?: { a: string; b: string; observed: number; expected: number; residual: number }[];
}

export function fitCards(r: FitCardInput, expected: string, pairNames?: string[]): SandboxCard[] {
  const what = pairNames ? `${pairNames[0]} and ${pairNames[1]}` : (r.column ?? "");
  const lines = [
    `# Fit test: ${what}`,
    `**${r.verdict}**`,
    "",
    mdTable(["Chi-square", "Degrees of freedom", "p-value", "Rows", r.effect_name], [[fmt(r.statistic), fmt(r.df), pText(r.p), fmt(r.n), `${fmt(r.effect_size)} (${r.effect})`]], [true, true, true, true, true]),
  ];
  if (r.conformity) lines.push("", `Benford's law: **${r.conformity}** (mean absolute deviation of the digit shares ${number.format(r.conformity_mad ?? 0)}, Nigrini's ranges).`);
  let figure: Record<string, unknown> | null = null;
  if (r.categories) {
    const far = [...r.categories].sort((a, b) => Math.abs(b.residual) - Math.abs(a.residual)).slice(0, 8);
    lines.push("", "#### Furthest from expected", mdTable(["Value", "Seen", "Expected", "Off by"], far.map((c) => [c.value, c.observed, c.expected, `${c.residual > 0 ? "+" : ""}${fmt(c.residual)}`]), [false, true, true, true]));
    const shown = expected.startsWith("benford") ? r.categories : r.categories.slice(0, 30);
    figure = {
      data: [
        { type: "bar", name: "Seen", x: shown.map((c) => c.value), y: shown.map((c) => c.observed), marker: { color: shown.map((c) => (Math.abs(c.residual) >= 2 ? "theme:accent" : "theme:border-strong")) } },
        { type: "scatter", mode: "lines+markers", name: "Expected", x: shown.map((c) => c.value), y: shown.map((c) => c.expected), line: { color: "theme:fg", width: 2 }, marker: { size: 5 } },
      ],
      layout: { ...BASE_LAYOUT, xaxis: { type: "category", title: { text: expected.startsWith("benford") ? "First digits" : (r.column ?? "") } }, yaxis: { title: { text: "Rows" } }, legend: { orientation: "h", y: 1.08 }, bargap: 0.25 },
    };
  } else if (r.cells && pairNames) {
    const top = r.cells.slice(0, 10);
    lines.push("", "#### The combinations furthest from independence", mdTable([pairNames[0] ?? "", pairNames[1] ?? "", "Seen", "Expected", "Off by"], top.map((c) => [c.a, c.b, c.observed, c.expected, `${c.residual > 0 ? "+" : ""}${fmt(c.residual)}`]), [false, false, true, true, true]));
    const bars = [...top].reverse();
    figure = {
      data: [{ type: "bar", orientation: "h", x: bars.map((c) => c.residual), y: bars.map((c) => `${c.a} · ${c.b}`), marker: { color: bars.map((c) => (c.residual > 0 ? "theme:accent" : "theme:ok")) }, hovertemplate: "%{y}: %{x:.2f}<extra></extra>" }],
      layout: { ...BASE_LAYOUT, margin: { l: 140, r: 16, t: 12, b: 48 }, xaxis: { title: { text: "Seen against expected (standardized)" }, zeroline: true }, yaxis: { automargin: true } },
    };
  }
  if (r.warnings.length) lines.push("", ...r.warnings.map((w) => `> ${w}`));
  return pair(`Fit test: ${what}`, lines.join("\n"), figure);
}

// ===== odds =====

export interface OddsCardInput {
  column: string;
  rows: number;
  outcome: string | null;
  rate?: number;
  bucketed: boolean;
  values: { value: string; rows: number; share: number; one_in: number; rate?: number; rest_rate?: number | null; odds_ratio?: number; low?: number; high?: number; clear?: boolean; small?: boolean }[];
  together?: { verdict: string };
}

export function oddsCards(r: OddsCardInput): SandboxCard[] {
  if (!r.outcome) {
    const top = r.values.slice(0, 15);
    const markdown = [
      `# How often each ${r.column} shows up`,
      `**${fmt(r.values.length)} values over ${fmt(r.rows)} rows.**${r.bucketed ? ` ${r.column} is a number, shown in equal-sized ranges.` : ""}`,
      "",
      mdTable([r.column, "Rows", "Share", "1 in"], top.map((v) => [v.value, v.rows, pct(v.share), fmt(v.one_in)]), [false, true, true, true]),
    ].join("\n");
    const bars = [...top].reverse();
    return pair(`Odds: ${r.column}`, markdown, {
      data: [{ type: "bar", orientation: "h", x: bars.map((v) => v.share), y: bars.map((v) => v.value), marker: { color: "theme:accent" }, hovertemplate: "%{y}: %{x:.1%}<extra></extra>" }],
      layout: { ...BASE_LAYOUT, margin: { l: 110, r: 16, t: 12, b: 48 }, xaxis: { title: { text: "Share of rows" }, tickformat: ".0%" }, yaxis: { automargin: true, type: "category" } },
    });
  }
  const top = r.values[0];
  const clear = r.values.filter((v) => v.clear);
  const lines = [
    `# Odds of ${r.outcome} by ${r.column}`,
    `${r.outcome}: **${pct(r.rate)}** of ${fmt(r.rows)} rows. ${r.together?.verdict ?? ""}`,
  ];
  if (top?.clear) lines.push("", `**${top.value}: ${r.outcome} ${pct(top.rate)} of the time, against ${pct(top.rest_rate)} elsewhere: ${fmt(top.odds_ratio)} times the odds.**`);
  lines.push(
    "",
    mdTable(
      [r.column, "Rows", `${r.outcome} rate`, "Elsewhere", "Odds ratio", "95% range"],
      r.values.slice(0, 15).map((v) => [`${v.clear ? "**" : ""}${v.value}${v.clear ? "**" : ""}${v.small ? " *" : ""}`, v.rows, pct(v.rate), pct(v.rest_rate), fmt(v.odds_ratio), `${fmt(v.low)} to ${fmt(v.high)}`]),
      [false, true, true, true, true, true],
    ),
    "",
    `> Bold values are clearly different: their range leaves out 1.${r.values.some((v) => v.small) ? " * fewer than 30 rows: rough." : ""}${clear.length ? "" : " No value is clearly different."}`,
  );
  const shown = [...r.values.slice(0, 20)].reverse();
  const figure = {
    data: [
      {
        type: "scatter",
        mode: "markers",
        x: shown.map((v) => v.odds_ratio),
        y: shown.map((v) => v.value),
        error_x: { type: "data", symmetric: false, array: shown.map((v) => (v.high ?? 0) - (v.odds_ratio ?? 0)), arrayminus: shown.map((v) => (v.odds_ratio ?? 0) - (v.low ?? 0)), color: "theme:muted", thickness: 1.5, width: 4 },
        marker: { size: 10, color: shown.map((v) => (!v.clear ? "theme:muted" : (v.odds_ratio ?? 1) > 1 ? "theme:accent" : "theme:ok")) },
        hovertemplate: "%{y}: %{x:.2f} times the odds<extra></extra>",
      },
    ],
    layout: {
      ...BASE_LAYOUT,
      margin: { l: 110, r: 16, t: 12, b: 48 },
      xaxis: { type: "log", title: { text: `Odds ratio for ${r.outcome} (log scale; 1 means no difference)` }, ...logTicks(shown.flatMap((v) => [v.low ?? 1, v.high ?? 1])) },
      yaxis: { automargin: true, type: "category" },
      shapes: [{ type: "line", x0: 1, x1: 1, yref: "paper", y0: 0, y1: 1, line: { color: "theme:fg", width: 1, dash: "dot" } }],
      showlegend: false,
    },
  };
  return pair(`Odds of ${r.outcome} by ${r.column}`, lines.join("\n"), figure);
}

/** Readable ticks for a log axis of ratios: 0.5, 0.7, 1, 1.5, 2, 3, 5, 10... within the data's range. */
function logTicks(values: number[]): Record<string, unknown> {
  const finite = values.filter((v) => Number.isFinite(v) && v > 0);
  const low = Math.min(1, ...finite) / 1.15;
  const high = Math.max(1, ...finite) * 1.15;
  const nice = [0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 1, 1.5, 2, 3, 5, 7, 10, 20, 50, 100, 200, 500];
  const ticks = nice.filter((v) => v >= low && v <= high);
  return { range: [Math.log10(low), Math.log10(high)], tickvals: ticks, ticktext: ticks.map(String), minor: { showgrid: false } };
}

// ===== find predictors =====

export interface PredictorsCardInput {
  target: string;
  target_bucketed: boolean;
  candidates: { column: string; strength: string; v: number; correlation: number | null; note: string | null; same_as?: string; bucketed: boolean }[];
  skipped: { column: string; reason: string }[];
  redundant: { keep: string; drop: string }[];
}

const STRENGTH_COLOR: Record<string, string> = { strong: "theme:accent", moderate: "theme:warn", weak: "theme:muted", none: "theme:border" };

export function predictorsCards(r: PredictorsCardInput): SandboxCard[] {
  const worth = r.candidates.filter((c) => c.strength === "strong" || c.strength === "moderate");
  const lines = [
    `# What goes with ${r.target}`,
    worth.length ? `**${worth.map((c) => c.column).join(", ")} ${worth.length === 1 ? "goes" : "go"} with ${r.target}.**` : `**No column goes clearly with ${r.target}.**`,
    "",
    mdTable(
      ["Column", "Strength", "Cramér's V", "Correlation", "Note"],
      r.candidates.slice(0, 15).map((c) => [c.column, c.strength, fmt(c.v), c.correlation === null ? "" : fmt(c.correlation), c.same_as ? `says the same as ${c.same_as}` : (c.note ?? "")]),
      [false, false, true, true, false],
    ),
  ];
  if (r.redundant.length) lines.push("", `For a regression keep one of each pair: ${r.redundant.map((p) => `${p.keep} (not ${p.drop})`).join(", ")}.`);
  if (r.skipped.length) lines.push("", `> Skipped: ${r.skipped.map((s) => `${s.column} (${s.reason})`).join("; ")}.`);
  const bars = [...r.candidates.slice(0, 15)].reverse();
  const figure = {
    data: [
      {
        type: "bar",
        orientation: "h",
        x: bars.map((c) => c.v),
        y: bars.map((c) => (c.same_as ? `${c.column} (= ${c.same_as})` : c.column)),
        marker: { color: bars.map((c) => STRENGTH_COLOR[c.strength] ?? "theme:muted"), opacity: bars.map((c) => (c.same_as ? 0.45 : 1)) },
        hovertemplate: "%{y}: Cramér's V %{x:.2f}<extra></extra>",
      },
    ],
    layout: {
      ...BASE_LAYOUT,
      margin: { l: 120, r: 16, t: 12, b: 48 },
      xaxis: { title: { text: `How much each tells about ${r.target} (Cramér's V)` }, range: [0, Math.max(0.35, ...bars.map((c) => c.v)) * 1.05] },
      yaxis: { automargin: true, type: "category" },
      shapes: [0.1, 0.2, 0.3].map((x) => ({ type: "line", x0: x, x1: x, yref: "paper", y0: 0, y1: 1, line: { color: "theme:border-strong", width: 1, dash: "dot" } })),
      annotations: [
        { x: 0.15, y: 1.04, yref: "paper", text: "weak", showarrow: false, font: { size: 10, color: "theme:muted" } },
        { x: 0.25, y: 1.04, yref: "paper", text: "moderate", showarrow: false, font: { size: 10, color: "theme:muted" } },
        { x: 0.33, y: 1.04, yref: "paper", text: "strong", showarrow: false, font: { size: 10, color: "theme:muted" }, xanchor: "left" },
      ],
      showlegend: false,
    },
  };
  return pair(`Predictors of ${r.target}`, lines.join("\n"), figure);
}

// ===== outliers =====

export interface AmountCardInput {
  column: string;
  by: string | null;
  method: string;
  rows: number;
  low: number;
  high: number;
  groups: number;
  groups_flagged: number;
  fences: { group: string | null; rows: number; low_fence: number | null; high_fence: number | null; median: number; low: number; high: number }[];
  outliers: { group: string | null; value: number; score: number }[];
}

const METHOD_WORDS: Record<string, string> = { robust: "distance from the median", iqr: "quartile fences (Tukey)", zscore: "mean and standard deviation" };

export function amountCards(r: AmountCardInput): SandboxCard[] {
  const total = r.low + r.high;
  const lines = [
    `# Outliers in ${r.column}${r.by ? ` within each ${r.by}` : ""}`,
    total ? `**${fmt(total)} of ${fmt(r.rows)} values look out of place: ${fmt(r.high)} high, ${fmt(r.low)} low.**` : `**Nothing stands out among ${fmt(r.rows)} values.**`,
    "",
    `Judged by ${METHOD_WORDS[r.method] ?? r.method}.`,
  ];
  const one = !r.by ? r.fences[0] : undefined;
  if (one && one.low_fence !== null) lines.push(`Usual range: **${fmt(one.low_fence)} to ${fmt(one.high_fence)}** (median ${fmt(one.median)}).`);
  if (r.by && r.fences.length) {
    lines.push("", `#### ${fmt(r.groups_flagged)} of ${fmt(r.groups)} ${r.by} values have outliers`, mdTable([r.by, "Values", "Usual range", "Low", "High"], r.fences.slice(0, 10).map((f) => [f.group ?? "(blank)", f.rows, `${fmt(f.low_fence)} to ${fmt(f.high_fence)}`, f.low, f.high]), [false, true, false, true, true]));
  }
  if (r.outliers.length) {
    lines.push("", "#### The furthest out", mdTable([...(r.by ? [r.by] : []), r.column, "Score"], r.outliers.slice(0, 10).map((o) => [...(r.by ? [o.group ?? "(blank)"] : []), o.value, `${o.score > 0 ? "+" : ""}${fmt(o.score)}`]), [...(r.by ? [false] : []), true, true]));
  }
  lines.push("", "> An outlier is a question, not proof: look at its rows before deciding.");
  const groups = r.by ? [...new Set([...r.fences.map((f) => f.group ?? "(blank)"), ...r.outliers.map((o) => o.group ?? "(blank)")])] : ["all"];
  const y = (g: string | null): string => (r.by ? (g ?? "(blank)") : "all");
  const fenced = r.by ? r.fences : r.fences.slice(0, 1);
  const figure = {
    data: [
      {
        type: "scatter",
        mode: "markers",
        x: r.outliers.map((o) => o.value),
        y: r.outliers.map((o) => y(o.group)),
        marker: { size: 10, color: "theme:accent", line: { color: "theme:fg", width: 1 } },
        hovertemplate: "%{x}<extra></extra>",
        name: "Outliers",
      },
      {
        type: "scatter",
        mode: "markers",
        x: fenced.map((f) => f.median),
        y: fenced.map((f) => y(f.group)),
        marker: { symbol: "line-ns-open", size: 18, color: "theme:fg" },
        hovertemplate: "median %{x}<extra></extra>",
        name: "Median",
      },
    ],
    layout: {
      ...BASE_LAYOUT,
      margin: { l: r.by ? 110 : 40, r: 16, t: 12, b: 48 },
      xaxis: { title: { text: `${r.column}: the usual range shaded` }, ...amountRange(fenced, r.outliers) },
      yaxis: { type: "category", automargin: true, categoryarray: groups, visible: Boolean(r.by) },
      shapes: fenced
        .filter((f) => f.low_fence !== null && f.high_fence !== null)
        .map((f) => ({ type: "rect", xref: "x", yref: "y", x0: f.low_fence, x1: f.high_fence, y0: groups.indexOf(y(f.group)) - 0.3, y1: groups.indexOf(y(f.group)) + 0.3, fillcolor: "theme:ok", opacity: 0.18, line: { width: 0 } })),
      showlegend: false,
    },
  };
  return pair(`Outliers in ${r.column}`, lines.join("\n"), figure);
}

/** The axis from the lowest point worth seeing to the highest: a fence below zero for a column of
 * positive amounts (a skewed spread) is cut at zero rather than drawing empty space. */
function amountRange(fences: AmountCardInput["fences"], outliers: AmountCardInput["outliers"]): Record<string, unknown> {
  const points = [...fences.flatMap((f) => [f.median, f.low_fence, f.high_fence]), ...outliers.map((o) => o.value)].filter((v): v is number => typeof v === "number");
  if (!points.length) return {};
  const positive = fences.every((f) => f.median > 0) && outliers.every((o) => o.value >= 0);
  const low = positive ? Math.max(0, Math.min(...points)) : Math.min(...points);
  const high = Math.max(...points);
  const pad = (high - low) * 0.04 || 1;
  return { range: [positive && low === 0 ? 0 : low - pad, high + pad] };
}

export interface FrequencyCardInput {
  column: string;
  distinct: number;
  typical: number;
  rarer: number;
  commoner: number;
  outliers: { value: string; count: number; score: number }[];
}

export function frequencyCards(r: FrequencyCardInput): SandboxCard[] {
  const total = r.rarer + r.commoner;
  const lines = [
    `# Values of ${r.column} seen too often or too rarely`,
    total ? `**${fmt(total)} of ${fmt(r.distinct)} values stand out: ${fmt(r.rarer)} rarer and ${fmt(r.commoner)} commoner than the usual ${fmt(r.typical)} rows per value.**` : `**Nothing stands out among ${fmt(r.distinct)} values (usually ${fmt(r.typical)} rows each).**`,
  ];
  if (r.outliers.length) lines.push("", mdTable(["Value", "Rows", "", "Score"], r.outliers.slice(0, 15).map((o) => [o.value, o.count, o.score < 0 ? "rarer" : "commoner", fmt(o.score)]), [false, true, false, true]));
  lines.push("", "> Rarer values are often typos; commoner ones, copies or defaults.");
  if (!r.outliers.length) return pair(`Frequency outliers in ${r.column}`, lines.join("\n"), null);
  const shown = r.outliers.slice(0, 20);
  const counts = shown.map((o) => o.count);
  const spread = Math.max(...counts, r.typical) / Math.max(1, Math.min(...counts, r.typical));
  const figure = {
    data: [{ type: "bar", x: shown.map((o) => o.value), y: counts, marker: { color: shown.map((o) => (o.score < 0 ? "theme:ok" : "theme:accent")) }, hovertemplate: "%{x}: %{y} rows<extra></extra>" }],
    layout: {
      ...BASE_LAYOUT,
      xaxis: { type: "category", title: { text: r.column } },
      yaxis: { title: { text: "Rows" }, type: spread > 50 ? "log" : "linear" },
      shapes: [{ type: "line", xref: "paper", x0: 0, x1: 1, y0: r.typical, y1: r.typical, line: { color: "theme:fg", width: 1, dash: "dot" } }],
      annotations: [{ xref: "paper", x: 1, y: spread > 50 ? Math.log10(r.typical) : r.typical, text: `usual: ${fmt(r.typical)} rows`, showarrow: false, xanchor: "right", yanchor: "bottom", font: { size: 10 } }],
      showlegend: false,
    },
  };
  return pair(`Frequency outliers in ${r.column}`, lines.join("\n"), figure);
}

// ===== anything else pushed: a column's profile, the whole table's, a value, a row, the Key Finder =====

interface ProfileInput {
  column: string;
  type: string;
  family: string;
  rows: number;
  blanks: number;
  blank_percent: number;
  distinct: number;
  all_unique: boolean;
  top_values: { value: unknown; count: number }[];
  [key: string]: unknown;
}

const STAT_NAMES: [string, string][] = [
  ["min", "Lowest"],
  ["q1", "1st quartile"],
  ["median", "Median"],
  ["mean", "Mean"],
  ["q3", "3rd quartile"],
  ["max", "Highest"],
  ["stddev", "Standard deviation"],
  ["sum", "Sum"],
  ["zeros", "Zeros"],
  ["negatives", "Negatives"],
  ["shortest", "Shortest text"],
  ["longest", "Longest text"],
  ["empty_strings", "Empty text"],
  ["padded", "Padded with spaces"],
  ["earliest", "Earliest"],
  ["latest", "Latest"],
];

/** A column's profile: its numbers in a table, its most common values, and a histogram (numbers),
 * rows by month (dates) or the top values (anything else). */
export function profileCards(p: ProfileInput, dataset: string): SandboxCard[] {
  const stats = STAT_NAMES.filter(([key]) => p[key] !== undefined && p[key] !== null).map(([key, name]) => [name, fmt(p[key])]);
  const lines = [
    `# Profile of ${p.column}`,
    `**${fmt(p.rows)} rows, ${fmt(p.distinct)} different values${p.all_unique ? " (every row different)" : ""}, ${fmt(p.blanks)} blank (${p.blank_percent}%).** A ${p.type.toLowerCase()} column of \`${dataset}\`.`,
  ];
  if (stats.length) lines.push("", mdTable(["Measure", "Value"], stats, [false, true]));
  if (p.top_values.length) {
    lines.push("", "#### Most common values", mdTable(["Value", "Rows", "Share"], p.top_values.slice(0, 10).map((t) => [t.value ?? "(blank)", t.count, pct(t.count / Math.max(1, p.rows))]), [false, true, true]));
  }
  const histogram = p.histogram as { low: number; high: number; count: number }[] | undefined;
  const months = p.by_month as { month: string; count: number }[] | undefined;
  let figure: Record<string, unknown> | null = null;
  if (histogram?.length) {
    figure = {
      data: [{ type: "bar", x: histogram.map((b) => (b.low + b.high) / 2), y: histogram.map((b) => b.count), width: histogram.map((b) => (b.high - b.low) * 0.95), marker: { color: "theme:accent" }, hovertemplate: "%{x}: %{y} rows<extra></extra>" }],
      layout: { ...BASE_LAYOUT, xaxis: { title: { text: p.column } }, yaxis: { title: { text: "Rows" } }, bargap: 0 },
    };
  } else if (months?.length) {
    figure = {
      data: [{ type: "scatter", mode: "lines+markers", x: months.map((m) => m.month), y: months.map((m) => m.count), line: { color: "theme:accent", width: 2 }, hovertemplate: "%{x}: %{y} rows<extra></extra>" }],
      layout: { ...BASE_LAYOUT, xaxis: { title: { text: `${p.column} (by month)` } }, yaxis: { title: { text: "Rows" } } },
    };
  } else if (p.top_values.length > 1) {
    const top = [...p.top_values.slice(0, 15)].reverse();
    figure = {
      data: [{ type: "bar", orientation: "h", x: top.map((t) => t.count), y: top.map((t) => fmt(t.value ?? "(blank)")), marker: { color: "theme:accent" }, hovertemplate: "%{y}: %{x} rows<extra></extra>" }],
      layout: { ...BASE_LAYOUT, margin: { l: 110, r: 16, t: 12, b: 48 }, xaxis: { title: { text: "Rows" } }, yaxis: { automargin: true, type: "category" } },
    };
  }
  return pair(`Profile of ${p.column}`, lines.join("\n"), figure);
}

/** The whole view's profile: one row per column. */
export function tableProfileCards(r: { rows: number; duplicate_rows: number; columns: Record<string, unknown>[] }, dataset: string): SandboxCard[] {
  const markdown = [
    `# Profile of ${dataset}`,
    `**${fmt(r.rows)} rows, ${fmt(r.columns.length)} columns, ${fmt(r.duplicate_rows)} duplicate rows.**`,
    "",
    mdTable(["Column", "Type", "Blank", "Blank %", "Different values", "Lowest", "Highest"], r.columns.map((c) => [c.column, String(c.type).toLowerCase(), c.blanks, `${String(c.blank_percent)}%`, c.distinct, c.min, c.max]), [false, false, true, true, true, false, false]),
  ].join("\n");
  return pair(`Profile of ${dataset}`, markdown, null).map((card) => ({ ...card, size: { w: 12, h: card.size.h } }));
}

/** One value (a cell, or a layer of one in the value viewer), titled with where it came from; a long
 * or structured value goes in a code block. */
export function valueCards(value: unknown, title: string, dataset: string): SandboxCard[] {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2) ?? "";
  const long = text.length > 120 || /[\n{[]/.test(text);
  const shown = text === "" ? "(empty text)" : value === null || value === undefined ? "(blank)" : text;
  const markdown = [`# ${title}`, `*A value from \`${dataset}\`.*`, "", long ? `\`\`\`\n${text}\n\`\`\`` : `**${shown}**`].join("\n");
  return [{ kind: "note", title, body: markdown, size: { w: long ? 6 : 4, h: long ? 8 : 4 } }];
}

/** One row: each column and its value, as a two-column table. */
export function rowCards(values: Record<string, unknown>, row: number, dataset: string): SandboxCard[] {
  const markdown = [`# Row ${fmt(row + 1)} of ${dataset}`, "", mdTable(["Column", "Value"], Object.entries(values).map(([k, v]) => [k, v === null || v === undefined ? "(blank)" : typeof v === "object" ? JSON.stringify(v) : v]))].join("\n");
  return [{ kind: "note", title: `Row ${fmt(row + 1)} of ${dataset}`, body: markdown, size: { w: 5, h: Math.max(5, Math.min(18, Math.ceil(Object.keys(values).length * 0.6) + 3)) } }];
}

// ===== regression =====

export interface RegressionTerm {
  term: string;
  column: string;
  kind: string;
  coefficient: number;
  se: number;
  p: number;
  low: number;
  high: number;
  odds_ratio?: number;
  words: string;
}

export interface RegressionResult {
  kind: "linear" | "logistic";
  target: string;
  outcome: string | null;
  predictors: string[];
  n: number;
  left_out: number;
  references: Record<string, string>;
  verdict: string;
  warnings?: string[];
  coefficients: RegressionTerm[];
  fit: { r2?: number; adj_r2?: number; rmse?: number; accuracy?: number; majority?: number; pseudo_r2?: number };
  model: { kind: string; terms: { column: string; kind: string; mean?: number; level?: string }[] } & Record<string, unknown>;
  steps: { predicted: { kind: string; params: Record<string, unknown> }; miss: { kind: string; params: Record<string, unknown> } | null };
}

export function regressionCards(r: RegressionResult): SandboxCard[] {
  const linear = r.kind === "linear";
  const terms = r.coefficients.slice(1);
  const fit = linear
    ? mdTable(["R-squared", "Adjusted", "Typical miss", "Rows used"], [[fmt(r.fit.r2), fmt(r.fit.adj_r2), fmt(r.fit.rmse), fmt(r.n)]], [true, true, true, true])
    : mdTable(["Right", "Guessing", "McFadden R-squared", "Rows used"], [[pct(r.fit.accuracy ?? 0), pct(r.fit.majority ?? 0), fmt(r.fit.pseudo_r2), fmt(r.n)]], [true, true, true, true]);
  const lines = [
    `# ${linear ? "What moves" : "What raises the odds of"} ${linear ? r.target : r.outcome}`,
    `**${r.verdict}**`,
    "",
    fit,
    "",
    "#### Each effect, the others held steady",
    mdTable(
      ["Term", "In words", linear ? "Effect" : "Odds ratio", "95% range", "p"],
      terms.slice(0, 15).map((c) => [`${c.p < 0.05 ? "**" : ""}${c.term}${c.p < 0.05 ? "**" : ""}`, c.words, fmt(linear ? c.coefficient : c.odds_ratio), `${fmt(c.low)} to ${fmt(c.high)}`, pText(c.p)]),
      [false, false, true, true, true],
    ),
  ];
  if (Object.keys(r.references).length) lines.push("", `Categories are compared with ${Object.entries(r.references).map(([k, v]) => `${k} = ${v}`).join(", ")}.`);
  if (r.left_out) lines.push("", `${fmt(r.left_out)} row${r.left_out === 1 ? "" : "s"} with a blank in a used column ${r.left_out === 1 ? "was" : "were"} left out.`);
  lines.push("", "> It shows what goes together, not what causes what.", ...(r.warnings ?? []).map((w) => `> ${w}`));
  const shown = [...terms.slice(0, 20)].reverse();
  const figure = {
    data: [
      {
        type: "scatter",
        mode: "markers",
        x: shown.map((c) => (linear ? c.coefficient : c.odds_ratio)),
        y: shown.map((c) => c.term),
        error_x: { type: "data", symmetric: false, array: shown.map((c) => c.high - (linear ? c.coefficient : (c.odds_ratio ?? 1))), arrayminus: shown.map((c) => (linear ? c.coefficient : (c.odds_ratio ?? 1)) - c.low), color: "theme:muted", thickness: 1.5, width: 4 },
        marker: { size: 10, color: shown.map((c) => (c.p >= 0.05 ? "theme:muted" : (linear ? c.coefficient > 0 : (c.odds_ratio ?? 1) > 1) ? "theme:accent" : "theme:ok")) },
        hovertemplate: "%{y}: %{x:.3g}<extra></extra>",
      },
    ],
    layout: {
      ...BASE_LAYOUT,
      margin: { l: 140, r: 16, t: 12, b: 48 },
      xaxis: linear
        ? { title: { text: `Effect on ${r.target} (per 1 unit, or against the reference)` }, zeroline: true, zerolinewidth: 2 }
        : { type: "log", title: { text: `Odds ratio for ${r.outcome} (log scale)` }, ...logTicks(shown.flatMap((c) => [c.low, c.high])) },
      yaxis: { automargin: true, type: "category" },
      shapes: linear ? [] : [{ type: "line", x0: 1, x1: 1, yref: "paper", y0: 0, y1: 1, line: { color: "theme:fg", width: 1, dash: "dot" } }],
      showlegend: false,
    },
  };
  return pair(`Regression: ${linear ? r.target : r.outcome}`, lines.join("\n"), figure);
}

// ===== time series and forecast =====

export interface TimeSeriesResult {
  date: string;
  metric: string | null;
  total: string;
  period: string;
  what: string;
  points: { period: string; value: number; trend: number; season: number }[];
  gaps: string[];
  seasonal: boolean;
  multiplicative: boolean;
  season_index: { position: string; index: number }[];
  season_words: string[];
  anomalies: { period: string; value: number; score: number; kind: string }[];
  shifts: { period: string; before: number; after: number; t: number }[];
  memory: number | null;
  growth: { period: string; value: number; change: number | null; season_ago: number | null; rolling: number | null }[];
  words: string[];
  step: { kind: string; params: Record<string, unknown> } | null;
  lines: { name: string; points: { period: string; value: number }[] }[];
}

export function timeSeriesFigure(r: TimeSeriesResult): Record<string, unknown> {
  const x = r.points.map((p) => p.period);
  const data: Record<string, unknown>[] = r.lines.length
    ? r.lines.map((line) => ({ type: "scatter", mode: "lines", name: line.name, x: line.points.map((p) => p.period), y: line.points.map((p) => p.value) }))
    : [
        { type: "scatter", mode: "lines", name: r.what, x, y: r.points.map((p) => p.value), line: { color: "theme:accent", width: 2 } },
        { type: "scatter", mode: "lines", name: "Trend", x, y: r.points.map((p) => p.trend), line: { color: "theme:fg", width: 1.5, dash: "dot" } },
      ];
  if (r.anomalies.length) {
    data.push({ type: "scatter", mode: "markers", name: "Stands out", x: r.anomalies.map((a) => a.period), y: r.anomalies.map((a) => a.value), marker: { size: 11, color: "theme:warn", symbol: "circle-open", line: { width: 3 } } });
  }
  return {
    data,
    layout: {
      ...BASE_LAYOUT,
      xaxis: { type: "date", title: { text: r.date } },
      yaxis: { title: { text: `${r.what} per ${r.period}` } },
      legend: { orientation: "h", y: 1.1 },
      shapes: r.shifts.map((s) => ({ type: "line", x0: s.period, x1: s.period, yref: "paper", y0: 0, y1: 1, line: { color: "theme:muted", width: 1, dash: "dash" } })),
      annotations: r.shifts.map((s) => ({ x: s.period, yref: "paper", y: 1, text: "step", showarrow: false, yanchor: "bottom", font: { size: 10, color: "theme:muted" } })),
    },
  };
}

export function timeSeriesCards(r: TimeSeriesResult): SandboxCard[] {
  const lines = [`# ${r.what} by ${r.period}`, ...r.words.map((w, i) => (i === 0 ? `**${w}.**` : `- ${w}`))];
  if (r.seasonal && r.season_index.length) {
    lines.push("", "#### The season", mdTable(["", r.multiplicative ? "Against a typical " + r.period : "Difference"], r.season_index.map((s) => [s.position, r.multiplicative ? `${s.index >= 1 ? "+" : ""}${((s.index - 1) * 100).toFixed(0)}%` : fmt(s.index)]), [false, true]));
  }
  const recent = r.growth.slice(-6);
  if (recent.length) {
    lines.push("", "#### Lately", mdTable([r.period, r.what, "Change", "Against a season ago"], recent.map((g) => [g.period, g.value, g.change === null ? "" : `${(g.change * 100).toFixed(1)}%`, g.season_ago === null ? "" : `${(g.season_ago * 100).toFixed(1)}%`]), [false, true, true, true]));
  }
  if (r.gaps.length) lines.push("", `> ${r.gaps.length} ${r.period}s had no rows.`);
  return pair(`Time series: ${r.what}`, lines.join("\n"), timeSeriesFigure(r));
}

export interface ForecastResult {
  date: string;
  what: string;
  period: string;
  horizon: number;
  words: string[];
  seasonal: boolean;
  trend_per_period: number;
  history: { period: string; value: number; fitted: number }[];
  forecast: { period: string; estimate: number; low80: number; high80: number; low95: number; high95: number }[];
  check: { periods: number; typical_miss: number; typical_miss_share: number; from: string } | null;
  predictors: { column: string; future: string; value?: number }[];
}

export function forecastFigure(r: ForecastResult): Record<string, unknown> {
  const fx = r.forecast.map((f) => f.period);
  const band = (low: number[], high: number[], color: string, name: string): Record<string, unknown> => ({
    type: "scatter",
    mode: "lines",
    name,
    x: [...fx, ...[...fx].reverse()],
    y: [...high, ...[...low].reverse()],
    fill: "toself",
    fillcolor: color,
    line: { width: 0 },
    hoverinfo: "skip",
  });
  return {
    data: [
      band(r.forecast.map((f) => f.low95), r.forecast.map((f) => f.high95), "rgba(200, 16, 46, 0.12)", "95% range"),
      band(r.forecast.map((f) => f.low80), r.forecast.map((f) => f.high80), "rgba(200, 16, 46, 0.22)", "80% range"),
      { type: "scatter", mode: "lines", name: r.what, x: r.history.map((h) => h.period), y: r.history.map((h) => h.value), line: { color: "theme:fg", width: 2 } },
      { type: "scatter", mode: "lines", name: "Fitted", x: r.history.map((h) => h.period), y: r.history.map((h) => h.fitted), line: { color: "theme:muted", width: 1, dash: "dot" } },
      { type: "scatter", mode: "lines+markers", name: "Forecast", x: fx, y: r.forecast.map((f) => f.estimate), line: { color: "theme:accent", width: 2.5 }, marker: { size: 5 } },
    ],
    layout: {
      ...BASE_LAYOUT,
      xaxis: { type: "date", title: { text: r.date } },
      yaxis: { title: { text: `${r.what} per ${r.period}` } },
      legend: { orientation: "h", y: 1.1 },
      shapes: r.check ? [{ type: "line", x0: r.check.from, x1: r.check.from, yref: "paper", y0: 0, y1: 1, line: { color: "theme:muted", width: 1, dash: "dash" } }] : [],
    },
  };
}

export function forecastCards(r: ForecastResult): SandboxCard[] {
  const lines = [
    `# Forecast: ${r.what} by ${r.period}`,
    ...r.words.map((w, i) => (i === 0 ? `**${w}**` : `- ${w}`)),
    "",
    mdTable([r.period, "Forecast", "80% range", "95% range"], r.forecast.slice(0, 12).map((f) => [f.period, f.estimate, `${fmt(f.low80)} to ${fmt(f.high80)}`, `${fmt(f.low95)} to ${fmt(f.high95)}`]), [false, true, true, true]),
  ];
  if (r.predictors.length) lines.push("", `Predictors: ${r.predictors.map((p) => `${p.column} (${p.future === "value" ? `set to ${fmt(p.value)}` : p.future === "trend" ? "its own trend" : "its last value"})`).join(", ")}.`);
  return pair(`Forecast: ${r.what}`, lines.join("\n"), forecastFigure(r));
}
