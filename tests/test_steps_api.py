"""Steps over HTTP: edit, header sort, preview, counts, errors, recipes, stale gens (M3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
from starlette.testclient import TestClient

from tests.conftest import AUTH, grid_id, make_client, push

ORDERS = pa.table(
    {
        "id": pa.array(range(1, 7), pa.int64()),
        "region": pa.array(["North", "South", "North", None, "South", "East"]),
        "qty": pa.array([5, 3, None, 1, 3, 10], pa.int64()),
    }
)


def set_steps(client: TestClient, grid: str, steps: list[dict[str, Any]], **extra: Any) -> Any:
    return client.post(f"/grids/{grid}/steps", json={"steps": steps, **extra}, headers=AUTH)


def ids(client: TestClient, grid: str, **params: Any) -> list[Any]:
    body = client.get(f"/grids/{grid}/rows", params=params, headers=AUTH).json()
    return [r[0] for r in body["rows"]]


def test_steps_change_the_view_and_counts(client: TestClient) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    steps = [
        {"kind": "filter", "params": {"column": "region", "op": "ne", "value": "East"}},
        {"kind": "sort", "params": {"keys": [["qty", "desc"]]}},
    ]
    described = set_steps(client, grid, steps).json()
    assert [s["description"] for s in described["steps"]] == [
        "Keep rows where region does not equal 'East'",
        "Sort by qty descending",
    ]
    # `ne` drops the blank region too (SQL semantics); blank qty sorts last.
    assert ids(client, grid) == [1, 2, 5, 3]
    assert client.get(f"/grids/{grid}/step-counts", headers=AUTH).json()["counts"] == [6, 4, 4]
    # Preview at step 1: the filter applied, the sort not yet.
    assert ids(client, grid, upto=1) == [1, 2, 3, 5]
    assert client.get(f"/grids/{grid}/count", params={"upto": 0}, headers=AUTH).json()["row_count"] == 6


def test_disabled_steps_are_skipped(client: TestClient) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    set_steps(client, grid, [{"kind": "limit", "params": {"n": 2}, "enabled": False}])
    assert len(ids(client, grid)) == 6


def test_header_sort_replaces_itself(client: TestClient) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    set_steps(client, grid, [{"kind": "filter", "params": {"column": "qty", "op": "not_blank"}}])
    client.post(f"/grids/{grid}/header-sort", json={"keys": [["qty", "asc"]]}, headers=AUTH)
    described = client.post(
        f"/grids/{grid}/header-sort", json={"keys": [["id", "desc"]]}, headers=AUTH
    ).json()
    assert [s["kind"] for s in described["steps"]] == ["filter", "sort"]
    assert ids(client, grid) == [6, 5, 4, 2, 1]
    cleared = client.post(f"/grids/{grid}/header-sort", json={"keys": []}, headers=AUTH).json()
    assert [s["kind"] for s in cleared["steps"]] == ["filter"]


def test_settings_apply(client: TestClient) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    set_steps(
        client,
        grid,
        [{"kind": "filter", "params": {"column": "region", "op": "eq", "value": "north"}}],
        settings={"case_sensitive": False},
    )
    assert ids(client, grid) == [1, 3]


def test_broken_step_is_marked_and_the_view_stops_before_it(client: TestClient) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    described = set_steps(
        client,
        grid,
        [
            {"kind": "limit", "params": {"n": 3}},
            {"kind": "filter", "params": {"column": "nope", "op": "blank"}},
            {"kind": "limit", "params": {"n": 1}},
        ],
    ).json()
    assert described["step_error"]["index"] == 1
    assert "nope" in described["step_error"]["message"]
    assert ids(client, grid) == [1, 2, 3]


def test_bad_step_shapes_are_400(client: TestClient) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    assert set_steps(client, grid, [{"kind": "explode"}]).status_code == 400
    assert client.post(f"/grids/{grid}/steps", json={"steps": "no"}, headers=AUTH).status_code == 400


def test_recipe_is_reapplied_and_column_drift_is_reported(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        grid = grid_id(push(client, "orders", ORDERS))
        set_steps(
            client,
            grid,
            [
                {"kind": "filter", "params": {"column": "qty", "op": "gt", "value": 2}},
                {"kind": "sort", "params": {"keys": [["region", "asc"]]}},
            ],
        )
        assert client.post(f"/grids/{grid}/recipe", headers=AUTH).status_code == 200
        # Same session: a re-push keeps the grid's steps (qty > 2, then region A-Z).
        push(client, "orders", ORDERS)
        assert ids(client, grid) == [6, 1, 2, 5]
    with make_client(tmp_path) as client:  # a new server process: recipe from disk
        fresh = grid_id(push(client, "orders", ORDERS))
        assert len(client.get(f"/grids/{fresh}", headers=AUTH).json()["steps"]) == 2
        assert ids(client, fresh) == [6, 1, 2, 5]
        # Push without the region column: the sort step (index 1) is the first broken one.
        drifted = ORDERS.drop_columns(["region"])
        push(client, "orders", drifted)
        described = client.get(f"/grids/{fresh}", headers=AUTH).json()
        assert described["step_error"]["index"] == 1
        assert ids(client, fresh) == [1, 2, 5, 6]


def test_stale_generation_is_refused(client: TestClient) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    set_steps(client, grid, [], gen=5)
    assert client.get(f"/grids/{grid}/rows", params={"gen": 4}, headers=AUTH).status_code == 409
    assert client.get(f"/grids/{grid}/rows", params={"gen": 5}, headers=AUTH).status_code == 200
    assert client.get(f"/grids/{grid}/rows", params={"gen": 6}, headers=AUTH).status_code == 200
    assert client.get(f"/grids/{grid}/rows", params={"gen": 5}, headers=AUTH).status_code == 409


def test_grid_lifecycle_routes(client: TestClient) -> None:
    push(client, "orders", ORDERS)
    made = client.post("/grids", json={"dataset": "orders"}, headers=AUTH).json()
    assert len(client.get("/grids", headers=AUTH).json()["grids"]) == 2
    assert client.delete(f"/grids/{made['grid']}", headers=AUTH).status_code == 200
    assert client.post("/grids", json={"dataset": "nope"}, headers=AUTH).status_code == 404


def test_two_pages_on_one_grid_never_make_each_other_stale(client: TestClient) -> None:
    """Regression: generations were per grid, so a second tab's requests came back 409 forever
    and its grid showed an empty placeholder row."""
    grid = grid_id(push(client, "orders", ORDERS))

    def rows(gen: int, page: str) -> Any:
        return client.get(f"/grids/{grid}/rows", params={"gen": gen, "client": page}, headers=AUTH)

    assert rows(7, "tab-a").status_code == 200
    assert rows(0, "tab-b").status_code == 200  # another page: its own counter
    assert rows(6, "tab-a").status_code == 409  # the same page, older: stale
    assert rows(1, "tab-b").status_code == 200
