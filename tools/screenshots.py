"""Capture the screens a visual review needs, against the running server.

Pushes made-up data, sets up steps, charts and a note, then photographs each surface in Edge
(headless) into docs/screenshots/: the overview in each theme, the step form, the export dialog,
SQL results, the column profile, the agent approval, the bottom-docked panel, a zoomed chart,
a step preview and a narrow window.

    uv run python tools/screenshots.py
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from examples.push_demo import make_orders

from swissarmygrid.client import push_client

SHOTS = ROOT / "docs" / "screenshots"


def call(info: dict[str, Any], method: str, path: str, body: Any = None, agent: bool = False) -> Any:
    headers = {"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"}
    if agent:
        headers["X-Actor"] = "agent"
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=200) as response:  # noqa: S310
        return json.loads(response.read() or b"null")


def ready(page: Page) -> None:
    page.locator(".ag-row[row-index='0']").first.wait_for(timeout=20000)
    page.wait_for_timeout(900)


def main() -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    orders = make_orders(250_000, 7)
    url = push_client.push(orders, new_grid=True)
    info = push_client.ensure_server()
    grid = url.split("/g/")[1].split("#")[0]
    call(
        info,
        "POST",
        f"/grids/{grid}/steps",
        {
            "steps": [
                {"kind": "transform", "params": {"columns": ["region"], "fn": "trim_both"}},
                {"kind": "filter", "params": {"column": "quantity", "op": "gt", "value": 5}},
                {
                    "kind": "bucket",
                    "params": {
                        "column": "unit_price",
                        "as": "price_band",
                        "mode": "edges",
                        "edges": [0, 50, 100, 250, 500],
                    },
                },
                {"kind": "sort", "params": {"keys": [["unit_price", "desc"]]}},
            ]
        },
    )
    for card in (
        {
            "kind": "chart",
            "title": "Quantity by product and region",
            "spec": {
                "mark": "bar",
                "x": {"field": "product"},
                "y": {"aggregate": "sum", "field": "quantity"},
                "color": {"field": "region"},
            },
        },
        {
            "kind": "chart",
            "title": "Average price by month",
            "spec": {
                "mark": "line",
                "x": {"field": "order_date", "time_unit": "month"},
                "y": {"aggregate": "mean", "field": "unit_price"},
            },
        },
        {
            "kind": "note",
            "title": "Findings",
            "body": "**Gizmo** leads quantity in every region.\n\n- `north` and `North` look like one region: consider *Lower case*\n- Prices above 250 are 50% of rows after the filter",
        },
    ):
        call(info, "POST", f"/grids/{grid}/cards", card, agent=card["kind"] == "note")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)

        def set_theme(page: Page, theme: str) -> None:
            """Pick a theme in the Preferences window, then close it."""
            page.get_by_role("button", name="Preferences").click()
            page.locator(f"dialog input[name='theme'][value='{theme}']").check()
            page.keyboard.press("Escape")

        def page_for(width: int = 1600, height: int = 950, theme: str = "dark") -> Page:
            page = browser.new_context(viewport={"width": width, "height": height}).new_page()
            page.goto(url)
            ready(page)
            set_theme(page, theme)
            return page

        for theme in ("light", "dark", "hc-light", "hc-dark"):
            page = page_for(theme=theme)
            page.locator(".card .chart svg").first.wait_for(timeout=20000)
            page.wait_for_timeout(800)
            page.screenshot(path=str(SHOTS / f"overview-{theme}.png"))
            page.context.close()

        page = page_for()
        page.locator(".ribbon").get_by_role("button", name="Filter", exact=True).click()
        page.locator("dialog").get_by_label("Column", exact=True).select_option("region")
        page.locator("dialog").get_by_label("Condition").select_option("in")
        page.locator("dialog").get_by_label("Values, one per line").fill("North\nSouth")
        page.wait_for_timeout(300)
        page.screenshot(path=str(SHOTS / "step-form.png"))
        page.keyboard.press("Escape")
        page.get_by_role("button", name="Export", exact=True).click()
        page.wait_for_timeout(300)
        page.screenshot(path=str(SHOTS / "export-dialog.png"))
        page.keyboard.press("Escape")
        page.locator(".step-list .step .text").nth(3).click()  # preview the bucket step
        page.wait_for_timeout(1500)
        page.screenshot(path=str(SHOTS / "step-preview.png"))
        page.get_by_role("button", name="Back to the full view").click()
        page.locator(".ag-header-cell[col-id='unit_price'] .sag-header-profile").click()
        page.wait_for_timeout(1200)
        page.screenshot(path=str(SHOTS / "column-profile.png"))
        page.context.close()

        page = page_for()
        page.get_by_role("button", name="SQL", exact=True).click()
        page.locator(".sql-editor .monaco-editor").first.wait_for(timeout=20000)
        page.locator(".sql-editor .monaco-editor").first.click()
        page.keyboard.press("Control+A")
        page.keyboard.type(
            "SELECT region, product, count(*) AS orders, round(avg(unit_price), 2) AS avg_price\nFROM grid GROUP BY ALL ORDER BY orders DESC"
        )
        page.keyboard.press("Control+Enter")
        page.locator("#sql-result-bar").wait_for(timeout=15000)
        page.wait_for_timeout(600)
        page.screenshot(path=str(SHOTS / "sql-result.png"))
        page.context.close()

        call(info, "POST", "/agent/policy/orders", {"level": 1})
        page = page_for()
        worker = threading.Thread(
            target=lambda: call(
                info,
                "POST",
                "/agent/query",
                {
                    "dataset": "orders",
                    "sql": "SELECT region, product, unit_price FROM grid WHERE unit_price > 400 LIMIT 50",
                },
                agent=True,
            )
        )
        worker.start()
        page.locator(".approval").wait_for(timeout=15000)
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / "agent-approval.png"))
        page.get_by_role("button", name="Deny").click()
        worker.join(timeout=30)
        page.context.close()

        page = page_for(theme="light", height=1150)
        page.get_by_role("button", name="Dock the panel below the grid").click()
        page.locator(".card .chart svg").first.wait_for(timeout=20000)
        page.wait_for_timeout(800)
        page.screenshot(path=str(SHOTS / "panel-bottom.png"))
        page.get_by_role("button", name="Open large (zoom)").first.click()
        page.locator("dialog .expanded-chart svg").wait_for(timeout=15000)
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / "chart-zoom.png"))
        page.keyboard.press("Escape")
        page.get_by_role("button", name="Dock the panel to the right").click()
        page.context.close()

        page = browser.new_context(viewport={"width": 900, "height": 800}).new_page()
        page.goto(url)
        ready(page)
        page.screenshot(path=str(SHOTS / "narrow.png"))
        page.context.close()

        # Evidence the first review asked for.
        page = page_for(theme="dark")
        page.locator(".card").first.get_by_role("button", name="More chart actions").click()
        page.wait_for_timeout(300)
        page.screenshot(path=str(SHOTS / "chart-menu.png"))
        page.keyboard.press("Escape")
        page.locator("body").click(position={"x": 5, "y": 900})
        for _ in range(40):  # tab through the toolbar to Export, by keyboard
            page.keyboard.press("Tab")
            label = page.evaluate(
                "document.activeElement && document.activeElement.getAttribute('aria-label')"
            )
            if label == "Export":
                break
        page.wait_for_timeout(200)
        page.screenshot(
            path=str(SHOTS / "keyboard-focus.png"), clip={"x": 0, "y": 0, "width": 1600, "height": 420}
        )
        page.context.close()

        page = page_for(theme="light", height=1150)
        page.get_by_role("button", name="Dock the panel below the grid").click()
        page.get_by_role("button", name="Maximize the panel").click()
        page.locator(".card .chart svg").first.wait_for(timeout=20000)
        page.wait_for_timeout(700)
        page.screenshot(path=str(SHOTS / "panel-max-bottom.png"))
        page.get_by_role("button", name="Restore the panel").click()
        page.get_by_role("button", name="Dock the panel to the right").click()
        page.context.close()

        page = browser.new_context(viewport={"width": 480, "height": 860}).new_page()
        page.goto(url)
        ready(page)
        page.screenshot(path=str(SHOTS / "width-480.png"))
        page.context.close()

        page = page_for(theme="dark")
        page.get_by_role("button", name="Maximize the panel").click()
        page.locator(".card .chart svg").first.wait_for(timeout=20000)
        page.wait_for_timeout(700)
        page.screenshot(path=str(SHOTS / "panel-max.png"))
        page.get_by_role("button", name="Restore the panel").click()
        for _ in range(4):
            page.get_by_role("button", name="Zoom in the panel").click()
        page.locator(".card .chart svg").first.wait_for(timeout=20000)
        page.wait_for_timeout(700)
        page.screenshot(path=str(SHOTS / "panel-zoom-140.png"))
        for _ in range(4):
            page.get_by_role("button", name="Zoom out the panel").click()
        page.context.close()

        for theme in ("light", "hc-light", "hc-dark"):
            page = page_for(theme=theme)
            page.get_by_role("button", name="Export", exact=True).click()
            page.locator("dialog").get_by_label("Rows").select_option("columns")
            page.wait_for_timeout(300)
            page.screenshot(path=str(SHOTS / f"export-dialog-{theme}.png"))
            page.keyboard.press("Escape")
            page.locator(".ribbon").get_by_role("button", name="Filter", exact=True).click()
            page.locator("dialog").get_by_label("Column", exact=True).select_option("unit_price")
            page.locator("dialog").get_by_label("Condition").select_option("between")
            page.wait_for_timeout(300)
            page.screenshot(path=str(SHOTS / f"step-form-{theme}.png"))
            page.keyboard.press("Escape")
            page.context.close()
        browser.close()
    call(info, "DELETE", f"/grids/{grid}")
    print("\n".join(sorted(str(p.name) for p in SHOTS.glob("*.png"))))


if __name__ == "__main__":
    main()
