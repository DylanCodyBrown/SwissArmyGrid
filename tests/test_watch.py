"""The watcher: finding the tables in a running Python, and the loop that offers them to the grid.

All data here is made up. The loop tests run a real server in a thread and point the client at it with
SWISSARMYGRID_INFO, so nothing touches the real ~/.swissarmygrid.
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import pyarrow as pa
import pytest
import uvicorn

from swissarmygrid.client import push_client, watcher
from swissarmygrid.client.watcher import Watcher, find_tables, label_for, resolve
from swissarmygrid.server.app import create_app
from swissarmygrid.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


class ArrowSpeaker:
    """Stands in for a query client's result object: columns and rows, readable as an Arrow stream."""

    def __init__(self, columns: list[str], rows: list[list[Any]]) -> None:
        self.columns, self.rows = columns, rows

    def __arrow_c_stream__(self, requested_schema: Any = None) -> Any:
        table = pa.table({c: [r[i] for r in self.rows] for i, c in enumerate(self.columns)})
        return table.__arrow_c_stream__(requested_schema)


# ===== which variables are tables =====


def test_tables_are_found_by_class_and_everything_else_is_left_alone() -> None:
    namespace: dict[str, Any] = {
        "df": pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]}),
        "arr": pa.table({"x": [1]}),
        "res": ArrowSpeaker(["a", "b"], [[1, 2], [3, 4]]),
        "rel": duckdb.sql("SELECT 1 AS one"),
        "n": 5,
        "text": "hello",
        "records": [{"a": 1}],  # a list of dicts could be any config, so it is not a table
        "func": len,
        "cls": ArrowSpeaker,
        "mod": duckdb,
        "_hidden": pd.DataFrame({"a": [1]}),
        "In": [],
        "Out": {},
        "bad name": pd.DataFrame({"a": [1]}),
    }
    found = {t.name: t for t in find_tables(namespace)}
    assert sorted(found) == ["arr", "df", "rel", "res"]
    assert (found["df"].kind, found["df"].rows, found["df"].columns) == ("dataframe", 3, 2)
    assert (found["arr"].kind, found["arr"].rows, found["arr"].columns) == ("arrow", 1, 1)
    assert (found["res"].kind, found["res"].rows, found["res"].columns) == ("arrow", 2, 2)
    assert (found["rel"].kind, found["rel"].rows, found["rel"].columns) == ("relation", None, 1)


def test_an_object_with_a_magic_getattr_is_never_touched() -> None:
    class Hostile:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the scanner touched {name}")

    assert find_tables({"hostile": Hostile()}) == []


class _RaisingMeta(type):
    def __getattr__(cls, name: str) -> Any:
        raise RuntimeError("a class that answers every lookup with an error")


class _Hostile(metaclass=_RaisingMeta):
    pass


class _NoModule:
    pass


_NoModule.__module__ = None  # type: ignore[assignment]


@pytest.mark.parametrize("odd", [_Hostile(), _NoModule()])
def test_one_odd_variable_never_hides_the_other_tables(odd: object) -> None:
    """hasattr only swallows AttributeError, and a class may have no module name: either made every
    scan raise, so the Open list stayed empty for the whole session."""
    namespace = {"orders": pa.table({"x": [1]}), "odd": odd}
    assert [t.name for t in find_tables(namespace)] == ["orders"]
    assert resolve(namespace, "odd") is None


def test_the_entry_sent_to_the_server_has_a_name_a_kind_and_sizes_only() -> None:
    [table] = find_tables({"df": pd.DataFrame({"a": [1, 2]})})
    assert table.entry() == {"name": "df", "kind": "dataframe", "rows": 2, "columns": 1}


def test_reassigning_a_variable_changes_its_fingerprint_and_leaving_it_does_not() -> None:
    namespace: dict[str, Any] = {"df": pd.DataFrame({"a": [1, 2]})}
    before = find_tables(namespace)[0].fingerprint
    assert find_tables(namespace)[0].fingerprint == before
    namespace["df"] = pd.DataFrame({"a": [1, 2]})  # same shape, a new object: a rerun
    assert find_tables(namespace)[0].fingerprint != before


# ===== tables inside a DuckDB connection =====


def test_tables_inside_a_connection_are_listed_and_the_connection_is_left_alone() -> None:
    con = duckdb.connect()
    con.execute("CREATE TABLE korea_rate_card AS SELECT * FROM range(5) t(a)")
    con.execute('CREATE TABLE "with space" AS SELECT 1 AS a')
    found = {t.name: t for t in find_tables({"con": con})}
    assert sorted(found) == ["korea_rate_card"]  # "with space" is not a dataset name
    assert (
        found["korea_rate_card"].kind,
        found["korea_rate_card"].rows,
        found["korea_rate_card"].columns,
    ) == (
        "table",
        5,
        1,
    )
    assert con.execute("SELECT count(*) FROM korea_rate_card").fetchone() == (5,)  # untouched, still usable


