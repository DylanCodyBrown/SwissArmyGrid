# ruff: noqa: F811
"""The tour: the question mark in the Application row, every word lit up on the page, then the game."""

from __future__ import annotations

from typing import Any

import pandas as pd
from playwright.sync_api import Browser, expect

from swissarmygrid.client import push_client
from tests.e2e.test_ui import browser, open_grid, server  # noqa: F401

STOPS = 15  # tour.ts STOPS: the welcome card and fourteen words


def test_the_tour_lights_up_each_word_and_ends_with_a_game(browser: Browser, server: dict[str, Any]) -> None:
    frame = pd.DataFrame({"order_id": [1, 2, 3], "item": ["Anvil", "Bolt", "Cog"]})  # made up
    url = push_client.push(frame, name="tour_orders", new_grid=True)
    page = open_grid(browser, url, width=1440)
    try:
        row = page.locator(".app-row")
        bubble = row.locator(".tour-bubble")
        expect(bubble).to_contain_text("New here?")  # a first visit
        row.get_by_role("button", name="Tour").click()
        tour = page.get_by_role("dialog", name="Tour")
        expect(tour).to_contain_text("Hi!")
        expect(bubble).to_have_count(0)  # seen: gone for good
        expect(tour).to_have_class("tour tour-dim")  # the welcome card has nothing to point at
        tour.get_by_role("button", name="Next").click()
        expect(tour.locator(".tour-word")).to_have_text("Grid")
        spot = tour.locator(".tour-spot")
        expect(spot).to_be_visible()
        tab = page.locator(".tabs-row .tab-item:not(.tab-app)").first.bounding_box()
        box = spot.bounding_box()
        assert tab and box and box["x"] <= tab["x"] and box["x"] + box["width"] >= tab["x"] + tab["width"]
        page.keyboard.press("ArrowLeft")
        expect(tour.locator(".tour-word")).to_have_text("Hi!")
        for _ in range(STOPS - 1):
            page.keyboard.press("ArrowRight")
        expect(tour.locator(".tour-word")).to_have_text("Agent")
        tour.get_by_role("button", name="Play").click()
        for round_number in range(1, 6):
            expect(tour.locator(".tour-kicker")).to_have_text(f"Round {round_number} of 5")
            tour.locator(".tour-choice").first.click()
            expect(tour.locator(".tour-choice.right")).to_have_count(1)
        expect(tour.locator(".tour-word")).to_contain_text("of 5.", timeout=5000)
        expect(tour.locator(".tour-code")).to_contain_text("sag.push(df)")
        tour.get_by_role("button", name="Start").click()
        expect(tour).to_have_count(0)
        page.reload()
        expect(page.locator(".app-row .tour-bubble")).to_have_count(0)  # remembered in this browser
        page.locator(".app-row").get_by_role("button", name="Tour").click()
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog", name="Tour")).to_have_count(0)
    finally:
        page.close()
