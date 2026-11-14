/**
 * tieOutDialog.ts: the tie-out report on a compare (Plans/grid-tabs-split-compare.plan.md; server:
 * routes_tieout.py). Do the two tables add up the same, group by group?
 *
 *   [CHOOSE]   group by (shared columns; a text column with few values is ticked), totals (shared
 *              number columns, all ticked), a tolerance (absolute, or percent of the left total)
 *   [REPORT]   in words, then one row per group: rows and each total, left, right, the difference,
 *              ties or off; off groups first, largest first; the grand total last
 *   [CLICK]    a group filters both grids to it (a step on each, matched as it was grouped)
 *   [KEEP]     Send to Sandbox (a note and the table), Show the math, Download CSV
 */
import { api } from "./api";
import { type CompareInfo } from "./compare";
import { button, clear, h, markStaleOnChange, openDialog, toast } from "./dom";
import { type Context } from "./panes";
import { saveBlob } from "./profileView";

interface Choice {
  name: string;
  type: string;
  group: boolean;
  measure: boolean;
}

interface Measure {
  column: string;
  left: number | null;
  right: number | null;
  difference: number | null;
  difference_share: number | null;
  ties: boolean;
}

interface Group {
  group: (string | null)[];
  keys: (string | null)[];
  left_rows: number | null;
  right_rows: number | null;
  row_difference: number;
  measures: Measure[];
  status: "ties" | "off";
  only: "left" | "right" | null;
}

interface Report {
  group_by: string[];
  measures: string[];
  groups: Group[];
  total: Omit<Group, "group" | "keys" | "only">;
  off: number;
  capped: boolean;
  words: string;
}

