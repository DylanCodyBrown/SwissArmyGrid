"""Datasets in DuckDB, and every way a table arrives: Arrow, JSON, files, pasted text.

DatasetStore is the repository over the in-memory database: one table per dataset, named
"ds_<name>". A push replaces the whole table and bumps the version. The store enforces the
plan's section 6a limits: one dataset's size, the number of datasets, and the total budget
across all of them. Nothing is ever evicted to make room; a refused load says what is using
the space so the user can close something.

All SQL here is built from validated names, never from caller text.

Files (plan section 5.3) are read here, in trusted Python, never by DuckDB: the database is locked
against file access (database.py). CSV, tab-separated text, JSON, Parquet, Arrow and Excel workbooks.
"""

from __future__ import annotations

import io
import json
import re
import threading
import uuid
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.ipc as ipc
import pyarrow.json as pajson
import pyarrow.parquet as pq
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from swissarmygrid.domain.datasets import (
    RESERVED_PREFIX,
    SOURCE_KINDS,
    Column,
    DatasetError,
    DatasetInfo,
    DatasetLimitError,
    DatasetTooLargeError,
    mib,
    quote,
    reserved_column_name,
    same_name_ignoring_case,
    validate_name,
)


def split_json_to_arrow(payload: Any) -> pa.Table:
    """Turn pandas' orient="split" JSON ({"columns": [...], "data": [[...], ...]}) into Arrow.

    Column types are inferred from the JSON values, so dates arrive as text. The Arrow push
    format keeps real types; the push client uses it whenever pyarrow is installed.
    """
    if not isinstance(payload, dict):
        raise DatasetError('Expected a JSON object with "columns" and "data".')
    columns: Any = payload.get("columns")
    data: Any = payload.get("data")
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        raise DatasetError('"columns" must be a list of column names.')
    if not isinstance(data, list) or not all(
        isinstance(row, list) and len(row) == len(columns) for row in data
    ):
        raise DatasetError(f'"data" must be a list of rows, each with {len(columns)} values.')

    arrays: list[pa.Array] = []
    for index, column in enumerate(columns):
        values: list[Any] = [row[index] for row in data]
        try:
            array: pa.Array = pa.array(values)
        except (pa.ArrowInvalid, pa.ArrowTypeError) as error:
            raise DatasetError(f"Column {column!r} mixes value types: {error}") from error
        arrays.append(array)
    return check_table(pa.Table.from_arrays(arrays, names=columns))


def arrow_stream_to_table(body: bytes, max_bytes: int) -> pa.Table:
    """Read an Arrow IPC stream, stopping as soon as the decoded size passes `max_bytes`.

    Arrow streams may carry compressed buffers, so a small body can decode into a huge table
    (a decompression bomb). Reading batch by batch and counting decoded bytes stops that
    before the whole table is in memory.
    """
    try:
        reader = ipc.open_stream(io.BytesIO(body))
        batches: list[pa.RecordBatch] = []
        decoded = 0
        for batch in _batches(reader):
            decoded += batch.nbytes
            if decoded > max_bytes:
                raise DatasetTooLargeError(
                    f"The pushed data decodes to more than {mib(max_bytes)}; the limit is "
                    f"{mib(max_bytes)}. Filter, sample, or select fewer columns before pushing."
                )
            batches.append(batch)
        table = pa.Table.from_batches(batches, schema=reader.schema)
    except pa.ArrowInvalid as error:
        raise DatasetError(f"The body is not a valid Arrow stream: {error}") from error
    return check_table(table)


def _batches(reader: Any) -> Iterator[pa.RecordBatch]:
    while True:
        try:
            yield reader.read_next_batch()
        except StopIteration:
            return


def check_table(table: pa.Table) -> pa.Table:
    """Names unique and not reserved; types DuckDB can store."""
    names: list[str] = list(table.column_names)
    if not names:
        raise DatasetError("The data has no columns.")
    if len(set(names)) != len(names):
        raise DatasetError("Column names must be unique.")
    clash = same_name_ignoring_case(names)
    if clash is not None:
        raise DatasetError(
            f"Columns {clash[0]!r} and {clash[1]!r} differ only in case; DuckDB matches names "
            "without case, so rename one."
        )
    if any(not n for n in names):
        raise DatasetError("Column names must be non-empty.")
    reserved = [n for n in names if reserved_column_name(n)]
    if reserved:
        raise DatasetError(
            f"Column name {reserved[0]!r} is reserved (rowid, and names starting with "
            f"{RESERVED_PREFIX!r}, in any case); rename it before pushing."
        )
    for index, column_field in enumerate(table.schema):
        converted = _storable(column_field.type)
        if converted is not None:
            table = table.set_column(index, column_field.name, table.column(index).cast(converted))
    return table


