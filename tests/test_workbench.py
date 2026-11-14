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
from tests.conftest import AGENT, AUTH, make_client


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