def test_a_variable_wins_over_a_connection_table_of_the_same_name() -> None:
    con = duckdb.connect()
    con.execute("CREATE TABLE sod AS SELECT 1 AS a")
    [table] = find_tables({"con": con, "sod": pd.DataFrame({"a": [1, 2, 3]})})
    assert (table.name, table.kind, table.rows) == ("sod", "dataframe", 3)


def test_a_closed_connection_is_skipped() -> None:
    con = duckdb.connect()
    con.execute("CREATE TABLE sod AS SELECT 1 AS a")
    con.close()
    assert find_tables({"con": con}) == []


def test_resolve_gives_the_variable_or_reads_the_connection_table_as_arrow() -> None:
    con = duckdb.connect()
    con.execute("CREATE TABLE claims AS SELECT * FROM range(4) t(a)")
    frame = pd.DataFrame({"a": [1]})
    namespace: dict[str, Any] = {"con": con, "frame": frame}
    assert resolve(namespace, "frame") is frame
    from_connection = resolve(namespace, "claims")
    assert isinstance(from_connection, pa.Table) and from_connection.num_rows == 4
    assert resolve(namespace, "nope") is None
    assert resolve({"frame": 5}, "frame") is None  # the name no longer holds a table


def test_the_default_duckdb_connection_is_scanned_too() -> None:
    duckdb.sql("CREATE OR REPLACE TABLE default_conn_sod AS SELECT 1 AS a, 2 AS b")
    try:
        found = {t.name: t for t in find_tables({})}
        assert found["default_conn_sod"].columns == 2
    finally:
        duckdb.sql("DROP TABLE IF EXISTS default_conn_sod")


# ===== the session label =====


def test_the_label_names_the_notebook_then_the_script_then_the_process() -> None:
    assert label_for({"__vsc_ipynb_file__": r"C:\work\Tie out.ipynb", "__file__": "x.py"}) == "Tie out.ipynb"
    assert label_for({"__file__": "/a/b/run.py"}) == "run.py"
    assert label_for({}).startswith("Python ")


# ===== when it starts by itself =====


def test_autostart_follows_ipython_and_the_environment_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[int] = []
    monkeypatch.setattr(watcher, "start", lambda namespace=None: started.append(1))
    monkeypatch.delenv("SWISSARMYGRID_WATCH", raising=False)

    monkeypatch.delitem(sys.modules, "IPython", raising=False)
    watcher.autostart()
    assert started == []  # a plain script, the server, pytest: nothing starts by itself

    monkeypatch.setitem(sys.modules, "IPython", types.SimpleNamespace(get_ipython=lambda: object()))
    watcher.autostart()
    assert started == [1]  # a notebook or the interactive window

    monkeypatch.setenv("SWISSARMYGRID_WATCH", "0")
    watcher.autostart()
    assert started == [1]  # switched off

    monkeypatch.delitem(sys.modules, "IPython")
    monkeypatch.setenv("SWISSARMYGRID_WATCH", "1")
    watcher.autostart()
    assert started == [1, 1]  # forced on in a script


def test_importing_the_package_in_a_plain_process_starts_no_thread() -> None:
    probe = "import threading, swissarmygrid; print(threading.active_count())"
    out = subprocess.run(  # noqa: S603 (this Python, a fixed probe)
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, cwd=ROOT
    ).stdout
    assert out.strip() == "1"


# ===== reporting a failure =====


def test_a_table_that_is_too_large_is_reported_for_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> str:
        raise push_client.DatasetTooLargeError("Dataset 'SOD' is about 2.0 MiB; the limit is 1 MiB.")

    monkeypatch.setattr(push_client, "push", refuse)
    namespace: dict[str, Any] = {"SOD": pd.DataFrame({"a": [1]})}
    w = Watcher(lambda: namespace)
    w.load("SOD", follow=False)
    assert w.take_errors() == [
        {"name": "SOD", "message": "Dataset 'SOD' is about 2.0 MiB; the limit is 1 MiB."}
    ]
    assert w.take_errors() == []


# ===== the whole loop against a real server =====


class Live:
    """A server in a thread, and calls to it as the page would make them."""

    def __init__(self, port: int, token: str) -> None:
        self.port, self.token = port, token

    def call(self, method: str, path: str, body: Any = None) -> Any:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return json.loads(response.read())

    def datasets(self) -> dict[str, dict[str, Any]]:
        return {d["name"]: d for d in self.call("GET", "/datasets")["datasets"]}

    def tables(self) -> list[dict[str, Any]]:
        return [t for p in self.call("GET", "/processes")["processes"] for t in p["tables"]]


