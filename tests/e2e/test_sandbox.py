# ruff: noqa: F811
"""The Sandbox in a real browser: the dashboard grid, resizing, and the Plotly, ECharts, Graphviz and
image cards, with what they may and may not do in the page.

Cards of the new kinds are served by a stand-in for GET /cards (the server learns those kinds in
the next step); chart cards are real. Made-up data only.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from playwright.sync_api import Browser, Page, Route, expect

# The grid tests' fixtures (browser, server, orders_url) are imported by name, which pytest needs and ruff
# reads as a redefinition in every test that takes them.
from tests.e2e.test_ui import browser, call, grid_of, open_agent, open_grid, orders_url, server  # noqa: F401

# A 2x1 PNG, red then steel: a picture as Python's savefig would send it.
PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000002000000010802000000"
        "7b40e8dd0000001049444154789c63f8cfc0c0a0a1a90100090e027e"
        "a9c0d1b50000000049454e44ae426082"
    )
).decode()


def card(kind: str, title: str, body: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"made-up-{kind}-{title.split()[0].lower()}",
        "kind": kind,
        "title": title,
        "body": body,
        "pinned": False,
        "created_by": "user",
        "grid": "g",
        "dataset": "orders",
    } | extra


def serve_cards(page: Page, cards: list[dict[str, Any]]) -> None:
    def answer(route: Route) -> None:
        if route.request.method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"cards": cards}))
        else:
            route.continue_()

    page.route("**/cards", answer)


def open_sandbox(browser: Browser, url: str, cards: list[dict[str, Any]]) -> Page:
    page = open_grid(browser, url, width=1440)
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.errors = errors  # type: ignore[attr-defined]
    serve_cards(page, cards)
    open_agent(page)
    return page


PLOTLY = json.dumps(
    {
        "data": [{"type": "bar", "x": ["Gizmo", "Widget", "Gadget"], "y": [12, 30, 21]}],
        "layout": {"title": {"text": "Made-up units"}, "images": [{"source": "https://example.com/x.png"}]},
    }
)
ECHARTS = json.dumps(
    {
        "xAxis": {"type": "category", "data": ["Mon", "Tue", "Wed"]},
        "yAxis": {"type": "value"},
        "series": [{"type": "line", "data": [3, 7, 5], "symbol": "image://https://example.com/pin.png"}],
        "graphic": [{"type": "image", "style": {"image": "https://example.com/track.png"}}],
        "tooltip": {"trigger": "axis", "renderMode": "html"},
    }
)
DOT = (
    'digraph orders { orders -> customers [label="customer_id"]; orders -> products; '
    'customers [URL="javascript:alert(1)"]; }'
)


def test_plotly_echarts_graphviz_and_image_cards_draw_and_load_nothing_from_outside(
    browser: Browser, orders_url: str
) -> None:
    page = open_sandbox(
        browser,
        orders_url,
        [
            card("plotly", "Plotly units", PLOTLY),
            card("echarts", "ECharts week", ECHARTS),
            card("graphviz", "Graphviz joins", DOT),
            card("image", "Matplotlib picture", f"data:image/png;base64,{PNG}"),
        ],
    )
    outside: list[str] = []
    page.on(
        "request",
        lambda r: (
            outside.append(r.url) if "127.0.0.1" not in r.url and not r.url.startswith("data:") else None
        ),
    )
    cards = page.locator(".cards > .card")
    expect(cards).to_have_count(4)
    plotly = page.locator(".card-plotly .figure")
    expect(plotly.locator("svg.main-svg").first).to_be_visible(timeout=20000)
    expect(plotly).to_contain_text("Made-up units")
    expect(page.locator(".card-echarts .figure canvas").first).to_be_visible(timeout=20000)
    graph = page.locator(".card-graphviz .diagram svg")
    expect(graph).to_be_visible(timeout=20000)
    expect(graph).to_contain_text("customers")
    # The DOT URL became no link at all.
    links = (
        "s => [...s.querySelectorAll('*')].flatMap(e => [...e.attributes]).filter(a => /href/i.test(a.name))"
    )
    assert graph.evaluate(links + ".every(a => a.value.startsWith('#'))")
    expect(page.locator(".card-image img")).to_have_attribute("alt", "Matplotlib picture")
    assert page.locator(".card-image img").evaluate("i => i.complete && i.naturalWidth === 2")
    page.wait_for_timeout(500)
    assert outside == [], outside
    assert page.errors == []  # type: ignore[attr-defined]
    page.close()


def test_a_figure_that_needs_the_internet_or_is_not_json_says_so(browser: Browser, orders_url: str) -> None:
    page = open_sandbox(
        browser,
        orders_url,
        [
            card(
                "plotly", "Plotly map", json.dumps({"data": [{"type": "scattergeo", "lat": [1], "lon": [2]}]})
            ),
            card("echarts", "ECharts broken", "{not json"),
            card("image", "Linked picture", "https://example.com/plot.png"),
        ],
    )
    expect(page.locator(".card-plotly")).to_contain_text("they load from the internet", timeout=20000)
    expect(page.locator(".card-echarts")).to_contain_text("not JSON", timeout=20000)
    expect(page.locator(".card-image")).to_contain_text("Unsupported image format")
    expect(page.locator(".card-image img")).to_have_count(0)
    page.close()


def test_cards_resize_on_the_dashboard_grid_and_the_chart_redraws_to_fit(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    gid = grid_of(orders_url)
    spec = {"mark": "bar", "x": {"field": "product"}, "y": {"aggregate": "count"}}
    added = call(
        server, "POST", f"/grids/{gid}/cards", {"kind": "chart", "title": "Units by product", "spec": spec}
    )
    page = open_grid(browser, orders_url, width=1440)
    open_agent(page)
    chart_card = page.locator(".cards > .card-chart")
    expect(chart_card.locator(".chart svg").first).to_be_visible(timeout=20000)
    assert chart_card.get_attribute("data-w") == "6" and chart_card.get_attribute("data-h") == "9"
    height_before = chart_card.locator(".chart svg").first.bounding_box()["height"]  # type: ignore[index]
    # Drag the corner grip to make it taller and wider; it snaps to the grid.
    grip = chart_card.locator(".card-grip")
    box = grip.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + 9, box["y"] + 9)
    page.mouse.down()
    page.mouse.move(box["x"] + 300, box["y"] + 260, steps=8)
    page.mouse.up()
    assert (
        int(chart_card.get_attribute("data-w") or 0) > 6 and int(chart_card.get_attribute("data-h") or 0) > 9
    )
    page.wait_for_timeout(800)
    assert chart_card.locator(".chart svg").first.bounding_box()["height"] > height_before + 100  # type: ignore[index]
    # The keyboard does the same, and the size is kept for this card.
    grip.focus()
    width = int(chart_card.get_attribute("data-w") or 0)
    page.keyboard.press("ArrowLeft")
    assert chart_card.get_attribute("data-w") == str(width - 1)
    page.reload()
    open_agent(page)
    expect(page.locator(".cards > .card-chart")).to_have_attribute("data-w", str(width - 1))
    call(server, "DELETE", f"/cards/{added['card']}")
    page.close()


def test_the_page_runs_no_script_a_card_could_inject(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url, width=1440)
    ran = page.evaluate(
        """async () => {
            window.__sag_ran = false;
            const probe = document.createElement('div');
            const handler = 'window.__sag_ran = true';
            const pixel = 'data:image/gif;base64,R0lGODlhAQABAAAAACw=';
            probe.innerHTML = `<img src="${pixel}" onerror="${handler}" onload="${handler}">`;
            document.body.append(probe);
            await new Promise((done) => setTimeout(done, 300));
            probe.remove();
            return window.__sag_ran;
        }"""
    )
    assert ran is False  # the page's Content Security Policy refuses inline handlers
    page.close()
