"""Export to delimited text, CSV, Excel and Parquet (plan section 10a).

Exports read the grid's own view, so the file always matches the grid. Rows stream out of
DuckDB in Arrow record batches and each format consumes them batch by batch.

Decisions recorded in docs/verified.md:
- Excel is written with xlsxwriter in in_memory mode. Its constant_memory mode would keep
  memory flat but spills rows to temp files, and the plan's default is "nothing written to
  disk". The Excel row limit (1,048,576) caps the size anyway, and the column limit (16,384)
  is refused with a message (xlsxwriter drops a cell past column XFD without a word). The data
  sheet's cells are written as XML by _SheetData, the same bytes xlsxwriter's write calls give.
- Parquet is written by pyarrow into memory in row groups; DuckDB's own COPY stays disabled.
- Text that starts with = + - @ tab or carriage return could run as a formula when a CSV is
  opened in Excel. By default such text cells get a leading apostrophe in delimited exports.
  Excel exports write every text cell as a string cell (never a formula), so nothing needs
  changing there.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import xlsxwriter
from xlsxwriter.utility import xl_col_to_name

from swissarmygrid.domain.datasets import Column
from swissarmygrid.domain.export import (
    FORMULA_STARTS,
    ExportError,
    ExportOptions,
    Provenance,
    neutralize,
)
from swissarmygrid.domain.steps import type_family

EXCEL_MAX_ROWS: int = 1_048_576
EXCEL_MAX_COLUMNS: int = 16_384
EXCEL_MAX_CELL_CHARS: int = 32_767
EXCEL_EXACT_DIGITS: int = 15
# Excel's dates start in 1900, and until 1900-03-01 its serial numbers count a 29 February
# 1900 that never was, so other readers (openpyxl, pandas, LibreOffice) read them as other days.
# Earlier dates are written as ISO text.
EXCEL_FIRST_DATE: date = date(1900, 3, 1)
_BATCH_ROWS: int = 50_000


def batches(cursor: duckdb.DuckDBPyConnection, sql: str) -> pa.RecordBatchReader:
    reader: pa.RecordBatchReader = cursor.sql(sql).to_arrow_reader(_BATCH_ROWS)
    return reader


def _text_value(value: Any, options: ExportOptions, is_text: bool) -> str:
    if value is None:
        return options.blank
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return neutralize(value) if (is_text and options.neutralize_formulas) else value
    return str(value)


def _column_texts(array: pa.Array, options: ExportOptions, is_text: bool) -> list[str]:
    """_text_value for every value of one column, with the per-type work chosen once."""
    values = array.to_pylist()
    blank = options.blank
    kind = array.type
    if pa.types.is_string(kind) or pa.types.is_large_string(kind) or pa.types.is_string_view(kind):
        if is_text and options.neutralize_formulas:
            return [blank if v is None else ("'" + v if v.startswith(FORMULA_STARTS) else v) for v in values]
        return [blank if v is None else v for v in values]
    if pa.types.is_integer(kind):
        return [blank if v is None else str(v) for v in values]
    if pa.types.is_floating(kind):
        return [blank if v is None else repr(v) for v in values]
    if pa.types.is_boolean(kind):
        return [blank if v is None else ("true" if v else "false") for v in values]
    return [_text_value(v, options, is_text) for v in values]


def delimited(
    reader: pa.RecordBatchReader, columns: tuple[Column, ...], options: ExportOptions
) -> Iterator[bytes]:
    """Delimited text, one encoded chunk per batch. The csv module quotes cells holding the
    delimiter, quotes or line breaks, doubling inner quotes."""
    line_terminator = "\r\n" if options.line_ending == "windows" else "\n"
    text_columns = [type_family(c.type) == "text" for c in columns]
    buffer = io.StringIO()
    writer = csv.writer(
        buffer,
        delimiter=options.delimiter,
        quoting=csv.QUOTE_ALL if options.quote_all else csv.QUOTE_MINIMAL,
        lineterminator=line_terminator,
    )
    first = True

    def flush() -> bytes:
        nonlocal first
        data = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        encoded = data.encode(options.encoding if first else "utf-8")
        first = False
        return encoded

    if options.header:
        # Column names come from the data too, so the header row is neutralized like text cells.
        writer.writerow([neutralize(c.name) if options.neutralize_formulas else c.name for c in columns])
    for batch in reader:
        # Column by column, one type check per column instead of one per cell: on 1,000,000
        # rows x 10 columns this took the export from about 6.2 s to 2.1 s, byte for byte the same.
        texts = [_column_texts(col, options, text_columns[i]) for i, col in enumerate(batch.columns)]
        writer.writerows(zip(*texts, strict=True))
        yield flush()
    tail = flush()
    if tail or first:
        yield tail


def excel(
    reader: pa.RecordBatchReader,
    columns: tuple[Column, ...],
    options: ExportOptions,
    provenance: Provenance | None,
    row_count: int,
    *,
    fast: bool = True,
) -> bytes:
    """The view as an xlsx workbook. `fast` writes the data sheet's cells as XML directly
    (_SheetData), the same bytes as xlsxwriter's own write calls give; False uses those calls."""
    if row_count + 1 > EXCEL_MAX_ROWS:
        raise ExportError(
            f"{row_count:,} rows is more than one Excel sheet holds ({EXCEL_MAX_ROWS - 1:,} "
            "data rows). Filter first, or export CSV or Parquet."
        )
    if len(columns) > EXCEL_MAX_COLUMNS:
        # xlsxwriter drops a cell past column XFD without a word: refuse instead.
        raise ExportError(
            f"{len(columns):,} columns is more than one Excel sheet holds ({EXCEL_MAX_COLUMNS:,}). "
            "Choose fewer columns, or export CSV or Parquet."
        )
    output = io.BytesIO()
    book = xlsxwriter.Workbook(
        output,
        {
            "in_memory": True,
            "strings_to_numbers": False,
            "strings_to_formulas": False,
            "strings_to_urls": False,
        },
    )
    sheet = book.add_worksheet(options.sheet_name)
    bold = book.add_format({"bold": True})
    date_format = book.add_format({"num_format": "yyyy-mm-dd"})
    datetime_format = book.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
    widths = [len(c.name) for c in columns]
    for i, column in enumerate(columns):
        sheet.write_string(0, i, column.name, bold)
    formats = _ExcelFormats(date_format, datetime_format)
    cells = _SheetData(sheet, bold, formats, columns, options) if fast and _SheetData.fits(sheet) else None
    r = 0
    for batch in reader:
        if cells is not None:
            batch_widths = cells.add(batch, r + 1)
        else:
            # Column by column, one type check per column instead of one per cell.
            batch_widths = [
                _write_column(sheet, r + 1, i, array, columns[i].name, options, formats)
                for i, array in enumerate(batch.columns)
            ]
        widths = [max(a, b) for a, b in zip(widths, batch_widths, strict=True)]
        r += batch.num_rows
    if cells is not None:
        cells.finish()
    sheet.freeze_panes(1, 0)
    sheet.autofilter(0, 0, max(r, 1), max(len(columns) - 1, 0))
    for i, width in enumerate(widths):
        sheet.set_column(i, i, max(8, width + 2))
    if provenance is not None:
        info = book.add_worksheet("Provenance")
        for row_index, (label, text) in enumerate(provenance.lines()):
            info.write_string(row_index, 0, label, bold)
            info.write_string(row_index, 1, text)
        info.set_column(0, 0, 14)
        info.set_column(1, 1, 80)
    book.close()
    return output.getvalue()


