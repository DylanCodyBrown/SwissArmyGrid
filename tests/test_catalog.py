"""The catalog engine: a source database's names and types, loaded, browsed and searched.

Made-up names only (a pretend warehouse of a bakery chain).
"""

from __future__ import annotations

import time

import duckdb
import pyarrow as pa
import pytest

from swissarmygrid.adapters.database import open_database
from swissarmygrid.engine.catalog import CatalogError, CatalogStore, select_sql

INFORMATION_SCHEMA = pa.table(
    {
        "TABLE_CATALOG": ["bakery"] * 7,
        "TABLE_SCHEMA": ["sales", "sales", "sales", "sales", "stock", "stock", "sales"],
        "TABLE_NAME": ["orders", "orders", "orders", "order_lines", "flour", "flour", "daily_totals"],
        "TABLE_TYPE": ["BASE TABLE"] * 6 + ["VIEW"],
        "COLUMN_NAME": ["order_id", "ordered_at", "customer_id", "order_id", "flour_id", "kilos", "day"],
        "DATA_TYPE": ["int", "datetime2", "int", "int", "int", "decimal", "date"],
        "IS_NULLABLE": ["NO", "YES", "YES", "NO", "NO", "YES", "NO"],
        "ORDINAL_POSITION": [1, 2, 3, 1, 1, 2, 1],
    }
)


@pytest.fixture
def store() -> CatalogStore:
    return CatalogStore(open_database("256MiB"), max_catalogs=3, max_rows=1_000_000)


@pytest.fixture
def cur(store: CatalogStore) -> duckdb.DuckDBPyConnection:
    return store.connection.cursor()


def test_information_schema_columns_load_as_they_are(
    store: CatalogStore, cur: duckdb.DuckDBPyConnection
) -> None:
    info = store.load(cur, "Bakery", INFORMATION_SCHEMA, dialect="sqlserver")
    assert (info.name, info.dialect, info.schemas, info.tables, info.views, info.columns) == (
        "Bakery",
        "sqlserver",
        2,
        3,
        1,
        7,
    )
    assert [s["schema"] for s in store.schemas(cur, "Bakery")] == ["sales", "stock"]
    tables = store.tables(cur, "Bakery", "sales")["tables"]
    assert [(t["table"], t["kind"], t["columns"]) for t in tables] == [
        ("daily_totals", "view", 1),
        ("order_lines", "table", 1),
        ("orders", "table", 3),
    ]
    columns = store.columns(cur, "Bakery", "sales", "orders")
    assert [(c["column"], c["type"], c["nullable"]) for c in columns] == [
        ("order_id", "int", False),
        ("ordered_at", "datetime2", True),
        ("customer_id", "int", True),
    ]


def test_plain_names_and_missing_optional_columns(
    store: CatalogStore, cur: duckdb.DuckDBPyConnection
) -> None:
    plain = pa.table({"Table": ["loaves"], "Column": ["crust"], "Type": ["VARCHAR"]})
    info = store.load(cur, "plain", plain)
    assert info.schemas == 1 and info.dialect == "other"
    assert store.schemas(cur, "plain")[0]["schema"] == ""
    assert store.columns(cur, "plain", "", "loaves")[0] == {
        "column": "crust",
        "type": "VARCHAR",
        "nullable": None,
        "position": 1,
        "description": None,
    }


def test_oracle_names_are_understood(store: CatalogStore, cur: duckdb.DuckDBPyConnection) -> None:
    oracle = pa.table(
        {
            "OWNER": ["BAKERY"],
            "TABLE_NAME": ["OVENS"],
            "COLUMN_NAME": ["OVEN_ID"],
            "DATA_TYPE": ["NUMBER"],
            "NULLABLE": ["N"],
        }
    )
    store.load(cur, "ora", oracle, dialect="oracle")
    assert store.columns(cur, "ora", "BAKERY", "OVENS")[0]["nullable"] is False


def test_a_table_without_table_or_column_names_is_refused_plainly(
    store: CatalogStore, cur: duckdb.DuckDBPyConnection
) -> None:
    with pytest.raises(CatalogError, match="column_name"):
        store.load(cur, "bad", pa.table({"table_name": ["x"], "kilos": [1]}))


def test_bad_names_dialects_and_limits(store: CatalogStore, cur: duckdb.DuckDBPyConnection) -> None:
    for name in ["", " lead", "a/b", "x" * 65, "trail "]:
        with pytest.raises(CatalogError):
            store.load(cur, name, INFORMATION_SCHEMA)
    with pytest.raises(CatalogError, match="dialect"):
        store.load(cur, "d", INFORMATION_SCHEMA, dialect="cobol")
    for n in range(3):
        store.load(cur, f"c{n}", INFORMATION_SCHEMA)
    with pytest.raises(CatalogError, match="3 catalogs"):
        store.load(cur, "one too many", INFORMATION_SCHEMA)
    store.load(cur, "C0", INFORMATION_SCHEMA)  # the same name in another case replaces, it is not a new one
    assert [i.name for i in store.all()] == ["C0", "c1", "c2"]
    small = CatalogStore(open_database("256MiB"), max_catalogs=3, max_rows=5)
    with pytest.raises(CatalogError, match="5 rows"):
        small.load(small.connection.cursor(), "big", INFORMATION_SCHEMA)


def test_remove_and_unknown_catalogs(store: CatalogStore, cur: duckdb.DuckDBPyConnection) -> None:
    store.load(cur, "Bakery", INFORMATION_SCHEMA)
    store.remove(cur, "bakery")
    assert store.all() == []
    with pytest.raises(KeyError):
        store.schemas(cur, "Bakery")


