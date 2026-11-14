# ruff: noqa: F811
"""Tangs and the relationship finder in a real browser, on the made-up nine-table store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from examples.sample_store import make_store
from playwright.sync_api import Browser, expect

from swissarmygrid.client import push_client
from swissarmygrid.domain.relationships import read_tang

# The grid tests' fixtures (browser, server) are imported by name, which pytest needs and ruff reads
# as a redefinition in every test that takes them.
from tests.e2e.test_ui import browser, call, grid_of, open_grid, server  # noqa: F401


def test_find_accept_run_and_share_a_tang(browser: Browser, server: dict[str, Any], tmp_path: Path) -> None:
    urls = [push_client.push(frame, name=name, new_grid=True) for name, frame in make_store().items()]
    team = tmp_path / "team"
    team.mkdir()
    call(server, "PUT", "/blades/folders", {"folders": [str(team)]})
    call(server, "POST", "/relationships", {"model": {"name": "empty"}, "replace": True})
    page = open_grid(browser, urls[0], width=1440)
    try:
        page.locator(".ribbon .ribbon-group").first.get_by_role("button", name="Relationships").click()
        dialog = page.locator("dialog")
        expect(dialog).to_contain_text("No tangs yet")
        dialog.get_by_role("button", name="Find relationships").click()
        suggestions = dialog.locator(".suggestion")
        expect(suggestions).to_have_count(10, timeout=60000)
        expect(dialog.locator(".suggestion", has_text="ship_date")).to_contain_text("inactive")
        expect(dialog.locator(".suggestion input:checked")).to_have_count(10)
        dialog.get_by_role("button", name="Accept the ticked joins").click()
        current = dialog.locator(".rel-table tbody tr")
        expect(current).to_have_count(10, timeout=20000)
        expect(dialog.locator(".suggestion.known")).to_have_count(10, timeout=60000)  # now in use
        dialog.get_by_role("button", name="Run against the open tables").click()
        expect(dialog.locator(".rel-table tr.run-orphans")).to_have_count(
            1, timeout=60000
        )  # orders -> customers
        expect(dialog.locator(".rel-table tr.run-holds")).to_have_count(9)
        # Save it where the team sees it.
        dialog.get_by_role("button", name="Save as tang").click()
        save = page.locator("dialog").last
        save.get_by_label("Tang name").fill("Sample store")
        save.get_by_label("Tags").fill("sales, made-up")
        save.get_by_label("Save it in").select_option(str(team))
        save.get_by_role("button", name="Save").click()
        expect(page.locator(".toast").last).to_contain_text("Saved Sample store in the shared folder")
        listed = dialog.locator(".tang-item", has_text="Sample store")
        expect(listed.locator(".tang-where")).to_have_text("Shared")
        expect(listed).to_contain_text("10 joins")
        tang = read_tang((team / "sample_store.tang").read_text(encoding="utf-8"))
        assert tang.tags == ("sales", "made-up") and len(tang.relationships) == 10
    finally:
        page.close()
        call(server, "POST", "/relationships", {"model": {"name": "empty"}, "replace": True})
        call(server, "PUT", "/blades/folders", {"folders": []})
        for url in urls:
            call(server, "DELETE", f"/grids/{grid_of(url)}")
