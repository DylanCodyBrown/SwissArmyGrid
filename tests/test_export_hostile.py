"""Exports of hostile names and values the format cannot hold as they are. Made-up data only."""

from __future__ import annotations

import datetime as dt
import io
from collections.abc import Iterator

import openpyxl
import pyarrow as pa
import pytest

from swissarmygrid.engine.database import open_database
from swissarmygrid.engine.datasets import DatasetInfo, DatasetStore
from swissarmygrid.engine.export import ExportOptions, batches, delimited, excel


@pytest.fixture
def store() -> Iterator[DatasetStore]:
    connection = open_database("256MiB")
    yield DatasetStore(connection, 1 << 28, 1 << 28, 10)
    connection.close()


def whole(info: DatasetInfo) -> str:
    return f"SELECT * FROM {info.table}"  # noqa: S608 (a generated table name)


def test_header_cells_are_neutralized_like_text_cells(store: DatasetStore) -> None:
    # Column names come from the data too (a CSV someone sent): "=HYPERLINK(...)" went out raw.
    info = store.replace(
        "h", pa.table({'=HYPERLINK("http://example.invalid")': ["a"], "@sum": ["b"]}), "arrow"
    )
    text = b"".join(delimited(batches(store.cursor(), whole(info)), info.columns, ExportOptions())).decode()
    assert text.splitlines()[0] == '"\'=HYPERLINK(""http://example.invalid"")",\'@sum'
    plain = ExportOptions(neutralize_formulas=False)
    raw = b"".join(delimited(batches(store.cursor(), whole(info)), info.columns, plain)).decode()
    assert raw.splitlines()[0] == '"=HYPERLINK(""http://example.invalid"")",@sum'


def test_dates_before_march_1900_go_to_excel_as_text(store: DatasetStore) -> None:
    # Excel's dates start in 1900: 1850-01-01 read back as 1849-12-31, 0001-01-01 as #VALUE!
    # and 1899-12-31 as a bare time; until 1900-03-01 Excel counts a 29 February 1900 that
    # other readers do not.
    info = store.replace(
        "d",
        pa.table(
            {
                "day": pa.array(
                    [dt.date(1, 1, 1), dt.date(1899, 12, 31), dt.date(1900, 3, 1), dt.date(2024, 2, 29)]
                ),
                "at": pa.array(
                    [
                        dt.datetime(1850, 1, 1, 12),
                        dt.datetime(1900, 2, 28, 23, 59),
                        dt.datetime(1900, 3, 1),
                        None,
                    ]
                ),
            }
        ),
        "arrow",
    )
    data = excel(batches(store.cursor(), whole(info)), info.columns, ExportOptions(format="xlsx"), None, 4)
    sheet = openpyxl.load_workbook(io.BytesIO(data)).active
    assert sheet is not None
    rows = list(sheet.iter_rows(min_row=2, values_only=True))
    assert rows == [
        ("0001-01-01", "1850-01-01T12:00:00"),
        ("1899-12-31", "1900-02-28T23:59:00"),
        (dt.datetime(1900, 3, 1), dt.datetime(1900, 3, 1)),
        (dt.datetime(2024, 2, 29), None),
    ]
