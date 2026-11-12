"""End-to-end: every page feature, driven in the installed Microsoft Edge (headless).

A real server process runs with its connection file and state in a temp folder; data arrives
through the real push client; Playwright clicks through the page like a user and checks what
it shows and what the server now holds. Screenshots of each theme land in docs/screenshots/ for
the visual review.

Run just these with: uv run pytest tests/e2e -q
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import pytest
from playwright.sync_api import Browser, Error, Locator, Page, expect, sync_playwright

from swissarmygrid.client import push_client

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "docs" / "screenshots"
ROWS = 5000


def demo_frame(rows: int = ROWS) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "order_id": range(1, rows + 1),
            "region": [("North", "South", "East", "West", " Central ", "north")[i % 6] for i in range(rows)],
            "product": [("Widget", "Gadget", "Gizmo", "Doohickey")[i % 4] for i in range(rows)],
            "quantity": [(i * 7) % 50 + 1 for i in range(rows)],
            "unit_price": [round(((i * 7919) % 50000) / 100, 2) for i in range(rows)],
            "order_date": [date(2026, 1, 1) + timedelta(days=i % 365) for i in range(rows)],
            "note": [None if i % 17 == 0 else ("x" * 700 if i == 3 else f"note {i}") for i in range(rows)],
        }
    )


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    tmp = tmp_path_factory.mktemp("e2e")
    env = os.environ | {
        "SWISSARMYGRID_INFO": str(tmp / "server.json"),
        "SWISSARMYGRID_STATE": str(tmp / "state"),
    }
    saved = {key: os.environ.get(key) for key in ("SWISSARMYGRID_INFO", "SWISSARMYGRID_STATE")}
    for key in saved:
        os.environ[key] = env[key]
    process = subprocess.Popen([sys.executable, "-m", "swissarmygrid.server", "--port", "0"], env=env)
    for _ in range(100):
        try:
            info = json.loads((tmp / "server.json").read_text())
            break
        except (OSError, ValueError):
            time.sleep(0.1)
    else:
        process.kill()
        raise AssertionError("server did not start")
    yield info | {"tmp": tmp}
    process.terminate()
    process.wait(timeout=20)
    for key, value in saved.items():  # leave the environment as we found it
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        # SAG_HEADED=1 shows the browser so a person can watch; SAG_SLOWMO pauses between
        # actions (milliseconds, 350 by default when headed).
        headed = os.environ.get("SAG_HEADED") == "1"
        slow = int(os.environ.get("SAG_SLOWMO", "350" if headed else "0"))
        instance = p.chromium.launch(channel="msedge", headless=not headed, slow_mo=slow)
        yield instance
        instance.close()


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


def open_grid(browser: Browser, url: str, width: int = 1920, height: int = 900, **context: Any) -> Page:
    page = browser.new_context(
        viewport={"width": width, "height": height}, accept_downloads=True, **context
    ).new_page()
    page.goto(url)
    expect(page.locator(".ag-row[row-index='0']").first).to_be_visible(timeout=20000)
    return page


def open_steps(browser: Browser, grid_url: str) -> Page:
    """The grid's steps window: the Table Transformations panel in a tab of its own (plan decision 13)."""
    page = browser.new_page(viewport={"width": 520, "height": 900})
    page.goto(grid_url.replace("#", "?panel=steps#", 1))
    expect(page.locator(".steps-panel .step.source")).to_be_visible(timeout=15000)
    return page


def open_agent(page: Page) -> Locator:
    """The Agent tab: the Agent panel lives only there."""
    page.locator(".tab-agent").click()
    return page.locator("#ai-panel")


def back_to_grid(page: Page, url: str) -> None:
    page.locator(f".tab-item[data-grid='{grid_of(url)}']").click()


def set_theme(page: Page, theme: str) -> None:
    """Pick a theme in the Settings window, then close it."""
    page.get_by_role("button", name="Settings").click()
    page.locator(f"dialog input[name='theme'][value='{theme}']").check()
    page.keyboard.press("Escape")


def cell(page: Page, row: int, column: str) -> str:
    return page.locator(f".ag-row[row-index='{row}'] .ag-cell[col-id='{column}']").inner_text()


def wait_for_cell(page: Page, row: int, column: str, value: str) -> None:
    expect(page.locator(f".ag-row[row-index='{row}'] .ag-cell[col-id='{column}']")).to_have_text(
        value, timeout=15000
    )


def grid_of(url: str) -> str:
    return url.split("/g/")[1].split("#")[0]


@pytest.fixture(autouse=True)
def close_pages(browser: Browser) -> Iterator[None]:
    """Close every page a test opened. Each open workspace page stays connected and reacts to
    every push, so without this the later tests ran against dozens of live pages and timed out."""
    yield
    for context in list(browser.contexts):
        context.close()


@pytest.fixture
def orders_url(server: dict[str, Any]) -> Iterator[str]:
    """A fresh grid on "orders" for each test (a plain re-push would reuse the first grid,
    steps and all), closed afterwards so the 20-grid limit is never reached."""
    orders = demo_frame()
    url = push_client.push(orders, new_grid=True)
    yield url
    call(server, "DELETE", f"/grids/{grid_of(url)}")


# ===== the grid =====


