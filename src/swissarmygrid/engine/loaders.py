"""Loading files and pasted text into Arrow tables, in trusted Python (plan section 3).

DuckDB's own file access is off, so SQL can never read a file; when the user (or an agent,
through MCP) asks to open one, this module reads it with pyarrow and hands the table to the
DatasetStore like any push. Readers stream batch by batch and stop as soon as the decoded size
passes the dataset cap, so a small compressed Parquet file cannot expand into gigabytes
(plan section 6a, "decompression bombs").
"""

from __future__ import annotations

import io
import json
import re
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
            table = _read_delimited(path, _delimiter(path), max_bytes, what)
        elif suffix == ".parquet":
            parquet = pq.ParquetFile(path)
            table = _collect(parquet.iter_batches(batch_size=64_000), parquet.schema_arrow, max_bytes, what)
        elif suffix in (".json", ".jsonl", ".ndjson"):
            if path.stat().st_size > max_bytes:
                raise DatasetTooLargeError(f"{what} is larger than {mib(max_bytes)}.")
            try:
                table = pajson.read_json(path)
            except pa.ArrowInvalid:
                # pyarrow reads JSON lines of one shape only: an array of records, or values
                # that mix kinds, are read in Python instead (which takes several times the
                # file's size in memory, hence the smaller cap).
                if path.stat().st_size > _PYTHON_JSON_BYTES:
                    raise
                table = _read_json_records(path, what)
        else:
            with pa.memory_map(str(path)) as source:
                try:
                    stream = ipc.open_file(source)
                    # One batch at a time, so a compressed file stops at the limit as it decodes.
                    batches = (stream.get_batch(i) for i in range(stream.num_record_batches))
                    table = _collect(batches, stream.schema, max_bytes, what)
                except pa.ArrowInvalid:
                    source.seek(0)
                    reader2 = ipc.open_stream(source)
                    table = _collect(reader2, reader2.schema, max_bytes, what)
    except (pa.ArrowInvalid, OSError) as error:
        raise DatasetError(f"Could not read {what}: {error}") from error
    return check_table(table), SUFFIXES[suffix]


# ===== Delimited text =====

# Excel's "CSV" on Windows is cp1252; latin-1 reads any bytes at all.
_ENCODINGS: tuple[str, ...] = ("utf8", "cp1252", "latin-1")
_CONVERSION_ERROR: re.Pattern[str] = re.compile(r"In CSV column #(\d+): CSV conversion error")


def _delimiter(path: Path) -> str:
    """Tab for .tsv; comma for .csv; for .txt, tab when the first line has one (the .txt
    export is tab-separated), else comma."""
    suffix = path.suffix.lower()
    if suffix != ".txt":
        return "\t" if suffix == ".tsv" else ","
    with path.open("rb") as handle:
        first = handle.readline(65_536)
    return "\t" if b"\t" in first else ","


def _read_delimited(path: Path, delimiter: str, max_bytes: int, what: str) -> pa.Table:
    """A delimited file as a table, with headers made unique (as for Excel).

    pyarrow types each column from the file's first block, so one stray value further down
    ("oops" in a column of numbers) failed the whole load: such a column is read again as
    text. A file that is not UTF-8 (pyarrow gave its text columns as binary) is read again as
    cp1252, then latin-1."""
    parse = pacsv.ParseOptions(delimiter=delimiter, newlines_in_values=True)
    as_text: dict[str, pa.DataType] = {}
    encodings = iter(_ENCODINGS)
    encoding = next(encodings)
    while True:
        names: list[str] = []
        try:
            reader = pacsv.open_csv(
                path,
                read_options=pacsv.ReadOptions(encoding=encoding),
                parse_options=parse,
                convert_options=pacsv.ConvertOptions(column_types=as_text),
            )
            names = list(reader.schema.names)
            table = _collect(reader, reader.schema, max_bytes, what)
        except (pa.ArrowInvalid, UnicodeDecodeError) as error:
            match = _CONVERSION_ERROR.match(str(error))
            index = int(match.group(1)) if match else -1
            if 0 <= index < len(names) and names[index] not in as_text:
                as_text[names[index]] = pa.string()
                continue
            if isinstance(error, UnicodeDecodeError) or "decode" in str(error).lower():
                encoding = next(encodings, "")
                if encoding:
                    continue
            raise
        if encoding == _ENCODINGS[0] and any(pa.types.is_binary(t) for t in table.schema.types):
            encoding = next(encodings)
            continue
        return table.rename_columns(_header_names(list(table.column_names)))


# ===== JSON =====

_PYTHON_JSON_BYTES: int = 256 * 1024 * 1024


def _read_json_records(path: Path, what: str) -> pa.Table:
    """A JSON array of objects, or JSON lines of objects, read in Python: a column that mixes
    kinds becomes text (as in an Excel sheet), objects and lists in it as JSON."""
    try:
        text = path.read_text(encoding="utf-8-sig")
        stripped = text.lstrip()
        if stripped.startswith("["):
            records: Any = json.loads(stripped)
        else:
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DatasetError(f"Could not read {what} as JSON: {error}") from error
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise DatasetError(
            f"{what} is not a list of records: give an array of objects, or one object per line."
        )
    names: dict[str, None] = {}
    for record in records:
        names.update(dict.fromkeys(str(k) for k in record))
    if not names:
        raise DatasetError(f"{what} has no fields.")
    columns = {name: _arrow_column([record.get(name) for record in records]) for name in names}
    return pa.table(columns)


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
    """Header cells as unique column names: a blank one becomes column_<n>, a repeat gets _2.
    A repeat in another case counts (DuckDB matches names without case)."""
    names: list[str] = []
    taken: set[str] = set()
    for n, cell in enumerate(cells, start=1):
        base = str(cell).strip() if cell is not None and str(cell).strip() else f"column_{n}"
        name, k = base, 2
        while name.lower() in taken:
            name, k = f"{base}_{k}", k + 1
        names.append(name)
        taken.add(name.lower())
    return names


def _arrow_column(values: list[Any]) -> pa.Array:
    try:
        return pa.array(values)
    except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError):
        # Mixed kinds (a number column with a "n/a" in it): keep every value, as text.
        return pa.array([None if v is None else _as_text(v) for v in values], pa.string())


def _as_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False) if isinstance(value, dict | list) else str(value)


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
    return check_table(table.rename_columns(_header_names(list(table.column_names))))
