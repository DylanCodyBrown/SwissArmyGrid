/**
 * workbenchCompletion.ts: what the Workbench's editors offer as you type. [FUNCTION]
 *
 *   PYTHON     the document's kernel (IPython's completer, with Jedi): your variables, their attributes,
 *              a DataFrame's columns, modules; hover shows a signature and its help. Inside
 *              sag.connection("…") or sag.blade("…"): your connection and blade names
 *   SQL CELL   the data explorer's catalog of the cell's connection (every catalog when it has none):
 *              schemas, tables, views and columns, `orders.` gives orders' columns; the kernel's names
 *              too once it runs (a file connection's own tables); SQL's words
 *
 * The catalog answers are kept in the page (by what was asked), so typing on asks the server little.
 * Before the kernel starts nothing runs on the user's computer to complete: the catalog still answers.
 *
 * ROUTING: GET /catalogs, /catalogs/{name}/search, /catalogs/{name}/columns; POST /workbench/kernels/{id}/complete
 */
import { api } from "./api";
import type { Suggestion } from "./lazy/codeEditor";
import { sqlLine } from "./workbenchNotebook";
import type { Kernel } from "./workbenchKernel";

interface Found {
  tables: { schema: string | null; table: string; kind: string; columns: number }[];
  columns: { schema: string | null; table: string; column: string; type: string }[];
}

const SQL_WORDS = [
  "SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "HAVING", "JOIN", "LEFT JOIN", "INNER JOIN", "ON", "AS",
  "DISTINCT", "COUNT", "SUM", "AVG", "MIN", "MAX", "CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR", "NOT",
  "IN", "IS NULL", "IS NOT NULL", "LIKE", "BETWEEN", "LIMIT", "TOP", "WITH", "UNION ALL", "INSERT INTO", "UPDATE",
  "DELETE", "CAST", "COALESCE", "DESC", "ASC",
];

let catalogList: Promise<string[]> | null = null;
let catalogsAt = 0;
const searches = new Map<string, Promise<Found>>();
const tableColumns = new Map<string, Promise<{ column: string; type: string }[]>>();

/** Forget what was kept: a catalog was refreshed or loaded. */
export function forgetCatalogs(): void {
  catalogList = null;
  searches.clear();
  tableColumns.clear();
}

function catalogs(): Promise<string[]> {
  if (!catalogList || Date.now() - catalogsAt > 30_000) {
    catalogsAt = Date.now();
    catalogList = api<{ catalogs: { name: string }[] }>("/catalogs").then((r) => r.catalogs.map((c) => c.name), () => []);
  }
  return catalogList;
}

function search(catalog: string, text: string): Promise<Found> {
  const key = `${catalog}\u0000${text.toLowerCase()}`;
  let found = searches.get(key);
  if (!found) {
    found = api<Found>(`/catalogs/${encodeURIComponent(catalog)}/search?q=${encodeURIComponent(text)}&limit=200`).catch(() => ({ tables: [], columns: [] }));
    searches.set(key, found);
    if (searches.size > 500) searches.delete(searches.keys().next().value!);
  }
  return found;
}

function columnsOf(catalog: string, schema: string | null, table: string): Promise<{ column: string; type: string }[]> {
  const key = `${catalog}\u0000${schema ?? ""}\u0000${table}`;
  let found = tableColumns.get(key);
  if (!found) {
    const query = new URLSearchParams({ table, ...(schema ? { schema } : {}) });
    found = api<{ columns: { column: string; type: string }[] }>(`/catalogs/${encodeURIComponent(catalog)}/columns?${query}`).then((r) => r.columns, () => []);
    tableColumns.set(key, found);
  }
  return found;
}

