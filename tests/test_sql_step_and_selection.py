"""SQL transformation steps, Excel-style selections, and the agent's CSV read of a grid.

All data is made up.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

import duckdb
import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from swissarmygrid.cli import api
from swissarmygrid.cli.api import CommandError
from swissarmygrid.cli.commands import execute
from tests.conftest import AGENT, AUTH, grid_id, push

ORDERS = pa.table(
    {
        "id": pa.array(list(range(1, 11)), pa.int64()),
        "region": pa.array(
            ["North", "South", "East", None, "North", "South", "East", "North", "West", "South"]
        ),
        "amount": pa.array([10.0, 20.0, None, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]),
    }
)


def steps(client: TestClient, grid: str, payload: list[dict[str, Any]]) -> Any:
    response = client.post(f"/grids/{grid}/steps", json={"steps": payload}, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def fake_call(client: TestClient) -> Any:
    def call(method: str, path: str, body: Any = None, params: Any = None, timeout: float = 0) -> Any:
        response = client.request(method, path, json=body, params=params, headers=AGENT)
        if response.status_code >= 400:
            raise CommandError(f"{response.status_code}: {response.text}")
        return response.json()

    return call


def tool(name: str, args: dict[str, Any]) -> str:
    return execute(name, args)


# ===== SQL steps =====


def test_sql_step_shapes_the_view_and_later_steps_see_it(client: TestClient) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    described = steps(
        client,
        grid,
        [
            {"kind": "filter", "params": {"column": "amount", "op": "gt", "value": 15}},
            {"kind": "sql", "params": {"sql": "SELECT id, amount * 2 AS doubled FROM grid WHERE id < 9"}},
            {"kind": "sort", "params": {"keys": [["doubled", "desc"]]}},
        ],
    )
    assert described["step_error"] is None
    assert [c["name"] for c in described["columns"]] == ["id", "doubled"]
    rows = client.get(f"/grids/{grid}/rows", params={"start": 0, "end": 50}, headers=AUTH).json()
    assert rows["rows"] == [[8, 160.0], [7, 140.0], [6, 120.0], [5, 100.0], [4, 80.0], [2, 40.0]]
    assert "SQL: SELECT id" in described["steps"][1]["description"]


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT * FROM customers", "reads only the table entering it"),
        ("SELECT * FROM read_csv('C:/x.csv')", "not allowed"),
        ("CREATE TABLE x AS SELECT * FROM grid", "one SELECT over orders"),
        ("SELECT 1 AS __sag_pos FROM grid", "may not start"),
    ],
)
def test_sql_step_refusals_are_step_errors(client: TestClient, sql: str, message: str) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    described = steps(client, grid, [{"kind": "sql", "params": {"sql": sql}}])
    assert described["step_error"]["index"] == 0
    assert message in described["step_error"]["message"]


def test_sql_step_code_is_verified(client: TestClient) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    steps(
        client,
        grid,
        [{"kind": "sql", "params": {"sql": "SELECT region, sum(amount) AS total FROM grid GROUP BY region"}}],
    )
    for target in ("duckdb", "pandas", "plain"):
        code = client.get(f"/grids/{grid}/code", params={"target": target}, headers=AUTH).json()
        assert code["verified"], (target, code["difference"], code["code"])


def test_sql_step_reads_the_dataset_by_its_own_name(client: TestClient) -> None:
    """A step may name the grid's dataset instead of `grid`, so the SQL is the same text that
    runs in the user's Python, where the table is a variable of that name."""
    grid = grid_id(push(client, "orders", ORDERS))
    sql = "SELECT id, amount FROM orders WHERE amount > 50"
    described = steps(client, grid, [{"kind": "sql", "params": {"sql": sql}}])
    assert described["step_error"] is None
    rows = client.get(f"/grids/{grid}/rows", params={"start": 0, "end": 50}, headers=AUTH).json()["rows"]
    # A plain filter keeps the grid's order, exactly as FROM grid does.
    assert rows == [[6, 60.0], [7, 70.0], [8, 80.0], [9, 90.0], [10, 100.0]]

    # Pasted into Python, the same text reads the local variable `orders` (a DuckDB replacement scan).
    orders = ORDERS.to_pandas()  # noqa: F841 (read by name inside duckdb.sql)
    pasted = duckdb.sql(sql)
    assert pasted is not None and [list(r) for r in pasted.fetchall()] == rows

    for target in ("duckdb", "pandas", "plain"):
        code = client.get(f"/grids/{grid}/code", params={"target": target}, headers=AUTH).json()
        assert code["verified"], (target, code["difference"], code["code"])
        if target != "duckdb":
            assert "'SELECT id, amount FROM orders WHERE amount > 50'" in code["code"]
    pandas_code = client.get(f"/grids/{grid}/code", params={"target": "pandas"}, headers=AUTH).json()["code"]
    assert 'con.register("orders", df)' in pandas_code and '"grid"' not in pandas_code


