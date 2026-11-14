# ruff: noqa: F811
"""The Connections window and the Workbench in a real browser (Plans/workbench-and-connections.plan.md).

Made-up data only: a CSV of three orders in the test's own folder. No test saves a password: on Windows
that would reach the real Credential Manager.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from playwright.sync_api import Browser, Page, expect

# The grid tests' fixtures (browser, server, orders_url), imported by name.
from tests.e2e.test_ui import browser, call, open_grid, orders_url, server  # noqa: F401


@pytest.fixture
def orders_csv(tmp_path: Path, server: dict[str, Any]) -> Iterator[Path]:
    csv = tmp_path / "shop" / "orders.csv"
    csv.parent.mkdir()
    csv.write_text("order_id,region,amount\n1,North,10.5\n2,South,7\n3,North,2.5\n", encoding="utf-8")
    yield csv
    for name in ("orders", "Orders"):
        try:
            call(server, "DELETE", f"/blades/{quote(name)}")
        except Exception:  # noqa: S110 (not saved by this test)
            pass


def test_a_connection_is_added_with_a_form_tested_and_saved(
    browser: Browser, orders_url: str, server: dict[str, Any], orders_csv: Path
) -> None:
    page = open_grid(browser, orders_url)
    page.locator(".app-row").get_by_role("button", name="Connections").click()
    page.get_by_role("button", name="Add connection").click()
    page.locator(".connection-type", has_text="CSV file").click()
    # Browse… lists the folder through the server: a real path, which a browser's own picker hides.
    page.get_by_role("button", name="Browse…").click()
    folder = page.locator(".file-chooser input[aria-label=Folder]")
    folder.fill(str(orders_csv.parent))
    folder.press("Enter")
    page.locator(".file-chooser .file-row.file", has_text="orders.csv").click()
    name = page.locator("dialog").get_by_role("textbox", name="Name", exact=True)
    expect(name).to_have_value("orders")  # named after the file
    page.get_by_role("button", name="Test connection").click()
    expect(page.locator(".connection-result")).to_contain_text("It works: a file of", timeout=30000)
    page.locator("dialog .dialog-foot").get_by_role("button", name="Save").click()
    expect(page.locator(".connection-list")).to_contain_text("orders")
    expect(page.locator(".connection-list")).to_contain_text('sag.connection("orders")')
    listed = call(server, "GET", "/connections")["connections"]
    assert [(c["name"], c["connection"]) for c in listed] == [("orders", "file")]


def open_workbench(page: Page) -> Page:
    with page.context.expect_page() as opened:
        page.locator(".tab-workbench").click()
    workbench = opened.value
    expect(workbench.locator(".wb-welcome")).to_be_visible(timeout=20000)
    return workbench


def test_the_workbench_runs_a_notebook_with_a_sql_cell_saves_it_and_reopens_it(
    browser: Browser, orders_url: str, server: dict[str, Any], orders_csv: Path, tmp_path: Path
) -> None:
    call(
        server,
        "POST",
        "/connections",
        {
            "name": "Orders",
            "kind": "other",
            "template": "csv",
            "code": f'from pathlib import Path\npath = Path(r"{orders_csv}")',
        },
    )
    workbench = open_workbench(open_grid(browser, orders_url))
    # The left panel lists the connection apart from the blades.
    expect(workbench.locator(".blade-item", has_text="Orders").locator(".blade-kind")).to_have_text("File")
    workbench.get_by_role("button", name="New notebook").first.click()
    editor = workbench.locator(".wb-cell .monaco-editor").first
    expect(editor).to_be_visible(timeout=20000)
    editor.click()
    workbench.keyboard.type("import pandas as pd\nprint('hello')\npd.DataFrame({'city': ['Springfield']})")
    workbench.keyboard.press("Shift+Enter")  # runs, and opens the next cell
    first = workbench.locator(".wb-cell").first
    expect(first.locator(".wb-stream")).to_have_text("hello\n", timeout=60000)
    expect(first.locator("iframe.wb-html")).to_be_visible()
    expect(first.locator(".wb-count")).to_have_text("[1]")
    # Every connection is there by its name: no sag.connection(...) line (opened when first used).
    workbench.get_by_role("button", name="Python", exact=True).click()
    named = workbench.locator(".wb-cell").nth(1)
    expect(named.locator(".monaco-editor")).to_be_visible(timeout=20000)
    named.locator(".monaco-editor").click()
    workbench.keyboard.type("print(pd.read_csv(orders)['amount'].sum())")
    workbench.keyboard.press("Control+Enter")
    expect(named.locator(".wb-stream")).to_have_text("20.0\n", timeout=60000)
    named.get_by_role("button", name="Delete cell").click()
    second = workbench.locator(".wb-cell").nth(1)
    second.get_by_label("Cell kind").select_option("sql")
    second.get_by_label("Connection").select_option("Orders")
    second.locator(".monaco-editor").click()
    workbench.keyboard.type("SELECT sum(amount) AS total FROM orders")
    workbench.keyboard.press("Control+Enter")
    frame = second.frame_locator("iframe.wb-html")
    expect(frame.locator("td").last).to_have_text("20.0", timeout=60000)
    expect(workbench.locator(".wb-status .wb-state")).to_contain_text("ready")
    # Save as… through the file chooser; the file is a notebook Jupyter reads.
    workbench.get_by_role("button", name="Save", exact=True).click()
    folder = workbench.locator(".file-chooser input[aria-label=Folder]")
    folder.fill(str(tmp_path))
    folder.press("Enter")
    workbench.get_by_label("File name").fill("orders.ipynb")
    workbench.get_by_role("button", name="Save here").click()
    expect(workbench.locator(".wb-tabs .tab-label")).to_have_text(["orders.ipynb"])
    saved = json.loads((tmp_path / "orders.ipynb").read_text(encoding="utf-8"))
    assert saved["nbformat"] == 4 and saved["cells"][1]["source"][0] == "%%sql Orders\n"
    # Closed and opened again from Recent: the cells and their outputs come back.
    workbench.locator(".wb-tabs").get_by_role("button", name="Close orders.ipynb").click()
    workbench.get_by_role("button", name="Recent").click()
    workbench.get_by_role("menuitem", name="orders.ipynb").click()
    expect(workbench.locator(".wb-cell")).to_have_count(2)
    expect(workbench.locator(".wb-cell").nth(1).get_by_label("Connection")).to_have_value("Orders")
    expect(workbench.locator(".wb-cell").first.locator(".wb-stream")).to_have_text("hello\n")


def new_script(workbench: Page, code: str) -> None:
    """A new script with `code` in its editor (typed as one insert, so the editor adds no indent)."""
    workbench.get_by_role("button", name="New script").first.click()
    editor = workbench.locator(".wb-script-editor .monaco-editor")
    expect(editor).to_be_visible(timeout=20000)
    editor.click()
    workbench.keyboard.insert_text(code)


def console_type(workbench: Page, text: str) -> None:
    workbench.locator(".wb-console-editor .monaco-editor").click()
    workbench.keyboard.insert_text(text)


def test_a_script_runs_in_its_console(browser: Browser, orders_url: str) -> None:
    workbench = open_workbench(open_grid(browser, orders_url))
    new_script(workbench, "total = sum(range(10))\nprint('total is', total)")
    workbench.get_by_role("button", name="Run file").click()
    log = workbench.locator(".wb-console-log")
    expect(log).to_contain_text("total is 45", timeout=60000)
    console_type(workbench, "total * 2")
    workbench.keyboard.press("Enter")
    expect(log).to_contain_text("90")
    workbench.keyboard.press("ArrowUp")
    expect(workbench.locator(".wb-console-editor .view-lines")).to_contain_text("total * 2")  # back again
    # Several lines: Enter after a block's first line adds a line; an empty line ends the block.
    workbench.keyboard.press("Control+a")
    workbench.keyboard.press("Delete")
    workbench.keyboard.insert_text("for i in range(2):")
    workbench.keyboard.press("Enter")
    workbench.keyboard.insert_text("print('row', i)")
    workbench.keyboard.press("Enter")
    expect(log).not_to_contain_text("row 1")
    workbench.keyboard.press("Enter")
    expect(log).to_contain_text("row 1")
    # F5 anywhere runs the file, never reloading the window (which would stop the kernel).
    workbench.locator(".wb-console-head").click()
    workbench.keyboard.press("F5")
    expect(log.locator(".wb-console-code", has_text="the script")).to_have_count(2)
    console_type(workbench, "total")
    workbench.keyboard.press("Enter")
    expect(log.locator(".wb-console-entry").last).to_contain_text("45")  # the same kernel, still there


def open_file(workbench: Page, path: Path) -> None:
    """Open a file as the window's address does (#open=...): its text exactly as written."""
    workbench.goto(workbench.url.split("#")[0] + "#open=" + quote(str(path)))
    workbench.reload()
    expect(workbench.locator(".wb-tabs .tab-item", has_text=path.name)).to_be_visible(timeout=20000)


