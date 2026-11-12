"""Python sessions in the Open list: a session reports its tables (names and sizes, never rows), the
page asks for one to be opened, and the session pushes it. All data here is made up.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from swissarmygrid.server.processes import ProcessRegistry
from swissarmygrid.server.routes_processes import QuietHeartbeats
from tests.conftest import AGENT, AUTH, grid_id, push

SESSION = {"X-Process": "s1"}


def table(
    name: str, kind: str = "dataframe", rows: int | None = 3, columns: int | None = 2
) -> dict[str, Any]:
    return {"name": name, "kind": kind, "rows": rows, "columns": columns}


def sync(
    client: TestClient,
    process: str = "s1",
    tables: list[dict[str, Any]] | None = None,
    label: str = "tie_out.ipynb",
    headers: dict[str, str] | None = None,
) -> Any:
    body = {"label": label, "pid": 4242, "tables": [table("SOD")] if tables is None else tables}
    return client.post(f"/processes/{process}/sync", json=body, headers=headers or AUTH)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ===== the registry =====


def test_a_session_appears_with_its_tables_and_none_is_open_yet() -> None:
    registry = ProcessRegistry()
    registry.sync("s1", "tie_out.ipynb", 4242, [table("SOD"), table("claims", "arrow", 10, 4)])
    [session] = registry.snapshot(lambda name: False)
    assert session["label"] == "tie_out.ipynb"
    assert [(t["name"], t["open"]) for t in session["tables"]] == [("SOD", False), ("claims", False)]


def test_a_session_that_stops_reporting_expires() -> None:
    clock = Clock()
    registry = ProcessRegistry(ttl_seconds=30, clock=clock)
    registry.sync("s1", "a.ipynb", 1, [table("SOD")])
    clock.now += 29
    assert len(registry.snapshot(lambda name: False)) == 1
    clock.now += 2
    assert registry.snapshot(lambda name: False) == []
    assert registry.prune() is False  # snapshot already dropped it, so nothing is left to report


def test_prune_says_when_it_dropped_a_session() -> None:
    clock = Clock()
    registry = ProcessRegistry(ttl_seconds=30, clock=clock)
    registry.sync("s1", "a.ipynb", 1, [table("SOD")])
    clock.now += 31
    assert registry.prune() is True
    assert registry.prune() is False


def test_sync_reports_whether_what_the_page_shows_changed() -> None:
    registry = ProcessRegistry()
    assert registry.sync("s1", "a.ipynb", 1, [table("SOD", rows=3)]) is True  # a new session
    assert registry.sync("s1", "a.ipynb", 1, [table("SOD", rows=3)]) is False  # a heartbeat
    assert registry.sync("s1", "a.ipynb", 1, [table("SOD", rows=4)]) is True  # a rerun changed the size


def test_bad_table_entries_are_refused() -> None:
    registry = ProcessRegistry()
    with pytest.raises(ValueError, match="name"):
        registry.sync("s1", "a.ipynb", 1, [table("has space")])
    with pytest.raises(ValueError, match="too many"):
        registry.sync("s1", "a.ipynb", 1, [table(f"t{i}") for i in range(501)])
    with pytest.raises(ValueError, match="process id"):
        registry.sync("not ok!", "a.ipynb", 1, [])


def test_a_request_is_queued_once_and_only_for_a_table_the_session_has() -> None:
    registry = ProcessRegistry()
    registry.sync("s1", "a.ipynb", 1, [table("SOD")])
    assert registry.request_load("s1", "SOD") is True
    assert registry.request_load("s1", "nope") is False
    assert registry.request_load("other", "SOD") is False
    assert asyncio.run(registry.wait_requests("s1", 0)) == [{"action": "load", "name": "SOD"}]
    assert asyncio.run(registry.wait_requests("s1", 0)) == []


def test_the_long_poll_wakes_the_moment_a_request_arrives() -> None:
    async def scenario() -> list[dict[str, str]]:
        registry = ProcessRegistry()
        registry.sync("s1", "a.ipynb", 1, [table("SOD")])
        waiter = asyncio.create_task(registry.wait_requests("s1", 5))
        await asyncio.sleep(0.05)
        assert not waiter.done()
        registry.request_load("s1", "SOD")
        return await asyncio.wait_for(waiter, 1)

    assert asyncio.run(scenario()) == [{"action": "load", "name": "SOD"}]


def test_the_long_poll_gives_up_after_its_wait() -> None:
    registry = ProcessRegistry()
    registry.sync("s1", "a.ipynb", 1, [table("SOD")])
    assert asyncio.run(registry.wait_requests("s1", 0.05)) == []


def test_a_session_follows_only_what_it_loaded_and_the_store_still_holds() -> None:
    registry = ProcessRegistry()
    registry.sync("s1", "a.ipynb", 1, [table("SOD"), table("claims")])
    registry.note_loaded("s1", "SOD")
    assert registry.following("s1", lambda name: True) == ["SOD"]
    assert registry.following("s1", lambda name: False) == []  # the dataset was closed


def test_the_second_session_to_load_a_name_takes_it_over() -> None:
    registry = ProcessRegistry()
    registry.sync("s1", "a.ipynb", 1, [table("SOD")])
    registry.sync("s2", "b.ipynb", 2, [table("SOD")])
    registry.note_loaded("s1", "SOD")
    registry.note_loaded("s2", "SOD")
    assert registry.following("s1", lambda name: True) == []
    assert registry.following("s2", lambda name: True) == ["SOD"]


def test_loading_a_name_for_a_session_nobody_reported_is_ignored() -> None:
    registry = ProcessRegistry()
    registry.note_loaded("ghost", "SOD")
    assert registry.following("ghost", lambda name: True) == []


# ===== the routes =====


def test_sync_needs_the_token(client: TestClient) -> None:
    assert client.post("/processes/s1/sync", json={}).status_code == 401
    assert client.get("/processes").status_code == 401
    assert client.get("/processes/s1/requests?wait=0").status_code == 401


def test_the_page_lists_a_session_and_its_tables(client: TestClient) -> None:
    assert sync(client).status_code == 200
    [session] = client.get("/processes", headers=AUTH).json()["processes"]
    assert session["label"] == "tie_out.ipynb"
    assert session["id"] == "s1"
    assert session["tables"] == [{"name": "SOD", "kind": "dataframe", "rows": 3, "columns": 2, "open": False}]


def test_a_malformed_sync_is_a_400(client: TestClient) -> None:
    assert client.post("/processes/s1/sync", json=["no"], headers=AUTH).status_code == 400
    assert sync(client, tables=[table("has space")]).status_code == 400
    assert client.post("/processes/s1/sync", content=b"not json", headers=AUTH).status_code == 400


def test_open_queues_a_load_for_the_listener(client: TestClient) -> None:
    sync(client)
    assert client.post("/processes/s1/open", json={"name": "SOD"}, headers=AUTH).status_code == 202
    got = client.get("/processes/s1/requests?wait=0", headers=AUTH).json()
    assert got == {"requests": [{"action": "load", "name": "SOD"}]}
    assert client.get("/processes/s1/requests?wait=0", headers=AUTH).json() == {"requests": []}


def test_opening_a_table_the_session_does_not_have_is_a_404(client: TestClient) -> None:
    sync(client)
    assert client.post("/processes/s1/open", json={"name": "nope"}, headers=AUTH).status_code == 404
    assert client.post("/processes/ghost/open", json={"name": "SOD"}, headers=AUTH).status_code == 404
    assert client.get("/processes/ghost/requests?wait=0", headers=AUTH).status_code == 404


def test_the_agent_can_list_sessions_but_not_open_anything(client: TestClient) -> None:
    sync(client)
    assert client.get("/processes", headers=AGENT).status_code == 200
    refused = client.post("/processes/s1/open", json={"name": "SOD"}, headers=AGENT)
    assert refused.status_code == 403
    assert client.get("/processes/s1/requests?wait=0", headers=AUTH).json() == {"requests": []}


def test_a_push_from_a_session_marks_the_table_open_and_it_is_followed(client: TestClient) -> None:
    sync(client)
    assert sync(client).json()["following"] == []
    push(client, "SOD", pa.table({"a": [1, 2, 3]}), headers=AUTH | SESSION)
    assert sync(client).json()["following"] == ["SOD"]
    [session] = client.get("/processes", headers=AUTH).json()["processes"]
    assert session["tables"][0]["open"] is True
    client.delete("/datasets/SOD", headers=AUTH)
    assert sync(client).json()["following"] == []
    [session] = client.get("/processes", headers=AUTH).json()["processes"]
    assert session["tables"][0]["open"] is False


def test_a_push_without_a_session_header_changes_nothing_here(client: TestClient) -> None:
    sync(client)
    push(client, "SOD", pa.table({"a": [1]}))
    assert sync(client).json()["following"] == []


def test_a_follow_keeps_the_data_fresh_but_does_not_bring_a_closed_tab_back(client: TestClient) -> None:
    sync(client)
    gid = grid_id(push(client, "SOD", pa.table({"a": [1, 2]}), headers=AUTH | SESSION))
    assert client.delete(f"/grids/{gid}", headers=AUTH).status_code == 200
    again = push(client, "SOD", pa.table({"a": [1, 2, 3]}), "?follow=1", headers=AUTH | SESSION)
    assert again.status_code == 200
    assert again.json()["grid"] is None
    [dataset] = client.get("/datasets", headers=AUTH).json()["datasets"]
    assert (dataset["rows"], dataset["grids"]) == (3, [])


def test_a_follow_refreshes_the_open_tab_in_place_without_taking_the_front(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync(client)
    gid = grid_id(push(client, "SOD", pa.table({"a": [1, 2]}), headers=AUTH | SESSION))
    sent: list[dict[str, Any]] = []
    live = client.app.state.ctx.live  # type: ignore[attr-defined]
    original = live.broadcast

    async def record(message: dict[str, Any]) -> None:
        sent.append(message)
        await original(message)

    monkeypatch.setattr(live, "broadcast", record)
    again = push(client, "SOD", pa.table({"a": [1, 2, 3]}), "?follow=1", headers=AUTH | SESSION)
    assert again.json()["grid"] == gid
    assert [m["type"] for m in sent] == ["dataset_updated"]  # no grid_shown, so no jump to the front
    push(client, "SOD", pa.table({"a": [1, 2, 3, 4]}), headers=AUTH | SESSION)
    assert [m["type"] for m in sent][-2:] == ["dataset_updated", "grid_shown"]  # a plain push still shows it


def test_a_follow_for_a_table_that_is_not_open_is_refused_so_a_closed_table_stays_closed(
    client: TestClient,
) -> None:
    sync(client)
    table_ = pa.table({"a": [1]})
    # Never loaded by this session.
    assert push(client, "SOD", table_, "?follow=1", headers=AUTH | SESSION).status_code == 409
    # Loaded, then closed in the grid: a follow that was already on its way must not bring it back.
    push(client, "SOD", table_, headers=AUTH | SESSION)
    client.delete("/datasets/SOD", headers=AUTH)
    assert push(client, "SOD", table_, "?follow=1", headers=AUTH | SESSION).status_code == 409
    assert client.get("/datasets", headers=AUTH).json()["datasets"] == []
    # No session named at all.
    assert push(client, "SOD", table_, "?follow=1").status_code == 409


def test_a_session_can_say_why_a_table_would_not_open(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, Any]] = []
    live = client.app.state.ctx.live  # type: ignore[attr-defined]

    async def record(message: dict[str, Any]) -> None:
        sent.append(message)

    monkeypatch.setattr(live, "broadcast", record)
    body = {
        "label": "tie_out.ipynb",
        "pid": 1,
        "tables": [table("SOD")],
        "errors": [{"name": "SOD", "message": "Dataset 'SOD' is about 2.0 MiB; the limit is 1 MiB."}],
    }
    assert client.post("/processes/s1/sync", json=body, headers=AUTH).status_code == 200
    errors = [m for m in sent if m["type"] == "process_error"]
    assert errors == [
        {
            "type": "process_error",
            "label": "tie_out.ipynb",
            "name": "SOD",
            "message": "Dataset 'SOD' is about 2.0 MiB; the limit is 1 MiB.",
        }
    ]


def test_the_pages_hear_when_a_sessions_tables_change(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    live = client.app.state.ctx.live  # type: ignore[attr-defined]

    async def record(message: dict[str, Any]) -> None:
        sent.append(message["type"])

    monkeypatch.setattr(live, "broadcast", record)
    sync(client)
    sync(client)  # a heartbeat says nothing
    sync(client, tables=[table("SOD", rows=4)])
    assert sent == ["processes_changed", "processes_changed"]


# ===== the access log =====


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5", "POST", path, "1.1", 200),
        None,
    )


def test_heartbeats_stay_out_of_the_access_log() -> None:
    quiet = QuietHeartbeats()
    assert quiet.filter(_access_record("/processes/abc/sync")) is False
    assert quiet.filter(_access_record("/processes/abc/requests?wait=25")) is False
    assert quiet.filter(_access_record("/datasets/SOD")) is True
    assert quiet.filter(_access_record("/processes")) is True  # the page opening the list is still logged
