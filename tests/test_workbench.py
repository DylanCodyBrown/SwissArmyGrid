"""The Workbench: notebooks (.ipynb) and scripts opened and saved on the user's click, the recent list,
and real IPython kernels (this test's own Python) streaming a run's outputs. Made-up data only."""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from swissarmygrid.domain import notebooks
from tests.conftest import AGENT, AUTH, PORT, make_client


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with make_client(tmp_path, workbench_dir=tmp_path / "home" / "SwissArmyGrid") as test_client:
        yield test_client


def test_a_notebook_keeps_its_sql_cells_as_sql_cells_jupyter_can_run() -> None:
    cells = [
        notebooks.Cell("markdown", "# Orders\n"),
        notebooks.Cell(
            "code",
            "import swissarmygrid as sag\n",
            outputs=[{"output_type": "stream", "name": "stdout", "text": "ok\n"}],
            execution_count=1,
        ),
        notebooks.Cell(
            "sql", "SELECT region, sum(amount)\nFROM orders\nGROUP BY region", connection="Sales DB"
        ),
        notebooks.Cell("sql", "SELECT 1", connection=None),
    ]
    text = notebooks.write_notebook(cells)
    data = json.loads(text)
    assert data["nbformat"] == 4 and data["cells"][2]["source"][0] == "%%sql 'Sales DB'\n"
    assert data["cells"][3]["source"] == ["%%sql\n", "SELECT 1"]
    read = notebooks.read_notebook(text)
    assert [(c.kind, c.connection) for c in read] == [
        ("markdown", None),
        ("code", None),
        ("sql", "Sales DB"),
        ("sql", None),
    ]
    assert (
        read[2].source == cells[2].source
        and read[1].outputs == cells[1].outputs
        and read[1].execution_count == 1
    )
    # A notebook written by Jupyter: a %%sag cell is a SQL cell; old formats and other files are refused.
    jupyter = {
        "nbformat": 4,
        "cells": [{"cell_type": "code", "source": ["%%sag Shop\n", "SELECT 2"], "outputs": []}],
    }
    assert [(c.kind, c.connection, c.source) for c in notebooks.read_notebook(json.dumps(jupyter))] == [
        ("sql", "Shop", "SELECT 2")
    ]
    with pytest.raises(notebooks.NotebookError):
        notebooks.read_notebook('{"nbformat": 3, "worksheets": []}')
    with pytest.raises(notebooks.NotebookError):
        notebooks.kind_of("report.docx")


def test_open_save_and_recent(client: TestClient, tmp_path: Path) -> None:
    home = client.get("/workbench/home", headers=AUTH).json()
    assert home["folder"] == str(tmp_path / "home" / "SwissArmyGrid") and home["recent"] == []
    # Save as into the default folder makes it; a script and a notebook.
    script = Path(home["folder"]) / "clean.py"
    saved = client.post(
        "/workbench/save", json={"path": str(script), "text": "print('clean')\n"}, headers=AUTH
    )
    assert saved.status_code == 200, saved.text and script.read_text() == "print('clean')\n"
    book = tmp_path / "analysis.ipynb"
    cells = [
        {"kind": "sql", "source": "SELECT 1", "connection": "Shop"},
        {"kind": "markdown", "source": "Done."},
    ]
    first = client.post("/workbench/save", json={"path": str(book), "cells": cells}, headers=AUTH).json()
    opened = client.post("/workbench/open", json={"path": str(book)}, headers=AUTH).json()
    assert opened["kind"] == "notebook" and [c["kind"] for c in opened["cells"]] == ["sql", "markdown"]
    assert opened["cells"][0]["connection"] == "Shop"
    recent = [r["path"] for r in client.get("/workbench/home", headers=AUTH).json()["recent"]]
    assert recent == [str(book.resolve()), str(script.resolve())]  # the last opened or saved first
    # Changed on disk since it was opened: not overwritten unless forced.
    time.sleep(0.02)
    book.write_text(book.read_text(encoding="utf-8"), encoding="utf-8")
    stale = client.post(
        "/workbench/save",
        json={"path": str(book), "cells": cells, "modified": first["modified"]},
        headers=AUTH,
    )
    assert stale.status_code == 409 and "changed on disk" in stale.text
    forced = client.post(
        "/workbench/save",
        json={"path": str(book), "cells": cells, "modified": first["modified"], "force": True},
        headers=AUTH,
    )
    assert forced.status_code == 200
    # Only notebooks and scripts, only folders that are there.
    assert (
        client.post("/workbench/open", json={"path": str(tmp_path / "gone.ipynb")}, headers=AUTH).status_code
        == 404
    )
    assert (
        client.post(
            "/workbench/save", json={"path": str(tmp_path / "x.txt"), "text": ""}, headers=AUTH
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/workbench/save", json={"path": str(tmp_path / "nowhere" / "x.py"), "text": ""}, headers=AUTH
        ).status_code
        == 404
    )


