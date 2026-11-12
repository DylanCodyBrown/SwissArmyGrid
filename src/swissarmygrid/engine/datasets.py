"""Datasets: named DuckDB tables with a version number (plan section 4, "Dataset").

DatasetStore is the repository over the in-memory database: one table per dataset, named
"ds_<name>". A push replaces the whole table and bumps the version. The store enforces the
plan's section 6a limits: one dataset's size, the number of datasets, and the total budget
across all of them. Nothing is ever evicted to make room; a refused load says what is using
the space so the user can close something.

All SQL here is built from validated names, never from caller text.
"""

from __future__ import annotations

import io
import re
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.ipc as ipc

from swissarmygrid.settings import MIB

NAME_PATTERN: re.Pattern[str] = re.compile(r"[A-Za-z0-9_-]{1,64}")
RESERVED_PREFIX: str = "__sag_"
SOURCE_KINDS: tuple[str, ...] = (
    "pandas",
    "polars",
    "duckdb",
    "arrow",
    "records",
    "csv_file",
    "json_file",
    "parquet_file",
    "excel_file",
    "clipboard",
    "sql_result",
    "unknown",
)


class DatasetError(ValueError):
    """The pushed data or the request is malformed. Maps to HTTP 400."""


class DatasetTooLargeError(DatasetError):
    """One dataset, or all of them together, would be over a size limit. Maps to HTTP 413."""


class DatasetLimitError(DatasetError):
    """Too many datasets are open. Maps to HTTP 409."""


@dataclass(frozen=True)
class Column:
    name: str
    type: str


@dataclass(frozen=True)
class DatasetInfo:
    name: str
    version: int
    columns: tuple[Column, ...]
    row_count: int
    source_kind: str
    nbytes: int
    # The SELECT that built it (a "+" tab's table), kept so its tab can show and re-run it.
    query: str | None = None

    @property
    def table(self) -> str:
        return quote("ds_" + self.name)


def validate_name(name: str) -> str:
    """Dataset names are [A-Za-z0-9_-]{1,64} (plan section 5.1)."""
    if not NAME_PATTERN.fullmatch(name):
        raise DatasetError(f"Dataset name {name!r} is not allowed. Use 1 to 64 letters, digits, '_' or '-'.")
    return name


def quote(identifier: str) -> str:
    """Quote a DuckDB identifier, so any column name is safe inside SQL."""
    return '"' + identifier.replace('"', '""') + '"'


def mib(size: int) -> str:
    return f"{size / MIB:.1f} MiB"


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


def reserved_column_name(name: str) -> bool:
    """Names a column may not have, in any case (DuckDB matches names without case): the
    engine's own __sag_ columns, and rowid, which would hide the row numbers views read."""
    lowered = name.lower()
    return lowered.startswith(RESERVED_PREFIX) or lowered == "rowid"


def same_name_ignoring_case(names: list[str]) -> tuple[str, str] | None:
    """The first two names DuckDB would take for one (it matches names without case)."""
    seen: dict[str, str] = {}
    for name in names:
        earlier = seen.setdefault(name.lower(), name)
        if earlier != name:
            return earlier, name
    return None


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
