"""A materialized view's positions are its rowids (engine/views.py): the table is created from the view's
ordered query, and DuckDB keeps that order. These tests use enough rows that DuckDB builds the table on
several threads at once, which is where a lost order would show."""

from __future__ import annotations

import pyarrow as pa
import pyarrow.compute as pc

from swissarmygrid.engine.database import open_database
from swissarmygrid.engine.datasets import DatasetStore
from swissarmygrid.engine.steps import ViewSettings, steps_from_json
from swissarmygrid.engine.views import POS, ViewEngine

ROWS = 600_000


def engine_with_table() -> tuple[ViewEngine, DatasetStore, object]:
    connection = open_database("2GiB")
    store = DatasetStore(connection, 10**9, 10**9, 50)
    i = pa.array(range(ROWS), pa.int64())
    # A shuffled key: (i * 7919) mod ROWS visits every value once (7919 is prime and does not divide
    # ROWS), far from table order.
    key = pa.array([(n * 7919) % ROWS for n in range(ROWS)], pa.int64())
    table = pa.table({"id": i, "key": key, "group": pc.bit_wise_and(i, 7)})
    store.replace("big", table, "arrow")
    return ViewEngine(connection, 10**9), store, connection


def test_sorted_view_positions_follow_the_sort_on_every_window() -> None:
    engine, store, connection = engine_with_table()
    info = store.get("big")
    assert info is not None
    steps = steps_from_json([{"kind": "sort", "params": {"keys": [["key", "desc"]]}}])
    cursor = connection.cursor()  # type: ignore[attr-defined]
    settings = ViewSettings()
    for start in (0, 1_000, 299_950, 599_800):
        window = engine.window(cursor, info, steps, settings, start, start + 200)
        keys = [row[1] for row in window.rows]
        assert keys == list(range(ROWS - 1 - start, ROWS - 1 - start - len(keys), -1))


def test_filtered_view_keeps_table_order_and_positions_are_dense() -> None:
    engine, store, connection = engine_with_table()
    info = store.get("big")
    assert info is not None
    steps = steps_from_json([{"kind": "filter", "params": {"column": "group", "op": "eq", "value": 3}}])
    cursor = connection.cursor()  # type: ignore[attr-defined]
    settings = ViewSettings()
    sql, _ = engine.positioned_sql(cursor, info, steps, settings)
    rows = cursor.execute(f"SELECT {POS}, id FROM ({sql}) ORDER BY {POS}").fetchall()  # noqa: S608 (the engine's own SQL)
    assert [r[0] for r in rows] == list(range(ROWS // 8))
    assert [r[1] for r in rows] == list(range(3, ROWS, 8))