@dataclass(frozen=True)
class _ExcelFormats:
    date: Any
    datetime: Any


_EXCEL_EXACT_LIMIT: int = 10**EXCEL_EXACT_DIGITS  # integers below this keep every digit as numbers
_WIDEST: int = 60


def _write_column(
    sheet: Any,
    first_row: int,
    index: int,
    array: pa.Array,
    name: str,
    options: ExportOptions,
    formats: _ExcelFormats,
) -> int:
    """Write one batch's column from row `first_row` down; returns its widest text (capped at 60)."""
    values = array.to_pylist()
    kind = array.type
    present = [v for v in values if v is not None]
    if not present:
        return 0
    if pa.types.is_boolean(kind):
        for k, value in enumerate(values):
            if value is not None:
                sheet.write_boolean(first_row + k, index, value)
        return len(str(False)) if False in present else len(str(True))
    if pa.types.is_integer(kind):
        for k, value in enumerate(values):
            if value is None:
                continue
            # Excel numbers hold 15 significant digits; longer integers stay exact as text.
            if -_EXCEL_EXACT_LIMIT < value < _EXCEL_EXACT_LIMIT:
                sheet.write_number(first_row + k, index, value)
            else:
                sheet.write_string(first_row + k, index, str(value))
        # An integer's text is longest at one of the extremes.
        return min(_WIDEST, max(len(str(min(present))), len(str(max(present)))))
    if pa.types.is_floating(kind):
        width = 0
        for k, value in enumerate(values):
            if value is None:
                continue
            if value != value or value in (_INF, _NEG_INF):
                sheet.write_string(first_row + k, index, repr(value))
            else:
                sheet.write_number(first_row + k, index, value)
            if width < _WIDEST:
                width = max(width, len(str(value)))
        return min(_WIDEST, width)
    width = 0
    for k, value in enumerate(values):
        if value is not None:
            _write_value(sheet, first_row + k, index, value, name, options, formats)
            if width < _WIDEST:
                width = max(width, len(str(value)))
    return min(_WIDEST, width)