def run(client: TestClient, kernel: str, code: str, **extra: Any) -> list[dict[str, Any]]:
    answer = client.post(f"/workbench/kernels/{kernel}/execute", json={"code": code, **extra}, headers=AUTH)
    assert answer.status_code == 200, answer.text
    return [json.loads(line) for line in answer.text.splitlines() if line]


def test_a_kernel_runs_code_and_streams_its_outputs(client: TestClient, tmp_path: Path) -> None:
    pythons = client.get("/workbench/pythons", headers=AUTH).json()["pythons"]
    assert pythons[0]["python"] == sys.executable and pythons[0]["ipykernel"] is True
    started = client.post("/workbench/kernels", json={"path": str(tmp_path / "a.ipynb")}, headers=AUTH)
    assert started.status_code == 200, started.text
    kernel = started.json()["kernel"]
    try:
        outputs = run(client, kernel, "import os\nprint(os.getcwd())\n6 * 7")
        assert outputs[0] == {
            "output_type": "stream",
            "name": "stdout",
            "text": f"{tmp_path}\n",
        }  # the document's folder
        assert outputs[1]["data"]["text/plain"] == "42" and outputs[-1]["done"]["ok"] is True
        failed = run(client, kernel, "1 / 0")
        assert failed[0]["ename"] == "ZeroDivisionError" and failed[-1]["done"]["ok"] is False
        # The kernel keeps its state between runs; the package is imported quietly when it starts.
        assert run(client, kernel, "'sag' in globals()")[0]["data"]["text/plain"] == "True"
        # Completion and help from the kernel: its own variables, a DataFrame's columns, a signature.
        run(
            client,
            kernel,
            "import pandas as pd\nrainfall = pd.DataFrame({'city': ['Springfield'], 'rain_mm': [3.5]})",
        )
        code = "rainfall.rai"
        done = client.post(
            f"/workbench/kernels/{kernel}/complete", json={"code": code, "offset": len(code)}, headers=AUTH
        )
        assert done.status_code == 200, done.text
        assert "rain_mm" in done.json()["matches"] and done.json()["start"] == len("rainfall.")
        code = "rain"
        assert (
            "rainfall"
            in client.post(
                f"/workbench/kernels/{kernel}/complete", json={"code": code, "offset": 4}, headers=AUTH
            ).json()["matches"]
        )
        helped = client.post(
            f"/workbench/kernels/{kernel}/inspect", json={"code": "len(", "offset": 3}, headers=AUTH
        )
        assert "Return the number of items" in helped.json()["help"]
        bad = client.post(
            f"/workbench/kernels/{kernel}/complete", json={"code": "x", "offset": 9}, headers=AUTH
        )
        assert bad.status_code == 400
        # Interrupt stops a long run.
        stopper = threading.Timer(
            1.5, lambda: client.post(f"/workbench/kernels/{kernel}/interrupt", headers=AUTH)
        )
        stopper.start()
        stopped = run(client, kernel, "import time\nfor i in range(60):\n    time.sleep(0.5)")
        assert stopped[0]["ename"] == "KeyboardInterrupt"
    finally:
        assert client.delete(f"/workbench/kernels/{kernel}", headers=AUTH).status_code == 200
    assert (
        client.post(f"/workbench/kernels/{kernel}/execute", json={"code": "1"}, headers=AUTH).status_code
        == 404
    )


def test_the_agent_may_use_every_workbench_route(client: TestClient) -> None:
    for method, path in (
        ("GET", "/workbench/home"),
        ("POST", "/workbench/open"),
        ("POST", "/workbench/save"),
        ("GET", "/workbench/pythons"),
        ("POST", "/workbench/kernels"),
        ("POST", "/workbench/kernels/abc/execute"),
        ("DELETE", "/workbench/kernels/abc"),
    ):
        assert client.request(method, path, headers=AGENT, json={}).status_code != 403, path


