"""A grid selection, as the browser sends it, turned into SQL (Excel-style selection).

The browser selects the way Excel does: whole rows (click or drag the row numbers), whole
columns (click or drag the headers), a block of cells (drag across cells), or everything
(the corner, or Ctrl+A). Every kind fits one shape:

    {"rows": [[first, last], ...] or null, "columns": ["name", ...] or null}

Row numbers are view positions (0-based, inclusive ranges); null means every row or every
column. So a row selection has columns null, a column selection has rows null, a block of
cells has both, and select-all has neither. The selection can cover rows the browser never
loaded, which is why stats, copy and export run here instead of in the page.
"""

from __future__ import annotations

from typing import Any

from swissarmygrid.engine.datasets import Column
from swissarmygrid.engine.steps import quote, type_family
from swissarmygrid.engine.views import POS

MAX_RANGES: int = 10_000


def selection_scope(raw: Any, columns: tuple[Column, ...]) -> tuple[str, tuple[Column, ...]]:
    """(WHERE clause over the positioned view, the selected columns in view order)."""
    if not isinstance(raw, dict) or not set(raw) <= {"rows", "columns"}:
        raise ValueError('A selection is {"rows": [[first, last], ...] | null, "columns": [...] | null}.')
    rows, names = raw.get("rows"), raw.get("columns")
    where = ""
    if rows is not None:
        if not isinstance(rows, list) or not rows or len(rows) > MAX_RANGES:
            raise ValueError(f"rows must be 1 to {MAX_RANGES:,} [first, last] ranges, or null.")
        parts: list[str] = []
        for pair in rows:
            if not (
                isinstance(pair, list)
                and len(pair) == 2
                and all(isinstance(n, int) and not isinstance(n, bool) for n in pair)
                and 0 <= pair[0] <= pair[1]
            ):
                raise ValueError("Each row range is [first, last] with 0 <= first <= last.")
            parts.append(f"{POS} BETWEEN {pair[0]} AND {pair[1]}")
        where = "WHERE " + " OR ".join(parts)
    chosen = columns
    if names is not None:
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            raise ValueError("columns must be a list of column names, or null.")
        unknown = set(names) - {c.name for c in columns}
        if unknown:
            raise ValueError(f"Not columns of this view: {sorted(unknown)}.")
        chosen = tuple(c for c in columns if c.name in names)
    return where, chosen


def stats_sql(positioned: str, where: str, columns: tuple[Column, ...]) -> tuple[str, list[str]]:
    """One query for the status bar: rows, then per column its non-blank count, and for number
    columns also sum, count, min and max. Returns (sql, the number columns' names)."""
    parts = ["count(*)"]
    numeric: list[str] = []
    for column in columns:
        q = quote(column.name)
        parts.append(f"count({q})")
        if type_family(column.type) == "number":
            numeric.append(column.name)
            parts += [f"sum({q})::DOUBLE", f"min({q})::DOUBLE", f"max({q})::DOUBLE"]
    # Names are quoted identifiers; positions are validated ints.
    return f"SELECT {', '.join(parts)} FROM ({positioned}) {where}", numeric  # noqa: S608


def combine_stats(row: tuple[Any, ...], columns: tuple[Column, ...]) -> dict[str, Any]:
    """Excel's status bar numbers: Count = non-blank cells; Sum, Average, Min, Max over the
    number cells."""
    rows = int(row[0])
    at = 1
    count = numbers = 0
    total = 0.0
    low: float | None = None
    high: float | None = None
    for column in columns:
        filled = int(row[at])
        count += filled
        at += 1
        if type_family(column.type) == "number":
            s, lo, hi = row[at], row[at + 1], row[at + 2]
            at += 3
            numbers += filled
            if s is not None:
                total += float(s)
                low = lo if low is None else min(low, lo)
                high = hi if high is None else max(high, hi)
    return {
        "rows": rows,
        "cells": rows * len(columns),
        "count": count,
        "numeric": numbers,
        "sum": total if numbers else None,
        "mean": total / numbers if numbers else None,
        "min": low,
        "max": high,
    }
