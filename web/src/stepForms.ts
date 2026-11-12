/**
 * stepForms.ts: the add/edit form for every step kind (plan section 7). [WIRE]
 *
 * editStep(kind, existing?) opens a dialog with the right fields for that kind, then saves the
 * step through state.addStep / state.saveSteps. The server validates again and reports the
 * first broken step, so a form only checks what it can check cheaply.
 *
 * ROUTING: [FILTER FORM]     column + op + value(s)        -> save()
 *          [SORT FORM]       up to three keys               -> save()
 *          [COLUMNS FORM]    keep / reorder                 -> save()
 *          [RENAME FORM]     new name per column            -> save()
 *          [GROUP BY FORM]   keys + aggregate rows          -> save()
 *          [LIMIT/DISTINCT]  n                              -> save()
 *          [TRANSFORM FORM]  fn + columns                   -> save()
 *          [BUCKET FORM]     edges / width / date unit      -> save()  (range prefilled from profile)
 *          [CATEGORIZE FORM] ordered rules + unmatched      -> save()
 *          [SQL STEP]        sqlForm: one SELECT over the table entering the step
 */
import { api, grid, query, type ColumnInfo, type StepJson } from "./api";
import { button, h, isBoolType, isDateType, isNumericType, isTextType, openDialog, toast } from "./dom";
import { state } from "./state";

type Params = Record<string, unknown>;
interface Form {
  element: HTMLElement;
  read: () => Params;
}

export const KIND_LABELS: Record<string, string> = {
  filter: "Filter",
  sort: "Sort",
  columns: "Choose columns",
  rename: "Rename",
  group_by: "Group by",
  distinct: "Distinct rows",
  limit: "Keep first rows",
  transform: "Clean text",
  bucket: "Bucket",
  categorize: "Categorize",
  sql: "SQL transformation",
};

const OPS: Record<string, [string, string][]> = {
  text: [
    ["eq", "equals"], ["ne", "does not equal"], ["in", "is one of"], ["not_in", "is none of"],
    ["contains", "contains"], ["starts_with", "starts with"], ["ends_with", "ends with"],
    ["blank", "is blank"], ["not_blank", "is not blank"],
  ],
  number: [
    ["eq", "equals"], ["ne", "does not equal"], ["lt", "less than"], ["gt", "greater than"],
    ["between", "between"], ["in", "is one of"], ["not_in", "is none of"],
    ["blank", "is blank"], ["not_blank", "is not blank"],
  ],
  date: [
    ["before", "before"], ["after", "after"], ["date_between", "between"], ["eq", "on"],
    ["blank", "is blank"], ["not_blank", "is not blank"],
  ],
  boolean: [["true", "is true"], ["false", "is false"], ["blank", "is blank"], ["not_blank", "is not blank"]],
};

function family(type: string): string {
  if (isNumericType(type)) return "number";
  if (isDateType(type)) return "date";
  if (isBoolType(type)) return "boolean";
  return "text";
}

function select(options: [string, string][], value?: string, label?: string): HTMLSelectElement {
  const el = h("select", { "aria-label": label });
  for (const [v, text] of options) el.append(h("option", { value: v, selected: v === value }, text));
  return el;
}

function field(label: string, control: HTMLElement): HTMLElement {
  return h("label", { class: "field" }, h("span", {}, label), control);
}

function columnSelect(columns: ColumnInfo[], value?: unknown, only?: (c: ColumnInfo) => boolean): HTMLSelectElement {
  const options = columns.filter(only ?? (() => true)).map((c): [string, string] => [c.name, `${c.name}  (${c.type.toLowerCase()})`]);
  return select(options, typeof value === "string" ? value : undefined, "Column");
}

function numberOrText(raw: string, numeric: boolean): unknown {
  if (!numeric) return raw;
  const n = Number(raw);
  if (raw.trim() === "" || Number.isNaN(n)) throw new Error(`"${raw}" is not a number.`);
  return n;
}

function splitList(raw: string): string[] {
  return raw.split(/\r?\n|,/).map((s) => s.trim()).filter((s) => s.length > 0);
}

// ===== one form per kind =====

