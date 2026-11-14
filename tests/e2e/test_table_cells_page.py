# ruff: noqa: F811
"""Every table has a read-only cell (docs/ideas.md, 2026-11-14), in a real browser: the Tables group in a
notebook and in a script, a cell that follows the grid and turns yellow, cell numbers and @cell links,
the uses line kept as the user types. Made-up data only."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from playwright.sync_api import Browser, Page, expect

from swissarmygrid.domain import notebooks
from tests.e2e.test_ui import browser, call, server  # noqa: F401

SALES = "sales = pd.DataFrame({'store': ['North', 'South', 'East', 'West'], 'amount': [10, 20, 30, 40]})"
OVER_15 = [{"kind": "filter", "params": {"column": "amount", "op": "gt", "value": 15}}]
OVER_25 = [{"kind": "filter", "params": {"column": "amount", "op": "gt", "value": 25}}]


def open_workbench(browser: Browser, server: dict[str, Any], path: Path | None = None) -> Page:
    """The Workbench window by its address (with a file to open), in a browser of its own."""
    page = browser.new_context(viewport={"width": 1500, "height": 950}).new_page()
    opened = f"&open={quote(str(path))}" if path else ""
    page.goto(f"http://127.0.0.1:{server['port']}/workbench#token={server['token']}{opened}")
    expect(page.locator(".wb-tabs .tab-item" if path else ".wb-welcome").first).to_be_visible(timeout=20000)
    return page


def window(server: dict[str, Any], command: str, **args: Any) -> Any:
    """A command of the Workbench window, as the agent sends it."""
    return call(server, "POST", "/workbench/window", {"command": command, "args": args}, agent=True)["result"]


def kernel_of(server: dict[str, Any]) -> str:
    front = next(d for d in window(server, "documents")["documents"] if d["in_front"])
    return str(front["kernel"]["id"])


def open_as_grid(server: dict[str, Any], name: str) -> str:
    """Open a kernel's table variable as a grid (the Variables pane's Open as grid); its grid."""
    call(server, "POST", f"/workbench/kernels/{kernel_of(server)}/grid", {"name": name})
    return str(
        next(t["grid"] for t in call(server, "GET", "/workbench/tables")["tables"] if t["name"] == name)
    )


def close_tables(server: dict[str, Any]) -> None:
    for dataset in call(server, "GET", "/datasets")["datasets"]:
        call(server, "DELETE", f"/datasets/{dataset['name']}")


def type_cell(workbench: Page, index: int, text: str) -> None:
    """Put `text` in the user's cell at `index` (the app's own cells are not counted)."""
    editor = workbench.locator(".wb-cell").nth(index).locator(".monaco-editor")
    expect(editor).to_be_visible(timeout=20000)
    editor.click()
    workbench.keyboard.insert_text(text)


def source_of(server: dict[str, Any], number: int) -> str:
    cells = window(server, "doc.read")["cells"]
    return str(next(c["source"] for c in cells if c["number"] == number))


def test_a_notebook_table_has_a_read_only_cell_that_follows_the_grid(
    browser: Browser, server: dict[str, Any]
) -> None:
    close_tables(server)
    workbench = open_workbench(browser, server)
    workbench.get_by_role("button", name="New notebook").first.click()
    type_cell(workbench, 0, "import pandas as pd")
    workbench.keyboard.press("Shift+Enter")
    type_cell(workbench, 1, SALES)
    workbench.keyboard.press("Shift+Enter")
    type_cell(workbench, 2, "total = sales.amount.sum()")
    workbench.keyboard.press("Control+Enter")
    expect(workbench.locator(".wb-cell").nth(2).locator(".wb-count")).to_have_text("[3]", timeout=60000)
    # Every cell has its own number, apart from the run count.
    expect(workbench.locator(".wb-cell .wb-number")).to_have_text(["#1", "#2", "#3"])
    grid = open_as_grid(server, "sales")

    # The table has its cell at once, in a Tables group at the top.
    group = workbench.locator(".wb-tcell.role-tables")
    expect(group).to_have_text(re.compile(r"Tables · 1 table · up to date"), timeout=20000)
    line = workbench.locator(".wb-tcell.role-table")
    expect(line).to_contain_text("sales · 0 steps · no steps yet")
    cell = workbench.locator(".wb-tcell.role-transformations")
    maker = workbench.locator(".wb-cell.role-maker")
    # Made while working, the table shows open (the user's cell is not folded away under them); it
    # folds to its one line.
    expect(cell).to_be_visible()
    expect(maker).to_be_visible()
    line.locator(".wb-fold").click()
    expect(cell).to_be_hidden()
    expect(maker).to_be_hidden()
    # The imports the tables need come first; the user's other cell stays below, with its uses line.
    expect(workbench.locator(".wb-cell.role-imports")).to_contain_text("imports the tables need")
    order = workbench.locator(".wb-cells > section").evaluate_all(
        "els => els.map(e => e.className.split(' ')[0])"
    )
    assert order == ["wb-tcell", "wb-cell", "wb-tcell", "wb-cell", "wb-tcell", "wb-cell"]
    number = window(server, "tables.list")["tables"][0]["cell"]
    expect(workbench.locator(".wb-cell").last.locator(".view-lines")).to_contain_text(
        f"# uses sales @cell{number}"
    )
    assert source_of(server, 3) == f"# uses sales @cell{number}\ntotal = sales.amount.sum()"

    # Open the table: its maker (still the user's) and its read-only transformations.
    line.locator(".wb-fold").click()
    expect(maker).to_be_visible()
    expect(maker).to_contain_text("makes sales")
    expect(cell).to_contain_text("read-only: it follows the grid")
    expect(cell.locator(".wb-table-state")).to_have_text("No steps yet")
    # A step made in the grid rewrites the cell at once; nothing runs by itself.
    call(server, "POST", f"/grids/{grid}/steps", {"steps": OVER_15})
    expect(cell.locator(".view-lines")).to_contain_text('WHERE "amount" > 15', timeout=20000)
    expect(cell.locator(".view-lines")).to_contain_text('sales = duckdb.sql("""')
    expect(cell.locator(".wb-table-state")).to_have_text("Not run yet")
    expect(line).to_contain_text("sales · 1 step · not run yet")
    # The cell is never typed in.
    cell.locator(".monaco-editor").click()
    workbench.keyboard.type("x")
    assert "x" not in source_of(server, number).split("\n")[0]
    assert source_of(server, number).startswith('import duckdb\n\nsales = duckdb.sql("""')

    # Run: up to date, with the time. Twice gives the same table (it starts from what the maker made).
    line.get_by_role("button", name="Run", exact=True).click()
    expect(cell.locator(".wb-table-state")).to_have_text(
        re.compile(r"^Up to date · last run \d\d:\d\d$"), timeout=60000
    )
    cell.get_by_role("button", name="Run cell").click()
    expect(cell.locator(".wb-count")).to_have_text("[5]", timeout=60000)
    assert window(server, "console.run", code="print('rows', len(sales))") is not None
    assert "rows 3" in window(server, "console.read")
    # The grid still holds the four rows the maker made, and shows three: the result was not sent back.
    assert next(d["rows"] for d in call(server, "GET", "/datasets")["datasets"] if d["name"] == "sales") == 4

    # A grid change: yellow, on the cell, the table's line and the group's line.
    call(server, "POST", f"/grids/{grid}/steps", {"steps": OVER_25})
    expect(cell.locator(".wb-table-state")).to_have_text(
        re.compile(r"^Out of date · last run \d\d:\d\d$"), timeout=20000
    )
    expect(cell).to_have_class(re.compile(r"\bstale\b"))
    expect(line).to_have_class(re.compile(r"\bstale\b"))
    expect(group).to_have_text(re.compile(r"Tables · 1 table · 1 out of date"))
    expect(group).to_have_class(re.compile(r"\bstale\b"))
    expect(workbench.get_by_role("button", name="Run tables: 1 out of date")).to_be_visible()
    # Undoing the change in the grid clears the yellow by itself: the text is what last ran.
    call(server, "POST", f"/grids/{grid}/steps", {"steps": OVER_15})
    expect(cell.locator(".wb-table-state")).to_have_text(re.compile(r"^Up to date"), timeout=20000)
    expect(group).not_to_have_class(re.compile(r"\bstale\b"))

    # The group folds to one line; the agent reads the tables' state and runs a table's cell.
    group.locator(".wb-fold").click()
    expect(line).to_be_hidden()
    call(server, "POST", f"/grids/{grid}/steps", {"steps": OVER_25})
    expect(group).to_have_text(re.compile(r"1 out of date"), timeout=20000)
    listed = window(server, "tables.list")["tables"][0]
    assert (listed["name"], listed["state"], listed["steps"], listed["maker"]) == ("sales", "outOfDate", 1, 2)
    assert window(server, "tables.run", table="sales") == {"table": "sales", "ok": True}
    assert window(server, "tables.list")["tables"][0]["state"] == "upToDate"
    close_tables(server)


