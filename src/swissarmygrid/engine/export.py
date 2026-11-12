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
        writer.writerow([c.name for c in columns])
    for batch in reader:
        for row in zip(*(col.to_pylist() for col in batch.columns), strict=True):
            writer.writerow([_text_value(v, options, text_columns[i]) for i, v in enumerate(row)])
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
    r = 0
    for batch in reader:
        for row in zip(*(col.to_pylist() for col in batch.columns), strict=True):
            r += 1
            for i, value in enumerate(row):
                widths[i] = max(widths[i], min(60, len(str(value)) if value is not None else 0))
                if value is None:
                    continue
                if isinstance(value, bool):
                    sheet.write_boolean(r, i, value)
                elif isinstance(value, int):
                    # Excel numbers hold 15 significant digits; longer integers stay exact as text.
                    if len(str(abs(value))) > EXCEL_EXACT_DIGITS:
                        sheet.write_string(r, i, str(value))
                    else:
                        sheet.write_number(r, i, value)
                elif isinstance(value, float):
                    if value != value or value in (float("inf"), float("-inf")):
                        sheet.write_string(r, i, repr(value))
                    else:
                        sheet.write_number(r, i, value)
                elif isinstance(value, Decimal):
                    if len(value.as_tuple().digits) > EXCEL_EXACT_DIGITS:
                        sheet.write_string(r, i, str(value))
                    else:
                        sheet.write_number(r, i, float(value))
                elif isinstance(value, datetime):
                    if value.tzinfo is not None:
                        # Excel has no time zones; never shift silently, keep the offset as text.
                        sheet.write_string(r, i, value.isoformat())
                    else:
                        sheet.write_datetime(r, i, value, datetime_format)
                elif isinstance(value, date):
                    sheet.write_datetime(r, i, datetime(value.year, value.month, value.day), date_format)
                else:
                    text = value if isinstance(value, str) else str(value)
                    if len(text) > EXCEL_MAX_CELL_CHARS:
                        if not options.allow_cell_truncation:
                            raise ExportError(
                                f"Row {r:,}, column {columns[i].name} holds {len(text):,} "
                                f"characters; an Excel cell holds {EXCEL_MAX_CELL_CHARS:,}. Export "
                                "CSV instead, or allow truncation."
                            )
                        text = text[:EXCEL_MAX_CELL_CHARS]
                    sheet.write_string(r, i, text)
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
