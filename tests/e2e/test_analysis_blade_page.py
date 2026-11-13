# ruff: noqa: F811
"""Your own analysis in the Analyze menu: an analysis blade saved, then run on the selected column of
the grid as it shows it, its result in a window and then in the Sandbox."""

from __future__ import annotations

from typing import Any

import pandas as pd
from playwright.sync_api import Browser, expect

from swissarmygrid.client import push_client
from tests.e2e.test_ui import browser, call, open_grid, server  # noqa: F401

CODE = """def analyze(df, columns):
    column = columns[0]
    counts = df[column].value_counts().rename_axis(column).reset_index(name="rows")
    return [f"## {column}: {len(counts)} values", counts]
"""


def test_an_analysis_blade_runs_from_the_analyze_menu(browser: Browser, server: dict[str, Any]) -> None:
    call(
        server,
        "POST",
        "/blades",
        {"name": "Value counts", "kind": "analysis", "code": CODE, "variable": "analyze"},
    )
    data = pd.DataFrame({"shop": ["Mill", "Mill", "Oven", "Crust"], "loaves": [3, 5, 2, 8]})
    page = open_grid(
        browser, push_client.push(data, name="bakery_sales", new_grid=True), width=1300, height=900
    )
    try:
        page.locator(".ag-header-cell[col-id='shop'] .sag-header-name").click()
        page.locator(".ribbon .ribbon-group").first.get_by_role("button", name="Analyze").click()
        menu = page.get_by_role("menu", name="Analyze")
        expect(menu).to_contain_text("Your analyses")
        menu.get_by_role("menuitem", name="Value counts").click()
        window = page.locator("dialog", has_text="Value counts")
        expect(window.locator("h2", has_text="shop: 3 values")).to_be_visible(timeout=30000)
        expect(window.locator("tbody tr")).to_have_count(3)
        expect(window.locator("tbody tr").first).to_contain_text("Mill")
        window.get_by_role("button", name="Send to Sandbox").click()
        expect(page.locator(".toast").last).to_contain_text("2 cards from Value counts")
        window.get_by_role("button", name="Close").last.click()
        page.locator(".tab-agent").click()
        expect(page.locator("#ai-panel .card", has_text="shop: 3 values")).to_be_visible(timeout=20000)
    finally:
        page.close()
        call(server, "DELETE", "/blades/Value%20counts")