def test_the_uses_line_follows_the_typing_and_a_cell_link_goes_to_its_cell(
    browser: Browser, server: dict[str, Any]
) -> None:
    close_tables(server)
    workbench = open_workbench(browser, server)
    workbench.get_by_role("button", name="New notebook").first.click()
    type_cell(workbench, 0, "import pandas as pd\n" + SALES)
    workbench.keyboard.press("Shift+Enter")
    expect(workbench.locator(".wb-cell").first.locator(".wb-count")).to_have_text("[1]", timeout=60000)
    open_as_grid(server, "sales")
    expect(workbench.locator(".wb-tcell.role-tables")).to_be_visible(timeout=20000)
    number = window(server, "tables.list")["tables"][0]["cell"]
    # Type a line that names the table: after a short pause the app's line is above it, and the
    # cursor is where it was (the next keys land at the end of what was typed).
    user = workbench.locator(".wb-cell").last
    user.locator(".monaco-editor").click()
    workbench.keyboard.type("# sales in a comment and 'sales' in a string do not count")
    workbench.wait_for_timeout(1200)
    assert source_of(server, 2) == "# sales in a comment and 'sales' in a string do not count"
    workbench.keyboard.press("Enter")
    workbench.keyboard.type("big = sales")
    expect(user.locator(".view-lines")).to_contain_text(f"# uses sales @cell{number}", timeout=10000)
    workbench.keyboard.type(".head(2)")
    workbench.wait_for_timeout(1200)
    assert source_of(server, 2).split("\n") == [
        f"# uses sales @cell{number}",
        "# sales in a comment and 'sales' in a string do not count",
        "big = sales.head(2)",
    ]
    # A longer name is another name: the line goes when the cell names no table any more.
    for _ in range(len(".head(2)")):
        workbench.keyboard.press("Backspace")
    workbench.keyboard.type("_2026")
    workbench.wait_for_timeout(1500)
    assert source_of(server, 2).split("\n") == [
        "# sales in a comment and 'sales' in a string do not count",
        "big = sales_2026",
    ]
    # Undo takes the typing back; the app does not answer an undo with an edit of its own.
    for _ in range(6):
        workbench.keyboard.press("Control+z")
    workbench.wait_for_timeout(1500)
    assert "@cell" not in source_of(server, 2) and "# uses" not in source_of(server, 2)
    workbench.keyboard.press("Control+a")
    workbench.keyboard.type("report = sales")
    expect(user.locator(".view-lines")).to_contain_text(f"# uses sales @cell{number}", timeout=10000)

    # The link: a hover shows the cell's first line, a click goes there (opening what hides it).
    link = user.locator(".wb-cell-link").first
    expect(link).to_have_text(f"@cell{number}")
    link.hover()
    hover = workbench.locator(".monaco-hover", has_text=f"Cell {number}")
    expect(hover).to_be_visible(timeout=10000)
    expect(hover).to_contain_text("# sales: no transformations yet.")  # the cell's first line
    link.click()
    table_cell = workbench.locator(".wb-tcell.role-transformations")
    expect(table_cell).to_be_visible(timeout=10000)
    expect(table_cell).to_have_class(re.compile(r"\bselected\b"))
    # The agent goes to a cell by its number too.
    assert window(server, "cell.goTo", number=2) == {"cell": 2}
    expect(user).to_have_class(re.compile(r"\bselected\b"))
    # A SQL cell gets the line in SQL's own comment.
    workbench.get_by_role("button", name="SQL", exact=True).click()
    sql = workbench.locator(".wb-cell").last
    expect(sql.locator(".monaco-editor")).to_be_visible(timeout=20000)
    sql.locator(".monaco-editor").click()
    workbench.keyboard.insert_text("SELECT * FROM sales")
    expect(sql.locator(".view-lines")).to_contain_text(f"-- uses sales @cell{number}", timeout=10000)
    close_tables(server)


