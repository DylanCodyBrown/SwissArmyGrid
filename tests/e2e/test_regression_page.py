# ruff: noqa: F811
"""Regression in a real browser on made-up orders with planted effects: Find predictors hands over to
Regression, the what-if predicts, Check the misses adds the miss column and opens Outliers on it; and an
analysis form's Data shape panel checks the grid."""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Browser, expect

from swissarmygrid.client import push_client
from tests.e2e.test_ui import browser, open_grid, server  # noqa: F401
from tests.test_regression import orders


def test_predictors_hand_over_to_a_regression_whose_misses_go_to_outliers(
    browser: Browser, server: dict[str, Any]
) -> None:
    url = push_client.push(orders(4000), name="reg_orders", new_grid=True)
    page = open_grid(browser, url, width=1440, height=1000)
    try:
        row = page.locator(".ribbon .ribbon-group").first

        def menu(entry: str) -> None:
            row.get_by_role("button", name="Analyze").click()
            page.get_by_role("menuitem", name=entry).click()

        menu("Odds…")
        dialog = page.locator("dialog")
        dialog.get_by_label("Column", exact=True).select_option("region")
        dialog.locator("summary", has_text="Data shape").click()
        expect(dialog.locator(".cheat-checks")).to_contain_text("region: 4 values: fine.", timeout=20000)
        expect(dialog.locator(".cheat-bad")).to_contain_text("Already counted")
        dialog.get_by_role("button", name="Close").last.click()

        menu("Find predictors…")
        dialog = page.locator("dialog")
        dialog.get_by_label("Target").select_option("amount")
        dialog.get_by_role("button", name="Find").click()
        dialog.get_by_role("button", name="Regression with these").click(timeout=30000)
        dialog = page.locator("dialog")
        expect(dialog.locator(".analysis-verdict")).to_contain_text("explains", timeout=30000)
        expect(dialog.locator("tbody")).to_contain_text("each extra 1 of units")
        dialog.get_by_role("button", name="Predict", exact=True).click()
        expect(dialog.locator(".what-if .analysis-headline")).to_contain_text("amount: about", timeout=10000)
        dialog.get_by_role("button", name="Check the misses").click()
        outliers = page.locator("dialog")
        expect(outliers.get_by_label("Column", exact=True)).to_have_value("miss_amount", timeout=20000)
        expect(outliers.locator(".analysis-verdict")).to_be_visible(timeout=20000)
        expect(page.locator(".steps-strip")).to_contain_text("1 step")
    finally:
        page.close()
