/**
 * catalogRail.ts: the blades and catalog navigation of the blades page. [VIEW]
 *
 * Blades (saved code that reaches outside Python to load something) above the Catalog (a source
 * database's names and types, as a tree: schema -> table or view -> column and its type, searchable).
 * The page that mounts it decides where a table's details and the blade editor show. A drag gives
 * plain text: a name, a query, or a blade's sag.blade line, to drop into Jupyter or an editor.
 *
 * The tree loads lazily, so a catalog of half a million columns opens at once.
 */
import { api, query } from "./api";
import { type BladeSummary, bladeLine, kindLabel, runBlade } from "./bladeEditor";
import { openConnections } from "./connectionsWindow";
import { chooseBladeFiles, editBladeFolders, exportBlades, importBlades, openPacks } from "./bladeLibrary";
import { button, clear, formatCount, h, recall, remember, showMenu, toast } from "./dom";
import { toPython } from "./toPython";

// ===== shapes the server sends (server/routes_catalog.py) =====

export interface CatalogInfo {
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

export interface ColumnRow {
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

export function applyTheme(): void {
  const choice = recall("theme") ?? "system";
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const more = matchMedia("(prefers-contrast: more)").matches;
  const resolved = choice !== "system" ? choice : more ? (dark ? "hc-dark" : "hc-light") : dark ? "dark" : "light";
  document.documentElement.dataset.theme = resolved;
  document.documentElement.style.setProperty("--ui-zoom", recall("zoom") ?? "1");
}
/** Follow the grid page's theme and zoom, now and as they change in another tab. */
export function watchTheme(): void {
  applyTheme();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
  window.addEventListener("storage", (event) => {
    if (event.key === "swissarmygrid.theme" || event.key === "swissarmygrid.zoom") applyTheme();
  });
}

// ===== what the page provides, and what the rail gives back =====

/** A table's details, for the page to show. */
export interface TableView {
  catalog: string;
  table: string;
  kind: "table" | "view";
  where: string; // "sales in Warehouse"
  name: string; // quoted for the source's dialect
  select: string; // a first SELECT, quoted for the dialect
  dialect: string;
  columns: ColumnRow[];
  focusColumn?: string;
}

export interface RailHost {
  bladesHost: HTMLElement;
  catalogHost: HTMLElement;
  /** Open the blade editor: a saved blade by name, or a new one (null). */
  editBlade(name: string | null): void;
  /** True while the editor holds unsaved work: the tree then keeps the page as it is. */
  isEditing(): boolean;
  showTable(view: TableView): void;
  showSchema?(label: string, catalog: string): void;
  /** In the Workbench every connection is there by its name: drag and copy give the name alone. */
  connectionsByName?: boolean;
}

export interface Rail {
  loadBlades(): Promise<void>;
  loadCatalogs(select?: string): Promise<void>;
  /** The editor saved `name`: mark it and reload the list. */
  bladeSaved(name: string): void;
  /** The editor closed: nothing is marked. */
  editorClosed(): void;
  blades(): BladeSummary[];
}

/** Code and the imports it needs, carried beside a drag's plain text (for pages that place cells). */
export const DROP_TYPE = "application/x-sag-drop";

export function copy(text: string, what: string): void {
  void navigator.clipboard.writeText(text).then(
    () => toast(`Copied ${what}.`),
    () => toast("The browser did not allow copying.", "error"),
  );
}

/** A table's columns and a first SELECT, for the page to show (main area or dialog). */
export function tableDetails(view: TableView): HTMLElement[] {
  const rows = view.columns.map((c) =>
    h(
      "tr",
      {
        "aria-current": c.column === view.focusColumn ? "true" : undefined,
        style: c.column === view.focusColumn ? { background: "var(--selected)" } : undefined,
      },
      h("td", {}, c.column),
      h("td", {}, c.type),
      h("td", {}, c.nullable === null ? "" : c.nullable ? "yes" : "no"),
      h("td", { class: "hint" }, c.description ?? ""),
    ),
  );
  return [
    h("h2", {}, view.table, view.kind === "view" ? h("span", { class: "hint" }, "  view") : null),
    h("p", { class: "hint" }, `${view.where} · ${view.columns.length} columns · ${view.dialect}`),
    h(
      "div",
      { class: "row-actions" },
      button("Copy the name", () => copy(view.name, "the name"), { icon: "copy" }),
      button("Copy the SELECT", () => copy(view.select, "the SELECT"), { icon: "sql" }),
    ),
    h("pre", { class: "code-box", "aria-label": "A first SELECT" }, view.select),
    h(
      "table",
      { class: "columns-table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Column"), h("th", {}, "Type"), h("th", {}, "Nullable"), h("th", {}, "Description"))),
      h("tbody", {}, ...rows),
    ),
  ];
}

