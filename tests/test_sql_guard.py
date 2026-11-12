"""The SQL guard on its own, against a real locked database: escapes and the shapes it must allow.

Each escape below got past the guard once and read something it must not: another table by its
internal name, the catalog, or the settings. A refused query raises SqlRefused before DuckDB runs it.
"""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pyarrow as pa
import pytest

from swissarmygrid.engine.database import open_database
from swissarmygrid.engine.datasets import DatasetStore
from swissarmygrid.engine.sql_guard import SqlRefused, check_duckdb_parse, guard, guard_step


@pytest.fixture
def store() -> Iterator[DatasetStore]:
    connection = open_database("256MiB")
    store = DatasetStore(connection, 1 << 28, 1 << 28, 10)
    store.replace("pub", pa.table({"a": [1, 2], "b": ["x", "y"]}), "arrow")
    store.replace("other", pa.table({"s": ["made-up", "values"]}), "arrow")
    # A view cache table, as views.py makes one.
    store.cursor().execute("CREATE TABLE vc_0123 AS SELECT 42 AS cached")
    yield store
    connection.close()


def run(store: DatasetStore, text: str, grid: str = "SELECT * FROM ds_pub") -> list[tuple[object, ...]]:
    guarded = guard(text, {i.name for i in store.all()}, lambda: grid)
    return store.cursor().execute(guarded.sql).fetchall()


ESCAPES = [
    # A CTE defined in one scope makes the same name pass in another, where it is a real table.
    "SELECT * FROM (WITH ds_other AS (SELECT 1) SELECT 1) t, ds_other",
    "SELECT * FROM (WITH vc_0123 AS (SELECT 1) SELECT 1) t, vc_0123",
    "SELECT * FROM (WITH duckdb_tables AS (SELECT 1) SELECT 1) t, duckdb_tables",
    "SELECT * FROM (WITH duckdb_columns AS (SELECT 1) SELECT 1) t, duckdb_columns",
    "SELECT * FROM (WITH pg_settings AS (SELECT 1) SELECT 1) t, pg_settings",
    "SELECT * FROM (WITH sqlite_master AS (SELECT 1) SELECT 1) t, sqlite_master",
    "SELECT * FROM grid WHERE a IN (SELECT 1 FROM (WITH ds_other AS (SELECT 1) SELECT 1), ds_other)",
    # A CTE's body does not see the CTE itself (unless recursive): the name is the real table.
    "WITH ds_other AS (SELECT * FROM ds_other) SELECT * FROM ds_other",
    # A later CTE is not visible to an earlier one.
    "WITH a AS (SELECT * FROM ds_other), ds_other AS (SELECT 1) SELECT * FROM a",
    # A table function after LATERAL is not a FROM table, so it skipped the table check;
    # json_execute_serialized_sql runs any SELECT given as text.
    "SELECT * FROM grid, LATERAL json_execute_serialized_sql(json_serialize_sql('SELECT * FROM ds_other'))",
    "SELECT * FROM grid, LATERAL duckdb_settings()",
    "SELECT * FROM grid, LATERAL test_all_types()",
    "SELECT * FROM grid, LATERAL repeat_row(1, num_rows := 2)",
    "SELECT * FROM grid CROSS JOIN LATERAL arrow_scan(0, 0, 0)",
    # A table function called as a scalar never runs, but is refused plainly too.
    "SELECT json_execute_serialized_sql(json_serialize_sql('SELECT * FROM ds_other'))",
    # Internal names are not for user text.
    "WITH __sag_grid AS (SELECT * FROM grid) SELECT * FROM __sag_grid",
    "SELECT * FROM __sag_cte_0",
]


@pytest.mark.parametrize("text", ESCAPES)
def test_escapes_are_refused(store: DatasetStore, text: str) -> None:
    with pytest.raises(SqlRefused):
        run(store, text)


