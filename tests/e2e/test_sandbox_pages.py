# ruff: noqa: F811
"""The Sandbox as pages in a real browser: moving cards (the menu and a drop), Page view on letter and
landscape paper, a card that starts a page, and printing (the print dialog is stubbed: headless
browsers have none)."""

from __future__ import annotations

from typing import Any

import pandas as pd
from playwright.sync_api import Browser, expect

from swissarmygrid.client import push_client
from tests.e2e.test_ui import browser, call, open_grid, server  # noqa: F401

TABLE = "| Region | Late |\n|---|---:|\n| West | 46% |\n| North | 15% |"


def titles(page: Any) -> list[str]:
    return [t.strip() for t in page.locator("#ai-panel .card .card-header h3").all_inner_texts()]


def test_cards_move_lay_out_on_pages_and_print(browser: Browser, server: dict[str, Any]) -> None:
    url = push_client.push(pd.DataFrame({"a": [1, 2, 3]}), name="pages_demo", new_grid=True)  # made up
    grid = url.split("/g/")[1].split("#")[0]
    for title in ("First", "Second", "Third"):
        call(
            server,
            "POST",
            f"/grids/{grid}/cards",
            {"kind": "note", "title": title, "body": f"# {title}\n\n{TABLE}", "size": {"w": 6, "h": 5}},
        )
    page = open_grid(browser, url, width=1400, height=1000)
    page.add_init_script("window.print = () => { window.__printed = document.body.className; };")
    page.reload()
    try:
        page.locator(".tab-agent").click()
        expect(page.locator("#ai-panel .card")).to_have_count(3)
        assert titles(page) == ["First", "Second", "Third"]
        expect(page.locator("#ai-panel .md-table").first).to_contain_text("46%")  # markdown tables draw
        page.locator("#ai-panel .card", has_text="Third").get_by_role(
            "button", name="More card actions"
        ).click()
        page.get_by_role("menuitem", name="Move earlier").click()
        expect(page.locator("#ai-panel .card").nth(1)).to_contain_text("Third")
        # A drop on the left half of the first card puts the dragged card before it.
        page.evaluate(
            """() => {
              const cards = [...document.querySelectorAll('#ai-panel .card')];
              const data = new DataTransfer();
              const start = new DragEvent('dragstart', {bubbles: true, dataTransfer: data});
              cards[2].querySelector('.card-header').dispatchEvent(start);
              const box = cards[0].getBoundingClientRect();
              const at = {bubbles: true, cancelable: true, dataTransfer: data,
                          clientX: box.left + 5, clientY: box.top + 20};
              cards[0].dispatchEvent(new DragEvent('dragover', at));
              cards[0].dispatchEvent(new DragEvent('drop', at));
            }"""
        )
        expect(page.locator("#ai-panel .card").first).to_contain_text("Second")
        assert titles(page) == ["Second", "First", "Third"]

        page.get_by_role("button", name="Page view").click()
        expect(page.locator(".page")).to_have_count(1)  # three half-width notes fit one letter page
        page.locator("#ai-panel .card", has_text="Third").get_by_role(
            "button", name="More card actions"
        ).click()
        page.get_by_role("menuitem", name="Start a new page here").click()
        expect(page.locator(".page")).to_have_count(2)
        expect(page.locator(".page").nth(1)).to_contain_text("Third")
        expect(page.locator(".page-number").last).to_have_text("2 / 2")
        page.get_by_label("Paper").select_option("landscape")
        assert page.locator(".pages").get_attribute("data-paper") == "landscape"

        page.get_by_role("button", name="Print to PDF").click()
        page.wait_for_function("window.__printed !== undefined", timeout=10000)
        assert "sandbox-printing" in page.evaluate("window.__printed")
        expect(page.locator("body")).not_to_have_class("sandbox-printing", timeout=5000)  # restored after
    finally:
        page.close()
