/**
 * catalogPage.ts: the blades and catalog page, in its own tab (plan decisions 14-18). [COMPOSITION]
 *
 * The left rail is the navigation that will sit beside the notebook: Blades (saved code that
 * reaches outside Python to load something) above the Catalog (a source database's names and types,
 * as a tree: schema -> table or view -> column and its type, searchable). The main area shows a
 * table's columns with a first SELECT, or a blade's save screen.
 *
 * Dragging works everywhere text can be dropped: a blade gives its `sag.blade(...)` line, a table its
 * quoted name, a column its quoted name. The tree loads lazily, so a catalog of half a million columns
 * opens at once.
 */
import { api, connect, onNotice, openedWith, query } from "./api";
import { type BladeKind, type BladeSummary, bladeLine, editBlade, kindLabel, runBlade, showTemplatePicker } from "./bladeEditor";
import { button, clear, formatCount, h, recall, remember, toast } from "./dom";

// ===== shapes the server sends (server/routes_catalog.py) =====

interface CatalogInfo {
  name: string;
  dialect: string;
  source: "load" | "blade";
  loaded_at: number;
  databases: number;
  schemas: number;
  tables: number;
  views: number;
  columns: number;
}

interface SchemaRow {
  database: string;
  schema: string;
  tables: number;
  views: number;
}

interface TableRow {
  database: string;
  schema: string;
  table: string;
  kind: "table" | "view";
  columns: number;
}

interface ColumnRow {
  column: string;
  type: string;
  nullable: boolean | null;
  position: number | null;
  description: string | null;
}

interface ColumnHit {
  database: string;
  schema: string;
  table: string;
  kind: "table" | "view";
  column: string;
  type: string;
}

interface SearchResult {
  tables: TableRow[];
  columns: ColumnHit[];
  total_tables: number;
  total_columns: number;
}

// ===== theme: the same choice as the grid page, without loading the grid library =====

function applyTheme(): void {
  const choice = recall("theme") ?? "system";
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const more = matchMedia("(prefers-contrast: more)").matches;
  const resolved = choice !== "system" ? choice : more ? (dark ? "hc-dark" : "hc-light") : dark ? "dark" : "light";
  document.documentElement.dataset.theme = resolved;
  document.documentElement.style.setProperty("--ui-zoom", recall("zoom") ?? "1");
}
applyTheme();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
window.addEventListener("storage", (event) => {
  if (event.key === "swissarmygrid.theme" || event.key === "swissarmygrid.zoom") applyTheme();
});

// ===== state =====

const bladesHost = document.getElementById("blades") as HTMLElement;
const catalogHost = document.getElementById("catalog") as HTMLElement;
const detail = document.getElementById("detail") as HTMLElement;
const liveNote = document.querySelector<HTMLElement>(".cat-live");

let blades: BladeSummary[] = [];
let catalogs: CatalogInfo[] = [];
let current: CatalogInfo | null = null;
let editing: string | null = null; // the blade on the save screen
let selectedBlade: string | null = null;

const catalogPath = (name: string): string => `/catalogs/${encodeURIComponent(name)}`;

// ===== quoting for the source's dialect (as engine/catalog.py) =====

function quoteName(name: string, dialect: string): string {
  if (dialect === "sqlserver") return `[${name.replaceAll("]", "]]")}]`;
  if (dialect === "mysql") return `\`${name.replaceAll("`", "``")}\``;
  return `"${name.replaceAll('"', '""')}"`;
}

function qualified(row: { database: string; schema: string; table: string }): string {
  const dialect = current?.dialect ?? "other";
  const parts = [current && current.databases > 1 ? row.database : "", row.schema, row.table].filter(Boolean);
  return parts.map((p) => quoteName(p, dialect)).join(".");
}

function copy(text: string, what: string): void {
  void navigator.clipboard.writeText(text).then(
    () => toast(`Copied ${what}.`),
    () => toast("The browser did not allow copying.", "error"),
  );
}

// ===== blades =====

async function loadBlades(): Promise<void> {
  try {
    blades = (await api<{ blades: BladeSummary[] }>("/blades")).blades;
  } catch (error) {
    toast((error as Error).message, "error");
    blades = [];
  }
  renderBlades();
}