export function mountRail(host: RailHost): Rail {
  // ===== state =====

  const { bladesHost, catalogHost } = host;

  let blades: BladeSummary[] = [];
  let catalogs: CatalogInfo[] = [];
  let current: CatalogInfo | null = null;
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

  // The blades list: a search (names, descriptions, tags), tag chips to narrow it, favourites first.
  let bladeTag: string | null = null;
  const bladeSearch = h("input", {
    class: "cat-search",
    type: "search",
    placeholder: "Search",
    "aria-label": "Search blades",
    spellcheck: "false",
  });
  bladeSearch.addEventListener("input", () => renderBladeList());
  const bladeTags = h("div", { class: "chips blade-tags", role: "group", "aria-label": "Tags" });
  const bladeList = h("ul", { class: "blade-list", "aria-label": "Saved blades" });

  function bladeMatches(blade: BladeSummary): boolean {
    const tags = blade.tags ?? [];
    if (bladeTag && !tags.some((t) => t.toLowerCase() === bladeTag)) return false;
    const text = bladeSearch.value.trim().toLowerCase();
    if (!text) return true;
    return [blade.name, blade.description ?? "", ...tags].some((field) => field.toLowerCase().includes(text));
  }

  async function setPinned(blade: BladeSummary, pinned: boolean): Promise<void> {
    try {
      await api(`/blades/${encodeURIComponent(blade.name)}/pin`, "PUT", { pinned });
      blade.pinned = pinned;
      renderBladeList();
    } catch (error) {
      toast((error as Error).message, "error");
    }
  }

  function renderBladeList(): void {
    // Every tag once (as first written), the chosen one pressed; a click narrows the list to it.
    const allTags = new Map<string, string>();
    for (const blade of blades) for (const tag of blade.tags ?? []) if (!allTags.has(tag.toLowerCase())) allTags.set(tag.toLowerCase(), tag);
    if (bladeTag && !allTags.has(bladeTag)) bladeTag = null;
    clear(
      bladeTags,
      ...[...allTags].sort(([a], [b]) => a.localeCompare(b)).map(([key, tag]) =>
        button(tag, () => {
          bladeTag = bladeTag === key ? null : key;
          renderBladeList();
        }, { class: "chip tag-chip", pressed: bladeTag === key, title: bladeTag === key ? "Clear filter" : undefined }),
      ),
    );
    bladeTags.hidden = allTags.size === 0;
    const shown = blades.filter(bladeMatches).sort((a, b) => Number(b.pinned) - Number(a.pinned));
    clear(bladeList);
    // Connections (databases and files) first, under their own heading; then the other blades.
    const connections = shown.filter((b) => b.connection);
    const others = shown.filter((b) => !b.connection);
    const heading = (text: string, extra: HTMLElement | null = null): HTMLElement => h("li", { class: "blade-group" }, h("span", {}, text), extra);
    if (connections.length || !blades.some((b) => b.connection)) {
      bladeList.append(heading("Connections", button("Add connection", () => void openConnections().then(() => void loadBlades()), { icon: "plus", iconOnly: true, class: "ghost" })));
    }
    let othersHeading = false;
    for (const blade of [...connections, ...others]) {
      if (!blade.connection && !othersHeading) {
        bladeList.append(heading("Blades"));
        othersHeading = true;
      }
      const line = host.connectionsByName && blade.code_name ? blade.code_name : bladeLine(blade);
      const item = h(
        "li",
        {
          class: "blade-item",
          draggable: "true",
          "aria-current": blade.name === selectedBlade ? "true" : undefined,
          title: `${blade.description ? blade.description + "\n" : ""}${line}`,
          ondragstart: (event: DragEvent) => {
            event.dataTransfer?.setData("text/plain", line);
            event.dataTransfer?.setData("application/x-sag-blade", JSON.stringify({ name: blade.name, kind: blade.kind, line }));
            event.dataTransfer?.setData(DROP_TYPE, JSON.stringify({ code: line, imports: ["import swissarmygrid as sag"] }));
          },
        },
        button(blade.pinned ? "Unpin" : "Pin", () => void setPinned(blade, !blade.pinned), {
          icon: "star",
          iconOnly: true,
          class: `ghost star${blade.pinned ? " on" : ""}`,
          pressed: blade.pinned,
        }),
        blade.connection
          ? h("span", { class: `blade-kind ${blade.connection === "database" ? "sql" : "other"}` }, { database: "Database", file: "File", vault: "Key vault" }[blade.connection] ?? "File")
          : h("span", { class: `blade-kind ${blade.kind}` }, kindLabel(blade.kind)),
        h(
          "span",
          { class: "blade-text" },
          h("button", { class: "blade-name", type: "button", onclick: () => openBlade(blade.name) }, blade.name),
          host.connectionsByName && blade.code_name ? h("code", { class: "blade-code-name", title: "Name in code" }, blade.code_name) : null,
          blade.tags?.length ? h("span", { class: "blade-item-tags" }, blade.tags.join(" · ")) : null,
        ),
        blade.source === "folder" ? h("span", { class: "blade-file", title: blade.path ?? "" }, "file") : null,
        blade.source === "pack" ? h("span", { class: "blade-file blade-pack", title: `${blade.pack ?? ""}, read-only` }, "pack") : null,
        blade.has_catalog
          ? button("Refresh catalog", () => void refresh(blade.name), { icon: "play", iconOnly: true, class: "ghost", title: `Refresh ${blade.name}'s catalog` })
          : null,
        button(blade.connection ? "Copy the sag.connection line" : "Copy the sag.blade line", () => copy(line, "the line"), { icon: "copy", iconOnly: true, class: "ghost", title: line }),
      );
      bladeList.append(item);
    }
    if (!shown.length && blades.length) bladeList.append(h("li", { class: "empty" }, "No blade matches."));
  }

  function renderBlades(): void {
    clear(
      bladesHost,
      h(
        "div",
        { class: "rail-head" },
        h("h2", { id: "blades-title" }, "Connections and blades"),
        button("Import blades", () => chooseBladeFiles(() => void loadBlades()), { icon: "import", iconOnly: true, class: "ghost", title: "Import .blade.py or zip" }),
        button("Export every blade", () => void exportBlades(), { icon: "export", iconOnly: true, class: "ghost" }),
        button("Blade folders", () => void editBladeFolders(() => void loadBlades()), { icon: "folder", iconOnly: true, class: "ghost" }),
        button("Packs", () => void openPacks(() => void loadBlades()), { icon: "group", iconOnly: true, class: "ghost" }),
        button("New blade", () => void newBlade(), { icon: "plus", class: "ghost" }),
      ),
      // The list always shows (its Connections heading has Add connection); the search only once there is something to search.
      blades.length
        ? h("div", {}, bladeSearch, bladeTags, bladeList)
        : h("div", {}, bladeList, h("p", { class: "empty" }, "No blades")),
    );
    renderBladeList();
  }

  // Dropping .blade.py, .py or .zip files on the blades opens the import review.
  bladesHost.addEventListener("dragover", (event) => {
    if (event.dataTransfer?.types.includes("Files")) {
      event.preventDefault();
      bladesHost.classList.add("dropping");
    }
  });
  bladesHost.addEventListener("dragleave", () => bladesHost.classList.remove("dropping"));
  bladesHost.addEventListener("drop", (event) => {
    bladesHost.classList.remove("dropping");
    const files = [...(event.dataTransfer?.files ?? [])];
    if (!files.length) return;
    event.preventDefault();
    void importBlades(files, () => void loadBlades());
  });

  async function newBlade(): Promise<void> {
    selectedBlade = null;
    renderBlades();
    host.editBlade(null);
  }

  function openBlade(name: string): void {
    selectedBlade = name;
    renderBlades();
    host.editBlade(name);
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
    placeholder: "Search",
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
    if (data.kind === "table") {
      // Script as (like SSMS's right-click): the server writes it, quoted for the source's own SQL.
      node.addEventListener("contextmenu", (event) => {
        event.preventDefault();
        event.stopPropagation();
        const run = (kind: string): void => void scriptAs(data, kind);
        showMenu(event.clientX, event.clientY, `Script ${data.table ?? "table"} as`, [
          ["sql", "SELECT *", () => run("select_star")],
          ["columns", "SELECT the columns", () => run("select_columns")],
          ["code", "Python: pandas", () => run("pandas")],
        ]);
      });
    }
    if (data.kind === "table" || data.kind === "column") {
      item.draggable = true;
      item.addEventListener("dragstart", (event) => {
        event.stopPropagation();
        const dragged = dragOf(data, item);
        event.dataTransfer?.setData("text/plain", dragged.text);
        if (dragged.imports.length) event.dataTransfer?.setData(DROP_TYPE, JSON.stringify({ code: dragged.text, imports: dragged.imports }));
      });
    }
    nodes.set(item, data);
    return item;
  }

  /** A table scripted as SQL or Python: copied, and queued for sag.paste() in Jupyter or IPython. */
  async function scriptAs(data: NodeData, kind: string): Promise<void> {
    if (!current || !data.table) return;
    const catalogName = current.name;
    const blade = blades.find((b) => b.name.toLowerCase() === catalogName.toLowerCase() && b.variable);
    const params = { schema: data.schema, table: data.table, database: data.database, as: kind, blade: blade?.name ?? "" };
    try {
      const made = await api<{ code: string; imports: string[]; label: string; language: string }>(`${catalogPath(catalogName)}/script${query(params)}`);
      await toPython(made.code, made.imports, `${data.table}: ${made.label}`);
    } catch (error) {
      toast((error as Error).message, "error");
    }
  }

  function dragText(data: NodeData): string {
    if (data.kind === "column" && data.column) return quoteName(data.column, current?.dialect ?? "other");
    return qualified({ database: data.database, schema: data.schema, table: data.table ?? "" });
  }

  /** A first SELECT over a table, quoted for the dialect (as engine/catalog.py's select_sql). */
  function selectFor(table: string, columns: string[]): string {
    const dialect = current?.dialect ?? "other";
    const picked = columns.length ? columns.map((c) => quoteName(c, dialect)).join(", ") : "*";
    if (dialect === "sqlserver") return `SELECT TOP 100 ${picked}\nFROM ${table}`;
    if (dialect === "oracle") return `SELECT ${picked}\nFROM ${table}\nFETCH FIRST 100 ROWS ONLY`;
    return `SELECT ${picked}\nFROM ${table}\nLIMIT 100`;
  }

  /** What a tree item gives when dragged: its name, or for a table a query over it. */
  function dragOf(data: NodeData, _item: HTMLElement): { text: string; imports: string[] } {
    return { text: dragText(data), imports: [] };
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
      tree.append(h("li", { class: "empty" }, "No catalog loaded"));
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


  // ===== details: a table's columns and a first SELECT, shown by the page =====

  async function showNode(item: HTMLElement): Promise<void> {
    const data = nodes.get(item);
    if (!data || !current) return;
    if (host.isEditing()) {
      toast("Save or cancel the blade first.");
      return;
    }
    if (data.kind === "table" || data.kind === "column") await showTable(data, data.column);
    if (data.kind === "schema") host.showSchema?.(schemaLabel(data), current.name);
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
    host.showTable({
      catalog: current.name,
      table: data.table,
      kind: data.tableKind ?? "table",
      where: `${schemaLabel(data)} in ${current.name}`,
      name: qualified({ database: data.database, schema: data.schema, table: data.table }),
      select: answer.select,
      dialect: answer.dialect,
      columns: answer.columns,
      focusColumn,
    });
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
    if (host.isEditing()) {
      toast("Save or cancel the blade first.");
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

  return {
    loadBlades,
    loadCatalogs,
    bladeSaved(name: string): void {
      selectedBlade = name;
      void loadBlades().then(renderPicker);
    },
    editorClosed(): void {
      selectedBlade = null;
      renderBlades();
    },
    blades: () => blades,
  };
}