function filterForm(columns: ColumnInfo[], p: Params): Form {
  const column = columnSelect(columns, p.column);
  const op = h("select", { "aria-label": "Condition" });
  const value = h("input", { type: "text", "aria-label": "Value" });
  const high = h("input", { type: "text", "aria-label": "Upper value" });
  const values = h("textarea", { rows: "4", "aria-label": "Values, one per line" });
  const valueField = field("Value", value);
  const highField = field("To", high);
  const valuesField = field("Values (one per line)", values);
  const colType = (): string => columns.find((c) => c.name === column.value)?.type ?? "VARCHAR";

  const fillOps = (keep?: string): void => {
    op.replaceChildren();
    for (const [v, text] of OPS[family(colType())] ?? []) op.append(h("option", { value: v, selected: v === keep }, text));
    show();
  };
  const show = (): void => {
    const o = op.value;
    const fam = family(colType());
    value.type = fam === "date" ? "date" : fam === "number" ? "number" : "text";
    high.type = value.type;
    valueField.hidden = ["blank", "not_blank", "true", "false", "in", "not_in"].includes(o);
    highField.hidden = !["between", "date_between"].includes(o);
    valuesField.hidden = !["in", "not_in"].includes(o);
    valueField.querySelector("span")!.textContent = ["between", "date_between"].includes(o) ? "From" : "Value";
  };
  column.addEventListener("change", () => fillOps());
  op.addEventListener("change", show);
  fillOps(typeof p.op === "string" ? p.op : undefined);
  value.value = String(p.value ?? p.low ?? "");
  high.value = String(p.high ?? "");
  values.value = Array.isArray(p.values) ? p.values.join("\n") : "";
  show();

  return {
    element: h("div", { class: "form-grid" }, field("Column", column), field("Condition", op), h("div", { class: "range-row" }, valueField, highField), valuesField),
    read: () => {
      const fam = family(colType());
      const o = op.value;
      const out: Params = { column: column.value, op: o };
      if (["between", "date_between"].includes(o)) {
        out.low = numberOrText(value.value, fam === "number");
        out.high = numberOrText(high.value, fam === "number");
      } else if (["in", "not_in"].includes(o)) {
        out.values = splitList(values.value).map((v) => numberOrText(v, fam === "number"));
      } else if (!["blank", "not_blank", "true", "false"].includes(o)) {
        out.value = numberOrText(value.value, fam === "number");
      }
      return out;
    },
  };
}

function sortForm(columns: ColumnInfo[], p: Params): Form {
  const keys = (p.keys as [string, string][] | undefined) ?? [[columns[0]?.name ?? "", "asc"]];
  const rows = [0, 1, 2].map((i) => {
    const col = select([["", "(none)"], ...columns.map((c): [string, string] => [c.name, c.name])], keys[i]?.[0] ?? "", `Sort key ${i + 1}`);
    const dir = select([["asc", "ascending"], ["desc", "descending"]], keys[i]?.[1] ?? "asc", `Direction ${i + 1}`);
    return { col, dir };
  });
  return {
    element: h(
      "div",
      { class: "list-editor" },
      h("p", { class: "hint" }, "Blanks always sort last. Ties keep their current order."),
      ...rows.map((r, i) => h("div", { class: "row" }, field(i === 0 ? "Sort by" : "then by", r.col), field("Direction", r.dir))),
    ),
    read: () => ({ keys: rows.filter((r) => r.col.value).map((r) => [r.col.value, r.dir.value]) }),
  };
}

function columnsForm(columns: ColumnInfo[], p: Params): Form {
  const keep = new Set((p.keep as string[] | undefined) ?? columns.map((c) => c.name));
  const order = [...((p.keep as string[] | undefined) ?? []), ...columns.map((c) => c.name).filter((n) => !keep.has(n) || !(p.keep as string[] | undefined)?.includes(n))];
  const unique = [...new Set(order)].filter((n) => columns.some((c) => c.name === n));
  const list = h("div", { class: "list-editor" });
  const render = (): void => {
    list.replaceChildren(
      ...unique.map((name, i) =>
        h(
          "div",
          { class: "row" },
          h("label", { class: "check" }, h("input", { type: "checkbox", checked: keep.has(name), onchange: (e: Event) => ((e.target as HTMLInputElement).checked ? keep.add(name) : keep.delete(name)) }), name),
          h(
            "div",
            {},
            button("Move up", () => { if (i > 0) { [unique[i - 1], unique[i]] = [unique[i]!, unique[i - 1]!]; render(); } }, { icon: "collapse", iconOnly: true, class: "ghost" }),
            button("Move down", () => { if (i < unique.length - 1) { [unique[i + 1], unique[i]] = [unique[i]!, unique[i + 1]!]; render(); } }, { icon: "expand", iconOnly: true, class: "ghost" }),
          ),
        ),
      ),
    );
  };
  render();
  return { element: h("div", {}, h("p", { class: "hint" }, "Tick the columns to keep; arrows set their order."), list), read: () => ({ keep: unique.filter((n) => keep.has(n)) }) };
}