def test_ctrl_enter_runs_the_whole_statement_then_the_next(
    browser: Browser, orders_url: str, tmp_path: Path
) -> None:
    script = tmp_path / "rates.py"
    script.write_text("for r in [1, 2]:\n    print('rate', r)\n\nprint('after')\n", encoding="utf-8")
    workbench = open_workbench(open_grid(browser, orders_url))
    open_file(workbench, script)
    workbench.locator(".wb-script-editor .monaco-editor").click()
    workbench.keyboard.press("Control+Home")
    workbench.keyboard.press("Control+Enter")
    log = workbench.locator(".wb-console-log")
    expect(log).to_contain_text("rate 2", timeout=60000)  # the whole for block, not its first line
    expect(log).not_to_contain_text("after")
    workbench.keyboard.press("Control+Enter")  # the cursor moved on to the next statement
    expect(log).to_contain_text("after")


def test_outputs_keep_up_with_many_prints_colours_and_progress_bars(
    browser: Browser, orders_url: str, tmp_path: Path
) -> None:
    script = tmp_path / "noisy.py"
    script.write_text(
        "import sys\n"
        "for i in range(6000):\n    print('line', i, flush=True)\n"
        "print('\\x1b[31mred\\x1b[0m plain')\n"
        "sys.stdout.write('10%\\r50%\\r100%\\n')\n",
        encoding="utf-8",
    )
    workbench = open_workbench(open_grid(browser, orders_url))
    open_file(workbench, script)
    workbench.get_by_role("button", name="Run file").click()
    log = workbench.locator(".wb-console-log")
    # The stress test froze the page for over 100 seconds at 5,000 prints; drawn as they come it keeps up.
    expect(log).to_contain_text("100%", timeout=30000)
    expect(log).to_contain_text("line 5999")
    expect(log.locator(".wb-cut")).to_contain_text("earlier lines are not shown")
    expect(log.locator(".ansi-fg-1")).to_have_text("red")
    expect(log).not_to_contain_text("50%")  # each \\r wrote over its line


