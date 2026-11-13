"""The light client and where grids show: it only connects (VS Code's extension starts the
server and shows the grids), and the server asks VS Code to show a grid nothing shows yet."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from swissarmygrid.adapters import live_updates
from swissarmygrid.adapters.live_updates import LiveUpdates
from swissarmygrid.client import push_client

ROOT = Path(__file__).resolve().parents[1]


def test_the_client_imports_only_the_standard_library() -> None:
    """`import swissarmygrid` must work in a project with none of the server's packages."""
    probe = (
        "import sys; import swissarmygrid as sag; "
        "heavy = sorted(m for m in ('duckdb', 'pyarrow', 'pandas', 'starlette', 'uvicorn', 'sqlglot', 'mcp') "
        "if m in sys.modules); print(heavy, callable(sag.push))"
    )
    out = subprocess.run(  # noqa: S603 (this Python, a fixed probe)
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, cwd=ROOT
    ).stdout
    assert out.strip() == "[] True"


def test_only_the_server_command_can_start_a_process_or_open_a_browser() -> None:
    """SwissArmyGrid lives in VS Code: its extension starts the one server, and the server has VS
    Code show the grids. So nothing outside server/ (the push client, the MCP, the prompt hook)
    may start a process or open a browser, or the server could run from another install again."""
    package = ROOT / "src" / "swissarmygrid"
    offenders = [
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if "server" not in path.relative_to(package).parts
        and any(word in path.read_text(encoding="utf-8") for word in ("subprocess", "webbrowser"))
    ]
    assert offenders == []


def test_live_updates_know_which_grid_is_on_screen() -> None:
    live = LiveUpdates()
    page: Any = object()
    live.add(page, "g1")
    assert live.watching("g1") and not live.watching("g2")
    live.discard(page)
    assert not live.watching("g1")


