"""Excel (.xlsx) files: one sheet becomes one dataset; a workbook with several names the sheet."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import Workbook
from starlette.testclient import TestClient

from swissarmygrid.adapters.datasets import excel_sheets, load_file
from swissarmygrid.domain.datasets import DatasetError
from tests.conftest import AUTH

MAX = 64 * 1024 * 1024


def workbook(path: Path, sheets: dict[str, list[list[object]]]) -> Path:
    book = Workbook()
    book.remove(book.active)
    for name, rows in sheets.items():
        sheet = book.create_sheet(name)
        for row in rows:
            sheet.append(row)
    book.save(path)
    return path


def test_one_sheet_reads_with_its_header_and_types(tmp_path: Path) -> None:
    path = workbook(
        tmp_path / "orders.xlsx",
        {
            "Orders": [
                ["order_id", "region", "amount", "ordered"],
                [1, "North", 120.5, datetime(2026, 1, 5)],
                [None, None, None, None],  # a blank row inside the data: kept
                [3, "South", 80.0, datetime(2026, 2, 1)],
                [None, None, None, None],  # blank rows after the data: dropped
                [None, None, None, None],
            ]
        },
    )
    table, kind = load_file(str(path), MAX)
    assert kind == "excel_file"
    assert table.column_names == ["order_id", "region", "amount", "ordered"]
    assert table.column("order_id").to_pylist() == [1, None, 3]
    assert table.column("ordered").to_pylist()[2] == datetime(2026, 2, 1)
    assert str(table.schema.field("amount").type) == "double"


def test_a_workbook_with_several_sheets_names_the_one_to_load(tmp_path: Path) -> None:
    path = workbook(tmp_path / "book.xlsx", {"Q1": [["a"], [1]], "Q2": [["b"], [2], [3]]})
    assert excel_sheets(str(path)) == ["Q1", "Q2"]
    with pytest.raises(DatasetError, match="2 sheets"):
        load_file(str(path), MAX)
    table, _ = load_file(str(path), MAX, sheet="Q2")
    assert table.column("b").to_pylist() == [2, 3]
    with pytest.raises(DatasetError, match="no sheet 'Q3'"):
        load_file(str(path), MAX, sheet="Q3")


def test_mixed_columns_become_text_and_headers_are_made_unique(tmp_path: Path) -> None:
    path = workbook(
        tmp_path / "messy.xlsx",
        {"Sheet": [["code", None, "code"], [42, "x", 1], ["n/a", "y", 2]]},
    )
    table, _ = load_file(str(path), MAX)
    assert table.column_names == ["code", "column_2", "code_2"]
    assert table.column("code").to_pylist() == ["42", "n/a"]  # a number and a text: all text


def test_a_file_that_is_not_a_workbook_says_so(tmp_path: Path) -> None:
    path = tmp_path / "fake.xlsx"
    path.write_text("not a zip file", encoding="utf-8")
    with pytest.raises(DatasetError, match="as an Excel workbook"):
        load_file(str(path), MAX)


def test_the_routes_list_sheets_and_load_one(client: TestClient, tmp_path: Path) -> None:
    path = workbook(tmp_path / "book.xlsx", {"Q1": [["a"], [1]], "Q2": [["b"], [2], [3]]})
    listed = client.post("/files/sheets", json={"path": str(path)}, headers=AUTH)
    assert listed.json() == {"sheets": ["Q1", "Q2"]}
    refused = client.post("/datasets/book/load", json={"path": str(path)}, headers=AUTH)
    assert refused.status_code == 400 and "Q1, Q2" in refused.text
    loaded = client.post("/datasets/book_Q2/load", json={"path": str(path), "sheet": "Q2"}, headers=AUTH)
    assert loaded.status_code == 200, loaded.text
    rows = client.get(f"/grids/{loaded.json()['grid']}/rows", headers=AUTH).json()["rows"]
    assert [r[0] for r in rows] == [2, 3]


def test_a_dropped_file_opens_from_its_bytes_and_a_workbook_lists_its_sheets(client: TestClient) -> None:
    import io

    from openpyxl import Workbook

    csv = b"order_id,region\n1,North\n2,South\n"
    opened = client.post("/datasets/orders/drop?filename=orders.csv", content=csv, headers=AUTH)
    assert opened.status_code == 200, opened.text
    assert opened.json()["dataset"] == "orders" and opened.json()["grid"]
    assert client.get("/datasets", headers=AUTH).json()["datasets"][0]["rows"] == 2
    book = Workbook()
    book.active.title = "Jan"
    book.active.append(["region", "sales"])
    book.active.append(["North", 10])
    feb = book.create_sheet("Feb")
    feb.append(["region", "sales"])
    feb.append(["South", 7])
    sink = io.BytesIO()
    book.save(sink)
    data = sink.getvalue()
    sheets = client.post("/files/dropped-sheets?filename=sales.xlsx", content=data, headers=AUTH)
    assert sheets.json() == {"sheets": ["Jan", "Feb"]}
    for sheet in ("Jan", "Feb"):
        one = client.post(
            f"/datasets/sales_{sheet}/drop?filename=sales.xlsx&sheet={sheet}", content=data, headers=AUTH
        )
        assert one.status_code == 200 and one.json()["dataset"] == f"sales_{sheet}"
    refused = client.post("/datasets/notes/drop?filename=notes.docx", content=b"made-up", headers=AUTH)
    assert refused.status_code == 400 and ".docx" in refused.text
