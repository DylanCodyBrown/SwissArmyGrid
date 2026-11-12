/**
 * compareDialog.ts: pick the keys to compare two grids, with the Key Finder already run. [WIRE + EXPORT]
 *
 * Opens by itself when a split is made (and from the ribbon's Compare row). It runs the Key Finder
 * between the two grids (left as the source, right as the target) and lists the likely key
 * pairs, best first, each with a checkbox. Tick one pair for a single key, several for a key made
 * of several columns, or add a pair by hand. Compare assigns them and lines the grids up; Close
 * leaves the split as it is, with no keys, to explore.
 *
 * A known relationship (the relationship model) goes first, with a crown, and starts ticked.
 *
 * ROUTING: [PAIR CHECKBOX]   -> read-only form state (a known pair, else the best pair covering half of either table, starts ticked)
 *          [ADD A PAIR]      left column + right column -> addPair()
 *          [COMPARE]         -> startCompare(checked pairs)
 *          [CLOSE]           -> close(): the split stays, no keys
 */
import { api } from "./api";
import { button, clear, formatCount, h, icon, openDialog, toast } from "./dom";
import { currentCompare, startCompare } from "./compare";
import type { Context } from "./panes";
import { isKnownPair, knownPairs, loadRelationships } from "./relationships";

interface Pair {
  source_column: string;
  target_column: string;
  shared: number;
  source_found: number;
  target_found: number;
  relationship: string;
  score: number;
}

const pct = (share: number): string => `${(Math.round(share * 1000) / 10).toLocaleString()}%`;

export function openCompareDialog(left: Context, right: Context): void {
  const { body, foot, close } = openDialog(`Compare ${left.dataset} with ${right.dataset}`, true);
  body.classList.add("compare-dialog");
  const status = h("p", { class: "hint", role: "status" }, "Finding the columns the two tables share…");
  const tbody = h("tbody", {});
  const rows: { pair: [string, string]; box: HTMLInputElement }[] = [];
  const compareButton = button("Compare", () => void go(), { class: "primary", title: "Assign the ticked pairs as keys and line the grids up" });
  const sync = (): void => { compareButton.disabled = !rows.some((r) => r.box.checked); };

  const addRow = (pair: [string, string], cells: (string | HTMLElement)[], checked: boolean): void => {
    if (rows.some((r) => r.pair[0] === pair[0] && r.pair[1] === pair[1])) {
      const found = rows.find((r) => r.pair[0] === pair[0] && r.pair[1] === pair[1])!;
      found.box.checked = true;
      sync();
      return;
    }
    const box = h("input", { type: "checkbox", checked, "aria-label": `Use ${pair[0]} = ${pair[1]} as a key`, onchange: sync });
    rows.push({ pair, box });
    const crown = isKnownPair(left.dataset, right.dataset, pair[0], pair[1]) ? h("span", { class: "known-pair", title: "A known relationship" }, icon("crown")) : null;
    tbody.append(h("tr", {}, h("td", {}, box, crown), h("td", {}, h("strong", {}, pair[0])), h("td", {}, h("strong", {}, pair[1])), ...cells.map((c) => h("td", { class: typeof c === "string" ? "num" : "" }, c))));
    sync();
  };

  // A pair by hand, for keys the finder skipped (long text, or no shared values yet).
  const leftPick = h("select", { "aria-label": `${left.dataset} column` });
  const rightPick = h("select", { "aria-label": `${right.dataset} column` });
  for (const c of left.state.info?.columns ?? []) leftPick.append(h("option", { value: c.name }, c.name));
  for (const c of right.state.info?.columns ?? []) rightPick.append(h("option", { value: c.name }, c.name));
  const addPair = (): void => addRow([leftPick.value, rightPick.value], ["", "", "", "added by hand"], true);

  const existing = currentCompare();
  body.append(
    h("p", {}, `Tick the column pair (or pairs, for a key of several columns) that identifies the same row in both tables. The grids then line up row for row on it. Close to explore the two grids without keys.`),
    status,
    h(
      "table",
      { class: "kf-table" },
      h(
        "thead",
        {},
        h("tr", {}, h("th", {}, "Key"), h("th", {}, `${left.dataset} column`), h("th", {}, `${right.dataset} column`), h("th", { class: "num" }, `% of ${left.dataset} found`), h("th", { class: "num" }, `% of ${right.dataset} found`), h("th", { class: "num" }, "Shared values"), h("th", {}, "Relationship")),
      ),
      tbody,
    ),
    h("div", { class: "kf-form" }, h("span", { class: "hint" }, "Or pick a pair yourself:"), leftPick, h("span", {}, "="), rightPick, button("Add", addPair, { icon: "plus" })),
  );
  foot.append(button("Close", close, { title: "Keep the split without keys" }), compareButton);
  sync();

  void (async () => {
    try {
      const [result] = await Promise.all([
        api<{ pairs: Pair[]; pair_count: number }>(`/grids/${encodeURIComponent(left.id)}/key-finder`, "POST", { target: right.id }),
        loadRelationships(),
      ]);
      // A known relationship goes first and decides the keys, before any guess.
      const known = existing ? [] : knownPairs(left.dataset, right.dataset);
      for (const pair of known) addRow(pair, ["", "", "", "known relationship"], true);
      const top = result.pairs.slice(0, 20);
      clear(status, top.length ? `${formatCount(result.pair_count)} column pair${result.pair_count === 1 ? "" : "s"} share values; the best ${top.length} are below.` : "No columns share any value. Add a pair by hand, or close to explore without keys.");
      const keep = new Set((existing?.keys ?? []).map((k) => k.join("\u0000")));
      top.forEach((p, i) => {
        const pair: [string, string] = [p.source_column, p.target_column];
        // The best pair starts ticked when it covers at least half of either table's values.
        const ticked = keep.size ? keep.has(pair.join("\u0000")) : !known.length && i === 0 && Math.max(p.source_found, p.target_found) >= 0.5;
        addRow(pair, [pct(p.source_found), pct(p.target_found), formatCount(p.shared), p.relationship], ticked);
      });
      for (const k of existing?.keys ?? []) if (!top.some((p) => p.source_column === k[0] && p.target_column === k[1])) addRow(k, ["", "", "", "current key"], true);
    } catch (e) {
      status.textContent = (e as Error).message;
      status.className = "status-error";
    }
  })();

  async function go(): Promise<void> {
    const keys = rows.filter((r) => r.box.checked).map((r) => r.pair);
    const lefts = new Set(keys.map((k) => k[0]));
    const rights = new Set(keys.map((k) => k[1]));
    if (lefts.size < keys.length || rights.size < keys.length) {
      toast("Use each column in one key pair only.", "error");
      return;
    }
    close();
    await startCompare(keys);
  }
}
