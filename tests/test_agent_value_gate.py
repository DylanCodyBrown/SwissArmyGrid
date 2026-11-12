"""The routes the grid page uses to show cell values must honor the agent's privacy level.

The CLI hands an agent every route the page has. Before it did, `/grids/{id}/rows`, `/cell`, `/sql`
and friends answered an `X-Actor: agent` caller at any level, because only `/agent/*` checked. These
tests pin the fix: below level 3 the page-only value routes refuse the agent (pointing it at
`read_grid` / `query`, which mask and ask), the user is never affected, and routes that return no
values (CREATE TABLE, counts, card metadata) keep working at every level.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from tests.conftest import AGENT, AUTH, grid_id, push

TABLE = pa.table({"client": pa.array(["Acme", "Globex"]), "balance": pa.array([100.0, 250.5])})


def set_level(client: TestClient, level: int) -> None:
    assert client.post("/agent/policy", json={"level": level}, headers=AUTH).status_code == 200


def open_grid(client: TestClient) -> str:
    return grid_id(push(client, "clients", TABLE))


VALUE_ROUTES: list[tuple[str, str, dict[str, Any]]] = [
    ("GET", "/grids/{g}/rows?start=0&end=2", {}),
    ("GET", "/grids/{g}/cell?row=0&column=client", {}),
    ("GET", "/grids/{g}/profile?column=balance", {}),
    ("GET", "/grids/{g}/header-stats", {}),
    ("POST", "/grids/{g}/selection-stats", {"json": {"selection": {"rows": [[0, 1]], "columns": None}}}),
    ("POST", "/grids/{g}/sql", {"json": {"sql": "SELECT client FROM grid"}}),
]


@pytest.mark.parametrize("level", [0, 1, 2])
@pytest.mark.parametrize(("method", "path", "extra"), VALUE_ROUTES)
def test_page_value_routes_refuse_the_agent_below_level_3(
    client: TestClient, level: int, method: str, path: str, extra: dict[str, Any]
) -> None:
    gid = open_grid(client)
    set_level(client, level)
    refused = client.request(method, path.format(g=gid), headers=AGENT, **extra)
    assert refused.status_code == 403
    assert "read_grid" in refused.text or "query" in refused.text


@pytest.mark.parametrize(("method", "path", "extra"), VALUE_ROUTES)
def test_the_user_is_never_refused(client: TestClient, method: str, path: str, extra: dict[str, Any]) -> None:
    gid = open_grid(client)
    set_level(client, 0)
    assert client.request(method, path.format(g=gid), headers=AUTH, **extra).status_code == 200


def test_level_3_lets_the_agent_use_the_page_routes(client: TestClient) -> None:
    gid = open_grid(client)
    rows = client.get(f"/grids/{gid}/rows?start=0&end=2", headers=AGENT)
    assert rows.status_code == 200 and rows.json()["rows"][0][0] == "Acme"


def test_creating_a_table_returns_no_values_so_it_works_at_level_0(client: TestClient) -> None:
    gid = open_grid(client)
    set_level(client, 0)
    made = client.post(
        f"/grids/{gid}/sql", json={"sql": "CREATE TABLE big AS SELECT * FROM grid"}, headers=AGENT
    )
    assert made.status_code == 200 and made.json()["pinned"] == "big"


def test_counts_stay_open_at_level_0(client: TestClient) -> None:
    gid = open_grid(client)
    set_level(client, 0)
    assert client.get(f"/grids/{gid}/count", headers=AGENT).json()["row_count"] == 2
    assert client.get(f"/grids/{gid}/step-counts", headers=AGENT).status_code == 200


def test_card_listing_drops_values_for_the_agent_below_level_3(client: TestClient) -> None:
    gid = open_grid(client)
    added = client.post(
        f"/grids/{gid}/cards",
        json={"kind": "table", "title": "t", "sql": "SELECT client FROM grid"},
        headers=AGENT,
    )
    assert added.status_code == 200
    set_level(client, 0)
    card = client.get(f"/grids/{gid}/cards", headers=AGENT).json()["cards"][0]
    assert "rows" not in card and "columns" not in card and card["kind"] == "table"
    as_user = client.get(f"/grids/{gid}/cards", headers=AUTH).json()["cards"][0]
    assert as_user["rows"] == [["Acme"], ["Globex"]]
    assert "rows" not in client.get("/cards", headers=AGENT).json()["cards"][0]


def test_compare_rows_and_key_groups_refuse_the_agent_below_level_3(client: TestClient) -> None:
    left = open_grid(client)
    right = grid_id(push(client, "clients2", TABLE))
    made = client.post(
        "/compares", json={"left": left, "right": right, "keys": [["client", "client"]]}, headers=AGENT
    )
    assert made.status_code == 200
    cid = made.json()["compare"]
    set_level(client, 1)
    assert client.get(f"/compares/{cid}/rows?side=left", headers=AGENT).status_code == 403
    assert client.get(f"/compares/{cid}/key-group?row=0", headers=AGENT).status_code == 403
    assert client.get(f"/compares/{cid}", headers=AGENT).status_code == 200  # counts only
