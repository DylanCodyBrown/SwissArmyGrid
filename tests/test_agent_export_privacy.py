"""An export the user approved for an agent holds what that agent may read, no more.

The approval click lets the agent write a file into the export folder, and an agent with its own
file tools can read that file. So the file must follow the same rules as `/agent/read`: hidden
columns left out, hashed columns hashed, text masked at level 1, the level's row cap, and no rows at
all at level 0 (only the profile, redacted like `agent_describe`). Made-up data only.
"""

from __future__ import annotations

import csv
import io
import threading
import time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from starlette.testclient import TestClient

from swissarmygrid.server.masking import hash_value, mask_text
from tests.conftest import AGENT, AUTH, grid_id, push

SECRET = "Acme Secret"
TABLE = pa.table(
    {
        "client": pa.array([SECRET, "Globex"]),
        "balance": pa.array([100.0, 250.5]),
        "region": pa.array(["east", "west"]),
    }
)


def set_policy(client: TestClient, level: int, **extra: Any) -> None:
    response = client.post("/agent/policy", json={"level": level, **extra}, headers=AUTH)
    assert response.status_code == 200, response.text


def _approve_next(client: TestClient) -> threading.Thread:
    def approve() -> None:
        for _ in range(250):
            pending = client.get("/agent/approvals", headers=AUTH).json()["pending"]
            if pending:
                client.post(f"/agent/approvals/{pending[0]['id']}", json={"approve": True}, headers=AUTH)
                return
            time.sleep(0.02)

    thread = threading.Thread(target=approve)
    thread.start()
    return thread


def ticket_for(client: TestClient, gid: str, **params: str) -> str:
    helper = _approve_next(client)
    approved = client.post(f"/agent/export-approval/{gid}", params={"format": "csv"} | params, headers=AGENT)
    helper.join()
    assert approved.status_code == 200, approved.text
    return str(approved.json()["ticket"])


def agent_export(client: TestClient, gid: str, **params: str) -> Any:
    fmt = params.get("format", "csv")
    ticket = ticket_for(client, gid, format=fmt, **({"scope": params["scope"]} if "scope" in params else {}))
    return client.get(
        f"/grids/{gid}/export",
        params={"format": fmt, "file": "out"} | params | {"ticket": ticket},
        headers=AGENT,
    )


def saved_csv(response: Any) -> list[list[str]]:
    assert response.status_code == 200, response.text
    text = Path(response.json()["saved"]).read_text(encoding="utf-8-sig")
    return [row for row in csv.reader(io.StringIO(text)) if row and not row[0].startswith("#")]


def open_grid(client: TestClient, table: pa.Table = TABLE) -> str:
    return grid_id(push(client, "clients", table))


def test_a_hidden_column_is_left_out_of_an_agent_export(client: TestClient) -> None:
    gid = open_grid(client)
    set_policy(client, 2, auto_rows=True, masks={"client": "hide"})
    rows = saved_csv(agent_export(client, gid, provenance="0"))
    assert rows[0] == ["balance", "region"]
    assert SECRET not in str(rows)


def test_a_hashed_column_is_hashed_in_an_agent_export(client: TestClient) -> None:
    gid = open_grid(client)
    set_policy(client, 2, masks={"client": "hash"})
    rows = saved_csv(agent_export(client, gid, provenance="0"))
    assert rows[0] == ["client", "balance", "region"]
    assert rows[1][0] == hash_value(SECRET) and SECRET not in str(rows)


def test_level_1_masks_text_in_an_agent_export(client: TestClient) -> None:
    gid = open_grid(client)
    set_policy(client, 1, masks={"region": "show"})
    rows = saved_csv(agent_export(client, gid, provenance="0"))
    assert rows[1] == [mask_text(SECRET), "100.0", "east"]
    assert SECRET not in str(rows)


def test_a_rename_step_does_not_lift_the_mask_in_an_export(client: TestClient) -> None:
    gid = open_grid(client)
    step = {"kind": "rename", "params": {"mapping": {"client": "who"}}}
    assert client.post(f"/grids/{gid}/steps", json={"steps": [step]}, headers=AUTH).status_code == 200
    set_policy(client, 2, masks={"client": "hide"})
    rows = saved_csv(agent_export(client, gid, provenance="0"))
    assert "who" not in rows[0] and SECRET not in str(rows)


def test_the_source_scope_is_masked_too(client: TestClient) -> None:
    gid = open_grid(client)
    set_policy(client, 2, masks={"client": "hide"})
    rows = saved_csv(agent_export(client, gid, scope="source", provenance="0"))
    assert rows[0] == ["balance", "region"]


def test_an_agent_export_keeps_to_the_levels_row_cap(client: TestClient) -> None:
    many = pa.table({"client": [f"c{i}" for i in range(250)], "balance": [float(i) for i in range(250)]})
    gid = open_grid(client, many)
    set_policy(client, 2)
    saved = agent_export(client, gid, provenance="0")
    assert len(saved_csv(saved)) == 1 + 200
    assert "200" in saved.json()["message"]


@pytest.mark.parametrize("fmt", ["parquet", "xlsx"])
def test_binary_formats_are_masked_as_well(client: TestClient, fmt: str) -> None:
    gid = open_grid(client)
    set_policy(client, 1)
    saved = agent_export(client, gid, format=fmt, provenance="0")
    assert saved.status_code == 200, saved.text
    path = Path(saved.json()["saved"])
    if fmt == "parquet":
        assert pq.read_table(path).column("client").to_pylist() == [mask_text(SECRET), mask_text("Globex")]
    else:
        from zipfile import ZipFile

        with ZipFile(path) as book:
            assert not any(SECRET.encode() in book.read(name) for name in book.namelist())


def test_level_0_refuses_a_row_export_before_asking_the_user(client: TestClient) -> None:
    gid = open_grid(client)
    set_policy(client, 0)
    refused = client.post(f"/agent/export-approval/{gid}", params={"format": "csv"}, headers=AGENT)
    assert refused.status_code == 403 and "level 0" in refused.text
    assert client.get("/agent/approvals", headers=AUTH).json()["pending"] == []


def test_level_0_refuses_a_row_export_even_with_a_ticket(client: TestClient) -> None:
    gid = open_grid(client)
    ticket = ticket_for(client, gid)
    set_policy(client, 0)
    refused = client.get(f"/grids/{gid}/export", params={"format": "csv", "ticket": ticket}, headers=AGENT)
    assert refused.status_code == 403 and SECRET not in refused.text


def test_level_0_exports_the_profile_without_text_values(client: TestClient) -> None:
    gid = open_grid(client)
    set_policy(client, 0)
    rows = saved_csv(agent_export(client, gid, scope="profile", provenance="0"))
    assert rows[0][0] == "column" and SECRET not in str(rows) and "Globex" not in str(rows)
    balance = next(r for r in rows if r[0] == "balance")
    assert "100.0" in balance and "250.5" in balance  # numbers stay, as in agent_describe


def test_level_3_and_the_user_export_everything(client: TestClient) -> None:
    gid = open_grid(client)
    assert SECRET in str(saved_csv(agent_export(client, gid, provenance="0")))
    set_policy(client, 0, masks={"client": "hide"})
    mine = client.get(f"/grids/{gid}/export", params={"format": "csv", "provenance": "0"}, headers=AUTH)
    assert SECRET in mine.text