def test_input_is_answered_in_place_and_a_dead_kernel_says_so(browser: Browser, orders_url: str) -> None:
    workbench = open_workbench(open_grid(browser, orders_url))
    new_script(workbench, "name = input('Who? ')\nprint('hello', name)\n")
    workbench.get_by_role("button", name="Run file").click()
    box = workbench.locator(".wb-input-box")
    expect(box).to_be_visible(timeout=60000)
    box.fill("Ana")
    box.press("Enter")
    log = workbench.locator(".wb-console-log")
    expect(log).to_contain_text("hello Ana")
    console_type(workbench, "import os; os._exit(1)")
    workbench.keyboard.press("Enter")
    expect(log.locator(".wb-notice.error")).to_contain_text("The kernel stopped", timeout=30000)


def test_a_deleted_cell_comes_back_and_open_documents_survive_a_reload(
    browser: Browser, orders_url: str
) -> None:
    workbench = open_workbench(open_grid(browser, orders_url))
    workbench.on("dialog", lambda dialog: dialog.accept())  # the unsaved-changes warning on reload
    workbench.get_by_role("button", name="New notebook").first.click()
    first = workbench.locator(".wb-cell .monaco-editor").first
    expect(first).to_be_visible(timeout=20000)
    first.click()
    workbench.keyboard.insert_text("kept = 'first cell'")
    workbench.get_by_role("button", name="Python", exact=True).click()
    expect(workbench.locator(".wb-cell")).to_have_count(2)
    workbench.locator(".wb-cell").first.get_by_role("button", name="Delete cell").click()
    expect(workbench.locator(".wb-cell")).to_have_count(1)
    workbench.locator(".toast").get_by_role("button", name="Undo").click()
    expect(workbench.locator(".wb-cell")).to_have_count(2)
    expect(workbench.locator(".wb-cell").first).to_contain_text("first cell")
    new_script(workbench, "draft = 'not saved yet'")
    workbench.wait_for_timeout(1200)  # remembered a moment after the last change
    workbench.reload()
    expect(workbench.locator(".wb-tabs .tab-item")).to_have_count(2, timeout=20000)
    expect(workbench.locator(".wb-script-editor .view-lines")).to_contain_text("not saved yet", timeout=20000)
    workbench.locator(".wb-tabs .tab-item").first.click()
    expect(workbench.locator(".wb-cell").first).to_contain_text("first cell")


