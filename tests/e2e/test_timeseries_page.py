# ruff: noqa: F811
"""Time series and Forecast in a real browser on made-up daily sales with a planted season, spike and
step: the words, the chart, Show these rows, and the hand-over to Forecast with its ranges."""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Browser, expect

from swissarmygrid.client import push_client
from tests.e2e.test_ui import browser, open_grid, server  # noqa: F401
from tests.test_timeseries import sales_rows


def test_time_series_reads_the_series_and_hands_over_to_forecast(
    browser: Browser, server: dict[str, Any]
) -> None:
    url = push_client.push(sales_rows(), name="ts_sales", new_grid=True)
    page = open_grid(browser, url, width=1440, height=1000)
    try:
        page.locator(".ribbon .ribbon-group").first.get_by_role("button", name="Analyze").click()
        page.get_by_role("menuitem", name="Time series…").click()
        dialog = page.locator("dialog")
        words = dialog.locator(".analysis-summary")
        expect(words).to_contain_text("December run", timeout=30000)
        expect(words).to_contain_text("spike 2021-06-01")
        expect(dialog.locator(".analysis-chart .main-svg").first).to_be_visible(timeout=20000)
        dialog.get_by_role("button", name="Forecast…").click()
        dialog = page.locator("dialog")
        expect(dialog.locator(".analysis-summary")).to_contain_text("Next month: about", timeout=30000)
        expect(dialog.locator(".analysis-summary")).to_contain_text("Held back the last 12 months")
        expect(dialog.locator(".analysis-results tbody tr")).to_have_count(12)
        dialog.get_by_label("Periods ahead").fill("3")
        dialog.get_by_role("button", name="Forecast", exact=True).click()
        expect(dialog.locator(".analysis-results tbody tr")).to_have_count(3, timeout=20000)
        dialog.get_by_role("button", name="Close").last.click()
        page.locator(".ribbon .ribbon-group").first.get_by_role("button", name="Analyze").click()
        page.get_by_role("menuitem", name="Time series…").click()
        page.locator("dialog").get_by_role("button", name="Show these rows").click(timeout=30000)
        expect(page.locator("#status")).to_contain_text("of 4", timeout=20000)
    finally:
        page.close()