def test_a_maker_that_needs_something_further_down_stays_put_with_a_link(
    browser: Browser, server: dict[str, Any]
) -> None:
    close_tables(server)
    workbench = open_workbench(browser, server)
    workbench.get_by_role("button", name="New notebook").first.click()
    type_cell(
        workbench,
        0,
        "import pandas as pd\n\ndef add_tax(frame):\n    return frame.assign(tax=frame.amount * 0.2)",
    )
    workbench.keyboard.press("Shift+Enter")
    type_cell(workbench, 1, SALES.replace("sales = ", "sales = add_tax(") + ")")
    workbench.keyboard.press("Control+Enter")
    expect(workbench.locator(".wb-cell").nth(1).locator(".wb-count")).to_have_text("[2]", timeout=60000)
    open_as_grid(server, "sales")
    line = workbench.locator(".wb-tcell.role-table")
    expect(line).to_contain_text("sales is made at @cell2 (it needs add_tax and pd)", timeout=20000)
    # Its cells stay where they are, together: the maker, then its transformations.
    kinds = "l => l.map(e => e.classList.contains('role-transformations') ? 'table cell' : e.classList[0])"
    order = workbench.locator(".wb-cells > section").evaluate_all(kinds)
    assert order == ["wb-tcell", "wb-tcell", "wb-cell", "wb-cell", "table cell"]
    expect(workbench.locator(".wb-tcell.role-transformations")).to_be_visible()
    line.locator(".wb-cell-link").click()
    expect(workbench.locator(".wb-cell").nth(1)).to_have_class(re.compile(r"\bselected\b"))
    close_tables(server)


