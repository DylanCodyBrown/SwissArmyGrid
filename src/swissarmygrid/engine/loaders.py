"""Loading files and pasted text into Arrow tables, in trusted Python (plan section 3).

DuckDB's own file access is off, so SQL can never read a file; when the user (or an agent,
through MCP) asks to open one, this module reads it with pyarrow and hands the table to the
DatasetStore like any push. Readers stream batch by batch and stop as soon as the decoded size
passes the dataset cap, so a small compressed Parquet file cannot expand into gigabytes
(plan section 6a, "decompression bombs").
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.ipc as ipc
import pyarrow.json as pajson
import pyarrow.parquet as pq
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from swissarmygrid.engine.datasets import DatasetError, DatasetTooLargeError, check_table, mib

SUFFIXES: dict[str, str] = {
    ".csv": "csv_file",
    ".tsv": "csv_file",
    ".txt": "csv_file",
    ".parquet": "parquet_file",
    ".json": "json_file",
    ".jsonl": "json_file",
    ".ndjson": "json_file",
    ".arrow": "arrow",
    ".feather": "arrow",
    ".xlsx": "excel_file",
}


def _collect(batches: Iterable[pa.RecordBatch], schema: Any, max_bytes: int, what: str) -> pa.Table:
    kept: list[pa.RecordBatch] = []
    decoded = 0
    for batch in batches:
        decoded += batch.nbytes
        if decoded > max_bytes:
            raise DatasetTooLargeError(
                f"{what} is more than {mib(max_bytes)} once loaded; the limit is "
                f"{mib(max_bytes)}. Load a smaller file or raise the limit "
                "(SWISSARMYGRID_MAX_DATASET_MB, then restart the server)."
            )
        kept.append(batch)
    return pa.Table.from_batches(kept, schema=schema)


def _checked_path(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        raise DatasetError("Give the full path of the file.")
    suffix = path.suffix.lower()
    if suffix not in SUFFIXES:
        raise DatasetError(
            f"Cannot open {suffix or 'files without an extension'}; supported: " + ", ".join(sorted(SUFFIXES))
        )
    if not path.is_file():
        raise DatasetError(f"No file at {path}.")
    return path


def load_file(path_text: str, max_bytes: int, sheet: str | None = None) -> tuple[pa.Table, str]:
    """Read a CSV, TSV, Parquet, JSON-lines, Arrow or Excel file. Returns (table, source kind).
    `sheet`: which sheet of a workbook (needed when it has more than one)."""
    path = _checked_path(path_text)
    suffix = path.suffix.lower()
    what = f"'{path.name}'"
    try:
        if suffix == ".xlsx":
            table = _read_sheet(path, sheet, max_bytes, what)
        elif suffix in (".csv", ".tsv", ".txt"):
            delimiter = "\t" if suffix == ".tsv" else ","
            reader = pacsv.open_csv(
                path, parse_options=pacsv.ParseOptions(delimiter=delimiter, newlines_in_values=True)
            )
            table = _collect(reader, reader.schema, max_bytes, what)
        elif suffix == ".parquet":
            parquet = pq.ParquetFile(path)
            table = _collect(parquet.iter_batches(batch_size=64_000), parquet.schema_arrow, max_bytes, what)
        elif suffix in (".json", ".jsonl", ".ndjson"):
            if path.stat().st_size > max_bytes:
                raise DatasetTooLargeError(f"{what} is larger than {mib(max_bytes)}.")
            table = pajson.read_json(path)
        else:
            with pa.memory_map(str(path)) as source:
                try:
                    stream = ipc.open_file(source)
                    batches = [stream.get_batch(i) for i in range(stream.num_record_batches)]
                    table = _collect(batches, stream.schema, max_bytes, what)
                except pa.ArrowInvalid:
                    source.seek(0)
                    reader2 = ipc.open_stream(source)
                    table = _collect(reader2, reader2.schema, max_bytes, what)
    except (pa.ArrowInvalid, OSError) as error:
        raise DatasetError(f"Could not read {what}: {error}") from error
    return check_table(table), SUFFIXES[suffix]


# ===== Excel: one sheet at a time, read by openpyxl in read-only (streaming) mode =====


def excel_sheets(path_text: str) -> list[str]:
    """The sheet names of an .xlsx workbook, in workbook order (for the VS Code sheet picker)."""
    path = _checked_path(path_text)
    if path.suffix.lower() != ".xlsx":
        raise DatasetError(f"'{path.name}' is not an Excel workbook (.xlsx).")
    book = _open_book(path)
    try:
        return list(book.sheetnames)
    finally:
        book.close()


def _open_book(path: Path) -> Any:
    try:
        return load_workbook(path, read_only=True, data_only=True)
    except (zipfile.BadZipFile, InvalidFileException, KeyError) as error:
        raise DatasetError(f"Could not read '{path.name}' as an Excel workbook: {error}") from error


def _read_sheet(path: Path, sheet: str | None, max_bytes: int, what: str) -> pa.Table:
    """One sheet as a table: the first row is the header, values are the cached results of
    formulas (data_only), and a column that mixes kinds (numbers and text) becomes text. Blank
    rows inside the data are kept; the blank rows Excel often reports after it are not."""
    book = _open_book(path)
    try:
        names = list(book.sheetnames)
        if sheet is None:
            if len(names) > 1:
                raise DatasetError(
                    f"{what} has {len(names)} sheets ({', '.join(names)}); name the one to load."
                )
            sheet = names[0]
        if sheet not in names:
            raise DatasetError(f"{what} has no sheet {sheet!r}; its sheets are {', '.join(names)}.")
        rows = book[sheet].iter_rows(values_only=True)
        header = list(next(rows, None) or [])
        if not header:
            raise DatasetError(f"Sheet {sheet!r} of {what} is empty.")
        columns: list[list[Any]] = [[] for _ in header]
        blank_run = 0  # blank rows seen since the last row with a value
        size = 0
        for row in rows:
            if all(value is None for value in row):
                blank_run += 1
                continue
            for _ in range(blank_run):
                for column in columns:
                    column.append(None)
            blank_run = 0
            for i, column in enumerate(columns):
                value = row[i] if i < len(row) else None
                column.append(value)
                size += 8 + (len(value) if isinstance(value, str) else 0)
            if size > max_bytes:
                raise DatasetTooLargeError(
                    f"Sheet {sheet!r} of {what} is more than {mib(max_bytes)} once loaded; the limit "
                    f"is {mib(max_bytes)}. Load a smaller sheet or raise the limit "
                    "(SWISSARMYGRID_MAX_DATASET_MB, then restart the server)."
                )
    finally:
        book.close()
    kept = [i for i, name in enumerate(header) if name is not None or any(v is not None for v in columns[i])]
    names_out = _header_names([header[i] for i in kept])
    return pa.table({name: _arrow_column(columns[i]) for name, i in zip(names_out, kept, strict=True)})


def _header_names(cells: list[Any]) -> list[str]:
    """Header cells as unique column names: a blank one becomes column_<n>, a repeat gets _2."""
    names: list[str] = []
    for n, cell in enumerate(cells, start=1):
        base = str(cell).strip() if cell is not None and str(cell).strip() else f"column_{n}"
        name, k = base, 2
        while name in names:
            name, k = f"{base}_{k}", k + 1
        names.append(name)
    return names


def _arrow_column(values: list[Any]) -> pa.Array:
    try:
        return pa.array(values)
    except (pa.ArrowInvalid, pa.ArrowTypeError):
        # Mixed kinds (a number column with a "n/a" in it): keep every value, as text.
        return pa.array([None if v is None else str(v) for v in values], pa.string())


def parse_pasted(text: str, max_bytes: int) -> pa.Table:
    """Tab-separated text copied from Excel: first line is the header (plan section 10)."""
    data = text.encode("utf-8")
    if len(data) > max_bytes:
        raise DatasetTooLargeError(f"The pasted text is larger than {mib(max_bytes)}.")
    if not text.strip():
        raise DatasetError("Nothing to paste.")
    try:
        table = pacsv.read_csv(
            io.BytesIO(data),
            parse_options=pacsv.ParseOptions(delimiter="\t", newlines_in_values=True),
        )
    except pa.ArrowInvalid as error:
        raise DatasetError(f"Could not read the pasted text as a table: {error}") from error
    return check_table(table)