def test_completion_comes_from_the_catalog_and_the_kernel(
    browser: Browser, orders_url: str, server: dict[str, Any], orders_csv: Path
) -> None:
    call(
        server,
        "POST",
        "/connections",
        {
            "name": "Orders",
            "kind": "other",
            "template": "csv",
            "code": f'from pathlib import Path\npath = Path(r"{orders_csv}")',
        },
    )
    catalog = {
        "columns": ["TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME", "DATA_TYPE"],
        "data": [
            ["sales", "orders", "order_id", "int"],
            ["sales", "orders", "region", "varchar"],
            ["sales", "orders", "amount", "decimal"],
        ],
    }
    request_catalog = call(server, "PUT", "/catalogs/Orders?dialect=duckdb", catalog)
    assert request_catalog["catalog"]["columns"] == 3
    try:
        workbench = open_workbench(open_grid(browser, orders_url))
        workbench.get_by_role("button", name="New notebook").first.click()
        cell = workbench.locator(".wb-cell").first
        expect(cell.locator(".monaco-editor")).to_be_visible(timeout=20000)
        # A SQL cell before any kernel runs: the connection's catalog answers.
        cell.get_by_label("Cell kind").select_option("sql")
        cell.get_by_label("Connection").select_option("Orders")
        cell.locator(".monaco-editor").click()
        workbench.keyboard.type("SELECT * FROM ord")
        workbench.keyboard.press("Control+Space")  # suggestions now (typing fast can outrun the widget)
        suggest = workbench.locator(".suggest-widget .monaco-list-row")
        expect(suggest.filter(has_text="orders").first).to_be_visible(timeout=10000)
        workbench.keyboard.press("Escape")
        workbench.keyboard.type("ers WHERE orders.reg")
        workbench.keyboard.press("Control+Space")
        expect(suggest.filter(has_text="region").first).to_be_visible(timeout=10000)
        workbench.keyboard.press("Escape")
        # A Python cell once the kernel runs: the kernel's own names, a DataFrame's columns.
        workbench.get_by_role("button", name="Python", exact=True).click()
        python = workbench.locator(".wb-cell").nth(1)
        expect(python.locator(".monaco-editor")).to_be_visible(timeout=20000)
        python.locator(".monaco-editor").click()
        workbench.keyboard.type("import pandas as pd\nrainfall = pd.DataFrame({'rain_mm': [3.5]})")
        workbench.keyboard.press("Control+Enter")
        expect(python.locator(".wb-count")).to_have_text("[1]", timeout=60000)
        workbench.keyboard.press("End")
        workbench.keyboard.press("Enter")
        workbench.keyboard.type("rainfall.rai")
        workbench.keyboard.press("Control+Space")
        expect(suggest.filter(has_text="rain_mm").first).to_be_visible(timeout=15000)
        workbench.keyboard.press("Escape")
        workbench.keyboard.press("Enter")
        workbench.keyboard.type('conn = sag.connection("Or')
        expect(suggest.filter(has_text="Orders").first).to_be_visible(timeout=10000)
    finally:
        call(server, "DELETE", "/catalogs/Orders")