def saved_notebook(path: Path) -> None:
    cells = [
        notebooks.Cell("markdown", "# Tables", number=4, role="tables", grouped=True),
        notebooks.Cell("code", "import pandas as pd", number=1, role="imports", grouped=True),
        notebooks.Cell("markdown", "## sales", number=5, role="table", table="sales", grouped=True),
        notebooks.Cell("code", SALES, number=2, role="maker", table="sales", grouped=True),
        notebooks.Cell(
            "code",
            # As the app writes it for this step (so the cell is up to date once the grid has the step).
            'import duckdb\n\nsales = duckdb.sql("""\n-- 1. Keep rows where amount is greater than 15\n'
            'SELECT *\nFROM sales\nWHERE "amount" > 15\n""").df()',
            number=6,
            role="transformations",
            table="sales",
            grouped=True,
            steps=OVER_15,
        ),
        notebooks.Cell("code", "# uses sales @cell6\nprint('rows', len(sales))", number=3),
    ]
    path.write_text(notebooks.write_notebook(cells, next_number=9), encoding="utf-8")


def test_a_saved_notebook_opens_folded_runs_top_to_bottom_and_keeps_its_numbers(
    browser: Browser, server: dict[str, Any], tmp_path: Path
) -> None:
    close_tables(server)
    book = tmp_path / "sales.ipynb"
    saved_notebook(book)
    workbench = open_workbench(browser, server, book)
    # It starts folded to one line, and says its table has not run since the file opened.
    group = workbench.locator(".wb-tcell.role-tables")
    expect(group).to_have_text(re.compile(r"Tables · 1 table · 1 not run"), timeout=20000)
    expect(workbench.locator(".wb-tcell.role-table")).to_be_hidden()
    expect(workbench.locator(".wb-cells > section:visible")).to_have_count(
        2
    )  # the group's line, the user's cell
    group.locator(".wb-fold").click()
    line = workbench.locator(".wb-tcell.role-table")
    expect(line).to_contain_text("sales · not open in the grid · not run since opening")
    line.locator(".wb-fold").click()
    cell = workbench.locator(".wb-tcell.role-transformations")
    expect(cell.locator(".wb-table-state")).to_have_text("Not run since opening")
    # Run all: the maker makes the table, its cell transforms it once.
    workbench.get_by_role("button", name="Run all").click()
    expect(workbench.locator(".wb-cell").last.locator(".wb-stream")).to_have_text("rows 3\n", timeout=60000)
    expect(cell.locator(".wb-table-state")).to_have_text(re.compile(r"^Up to date"))
    # Opened as a grid again, the grid gets the file's steps back and the cell stays as it is.
    grid = open_as_grid(server, "sales")
    expect(line).to_contain_text("sales · 1 step · up to date", timeout=20000)
    assert [s["kind"] for s in call(server, "GET", f"/grids/{grid}")["steps"]] == ["filter"]
    assert call(server, "GET", f"/grids/{grid}/count")["row_count"] == 3
    # A new cell gets a number never used before (the file says 9 comes next), and saving keeps them.
    workbench.locator(".wb-cell").last.click()
    workbench.get_by_role("button", name="Python", exact=True).click()
    expect(workbench.locator(".wb-cell").last.locator(".wb-number")).to_have_text("#9")
    workbench.keyboard.press("Control+s")
    expect(workbench.locator(".toast", has_text="Saved")).to_be_visible(timeout=20000)
    saved = json.loads(book.read_text(encoding="utf-8"))
    marks = [c["metadata"].get("swissarmygrid", {}) for c in saved["cells"]]
    assert [m.get("number") for m in marks] == [4, 1, 5, 2, 6, 3, 9]
    assert [m.get("role", "") for m in marks] == [
        "tables",
        "imports",
        "table",
        "maker",
        "transformations",
        "",
        "",
    ]
    assert saved["metadata"]["swissarmygrid"]["next_cell"] == 10
    # In the file the group is Markdown headings, which JupyterLab folds too.
    assert [c["cell_type"] for c in saved["cells"][:3]] == ["markdown", "code", "markdown"]
    assert (
        "".join(saved["cells"][0]["source"]) == "# Tables"
        and "".join(saved["cells"][2]["source"]) == "## sales"
    )
    close_tables(server)


