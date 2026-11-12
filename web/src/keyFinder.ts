/**
 * keyFinder.ts: the Key Finder window. [WIRE + EXPORT]
 *
 * Pick another grid (or every other grid), run, and get the column pairs that share values:
 * the likely join keys, with how much of each side matches. Both sides are grid views, so
 * filter the grids first if only part of a table matters. The server does the work
 * (engine/keyfinder.py); this file draws the form and the ranked table. It opens in a window of
 * its own (openKeyFinderWindow, the page with ?panel=keys), so it can sit beside the grids.
 * Every run also names this grid's own key: the column combination that tells its first rows
 * apart (the server's most_distinct), drawn in the same table style above the pairs.
 *
 * ROUTING: [TARGET PICKER]   -> read-only form state (GET /grids/{id}/key-finder lists grids)
 *          [TRIM / CASE / MAX LENGTH] -> read-only form state
 *          [FIND KEYS]       -> run()        (POST /grids/{id}/key-finder)
 *          [SUMMARY]         -> read-only, derived from the last result
 *          [MOST DISTINCT]   -> read-only, derived from result.unique_key (the build, then alternatives)
 *          [% FOUND HEADERS] -> read-only, named for this grid's table and the target's
 *          [SKIPPED LIST]    -> read-only, derived from result.skipped
 *          [RESULT ROW]      click -> showSample(pair)  (shared values, and values the target lacks)
 *          [SHOW ALL]        -> expands the table past the first 25 pairs
 */
import { api, grid, gridId, token } from "./api";
import { button, clear, formatCount, h, icon, toast } from "./dom";
import { isKnownPair, loadRelationships } from "./relationships";

interface Pair {
  source_column: string;
  target_column: string;
  target_grid: string;
  target_dataset: string;
  target: string;
  shared: number;
  source_distinct: number;
  target_distinct: number;
  source_found: number;
  target_found: number;
  relationship: string;
  score: number;
}
interface Result {
  source: string;
  pairs: Pair[];
  pair_count: number;
  source_checked: string[];
  skipped: Record<string, { column: string; reason: string }[]>;
  targets: { grid: string; label: string; dataset: string }[];
  unique_key?: UniqueKey;
}
/** A column combination and how many of the checked rows it tells apart. */
interface Combo {
  columns: string[];
  distinct: number;
  share: number;
  repeated_rows: number;
  unique: boolean;
}
interface UniqueKey {
  stopped?: boolean;
  row_limit: number;
  rows_checked?: number;
  limited?: boolean;
  steps?: Combo[];
  alternatives?: Combo[];
}
interface Sample {
  matched: unknown[];
  missing: unknown[];
  matched_count: number;
  missing_count: number;
}

const FIRST = 25;
// The last run, kept so switching panel tabs doesn't rerun it.
let last: { result: Result; options: Record<string, unknown>; target: string } | null = null;
let showAll = false;
let open: string | null = null; // the expanded pair

// 0.9 reads as 90%, 0.955 as 95.5%: one decimal at most, none when it would be .0.
/** Open the Key Finder for this grid in its own window (one per grid; a second click focuses it). */
export function openKeyFinderWindow(): void {
  const page = `/g/${encodeURIComponent(gridId)}`;
  const url = `${page}?panel=keys#token=${encodeURIComponent(token)}`;
  const win = window.open(url, `keys-${page}`, "width=1180,height=780");
  if (!win) toast("The browser blocked the Key Finder window; allow pop-ups for this page.", "error");
  else win.focus();
}

const pct = (share: number): string => `${(Math.round(share * 1000) / 10).toLocaleString()}%`;