def test_search_ranks_exact_then_starts_with_then_contains(
    store: CatalogStore, cur: duckdb.DuckDBPyConnection
) -> None:
    store.load(cur, "Bakery", INFORMATION_SCHEMA)
    found = store.search(cur, "Bakery", "order_id")
    assert [(c["table"], c["column"]) for c in found["columns"]] == [
        ("order_lines", "order_id"),
        ("orders", "order_id"),
    ]
    found = store.search(cur, "Bakery", "ORDER")
    # Tables named like it first: starts-with before contains, then shorter names first.
    assert [t["table"] for t in found["tables"]] == ["orders", "order_lines"]
    assert found["columns"][0]["column"] in ("order_id", "ordered_at")
    assert found["total_columns"] == len(found["columns"])


def test_search_by_dotted_path_and_by_several_words(
    store: CatalogStore, cur: duckdb.DuckDBPyConnection
) -> None:
    store.load(cur, "Bakery", INFORMATION_SCHEMA)
    by_table_column = store.search(cur, "Bakery", "flour.kilos")
    assert [(c["table"], c["column"]) for c in by_table_column["columns"]] == [("flour", "kilos")]
    by_schema_table = store.search(cur, "Bakery", "stock.flour")
    assert [t["table"] for t in by_schema_table["tables"]] == ["flour"]
    words = store.search(cur, "Bakery", "sales  id")
    assert {(c["table"], c["column"]) for c in words["columns"]} == {
        ("orders", "order_id"),
        ("orders", "customer_id"),
        ("order_lines", "order_id"),
    }
    assert store.search(cur, "Bakery", "") == {
        "tables": [],
        "columns": [],
        "total_tables": 0,
        "total_columns": 0,
    }


@pytest.mark.parametrize("text", ["%", "_", "'", '"', "\\", "o'; DROP TABLE x; --", "[a-z]*", "*"])
def test_search_text_is_only_ever_text(
    store: CatalogStore, cur: duckdb.DuckDBPyConnection, text: str
) -> None:
    store.load(cur, "Bakery", INFORMATION_SCHEMA)
    found = store.search(cur, "Bakery", text)
    columns = zip(
        INFORMATION_SCHEMA["TABLE_SCHEMA"].to_pylist(),
        INFORMATION_SCHEMA["TABLE_NAME"].to_pylist(),
        INFORMATION_SCHEMA["COLUMN_NAME"].to_pylist(),
        strict=True,
    )
    assert found["total_columns"] == sum(1 for path in columns if text in ".".join(path))


def test_a_large_catalog_answers_quickly(store: CatalogStore, cur: duckdb.DuckDBPyConnection) -> None:
    n = 500_000
    big = pa.table(
        {
            "table_schema": [f"schema_{i % 40}" for i in range(n)],
            "table_name": [f"table_{i % 20_000}" for i in range(n)],
            "column_name": [f"column_{i}" for i in range(n)],
            "data_type": ["int"] * n,
        }
    )
    started = time.perf_counter()
    info = store.load(cur, "big", big)
    assert info.columns == n and info.tables == 20_000
    load_seconds = time.perf_counter() - started
    started = time.perf_counter()
    store.schemas(cur, "big")
    store.tables(cur, "big", "schema_7")
    store.columns(cur, "big", "schema_7", "table_7")
    found = store.search(cur, "big", "column_49999")
    browse_seconds = time.perf_counter() - started
    assert found["columns"][0]["column"] == "column_49999"
    assert load_seconds < 5 and browse_seconds < 1.5, (load_seconds, browse_seconds)


def test_tables_page_through_a_large_schema(store: CatalogStore, cur: duckdb.DuckDBPyConnection) -> None:
    many = pa.table({"table_name": [f"t{i:04}" for i in range(2500)], "column_name": ["c"] * 2500})
    store.load(cur, "many", many)
    first = store.tables(cur, "many", "", limit=1000)
    assert len(first["tables"]) == 1000 and first["total"] == 2500
    last = store.tables(cur, "many", "", offset=2000, limit=1000)
    assert last["tables"][-1]["table"] == "t2499" and len(last["tables"]) == 500


def test_select_is_quoted_for_each_dialect() -> None:
    cols = ["order id", "kilos"]
    assert select_sql("sqlserver", "", "sales", "or]ders", cols) == (
        "SELECT TOP 100 [order id], [kilos]\nFROM [sales].[or]]ders]"
    )
    # The database is named only when the catalog spans more than one (the caller decides).
    assert select_sql("trino", "hive", "sales", "orders", ["kilos"]) == (
        'SELECT "kilos"\nFROM "hive"."sales"."orders"\nLIMIT 100'
    )
    assert select_sql("postgres", "", "sales", 'or"ders', cols) == (
        'SELECT "order id", "kilos"\nFROM "sales"."or""ders"\nLIMIT 100'
    )
    assert (
        select_sql("mysql", "", "sales", "orders", cols)
        == "SELECT `order id`, `kilos`\nFROM `sales`.`orders`\nLIMIT 100"
    )
    assert select_sql("oracle", "", "BAKERY", "OVENS", ["OVEN_ID"]) == (
        'SELECT "OVEN_ID"\nFROM "BAKERY"."OVENS"\nFETCH FIRST 100 ROWS ONLY'
    )
    assert select_sql("sqlite", "", "", "loaves", []) == 'SELECT *\nFROM "loaves"\nLIMIT 100'