function renderBlades(): void {
  const list = h("ul", { class: "blade-list", "aria-label": "Saved blades" });
  for (const blade of blades) {
    const line = bladeLine(blade);
    const item = h(
      "li",
      {
        class: "blade-item",
        draggable: "true",
        "aria-current": blade.name === selectedBlade ? "true" : undefined,
        title: `Drag into a script: ${line}`,
        ondragstart: (event: DragEvent) => {
          event.dataTransfer?.setData("text/plain", line);
          event.dataTransfer?.setData("application/x-sag-blade", JSON.stringify({ name: blade.name, kind: blade.kind, line }));
        },
      },
      h("span", { class: `blade-kind ${blade.kind}` }, kindLabel(blade.kind)),
      h("button", { class: "blade-name", type: "button", onclick: () => openBlade(blade.name) }, blade.name),
      blade.has_catalog
        ? button("Refresh catalog", () => void refresh(blade.name), { icon: "play", iconOnly: true, class: "ghost", title: `Refresh ${blade.name}'s catalog` })
        : null,
      button("Copy the sag.blade line", () => copy(line, "the line"), { icon: "copy", iconOnly: true, class: "ghost", title: line }),
    );
    list.append(item);
  }
  clear(
    bladesHost,
    h(
      "div",
      { class: "rail-head" },
      h("h2", { id: "blades-title" }, "Blades"),
      button("New blade", () => void newBlade(), { icon: "plus", class: "ghost" }),
    ),
    blades.length ? list : h("p", { class: "empty" }, "No blades yet. Any code that loads from outside Python can be one."),
  );
}

async function newBlade(): Promise<void> {
  editing = null;
  selectedBlade = null;
  renderBlades();
  await showTemplatePicker(detail, (template, kind) => {
    editing = "";
    void editBlade(detail, null, template, kind, afterSave, showWelcome);
  });
}

function openBlade(name: string): void {
  editing = name;
  selectedBlade = name;
  renderBlades();
  void editBlade(detail, name, null, "other" as BladeKind, afterSave, () => {
    editing = null;
    selectedBlade = null;
    renderBlades();
    showWelcome();
  });
}

function afterSave(name: string): void {
  editing = null; // saved: nothing to lose, so a click in the tree shows what was clicked again
  selectedBlade = name;
  void loadBlades();
}

async function refresh(name: string): Promise<void> {
  toast(`Refreshing ${name}...`);
  const answer = await runBlade<{ catalog: CatalogInfo }>(name, "refresh");
  if (!answer) return;
  toast(`${name}: ${formatCount(answer.catalog.tables)} tables, ${formatCount(answer.catalog.columns)} columns.`);
  await loadCatalogs(answer.catalog.name);
}

// ===== catalogs: picker and search =====

const picker = h("select", { "aria-label": "Catalog" });
const search = h("input", {
  class: "cat-search",
  type: "search",
  placeholder: "Search tables and columns  (try sales.orders)",
  "aria-label": "Search the catalog",
  spellcheck: "false",
});
const tree = h("ul", { class: "cat-tree", role: "tree", "aria-label": "Catalog tree", tabindex: "0" });
const results = h("div", { class: "cat-tree", hidden: true, role: "region", "aria-label": "Search results" });
const catalogActions = h("span", { class: "row-actions" });

async function loadCatalogs(select?: string): Promise<void> {
  try {
    catalogs = (await api<{ catalogs: CatalogInfo[] }>("/catalogs")).catalogs;
  } catch (error) {
    toast((error as Error).message, "error");
    catalogs = [];
  }
  const wanted = select ?? current?.name ?? recall("catalog");
  current = catalogs.find((c) => c.name.toLowerCase() === (wanted ?? "").toLowerCase()) ?? catalogs[0] ?? null;
  renderPicker();
  await showCatalog();
}

function renderPicker(): void {
  clear(picker);
  for (const c of catalogs) picker.append(h("option", { value: c.name, selected: c === current }, c.name));
  picker.disabled = catalogs.length === 0;
  const blade = current ? blades.find((b) => b.name.toLowerCase() === current?.name.toLowerCase() && b.has_catalog) : undefined;
  clear(
    catalogActions,
    blade ? button("Refresh", () => void refresh(blade.name), { icon: "play", iconOnly: true, class: "ghost", title: `Refresh ${blade.name}` }) : null,
    current
      ? button("Remove this catalog", () => {
          const target = current;
          if (!target) return;
          void api(catalogPath(target.name), "DELETE").then(() => {
            toast(`Removed the ${target.name} catalog.`);
            current = null;
            void loadCatalogs();
          }, (error: Error) => toast(error.message, "error"));
        }, { icon: "trash", iconOnly: true, class: "ghost" })
      : null,
  );
}

