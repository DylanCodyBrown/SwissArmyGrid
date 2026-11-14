# ruff: noqa: F811
"""Renaming a table from the page (the usability test, 2026-11-14): the Pasted toast holds the name in a
box, a double-click on a tab renames its table, and the agent's rename shows up the same way. Made-up
data only."""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Browser, expect

from tests.e2e.test_ui import browser, call, open_grid, orders_url, server  # noqa: F401


def test_a_pasted_table_is_named_in_its_toast_and_any_tab_renamed_in_place(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    page = open_grid(browser, orders_url)
    page.evaluate(
        """() => {
            const data = new DataTransfer();
            data.setData('text/plain', 'store\\tsales\\nNorth\\t10\\nSouth\\t7\\n');
            document.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true }));
        }"""
    )
    # The toast holds the new name: click, type, Enter (3 actions).
    box = page.locator(".toast-name")
    expect(box).to_be_visible(timeout=10000)
    pasted = box.input_value()
    assert pasted.startswith("pasted_")
    box.click()
    box.fill("store_sales")
    box.press("Enter")
    tab = page.locator(".tab-item .tab-label", has_text="store_sales")
    expect(tab).to_be_visible(timeout=10000)
    expect(page.locator("#grid-title")).to_contain_text("store_sales")
    names = [d["name"] for d in call(server, "GET", "/datasets")["datasets"]]
    assert "store_sales" in names and pasted not in names

    # A double-click on a tab: its name in a box, Enter renames (3 actions).
    tab.dblclick()
    rename = page.get_by_label("New name for store_sales")
    expect(rename).to_be_focused()
    page.keyboard.type("north_south")
    page.keyboard.press("Enter")
    expect(page.locator(".tab-item .tab-label", has_text="north_south")).to_be_visible(timeout=10000)
    expect(page.locator(".pane.active .ag-row[row-index='0']").first).to_be_visible(timeout=10000)

    # A taken name is refused and the tab keeps its name.
    page.locator(".tab-item .tab-label", has_text="north_south").dblclick()
    page.keyboard.type("orders")
    page.keyboard.press("Enter")
    expect(page.locator(".toast.error").last).to_contain_text("orders")
    expect(page.locator(".tab-item .tab-label", has_text="north_south")).to_be_visible()

    # The agent renames it: the page follows.
    call(server, "POST", "/datasets/north_south/rename", {"to": "regions"}, agent=True)
    expect(page.locator(".tab-item .tab-label", has_text="regions")).to_be_visible(timeout=10000)
    call(server, "DELETE", "/datasets/regions")
