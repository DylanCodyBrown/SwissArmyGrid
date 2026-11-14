# ruff: noqa: F811
"""Every table's read-only cell, against a running server and a real kernel: the cell's text follows
the grid's steps, runs twice without transforming twice, and its result is never sent back to the
grid. Made-up data only."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import swissarmygrid as sag
from swissarmygrid.client import push_client, table_cells, watcher
from tests.test_cli import Live, live, run  # noqa: F401 (the running server, and the agent's commands)

ORDERS = pd.DataFrame(
    {
        "order_id": [1, 2, 3, 4, 5, 6],
        "region": ["North", "South", "North", "East", "South", "North"],
        "amount": [10.5, 7.0, 2.5, 40.0, 31.0, 22.0],
    }
)
STEPS = [
    {"kind": "filter", "params": {"column": "amount", "op": "gt", "value": 5}},
    {"kind": "sort", "params": {"keys": [["amount", "desc"]]}},
]


def table(live: Live, name: str) -> dict[str, Any]:
    return next(t for t in live.user("GET", "/workbench/tables")["tables"] if t["name"] == name)


def grid_rows(live: Live, grid: str) -> list[list[Any]]:
    return list(live.user("GET", f"/grids/{grid}/rows?start=0&end=100")["rows"])


def lines(live: Live, path: str, body: Any) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{live.port}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {live.token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 (the test's own server)
        return [json.loads(line) for line in response.read().decode().splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def clean() -> Any:
    table_cells.forget()
    yield
    table_cells.forget()


def test_a_tables_cell_is_the_grids_steps_and_running_it_twice_changes_nothing(live: Live) -> None:
    sag.push(ORDERS, name="orders")
    listed = table(live, "orders")
    assert listed["steps"] == 0 and listed["origin"] == "python" and listed["kernel"] is None
    assert listed["code"] == "# orders: no transformations yet. Steps made in the grid appear here."
    grid = listed["grid"]
    live.user("POST", f"/grids/{grid}/steps", {"steps": STEPS})
    listed = table(live, "orders")
    code = listed["code"]
    # One DuckDB query whose result is the table's own name, as the table was (a pandas DataFrame).
    assert code.startswith('import duckdb\n\norders = duckdb.sql("""\n') and code.endswith('""").df()')
    assert listed["steps"] == 2 and [s["kind"] for s in listed["step_list"]] == ["filter", "sort"]
    namespace: dict[str, Any] = {"orders": ORDERS}
    table_cells.note_original("orders", ORDERS)
    expected = [row[:3] for row in grid_rows(live, grid)]
    for _ in range(3):  # safe to repeat: it always starts from what the maker made
        table_cells.run("orders", code, namespace)
        assert namespace["orders"].values.tolist() == expected
    assert len(expected) == 5 and namespace["orders"] is not ORDERS and len(ORDERS) == 6
    # A sort made by clicking a header is a way of looking: the cell does not change.
    live.user("POST", f"/grids/{grid}/header-sort", {"keys": [["region", "asc"]]})
    assert table(live, "orders")["code"] == code
    # A step that cannot run: the cell says so instead of holding a query that fails.
    live.user(
        "POST", f"/grids/{grid}/steps", {"steps": [*STEPS, {"kind": "columns", "params": {"keep": ["x"]}}]}
    )
    broken = table(live, "orders")
    assert broken["problem"].startswith("Step 3") and "a step in the grid cannot run" in broken["code"]


def test_a_table_no_code_made_starts_with_its_load(live: Live, tmp_path: Path) -> None:
    pasted = run("datasets_paste", name="fruit", text="item\tprice\napple\t1.5\npear\t2\n")
    live.user("POST", f"/grids/{pasted['grid']}/steps", {"steps": [{"kind": "limit", "params": {"n": 1}}]})
    fruit = table(live, "fruit")
    assert fruit["origin"] == "paste" and "FROM (VALUES" in fruit["loaded"]
    assert "VALUES" not in fruit["code"]  # the transformations alone, for a table a cell makes
    namespace: dict[str, Any] = {}
    table_cells.run("fruit", fruit["loaded"], namespace)
    table_cells.run("fruit", fruit["loaded"], namespace)  # the load runs again: still one row
    assert namespace["fruit"].values.tolist() == [["apple", 1.5]]
    # A file: its path, read again by the cell.
    csv = tmp_path / "sales.csv"
    csv.write_text("store,sales\nNorth,10\nSouth,7\n", encoding="utf-8")
    run("datasets_load", path=str(csv), name="sales")
    sales = table(live, "sales")
    assert sales["origin"] == "file" and f"duckdb.read_csv({str(csv)!r}).df()" in sales["loaded"]
    # Run as plain Python in a Python of its own (in this process the server has locked DuckDB's
    # default connection away from files, as it does for copied code).
    plain = subprocess.run(  # noqa: S603 (this Python, the made-up cell above)
        [sys.executable, "-c", sales["loaded"] + "\nprint(sales.values.tolist())"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert plain.stdout.strip() == "[['North', 10], ['South', 7]]", plain.stderr
    # A "+" tab's table: its query.
    run("tabs_new_query", name="north", sql="SELECT store FROM sales WHERE store = 'North'")
    assert 'north = duckdb.sql("""\nSELECT store FROM sales' in table(live, "north")["loaded"]
    # A cell that cannot make its table says what to do, in one line.
    with pytest.raises(Exception, match="missing is not in this Python yet"):
        table_cells.run("missing", 'import duckdb\nmissing = duckdb.sql("SELECT * FROM missing").df()', {})


