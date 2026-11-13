/**
 * cheatSheets.ts: each analysis's "Data shape" panel (Plans/sandbox-report.plan.md, S5): a tiny mock
 * table in the shape that works, one in a shape that does not and why, a few tips, and a check of the
 * grid in front (POST /grids/{id}/shape-check: its columns' types and counts). The rows are made up.
 * The cheat sheet can go to the Sandbox like any result.
 */
import { mdTable, type SandboxCard } from "./analysisCards";
import { sendCards } from "./analyzeMenu";
import { api, grid } from "./api";
import { button, clear, h } from "./dom";

export type Sheet = "odds" | "fit_test" | "predictors" | "outliers";

interface Mock {
  caption: string;
  columns: string[];
  rows: string[][];
}

interface CheatSheet {
  title: string;
  good: Mock;
  bad: Mock & { why: string };
  tips: string[];
}

export const SHEETS: Record<Sheet, CheatSheet> = {
  odds: {
    title: "Odds",
    good: {
      caption: "Long: one row per order, the category and the outcome as columns",
      columns: ["order_id", "region", "late"],
      rows: [["1001", "West", "late"], ["1002", "North", "on time"], ["1003", "West", "on time"], ["1004", "East", "on time"]],
    },
    bad: {
      caption: "Already counted: one row per region",
      columns: ["region", "late", "on_time"],
      rows: [["West", "365", "429"], ["North", "484", "2740"]],
      why: "Odds count rows, so each row must be one chance (one order). Counts are already the answer: push the orders, or test the counts with the Fit test's own shares.",
    },
    tips: [
      "A number column is cut into equal-sized ranges; Buckets sets how many.",
      "An outcome is a value (late, refund) or a line on a number (amount above 1000).",
      "Under 30 rows for a value, its odds are rough: they are marked.",
    ],
  },
  fit_test: {
    title: "Fit test",
    good: {
      caption: "Long: one row per invoice; a category to count, or real numbers for Benford",
      columns: ["invoice", "region", "amount"],
      rows: [["A-17", "West", "1250.00"], ["A-18", "North", "86.40"], ["A-19", "West", "4310.75"]],
    },
    bad: {
      caption: "Numbers stored as text, and an identifier as the category",
      columns: ["invoice", "amount_text"],
      rows: [["A-17", "$1,250.00"], ["A-18", "$86.40"]],
      why: "Benford needs a number column (clean the currency text into numbers first), and an even spread needs categories that repeat: a different value on every row tells nothing.",
    },
    tips: [
      "Even: categories that should be equally common (weekdays, shifts, regions).",
      "Benford: amounts that grew naturally and span several orders of magnitude (10s to 10,000s); not prices with a set range, not invented codes.",
      "Over 200 different values: bucket the column first.",
    ],
  },
  predictors: {
    title: "Find predictors",
    good: {
      caption: "Long: one row per thing predicted, the target and every candidate as columns",
      columns: ["order_id", "region", "size", "units", "price", "late"],
      rows: [["1001", "West", "large", "12", "40.50", "late"], ["1002", "North", "small", "3", "18.00", "on time"]],
    },
    bad: {
      caption: "Wide: one column per month",
      columns: ["region", "jan_late", "feb_late", "mar_late"],
      rows: [["West", "41", "38", "52"], ["North", "12", "15", "11"]],
      why: "A predictor is a column of the same rows as the target. One column per month spreads one measure across columns: unpivot it (one row per region and month) in the notebook first.",
    },
    tips: [
      "Identifiers (a different value on every row) and dates are skipped; dates belong to Time series.",
      "Numbers are cut into equal-sized ranges (Buckets), so a curve counts as well as a straight line.",
      "Of two candidates that say the same thing, give a regression one.",
    ],
  },
  outliers: {
    title: "Outliers",
    good: {
      caption: "Long: one row per value, with the group to judge it within",
      columns: ["invoice", "vendor", "unit_cost"],
      rows: [["A-17", "Northwind", "12.40"], ["A-18", "Northwind", "12.10"], ["A-19", "Contoso", "88.00"]],
    },
    bad: {
      caption: "Totals: one row per vendor",
      columns: ["vendor", "total_cost"],
      rows: [["Northwind", "48,210.55"], ["Contoso", "9,880.00"]],
      why: "A total hides the one odd invoice inside it. Judge the rows that make it up, within each vendor.",
    },
    tips: [
      "Amount needs a number column; Frequency works on anything (typos, copied values).",
      "Within each: judge every vendor, store or month by its own usual range.",
      "Robust (the median) is the safe default: a few wild values cannot hide themselves.",
    ],
  },
};

function mockTable(mock: Mock): HTMLElement {
  return h(
    "figure",
    { class: "cheat-mock" },
    h("table", { class: "small" }, h("thead", {}, h("tr", {}, ...mock.columns.map((c) => h("th", {}, c)))), h("tbody", {}, ...mock.rows.map((r) => h("tr", {}, ...r.map((v) => h("td", {}, v)))))),
    h("figcaption", {}, mock.caption),
  );
}

interface ShapeNote {
  level: "ok" | "warn";
  text: string;
}

/** The "Data shape" panel for `sheet`; `columns()` gives the columns the form is set to, for the check. */
export function cheatSheet(sheet: Sheet, columns: () => string[]): { element: HTMLElement; refresh: () => void } {
  const content = SHEETS[sheet];
  const checks = h("ul", { class: "cheat-checks" });
  const refresh = (): void => {
    clear(checks, h("li", { class: "hint" }, "Checking your grid…"));
    void api<{ notes: ShapeNote[] }>(`${grid}/shape-check`, "POST", { analysis: sheet, columns: columns() })
      .then(({ notes }) => clear(checks, ...notes.map((n) => h("li", { class: `cheat-${n.level}` }, n.text))))
      .catch((e: Error) => clear(checks, h("li", { class: "cheat-warn" }, e.message)));
  };
  const element = h(
    "details",
    { class: "cheat-sheet", ontoggle: (event: Event) => { if ((event.target as HTMLDetailsElement).open) refresh(); } },
    h("summary", {}, "Data shape: what this analysis needs"),
    h(
      "div",
      { class: "cheat-body" },
      h("div", { class: "cheat-pair" }, h("div", { class: "cheat-good" }, h("strong", {}, "Works"), mockTable(content.good)), h("div", { class: "cheat-bad" }, h("strong", {}, "Does not work"), mockTable(content.bad), h("p", {}, content.bad.why))),
      h("ul", { class: "cheat-tips" }, ...content.tips.map((t) => h("li", {}, t))),
      h("div", { class: "cheat-yours" }, h("strong", {}, "Your grid"), checks),
      button("Send to Sandbox", () => void sendCards(cheatCards(sheet)), { icon: "image", class: "small", title: "This cheat sheet as a note in the Sandbox" }),
    ),
  );
  return { element, refresh };
}

/** The cheat sheet as a Sandbox note. */
export function cheatCards(sheet: Sheet): SandboxCard[] {
  const s = SHEETS[sheet];
  const markdown = [
    `# Data shape: ${s.title}`,
    `#### Works: ${s.good.caption}`,
    mdTable(s.good.columns, s.good.rows),
    "",
    `#### Does not work: ${s.bad.caption}`,
    mdTable(s.bad.columns, s.bad.rows),
    "",
    `> ${s.bad.why}`,
    "",
    ...s.tips.map((t) => `- ${t}`),
  ].join("\n");
  return [{ kind: "note", title: `Data shape: ${s.title}`, body: markdown, size: { w: 6, h: 12 } }];
}