def test_the_variables_pane_shows_what_the_kernel_holds(
    browser: Browser, orders_url: str, server: dict[str, Any], orders_csv: Path
) -> None:
    call(
        server,
        "POST",
        "/connections",
        {
            "name": "Orders",
            "kind": "other",
            "template": "csv",
            "code": f'from pathlib import Path\npath = Path(r"{orders_csv}")',
        },
    )
    workbench = open_workbench(open_grid(browser, orders_url))
    workbench.get_by_role("button", name="New notebook").first.click()
    cell = workbench.locator(".wb-cell").first
    expect(cell.locator(".monaco-editor")).to_be_visible(timeout=20000)
    cell.locator(".monaco-editor").click()
    frame = "{'city': ['Springfield', 'Riverton'], 'rain_mm': [3.5, 0.0]}"
    workbench.keyboard.type(f"import pandas as pd\nrainfall = pd.DataFrame({frame})")
    workbench.keyboard.press("Control+Enter")
    expect(cell.locator(".wb-count")).to_have_text("[1]", timeout=60000)
    overlay = workbench.get_by_role("region", name="Variables")  # docked top right, open from the start
    row = overlay.locator("tr", has_text="rainfall")
    expect(row).to_contain_text("2 rows by 2 columns", timeout=15000)
    expect(row).to_contain_text("columns: city, rain_mm")
    expect(overlay.locator("tr", has_text="Orders")).to_contain_text("opened when a cell first uses it")
    # A cell that uses the connection opens it; the overlay reads again when the run ends.
    cell.locator(".monaco-editor").click()
    workbench.keyboard.press("Control+End")
    workbench.keyboard.type("\nlen(pd.read_csv(orders))")
    workbench.keyboard.press("Control+Enter")
    expect(overlay.locator("tr", has_text="Orders")).to_contain_text("orders.csv", timeout=30000)
    # A table opens as a grid in the grid page.
    row.get_by_role("button", name="Open rainfall as a grid").click()
    expect(workbench.locator(".toast")).to_contain_text("rainfall is open as a grid", timeout=30000)
    assert any(d["name"] == "rainfall" for d in call(server, "GET", "/datasets")["datasets"])


def test_four_panes_a_palette_and_jupyters_keys(browser: Browser, orders_url: str) -> None:
    workbench = open_workbench(open_grid(browser, orders_url))
    workbench.get_by_role("button", name="New notebook").first.click()
    cell = workbench.locator(".wb-cell").first
    expect(cell.locator(".monaco-editor")).to_be_visible(timeout=20000)
    # RStudio's four places: source, its console, Variables, Plots and Help.
    for pane in (".wb-source .wb-cells", ".wb-console-pane .wb-console", ".wb-env .wb-vars", ".wb-lower"):
        expect(workbench.locator(pane)).to_be_visible()
    # The notebook's own console, on its kernel.
    workbench.locator(".wb-console-editor .monaco-editor").click()
    workbench.keyboard.insert_text("21 * 2")
    workbench.keyboard.press("Enter")
    expect(workbench.locator(".wb-console-log")).to_contain_text("42", timeout=60000)
    # The palette runs any command by its name.
    workbench.keyboard.press("Control+Shift+P")
    workbench.get_by_role("combobox", name="Command").fill("insert below")
    workbench.keyboard.press("Enter")
    expect(workbench.locator(".wb-cell")).to_have_count(2)
    # Command mode: Escape leaves the editor; B, D D, Z and M act on the cell.
    workbench.keyboard.press("Escape")
    workbench.keyboard.press("b")
    expect(workbench.locator(".wb-cell")).to_have_count(3)
    workbench.keyboard.press("Escape")
    workbench.keyboard.press("d")
    workbench.keyboard.press("d")
    expect(workbench.locator(".wb-cell")).to_have_count(2)
    workbench.keyboard.press("z")
    expect(workbench.locator(".wb-cell")).to_have_count(3)
    workbench.keyboard.press("m")
    expect(workbench.locator(".wb-cell.selected").get_by_label("Cell kind")).to_have_value("markdown")
    # A splitter moves by keyboard, and Focus shows the source alone.
    splitter = workbench.locator(".wb-split.vertical")
    before = int(splitter.get_attribute("aria-valuenow") or "0")
    splitter.focus()
    workbench.keyboard.press("ArrowLeft")
    expect(splitter).to_have_attribute("aria-valuenow", str(before - 2))
    workbench.keyboard.press("Control+Shift+1")
    expect(workbench.locator(".wb-col-right")).to_be_hidden()
    workbench.keyboard.press("Control+Shift+P")
    workbench.get_by_role("combobox", name="Command").fill("layout standard")
    workbench.keyboard.press("Enter")
    expect(workbench.locator(".wb-col-right")).to_be_visible()


