"""The Key Finder: likely join columns between grids, with the percent match each way.

All data is made up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
from starlette.testclient import TestClient

from swissarmygrid.engine.keyfinder import score
from tests.conftest import AGENT, AUTH, grid_id, make_client, push

N = 2000
# Orders point at customers by customer_id; 1,900 of the 2,000 customer ids are ever ordered.
ORDERS = pa.table(
    {
        "order_id": pa.array(range(1, 3 * N + 1), pa.int64()),
        "customer_id": pa.array([i % (N - 100) + 1 for i in range(3 * N)], pa.int64()),
        "status": pa.array(["Y" if i % 2 else "N" for i in range(3 * N)]),
        "comment": pa.array(["x" * 200 if i == 0 else "ok" for i in range(3 * N)]),
        "paid": pa.array([i % 3 == 0 for i in range(3 * N)]),
    }
)
CUSTOMERS = pa.table(
    {
        # Text ids, padded and as decimals elsewhere: they still meet the integer customer_id.
        "id": pa.array([f" {i} " if i % 7 == 0 else str(i) for i in range(1, N + 1)]),
        "active": pa.array(["Y" if i % 3 else "N" for i in range(N)]),
        "region_code": pa.array([f"R{i % 40}" for i in range(N)]),
        "parent": pa.array([float(i // 2) if i > 1 else None for i in range(1, N + 1)]),
    }
)


def run(client: TestClient, grid: str, target: str, headers: dict[str, str] = AUTH, **options: Any) -> Any:
    response = client.post(
        f"/grids/{grid}/key-finder", json={"target": target, "options": options}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_finds_the_foreign_key_first_with_the_percent_match(client: TestClient) -> None:
    orders = grid_id(push(client, "orders", ORDERS))
    customers = grid_id(push(client, "customers", CUSTOMERS))
    result = run(client, orders, customers)
    best = result["pairs"][0]
    assert (best["source_column"], best["target_column"]) == ("customer_id", "id")
    assert best["shared"] == N - 100
    assert best["source_found"] == 1.0  # every ordered customer exists
    assert best["target_found"] == round((N - 100) / N, 4)  # 95% of customers have orders
    assert best["relationship"].startswith("many to one")
    # The Y/N status matches active perfectly, but only two values: well below the key.
    flag = next(p for p in result["pairs"] if p["source_column"] == "status")
    assert flag["score"] < 0.2 < best["score"]
    reasons = {s["column"]: s["reason"] for s in result["skipped"]["source"]}
    assert "200 characters" in reasons["comment"] and "BOOLEAN" in reasons["paid"]


def test_trim_and_case_switches(client: TestClient) -> None:
    orders = grid_id(push(client, "orders", ORDERS))
    customers = grid_id(push(client, "customers", CUSTOMERS))
    exact = run(client, orders, customers, trim=False)
    best = next(p for p in exact["pairs"] if p["target_column"] == "id")
    assert best["shared"] < N - 100  # the padded ids no longer meet


def test_self_join_finds_parent_to_id_and_skips_a_column_on_itself(client: TestClient) -> None:
    customers = grid_id(push(client, "customers", CUSTOMERS))
    result = run(client, customers, customers)
    pairs = {(p["source_column"], p["target_column"]) for p in result["pairs"]}
    assert ("parent", "id") in pairs  # 1.0, 2.0 ... meet "1", "2" ...
    assert ("id", "id") not in pairs
    parent = next(p for p in result["pairs"] if (p["source_column"], p["target_column"]) == ("parent", "id"))
    assert parent["source_found"] == 1.0


def test_all_grids_and_steps_count(client: TestClient) -> None:
    orders = grid_id(push(client, "orders", ORDERS))
    push(client, "customers", CUSTOMERS)
    client.post(
        f"/grids/{orders}/steps",
        json={"steps": [{"kind": "filter", "params": {"column": "customer_id", "op": "lt", "value": 11}}]},
        headers=AUTH,
    )
    result = run(client, orders, "*")
    best = result["pairs"][0]
    assert best["target"] == "customers" and best["shared"] == 10  # the filter shapes the source


def test_samples_for_the_page_and_counts_only_for_the_agent(client: TestClient) -> None:
    orders = grid_id(push(client, "orders", ORDERS))
    customers = grid_id(push(client, "customers", CUSTOMERS))
    body = {"target": customers, "source_column": "customer_id", "target_column": "id"}
    sample = client.post(f"/grids/{orders}/key-finder/sample", json=body, headers=AUTH).json()
    assert sample["matched_count"] == N - 100 and sample["missing_count"] == 0
    assert sample["matched"][:3] == ["1", "10", "100"]
    back = client.post(
        f"/grids/{customers}/key-finder/sample",
        json={"target": orders, "source_column": "id", "target_column": "customer_id"},
        headers=AUTH,
    ).json()
    assert back["missing_count"] == 100  # customers who never ordered
    assert client.post(f"/grids/{orders}/key-finder/sample", json=body, headers=AGENT).status_code == 403
    agent = run(client, orders, customers, headers=AGENT)
    assert "matched" not in str(agent) and agent["pairs"][0]["target_column"] == "id"


# ===== the most distinct column combination (this grid's own key) =====

# 10 stores x 300 days: no column alone tells the rows apart, store and day together do.
STOCK = pa.table(
    {
        "store": pa.array([f"S{i % 10}" for i in range(3000)]),
        "day": pa.array([i // 10 for i in range(3000)], pa.int64()),
        "units": pa.array([(i * 7) % 50 for i in range(3000)], pa.int64()),
        "region": pa.array(["East" if i % 10 < 5 else None for i in range(3000)]),
    }
)


def test_most_distinct_combination_is_the_composite_key(client: TestClient) -> None:
    stock = grid_id(push(client, "stock", STOCK))
    found = run(client, stock, stock)["unique_key"]
    best = found["steps"][-1]
    assert set(best["columns"]) == {"store", "day"} and best["unique"] and best["repeated_rows"] == 0
    assert found["rows_checked"] == 3000 and not found["limited"]
    # The build: the most distinct column first, then the one that separates the most rows left.
    assert found["steps"][0] == {
        "columns": ["day"],
        "distinct": 300,
        "share": 0.1,
        "repeated_rows": 2700,
        "unique": False,
    }


def test_a_unique_column_alone_is_the_answer_and_others_are_alternatives(client: TestClient) -> None:
    coded = CUSTOMERS.append_column("code", pa.array([f"C{i}" for i in range(N)]))
    customers = grid_id(push(client, "customers", coded))
    found = run(client, customers, "*")["unique_key"]
    # Two columns are unique per row alone; the first in table order is the answer.
    assert [s["columns"] for s in found["steps"]] == [["id"]]
    assert [a["columns"] for a in found["alternatives"]] == [["code"]]


def test_duplicate_rows_have_no_unique_combination(client: TestClient) -> None:
    doubled = pa.concat_tables([STOCK, STOCK.slice(0, 5)])
    grid = grid_id(push(client, "doubled", doubled))
    best = run(client, grid, grid)["unique_key"]["steps"][-1]
    assert not best["unique"] and best["repeated_rows"] == 5


def test_only_the_first_rows_are_checked(tmp_path: Path) -> None:
    with make_client(tmp_path, key_finder_unique_rows=10) as client:
        stock = grid_id(push(client, "stock", STOCK))
        found = run(client, stock, stock)["unique_key"]
        # The first 10 rows are one day across the 10 stores, so store alone is unique there.
        assert found["rows_checked"] == 10 and found["limited"] and found["row_limit"] == 10
        assert found["steps"][-1]["columns"] == ["store"] and found["steps"][-1]["unique"]


def test_score_rewards_overlap_and_enough_shared_values() -> None:
    assert score(1000, 1000, 1000) == 1.0
    # A running counter that happens to contain every id ranks below the real reference.
    assert score(2000, 6000, 2000) < score(1900, 1900, 2000)
    assert score(2, 2, 2) < 0.15
    assert score(0, 10, 10) == 0.0
    assert score(500, 1000, 1000) < score(1000, 1000, 1000)