function renameForm(columns: ColumnInfo[], p: Params): Form {
  const mapping = (p.mapping as Record<string, string> | undefined) ?? {};
  const inputs = columns.map((c) => ({ name: c.name, input: h("input", { type: "text", value: mapping[c.name] ?? "", placeholder: c.name, "aria-label": `New name for ${c.name}` }) }));
  return {
    element: h("div", { class: "form-grid" }, ...inputs.map((i) => field(i.name, i.input))),
    read: () => {
      const out: Record<string, string> = {};
      for (const { name, input } of inputs) if (input.value.trim() && input.value.trim() !== name) out[name] = input.value.trim();
      return { mapping: out };
    },
  };
}

function groupByForm(columns: ColumnInfo[], p: Params): Form {
  const keys = h("select", { multiple: true, size: String(Math.min(8, columns.length)), "aria-label": "Group by columns" });
  for (const c of columns) keys.append(h("option", { value: c.name, selected: ((p.keys as string[] | undefined) ?? []).includes(c.name) }, c.name));
  const aggs = (p.aggregates as Params[] | undefined) ?? [{ fn: "count_rows" }];
  const list = h("div", { class: "list-editor" });
  const fns: [string, string][] = [["count_rows", "count of rows"], ["count", "count of non-blank"], ["distinct_count", "distinct count"], ["sum", "sum"], ["mean", "mean"], ["min", "min"], ["max", "max"]];
  const rows: { fn: HTMLSelectElement; col: HTMLSelectElement; as: HTMLInputElement; el: HTMLElement }[] = [];
  const addRow = (a: Params): void => {
    const fn = select(fns, String(a.fn ?? "count_rows"), "Aggregate");
    const col = columnSelect(columns, a.column);
    const as = h("input", { type: "text", value: String(a.as ?? ""), placeholder: "name (optional)", "aria-label": "Output name" });
    const row = { fn, col, as, el: h("div", { class: "row" }) };
    const sync = (): void => { col.disabled = fn.value === "count_rows"; };
    fn.addEventListener("change", sync);
    sync();
    row.el.append(field("Aggregate", fn), field("of", col), field("as", as), button("Remove", () => { row.el.remove(); rows.splice(rows.indexOf(row), 1); }, { icon: "trash", iconOnly: true, class: "ghost" }));
    rows.push(row);
    list.append(row.el);
  };
  aggs.forEach(addRow);
  return {
    element: h("div", { class: "list-editor" }, field("Group by (Ctrl+click for several; none = whole table)", keys), h("h3", {}, "Aggregates"), list, button("Add aggregate", () => addRow({ fn: "sum" }), { icon: "plus" })),
    read: () => ({
      keys: [...keys.selectedOptions].map((o) => o.value),
      aggregates: rows.map((r) => {
        const out: Params = { fn: r.fn.value };
        if (r.fn.value !== "count_rows") out.column = r.col.value;
        if (r.as.value.trim()) out.as = r.as.value.trim();
        return out;
      }),
    }),
  };
}

function limitForm(_: ColumnInfo[], p: Params): Form {
  const n = h("input", { type: "number", min: "0", value: String(p.n ?? 100), "aria-label": "Rows to keep" });
  return { element: field("Keep the first … rows", n), read: () => ({ n: Math.max(0, Math.floor(Number(n.value))) }) };
}

function transformForm(columns: ColumnInfo[], p: Params): Form {
  const fn = select([["trim_both", "Trim both ends"], ["trim_left", "Trim left"], ["trim_right", "Trim right"], ["upper", "UPPER CASE"], ["lower", "lower case"]], String(p.fn ?? "trim_both"), "Clean-up");
  const all = h("input", { type: "checkbox", checked: p.columns === "*text" || p.columns === undefined });
  const text = columns.filter((c) => isTextType(c.type));
  const picks = text.map((c) => ({ name: c.name, box: h("input", { type: "checkbox", checked: Array.isArray(p.columns) && p.columns.includes(c.name) }) }));
  const list = h("div", { class: "form-grid" }, ...picks.map((x) => h("label", { class: "check" }, x.box, x.name)));
  const sync = (): void => { list.hidden = all.checked; };
  all.addEventListener("change", sync);
  sync();
  return {
    element: h("div", { class: "list-editor" }, field("Clean-up", fn), h("p", { class: "hint" }, "Trimming removes spaces, tabs and line breaks."), h("label", { class: "check" }, all, "Every text column"), list),
    read: () => ({ fn: fn.value, columns: all.checked ? "*text" : picks.filter((x) => x.box.checked).map((x) => x.name) }),
  };
}

