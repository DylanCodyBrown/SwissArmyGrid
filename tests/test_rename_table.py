"""Renaming a table (the usability test, 2026-11-14): a pasted table is named in its toast, any tab's
table is renamed by a double-click, and the new name holds everywhere: the DuckDB table, every grid on
it, the relationship model, the pages. Made-up data only."""

from __future__ import annotations

import pyarrow as pa
from starlette.testclient import TestClient

from swissarmygrid.domain.relationships import Model, Relationship, rename_table
from tests.conftest import AUTH, grid_id, push

ORDERS = pa.table({"order_id": [1, 2, 3], "region": ["North", "South", "North"]})


def test_the_model_carries_a_renamed_table() -> None:
    model = Model(
        keys={"orders": ("order_id",)},
        sources={"orders": "pasted"},
        relationships=(Relationship("orders", ("region",), "regions", ("region",)),),
        columns={"orders": (("order_id", "BIGINT"),)},
    )
    renamed = rename_table(model, "orders", "sales")
    assert renamed.keys == {"sales": ("order_id",)} and renamed.sources == {"sales": "pasted"}
    assert renamed.relationships[0].from_table == "sales" and renamed.relationships[0].to_table == "regions"
    assert "sales" in renamed.columns and "orders" not in renamed.columns
    assert rename_table(model, "nothing", "else") == model


def test_a_table_is_renamed_everywhere(client: TestClient) -> None:
    grid = grid_id(push(client, "pasted_101500", ORDERS))
    client.post("/grids", json={"dataset": "pasted_101500"}, headers=AUTH)
    client.post(
        "/relationships",
        json={"model": {"tables": {"pasted_101500": {"primary_key": ["order_id"]}}, "relationships": []}},
        headers=AUTH,
    )
    renamed = client.post("/datasets/pasted_101500/rename", json={"to": "orders"}, headers=AUTH)
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["dataset"] == "orders"
    names = [d["name"] for d in client.get("/datasets", headers=AUTH).json()["datasets"]]
    assert names == ["orders"]
    grids = client.get("/grids", headers=AUTH).json()["grids"]
    assert {g["dataset"] for g in grids} == {"orders"} and len(grids) == 2
    rows = client.get(f"/grids/{grid}/rows", headers=AUTH).json()["rows"]
    assert [r[0] for r in rows] == [1, 2, 3]
    assert client.get("/relationships", headers=AUTH).json()["tables"] == {
        "orders": {"primary_key": ["order_id"]}
    }
    # The SQL box names it by its new name.
    answer = client.post(f"/grids/{grid}/sql", json={"sql": "SELECT count(*) AS n FROM orders"}, headers=AUTH)
    assert answer.status_code == 200, answer.text


def test_a_rename_is_refused_when_the_name_is_taken_or_bad(client: TestClient) -> None:
    push(client, "orders", ORDERS)
    push(client, "regions", pa.table({"region": ["North"]}))
    taken = client.post("/datasets/orders/rename", json={"to": "Regions"}, headers=AUTH)
    assert taken.status_code == 409 and "regions" in taken.text
    bad = client.post("/datasets/orders/rename", json={"to": "no spaces here"}, headers=AUTH)
    assert bad.status_code == 400
    missing = client.post("/datasets/nothing/rename", json={"to": "else"}, headers=AUTH)
    assert missing.status_code == 404
    # Only the case changes: still one table.
    cased = client.post("/datasets/orders/rename", json={"to": "Orders"}, headers=AUTH)
    assert cased.status_code == 200, cased.text
    names = [d["name"] for d in client.get("/datasets", headers=AUTH).json()["datasets"]]
    assert names == ["Orders", "regions"]
