"""Phase 2's "done when": a table made in the notebook tab shows in the grid's Open list, opens as a grid,
and running its cell again refreshes the grid. A real SAG server, a real marimo, a real kernel."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import uvicorn
import websockets

from swissarmygrid.server.app import create_app
from swissarmygrid.server.notebook import _cells
from swissarmygrid.settings import Settings

TOKEN = "notebook-grids-token"
AUTH = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


class Live:
    def __init__(self, port: int) -> None:
        self.port = port

    def call(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None) -> Any:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers=AUTH | (headers or {}),
        )
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 (loopback)
            raw = response.read()
            return json.loads(raw) if raw else None


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Live]:
    state = tmp_path_factory.mktemp("notebook-grids")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    # The notebook's kernel finds the server the way any Python does: through the connection file. It
    # inherits this environment from the server that starts marimo.
    info = state / "server.json"
    info.write_text(json.dumps({"port": port, "token": TOKEN, "pid": os.getpid()}))
    saved = {
        k: os.environ.get(k) for k in ("SWISSARMYGRID_INFO", "SWISSARMYGRID_OPEN", "SWISSARMYGRID_WATCH")
    }
    os.environ.update(SWISSARMYGRID_INFO=str(info), SWISSARMYGRID_OPEN="0")
    os.environ.pop("SWISSARMYGRID_WATCH", None)
    app = create_app(port, TOKEN, Settings(state_dir=state, export_dir=state / "exports"), audit_log=False)
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        yield Live(port)
    finally:
        server.should_exit = True
        thread.join(20)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def wait_for(check: Callable[[], Any], seconds: float = 60) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.25)
    raise AssertionError("timed out")


def test_a_notebook_table_opens_as_a_grid_and_follows_its_cell(live: Live) -> None:
    live.call("POST", "/notebook/session")

    def run(session: str, ids: list[str], codes: list[str]) -> None:
        live.call(
            "POST", "/marimo/api/kernel/run", {"cellIds": ids, "codes": codes}, {"Marimo-Session-Id": session}
        )

    def process_with(table: str) -> str | None:
        for process in live.call("GET", "/processes")["processes"]:
            if any(t["name"] == table for t in process["tables"]):
                return str(process["id"])
        return None

    def rows_of(name: str) -> int | None:
        for dataset in live.call("GET", "/datasets")["datasets"]:
            if dataset["name"] == name:
                return int(dataset["rows"])
        return None

    async def scenario() -> tuple[str, int, int]:
        url = f"ws://127.0.0.1:{live.port}/marimo/ws?session_id=s-grid-owner&file=workspace.py"
        async with websockets.connect(url, additional_headers=AUTH, max_size=None) as owner:
            ids, codes = await _cells(owner)
            loaves = (
                "import pandas as pd\nloaves = pd.DataFrame({'loaf': ['rye', 'spelt'], 'grams': [800, 650]})"
            )
            # Run the starter cell (it imports swissarmygrid, which starts the watcher) and a table cell.
            await asyncio.to_thread(run, "s-grid-owner", [ids[0], "sagdf1"], [codes[0], loaves])
            process = await asyncio.to_thread(wait_for, lambda: process_with("loaves"))
            await asyncio.to_thread(live.call, "POST", f"/processes/{process}/open", {"name": "loaves"})
            first = await asyncio.to_thread(wait_for, lambda: rows_of("loaves"))
            # Run the cell again with more rows: the open grid follows its variable.
            more = loaves.replace(
                "['rye', 'spelt'], 'grams': [800, 650]", "['rye', 'spelt', 'oat'], 'grams': [800, 650, 500]"
            )
            await asyncio.to_thread(run, "s-grid-owner", ["sagdf1"], [more])
            second = await asyncio.to_thread(wait_for, lambda: rows_of("loaves") == 3 and 3)
            return process, first, second

    process, first, second = asyncio.run(scenario())
    assert (first, second) == (2, 3)
    label = next(p["label"] for p in live.call("GET", "/processes")["processes"] if p["id"] == process)
    assert label  # the session is named (its notebook)


def test_outside_marimo_the_watcher_namespace_is_main() -> None:
    from swissarmygrid.client.watcher import _marimo_namespace

    assert _marimo_namespace() is None  # not a marimo kernel: the usual __main__ rules apply