def test_a_new_environment_is_checked_and_a_documents_python_remembered(
    client: TestClient, tmp_path: Path
) -> None:
    from swissarmygrid.domain import environments

    offered = client.get("/workbench/environments", headers=AUTH).json()
    assert "pandas" in offered["presets"]["Data analysis"] and "3.12" in offered["versions"]
    assert {
        "pip": "pandas",
        "label": "pandas",
        "group": "Data",
        "imports": "import pandas as pd",
        "note": "tables (DataFrames)",
    } in offered["libraries"]
    _folder, version, packages = environments.check_request(
        {
            "folder": str(tmp_path / ".venv"),
            "python": "3.12",
            "packages": ["pandas", "psycopg[binary]", "pandas"],
        }
    )
    assert version == "3.12" and packages == ["ipykernel", "pandas", "psycopg[binary]"]
    assert environments.import_lines(["duckdb", "pyodbc", "pandas"]) == [
        "import pandas as pd",
        "import duckdb",
    ]
    # Refused before anything runs: a bad package name, an unknown version; an existing folder says so.
    bad = client.post(
        "/workbench/environments",
        json={"folder": str(tmp_path / "env"), "packages": ["pandas; rm -rf /"]},
        headers=AUTH,
    )
    assert bad.status_code == 400
    assert (
        client.post(
            "/workbench/environments", json={"folder": str(tmp_path / "env"), "python": "2.7"}, headers=AUTH
        ).status_code
        == 400
    )
    there = client.post("/workbench/environments", json={"folder": str(tmp_path)}, headers=AUTH)
    assert there.status_code == 200 and "already there" in there.text
    # A document's Python: remembered when chosen, and a .venv beside a document is found by itself.
    book = tmp_path / "analysis.ipynb"
    client.post("/workbench/save", json={"path": str(book), "cells": []}, headers=AUTH)
    assert client.post("/workbench/open", json={"path": str(book)}, headers=AUTH).json()["python"] is None
    beside = tmp_path / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    beside.parent.mkdir(parents=True)
    beside.symlink_to(sys.executable)
    assert client.post("/workbench/open", json={"path": str(book)}, headers=AUTH).json()["python"] == str(
        beside
    )
    chosen = client.post(
        "/workbench/environment", json={"path": str(book), "python": sys.executable}, headers=AUTH
    )
    assert chosen.status_code == 200, chosen.text
    assert (
        client.post("/workbench/open", json={"path": str(book)}, headers=AUTH).json()["python"]
        == chosen.json()["python"]
    )
    assert (
        client.post("/workbench/environments", json={}, headers=AGENT).status_code == 400
    )  # asked, not refused


def test_the_variables_overlay_shows_values_but_never_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    import os as os_module

    import pandas as pd

    from swissarmygrid.client import kernel_connections, kernel_variables

    monkeypatch.setattr(
        kernel_connections, "code_names", lambda: {"sales_db": "Sales DB", "orders_file": "Orders file"}
    )
    monkeypatch.setenv("SHOP_PASSWORD", "made-up-secret")
    monkeypatch.delenv("OVERLAY_UNSET_PASSWORD", raising=False)
    namespace: dict[str, Any] = {
        "rainfall": pd.DataFrame({"city": ["Springfield", "Riverton"], "rain_mm": [3.5, 0.0]}),
        "cities": ["Springfield", "Riverton", "Ogdenville"],
        "note": "x" * 500,
        "sales_db": 41,  # a connection already opened
        "os": os_module,  # modules, functions and private names are left out
        "helper": lambda: None,
        "_hidden": 1,
    }
    found = json.loads(kernel_variables.snapshot(["SHOP_PASSWORD", "OVERLAY_UNSET_PASSWORD"], namespace))
    assert [v["name"] for v in found["variables"]] == ["cities", "note", "rainfall"]
    rainfall = next(v for v in found["variables"] if v["name"] == "rainfall")
    assert rainfall["size"] == "2 rows by 2 columns" and rainfall["value"] == "columns: city, rain_mm"
    assert rainfall["table"] is True and rainfall["type"] == "pandas.DataFrame"
    assert len(next(v for v in found["variables"] if v["name"] == "note")["value"]) <= 200
    assert found["connections"] == [
        {
            "name": "orders_file",
            "connection": "Orders file",
            "open": False,
            "type": "",
            "value": "opened when a cell first uses it",
            "table": False,
        },
        {
            "name": "sales_db",
            "connection": "Sales DB",
            "open": True,
            "type": "int",
            "value": "41",
            "table": False,
        },
    ]
    assert found["environment"] == [
        {"name": "OVERLAY_UNSET_PASSWORD", "set": False},
        {"name": "SHOP_PASSWORD", "set": True},
    ]
    assert "made-up-secret" not in json.dumps(found)