export async function renderKeyFinder(body: HTMLElement): Promise<void> {
  await loadRelationships();
  let sourceName = ""; // the source table's dataset name, for the known-relationship crowns
  const picker = h("select", { "aria-label": "Compare with" });
  picker.append(h("option", { value: "*" }, "Every other open grid"));
  try {
    const { grids } = await api<{ grids: { grid: string; label: string; self: boolean }[] }>(`${grid}/key-finder`);
    for (const g of grids) picker.append(h("option", { value: g.grid }, g.self ? `${g.label} (this grid: a self-join)` : g.label));
  } catch (e) {
    toast((e as Error).message, "error");
  }
  if (last) picker.value = last.target;
  const trim = h("input", { type: "checkbox", checked: last ? Boolean(last.options.trim) : true });
  const ignoreCase = h("input", { type: "checkbox", checked: last ? Boolean(last.options.ignore_case) : false });
  const maxLength = h("input", { type: "number", min: "1", max: "4096", value: String(last?.options.max_length ?? 128), "aria-label": "Longest value to consider, in characters", style: { width: "6em" } });
  const results = h("div", { class: "kf-results" });
  const status = h("p", { class: "hint", role: "status" });

  const run = async (): Promise<void> => {
    const options = { trim: trim.checked, ignore_case: ignoreCase.checked, max_length: Math.floor(Number(maxLength.value) || 128) };
    clear(status, "Comparing every column's distinct values…");
    status.className = "hint";
    clear(results);
    try {
      const result = await api<Result>(`${grid}/key-finder`, "POST", { target: picker.value, options });
      last = { result, options, target: picker.value };
      showAll = false;
      open = null;
      draw();
    } catch (e) {
      status.textContent = (e as Error).message;
      status.className = "status-error";
    }
  };

  const draw = (): void => {
    if (!last) {
      clear(status, "Finds the columns of other grids that share values with this one: the likely join keys, with how much of each side matches.");
      return;
    }
    const { result } = last;
    sourceName = result.source;
    const skippedHere = result.skipped.source ?? [];
    const skippedThere = Object.entries(result.skipped).filter(([k]) => k !== "source").flatMap(([, v]) => v);
    clear(
      status,
      `${formatCount(result.pair_count)} column pair${result.pair_count === 1 ? "" : "s"} share values · `,
      `${result.source_checked.length} column${result.source_checked.length === 1 ? "" : "s"} checked here against ${result.targets.map((t) => t.label).join(", ") || "no other grid"}`,
    );
    const shown = showAll ? result.pairs : result.pairs.slice(0, FIRST);
    clear(
      results,
      uniqueKeyTable(result.unique_key, result.source),
      result.pairs.length
        ? h(
            "table",
            { class: "kf-table" },
            h(
              "thead",
              {},
              h(
                "tr",
                {},
                h("th", {}, "Current Table Column Name"),
                h("th", {}, "Table"),
                h("th", {}, "Column"),
                h("th", {}, "Relationship"),
                // Each table's name heads the share of its distinct values found in the other table.
                h("th", { class: "num", title: `Share of the ${result.source} column's distinct values found in the other table` }, result.source),
                h("th", { class: "num", title: "Share of the other table's column values found here" }, result.targets.length === 1 ? result.targets[0]!.dataset : "Other table"),
                h("th", { class: "num" }, "Shared values"),
              ),
            ),
            h("tbody", {}, ...shown.flatMap((pair) => row(pair))),
          )
        : h("p", {}, "No column shares any value with the chosen grid(s)."),
      result.pairs.length > shown.length ? button(`Show all ${formatCount(result.pairs.length)}`, () => { showAll = true; draw(); }, { class: "small" }) : null,
      skippedHere.length || skippedThere.length
        ? h(
            "details",
            { class: "kf-skipped" },
            h("summary", {}, `${skippedHere.length + skippedThere.length} column${skippedHere.length + skippedThere.length === 1 ? "" : "s"} not compared`),
            h("ul", {}, ...skippedHere.map((s) => h("li", {}, h("strong", {}, s.column), ` (this grid): ${s.reason}`)), ...skippedThere.map((s) => h("li", {}, h("strong", {}, s.column), `: ${s.reason}`))),
          )
        : null,
    );
  };

  const row = (pair: Pair): HTMLElement[] => {
    const key = `${pair.source_column}|${pair.target_grid}|${pair.target_column}`;
    const expanded = open === key;
    const sampleSlot = h("td", { colspan: "7" });
    const tr = h(
      "tr",
      {
        class: `kf-row ${expanded ? "open" : ""}`,
        tabindex: "0",
        title: "Show shared values and the values the other side lacks",
        "aria-expanded": String(expanded),
        onclick: () => { open = expanded ? null : key; draw(); },
        onkeydown: (e: KeyboardEvent) => { if (e.key === "Enter") { open = expanded ? null : key; draw(); } },
      },
      h("td", {}, isKnownPair(sourceName, pair.target_dataset, pair.source_column, pair.target_column) ? h("span", { class: "known-pair", title: "A known relationship (the relationship model)" }, icon("crown")) : null, h("strong", {}, pair.source_column)),
      h("td", {}, h("span", { class: "kf-target" }, pair.target)),
      h("td", {}, h("strong", {}, pair.target_column)),
      h("td", {}, pair.relationship),
      h("td", { class: "num" }, bar(pair.source_found), pct(pair.source_found)),
      h("td", { class: "num" }, bar(pair.target_found), pct(pair.target_found)),
      // Shared values as fractions: of this table's distinct values, and (below) of the other's.
      h("td", { class: "num", title: `${formatCount(pair.shared)} values in both: out of ${formatCount(pair.source_distinct)} distinct here and ${formatCount(pair.target_distinct)} in ${pair.target}` }, `${formatCount(pair.shared)}/${formatCount(pair.source_distinct)}`, h("div", { class: "hint" }, `${formatCount(pair.shared)}/${formatCount(pair.target_distinct)} of theirs`)),
    );
    if (!expanded) return [tr];
    void showSample(pair, sampleSlot);
    return [tr, h("tr", { class: "kf-sample" }, sampleSlot)];
  };

  body.append(
    h(
      "div",
      { class: "kf-form" },
      h("label", { class: "field" }, h("span", {}, "Compare with"), picker),
      h("label", { class: "check" }, trim, "Trim spaces"),
      h("label", { class: "check" }, ignoreCase, "Ignore case"),
      h("label", { class: "field" }, h("span", {}, "Longest value (characters)"), maxLength),
      button("Find keys", () => void run(), { icon: "zoom", class: "primary" }),
    ),
    status,
    results,
  );
  draw();
}

