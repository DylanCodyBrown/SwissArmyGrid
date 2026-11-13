"""The view cache under pressure: eviction while another thread reads, and rebuilds of an
evicted view racing each other (the worker pool runs four threads). Made-up data only."""

from __future__ import annotations

import random
import threading
from collections import Counter

import pyarrow as pa

from swissarmygrid.adapters.database import open_database
from swissarmygrid.adapters.datasets import DatasetStore
from swissarmygrid.adapters.views import ViewEngine
from swissarmygrid.domain.steps import Step, ViewSettings

ROWS = 20_000


def _store() -> DatasetStore:
    store = DatasetStore(open_database("512MiB"), 1 << 28, 1 << 28, 10)
    store.replace("t", pa.table({"x": list(range(ROWS)), "y": [str(i % 97) for i in range(ROWS)]}), "arrow")
    return store


def _steps(k: int) -> list[Step]:
    return [Step("filter", {"column": "x", "op": "gt", "value": k}), Step("sort", {"keys": [["y", "desc"]]})]


def test_an_entry_being_read_is_not_dropped_by_eviction() -> None:
    store = _store()
    info = store.get("t")
    assert info is not None
    views = ViewEngine(store.cursor(), int(info.nbytes * 1.5))  # room for one view only
    cursor = store.cursor()
    first = views.compile(cursor, info, _steps(1), ViewSettings())
    leased = views._materialize(cursor, info, first, _steps(1), ViewSettings(), None)
    # A second view evicts the first while it is still being read.
    views.window(cursor, info, _steps(2), ViewSettings(), 0, 10)
    assert cursor.execute(f"SELECT count(*) FROM {leased.table}").fetchone() == (ROWS - 2,)  # noqa: S608
    views._release(cursor, leased)
    tables = {r[0] for r in cursor.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    assert leased.table.strip('"') not in tables  # dropped by its last reader


def test_windows_from_four_threads_never_fail_while_the_cache_churns() -> None:
    store = _store()
    info = store.get("t")
    assert info is not None
    views = ViewEngine(store.cursor(), int(info.nbytes * 1.5))
    errors: Counter[str] = Counter()

    def worker(seed: int) -> None:
        chooser = random.Random(seed)  # noqa: S311 (a test schedule, not a secret)
        cursor = store.cursor()
        for _ in range(120):
            k = chooser.randrange(6)
            try:
                window = views.window(cursor, info, _steps(k), ViewSettings(), 0, 20)
                assert window.row_count == ROWS - 1 - k
                assert len(window.rows) == 20
            except Exception as error:  # collected, so the test reports every kind
                errors[f"{type(error).__name__}: {str(error)[:80]}"] += 1

    threads = [threading.Thread(target=worker, args=(seed,)) for seed in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # Was: "Table vc_... does not exist" (evicted between lookup and read) and "Catalog
    # write-write conflict" (two rebuilds of one evicted view).
    assert not errors, errors
    # Nothing leaks: only the entries still cached have tables.
    tables = {r[0] for r in store.cursor().execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    cached = {e.table.strip('"') for e in views._tables.values()}
    assert {t for t in tables if t.startswith("vc_")} == cached


def test_reading_gives_the_view_rows_in_order_and_holds_them_through_eviction() -> None:
    store = _store()
    info = store.get("t")
    assert info is not None
    views = ViewEngine(store.cursor(), int(info.nbytes * 1.5))
    cursor = store.cursor()
    settings = ViewSettings()
    expected = cursor.execute(views.compile(cursor, info, _steps(3), settings).sql()).fetchall()
    with views.reading(cursor, info, _steps(3), settings, ordered=True) as ordered:
        assert "vc_" in ordered
        views.window(cursor, info, _steps(4), settings, 0, 10)  # evicts the entry being read
        assert cursor.execute(ordered).fetchall() == expected
    raw = cursor.execute(views.compile(cursor, info, [], settings).sql()).fetchall()
    with views.reading(cursor, info, [], settings, ordered=True) as plain:
        assert cursor.execute(plain).fetchall() == raw
    tables = {r[0] for r in cursor.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    cached = {e.table.strip('"') for e in views._tables.values()}
    assert {t for t in tables if t.startswith("vc_")} == cached
