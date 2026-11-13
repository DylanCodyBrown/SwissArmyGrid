# ruff: noqa: F811
"""The Sandbox's own theme in the browser: chosen from its toolbar, applied to the Sandbox only (the grid
keeps the app's), a theme file imported and made readable, and back to following the app."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
from playwright.sync_api import Browser, expect

from swissarmygrid.client import push_client
from tests.e2e.test_ui import browser, call, open_grid, server  # noqa: F401


def test_the_sandbox_takes_a_theme_of_its_own(
    browser: Browser, server: dict[str, Any], tmp_path: Path
) -> None:
    page = open_grid(
        browser,
        push_client.push(pa.table({"n": [1, 2]}), name="themed", new_grid=True),
        width=1300,
        height=900,
    )
    try:
        call(server, "POST", "/cards", {"kind": "note", "title": "Themed note", "body": "Some **words**."})
        page.locator(".tab-agent").click()
        picker = page.get_by_label("Sandbox theme")
        picker.select_option("hc-dark")
        body = page.locator("#ai-panel-body")
        expect(body).to_have_attribute("data-sandbox-theme", "hc-dark", timeout=20000)
        assert body.evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(0, 0, 0)"
        assert (
            body.locator(".card h3").first.evaluate("e => getComputedStyle(e).color") == "rgb(255, 255, 255)"
        )
        assert (
            page.evaluate("getComputedStyle(document.body).backgroundColor") != "rgb(0, 0, 0)"
        )  # the app keeps its own

        theme = tmp_path / "harbour.theme.json"
        theme.write_text(
            json.dumps({"name": "Harbour test", "dark": False, "tokens": {"bg": "#f4f8fb", "fg": "#9aa5b1"}})
        )
        page.get_by_label("Theme file").set_input_files(str(theme))
        expect(page.locator(".toast").last).to_contain_text("Harbour test imported")
        expect(page.locator("#ai-panel-body")).to_have_attribute(
            "data-sandbox-theme", "custom-harbour-test", timeout=20000
        )
        ink = page.locator("#ai-panel-body").evaluate("e => getComputedStyle(e).color")
        assert ink != "rgb(154, 165, 177)"  # the pale text was darkened to stay readable

        page.get_by_label("Sandbox theme").select_option("app")
        expect(page.locator("#ai-panel-body")).not_to_have_attribute(
            "data-sandbox-theme", "custom-harbour-test", timeout=20000
        )
    finally:
        page.close()
        call(server, "PUT", "/sandbox/theme", {"theme": "app"})
        call(server, "DELETE", "/sandbox/themes/custom-harbour-test")
