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
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 (loopback)
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raise AssertionError(f"{method} {path}: {error.code} {error.read().decode()[:600]}") from None


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


def test_a_notebook_table_opens_as_a_grid_follows_its_cell_and_its_steps_go_back(live: Live) -> None:
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

    def notebook_rows(name: str) -> int | None:
        for session in live.call("GET", "/processes")["processes"]:
            for table in session["tables"]:
                if table["name"] == name:
                    return int(table.get("rows") or 0) or None
        return None

    def grid_steps() -> dict[str, Any]:
        """Everything after the notebook is open: open the table, follow its cell, write its steps back."""
        found: dict[str, Any] = {}
        process = wait_for(lambda: process_with("loaves"))
        live.call("POST", f"/processes/{process}/open", {"name": "loaves"})
        found["first"] = wait_for(lambda: rows_of("loaves"))
        # Run the cell again with more rows: the open grid follows its variable.
        more = (
            "import pandas as pd\n"
            "loaves = pd.DataFrame({'loaf': ['rye', 'spelt', 'oat'], 'grams': [800, 650, 500]})"
        )
        run("s-grid-owner", ["sagdf1"], [more])
        found["second"] = wait_for(lambda: rows_of("loaves") == 3 and 3)
        # Code-behind: a step on the notebook's grid goes back to the notebook as one SQL cell, run in the
        # kernel over the notebook's own variable, so its result is one more table there.
        grid = next(d["grids"][0] for d in live.call("GET", "/datasets")["datasets"] if d["name"] == "loaves")
        found["linked"] = live.call("GET", f"/grids/{grid}")["notebook"]
        heavy = {"steps": [{"kind": "filter", "params": {"column": "grams", "op": "gt", "value": 600}}]}
        live.call("POST", f"/grids/{grid}/steps", heavy)
        found["kept"] = live.call("POST", f"/grids/{grid}/notebook-cell")
        found["view"] = wait_for(lambda: notebook_rows("loaves_view"))
        # A second change updates the same cell rather than adding another.
        heavier = {"steps": [{"kind": "filter", "params": {"column": "grams", "op": "gt", "value": 700}}]}
        live.call("POST", f"/grids/{grid}/steps", heavier)
        found["again"] = live.call("POST", f"/grids/{grid}/notebook-cell")
        found["view_after"] = wait_for(lambda: notebook_rows("loaves_view") == 1 and 1)
        return found

    async def scenario() -> dict[str, Any]:
        url = f"ws://127.0.0.1:{live.port}/marimo/ws?session_id=s-grid-owner&file=workspace.py"
        async with websockets.connect(
            url, additional_headers=AUTH, max_size=None
        ) as owner:  # the notebook tab
            ids, codes = await _cells(owner)
            loaves = (
                "import pandas as pd\nloaves = pd.DataFrame({'loaf': ['rye', 'spelt'], 'grams': [800, 650]})"
            )
            # Run the starter cell (it imports swissarmygrid, which starts the watcher) and a table cell.
            await asyncio.to_thread(run, "s-grid-owner", [ids[0], "sagdf1"], [codes[0], loaves])
            return await asyncio.to_thread(grid_steps)

    found = asyncio.run(scenario())
    assert (found["first"], found["second"]) == (2, 3)
    assert found["linked"] is True
    kept = found["kept"]
    assert kept["ran"] and kept["variable"] == "loaves_view"
    assert kept["code"] == (
        'loaves_view = mo.sql(\n    """\n-- 1. Keep rows where grams is greater than 600\n'
        'SELECT *\nFROM loaves\nWHERE "grams" > 600\n"""\n)'
    )
    assert found["view"] == 2  # rye and spelt are over 600 grams; oat is not
    assert found["again"]["cell"] == kept["cell"] and found["view_after"] == 1


def test_outside_marimo_the_watcher_namespace_is_main() -> None:
    from swissarmygrid.client.watcher import _marimo_namespace

    assert _marimo_namespace() is None  # not a marimo kernel: the usual __main__ rules apply