picker.addEventListener("change", () => {
  current = catalogs.find((c) => c.name === picker.value) ?? null;
  if (current) remember("catalog", current.name);
  renderPicker();
  void showCatalog();
});

function renderCatalogSection(): void {
  clear(
    catalogHost,
    h("div", { class: "rail-head" }, h("h2", { id: "catalog-title" }, "Catalog")),
    h("div", { class: "catalog-picker" }, picker, catalogActions),
    search,
    tree,
    results,
  );
}

// ===== the tree =====

type NodeKind = "schema" | "table" | "column" | "more";

interface NodeData {
  kind: NodeKind;
  database: string;
  schema: string;
  table?: string;
  tableKind?: "table" | "view";
  column?: string;
  type?: string;
  offset?: number;
  loaded?: boolean;
}

const nodes = new WeakMap<HTMLElement, NodeData>();

function treeItem(data: NodeData, depth: number, label: string, meta: string, expandable: boolean): HTMLElement {
  const item = h("li", { role: "treeitem", "aria-level": String(depth + 1), "aria-selected": "false", tabindex: "-1" });
  if (expandable) item.setAttribute("aria-expanded", "false");
  const node = h(
    "div",
    { class: `node ${data.kind === "more" ? "more" : ""}` },
    h("span", { class: "twisty", "aria-hidden": "true" }, expandable ? "▸" : ""),
    h("span", { class: "node-label" }, label),
    data.kind === "table" && data.tableKind === "view" ? h("span", { class: "node-kind" }, "view") : null,
    meta ? h("span", { class: "node-meta" }, meta) : null,
  );
  node.style.setProperty("--depth", String(depth)); // a custom property needs setProperty, not assignment
  item.append(node);
  if (data.kind === "table" || data.kind === "column") {
    item.draggable = true;
    item.addEventListener("dragstart", (event) => {
      event.stopPropagation();
      event.dataTransfer?.setData("text/plain", dragText(data));
    });
  }
  nodes.set(item, data);
  return item;
}

function dragText(data: NodeData): string {
  if (data.kind === "column" && data.column) return quoteName(data.column, current?.dialect ?? "other");
  return qualified({ database: data.database, schema: data.schema, table: data.table ?? "" });
}

function schemaLabel(row: { database: string; schema: string }): string {
  const schema = row.schema || "(no schema)";
  return current && current.databases > 1 && row.database ? `${row.database}.${schema}` : schema;
}

async function showCatalog(): Promise<void> {
  clear(tree);
  search.value = "";
  results.hidden = true;
  tree.hidden = false;
  if (!current) {
    tree.append(
      h(
        "li",
        { class: "empty" },
        "No catalog loaded. Refresh a blade that has a catalog command, or run sag.load(frame, name=...) in Python.",
      ),
    );
    return;
  }
  remember("catalog", current.name);
  let schemas: SchemaRow[];
  try {
    schemas = (await api<{ schemas: SchemaRow[] }>(`${catalogPath(current.name)}/schemas`)).schemas;
  } catch (error) {
    toast((error as Error).message, "error");
    return;
  }
  for (const row of schemas) {
    const count = row.views ? `${formatCount(row.tables)} + ${formatCount(row.views)} views` : formatCount(row.tables);
    tree.append(treeItem({ kind: "schema", database: row.database, schema: row.schema }, 0, schemaLabel(row), count, true));
  }
  const first = tree.querySelector<HTMLElement>('[role="treeitem"]');
  if (first) select(first, false);
  if (schemas.length === 1 && first) await expand(first);
}

function group(item: HTMLElement): HTMLElement {
  let list = item.querySelector<HTMLElement>(":scope > ul");
  if (!list) {
    list = h("ul", { role: "group" });
    item.append(list);
  }
  return list;
}

async function loadChildren(item: HTMLElement, data: NodeData, offset = 0): Promise<void> {
  if (!current) return;
  const list = group(item);
  if (data.kind === "schema") {
    const page = await api<{ total: number; tables: TableRow[] }>(
      `${catalogPath(current.name)}/tables${query({ schema: data.schema, database: current.databases > 1 ? data.database : undefined, offset, limit: 500 })}`,
    );
    for (const t of page.tables) {
      list.append(
        treeItem({ kind: "table", database: t.database, schema: data.schema, table: t.table, tableKind: t.kind }, 1, t.table, formatCount(t.columns), true),
      );
    }
    const shown = offset + page.tables.length;
    if (shown < page.total) {
      list.append(treeItem({ kind: "more", database: data.database, schema: data.schema, offset: shown }, 1, `Show more (${formatCount(page.total - shown)} left)`, "", false));
    }
  } else if (data.kind === "table" && data.table) {
    const columns = await tableColumns(data);
    for (const c of columns) {
      list.append(treeItem({ kind: "column", database: data.database, schema: data.schema, table: data.table, column: c.column, type: c.type }, 2, c.column, c.type, false));
    }
  }
  data.loaded = true;
}

