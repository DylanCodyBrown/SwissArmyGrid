/**
 * selection.ts: Excel-style selection for one grid. [STATE + FUNCTION]
 *
 * What the user has selected, the way Excel thinks about it:
 *   rows     click or drag the row numbers at the left (Shift extends, Ctrl adds)
 *   columns  click or drag the column headers (Shift extends, Ctrl adds)
 *   cells    drag across cells, or Shift+click / Shift+arrow from the active cell
 *   all      the corner above the row numbers, or Ctrl+A
 *
 * Rows are view positions, so a selection can cover rows the browser never loaded; the
 * server computes stats, copy and export from `descriptor()` (server/selection.py). Think of
 * this class as the view-model of a WPF DataGrid's SelectedCells, minus the cells themselves.
 * One instance per grid, so two grids side by side keep separate selections.
 */

export type SelectionKind = "none" | "rows" | "columns" | "cells" | "all";
type Range = [number, number]; // inclusive, first <= last

export interface SelectionDescriptor {
  rows: Range[] | null;
  columns: string[] | null;
}

export class GridSelection {
  kind: SelectionKind = "none";
  /** Row ranges (rows and cells kinds). */
  rows: Range[] = [];
  /** Column names (columns and cells kinds), in view order. */
  columns: string[] = [];
  /** Where a Shift+click or a drag extends from: a row position and a column index. */
  anchor: { row: number; col: number } | null = null;
  private order: string[] = [];
  private listeners: (() => void)[] = [];

  /** The view's column names, in order (for column ranges and cell blocks). */
  setColumnOrder(names: string[]): void {
    this.order = names;
  }

  onChange(listener: () => void): void {
    this.listeners.push(listener);
  }

  private changed(): void {
    for (const listener of this.listeners) listener();
  }

  clear(): void {
    if (this.kind === "none") return;
    this.kind = "none";
    this.rows = [];
    this.columns = [];
    this.anchor = null;
    this.changed();
  }

  selectAll(): void {
    this.kind = "all";
    this.rows = [];
    this.columns = [];
    this.changed();
  }

  /** Row numbers: `add` keeps earlier ranges (Ctrl); `extend` grows from the anchor (Shift). */
  selectRows(row: number, mode: "replace" | "add" | "extend"): void {
    if (mode === "extend" && this.kind === "rows" && this.anchor) {
      const span = sorted([this.anchor.row, row]);
      this.rows = [...this.rows.slice(0, -1), span];
    } else {
      this.rows = mode === "add" && this.kind === "rows" ? [...this.rows, [row, row]] : [[row, row]];
      this.anchor = { row, col: 0 };
    }
    this.kind = "rows";
    this.columns = [];
    this.changed();
  }

  selectColumns(name: string, mode: "replace" | "add" | "extend"): void {
    const at = this.order.indexOf(name);
    if (at < 0) return;
    if (mode === "extend" && this.kind === "columns" && this.anchor) {
      const [a, b] = sorted([this.anchor.col, at]);
      this.columns = this.order.slice(a, b + 1);
    } else if (mode === "add" && this.kind === "columns") {
      this.columns = this.columns.includes(name) ? this.columns.filter((c) => c !== name) : [...this.columns, name];
      this.columns = this.order.filter((c) => this.columns.includes(c));
      this.anchor = { row: 0, col: at };
    } else {
      this.columns = [name];
      this.anchor = { row: 0, col: at };
    }
    this.kind = "columns";
    this.rows = [];
    this.changed();
  }

  /** A block of cells from the anchor (or this cell, starting fresh) to `row`, `name`. */
  selectCells(row: number, name: string, extend: boolean): void {
    const col = this.order.indexOf(name);
    if (col < 0) return;
    if (!extend || !this.anchor || this.kind !== "cells") this.anchor = { row, col };
    const [r0, r1] = sorted([this.anchor.row, row]);
    const [c0, c1] = sorted([this.anchor.col, col]);
    this.kind = "cells";
    this.rows = [[r0, r1]];
    this.columns = this.order.slice(c0, c1 + 1);
    this.changed();
  }

  hasRow(row: number): boolean {
    if (this.kind === "all") return true;
    return this.kind === "rows" && this.rows.some(([a, b]) => row >= a && row <= b);
  }

  hasColumn(name: string): boolean {
    return this.kind === "all" || (this.kind === "columns" && this.columns.includes(name));
  }

  hasCell(row: number, name: string): boolean {
    if (this.kind === "all") return true;
    if (this.kind === "rows") return this.hasRow(row);
    if (this.kind === "columns") return this.columns.includes(name);
    if (this.kind === "cells") return this.columns.includes(name) && this.rows.some(([a, b]) => row >= a && row <= b);
    return false;
  }

  /** The shape the server reads; null means every row or every column. */
  descriptor(): SelectionDescriptor | null {
    switch (this.kind) {
      case "none":
        return null;
      case "all":
        return { rows: null, columns: null };
      case "rows":
        return { rows: merge(this.rows), columns: null };
      case "columns":
        return { rows: null, columns: this.columns };
      case "cells":
        return { rows: merge(this.rows), columns: this.columns };
    }
  }

  /** How many rows the selection touches, given the view's row count. */
  rowCount(total: number): number {
    if (this.kind === "none") return 0;
    if (this.kind === "all" || this.kind === "columns") return total;
    return merge(this.rows).reduce((n, [a, b]) => n + Math.max(0, Math.min(b, total - 1) - a + 1), 0);
  }

  /** The columns the Column tools act on, or [] when the selection picks none. */
  targetColumns(): string[] {
    return this.kind === "columns" || this.kind === "cells" ? this.columns : [];
  }
}

function sorted([a, b]: Range): Range {
  return a <= b ? [a, b] : [b, a];
}

/** Overlapping or touching ranges joined, in order. */
export function merge(ranges: Range[]): Range[] {
  const out: Range[] = [];
  for (const [a, b] of [...ranges].sort((x, y) => x[0] - y[0])) {
    const last = out[out.length - 1];
    if (last && a <= last[1] + 1) last[1] = Math.max(last[1], b);
    else out.push([a, b]);
  }
  return out;
}
