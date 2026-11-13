"""Exports of hostile names and values the format cannot hold as they are. Made-up data only."""

from __future__ import annotations

import datetime as dt
import decimal
import io
import zipfile
from collections.abc import Iterator

import openpyxl
import pyarrow as pa
import pytest

from swissarmygrid.adapters.database import open_database
from swissarmygrid.adapters.datasets import DatasetStore
from swissarmygrid.domain.datasets import Column, DatasetInfo
from swissarmygrid.engine.export import (
    ExportError,
    ExportOptions,
    Provenance,
    batches,
    delimited,
    excel,
)


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

    from swissarmygrid.domain.steps import type_family
    from swissarmygrid.engine.export import _text_value

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


def test_more_columns_than_an_excel_sheet_holds_is_refused() -> None:
    # 16,390 columns went out as 16,384: xlsxwriter drops a cell past column XFD without a word.
    wide = pa.table({f"c{i}": [i] for i in range(16_385)})
    columns = tuple(Column(name, "BIGINT") for name in wide.column_names)
    with pytest.raises(ExportError, match="16,385 columns"):
        excel(_reader(wide, [1]), columns, ExportOptions(format="xlsx"), None, 1)


def _parts(data: bytes) -> dict[str, bytes]:
    """Every part of an xlsx except docProps/core.xml, which carries the time it was written."""
    with zipfile.ZipFile(io.BytesIO(data)) as book:
        return {name: book.read(name) for name in book.namelist() if name != "docProps/core.xml"}


def _reader(table: pa.Table, sizes: list[int]) -> pa.RecordBatchReader:
    """The table in batches of the given sizes, so 16-row blocks straddle batch boundaries."""
    out: list[pa.RecordBatch] = []
    start = 0
    for size in sizes:
        part = table.slice(start, size)
        out.append(
            pa.record_batch(
                [part.column(i).combine_chunks() for i in range(part.num_columns)], schema=table.schema
            )
        )
        start += size
    return pa.RecordBatchReader.from_batches(table.schema, out)


def _duck_type(kind: pa.DataType) -> str:
    """Enough of DuckDB's type names for the writers (they only ask for its family)."""
    if pa.types.is_string(kind) or pa.types.is_null(kind):
        return "VARCHAR"
    if pa.types.is_date(kind):
        return "DATE"
    if pa.types.is_timestamp(kind):
        return "TIMESTAMP WITH TIME ZONE" if kind.tz else "TIMESTAMP"
    if pa.types.is_integer(kind) or pa.types.is_floating(kind) or pa.types.is_decimal(kind):
        return "BIGINT"
    return str(kind).upper()


ROWS = 100
HOSTILE = pa.table(
    {
        # The first rows start blank, so their 16-row block's span starts past column A.
        "gap": pa.array([None] * 20 + list(range(1, 81)), pa.int64()),
        "text": pa.array(
            (["=1+1", "", None, "a\x01b", "_x0041_", "名前 😀", "<&>\"'", " lead", "x" * 70] * 12)[:ROWS]
        ),
        # A timestamp is the first formatted cell (row 3) though its column is right of the date's.
        "day": pa.array([None] * 4 + [dt.date(1899, 12, 31)] + [dt.date(2024, 2, 29)] * 95),
        "at": pa.array(
            [dt.datetime(1850, 1, 1), dt.datetime(2026, 3, 29, 1, 30, 0, 123456)]
            + [None, dt.datetime(1900, 3, 1, 0, 0, 1)] * 49
        ),
        "zoned": pa.array(
            [dt.datetime(2026, 1, 1, 9, tzinfo=dt.UTC), None] * 50, pa.timestamp("us", tz="Asia/Kolkata")
        ),
        "nano": pa.array([10**18 + 1, None, -(10**18)] * 33 + [5], pa.timestamp("ns")),
        "milli": pa.array([10**12, -(10**12), None, 0] * 25, pa.timestamp("ms")),
        "day64": pa.array([10**12, None, -(10**13)] * 33 + [0], pa.date64()),
        "int": pa.array([-(2**63), 2**63 - 1, 10**15 - 1, -(10**15) + 1, 0, None] * 16 + [7] * 4, pa.int64()),
        "uint": pa.array([2**64 - 1, 0, None, 5] * 25, pa.uint64()),
        "small": pa.array([1, None, -3, 127] * 25, pa.int8()),
        "float": pa.array(
            [0.1 + 0.2, float("nan"), float("inf"), -0.0, 5e-324, 1e300, None, -1.5] * 12 + [1.0] * 4
        ),
        "single": pa.array([1.1, None, 2.5, 3.0] * 25, pa.float32()),
        "flag": pa.array([True, None, False] * 33 + [True]),
        "dec": pa.array(
            [decimal.Decimal("12345678901234567890.1234567890"), decimal.Decimal("-1.5"), None] * 33
            + [decimal.Decimal(0)],
            pa.decimal128(30, 10),
        ),
        "time": pa.array([dt.time(23, 59, 59, 999999), None] * 50),
        "list": pa.array([[1, 2], None, []] * 33 + [[3]]),
        "blob": pa.array([b"\x00\x01", None] * 50),
        "nothing": pa.array([None] * ROWS, pa.null()),
    }
)
# Blank in every column at the start and the end (no <row> for those) and in the middle.
SPARSE = pa.table(
    {
        "a": pa.array([None, None, 1, None, None, None, 2] + [None] * 30, pa.int64()),
        "b": pa.array([None] * 17 + ["x"] + [None] * 19),
    }
)


@pytest.mark.parametrize(
    ("table", "sizes"),
    [
        (HOSTILE, [ROWS]),
        (HOSTILE, [7, 33, 1, 0, 59]),
        (HOSTILE, [16, 16, 68]),
        (SPARSE, [37]),
        (SPARSE, [3, 15, 19]),
    ],
)
@pytest.mark.parametrize("provenance", [True, False])
def test_the_fast_excel_writer_writes_what_xlsxwriter_writes(
    table: pa.Table, sizes: list[int], provenance: bool
) -> None:
    """The data sheet's cells are written as XML directly; every part of the file must be the
    same, byte for byte, as when each cell goes through xlsxwriter's own write calls."""
    columns = tuple(Column(f.name, _duck_type(f.type)) for f in table.schema)
    made = Provenance("t (version 1, arrow)", ["Keep rows where gap is 1"], "defaults", "now", 100)
    shown = made if provenance else None
    options = ExportOptions(format="xlsx")
    fast = excel(_reader(table, sizes), columns, options, shown, table.num_rows)
    slow = excel(_reader(table, sizes), columns, options, shown, table.num_rows, fast=False)
    assert _parts(fast) == _parts(slow)


def test_the_fast_excel_writer_refuses_and_truncates_long_text_the_same_way() -> None:
    table = pa.table({"a": [1, 2], "long": ["ok", "y" * 40_000]})
    columns = (Column("a", "BIGINT"), Column("long", "VARCHAR"))
    for fast in (True, False):
        with pytest.raises(ExportError, match="Row 2, column long holds 40,000 characters"):
            excel(_reader(table, [2]), columns, ExportOptions(format="xlsx"), None, 2, fast=fast)
    cut = ExportOptions(format="xlsx", allow_cell_truncation=True)
    fast_cut = excel(_reader(table, [2]), columns, cut, None, 2)
    assert _parts(fast_cut) == _parts(excel(_reader(table, [2]), columns, cut, None, 2, fast=False))
