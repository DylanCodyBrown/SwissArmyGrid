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
    second = workbench.locator(".wb-cell").nth(1)
    second.get_by_label("Cell kind").select_option("sql")
    second.get_by_label("Connection").select_option("Orders")
    second.locator(".monaco-editor").click()
    workbench.keyboard.type("SELECT sum(amount) AS total FROM orders")
    workbench.keyboard.press("Control+Enter")
    frame = second.frame_locator("iframe.wb-html")
    expect(frame.locator("td").last).to_have_text("20.0", timeout=60000)
    expect(workbench.locator(".wb-state")).to_have_text("ready")
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


def test_a_script_runs_in_its_console(browser: Browser, orders_url: str) -> None:
    workbench = open_workbench(open_grid(browser, orders_url))
    workbench.get_by_role("button", name="New script").first.click()
    editor = workbench.locator(".wb-script .monaco-editor")
    expect(editor).to_be_visible(timeout=20000)
    editor.click()
    workbench.keyboard.type("total = sum(range(10))\nprint('total is', total)")
    workbench.get_by_role("button", name="Run file").click()
    log = workbench.locator(".wb-console-log")
    expect(log).to_contain_text("total is 45", timeout=60000)
    console = workbench.get_by_label("Python console: type a line and press Enter")
    console.fill("total * 2")
    console.press("Enter")
    expect(log).to_contain_text("90")
    console.press("ArrowUp")
    expect(console).to_have_value("total * 2")  # what was typed comes back
