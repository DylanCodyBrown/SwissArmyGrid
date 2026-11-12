"""One change to one step, by its index or id (POST /grids/{id}/step-ops), the way the agent's step
commands work: the server applies the change, so the other steps keep the values the user typed.
The agent sees those values too, on every route it reads steps through. Made-up data only.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from tests.conftest import AGENT, AUTH, grid_id, push

SECRET = "Acme Secret"
TABLE = pa.table(
    {
        "client": pa.array([SECRET, "Globex", "Initech"]),
        "balance": pa.array([100.0, 250.5, 3.0]),
        "region": pa.array(["east", "west", "east"]),
    }
)
USER_STEPS: list[dict[str, Any]] = [
    {"kind": "filter", "params": {"column": "client", "op": "ne", "value": SECRET}},
    {
        "kind": "categorize",
        "params": {
            "column": "client",
            "as": "kind",
            "rules": [{"type": "contains", "value": "Acme", "label": SECRET}],
            "unmatched": "Other " + SECRET,
        },
    },
    {"kind": "sql", "params": {"sql": f"SELECT * FROM grid WHERE client <> '{SECRET}'"}},  # noqa: S608 (test text)
    {"kind": "sort", "params": {"keys": [["balance", "desc"]]}},
]


def user_grid(client: TestClient) -> str:
    gid = grid_id(push(client, "clients", TABLE))
    assert client.post(f"/grids/{gid}/steps", json={"steps": USER_STEPS}, headers=AUTH).status_code == 200
    client.post("/workspace/active", json={"grid": gid}, headers=AUTH)
    return gid


def user_steps(client: TestClient, gid: str) -> list[dict[str, Any]]:
    return [
        {k: v for k, v in s.items() if k != "description"}
        for s in client.get(f"/grids/{gid}", headers=AUTH).json()["steps"]
    ]


def test_every_agent_view_of_the_steps_shows_their_values(client: TestClient) -> None:
    gid = user_grid(client)
    for path in (f"/grids/{gid}", f"/agent/view-state/{gid}", "/agent/context"):
        answer = client.get(path, headers=AGENT)
        assert answer.status_code == 200, answer.text
        assert SECRET in answer.text, path
    shown = client.get(f"/grids/{gid}", headers=AGENT).json()["steps"]
    assert [{k: v for k, v in s.items() if k != "description"} for s in shown] == user_steps(client, gid)


def test_the_agent_sees_the_plus_tabs_query(client: TestClient) -> None:
    grid_id(push(client, "clients", TABLE))
    made = client.post(
        "/datasets/picked/query",
        json={"sql": f"SELECT * FROM clients WHERE client = '{SECRET}'"},  # noqa: S608
        headers=AUTH,
    )
    shown = client.get(f"/grids/{made.json()['grid']}", headers=AGENT).json()
    assert SECRET in shown["query"]


@pytest.mark.parametrize(
    "op",
    [
        {"op": "toggle", "index": 0},
        {"op": "move", "index": 0, "to": 2},
        {"op": "add", "step": {"kind": "limit", "params": {"n": 2}}},
        {"op": "toggle", "index": 1, "enabled": False},
    ],
)
def test_one_step_changes_keep_the_users_values(client: TestClient, op: dict[str, Any]) -> None:
    gid = user_grid(client)
    before = user_steps(client, gid)
    changed = client.post(f"/grids/{gid}/step-ops", json=op, headers=AGENT)
    assert changed.status_code == 200, changed.text
    by_id = {s["id"]: s for s in user_steps(client, gid)}
    for step in before:  # every user step is still there with its params
        assert by_id[step["id"]]["params"] == step["params"]


def test_delete_and_edit_by_id(client: TestClient) -> None:
    gid = user_grid(client)
    first = user_steps(client, gid)[0]["id"]
    edited = client.post(
        f"/grids/{gid}/step-ops",
        json={"op": "edit", "id": first, "params": {"column": "balance", "op": "gt", "value": 50}},
        headers=AGENT,
    )
    assert edited.status_code == 200, edited.text
    assert user_steps(client, gid)[0]["params"] == {"column": "balance", "op": "gt", "value": 50}
    removed = client.post(f"/grids/{gid}/step-ops", json={"op": "delete", "id": first}, headers=AGENT)
    assert removed.status_code == 200 and len(user_steps(client, gid)) == 3


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"op": "explode"}, 400),
        ({"op": "toggle"}, 400),
        ({"op": "toggle", "index": "0"}, 400),
        ({"op": "toggle", "index": True}, 400),
        ({"op": "toggle", "index": 99}, 400),
        ({"op": "delete", "id": "nope"}, 404),
        ({"op": "edit", "index": 0, "params": "x"}, 400),
        ({"op": "move", "index": 0}, 400),
        ({"op": "add", "step": {"kind": "nonsense"}}, 400),
        ({"op": "add", "step": "filter"}, 400),
        ({"op": "toggle", "index": 0, "enabled": "yes"}, 400),
        ([1, 2], 400),
    ],
)
def test_hostile_step_ops_get_plain_errors(client: TestClient, body: Any, status: int) -> None:
    gid = user_grid(client)
    answer = client.post(f"/grids/{gid}/step-ops", json=body, headers=AGENT)
    assert answer.status_code == status, answer.text
    assert "Traceback" not in answer.text and answer.text.strip()


def test_a_step_error_in_duckdbs_words_reaches_the_agent(client: TestClient) -> None:
    gid = grid_id(push(client, "clients", TABLE))
    step = {"kind": "sql", "params": {"sql": "SELECT acme_secret_typo FROM grid"}}
    assert client.post(f"/grids/{gid}/steps", json={"steps": [step]}, headers=AUTH).status_code == 200
    for headers in (AUTH, AGENT):
        shown = client.get(f"/grids/{gid}", headers=headers).json()["step_error"]
        assert shown["index"] == 0 and "acme_secret_typo" in shown["message"]


# ===== the step commands, the way the MCP calls them =====


@pytest.fixture
def as_agent(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from swissarmygrid.cli import api
    from swissarmygrid.cli.api import CommandError

    def fake_call(method: str, path: str, body: Any = None, params: Any = None, timeout: float = 0) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        response = client.request(method, path, json=body, params=clean, headers=AGENT)
        if response.status_code >= 300:
            raise CommandError(f"{response.status_code}: {response.text}")
        return response.json()

    monkeypatch.setattr(api, "call", fake_call)
    return client


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("steps_toggle", {"index": 0}),
        ("steps_move", {"index": 3, "to": 0}),
        ("steps_add", {"kind": "limit", "params": {"n": 2}}),
        ("steps_edit", {"index": 3, "params": {"keys": [["balance", "asc"]]}}),
        ("steps_delete", {"index": 3}),
        ("steps_put", {"settings": {"case_sensitive": False}}),
        ("table_matching", {"trim_spaces": True}),
        ("row_limit", {"n": 1}),
        ("column_keep", {"columns": ["client", "balance"], "index": 4}),
    ],
)
def test_the_step_commands_keep_the_users_values(
    as_agent: TestClient, name: str, args: dict[str, Any]
) -> None:
    from swissarmygrid.cli.commands import execute

    gid = user_grid(as_agent)
    filter_id = user_steps(as_agent, gid)[0]["id"]
    execute(name, {"grid": gid, **args})
    survivors = {s["id"]: s for s in user_steps(as_agent, gid)}
    assert survivors[filter_id]["params"]["value"] == SECRET
