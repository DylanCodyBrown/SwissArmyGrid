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


MIXED = pa.table(
    {
        "text": ["=1+1", None, "plain", "-x", "a,b", 'q"uote', "\ttab", ""],
        "int": pa.array([1, None, -3, 2**62, 0, 7, 8, 9], pa.int64()),
        "small": pa.array([1, 2, None, 4, 5, 6, 7, 8], pa.int8()),
        "float": [1.0, None, float("nan"), 1e308, -0.0, 0.1, float("inf"), 2.5],
        "single": pa.array([1.1, None, 2.5, 3.0, 4.0, 5.0, 6.0, 7.0], pa.float32()),
        "flag": [True, None, False, True, False, True, False, True],
        "day": [dt.date(2024, 1, 1), None, dt.date(1999, 12, 31)] + [dt.date(2000, 1, 1)] * 5,
        "list": [[1, 2], None, [], [3], [4], [5], [6], [7]],
    }
)


@pytest.mark.parametrize(
    "options",
    [ExportOptions(), ExportOptions(neutralize_formulas=False, blank="NULL", quote_all=True, delimiter=";")],
)
def test_the_column_wise_csv_writer_matches_the_cell_by_cell_one(
    store: DatasetStore, options: ExportOptions
) -> None:
    import csv

    from swissarmygrid.engine.export import _text_value
    from swissarmygrid.engine.steps import type_family

    info = store.replace("mixed", MIXED, "arrow")
    got = b"".join(delimited(batches(store.cursor(), whole(info)), info.columns, options))
    buffer = io.StringIO()
    writer = csv.writer(
        buffer,
        delimiter=options.delimiter,
        quoting=csv.QUOTE_ALL if options.quote_all else csv.QUOTE_MINIMAL,
        lineterminator="\r\n",
    )
    writer.writerow([("'" + c.name if c.name.startswith("=") else c.name) for c in info.columns])
    texts = [type_family(c.type) == "text" for c in info.columns]
    for row in store.cursor().execute(whole(info) + " ORDER BY rowid").fetchall():
        writer.writerow([_text_value(v, options, texts[i]) for i, v in enumerate(row)])
    assert got.decode() == buffer.getvalue()


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


def test_the_column_wise_excel_writer_keeps_every_cell_kind(store: DatasetStore) -> None:
    info = store.replace("mixed", MIXED, "arrow")
    data = excel(batches(store.cursor(), whole(info)), info.columns, ExportOptions(format="xlsx"), None, 8)
    sheet = openpyxl.load_workbook(io.BytesIO(data)).active
    assert sheet is not None
    rows = list(sheet.iter_rows(min_row=2, max_row=4, values_only=True))
    single = MIXED.column("single").to_pylist()[0]  # a float32 1.1, as Python reads it
    assert rows[0] == ("=1+1", 1, 1, 1, single, True, dt.datetime(2024, 1, 1), "[1, 2]")
    assert rows[1] == (None, None, 2, None, None, None, None, None)
    assert rows[2] == ("plain", -3, None, "nan", 2.5, False, dt.datetime(1999, 12, 31), "[]")
    # 2**62 has 19 digits, more than an Excel number keeps: it stays exact as text.
    assert sheet.cell(row=5, column=2).value == str(2**62)
    assert sheet.cell(row=8, column=4).value == "inf"
    # Column widths still follow the longest text, between 8 and 62.
    assert sheet.column_dimensions["B"].width >= len(str(2**62))
