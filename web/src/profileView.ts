/**
 * profileView.ts: the column profile and the whole-table Profile view (plan section 8). [WIRE]
 *
 * ROUTING: [STAT TILES]    -> read-only, from GET /grids/{id}/profile?column=
 *          [HISTOGRAM]     -> read-only
 *          [VALUE COUNTS]  -> click a value -> state.addStep(equals filter)
 *          [CASE GROUPS]   -> read-only (values that differ only by case)
 *          [TABLE PROFILE] -> openTableProfile(); Export -> scope=profile download
 *          [COLUMN PROFILE] popup, one column at a time -> openColumnProfile(column); its picker re-profiles
 */
import { api, download, grid, query } from "./api";
import { button, clear, formatCount, h, openDialog, toast } from "./dom";
import { formatTemporal } from "./gridView";
import { state } from "./state";

interface Profile {
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

const decimals = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 0 });
const tiny = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 4 });
/** Thousands separators, at most two decimals (four significant digits below 1), never
 * scientific notation; dates without a midnight time. */
const fmt = (v: unknown): string => {
  if (v === null || v === undefined) return "(blank)";
  if (typeof v === "number") return Number.isInteger(v) ? formatCount(v) : Math.abs(v) < 1 ? tiny.format(v) : decimals.format(v);
  return formatTemporal(String(v));
};