def _storable(arrow_type: pa.DataType) -> pa.DataType | None:
    """The type to cast a column to so DuckDB can store it (None: it already can). Every cast
    keeps each value exactly."""
    # An all-blank column has Arrow's null type, which DuckDB cannot store; keep it as text.
    if arrow_type == pa.null():
        return pa.string()
    if pa.types.is_float16(arrow_type):
        return pa.float32()
    if pa.types.is_decimal256(arrow_type):
        # DuckDB reads decimals up to 38 digits; a wider one is kept exactly as text.
        if arrow_type.precision <= 38:
            return pa.decimal128(arrow_type.precision, arrow_type.scale)
        return pa.string()
    if pa.types.is_union(arrow_type):
        raise DatasetError("Union columns cannot be stored; convert them to one type first.")
    return None


class DatasetStore:
    """Holds every dataset as a table in the shared in-memory DuckDB database."""

    def __init__(
        self,
        connection: duckdb.DuckDBPyConnection,
        max_dataset_bytes: int,
        max_total_bytes: int,
        max_datasets: int,
    ) -> None:
        self._connection = connection
        self.max_dataset_bytes = max_dataset_bytes
        self.max_total_bytes = max_total_bytes
        self.max_datasets = max_datasets
        self._datasets: dict[str, DatasetInfo] = {}
        # Pushes are serialized so a table, its version and the budget change together.
        self._lock = threading.Lock()

    @property
    def used_bytes(self) -> int:
        return sum(info.nbytes for info in self._datasets.values())

    def replace(self, name: str, table: pa.Table, source_kind: str, query: str | None = None) -> DatasetInfo:
        """Create or replace dataset `name` with `table`, bump its version, and describe it."""
        validate_name(name)
        table = check_table(table)
        size: int = table.nbytes
        if size > self.max_dataset_bytes:
            raise DatasetTooLargeError(
                f"Dataset '{name}' is {mib(size)}; the limit is {mib(self.max_dataset_bytes)}. "
                "Filter, sample, or select fewer columns before pushing, or raise the limit "
                "(SWISSARMYGRID_MAX_DATASET_MB, then restart the server)."
            )
        with self._lock:
            previous = self._datasets.get(name)
            # DuckDB matches table names without case: "ds_orders" is the table of "Orders" too.
            clash = next((n for n in self._datasets if n != name and n.lower() == name.lower()), None)
            if clash is not None:
                raise DatasetError(
                    f"A dataset named {clash!r} is open; names that differ only in case are one "
                    f"table to DuckDB. Push {clash!r} again to replace it, or pick another name."
                )
            if previous is None and len(self._datasets) >= self.max_datasets:
                raise DatasetLimitError(
                    f"{len(self._datasets)} datasets are open, the most allowed. Close one "
                    "before loading another."
                )
            others = self.used_bytes - (previous.nbytes if previous else 0)
            if others + size > self.max_total_bytes:
                listing = ", ".join(
                    f"{i.name} ({mib(i.nbytes)})"
                    for i in sorted(self._datasets.values(), key=lambda i: -i.nbytes)
                    if i.name != name
                )
                raise DatasetTooLargeError(
                    f"Loading '{name}' ({mib(size)}) would pass the {mib(self.max_total_bytes)} "
                    f"budget for all datasets ({mib(others)} in use: {listing}). Close a "
                    "dataset first; nothing is closed automatically."
                )
            cursor: duckdb.DuckDBPyConnection = self._connection.cursor()
            staging: str = "incoming_" + uuid.uuid4().hex
            try:
                cursor.register(staging, table)
                cursor.execute(
                    f"CREATE OR REPLACE TABLE {quote('ds_' + name)} AS SELECT * FROM {staging}"  # noqa: S608 (both names are generated)
                )
            except duckdb.Error as error:
                raise DatasetError(f"DuckDB could not store this data: {error}") from error
            finally:
                cursor.unregister(staging)
            described: list[tuple[str, str]] = cursor.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = ? ORDER BY ordinal_position",
                ["ds_" + name],
            ).fetchall()
            info = DatasetInfo(
                name=name,
                version=(previous.version + 1) if previous else 1,
                columns=tuple(Column(n, t) for n, t in described),
                row_count=table.num_rows,
                source_kind=source_kind if source_kind in SOURCE_KINDS else "unknown",
                nbytes=size,
                query=query,
            )
            self._datasets[name] = info
            return info

    def close(self, name: str) -> bool:
        with self._lock:
            if self._datasets.pop(name, None) is None:
                return False
            self._connection.cursor().execute(f"DROP TABLE IF EXISTS {quote('ds_' + name)}")
            return True

    def get(self, name: str) -> DatasetInfo | None:
        return self._datasets.get(name)

    def all(self) -> list[DatasetInfo]:
        return sorted(self._datasets.values(), key=lambda i: i.name)

    def cursor(self) -> duckdb.DuckDBPyConnection:
        return self._connection.cursor()


# ===== files and pasted text =====
#
# Readers stream batch by batch and stop as soon as the decoded size passes the dataset cap, so a
# small compressed Parquet file cannot expand into gigabytes (plan section 6a, "decompression bombs").


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
