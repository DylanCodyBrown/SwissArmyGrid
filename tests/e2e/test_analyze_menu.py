# ruff: noqa: F811
"""The Table row's Analyze menu in a real browser, on the made-up orders: odds with an outcome handed
to the fit test, find predictors opened as a grid, a result sent to the Sandbox, outliers excluded."""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Browser, expect

from swissarmygrid.client import push_client
from tests.e2e.test_ui import browser, open_grid, server  # noqa: F401
from tests.test_analysis import orders


def test_the_analyze_menu_chains_odds_fit_predictors_and_outliers(
    browser: Browser, server: dict[str, Any]
) -> None:
    url = push_client.push(orders(), name="orders", new_grid=True)
    page = open_grid(browser, url, width=1440, height=1000)
    try:
        table_row = page.locator(".ribbon .ribbon-group").first

        def menu(entry: str) -> None:
            table_row.get_by_role("button", name="Analyze").click()
            page.get_by_role("menuitem", name=entry).click()

        page.locator(".ag-header-cell[col-id='region'] .sag-header-name").click()
        menu("Odds…")
        dialog = page.locator("dialog")
        expect(dialog.locator(".odds-table tbody tr")).to_have_count(4, timeout=20000)  # four regions
        dialog.get_by_label("Outcome column").select_option("late")
        dialog.get_by_label("Outcome value").fill("late")
        dialog.get_by_role("button", name="Count").click()
        expect(dialog.locator(".analysis-headline")).to_contain_text("West: late 4", timeout=20000)
        expect(dialog.locator("tr.odds-up")).to_have_count(1)
        dialog.get_by_role("button", name="Send to Sandbox").click()
        expect(page.locator(".toast")).to_contain_text("In the Sandbox")
        dialog.get_by_role("button", name="Test together").click()
        dialog = page.locator("dialog")
        expect(dialog.get_by_label("Expected spread")).to_have_value("together")
        expect(dialog.locator(".analysis-verdict")).to_contain_text("Related", timeout=20000)
        dialog.get_by_role("button", name="Close").last.click()

        menu("Find predictors…")
        dialog = page.locator("dialog")
        dialog.get_by_label("Target").select_option("amount")
        dialog.get_by_role("button", name="Find").click()
        expect(dialog.locator(".analysis-verdict")).to_contain_text(
            "units, price go with amount", timeout=30000
        )
        expect(dialog).to_contain_text("order_id (every row a different whole number")
        dialog.get_by_role("button", name="Open as grid").click()
        expect(page.locator(".tabs-row .tab-item", has_text="orders_predictors_amount")).to_be_visible(
            timeout=20000
        )
        dialog.get_by_role("button", name="Close").last.click()

        page.locator(".tabs-row .tab-item", has_text="orders").first.click()
        menu("Outliers…")
        dialog = page.locator("dialog")
        dialog.get_by_label("Column", exact=True).select_option("amount")
        dialog.get_by_role("button", name="Look", exact=True).click()
        expect(dialog.locator(".analysis-verdict")).to_contain_text("values look out of place", timeout=20000)
        dialog.get_by_role("button", name="Exclude these").click()
        expect(dialog).to_have_count(0)
        expect(page.locator(".steps-strip")).to_contain_text("1 step")

        page.locator(".tab-agent").click()
        expect(page.locator("#ai-panel")).to_contain_text("Odds of late by region")
    finally:
        page.close()
