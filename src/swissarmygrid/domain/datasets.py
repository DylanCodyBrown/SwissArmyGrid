"""Datasets: a named table with a version number (plan section 4, "Dataset").

The model only: a dataset's description, the rules for its name and its columns' names, and the
errors a refused push raises. The tables themselves live in DuckDB (adapters/datasets.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MIB: int = 1024 * 1024


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
    # Which load of the store this is: no two loads share one, so a table closed and opened again
    # under its old name (version 1 once more) is never taken for the table it replaced.
    serial: int = 0

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
