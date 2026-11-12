"""The copied code works where the user pastes it (the owner's report: copied SQL and Python never
transformed the source table back in their own Python).

Each case pushes a made-up pandas table named `orders`, applies steps, takes the code exactly as
the page copies it, and runs it the way the user would: in a fresh Python process (`python -I`)
whose only data is `orders`, the same DataFrame. What it assigns (`orders_view`) must equal the
grid's view: same columns, same rows, same order. Column names carry spaces, quotes, keywords,
capitals and non-ASCII letters; values carry blanks, padded text, dates and timestamps.

All data is made up.
"""

from __future__ import annotations

import ast
import io
import pickle
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from starlette.testclient import TestClient

from swissarmygrid.codegen import verify as verifying
from swissarmygrid.codegen.duckdb_code import python_string, table_name_sql, variable_name
from swissarmygrid.codegen.generate import generate_code
from swissarmygrid.codegen.verify import compare, result_rows
from swissarmygrid.engine.database import open_database
from swissarmygrid.engine.datasets import DatasetStore
from swissarmygrid.engine.steps import ViewSettings, steps_from_json
from swissarmygrid.engine.views import ViewEngine
from tests.conftest import ARROW, AUTH, arrow_bytes, grid_id


def make_orders(n: int = 240) -> pd.DataFrame:
    regions = ["North", "south", " East ", None, "Wést", "NORTH"]
    products = ['Widget "XL"', "gizmo", "Gadget", "ß-tool", None]
    return pd.DataFrame(
        {
            "order id": list(range(1, n + 1)),
            "Region": [regions[i % len(regions)] for i in range(n)],
            'say "hi"': [products[(i * 7) % len(products)] for i in range(n)],
            "select": [(i * 13) % 5 for i in range(n)],
            "größe": [None if i % 9 == 0 else round((i * 37) % 101 * 1.25, 2) for i in range(n)],
            "day": [
                None if i % 11 == 0 else date(2026, 1, 1) + timedelta(days=(i * 5) % 120) for i in range(n)
            ],
            "when": [datetime(2026, 3, 1, 8, 30) + timedelta(hours=(i * 19) % 500) for i in range(n)],
            "paid": [None if i % 7 == 0 else i % 3 == 0 for i in range(n)],
            "note": [None if i % 4 == 0 else ("" if i % 4 == 1 else f"  note {i % 6}  ") for i in range(n)],
        }
    )


ORDERS = make_orders()
SOURCE = pa.Table.from_pandas(ORDERS, preserve_index=False)

