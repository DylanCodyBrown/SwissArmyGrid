# ruff: noqa: F811
"""The blades and catalog page in a real browser: the tree, search, the save screen and Refresh.

Made-up data only. No test saves a secret: on Windows that would reach the real Credential Manager.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from playwright.sync_api import Browser, Page, expect

# The grid tests' fixtures (browser, server, orders_url) are imported by name, which pytest needs and ruff
# reads as a redefinition in every test that takes them.
from tests.e2e.test_ui import browser, call, open_grid, orders_url, server  # noqa: F401

ROWS = [
    [f"schema_{i % 3}", f"table_{i % 700:03}", f"column_{i}", "int" if i % 2 else "varchar"]
    for i in range(2100)
]
ROWS += [["sales", "orders", "order_id", "int"], ["sales", "orders", "placed at", "datetime2"]]


@pytest.fixture
def warehouse(server: dict[str, Any]) -> Iterator[str]:
    call(
        server,
        "PUT",
        "/catalogs/Warehouse?dialect=sqlserver",
        {"columns": ["TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME", "DATA_TYPE"], "data": ROWS},
    )
    yield f"http://127.0.0.1:{server['port']}/catalog#token={server['token']}&catalog=Warehouse"
    for blade in call(server, "GET", "/blades")["blades"]:
        call(server, "DELETE", f"/blades/{quote(blade['name'])}")
    for catalog in call(server, "GET", "/catalogs")["catalogs"]:
        call(server, "DELETE", f"/catalogs/{quote(catalog['name'])}")


def open_page(browser: Browser, url: str) -> Page:
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url)
    page.wait_for_selector('[role="treeitem"]')
    page.errors = errors  # type: ignore[attr-defined]
    return page


def item(page: Page, label: str) -> Any:
    return page.locator(
        '[role="treeitem"]', has=page.locator(f':scope > .node > .node-label:text-is("{label}")')
    )


def test_the_tree_opens_lazily_and_shows_a_table_with_its_select(browser: Browser, warehouse: str) -> None:
    page = open_page(browser, warehouse)
    schemas = page.locator('[role="treeitem"][aria-level="1"]')
    expect(schemas).to_have_count(4)
    sales = item(page, "sales")
    sales.locator(":scope > .node").click()
    expect(sales).to_have_attribute("aria-expanded", "true")
    item(page, "orders").locator(":scope > .node").click()
    expect(page.locator("#detail pre")).to_have_text(
        "SELECT TOP 100 [order_id], [placed at]\nFROM [sales].[orders]"
    )
    expect(item(page, "placed at").locator(".node-meta")).to_have_text("datetime2")
    # Columns are not loaded until a table opens: schema_0 holds 700 tables, shown 500 at a time.
    item(page, "schema_0").locator(":scope > .node").click()
    expect(page.locator(".node.more")).to_have_count(1)
    expect(page.locator(".node.more")).to_contain_text("Show more (200 left)")
    page.locator(".node.more").click()
    expect(page.locator('[role="treeitem"][aria-level="2"]')).to_have_count(1 + 700)
    assert page.errors == []  # type: ignore[attr-defined]
    page.close()


def test_the_keyboard_walks_the_tree(browser: Browser, warehouse: str) -> None:
    page = open_page(browser, warehouse)
    page.locator('[role="tree"]').focus()
    for key in ["Home", "ArrowRight", "ArrowDown", "ArrowRight", "ArrowDown"]:  # sales -> orders -> order_id
        page.keyboard.press(key)
        page.wait_for_timeout(150)  # each expand loads its children
    selected = page.locator('[role="treeitem"][aria-selected="true"] > .node > .node-label')
    expect(selected).to_have_text("order_id")
    page.keyboard.press("ArrowLeft")
    expect(selected).to_have_text("orders")
    page.keyboard.press("ArrowLeft")
    expect(page.locator('[role="treeitem"][aria-selected="true"]')).to_have_attribute(
        "aria-expanded", "false"
    )
    page.close()


def test_search_finds_a_column_and_opens_the_tree_at_it(browser: Browser, warehouse: str) -> None:
    page = open_page(browser, warehouse)
    page.get_by_role("searchbox").fill("placed")
    hit = page.locator(".cat-tree button", has_text="orders.placed at")
    expect(hit).to_be_visible()
    hit.click()
    expect(page.locator('[role="treeitem"][aria-selected="true"] > .node > .node-label')).to_have_text(
        "placed at"
    )
    expect(page.locator("#detail h2")).to_contain_text("orders")
    # A table past the first page of its schema is revealed too.
    page.get_by_role("searchbox").fill("table_650")
    page.locator(".cat-tree button", has_text="schema_2.table_650").click()
    expect(page.locator('[role="treeitem"][aria-selected="true"] > .node > .node-label')).to_have_text(
        "table_650"
    )
    page.close()


def test_dragging_gives_the_quoted_name_or_the_blade_line(
    browser: Browser, warehouse: str, server: dict[str, Any]
) -> None:
    call(server, "POST", "/blades", {"name": "Bakery", "kind": "other", "code": "path = 'bakery.csv'\n"})
    page = open_page(browser, warehouse)
    item(page, "sales").locator(":scope > .node").click()
    item(page, "orders").locator(":scope > .node").click()
    expect(item(page, "placed at")).to_be_visible()
    dragged = page.evaluate(
        """() => {
            const pick = (el) => {
                const t = new DataTransfer();
                el.dispatchEvent(new DragEvent('dragstart', {dataTransfer: t, bubbles: true}));
                return t.getData('text/plain');
            };
            const label = (text) => [...document.querySelectorAll('[role=treeitem]')]
                .find(i => i.querySelector(':scope > .node > .node-label').textContent === text);
            const blade = document.querySelector('.blade-item');
            return [pick(label('orders')), pick(label('placed at')), pick(blade)];
        }"""
    )
    assert dragged == ["[sales].[orders]", "[placed at]", 'path = sag.blade("Bakery")']
    page.close()


def test_a_new_sqlite_blade_saves_refreshes_and_fills_the_catalog(
    browser: Browser, warehouse: str, server: dict[str, Any]
) -> None:
    database = Path(server["tmp"]) / "bakery.sqlite"
    with sqlite3.connect(database) as setup:
        setup.execute("CREATE TABLE IF NOT EXISTS loaves (loaf_id INTEGER, crust TEXT)")
    page = open_page(browser, warehouse)
    page.get_by_role("button", name="New blade").first.click()
    page.get_by_role("button", name="SQLite", exact=True).click()
    page.get_by_label("Blade name").fill("Bakery")
    code = page.get_by_label("Blade code")
    code.fill(code.input_value().replace(r"C:\data\my_database.sqlite", str(database)))
    expect(page.get_by_label("The variable it makes")).to_have_value("conn")
    page.get_by_role("button", name="Save and refresh the catalog").click()
    expect(page.locator(".status-line")).to_contain_text("Refreshed: 1 tables, 2 columns")
    expect(page.locator(".blade-item")).to_contain_text("Bakery")
    page.locator('select[aria-label="Catalog"]').select_option("Bakery")
    # One schema: it opens by itself.
    expect(item(page, "main")).to_have_attribute("aria-expanded", "true")
    item(page, "loaves").locator(":scope > .node").click()
    expect(page.locator("#detail pre")).to_have_text(
        'SELECT "loaf_id", "crust"\nFROM "main"."loaves"\nLIMIT 100'
    )
    page.close()


def test_a_refresh_asks_for_a_secret_blade_value_and_never_keeps_it(
    browser: Browser,
    warehouse: str,
    server: dict[str, Any],
) -> None:
    database = Path(server["tmp"]) / "ovens.sqlite"
    with sqlite3.connect(database) as setup:
        setup.execute("CREATE TABLE IF NOT EXISTS ovens (oven_id INTEGER)")
    call(server, "POST", "/blades", {"name": "Oven login", "kind": "secret", "env_names": ["OVEN_PASSWORD"]})
    code = (
        "import sqlite3\nassert secret('OVEN_PASSWORD') == 'proof-42'\n"
        f"conn = sqlite3.connect({str(database)!r})\n"
    )
    blade = {
        "name": "Ovens",
        "kind": "sql",
        "dialect": "sqlite",
        "code": code,
        "catalog": {
            "language": "sql",
            "command": "SELECT name AS table_name, 'oven_id' AS column_name FROM sqlite_master",
        },
    }
    call(server, "POST", "/blades", blade)
    page = open_page(browser, warehouse)
    page.locator(".blade-item", has_text="Ovens").get_by_role("button", name="Refresh catalog").click()
    dialog = page.locator("dialog")
    expect(dialog).to_contain_text("OVEN_PASSWORD")
    dialog.get_by_label("OVEN_PASSWORD").fill("proof-42")
    dialog.get_by_role("button", name="Run").click()
    expect(page.locator(".toast").last).to_contain_text("Ovens: 1 tables")
    # Never kept: the next refresh asks again.
    page.locator(".blade-item", has_text="Ovens").get_by_role("button", name="Refresh catalog").click()
    expect(page.locator("dialog")).to_contain_text("OVEN_PASSWORD")
    page.close()


def test_the_page_follows_the_theme(browser: Browser, warehouse: str) -> None:
    page = open_page(browser, warehouse)
    page.evaluate("localStorage.setItem('swissarmygrid.theme', 'hc-dark')")
    page.reload()
    page.goto(warehouse)
    expect(page.locator("html")).to_have_attribute("data-theme", "hc-dark")
    page.evaluate("localStorage.removeItem('swissarmygrid.theme')")
    page.close()


def test_the_grid_page_opens_the_blades_page_in_its_own_tab(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    with page.context.expect_page() as opened:
        page.get_by_role("button", name="Blades and catalog").click()
    blades = opened.value
    blades.wait_for_load_state()
    assert blades.url.split("#")[0].endswith("/catalog")
    expect(blades.get_by_role("heading", name="Blades", exact=True)).to_be_visible()
    blades.close()
