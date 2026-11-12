"""Views: a dataset run through a grid's steps, and the windows of rows a grid scrolls through.

Scrolling must stay fast (plan section 14: a window in under 300 ms, a re-sort in under 500 ms
on 1,000,000 rows). A sort step means "renumber every row", which is too slow to redo for
every 200-row window. So the first window of a new step list materializes the whole view
once into a cache table with a dense position column; every later window is a range read on
that position. With no steps at all the dataset table is read directly by rowid, which DuckDB
keeps dense (0..n-1, checked in docs/verified.md).

The cache is a size-capped LRU: entries are derived data, so evicting one costs only a
recompute (plan section 6a: caches "may be evicted freely"). An entry dies when its dataset
is re-pushed or closed.

Think of ViewEngine as a query service over the DatasetStore repository: it never changes a
dataset, only reads it.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import duckdb

from swissarmygrid.engine.datasets import Column, DatasetInfo, quote
from swissarmygrid.engine.steps import CompiledView, Step, ViewSettings, compile_view

POS: str = "__sag_pos"


@dataclass(frozen=True)
class Window:
    rows: list[list[Any]]
    row_count: int
    columns: tuple[Column, ...]


@dataclass
class _Cached:
    table: str
    row_count: int
    nbytes: int
    dataset: str
    # Queries reading the table right now; an evicted entry's table is dropped by the last one.
    readers: int = 0
    evicted: bool = False


def view_key(info: DatasetInfo, steps: list[Step], settings: ViewSettings, upto: int | None) -> str:
    payload = json.dumps(
        {
            "d": info.name,
            "v": info.version,
            "s": [s.to_json() | {"id": ""} for s in steps],
            "o": settings.to_json(),
            "u": upto,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


class ViewEngine:
    def __init__(self, connection: duckdb.DuckDBPyConnection, cache_bytes: int) -> None:
        self._connection = connection
        self._cache_bytes = cache_bytes
        self._compiled: OrderedDict[str, CompiledView] = OrderedDict()
        self._tables: OrderedDict[str, _Cached] = OrderedDict()
        self._lock = threading.Lock()
        # One lock per cache key, so two windows of a new view materialize it only once.
        self._key_locks: dict[str, threading.Lock] = {}

    # ===== compile =====

    def compile(
        self,
        cursor: duckdb.DuckDBPyConnection,
        info: DatasetInfo,
        steps: list[Step],
        settings: ViewSettings,
    ) -> CompiledView:
        key = view_key(info, steps, settings, None)
        with self._lock:
            hit = self._compiled.get(key)
            if hit is not None:
                self._compiled.move_to_end(key)
                return hit
        compiled = compile_view(cursor, info.table, info.columns, steps, settings, info.name)
        with self._lock:
            self._compiled[key] = compiled
            while len(self._compiled) > 256:
                self._compiled.popitem(last=False)
        return compiled

    # ===== windows =====

    def window(
        self,
        cursor: duckdb.DuckDBPyConnection,
        info: DatasetInfo,
        steps: list[Step],
        settings: ViewSettings,
        start: int,
        end: int,
        upto: int | None = None,
    ) -> Window:
        compiled = self.compile(cursor, info, steps, settings)
        names = ", ".join(quote(c.name) for c in compiled.columns_at(upto))
        applied = compiled.applied(upto)
        if applied == 0:
            rows = cursor.execute(
                f"SELECT {names} FROM {info.table} WHERE rowid >= ? AND rowid < ? ORDER BY rowid",
                [start, end],
            ).fetchall()
            return Window([list(r) for r in rows], info.row_count, compiled.columns_at(upto))
        cached = self._materialize(cursor, info, compiled, steps, settings, upto)
        try:
            rows = cursor.execute(
                f"SELECT {names} FROM {cached.table} WHERE rowid >= ? AND rowid < ? ORDER BY rowid",
                [start, end],
            ).fetchall()
        finally:
            self._release(cursor, cached)
        return Window([list(r) for r in rows], cached.row_count, compiled.columns_at(upto))

    def row_count(
        self,
        cursor: duckdb.DuckDBPyConnection,
        info: DatasetInfo,
        steps: list[Step],
        settings: ViewSettings,
        upto: int | None = None,
    ) -> int:
        compiled = self.compile(cursor, info, steps, settings)
        if compiled.applied(upto) == 0:
            return info.row_count
        cached = self._materialize(cursor, info, compiled, steps, settings, upto)
        self._release(cursor, cached)
        return cached.row_count

    def positioned_sql(
        self,
        cursor: duckdb.DuckDBPyConnection,
        info: DatasetInfo,
        steps: list[Step],
        settings: ViewSettings,
        upto: int | None = None,
    ) -> tuple[str, tuple[Column, ...]]:
        """A relation exposing the view's columns plus __sag_pos (0-based, in view order), for
        exports and selected-row scopes. Uses the cache, so it is cheap after a scroll.

        The relation names a cache table, which is not leased past this call: run it right
        away (it is the most recently used entry, so only a cache over its cap twice over by
        newer views can evict it before the caller's query starts)."""
        compiled = self.compile(cursor, info, steps, settings)
        columns = compiled.columns_at(upto)
        names = ", ".join(quote(c.name) for c in columns)
        if compiled.applied(upto) == 0:
            return f"SELECT rowid AS {POS}, {names} FROM {info.table}", columns
        cached = self._materialize(cursor, info, compiled, steps, settings, upto)
        self._release(cursor, cached)
        return f"SELECT rowid AS {POS}, {names} FROM {cached.table}", columns

    def step_counts(
        self,
        cursor: duckdb.DuckDBPyConnection,
        info: DatasetInfo,
        steps: list[Step],
        settings: ViewSettings,
    ) -> list[int]:
        """Rows after each stage: [source, after step 1, ...] for "12,000 -> 11,420"."""
        compiled = self.compile(cursor, info, steps, settings)
        with_clause = ", ".join(f"{n} AS ({b})" for n, b in compiled.ctes)
        selects = ", ".join(f"(SELECT count(*) FROM {stage.name})" for stage in compiled.stages)
        row = cursor.execute(f"WITH {with_clause} SELECT {selects}").fetchone()
        return [int(v) for v in row] if row else []

    def cell(
        self,
        cursor: duckdb.DuckDBPyConnection,
        info: DatasetInfo,
        steps: list[Step],
        settings: ViewSettings,
        row: int,
        column: str,
    ) -> Any:
        """One full, untruncated value: the detail view behind a truncated cell."""
        compiled = self.compile(cursor, info, steps, settings)
        if column not in {c.name for c in compiled.columns}:
            raise KeyError(column)
        window = self.window(cursor, info, steps, settings, row, row + 1)
        if not window.rows:
            raise IndexError(row)
        index = [c.name for c in window.columns].index(column)
        return window.rows[0][index]

    # ===== cache =====

    def _materialize(
        self,
        cursor: duckdb.DuckDBPyConnection,
        info: DatasetInfo,
        compiled: CompiledView,
        steps: list[Step],
        settings: ViewSettings,
        upto: int | None,
    ) -> _Cached:
        """The cache entry for the view at `upto`, leased: the caller must `_release` it once its
        query has started (DuckDB keeps a table a running query reads, even if it is dropped)."""
        key = view_key(info, steps, settings, compiled.applied(upto))
        with self._lock:
            hit = self._lease(key)
            if hit is not None:
                return hit
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            with self._lock:
                hit = self._lease(key)
                if hit is not None:
                    return hit
            # A fresh name per build: if the entry was evicted while another thread waited on an
            # older lock for this key, two builds can run at once, and one name would make them
            # a catalog write-write conflict.
            table = quote(f"vc_{key}_{secrets.token_hex(4)}")
            # The view's query ends ORDER BY its order column, and a table created from an ordered
            # query keeps that order, so each row's rowid (dense from 0, docs/verified.md) is its
            # position in the view. Numbering the rows with row_number() instead meant a second full
            # sort of the view: on 1,000,000 rows it took a sort step from about 450 ms to over 700.
            cursor.execute(f"CREATE TABLE {table} AS {compiled.sql(upto)}")
            count_row = cursor.execute(f"SELECT count(*) FROM {table}").fetchone()
            count = int(count_row[0]) if count_row else 0
            # Estimate: the dataset's bytes scaled by the share of rows the view keeps.
            share = count / info.row_count if info.row_count else 0.0
            entry = _Cached(table, count, int(info.nbytes * max(share, 0.01)), info.name)
            with self._lock:
                if self._key_locks.get(key) is key_lock:
                    self._key_locks.pop(key)
                won = self._lease(key)
                if won is None:
                    entry.readers = 1
                    self._tables[key] = entry
                    doomed = self._evict_over_cap(keep=key)
                else:  # another build of this key finished first: use it, drop this one
                    doomed = [entry.table]
                    entry = won
        self._drop(cursor, doomed)
        return entry

    def _lease(self, key: str) -> _Cached | None:
        """With self._lock held: the entry for `key`, marked as being read, or None."""
        hit = self._tables.get(key)
        if hit is not None:
            self._tables.move_to_end(key)
            hit.readers += 1
        return hit

    def _release(self, cursor: duckdb.DuckDBPyConnection, entry: _Cached) -> None:
        with self._lock:
            entry.readers -= 1
            doomed = [entry.table] if entry.evicted and entry.readers == 0 else []
        self._drop(cursor, doomed)

    def _evict_over_cap(self, keep: str) -> list[str]:
        """With self._lock held: take the oldest entries out until the cache fits its cap.
        Returns the tables to drop now; one still being read is dropped by its last reader."""
        total = sum(e.nbytes for e in self._tables.values())
        doomed: list[str] = []
        for key in list(self._tables):
            if total <= self._cache_bytes:
                break
            if key == keep:
                continue
            entry = self._tables.pop(key)
            total -= entry.nbytes
            doomed.extend(self._retire(entry))
        return doomed

    @staticmethod
    def _retire(entry: _Cached) -> list[str]:
        entry.evicted = True
        return [entry.table] if entry.readers == 0 else []

    @staticmethod
    def _drop(cursor: duckdb.DuckDBPyConnection, tables: list[str]) -> None:
        for table in tables:
            cursor.execute(f"DROP TABLE IF EXISTS {table}")

    def forget_dataset(self, name: str) -> None:
        """Drop every cache entry built from dataset `name` (re-pushed or closed)."""
        with self._lock:
            doomed: list[str] = []
            for key in [k for k, e in self._tables.items() if e.dataset == name]:
                doomed.extend(self._retire(self._tables.pop(key)))
        self._drop(self._connection.cursor(), doomed)
