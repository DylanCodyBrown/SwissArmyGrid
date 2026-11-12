# ruff: noqa: F811
"""The notebook tab in a real browser: marimo through SAG, the rail beside it, drops and Save as blade.

Made-up data only. Every test shares the e2e server's one notebook, so each looks at what it added.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from playwright.sync_api import Browser, FrameLocator, Page, expect

# The grid tests' fixtures (browser, server, orders_url) are imported by name, which pytest needs and ruff
# reads as a redefinition in every test that takes them.
from tests.e2e.test_ui import browser, call, open_grid, orders_url, server  # noqa: F401

DRAG_ONTO_LAST_CELL = """(selector) => {
    const t = new DataTransfer();
    const start = new DragEvent('dragstart', {dataTransfer: t, bubbles: true});
    document.querySelector(selector).dispatchEvent(start);
    const doc = document.querySelector('.nb-frame').contentDocument;
    const cells = doc.querySelectorAll('.cm-content');
    const drop = new DragEvent('drop', {dataTransfer: t, bubbles: true, cancelable: true});
    cells[cells.length - 1].dispatchEvent(drop);
    return [t.getData('text/plain'), t.getData('application/x-sag-drop')];
}"""


@pytest.fixture
def bakery(server: dict[str, Any]) -> Iterator[Path]:
    path = Path(server["tmp"]) / "nb_bakery.sqlite"
    with sqlite3.connect(path) as setup:
        setup.execute("CREATE TABLE IF NOT EXISTS loaves (loaf_id INTEGER, crust TEXT)")
    code = f"import sqlite3\nconn = sqlite3.connect({str(path)!r})\n"
    catalog = {
        "language": "sql",
        "command": "SELECT 'main' AS table_schema, m.name AS table_name, p.name AS column_name, "
        "p.type AS data_type "
        "FROM sqlite_master m JOIN pragma_table_info(m.name) p",
    }
    call(
        server,
        "POST",
        "/blades",
        {"name": "Bakery", "kind": "sql", "dialect": "sqlite", "code": code, "catalog": catalog},
    )
    call(server, "POST", "/blades/Bakery/refresh", {"env": {}})
    yield path
    for blade in call(server, "GET", "/blades")["blades"]:
        call(server, "DELETE", f"/blades/{quote(blade['name'])}")
    for catalog_info in call(server, "GET", "/catalogs")["catalogs"]:
        call(server, "DELETE", f"/catalogs/{quote(catalog_info['name'])}")


def open_notebook(browser: Browser, server: dict[str, Any]) -> tuple[Page, FrameLocator]:
    page = browser.new_page(viewport={"width": 1500, "height": 900})
    page.goto(f"http://127.0.0.1:{server['port']}/notebook#token={server['token']}")
    notebook = page.frame_locator(".nb-frame")
    notebook.locator(".cm-content").first.wait_for(timeout=60_000)
    page.locator(".blade-item").first.wait_for()
    return page, notebook


def cells(notebook: FrameLocator) -> list[str]:
    return [text.strip() for text in notebook.locator(".cm-content").all_inner_texts()]


def test_the_notebook_opens_through_sag_beside_the_rail(
    browser: Browser, server: dict[str, Any], bakery: Path
) -> None:
    page, notebook = open_notebook(browser, server)
    assert cells(notebook)[0] == "import marimo as mo\nimport swissarmygrid as sag"
    # marimo is served from SAG's own address: the page can read inside the frame.
    assert page.evaluate("document.querySelector('.nb-frame').contentWindow.location.pathname") == "/marimo/"
    expect(page.locator(".blade-item")).to_contain_text("Bakery")
    expect(page.get_by_role("button", name="Save as blade")).to_be_visible()
    page.close()


def test_a_blade_dropped_on_the_notebook_becomes_its_line(
    browser: Browser, server: dict[str, Any], bakery: Path
) -> None:
    page, notebook = open_notebook(browser, server)
    before = len(cells(notebook))
    text, dropped = page.evaluate(DRAG_ONTO_LAST_CELL, ".blade-item")
    assert text == 'conn = sag.blade("Bakery")' and '"import swissarmygrid as sag"' in dropped
    expect(notebook.locator(".cm-content", has_text='conn = sag.blade("Bakery")')).to_have_count(1)
    assert len(cells(notebook)) in (before, before + 1)  # into the empty cell, or a new one after it
    expect(page.locator(".toast").last).to_contain_text("Added to the notebook")
    page.close()


def test_a_table_dropped_on_the_notebook_is_a_sql_cell_over_its_blade(
    browser: Browser, server: dict[str, Any], bakery: Path
) -> None:
    page, notebook = open_notebook(browser, server)
    page.locator('select[aria-label="Catalog"]').select_option("Bakery")
    table = page.locator(
        '[role="treeitem"]', has=page.locator(':scope > .node > .node-label:text-is("loaves")')
    )
    table.locator(":scope > .node").click()  # open it, so the SELECT names its columns
    expect(page.locator('[role="treeitem"][aria-level="3"]')).to_have_count(2)
    page.keyboard.press("Escape")  # the table's details open in a dialog on this page
    text, _ = page.evaluate(DRAG_ONTO_LAST_CELL, '[role="treeitem"][aria-level="2"]')
    assert (
        text == 'loaves_rows = mo.sql(\n    """\nSELECT "loaf_id", "crust"\nFROM "main"."loaves"\n'
        'LIMIT 100\n""",\n    engine=conn,\n)'
    )
    # marimo shows `x = mo.sql("""...""", engine=conn)` as one of its own SQL cells: the SQL itself.
    expect(notebook.locator(".cm-content", has_text='FROM "main"."loaves"')).to_have_count(1)
    # A column is a name inside a line: an ordinary text drop, not a cell.
    column = page.evaluate(
        """() => {
            const t = new DataTransfer();
            const column = document.querySelector('[role=treeitem][aria-level="3"]');
            column.dispatchEvent(new DragEvent('dragstart', {dataTransfer: t, bubbles: true}));
            return [t.getData('text/plain'), t.getData('application/x-sag-drop')];
        }"""
    )
    assert column == ['"loaf_id"', ""]
    page.close()


