"""Export to delimited text, CSV, Excel and Parquet (plan section 10a).

Exports read the grid's own view, so the file always matches the grid. Rows stream out of
DuckDB in Arrow record batches and each format consumes them batch by batch.

Decisions recorded in docs/verified.md:
- Excel is written with xlsxwriter in in_memory mode. Its constant_memory mode would keep
  memory flat but spills rows to temp files, and the plan's default is "nothing written to
  disk". The Excel row limit (1,048,576) caps the size anyway.
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
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import xlsxwriter

from swissarmygrid.engine.datasets import Column
from swissarmygrid.engine.steps import type_family

EXCEL_MAX_ROWS: int = 1_048_576
EXCEL_MAX_CELL_CHARS: int = 32_767
EXCEL_EXACT_DIGITS: int = 15
# Excel's dates start in 1900, and until 1900-03-01 its serial numbers count a 29 February
# 1900 that never was, so other readers (openpyxl, pandas, LibreOffice) read them as other days.
# Earlier dates are written as ISO text.
EXCEL_FIRST_DATE: date = date(1900, 3, 1)
FORMULA_STARTS: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")
FORMATS: dict[str, tuple[str, str]] = {
    "csv": (".csv", "text/csv"),
    "txt": (".txt", "text/plain"),
    "xlsx": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "parquet": (".parquet", "application/vnd.apache.parquet"),
}
_BATCH_ROWS: int = 50_000


class ExportError(ValueError):
    """The export cannot be made as asked. The message says why."""


@dataclass(frozen=True)
class ExportOptions:
    format: str = "csv"
    delimiter: str = ","
    header: bool = True
    quote_all: bool = False
    line_ending: str = "windows"  # or "unix"
    encoding: str = "utf-8"  # or "utf-8-sig" (with byte order mark, for Excel)
    blank: str = ""
    neutralize_formulas: bool = True
    sheet_name: str = "Data"
    provenance: bool = True
    compression: str = "snappy"  # parquet: snappy or zstd
    allow_cell_truncation: bool = False

    @staticmethod
    def from_query(params: dict[str, str]) -> ExportOptions:
        def flag(name: str, default: bool) -> bool:
            value = params.get(name)
            return default if value is None else value in ("1", "true", "yes")

        fmt = params.get("format", "csv")
        if fmt not in FORMATS:
            raise ExportError(f"format must be one of {', '.join(FORMATS)}.")
        delimiter = params.get("delimiter", "\t" if fmt == "txt" else ",")
        delimiter = {"tab": "\t", "pipe": "|", "comma": ",", "semicolon": ";"}.get(delimiter, delimiter)
        if len(delimiter) != 1 or delimiter in ('"', "\r", "\n"):
            raise ExportError("The delimiter must be one character other than a quote or line break.")
        line_ending = params.get("line_ending", "windows")
        encoding = params.get("encoding", "utf-8")
        compression = params.get("compression", "snappy")
        if line_ending not in ("windows", "unix"):
            raise ExportError("line_ending must be windows or unix.")
        if encoding not in ("utf-8", "utf-8-sig"):
            raise ExportError("encoding must be utf-8 or utf-8-sig.")
        if compression not in ("snappy", "zstd"):
            raise ExportError("compression must be snappy or zstd.")
        sheet = params.get("sheet_name", "Data")
        if not sheet or len(sheet) > 31 or any(ch in sheet for ch in "[]:*?/\\"):
            raise ExportError("Sheet names are 1-31 characters without [ ] : * ? / \\.")
        return ExportOptions(
            format=fmt,
            delimiter=delimiter,
            header=flag("header", True),
            quote_all=flag("quote_all", False),
            line_ending=line_ending,
            encoding=encoding,
            blank=params.get("blank", ""),
            neutralize_formulas=flag("neutralize_formulas", True),
            sheet_name=sheet,
            provenance=flag("provenance", True),
            compression=compression,
            allow_cell_truncation=flag("allow_cell_truncation", False),
        )


@dataclass(frozen=True)
class Provenance:
    source: str
    steps: list[str]
    settings: str
    exported_at: str
    rows: int
    extra: dict[str, Any] = field(default_factory=dict)

    def lines(self) -> list[tuple[str, str]]:
        out = [
            ("Source", self.source),
            ("Exported at", self.exported_at),
            ("Rows", f"{self.rows:,}"),
            ("Settings", self.settings),
            ("Steps", "none" if not self.steps else ""),
        ]
        out += [("", step) for step in self.steps]
        return out


def batches(cursor: duckdb.DuckDBPyConnection, sql: str) -> pa.RecordBatchReader:
    reader: pa.RecordBatchReader = cursor.sql(sql).to_arrow_reader(_BATCH_ROWS)
    return reader


def neutralize(text: str) -> str:
    return "'" + text if text.startswith(FORMULA_STARTS) else text


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
) -> bytes:
    if row_count + 1 > EXCEL_MAX_ROWS:
        raise ExportError(
            f"{row_count:,} rows is more than one Excel sheet holds ({EXCEL_MAX_ROWS - 1:,} "
            "data rows). Filter first, or export CSV or Parquet."
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
    r = 0
    for batch in reader:
        # Column by column, one type check per column instead of one per cell: on 1,000,000 rows
        # x 10 columns the export went from about 50 s to 45 s. The rest is xlsxwriter's own work
        # (its write calls, and close(), which writes the XML: over half).
        for i, array in enumerate(batch.columns):
            width = _write_column(sheet, r + 1, i, array, columns[i].name, options, formats)
            widths[i] = max(widths[i], width)
        r += batch.num_rows
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
