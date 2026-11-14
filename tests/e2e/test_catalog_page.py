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
    expect(blades.get_by_role("heading", name="Connections and blades", exact=True)).to_be_visible()
    blades.close()


# ===== portable blades: search, tags, favourites, import, export, blade folders =====


def blade_item(page: Page, name: str) -> Any:
    return page.locator(".blade-item", has=page.locator(f'.blade-name:text-is("{name}")'))


def test_blades_are_found_by_name_description_or_tag_and_favourites_come_first(
    browser: Browser, server: dict[str, Any], warehouse: str
) -> None:
    for name, description, tags in [
        ("Bakery", "Daily bread counts", ["bakery"]),
        ("Orders feed", "What the orders app reads", ["orders-function-app", "sales"]),
        ("Stock", "Shelf stock", ["sales"]),
    ]:
        call(
            server,
            "POST",
            "/blades",
            {"name": name, "kind": "other", "code": "x = 1\n", "description": description, "tags": tags},
        )
    page = open_page(browser, warehouse)
    names = page.locator(".blade-list .blade-name")
    expect(names).to_have_count(3)
    search = page.get_by_label("Search blades")
    search.fill("orders-function")  # a tag
    expect(names).to_have_text(["Orders feed"])
    search.fill("shelf")  # a description
    expect(names).to_have_text(["Stock"])
    search.fill("")
    page.locator(".blade-tags").get_by_role("button", name="sales").click()
    expect(names).to_have_text(["Orders feed", "Stock"])
    page.locator(".blade-tags").get_by_role("button", name="sales").click()  # again: every tag
    expect(names).to_have_count(3)
    blade_item(page, "Stock").get_by_role("button", name="Pin", exact=True).click()
    expect(names.first).to_have_text("Stock")
    expect(blade_item(page, "Stock").get_by_role("button", name="Unpin")).to_have_attribute(
        "aria-pressed", "true"
    )
    pinned = {b["name"]: b["pinned"] for b in call(server, "GET", "/blades")["blades"]}
    assert pinned == {"Bakery": False, "Orders feed": False, "Stock": True}
    page.reload()
    expect(names.first).to_have_text("Stock")  # kept on this computer
    call(server, "PUT", "/blades/Stock/pin", {"pinned": False})
    assert page.errors == []  # type: ignore[attr-defined]
    page.close()


def test_an_imported_blade_file_is_reviewed_first_and_a_taken_name_keeps_both(
    browser: Browser, server: dict[str, Any], warehouse: str, tmp_path: Path
) -> None:
    from swissarmygrid.domain.blades import write_blade

    marker = tmp_path / "ran.txt"
    code = f"open({str(marker)!r}, 'w').write('ran')\nconn = 1\n"
    blade_file = tmp_path / "bakery_db.blade.py"
    blade_file.write_text(
        write_blade(
            {
                "name": "Bakery DB",
                "kind": "other",
                "code": code,
                "variable": "conn",
                "description": "The bakery's made-up database",
                "tags": ["bakery", "orders-function-app"],
            }
        ),
        encoding="utf-8",
    )
    page = open_page(browser, warehouse)
    for round_ in (1, 2):
        with page.expect_file_chooser() as chooser:
            page.get_by_role("button", name="Import blades").click()
        chooser.value.set_files(str(blade_file))
        dialog = page.locator("dialog")
        expect(dialog.locator(".import-card h3")).to_contain_text("Bakery DB")
        expect(dialog).to_contain_text("The bakery's made-up database")
        dialog.locator("summary", has_text="The code").click()
        expect(dialog.locator("pre")).to_contain_text("conn = 1")
        if round_ == 2:  # the name is taken now: keep both is offered first
            expect(dialog.get_by_label("When Bakery DB is taken")).to_have_value("copy")
        assert not marker.exists()  # reviewing runs nothing
        dialog.get_by_role("button", name="Import 1 blade").click()
        expect(dialog).to_have_count(0)
    expect(page.locator(".blade-list .blade-name")).to_have_text(["Bakery DB", "Bakery DB 2"])
    saved = call(server, "GET", "/blades/Bakery%20DB")
    assert saved["tags"] == ["bakery", "orders-function-app"] and saved["code"] == code
    assert not marker.exists()  # nor does importing
    assert page.errors == []  # type: ignore[attr-defined]
    page.close()


