# ruff: noqa: F811
"""The tie-out report on a compare: made-up books on both sides, the report in a window (off groups
first), a click on a group filters both grids to it, and the report goes to the Sandbox."""

from __future__ import annotations

from typing import Any

import pandas as pd
from playwright.sync_api import Browser, expect

from swissarmygrid.client import push_client
from tests.e2e.test_ui import browser, open_grid, server  # noqa: F401


def test_the_tie_out_report_and_a_click_on_a_group(browser: Browser, server: dict[str, Any]) -> None:
    books = pd.DataFrame(
        {
            "order_no": [1, 2, 3, 4, 5],
            "region": ["North", "North", "South", "East", "East"],
            "amount": [10.0, 20.0, 30.0, 40.0, 5.0],
        }
    )
    ledger = pd.DataFrame(
        {
            "order_no": [1, 2, 3, 4, 6],
            "region": ["North", "North", "South", "East", "South"],
            "amount": [10.0, 20.0, 30.0, 25.0, 1.0],
        }
    )
    push_client.push(ledger, name="tie_ledger", new_grid=True)
    page = open_grid(
        browser, push_client.push(books, name="tie_books", new_grid=True), width=1440, height=950
    )
    try:
        tabs = page.locator(".tab-item")
        tabs.filter(has_text="tie_ledger").drag_to(tabs.filter(has_text="tie_books"))
        picker = page.locator("dialog .compare-dialog")
        expect(picker.locator("tbody tr").first).to_contain_text("order_no", timeout=20000)
        page.locator("dialog").get_by_role("button", name="Compare", exact=True).click()
        expect(page.locator(".ribbon")).to_contain_text("matched", timeout=20000)

        page.locator(".ribbon").get_by_role("button", name="Tie-out…").click()
        window = page.locator("dialog", has_text="Tie-out: tie_books and tie_ledger")
        expect(window.get_by_label("Group by region")).to_be_checked()
        expect(window.locator(".analysis-verdict")).to_have_text("2 of 3 groups are off.", timeout=20000)
        first = window.locator("tbody .tie-row").first
        expect(first).to_contain_text("East")  # off by 20, the largest
        expect(first.locator(".badge")).to_have_text("off")
        expect(window.locator("tbody .tie-row", has_text="North").locator(".badge")).to_have_text("ties")
        expect(window.locator("tfoot")).to_contain_text("Total")

        window.get_by_role("button", name="Send to Sandbox").click()
        expect(page.locator(".toast").last).to_contain_text("a note and the table")
        first.click()
        expect(page.locator(".toast").last).to_contain_text("Both grids show East")
        # East on both sides: orders 4 and 5 on the left, order 4 on the right.
        counts = "1 matched · 1 only in tie_books · 0 only"
        expect(page.locator(".ribbon")).to_contain_text(counts, timeout=20000)
        panes = page.locator(".pane:not([hidden])")
        expect(panes.nth(0).locator(".ag-row")).to_have_count(2)
        expect(panes.nth(1).locator(".ag-row")).to_have_count(2)
    finally:
        page.close()
