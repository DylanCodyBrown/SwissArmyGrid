"""The "+" tab: a new table built by one SELECT over the open tables, each read as its tab shows it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from tests.conftest import AUTH, grid_id, make_client, push

ORDERS = pa.table({"id": [1, 2, 3], "region": ["N", "S", "N"], "qty": [5, 3, 7]})
PEOPLE = pa.table({"id": [1, 2, 3], "name": ["Ann", "Bo", "Cy"]})
NORTH = [{"kind": "filter", "params": {"column": "region", "op": "eq", "value": "N"}}]


def build(client: TestClient, name: str, text: str, replace: bool = False) -> Any:
    return client.post(f"/datasets/{name}/query", json={"sql": text, "replace": replace}, headers=AUTH)


def steps(client: TestClient, grid: str, step_list: list[dict[str, Any]]) -> None:
    response = client.post(f"/grids/{grid}/steps", json={"steps": step_list}, headers=AUTH)
    assert response.status_code == 200, response.text


def ids(client: TestClient, grid: str) -> list[Any]:
    return [row[0] for row in client.get(f"/grids/{grid}/rows", headers=AUTH).json()["rows"]]


def test_a_table_reads_as_its_tab_shows_it(client: TestClient) -> None:
    steps(client, grid_id(push(client, "orders", ORDERS)), NORTH)
    made = build(client, "north", "SELECT id, qty FROM orders")
    assert made.status_code == 200, made.text
    grid = made.json()["grid"]
    assert ids(client, grid) == [1, 3]  # the filter step on the orders tab applied
    info = client.get(f"/grids/{grid}", headers=AUTH).json()
    assert info["query"] == "SELECT id, qty FROM orders"
    assert [c["name"] for c in info["columns"]] == ["id", "qty"]


def test_a_join_reads_every_tab_as_shown(client: TestClient) -> None:
    steps(client, grid_id(push(client, "orders", ORDERS)), NORTH)
    people = grid_id(push(client, "people", PEOPLE))
    steps(client, people, [{"kind": "filter", "params": {"column": "name", "op": "eq", "value": "Ann"}}])
    made = build(
        client, "joined", "SELECT o.id, p.name FROM orders o LEFT JOIN people p USING (id) ORDER BY o.id"
    )
    rows = client.get(f"/grids/{grid_id(made)}/rows", headers=AUTH).json()["rows"]
    assert rows == [[1, "Ann"], [3, None]]


def test_two_tabs_on_one_table_read_the_first(client: TestClient) -> None:
    push(client, "orders", ORDERS)
    second = client.post("/grids", json={"dataset": "orders"}, headers=AUTH).json()["grid"]
    steps(client, second, NORTH)
    assert ids(client, grid_id(build(client, "copy", "SELECT * FROM orders"))) == [1, 2, 3]


def test_a_table_with_no_tab_open_reads_its_rows(client: TestClient) -> None:
    orders = grid_id(push(client, "orders", ORDERS))
    steps(client, orders, NORTH)
    client.delete(f"/grids/{orders}", headers=AUTH)  # the tab closes; the dataset stays
    assert ids(client, grid_id(build(client, "copy", "SELECT * FROM orders"))) == [1, 2, 3]


def test_run_again_rebuilds_in_place_and_keeps_its_steps(client: TestClient) -> None:
    push(client, "orders", ORDERS)
    grid = grid_id(build(client, "q1", "SELECT * FROM orders"))
    steps(client, grid, NORTH)
    again = build(client, "q1", "SELECT * FROM orders WHERE qty < 7", replace=True)
    assert again.status_code == 200, again.text
    assert again.json()["grid"] == grid and again.json()["version"] == 2
    assert ids(client, grid) == [1]  # the new rows (ids 1, 2), then the tab's own filter step
    assert client.get(f"/grids/{grid}", headers=AUTH).json()["query"].endswith("qty < 7")


def test_a_name_in_use_is_refused(client: TestClient) -> None:
    orders = grid_id(push(client, "orders", ORDERS))
    taken = build(client, "orders", "SELECT 1 AS one")
    assert taken.status_code == 400 and "already exists" in taken.text
    # Run again rebuilds only a table that a query made; a pushed one is never replaced.
    assert build(client, "orders", "SELECT 1 AS one", replace=True).status_code == 400
    assert ids(client, orders) == [1, 2, 3]
    build(client, "q1", "SELECT 1 AS one")
    assert build(client, "q1", "SELECT 2 AS two").status_code == 400  # another "+" tab can't take it


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("SELECT * FROM q1", "can't read"),
        ("SELECT * FROM grid", "no grid view"),
        ("CREATE TABLE other AS SELECT 1 AS one", "one SELECT"),
        ("SELECT * FROM read_csv('C:/Windows/win.ini')", "not allowed"),
        ("SELECT * FROM nowhere", "Unknown table"),
    ],
)
def test_refusals(client: TestClient, text: str, message: str) -> None:
    push(client, "orders", ORDERS)
    build(client, "q1", "SELECT 1 AS one")
    refused = build(client, "q1", text, replace=True)
    assert refused.status_code == 400 and message in refused.text, refused.text


def test_a_bad_name_is_refused(client: TestClient) -> None:
    assert build(client, "bad name", "SELECT 1 AS one").status_code == 400


def test_a_slow_query_stops_and_builds_nothing(tmp_path: Path) -> None:
    with make_client(tmp_path, query_timeout_seconds=0.3) as client:
        slow = "SELECT count(*) AS n FROM range(3000000000) a(x) WHERE md5(x::VARCHAR) = 'x'"
        assert build(client, "slow", slow).status_code == 408
        assert "slow" not in {d["name"] for d in client.get("/datasets", headers=AUTH).json()["datasets"]}


def test_the_audit_log_records_the_query(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        push(client, "orders", ORDERS)
        build(client, "q1", "SELECT region FROM orders")
    lines = [json.loads(x) for x in (tmp_path / "state" / "audit.log").read_text().splitlines()]
    entry = next(e for e in lines if e["action"] == "sql_query")
    assert entry["dataset"] == "q1" and entry["query"] == "SELECT region FROM orders" and entry["rows"] == 3