def test_the_variables_overlay_through_a_kernel(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(
        "OVERLAY_LOGIN_PASSWORD", raising=False
    )  # the kernel inherits this process's environment
    secret_blade = {"name": "Warehouse login", "kind": "secret", "env_names": ["OVERLAY_LOGIN_PASSWORD"]}
    assert client.post("/blades", json=secret_blade, headers=AUTH).status_code == 200
    kernel = client.post("/workbench/kernels", json={}, headers=AUTH).json()["kernel"]
    try:
        run(client, kernel, "import pandas as pd\nrainfall = pd.DataFrame({'rain_mm': [3.5, 0.0, 1.2]})")
        found = client.get(f"/workbench/kernels/{kernel}/variables", headers=AUTH)
        assert found.status_code == 200, found.text
        body = found.json()
        assert body["busy"] is False
        assert [(v["name"], v["size"]) for v in body["variables"]] == [("rainfall", "3 rows by 1 column")]
        assert {"name": "OVERLAY_LOGIN_PASSWORD", "set": False} in body["environment"]
        assert (
            client.post(
                f"/workbench/kernels/{kernel}/grid", json={"name": "not a name"}, headers=AUTH
            ).status_code
            == 400
        )
        assert client.get(f"/workbench/kernels/{kernel}/variables", headers=AGENT).status_code == 200
    finally:
        client.delete(f"/workbench/kernels/{kernel}", headers=AUTH)


# ===== the stress test's findings (2026-11-14), each kept fixed =====


def stream(client: TestClient, kernel: str, body: dict[str, Any], seen: list[dict[str, Any]]) -> None:
    """Run in a thread: each output appended to `seen` as it arrives (so a test can answer input())."""
    with client.stream("POST", f"/workbench/kernels/{kernel}/execute", json=body, headers=AUTH) as answer:
        for line in answer.iter_lines():
            if line:
                seen.append(json.loads(line))


def wait_for(seen: list[dict[str, Any]], test: Any, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while not any(test(item) for item in seen):
        assert time.monotonic() < deadline, seen
        time.sleep(0.05)


def test_a_dead_kernel_is_dropped_and_said_so(client: TestClient) -> None:
    kernel = client.post("/workbench/kernels", json={}, headers=AUTH).json()["kernel"]
    outputs = run(client, kernel, "import os\nos._exit(3)")
    assert outputs[-1]["done"]["dead"] is True
    # Gone from the running kernels: it no longer counts against the limit.
    assert (
        client.post(f"/workbench/kernels/{kernel}/execute", json={"code": "1"}, headers=AUTH).status_code
        == 404
    )


def test_interrupt_cancels_queued_runs_and_restart_ends_the_run_under_way(client: TestClient) -> None:
    kernel = client.post("/workbench/kernels", json={}, headers=AUTH).json()["kernel"]
    try:
        first: list[dict[str, Any]] = []
        queued: list[dict[str, Any]] = []
        threads = [
            threading.Thread(
                target=stream, args=(client, kernel, {"code": "import time\ntime.sleep(30)"}, first)
            ),
            threading.Thread(
                target=stream, args=(client, kernel, {"code": "print('should not run')"}, queued)
            ),
        ]
        threads[0].start()
        time.sleep(1.0)
        threads[1].start()
        time.sleep(0.5)
        assert client.post(f"/workbench/kernels/{kernel}/interrupt", headers=AUTH).status_code == 200
        for t in threads:
            t.join(30)
        assert any(o.get("ename") == "KeyboardInterrupt" for o in first)
        assert queued == [{"done": {"ok": False, "cancelled": True, "execution_count": None}}]
        # A run under way when the kernel restarts ends, saying so.
        under: list[dict[str, Any]] = []
        worker = threading.Thread(
            target=stream, args=(client, kernel, {"code": "import time\ntime.sleep(30)"}, under)
        )
        worker.start()
        time.sleep(1.0)
        assert client.post(f"/workbench/kernels/{kernel}/restart", headers=AUTH).status_code == 200
        worker.join(30)
        assert under[-1]["done"]["restarted"] is True
        # An error no longer drops the run queued behind it (stop_on_error is off).
        assert run(client, kernel, "1/0")[-1]["done"]["ok"] is False
        assert run(client, kernel, "print('next')")[0]["text"] == "next\n"
    finally:
        client.delete(f"/workbench/kernels/{kernel}", headers=AUTH)


def test_input_is_asked_of_the_page_and_late_output_still_arrives(client: TestClient) -> None:
    kernel = client.post("/workbench/kernels", json={}, headers=AUTH).json()["kernel"]
    try:
        seen: list[dict[str, Any]] = []
        worker = threading.Thread(
            target=stream,
            args=(client, kernel, {"code": "name = input('Who? ')\nprint('hello', name)"}, seen),
        )
        worker.start()
        wait_for(seen, lambda o: "input_request" in o)
        assert next(o for o in seen if "input_request" in o)["input_request"] == {
            "prompt": "Who? ",
            "password": False,
        }
        assert (
            client.post(f"/workbench/kernels/{kernel}/input", json={"value": "Ana"}, headers=AUTH).status_code
            == 200
        )
        worker.join(30)
        assert any(o.get("text") == "hello Ana\n" for o in seen)
        # The agent's runs have no stdin: input() says so instead of waiting for ever.
        refused = run(client, kernel, "input('x')", stdin=False)
        assert refused[0]["ename"] == "StdinNotImplementedError"
        # A thread that prints after its run ended: its output comes with the next run, marked late.
        run(client, kernel, "import threading, time\nthreading.Timer(0.3, lambda: print('later')).start()")
        time.sleep(0.8)
        nxt = run(client, kernel, "print('now')")
        assert {"output_type": "stream", "name": "stdout", "text": "later\n", "late": True} in nxt
        # Complete or not, for the console's Enter.
        assert (
            client.post(
                f"/workbench/kernels/{kernel}/is-complete", json={"code": "for i in range(3):"}, headers=AUTH
            ).json()["status"]
            == "incomplete"
        )
        assert (
            client.post(
                f"/workbench/kernels/{kernel}/is-complete", json={"code": "x = 1"}, headers=AUTH
            ).json()["status"]
            == "complete"
        )
    finally:
        client.delete(f"/workbench/kernels/{kernel}", headers=AUTH)


def test_run_file_runs_a_script_as_python_would(client: TestClient, tmp_path: Path) -> None:
    script = tmp_path / "my script.py"
    script.write_text(
        "import asyncio, multiprocessing, sys\n"
        "def square(n):\n    return n * n\n"
        "async def main():\n    return 'async ok'\n"
        "if __name__ == '__main__':\n"
        "    print(__file__.endswith('my script.py'), sys.argv[1:])\n"
        "    print(asyncio.run(main()))\n"
        "    with multiprocessing.get_context('spawn').Pool(2) as pool:\n"
        "        print(pool.map(square, [1, 2, 3]))\n",
        encoding="utf-8",
    )
    kernel = client.post("/workbench/kernels", json={"path": str(script)}, headers=AUTH).json()["kernel"]
    try:
        outputs = run(client, kernel, "", kind="file", path=str(script), args="north 2024")
        text = "".join(o.get("text", "") for o in outputs if o.get("output_type") == "stream")
        assert "True ['north', '2024']" in text and "async ok" in text and "[1, 4, 9]" in text, outputs
        assert outputs[-1]["done"]["ok"] is True
        # Its variables stay for the Variables pane; an unsaved script runs from a private copy.
        assert run(client, kernel, "square(5)")[0]["data"]["text/plain"] == "25"
        assert "untitled" in "".join(
            o.get("text", "") for o in run(client, kernel, "print(__file__)", kind="file")
        )
        # exit() and sys.exit() say what happened in one line; help shows as output.
        assert (
            run(client, kernel, "import sys\nsys.exit(3)")[0]["text"] == "The code ended with sys.exit(3).\n"
        )
        helped = run(client, kernel, "len?")
        assert any("Return the number of items" in json.dumps(o) for o in helped)
    finally:
        client.delete(f"/workbench/kernels/{kernel}", headers=AUTH)


def test_saving_keeps_the_files_encoding_line_endings_and_permissions(
    client: TestClient, tmp_path: Path
) -> None:
    import os
    import stat

    crlf = tmp_path / "windows.py"
    crlf.write_bytes(b"\xef\xbb\xbfprint('caf\xc3\xa9')\r\nx = 1\r\n")  # UTF-8 with a byte-order mark, CRLF
    os.chmod(crlf, 0o644)
    opened = client.post("/workbench/open", json={"path": str(crlf)}, headers=AUTH).json()
    assert opened["text"] == "print('café')\r\nx = 1\r\n"  # the mark taken off, the line endings kept
    client.post(
        "/workbench/save", json={"path": str(crlf), "text": opened["text"] + "y = 2\r\n"}, headers=AUTH
    )
    assert crlf.read_bytes() == b"\xef\xbb\xbfprint('caf\xc3\xa9')\r\nx = 1\r\ny = 2\r\n"
    assert stat.S_IMODE(crlf.stat().st_mode) == 0o644
    latin = tmp_path / "old.py"
    latin.write_bytes(b"# -*- coding: latin-1 -*-\nname = 'Ren\xe9'\n")
    assert "René" in client.post("/workbench/open", json={"path": str(latin)}, headers=AUTH).json()["text"]
    client.post(
        "/workbench/save",
        json={"path": str(latin), "text": "# -*- coding: latin-1 -*-\nname = 'Zoë'\n"},
        headers=AUTH,
    )
    assert latin.read_bytes().endswith(b"'Zo\xeb'\n")
    locked = tmp_path / "locked.py"
    locked.write_text("x = 1\n", encoding="utf-8")
    os.chmod(locked, 0o444)
    refused = client.post("/workbench/save", json={"path": str(locked), "text": "x = 2\n"}, headers=AUTH)
    assert refused.status_code == 409 and "read-only" in refused.text
    assert locked.read_text(encoding="utf-8") == "x = 1\n"
    forced = client.post(
        "/workbench/save", json={"path": str(locked), "text": "x = 2\n", "force": True}, headers=AUTH
    )
    assert forced.status_code == 200 and locked.read_text(encoding="utf-8") == "x = 2\n"


def test_connection_names_never_hide_a_builtin_or_the_cells_own_name(monkeypatch: pytest.MonkeyPatch) -> None:
    import ast

    from swissarmygrid.client import kernel_connections
    from swissarmygrid.domain.connections import code_names

    assert code_names(["Print", "List", "Input", "ID", "Type", "Open"]) == {
        "Print": "print_",
        "List": "list_",
        "Input": "input_",
        "ID": "id_",
        "Type": "type_",
        "Open": "open_",
    }
    tree = ast.parse(
        "broken_db = 1\nprint(broken_db + 1)\nfor sales_db in []: pass\nimport orders\nwarehouse.cursor()"
    )
    assert kernel_connections.used_names(tree) == {"print", "warehouse"}


def test_one_odd_object_costs_its_row_never_the_list() -> None:
    from swissarmygrid.client import kernel_variables

    class Odd:
        columns = ("a",)

        @property
        def shape(self) -> Any:
            raise RuntimeError("no shape today")

        def __repr__(self) -> str:
            raise ValueError("no repr either")

    found = json.loads(kernel_variables.snapshot([], {"odd": Odd(), "fine": [1, 2]}))
    rows = {v["name"]: v for v in found["variables"]}
    assert rows["fine"]["size"] == "2" and rows["odd"]["value"].startswith("<could not be read")


# ===== Ctrl+Enter: the whole statement at the cursor, then on to the next =====


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (1, (1, 1, 3)),  # import, then on to the decorated function (its decorator line)
        (3, (3, 5, 8)),  # the decorator: the decorator and the function
        (5, (3, 5, 8)),  # inside the function's body: the whole function
        (7, None),  # a blank line: nothing to run (the page moves on)
        (8, (8, 11, 13)),  # a call spread over lines
        (10, (8, 11, 13)),
        (13, (13, 16, None)),  # for with its else
        (16, (13, 16, None)),
    ],
)
def test_the_statement_at_a_line(line: int, expected: tuple[int, int, int | None] | None) -> None:
    from swissarmygrid.domain.notebooks import statement_at

    code = (
        "import math\n"  # 1
        "\n"
        "@staticmethod\n"  # 3
        "def area(r):\n"
        "    return math.pi * r ** 2\n"  # 5
        "\n"
        "\n"  # 7
        "total = sum(\n"  # 8
        "    [1, 2],\n"
        "    start=3,\n"  # 10
        ")\n"
        "# a comment\n"
        "for i in range(3):\n"  # 13
        "    print(i)\n"
        "else:\n"
        "    print('done')\n"  # 16
    )
    assert statement_at(code, line) == expected