def test_a_blade_folder_lists_its_blade_files_and_the_save_screen_exports(
    browser: Browser, server: dict[str, Any], warehouse: str, tmp_path: Path
) -> None:
    from swissarmygrid.domain.blades import write_blade

    folder = tmp_path / "team blades"
    (folder / "apps").mkdir(parents=True)
    (folder / "apps" / "orders.blade.py").write_text(
        write_blade(
            {
                "name": "Orders app DB",
                "kind": "other",
                "code": "conn = 2\n",
                "variable": "conn",
                "tags": ["orders-function-app"],
            }
        ),
        encoding="utf-8",
    )
    (folder / "helper.py").write_text("x = 1\n", encoding="utf-8")  # an ordinary file: not a blade
    page = open_page(browser, warehouse)
    try:
        page.get_by_role("button", name="Blade folders").click()
        dialog = page.locator("dialog")
        dialog.get_by_label("Folder to add").fill(str(folder))
        dialog.get_by_role("button", name="Add").click()
        dialog.get_by_role("button", name="Save").click()
        expect(dialog).to_have_count(0)
        orders = blade_item(page, "Orders app DB")
        expect(orders.locator(".blade-file")).to_have_text("file")
        expect(page.locator(".blade-list .blade-name")).to_have_count(1)
        orders.locator(".blade-name").click()
        expect(page.locator("#detail")).to_contain_text("orders.blade.py")
        expect(page.locator("#detail").get_by_role("button", name="Remove", exact=True)).to_have_count(
            0
        )  # its file is the user's to remove
        page.get_by_label("Description").fill("The orders app's database")
        page.get_by_role("button", name="Save", exact=True).click()
        expect(page.locator(".status-line")).to_contain_text("Saved Orders app DB.")
        assert 'description = "The orders app\'s database"' in (
            folder / "apps" / "orders.blade.py"
        ).read_text(encoding="utf-8")
        with page.expect_download() as download:
            page.locator("#detail").get_by_role("button", name="Export", exact=True).click()
        assert download.value.suggested_filename == "orders_app_db.blade.py"
        assert "conn = 2" in Path(download.value.path()).read_text(encoding="utf-8")
        assert page.errors == []  # type: ignore[attr-defined]
    finally:
        call(server, "PUT", "/blades/folders", {"folders": []})
        page.close()


def test_a_blade_the_agent_proposes_shows_its_code_here_and_is_saved_on_allow(
    browser: Browser, server: dict[str, Any], warehouse: str
) -> None:
    import threading

    page = open_page(browser, warehouse)
    answers: list[Any] = []
    blade = {
        "name": "Agent orders DB",
        "kind": "other",
        "code": "import json\norders = json.loads('[1, 2]')\n",
        "description": "Made-up orders the agent wired up",
        "tags": ["orders-function-app"],
    }
    asking = threading.Thread(
        target=lambda: answers.append(call(server, "POST", "/agent/blades", blade, agent=True))
    )
    asking.start()
    request = page.locator("section.approval")
    expect(request).to_contain_text("The agent wants to save the new blade Agent orders DB")
    expect(request.locator("pre")).to_contain_text("orders = json.loads('[1, 2]')")
    expect(request.locator("pre")).to_contain_text('tags = ["orders-function-app"]')
    request.get_by_role("button", name="Allow").click()
    asking.join(timeout=30)
    assert answers and answers[0]["saved"] == "Agent orders DB"
    expect(blade_item(page, "Agent orders DB")).to_have_count(1)
    expect(page.locator("section.approval")).to_have_count(0)
    assert page.errors == []  # type: ignore[attr-defined]
    page.close()


