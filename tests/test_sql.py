"""The SQL box: every guard rule has a failing input (plan M3 acceptance), plus the happy paths."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from tests.conftest import AUTH, grid_id, make_client, push

ORDERS = pa.table({"id": [1, 2, 3], "region": ["N", "S", "N"], "qty": [5, 3, 7]})
PEOPLE = pa.table({"id": [1, 2], "name": ["Ann", "Bo"]})

REFUSED = [
    "SELECT * FROM read_csv('C:/Windows/win.ini')",
    "SELECT * FROM 'C:/Windows/win.ini'",
    "SELECT * FROM read_parquet('https://example.com/x.parquet')",
    "SELECT * FROM query('SELECT 1')",
    "COPY ds_orders TO 'C:/Users/Public/x.csv'",
    "ATTACH 'C:/x.db' AS other",
    "INSTALL httpfs",
    "LOAD httpfs",
    "SELECT 1; SELECT 2",
    "DROP TABLE ds_orders",
    "DELETE FROM orders",
    "UPDATE orders SET qty = 0",
    "INSERT INTO orders VALUES (9, 'X', 1)",
    "CREATE TABLE orders AS SELECT 1",
    "SELECT * FROM main.ds_orders",
    "SELECT * FROM ds_orders",
    "SET enable_external_access = true",
    "PRAGMA database_list",
    "SELECT * FROM duckdb_settings()",
    "SELECT * FROM glob('C:/*')",
]


def sql(client: TestClient, grid: str, text: str) -> object:
    return client.post(f"/grids/{grid}/sql", json={"sql": text}, headers=AUTH)


@pytest.mark.parametrize("text", REFUSED)
def test_guard_refuses(client: TestClient, text: str) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    response = sql(client, grid, text)
    assert response.status_code == 400, f"{text} -> {response.status_code} {response.text}"  # type: ignore[attr-defined]
    # The source is untouched.
    rows = client.get(f"/grids/{grid}/rows", headers=AUTH).json()["rows"]
    assert len(rows) == 3


def test_select_over_grid_and_join_by_name(client: TestClient) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    push(client, "people", PEOPLE)
    client.post(
        f"/grids/{grid}/steps",
        json={"steps": [{"kind": "filter", "params": {"column": "region", "op": "eq", "value": "N"}}]},
        headers=AUTH,
    )
    body = sql(client, grid, "SELECT sum(qty) AS total FROM grid").json()  # type: ignore[attr-defined]
    assert body["rows"] == [[12]] and body["columns"] == ["total"]
    joined = sql(
        client,
        grid,
        "SELECT o.id, p.name FROM orders o LEFT JOIN people p USING (id) ORDER BY o.id",
    ).json()  # type: ignore[attr-defined]
    # A dataset name reads the rows as loaded, even on its own tab (as the variable of that name
    # does in the user's Python); `grid` above is the view after the filter.
    assert joined["rows"] == [[1, "Ann"], [2, "Bo"], [3, None]]


def test_row_cap(tmp_path: Path) -> None:
    with make_client(tmp_path, max_sql_rows=2) as client:
        grid = grid_id(push(client, "orders", ORDERS))
        body = sql(client, grid, "SELECT * FROM grid").json()  # type: ignore[attr-defined]
        assert body["row_count"] == 2 and body["capped"] is True


def test_timeout_interrupts(tmp_path: Path) -> None:
    with make_client(tmp_path, query_timeout_seconds=0.3) as client:
        grid = grid_id(push(client, "orders", ORDERS))
        slow = "SELECT count(*) FROM range(3000000000) a(x) WHERE md5(x::VARCHAR) = 'x'"
        response = sql(client, grid, slow)
        assert response.status_code == 408  # type: ignore[attr-defined]
        assert sql(client, grid, "SELECT 1 AS one").status_code == 200  # type: ignore[attr-defined]


def test_pin_as_new_grid(client: TestClient) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    pinned = sql(client, grid, "CREATE TABLE north AS SELECT * FROM grid WHERE region = 'N'").json()  # type: ignore[attr-defined]
    assert pinned["pinned"] == "north"
    rows = client.get(f"/grids/{pinned['grid']}/rows", headers=AUTH).json()["rows"]
    assert [r[0] for r in rows] == [1, 3]
    listing = client.get("/datasets", headers=AUTH).json()
    assert {d["name"] for d in listing["datasets"]} == {"orders", "north"}


def test_audit_log_records_queries_without_values(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        grid = grid_id(push(client, "orders", ORDERS))
        sql(client, grid, "SELECT region FROM grid")
    lines = [json.loads(x) for x in (tmp_path / "state" / "audit.log").read_text().splitlines()]
    query = next(e for e in lines if e["action"] == "sql")
    assert query["query"] == "SELECT region FROM grid" and query["rows"] == 3
    assert "N" not in json.dumps([e for e in lines if e["action"] != "sql"])