# name -> (steps, settings)
CASES: dict[str, tuple[list[dict[str, Any]], dict[str, bool]]] = {
    "no steps": ([], {}),
    "filter text eq, case insensitive": (
        [{"kind": "filter", "params": {"column": "Region", "op": "eq", "value": "north"}}],
        {"case_sensitive": False, "trim_spaces": True},
    ),
    "filter number ne drops blanks": (
        [{"kind": "filter", "params": {"column": "größe", "op": "ne", "value": 12.5}}],
        {},
    ),
    "filter keyword column in": (
        [{"kind": "filter", "params": {"column": "select", "op": "in", "values": [0, 3]}}],
        {},
    ),
    "filter blank and not blank": (
        [
            {"kind": "filter", "params": {"column": "note", "op": "not_blank"}},
            {"kind": "filter", "params": {"column": "größe", "op": "blank"}},
        ],
        {"empty_is_blank": True},
    ),
    "filter wildcard on a quoted column": (
        [{"kind": "filter", "params": {"column": 'say "hi"', "op": "eq", "value": '%"XL"'}}],
        {"wildcards": True},
    ),
    "filter dates and booleans": (
        [
            {
                "kind": "filter",
                "params": {"column": "day", "op": "date_between", "low": "2026-01-10", "high": "2026-03-01"},
            },
            {"kind": "filter", "params": {"column": "paid", "op": "false"}},
        ],
        {},
    ),
    "sort with ties keeps order": (
        [{"kind": "sort", "params": {"keys": [["select", "desc"], ["Region", "asc"]]}}],
        {},
    ),
    "sort then sort is stable": (
        [
            {"kind": "sort", "params": {"keys": [["größe", "asc"]]}},
            {"kind": "sort", "params": {"keys": [["select", "asc"]]}},
        ],
        {},
    ),
    "columns and rename to awkward names": (
        [
            {"kind": "columns", "params": {"keep": ["größe", "order id", 'say "hi"', "select"]}},
            {
                "kind": "rename",
                "params": {"mapping": {"größe": "size (€)", "select": "from", 'say "hi"': "it's"}},
            },
        ],
        {},
    ),
    "group by with every aggregate": (
        [
            {
                "kind": "group_by",
                "params": {
                    "keys": ["Region", "paid"],
                    "aggregates": [
                        {"fn": "count_rows"},
                        {"fn": "count", "column": "größe"},
                        {"fn": "distinct_count", "column": 'say "hi"'},
                        {"fn": "sum", "column": "größe"},
                        {"fn": "min", "column": "day"},
                        {"fn": "max", "column": "when"},
                        {"fn": "mean", "column": "select", "as": "avg select"},
                    ],
                },
            }
        ],
        {},
    ),
    "group by without keys": (
        [{"kind": "group_by", "params": {"keys": [], "aggregates": [{"fn": "sum", "column": "größe"}]}}],
        {},
    ),
    "distinct keeps first positions": (
        [
            {"kind": "columns", "params": {"keep": ["Region", "select"]}},
            {"kind": "distinct", "params": {}},
        ],
        {},
    ),
    "keep first rows": ([{"kind": "limit", "params": {"n": 17}}], {}),
    "clean text": (
        [
            {"kind": "transform", "params": {"fn": "trim_both", "columns": "*text"}},
            {"kind": "transform", "params": {"fn": "upper", "columns": ["Region"]}},
            {"kind": "transform", "params": {"fn": "lower", "columns": ['say "hi"']}},
        ],
        {},
    ),
    "bucket numbers and dates": (
        [
            {
                "kind": "bucket",
                "params": {"column": "größe", "as": "size band", "mode": "edges", "edges": [0, 25, 50, 200]},
            },
            {"kind": "bucket", "params": {"column": "when", "as": "week", "mode": "date", "unit": "week"}},
            {
                "kind": "bucket",
                "params": {"column": "day", "as": "quarter", "mode": "date", "unit": "quarter"},
            },
        ],
        {},
    ),
    "categorize, then categorize the new column": (
        [
            {
                "kind": "categorize",
                "params": {
                    "column": "Region",
                    "as": "zone",
                    "rules": [
                        {"type": "in", "values": ["north", "south"], "label": "N/S"},
                        {"type": "contains", "value": "é", "label": "accent"},
                    ],
                    "unmatched": "other",
                },
            },
            {
                "kind": "categorize",
                "params": {
                    "column": "zone",
                    "as": "zone 2",
                    "rules": [{"type": "starts_with", "value": "n", "label": "n"}],
                },
            },
            {"kind": "filter", "params": {"column": "zone", "op": "ne", "value": "other"}},
        ],
        {"case_sensitive": False, "trim_spaces": True},
    ),
    "sql step over grid": (
        [
            {
                "kind": "sql",
                "params": {"sql": 'SELECT "order id", "größe" * 2 AS double FROM grid WHERE "select" > 1'},
            }
        ],
        {},
    ),
    "sql step by the dataset name, unordered": (
        [
            {"kind": "filter", "params": {"column": "paid", "op": "not_blank"}},
            {"kind": "sql", "params": {"sql": "SELECT Region, count(*) AS n FROM orders GROUP BY Region;"}},
        ],
        {},
    ),
    "sql step with its own WITH and a comment": (
        [
            {"kind": "sort", "params": {"keys": [["größe", "desc"]]}},
            {
                "kind": "sql",
                "params": {
                    "sql": 'WITH big AS (SELECT * FROM grid WHERE "größe" > 50) -- big ones\n'
                    'SELECT "order id", \'a\\b """ c\' AS odd FROM big ORDER BY "order id" DESC'
                },
            },
            {"kind": "limit", "params": {"n": 5}},
        ],
        {},
    ),
    "two sql steps in a row": (
        [
            {"kind": "sql", "params": {"sql": 'SELECT "order id" AS id, "select" AS s FROM orders'}},
            {"kind": "sql", "params": {"sql": "SELECT s, max(id) AS top FROM grid GROUP BY s"}},
        ],
        {},
    ),
    "a realistic list": (
        [
            {"kind": "filter", "params": {"column": "größe", "op": "gt", "value": 5}},
            {"kind": "transform", "params": {"fn": "trim_both", "columns": ["Region"]}},
            {
                "kind": "categorize",
                "params": {
                    "column": 'say "hi"',
                    "as": "family",
                    "rules": [{"type": "contains", "value": "g", "label": "G"}],
                },
            },
            {"kind": "bucket", "params": {"column": "day", "as": "month", "mode": "date", "unit": "month"}},
            {"kind": "sort", "params": {"keys": [["select", "desc"]]}},
            {"kind": "rename", "params": {"mapping": {"select": "qty"}}},
            {
                "kind": "columns",
                "params": {"keep": ["order id", "Region", "family", "month", "qty", "größe"]},
            },
            {"kind": "limit", "params": {"n": 60}},
            {"kind": "distinct", "params": {}},
            {
                "kind": "sql",
                "params": {
                    "sql": 'SELECT Region, month, sum("größe") AS total, count(*) AS n '
                    "FROM orders GROUP BY ALL"
                },
            },
            {"kind": "sort", "params": {"keys": [["total", "desc"]], "header": True}},
        ],
        {},
    ),
    "merges: sort, filter, columns, limit in one select": (
        [
            {"kind": "sort", "params": {"keys": [["größe", "desc"]]}},
            {"kind": "filter", "params": {"column": "paid", "op": "true"}},
            {"kind": "columns", "params": {"keep": ["order id", "Region"]}},
            {"kind": "filter", "params": {"column": "Region", "op": "not_blank"}},
            {"kind": "limit", "params": {"n": 9}},
            {"kind": "sort", "params": {"keys": [["Region", "asc"]]}},
        ],
        {},
    ),
    "sort after group and after distinct": (
        [
            {"kind": "group_by", "params": {"keys": ["select"], "aggregates": [{"fn": "count_rows"}]}},
            {"kind": "sort", "params": {"keys": [["rows", "desc"]]}},
            {"kind": "distinct", "params": {}},
            {"kind": "sort", "params": {"keys": [["select", "asc"]]}},
        ],
        {},
    ),
    "group after a sort and a filter on a new column": (
        [
            {"kind": "sort", "params": {"keys": [["when", "desc"]]}},
            {
                "kind": "bucket",
                "params": {"column": "select", "as": "band", "mode": "edges", "edges": [0, 2, 5]},
            },
            {"kind": "filter", "params": {"column": "band", "op": "not_blank"}},
            {
                "kind": "group_by",
                "params": {"keys": ["band"], "aggregates": [{"fn": "max", "column": "when"}]},
            },
        ],
        {},
    ),
    "a step turned off is left out": (
        [
            {"kind": "filter", "params": {"column": "select", "op": "eq", "value": 1}, "enabled": False},
            {"kind": "limit", "params": {"n": 3}},
        ],
        {},
    ),
}


