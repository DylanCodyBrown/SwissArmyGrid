"""Relationship models: parse, merge, the ERD text, the routes and the shared library."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from swissarmygrid.engine.relationships import merge, mermaid_erd, parse_model
from tests.conftest import AGENT, AUTH, grid_id, push

MODEL: dict[str, Any] = {
    "name": "shop",
    "description": "Orders and customers",
    "tables": {"orders": {"primary_key": ["order_id"]}, "customers": {"primary_key": "customer_id"}},
    "relationships": [
        {"from": "orders", "from_columns": ["cust"], "to": "customers", "to_columns": ["customer_id"]}
    ],
}


def test_a_model_parses_merges_and_answers_the_join() -> None:
    model = parse_model(MODEL)
    assert model.keys == {"orders": ("order_id",), "customers": ("customer_id",)}
    assert model.between("orders", "customers") == [("cust", "customer_id")]
    assert model.between("customers", "orders") == [("customer_id", "cust")]  # either way round
    assert model.between("orders", "orders") == [("order_id", "order_id")]  # two copies: the key
    later = parse_model({"name": "fix", "tables": {"orders": {"primary_key": ["order_id", "line"]}}})
    merged = merge(model, later)
    assert merged.keys["orders"] == ("order_id", "line") and len(merged.relationships) == 1


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ({"tables": {"bad name!": {}}}, "dataset name"),
        (
            {"relationships": [{"from": "a", "from_columns": ["x", "y"], "to": "b", "to_columns": ["x"]}]},
            "as many",
        ),
        (
            {
                "relationships": [
                    {"from": "a", "from_columns": "x", "to": "b", "to_columns": "x", "kind": "lots"}
                ]
            },
            "kind",
        ),
    ],
)
def test_a_bad_model_says_why(bad: dict[str, Any], reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_model(bad)


def test_the_erd_marks_keys_and_joins() -> None:
    text = mermaid_erd(parse_model(MODEL), {"orders": [("order_id", "BIGINT"), ("cust", "VARCHAR")]})
    assert text.startswith("erDiagram")
    assert "bigint order_id PK" in text and "varchar cust FK" in text
    assert 'orders }o--|| customers : ""' in text  # crow's feet, no column names on the line
    noted = parse_model(
        MODEL | {"relationships": [MODEL["relationships"][0] | {"note": 'two "rows" orphaned'}]}
    )
    assert "orders }o--|| customers : \"two 'rows' orphaned\"" in mermaid_erd(noted, {})


def test_the_routes_merge_replace_and_draw(client: TestClient) -> None:
    grid = grid_id(push(client, "orders", pa.table({"order_id": [1, 2], "cust": ["a", "b"]})))
    # An agent may set relationships: keys and joins are metadata, not cell values.
    set_ = client.post("/relationships", json={"model": MODEL}, headers=AGENT)
    assert set_.status_code == 200 and set_.json()["tables"]["orders"]["primary_key"] == ["order_id"]
    bad = client.post("/relationships", json={"model": {"tables": {"x y": {}}}}, headers=AUTH)
    assert bad.status_code == 400
    erd = client.get("/relationships/erd", headers=AUTH).json()
    assert erd["tables"] == ["orders"] and "order_id PK" in erd["mermaid"]  # open tables only
    card = client.post(
        f"/grids/{grid}/cards",
        json={"kind": "diagram", "title": "ERD", "body": erd["mermaid"]},
        headers=AGENT,
    )
    assert card.status_code == 200
    cards = client.get(f"/grids/{grid}/cards", headers=AUTH).json()["cards"]
    assert cards[0]["kind"] == "diagram" and cards[0]["body"].startswith("erDiagram")
    cleared = client.post("/relationships", json={"model": {"name": "empty"}, "replace": True}, headers=AUTH)
    assert cleared.json()["relationships"] == []


def test_the_package_ships_no_models(client: TestClient) -> None:
    # The team's models no longer ship in the package: models come from a person's own folder
    # (next test), and later from relationship autodetection.
    library = client.get("/relationships/library", headers=AUTH).json()
    assert library["problems"] == []
    assert client.post("/relationships/library/nope", headers=AUTH).status_code == 404


def test_a_personal_library_folder_adds_models(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "team_models"
    folder.mkdir()
    (folder / "shop.json").write_text(json.dumps(MODEL), encoding="utf-8")
    (folder / "broken.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("SWISSARMYGRID_MODELS", str(folder))
    library = client.get("/relationships/library", headers=AUTH).json()
    assert "shop" in [m["name"] for m in library["models"]]
    assert any("broken.json" in p for p in library["problems"])


def test_a_star_schema_draws_with_its_hub_in_the_middle() -> None:
    """Every other spoke is written from the hub's side (ends swapped), so half rank above it."""
    spokes = [
        {"from": f"fact_{n}", "from_columns": ["k"], "to": "hub", "to_columns": ["k"], "kind": "many-to-one"}
        for n in range(4)
    ]
    text = mermaid_erd(
        parse_model({"name": "star", "tables": {"hub": {"primary_key": ["k"]}}, "relationships": spokes}), {}
    )
    lines = [line.strip() for line in text.splitlines() if "--" in line]
    assert lines == [
        'fact_0 }o--|| hub : ""',
        'hub ||--o{ fact_1 : ""',
        'fact_2 }o--|| hub : ""',
        'hub ||--o{ fact_3 : ""',
    ]