POSTGRES_STARTER = (
    "import psycopg\n\nconn = psycopg.connect(\n"
    '    host="my-server",\n    port=5432,\n    dbname="my_database",\n'
    '    user="my_user",\n    password=secret("password"),\n)\n'
)


def test_import_suggests_values_for_a_blades_settings_and_writes_them_in(
    browser: Browser, server: dict[str, Any], warehouse: str, tmp_path: Path
) -> None:
    from swissarmygrid.domain.blades import write_blade

    mine = 'import psycopg\nconn = psycopg.connect(host="db01.corp.local", dbname="sales")\n'
    call(server, "POST", "/blades", {"name": "Sales DB", "kind": "sql", "dialect": "postgres", "code": mine})
    blade_file = tmp_path / "orders_db.blade.py"
    blade_file.write_text(
        write_blade(
            {
                "name": "Orders DB",
                "kind": "sql",
                "dialect": "postgres",
                "code": POSTGRES_STARTER,
                "variable": "conn",
            }
        ),
        encoding="utf-8",
    )
    page = open_page(browser, warehouse)
    with page.expect_file_chooser() as chooser:
        page.get_by_role("button", name="Import blades").click()
    chooser.value.set_files(str(blade_file))
    fill = page.locator("dialog fieldset.fill-in")
    expect(fill.locator(".needs-value-badge")).to_have_count(3)  # host, dbname, user
    host = fill.locator(".setting-row", has=page.get_by_label("host (server)"))
    host.get_by_role("button", name="db01.corp.local").click()
    expect(page.get_by_label("host (server)")).to_have_value("db01.corp.local")
    page.get_by_label("dbname (database)").fill("orders")
    page.locator("dialog").get_by_role("button", name="Import 1 blade").click()
    expect(page.locator("dialog")).to_have_count(0)
    saved = call(server, "GET", "/blades/Orders%20DB")["code"]
    assert 'host="db01.corp.local"' in saved and 'dbname="orders"' in saved and 'user="my_user"' in saved
    assert page.errors == []  # type: ignore[attr-defined]
    page.close()


def test_the_save_screen_changes_a_setting_in_the_code(
    browser: Browser, server: dict[str, Any], warehouse: str
) -> None:
    call(
        server,
        "POST",
        "/blades",
        {"name": "Orders DB", "kind": "sql", "dialect": "postgres", "code": POSTGRES_STARTER},
    )
    page = open_page(browser, warehouse)
    blade_item(page, "Orders DB").locator(".blade-name").click()
    settings = page.locator("#detail fieldset", has=page.locator("legend", has_text="Settings"))
    expect(settings.get_by_text("needs your value")).to_have_count(3)
    host = page.get_by_label("host (server)")
    host.fill("db07.corp.local")
    host.press("Enter")
    expect(page.get_by_label("Blade code")).to_have_value(
        POSTGRES_STARTER.replace("my-server", "db07.corp.local")
    )
    expect(settings.get_by_text("needs your value")).to_have_count(2)
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator(".status-line")).to_contain_text("Saved Orders DB.")
    assert 'host="db07.corp.local"' in call(server, "GET", "/blades/Orders%20DB")["code"]
    assert page.errors == []  # type: ignore[attr-defined]
    page.close()


def test_a_long_blade_name_never_hides_the_blade_buttons(
    browser: Browser, server: dict[str, Any], warehouse: str
) -> None:
    for name in ("Quarterly finance extract for the regional managers", "Warehouse"):
        call(server, "POST", "/blades", {"name": name, "kind": "other", "code": "x = 1\n"})
    page = open_page(browser, warehouse)
    listing = page.locator(".blade-list")
    expect(listing.locator(".blade-item")).to_have_count(2)
    assert listing.evaluate("e => e.scrollWidth <= e.clientWidth")
    for item in listing.locator(".blade-item").all():
        expect(item.get_by_role("button", name="Copy the sag.blade line")).to_be_in_viewport()
    page.close()


