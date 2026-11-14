# ruff: noqa: F811
"""The Workbench's usability fixes (the usability test, 2026-11-14): a new notebook has the cursor in its
first cell, its console is a title bar until it is used, and a table result opens as a grid from beside
it. Made-up data only."""

from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import Browser, expect

from tests.e2e.test_ui import browser, call, open_grid, orders_url, server  # noqa: F401
from tests.e2e.test_workbench_page import open_workbench


def test_a_new_notebook_types_at_once_and_a_table_result_opens_as_a_grid(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    workbench = open_workbench(open_grid(browser, orders_url))
    workbench.get_by_role("button", name="New notebook").first.click()
    cell = workbench.locator(".wb-cell").first
    expect(cell.locator(".monaco-editor")).to_be_visible(timeout=20000)
    # No click first: the cursor is in the first cell.
    expect(cell.locator(".monaco-editor.focused")).to_be_visible(timeout=10000)
    # The console is its title bar: the cells have the column.
    panes = workbench.locator(".wb-panes")
    expect(panes).to_have_class(re.compile("console-compact"))
    expect(workbench.locator(".wb-console-log")).to_be_hidden()

    frame = "{'city': ['Springfield', 'Riverton'], 'rain_mm': [3.5, 0.0]}"
    workbench.keyboard.type(f"import pandas as pd\nrainfall = pd.DataFrame({frame})\nrainfall")
    workbench.keyboard.press("Control+Enter")
    expect(cell.locator(".wb-count")).to_have_text("[1]", timeout=60000)
    # The result's Open as grid: named after the cell's variable (2 actions from the cell).
    cell.get_by_role("button", name="Open as grid").click()
    expect(workbench.locator(".toast").last).to_contain_text("rainfall is open as a grid", timeout=30000)
    assert any(d["name"] == "rainfall" for d in call(server, "GET", "/datasets")["datasets"])

    # An expression with no name of its own: out_<n>.
    cell.locator(".monaco-editor").click()
    workbench.keyboard.press("Control+End")
    workbench.keyboard.type("\nrainfall.describe().reset_index()")
    workbench.keyboard.press("Control+Enter")
    expect(cell.locator(".wb-count")).to_have_text("[2]", timeout=60000)
    cell.get_by_role("button", name="Open as grid").click()
    expect(workbench.locator(".toast").last).to_contain_text("out_2 is open as a grid", timeout=30000)
    rows = {d["name"]: d["rows"] for d in call(server, "GET", "/datasets")["datasets"]}
    assert rows["rainfall"] == 2 and rows["out_2"] == 8  # describe() did not replace the table

    # A click on the console's bar opens it at its size, and it stays open for this notebook.
    workbench.locator(".wb-console-head").click()
    expect(workbench.locator(".wb-console-log")).to_be_visible()
    expect(panes).not_to_have_class(re.compile("console-compact"))
    for name in ("rainfall", "out_2"):
        call(server, "DELETE", f"/datasets/{name}")