/** [MOST DISTINCT]: the columns that tell this grid's rows apart, one row per column added and
 * the last row the answer, then any other combination as small that is also unique. */
function uniqueKeyTable(found: UniqueKey | undefined, source: string): HTMLElement | null {
  if (!found) return null;
  const heading = h("h3", { class: "kf-heading" }, `Most distinct column combination in ${source}`);
  if (found.stopped) {
    return h("div", { class: "kf-unique" }, heading, h("p", { class: "status-error" }, "The search stopped at the time limit. Filter the grid first."));
  }
  const steps = found.steps ?? [];
  const rows = found.rows_checked ?? 0;
  const best = steps[steps.length - 1];
  if (!best) return h("div", { class: "kf-unique" }, heading, h("p", { class: "hint" }, rows ? "No column could be checked." : "The grid has no rows."));
  const scope = found.limited ? `the first ${formatCount(rows)} rows` : `all ${formatCount(rows)} rows`;
  const verdict = best.unique
    ? `${best.columns.join(" + ")} tells every one of ${scope} apart: the most distinct combination of columns in ${source}.`
    : `No combination tells every row apart: ${formatCount(best.repeated_rows)} of ${scope} repeat an earlier row in every column checked.`;
  const line = (combo: Combo, note: string, answer: boolean): HTMLElement =>
    h(
      "tr",
      { class: answer ? "kf-best" : "" },
      h("td", {}, ...combo.columns.flatMap((name, i) => [i ? " + " : "", h("strong", {}, name)])),
      h("td", {}, note),
      h("td", { class: "num" }, bar(combo.share), pct(combo.share)),
      h(
        "td",
        { class: "num", title: `${formatCount(combo.distinct)} distinct combinations across ${formatCount(rows)} rows` },
        `${formatCount(combo.distinct)}/${formatCount(rows)}`,
        h("div", { class: "hint" }, combo.repeated_rows ? `${formatCount(combo.repeated_rows)} rows repeat` : "no row repeats"),
      ),
    );
  return h(
    "div",
    { class: "kf-unique" },
    heading,
    h("p", { class: "hint" }, verdict),
    h(
      "table",
      { class: "kf-table" },
      h(
        "thead",
        {},
        h(
          "tr",
          {},
          h("th", {}, "Current Table Column Names"),
          h("th", {}, "Relationship"),
          h("th", { class: "num", title: `Share of ${scope} each combination tells apart` }, "Unique rows"),
          h("th", { class: "num" }, "Distinct combinations"),
        ),
      ),
      h(
        "tbody",
        {},
        ...steps.map((combo, i) => {
          const answer = i === steps.length - 1;
          const note = answer ? (combo.unique ? "unique per row: the most distinct combination" : "the most distinct combination") : `add a column (${i + 1} of ${steps.length})`;
          return line(combo, note, answer);
        }),
        ...(found.alternatives ?? []).map((combo) => line(combo, "also unique per row", false)),
      ),
    ),
  );
}

function bar(share: number): HTMLElement {
  return h("span", { class: "kf-bar", "aria-hidden": "true" }, h("span", { style: { width: `${Math.round(share * 100)}%` } }));
}

async function showSample(pair: Pair, slot: HTMLElement): Promise<void> {
  clear(slot, h("span", { class: "hint" }, "Loading values…"));
  try {
    const sample = await api<Sample>(`${grid}/key-finder/sample`, "POST", {
      target: pair.target_grid,
      source_column: pair.source_column,
      target_column: pair.target_column,
      options: last?.options,
    });
    const list = (values: unknown[]): HTMLElement => h("div", { class: "kf-values" }, ...values.map((v) => h("code", {}, String(v))));
    clear(
      slot,
      h("div", { class: "kf-sample-body" },
        h("div", {}, h("strong", {}, `${formatCount(sample.matched_count)} shared`), sample.matched.length ? list(sample.matched) : h("span", { class: "hint" }, " none")),
        h("div", {}, h("strong", {}, `${formatCount(sample.missing_count)} of ${pair.source_column} not in ${pair.target_column}`), h("span", { class: "hint" }, " (rows an inner join would drop)"), sample.missing.length ? list(sample.missing) : h("span", { class: "hint" }, " none")),
      ),
    );
  } catch (e) {
    clear(slot, h("span", { class: "status-error" }, (e as Error).message));
  }
}