const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const fmt = (v: number | null): string => (v === null ? "" : number.format(v));
const signed = (v: number | null): string => (v === null ? "" : `${v > 0 ? "+" : ""}${number.format(v)}`);
const share = (v: number | null): string => (v === null ? "" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(1)}%`);

/** The report as a plain table: a header row and one row per group, the total last. */
function asRows(r: Report, left: string, right: string): { columns: string[]; rows: (string | number | null)[][] } {
  const columns = [
    ...(r.group_by.length ? r.group_by : ["Group"]),
    `Rows ${left}`,
    `Rows ${right}`,
    "Rows difference",
    ...r.measures.flatMap((m) => [`${m} ${left}`, `${m} ${right}`, `${m} difference`]),
    "Status",
  ];
  const line = (g: Group | Report["total"], label: (string | null)[]): (string | number | null)[] => [
    ...label,
    g.left_rows,
    g.right_rows,
    g.row_difference,
    ...g.measures.flatMap((m) => [m.left, m.right, m.difference]),
    g.status,
  ];
  const total = r.group_by.length ? ["Total", ...r.group_by.slice(1).map(() => "")] : ["Total"];
  return { columns, rows: [...r.groups.map((g) => line(g, r.group_by.length ? g.group : ["All rows"])), line(r.total, total)] };
}

function csv(columns: string[], rows: (string | number | null)[][]): string {
  const cell = (v: string | number | null): string => {
    const text = v === null ? "" : String(v);
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  return [columns, ...rows].map((r) => r.map(cell).join(",")).join("\n") + "\n";
}

export async function openTieOut(cmp: CompareInfo, pair: [Context, Context]): Promise<void> {
  const [left, right] = [pair[0].dataset, pair[1].dataset];
  const { body, foot, close } = openDialog(`Tie-out: ${left} and ${right}`, true);
  let choices: { columns: Choice[]; suggested: { group: string[]; measures: string[] } };
  try {
    choices = await api(`/compares/${cmp.compare}/tie-out`);
  } catch (e) {
    body.append(h("p", { class: "status-error" }, (e as Error).message));
    foot.append(button("Close", close));
    return;
  }
  const ticks = (kind: "group" | "measure", picked: string[]): HTMLInputElement[] =>
    choices.columns.filter((c) => c[kind]).map((c) => h("input", { type: "checkbox", value: c.name, checked: picked.includes(c.name), "aria-label": `${kind === "group" ? "Group by" : "Total"} ${c.name}` }));
  const groupTicks = ticks("group", choices.suggested.group);
  const measureTicks = ticks("measure", choices.suggested.measures);
  const tolerance = h("input", { type: "number", min: "0", step: "any", value: "0", size: 6, "aria-label": "Tolerance" });
  const kind = h("select", { "aria-label": "Tolerance kind" }, h("option", { value: "absolute" }, "absolute"), h("option", { value: "percent" }, "% of the left total"));
  const results = h("div", { class: "analysis-results" });
  const actions = h("span", { class: "analysis-actions" });
  const list = (label: string, boxes: HTMLInputElement[]): HTMLElement =>
    h("fieldset", { class: "tie-out-pick" }, h("legend", {}, label), ...(boxes.length ? boxes.map((b) => h("label", {}, b, b.value)) : [h("span", { class: "hint" }, "(none shared)")]));
  const sent = (): Record<string, unknown> => ({
    group: groupTicks.filter((b) => b.checked).map((b) => b.value),
    measures: measureTicks.filter((b) => b.checked).map((b) => b.value),
    tolerance: Number(tolerance.value) || 0,
    tolerance_kind: kind.value,
  });

  const show = async (r: Report, g: Group): Promise<void> => {
    try {
      await api(`/compares/${cmp.compare}/tie-out/show`, "POST", { group: r.group_by, keys: g.keys });
      toast(`Both grids show ${g.group.map((v) => v ?? "(blank)").join(" · ")}.`);
      close();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  const draw = (r: Report): void => {
    const head = h(
      "tr",
      {},
      ...(r.group_by.length ? r.group_by : ["Group"]).map((c) => h("th", { scope: "col" }, c)),
      h("th", { class: "num", scope: "col" }, `Rows ${left}`),
      h("th", { class: "num", scope: "col" }, `Rows ${right}`),
      h("th", { class: "num", scope: "col" }, "Difference"),
      ...r.measures.flatMap((m) => [
        h("th", { class: "num", scope: "col" }, `${m} ${left}`),
        h("th", { class: "num", scope: "col" }, `${m} ${right}`),
        h("th", { class: "num", scope: "col" }, "Difference"),
        h("th", { class: "num", scope: "col" }, "%"),
      ]),
      h("th", { scope: "col" }, "Status"),
    );
    const cells = (g: Group | Report["total"]): HTMLElement[] => [
      h("td", { class: "num" }, fmt(g.left_rows)),
      h("td", { class: "num" }, fmt(g.right_rows)),
      h("td", { class: `num ${g.row_difference ? "far" : ""}` }, signed(g.row_difference)),
      ...g.measures.flatMap((m) => [
        h("td", { class: "num" }, fmt(m.left)),
        h("td", { class: "num" }, fmt(m.right)),
        h("td", { class: `num ${m.ties ? "" : "far"}` }, signed(m.difference)),
        h("td", { class: "num" }, share(m.difference_share)),
      ]),
      h("td", {}, h("span", { class: `badge tie-${g.status}` }, g.status === "ties" ? "ties" : "off")),
    ];
    const rows = r.groups.map((g) => {
      const label = r.group_by.length ? g.group.map((v) => h("td", {}, v ?? "(blank)")) : [h("td", {}, "All rows")];
      const where = g.only ? ` (only in ${g.only === "left" ? left : right})` : "";
      const row = h("tr", { class: `tie-row ${g.status}`, title: r.group_by.length ? `Filter both grids${where}` : where.trim() }, ...label, ...cells(g));
      if (r.group_by.length) {
        row.tabIndex = 0;
        row.addEventListener("click", () => void show(r, g));
        row.addEventListener("keydown", (e) => { if (e.key === "Enter") void show(r, g); });
      }
      return row;
    });
    const totalLabel = (r.group_by.length ? r.group_by : ["Group"]).map((_, i) => h("th", { scope: "row" }, i === 0 ? "Total" : ""));
    clear(
      results,
      h("p", { class: `analysis-verdict ${r.off ? "far" : "off"}` }, r.words),
      h("div", { class: "tie-out-table" }, h("table", { class: "small" }, h("thead", {}, head), h("tbody", {}, ...rows), h("tfoot", {}, h("tr", { class: `tie-row total ${r.total.status}` }, ...totalLabel, ...cells(r.total))))),
    );
    const table = asRows(r, left, right);
    clear(
      actions,
      button("Send to Sandbox", () => {
        const note = `## Tie-out: ${left} and ${right}\n\n${r.words}\n\nGrouped by ${r.group_by.join(", ") || "nothing (one total)"}; totals of ${r.measures.join(", ") || "rows only"}.`;
        void Promise.all([
          api("/cards", "POST", { kind: "note", title: `Tie-out: ${left} and ${right}`, body: note, size: { w: 4, h: 6 } }),
          api("/cards", "POST", { kind: "table", title: `Tie-out by ${r.group_by.join(", ") || "total"}`, columns: table.columns, rows: table.rows, size: { w: 8, h: 10 } }),
        ]).then(() => toast("In the Sandbox: a note and the table."), (e: Error) => toast(e.message, "error"));
      }, { icon: "agent" }),
      button("Show the math", () => {
        void api<{ title: string; markdown: string }>("/analysis-proof", "POST", { analysis: "tie_out", result: r, dataset: `${left} and ${right}` })
          .then((proof) => api("/cards", "POST", { kind: "note", title: proof.title, body: proof.markdown, size: { w: 8, h: 14 } }))
          .then(() => toast("In the Sandbox: the math, step by step."), (e: Error) => toast(e.message, "error"));
      }, { icon: "steps", title: "Step by step, to the Sandbox" }),
      button("Download CSV", () => saveBlob(new Blob([csv(table.columns, table.rows)], { type: "text/csv" }), `tie-out-${left}-${right}.csv`), { icon: "export" }),
    );
  };

  const run = async (): Promise<void> => {
    clear(results, h("p", { class: "hint" }, "Totalling…"));
    clear(actions);
    try {
      draw(await api<Report>(`/compares/${cmp.compare}/tie-out`, "POST", sent()));
    } catch (e) {
      clear(results, h("p", { class: "status-error" }, (e as Error).message));
    }
  };

  body.append(
    h("div", { class: "tie-out-choices" }, list("Group by", groupTicks), list("Totals", measureTicks)),
    h("div", { class: "analysis-controls" }, h("label", { class: "analysis-field" }, h("span", {}, "Ties within"), tolerance), kind, button("Tie out", () => void run(), { icon: "play", class: "primary" })),
    results,
  );
  markStaleOnChange(body, results);
  foot.append(actions, button("Close", close));
  await run();
}