def test_a_document_is_arranged_through_the_door_and_by_the_agent(live: Live) -> None:
    sag.push(ORDERS, name="orders")
    cells = [
        {"kind": "code", "source": "import pandas as pd", "number": 1},
        {"kind": "code", "source": "orders = pd.DataFrame({'amount': [1]})", "number": 2},
        {"kind": "code", "source": "orders.describe()", "number": 3},
    ]
    body = {"kind": "notebook", "cells": cells, "tables": ["orders"], "next_cell": 4}
    done = live.user("POST", "/workbench/tables/arrange", body)
    assert [(c["number"], c["role"], c["grouped"]) for c in done["cells"]] == [
        (4, "tables", True),
        (1, "imports", True),
        (5, "table", True),
        (2, "maker", True),
        (6, "transformations", True),
        (3, "", False),
    ]
    assert done["next_cell"] == 7 and done["cells"][-1]["uses"] == "# uses orders @cell6"
    assert done["cells"][4]["steps"] == [] and done["tables"][0]["cell"] == 6
    # The agent does the same with its own command, and a script's text.
    script = run("workbench_tables_arrange", kind="script", text="# %%\norders = make()\n", tables=["orders"])
    assert script["text"].startswith("# region Tables\n# %% [1] [table] orders\norders = make()\n")
    assert script["outline"]["region"] == [1, 6] and script["tables"][0]["moved"] is True
    assert [t["name"] for t in run("workbench_table_cells")["tables"]] == ["orders"]
    with pytest.raises(Exception, match="400"):
        live.user("POST", "/workbench/tables/arrange", {"kind": "book"})


def test_the_watcher_never_sends_a_tables_own_result_back(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[Any, bool]] = []

    def fake_push(data: Any, name: str | None = None, **more: Any) -> str:
        sent.append((data, bool(more.get("_follow"))))
        return "http://127.0.0.1/g/1"

    monkeypatch.setattr(push_client, "push", fake_push)
    namespace: dict[str, Any] = {"orders": ORDERS}
    watching = watcher.Watcher(lambda: namespace)
    watching.load("orders", follow=False)  # the user opens the table
    assert [s[0] is ORDERS for s in sent] == [True]
    code = 'import duckdb\norders = duckdb.sql("SELECT * FROM orders WHERE amount > 20").df()'
    table_cells.run("orders", code, namespace)
    transformed = namespace["orders"]
    assert len(transformed) == 3
    # The scanner sees the variable changed: it is the cell's own result, so nothing is sent.
    watching.load("orders", follow=True)
    assert len(sent) == 1
    # Opened again from the Open list after the cell ran: the grid gets the original.
    watching.load("orders", follow=False)
    assert sent[-1][0] is ORDERS
    # The user's own code assigns the name: that is new data, and it is sent.
    namespace["orders"] = ORDERS.head(2)
    watching.load("orders", follow=True)
    assert len(sent) == 3 and len(sent[-1][0]) == 2 and sent[-1][1] is True
    # The cell now starts from the new original.
    table_cells.run("orders", code, namespace)
    assert len(namespace["orders"]) == 0
    table_cells.run("orders", "# orders: no transformations yet.", namespace)
    assert len(namespace["orders"]) == 2  # no steps: the table as its maker made it