def test_save_as_blade_takes_the_highlighted_code_and_guesses_its_kind(
    browser: Browser, server: dict[str, Any], bakery: Path
) -> None:
    page, notebook = open_notebook(browser, server)
    page.get_by_role("button", name="Save as blade").click()
    expect(page.locator(".toast").last).to_contain_text("Highlight the code")
    last = notebook.locator(".cm-content").last
    last.click()
    page.keyboard.press("Control+a")
    page.keyboard.press("Delete")
    page.keyboard.type("import sqlite3; pantry = sqlite3.connect('pantry.db'")
    page.keyboard.press("Escape")  # close any completion popup
    last.click()
    page.keyboard.press("Control+a")
    page.get_by_role("button", name="Save as blade").click()
    dialog = page.locator("dialog")
    expect(dialog.get_by_label("Blade code")).to_have_value(
        "import sqlite3; pantry = sqlite3.connect('pantry.db')"
    )
    # The driver says what it is: a SQL connection, SQLite's dialect, the variable it makes.
    expect(dialog.get_by_role("button", name="SQL connection")).to_have_attribute("aria-pressed", "true")
    expect(dialog.get_by_label("SQL dialect", exact=True)).to_have_value("sqlite")
    expect(dialog.get_by_label("The variable it makes")).to_have_value("pantry")
    dialog.get_by_label("Blade name").fill("Pantry")
    dialog.get_by_role("button", name="Save", exact=True).click()
    expect(dialog.locator(".status-line")).to_contain_text("Saved Pantry")
    expect(page.locator(".blade-item", has_text="Pantry")).to_be_visible()
    page.close()


def test_the_grid_page_opens_the_notebook_in_its_own_tab(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    with page.context.expect_page() as opened:
        page.get_by_role("button", name="Notebook").click()
    notebook = opened.value
    notebook.wait_for_load_state()
    assert notebook.url.split("#")[0].endswith("/notebook")
    notebook.frame_locator(".nb-frame").locator(".cm-content").first.wait_for(timeout=60_000)
    notebook.close()