_INF: float = float("inf")
_NEG_INF: float = float("-inf")


def _write_value(
    sheet: Any, row: int, index: int, value: Any, name: str, options: ExportOptions, formats: _ExcelFormats
) -> None:
    """One cell of any type (the column writer's fallback)."""
    if isinstance(value, bool):
        sheet.write_boolean(row, index, value)
    elif isinstance(value, int):
        if -_EXCEL_EXACT_LIMIT < value < _EXCEL_EXACT_LIMIT:
            sheet.write_number(row, index, value)
        else:
            sheet.write_string(row, index, str(value))
    elif isinstance(value, float):
        if value != value or value in (_INF, _NEG_INF):
            sheet.write_string(row, index, repr(value))
        else:
            sheet.write_number(row, index, value)
    elif isinstance(value, Decimal):
        if len(value.as_tuple().digits) > EXCEL_EXACT_DIGITS:
            sheet.write_string(row, index, str(value))
        else:
            sheet.write_number(row, index, float(value))
    elif isinstance(value, datetime):
        if value.tzinfo is not None or value.date() < EXCEL_FIRST_DATE:
            # Excel has no time zones; never shift silently, keep the offset as text.
            # Nor dates before EXCEL_FIRST_DATE: they read back as another day, or #VALUE!.
            sheet.write_string(row, index, value.isoformat())
        else:
            sheet.write_datetime(row, index, value, formats.datetime)
    elif isinstance(value, date):
        if value < EXCEL_FIRST_DATE:
            sheet.write_string(row, index, value.isoformat())
        else:
            sheet.write_datetime(row, index, datetime(value.year, value.month, value.day), formats.date)
    else:
        text = value if isinstance(value, str) else str(value)
        if len(text) > EXCEL_MAX_CELL_CHARS:
            if not options.allow_cell_truncation:
                raise ExportError(
                    f"Row {row:,}, column {name} holds {len(text):,} characters; an Excel cell "
                    f"holds {EXCEL_MAX_CELL_CHARS:,}. Export CSV instead, or allow truncation."
                )
            text = text[:EXCEL_MAX_CELL_CHARS]
        sheet.write_string(row, index, text)


_EXCEL_EPOCH: datetime = datetime(1899, 12, 31)
_EXCEL_EPOCH_ORDINAL: int = _EXCEL_EPOCH.toordinal()
_EXCEL_FIRST_DATETIME: datetime = datetime(
    EXCEL_FIRST_DATE.year, EXCEL_FIRST_DATE.month, EXCEL_FIRST_DATE.day
)


