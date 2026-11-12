"""Ways an agent got cell values past its privacy level, each pinned so it stays closed.

Every test uses the made-up value "Acme Secret" and checks it never reaches an agent at a level
that should hide it. The user (no X-Actor header) is never affected.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from tests.conftest import AGENT, AUTH, grid_id, push

SECRET = "Acme Secret"
TABLE = pa.table({"client": pa.array([SECRET, "Globex"]), "balance": pa.array([100.0, 250.5])})


def set_policy(client: TestClient, level: int, **extra: Any) -> None:
    response = client.post("/agent/policy", json={"level": level, **extra}, headers=AUTH)
    assert response.status_code == 200, response.text


def open_grid(client: TestClient) -> str:
    return grid_id(push(client, "clients", TABLE))


# ===== DuckDB error messages quote the value that failed to convert =====


def _error_routes(gid: str) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (
            "POST",
            f"/grids/{gid}/sql",
            {"json": {"sql": "CREATE TABLE x AS SELECT CAST(client AS INT) FROM grid"}},
        ),
        ("POST", "/datasets/q1/query", {"json": {"sql": "SELECT CAST(client AS INT) AS n FROM clients"}}),
        (
            "POST",
            "/agent/query",
            {"json": {"dataset": "clients", "sql": "SELECT sum(CAST(client AS INT)) FROM grid"}},
        ),
    ]


@pytest.mark.parametrize("level", [0, 1])
@pytest.mark.parametrize("index", [0, 1, 2])
def test_engine_errors_never_quote_a_value_to_an_agent_below_level_2(
    client: TestClient, level: int, index: int
) -> None:
    gid = open_grid(client)
    set_policy(client, level)
    method, path, extra = _error_routes(gid)[index]
    refused = client.request(method, path, headers=AGENT, **extra)
    assert refused.status_code == 400
    assert SECRET not in refused.text
    assert "hidden" in refused.text


def test_a_failing_view_count_hides_the_value_too(client: TestClient) -> None:
    gid = open_grid(client)
    step = {"kind": "sql", "params": {"sql": "SELECT * FROM grid WHERE CAST(client AS INT) > 0"}}
    assert client.post(f"/grids/{gid}/steps", json={"steps": [step]}, headers=AUTH).status_code == 200
    set_policy(client, 0)
    for path in (f"/grids/{gid}/count", f"/grids/{gid}/step-counts"):
        refused = client.get(path, headers=AGENT)
        assert refused.status_code == 400 and SECRET not in refused.text


def test_the_user_and_a_level_2_agent_still_get_the_full_message(client: TestClient) -> None:
    gid = open_grid(client)
    body = {"sql": "CREATE TABLE x AS SELECT CAST(client AS INT) FROM grid"}
    set_policy(client, 0)
    assert SECRET in client.post(f"/grids/{gid}/sql", json=body, headers=AUTH).text
    set_policy(client, 2)
    assert SECRET in client.post(f"/grids/{gid}/sql", json=body, headers=AGENT).text
