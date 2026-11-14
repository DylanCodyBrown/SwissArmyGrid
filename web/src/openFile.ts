/**
 * openFile.ts: a file as a grid, from the page (the usability test, 2026-11-14: only the agent could). [WIRE]
 *
 *   [OPEN FILE]  the folder button beside "+": the app's file chooser (a real path) -> the file loads
 *   [DROP]       a file dropped anywhere on the page: its bytes (the browser never gives a path)
 *   [SHEETS]     a workbook with several sheets asks which to open, the first ticked; each its own tab
 *
 * The new grid arrives as a tab by itself (the server's grid_shown notice). A table is named after its
 * file (orders.csv -> orders; a sheet: sales_Jan); opening the same file again reloads that table.
 *
 * ROUTING: [OPEN FILE] POST /files/sheets {path}; POST /datasets/{name}/load {path, sheet}
 *          [DROP]      POST /files/dropped-sheets?filename= (bytes); POST /datasets/{name}/drop?filename=&sheet=
 */
import { api, token } from "./api";
import { button, h, openDialog, toast } from "./dom";
import { chooseFile } from "./fileChooser";

/** The kinds a table opens from (adapters/datasets.py SUFFIXES). */
export const TABLE_FILES = [".csv", ".tsv", ".txt", ".parquet", ".json", ".jsonl", ".ndjson", ".arrow", ".feather", ".xlsx"];

/** A file's name as a table's: orders 2026.csv -> orders_2026 (letters, digits, _ and -; 64 at most). */
export function tableName(file: string, sheet?: string): string {
  const stem = (file.split(/[\\/]/).pop() ?? "").replace(/\.[^.]+$/, "");
  const name = [stem, sheet].filter(Boolean).join("_").replace(/[^A-Za-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 64);
  return name || "table";
}

const isWorkbook = (file: string): boolean => file.toLowerCase().endsWith(".xlsx");

/** Which sheets to open: none asked when there is one; else a list, the first ticked. */
export function pickSheets(file: string, sheets: string[]): Promise<string[]> {
  if (sheets.length <= 1) return Promise.resolve(sheets);
  return new Promise((resolve) => {
    const { body, foot, close } = openDialog(`Open ${file.split(/[\\/]/).pop()}`);
    const boxes = sheets.map((sheet, i) => h("input", { type: "checkbox", checked: i === 0, "aria-label": sheet }) as HTMLInputElement);
    body.append(h("div", { class: "sheet-list" }, ...sheets.map((sheet, i) => h("label", { class: "field-flag" }, boxes[i]!, h("span", {}, sheet)))));
    let answered = false;
    const answer = (chosen: string[]): void => {
      answered = true;
      close();
      resolve(chosen);
    };
    foot.append(button("Cancel", () => answer([])), button("Open", () => answer(sheets.filter((_, i) => boxes[i]!.checked)), { class: "primary", icon: "folder" }));
    body.closest("dialog")?.addEventListener("close", () => answered || resolve([]));
    boxes[0]?.focus();
  });
}

/** Open file…: the chooser, then the file (each chosen sheet of a workbook) as a grid. */
export async function openFile(): Promise<void> {
  const path = await chooseFile({ title: "Open file", mode: "file", suffixes: TABLE_FILES });
  if (!path) return;
  try {
    const sheets = isWorkbook(path) ? (await api<{ sheets: string[] }>("/files/sheets", "POST", { path })).sheets : [];
    const chosen = isWorkbook(path) ? await pickSheets(path, sheets) : [null];
    for (const sheet of chosen) {
      const name = tableName(path, sheets.length > 1 && sheet ? sheet : undefined);
      await api(`/datasets/${encodeURIComponent(name)}/load`, "POST", sheet ? { path, sheet } : { path });
    }
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

async function send(url: string, file: File): Promise<Response> {
  const response = await fetch(url, { method: "POST", headers: { Authorization: `Bearer ${token}` }, body: await file.arrayBuffer() });
  if (!response.ok) throw new Error(await response.text());
  return response;
}

/** A file dropped on the page: its bytes, each chosen sheet of a workbook its own grid. */
async function openDropped(file: File): Promise<void> {
  const filename = encodeURIComponent(file.name);
  try {
    const sheets = isWorkbook(file.name) ? ((await (await send(`/files/dropped-sheets?filename=${filename}`, file)).json()) as { sheets: string[] }).sheets : [];
    const chosen = isWorkbook(file.name) ? await pickSheets(file.name, sheets) : [null];
    for (const sheet of chosen) {
      const name = tableName(file.name, sheets.length > 1 && sheet ? sheet : undefined);
      await send(`/datasets/${encodeURIComponent(name)}/drop?filename=${filename}${sheet ? `&sheet=${encodeURIComponent(sheet)}` : ""}`, file);
    }
  } catch (e) {
    toast(`${file.name}: ${(e as Error).message}`, "error");
  }
}

/** The folder button beside "+". */
export function openFileButton(): HTMLButtonElement {
  return button("Open file…", () => void openFile(), { icon: "folder", iconOnly: true, class: "ghost tab-new" });
}

/** Files dropped anywhere on the page open as grids (a tab dragged between tabs is not a file). */
export function mountFileDrop(): void {
  const hasFiles = (e: DragEvent): boolean => Array.from(e.dataTransfer?.types ?? []).includes("Files");
  document.addEventListener("dragover", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    document.body.classList.add("dropping-file");
  });
  document.addEventListener("dragleave", (e) => {
    if (!e.relatedTarget) document.body.classList.remove("dropping-file");
  });
  document.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    document.body.classList.remove("dropping-file");
    for (const file of Array.from(e.dataTransfer?.files ?? [])) void openDropped(file);
  });
}
