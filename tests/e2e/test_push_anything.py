# ruff: noqa: F811
"""Anything that shows details goes to the Sandbox: right-click a cell (its value, its row, Keep these),
right-click a header (its profile, its distinct values), and the Send to Sandbox buttons of the column
profile, the whole-table profile and the value viewer. On the made-up orders."""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Browser, expect

from swissarmygrid.client import push_client
from tests.e2e.test_ui import browser, open_grid, server  # noqa: F401
from tests.test_analysis import orders


def test_right_click_and_send_buttons_put_details_in_the_sandbox(
    browser: Browser, server: dict[str, Any]
) -> None:
    url = push_client.push(orders().slice(0, 400), name="push_orders", new_grid=True)
    page = open_grid(browser, url, width=1440, height=1000)
    toast = page.locator(".toast").last

    def sandbox_has(*titles: str) -> None:
        page.locator(".tab-agent").click()
        for title in titles:
            expect(page.locator("#ai-panel .card-header h3", has_text=title).first).to_be_visible(
                timeout=20000
            )
        page.locator(".tabs-row .tab-item", has_text="push_orders").first.click()

    try:
        cell = page.locator(".ag-row[row-index='0'] .ag-cell[col-id='region']")
        cell.click(button="right")
        menu = page.get_by_role("menu", name="region, row 1")
        menu.get_by_role("menuitem", name="Send value to Sandbox").click()
        expect(toast).to_contain_text("In the Sandbox")
        cell.click(button="right")
        page.get_by_role("menuitem", name="Send the row to Sandbox").click()
        expect(toast).to_contain_text("In the Sandbox")
        sandbox_has("region, row 1", "Row 1 of push_orders")
        expect(
            page.locator("#ai-panel .card", has_text="Row 1 of push_orders").locator(".md-table tr")
        ).to_have_count(9)

        header = page.locator(".ag-header-cell[col-id='units'] .sag-header-name")
        header.click(button="right")
        page.get_by_role("menu", name="units").get_by_role("menuitem", name="Profile to Sandbox").click()
        expect(toast).to_contain_text("a note and its chart")
        page.locator(".ag-header-cell[col-id='size'] .sag-header-name").click(button="right")
        page.get_by_role("menuitem", name="Distinct values to Sandbox").click()
        expect(toast).to_contain_text("In the Sandbox")
        sandbox_has("Profile of units", "Distinct values of size")

        page.locator(".ag-header-cell[col-id='price'] .sag-header-name").click(button="right")
        page.get_by_role("menuitem", name="Profile…").click()
        page.locator("dialog").get_by_role("button", name="Send to Sandbox").click()
        expect(toast).to_contain_text("a note and its chart")
        page.locator("dialog").get_by_role("button", name="Close").last.click()
        page.locator(".ribbon .ribbon-group").first.get_by_role("button", name="Profile").click()
        expect(page.locator("dialog .stat-grid")).to_be_visible(timeout=20000)
        page.locator("dialog").get_by_role("button", name="Send to Sandbox").click()
        expect(toast).to_contain_text("In the Sandbox")
        page.locator("dialog").get_by_role("button", name="Close").last.click()
        page.locator(".ag-row[row-index='2'] .ag-cell[col-id='office']").dblclick()
        page.locator("dialog").get_by_role("button", name="Send to Sandbox").click()
        expect(toast).to_contain_text("In the Sandbox")
        page.locator("dialog").get_by_role("button", name="Close").last.click()
        sandbox_has("Profile of price", "Profile of push_orders")

        cell.click(button="right")
        page.get_by_role("menuitem", name="Keep these").click()
        expect(page.locator(".steps-strip")).to_contain_text("1 step", timeout=20000)
    finally:
        page.close()