def test_the_statement_at_a_line_while_the_rest_does_not_parse() -> None:
    from swissarmygrid.domain.notebooks import statement_at

    code = "for r in rates:\n    print(r)\n\nx = (\n"  # the last line is half typed
    assert statement_at(code, 1) == (1, 2, 4)
    assert statement_at(code, 2) == (1, 2, 4)
    assert statement_at(code, 4) == (4, 4, None)  # it does not parse: the line alone (the error says why)


def test_the_statement_route(client: TestClient) -> None:
    answer = client.post(
        "/workbench/statement", json={"code": "def f():\n    return 1\n\nf()\n", "line": 2}, headers=AUTH
    )
    assert answer.json() == {"start": 1, "end": 2, "next": 4}
    assert client.post("/workbench/statement", json={"code": "x", "line": 0}, headers=AUTH).status_code == 400


# ===== the agent in the Workbench window: workbench_window, answered by the window =====


def test_a_window_command_is_run_by_the_window_and_answered(client: TestClient) -> None:
    import threading as _threading

    # No Workbench window open: said plainly.
    refused = client.post("/workbench/window", json={"command": "documents"}, headers=AUTH)
    assert refused.status_code == 409 and "not open" in refused.text
    assert (
        client.post("/workbench/window", json={"command": "no spaces allowed"}, headers=AUTH).status_code
        == 400
    )
    with client.websocket_connect(
        "/ws?catalog=1&workbench=1", headers=AUTH | {"Host": f"127.0.0.1:{PORT}"}
    ) as window:
        answers: list[Any] = []
        asking = _threading.Thread(
            target=lambda: answers.append(
                client.post(
                    "/workbench/window",
                    json={"command": "cell.insertBelow", "args": {"kind": "sql"}},
                    headers=AUTH,
                )
            )
        )
        asking.start()
        notice = window.receive_json()
        while notice.get("type") != "workbench_command":
            notice = window.receive_json()
        assert notice["command"] == "cell.insertBelow" and notice["args"] == {"kind": "sql"}
        # The agent cannot answer for the window.
        agent = client.post(f"/workbench/window/{notice['id']}", json={"result": 1}, headers=AGENT)
        assert agent.status_code == 403
        assert client.post(
            f"/workbench/window/{notice['id']}", json={"result": {"cells": 2}}, headers=AUTH
        ).json() == {"received": True}
        asking.join(10)
        assert answers[0].json() == {"result": {"cells": 2}}
        # The window says why it could not.
        answers.clear()
        asking = _threading.Thread(
            target=lambda: answers.append(
                client.post("/workbench/window", json={"command": "run.cell"}, headers=AUTH)
            )
        )
        asking.start()
        notice = window.receive_json()
        while notice.get("type") != "workbench_command":
            notice = window.receive_json()
        client.post(
            f"/workbench/window/{notice['id']}",
            json={"error": "The document in front is not a notebook."},
            headers=AUTH,
        )
        asking.join(10)
        assert answers[0].status_code == 409 and "not a notebook" in answers[0].text
    assert client.post("/workbench/window/nothing", json={"result": 1}, headers=AUTH).status_code == 404