function bucketForm(columns: ColumnInfo[], p: Params): Form {
  const usable = columns.filter((c) => isNumericType(c.type) || isDateType(c.type));
  const column = columnSelect(usable, p.column);
  const as = h("input", { type: "text", value: String(p.as ?? ""), "aria-label": "New column name" });
  const mode = select([["width", "Fixed width"], ["edges", "Custom edges"], ["date", "Date unit"]], String(p.mode ?? "width"), "Bucket by");
  const width = h("input", { type: "number", value: String(p.width ?? ""), "aria-label": "Width" });
  const low = h("input", { type: "number", value: String(p.low ?? ""), "aria-label": "From" });
  const high = h("input", { type: "number", value: String(p.high ?? ""), "aria-label": "To" });
  const edges = h("input", { type: "text", value: Array.isArray(p.edges) ? p.edges.join(", ") : "", placeholder: "0, 10, 50, 100", "aria-label": "Edges" });
  const unit = select([["day", "day"], ["week", "week (from Monday)"], ["month", "month"], ["quarter", "quarter"], ["year", "year"]], String(p.unit ?? "month"), "Unit");
  const right = h("input", { type: "checkbox", checked: Boolean(p.right_closed) });
  const other = h("input", { type: "text", value: String(p.other_label ?? "Other"), "aria-label": "Out-of-range label" });
  const parts = {
    width: [field("Width", width), field("From", low), field("To", high)],
    edges: [field("Edges (comma separated)", edges)],
    date: [field("Unit", unit)],
  };
  const extra = [h("label", { class: "check" }, right, "Right-closed: (a, b] instead of [a, b)"), field("Outside the range", other)];
  const box = h("div", { class: "form-grid" });
  const sync = async (): Promise<void> => {
    const isDate = isDateType(usable.find((c) => c.name === column.value)?.type ?? "");
    if (isDate) mode.value = "date";
    if (!as.value || as.value.endsWith("_bucket")) as.value = `${column.value}_bucket`;
    box.replaceChildren(field("Column", column), field("New column", as), field("Bucket by", mode), ...(parts[mode.value as keyof typeof parts] ?? []), ...(mode.value === "date" ? [] : extra));
    if (mode.value === "width" && (!low.value || !high.value)) {
      try {
        const prof = await api<{ min?: number; max?: number }>(`${grid}/profile${query({ column: column.value })}`);
        if (prof.min != null && prof.max != null) {
          low.value = String(Math.floor(prof.min));
          high.value = String(Math.ceil(prof.max) + (Number.isInteger(prof.max) ? 1 : 0));
          if (!width.value) width.value = String(Math.max(1, Math.ceil((prof.max - prof.min) / 10)));
        }
      } catch {
        // No profile: the user types the range.
      }
    }
  };
  column.addEventListener("change", () => void sync());
  mode.addEventListener("change", () => void sync());
  void sync();
  return {
    element: box,
    read: () => {
      const out: Params = { column: column.value, as: as.value.trim(), mode: mode.value };
      if (mode.value === "date") out.unit = unit.value;
      else {
        if (mode.value === "width") Object.assign(out, { width: Number(width.value), low: Number(low.value), high: Number(high.value) });
        else out.edges = edges.value.split(",").map((e) => numberOrText(e.trim(), true));
        out.right_closed = right.checked;
        out.other_label = other.value;
      }
      return out;
    },
  };
}

