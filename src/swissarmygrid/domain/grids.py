"""Grids: views of a dataset, each with its own address /g/<id> (plan section 4, "Grid").

A grid owns its step list and toolbar settings; many grids can share one dataset. Each page
showing the grid has its own request generation (`gen`, plan section 5.2), keyed by a client id
the page picks at random: the page bumps gen whenever its view changes, and a request carrying
an older gen from the same page is stale, so the server drops it before it runs and interrupts
it if it is already running. Rapid typing in a filter box therefore never queues more than one
live query per page (plan section 14). Generations are per page, not per grid: two tabs on one
grid must never make each other's requests look stale.

Also here, because they are the grid's own shapes: a window of rows, the position column and the
key of a view (what the view cache, adapters/views.py, is keyed by), and a selection as the
browser sends it, turned into SQL text.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

from swissarmygrid.domain.datasets import Column, DatasetInfo
from swissarmygrid.domain.steps import Step, ViewSettings, quote, type_family

# The view's position column: 0-based, in view order.
POS: str = "__sag_pos"


@dataclass(frozen=True)
class Window:
    rows: list[list[Any]]
    row_count: int
    columns: tuple[Column, ...]


def view_key(info: DatasetInfo, steps: list[Step], settings: ViewSettings, upto: int | None) -> str:
    payload = json.dumps(
        {
            "d": info.name,
            "v": info.version,
            "n": info.serial,
            "s": [s.to_json() | {"id": ""} for s in steps],
            "o": settings.to_json(),
            "u": upto,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


class Interruptible(Protocol):
    """A running query that can be told to stop (a DuckDB cursor is one)."""

    def interrupt(self) -> None: ...


class GridLimitError(ValueError):
    """Too many grids are open. Maps to HTTP 409."""


class StaleRequestError(Exception):
    """A newer request for the same grid replaced this one. Maps to HTTP 409."""


@dataclass
class Grid:
    id: str
    dataset: str
    steps: list[Step] = field(default_factory=list)
    settings: ViewSettings = field(default_factory=ViewSettings)
    gen: int = 0  # the highest gen seen from any page (informational)
    _gens: dict[str, int] = field(default_factory=dict)  # client id -> latest gen
    # Rows to mark (agent or user), as filter conditions: [{"column", "op", ..., "label"}].
    highlights: list[dict[str, Any]] = field(default_factory=list)
    # How many rows the user has selected (agents may see the count, never the values).
    selected: int = 0
    # Cursors running a query for this grid, with the gen they serve.
    _running: dict[int, tuple[str, int, Interruptible]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def admit(self, gen: int | None, cursor: Interruptible, client: str = "") -> int:
        """Register a query. A newer gen from a page interrupts that page's older running
        queries; an older gen from the same page is refused."""
        with self._lock:
            latest = self._gens.get(client, 0)
            if gen is not None and gen < latest:
                raise StaleRequestError()
            if gen is not None and gen > latest:
                self._gens[client] = gen
                self.gen = max(self.gen, gen)
                for old_client, old_gen, old_cursor in list(self._running.values()):
                    if old_client == client and old_gen < gen:
                        old_cursor.interrupt()
            ticket = id(cursor)
            self._running[ticket] = (client, gen if gen is not None else latest, cursor)
            return ticket

    def is_stale(self, gen: int | None, client: str = "") -> bool:
        return gen is not None and gen < self._gens.get(client, 0)

    def interrupt_all(self) -> int:
        """The SQL box's Cancel: stop every query running for this grid."""
        with self._lock:
            running = list(self._running.values())
        for _, _, cursor in running:
            cursor.interrupt()
        return len(running)

    def release(self, ticket: int) -> None:
        with self._lock:
            self._running.pop(ticket, None)


class GridRegistry:
    """In-memory map of grid id to grid."""

    def __init__(self, max_grids: int) -> None:
        self._grids: dict[str, Grid] = {}
        self._max_grids = max_grids

    def create(
        self, dataset: str, steps: list[Step] | None = None, settings: ViewSettings | None = None
    ) -> Grid:
        if len(self._grids) >= self._max_grids:
            raise GridLimitError(f"{len(self._grids)} grids are open, the most allowed. Close one first.")
        grid = Grid(
            id=secrets.token_hex(4),
            dataset=dataset,
            steps=list(steps or []),
            settings=settings or ViewSettings(),
        )
        self._grids[grid.id] = grid
        return grid

    def get(self, grid_id: str) -> Grid | None:
        return self._grids.get(grid_id)

    def first_for(self, dataset: str) -> Grid | None:
        """The oldest grid on `dataset`, which a plain re-push reuses."""
        return next((grid for grid in self._grids.values() if grid.dataset == dataset), None)

    def for_dataset(self, dataset: str) -> list[Grid]:
        return [g for g in self._grids.values() if g.dataset == dataset]

    def close(self, grid_id: str) -> Grid | None:
        return self._grids.pop(grid_id, None)

    def rename_dataset(self, old: str, new: str) -> list[Grid]:
        """Every grid on dataset `old` now shows `new` (the table was renamed): its steps and settings stay."""
        moved = self.for_dataset(old)
        for grid in moved:
            grid.dataset = new
        return moved

    def all(self) -> list[Grid]:
        return list(self._grids.values())


# ===== A grid selection, as the browser sends it, turned into SQL (Excel-style selection) =====
#
# The browser selects the way Excel does: whole rows (click or drag the row numbers), whole
# columns (click or drag the headers), a block of cells (drag across cells), or everything
# (the corner, or Ctrl+A). Every kind fits one shape:
#
#     {"rows": [[first, last], ...] or null, "columns": ["name", ...] or null}
#
# Row numbers are view positions (0-based, inclusive ranges); null means every row or every
# column. So a row selection has columns null, a column selection has rows null, a block of
# cells has both, and select-all has neither. The selection can cover rows the browser never
# loaded, which is why stats, copy and export run here instead of in the page.

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