/** The SQL names at the cursor from the connection's catalog (all catalogs when it has none). */
async function catalogSuggestions(token: string, connection: string | null): Promise<Suggestion[]> {
  const names = await catalogs();
  const own = names.filter((c) => connection && c.toLowerCase() === connection.toLowerCase());
  const asked = own.length ? own : names.slice(0, 4);
  const out: Suggestion[] = [];
  const dot = token.lastIndexOf(".");
  for (const catalog of asked) {
    if (dot >= 0) {
      // orders.cu: the columns of orders; sales.or: the tables of the schema sales
      const owner = token.slice(0, dot).split(".").pop()!.replace(/"/g, "");
      const start = token.slice(dot + 1).replace(/"/g, "").toLowerCase();
      const found = await search(catalog, owner);
      for (const t of found.tables.filter((t) => t.table.toLowerCase() === owner.toLowerCase())) {
        for (const c of await columnsOf(catalog, t.schema, t.table)) {
          if (c.column.toLowerCase().startsWith(start)) out.push({ label: c.column, kind: "column", detail: `${t.table} · ${c.type}` });
        }
      }
      for (const t of found.tables.filter((t) => (t.schema ?? "").toLowerCase() === owner.toLowerCase() && t.table.toLowerCase().startsWith(start))) {
        out.push({ label: t.table, kind: t.kind === "view" ? "view" : "table", detail: `${t.schema} · ${t.columns} columns` });
      }
      continue;
    }
    if (!token) continue;
    const found = await search(catalog, token);
    const lower = token.toLowerCase();
    for (const t of found.tables) if (t.table.toLowerCase().startsWith(lower)) out.push({ label: t.table, kind: t.kind === "view" ? "view" : "table", detail: `${t.schema ? `${t.schema} · ` : ""}${t.columns} columns · ${catalog}` });
    for (const t of found.tables) if (t.schema && t.schema.toLowerCase().startsWith(lower)) out.push({ label: t.schema, kind: "schema", detail: catalog });
    for (const c of found.columns) if (c.column.toLowerCase().startsWith(lower)) out.push({ label: c.column, kind: "column", detail: `${c.table} · ${c.type}` });
  }
  const seen = new Set<string>();
  return out.filter((s) => (seen.has(`${s.kind}:${s.label}`) ? false : (seen.add(`${s.kind}:${s.label}`), true)));
}

async function kernelSuggestions(kernel: Kernel, code: string, offset: number, shift = 0): Promise<Suggestion[]> {
  const found = await kernel.complete(code, offset);
  if (!found) return [];
  return found.matches.map((m) => {
    const kind = found.types[m];
    return { label: m.split(".").pop() || m, insert: m, kind: kind && kind !== "<unknown>" ? kind : "statement", detail: kind && kind !== "<unknown>" ? kind : "", start: found.start - shift, end: found.end - shift };
  });
}

/** Python: the kernel's completions, every connection by its name (there before it is first used, so
 *  the kernel does not know it yet), and names inside sag.connection("…"). */
export function pythonCompleter(kernel: Kernel, names: () => { connections: string[]; blades: string[]; codeNames?: { name: string; connection: string; kind: string }[] }) {
  return async (text: string, offset: number): Promise<Suggestion[]> => {
    const before = text.slice(Math.max(0, offset - 200), offset);
    const quoted = /sag\.(connection|blade|sql)\(\s*["']([^"']*)$/.exec(before);
    if (quoted) {
      const wanted = quoted[1] === "blade" ? names().blades : names().connections;
      const typed = quoted[2]!.toLowerCase();
      return wanted.filter((n) => n.toLowerCase().startsWith(typed)).map((n) => ({ label: n, kind: "connection", detail: quoted[1] === "blade" ? "blade" : "connection", start: offset - quoted[2]!.length, end: offset }));
    }
    // A quote just typed opens a string: nothing to offer yet (IPython would list its 142 magics, and
    // Enter would put `%%!` in the code). Inside a string, its file paths come once a letter follows.
    if (/(^|[^\w])["']$/.test(before)) return [];
    // The cursor's line first: the kernel then completes against the live objects (a DataFrame's columns).
    // Sent the whole cell, Jedi reads `rainfall = pd.DataFrame(...)` above it as code and misses them.
    const word = /(^|[^\w.])([A-Za-z_]\w*)$/.exec(before)?.[2] ?? "";
    const connected = word
      ? (names().codeNames ?? []).filter((c) => c.name.startsWith(word.toLowerCase())).map((c) => ({ label: c.name, kind: "connection", detail: `${c.connection} (${c.kind === "file" ? "a file" : "a database"} connection)`, start: offset - word.length, end: offset }))
      : [];
    const lineStart = text.lastIndexOf("\n", offset - 1) + 1;
    const lineEnd = text.indexOf("\n", offset);
    const line = text.slice(lineStart, lineEnd < 0 ? undefined : lineEnd);
    const live = await kernelSuggestions(kernel, line, offset - lineStart, -lineStart);
    const fromKernel = live.length || lineStart === 0 ? live : await kernelSuggestions(kernel, text, offset);
    // Magics (%timeit, %%sql) only once a % is typed.
    const magic = /%\w*$/.test(before);
    const offered = fromKernel.filter((s) => magic || !(s.insert ?? s.label).startsWith("%"));
    return [...connected, ...offered.filter((s) => !connected.some((c) => c.label === s.label))];
  };
}

/** A SQL cell: its connection's catalog, the kernel's names (as the %%sql cell it runs as), SQL's words. */
export function sqlCompleter(kernel: Kernel, connection: () => string | null) {
  return async (text: string, offset: number): Promise<Suggestion[]> => {
    const token = /[\w."]*$/.exec(text.slice(0, offset))![0];
    const header = `${sqlLine(connection())}\n`;
    const [fromCatalog, fromKernel] = await Promise.all([
      catalogSuggestions(token, connection()),
      kernelSuggestions(kernel, header + text, offset + header.length, header.length).catch(() => []),
    ]);
    const words = token.includes(".") ? [] : SQL_WORDS.filter((w) => w.toLowerCase().startsWith(token.toLowerCase()) && token).map((w) => ({ label: w, kind: "keyword" }));
    const seen = new Set(fromCatalog.map((s) => s.label.toLowerCase()));
    return [...fromCatalog, ...fromKernel.filter((s) => !seen.has(s.label.toLowerCase())), ...words];
  };
}