async function tableColumns(data: { database: string; schema: string; table?: string }): Promise<ColumnRow[]> {
  if (!current || !data.table) return [];
  const answer = await api<{ columns: ColumnRow[] }>(
    `${catalogPath(current.name)}/columns${query({ schema: data.schema, table: data.table, database: current.databases > 1 ? data.database : undefined })}`,
  );
  return answer.columns;
}

async function expand(item: HTMLElement): Promise<void> {
  const data = nodes.get(item);
  if (!data || item.getAttribute("aria-expanded") === null) return;
  if (!data.loaded) {
    try {
      await loadChildren(item, data);
    } catch (error) {
      toast((error as Error).message, "error");
      return;
    }
  }
  item.setAttribute("aria-expanded", "true");
  group(item).hidden = false;
  const twisty = item.querySelector<HTMLElement>(":scope > .node > .twisty");
  if (twisty) twisty.textContent = "▾";
}

function collapse(item: HTMLElement): void {
  if (item.getAttribute("aria-expanded") !== "true") return;
  item.setAttribute("aria-expanded", "false");
  group(item).hidden = true;
  const twisty = item.querySelector<HTMLElement>(":scope > .node > .twisty");
  if (twisty) twisty.textContent = "▸";
}

function select(item: HTMLElement, show = true): void {
  for (const old of tree.querySelectorAll<HTMLElement>('[aria-selected="true"]')) {
    old.setAttribute("aria-selected", "false");
    old.tabIndex = -1;
  }
  item.setAttribute("aria-selected", "true");
  item.tabIndex = 0;
  item.querySelector(".node")?.scrollIntoView({ block: "nearest" });
  if (show) void showNode(item);
}

/** Every item a person can see now (not inside a collapsed parent), in order. */
function visibleItems(): HTMLElement[] {
  return [...tree.querySelectorAll<HTMLElement>('[role="treeitem"]')].filter((item) => {
    let parent = item.parentElement?.closest<HTMLElement>('[role="treeitem"]');
    while (parent) {
      if (parent.getAttribute("aria-expanded") !== "true") return false;
      parent = parent.parentElement?.closest<HTMLElement>('[role="treeitem"]');
    }
    return true;
  });
}

async function activate(item: HTMLElement): Promise<void> {
  const data = nodes.get(item);
  if (!data) return;
  if (data.kind === "more") {
    const parent = item.parentElement?.closest<HTMLElement>('[role="treeitem"]');
    const parentData = parent ? nodes.get(parent) : undefined;
    item.remove();
    if (parent && parentData) await loadChildren(parent, parentData, data.offset ?? 0);
    return;
  }
  if (item.getAttribute("aria-expanded") === "true") collapse(item);
  else await expand(item);
}

tree.addEventListener("click", (event) => {
  const item = (event.target as HTMLElement).closest<HTMLElement>('[role="treeitem"]');
  if (!item) return;
  select(item);
  void activate(item);
});

tree.addEventListener("keydown", (event) => {
  const items = visibleItems();
  const at = items.findIndex((i) => i.getAttribute("aria-selected") === "true");
  const item = items[at];
  const move = (to: HTMLElement | undefined): void => {
    if (!to) return;
    event.preventDefault();
    select(to);
  };
  switch (event.key) {
    case "ArrowDown":
      move(items[at + 1]);
      break;
    case "ArrowUp":
      if (at <= 0) {
        event.preventDefault();
        search.focus();
      } else move(items[at - 1]);
      break;
    case "Home":
      move(items[0]);
      break;
    case "End":
      move(items[items.length - 1]);
      break;
    case "ArrowRight":
      if (!item) break;
      event.preventDefault();
      if (item.getAttribute("aria-expanded") === "false") void expand(item);
      else if (item.getAttribute("aria-expanded") === "true") move(items[at + 1]);
      break;
    case "ArrowLeft":
      if (!item) break;
      event.preventDefault();
      if (item.getAttribute("aria-expanded") === "true") collapse(item);
      else move(item.parentElement?.closest<HTMLElement>('[role="treeitem"]') ?? undefined);
      break;
    case "Enter":
    case " ":
      if (!item) break;
      event.preventDefault();
      void activate(item);
      break;
    default:
      // Typing a letter starts a search, like a file tree.
      if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
        search.focus();
      }
  }
});

