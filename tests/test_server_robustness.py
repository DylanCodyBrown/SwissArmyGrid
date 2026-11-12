"""Malformed requests get a plain 4xx, never a 500 with a traceback (found by fuzzing every route)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from tests.conftest import AGENT, AUTH, BASE, PORT, TOKEN, grid_id, make_client, push

TABLE = pa.table({"client": ["Acme", "Globex"], "balance": [1.0, 2.0]})


@pytest.fixture
def lenient(tmp_path: Path) -> Any:
    """A client that reports a server error as a 500 response instead of raising it."""
    from swissarmygrid.server.app import create_app
    from swissarmygrid.settings import Settings

    settings = Settings(state_dir=tmp_path / "state", export_dir=tmp_path / "exports")
    with TestClient(create_app(PORT, TOKEN, settings), base_url=BASE, raise_server_exceptions=False) as c:
        yield c


@pytest.mark.parametrize("limit", ["x", "1.5", "-5", "0", "99999999999999999999"])
def test_audit_limit_is_checked_and_bounded(lenient: TestClient, limit: str) -> None:
    for i in range(3):
        push(lenient, f"t{i}", TABLE)
    response = lenient.get(f"/audit?limit={limit}", headers=AUTH)
    assert response.status_code in (200, 400), response.text
    if response.status_code == 200:
        assert 1 <= len(response.json()["entries"]) <= 500


@pytest.mark.parametrize("body", [b"[]", b"null", b"1", b'"x"'])
@pytest.mark.parametrize(
    ("path", "headers"),
    [("/agent/query", AGENT)],
)
def test_a_body_that_is_not_an_object_is_a_400(
    lenient: TestClient, path: str, headers: dict[str, str], body: bytes
) -> None:
    push(lenient, "clients", TABLE)
    response = lenient.post(path, content=body, headers=headers | {"Content-Type": "application/json"})
    assert response.status_code == 400, response.text


def test_an_agent_cannot_stop_the_server(tmp_path: Path) -> None:
    stopped: list[bool] = []
    from swissarmygrid.server.app import create_app
    from swissarmygrid.settings import Settings

    settings = Settings(state_dir=tmp_path / "state", export_dir=tmp_path / "exports")
    app = create_app(PORT, TOKEN, settings, on_shutdown=lambda: stopped.append(True))
    with TestClient(app, base_url=BASE) as c:
        assert c.post("/shutdown", headers=AGENT).status_code == 403
        assert not stopped
        assert c.post("/shutdown", headers=AUTH).status_code == 200
        assert stopped


def test_json_bodies_are_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("swissarmygrid.server.context.MAX_JSON_BODY_BYTES", 1024)
    with make_client(tmp_path) as c:
        gid = grid_id(push(c, "clients", TABLE))
        big = b'{"steps": [], "pad": "' + b"x" * 4096 + b'"}'
        response = c.post(
            f"/grids/{gid}/steps", content=big, headers=AUTH | {"Content-Type": "application/json"}
        )
        assert response.status_code == 413
        small = c.post(f"/grids/{gid}/steps", json={"steps": []}, headers=AUTH)
        assert small.status_code == 200


class _FakeSocket:
    """Stands in for a page's WebSocket: records what it is sent, or never finishes a send."""

    def __init__(self, stuck: bool = False) -> None:
        self.stuck = stuck
        self.sent: list[str] = []
        self.closed = False

    async def send_text(self, text: str) -> None:
        import asyncio

        if self.stuck:
            await asyncio.Event().wait()  # a frozen tab: its buffers are full and it never reads
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        self.closed = True


def test_a_page_that_stops_reading_never_holds_up_a_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import json as json_module
    import time

    from swissarmygrid.server import live_updates
    from swissarmygrid.server.live_updates import LiveUpdates

    monkeypatch.setattr(live_updates, "SEND_TIMEOUT_SECONDS", 0.2)
    live = LiveUpdates()
    pages: list[Any] = [_FakeSocket() for _ in range(5)]
    stuck: Any = _FakeSocket(stuck=True)
    for page in [*pages[:2], stuck, *pages[2:]]:
        live.add(page, "g1")

    async def go() -> float:
        started = time.perf_counter()
        await live.broadcast({"type": "grid_updated", "grid": "g1"})
        return time.perf_counter() - started

    took = asyncio.run(go())
    assert took < 1.0  # the stuck page held the broadcast up by the limit at most, not forever
    assert all(json_module.loads(p.sent[0]) == {"type": "grid_updated", "grid": "g1"} for p in pages)
    # The stuck page is dropped and its socket closed, so the next broadcast does not wait at all.
    assert stuck.closed and stuck not in live._sockets
    assert asyncio.run(go()) < 0.1 and all(len(p.sent) == 2 for p in pages)


def test_jsonable_keeps_its_meaning_on_the_fast_path() -> None:
    from datetime import date, datetime
    from decimal import Decimal
    from uuid import UUID

    from swissarmygrid.server.context import jsonable

    rows = [
        [1, 2**53 - 1, 2**53, -(2**60), 1.5, float("nan"), float("inf"), None, True, "x"],
        (date(2024, 1, 2), datetime(2024, 1, 2, 3, 4), Decimal("1.10"), b"abc", UUID(int=1)),
        [[1, [2**60]], {"k": float("-inf")}],
    ]
    assert jsonable(rows) == [
        [1, 2**53 - 1, str(2**53), str(-(2**60)), 1.5, "nan", "inf", None, True, "x"],
        ["2024-01-02", "2024-01-02T03:04:00", "1.10", "<3 bytes>", "00000000-0000-0000-0000-000000000001"],
        [[1, [str(2**60)]], {"k": "-inf"}],
    ]
    plain = [[1, "a", None, 2.5, False]]
    assert jsonable(plain) is plain  # nothing to change: no copy


def test_agent_reads_are_capped_in_bytes_like_the_sql_box(tmp_path: Path) -> None:
    """Level 3 lets an agent read up to 1,048,576 rows; a wide table made that one JSON body of
    gigabytes, built in memory, with no limit (the SQL box's answer has max_sql_response_bytes)."""
    wide = pa.table({f"c{i}": ["x" * 50] * 2000 for i in range(5)})
    with make_client(tmp_path, max_sql_response_bytes=64 * 1024) as c:
        gid = grid_id(push(c, "wide", wide))
        read = c.get(f"/agent/read/{gid}?limit=2000", headers=AGENT)
        assert read.status_code == 413 and "fewer rows" in read.text
        query = c.post("/agent/query", json={"dataset": "wide", "sql": "SELECT * FROM grid"}, headers=AGENT)
        assert query.status_code == 413
        assert c.get(f"/agent/read/{gid}?limit=10", headers=AGENT).status_code == 200


def test_a_pasted_body_is_capped_like_a_push(tmp_path: Path) -> None:
    with make_client(tmp_path, max_push_body_bytes=1024) as c:
        response = c.post("/datasets/p/paste", content=b"a\tb\n" + b"1\t2\n" * 1000, headers=AUTH)
        assert response.status_code == 413


def test_a_job_that_raises_stop_iteration_still_answers() -> None:
    # A future refuses StopIteration, and the request awaiting it never answered.
    import asyncio

    from swissarmygrid.engine.workers import PriorityWorkers

    workers = PriorityWorkers(1)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="StopIteration"):
            await asyncio.wait_for(workers.run(0, lambda: next(iter([]))), 5)

    asyncio.run(run())
    workers.close()
