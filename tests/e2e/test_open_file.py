# ruff: noqa: F811
"""Opening a file as a grid from the page: the Open file button (the app's chooser, a real path) and a
file dropped anywhere (its bytes). Made-up data only, written into the test's own folder."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from playwright.sync_api import Browser, Page, expect

from tests.e2e.test_ui import browser, call, open_grid, orders_url, server  # noqa: F401


def drop(page: Page, name: str, data: bytes) -> None:
    """Drop a file on the page as a person dragging it from their file explorer would."""
    page.evaluate(
        """([name, b64]) => {
            const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
            const transfer = new DataTransfer();
            transfer.items.add(new File([bytes], name));
            for (const type of ["dragover", "drop"]) {
                const init = { dataTransfer: transfer, bubbles: true, cancelable: true };
                const event = new DragEvent(type, init);
                document.body.dispatchEvent(event);
            }
        }""",
        [name, base64.b64encode(data).decode()],
    )


def workbook() -> bytes:
    book = Workbook()
    book.active.title = "Jan"
    book.active.append(["region", "sales"])
    book.active.append(["North", 10])
    feb = book.create_sheet("Feb")
    feb.append(["region", "sales"])
    feb.append(["South", 7])
    sink = io.BytesIO()
    book.save(sink)
    return sink.getvalue()


def test_open_file_and_dropped_files_open_as_grids(
    browser: Browser, orders_url: str, server: dict[str, Any], tmp_path: Path
) -> None:
    folder = tmp_path / "made-up"
    folder.mkdir()
    (folder / "people.csv").write_text("name,city\nAda,Springfield\nGrace,Riverton\n", encoding="utf-8")
    page = open_grid(browser, orders_url)
    tabs = page.locator(".tab-strip .tab, [role=tab]")
    try:
        # Open file…: beside "+", the app's chooser, the file: 3 actions (plus the folder typed here).
        page.get_by_role("button", name="Open file…").click()
        path = page.locator(".file-chooser input[aria-label=Folder]")
        path.fill(str(folder))
        path.press("Enter")
        page.locator(".file-chooser .file-row.file", has_text="people.csv").click()
        expect(tabs.filter(has_text="people")).to_have_count(1, timeout=20000)
        # Dropped anywhere: a CSV opens at once ...
        drop(page, "visits 2026.csv", b"day,visits\n1,5\n2,8\n")
        expect(tabs.filter(has_text="visits_2026")).to_have_count(1, timeout=20000)
        # ... a workbook with two sheets asks which, the first ticked.
        drop(page, "sales.xlsx", workbook())
        dialog = page.locator("dialog", has_text="Open sales.xlsx")
        expect(dialog.get_by_role("checkbox", name="Jan")).to_be_checked()
        expect(dialog.get_by_role("checkbox", name="Feb")).not_to_be_checked()
        dialog.get_by_role("checkbox", name="Feb").check()
        dialog.get_by_role("button", name="Open").click()
        expect(tabs.filter(has_text="sales_Jan")).to_have_count(1, timeout=20000)
        expect(tabs.filter(has_text="sales_Feb")).to_have_count(1, timeout=20000)
    finally:
        for name in ("people", "visits_2026", "sales_Jan", "sales_Feb"):
            try:
                call(server, "DELETE", f"/datasets/{name}")
            except Exception:  # noqa: S110 (not opened: the test failed earlier)
                pass