class _SheetData:
    """The data sheet's <sheetData>, written as XML here instead of one xlsxwriter call per cell.

    Byte for byte what xlsxwriter writes when every cell goes through its write calls (the
    reference writer above, _write_column, which stays as the fallback): the same cell XML; the
    shared strings registered in the same order (the header, then each batch column by column);
    format indexes handed out in the order xlsxwriter's row-by-row writing first meets each format;
    rows in blocks of 16 with their "spans"; rows with no cell left out; and the sheet's dimension.
    xlsxwriter still writes every other part of the file. tests/test_export_hostile.py compares
    each part of the file with the reference writer's. On 1,000,000 rows x 10 columns this took
    the export from 59 s to 22 s (docs/verified.md).
    """

    @staticmethod
    def fits(sheet: Any) -> bool:
        """The xlsxwriter internals this relies on are there (written against xlsxwriter 3.3)."""
        return (
            xlsxwriter.__version__.split(".")[0] == "3"
            and bool(sheet.table.get(0))
            and not sheet.constant_memory
            and all(
                hasattr(sheet, name) for name in ("str_table", "_check_dimensions", "_write_sheet_data", "fh")
            )
            and hasattr(sheet.str_table, "_get_shared_string_index")
        )

    def __init__(
        self,
        sheet: Any,
        bold: Any,
        formats: _ExcelFormats,
        columns: tuple[Column, ...],
        options: ExportOptions,
    ) -> None:
        self.sheet = sheet
        self.formats = formats
        self.columns = columns
        self.options = options
        self.letters = [xl_col_to_name(i) for i in range(len(columns))]
        self.shared = sheet.str_table._get_shared_string_index
        self.chunks: list[str] = []
        self.xf: dict[int, int] = {}  # id(format) -> its index, once xlsxwriter's order reaches it
        # The header (row 0) went through write_string, which registered its strings. Its bold
        # is the first format the row-by-row order meets (cell A1).
        bold_index = bold._get_xf_index()
        header = "".join(
            f'<c r="{self.letters[i]}1" s="{bold_index}" t="s"><v>{cell.string}</v></c>'
            for i, cell in sorted(sheet.table[0].items())
        )
        self.block = 0
        self.low, self.high = 0, len(columns) - 1
        self.pending: list[tuple[int, str]] = [(0, header)]
        self.last_row = 0
        sheet._write_sheet_data = self._write

    def add(self, batch: pa.RecordBatch, first_row: int) -> list[int]:
        """Write one batch's rows from sheet row `first_row`; returns each column's widest text."""
        n = batch.num_rows
        if not n:
            return [0] * batch.num_columns
        self._index_formats(batch)
        numbers = [str(first_row + k + 1) for k in range(n)]
        cells: list[list[str | None]] = []
        widths: list[int] = []
        # Each row's first and last column with a cell, for the blocks' spans.
        none = pa.scalar(-1, pa.int64())
        low = high = pc.if_else(pc.is_valid(batch.column(0)), pa.scalar(0, pa.int64()), none)
        for i, array in enumerate(batch.columns):
            column_cells, width = self._column(array, i, first_row, numbers)
            cells.append(column_cells)
            widths.append(width)
            if i:
                valid, at = pc.is_valid(array), pa.scalar(i, pa.int64())
                low = pc.if_else(pc.and_(pc.less(low, 0), valid), at, low)
                high = pc.if_else(valid, at, high)
        for k, (lo, hi, row_cells) in enumerate(
            zip(low.to_pylist(), high.to_pylist(), zip(*cells, strict=True), strict=True)
        ):
            if lo < 0:
                continue  # no cell in this row: xlsxwriter writes no <row> for it
            row = first_row + k
            block = row // 16
            if block != self.block:
                self._flush()
                self.block, self.low, self.high = block, lo, hi
            else:
                self.low, self.high = min(self.low, lo), max(self.high, hi)
            self.pending.append((row, "".join(filter(None, row_cells))))
            self.last_row = row
        return widths

    def finish(self) -> None:
        self._flush()
        self.sheet._check_dimensions(self.last_row, 0)

    def _flush(self) -> None:
        if self.pending:
            spans = f"{self.low + 1}:{self.high + 1}"
            self.chunks.append(
                "".join(f'<row r="{row + 1}" spans="{spans}">{xml}</row>' for row, xml in self.pending)
            )
            self.pending = []

    def _write(self) -> None:
        """Stands in for the sheet's own _write_sheet_data when xlsxwriter assembles the file."""
        self.sheet._xml_start_tag("sheetData")
        for chunk in self.chunks:
            self.sheet.fh.write(chunk)
        self.sheet._xml_end_tag("sheetData")

    def _index_formats(self, batch: pa.RecordBatch) -> None:
        """Give the date and date-time formats their indexes in the order xlsxwriter would: by
        the first cell (row, then column) written with each."""
        first: list[tuple[int, int, Any]] = []
        for i, array in enumerate(batch.columns):
            kind = array.type
            if pa.types.is_date(kind):
                cutoff = pa.scalar(EXCEL_FIRST_DATE, type=kind)
                fmt = self.formats.date
            elif pa.types.is_timestamp(kind) and kind.tz is None:
                cutoff = pa.scalar(_EXCEL_FIRST_DATETIME, type=kind)
                fmt = self.formats.datetime
            else:
                continue
            if id(fmt) in self.xf:
                continue
            at = pc.index(pc.greater_equal(array, cutoff), True).as_py()
            if at is not None and at >= 0:
                first.append((at, i, fmt))
        for _, _, fmt in sorted(first, key=lambda f: (f[0], f[1])):
            self.xf.setdefault(id(fmt), fmt._get_xf_index())

    def _column(
        self, array: pa.Array, index: int, first_row: int, numbers: list[str]
    ) -> tuple[list[str | None], int]:
        """One column's cells as XML (None where blank) and its widest text, as _write_column."""
        if array.null_count == len(array):
            return [None] * len(array), 0
        values = array.to_pylist()
        letter = self.letters[index]
        kind = array.type
        if pa.types.is_boolean(kind):
            cells: list[str | None] = [
                None if v is None else f'<c r="{letter}{n}" t="b"><v>{1 if v else 0}</v></c>'
                for v, n in zip(values, numbers, strict=True)
            ]
            return cells, len(str(False)) if False in values else len(str(True))
        if pa.types.is_integer(kind):
            extremes = pc.min_max(array).as_py()
            lowest, highest = extremes["min"], extremes["max"]
            if -_EXCEL_EXACT_LIMIT < lowest and highest < _EXCEL_EXACT_LIMIT:
                cells = [
                    None if v is None else f'<c r="{letter}{n}"><v>{v:.16G}</v></c>'
                    for v, n in zip(values, numbers, strict=True)
                ]
            else:
                cells = [
                    None if v is None else self._cell(v, letter, n, first_row + k, index)
                    for k, (v, n) in enumerate(zip(values, numbers, strict=True))
                ]
            return cells, min(_WIDEST, max(len(str(lowest)), len(str(highest))))
        if pa.types.is_string(kind) or pa.types.is_large_string(kind) or pa.types.is_string_view(kind):
            longest = pc.max(pc.utf8_length(array)).as_py()
            if longest <= EXCEL_MAX_CELL_CHARS:
                shared = self.shared
                cells = [
                    None if v is None else f'<c r="{letter}{n}" t="s"><v>{shared(v)}</v></c>'
                    for v, n in zip(values, numbers, strict=True)
                ]
                return cells, min(_WIDEST, longest)
        cells = []
        width = 0
        if pa.types.is_floating(kind):
            for value, n in zip(values, numbers, strict=True):
                if value is None:
                    cells.append(None)
                    continue
                if value != value or value in (_INF, _NEG_INF):
                    cells.append(self._text(repr(value), letter, n))
                else:
                    cells.append(f'<c r="{letter}{n}"><v>{value:.16G}</v></c>')
                if width < _WIDEST:
                    width = max(width, len(str(value)))
            return cells, min(_WIDEST, width)
        if pa.types.is_date(kind):
            first_ordinal = EXCEL_FIRST_DATE.toordinal()
            for value, n in zip(values, numbers, strict=True):
                if value is None:
                    cells.append(None)
                    continue
                ordinal = value.toordinal()
                if ordinal < first_ordinal:
                    cells.append(self._text(value.isoformat(), letter, n))
                else:
                    # The serial of midnight: whole days since 1899-12-31, plus Excel's 29 February.
                    cells.append(
                        self._serial(float(ordinal - _EXCEL_EPOCH_ORDINAL) + 1, self.formats.date, letter, n)
                    )
            return cells, len(str(EXCEL_FIRST_DATE))  # str(date) is always YYYY-MM-DD
        if pa.types.is_timestamp(kind) and kind.tz is None and kind.unit != "ns":
            for value, n in zip(values, numbers, strict=True):
                if value is None:
                    cells.append(None)
                    continue
                if value < _EXCEL_FIRST_DATETIME:
                    cells.append(self._text(value.isoformat(), letter, n))
                else:
                    cells.append(self._date_cell(value, self.formats.datetime, letter, n))
                if width < 26 and value.microsecond:
                    width = 26  # str(datetime) adds .ffffff when there are microseconds
            return cells, width or 19
        for k, (value, n) in enumerate(zip(values, numbers, strict=True)):
            if value is None:
                cells.append(None)
                continue
            cells.append(self._cell(value, letter, n, first_row + k, index))
            if width < _WIDEST:
                width = max(width, len(str(value)))
        return cells, min(_WIDEST, width)

    def _cell(self, value: Any, letter: str, n: str, row: int, index: int) -> str:
        """One cell of any type, decided as _write_value decides it."""
        if isinstance(value, bool):
            return f'<c r="{letter}{n}" t="b"><v>{1 if value else 0}</v></c>'
        if isinstance(value, int):
            if -_EXCEL_EXACT_LIMIT < value < _EXCEL_EXACT_LIMIT:
                return f'<c r="{letter}{n}"><v>{value:.16G}</v></c>'
            return self._text(str(value), letter, n)
        if isinstance(value, float):
            if value != value or value in (_INF, _NEG_INF):
                return self._text(repr(value), letter, n)
            return f'<c r="{letter}{n}"><v>{value:.16G}</v></c>'
        if isinstance(value, Decimal):
            if len(value.as_tuple().digits) > EXCEL_EXACT_DIGITS:
                return self._text(str(value), letter, n)
            return f'<c r="{letter}{n}"><v>{float(value):.16G}</v></c>'
        if isinstance(value, datetime):
            if value.tzinfo is not None or value.date() < EXCEL_FIRST_DATE:
                return self._text(value.isoformat(), letter, n)
            return self._date_cell(value, self.formats.datetime, letter, n)
        if isinstance(value, date):
            if value < EXCEL_FIRST_DATE:
                return self._text(value.isoformat(), letter, n)
            return self._date_cell(datetime(value.year, value.month, value.day), self.formats.date, letter, n)
        text = value if isinstance(value, str) else str(value)
        if len(text) > EXCEL_MAX_CELL_CHARS:
            if not self.options.allow_cell_truncation:
                raise ExportError(
                    f"Row {row:,}, column {self.columns[index].name} holds {len(text):,} characters; an "
                    f"Excel cell holds {EXCEL_MAX_CELL_CHARS:,}. Export CSV instead, or allow truncation."
                )
            text = text[:EXCEL_MAX_CELL_CHARS]
        return self._text(text, letter, n)

    def _text(self, text: str, letter: str, n: str) -> str:
        return f'<c r="{letter}{n}" t="s"><v>{self.shared(text[:EXCEL_MAX_CELL_CHARS])}</v></c>'

    def _date_cell(self, value: datetime, fmt: Any, letter: str, n: str) -> str:
        """xlsxwriter's serial number for a date-time from 1900-03-01 on (its
        _datetime_to_excel_datetime, the same float operations in the same order)."""
        delta = value - _EXCEL_EPOCH
        serial = delta.days + (float(delta.seconds) + float(delta.microseconds) / 1e6) / (60 * 60 * 24)
        serial += 1  # Excel's 29 February 1900
        return self._serial(serial, fmt, letter, n)

    def _serial(self, serial: float, fmt: Any, letter: str, n: str) -> str:
        xf = self.xf.get(id(fmt))
        if xf is None:
            xf = self.xf[id(fmt)] = fmt._get_xf_index()
        return f'<c r="{letter}{n}" s="{xf}"><v>{serial:.16G}</v></c>'