// ===== details: a table's columns and a first SELECT =====

async function showNode(item: HTMLElement): Promise<void> {
  const data = nodes.get(item);
  if (!data || !current) return;
  if (editing !== null) {
    toast("Save or cancel the blade on the right first.");
    return;
  }
  if (data.kind === "table" || data.kind === "column") await showTable(data, data.column);
  if (data.kind === "schema") showSchema(data);
}

function showSchema(data: NodeData): void {
  if (!current) return;
  clear(
    detail,
    h("h2", {}, schemaLabel(data)),
    h("p", { class: "hint" }, `A schema in ${current.name}. Open it to see its tables and views.`),
  );
}

async function showTable(data: NodeData, focusColumn?: string): Promise<void> {
  if (!current || !data.table) return;
  let answer: { columns: ColumnRow[]; select: string; dialect: string };
  try {
    answer = await api(
      `${catalogPath(current.name)}/columns${query({ schema: data.schema, table: data.table, database: current.databases > 1 ? data.database : undefined })}`,
    );
  } catch (error) {
    toast((error as Error).message, "error");
    return;
  }
  const name = qualified({ database: data.database, schema: data.schema, table: data.table });
  const rows = answer.columns.map((c) =>
    h(
      "tr",
      { "aria-current": c.column === focusColumn ? "true" : undefined, style: c.column === focusColumn ? { background: "var(--selected)" } : undefined },
      h("td", {}, c.column),
      h("td", {}, c.type),
      h("td", {}, c.nullable === null ? "" : c.nullable ? "yes" : "no"),
      h("td", { class: "hint" }, c.description ?? ""),
    ),
  );
  clear(
    detail,
    h("h2", {}, data.table, data.tableKind === "view" ? h("span", { class: "hint" }, "  view") : null),
    h("p", { class: "hint" }, `${schemaLabel(data)} in ${current.name} · ${answer.columns.length} columns · ${answer.dialect}`),
    h(
      "div",
      { class: "row-actions" },
      button("Copy the name", () => copy(name, "the name"), { icon: "copy" }),
      button("Copy the SELECT", () => copy(answer.select, "the SELECT"), { icon: "sql" }),
    ),
    h("pre", { class: "code-box", "aria-label": "A first SELECT" }, answer.select),
    h(
      "table",
      { class: "columns-table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Column"), h("th", {}, "Type"), h("th", {}, "Nullable"), h("th", {}, "Description"))),
      h("tbody", {}, ...rows),
    ),
  );
  detail.querySelector('[aria-current="true"]')?.scrollIntoView({ block: "nearest" });
}

function showWelcome(): void {
  editing = null;
  clear(
    detail,
    h("h2", {}, "Blades and the catalog"),
    h(
      "p",
      {},
      "A blade is any Python that reaches outside Python to load something: a database connection, a CSV or workbook, an API, a vault. Save it once here, then use it anywhere:",
    ),
    h("pre", { class: "code-box" }, 'import swissarmygrid as sag\n\nconn = sag.blade("Warehouse")'),
    h(
      "p",
      {},
      "A blade can add its source to the catalog: every schema, table and column with its type, searchable here and readable by your agent, so it can write SQL against the source. Or load one straight from Python:",
    ),
    h("pre", { class: "code-box" }, 'sag.load(conn.execute("SELECT * FROM INFORMATION_SCHEMA.COLUMNS").df(), name="Warehouse", dialect="sqlserver")'),
    h("div", { class: "row-actions" }, button("New blade", () => void newBlade(), { icon: "plus", class: "primary" })),
  );
}

// ===== search =====

let searchTimer = 0;
let searchSerial = 0;

search.addEventListener("input", () => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => void runSearch(), 150);
});

search.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    search.value = "";
    void runSearch();
  } else if (event.key === "ArrowDown") {
    event.preventDefault();
    const first = results.hidden ? visibleItems()[0] : results.querySelector<HTMLElement>("button");
    if (first && results.hidden) {
      select(first);
      first.focus();
    } else first?.focus();
  } else if (event.key === "Enter") {
    results.querySelector<HTMLElement>("button")?.click();
  }
});