function tile(label: string, value: unknown): HTMLElement {
  return h("div", { class: "stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, fmt(value)));
}

export async function renderColumnProfile(host: HTMLElement, column: string): Promise<void> {
  clear(host, h("p", { class: "hint" }, `Profiling ${column}…`));
  let p: Profile;
  try {
    p = await api<Profile>(`${grid}/profile${query({ column, upto: state.previewUpto })}`);
  } catch (e) {
    clear(host, h("p", { class: "status-error" }, (e as Error).message));
    return;
  }
  const tiles = [
    tile("Rows", p.rows),
    tile("Blank", `${formatCount(p.blanks)} (${p.blank_percent}%)`),
    tile("Distinct", p.distinct),
    tile("All unique", p.all_unique ? "yes" : "no"),
  ];
  if (p.family === "number") {
    for (const key of ["min", "max", "mean", "median", "q1", "q3", "stddev", "sum", "zeros", "negatives"]) {
      tiles.push(tile(key === "q1" ? "1st quartile" : key === "q3" ? "3rd quartile" : key === "stddev" ? "Std deviation" : key[0]!.toUpperCase() + key.slice(1), p[key]));
    }
  } else if (p.family === "text") {
    tiles.push(tile("Shortest", p.shortest), tile("Longest", p.longest), tile("Empty text", p.empty_strings), tile("Padded spaces", p.padded));
  } else if (p.family === "date" || p.family === "timestamp") {
    tiles.push(tile("Earliest", p.earliest), tile("Latest", p.latest));
  }
  const top = Math.max(1, ...p.top_values.map((t) => t.count));
  const values = h(
    "div",
    { class: "bars", role: "list", "aria-label": "Most common values; click one to filter" },
    ...p.top_values.map((t) =>
      h(
        "button",
        {
          type: "button",
          class: "bar-row",
          role: "listitem",
          title: `Keep only rows where ${column} ${t.value === null ? "is blank" : `equals ${fmt(t.value)}`}`,
          onclick: () => void filterOn(column, t.value, p.family),
        },
        h("span", { class: "label", style: { "--share": `${(100 * t.count) / top}%` } }, h("span", {}, fmt(t.value))),
        h("span", { class: "count" }, formatCount(t.count)),
      ),
    ),
  );
  const parts: (HTMLElement | null)[] = [
    h("h3", { class: "section-title" }, `${column} `, h("span", { class: "hint" }, p.type.toLowerCase())),
    h("div", { class: "stat-grid" }, ...tiles),
  ];
  const histogram = p.histogram as { low: number; high: number; count: number }[] | undefined;
  if (histogram?.length) {
    const max = Math.max(1, ...histogram.map((b) => b.count));
    parts.push(
      h("h3", {}, "Distribution"),
      h(
        "div",
        { class: "histogram", role: "img", "aria-label": `Histogram of ${column} from ${fmt(histogram[0]!.low)} to ${fmt(histogram.at(-1)!.high)}` },
        ...histogram.map((b) => h("span", { title: `${fmt(b.low)} to ${fmt(b.high)}: ${formatCount(b.count)} rows`, style: { height: `${(100 * b.count) / max}%` } })),
      ),
      h("div", { class: "histogram-axis", "aria-hidden": "true" }, h("span", {}, fmt(histogram[0]!.low)), h("span", {}, `tallest bar: ${formatCount(max)} rows`), h("span", {}, fmt(histogram.at(-1)!.high))),
    );
  }
  const byMonth = p.by_month as { month: string; count: number }[] | undefined;
  if (byMonth?.length) {
    const max = Math.max(1, ...byMonth.map((b) => b.count));
    parts.push(
      h("h3", {}, "Rows by month"),
      h("div", { class: "histogram", role: "img", "aria-label": `Rows by month for ${column}` }, ...byMonth.map((b) => h("span", { title: `${b.month}: ${formatCount(b.count)}`, style: { height: `${(100 * b.count) / max}%` } }))),
    );
  }
  parts.push(h("h3", {}, "Most common values"), values);
  const groups = p.case_groups as { variants: string[]; rows: number }[] | undefined;
  if (groups?.length) {
    parts.push(
      h("h3", {}, "Differ only by case"),
      h("ul", { class: "activity" }, ...groups.map((g) => h("li", {}, g.variants.join(" · "), h("span", { class: "hint" }, ` (${formatCount(g.rows)} rows)`)))),
    );
  }
  clear(host, ...parts);
}

async function filterOn(column: string, value: unknown, family: string): Promise<void> {
  const params: Record<string, unknown> =
    value === null ? { column, op: "blank" } : family === "date" || family === "timestamp" ? { column, op: "date_between", low: String(value).slice(0, 10), high: String(value).slice(0, 10) } : family === "boolean" ? { column, op: value ? "true" : "false" } : { column, op: "eq", value };
  try {
    await state.addStep({ kind: "filter", params });
    toast(`Added a filter step on ${column}.`);
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

/** One column's profile in a popup: a one-time look, opened from the Column row's Profile or a
 * header's profile button. The picker switches columns without closing it. */
export function openColumnProfile(column: string): void {
  const { dialog, body, foot, close } = openDialog(`Profile of ${column}`, true);
  dialog.classList.add("profile-window");
  const picker = h("select", { "aria-label": "Column to profile", onchange: () => void show(picker.value) });
  for (const c of state.previewColumns()) picker.append(h("option", { value: c.name, selected: c.name === column }, c.name));
  const slot = h("div");
  body.append(h("div", { class: "code-toolbar", style: { marginBottom: "10px" } }, picker, h("span", { class: "hint" }, "Click a value to add a filter step.")), slot);
  foot.append(button("Close", close, { class: "primary" }));
  const show = async (name: string): Promise<void> => {
    (body.closest("dialog")?.querySelector(".dialog-head h2") as HTMLElement | null)?.replaceChildren(`Profile of ${name}`);
    clear(slot);
    await renderColumnProfile(slot, name);
  };
  void show(column);
}

export async function openTableProfile(): Promise<void> {
  const { dialog, body, foot, close } = openDialog("Profile of the whole view", true);
  dialog.classList.add("profile-window");
  body.append(h("p", { class: "hint" }, "Profiling every column…"));
  try {
    const result = await api<{ rows: number; duplicate_rows: number; columns: Record<string, unknown>[] }>(`${grid}/profile${query({ upto: state.previewUpto })}`);
    const head = ["Column", "Type", "Blank", "Blank %", "Distinct", "Min", "Max"];
    clear(
      body,
      h("div", { class: "stat-grid" }, tile("Rows", result.rows), tile("Duplicate rows", result.duplicate_rows), tile("Columns", result.columns.length)),
      h(
        "div",
        { style: { overflow: "auto" } },
        h(
          "table",
          { class: "small" },
          h("thead", {}, h("tr", {}, ...head.map((x) => h("th", { scope: "col" }, x)))),
          h(
            "tbody",
            {},
            ...result.columns.map((c) =>
              h(
                "tr",
                {},
                h("td", {}, String(c.column)),
                h("td", {}, String(c.type).toLowerCase()),
                h("td", { class: "num" }, fmt(c.blanks)),
                h("td", { class: "num" }, `${String(c.blank_percent)}%`),
                h("td", { class: "num" }, fmt(c.distinct)),
                h("td", {}, fmt(c.min)),
                h("td", {}, fmt(c.max)),
              ),
            ),
          ),
        ),
      ),
    );
  } catch (e) {
    clear(body, h("p", { class: "status-error" }, (e as Error).message));
  }
  foot.append(
    button(
      "Export profile (CSV)",
      () =>
        void download(`${grid}/export${query({ format: "csv", scope: "profile", file: `${state.info?.dataset ?? "data"}_profile` })}`)
          .then(({ blob, name, message }) => { saveBlob(blob, name); toast(message); })
          .catch((e: Error) => toast(e.message, "error")),
      { icon: "export" },
    ),
    button("Close", close, { class: "primary" }),
  );
}

export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const link = h("a", { href: url, download: name });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
