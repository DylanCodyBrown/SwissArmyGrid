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


def test_a_saved_dashboard_reopens_as_drawn_and_relinks_when_its_table_is_back(client: TestClient) -> None:
    sales = pa.table({"region": ["North", "North", "South"], "amount": [1.0, 2.0, 5.0]})
    gid = grid_id(push(client, "sales", sales))
    spec = {"mark": "bar", "x": {"field": "region"}, "y": {"aggregate": "sum", "field": "amount"}}
    chart = client.post(
        f"/grids/{gid}/cards",
        json={"kind": "chart", "title": "By region", "spec": spec, "size": {"w": 8, "h": 9}},
        headers=AUTH,
    )
    query = client.post(
        f"/grids/{gid}/cards",
        json={
            "kind": "table",
            "title": "Top",
            "sql": "SELECT region FROM sales ORDER BY amount DESC LIMIT 1",
        },
        headers=AUTH,
    )
    assert chart.status_code == query.status_code == 200
    client.post("/cards", json={"kind": "note", "title": "Reading", "body": "North leads."}, headers=AUTH)
    first = client.post("/dashboards", json={"name": "weekly"}, headers=AUTH).json()
    assert first["version"] == 1 and first["cards"] == 3
    client.delete(f"/cards/{chart.json()['card']}", headers=AUTH)
    assert client.post("/dashboards", json={"name": "weekly"}, headers=AUTH).json()["version"] == 2
    listed = client.get("/dashboards", headers=AGENT).json()["dashboards"]
    assert listed == [{"name": "weekly", "versions": 2, "saved": listed[0]["saved"], "cards": 2}]
    history = client.get("/dashboards/weekly", headers=AGENT).json()["versions"]
    assert [len(v["cards"]) for v in history] == [3, 2] and history[0]["cards"][0] == {
        "kind": "chart",
        "title": "By region",
    }

    client.delete(f"/grids/{gid}", headers=AUTH)
    client.delete("/datasets/sales", headers=AUTH)
    opened = client.post("/dashboards/weekly/open", json={"version": 1}, headers=AUTH).json()
    assert opened == {"name": "weekly", "version": 1, "cards": 3, "live": 0, "as_saved": 2}
    shown = {c["title"]: c for c in cards(client)}
    assert [c["title"] for c in cards(client)] == ["By region", "Top", "Reading"]
    frozen = shown["By region"]
    assert (
        frozen["grid"] is None
        and frozen["size"] == {"w": 8, "h": 9}
        and "open sales to redraw" in frozen["note"]
    )
    assert sorted(v["y"] for v in frozen["vega_lite"]["data"]["values"]) == [3.0, 5.0]
    assert shown["Top"]["rows"] == [["South"]]
    assert (
        client.post(f"/cards/{frozen['id']}/select", json={"value": "North"}, headers=AUTH).status_code == 404
    )

    again = grid_id(push(client, "sales", pa.table({"region": ["East"], "amount": [9.0]})))
    live = {c["title"]: c for c in cards(client)}
    assert (
        live["By region"]["grid"] == again
        and live["By region"]["vega_lite"]["data"]["values"][0]["x"] == "East"
    )
    assert live["Top"]["rows"] == [["East"]]
    assert client.delete("/dashboards/weekly", headers=AUTH).json() == {"deleted": "weekly"}
    assert client.get("/dashboards/weekly", headers=AUTH).status_code == 404


def test_a_figure_with_sql_is_filled_on_its_grid_and_follows_its_steps(client: TestClient) -> None:
    sales = pa.table({"region": ["North", "North", "South", "East"], "amount": [1.0, 2.0, 5.0, 4.0]})
    gid = grid_id(push(client, "sales", sales))
    sql = "SELECT region, sum(amount) AS total FROM sales GROUP BY region ORDER BY region"
    plotly = {
        "data": [{"type": "bar", "x": "region", "y": "total", "marker": {"color": "theme:accent"}}],
        "layout": {},
    }
    echarts = {
        "xAxis": {"type": "category"},
        "yAxis": {},
        "series": [{"type": "bar", "encode": {"x": "region", "y": "total"}}],
    }
    for kind, figure in (("plotly", plotly), ("echarts", echarts)):
        added = client.post(
            f"/grids/{gid}/cards",
            json={"kind": kind, "title": kind, "body": json.dumps(figure), "sql": sql},
            headers=AGENT,
        )
        assert added.status_code == 200, added.text
    shown = {c["title"]: c for c in cards(client)}
    bars = json.loads(shown["plotly"]["body"])["data"][0]
    assert (
        bars["x"] == ["East", "North", "South"]
        and bars["y"] == [4.0, 3.0, 5.0]
        and bars["marker"]["color"] == "theme:accent"
    )
    assert json.loads(shown["echarts"]["body"])["dataset"]["source"][0] == ["region", "total"]
    step = {"kind": "filter", "params": {"column": "region", "op": "ne", "value": "East"}}
    assert (
        client.post(f"/grids/{gid}/step-ops", json={"op": "add", "step": step}, headers=AUTH).status_code
        == 200
    )
    after = {c["title"]: c for c in cards(client)}
    assert json.loads(after["plotly"]["body"])["data"][0]["x"] == ["North", "South"]
    wrong = {"data": [{"type": "bar", "x": "nowhere", "y": "total"}]}
    client.post(
        f"/grids/{gid}/cards",
        json={"kind": "plotly", "title": "wrong", "body": json.dumps(wrong), "sql": sql},
        headers=AUTH,
    )
    assert "names 'nowhere', not a column" in {c["title"]: c for c in cards(client)}["wrong"]["error"]
    free = client.post(
        "/cards", json={"kind": "plotly", "title": "", "body": json.dumps(plotly), "sql": sql}, headers=AUTH
    )
    assert free.status_code == 400


def test_a_table_of_its_own_rows_on_a_grid_draws_beside_the_grids_cards(client: TestClient) -> None:
    gid = grid_id(push(client, "sales", pa.table({"region": ["North"]})))
    own = {"kind": "table", "title": "From a blade", "columns": ["shop", "rows"], "rows": [["Mill", 2]]}
    assert client.post(f"/grids/{gid}/cards", json=own, headers=AUTH).status_code == 200
    shown = cards(client)
    assert [c["title"] for c in shown] == ["From a blade"] and shown[0]["rows"] == [["Mill", 2]]