def until(condition: Callable[[], bool], seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):  # the server may not answer yet
            if condition():
                return True
        time.sleep(0.05)
    return False


@pytest.fixture
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Live]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    token = "watch-test-token"
    settings = Settings(
        state_dir=tmp_path / "state", export_dir=tmp_path / "exports", max_total_bytes=256 * 1024 * 1024
    )
    server = uvicorn.Server(uvicorn.Config(create_app(port, token, settings), port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    assert until(lambda: server.started, 20)
    info = tmp_path / "server.json"
    info.write_text(
        json.dumps({"port": port, "token": token, "max_dataset_bytes": settings.max_dataset_bytes})
    )
    monkeypatch.setenv("SWISSARMYGRID_INFO", str(info))
    yield Live(port, token)
    server.should_exit = True
    thread.join(10)


def test_detect_open_follow_and_stay_closed(live: Live) -> None:
    namespace: dict[str, Any] = {"SOD": pd.DataFrame({"a": [1, 2, 3]}), "n": 5}
    w = Watcher(
        lambda: namespace, "tie_out.ipynb", scan_seconds=0.05, heartbeat_seconds=0.5, poll_seconds=0.5
    )
    w.start()
    try:
        # 1. The session and its table appear, with sizes and nothing loaded.
        assert until(lambda: [t["name"] for t in live.tables()] == ["SOD"])
        [session] = live.call("GET", "/processes")["processes"]
        assert session["label"] == "tie_out.ipynb"
        entry = live.tables()[0]
        assert {k: entry[k] for k in ("name", "kind", "rows", "columns")} == {
            "name": "SOD",
            "kind": "dataframe",
            "rows": 3,
            "columns": 1,
        }
        assert entry["open"] is False
        assert live.datasets() == {}  # detecting sent no rows

        # 2. Opening it loads the table as a dataset of the same name.
        live.call("POST", f"/processes/{session['id']}/open", {"name": "SOD"})
        assert until(lambda: live.datasets().get("SOD", {}).get("rows") == 3)
        assert until(lambda: live.tables()[0]["open"] is True)

        # 3. Running the cell again (a new object) refreshes the open table in place.
        namespace["SOD"] = pd.DataFrame({"a": [1, 2, 3, 4, 5]})
        assert until(lambda: live.datasets()["SOD"]["rows"] == 5)
        assert until(lambda: live.tables()[0]["rows"] == 5)

        # 4. Closing the table in the grid stops the following: a new run does not bring it back.
        live.call("DELETE", "/datasets/SOD")
        namespace["SOD"] = pd.DataFrame({"a": [9]})
        time.sleep(1.5)
        assert live.datasets() == {}
        assert w.alive()

        # 5. Opening it again from the list works.
        live.call("POST", f"/processes/{session['id']}/open", {"name": "SOD"})
        assert until(lambda: live.datasets().get("SOD", {}).get("rows") == 1)
    finally:
        w.stop()


def test_a_table_inside_a_connection_opens_and_follows_its_size(live: Live) -> None:
    con = duckdb.connect()
    con.execute("CREATE TABLE korea_rate_card AS SELECT * FROM range(5) t(a)")
    namespace: dict[str, Any] = {"con": con}
    w = Watcher(lambda: namespace, "build.ipynb", scan_seconds=0.05, heartbeat_seconds=0.5, poll_seconds=0.5)
    w.start()
    try:
        assert until(lambda: [t["name"] for t in live.tables()] == ["korea_rate_card"])
        [session] = live.call("GET", "/processes")["processes"]
        live.call("POST", f"/processes/{session['id']}/open", {"name": "korea_rate_card"})
        assert until(lambda: live.datasets().get("korea_rate_card", {}).get("rows") == 5)
        con.execute("INSERT INTO korea_rate_card SELECT * FROM range(3)")
        assert until(lambda: live.datasets()["korea_rate_card"]["rows"] == 8)
    finally:
        w.stop()


def test_the_watcher_waits_for_a_server_and_connects_when_one_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWISSARMYGRID_INFO", str(tmp_path / "missing.json"))
    namespace: dict[str, Any] = {"SOD": pd.DataFrame({"a": [1]})}
    w = Watcher(lambda: namespace, scan_seconds=0.05, heartbeat_seconds=0.5, poll_seconds=0.5)
    w.start()
    try:
        time.sleep(0.5)
        assert w.alive()  # no server, no crash, no output
    finally:
        w.stop()
