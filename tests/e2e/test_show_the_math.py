# ruff: noqa: F811
"""Show the math in the browser: a fit test's and an odds result's proofs land in the Sandbox as notes
with their working (a table, the sums, the formulas), and the what-if's estimate is proven term by term."""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Browser, expect

from swissarmygrid.client import push_client
from tests.e2e.test_ui import browser, open_grid, server  # noqa: F401
from tests.test_analysis import orders


def test_show_the_math_puts_each_proof_in_the_sandbox(browser: Browser, server: dict[str, Any]) -> None:
    url = push_client.push(orders().slice(0, 600), name="math_orders", new_grid=True)
    page = open_grid(browser, url, width=1440, height=1000)
    toast = page.locator(".toast").last
    try:
        table_row = page.locator(".ribbon .ribbon-group").first
        page.locator(".ag-header-cell[col-id='region'] .sag-header-name").click()
        table_row.get_by_role("button", name="Analyze").click()
        page.get_by_role("menuitem", name="Odds…").click()
        dialog = page.locator("dialog")
        dialog.get_by_label("Outcome column").select_option("late")
        dialog.get_by_label("Outcome value").fill("late")
        dialog.get_by_role("button", name="Count").click()
        expect(dialog.locator(".analysis-headline")).to_contain_text("late", timeout=20000)
        dialog.get_by_role("button", name="Show the math").click()
        expect(toast).to_contain_text("the math, step by step")
        dialog.get_by_role("button", name="Test together").click()
        dialog = page.locator("dialog")
        expect(dialog.locator(".analysis-verdict")).to_contain_text("Related", timeout=20000)
        dialog.get_by_role("button", name="Show the math").click()
        expect(toast).to_contain_text("the math, step by step")
        dialog.get_by_role("button", name="Close").last.click()

        page.locator(".tab-agent").click()
        panel = page.locator("#ai-panel")
        odds = panel.locator(".card", has_text="The math: Odds on math_orders")
        expect(odds.locator("table").first).to_be_visible(timeout=20000)
        expect(odds).to_contain_text("Odds ratio = (a × d) / (b × c)")
        together = panel.locator(".card", has_text="The math: Fit test on math_orders")
        expect(together).to_contain_text("χ² = Σ (O − E)² / E")
        expect(together).to_contain_text("Cramér's V")
    finally:
        page.close()
