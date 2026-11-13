# ruff: noqa: F811
"""Compare's diff mode on the page: only the rows that differ, the changed cells marked on both sides,
a button per changed column, a tolerance that makes a cent's change go away, and Every row."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd
from playwright.sync_api import Browser, expect

from swissarmygrid.client import push_client
from tests.e2e.test_ui import browser, open_grid, server  # noqa: F401


def test_diff_mode_shows_only_what_changed(browser: Browser, server: dict[str, Any]) -> None:
    before = pd.DataFrame(
        {
            "sku": ["A1", "A2", "A3", "A4"],
            "region": ["North", "North", "East", "East"],
            "price": [10.0, 20.0, 30.0, 40.0],
        }
    )
    after = pd.DataFrame(
        {
            "sku": ["A1", "A2", "A3", "A5"],
            "region": ["North", "North", "Central", "East"],
            "price": [10.0, 20.01, 30.0, 50.0],
        }
    )
    push_client.push(after, name="diff_after", new_grid=True)
    page = open_grid(
        browser, push_client.push(before, name="diff_before", new_grid=True), width=1440, height=950
    )
    try:
        tabs = page.locator(".tab-item")
        tabs.filter(has_text="diff_after").drag_to(tabs.filter(has_text="diff_before"))
        expect(page.locator("dialog .compare-dialog tbody tr").first).to_contain_text("sku", timeout=20000)
        page.locator("dialog").get_by_role("button", name="Compare", exact=True).click()
        ribbon = page.locator(".ribbon")
        ribbon.get_by_role("button", name="Diff", exact=True).click()
        note = ribbon.locator(".diff-note")
        expect(note).to_contain_text("2 of 3 matched rows differ", timeout=20000)
        expect(note).to_contain_text("1 only in diff_before · 1 only in diff_after")
        panes = page.locator(".pane:not([hidden])")
        left, right = panes.nth(0), panes.nth(1)
        expect(left.locator(".ag-row")).to_have_count(4)  # A2, A3, A4 and A5's placeholder
        changed = right.locator(".ag-row[row-index='0'] .ag-cell[col-id='price']")
        expect(changed).to_have_class(re.compile("cell-diff"))

        note.get_by_role("button", name="region 1").click()
        expect(left.locator(".ag-row")).to_have_count(1, timeout=20000)
        expect(left.locator(".ag-row .ag-cell[col-id='sku']")).to_have_text("A3")
        ribbon.get_by_role("button", name="region 1").click()  # again: back to every change
        expect(left.locator(".ag-row")).to_have_count(4, timeout=20000)

        ribbon.get_by_role("button", name="How values compare…").click()
        window = page.locator("dialog", has_text="Numbers are equal within")
        window.get_by_label("Numbers are equal within").fill("0.05")
        window.get_by_role("button", name="Apply").click()
        expect(note).to_contain_text("1 of 3 matched rows differ", timeout=20000)
        expect(note.get_by_role("button", name=re.compile("^price"))).to_have_count(0)

        note.get_by_role("button", name="Every row").click()
        expect(ribbon).to_contain_text("3 matched", timeout=20000)
        expect(left.locator(".ag-row")).to_have_count(5)
    finally:
        page.close()
