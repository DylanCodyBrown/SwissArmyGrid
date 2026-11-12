/**
 * gridView.ts: the data grid. [WIRE + EXPORT]
 *
 * AG Grid Community's infinite row model asks the server for windows of rows as the user
 * scrolls; the browser never holds the table (plan section 3). Sorting is a step on the server:
 * clicking a header posts the sort, bumps `gen`, and refetches.
 *
 * Selection works like Excel (selection.ts holds it): the row numbers select rows, a header
 * selects its column, dragging across cells selects a block, the corner or Ctrl+A selects
 * everything. AG Grid Community has no range selection, so the grid draws it with cell
 * classes and the server does the sums and the copying (the selection can be bigger than
 * what is loaded).
 *
 * ROUTING: [COLUMN HEADER]   click / drag -> selection.selectColumns(); stats from loadHeaderStats()
 *          [SORT BUTTON]     -> progressSort() -> syncHeaderSort()   (in each header)
 *          [CORNER]          -> selection.selectAll()  (also Ctrl+A)
 *          [ROW NUMBERS]     click / drag -> selection.selectRows()
 *          [CELLS]           click / drag / Shift+arrows -> selection.selectCells()
 *          [ROWS]            -> datasource.getRows(), refreshed by refresh()
 *          [MARKER COLUMN]   -> read-only, from the rows response's `highlighted`
 *          [CELL VIEWER]     double-click -> showCellDetail() -> openValueViewer()
 *          [CHANGED CELL]    -> read-only, computed while previewing a step
 *          [SELECTION STATS] -> selection change -> loadSelectionStats()  (status bar, agent count)
 *          [COPY]            -> Ctrl+C -> copySelection()  (server-built tab-separated text)
 */
import {
  AllCommunityModule,
  ModuleRegistry,
  createGrid,
  type ColDef,
  type GridApi,
  type ICellRendererParams,
  type IDatasource,
  type IGetRowsParams,
  type IHeaderComp,
  type IHeaderParams,
} from "ag-grid-community";
import { api, clientId, download, query, type ColumnInfo, type Row, type RowsResponse } from "./api";
import { formatCount, h, icon, isDateType, isNumericType, toast } from "./dom";
import { onRelationshipsChange, primaryKey } from "./relationships";
import { GridSelection } from "./selection";
import type { PageState } from "./state";
import { gridRowHeight, gridTheme, gridZoom, headerProfiles, onHeaderProfilesChange, onThemeChange } from "./theme";
import { openValueViewer } from "./valueViewer";

ModuleRegistry.registerModules([AllCommunityModule]);

const MARKER = "__sag_marker";
const FILLER = "__sag_filler";
const ROWNUM = "__sag_rownum";
/** Copying past this many rows points to Export instead (the clipboard is not a file). */
const COPY_CAP = 200_000;

/** "2026-03-16T00:00:00" reads as 2026-03-16; other times as "2026-03-16 13:05:00". */
export function formatTemporal(value: string): string {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$/.exec(value);
  if (!match) return value;
  return match[2] === "00:00:00" && !match[3] && !match[4] ? match[1]! : `${match[1]} ${match[2]}${match[4] ?? ""}`;
}
const numberFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 20, useGrouping: false });

/** One grid's view: what createGridView returns. The page holds one per open grid; the panels
 * talk to the active one through the functions at the end of this file. */
export interface GridView {
  state: PageState;
  selection: GridSelection;
  host: HTMLElement;
  refresh: () => void;
  syncHeaderSort: () => void;
  copySelection: () => Promise<void>;
  focusedCell: () => { row: number; column: string } | null;
  cellValue: (row: number, column: string) => Promise<unknown>;
  selectedValues: (column: string, cap?: number) => unknown[];
  viewCell: () => void;
  viewColumns: () => ColumnInfo[];
  displayedRange: () => [number, number];
  shownTotal: () => number;
  /** Fit columns to their header and the loaded rows: the named ones, or every data column. */
  autoSizeColumns: (names?: string[]) => void;
  /** Compare mode: read rows aligned with the other grid (null: this grid's own view). */
  setCompare: (compare: { id: string; side: "left" | "right" } | null) => void;
  /** Rows sharing the picked row's key, highlighted on both compared grids (null: none). */
  setKeyRows: (rows: { first: number; last: number } | null) => void;
  /** A data row was clicked (cross compare filters the other side to its key). */
  onRowPick: (listener: (row: number) => void) => void;
  destroy: () => void;
}

/** Build the grid for `state` (one open grid) inside `host`. Everything below is that one grid's
 * own: its windows of rows, its selection, its AG Grid instance, its listeners. Like a WPF
 * UserControl: create one per grid, and each keeps its own fields. */