function categorizeForm(columns: ColumnInfo[], p: Params): Form {
  const column = columnSelect(columns, p.column, (c) => isTextType(c.type) || isNumericType(c.type));
  const as = h("input", { type: "text", value: String(p.as ?? "category"), "aria-label": "New column name" });
  const unmatched = h("input", { type: "text", value: String(p.unmatched ?? "Other"), "aria-label": "Unmatched label" });
  const list = h("div", { class: "list-editor" });
  const rows: { type: HTMLSelectElement; value: HTMLInputElement; high: HTMLInputElement; label: HTMLInputElement; el: HTMLElement }[] = [];
  const addRule = (r: Params): void => {
    const type = select([["wildcard", "matches (% = any text)"], ["in", "is one of"], ["contains", "contains"], ["starts_with", "starts with"], ["range", "number from … to"]], String(r.type ?? "wildcard"), "Rule");
    const value = h("input", { type: "text", value: String(r.value ?? (Array.isArray(r.values) ? r.values.join(", ") : r.low ?? "")), "aria-label": "Rule value" });
    const high = h("input", { type: "number", value: String(r.high ?? ""), "aria-label": "Up to (not including)" });
    const label = h("input", { type: "text", value: String(r.label ?? ""), "aria-label": "Label" });
    const highField = field("up to (excl.)", high);
    const sync = (): void => { highField.hidden = type.value !== "range"; };
    type.addEventListener("change", sync);
    sync();
    const row = { type, value, high, label, el: h("div", { class: "row" }) };
    row.el.append(field(`Rule ${rows.length + 1}`, type), field("value", value), highField, field("label", label), button("Remove rule", () => { row.el.remove(); rows.splice(rows.indexOf(row), 1); }, { icon: "trash", iconOnly: true, class: "ghost" }));
    rows.push(row);
    list.append(row.el);
  };
  ((p.rules as Params[] | undefined) ?? [{ type: "wildcard" }]).forEach(addRule);
  return {
    element: h(
      "div",
      { class: "list-editor" },
      h("div", { class: "form-grid" }, field("Column", column), field("New column", as), field("Unmatched rows get", unmatched)),
      h("p", { class: "hint" }, "Rules run in order and the first match wins. Blanks stay blank. After saving, the step shows how many rows matched nothing and how many matched more than one rule."),
      list,
      button("Add rule", () => addRule({ type: "contains" }), { icon: "plus" }),
    ),
    read: () => ({
      column: column.value,
      as: as.value.trim(),
      unmatched: unmatched.value,
      rules: rows.map((r) => {
        const base: Params = { type: r.type.value, label: r.label.value };
        if (r.type.value === "range") return { ...base, low: numberOrText(r.value.value, true), high: numberOrText(r.high.value, true) };
        if (r.type.value === "in") return { ...base, values: splitList(r.value.value) };
        return { ...base, value: r.value.value };
      }),
    }),
  };
}

/** A SQL step: one SELECT over the table entering it, read as `grid` or by the table's name. The server
 * checks it (read-only, one query) when it is saved; transforming at length belongs in the notebook. */
function sqlForm(_columns: ColumnInfo[], params: Params): Form {
  const box = h("textarea", {
    class: "code-input",
    rows: "10",
    spellcheck: "false",
    "aria-label": "SQL",
    style: { width: "100%", fontFamily: '"Cascadia Code", Consolas, monospace' },
  }) as HTMLTextAreaElement;
  box.value = typeof params.sql === "string" ? params.sql : `SELECT *\nFROM grid`;
  const name = state.info?.dataset ?? "grid";
  return {
    element: h(
      "div",
      { class: "form-grid", style: { gridTemplateColumns: "1fr" } },
      h("p", { class: "hint" }, "One SELECT over the table entering this step: ", h("code", {}, "grid"), ", or ", h("code", {}, name), "."),
      box,
    ),
    read: () => {
      if (!box.value.trim()) throw new Error("Write a SELECT.");
      return { sql: box.value };
    },
  };
}

const FORMS: Record<string, (columns: ColumnInfo[], params: Params) => Form> = {
  filter: filterForm,
  sort: sortForm,
  columns: columnsForm,
  rename: renameForm,
  group_by: groupByForm,
  limit: limitForm,
  transform: transformForm,
  bucket: bucketForm,
  categorize: categorizeForm,
  sql: sqlForm,
};

/** Open the form for `kind`. With `index`, edit that step; otherwise add a new one. The
 * columns offered are the ones entering the step. */
export function editStep(kind: string, index?: number, preset: Params = {}): void {
  const info = state.info;
  if (!info) return;
  if (kind === "distinct") {
    void state.addStep({ kind: "distinct", params: {} }).catch((e: Error) => toast(e.message, "error"));
    return;
  }
  const existing = index !== undefined ? info.steps[index] : undefined;
  const entering = info.stage_columns[Math.min(index ?? info.steps.filter((s) => !s.params.header).length, info.stage_columns.length - 1)] ?? info.columns;
  const form = FORMS[kind]?.(entering, { ...preset, ...(existing?.params ?? {}) });
  if (!form) return;
  const { body, foot, close } = openDialog(`${existing ? "Edit" : "Add"} step: ${KIND_LABELS[kind] ?? kind}`);
  const error = h("p", { class: "status-error", role: "alert" });
  body.append(form.element, error);
  const save = async (): Promise<void> => {
    try {
      const params = form.read();
      if (existing && index !== undefined) {
        const steps = [...info.steps];
        steps[index] = { ...existing, params } as StepJson;
        await state.saveSteps(steps);
      } else {
        await state.addStep({ kind, params });
      }
      const broken = state.info?.step_error;
      if (broken) toast(`Step ${broken.index + 1}: ${broken.message}`, "error");
      close();
    } catch (e) {
      error.textContent = (e as Error).message;
    }
  };
  foot.append(button("Cancel", close), button(existing ? "Save step" : "Add step", () => void save(), { class: "primary" }));
}