def test_window_commands_go_to_the_workbench_window_last_in_front() -> None:
    import asyncio

    from swissarmygrid.adapters.live_updates import LiveUpdates

    class Window:
        def __init__(self) -> None:
            self.got: list[dict[str, Any]] = []

        async def send_json(self, message: dict[str, Any]) -> None:
            self.got.append(message)

    live = LiveUpdates()
    first, second, grid_page = Window(), Window(), Window()
    live.add(first, workbench=True)  # type: ignore[arg-type]
    live.add(grid_page, "g1")  # type: ignore[arg-type]
    live.add(second, workbench=True)  # type: ignore[arg-type]
    assert asyncio.run(live.tell_workbench({"n": 1})) == 1
    assert second.got == [{"n": 1}] and first.got == [] and grid_page.got == []
    live.focus(first)  # type: ignore[arg-type]
    asyncio.run(live.tell_workbench({"n": 2}))
    assert first.got == [{"n": 2}]
    live.discard(first)  # type: ignore[arg-type]
    assert live.workbench_open()
    live.discard(second)  # type: ignore[arg-type]
    assert not live.workbench_open() and asyncio.run(live.tell_workbench({"n": 3})) == 0


def test_a_failed_run_says_what_happened_and_what_to_do(client: TestClient) -> None:
    kernel = client.post("/workbench/kernels", json={}, headers=AUTH).json()["kernel"]
    try:
        run(
            client,
            kernel,
            "rates = {'north': 2, 'south': 3}\ndef share(region):\n    return 10 / rates[region]",
        )
        outputs = run(client, kernel, "share('nroth')")
        assert outputs[0]["output_type"] == "error"  # Jupyter's own record, as a notebook saves it
        detail = next(o["error_detail"] for o in outputs if "error_detail" in o)
        last = detail["chain"][-1]
        assert last["type"] == "KeyError" and detail["where"].startswith("cell [")
        assert last["explanation"].startswith("There is no key 'nroth'. The keys are 'north' and 'south'.")
        assert last["fixes"] == [
            {"kind": "replace", "title": "Use 'north'", "target": "nroth", "text": "north"}
        ]
        inner = last["frames"][last["your_frame"]]
        assert (
            inner["function"] == "share"
            and inner["line"] == 3
            and (inner["col"], inner["end_col"]) == (17, 30)
        )
        assert {"expr": "region", "repr": "'nroth'", "type": "str"} in inner["values"]  # stack_data's values
        assert outputs[-1]["done"]["ok"] is False
        # A missing import offers the line that brings it in.
        detail = next(o["error_detail"] for o in run(client, kernel, "pd.DataFrame()") if "error_detail" in o)
        assert detail["chain"][-1]["fixes"][0]["line"] == "import pandas as pd"
        # A run that worked sends none.
        assert not any("error_detail" in o for o in run(client, kernel, "1 + 1"))
    finally:
        client.delete(f"/workbench/kernels/{kernel}", headers=AUTH)


