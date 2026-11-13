# ruff: noqa: F811
"""Saved dashboards on the page: save the Sandbox, change it, save a second version, open the first from
History (it replaces the Sandbox, after asking), and a chart that reopens live on its open table."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
from playwright.sync_api import Browser, expect

from swissarmygrid.client import push_client, sandbox_client
from tests.e2e.test_ui import browser, open_grid, server  # noqa: F401


def test_save_change_and_open_an_earlier_version(browser: Browser, server: dict[str, Any]) -> None:
    sales = pa.table({"region": ["North", "South", "East"], "amount": [40.0, 25.0, 31.0]})
    url = push_client.push(sales, name="dash_sales", new_grid=True)
    page = open_grid(browser, url, width=1280, height=900)
    page.on("dialog", lambda dialog: dialog.accept())
    toast = page.locator(".toast").last
    try:
        sandbox_client.sandbox("## First reading\n\nNorth leads.", "First reading", key="dash-reading")
        page.locator(".tab-agent").click()
        panel = page.locator("#ai-panel")
        expect(panel.locator(".card-header h3", has_text="First reading")).to_be_visible(timeout=20000)

        def dashboards() -> Any:
            panel.get_by_role("button", name="Dashboards…").click()
            return page.locator("dialog", has_text="Save the Sandbox as")

        window = dashboards()
        window.get_by_label("Dashboard name").fill("dash_weekly")
        window.get_by_role("button", name="Save", exact=True).click()
        expect(toast).to_contain_text("Saved dash_weekly")
        expect(window.locator(".dashboard-row", has_text="dash_weekly")).to_contain_text("1 card")
        window.get_by_role("button", name="Close").last.click()

        sandbox_client.sandbox("## Second reading\n\nSouth caught up.", "Second reading", key="dash-reading")
        expect(panel.locator(".card-header h3", has_text="Second reading")).to_be_visible(timeout=20000)
        window = dashboards()
        window.get_by_label("Dashboard name").fill("dash_weekly")
        window.get_by_role("button", name="Save", exact=True).click()
        expect(toast).to_contain_text("(version 2)")
        row = window.locator(".dashboard-row", has_text="dash_weekly")
        row.get_by_role("button", name="History").click()
        versions = row.locator(".dashboard-history li")
        expect(versions).to_have_count(2)
        expect(versions.first).to_contain_text("Second reading")
        versions.last.get_by_role("button", name="Open this version").click()
        expect(toast).to_contain_text("Opened dash_weekly: 1 card")
        expect(panel.locator(".card-header h3", has_text="First reading")).to_be_visible()
        expect(panel.locator(".card-header h3", has_text="Second reading")).to_have_count(0)
        expect(panel.locator(".card", has_text="First reading").locator(".card-table")).to_have_text("Python")
    finally:
        page.close()
