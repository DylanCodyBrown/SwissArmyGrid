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


def test_the_agent_is_refused(client: TestClient) -> None:
    for method, path in (
        ("GET", "/workbench/home"),
        ("POST", "/workbench/open"),
        ("POST", "/workbench/save"),
        ("GET", "/workbench/pythons"),
        ("POST", "/workbench/kernels"),
        ("POST", "/workbench/kernels/abc/execute"),
        ("DELETE", "/workbench/kernels/abc"),
    ):
        assert client.request(method, path, headers=AGENT, json={}).status_code == 403, path