def test_plots_gather_and_f1_shows_help(browser: Browser, orders_url: str, tmp_path: Path) -> None:
    script = tmp_path / "shapes.py"
    script.write_text(
        "from IPython.display import SVG, display\n"
        'display(SVG(\'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
        '<rect width="20" height="20" fill="teal"/></svg>\'))\n'
        "print(len('made up'))\n",
        encoding="utf-8",
    )
    workbench = open_workbench(open_grid(browser, orders_url))
    open_file(workbench, script)
    workbench.get_by_role("button", name="Run file").click()
    expect(workbench.locator(".wb-console-log")).to_contain_text("7", timeout=60000)
    expect(workbench.locator(".wb-plot-head")).to_contain_text("Plot 1 of 1")
    expect(workbench.locator(".wb-plot-body img")).to_be_visible()
    # F1 on a name: its help in the Help tab.
    workbench.locator(".wb-script-editor .monaco-editor").click()
    workbench.keyboard.press("Control+End")
    workbench.keyboard.press("ArrowUp")
    workbench.keyboard.press("End")
    for _ in range(14):
        workbench.keyboard.press("ArrowLeft")  # into "len"
    workbench.keyboard.press("F1")
    expect(workbench.locator(".wb-help-body")).to_contain_text("Return the number of items", timeout=30000)


def test_the_agent_works_the_window_as_the_user_does(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    workbench = open_workbench(open_grid(browser, orders_url))

    def window(command: str, **args: Any) -> Any:
        return call(server, "POST", "/workbench/window", {"command": command, "args": args}, agent=True)[
            "result"
        ]

    window("file.newNotebook")
    expect(workbench.locator(".wb-cell")).to_have_count(1, timeout=20000)
    window("cell.insertBelow", kind="code", source="answer = 6 * 7\nanswer")
    expect(workbench.locator(".wb-cell")).to_have_count(2)
    assert window("run.cell", index=1) is True
    expect(workbench.locator(".wb-cell").nth(1).locator(".wb-text")).to_have_text("42")
    opened = window("documents")
    assert (
        opened["documents"][0]["kind"] == "notebook" and opened["documents"][0]["kernel"]["state"] == "idle"
    )
    assert window("doc.read")["cells"][1]["source"] == "answer = 6 * 7\nanswer"
    ids = {c["id"] for c in window("commands")}
    assert {"run.statement", "cell.delete", "view.focus", "console.run"} <= ids
    window("console.run", code="answer + 1")
    expect(workbench.locator(".wb-console-log")).to_contain_text("43", timeout=30000)


def test_an_error_is_explained_marked_in_the_code_and_fixed(
    browser: Browser, orders_url: str, tmp_path: Path
) -> None:
    script = tmp_path / "totals.py"
    script.write_text(
        "import pandas as pd\n"
        "orders = pd.DataFrame({'region': ['North', 'South'], 'amount': [10.5, 7.0]})\n"
        "total = orders['amont'].sum()\n",
        encoding="utf-8",
    )
    workbench = open_workbench(open_grid(browser, orders_url))
    open_file(workbench, script)
    workbench.get_by_role("button", name="Run file").click()
    panel = workbench.locator(".wb-console-log .wb-err")
    expect(panel).to_contain_text("The table has no column 'amont'", timeout=60000)
    expect(panel).to_contain_text("In your code: totals.py, line 3")
    expect(panel.locator(".wb-err-lib").first).to_contain_text("in pandas")  # library frames folded
    # The failing line is marked in the script, its message at the line's end.
    expect(workbench.locator(".wb-script-editor .wb-problem-lens")).to_contain_text("KeyError")
    panel.get_by_role("button", name="Use 'amount'").click()
    expect(workbench.locator(".wb-script-editor .view-lines")).to_contain_text("orders['amount'].sum()")
    expect(workbench.locator(".wb-script-editor .wb-problem-lens")).to_have_count(0)  # the edit clears it
    workbench.get_by_role("button", name="Run file").click()
    expect(workbench.locator(".wb-console-log .wb-console-entry").last).not_to_contain_text(
        "KeyError", timeout=30000
    )
    # In a notebook: a missing import, added with one click.
    workbench.get_by_role("button", name="New notebook").first.click()
    cell = workbench.locator(".wb-cell").first
    expect(cell.locator(".monaco-editor")).to_be_visible(timeout=20000)
    cell.locator(".monaco-editor").click()
    workbench.keyboard.insert_text("np.arange(3).sum()")
    workbench.keyboard.press("Control+Enter")
    expect(cell.locator(".wb-err")).to_contain_text("import numpy as np", timeout=60000)
    cell.locator(".wb-err").get_by_role("button", name="Add import numpy as np").click()
    expect(cell.locator(".view-lines")).to_contain_text("import numpy as np")
    workbench.keyboard.press("Control+Enter")
    expect(cell.locator(".wb-text")).to_contain_text("3", timeout=30000)  # np.int64(3) in numpy 2
