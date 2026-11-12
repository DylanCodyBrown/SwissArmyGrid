"""The current-table context an agent is told about, and the commands that read it (routes_context)."""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from swissarmygrid.client.context_hook import summary
from tests.conftest import AGENT, AUTH, PORT, grid_id, push

# Copied per call: websocket_connect writes the subprotocols into the headers dict it is given.
HOST = {"Host": f"127.0.0.1:{PORT}"}
ORDERS = pa.table({"id": pa.array([1, 2, 3]), "region": pa.array(["North", "South", "North"])})


def test_the_agent_hears_which_table_is_in_front(client: TestClient) -> None:
    grid = grid_id(push(client, "orders", ORDERS))
    assert client.get("/agent/context", headers=AGENT).json()["active"] is None
    assert client.post("/workspace/active", json={"grid": grid}, headers=AGENT).status_code == 403
    client.post("/workspace/active", json={"grid": grid}, headers=AUTH)
    client.post(
        f"/grids/{grid}/steps",
        json={"steps": [{"kind": "filter", "params": {"column": "region", "op": "eq", "value": "North"}}]},
        headers=AUTH,
    )
    context = client.get("/agent/context", headers=AGENT).json()
    active = context["active"]
    assert active["dataset"] == "orders" and active["view_rows"] == 2 and active["dataset_rows"] == 3
    assert [c["name"] for c in active["columns"]] == ["id", "region"]
    assert active["steps"][0]["params"]["value"] == "North"  # step values shown
    assert "privacy_level" not in context
    text = summary(context)
    assert "orders" in text and "2 rows in the view of 3" in text and "region (VARCHAR)" in text
    assert "privacy" not in text


def test_the_hook_prints_nothing_without_a_table_in_front() -> None:
    assert summary({"active": None, "open_tables": []}) == ""


def test_the_commands_get_python_compare_and_close(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from swissarmygrid.cli import api
    from swissarmygrid.cli.api import CommandError
    from swissarmygrid.cli.commands import execute

    def fake_call(method: str, path: str, body: Any = None, params: Any = None, timeout: float = 0) -> Any:
        response = client.request(method, path, json=body, params=params, headers=AGENT)
        if response.status_code >= 400:
            raise CommandError(f"{response.status_code}: {response.text}")
        return response.json()

    monkeypatch.setattr(api, "call", fake_call)
    left = grid_id(push(client, "orders", ORDERS))
    right = grid_id(push(client, "orders2", pa.table({"id": pa.array([2, 3, 4])})))
    client.post(
        f"/grids/{left}/steps",
        json={"steps": [{"kind": "filter", "params": {"column": "region", "op": "eq", "value": "North"}}]},
        headers=AUTH,
    )
    code = json.loads(execute("steps_python", {"grid": left, "target": "pandas"}))
    assert code["verified"] is True and "North" in code["code"] and "difference" not in code
    counts = json.loads(execute("compare_grids", {"left": left, "right": right, "keys": [["id", "id"]]}))[
        "counts"
    ]
    assert counts == {"matched": 1, "left_only": 1, "right_only": 2}  # the view has ids 1 and 3
    assert client.get("/grids", headers=AUTH).status_code == 200
    execute("tabs_close_grid", {"grid": right})
    assert client.get(f"/grids/{right}", headers=AUTH).status_code == 404