export function createGridView(host: HTMLElement, state: PageState): GridView {
  // This grid's API path (the page may show several grids; each view uses its own).
  const grid = state.base;
  const selection = new GridSelection();

  // ===== state for the current windows =====
  const highlights = new Map<number, string>();
  const truncated = new Set<string>(); // "row:col"
  const changed = new Set<string>(); // "row:column name"
  const newColumns = new Set<string>();
  // Compare mode (panes.ts sets it): rows come from the compare, aligned with the other grid.
  let compare: { id: string; side: "left" | "right" } | null = null;
  const placeholders = new Set<number>(); // compare rows where this side has no row for the key
  const diffs = new Set<string>(); // "row:col" cells that differ from the aligned row
  const pickListeners: ((row: number) => void)[] = [];
  let keyRows: { first: number; last: number } | null = null;
  let columns: ColumnInfo[] = [];
  let sentSort = "[]";
  let headerStats: Record<string, { distinct: number; blank_percent: number }> = {};
  const sparks: Record<string, number[]> = {};
  let gridApi: GridApi<Row> | null = null;
  // What a held mouse button is selecting, if anything (row numbers, headers or cells).
  let dragging: "rows" | "columns" | "cells" | null = null;
  let lastPointerDown = 0;
  const headerMarks = new Set<() => void>();
  // Bumped when what a header draws changes (new stats, profiles switched on or off); a header
  // drawn at an older version is rebuilt on the next refreshHeader().
  let headerVersion = 0;
  // What destroy() undoes: the document listeners and page-wide subscriptions this grid made.
  // Without them a closed grid stayed reachable (its AG Grid, its rows) and every closed grid
  // left four more document listeners behind.
  const disposers: (() => void)[] = [];
  let destroyed = false;
  function listen<K extends keyof DocumentEventMap>(type: K, listener: (event: DocumentEventMap[K]) => void, capture = false): void {
    document.addEventListener(type, listener, capture);
    disposers.push(() => document.removeEventListener(type, listener, capture));
  }

  // ===== the column header: name, sort, stats, a tiny histogram, a profile button =====

  class StatsHeader implements IHeaderComp {
    private gui!: HTMLElement;
    private sort!: HTMLElement;
    private params!: IHeaderParams;
    private drawnAt = 0;

    init(params: IHeaderParams): void {
      this.params = params;
      this.drawnAt = headerVersion;
      const name = params.column.getColId();
      const info = columns.find((c) => c.name === name);
      this.sort = h("button", {
        type: "button",
        class: "sag-header-sort",
        title: `Sort by ${name} (click again to reverse, a third time to clear; Shift+click adds a sort key)`,
        "aria-label": `Sort by ${name}`,
        onclick: (event: MouseEvent) => params.progressSort(event.shiftKey),
      });
      const stats = headerStats[name];
      const spark = sparks[name];
      this.gui = h(
        "div",
        {
          class: "sag-header",
          title: `${name} (${info?.type ?? ""}). Click to select the column; Shift or Ctrl+click for several; drag across headers.`,
          onmousedown: (event: MouseEvent) => {
            if (event.button !== 0 || (event.target as HTMLElement).closest("button")) return;
            event.preventDefault();
            selection.selectColumns(name, event.shiftKey ? "extend" : event.ctrlKey || event.metaKey ? "add" : "replace");
            dragging = "columns";
          },
          onmouseenter: () => {
            if (dragging === "columns") selection.selectColumns(name, "extend");
          },
        },
        h(
          "div",
          { class: "sag-header-top" },
          // A primary-key column (from the relationship model) carries a key mark.
          primaryKey(state.info?.dataset ?? "").includes(name) ? h("span", { class: "sag-header-key", title: `${name} is part of this table's primary key` }, icon("key")) : null,
          h("span", { class: "sag-header-name" }, params.displayName),
          this.sort,
        ),
        // The column profile lines, only when turned on in Settings.
        headerProfiles()
          ? h("div", { class: "sag-header-stats" }, stats ? `${formatCount(stats.distinct)} distinct · ${stats.blank_percent}% blank` : " ")
          : null,
        !headerProfiles()
          ? null
          : spark
            ? h("div", { class: "sag-header-spark", "aria-hidden": "true" }, ...spark.map((v) => h("span", { style: { height: `${v}%` } })))
            : h("div", { class: "sag-header-spark-empty", "aria-hidden": "true" }),
      );
      params.column.addEventListener("sortChanged", this.updateSort);
      this.updateSort();
      headerMarks.add(this.markSelected);
      setTimeout(this.markSelected, 0);
    }

    private updateSort = (): void => {
      const sort = this.params.column.getSort();
      const index = this.params.column.getSortIndex();
      const many = (gridApi?.getColumnState().filter((c) => c.sort).length ?? 0) > 1;
      this.sort.textContent = sort === "asc" ? `↑${many && index != null ? index + 1 : ""}` : sort === "desc" ? `↓${many && index != null ? index + 1 : ""}` : "⇅";
      this.sort.classList.toggle("active", Boolean(sort));
      this.gui.setAttribute("aria-sort", sort === "asc" ? "ascending" : sort === "desc" ? "descending" : "none");
    };

    /** Selected columns light up like Excel's; a column inside a selected block gets a tint. */
    private markSelected = (): void => {
      const name = this.params.column.getColId();
      const cell = this.gui.closest(".ag-header-cell");
      cell?.classList.toggle("col-selected", selection.hasColumn(name));
      cell?.classList.toggle("col-active", selection.kind === "cells" && selection.columns.includes(name));
    };

    getGui(): HTMLElement {
      return this.gui;
    }

    refresh(params: IHeaderParams): boolean {
      if (this.drawnAt !== headerVersion) return false; // AG Grid then builds a fresh header
      this.params = params;
      this.updateSort();
      return true;
    }

    destroy(): void {
      this.params.column.removeEventListener("sortChanged", this.updateSort);
      headerMarks.delete(this.markSelected);
    }
  }

  /** The corner above the row numbers: click to select everything, like Excel's. */
  class CornerHeader implements IHeaderComp {
    private gui!: HTMLElement;

    init(): void {
      this.gui = h("button", {
        type: "button",
        class: "sag-corner",
        title: "Select everything (Ctrl+A)",
        "aria-label": "Select all",
        onclick: () => selection.selectAll(),
      });
    }

    getGui(): HTMLElement {
      return this.gui;
    }

    refresh(): boolean {
      return true;
    }
  }

  // ===== column definitions =====

  function markerRenderer(params: ICellRendererParams<Row>): HTMLElement | string {
    const label = params.node.rowIndex != null ? highlights.get(params.node.rowIndex) : undefined;
    return label ? h("span", { class: "marker-cell", title: label, "aria-label": `Highlighted: ${label}` }, "⚑") : "";
  }

  function columnDefs(list: ColumnInfo[]): ColDef<Row>[] {
    const defs: ColDef<Row>[] = [
      {
        // The row numbers, Excel's row headers: click or drag them to select whole rows.
        colId: ROWNUM,
        headerName: "",
        headerComponent: CornerHeader,
        width: rowNumberWidth(shownTotal),
        pinned: "left",
        lockPosition: "left",
        sortable: false,
        resizable: false,
        suppressMovable: true,
        suppressNavigable: true,
        valueGetter: (p) => (p.node?.rowIndex != null ? p.node.rowIndex + 1 : ""),
        valueFormatter: (p) => (typeof p.value === "number" ? formatCount(p.value) : ""),
        cellClass: "row-header",
        cellClassRules: {
          "row-selected": (p) => p.node.rowIndex != null && selection.hasRow(p.node.rowIndex),
          "row-active": (p) => p.node.rowIndex != null && selection.kind === "cells" && selection.hasCell(p.node.rowIndex, selection.columns[0] ?? ""),
        },
      },
      {
        colId: MARKER,
        headerName: "",
        // Shown only while some row is highlighted (an agent or a rule marked it).
        hide: true,
        width: 40,
        pinned: "left",
        sortable: false,
        resizable: false,
        suppressMovable: true,
        cellRenderer: markerRenderer,
      },
    ];
    list.forEach((column, index) => {
      const numeric = isNumericType(column.type);
      const temporal = isDateType(column.type);
      defs.push({
        colId: column.name,
        headerName: column.name,
        headerComponent: StatsHeader,
        valueGetter: (params) => params.data?.[index],
        valueFormatter: (params) =>
          params.data === undefined || (params.node?.rowIndex != null && placeholders.has(params.node.rowIndex))
            ? "" // a row still loading, or a compare placeholder: empty, not "(blank)"
            : params.value === null || params.value === undefined
            ? "(blank)"
            : numeric && typeof params.value === "number"
              ? numberFormat.format(params.value)
              : temporal
                ? formatTemporal(String(params.value))
                : String(params.value),
        cellClassRules: {
          "cell-blank": (p) => p.data !== undefined && p.value === null && !(p.node.rowIndex != null && placeholders.has(p.node.rowIndex)),
          "cell-diff": (p) => p.node.rowIndex != null && diffs.has(`${p.node.rowIndex}:${index}`),
          "cell-truncated": (p) => p.node.rowIndex != null && truncated.has(`${p.node.rowIndex}:${index}`),
          "cell-changed": (p) => newColumns.has(column.name) || (p.node.rowIndex != null && changed.has(`${p.node.rowIndex}:${column.name}`)),
          "cell-selected": (p) => p.node.rowIndex != null && selection.hasCell(p.node.rowIndex, column.name),
        },
        cellClass: numeric ? "cell-number" : undefined,
        // No data column is pinned: every column scrolls sideways with the rest (only the row
        // numbers stay at the left). A pinned first column read as stuck to the scroll.
        headerClass: numeric ? "header-number" : undefined,
        sortable: true,
        minWidth: 90,
      });
    });
    // An empty last column, sized by fitFiller() so the farthest scroll position lands on a
    // column edge (otherwise the end of the table can leave a column cut in half at the left).
    defs.push({ colId: FILLER, headerName: "", width: 0, minWidth: 0, sortable: false, resizable: false, suppressMovable: true, suppressNavigable: true, valueGetter: () => "", headerClass: "filler", cellClass: "filler" });
    return defs;
  }

  // ===== the datasource =====

  const datasource: IDatasource = {
    getRows(params: IGetRowsParams): void {
      void loadWindow(params);
    },
  };

  async function loadWindow(params: IGetRowsParams): Promise<void> {
    const sort = JSON.stringify(params.sortModel.filter((s) => s.colId !== MARKER).map((s) => [s.colId, s.sort]));
    try {
      if (compare) {
        await loadCompareWindow(params, compare);
        return;
      }
      if (sort !== sentSort) {
        state.gen += 1;
        sentSort = sort;
        state.info = await api(`${grid}/header-sort`, "POST", { keys: JSON.parse(sort), gen: state.gen, client: clientId });
        state.emit("info");
      }
      const gen = state.gen;
      const upto = state.previewUpto;
      const body = await api<RowsResponse>(
        `${grid}/rows${query({ start: params.startRow, end: params.endRow, gen, upto, client: clientId })}`,
      );
      if (destroyed) return; // the tab closed while its rows loaded
      if (gen !== state.gen) {
        params.failCallback(); // a newer view replaced this one while it loaded
        return;
      }
      for (const [offset, label] of body.highlighted) highlights.set(params.startRow + offset, label);
      for (const [r, c] of body.truncated) truncated.add(`${params.startRow + r}:${c}`);
      if (upto !== null && upto > 0) {
        await markChanges(params.startRow, params.endRow, upto, body);
        const first = [...newColumns][0] ?? [...changed][0]?.split(":").slice(1).join(":");
        if (first && params.startRow === 0) setTimeout(() => gridApi?.ensureColumnVisible(first, "middle"), 0);
        state.changedCells = changed.size + newColumns.size * body.rows.length;
        state.emit("preview-marks");
      }
      params.successCallback(body.rows, body.row_count);
      showEmpty(body.row_count === 0 && params.startRow === 0);
      gridApi?.setColumnsVisible([MARKER], highlights.size > 0);
      shownTotal = body.row_count;
      fitRowNumbers();
      state.emit("selection");
    } catch (error) {
      if (destroyed) return;
      params.failCallback();
      if ((error as { status?: number }).status !== 409) toast(String((error as Error).message ?? error), "error");
    }
  }

  /** A window of compare rows: this side's rows in the order both grids share. */
  async function loadCompareWindow(params: IGetRowsParams, cmp: { id: string; side: string }): Promise<void> {
    const body = await api<RowsResponse & { placeholders: number[]; diffs: [number, number][] }>(
      `/compares/${encodeURIComponent(cmp.id)}/rows${query({ side: cmp.side, start: params.startRow, end: params.endRow })}`,
    );
    if (destroyed) return;
    if (compare?.id !== cmp.id) {
      params.failCallback();
      return;
    }
    for (const offset of body.placeholders) placeholders.add(params.startRow + offset);
    for (const [r, c] of body.diffs) diffs.add(`${params.startRow + r}:${c}`);
    for (const [r, c] of body.truncated) truncated.add(`${params.startRow + r}:${c}`);
    params.successCallback(body.rows, body.row_count);
    showEmpty(body.row_count === 0 && params.startRow === 0);
    shownTotal = body.row_count;
    fitRowNumbers();
    state.emit("selection");
  }

  /** While previewing a step that keeps row order (transforms, buckets, categorize), compare
   * each cell with the stage before it and mark the ones that changed. */
  async function markChanges(start: number, end: number, upto: number, after: RowsResponse): Promise<void> {
    const step = state.info?.steps[upto - 1];
    if (!step || !["transform", "bucket", "categorize", "rename"].includes(step.kind)) return;
    const before = await api<RowsResponse>(`${grid}/rows${query({ start, end, upto: upto - 1 })}`);
    after.columns.forEach((name, c) => {
      const at = before.columns.indexOf(name);
      if (at === -1) {
        newColumns.add(name);
        return;
      }
      after.rows.forEach((row, r) => {
        if (JSON.stringify(row[c]) !== JSON.stringify(before.rows[r]?.[at])) changed.add(`${start + r}:${name}`);
      });
    });
  }

  let shownTotal = 0;

  /** The row numbers' column fits the largest number at this zoom ("1,000,000" read "999,..."
   * in a fixed 64px, and so did "5,000" at 200%). */
  function fitRowNumbers(): void {
    const column = gridApi?.getColumn(ROWNUM);
    const width = rowNumberWidth(shownTotal);
    if (column && column.getActualWidth() !== width) gridApi?.setColumnWidths([{ key: ROWNUM, newWidth: width }]);
  }

  function showEmpty(empty: boolean): void {
    host.querySelector(".grid-empty")?.remove();
    if (!empty) return;
    const upto = state.previewUpto;
    const where = upto === null ? "after the steps" : upto === 0 ? "in the source" : `after step ${upto}`;
    host.append(
      h(
        "div",
        { class: "grid-empty", role: "status" },
        h("strong", {}, `No rows ${where}.`),
        h("span", {}, "A filter may be too strict: turn a step off or edit it in the steps panel."),
      ),
    );
  }

  // ===== clicking off the grid clears the selection =====

  /** Where a click leaves the selection alone: the ribbon's Column / Cell / Table buttons act on it, and menus,
   * popups and dialogs are how it is used (copy, export, a step from it). */
  const KEEP_SELECTION_UNDER = ".ribbon, .menu, .ag-popup, dialog, [role='menu'], [role='dialog']";
  function clearOnClickOff(event: PointerEvent): void {
    if (selection.kind === "none") return;
    const target = event.target;
    if (!(target instanceof Element) || host.contains(target) || target.closest(KEEP_SELECTION_UNDER)) return;
    selection.clear();
  }
  listen("pointerdown", clearOnClickOff, true);

  // ===== public: build, refresh, sync =====

  function build(): void {
    columns = state.previewColumns();
    selection.setColumnOrder(columns.map((c) => c.name));
    selection.onChange(onSelectionChange);
    gridApi = createGrid<Row>(host, {
      theme: gridTheme(),
      rowHeight: gridRowHeight(), // not only from the theme's CSS: see gridRowHeight
      rowModelType: "infinite",
      datasource,
      cacheBlockSize: 200,
      maxBlocksInCache: 40,
      columnDefs: columnDefs(columns),
      defaultColDef: { resizable: true },
      // Always keep the sideways scrollbar on screen (Edge's overlay scrollbars hide it otherwise).
      alwaysShowHorizontalScroll: true,
      getRowClass: (p) =>
        p.node.rowIndex == null
          ? undefined
          : [
              placeholders.has(p.node.rowIndex) ? "row-placeholder" : "",
              highlights.has(p.node.rowIndex) ? "row-highlight" : "",
              keyRows && p.node.rowIndex >= keyRows.first && p.node.rowIndex <= keyRows.last ? "row-key-match" : "",
            ].join(" ").trim() || undefined,
      onCellMouseDown: (event) => {
        const mouse = event.event as MouseEvent | null;
        if (!mouse || mouse.button !== 0 || event.rowIndex == null) return;
        const column = event.column.getColId();
        lastPointerDown = Date.now();
        // Any row, its number or a placeholder: the compare decides what a pick means.
        for (const pick of pickListeners) pick(event.rowIndex);
        if (column === ROWNUM) {
          mouse.preventDefault();
          selection.selectRows(event.rowIndex, mouse.shiftKey ? "extend" : mouse.ctrlKey || mouse.metaKey ? "add" : "replace");
          dragging = "rows";
        } else if (column !== MARKER && column !== FILLER) {
          selection.selectCells(event.rowIndex, column, mouse.shiftKey);
          dragging = "cells";
        }
        if (dragging) document.body.classList.add("sag-selecting");
      },
      onCellMouseOver: (event) => {
        if (!dragging || event.rowIndex == null) return;
        lastHover = { row: event.rowIndex, column: event.column.getColId() };
        extendDrag(event.rowIndex, event.column.getColId());
      },
      onCellFocused: (event) => {
        // Arrow keys move the active cell; without Shift the selection follows it, as in Excel.
        // (A mouse press already selected; Shift+arrows extend in the keydown handler.)
        if (Date.now() - lastPointerDown < 400 || shiftHeld || event.rowIndex == null) return;
        const column = typeof event.column === "string" ? event.column : event.column?.getColId();
        if (column && column !== ROWNUM && column !== MARKER && column !== FILLER) selection.selectCells(event.rowIndex, column, false);
      },
      onCellDoubleClicked: (event) => {
        const column = event.column.getColId();
        if (![MARKER, FILLER, ROWNUM].includes(column) && event.rowIndex != null) void showCellDetail(event.rowIndex, column);
      },
      onViewportChanged: () => state.emit("selection"),
      onGridSizeChanged: () => fitFiller(),
      onColumnResized: (event) => { if (event.finished && event.column?.getColId() !== FILLER) fitFiller(); },
      onDisplayedColumnsChanged: () => setTimeout(fitFiller, 0),
      onFirstDataRendered: () => fitFiller(),
      onBodyScrollEnd: () => state.emit("selection"),
      overlayNoRowsTemplate: "<span>No rows in this view.</span>",
    });
    // Capture phase: AG Grid handles Ctrl+A and Ctrl+C itself and stops them reaching us.
    host.addEventListener("keydown", onGridKey, true);
    listen("keydown", (e) => { shiftHeld = e.shiftKey; });
    listen("keyup", (e) => { shiftHeld = e.shiftKey; });
    listen("mouseup", () => stopDrag());
    listen("mousemove", (e) => { if (dragging === "rows" || dragging === "cells") autoScroll(e.clientY); });
    disposers.push(onThemeChange(() => {
      if (!gridApi || gridApi.isDestroyed()) return;
      gridApi.setGridOption("theme", gridTheme());
      // Zoom changes the row height too; the infinite model lays rows out again only after a purge.
      if (gridApi.getGridOption("rowHeight") !== gridRowHeight()) {
        gridApi.setGridOption("rowHeight", gridRowHeight());
        gridApi.purgeInfiniteCache();
      }
      fitRowNumbers();
    }));
    disposers.push(onRelationshipsChange(() => {
      // Keys changed: rebuild the headers so the key marks follow.
      headerVersion += 1;
      if (gridApi && !gridApi.isDestroyed()) gridApi.refreshHeader();
    }));
    disposers.push(onHeaderProfilesChange(() => {
      // New header height, and headers rebuilt with (or without) their profile lines.
      headerVersion += 1;
      gridApi?.setGridOption("theme", gridTheme());
      gridApi?.refreshHeader();
      void loadHeaderStats();
    }));
    syncHeaderSort();
    void loadHeaderStats();
  }

  /** Refetch rows in place (keeps scroll, sort and column layout). New columns rebuild defs. */
  function refresh(): void {
    if (!gridApi) return;
    highlights.clear();
    truncated.clear();
    changed.clear();
    newColumns.clear();
    placeholders.clear();
    diffs.clear();
    // The rows under a selection move when the view changes, so the selection goes.
    selection.clear();
    const next = state.previewColumns();
    const reshaped = JSON.stringify(next) !== JSON.stringify(columns);
    if (reshaped) {
      const added = next.filter((c) => !columns.some((old) => old.name === c.name));
      columns = next;
      selection.setColumnOrder(columns.map((c) => c.name));
      gridApi.setGridOption("columnDefs", columnDefs(columns));
      // A step just made a column (bucket, categorize, group by): bring it into view.
      const first = added[0];
      if (first) setTimeout(() => gridApi?.ensureColumnVisible(first.name, "end"), 0);
    }
    syncHeaderSort();
    // Cached rows no longer line up with new columns: drop them rather than redraw them.
    if (reshaped) gridApi.purgeInfiniteCache();
    else gridApi.refreshInfiniteCache();
    void loadHeaderStats();
  }

  /** Make the grid's sort arrows match the header-sort step on the server. */
  function syncHeaderSort(): void {
    if (!gridApi) return;
    const header = state.info?.steps.find((s) => s.params.header);
    const keys = (header?.params.keys as [string, string][] | undefined) ?? [];
    sentSort = JSON.stringify(keys);
    gridApi.applyColumnState({
      state: keys.map(([colId, sort], i) => ({ colId, sort: sort as "asc" | "desc", sortIndex: i })),
      defaultState: { sort: null },
    });
  }

  async function loadHeaderStats(): Promise<void> {
    // Off by default: the profile queries run only when the headers show them.
    if (!headerProfiles() || destroyed) return;
    try {
      headerStats = await api(`${grid}/header-stats${query({ gen: state.gen, client: clientId })}`);
      // Tiny histograms for up to 12 number columns, from the (cached) profiles.
      await Promise.all(
        columns
          .filter((c) => isNumericType(c.type))
          .slice(0, 12)
          .map(async (c) => {
            const p = await api<{ histogram?: { count: number }[] }>(`${grid}/profile${query({ column: c.name })}`);
            const counts = (p.histogram ?? []).map((b) => b.count);
            const top = Math.max(1, ...counts);
            sparks[c.name] = counts.map((n) => Math.round((100 * n) / top));
          }),
      );
      if (destroyed) return;
      headerVersion += 1;
      gridApi?.refreshHeader();
    } catch {
      // Header stats are a convenience; the grid works without them.
    }
  }

  // ===== selection, copy, detail =====

  let shiftHeld = false;
  let lastHover: { row: number; column: string } | null = null;
  let scrollTimer: number | undefined;
  let statsTimer: number | undefined;
  let statsAsked = 0;

  function extendDrag(row: number, column: string): void {
    if (dragging === "rows") selection.selectRows(row, "extend");
    else if (dragging === "cells" && column !== ROWNUM && column !== MARKER && column !== FILLER) selection.selectCells(row, column, true);
  }

  function stopDrag(): void {
    dragging = null;
    lastHover = null;
    window.clearInterval(scrollTimer);
    scrollTimer = undefined;
    document.body.classList.remove("sag-selecting");
  }

  /** Dragging past the top or bottom of the grid keeps scrolling and extending, like Excel. */
  function autoScroll(y: number): void {
    const body = host.querySelector(".ag-grid-viewport")?.getBoundingClientRect();
    if (!body || !gridApi) return;
    const direction = y > body.bottom - 4 ? 1 : y < body.top + 4 ? -1 : 0;
    window.clearInterval(scrollTimer);
    scrollTimer = undefined;
    if (!direction) return;
    scrollTimer = window.setInterval(() => {
      if (!gridApi || !dragging) return;
      const edge = direction > 0 ? gridApi.getLastDisplayedRowIndex() + 1 : gridApi.getFirstDisplayedRowIndex() - 1;
      const row = Math.max(0, Math.min(edge, Math.max(0, shownTotal - 1)));
      gridApi.ensureIndexVisible(row, direction > 0 ? "bottom" : "top");
      extendDrag(row, lastHover?.column ?? selection.columns[selection.columns.length - 1] ?? "");
    }, 60);
  }

  function onGridKey(event: KeyboardEvent): void {
    const ctrl = event.ctrlKey || event.metaKey;
    if ((event.target as HTMLElement).closest("input, textarea, select")) return;
    const focused = gridApi?.getFocusedCell();
    const row = focused?.rowIndex;
    const column = focused?.column.getColId();
    if (ctrl && event.key.toLowerCase() === "c") {
      event.preventDefault();
      event.stopPropagation();
      void copySelection();
    } else if (ctrl && event.key.toLowerCase() === "a") {
      event.preventDefault();
      event.stopPropagation();
      selection.selectAll();
    } else if (event.key === "Escape") {
      selection.clear();
    } else if (event.key === " " && event.shiftKey && row != null) {
      event.preventDefault();
      selection.selectRows(row, "replace");
    } else if (event.key === " " && ctrl && column) {
      event.preventDefault();
      selection.selectColumns(column, "replace");
    } else if (event.shiftKey && event.key.startsWith("Arrow")) {
      // AG Grid moves the active cell; the block then grows from the anchor to it.
      if (selection.kind !== "cells" && row != null && column) selection.selectCells(row, column, false);
      setTimeout(() => {
        const next = gridApi?.getFocusedCell();
        const id = next?.column.getColId();
        if (next?.rowIndex != null && id && id !== ROWNUM) selection.selectCells(next.rowIndex, id, true);
      }, 0);
    }
  }

  function onSelectionChange(): void {
    gridApi?.refreshCells({ force: true });
    for (const mark of headerMarks) mark();
    state.emit("grid-selection");
    window.clearTimeout(statsTimer);
    statsTimer = window.setTimeout(() => void loadSelectionStats(), 120);
  }

  /** The status bar's Count / Sum / Average, computed by the server over the whole selection. */
  async function loadSelectionStats(): Promise<void> {
    if (destroyed) return;
    const descriptor = selection.descriptor();
    const asked = ++statsAsked;
    if (!descriptor) {
      state.selection = null;
      state.emit("selection");
      void api(`${grid}/selection`, "POST", { count: 0 }).catch(() => undefined);
      return;
    }
    try {
      const stats = await api<SelectionStats>(`${grid}/selection-stats${query({ gen: state.gen, client: clientId })}`, "POST", { selection: descriptor });
      if (asked !== statsAsked || destroyed) return; // a newer selection is on its way
      state.selection = { ...stats, kind: selection.kind };
      state.emit("selection");
    } catch {
      // The status bar just keeps its last numbers.
    }
  }

  interface SelectionStats {
    rows: number;
    cells: number;
    count: number;
    numeric: number;
    sum: number | null;
    mean: number | null;
    min: number | null;
    max: number | null;
  }

  /** Ctrl+C: the selection as tab-separated text, built by the server, so rows that were never
   * loaded copy too. Whole rows, columns and select-all bring the header row; a block of cells
   * copies just the cells, as Excel does. */
  async function copySelection(): Promise<void> {
    const descriptor = selection.descriptor();
    if (!descriptor) {
      const cell = focusedCell();
      if (!cell) return;
      const value = await cellValue(cell.row, cell.column);
      await writeClipboard(Promise.resolve(value === null || value === undefined ? "" : String(value)), "Copied the cell.");
      return;
    }
    const rows = selection.rowCount(shownTotal);
    if (rows > COPY_CAP) {
      toast(`That is ${formatCount(rows)} rows; the clipboard takes up to ${formatCount(COPY_CAP)}. Use Export for more.`, "error");
      return;
    }
    const text = download(
      `${grid}/export${query({
        scope: "selection",
        selection: JSON.stringify(descriptor),
        format: "txt",
        delimiter: "tab",
        header: selection.kind === "cells" ? "0" : "1",
        provenance: "0",
        neutralize_formulas: "0",
        line_ending: "windows",
      })}`,
    ).then(({ blob }) => blob.text());
    const cells = descriptor.columns ? descriptor.columns.length : columns.length;
    await writeClipboard(text, `Copied ${formatCount(rows)} row${rows === 1 ? "" : "s"} × ${formatCount(cells)} column${cells === 1 ? "" : "s"}.`);
  }

  async function writeClipboard(text: Promise<string>, done: string): Promise<void> {
    try {
      // A ClipboardItem built from a promise keeps the key press's permission while the server
      // builds the text; plain writeText is the fallback for browsers without it.
      if (typeof ClipboardItem !== "undefined") {
        await navigator.clipboard.write([new ClipboardItem({ "text/plain": text.then((t) => new Blob([t], { type: "text/plain" })) })]);
      } else {
        await navigator.clipboard.writeText(await text);
      }
      toast(done);
    } catch (error) {
      toast(`Could not copy: ${(error as Error).message}`, "error");
    }
  }

  /** The active cell (the one keyboard focus is on), if it is a data cell. */
  function focusedCell(): { row: number; column: string } | null {
    const focused = gridApi?.getFocusedCell();
    const column = focused?.column.getColId();
    if (focused?.rowIndex == null || !column || [ROWNUM, MARKER, FILLER].includes(column)) return null;
    return { row: focused.rowIndex, column };
  }

  /** A cell's full value: from the loaded row unless it was cut short for display. */
  async function cellValue(row: number, column: string): Promise<unknown> {
    const index = columns.findIndex((c) => c.name === column);
    const data = gridApi?.getDisplayedRowAtIndex(row)?.data;
    if (data && index >= 0 && !truncated.has(`${row}:${index}`)) return data[index];
    return (await api<{ value: unknown }>(`${grid}/cell${query({ row, column, upto: state.previewUpto })}`)).value;
  }

  /** The distinct values the selection holds in `column`, from the loaded rows (for the Cell
   * tools: keep / exclude these values). Capped, since a filter list should stay short. */
  function selectedValues(column: string, cap = 500): unknown[] {
    const index = columns.findIndex((c) => c.name === column);
    if (index < 0 || !gridApi) return [];
    const seen = new Map<string, unknown>();
    const [first, last] = selection.kind === "cells" ? [selection.rows[0]![0], selection.rows[0]![1]] : [-1, -1];
    for (let r = first; r <= last && seen.size <= cap; r += 1) {
      const data = gridApi.getDisplayedRowAtIndex(r)?.data;
      if (data) seen.set(JSON.stringify(data[index]), data[index]);
    }
    return [...seen.values()];
  }

  /** Open the value viewer on the selected cell (the block's first cell, or the active one). */
  function viewCell(): void {
    const cell = selection.kind === "cells" ? { row: selection.rows[0]![0], column: selection.columns[0]! } : focusedCell();
    if (cell) void showCellDetail(cell.row, cell.column);
  }

  /** The view's columns (name and type), for the toolbar. */
  function viewColumns(): ColumnInfo[] {
    return columns;
  }

  async function showCellDetail(row: number, column: string): Promise<void> {
    try {
      const cell = await api<{ value: unknown }>(`${grid}/cell${query({ row, column, upto: state.previewUpto })}`);
      openValueViewer(`${column}, row ${formatCount(row + 1)}`, cell.value);
    } catch (error) {
      toast(String((error as Error).message), "error");
    }
  }

  /** Size the filler so (scrollable width - viewport) ends exactly on a column edge. */
  function fitFiller(): void {
    if (!gridApi) return;
    const range = gridApi.getHorizontalPixelRange();
    const viewport = range.right - range.left;
    const columns = gridApi.getAllDisplayedColumns().filter((c) => !c.getPinned() && c.getColId() !== FILLER);
    const total = columns.reduce((sum, c) => sum + c.getActualWidth(), 0);
    let width = 0;
    if (total > viewport) {
      let trailing = 0;
      for (const column of [...columns].reverse()) {
        if (trailing + column.getActualWidth() > viewport) break;
        trailing += column.getActualWidth();
      }
      width = Math.max(0, Math.floor(viewport - trailing));
    }
    const filler = gridApi.getColumn(FILLER);
    if (filler && Math.abs(filler.getActualWidth() - width) > 1) gridApi.setColumnWidths([{ key: FILLER, newWidth: width }]);
  }

  function displayedRange(): [number, number] {
    if (!gridApi) return [0, 0];
    // Rows on screen, not the rows rendered ahead of the scroll position: measured from a drawn
    // row, since past the browser's tallest scroll area AG Grid "stretches" the scroll (one
    // pixel of scrollbar is then more than one pixel of rows) and scrollTop / rowHeight drifts.
    const viewport = host.querySelector<HTMLElement>(".ag-grid-viewport");
    const drawn = host.querySelector<HTMLElement>(".ag-row[row-index]");
    if (viewport && drawn && shownTotal > 0) {
      const box = viewport.getBoundingClientRect();
      const at = drawn.getBoundingClientRect();
      if (box.height > 0 && at.height > 0) {
        const top = Math.max(box.top, host.querySelector(".ag-header")?.getBoundingClientRect().bottom ?? box.top);
        // An overlay scrollbar strip (grid.css) covers the bottom of the viewport.
        const strip = host.querySelector(".ag-body-horizontal-scroll.ag-scrollbar-invisible")?.getBoundingClientRect();
        const bottom = Math.min(box.top + viewport.clientTop + viewport.clientHeight, strip?.height ? strip.top : Infinity);
        const index = Number(drawn.getAttribute("row-index"));
        // A row counts as shown when its middle is on screen.
        const clamp = (n: number): number => Math.max(0, Math.min(shownTotal - 1, n));
        const first = clamp(index + Math.ceil((top - at.top) / at.height - 0.5));
        const last = clamp(index + Math.floor((bottom - at.top) / at.height - 0.5));
        return [first, Math.max(first, last)];
      }
    }
    const range = gridApi.getVerticalPixelRange();
    const row = gridApi.getDisplayedRowAtIndex(gridApi.getFirstDisplayedRowIndex());
    const height = row?.rowHeight ?? 32;
    const first = Math.floor(range.top / height);
    const last = Math.max(first, Math.floor((range.bottom - 1) / height));
    return [first, Math.min(last, gridApi.getLastDisplayedRowIndex())];
  }

  build();
  return {
    state,
    selection,
    host,
    refresh,
    syncHeaderSort,
    copySelection,
    focusedCell,
    cellValue,
    selectedValues,
    viewCell,
    viewColumns,
    displayedRange,
    shownTotal: () => shownTotal,
    autoSizeColumns: (names) => {
      // Only the data columns: the row numbers and the filler keep their fixed widths.
      const ids = names ?? columns.map((c) => c.name);
      gridApi?.autoSizeColumns(ids, false);
    },
    setCompare: (next) => {
      compare = next;
      keyRows = null;
      placeholders.clear();
      diffs.clear();
      truncated.clear();
      selection.clear();
      gridApi?.purgeInfiniteCache();
    },
    onRowPick: (listener) => pickListeners.push(listener),
    setKeyRows: (rows) => {
      keyRows = rows;
      gridApi?.redrawRows();
    },
    destroy: () => {
      destroyed = true;
      for (const dispose of disposers.splice(0)) dispose();
      stopDrag();
      window.clearTimeout(statsTimer);
      pickListeners.length = 0;
      headerMarks.clear();
      gridApi?.destroy();
      gridApi = null;
    },
  };
}