@pytest.mark.parametrize("folder", ["x", "nope-does-not-exist", "projects/.venv", "..\\env"])
def test_a_new_environment_needs_a_full_path(folder: str) -> None:
    from swissarmygrid.domain import environments

    with pytest.raises(environments.EnvironmentRequestError, match="full path"):
        environments.check_request({"folder": folder, "packages": []})
    for full in ("C:\\Users\\ana\\.venv", "\\\\server\\share\\env", "/home/ana/.venv"):
        assert environments.is_full_path(full)


VAULT = {
    "name": "Team vault",
    "kind": "vault",
    "template": "azure_key_vault",
    "variable": "get",
    "code": "values = {'warehouse-password': 'made-up-not-real'}\n\n\n"
    "def get(name):\n    return values[name]\n",
    "vault_mapping": [{"env": "WAREHOUSE_PASSWORD", "secret": "warehouse-password"}],
}


def test_a_kernel_starts_with_every_vaults_variables_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WAREHOUSE_PASSWORD", raising=False)  # another test's blade may have set it here
    assert client.post("/connections", json=VAULT, headers=AUTH).status_code == 200
    broken = VAULT | {"name": "Broken vault", "code": "def get(name):\n    raise KeyError(name)\n"}
    broken |= {"vault_mapping": [{"env": "OTHER_PASSWORD", "secret": "other"}]}
    assert client.post("/connections", json=broken, headers=AUTH).status_code == 200
    started = client.post("/workbench/kernels", json={}, headers=AGENT)  # the agent too (the golden rule)
    assert started.status_code == 200, started.text
    vaults = started.json()["vaults"]
    assert vaults["set"] == ["WAREHOUSE_PASSWORD"] and "made-up" not in started.text
    assert len(vaults["problems"]) == 1 and "Broken vault" in vaults["problems"][0]
    kernel = started.json()["kernel"]
    try:
        out = run(client, kernel, "import os\nos.environ['WAREHOUSE_PASSWORD'] == 'made-up-not-real'")
        assert out[0]["data"]["text/plain"] == "True"
        assert run(client, kernel, "'OTHER_PASSWORD' in os.environ")[0]["data"]["text/plain"] == "False"
        # A restart keeps them.
        assert client.post(f"/workbench/kernels/{kernel}/restart", headers=AUTH).status_code == 200
        out = run(client, kernel, "import os\nos.environ.get('WAREHOUSE_PASSWORD') == 'made-up-not-real'")
        assert out[0]["data"]["text/plain"] == "True"
    finally:
        client.delete(f"/workbench/kernels/{kernel}", headers=AUTH)
    # The server never kept the value: not in its own environment.
    import os

    assert "WAREHOUSE_PASSWORD" not in os.environ


def test_no_vault_starts_no_child_process(client: TestClient) -> None:
    started = client.post("/workbench/kernels", json={}, headers=AUTH).json()
    try:
        assert started["vaults"] == {"set": [], "problems": []}
    finally:
        client.delete(f"/workbench/kernels/{started['kernel']}", headers=AUTH)