def parquet(reader: pa.RecordBatchReader, options: ExportOptions, provenance: Provenance | None) -> bytes:
    """Types survive exactly; provenance goes into the file's key-value metadata."""
    schema = reader.schema
    if provenance is not None:
        schema = schema.with_metadata(
            {
                "swissarmygrid.provenance": json.dumps(
                    {
                        "source": provenance.source,
                        "steps": provenance.steps,
                        "settings": provenance.settings,
                        "exported_at": provenance.exported_at,
                        "rows": provenance.rows,
                    }
                )
            }
        )
    output = io.BytesIO()
    with pq.ParquetWriter(output, schema, compression=options.compression) as writer:
        for batch in reader:
            writer.write_batch(batch.replace_schema_metadata(schema.metadata))
    return output.getvalue()


# ===== the service layer's `ExportWriter` port =====


def write(
    cursor: duckdb.DuckDBPyConnection,
    sql: str,
    columns: tuple[Column, ...],
    options: ExportOptions,
    provenance: Provenance | None,
    row_count: int,
) -> bytes:
    """The rows of `sql` as the bytes of one file in the format the options name."""
    reader = batches(cursor, sql)
    if options.format == "xlsx":
        return excel(reader, columns, options, provenance, row_count)
    if options.format == "parquet":
        return parquet(reader, options, provenance)
    return b"".join(delimited(reader, columns, options))


def save(folder: Path, file_name: str, data: bytes, overwrite: bool) -> tuple[str, bool]:
    """Write an export into the export folder. Returns the file's path and whether it was written:
    an existing file is left alone unless `overwrite`."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / file_name
    if target.exists() and not overwrite:
        return str(target), False
    target.write_bytes(data)
    return str(target), True
