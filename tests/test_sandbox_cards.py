"""Cards of no table: what sag.sandbox makes of each kind of thing (stand-ins of the right shape, since
duck typing is all it uses), keys that replace a card in place, and what the agent may not send."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from swissarmygrid.client import sandbox_client
from swissarmygrid.client.sandbox_client import card_for
from tests.conftest import AGENT, AUTH, grid_id, make_client, push

PNG = b"\x89PNG\r\n\x1a\n" + b"made-up picture"


class FakeFigure:  # the shape of a matplotlib figure
    def savefig(self, buffer: Any, **options: Any) -> None:
        assert options["format"] == "png"
        buffer.write(PNG)


class FakeAxes:  # an Axes, or a seaborn grid: the figure behind it
    figure = FakeFigure()


class FakePlotly:
    def to_plotly_json(self) -> dict[str, Any]:
        return {}

    def to_json(self) -> str:
        return '{"data": [{"type": "bar", "x": ["a"], "y": [1]}], "layout": {}}'


def test_each_kind_of_thing_becomes_its_card(tmp_path: Path) -> None:
    picture = card_for(FakeFigure())
    assert picture["kind"] == "image" and base64.b64decode(picture["body"].split(",", 1)[1]) == PNG
    assert card_for(FakeAxes())["kind"] == "image"
    assert card_for(FakePlotly())["kind"] == "plotly"
    assert card_for(PNG)["body"].startswith("data:image/png;base64,")
    assert card_for("## Findings\nSales rose.") == {"kind": "note", "body": "## Findings\nSales rose."}
    assert card_for("flowchart LR\n A --> B", kind="mermaid")["kind"] == "diagram"
    assert (
        card_for({"$schema": "https://vega.github.io/schema/vega-lite/v5.json", "mark": "bar"})["kind"]
        == "vegalite"
    )
    (tmp_path / "flow.dot").write_text("digraph { a -> b }")
    (tmp_path / "chart.png").write_bytes(PNG)
    assert card_for(tmp_path / "flow.dot") == {"kind": "graphviz", "body": "digraph { a -> b }"}
    assert card_for(str(tmp_path / "chart.png"))["kind"] == "image"
    frame = pd.DataFrame(
        {"store": [f"S{i}" for i in range(150)], "sold": [date(2024, 1, 1)] * 150, "amount": [1.5] * 150}
    )
    frame.loc[3, "amount"] = float("nan")
    table = card_for(frame)
    assert table["columns"] == ["store", "sold", "amount"] and len(table["rows"]) == 100 and table["capped"]
    assert table["rows"][0] == ["S0", "2024-01-01", 1.5] and table["rows"][3][2] is None
    arrow = card_for(pa.table({"n": [1, 2]}))
    assert arrow == {"kind": "table", "columns": ["n"], "rows": [[1], [2]], "capped": False}
    with pytest.raises(TypeError, match="Can't put a object"):
        card_for(object())
    with pytest.raises(ValueError, match="kind is one of"):
        card_for("text", kind="powerpoint")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with make_client(tmp_path) as test_client:
        yield test_client


def cards(client: TestClient) -> list[dict[str, Any]]:
    shown: list[dict[str, Any]] = client.get("/cards", headers=AUTH).json()["cards"]
    return shown


def test_sag_sandbox_adds_a_card_of_no_table_and_a_key_replaces_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def call(method: str, path: str, body: bytes | None = None, content_type: str = "") -> Any:
        response = client.request(
            method, path, content=body, headers=AUTH | {"Content-Type": "application/json"}
        )
        assert response.status_code == 200, response.text
        return response.json()

    monkeypatch.setattr(sandbox_client, "_call", call)
    first = sandbox_client.sandbox(FakeFigure(), "Sales", key="sales-chart", size=(8, 12))
    note = sandbox_client.sandbox("A note")
    again = sandbox_client.sandbox(FakePlotly(), "Sales again", key="sales-chart")
    assert again == first
    shown = {c["id"]: c for c in cards(client)}
    assert len(shown) == 2 and shown[note]["origin"] == "Python" and shown[note]["grid"] is None
    replaced = shown[first]
    assert replaced["kind"] == "plotly" and replaced["title"] == "Sales again" and "body" in replaced
    assert replaced["size"] == {"w": 8, "h": 12}  # the size it was given first stays
    assert [c["id"] for c in cards(client)] == [first, note]  # and so does its place


def test_cards_of_no_table_stay_when_grids_close_and_refuse_what_needs_a_grid(client: TestClient) -> None:
    gid = grid_id(push(client, "sales", pa.table({"region": ["North", "South"], "amount": [1.0, 2.0]})))
    on_grid = client.post(
        f"/grids/{gid}/cards", json={"kind": "note", "title": "on the grid", "body": "x"}, headers=AUTH
    )
    free = client.post(
        "/cards",
        json={"kind": "table", "title": "mine", "columns": ["a"], "rows": [[1], [None]]},
        headers=AUTH,
    )
    assert on_grid.status_code == 200 and free.status_code == 200
    for refused in (
        {"kind": "chart", "spec": {"mark": "bar", "x": {"field": "region"}, "y": {"aggregate": "count"}}},
        {"kind": "table", "sql": "SELECT 1"},
        {"kind": "table", "columns": ["a"], "rows": [[{"nested": 1}]]},
        {"kind": "note", "body": "x", "key": ""},
    ):
        assert client.post("/cards", json={"title": "", **refused}, headers=AUTH).status_code == 400, refused
    assert client.delete(f"/grids/{gid}", headers=AUTH).status_code == 200
    assert [c["title"] for c in cards(client)] == ["mine"]


def test_a_vega_lite_card_is_inline_data_from_the_users_python_never_the_agents(client: TestClient) -> None:
    spec = {"mark": "bar", "data": {"values": [{"a": "x", "b": 1}]}, "encoding": {"x": {"field": "a"}}}
    body = {"kind": "vegalite", "title": "Altair", "body": json.dumps(spec)}
    assert client.post("/cards", json=body, headers=AUTH).status_code == 200
    assert client.post("/cards", json=body, headers=AGENT).status_code == 403
    fetching = {**spec, "data": {"url": "https://example.com/data.csv"}}
    linked = {**spec, "encoding": {"href": {"field": "a"}}}
    for bad in (fetching, linked):
        answer = client.post("/cards", json={**body, "body": json.dumps(bad)}, headers=AUTH)
        assert answer.status_code == 400 and "no url or href" in answer.text


def test_the_agent_adds_a_file_and_reads_the_formats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from swissarmygrid.cli import api, commands

    assert "plotly" in commands.REGISTRY["sandbox_formats"].run({})["kinds"]
    (tmp_path / "plan.md").write_text("# Plan\n- one")
    sent: list[tuple[str, str, Any]] = []

    def call(method: str, path: str, body: Any = None) -> Any:
        sent.append((method, path, body))
        return {"card": "x"}

    monkeypatch.setattr(api, "call", call)
    commands.REGISTRY["sandbox_add"].run({"file": str(tmp_path / "plan.md"), "key": "plan"})
    assert sent == [("POST", "/cards", {"kind": "note", "body": "# Plan\n- one", "key": "plan", "title": ""})]