ALLOWED = [
    ("WITH c AS (SELECT a FROM grid) SELECT sum(a) FROM c", [(3,)]),
    ("WITH c AS (SELECT a FROM grid), d AS (SELECT a FROM c) SELECT sum(a) FROM d", [(3,)]),
    ("SELECT * FROM (WITH c AS (SELECT a FROM grid) SELECT max(a) FROM c)", [(2,)]),
    ("WITH c AS (SELECT 5 AS a) SELECT (SELECT max(a) FROM c) AS m", [(5,)]),
    # DuckDB matches CTE names without case.
    ("WITH C AS (SELECT 7 AS v) SELECT v FROM c", [(7,)]),
    # A CTE named like a dataset hides it, as in DuckDB.
    ("WITH other AS (SELECT 9 AS s) SELECT s FROM other", [(9,)]),
    (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3) SELECT sum(i) FROM n",
        [(6,)],
    ),
    ("SELECT g.a, o.s FROM grid g POSITIONAL JOIN other o ORDER BY g.a", [(1, "made-up"), (2, "values")]),
    ("SELECT c.x FROM other AS c(x) ORDER BY 1", [("made-up",), ("values",)]),
    ("SELECT sum(r.range) FROM range(4) r", [(6,)]),
    ("SELECT count(*) FROM grid, unnest([1, 2]) u(v)", [(4,)]),
    ("SELECT count(*) FROM generate_series(1, 3)", [(3,)]),
    ("SELECT repeat('ab', 2) AS r", [("abab",)]),
    ("SELECT count(*) FROM grid, LATERAL (SELECT grid.a * 2 AS d)", [(2,)]),
    ("FROM grid SELECT max(b)", [("y",)]),
    ("SELECT a FROM grid WHERE b IN (SELECT 'x') UNION ALL SELECT 10 ORDER BY 1", [(1,), (10,)]),
]


@pytest.mark.parametrize(("text", "expected"), ALLOWED)
def test_allowed_queries_still_run(
    store: DatasetStore, text: str, expected: list[tuple[object, ...]]
) -> None:
    assert run(store, text) == expected


def test_duckdb_parse_check_catches_what_sqlglot_misses() -> None:
    # The second check reads DuckDB's own parse of the final SQL.
    for sql in (
        "SELECT * FROM (SHOW TABLES)",
        "SELECT * FROM (SUMMARIZE ds_pub)",
        'SELECT * FROM "ds_other"',
        "SELECT * FROM main.ds_pub",
        "SELECT * FROM ds_pub, LATERAL duckdb_settings()",
        "SELECT * FROM read_text('x')",
    ):
        with pytest.raises(SqlRefused):
            check_duckdb_parse(sql, {"ds_pub"})
    check_duckdb_parse("SELECT * FROM ds_pub, range(3), unnest([1]) WITH ORDINALITY", {"ds_pub"})


def test_step_query_cannot_reach_other_datasets(store: DatasetStore) -> None:
    for text in (
        "SELECT * FROM (WITH ds_other AS (SELECT 1) SELECT 1) t, ds_other",
        "WITH ds_other AS (SELECT * FROM ds_other) SELECT * FROM ds_other",
    ):
        with pytest.raises(SqlRefused):
            guard_step(text, "SELECT * FROM ds_pub", "pub")


def test_pin_reads_only_allowed_tables(store: DatasetStore) -> None:
    with pytest.raises(SqlRefused):
        guard(
            "CREATE TABLE leak AS SELECT * FROM (WITH ds_other AS (SELECT 1) SELECT 1) t, ds_other",
            {"pub", "other"},
            lambda: "SELECT * FROM ds_pub",
        )


@pytest.mark.parametrize(
    "text",
    [
        "SELECT " + "(" * 3000 + "1" + ")" * 3000,
        "SELECT * FROM " + "(SELECT * FROM " * 500 + "grid" + ")" * 500,
        "SELECT " + "+".join("1" for _ in range(10000)),
    ],
)
def test_deeply_nested_queries_are_refused_plainly(store: DatasetStore, text: str) -> None:
    # Once a RecursionError from the parser (a server error), now a plain refusal.
    with pytest.raises(SqlRefused):
        run(store, text)
    with pytest.raises(SqlRefused):
        guard_step(text, "SELECT * FROM ds_pub", "pub")


@pytest.mark.parametrize(
    "text",
    [
        # In a WITH RECURSIVE, DuckDB reads a self-reference outside the recursive part of a
        # UNION as the real table. The guard points it at the CTE's fresh name, which no real
        # table has, so DuckDB stops with a binder error instead of reading anything.
        "WITH RECURSIVE ds_other AS (SELECT * FROM ds_other) SELECT * FROM ds_other",
        "WITH RECURSIVE ds_other AS (SELECT * FROM ds_other UNION ALL SELECT 'q') SELECT * FROM ds_other",
        "WITH RECURSIVE x AS (SELECT * FROM x) SELECT * FROM x",
    ],
)
def test_recursive_self_reference_reads_nothing(store: DatasetStore, text: str) -> None:
    with pytest.raises((SqlRefused, duckdb.Error)):
        run(store, text)
