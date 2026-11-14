"""Tangs and the relationship finder over HTTP, on the made-up nine-table store.

The finder is scored on examples/sample_store.py's answer key: every true relationship found (the
two-column key, the text-to-number join, both calendar paths with ship_date inactive) and none of
its decoys. Then running a tang, saving one where coworkers see it, and moving one between people.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from examples.sample_store import DECOYS, TRUE_RELATIONSHIPS, make_store
from starlette.testclient import TestClient

from swissarmygrid.domain.relationships import read_tang
from tests.conftest import AGENT, AUTH, make_client, push


@pytest.fixture(scope="module")
def store() -> dict[str, pa.Table]:
    return {name: pa.Table.from_pandas(frame, preserve_index=False) for name, frame in make_store().items()}


@pytest.fixture
def client(tmp_path: Path, store: dict[str, pa.Table]) -> Iterator[TestClient]:
    with make_client(tmp_path) as test_client:
        for name, table in store.items():
            assert push(test_client, name, table).status_code == 200
        yield test_client


def answer_key() -> set[tuple[str, tuple[str, ...], str, tuple[str, ...]]]:
    return {(r.from_table, r.from_columns, r.to_table, r.to_columns) for r in TRUE_RELATIONSHIPS}


def found(suggestions: list[dict[str, Any]]) -> set[tuple[str, tuple[str, ...], str, tuple[str, ...]]]:
    return {(s["from"], tuple(s["from_columns"]), s["to"], tuple(s["to_columns"])) for s in suggestions}


def test_the_finder_finds_every_true_relationship_and_no_decoy(client: TestClient) -> None:
    answer = client.post("/relationships/find", json={}, headers=AUTH)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert found(body["suggestions"]) == answer_key()
    for a, ac, b, bc in DECOYS:
        assert not any(
            {(s["from"], s["from_columns"][0]), (s["to"], s["to_columns"][0])} == {(a, ac), (b, bc)}
            for s in body["suggestions"]
        )
    by = {(s["from"], tuple(s["from_columns"])): s for s in body["suggestions"]}
    assert by[("orders", ("ship_date",))].get("active") is False  # the second path to calendar
    assert by[("orders", ("order_date",))].get("active", True) is True
    assert by[("customers", ("home_store",))]["note"] == "text on one side, a number on the other"
    orphans = by[("orders", ("customer_id",))]
    assert orphans["orphans"] > 0 and "orphans" in orphans["evidence"]
    assert body["keys"]["order_lines"] == ["order_id", "line_no"]  # the two-column key


def test_accepting_suggestions_runs_clean_and_a_rejected_one_stays_away(client: TestClient) -> None:
    suggestions = client.post("/relationships/find", json={}, headers=AUTH).json()["suggestions"]
    model = {"name": "Sample store", "relationships": suggestions}
    assert client.post("/relationships", json={"model": model}, headers=AUTH).status_code == 200
    ran = client.post("/relationships/check", json={}, headers=AUTH).json()
    statuses = {(r["from"], tuple(r["from_columns"])): r["status"] for r in ran["results"]}
    assert statuses[("orders", ("customer_id",))] == "orphans"  # about 1% of orders: deleted accounts
    assert set(statuses.values()) <= {"holds", "orphans"} and ran["counts"]["holds"] >= 8
    customer = next(r for r in ran["results"] if r["from_columns"] == ["customer_id"])
    assert customer["orphan_samples"] and customer["actual_kind"] == "many-to-one"
    # Known joins are marked; a rejected one is not offered again, and undo brings it back.
    again = client.post("/relationships/find", json={}, headers=AUTH).json()["suggestions"]
    assert all(s["known"] for s in again)
    sku = {"from": "order_lines", "from_columns": ["sku"], "to": "products", "to_columns": ["sku"]}
    client.post("/relationships/reject", json=sku, headers=AUTH)
    assert ("order_lines", ("sku",), "products", ("sku",)) not in found(
        client.post("/relationships/find", json={}, headers=AUTH).json()["suggestions"]
    )
    client.post("/relationships/reject", json=sku | {"undo": True}, headers=AUTH)
    path = client.get(
        "/relationships/path", params={"from": "returns", "to": "categories"}, headers=AUTH
    ).json()
    assert [(j["from"], j["to"]) for j in path["joins"]] == [
        ("returns", "order_lines"),
        ("order_lines", "products"),
        ("products", "categories"),
    ]


def test_a_run_says_what_broke(client: TestClient) -> None:
    broken = {
        "name": "Broken",
        "relationships": [
            {
                "from": "orders",
                "from_columns": ["status_code"],
                "to": "customers",
                "to_columns": ["status_code"],
            },
            {
                "from": "orders",
                "from_columns": ["customer_id"],
                "to": "customers",
                "to_columns": ["gone_column"],
            },
            {"from": "orders", "from_columns": ["order_id"], "to": "warehouse", "to_columns": ["order_id"]},
        ],
    }
    client.post("/relationships", json={"model": broken, "replace": True}, headers=AUTH)
    results = client.post("/relationships/check", json={}, headers=AUTH).json()["results"]
    assert [r["status"] for r in results] == ["broken", "broken", "not open"]
    assert "repeat" in results[0]["detail"]  # customers.status_code is no key
    assert "customers.gone_column is no longer in the table" in results[1]["detail"]
    assert "Push warehouse" in results[2]["detail"]


def test_a_tang_saved_in_a_shared_folder_is_in_a_coworkers_list(client: TestClient, tmp_path: Path) -> None:
    team = tmp_path / "team share"
    (team / "finance").mkdir(parents=True)
    assert client.put("/blades/folders", json={"folders": [str(team)]}, headers=AUTH).status_code == 200
    suggestions = client.post("/relationships/find", json={}, headers=AUTH).json()["suggestions"]
    client.post("/relationships", json={"model": {"relationships": suggestions}}, headers=AUTH)
    tang = {
        "name": "Sample store",
        "description": "The made-up store's joins",
        "tags": ["sales"],
        "author": "Avery",
    }
    saved = client.post("/relationships/tangs", json=tang | {"folder": str(team)}, headers=AUTH).json()
    assert saved["where"] == "shared" and Path(saved["file"]).name == "sample_store.tang"
    on_disk = read_tang(Path(saved["file"]).read_text(encoding="utf-8"))
    assert on_disk.author == "Avery" and on_disk.saved_at and len(on_disk.relationships) == 10
    assert ("orders", "order_id") in {(t, c) for t, cols in on_disk.columns.items() for c, _ in cols}
    # A coworker: another server with the same shared folder lists it and applies it.
    with make_client(tmp_path / "coworker") as other:
        other.put("/blades/folders", json={"folders": [str(team)]}, headers=AUTH)
        listed = other.get("/relationships/library", headers=AUTH).json()
        entry = next(m for m in listed["models"] if m["name"] == "Sample store")
        assert entry["where"] == "shared" and entry["tang"] and entry["author"] == "Avery"
        assert entry["tags"] == ["sales"] and entry["relationships"] == 10
        applied = other.post("/relationships/library/Sample%20store", headers=AUTH).json()
        assert len(applied["relationships"]) == 10 and applied["changed"] == []
    # Only the person's own tangs or a shared folder: nowhere else on disk.
    elsewhere = client.post("/relationships/tangs", json=tang | {"folder": str(tmp_path)}, headers=AUTH)
    assert elsewhere.status_code == 400 and "shared folders" in elsewhere.text


def test_export_review_and_import_a_tang_file(client: TestClient) -> None:
    suggestions = client.post("/relationships/find", json={}, headers=AUTH).json()["suggestions"]
    client.post(
        "/relationships", json={"model": {"name": "Sample store", "relationships": suggestions}}, headers=AUTH
    )
    exported = client.get("/relationships/tang", headers=AUTH)
    assert exported.status_code == 200
    assert 'filename="sample_store.tang"' in exported.headers["content-disposition"]
    text = exported.text
    assert json.loads(text)["swissarmygrid"] == "tang"
    # Someone else's current relationships say order_lines -> orders is one-to-one: the review says
    # what importing would change, and applying does it.
    other = {
        "relationships": [
            {
                "from": "order_lines",
                "from_columns": ["order_id"],
                "to": "orders",
                "to_columns": ["order_id"],
                "kind": "one-to-one",
            }
        ]
    }
    client.post("/relationships", json={"model": other, "replace": True}, headers=AUTH)
    review = client.post("/relationships/import", json={"text": text}, headers=AUTH).json()
    assert review["changes"] == ["order_lines -> orders: one-to-one becomes many-to-one"]
    assert review["missing_tables"] == [] and "orders" in review["open_tables"]
    applied = client.post(
        "/relationships/import", json={"text": text, "apply": True, "save": True}, headers=AUTH
    ).json()
    assert applied["where"] == "mine" and len(applied["relationships"]) == 10
    plain = client.post("/relationships/import", json={"text": json.dumps({"name": "x"})}, headers=AUTH)
    assert plain.status_code == 400 and "not a tang" in plain.text


def test_the_agent_finds_runs_and_files_tangs(client: TestClient) -> None:
    assert (
        client.post(
            "/relationships/find", json={"tables": ["orders", "customers"]}, headers=AGENT
        ).status_code
        == 200
    )
    assert client.post("/relationships/check", json={}, headers=AGENT).status_code in (200, 400)
    # The golden rule: tang files are the agent's to save, export and import too.
    assert client.post("/relationships/tangs", json={"name": "x"}, headers=AGENT).status_code != 403
    assert client.get("/relationships/tang", headers=AGENT).status_code != 403
    assert client.post("/relationships/import", json={"text": "{}"}, headers=AGENT).status_code != 403
    one = client.post("/relationships/find", json={"tables": ["orders"]}, headers=AUTH)
    assert one.status_code == 400 and "two tables" in one.text


def test_the_finder_searches_open_tabs_and_names_a_one_to_one_join_once(client: TestClient) -> None:
    from tests.conftest import grid_id

    # Two copies of one table: their id columns join one to one, which is one join, not two.
    copy = pa.table({"customer_id": list(range(1, 51)), "tier": ["gold"] * 50})
    assert push(client, "tiers_a", copy).status_code == 200
    closing = grid_id(push(client, "tiers_b", copy))
    suggestions = client.post("/relationships/find", json={}, headers=AUTH).json()["suggestions"]
    pairs = [(s["from"], s["to"]) for s in suggestions if {s["from"], s["to"]} == {"tiers_a", "tiers_b"}]
    assert len(pairs) == 1, pairs
    # A closed tab's table stays in memory but is not searched (unless named).
    assert client.delete(f"/grids/{closing}", headers=AUTH).status_code == 200
    answer = client.post("/relationships/find", json={}, headers=AUTH).json()
    assert "tiers_b" not in answer["tables"]
    named = client.post("/relationships/find", json={"tables": ["tiers_a", "tiers_b"]}, headers=AUTH)
    assert named.json()["tables"] == ["tiers_a", "tiers_b"]