def test_quick_pushes_with_nothing_open_open_one_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first push opens the workspace; the pushes right behind it, while that window is still
    loading, wait for it instead of each opening their own (a script pushing three tables opened
    three windows)."""
    clock = [100.0]
    monkeypatch.setattr(live_updates, "monotonic", lambda: clock[0])
    live = LiveUpdates()
    assert [live.shown_or_opening(g) for g in ("g1", "g2", "g3")] == [False, True, True]
    # The window arrives: from then on it is what shows the grids.
    workspace: Any = object()
    live.add(workspace, workspace=True)
    assert live.shown_or_opening("g4") is True
    # Closed again: the next push opens a new one at once.
    live.discard(workspace)
    assert live.shown_or_opening("g5") is False
    # A promised window that never connects (closed while loading) stops holding pushes back.
    clock[0] += live_updates.WINDOW_GRACE_SECONDS + 1
    assert live.shown_or_opening("g6") is False


def test_push_reports_whether_its_grid_is_watched(client: TestClient) -> None:
    import io

    import pyarrow as pa
    import pyarrow.ipc as ipc

    sink = io.BytesIO()
    table = pa.table({"a": [1, 2]})
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    from tests.conftest import ARROW, AUTH, PORT

    first = client.put("/datasets/watch_me", content=sink.getvalue(), headers=AUTH | {"Content-Type": ARROW})
    assert first.json()["watched"] is False
    grid = first.json()["grid"]
    with client.websocket_connect(f"/ws?grid={grid}", headers=AUTH | {"Host": f"127.0.0.1:{PORT}"}):
        again = client.put(
            "/datasets/watch_me", content=sink.getvalue(), headers=AUTH | {"Content-Type": ARROW}
        )
        assert again.json()["watched"] is True


def test_a_push_only_connects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With no server a push says to open VS Code; with one it returns the grid's address. It
    starts nothing and opens nothing either way, even when no page shows the grid."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a push must not start a process or open a browser")

    monkeypatch.setattr("subprocess.Popen", refuse)
    monkeypatch.setattr("webbrowser.open", refuse)
    monkeypatch.setenv("SWISSARMYGRID_INFO", str(tmp_path / "server.json"))  # no server there
    with pytest.raises(push_client.ServerNotRunningError, match="VS Code"):
        push_client.push([{"a": 1}], name="rows")

    class Response:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b'{"grid_url": "http://x/g/1", "watched": false}'

    monkeypatch.setattr(push_client, "ensure_server", lambda: {"port": 1, "token": "t"})
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Response())
    assert push_client.push([{"a": 1}], name="rows") == "http://x/g/1"


def test_vscode_shows_a_grid_nothing_shows_yet(client: TestClient) -> None:
    """A VS Code window connected as the opener: a push that finds nothing on screen has it open
    the workspace (and counts as watched, so nothing else opens one); the push right behind it
    waits for that window instead of asking again."""
    import io

    import pyarrow as pa
    import pyarrow.ipc as ipc

    from tests.conftest import ARROW, AUTH, PORT

    sink = io.BytesIO()
    table = pa.table({"a": [1]})
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    headers = AUTH | {"Content-Type": ARROW}
    with client.websocket_connect("/ws?opener=1", headers=AUTH | {"Host": f"127.0.0.1:{PORT}"}) as vscode:
        first = client.put("/datasets/first", content=sink.getvalue(), headers=headers).json()
        assert first["watched"] is True
        notices = [vscode.receive_json() for _ in range(3)]
        assert [n["type"] for n in notices] == ["dataset_updated", "grid_shown", "open_workspace"]
        assert notices[2]["url"] == first["workspace_url"]
        second = client.put("/datasets/second", content=sink.getvalue(), headers=headers).json()
        assert second["watched"] is True
        client.delete("/datasets/second", headers=AUTH)  # a known last notice to read up to
        seen: list[str] = []
        while not seen or seen[-1] != "dataset_closed":
            seen.append(vscode.receive_json()["type"])
        assert "open_workspace" not in seen


def test_the_window_last_in_front_opens_the_workspace() -> None:
    class Window:
        def __init__(self) -> None:
            self.sent: list[dict[str, str]] = []

        async def send_json(self, message: dict[str, str]) -> None:
            self.sent.append(message)

    live = LiveUpdates()
    first: Any = Window()
    second: Any = Window()
    live.add(first, opener=True)
    live.add(second, opener=True)
    live.focus(first)  # the user clicked into the first window last
    assert asyncio.run(live.ask_opener("http://u")) is True
    assert first.sent == [{"type": "open_workspace", "url": "http://u"}] and second.sent == []
    live.discard(first)
    live.discard(second)
    assert asyncio.run(live.ask_opener("http://u")) is False  # no window: nothing to ask


def test_the_server_counts_how_long_vscode_has_been_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    from swissarmygrid.server.__main__ import end_with_vscode

    clock = [100.0]
    monkeypatch.setattr(live_updates, "monotonic", lambda: clock[0])
    live = LiveUpdates()  # just started: no window yet
    clock[0] = 110.0
    assert live.openers_gone_for() == 10.0
    window: Any = object()
    live.add(window, opener=True)
    clock[0] = 500.0
    assert live.openers_gone_for() == 0.0
    live.discard(window)  # the last VS Code window closed
    clock[0] = 505.0
    assert live.openers_gone_for() == 5.0
    # Gone long enough: a server VS Code started stops itself.
    stopped: list[bool] = []
    clock[0] = 1000.0
    end_with_vscode(live, lambda: stopped.append(True))
    assert stopped == [True]


def test_a_push_while_a_workspace_is_open_counts_as_shown(client: TestClient) -> None:
    import io

    import pyarrow as pa
    import pyarrow.ipc as ipc

    from tests.conftest import ARROW, AUTH, PORT

    sink = io.BytesIO()
    table = pa.table({"a": [1]})
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    headers = AUTH | {"Content-Type": ARROW}
    alone = client.put("/datasets/tabbed", content=sink.getvalue(), headers=headers).json()
    assert alone["watched"] is False and "/workspace#token=" in alone["workspace_url"]
    assert alone["workspace_url"].endswith(f"&grid={alone['grid']}")
    with client.websocket_connect("/ws?workspace=1", headers=AUTH | {"Host": f"127.0.0.1:{PORT}"}) as ws:
        again = client.put("/datasets/tabbed2", content=sink.getvalue(), headers=headers).json()
        assert again["watched"] is True  # the workspace adds it as a tab: no new window
        assert ws.receive_json()["type"] == "dataset_updated"
        assert ws.receive_json() == {"type": "grid_shown", "grid": again["grid"], "dataset": "tabbed2"}
    page = client.get("/workspace")
    assert page.status_code == 200 and "main.js" in page.text  # the grid page; the shell needs no token


def test_the_name_written_in_the_call_wins_over_a_loop_variable() -> None:
    import inspect

    seen: dict[str, str | None] = {}

    def push(data: object) -> None:  # stands in for sag.push: it reads its caller the same way
        frame = inspect.currentframe()
        seen["name"] = push_client.caller_variable_name(data, frame.f_back if frame else None)

    store_paid_orders = [{"a": 1}]
    frame = store_paid_orders  # a second name for the same table, as a loop variable leaves behind
    push(store_paid_orders)
    assert seen["name"] == "store_paid_orders"
    alias = store_paid_orders
    push(alias)  # the name written in the call, even with two names for one object
    assert seen["name"] == "alias"
    del frame
