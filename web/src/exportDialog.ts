/**
 * exportDialog.ts: export to text, CSV, Excel or Parquet (plan section 10a). [WIRE]
 *
 * The file is built by the server from the grid's own step list, so it always matches the
 * grid. By default it downloads (the user chooses where it lands); "Save to export folder"
 * writes into the one configured folder and never overwrites without asking.
 *
 * ROUTING: [FORMAT / SCOPE / OPTIONS] -> read-only form state
 *          [DOWNLOAD]                 -> exportNow(false)
 *          [SAVE TO FOLDER]           -> exportNow(true)
 */
import { ApiError, api, download, grid, query } from "./api";
import { button, h, openDialog, toast } from "./dom";
import { saveBlob } from "./profileView";
import { selection, shownTotal } from "./gridView";
import { state } from "./state";

export function openExport(): void {
  const info = state.info;
  if (!info) return;
  const { body, foot, close } = openDialog("Export");
  const field = (label: string, el: HTMLElement): HTMLElement => h("label", { class: "field" }, h("span", {}, label), el);
  const select = (pairs: [string, string][], value?: string): HTMLSelectElement => {
    const el = h("select", {});
    for (const [v, t] of pairs) el.append(h("option", { value: v, selected: v === value }, t));
    return el;
  };
  const format = select([["csv", "CSV (.csv)"], ["txt", "Delimited text (.txt)"], ["xlsx", "Excel (.xlsx)"], ["parquet", "Parquet (.parquet)"]], "csv");
  const selected = selection.descriptor();
  const selectedLabel = selected
    ? `Selection (${selection.rowCount(shownTotal()).toLocaleString()} rows${selected.columns ? ` × ${selected.columns.length} columns` : ""})`
    : "";
  const scope = select(
    [
      ["view", "Current view"],
      ...(selected ? ([["selection", selectedLabel]] as [string, string][]) : []),
      ["columns", "Some columns"],
      ["source", "Whole source (no steps)"],
      ["profile", "Profile table"],
    ],
    "view",
  );
  const file = h("input", { type: "text", value: info.dataset, "aria-label": "File name" });
  const delimiter = select([["comma", "comma"], ["tab", "tab"], ["pipe", "pipe |"], ["semicolon", "semicolon ;"]], "comma");
  const header = h("input", { type: "checkbox", checked: true });
  const quoteAll = h("input", { type: "checkbox" });
  const lineEnding = select([["windows", "Windows (CRLF)"], ["unix", "Unix (LF)"]], "windows");
  const bom = h("input", { type: "checkbox" });
  const blank = h("input", { type: "text", value: "", placeholder: "(empty)", "aria-label": "Blank cells as" });
  const neutralize = h("input", { type: "checkbox", checked: true });
  const neutralizeWarning = h("p", { class: "warning-text", hidden: true }, "⚠ Formula-like text may run in Excel.");
  const sheet = h("input", { type: "text", value: "Data", "aria-label": "Sheet name" });
  // Off by default: a longer cell is refused with a message naming it, and this cuts it instead.
  const cutLong = h("input", { type: "checkbox" });
  const provenance = h("input", { type: "checkbox", checked: true });
  // An Excel sheet or Parquet metadata; delimited text has nowhere to put it.
  const provenanceField = h("label", { class: "check" }, provenance, "Include provenance");
  const compression = select([["snappy", "Snappy"], ["zstd", "Zstandard"]], "snappy");
  const boxes = info.columns.map((c) => ({ name: c.name, box: h("input", { type: "checkbox", checked: true }) }));
  const count = h("span", { class: "hint" });
  const syncCount = (): void => { count.textContent = `${boxes.filter((b) => b.box.checked).length} of ${boxes.length} selected`; };
  for (const b of boxes) b.box.addEventListener("change", syncCount);
  syncCount();
  const setAll = (on: boolean): void => { for (const b of boxes) b.box.checked = on; syncCount(); };
  const columnsField = h(
    "fieldset",
    { class: "fieldset" },
    h("legend", {}, "Columns"),
    h("div", { class: "code-toolbar" }, button("Select all columns", () => setAll(true), { class: "small" }), button("Clear", () => setAll(false), { class: "small" }), count),
    h("div", { class: "checklist", role: "group", "aria-label": "Columns to export" }, ...boxes.map((b) => h("label", { class: "check" }, b.box, b.name))),
  );
  const textOptions = h("div", { class: "form-grid" }, field("Delimiter", delimiter), field("Line endings", lineEnding), field("Blank cells as", blank), h("label", { class: "check" }, header, "Header row"), h("label", { class: "check" }, quoteAll, "Quote every cell"), h("label", { class: "check" }, bom, "UTF-8 BOM (for Excel)"), h("label", { class: "check" }, neutralize, "Neutralize formula-like text"));
  const excelOptions = h("div", { class: "form-grid" }, field("Sheet name", sheet), h("label", { class: "check" }, cutLong, "Cut text over 32,767 characters"));
  const parquetOptions = h("div", { class: "form-grid" }, field("Compression", compression));
  const sync = (): void => {
    const f = format.value;
    textOptions.hidden = !(f === "csv" || f === "txt");
    excelOptions.hidden = f !== "xlsx";
    parquetOptions.hidden = f !== "parquet";
    provenanceField.hidden = f !== "xlsx" && f !== "parquet";
    columnsField.hidden = scope.value !== "columns";
    if (f === "txt" && delimiter.value === "comma") delimiter.value = "tab";
    neutralizeWarning.hidden = neutralize.checked;
  };
  for (const el of [format, scope, neutralize]) el.addEventListener("change", sync);
  sync();
  const error = h("p", { class: "status-error", role: "alert" });
  body.append(
    h("fieldset", { class: "fieldset" }, h("legend", {}, "What and where"), h("div", { class: "form-grid" }, field("Rows", scope), field("Format", format), field("File name", file))),
    columnsField,
    h("fieldset", { class: "fieldset", id: "format-options" }, h("legend", {}, "Format options"), textOptions, neutralizeWarning, excelOptions, parquetOptions, provenanceField),
    error,
  );
  const params = (save: boolean): Record<string, string | number | undefined> => ({
    format: format.value,
    scope: scope.value,
    file: file.value.trim(),
    selection: scope.value === "selection" && selected ? JSON.stringify(selected) : undefined,
    columns: scope.value === "columns" ? boxes.filter((b) => b.box.checked).map((b) => b.name).join(",") : undefined,
    delimiter: delimiter.value,
    header: header.checked ? "1" : "0",
    quote_all: quoteAll.checked ? "1" : "0",
    line_ending: lineEnding.value,
    encoding: bom.checked ? "utf-8-sig" : "utf-8",
    blank: blank.value,
    neutralize_formulas: neutralize.checked ? "1" : "0",
    sheet_name: sheet.value,
    allow_cell_truncation: cutLong.checked ? "1" : "0",
    provenance: provenance.checked ? "1" : "0",
    compression: compression.value,
    save: save ? "1" : undefined,
  });
  const exportNow = async (save: boolean): Promise<void> => {
    error.textContent = "";
    try {
      if (save) {
        const result = await api<{ saved: string; message: string }>(`${grid}/export${query(params(true))}`);
        toast(`${result.message} (saved in ${result.saved})`);
      } else {
        const { blob, name, message } = await download(`${grid}/export${query(params(false))}`);
        saveBlob(blob, name);
        toast(message);
      }
      close();
    } catch (e) {
      error.textContent = e instanceof ApiError ? e.message : String(e);
    }
  };
  foot.append(button("Cancel", close), button("Save to export folder", () => void exportNow(true), { icon: "save", title: "~\\.swissarmygrid\\exports" }), button("Download", () => void exportNow(false), { icon: "export", class: "primary" }));
}