def generate(
    steps: list[dict[str, Any]], settings: dict[str, bool], target: str = "duckdb"
) -> tuple[dict[str, Any], list[str], list[list[Any]]]:
    connection = open_database("1GiB")
    store = DatasetStore(connection, 10**9, 10**9, 50)
    info = store.replace("orders", SOURCE, "pandas")
    views = ViewEngine(connection, 10**8)
    parsed = steps_from_json(steps)
    view_settings = ViewSettings.from_json(settings)
    cursor = connection.cursor()
    compiled = views.compile(cursor, info, parsed, view_settings)
    assert compiled.error is None, compiled.error
    result = generate_code(cursor, views, info, parsed, view_settings, target)
    expected = views.window(cursor, info, parsed, view_settings, 0, info.row_count)
    return result, [c.name for c in expected.columns], expected.rows


RUNNER = """
import io, pickle, sys
import pyarrow as pa, pyarrow.ipc as ipc
frame, code = pickle.load(sys.stdin.buffer)
namespace = {"orders": frame}
exec(compile(code, "<pasted>", "exec"), namespace)
view = namespace["orders_view"]
table = pa.Table.from_pandas(view, preserve_index=False) if hasattr(view, "itertuples") else view
sink = io.BytesIO()
with ipc.new_stream(sink, table.schema) as writer:
    writer.write_table(table)
sys.stdout.buffer.write(sink.getvalue())
"""