def test_in_a_real_kernel_the_cell_runs_twice_and_the_grid_is_not_sent_its_result(
    live: Live, tmp_path: Path
) -> None:
    kernel = live.user("POST", "/workbench/kernels", {"path": str(tmp_path / "report.ipynb")})["kernel"]
    execute = f"/workbench/kernels/{kernel}/execute"

    def said(code: str) -> str:
        return "".join(str(o.get("text", "")) for o in lines(live, execute, {"code": code}))

    try:
        maker = "import pandas as pd\norders = pd.DataFrame({'id': [1, 2, 3, 4], 'amount': [10, 20, 30, 40]})"
        assert lines(live, execute, {"code": maker})[-1]["done"]["ok"] is True
        time.sleep(1.5)  # the kernel's watcher says it is there
        opened = live.user("POST", f"/workbench/kernels/{kernel}/grid", {"name": "orders"})
        assert opened["dataset"] == "orders"
        listed = table(live, "orders")
        assert listed["kernel"] == kernel  # the table belongs to this kernel's document
        grid = listed["grid"]
        steps = [{"kind": "filter", "params": {"column": "amount", "op": "gt", "value": 15}}]
        live.user("POST", f"/grids/{grid}/steps", {"steps": steps})
        code = table(live, "orders")["code"]
        version = live.user("GET", "/datasets")["datasets"][0]["version"]
        for _ in range(2):
            done = lines(live, execute, {"kind": "table", "table": "orders", "code": code})
            assert done[-1]["done"]["ok"] is True, done
            assert said("print(len(orders), type(orders).__name__)") == "3 DataFrame\n"
        time.sleep(2.5)  # several scans of the watcher
        # The cell's result was not sent back: the grid still holds the four rows and shows three.
        assert live.user("GET", "/datasets")["datasets"][0]["version"] == version
        assert len(grid_rows(live, grid)) == 3
        # The maker runs again: new data, sent to the grid as before.
        again = "orders = pd.DataFrame({'id': [1, 2], 'amount': [5, 50]})"
        lines(live, execute, {"code": again})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and len(grid_rows(live, grid)) != 1:
            time.sleep(0.3)
        assert grid_rows(live, grid) == [[2, 50]] or [r[:2] for r in grid_rows(live, grid)] == [[2, 50]]
        assert live.user("GET", "/datasets")["datasets"][0]["version"] > version
        lines(live, execute, {"kind": "table", "table": "orders", "code": code})
        assert said("print(len(orders))") == "1\n"
        # The agent runs a table's cell with its own command.
        ran = run("workbench_run", kernel=kernel, kind="table", table="orders", code=code)
        assert '"ok": true' in str(ran)
        # Run file: the script's own table cell runs the same safe way, and the grid gets what the
        # maker made (three rows), never the transformed table.
        script = tmp_path / "report.py"
        script.write_text(
            "import pandas as pd\n"
            "# region Tables\n"
            "# %% [1] [table] orders\n"
            "orders = pd.DataFrame({'id': [7, 8, 9], 'amount': [1, 60, 70]})\n"
            "# %% [2] [transformations] orders\n" + code + "\n"
            "# endregion\n"
            "# %%\n"
            "print('rows', len(orders))\n",
            encoding="utf-8",
        )
        ran_file = lines(live, execute, {"kind": "file", "path": str(script)})
        assert ran_file[-1]["done"]["ok"] is True, ran_file
        assert "rows 2\n" in "".join(str(o.get("text", "")) for o in ran_file)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and [r[:2] for r in grid_rows(live, grid)] != [[8, 60], [9, 70]]:
            time.sleep(0.3)
        assert [r[:2] for r in grid_rows(live, grid)] == [[8, 60], [9, 70]]
        assert live.user("GET", "/datasets")["datasets"][0]["rows"] == 3
        # A table's cell needs its table's name.
        with pytest.raises(Exception, match="400"):
            lines(live, execute, {"kind": "table", "table": "not a name", "code": code})
    finally:
        live.user("DELETE", f"/workbench/kernels/{kernel}")


def test_the_maker_run_again_just_before_the_cell_still_reaches_the_grid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[Any] = []

    def fake_push(data: Any, name: str | None = None, **more: Any) -> str:
        sent.append(data)
        return "url"

    monkeypatch.setattr(push_client, "push", fake_push)
    namespace: dict[str, Any] = {"orders": ORDERS}
    watching = watcher.Watcher(lambda: namespace)
    watching.load("orders", follow=False)
    code = 'import duckdb\norders = duckdb.sql("SELECT * FROM orders WHERE amount > 20").df()'
    # The maker and the cell run before the watcher looks (Run all, a script run as a file).
    fresh = ORDERS.head(5)
    namespace["orders"] = fresh
    table_cells.run("orders", code, namespace)
    watching.load("orders", follow=True)
    assert len(sent) == 2 and sent[-1] is fresh  # the new original, never the transformed result
    watching.load("orders", follow=True)
    assert len(sent) == 2


def test_a_script_run_as_a_file_runs_its_table_cells_safely() -> None:
    script = (
        "# region Tables\n"
        "# %% [1] [table] orders\n"
        "orders = load()\n"
        "# %% [2] [transformations] orders\n"
        "import duckdb\n"
        "\n"
        'orders = duckdb.sql("""\nSELECT * FROM orders WHERE amount > 20\n""").df()\n'
        "# endregion\n"
        "# %%\n"
        "print(len(orders))\n"
    )
    safe = table_cells.safe_source(script)
    assert len(safe.split("\n")) == len(script.split("\n"))  # an error still names the right line
    assert safe.split("\n")[4].startswith(
        "__import__('swissarmygrid.client.table_cells', fromlist=['run']).run('orders', "
    )
    assert safe.split("\n")[:4] == script.split("\n")[:4] and safe.endswith("# %%\nprint(len(orders))\n")
    namespace: dict[str, Any] = {"load": lambda: ORDERS}
    for _ in range(2):  # the file runs twice: each run transforms what the maker made, once
        exec(compile(safe, "report.py", "exec"), namespace)  # noqa: S102 (the made-up script above)
        assert len(namespace["orders"]) == 3
    assert table_cells.safe_source("x = 1\n# %% [3]\ny = 2\n") == "x = 1\n# %% [3]\ny = 2\n"