def test_grid_renders_with_title_rows_and_header_stats(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    assert page.title() == "SwissArmyGrid"  # the tab bar names the grid, not the browser tab
    expect(page.locator("#grid-title")).to_contain_text("orders")
    wait_for_cell(page, 0, "order_id", "1")
    expect(page.locator("#status")).to_contain_text("of 5,000")
    # Column profiles in the headers are off until turned on in Settings.
    assert page.locator(".sag-header-stats").count() == 0
    page.get_by_role("button", name="Settings").click()
    page.locator("dialog").get_by_role("checkbox", name=re.compile("column profiles")).check()
    page.keyboard.press("Escape")
    expect(page.locator(".ag-header-cell[col-id='region'] .sag-header-stats")).to_contain_text(
        "distinct", timeout=10000
    )
    page.get_by_role("button", name="Settings").click()
    page.locator("dialog").get_by_role("checkbox", name=re.compile("column profiles")).uncheck()
    page.keyboard.press("Escape")
    expect(page.locator(".sag-header-stats")).to_have_count(0)
    expect(page.locator("#live")).to_contain_text("live")
    wait_for_cell(page, 0, "note", "(blank)")  # blanks carry text, not just color


def test_header_click_sorts_on_the_server(browser: Browser, orders_url: str, server: dict[str, Any]) -> None:
    page = open_grid(browser, orders_url)
    # Sorting is the small arrow button in the header; a click on the name selects the column.
    header = page.locator(".ag-header-cell[col-id='unit_price'] .sag-header-sort")
    header.click()
    header.click()
    wait_for_cell(page, 0, "unit_price", str(demo_frame()["unit_price"].max()))
    grid = orders_url.split("/g/")[1].split("#")[0]
    steps = call(server, "GET", f"/grids/{grid}")["steps"]
    assert steps[-1]["params"] == {"keys": [["unit_price", "desc"]], "header": True}


def test_truncated_cell_opens_full_detail(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    target = page.locator(".ag-row[row-index='3'] .ag-cell[col-id='note']")
    expect(target).to_have_class(re.compile("cell-truncated"))
    target.dblclick()
    expect(page.locator("dialog pre")).to_have_text("x" * 700)


def test_zoom_changes_row_height_and_is_remembered(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    height = (
        "document.querySelector('.ag-center-cols-viewport .ag-row, .ag-row').getBoundingClientRect().height"
    )
    before = page.evaluate(height)
    page.keyboard.press("Control+Equal")
    page.keyboard.press("Control+Equal")
    expect(page.locator(".zoom-control output").first).to_have_text("125%")
    page.wait_for_function(f"{height} > {before}", timeout=10000)
    page.keyboard.press("Control+0")
    expect(page.locator(".zoom-control output").first).to_have_text("100%")
    page.reload()
    expect(page.locator(".zoom-control output").first).to_have_text("100%")


# ===== steps =====


def test_steps_add_filter_preview_toggle_and_delete(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    page.locator(".ribbon").get_by_role("button", name="Filter", exact=True).click()
    dialog = page.locator("dialog")
    dialog.get_by_label("Column", exact=True).select_option("region")
    dialog.get_by_label("Condition").select_option("eq")
    dialog.get_by_label("Value", exact=True).fill("North")
    dialog.get_by_role("button", name="Add step").click()
    expect(page.locator("#status")).to_contain_text("of 834")
    # The grid page shows the steps as a strip; the panel opens in a tab of its own.
    expect(page.locator(".steps-strip .steps-count")).to_have_text("1 step")
    steps = open_steps(browser, orders_url)
    expect(steps.locator(".step-list .step .text").nth(1)).to_have_text(
        "Keep rows where region equals 'North'"
    )
    expect(steps.locator(".step .meta").first).to_contain_text("5,000 \u2192 834 rows")
    # Turn the step off there: the grid page follows the server.
    steps.get_by_label("Step 1 on").uncheck()
    expect(page.locator("#status")).to_contain_text("of 5,000")
    expect(page.locator(".steps-strip .steps-count")).to_have_text("No steps")
    steps.get_by_role("button", name="Delete step 1").click()
    expect(steps.locator(".step-list .step")).to_have_count(1)  # only the source remains
    # Undo on the strip drops the last step.
    page.locator(".ribbon").get_by_role("button", name="Filter", exact=True).click()
    dialog = page.locator("dialog")
    dialog.get_by_label("Column", exact=True).select_option("region")
    dialog.get_by_label("Condition").select_option("eq")
    dialog.get_by_label("Value", exact=True).fill("North")
    dialog.get_by_role("button", name="Add step").click()
    expect(page.locator("#status")).to_contain_text("of 834")
    page.get_by_role("button", name="Undo the last step").click()
    expect(page.locator("#status")).to_contain_text("of 5,000")
    steps.close()


def test_case_toggle_changes_matching(browser: Browser, orders_url: str, server: dict[str, Any]) -> None:
    grid = orders_url.split("/g/")[1].split("#")[0]
    call(
        server,
        "POST",
        f"/grids/{grid}/steps",
        {"steps": [{"kind": "filter", "params": {"column": "region", "op": "eq", "value": "north"}}]},
    )
    page = open_grid(browser, orders_url)
    expect(page.locator("#status")).to_contain_text("of 833")
    page.locator(".ribbon-group").first.get_by_role("button", name=re.compile("^Matching")).click()
    page.locator("dialog").get_by_role("checkbox", name=re.compile("Match case")).uncheck()  # ignore case
    page.keyboard.press("Escape")
    # The Table row's button says what is on now.
    expect(page.locator(".ribbon-group").first).to_contain_text("Matching: ignore case")
    expect(page.locator("#status")).to_contain_text("of 1,667")


def test_bucket_and_categorize_forms(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    # Column tools act on the selected column: click its header first.
    page.locator(".ag-header-cell[col-id='quantity'] .sag-header-name").click()
    page.get_by_role("button", name="Bucket…").click()
    dialog = page.locator("dialog")
    dialog.get_by_label("Column", exact=True).select_option("quantity")
    dialog.get_by_label("Bucket by").select_option("edges")
    dialog.get_by_label("Edges").fill("0, 10, 25")
    dialog.get_by_role("button", name="Add step").click()
    expect(page.locator(".ag-header-cell[col-id='quantity_bucket']")).to_be_visible()
    wait_for_cell(page, 0, "quantity_bucket", "[0, 10)")

    # The new column was scrolled into view; scroll back to the start to reach region.
    page.wait_for_timeout(600)  # let the grid finish scrolling the new column in
    page.locator(".ag-body-horizontal-scroll-viewport").evaluate("e => e.scrollLeft = 0")
    page.wait_for_timeout(300)
    page.locator(".ag-header-cell[col-id='region'] .sag-header-name").click()
    page.get_by_role("button", name="Categorize…").click()
    dialog = page.locator("dialog")
    dialog.get_by_label("Column", exact=True).select_option("region")
    dialog.get_by_label("New column name").fill("zone")
    dialog.get_by_label("Rule value").first.fill("%orth%")
    dialog.get_by_label("Label").first.fill("Northern")
    dialog.get_by_role("button", name="Add step").click()
    expect(page.locator(".ag-header-cell[col-id='zone']")).to_be_visible()
    steps = open_steps(browser, orders_url)
    expect(steps.locator("[data-categorize]").last).to_contain_text("rows matched no rule", timeout=10000)
    steps.close()


def test_profile_click_value_adds_a_filter(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    # The Column row's Profile: a one-time popup over the grid; the grid tab stays open behind it.
    page.locator(".ag-header-cell[col-id='product'] .sag-header-name").click()
    page.locator(".ribbon-group").nth(1).get_by_role("button", name="Profile", exact=True).click()
    expect(page.locator(".sag-header-profile")).to_have_count(0)  # no profile button in the headers
    dialog = page.locator("dialog")
    expect(dialog.locator(".stat-grid")).to_contain_text("Distinct")
    expect(dialog.locator(".dialog-head h2")).to_have_text("Profile of product")
    expect(page.locator(".workspace")).not_to_have_attribute("data-view", "agent")
    dialog.locator(".bar-row", has_text="Gizmo").click()
    expect(page.locator("#status")).to_contain_text("of 1,250")
    dialog.get_by_role("button", name="Close").first.click()
    # The Column row's Profile opens the same popup for the selected column.
    page.locator(".ag-header-cell[col-id='region'] .sag-header-name").click()
    page.locator(".ribbon-group").nth(1).get_by_role("button", name="Profile", exact=True).click()
    expect(page.locator("dialog .dialog-head h2")).to_have_text("Profile of region")
    page.locator("dialog").get_by_role("button", name="Close").first.click()
    page.locator(".ribbon-group").first.get_by_role("button", name="Profile", exact=True).click()
    expect(page.locator("dialog table.small")).to_contain_text("unit_price")


def test_copy_as_sql_and_python_run_on_the_users_own_table(
    browser: Browser, server: dict[str, Any], orders_url: str
) -> None:
    grid = grid_of(orders_url)
    steps = [
        {"kind": "filter", "params": {"column": "quantity", "op": "gt", "value": 10}},
        {"kind": "sort", "params": {"keys": [["unit_price", "desc"]]}},
        {
            "kind": "sql",
            "params": {"sql": "SELECT order_id, region, quantity * unit_price AS total FROM orders"},
        },
        {"kind": "limit", "params": {"n": 40}},
    ]
    call(server, "POST", f"/grids/{grid}/steps", {"steps": steps})
    page = open_grid(browser, orders_url)
    page.evaluate("() => { navigator.clipboard.writeText = async (text) => { window.__copied = text; }; }")
    strip = page.locator(".steps-strip")
    expect(strip.locator(".steps-count")).to_have_text("4 steps")
    strip.get_by_role("button", name="Copy as SQL").click()
    page.wait_for_function("window.__copied !== undefined", timeout=30000)
    sql = page.evaluate("window.__copied")
    page.evaluate("() => { window.__copied = undefined; }")
    strip.get_by_role("button", name="Copy as Python").click()
    page.wait_for_function("window.__copied !== undefined", timeout=30000)
    code = page.evaluate("window.__copied")
    assert code.startswith('import duckdb\n\norders_view = duckdb.sql("""')
    assert "__sag" not in code and sql.strip() in code

    # Pasted where the user works: only their table, by its name.
    orders = demo_frame()
    namespace: dict[str, Any] = {"orders": orders}
    exec(compile(code, "<pasted>", "exec"), namespace)  # noqa: S102 (the copied code under test)
    pasted = namespace["orders_view"]
    grid_rows = call(server, "GET", f"/grids/{grid}/rows?start=0&end=100")["rows"]
    assert pasted["order_id"].tolist() == [r[0] for r in grid_rows]
    assert pasted["total"].round(6).tolist() == [round(r[2], 6) for r in grid_rows]
    relation = duckdb.sql(sql)  # "Copy as SQL" is the same one query
    assert relation is not None and relation.df()["order_id"].tolist() == pasted["order_id"].tolist()

    # The steps window's Code tab keeps every form, the longer pandas translation too.
    window = open_steps(browser, orders_url)
    window.get_by_role("button", name="Copy transformations as SQL or Python").click()
    view = window.locator(".code-view")
    expect(view).to_contain_text("Verified identical", timeout=30000)
    expect(window.locator(".steps-panel")).to_contain_text("Reads a pandas DataFrame named orders")
    view.locator("select").select_option("pandas")
    expect(window.locator(".code-view pre")).to_contain_text("orders_view = transform(orders)", timeout=30000)
    expect(window.locator(".code-view")).to_contain_text("Verified identical", timeout=30000)
    window.close()


def test_the_grid_has_the_whole_page_and_its_steps_open_in_their_own_tab(
    browser: Browser, orders_url: str
) -> None:
    """Plan decision 13: no SQL box and no Table Transformations panel beside the grid."""
    page = open_grid(browser, orders_url)
    expect(page.locator("#sql-box")).to_have_count(0)
    expect(page.locator(".steps-panel")).to_have_count(0)
    grid_box = page.locator(".center").bounding_box()
    panel_box = page.locator("#ai-panel").bounding_box()
    assert grid_box is not None
    # The grid runs to the analysis panel, or to the window's edge when that panel is closed.
    right_edge = panel_box["x"] if panel_box and panel_box["width"] > 0 else 1920
    assert grid_box["x"] + grid_box["width"] >= right_edge - 20
    strip = page.locator(".steps-strip")
    expect(strip.locator(".steps-count")).to_have_text("No steps")
    expect(strip.get_by_role("button", name="Undo the last step")).to_be_disabled()
    with page.context.expect_page() as opened:
        strip.get_by_role("button", name="Edit steps").click()
    steps = opened.value
    expect(steps.locator(".steps-panel h2")).to_have_text("Table Transformations")
    expect(steps.locator(".steps-panel .step.source")).to_be_visible(timeout=15000)
    assert steps.title() == "SwissArmyGrid \u00b7 Table Transformations"
    steps.close()


def test_a_sql_step_has_its_own_form_in_the_steps_window(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    steps = open_steps(browser, orders_url)
    steps.get_by_role("button", name="Add a SQL step").click()
    dialog = steps.locator("dialog")
    box = dialog.get_by_label("SQL")
    box.fill("SELECT * FROM grid WHERE quantity > 40")
    dialog.get_by_role("button", name="Add step").click()
    expect(page.locator(".steps-strip .steps-count")).to_have_text("1 step")
    expect(page.locator("#status")).to_contain_text("of 1,000")
    # Anything but one read-only query is refused when the view is built: the strip names the step.
    steps.get_by_role("button", name="Add a SQL step").click()
    dialog = steps.locator("dialog")
    dialog.get_by_label("SQL").fill("DROP TABLE orders")
    dialog.get_by_role("button", name="Add step").click()
    expect(page.locator(".steps-strip .status-error")).to_contain_text("Step 2", timeout=15000)
    steps.close()


def test_popout_steps_window(browser: Browser, orders_url: str) -> None:
    path, fragment = orders_url.split("#")
    page = browser.new_page()
    page.goto(f"{path}?panel=steps#{fragment}")
    expect(page.locator(".steps-panel .step.source")).to_be_visible(timeout=15000)
    assert page.title() == "SwissArmyGrid · Table Transformations"


# ===== SQL box =====


# ===== the "+" tab: a new table from a query over the open tables =====


def test_the_plus_tab_builds_a_table_from_a_query(browser: Browser, server: dict[str, Any]) -> None:
    plus_orders = pd.DataFrame({"id": [1, 2, 3, 4], "region": ["N", "S", "N", "S"], "qty": [5, 3, 7, 1]})
    url = push_client.push(plus_orders, new_grid=True)
    # The orders tab shows only the north rows, so a query reads it that way too.
    north = {"kind": "filter", "params": {"column": "region", "op": "eq", "value": "N"}}
    call(server, "POST", f"/grids/{grid_of(url)}/steps", {"steps": [north]})
    page = open_grid(browser, url)

    page.get_by_role("button", name="New table from a query").click()
    draft = page.locator(".tab-draft")
    expect(draft).to_have_attribute("aria-selected", "true")
    view = page.locator("#query-view")
    expect(view.locator(".query-blank")).to_contain_text("the result becomes this table")  # the blank grid
    view.get_by_role("textbox", name="Name of the new table").fill("plus_north")
    expect(draft).to_contain_text("plus_north")  # the tab follows the name
    editor = view.locator(".sql-editor .monaco-editor, .sql-editor textarea").first
    expect(editor).to_be_visible(timeout=20000)
    editor.click()
    page.keyboard.press("Control+A")
    page.keyboard.type("SELECT id, qty FROM plus_orders ORDER BY id")
    view.get_by_role("button", name="Run", exact=True).click()

    # The draft becomes an ordinary tab on the new table, with the grid tools, and keeps its tray.
    tab = page.locator(".tab-item", has_text="plus_north")
    expect(tab).to_have_attribute("aria-selected", "true", timeout=15000)
    expect(page.locator(".tab-draft")).to_have_count(0)
    pane = page.locator(".pane.active")
    expect(pane.locator(".ag-row")).to_have_count(2, timeout=15000)  # ids 1 and 3: north only
    expect(pane.locator(".query-tray")).to_be_visible()
    expect(page.locator(".ribbon")).to_be_visible()

    # A new query and Run again: the same tab, rebuilt in place.
    tray_editor = pane.locator(
        ".query-tray .sql-editor .monaco-editor, .query-tray .sql-editor textarea"
    ).first
    tray_editor.click()
    page.keyboard.press("Control+A")
    page.keyboard.type("SELECT id, qty FROM plus_orders WHERE qty > 5")
    pane.get_by_role("button", name="Run", exact=True).click()
    expect(pane.locator(".ag-row")).to_have_count(1, timeout=15000)
    built = next(d for d in call(server, "GET", "/datasets")["datasets"] if d["name"] == "plus_north")
    assert built["version"] == 2

    # Collapsed, the tray is one bar.
    pane.get_by_role("button", name="Hide the query").click()
    expect(pane.locator(".query-tray .sql-editor")).to_be_hidden()
    for name in ("plus_north", "plus_orders"):
        call(server, "DELETE", f"/datasets/{name}")


# ===== export =====


def test_export_csv_download(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    page.get_by_role("button", name="Export", exact=True).click()
    with page.expect_download() as download_info:
        page.locator("dialog").get_by_role("button", name="Download").click()
    path = download_info.value.path()
    text = Path(path).read_text(encoding="utf-8")
    assert text.startswith("order_id,region,product")
    assert text.count("\n") == ROWS + 1
    expect(page.locator(".toast")).to_contain_text("Exported 5,000 rows")


def test_export_excel_download(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    page.get_by_role("button", name="Export", exact=True).click()
    dialog = page.locator("dialog")
    # Only Excel (a sheet) and Parquet (file metadata) carry provenance; a CSV never did.
    expect(dialog.get_by_text("Include provenance")).to_be_hidden()
    dialog.get_by_label("Format").select_option("xlsx")
    expect(dialog.get_by_text("Include provenance")).to_be_visible()
    with page.expect_download() as download_info:
        dialog.get_by_role("button", name="Download").click()
    assert download_info.value.suggested_filename == "orders.xlsx"


def test_excel_export_can_cut_a_cell_over_excels_limit(browser: Browser, server: dict[str, Any]) -> None:
    # The refusal said "Export CSV instead, or allow truncation", and the dialog had no way to.
    import io

    import openpyxl

    url = push_client.push(pd.DataFrame({"id": [1, 2], "text": ["short", "y" * 40_000]}), name="long_cells")
    page = open_grid(browser, url)
    page.get_by_role("button", name="Export", exact=True).click()
    dialog = page.locator("dialog")
    dialog.get_by_label("Format").select_option("xlsx")
    dialog.get_by_role("button", name="Download").click()
    expect(dialog.locator(".status-error")).to_contain_text("holds 40,000 characters")
    dialog.get_by_label("Cut text longer than an Excel cell holds (32,767 characters)").check()
    with page.expect_download() as download_info:
        dialog.get_by_role("button", name="Download").click()
    sheet = openpyxl.load_workbook(io.BytesIO(Path(download_info.value.path()).read_bytes()))["Data"]
    assert len(str(sheet["B3"].value)) == 32_767
    call(server, "DELETE", f"/grids/{grid_of(url)}")


# ===== analysis panel: charts, notes, zoom, split layouts =====


def test_chart_card_draws_zooms_and_cross_filters(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    open_agent(page)
    page.get_by_role("button", name="Add chart").click()
    dialog = page.locator("dialog")
    dialog.get_by_label("Chart type").select_option("bar")
    dialog.get_by_label("X axis").select_option("product")
    dialog.get_by_label("Aggregate").select_option("sum")
    dialog.get_by_label("Y axis").select_option("quantity")
    dialog.get_by_role("button", name="Add chart").click()
    expect(page.locator(".card .chart svg").first).to_be_visible(timeout=20000)
    expect(page.locator(".card-foot").first).to_contain_text("4 points")
    page.get_by_role("button", name="Open large (zoom)").click()
    expect(page.locator("dialog .expanded-chart svg")).to_be_visible(timeout=15000)
    page.locator("dialog").get_by_role("button", name="Close", exact=True).last.click()
    page.locator(".card .chart svg path.mark-rect, .card .chart svg .mark-rect path").first.click()
    expect(page.locator("#status")).to_contain_text("of 1,250", timeout=15000)


def test_chart_clicks_filter_by_date_by_heatmap_cell_and_never_by_a_whole_line(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    # Vega hands a clicked date over as epoch milliseconds: every date chart's click failed
    # ("month must be in 1..12"), and a raw date's became a filter on a number.
    grid = grid_of(orders_url)

    def add_chart(title: str, spec: dict[str, Any]) -> None:
        call(server, "POST", f"/grids/{grid}/cards", {"kind": "chart", "title": title, "spec": spec})

    def steps() -> list[dict[str, Any]]:
        return call(server, "GET", f"/grids/{grid}")["steps"]  # type: ignore[no-any-return]

    count = {"aggregate": "count"}
    add_chart("By month", {"mark": "bar", "x": {"field": "order_date", "time_unit": "month"}, "y": count})
    add_chart("By week", {"mark": "line", "x": {"field": "order_date", "time_unit": "week"}, "y": count})
    add_chart("By day", {"mark": "point", "x": {"field": "order_date"}, "y": count})
    add_chart("Cells", {"mark": "heatmap", "x": {"field": "product"}, "y": {"field": "region", **count}})
    add_chart("Prices", {"mark": "histogram", "x": {"field": "unit_price"}})
    page = open_grid(browser, orders_url)
    open_agent(page)
    for title in ("By month", "By week", "By day", "Cells", "Prices"):
        expect(page.locator(".card", has_text=title).locator(".chart svg")).to_be_visible(timeout=20000)
    # The line itself is not one week (it was the first one): only its points filter.
    page.locator(".card", has_text="By week").locator(".mark-line path").first.dispatch_event("click")
    page.wait_for_timeout(700)
    assert steps() == []
    page.locator(".card", has_text="By month").locator(".mark-rect path").first.click()
    expect(page.locator(".toast").last).to_contain_text("2026-01-01")
    assert [(s["params"]["op"], s["params"]["low"]) for s in steps()] == [("date_between", "2026-01-01")]
    assert call(server, "GET", f"/grids/{grid}/count")["row_count"] == 31 * 14
    call(server, "POST", f"/grids/{grid}/steps", {"steps": []})
    page.locator(".card", has_text="By day").locator(".mark-symbol path").first.dispatch_event("click")
    expect(page.locator(".toast").last).to_contain_text("Filtered the grid to order_date = 2026-")
    assert [s["params"]["op"] for s in steps()] == ["eq"]
    call(server, "POST", f"/grids/{grid}/steps", {"steps": []})
    page.locator(".card", has_text="Cells").locator(".mark-rect path").first.click()
    expect(page.locator(".toast").last).to_contain_text(" and region = ")
    assert sorted(s["params"]["column"] for s in steps()) == ["product", "region"]
    # A histogram bar does not filter, so its card does not say it does.
    expect(page.locator(".card", has_text="Prices").locator(".card-foot")).not_to_contain_text("click")


def test_add_chart_offers_every_open_table(browser: Browser, orders_url: str, server: dict[str, Any]) -> None:
    # Only tables whose tab had been opened were offered (their columns were in the page).
    other = push_client.push(
        pd.DataFrame({"shelf": ["A", "B", "A"], "units": [3, 4, 5]}), name="unvisited", new_grid=True
    )
    page = open_grid(browser, orders_url)
    open_agent(page)
    page.get_by_role("button", name="Add chart").click()
    dialog = page.locator("dialog")
    dialog.get_by_label("Table").select_option(label="unvisited")
    expect(dialog.get_by_label("X axis").locator("option")).to_have_text(["shelf", "units"])
    dialog.get_by_label("X axis").select_option("shelf")
    dialog.get_by_role("button", name="Add chart").click()
    card = page.locator(".card", has=page.locator(".card-table", has_text="unvisited"))
    expect(card.locator(".chart svg")).to_be_visible(timeout=20000)
    expect(card.locator(".card-foot")).to_contain_text("2 points")
    call(server, "DELETE", f"/grids/{grid_of(other)}")


def test_thousands_of_bars_stay_bars_with_readable_labels(browser: Browser, server: dict[str, Any]) -> None:
    # 5,000 bars drew as one black block (each bar's outline was wider than the bar) over a
    # smear of 5,000 angled labels on top of each other.
    many = pd.DataFrame({"code": [f"code {i:04d}" for i in range(3000)], "n": range(3000)})
    url = push_client.push(many, name="many_codes", new_grid=True)
    grid = grid_of(url)
    spec = {"mark": "bar", "x": {"field": "code"}, "y": {"aggregate": "sum", "field": "n"}}
    call(server, "POST", f"/grids/{grid}/cards", {"kind": "chart", "title": "Codes", "spec": spec})
    page = open_grid(browser, url)
    open_agent(page)
    card = page.locator(".card", has_text="Codes")
    expect(card.locator(".chart svg")).to_be_visible(timeout=20000)
    widths = card.locator(".mark-rect path").evaluate_all(
        "paths => paths.map(p => getComputedStyle(p).strokeWidth)"
    )
    assert len(widths) == 3000 and set(widths) == {"0px"}
    shown = card.locator(".role-axis-label text").evaluate_all(
        "labels => labels.filter(l => getComputedStyle(l).opacity !== '0'"
        " && l.getAttribute('opacity') !== '0').length"
    )
    assert 0 < shown < 300
    call(server, "DELETE", f"/grids/{grid}")


def test_a_result_card_keeps_long_text_from_hiding_the_other_columns(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    # demo_frame's row 3 holds a 700-character note: its column took the card's whole width.
    sql = "SELECT note, order_id, region FROM grid WHERE order_id IN (3, 4, 5) ORDER BY order_id"
    call(
        server, "POST", f"/grids/{grid_of(orders_url)}/cards", {"kind": "table", "title": "Notes", "sql": sql}
    )
    page = open_grid(browser, orders_url)
    open_agent(page)
    card = page.locator(".card", has_text="Notes")
    expect(card.locator("tbody tr").nth(1).locator("td").first).to_have_attribute("title", "x" * 700)
    card_box = card.bounding_box()
    region = card.locator("th", has_text="region").bounding_box()
    assert card_box and region and region["x"] + region["width"] <= card_box["x"] + card_box["width"]


def test_a_diagram_card_never_makes_the_page_fetch_anything(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    # Mermaid's sanitizer kept <img src> in a label, so an agent's diagram made the user's
    # browser request any URL it named (the page has no Content-Security-Policy).
    body = (
        '%%{init: {"themeCSS": ".node rect { fill: url(/beacon-css.png) }"}}%%\n'
        "flowchart LR\n"
        '  A["<img src=/beacon-label.png> label"] --> B\n'
        '  B["<image href=/beacon-svg.png/>"] --> C\n'
        '  click A "/beacon-click"'
    )
    grid = grid_of(orders_url)
    call(
        server,
        "POST",
        f"/grids/{grid}/cards",
        {"kind": "diagram", "title": "Beacons", "body": body},
        agent=True,
    )
    page = open_grid(browser, orders_url)
    fetched: list[str] = []
    page.on("request", lambda request: fetched.append(request.url))
    open_agent(page)
    card = page.locator(".card", has_text="Beacons")
    expect(card.locator(".diagram svg")).to_be_visible(timeout=20000)
    expect(card.locator(".diagram")).to_contain_text("label")
    page.wait_for_timeout(1000)
    assert not [url for url in fetched if "beacon" in url]
    assert card.locator(".diagram img, .diagram image, .diagram [href*='beacon']").count() == 0


def test_a_small_diagram_is_not_blown_up_to_the_cards_width(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    # A two-column ERD (155 px wide) was stretched to the card's 760 px: 50 px text, 1,360 px tall.
    body = "erDiagram\n  ORDERS ||--o{ LINES : has\n  ORDERS {\n    int order_id PK\n  }"
    call(
        server,
        "POST",
        f"/grids/{grid_of(orders_url)}/cards",
        {"kind": "diagram", "title": "Tiny", "body": body},
    )
    page = open_grid(browser, orders_url)
    open_agent(page)
    svg = page.locator(".card", has_text="Tiny").locator(".diagram svg")
    expect(svg).to_be_visible(timeout=20000)
    natural = float(svg.get_attribute("viewBox").split()[2])  # type: ignore[union-attr]
    box = svg.bounding_box()
    assert box and box["width"] <= natural + 1


def test_note_markdown_is_rendered_safely(browser: Browser, orders_url: str, server: dict[str, Any]) -> None:
    grid = orders_url.split("/g/")[1].split("#")[0]
    call(
        server,
        "POST",
        f"/grids/{grid}/cards",
        {
            "kind": "note",
            "title": "Findings",
            "body": "**North** leads.\n\n- one\n- two\n\n<script>alert(1)</script>",
        },
        agent=True,
    )
    page = open_grid(browser, orders_url)
    open_agent(page)
    note = page.locator(".card", has_text="Findings")
    expect(note.locator("strong")).to_have_text("North")
    expect(note.locator("li")).to_have_count(2)
    expect(note).to_contain_text("<script>alert(1)</script>")  # shown as text, never run
    expect(note.locator(".badge.agent")).to_be_visible()


def test_agent_panel_lives_only_in_the_agent_tab(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    expect(page.locator("#ai-panel")).to_be_hidden()  # the grid gets the full width
    panel = open_agent(page)
    expect(panel).to_be_visible()
    page.get_by_role("button", name="Zoom in the panel").click()
    expect(panel.locator(".zoom-control output")).to_have_text("110%")
    back_to_grid(page, orders_url)
    expect(page.locator("#ai-panel")).to_be_hidden()


# ===== the agent door =====


def test_agent_approval_banner_allow(browser: Browser, orders_url: str, server: dict[str, Any]) -> None:
    import threading

    grid = orders_url.split("/g/")[1].split("#")[0]
    page = open_grid(browser, orders_url)
    result: dict[str, Any] = {}
    worker = threading.Thread(
        target=lambda: result.update(
            call(server, "POST", f"/agent/export-approval/{grid}?format=csv", agent=True)
        )
    )
    worker.start()
    # The request pops up at once, over whatever tab is open.
    expect(page.locator(".approval")).to_be_visible(timeout=15000)
    expect(page.locator(".approval pre")).to_have_text("csv export of orders to the export folder")
    page.get_by_role("button", name="Allow").click()
    worker.join(timeout=30)
    assert result["approved"] is True and result["ticket"]
    expect(page.locator(".approval")).to_have_count(0)


def test_closing_the_approval_window_declines_the_request(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    # Escape (or the window's X) hid a request that was still waiting: nothing in the page
    # showed it again until a reload, and the agent waited out the 3 minutes.
    import threading

    grid = orders_url.split("/g/")[1].split("#")[0]
    page = open_grid(browser, orders_url)
    answers: list[int] = []

    def ask() -> None:
        try:
            call(server, "POST", f"/agent/export-approval/{grid}?format=csv", agent=True)
            answers.append(200)
        except urllib.error.HTTPError as error:
            answers.append(error.code)

    for close in ("Escape", "X"):
        worker = threading.Thread(target=ask)
        worker.start()
        expect(page.locator(".approval")).to_be_visible(timeout=15000)
        expect(page.locator("dialog")).to_contain_text("Closing this window declines")
        if close == "Escape":
            page.keyboard.press("Escape")
        else:
            page.locator("dialog").get_by_role("button", name="Close").first.click()
        worker.join(timeout=20)
        assert not worker.is_alive()
        assert call(server, "GET", "/agent/approvals")["pending"] == []
    assert answers == [403, 403]


def test_agent_activity_in_settings(browser: Browser, orders_url: str, server: dict[str, Any]) -> None:
    page = open_grid(browser, orders_url)
    sql = "SELECT count(*) AS activity_check FROM grid"
    call(server, "POST", "/agent/query", {"dataset": "orders", "sql": sql}, agent=True)
    page.get_by_role("button", name="Settings").click()
    dialog = page.locator("dialog")
    expect(dialog.locator(".activity li").first).to_contain_text(sql)  # newest first
    expect(dialog).to_contain_text("Its exports wait for your Allow")
    # The privacy levels and column masks are gone.
    expect(dialog.get_by_role("radio", name=re.compile("Level 3"))).to_have_count(0)
    expect(dialog.get_by_text("What the agent may see")).to_have_count(0)
    install = "claude mcp add --scope user swissarmygrid -- swissarmygrid-mcp"
    expect(dialog.locator(".mcp-prompt")).to_contain_text(install)


def test_agent_changes_show_up_live(browser: Browser, orders_url: str, server: dict[str, Any]) -> None:
    page = open_grid(browser, orders_url)
    grid = orders_url.split("/g/")[1].split("#")[0]
    call(
        server,
        "POST",
        f"/grids/{grid}/highlights",
        {"highlights": [{"column": "order_id", "op": "eq", "value": 2, "label": "check this"}]},
        agent=True,
    )
    expect(page.locator(".ag-row[row-index='1'].row-highlight").first).to_be_visible(timeout=15000)
    expect(page.locator(".marker-cell").first).to_have_attribute("title", "check this")
    call(
        server,
        "POST",
        f"/grids/{grid}/cards",
        {"kind": "chart", "spec": {"mark": "histogram", "x": {"field": "unit_price"}}},
        agent=True,
    )
    open_agent(page)
    expect(page.locator(".card .chart svg")).to_be_visible(timeout=20000)


# ===== live updates, datasets, paste =====


def test_repush_hot_reloads_and_keeps_sort(browser: Browser, server: dict[str, Any]) -> None:
    reload_me = demo_frame(200)
    url = push_client.push(reload_me)
    page = open_grid(browser, url)
    page.locator(".ag-header-cell[col-id='order_id'] .sag-header-sort").click()
    page.locator(".ag-header-cell[col-id='order_id'] .sag-header-sort").click()
    wait_for_cell(page, 0, "order_id", "200")
    push_client.push(demo_frame(300), name="reload_me")
    expect(page.locator("#grid-title")).to_contain_text("v2", timeout=15000)
    wait_for_cell(page, 0, "order_id", "300")


def test_datasets_popover_and_memory_meter(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    expect(page.locator(".meter")).to_contain_text("MiB of")
    page.get_by_role("button", name="Datasets").click()
    expect(page.locator(".popover table")).to_contain_text("orders")


def test_paste_tab_separated_text_makes_a_dataset(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    page = open_grid(browser, orders_url)
    page.evaluate(
        """() => {
            const data = new DataTransfer();
            data.setData('text/plain', 'a\\tb\\n1\\tx\\n2\\ty\\n');
            document.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true }));
        }"""
    )
    page.wait_for_timeout(1500)
    assert any(d["name"].startswith("pasted_") for d in call(server, "GET", "/datasets")["datasets"])


def test_two_quick_pastes_make_two_datasets(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    def pasted() -> set[str]:
        return {
            d["name"] for d in call(server, "GET", "/datasets")["datasets"] if d["name"].startswith("pasted_")
        }

    before = pasted()
    page = open_grid(browser, orders_url)
    page.evaluate(
        """() => {
            for (const text of ['a\\tb\\n1\\tx\\n', 'c\\td\\n2\\ty\\n']) {
              const data = new DataTransfer();
              data.setData('text/plain', text);
              document.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true }));
            }
        }"""
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and len(pasted() - before) < 2:
        time.sleep(0.2)
    made = pasted() - before
    try:
        assert len(made) == 2, made  # the second paste did not replace the first
    finally:
        for name in made:
            call(server, "DELETE", f"/datasets/{name}")


# ===== themes =====


@pytest.mark.parametrize("theme", ["light", "dark", "hc-light", "hc-dark"])
def test_themes_apply_and_screenshot(
    browser: Browser, orders_url: str, server: dict[str, Any], theme: str
) -> None:
    grid = orders_url.split("/g/")[1].split("#")[0]
    call(
        server,
        "POST",
        f"/grids/{grid}/cards",
        {
            "kind": "chart",
            "title": "Quantity by product",
            "spec": {
                "mark": "bar",
                "x": {"field": "product"},
                "y": {"aggregate": "sum", "field": "quantity"},
                "color": {"field": "region"},
            },
        },
    )
    call(
        server,
        "POST",
        f"/grids/{grid}/cards",
        {
            "kind": "note",
            "title": "Notes",
            "body": "**Gizmo** sells the most.\n\n- Check the *Central* spelling\n- `north` vs `North`",
        },
    )
    page = open_grid(browser, orders_url)
    set_theme(page, theme)
    expect(page.locator("html")).to_have_attribute("data-theme", theme)
    open_agent(page)
    expect(page.locator(".card .chart svg").first).to_be_visible(timeout=20000)
    back_to_grid(page, orders_url)
    page.wait_for_timeout(600)
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"theme-{theme}.png"))
    background = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert (
        background
        in {
            "light": ("rgb(255, 255, 255)",),
            "dark": ("rgb(15, 20, 25)",),
            "hc-light": ("rgb(255, 255, 255)",),
            "hc-dark": ("rgb(0, 0, 0)",),
        }[theme]
    )


def test_system_theme_follows_dark_and_more_contrast(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url, color_scheme="dark")
    set_theme(page, "system")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.emulate_media(color_scheme="light")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")


def test_forced_colors_mode(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url, forced_colors="active")
    assert page.evaluate("matchMedia('(forced-colors: active)').matches")
    page.screenshot(path=str(SHOTS / "forced-colors.png"))
    expect(page.locator(".ag-row").first).to_be_visible()


def test_keyboard_only_reaches_the_toolbar_and_grid(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    # The tab bar comes first: Settings at its far left, the blades page, the notebook, then the tab.
    page.keyboard.press("Tab")
    focused = page.evaluate("document.activeElement && document.activeElement.getAttribute('aria-label')")
    assert focused == "Settings"  # the cog
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.getAttribute('aria-label')") == "Blades and catalog"
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.getAttribute('aria-label')") == "Notebook"
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.getAttribute('role')") == "tab"
    outline = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    assert outline != "none"


def test_no_column_is_cut_beside_the_row_numbers(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    """Regression (visual review): a sideways scroll stopped mid-column, so a half-hidden
    "499.99" read as "199.99" next to the pinned column. Only the row numbers are pinned now:
    the first data column scrolls with the rest."""
    grid = grid_of(orders_url)
    call(
        server,
        "POST",
        f"/grids/{grid}/steps",
        {
            "steps": [
                {
                    "kind": "bucket",
                    "params": {
                        "column": "unit_price",
                        "as": "price_band",
                        "mode": "edges",
                        "edges": [0, 100, 500],
                    },
                }
            ]
        },
    )
    page = open_grid(browser, orders_url)
    page.locator(".ag-body-horizontal-scroll-viewport").evaluate("e => e.scrollLeft = 160")  # mid-way across
    probe = """() => {
      const cells = [...document.querySelectorAll('.ag-header-cell[col-id]')]
        .map(c => [c.getAttribute('col-id'), c.getBoundingClientRect()]);
      const key = cells.find(c => c[0] === '__sag_rownum')[1];
      return cells.filter(c => !['__sag_rownum', '__sag_filler'].includes(c[0]))
        .filter(c => c[1].left < key.right - 1 && c[1].right > key.right + 1).map(c => c[0]);
    }"""
    page.wait_for_timeout(1500)
    assert page.evaluate(probe) == []
    page.locator(".ag-body-horizontal-scroll-viewport").evaluate("e => e.scrollLeft = 100000")
    page.wait_for_timeout(1200)
    assert page.evaluate(probe) == []


# ===== Excel-style selection, the ribbon, SQL steps, the value viewer =====


def drag(page: Page, start: str, end: str) -> None:
    page.locator(start).wait_for()
    page.locator(end).wait_for()
    a = page.locator(start).bounding_box()
    b = page.locator(end).bounding_box()
    assert a and b
    page.mouse.move(a["x"] + 8, a["y"] + 6)
    page.mouse.down()
    page.mouse.move(b["x"] + 8, b["y"] + 6, steps=8)
    page.mouse.up()


def row_cell(row: int, column: str) -> str:
    return f".ag-row[row-index='{row}'] .ag-cell[col-id='{column}']"


def test_row_numbers_select_rows_by_click_and_drag(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    assert page.locator(".ag-selection-checkbox").count() == 0  # no checkboxes any more
    drag(page, row_cell(1, "__sag_rownum"), row_cell(4, "__sag_rownum"))
    expect(page.locator(".statusbar")).to_contain_text("4 rows selected")
    expect(page.locator(row_cell(2, "region"))).to_have_class(re.compile("cell-selected"))
    expect(page.locator(row_cell(5, "region"))).not_to_have_class(re.compile("cell-selected"))
    page.locator(row_cell(8, "__sag_rownum")).click(modifiers=["Control"])
    expect(page.locator(".statusbar")).to_contain_text("5 rows selected")


def test_header_selects_its_column_and_the_column_tools_follow(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    page = open_grid(browser, orders_url)
    page.locator(".ag-header-cell[col-id='quantity'] .sag-header-name").click()
    expect(page.locator(".ag-header-cell[col-id='quantity']")).to_have_class(re.compile("col-selected"))
    expect(page.locator(row_cell(0, "quantity"))).to_have_class(re.compile("cell-selected"))
    # Excel's status bar, over all 5,000 rows (not just the loaded ones).
    total = int(demo_frame()["quantity"].sum())
    expect(page.locator(".statusbar")).to_contain_text(f"Sum {total:,}")
    expect(page.locator(".ribbon-caption").nth(1)).to_contain_text("quantity")
    page.get_by_role("button", name="Sort Z→A").click()
    wait_for_cell(page, 0, "quantity", "50")
    page.locator(".ag-header-cell[col-id='note'] .sag-header-name").click()
    page.get_by_role("button", name="Remove", exact=True).click()
    expect(page.locator(".ag-header-cell[col-id='note']")).to_have_count(0)
    steps = call(server, "GET", f"/grids/{grid_of(orders_url)}")["steps"]
    assert [s["kind"] for s in steps] == ["sort", "columns"]


def test_cell_block_ctrl_a_and_copy(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    # Capture what the page writes rather than use the real Windows clipboard, which headless
    # Edge shares with the desktop (a test would clobber it, and a copy elsewhere breaks it).
    page.evaluate(
        """() => {
          navigator.clipboard.write = async (items) => {
            window.__copied = await (await items[0].getType('text/plain')).text();
          };
          navigator.clipboard.writeText = async (text) => { window.__copied = text; };
        }"""
    )
    drag(page, row_cell(0, "quantity"), row_cell(2, "unit_price"))
    expect(page.locator(".statusbar")).to_contain_text("6 cells selected")
    page.keyboard.press("Control+c")
    expect(page.locator(".toast").last).to_contain_text("Copied 3 rows", timeout=10000)
    frame = demo_frame()
    expected = "\r\n".join(f"{frame.quantity[i]}\t{frame.unit_price[i]}" for i in range(3))
    page.wait_for_function("window.__copied !== undefined")
    assert page.evaluate("window.__copied").strip() == expected
    page.keyboard.press("Control+a")
    expect(page.locator(".statusbar")).to_contain_text("All selected")
    page.keyboard.press("Escape")
    expect(page.locator(".statusbar")).not_to_contain_text("selected")


def test_clicking_off_the_grid_clears_a_selected_column_but_the_ribbon_keeps_it(
    browser: Browser, orders_url: str
) -> None:
    page = open_grid(browser, orders_url)
    header = page.locator(".ag-header-cell[col-id='quantity']")
    page.locator(".ag-header-cell[col-id='quantity'] .sag-header-name").click()
    expect(header).to_have_class(re.compile("col-selected"))
    # The ribbon's tools act on the selection, so a click there leaves it.
    page.locator(".ribbon-caption").nth(1).click()
    expect(header).to_have_class(re.compile("col-selected"))
    # A click anywhere else outside the grid clears it.
    page.locator(".statusbar").click()
    expect(header).not_to_have_class(re.compile("col-selected"))


def test_clicking_off_the_grid_clears_selected_rows(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    drag(page, row_cell(1, "__sag_rownum"), row_cell(4, "__sag_rownum"))
    expect(page.locator(".statusbar")).to_contain_text("4 rows selected")
    page.locator(".statusbar").click()
    expect(page.locator(".statusbar")).not_to_contain_text("rows selected")
    expect(page.locator(row_cell(2, "region"))).not_to_have_class(re.compile("cell-selected"))


def test_clicking_off_the_grid_clears_a_select_all(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    page.locator(row_cell(1, "quantity")).click()  # a data cell, so the grid has the keyboard focus
    # Wait for the click to land before Ctrl+A: pressed in the same instant, the key can arrive before
    # the grid has the focus (a race of the test's speed, not something a person can do).
    expect(page.locator(".statusbar")).to_contain_text("1 cell selected")
    page.keyboard.press("Control+a")
    expect(page.locator(".statusbar")).to_contain_text("All selected")
    page.locator(".statusbar").click()
    expect(page.locator(".statusbar")).not_to_contain_text("All selected")


def test_cell_tool_keeps_rows_with_the_selected_value(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    page.locator(row_cell(0, "product")).click()
    expect(page.locator(".ribbon-caption").nth(2)).to_contain_text("row 1 of product")
    page.get_by_role("button", name="Keep these").click()
    expect(page.locator("#status")).to_contain_text("of 1,250")
    page.locator(row_cell(0, "region")).click()
    page.get_by_role("button", name="Exclude these").click()
    expect(page.locator("#status")).to_contain_text("of 833")  # Widget and North: 417 rows go


def test_value_viewer_opens_json_layers_and_markdown(browser: Browser, server: dict[str, Any]) -> None:
    viewer_demo = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "payload": [
                '{"customer": {"name": "Acme", "tags": ["a", "b"]}, '
                '"lines": [{"sku": "W-1"}, {"sku": "G-7"}]}',
                "# Notes\n\n- leave at **door**\n- call `555-0100`\n\n<script>alert(1)</script>",
                "plain",
            ],
        }
    )
    page = open_grid(browser, push_client.push(viewer_demo, new_grid=True))
    page.locator(row_cell(0, "payload")).dblclick()
    viewer = page.locator("dialog .value-viewer")
    expect(viewer.locator(".badge")).to_have_text("JSON object · 2 keys")
    viewer.get_by_role("button", name=re.compile("Open customer")).click()
    expect(viewer.locator(".vv-crumbs")).to_contain_text("customer")
    expect(viewer.locator(".vv-table")).to_contain_text("Acme")
    viewer.get_by_role("button", name=re.compile("Open tags")).click()
    expect(viewer.locator(".badge")).to_have_text("Array · 2 items")
    viewer.locator(".vv-crumbs button").first.click()
    viewer.get_by_role("tab", name="JSON").click()
    expect(viewer.locator(".vv-json .j-key").first).to_have_text('"customer"')
    page.keyboard.press("Escape")
    page.locator(row_cell(1, "payload")).dblclick()
    expect(viewer.locator(".badge")).to_contain_text("Markdown")
    expect(viewer.locator(".vv-markdown h3")).to_have_text("Notes")
    expect(viewer.locator(".vv-markdown strong")).to_have_text("door")
    assert viewer.locator("script").count() == 0  # markup in a value never becomes markup


def test_horizontal_scrollbar_is_always_shown(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url, width=900, height=700)
    bar = page.locator(".ag-body-horizontal-scroll")
    expect(bar).to_be_visible()
    assert bar.evaluate("e => e.getBoundingClientRect().height") >= 14
    viewport = page.locator(".ag-body-horizontal-scroll-viewport")
    assert viewport.evaluate("e => e.scrollWidth > e.clientWidth")


def test_the_grid_scrolls_freely_through_a_column_wider_than_the_view(
    browser: Browser, server: dict[str, Any]
) -> None:
    """Sideways scrolling is free: it never snaps to a column edge. It used to, and inside a column wider
    than the view that dragged the scroll back to the column's start, so such a column could not be read past
    its first screenful. The column is widened the way a person does it, by dragging its edge."""
    df = pd.DataFrame({"id": [1, 2, 3], "note": ["short"] * 3, "after": ["x", "y", "z"], "more": [1, 2, 3]})
    page = open_grid(browser, push_client.push(df, new_grid=True), width=900, height=600)
    handle = page.locator(".ag-header-cell[col-id='note'] .ag-header-cell-resize")
    handle.wait_for(state="attached")
    box = handle.bounding_box()
    assert box
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    for step in range(1, 21):
        page.mouse.move(x + step * 40, y)
    page.mouse.up()
    page.wait_for_timeout(500)
    viewport = page.locator(".ag-body-horizontal-scroll-viewport")
    view_width = viewport.evaluate("e => e.clientWidth")
    note_width = page.locator(".ag-header-cell[col-id='note']").evaluate("e => e.clientWidth")
    assert note_width > view_width, (view_width, note_width)
    page.mouse.move(450, 300)
    for notch in range(1, 6):
        page.mouse.wheel(150, 0)
        page.wait_for_timeout(450)  # longer than the old snapping's delay
        assert abs(viewport.evaluate("e => e.scrollLeft") - 150 * notch) < 3, notch
    page.mouse.wheel(-150, 0)  # and back again, a notch at a time
    page.wait_for_timeout(450)
    assert abs(viewport.evaluate("e => e.scrollLeft") - 600) < 3


def test_percent_is_the_only_wildcard(browser: Browser, orders_url: str, server: dict[str, Any]) -> None:
    call(
        server,
        "POST",
        f"/grids/{grid_of(orders_url)}/steps",
        {
            "steps": [{"kind": "filter", "params": {"column": "product", "op": "eq", "value": "G%o"}}],
            "settings": {"wildcards": True},
        },
    )
    page = open_grid(browser, orders_url)
    expect(page.locator("#status")).to_contain_text("of 1,250")  # Gizmo, not Gadget
    call(
        server,
        "POST",
        f"/grids/{grid_of(orders_url)}/steps",
        {
            "steps": [{"kind": "filter", "params": {"column": "product", "op": "eq", "value": "G*o"}}],
            "settings": {"wildcards": True},
        },
    )
    expect(page.locator("#status")).to_contain_text("0 rows", timeout=15000)  # * is literal


def test_key_finder_ranks_join_columns_with_percent_match(browser: Browser, server: dict[str, Any]) -> None:
    key_orders = pd.DataFrame(
        {
            "order_id": range(1, 3001),
            "cust": [i % 900 + 1 for i in range(3000)],
            "status": ["Y" if i % 2 else "N" for i in range(3000)],
        }
    )
    key_customers = pd.DataFrame(
        {
            "customer_id": [str(i) for i in range(1, 1001)],
            "active": ["Y" if i % 3 else "N" for i in range(1000)],
        }
    )
    push_client.push(key_customers, new_grid=True)
    page = open_grid(browser, push_client.push(key_orders, new_grid=True))
    # The Key Finder opens in a window of its own, beside the grid.
    with page.expect_popup() as popup:
        page.locator(".ribbon-group").first.get_by_role("button", name="Key Finder").click()
    finder = popup.value
    expect(finder).to_have_title("SwissArmyGrid · Key Finder")
    expect(finder.locator("#grid-title")).to_have_text("Possible Join Keys for key_orders")
    panel = finder.locator(".keys-window")
    panel.get_by_label("Compare with").select_option(label="key_customers")
    panel.get_by_role("button", name="Find keys").click()
    best = panel.locator(".kf-row").first
    expect(best).to_contain_text("cust", timeout=20000)
    expect(best).to_contain_text("customer_id")
    expect(best).to_contain_text("100%")  # every cust is a customer
    expect(best).to_contain_text("90%")  # 900 of the 1,000 customers have orders
    # Above the pairs: this table's own key. order_id alone tells the 3,000 rows apart.
    unique = panel.locator(".kf-unique")
    expect(unique).to_contain_text("Most distinct column combination in key_orders")
    expect(unique.locator(".kf-best")).to_contain_text("order_id")
    expect(unique.locator(".kf-best")).to_contain_text("unique per row")
    expect(unique.locator(".kf-best")).to_contain_text("3,000/3,000")
    # Separate table, column and relationship columns; the share columns are headed by the table names.
    headers = panel.locator(".kf-results > .kf-table th")
    expect(headers.nth(0)).to_have_text("Current Table Column Name")
    expect(headers.nth(1)).to_have_text("Table")
    expect(headers.nth(4)).to_have_text("key_orders")
    expect(headers.nth(5)).to_have_text("key_customers")
    expect(headers.filter(has_text="Score")).to_have_count(0)
    best.click()
    expect(panel.locator(".kf-sample")).to_contain_text("900 shared")
    expect(panel.locator(".kf-sample")).to_contain_text("0 of cust not in customer_id")


# ===== the workspace: one window, a tab per grid =====


def test_workspace_shows_grids_as_tabs(browser: Browser, server: dict[str, Any]) -> None:
    tab_one = pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"]})
    tab_two = pd.DataFrame({"code": ["X", "Y"], "amount": [1.5, 2.5]})
    push_client.push(tab_one, new_grid=True)
    push_client.push(tab_two, new_grid=True)
    page = browser.new_context(viewport={"width": 1920, "height": 900}).new_page()
    page.goto(f"http://127.0.0.1:{server['port']}/workspace#token={server['token']}")
    tabs = page.locator(".tab-item")
    expect(tabs.filter(has_text="tab_one")).to_have_count(1, timeout=15000)
    expect(tabs.filter(has_text="tab_two")).to_have_count(1)

    # Clicking a tab shows that grid, and the shared panels follow it.
    tabs.filter(has_text="tab_two").click()
    expect(tabs.filter(has_text="tab_two")).to_have_attribute("aria-selected", "true")
    active = page.locator(".pane.active")
    expect(active.locator(".ag-row[row-index='0'] .ag-cell[col-id='code']")).to_have_text("X", timeout=20000)
    expect(page.locator("#grid-title")).to_contain_text("tab_two")
    expect(page.locator(".steps-strip .steps-count")).to_have_text("No steps")
    assert page.title() == "SwissArmyGrid"

    # A push while the workspace is open arrives as a new, active tab (no new window).
    tab_three = pd.DataFrame({"x": [1]})
    push_client.push(tab_three, new_grid=True)
    expect(tabs.filter(has_text="tab_three")).to_have_attribute("aria-selected", "true", timeout=15000)

    # Dragging a tab to another tab's left edge reorders the bar, and the order survives a reload.
    tabs.filter(has_text="tab_three").drag_to(
        tabs.filter(has_text="tab_one"), target_position={"x": 4, "y": 10}
    )
    names = tabs.locator(".tab-label").all_inner_texts()
    assert names.index("tab_three") < names.index("tab_one")
    page.reload()
    expect(tabs.filter(has_text="tab_one")).to_have_count(1, timeout=15000)
    reloaded = tabs.locator(".tab-label").all_inner_texts()
    assert reloaded.index("tab_three") < reloaded.index("tab_one")

    # The tab's close button closes the grid on the server; the dataset stays.
    tabs.filter(has_text="tab_one").get_by_role("button", name="Close tab_one").click()
    expect(tabs.filter(has_text="tab_one")).to_have_count(0)
    open_grids = {g["dataset"] for g in call(server, "GET", "/grids")["grids"]}
    assert "tab_one" not in open_grids
    assert "tab_one" in {d["name"] for d in call(server, "GET", "/datasets")["datasets"]}


def test_split_view_by_dragging_a_tab_onto_the_grid(browser: Browser, server: dict[str, Any]) -> None:
    left_table = pd.DataFrame({"k": [1, 2, 3], "v": ["a", "b", "c"]})
    right_table = pd.DataFrame({"k": [3, 4], "w": ["x", "y"]})
    push_client.push(left_table, new_grid=True)
    page = open_grid(browser, push_client.push(right_table, new_grid=True))
    tabs = page.locator(".tab-item")
    tabs.filter(has_text="left_table").click()
    expect(page.locator("#grid-title")).to_contain_text("left_table")

    # Drag the other tab down onto the right half of the grid: side by side.
    box = page.locator(".panes").bounding_box()
    assert box
    tabs.filter(has_text="right_table").drag_to(
        page.locator(".panes"), target_position={"x": box["width"] * 0.8, "y": box["height"] / 2}
    )
    expect(page.locator(".panes")).to_have_attribute("data-split", "on")
    # A new split opens the key picker; Close keeps the two grids side by side without keys.
    picker = page.locator("dialog .compare-dialog")
    expect(picker).to_be_visible()
    page.locator("dialog").get_by_role("button", name="Close", exact=True).last.click()
    expect(picker).to_have_count(0)
    panes = page.locator(".pane:not([hidden])")
    expect(panes).to_have_count(2)
    expect(panes.nth(0).locator(".pane-head")).to_contain_text("left_table")
    expect(panes.nth(1).locator(".pane-head")).to_contain_text("right_table")
    expect(panes.nth(1).locator(".ag-row[row-index='0'] .ag-cell[col-id='w']")).to_have_text(
        "x", timeout=20000
    )

    # Clicking into a pane makes it active: the title and the steps panel follow.
    panes.nth(0).locator(".ag-row[row-index='0'] .ag-cell[col-id='v']").click()
    expect(page.locator("#grid-title")).to_contain_text("left_table")
    expect(page.locator(".pane.active .pane-head")).to_contain_text("left_table")
    panes.nth(1).locator(".ag-row[row-index='0'] .ag-cell[col-id='w']").click()
    expect(page.locator("#grid-title")).to_contain_text("right_table")

    # A step added now lands on the active grid only.
    page.locator(".ribbon").get_by_role("button", name="Keep first rows").click()
    page.locator("dialog").get_by_label("Rows to keep").fill("1")
    page.locator("dialog").get_by_role("button", name="Add step").click()
    expect(panes.nth(1).locator(".ag-row")).to_have_count(1, timeout=15000)
    expect(panes.nth(0).locator(".ag-row")).to_have_count(3)

    # Close one side: back to one grid; the other stays open as a tab.
    panes.nth(0).get_by_role("button", name="Close this side").click()
    expect(page.locator(".panes")).to_have_attribute("data-split", "off")
    expect(page.locator(".pane:not([hidden])")).to_have_count(1)
    expect(tabs.filter(has_text="left_table")).to_have_count(1)


def test_compare_lines_the_two_grids_up_row_for_row(browser: Browser, server: dict[str, Any]) -> None:
    ledger = pd.DataFrame({"invoice": [3, 1, 2, 5], "amount": [30.0, 10.0, 20.0, 50.0]})
    bank = pd.DataFrame({"invoice_ref": ["1", "2", "4", "5"], "amount": [10.0, 25.0, 40.0, 50.0]})
    push_client.push(bank, new_grid=True)
    page = open_grid(browser, push_client.push(ledger, new_grid=True))
    tabs = page.locator(".tab-item")
    tabs.filter(has_text="bank").drag_to(tabs.filter(has_text="ledger"))  # a tab's middle: side by side

    # The key picker opens with the Key Finder already run; its best pair starts ticked.
    picker = page.locator("dialog .compare-dialog")
    expect(picker.locator("tbody tr").first).to_contain_text("invoice_ref", timeout=20000)
    expect(picker.locator("tbody tr").first.locator("input[type=checkbox]")).to_be_checked()
    page.locator("dialog").get_by_role("button", name="Compare", exact=True).click()

    panes = page.locator(".pane:not([hidden])")
    left, right = panes.nth(0), panes.nth(1)
    expect(left.locator(".pane-head")).to_contain_text("ledger")
    # Five keys in all (1, 2, 3, 4, 5): each grid shows five rows, level with each other.
    expect(left.locator(".ag-row")).to_have_count(5, timeout=20000)
    expect(right.locator(".ag-row")).to_have_count(5)

    def column(pane: Any, name: str) -> list[str]:
        cells = pane.locator(f".ag-row .ag-cell[col-id='{name}']")
        rows = pane.locator(".ag-row")
        order = [int(r) for r in rows.evaluate_all("rs => rs.map(r => r.getAttribute('row-index'))")]
        texts = cells.all_inner_texts()
        return [t for _, t in sorted(zip(order, texts, strict=True))]

    # Read the amounts: row N is the same invoice on both sides.
    assert column(left, "amount") == ["10", "20", "30", "", "50"]
    assert column(right, "amount") == ["10", "25", "", "40", "50"]
    # A placeholder row stands where a side has no such invoice; differing amounts are marked.
    expect(left.locator(".ag-row.row-placeholder").first).to_be_visible()
    expect(right.locator(".ag-row[row-index='1'] .ag-cell[col-id='amount']").first).to_have_class(
        re.compile("cell-diff")
    )
    expect(page.locator(".ribbon")).to_contain_text("3 matched")
    # One shared scrollbar in the center, between the aligned grids.
    center = page.locator(".panes > .center-scroll")
    expect(center).to_be_visible()
    # Scrolling either grid (or the bar) puts the other two at the very same position.
    level = page.evaluate(
        """async () => {
          const views = [...document.querySelectorAll('.pane:not([hidden]) .ag-grid-viewport')];
          const bar = document.querySelector('.panes > .center-scroll');
          const frame = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
          const out = [];
          for (const [mover, top] of [[views[0], 40], [bar, 75], [views[1], 10]]) {
            mover.scrollTop = top;
            await frame();
            out.push([views[0].scrollTop, bar.scrollTop, views[1].scrollTop]);
          }
          return out;
        }"""
    )
    assert all(a == b == c for a, b, c in level), level
    # Scrolling the center bar moves both grids, keeping row N level with row N.
    scrolled = page.evaluate(
        """() => {
          const bar = document.querySelector('.panes > .center-scroll');
          return { bar: bar.clientHeight > 0, spacer: bar.firstElementChild.offsetHeight };
        }"""
    )
    assert scrolled["bar"] and scrolled["spacer"] > 0
    order = page.evaluate(
        "[...document.querySelector('.panes').children]"
        ".filter(e => !e.hidden).map(e => e.className.split(' ')[0])"
    )
    assert order[:3] == ["pane", "center-scroll", "pane"]

    # Clicking a row lights up the rows that share its key on both grids (here: the row level with it).
    left.locator(".ag-row[row-index='1'] .ag-cell[col-id='amount']").first.click()
    expect(right.locator(".ag-row[row-index='1']").first).to_have_class(re.compile("row-key-match"))
    expect(left.locator(".ag-row[row-index='1']").first).to_have_class(re.compile("row-key-match"))
    expect(right.locator(".ag-row[row-index='2']").first).not_to_have_class(re.compile("row-key-match"))

    # Anti join, right side only: just invoice 4.
    page.locator(".ribbon").get_by_role("button", name="Anti join").click()
    page.locator(".ribbon").get_by_role("button", name="Only in bank").click()
    expect(right.locator(".ag-row")).to_have_count(1, timeout=15000)
    assert column(right, "amount") == ["40"]

    # Cross compare: each grid shows its own rows; a click filters the other side to that key.
    page.locator(".ribbon").get_by_role("button", name="Cross compare").click()
    expect(page.locator(".panes > .center-scroll")).to_have_count(0)  # each grid scrolls on its own
    expect(left.locator(".ag-row")).to_have_count(4, timeout=15000)
    right.locator(".ag-row[row-index='0'] .ag-cell[col-id='amount']").first.click()  # invoice "1"
    expect(left.locator(".compare-banner")).to_contain_text("1 row with key 1")
    expect(left.locator(".ag-row")).to_have_count(1, timeout=15000)
    left.get_by_role("button", name="Clear").click()
    expect(left.locator(".ag-row")).to_have_count(4, timeout=15000)

    # Stop: two independent grids again, still side by side.
    page.locator(".ribbon").get_by_role("button", name="Stop compare").click()
    expect(page.locator(".ribbon")).to_contain_text("no keys yet")
    expect(page.locator(".panes")).to_have_attribute("data-split", "on")


def test_the_agent_tab_comes_first_and_holds_the_panel(browser: Browser, server: dict[str, Any]) -> None:
    first_tab = pd.DataFrame({"a": [1, 2]})
    second_tab = pd.DataFrame({"b": [3]})
    push_client.push(second_tab, new_grid=True)
    url = push_client.push(first_tab, new_grid=True)
    page = open_grid(browser, url)
    tabs = page.locator(".tabs-row .tab-item")
    # Always first, and it has no close button.
    expect(tabs.first).to_have_text("Agent")
    expect(tabs.first.locator("svg")).to_be_visible()  # an icon, so it's easy to find
    assert tabs.first.get_by_role("button").count() == 0
    open_agent(page)
    expect(page.locator("#ai-panel-title")).to_have_text("Analysis Sandbox")
    expect(page.locator("#ai-panel .tabs")).to_have_count(0)  # one panel, no inner tabs
    expect(page.locator(".workspace")).to_have_attribute("data-view", "agent")
    # One sandbox for every table: no table picker.
    expect(page.locator(".agent-table-picker")).to_have_count(0)
    # A grid tab shows the grids, full width, with no panel beside them.
    back_to_grid(page, url)
    expect(page.locator(".workspace")).not_to_have_attribute("data-view", "agent")
    expect(page.locator("#ai-panel")).to_be_hidden()


def test_sandbox_cards_fill_the_width_of_the_screen(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    grid = orders_url.split("/g/")[1].split("#")[0]
    page = open_grid(browser, orders_url, width=1920)
    open_agent(page)
    call(server, "POST", f"/grids/{grid}/cards", {"kind": "note", "title": "Wide note", "body": "A note."})
    cards = page.locator(".cards")
    card = page.locator(".card", has_text="Wide note")
    expect(card).to_be_visible(timeout=20000)

    def fill() -> float:
        """The share of the sandbox's width that the card takes."""
        return float(
            card.evaluate(
                "(c) => c.getBoundingClientRect().width / c.parentElement.getBoundingClientRect().width"
            )
        )

    # A lone card spans the row (auto-fit drops the empty tracks that auto-fill leaves on the right).
    assert fill() > 0.97, fill()
    # Several cards share the row with no gap at its end: together they reach the right edge.
    call(server, "POST", f"/grids/{grid}/cards", {"kind": "note", "title": "Second note", "body": "Another."})
    expect(page.locator(".card")).to_have_count(2, timeout=20000)
    edge = float(
        cards.evaluate(
            "(row) => Math.max(...[...row.children].map((c) => c.getBoundingClientRect().right))"
            " - row.getBoundingClientRect().right"
        )
    )
    assert abs(edge) < 2, edge


def test_relationships_mark_keys_and_draw_an_erd(browser: Browser, server: dict[str, Any]) -> None:
    rel_orders = pd.DataFrame({"order_id": [1, 2, 3], "cust": ["a", "b", "a"]})
    rel_customers = pd.DataFrame({"customer_id": ["a", "b"], "name": ["Ann", "Bo"]})
    push_client.push(rel_customers, new_grid=True)
    url = push_client.push(rel_orders, new_grid=True)
    model = {
        "name": "rel_test",
        "tables": {
            "rel_orders": {"primary_key": ["order_id"]},
            "rel_customers": {"primary_key": ["customer_id"]},
        },
        "relationships": [
            {
                "from": "rel_orders",
                "from_columns": ["cust"],
                "to": "rel_customers",
                "to_columns": ["customer_id"],
            }
        ],
    }
    call(server, "POST", "/relationships", {"model": model, "replace": True})
    page = open_grid(browser, url)
    # A primary-key column carries a key mark in its header; others don't.
    expect(page.locator(".ag-header-cell[col-id='order_id'] .sag-header-key")).to_be_visible()
    expect(page.locator(".ag-header-cell[col-id='cust'] .sag-header-key")).to_have_count(0)
    page.locator(".ribbon-group").first.get_by_role("button", name="Relationships").click()
    dialog = page.locator("dialog")
    expect(dialog).to_contain_text("No shared models found")  # the package ships no models
    expect(dialog).to_contain_text("rel_orders")
    dialog.get_by_role("button", name="Draw ERD").click()
    expect(page.locator(".workspace")).to_have_attribute("data-view", "agent")
    expect(page.locator(".card .diagram svg")).to_be_visible(timeout=30000)
    call(server, "POST", "/relationships", {"model": {"name": "empty"}, "replace": True})


def test_the_erd_tab_shows_keys_or_says_none_are_defined(browser: Browser, server: dict[str, Any]) -> None:
    erd_orders = pd.DataFrame({"order_id": [1, 2], "cust": ["a", "b"], "note": ["x", "y"]})
    erd_customers = pd.DataFrame({"customer_id": ["a", "b"], "name": ["Ann", "Bo"]})
    push_client.push(erd_customers, new_grid=True)
    page = open_grid(browser, push_client.push(erd_orders, new_grid=True))
    call(server, "POST", "/relationships", {"model": {"name": "empty"}, "replace": True})
    tabs = page.locator(".tabs-row .tab-item")
    expect(tabs.nth(1)).to_have_text("ERD")  # right after Agent, and it can't be closed
    assert tabs.nth(1).get_by_role("button").count() == 0
    tabs.nth(1).click()
    erd = page.locator("#erd-view")
    expect(erd).to_contain_text("No relationships defined for the open tables")
    expect(erd.get_by_role("button", name="Import a relationship file")).to_be_visible()
    model = {
        "name": "erd_test",
        "tables": {
            "erd_orders": {"primary_key": ["order_id"]},
            "erd_customers": {"primary_key": ["customer_id"]},
        },
        "relationships": [
            {
                "from": "erd_orders",
                "from_columns": ["cust"],
                "to": "erd_customers",
                "to_columns": ["customer_id"],
            }
        ],
    }
    call(server, "POST", "/relationships", {"model": model, "replace": True})
    # The diagram: keys only (order_id, cust, customer_id), never the other columns.
    expect(erd.locator(".erd-diagram svg")).to_be_visible(timeout=30000)
    expect(erd.locator(".erd-diagram")).to_contain_text("order_id")
    expect(erd.locator(".erd-diagram")).not_to_contain_text("note")
    call(server, "POST", "/relationships", {"model": {"name": "empty"}, "replace": True})


# ===== open a table from a running Python session =====


def test_the_open_dialog_lists_a_session_opens_its_table_and_keeps_it_current(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    from swissarmygrid.client.watcher import Watcher

    namespace: dict[str, Any] = {"SOD": pd.DataFrame({"a": range(30), "b": range(30)})}
    watcher = Watcher(
        lambda: namespace, "tie_out.ipynb", scan_seconds=0.1, heartbeat_seconds=1.0, poll_seconds=1.0
    )
    watcher.start()
    try:
        page = open_grid(browser, orders_url)
        page.get_by_role("button", name="Open a table from Python").click()
        dialog = page.locator("dialog:has(.open-dialog)")
        expect(dialog.get_by_text("tie_out.ipynb")).to_be_visible(timeout=15000)
        row = dialog.get_by_role("row", name=re.compile("SOD"))
        expect(row).to_contain_text("30 × 2")
        row.get_by_role("button", name="Open").click()
        expect(row.locator(".open-state")).to_be_visible(timeout=15000)
        page.keyboard.press("Escape")
        expect(page.locator(".tab-label", has_text="SOD")).to_be_visible(timeout=15000)

        # Running the cell again assigns a new object: the open tab refreshes, no click needed.
        namespace["SOD"] = pd.DataFrame({"a": range(60), "b": range(60)})
        deadline = time.monotonic() + 15
        rows = 0
        while time.monotonic() < deadline and rows != 60:
            datasets = {d["name"]: d for d in call(server, "GET", "/datasets")["datasets"]}
            rows = datasets.get("SOD", {}).get("rows", 0)
            time.sleep(0.1)
        assert rows == 60
    finally:
        watcher.stop()
        with contextlib.suppress(Exception):  # already gone: nothing to clean
            call(server, "DELETE", "/datasets/SOD")


# ===== an agent looks at the page =====


def agent_snapshot(info: dict[str, Any], body: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    """What an agent's app_snapshot does over HTTP: ask, wait, get the PNG and its headers."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}/snapshots",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {info['token']}",
            "Content-Type": "application/json",
            "X-Actor": "agent",
        },
    )
    with urllib.request.urlopen(request, timeout=90) as response:  # noqa: S310
        return response.read(), {k.lower(): v for k, v in response.headers.items()}


def snapshot_while_page_runs(
    page: Page, info: dict[str, Any], body: dict[str, Any]
) -> tuple[bytes, dict[str, str]]:
    """The agent's request waits on the page, and Playwright's sync page only runs its scripts while the
    test thread calls into it, so the request goes on a thread and the test keeps the page turning."""
    import threading

    result: dict[str, Any] = {}

    def ask() -> None:
        try:
            result["reply"] = agent_snapshot(info, body)
        except Exception as error:  # surfaced below, on the test thread
            result["error"] = error

    thread = threading.Thread(target=ask)
    thread.start()
    while thread.is_alive():
        page.wait_for_timeout(100)
    if "error" in result:
        raise result["error"]
    reply: tuple[bytes, dict[str, str]] = result["reply"]
    return reply


def png_size(data: bytes) -> tuple[int, int]:
    import struct

    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def test_an_agent_snapshot_is_the_page_as_the_user_sees_it(
    browser: Browser, orders_url: str, server: dict[str, Any], tmp_path: Path
) -> None:
    page = open_grid(browser, orders_url, width=1280, height=800)
    wait_for_cell(page, 0, "order_id", "1")  # the data has loaded, not just the first empty row
    whole, headers = snapshot_while_page_runs(page, server, {"area": "app"})
    assert png_size(whole) == (1280, 800) and headers["x-snapshot-area"] == "app"
    assert len(whole) > 30_000, "a full app screenshot with a grid of text is not a few kilobytes"
    sharp, _ = snapshot_while_page_runs(page, server, {"area": "app", "scale": 2})
    assert png_size(sharp) == (2560, 1600)
    grid_only, _ = snapshot_while_page_runs(page, server, {"area": "grid"})
    width, height = png_size(grid_only)
    assert 400 < width <= 1280 and 200 < height < 800  # the grid area: the window's width, less its bars
    steps, _ = snapshot_while_page_runs(page, server, {"area": "steps"})
    assert png_size(steps)[0] < width
    # Kept for a person to look at when reviewing: the picture the agent would see.
    (SHOTS / "agent-snapshot.png").write_bytes(whole)
    (tmp_path / "seen.png").write_bytes(whole)


def test_an_agent_snapshot_follows_what_the_user_has_done(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    page = open_grid(browser, orders_url, width=1280, height=800)
    wait_for_cell(page, 0, "order_id", "1")
    before, _ = snapshot_while_page_runs(page, server, {"area": "grid"})
    call(
        server,
        "POST",
        f"/grids/{grid_of(orders_url)}/steps",
        {"steps": [{"kind": "filter", "params": {"column": "region", "op": "eq", "value": "North"}}]},
    )
    wait_for_cell(page, 0, "region", "North")
    after, _ = snapshot_while_page_runs(page, server, {"area": "grid"})
    assert before != after, "the picture did not change when the grid did"


# ===== hardening: leaks, big tables, races, the live connection =====

LISTENER_COUNTER = """
(() => {
  const live = new Map();
  const key = (type, options) =>
    `${type}|${typeof options === "boolean" ? options : Boolean(options && options.capture)}`;
  const add = document.addEventListener.bind(document);
  const remove = document.removeEventListener.bind(document);
  document.addEventListener = (type, listener, options) => {
    const k = key(type, options);
    if (!live.has(k)) live.set(k, new Set());
    live.get(k).add(listener);
    add(type, listener, options);
  };
  document.removeEventListener = (type, listener, options) => {
    live.get(key(type, options))?.delete(listener);
    remove(type, listener, options);
  };
  window.__documentListeners = (type) =>
    [...live].filter(([k]) => k.startsWith(`${type}|`)).reduce((n, [, s]) => n + s.size, 0);
})();
"""


def test_a_closed_grid_leaves_no_document_listeners_behind(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    context = browser.new_context(viewport={"width": 1600, "height": 900})
    context.add_init_script(LISTENER_COUNTER)
    page = context.new_page()
    page.goto(orders_url)
    expect(page.locator(".ag-row[row-index='0']").first).to_be_visible(timeout=20000)
    for _ in range(4):
        grid = call(server, "POST", "/grids", {"dataset": "orders"})["grid"]
        expect(page.locator(f".pane[data-grid='{grid}'] .ag-row[row-index='0']").first).to_be_visible(
            timeout=20000
        )
        call(server, "DELETE", f"/grids/{grid}")
        expect(page.locator(f".pane[data-grid='{grid}']")).to_have_count(0, timeout=10000)
    # Each grid view listens for keyup and mousemove on the document: one per open pane, no more.
    panes = page.locator(".pane").count()
    assert page.evaluate("window.__documentListeners('keyup')") == panes
    assert page.evaluate("window.__documentListeners('mousemove')") == panes


def test_a_table_over_a_million_pixels_scrolls_row_for_row(browser: Browser, server: dict[str, Any]) -> None:
    rows = 40_000  # 40,000 rows of 32px: past the 1,000,000px AG Grid measured in the page's flex body
    url = push_client.push(pd.DataFrame({"n": range(rows)}), name="tall", new_grid=True)
    try:
        page = open_grid(browser, url)
        expect(page.locator("#status")).to_contain_text("of 40,000", timeout=15000)
        viewport = page.locator(".pane.active .ag-grid-viewport")
        assert viewport.evaluate("v => v.scrollHeight") > rows * 32  # not "stretched" scrolling
        viewport.evaluate("v => { v.scrollTop = 20000 * 32; }")
        wait_for_cell(page, 20000, "n", "20000")
        expect(page.locator("#status")).to_contain_text("Rows 20,001", timeout=10000)
        # The row numbers fit, at 100% and at 200% zoom.
        fits = (
            "() => { const c = document.querySelector("
            "\".pane.active .ag-row[row-index='39999'] .ag-cell[col-id='__sag_rownum']\");"
            " return Boolean(c) && c.textContent === '40,000' && c.scrollWidth <= c.clientWidth; }"
        )
        viewport.evaluate("v => { v.scrollTop = v.scrollHeight; }")
        page.wait_for_function(fits, timeout=10000)
        for _ in range(8):
            page.keyboard.press("Control+Equal")
        expect(page.locator(".zoom-control output").first).to_have_text("200%")
        page.wait_for_timeout(500)
        viewport.evaluate("v => { v.scrollTop = v.scrollHeight; }")
        page.wait_for_function(fits, timeout=10000)
        page.keyboard.press("Control+0")
    finally:
        call(server, "DELETE", "/datasets/tall")


def test_the_last_row_scrolls_clear_of_the_sideways_scrollbar(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    expect(page.locator("#status")).to_contain_text("of 5,000", timeout=15000)
    viewport = page.locator(".pane.active .ag-grid-viewport")
    viewport.evaluate("v => { v.scrollTop = v.scrollHeight; }")
    last = page.locator(f".pane.active .ag-row[row-index='{ROWS - 1}']").last
    expect(last).to_be_visible(timeout=10000)
    page.wait_for_timeout(300)
    row = last.bounding_box()
    strip = page.locator(".pane.active .ag-body-horizontal-scroll").bounding_box()
    bottom = viewport.evaluate("v => v.getBoundingClientRect().top + v.clientHeight")
    assert row is not None and strip is not None
    assert row["y"] + row["height"] <= min(bottom, strip["y"]) + 1


def test_the_last_tab_clicked_wins_over_a_slow_first_load(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    slow = call(server, "POST", "/grids", {"dataset": "orders"})["grid"]
    try:
        page = open_grid(browser, orders_url)
        first = grid_of(orders_url)
        expect(page.locator(f".tab-item[data-grid='{slow}']")).to_be_visible(timeout=10000)
        page.locator(f".tab-item[data-grid='{first}']").click()

        def delayed(route: Any) -> None:
            time.sleep(1.5)
            route.continue_()

        page.route(re.compile(f"/grids/{slow}$"), delayed)
        # Both clicks inside the page, so the second lands while the first tab's info still loads.
        page.evaluate(
            "([slow, first]) => { for (const id of [slow, first])"
            " document.querySelector(`.tab-item[data-grid='${id}']`).click(); }",
            [slow, first],
        )
        page.wait_for_timeout(2500)
        expect(page.locator(".tab-item.active")).to_have_attribute("data-grid", first)
        expect(page.locator(f".pane[data-grid='{first}']")).to_be_visible()
    finally:
        call(server, "DELETE", f"/grids/{slow}")


SOCKET_KEEPER = """
(() => {
  const Native = window.WebSocket;
  window.__sockets = [];
  window.WebSocket = class extends Native {
    constructor(...args) { super(...args); window.__sockets.push(this); }
  };
})();
"""


def test_the_page_reconnects_when_its_live_socket_drops(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    context = browser.new_context(viewport={"width": 1600, "height": 900})
    context.add_init_script(SOCKET_KEEPER)
    page = context.new_page()
    page.goto(orders_url)
    expect(page.locator("#live")).to_have_text(re.compile("^● live"), timeout=15000)
    page.evaluate("window.__sockets.forEach(s => s.close())")
    expect(page.locator("#live")).to_contain_text("not live")
    # A grid opened while the socket is down still arrives once it is back.
    grid = call(server, "POST", "/grids", {"dataset": "orders"})["grid"]
    try:
        expect(page.locator("#live")).to_have_text(re.compile("^● live"), timeout=15000)
        expect(page.locator(f".tab-item[data-grid='{grid}']")).to_be_visible(timeout=10000)
    finally:
        call(server, "DELETE", f"/grids/{grid}")
    expect(page.locator(f".tab-item[data-grid='{grid}']")).to_have_count(0, timeout=10000)


def test_the_pop_out_windows_start_without_errors(browser: Browser, orders_url: str) -> None:
    path, fragment = orders_url.split("#")
    for panel in ("steps", "keys"):
        page = browser.new_page()
        errors: list[str] = []

        def record(error: Error, errors: list[str] = errors) -> None:
            errors.append(str(error))

        page.on("pageerror", record)
        page.goto(f"{path}?panel={panel}#{fragment}")
        page.wait_for_timeout(2500)
        assert errors == [], f"{panel}: {errors}"


CAPTURE_CLIPBOARD = """() => {
  window.__copied = undefined;
  navigator.clipboard.write = async (items) => {
    window.__copied = await (await items[0].getType('text/plain')).text();
  };
  navigator.clipboard.writeText = async (text) => { window.__copied = text; };
}"""


def test_ctrl_c_copies_a_column_picked_by_its_header_or_used_from_the_ribbon(
    browser: Browser, orders_url: str
) -> None:
    page = open_grid(browser, orders_url)
    page.evaluate(CAPTURE_CLIPBOARD)
    page.locator(".ag-header-cell[col-id='product'] .sag-header-name").click()
    expect(page.locator(".statusbar")).to_contain_text("5,000 cells selected")
    page.keyboard.press("Control+c")
    page.wait_for_function("window.__copied !== undefined", timeout=10000)
    lines = page.evaluate("window.__copied").strip().split("\r\n")
    assert lines[:3] == ["product", "Widget", "Gadget"] and len(lines) == ROWS + 1
    # The focus on a ribbon button just used: Ctrl+C still copies the grid's selection.
    page.evaluate(CAPTURE_CLIPBOARD)
    page.locator(".ribbon").get_by_role("button", name="Auto width", exact=True).nth(1).click()
    page.keyboard.press("Control+c")
    page.wait_for_function("window.__copied !== undefined", timeout=10000)
    assert page.evaluate("window.__copied").startswith("product\r\nWidget")


def test_ctrl_click_on_the_only_selected_header_selects_nothing(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    refused: list[str] = []
    page.on("response", lambda r: refused.append(r.url) if r.status >= 400 else None)
    name = page.locator(".ag-header-cell[col-id='product'] .sag-header-name")
    name.click()
    expect(page.locator(".statusbar")).to_contain_text("5,000 cells selected")
    name.click(modifiers=["Control"])
    expect(page.locator(".statusbar")).not_to_contain_text("selected")
    expect(page.locator(".ribbon-caption").nth(1)).to_contain_text("select a column")
    page.wait_for_timeout(500)
    assert refused == []


def test_a_narrow_window_has_no_sideways_page_scroll(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url, width=420, height=760)
    expect(page.locator(".steps-strip .steps-count")).to_be_visible(timeout=15000)
    assert page.evaluate("document.documentElement.scrollWidth") <= 420
    grid = page.locator(".pane.active .grid-host").bounding_box()
    assert grid is not None and grid["width"] > 200  # most of the width stays with the grid


def test_switching_tabs_asks_for_no_step_counts_and_not_for_hidden_cards(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    other = call(server, "POST", "/grids", {"dataset": "orders"})["grid"]
    try:
        page = open_grid(browser, orders_url)
        first = grid_of(orders_url)
        page.locator(f".tab-item[data-grid='{other}']").click()
        expect(page.locator(f".pane[data-grid='{other}'] .ag-row[row-index='0']").first).to_be_visible(
            timeout=15000
        )
        page.wait_for_timeout(500)
        seen: list[str] = []
        page.on("request", lambda request: seen.append(request.url))
        page.locator(f".tab-item[data-grid='{first}']").click()
        expect(page.locator("#grid-title")).to_contain_text("orders")
        page.wait_for_timeout(1000)
        # The step counts belong to the steps window: the grid page no longer asks for them at all.
        assert sum("/step-counts" in url for url in seen) == 0, seen
        assert not any(url.endswith("/cards") for url in seen), seen  # the Agent tab is not showing
    finally:
        call(server, "DELETE", f"/grids/{other}")


def test_the_sandbox_draws_cleanly_and_a_chart_click_keeps_the_step_counts(
    browser: Browser, orders_url: str, server: dict[str, Any]
) -> None:
    grid = grid_of(orders_url)
    chart = {"mark": "bar", "x": {"field": "product"}, "y": {"aggregate": "count"}}
    cards = [
        call(
            server,
            "POST",
            f"/grids/{grid}/cards",
            {"kind": "chart", "title": "Rows per product", "spec": chart},
        )["card"],
        call(
            server,
            "POST",
            f"/grids/{grid}/cards",
            {"kind": "diagram", "title": "Broken diagram", "body": 'erDiagram\n  A["x"] ||--o{ B : "y"'},
        )["card"],
    ]
    try:
        page = open_grid(browser, orders_url)
        noise: list[str] = []
        page.on("console", lambda m: noise.append(m.text) if m.type in ("warning", "error") else None)
        page.on("response", lambda r: noise.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        open_agent(page)
        # A diagram Mermaid can't parse says so in its card and leaves nothing under the page.
        broken = page.locator(".card", has_text="Broken diagram")
        expect(broken).to_contain_text("could not be drawn", timeout=30000)
        assert page.locator("body > [id*='sag-diagram']").count() == 0
        expect(page.locator("body")).not_to_contain_text("Syntax error in text")
        card = page.locator(".card", has_text="Rows per product")
        card.locator(".chart svg path.mark-rect, .chart svg .mark-rect path").first.click()
        expect(page.locator(".toast").last).to_contain_text("Filtered the grid", timeout=15000)
        back_to_grid(page, orders_url)
        expect(page.locator(".steps-strip .steps-count")).to_have_text("1 step", timeout=15000)
        page.wait_for_timeout(500)
        assert noise == [], noise  # no version warnings from the charts, no refused (409) requests
    finally:
        for card_id in cards:
            call(server, "DELETE", f"/cards/{card_id}")


def test_closing_one_side_of_a_compare_gives_the_other_grid_its_own_rows_back(
    browser: Browser, server: dict[str, Any]
) -> None:
    lefts = pd.DataFrame({"invoice": [1, 1, 2], "amount": [10.0, 11.0, 20.0]})
    rights = pd.DataFrame({"invoice": [1, 2, 3], "amount": [10.0, 25.0, 30.0]})
    right_url = push_client.push(rights, new_grid=True)
    page = open_grid(browser, push_client.push(lefts, new_grid=True))
    tabs = page.locator(".tab-item")
    tabs.filter(has_text="rights").drag_to(tabs.filter(has_text="lefts"))
    picker = page.locator("dialog .compare-dialog")
    expect(picker.locator("tbody tr").first).to_contain_text("invoice", timeout=20000)
    page.locator("dialog").get_by_role("button", name="Compare", exact=True).click()
    left = page.locator(".pane:not([hidden])").nth(0)
    expect(left.locator(".ag-row")).to_have_count(4, timeout=20000)  # keys 1, 1, 2, 3 lined up
    page.locator(f".tab-item[data-grid='{grid_of(right_url)}'] .tab-close").click()
    expect(page.locator(".panes")).to_have_attribute("data-split", "off")
    # lefts is its own three rows again, not the compare's four.
    expect(page.locator(".pane.active .ag-row")).to_have_count(3, timeout=15000)
    expect(page.locator("#status")).to_contain_text("of 3")


def test_a_server_that_stops_answering_gets_one_plain_message(browser: Browser, orders_url: str) -> None:
    page = open_grid(browser, orders_url)
    page.route(re.compile("/steps$"), lambda route: route.abort("connectionrefused"))
    page.locator(".ag-header-cell[col-id='product'] .sag-header-name").click()
    sort = page.locator(".ribbon").get_by_role("button", name="Sort A→Z", exact=True)
    sort.click()
    sort.click()
    expect(page.locator(".toast.error")).to_have_text(re.compile("server is not answering"), timeout=10000)
    page.wait_for_timeout(500)
    expect(page.locator(".toast.error")).to_have_count(1)  # the same message once, not stacked
    expect(page.locator(".toast")).not_to_contain_text("Failed to fetch")


def test_long_names_and_empty_tables_read_well(browser: Browser, server: dict[str, Any]) -> None:
    long = "amount_" + "very_long_name_" * 8
    frame = pd.DataFrame({long: pd.Series([], dtype="int64"), "label": pd.Series([], dtype="object")})
    page = open_grid_without_rows(browser, push_client.push(frame, name="nothing_here", new_grid=True))
    try:
        # One message that says what is going on, not a filter hint plus AG Grid's own line.
        expect(page.locator(".grid-empty")).to_contain_text("nothing_here has no rows.", timeout=15000)
        expect(page.locator(".grid-empty")).not_to_contain_text("filter")
        expect(page.get_by_text("No rows in this view.")).to_have_count(0)
        # A long name is cut at its end with an ellipsis, also in a right-aligned number column.
        name = page.locator(f".ag-header-cell[col-id='{long}'] .sag-header-name")
        expect(name).to_be_visible()
        first_letter_shown = name.evaluate(
            """el => {
              const r = document.createRange();
              r.setStart(el.firstChild, 0);
              r.setEnd(el.firstChild, 1);
              return r.getBoundingClientRect().left >= el.getBoundingClientRect().left - 1;
            }"""
        )
        assert first_letter_shown
        assert name.evaluate(
            "el => getComputedStyle(el).textOverflow === 'ellipsis' && el.scrollWidth > el.clientWidth"
        )
    finally:
        call(server, "DELETE", "/datasets/nothing_here")


def open_grid_without_rows(browser: Browser, url: str) -> Page:
    page = browser.new_context(viewport={"width": 1600, "height": 900}).new_page()
    page.goto(url)
    expect(page.locator(".pane.active .ag-header-cell").first).to_be_visible(timeout=20000)
    return page