results.addEventListener("keydown", (event) => {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  const buttons = [...results.querySelectorAll<HTMLElement>("button")];
  const at = buttons.indexOf(document.activeElement as HTMLElement);
  const next = buttons[at + (event.key === "ArrowDown" ? 1 : -1)];
  event.preventDefault();
  if (next) next.focus();
  else if (event.key === "ArrowUp") search.focus();
});

async function runSearch(): Promise<void> {
  const text = search.value.trim();
  const serial = ++searchSerial;
  if (!text || !current) {
    results.hidden = true;
    tree.hidden = false;
    return;
  }
  let found: SearchResult;
  try {
    found = await api<SearchResult>(`${catalogPath(current.name)}/search${query({ q: text, limit: 50 })}`);
  } catch (error) {
    toast((error as Error).message, "error");
    return;
  }
  if (serial !== searchSerial) return; // a newer search is on its way
  const hit = (label: string, meta: string, onPick: () => void, drag: string): HTMLElement =>
    h(
      "button",
      {
        type: "button",
        class: "node",
        style: { width: "100%", border: "0", background: "none", color: "inherit", font: "inherit", textAlign: "left" },
        draggable: "true",
        onclick: onPick,
        ondragstart: (event: DragEvent) => event.dataTransfer?.setData("text/plain", drag),
      },
      h("span", { class: "node-label" }, label),
      h("span", { class: "node-meta" }, meta),
    );
  const dialect = current.dialect;
  clear(
    results,
    h("div", { class: "result-group" }, `Tables (${formatCount(found.total_tables)})`),
    ...found.tables.map((t) =>
      hit(`${t.schema ? t.schema + "." : ""}${t.table}`, t.kind === "view" ? "view" : formatCount(t.columns), () => void reveal(t.database, t.schema, t.table), qualified(t)),
    ),
    h("div", { class: "result-group" }, `Columns (${formatCount(found.total_columns)})`),
    ...found.columns.map((c) =>
      hit(`${c.table}.${c.column}`, c.type, () => void reveal(c.database, c.schema, c.table, c.column), quoteName(c.column, dialect)),
    ),
    found.total_tables + found.total_columns === 0 ? h("p", { class: "empty" }, "Nothing matches.") : null,
  );
  tree.hidden = true;
  results.hidden = false;
}

/** Open the tree at a table (and column) found by search, and show it. */
async function reveal(database: string, schema: string, table: string, column?: string): Promise<void> {
  if (editing !== null) {
    toast("Save or cancel the blade on the right first.");
    return;
  }
  search.value = "";
  results.hidden = true;
  tree.hidden = false;
  const find = (parent: ParentNode, match: (d: NodeData) => boolean): HTMLElement | undefined =>
    [...parent.querySelectorAll<HTMLElement>(':scope > [role="treeitem"], :scope > ul > [role="treeitem"]')].find((i) => {
      const d = nodes.get(i);
      return d !== undefined && match(d);
    });
  const schemaItem = find(tree, (d) => d.kind === "schema" && d.schema === schema && (!current || current.databases < 2 || d.database === database));
  if (!schemaItem) return;
  await expand(schemaItem);
  let tableItem = find(group(schemaItem), (d) => d.kind === "table" && d.table === table);
  // A table past the first page: keep showing more until it appears.
  while (!tableItem) {
    const more = find(group(schemaItem), (d) => d.kind === "more");
    if (!more) break;
    await activate(more);
    tableItem = find(group(schemaItem), (d) => d.kind === "table" && d.table === table);
  }
  if (!tableItem) return;
  await expand(tableItem);
  const target = column ? (find(group(tableItem), (d) => d.kind === "column" && d.column === column) ?? tableItem) : tableItem;
  select(target, false);
  tree.focus();
  const data = nodes.get(tableItem);
  if (data) await showTable(data, column);
}

// ===== start =====

renderBlades();
renderCatalogSection();
showWelcome();
void loadBlades().then(() => loadCatalogs(openedWith.get("catalog") ?? undefined));
connect((live) => {
  if (liveNote) liveNote.textContent = live ? "" : "Not connected to the server";
}, { catalog: "1" });
onNotice((notice) => {
  if (notice.type === "catalogs_changed") void loadCatalogs();
  if (notice.type === "blades_changed") void loadBlades().then(renderPicker);
});