let measure: CanvasRenderingContext2D | null = null;

/** The row-number column's width for a view of `total` rows: the widest number plus padding. */
function rowNumberWidth(total: number): number {
  const zoom = gridZoom();
  measure ??= document.createElement("canvas").getContext("2d");
  if (!measure) return Math.round(64 * zoom);
  measure.font = `${Math.round(13 * zoom)}px "Segoe UI", system-ui, sans-serif`;
  const text = measure.measureText(formatCount(Math.max(1, total))).width;
  return Math.max(Math.round(64 * zoom), Math.ceil(text + 28 * zoom));
}

// ===== the active view: what the panels and the ribbon act on =====

let active: GridView | null = null;

/** The active grid's selection (a live binding: it changes when another grid becomes active). */
export let selection = new GridSelection();

export function setActiveView(view: GridView): void {
  active = view;
  selection = view.selection;
}

export function activeView(): GridView | null {
  return active;
}

export const copySelection = (): Promise<void> => (active ? active.copySelection() : Promise.resolve());
export const focusedCell = (): { row: number; column: string } | null => active?.focusedCell() ?? null;
export const cellValue = (row: number, column: string): Promise<unknown> => (active ? active.cellValue(row, column) : Promise.resolve(undefined));
export const selectedValues = (column: string, cap?: number): unknown[] => active?.selectedValues(column, cap) ?? [];
export const viewCell = (): void => active?.viewCell();
export const viewColumns = (): ColumnInfo[] => active?.viewColumns() ?? [];
export const displayedRange = (): [number, number] => active?.displayedRange() ?? [0, 0];
export const shownTotal = (): number => active?.shownTotal() ?? 0;
export const autoSizeColumns = (names?: string[]): void => active?.autoSizeColumns(names);
