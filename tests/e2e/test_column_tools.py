# ruff: noqa: F811
"""The column's tools beside the column (the usability test, 2026-11-14): its right-click menu has the
column tools above the analyses, Rename… opens only the selected column, and a double-click on its name
renames it in place. Made-up data only (the grid tests' orders)."""

from __future__ import annotations

from playwright.sync_api import Browser, expect

from tests.e2e.test_ui import browser, open_grid, orders_url, server  # noqa: F401


def test_a_columns_tools_are_beside_it(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    # Right-click the header: the column's own tools first, then the analyses.
    page.locator(".ag-header-cell[col-id='region'] .sag-header-name").click(button="right")
    menu = page.get_by_role("menu", name="region")
    items = menu.get_by_role("menuitem")
    expect(items.nth(0)).to_have_text("Sort A→Z")
    for label in ("Sort Z→A", "Filter…", "Rename…", "Remove", "Keep only", "Group by…", "Profile to Sandbox"):
        expect(menu.get_by_role("menuitem", name=label)).to_be_visible()
    menu.get_by_role("menuitem", name="Sort Z→A").click()
    expect(page.locator(".steps-count")).to_have_text("1 step")

    # Rename… on the selected column: only its box, ready to type, Enter adds the step (3 actions).
    page.locator(".ag-header-cell[col-id='region'] .sag-header-name").click()
    page.locator(".ribbon").get_by_role("button", name="Rename…").click()
    dialog = page.locator("dialog")
    expect(dialog.get_by_role("textbox")).to_have_count(1)
    expect(dialog.get_by_label("New name for region")).to_be_focused()
    page.keyboard.type("area")
    page.keyboard.press("Enter")
    expect(page.locator(".ag-header-cell[col-id='area']")).to_be_visible(timeout=15000)

    # Double-click a name: rename it there, Enter keeps it (3 actions).
    page.locator(".ag-header-cell[col-id='product'] .sag-header-name").dblclick()
    box = page.get_by_label("New name for product")
    expect(box).to_be_focused()
    page.keyboard.type("item")
    page.keyboard.press("Enter")
    expect(page.locator(".ag-header-cell[col-id='item']")).to_be_visible(timeout=15000)
    # Escape leaves a name as it was.
    page.locator(".ag-header-cell[col-id='item'] .sag-header-name").dblclick()
    page.keyboard.type("nope")
    page.keyboard.press("Escape")
    expect(page.locator(".ag-header-cell[col-id='item']")).to_be_visible()
    expect(page.locator(".steps-count")).to_have_text("3 steps")
