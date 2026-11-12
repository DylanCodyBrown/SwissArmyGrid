"""The notebook: one marimo server, started and watched by SAG (plan phase 2, decisions 8 and 12).

marimo runs as a child process of the server's own Python, on a free port on 127.0.0.1, with a token
SAG makes up and hands over on stdin (never on the command line, where other users could read it).
Its pages are served through SAG at /marimo/ (routes_notebook.py), so the notebook tab is the same
origin as SAG: SAG's notebook page can read what is highlighted for "Save as blade" and see what is
dropped into a cell. The browser never sees marimo's token; the proxy adds it to every request.

Started on first use, not with SAG: nothing runs Python until the user opens the notebook (decision 5).
A marimo that died is started again on the next request. The notebooks live in
~/.swissarmygrid/notebooks; until workspaces exist (phase 1) there is one, `workspace.py`.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator
from pathlib import Path

BASE_URL: str = "/marimo"
DEFAULT_NOTEBOOK: str = "workspace.py"
_START_SECONDS: float = 90.0

# A new workspace notebook: the imports a blade or a SQL cell needs, ready in the first cell.
STARTER = """import marimo

app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo
    import swissarmygrid as sag

    return mo, sag


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
"""


class NotebookError(RuntimeError):
    """marimo would not start or answer. The message says why, with the end of its output."""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class NotebookServer:
    """The one marimo server. `ensure_started` is safe to call on every request."""

    def __init__(self, folder: Path, python: str | None = None) -> None:
        self.folder = folder
        self.python = python or sys.executable
        self.token: str = secrets.token_urlsafe(32)
        self.port: int | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._output: collections.deque[str] = collections.deque(maxlen=200)
        self._lock = asyncio.Lock()

    # ===== the notebook files =====

    def notebook(self, name: str = DEFAULT_NOTEBOOK) -> Path:
        """The notebook file, made from the starter the first time."""
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / name
        if not path.exists():
            path.write_text(STARTER, encoding="utf-8")
        return path

    # ===== the process =====

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _healthy(self) -> bool:
        if self.port is None:
            return False
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{BASE_URL}/health", timeout=2
            ) as response:
                return bool(response.status == 200)
        except (urllib.error.URLError, OSError):
            return False

    async def ensure_started(self) -> None:
        async with self._lock:
            if self.running and await asyncio.to_thread(self._healthy):
                return
            self.stop()
            await asyncio.to_thread(self._start)

    def _start(self) -> None:
        self.notebook()
        self.port = _free_port()
        self._output.clear()
        command = [
            self.python,
            "-m",
            "marimo",
            "edit",
            str(self.folder),
            "--headless",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--base-url",
            BASE_URL,
            "--token-password-file",
            "-",
            "--skip-update-check",
            "--no-sandbox",
            # SAG starts marimo and serves its own page, so the page and the server can't differ in
            # version; the check would also need a header the proxy cannot know in advance.
            "--no-skew-protection",
        ]
        process = subprocess.Popen(  # noqa: S603 (this Python, fixed arguments)
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self.folder,
        )
        if process.stdin is not None:  # always, with stdin=PIPE
            process.stdin.write(self.token.encode() + b"\n")
            process.stdin.close()
        self._process = process
        threading.Thread(target=self._drain, args=(process,), daemon=True).start()
        deadline = time.monotonic() + _START_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise NotebookError(f"The notebook (marimo) stopped while starting:\n{self.output()}")
            if self._healthy():
                return
            time.sleep(0.2)
        self.stop()
        raise NotebookError(
            f"The notebook (marimo) did not start within {_START_SECONDS:.0f} seconds:\n{self.output()}"
        )

    def _drain(self, process: subprocess.Popen[bytes]) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            self._output.append(line.decode("utf-8", "replace").rstrip())

    def output(self) -> str:
        return "\n".join(list(self._output)[-30:])

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)


# ===== adding imports to the open notebook =====
#
# marimo lets a second viewer (a "kiosk") join a running notebook: it is sent the notebook's cells
# when it connects, and a document change it sends reaches the person's open notebook as if typed
# there, and is saved. SAG joins that way to put missing imports in a new cell above the first one
# (plan, part 2: dropping a blade adds the imports it needs at the top), then runs that cell.
# Checked with marimo 0.25.2 (docs/verified.md).


async def _cells(websocket: object) -> tuple[list[str], list[str]]:
    """Read messages until marimo's kernel-ready, which lists the cells: (ids, codes)."""
    import json

    recv = getattr(websocket, "recv")  # noqa: B009 (websockets' connection, typed loosely here)
    for _ in range(500):
        message = json.loads(await asyncio.wait_for(recv(), 20))
        data = message.get("data", message) if isinstance(message, dict) else {}
        if message.get("op") == "kernel-ready" or data.get("op") == "kernel-ready":
            return list(data.get("cell_ids", [])), list(data.get("codes", []))
    raise NotebookError("The notebook did not send its cells.")