def test_a_packs_blade_is_read_only_and_a_new_marketplace_takes_blades(
    browser: Browser, server: dict[str, Any], warehouse: str, tmp_path: Path
) -> None:
    import json as json_module

    from swissarmygrid.domain.blades import write_blade

    # A made-up installed pack in this server's own Claude folder (never the machine's).
    root = Path(server["tmp"]) / "claude" / "plugins" / "cache" / "team-blades" / "sales" / "1a2b3c4d5e6f"
    (root / "blades").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text('{"name": "sales"}', encoding="utf-8")
    (root / "blades" / "till.blade.py").write_text(
        write_blade({"name": "Till export", "kind": "other", "code": "rows = [1, 2]\n", "variable": "rows"}),
        encoding="utf-8",
    )
    record = {"plugins": {"sales@team-blades": [{"installPath": str(root), "version": "1a2b3c4d5e6f"}]}}
    (root.parents[3] / "installed_plugins.json").write_text(json_module.dumps(record), encoding="utf-8")
    repo = tmp_path / "data-blades"
    page = open_page(browser, warehouse)
    try:
        till = blade_item(page, "Till export")
        expect(till.locator(".blade-pack")).to_have_text("pack")
        till.locator(".blade-name").click()
        expect(page.locator("#detail")).to_contain_text("From the pack sales@team-blades")
        expect(page.locator("#detail").get_by_role("button", name="Save", exact=True)).to_have_count(0)
        page.locator("#detail").get_by_role("button", name="Save a copy").click()
        expect(blade_item(page, "Till export copy")).to_have_count(1)
        # The Packs window: the installed pack, the commands, and a new marketplace repository.
        page.get_by_role("button", name="Packs").click()
        packs = page.locator("dialog")
        expect(packs.locator(".tang-item", has_text="sales")).to_contain_text("version 1a2b3c4d5e6f")
        expect(packs).to_contain_text("/plugin install <pack>@<marketplace>")
        packs.get_by_role("button", name="New marketplace repository").click()
        form = page.locator("dialog").last
        form.get_by_label("New folder").fill(str(repo))
        form.get_by_label("Marketplace name").fill("data-blades")
        form.get_by_label("First pack's name").fill("bakery")
        form.get_by_role("button", name="Create").click()
        clone = page.locator("dialog .tang-item", has_text="editable clone")
        expect(clone).to_contain_text("bakery", timeout=10000)
        clone.get_by_role("button", name="Add blades and tangs").click()
        adding = page.locator("dialog").last
        adding.get_by_label("Blade Till export copy").check()
        adding.get_by_role("button", name="Add", exact=True).click()
        expect(page.locator(".toast").last).to_contain_text("Added 1 files to bakery")
        written = repo / "packs" / "bakery" / "blades" / "till_export_copy.blade.py"
        assert "rows = [1, 2]" in written.read_text(encoding="utf-8")
        assert page.errors == []  # type: ignore[attr-defined]
    finally:
        page.close()
        call(server, "PUT", "/blades/folders", {"folders": []})
        import shutil

        shutil.rmtree(root.parents[3])


def test_script_as_from_the_right_click_waits_for_sag_paste(
    browser: Browser, warehouse: str, server: dict[str, Any]
) -> None:
    page = open_page(browser, warehouse)
    item(page, "sales").locator(":scope > .node").click()
    orders = item(page, "orders").locator(":scope > .node")
    orders.click(button="right")
    menu = page.get_by_role("menu", name="Script orders as")
    expect(menu.get_by_role("menuitem")).to_have_text(["SELECT *", "SELECT the columns", "Python: pandas"])
    menu.get_by_role("menuitem", name="SELECT the columns").click()
    expect(page.locator(".toast").last).to_contain_text("sag.paste()")
    taken = call(server, "POST", "/python/take")
    assert taken["code"] == "SELECT\n    [order_id],\n    [placed at]\nFROM [sales].[orders];"
