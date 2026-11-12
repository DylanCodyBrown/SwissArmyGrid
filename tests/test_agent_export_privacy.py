"""An agent export waits for the user's click, and one click writes one file.

The click (POST /agent/export-approval) gives a one-time ticket for that grid, and the export route
writes an agent's file only against one, so skipping the click is refused by the server, not just by
the command that normally asks. The approved file holds the grid as the user sees it. Made-up data
only.
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

from tests.conftest import AGENT, AUTH, grid_id, push

SECRET = "Acme Secret"
TABLE = pa.table(
    {
        "client": pa.array([SECRET, "Globex"]),
        "balance": pa.array([100.0, 250.5]),
        "region": pa.array(["east", "west"]),
    }
)


def _answer_next(client: TestClient, approve: bool = True) -> threading.Thread:
    def answer() -> None:
        for _ in range(250):
            pending = client.get("/agent/approvals", headers=AUTH).json()["pending"]
            if pending:
                client.post(f"/agent/approvals/{pending[0]['id']}", json={"approve": approve}, headers=AUTH)
                return
            time.sleep(0.02)

    thread = threading.Thread(target=answer)
    thread.start()
    return thread


def ticket_for(client: TestClient, gid: str, **params: str) -> str:
    helper = _answer_next(client)
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


@pytest.mark.parametrize("ticket", [None, "forged"])
def test_an_agent_export_without_the_users_approval_is_refused(
    client: TestClient, ticket: str | None
) -> None:
    gid = open_grid(client)
    params = {"format": "csv", "save": "1"} | ({"ticket": ticket} if ticket else {})
    refused = client.get(f"/grids/{gid}/export", params=params, headers=AGENT)
    assert refused.status_code == 403
    assert "approval" in refused.text


def test_a_declined_export_gets_no_ticket(client: TestClient) -> None:
    gid = open_grid(client)
    helper = _answer_next(client, approve=False)
    declined = client.post(f"/agent/export-approval/{gid}", params={"format": "csv"}, headers=AGENT)
    helper.join()
    assert declined.status_code == 403 and "declined" in declined.text
    assert "ticket" not in declined.text


def test_an_approved_export_is_written_once(client: TestClient) -> None:
    gid = open_grid(client)
    ticket = ticket_for(client, gid)
    params = {"format": "csv", "file": "approved", "ticket": ticket}
    saved = client.get(f"/grids/{gid}/export", params=params, headers=AGENT)
    assert saved.status_code == 200, saved.text
    assert saved.json()["saved"].endswith("approved.csv")
    # One click, one export: the same approval does not write a second file.
    again = client.get(f"/grids/{gid}/export", params=params | {"file": "again"}, headers=AGENT)
    assert again.status_code == 403


def test_an_approval_for_one_grid_does_not_export_another(client: TestClient) -> None:
    gid = open_grid(client)
    other = grid_id(push(client, "others", TABLE))
    ticket = ticket_for(client, gid)
    refused = client.get(f"/grids/{other}/export", params={"ticket": ticket}, headers=AGENT)
    assert refused.status_code == 403


def test_an_agent_cannot_approve_its_own_export(client: TestClient) -> None:
    gid = open_grid(client)
    waiting = threading.Thread(
        target=lambda: client.post(f"/agent/export-approval/{gid}", params={"format": "csv"}, headers=AGENT)
    )
    waiting.start()
    pending: list[dict[str, Any]] = []
    for _ in range(250):
        pending = client.get("/agent/approvals", headers=AGENT).json()["pending"]
        if pending:
            break
        time.sleep(0.02)
    assert pending
    refused = client.post(f"/agent/approvals/{pending[0]['id']}", json={"approve": True}, headers=AGENT)
    assert refused.status_code == 403
    client.post(f"/agent/approvals/{pending[0]['id']}", json={"approve": False}, headers=AUTH)
    waiting.join()


def test_the_user_exports_without_any_ticket(client: TestClient) -> None:
    gid = open_grid(client)
    mine = client.get(f"/grids/{gid}/export", params={"format": "csv", "provenance": "0"}, headers=AUTH)
    assert mine.status_code == 200 and SECRET in mine.text


def test_an_approved_export_holds_the_whole_view(client: TestClient) -> None:
    many = pa.table({"client": [f"c{i}" for i in range(250)], "balance": [float(i) for i in range(250)]})
    gid = open_grid(client, many)
    rows = saved_csv(agent_export(client, gid, provenance="0"))
    assert rows[0] == ["client", "balance"] and len(rows) == 1 + 250


@pytest.mark.parametrize("scope", ["view", "source", "profile"])
def test_every_scope_can_be_exported_once_approved(client: TestClient, scope: str) -> None:
    gid = open_grid(client)
    rows = saved_csv(agent_export(client, gid, scope=scope, provenance="0"))
    assert SECRET in str(rows)


@pytest.mark.parametrize("fmt", ["parquet", "xlsx"])
def test_binary_formats_are_written_too(client: TestClient, fmt: str) -> None:
    gid = open_grid(client)
    saved = agent_export(client, gid, format=fmt, provenance="0")
    assert saved.status_code == 200, saved.text
    path = Path(saved.json()["saved"])
    if fmt == "parquet":
        assert pq.read_table(path).column("client").to_pylist() == [SECRET, "Globex"]
    else:
        assert path.stat().st_size > 0