def run_in_fresh_python(code: str, frame: pd.DataFrame) -> tuple[list[str], list[list[Any]]]:
    """A new interpreter (no site, no project on the path) that has pandas, duckdb and `orders`."""
    done = subprocess.run(  # noqa: S603 (this interpreter, running the code under test)
        [sys.executable, "-I", "-c", RUNNER],
        input=pickle.dumps((frame, code)),
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode() + "\n\n" + code
    table = ipc.open_stream(io.BytesIO(done.stdout)).read_all()
    return list(table.column_names), [list(r.values()) for r in table.to_pylist()]


@pytest.mark.parametrize("name", list(CASES))
def test_copied_python_runs_in_a_fresh_python(name: str) -> None:
    steps, settings = CASES[name]
    result, names, rows = generate(steps, settings)
    assert result["verified"], f"{result['difference']}\n\n{result['code']}"
    code = result["code"]
    # Only `import duckdb`, one statement, nothing from the server.
    tree = ast.parse(code)
    assert [type(n).__name__ for n in tree.body] == ["Import", "Assign"], code
    for internal in ("__sag", "ds_orders", "source", "pyarrow", "transform", "register"):
        assert internal not in code, (internal, code)
    got_names, got = run_in_fresh_python(code, ORDERS.copy())
    outcome = compare(got_names, got, names, rows)
    assert outcome.identical, f"{outcome.message}\n\n{code}"


@pytest.mark.parametrize(
    "name", ["a realistic list", "sql step with its own WITH and a comment", "group by with every aggregate"]
)
def test_copied_sql_is_one_query_over_the_users_table(name: str) -> None:
    steps, settings = CASES[name]
    result, names, rows = generate(steps, settings)
    sql = result["sql"]
    assert len([s for s in duckdb.extract_statements(sql)]) == 1
    orders = ORDERS.copy()  # noqa: F841 (read by name inside duckdb.sql)
    relation = duckdb.sql(sql)
    assert relation is not None
    got_names, got = result_rows(relation.df())
    assert compare(got_names, got, names, rows).identical, sql


@pytest.mark.parametrize("target", ["pandas", "plain"])
@pytest.mark.parametrize(
    "name", ["a realistic list", "clean text", "bucket numbers and dates", "distinct keeps first positions"]
)
def test_secondary_targets_also_transform_the_users_variable(name: str, target: str) -> None:
    steps, settings = CASES[name]
    result, names, rows = generate(steps, settings, target)
    assert result["verified"], f"{result['difference']}\n\n{result['code']}"
    assert result["code"].rstrip().endswith("orders_view = transform(orders)")
    value = ORDERS.copy() if target == "pandas" else SOURCE.to_pylist()
    namespace: dict[str, Any] = {"orders": value}
    exec(compile(result["code"], "<pasted>", "exec"), namespace)  # noqa: S102 (generated code under test)
    got_names, got = result_rows(namespace["orders_view"])
    assert compare(got_names, got, names, rows).identical


def test_without_pandas_beside_the_server_the_query_is_still_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    real = verifying.user_value

    def no_pandas(kind: str, table: pa.Table) -> Any:
        if kind == "pandas":
            raise ModuleNotFoundError("No module named 'pandas'")
        return real(kind, table)

    monkeypatch.setattr(verifying, "user_value", no_pandas)
    result, _, _ = generate(*CASES["a realistic list"])
    assert result["verified"] and result["code"].rstrip().endswith(".df()")
    assert "pandas is not installed" in result["note"]


def test_wrong_code_is_not_verified() -> None:
    """The check runs what the user gets: code naming a table they do not have fails."""
    pasted = verifying.Pasted(
        'import duckdb\n\norders_view = duckdb.sql("SELECT * FROM ds_orders").df()\n',
        "orders",
        "pandas",
        "orders_view",
    )
    outcome = verifying.verify(
        pasted, SOURCE, list(SOURCE.column_names), [list(r.values()) for r in SOURCE.to_pylist()]
    )
    assert not outcome.identical and "ds_orders" in (outcome.message or "")
    no_result = verifying.Pasted("import duckdb\n", "orders", "pandas", "orders_view")
    assert not verifying.verify(no_result, SOURCE, [], []).identical


@pytest.mark.parametrize(
    "text",
    [
        "plain",
        'a "quoted" word',
        "a\\b",
        'three """ quotes',
        "both \"\"\" and ''' and \\",
        "line\r\nbreak",
        "tab\there",
        "ß ẞ İ",
    ],
)
def test_python_string_reads_back_exactly(text: str) -> None:
    literal = python_string(text)
    assert ast.literal_eval(literal).strip("\n") == text.strip("\n")


def test_names_that_are_not_plain_identifiers() -> None:
    assert variable_name("orders") == "orders"
    assert variable_name("my-orders") == "my_orders"
    assert variable_name("2026") == "data_2026"
    assert variable_name("class") == "class_"
    assert table_name_sql("orders") == "orders"
    assert table_name_sql("Orders") == '"Orders"'
    assert table_name_sql("order") == '"order"'  # a keyword


def test_a_dataset_named_like_a_keyword_or_with_a_dash() -> None:
    for dataset, variable in (("order", "order"), ("my-orders", "my_orders")):
        connection = open_database("1GiB")
        store = DatasetStore(connection, 10**9, 10**9, 50)
        info = store.replace(dataset, SOURCE, "pandas")
        views = ViewEngine(connection, 10**8)
        sql_text = f'SELECT "order id" FROM "{dataset}" WHERE "select" = 2'  # noqa: S608 (test text)
        steps = steps_from_json(
            [{"kind": "limit", "params": {"n": 50}}, {"kind": "sql", "params": {"sql": sql_text}}]
        )
        result = generate_code(connection.cursor(), views, info, steps, ViewSettings(), None)
        assert result["verified"], f"{result['difference']}\n\n{result['code']}"
        assert result["variable"] == variable and f"{variable}_view = duckdb.sql(" in result["code"]


# ===== Through the server, as the page and the agent get it =====


def test_code_route_offers_sql_and_python(client: TestClient) -> None:
    response = client.put(
        "/datasets/orders",
        content=arrow_bytes(SOURCE),
        headers=AUTH | {"Content-Type": ARROW, "X-Source-Kind": "pandas"},
    )
    grid = grid_id(response)
    steps, _ = CASES["a realistic list"]
    assert client.post(f"/grids/{grid}/steps", json={"steps": steps}, headers=AUTH).status_code == 200
    body = client.get(f"/grids/{grid}/code", headers=AUTH).json()
    assert body["target"] == "duckdb" and body["verified"], body
    assert body["code"].startswith("import duckdb\n\norders_view = duckdb.sql(")
    assert body["sql"].strip() in body["code"]
    assert body["reads"] == "Reads a pandas DataFrame named orders and assigns the result to orders_view."
    rows = client.get(f"/grids/{grid}/rows", params={"start": 0, "end": 1000}, headers=AUTH).json()["rows"]
    columns = [c["name"] for c in client.get(f"/grids/{grid}", headers=AUTH).json()["columns"]]
    got_names, got = run_in_fresh_python(body["code"], ORDERS.copy())
    assert got_names == columns == ["Region", "month", "total", "n"]
    # The page's rows are JSON (dates as text); compare the other columns value by value.
    assert [[r[0], r[2], r[3]] for r in got] == [[r[0], r[2], r[3]] for r in rows]
    assert [r[1].date().isoformat() if r[1] else None for r in got] == [r[1] for r in rows]

    # The agent's command returns both copies too.
    python = client.get(f"/grids/{grid}/code", params={"target": "pandas"}, headers=AUTH).json()
    assert python["verified"] and python["sql"] == body["sql"]


def test_verifying_writes_nothing_and_reads_no_files(tmp_path: Path) -> None:
    """Nothing is written while verifying: the default connection has no file system."""
    before = set(Path.cwd().iterdir())
    generate(*CASES["a realistic list"])
    with pytest.raises(duckdb.Error):
        duckdb.sql(f"COPY (SELECT 1) TO '{tmp_path / 'x.csv'}'")
    assert set(Path.cwd().iterdir()) == before
