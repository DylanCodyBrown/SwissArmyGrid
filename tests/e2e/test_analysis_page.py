# ruff: noqa: F811
"""The Column row's Fit test and Outliers windows in a real browser, on the made-up invoices."""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Browser, expect

from swissarmygrid.client import push_client
from tests.e2e.test_ui import browser, open_grid, server  # noqa: F401
from tests.test_analysis import invoices


def test_fit_test_then_outliers_within_each_vendor_shown_as_a_step(
    browser: Browser, server: dict[str, Any]
) -> None:
    url = push_client.push(invoices(), name="invoices", new_grid=True)
    page = open_grid(browser, url, width=1440)
    try:
        column_row = page.locator(".ribbon .ribbon-group").nth(1)
        page.locator(".ag-header-cell[col-id='amount'] .sag-header-name").click()
        page.locator(".ag-header-cell[col-id='typed'] .sag-header-name").click(modifiers=["Control"])
        column_row.get_by_role("button", name="Fit test…").click()
        dialog = page.locator("dialog")
        expect(dialog.get_by_label("Expected spread")).to_have_value("benford")  # number columns
        cards = dialog.locator(".analysis-card")
        expect(cards).to_have_count(2, timeout=20000)
        expect(cards.nth(0).locator(".analysis-verdict")).to_have_class("analysis-verdict ok")
        expect(cards.nth(1).locator(".analysis-verdict")).to_contain_text("Doesn't fit")
        expect(cards.nth(1)).to_contain_text("nonconformity")
        expect(cards.nth(1).locator(".fit-row:not(.head)")).to_have_count(9)  # the nine first digits
        dialog.get_by_role("button", name="Close").last.click()

        page.locator(".ag-header-cell[col-id='unit_cost'] .sag-header-name").click()
        column_row.get_by_role("button", name="Outliers…").click()
        dialog = page.locator("dialog")
        expect(dialog.locator(".analysis-verdict")).to_contain_text("3 of 6,000 values", timeout=20000)
        dialog.get_by_label("Within each").select_option("vendor")
        dialog.get_by_role("button", name="Look", exact=True).click()
        expect(dialog).to_contain_text("1 of 4 have outliers")
        dialog.get_by_role("button", name="Show these rows").click()
        expect(dialog).to_have_count(0)
        expect(page.locator("#status")).to_contain_text("of 3", timeout=20000)
        expect(page.locator(".steps-strip")).to_contain_text("1 step")

        page.locator(".steps-strip").get_by_role("button", name="Undo").first.click()
        expect(page.locator("#status")).to_contain_text("of 6,000", timeout=20000)
        page.locator(".ag-header-cell[col-id='region'] .sag-header-name").click()
        column_row.get_by_role("button", name="Outliers…").click()
        dialog = page.locator("dialog")
        expect(dialog.get_by_label("Look at")).to_have_value("frequency")  # a text column
        expect(dialog.locator(".analysis-verdict")).to_contain_text("1 rarer", timeout=20000)
        expect(dialog.locator(".analysis-results tbody")).to_contain_text("Nrth")
    finally:
        page.close()
