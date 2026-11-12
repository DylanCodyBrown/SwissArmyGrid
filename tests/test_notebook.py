"""The notebook behind SAG: a real marimo, started by a real SAG server on a free port."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uvicorn
import websockets

from swissarmygrid.server.app import create_app
from swissarmygrid.server.notebook import STARTER, _cells
from swissarmygrid.settings import Settings

TOKEN = "notebook-test-token"


@dataclass
class Live:
    port: int
    state: Path
    app: Any

    def call(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> tuple[int, Any, dict[str, str]]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 (loopback)
                raw = response.read()
                return response.status, raw, dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)


AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Live]:
    state = tmp_path_factory.mktemp("notebook-state")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    app = create_app(port, TOKEN, Settings(state_dir=state, export_dir=state / "exports"), audit_log=False)
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    yield Live(port, state, app)
    server.should_exit = True
    thread.join(20)
    assert not app.state.ctx.notebook.running  # SAG stops marimo when it stops


def cookie_of(headers: dict[str, str]) -> str:
    raw = next(v for k, v in headers.items() if k.lower() == "set-cookie")
    assert "HttpOnly" in raw and "SameSite=strict" in raw and "Path=/marimo" in raw
    return raw.split(";")[0]


@pytest.fixture(scope="module")
def cookie(live: Live) -> str:
    status, body, headers = live.call("POST", "/notebook/session", headers=AUTH)
    assert status == 200, body
    assert json.loads(body) == {"src": "/marimo/?file=workspace.py", "file": "workspace.py"}
    return cookie_of(headers)


def test_the_notebook_starts_on_first_use_with_the_starter(live: Live, cookie: str) -> None:
    assert live.app.state.ctx.notebook.running
    assert (live.state / "notebooks" / "workspace.py").read_text() == STARTER


def test_marimo_needs_the_notebook_cookie_or_the_token(live: Live, cookie: str) -> None:
    assert live.call("GET", "/marimo/")[0] == 401
    assert live.call("GET", "/marimo/", headers={"Cookie": "sag_notebook=forged"})[0] == 401
    status, body, _ = live.call("GET", "/marimo/?file=workspace.py", headers={"Cookie": cookie})
    assert status == 200 and b"marimo" in body
    # marimo's own token never reaches the page.
    assert live.app.state.ctx.notebook.token.encode() not in body
    assert live.call("GET", "/marimo/health", headers=AUTH)[0] == 200
    assert live.call("GET", "/marimo", headers={"Cookie": cookie})[0] in (200, 307)
    # A page from another site is refused even with the cookie.
    assert live.call("GET", "/marimo/", headers={"Cookie": cookie, "Origin": "http://evil.example"})[0] == 403


def test_the_agent_never_reaches_the_notebook(live: Live) -> None:
    agent = AUTH | {"X-Actor": "agent"}
    assert live.call("POST", "/notebook/session", headers=agent)[0] == 403
    assert live.call("POST", "/notebook/imports", {"lines": ["import os"]}, headers=agent)[0] == 403


@pytest.mark.parametrize(
    "body",
    [
        {"lines": []},
        {"lines": ["os.system('x')"]},
        {"lines": ["import os"], "file": "../x.py"},
        {"lines": ["import os"] * 21},
        {"lines": "import os"},
    ],
)
def test_bad_import_requests_are_refused(live: Live, cookie: str, body: dict[str, Any]) -> None:
    assert live.call("POST", "/notebook/imports", body, headers=AUTH)[0] == 400


async def _open_notebook(
    port: int, cookie: str, session: str, kiosk: bool = False
) -> tuple[list[str], list[str]]:
    query = f"session_id={session}&file=workspace.py" + ("&kiosk=true" if kiosk else "")
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/marimo/ws?{query}",
        additional_headers={"Cookie": cookie, "Origin": f"http://127.0.0.1:{port}"},
        max_size=None,
    ) as websocket:
        return await _cells(websocket)


def test_the_websocket_goes_through_and_imports_land_at_the_top(live: Live, cookie: str) -> None:
    async def scenario() -> tuple[list[str], list[str], int, Any, tuple[str, Any]]:
        query = "session_id=s-owner&file=workspace.py"
        async with websockets.connect(
            f"ws://127.0.0.1:{live.port}/marimo/ws?{query}",
            additional_headers={"Cookie": cookie, "Origin": f"http://127.0.0.1:{live.port}"},
            max_size=None,
        ) as owner:  # the person's own open notebook
            ids, codes = await _cells(owner)
            status, body, _ = await asyncio.to_thread(
                live.call,
                "POST",
                "/notebook/imports",
                {"lines": ["import swissarmygrid as sag", "import json as js"]},
                AUTH,
            )
            after = await _open_notebook(live.port, cookie, "s-check", kiosk=True)
            again = await asyncio.to_thread(
                live.call, "POST", "/notebook/imports", {"lines": ["import json as js"]}, AUTH
            )
            return ids, codes, status, json.loads(body), (after[1][0], json.loads(again[1]))

    _ids, codes, status, added, (first_cell, again) = asyncio.run(scenario())
    assert codes[0] == "import marimo as mo\nimport swissarmygrid as sag"
    assert status == 200 and added == {"added": ["import json as js"]}  # sag was there already
    assert first_cell == "import json as js"
    assert again == {"added": []}


def test_the_websocket_is_refused_without_the_cookie(live: Live, cookie: str) -> None:
    async def attempt() -> None:
        async with websockets.connect(f"ws://127.0.0.1:{live.port}/marimo/ws?session_id=x&file=workspace.py"):
            pass

    with pytest.raises(websockets.exceptions.InvalidStatus):
        asyncio.run(attempt())