SCRIPT = '''"""A made-up report."""
print("start")
# %% the imports
import pandas as pd
# %% Load the sales
sales = pd.DataFrame({"store": ["North", "South", "East", "West"], "amount": [10, 20, 30, 40]})
# %% the report
print("rows", len(sales))
'''


def script_text(server: dict[str, Any]) -> str:
    return str(window(server, "doc.read")["text"])


def test_a_scripts_tables_are_a_region_of_cells_the_editor_folds_and_draws(
    browser: Browser, server: dict[str, Any], tmp_path: Path
) -> None:
    close_tables(server)
    script = tmp_path / "report.py"
    script.write_text(SCRIPT, encoding="utf-8")
    workbench = open_workbench(browser, server, script)
    editor = workbench.locator(".wb-script-editor")
    expect(editor.locator(".monaco-editor")).to_be_visible(timeout=20000)
    # Each cell has its number on its marker, given without counting as a change to the file.
    expect(editor.locator(".view-lines")).to_contain_text("# %% [2] Load the sales", timeout=20000)
    expect(workbench.locator(".wb-tabs .tab-item")).not_to_contain_text("●")
    workbench.get_by_role("button", name="Run file").click()
    log = workbench.locator(".wb-console-log")
    expect(log).to_contain_text("rows 4", timeout=60000)
    grid = open_as_grid(server, "sales")
    expect(editor.locator(".view-lines")).to_contain_text("# region Tables", timeout=20000)
    assert script_text(server) == (
        "# region Tables\n"
        "# %% [1] the imports\n"
        "import pandas as pd\n"
        "# %% [2] [table] sales Load the sales\n"
        'sales = pd.DataFrame({"store": ["North", "South", "East", "West"], "amount": [10, 20, 30, 40]})\n'
        "# %% [4] [transformations] sales\n"
        "# sales: no transformations yet. Steps made in the grid appear here.\n"
        "# endregion\n"
        "# %%\n"
        '"""A made-up report."""\n'
        'print("start")\n'
        "# %% [3] the report\n"
        "# uses sales @cell4\n"
        'print("rows", len(sales))\n'
    )
    # A step in the grid: the cell's text follows; the editor says its state beside its marker.
    call(server, "POST", f"/grids/{grid}/steps", {"steps": OVER_15})
    expect(editor.locator(".view-lines")).to_contain_text('WHERE "amount" > 15', timeout=20000)
    lines = editor.locator(".view-lines")  # with the words the editor draws after the markers
    expect(lines).to_contain_text("read-only: it follows the grid · Not run yet")
    expect(lines).to_contain_text("# region Tables 1 table · 1 not run")
    # Ctrl+Enter in the table's cell runs it (the cell is read-only: typing changes nothing).
    line = script_text(server).split("\n").index("# %% [4] [transformations] sales") + 3
    window(server, "cell.goTo", number=4)
    for _ in range(3):
        workbench.keyboard.press("ArrowDown")
    before = script_text(server)
    workbench.keyboard.type("x")
    assert script_text(server) == before and line > 0
    workbench.keyboard.press("Control+Enter")
    # (The editor draws these words with spaces that do not break: \\s matches them.)
    expect(lines).to_contain_text(
        re.compile(r"grid\s·\sUp\sto\sdate\s·\slast\srun\s\d\d:\d\d"), timeout=60000
    )
    # A grid change turns it yellow; the toolbar offers the run; the agent's command runs it.
    call(server, "POST", f"/grids/{grid}/steps", {"steps": OVER_25})
    expect(lines).to_contain_text("follows the grid · Out of date · last run", timeout=20000)
    expect(lines).to_contain_text("# region Tables 1 table · 1 out of date")
    expect(editor.locator(".wb-line-note.stale").first).to_be_visible()  # yellow
    workbench.get_by_role("button", name="Run tables: 1 out of date").click()
    expect(lines).to_contain_text("# region Tables 1 table · up to date", timeout=60000)
    expect(editor.locator(".wb-line-note.stale")).to_have_count(0)
    # Run file runs the table's cell the safe way too: the kernel holds two rows, the grid still holds
    # the four the maker made (a transformed table is never sent to the grid as new data).
    workbench.locator(".wb-console-head").get_by_role("button", name="Clear").click()
    workbench.get_by_role("button", name="Run file").click()
    expect(log).to_contain_text("rows 2", timeout=60000)
    workbench.wait_for_timeout(2000)
    assert next(d["rows"] for d in call(server, "GET", "/datasets")["datasets"] if d["name"] == "sales") == 4
    assert call(server, "GET", f"/grids/{grid}/count")["row_count"] == 2
    # The group folds to one line, and a link in a comment goes to its cell.
    window(server, "tables.fold")
    expect(editor.locator(".view-lines")).not_to_contain_text("import pandas as pd")
    expect(editor.locator(".view-lines")).to_contain_text("# region Tables")
    editor.locator(".wb-cell-link", has_text="@cell4").click()
    expect(editor.locator(".view-lines")).to_contain_text("# %% [4] [transformations] sales")  # opened again
    expect(workbench.locator(".wb-status")).to_contain_text(f"Line {line - 2},")
    # Saved, the file runs as plain Python with no app, and opens folded the next time.
    workbench.keyboard.press("Control+s")
    expect(workbench.locator(".toast", has_text="Saved")).to_be_visible(timeout=20000)
    namespace: dict[str, Any] = {}
    exec(compile(script.read_text(encoding="utf-8"), str(script), "exec"), namespace)  # noqa: S102 (the made-up script)
    assert len(namespace["sales"]) == 2
    workbench.reload()
    again = workbench.locator(".wb-script-editor")
    expect(again.locator(".view-lines")).to_contain_text("# region Tables", timeout=20000)
    expect(again.locator(".view-lines")).not_to_contain_text("import pandas as pd", timeout=20000)
    expect(again.locator(".view-lines")).to_contain_text("# region Tables 1 table · 1 not run")
    close_tables(server)