class NotebookControl:
    """Changes to a notebook someone has open, made through marimo's own document changes."""

    def __init__(self, server: NotebookServer) -> None:
        self.server = server

    def _post(self, route: str, body: object, session: str) -> object:
        import json

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.port}{BASE_URL}{route}",
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.server.token}",
                "Content-Type": "application/json",
                "Marimo-Session-Id": session,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 (fixed http://127.0.0.1 URL)
                answer: object = json.loads(response.read() or b"null")
                return answer
        except urllib.error.HTTPError as error:
            raise NotebookError(f"The notebook refused {route}: {error.read().decode()[:300]}") from None

    def _owner(self, file: str, kiosk: str) -> str | None:
        """The session id of the person's own open notebook `file` (not a kiosk)."""
        answer = self._post("/api/home/running_notebooks", {}, kiosk)
        files = answer.get("files", []) if isinstance(answer, dict) else []
        for entry in files:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path", ""))
            if Path(path).name == file and entry.get("sessionId"):
                return str(entry["sessionId"])
        return None

    @contextlib.asynccontextmanager
    async def _joined(self, file: str) -> AsyncIterator[tuple[str, list[str], list[str]]]:
        """Join the open notebook as a kiosk: (this connection's session id, cell ids, cell codes)."""
        import websockets

        await self.server.ensure_started()
        session = "sag-" + secrets.token_hex(6)
        url = f"ws://127.0.0.1:{self.server.port}{BASE_URL}/ws?" + urllib.parse.urlencode(
            {"session_id": session, "file": file, "kiosk": "true"}
        )
        try:
            async with websockets.connect(
                url, additional_headers={"Authorization": f"Bearer {self.server.token}"}, max_size=None
            ) as websocket:
                ids, codes = await _cells(websocket)
                yield session, ids, codes
        except (OSError, TimeoutError, websockets.exceptions.WebSocketException) as error:
            raise NotebookError(
                f"Could not reach the open notebook {file} ({error}). Open it in its tab and try again."
            ) from None

    async def _change(self, session: str, changes: list[dict[str, object]]) -> None:
        await asyncio.to_thread(self._post, "/api/document/transaction", {"changes": changes}, session)

    async def _imports(
        self, file: str, session: str, ids: list[str], codes: list[str], lines: list[str]
    ) -> list[str]:
        """Put the lines the notebook does not make yet in a new cell above the first, and run it."""
        from swissarmygrid.blade_core.code import missing_imports

        needed = missing_imports("\n\n".join(codes), lines)
        if not needed:
            return []
        cell = "sag" + secrets.token_hex(3)
        code = "\n".join(needed)
        await self._change(session, [_create(cell, code, before=ids[0] if ids else None)])
        # A kiosk may not run cells; the notebook's own connection may, so run it as that one.
        owner = await asyncio.to_thread(self._owner, file, session)
        if owner is not None:
            await asyncio.to_thread(
                self._post, "/api/kernel/run", {"cellIds": [cell], "codes": [code]}, owner
            )
        return needed

    async def ensure_imports(self, file: str, lines: list[str]) -> list[str]:
        """Add the import lines the notebook does not make yet, in a new cell at the top, and run it.
        Returns the lines added (none when the notebook already has them all)."""
        async with self._joined(file) as (session, ids, codes):
            return await self._imports(file, session, ids, codes, lines)

    async def drop(self, file: str, code: str, imports: list[str], target: str | None) -> dict[str, object]:
        """What was dragged from the rail and dropped on cell `target`: into that cell when it is empty,
        else into a new cell right after it (at the end when there is no target). Then the imports it
        needs, at the top. The dropped code is not run: the person runs it when ready."""
        async with self._joined(file) as (session, ids, codes):
            if target in ids and not codes[ids.index(target)].strip():
                cell = target
                await self._change(session, [{"type": "set-code", "cellId": cell, "code": code}])
            else:
                cell = "sag" + secrets.token_hex(3)
                after = target if target in ids else (ids[-1] if ids else None)
                await self._change(session, [_create(cell, code, after=after)])
            added = await self._imports(file, session, ids, codes, imports) if imports else []
            return {"cell": cell, "added": added}


def _create(cell: str, code: str, before: str | None = None, after: str | None = None) -> dict[str, object]:
    change: dict[str, object] = {
        "type": "create-cell",
        "cellId": cell,
        "code": code,
        "name": "_",
        "config": {"column": None, "disabled": False, "hideCode": False, "expandOutput": False},
    }
    if before is not None:
        change["before"] = before
    elif after is not None:
        change["after"] = after
    return change