def test_sql_step_order_rule_treats_the_dataset_name_like_grid(client: TestClient) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    by_name = "SELECT region, sum(amount) AS total FROM orders GROUP BY region"
    steps(client, grid, [{"kind": "sql", "params": {"sql": by_name}}])
    named = client.get(f"/grids/{grid}/rows", params={"start": 0, "end": 50}, headers=AUTH).json()["rows"]
    steps(client, grid, [{"kind": "sql", "params": {"sql": by_name.replace("FROM orders", "FROM grid")}}])
    plain = client.get(f"/grids/{grid}/rows", params={"start": 0, "end": 50}, headers=AUTH).json()["rows"]
    assert named == plain


# ===== Excel-style selection =====


def test_selection_stats_cover_unloaded_rows(client: TestClient) -> None:
    grid = grid_id(push(client, "orders", ORDERS))

    def stats(sel: dict[str, Any]) -> Any:
        return client.post(f"/grids/{grid}/selection-stats", json={"selection": sel}, headers=AUTH).json()

    everything = stats({"rows": None, "columns": None})
    assert everything["rows"] == 10 and everything["cells"] == 30
    assert everything["count"] == 28  # one blank region, one blank amount
    # Sum covers every number cell: the ids (55) and the amounts (520).
    assert everything["sum"] == pytest.approx(575.0) and everything["numeric"] == 19
    column = stats({"rows": None, "columns": ["amount"]})
    assert column["count"] == 9 and column["mean"] == pytest.approx(520.0 / 9)
    assert column["sum"] == pytest.approx(520.0) and column["min"] == 10.0 and column["max"] == 100.0
    block = stats({"rows": [[0, 1], [4, 4]], "columns": ["region", "amount"]})
    assert block["rows"] == 3 and block["count"] == 6 and block["sum"] == pytest.approx(80.0)
    text_only = stats({"rows": [[0, 2]], "columns": ["region"]})
    assert text_only["sum"] is None and text_only["count"] == 3
    bad = client.post(f"/grids/{grid}/selection-stats", json={"selection": {"rows": [[3, 1]]}}, headers=AUTH)
    assert bad.status_code == 400


def test_selection_export_is_the_selected_block(client: TestClient) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    selection = json.dumps({"rows": [[1, 2], [8, 9]], "columns": ["id", "amount"]})
    response = client.get(
        f"/grids/{grid}/export",
        params={
            "scope": "selection",
            "selection": selection,
            "format": "txt",
            "provenance": "0",
            "header": "0",
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    lines = response.content.decode().split("\r\n")
    assert lines[:4] == ["2\t20.0", "3\t", "9\t90.0", "10\t100.0"]


# ===== the agent reads the grid as CSV =====


def test_read_grid_pages_and_always_gives_the_row_count(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    monkeypatch.setattr(api, "call", fake_call(client))

    none = tool("agent_read_grid", {"grid": grid, "limit": 5, "offset": 50})
    assert "row_count: 10" in none and "rows_returned: 0" in none

    text = tool("agent_read_grid", {"grid": grid, "limit": 3, "offset": 2})
    assert "row_count: 10" in text and "rows_returned: 3" in text
    body = text.split('format="csv">\n', 1)[1].split("</untrusted-data>")[0]
    assert list(csv.reader(io.StringIO(body))) == [
        ["id", "region", "amount"],
        ["3", "East", ""],
        ["4", "", "40.0"],
        ["5", "North", "50.0"],
    ]

    capped = tool("agent_read_grid", {"grid": grid, "limit": 5000})
    assert "rows_returned: 10" in capped


def test_agent_applies_a_sql_step_and_sees_the_new_count(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    monkeypatch.setattr(api, "call", fake_call(client))
    # A header-click sort stays last, as with the Apply button in the page.
    client.post(f"/grids/{grid}/header-sort", json={"keys": [["id", "desc"]]}, headers=AUTH)
    result = json.loads(
        tool("steps_apply_sql", {"grid": grid, "sql": "SELECT * FROM grid WHERE amount > 50"})
    )
    assert result["step_error"] is None and result["row_count"] == 5 and result["index"] == 0
    state = client.get(f"/grids/{grid}", headers=AUTH).json()
    assert [s["kind"] for s in state["steps"]] == ["sql", "sort"]
    view = json.loads(tool("steps_put", {"grid": grid, "steps": []}))
    assert view["row_count"] == 10


def test_duckdb_errors_reach_the_agent_in_full(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A conversion error quotes the cell ("Could not convert string 'North'..."), which helps the
    agent fix its query."""
    grid = grid_id(push(client, "orders", ORDERS))
    monkeypatch.setattr(api, "call", fake_call(client))
    failed = client.post(
        "/agent/query",
        json={"grid": grid, "sql": "SELECT count(*) FROM grid WHERE CAST(region AS INTEGER) > 0"},
        headers=AGENT,
    )
    assert failed.status_code == 400 and "North" in failed.text and failed.text.startswith("DuckDB:")
    steps(client, grid, [{"kind": "sql", "params": {"sql": "SELECT CAST(region AS INTEGER) AS n FROM grid"}}])
    with pytest.raises(Exception) as caught:
        tool("agent_read_grid", {"grid": grid})
    assert "North" in str(caught.value)
