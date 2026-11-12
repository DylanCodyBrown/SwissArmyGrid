"""Run the copied code the way the user will, and compare its result with the grid (plan section 9).

The user pastes the code into their own Python, where the only thing it can count on is their
table, in a variable named after the dataset. So the check runs exactly that text, with `exec`,
in a fresh namespace holding only that variable (rebuilt from the dataset as what was pushed: a
pandas DataFrame, a Polars DataFrame, an Arrow table, a DuckDB relation, or a list of dicts), and
reads back the variable the code assigns (`orders_view`). A copy that needs a server name, an
internal column, a helper the user does not have, or a call to make it do anything fails here.

The runner only ever executes code this package generated from the step list (user values appear
in it as SQL or Python literals, never as code; a SQL step's text has passed the step guard), with
an import allowlist. It is not a sandbox for arbitrary code; agent-written code never comes through
here (that is milestone 9, not built). The DuckDB copy calls `duckdb.sql`, which runs on DuckDB's
default connection, as in the user's Python. In this process that connection is locked down first
(no file system, no extension downloads, no spilling to disk, a memory limit, then the settings
locked), which leaves it able to read Python variables by name, and it is used by one check at a time.

When the server's Python lacks the library the copy hands its result to (pandas for .df(), Polars
for .pl()), the same query runs over an Arrow table instead, and the outcome says so.

Comparison: same column names, same number of rows, same values in the same order. Floats
compare with a relative tolerance of 1e-9 (a sum can differ in its last bit when DuckDB adds in
parallel), integers and floats with equal values count as equal (pandas turns an integer column
with blanks into floats), a date equals a midnight timestamp of that day (pandas has no date
type), and pandas' NaN/NaT/NA count as blank.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import math
import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import duckdb
import pyarrow as pa

_ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {"__future__", "datetime", "fnmatch", "typing", "pandas", "duckdb", "pyarrow", "polars"}
)
VALUE_KINDS: tuple[str, ...] = ("pandas", "polars", "arrow", "duckdb", "records")

_default_connection_lock = threading.Lock()
_default_connection_ready = False


@dataclass(frozen=True)
class Pasted:
    """Code as the user copies it, and how they run it."""

    code: str
    variable: str  # the user's table, by name: the only thing in the namespace
    value: str  # what that variable holds (VALUE_KINDS)
    result: str  # the variable the code assigns
    # Run instead when `value` (or the copy's output call) needs a library this Python lacks.
    fallback: Pasted | None = None
    fallback_note: str | None = None


@dataclass(frozen=True)
class Outcome:
    identical: bool
    message: str | None
    note: str | None = None


def check_imports(code: str) -> None:
    """Refuse generated code whose own import statements leave the allowlist. (Checked on the
    syntax tree: hooking __import__ would also catch imports C extensions make internally.)"""
    for node in ast.walk(ast.parse(code)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for name in names:
            if name.split(".")[0] not in _ALLOWED_IMPORTS:
                raise ImportError(f"Generated code may not import {name}.")


def prepare_default_connection(memory_limit: str) -> None:
    """Lock down DuckDB's default connection once (the first call's limit wins)."""
    global _default_connection_ready
    with _default_connection_lock:
        if _default_connection_ready:
            return
        for statement in (
            "SET disabled_filesystems = 'LocalFileSystem'",
            "SET autoinstall_known_extensions = false",
            "SET autoload_known_extensions = false",
            "SET temp_directory = ''",
            f"SET memory_limit = '{memory_limit.replace(chr(39), '')}'",
            "SET lock_configuration = true",
        ):
            duckdb.execute(statement)
        _default_connection_ready = True


def user_value(kind: str, table: pa.Table) -> Any:
    """The user's table as they pushed it. Raises ImportError when that library is missing here."""
    if kind == "pandas":
        return table.to_pandas()
    if kind == "polars":
        polars: Any = importlib.import_module("polars")  # optional; not a server dependency
        return polars.from_arrow(table)
    if kind == "duckdb":
        return duckdb.from_arrow(table)
    if kind == "records":
        return table.to_pylist()
    return table


def run_pasted(
    pasted: Pasted, source: pa.Table, memory_limit: str = "1GiB"
) -> tuple[list[str], list[list[Any]], str | None]:
    """Run the code as pasted; (column names, rows, a note when the fallback ran instead)."""
    check_imports(pasted.code)
    prepare_default_connection(memory_limit)
    with _default_connection_lock:  # the default connection serves one check at a time
        try:
            value = user_value(pasted.value, source)
        except ImportError:
            if pasted.fallback is None:
                raise
            value = None
    if value is None and pasted.fallback is not None:
        names, rows, _ = run_pasted(pasted.fallback, source, memory_limit)
        return names, rows, pasted.fallback_note
    namespace: dict[str, Any] = {"__builtins__": builtins, "__name__": "__main__", pasted.variable: value}
    with _default_connection_lock:
        exec(compile(pasted.code, "<pasted>", "exec"), namespace)  # noqa: S102 (our own generated code)
        if pasted.result not in namespace:
            raise NameError(f"The code did not assign {pasted.result}.")
        names, rows = result_rows(namespace[pasted.result])
    return names, rows, None


def result_rows(value: Any) -> tuple[list[str], list[list[Any]]]:
    """Column names and rows of whatever the copy produced."""
    if isinstance(value, list):
        names = list(value[0].keys()) if value else []
        return names, [list(r.values()) for r in value]
    module = type(value).__module__.split(".")[0]
    if module == "pandas":
        names = [str(c) for c in value.columns]
        return names, [[_from_pandas(v) for v in row] for row in value.itertuples(index=False)]
    if isinstance(value, duckdb.DuckDBPyRelation):
        value = value.to_arrow_table()
    elif isinstance(value, pa.RecordBatchReader):
        value = value.read_all()
    elif module == "polars":
        value = value.to_arrow()
    if not isinstance(value, pa.Table):
        raise TypeError(f"The code produced a {type(value).__name__}, not a table.")
    return list(value.column_names), [list(r.values()) for r in value.to_pylist()]


def _from_pandas(value: Any) -> Any:
    try:
        import pandas as pd

        if value is pd.NA or value is pd.NaT:
            return None
        if isinstance(value, pd.Timestamp):
            return value.to_pydatetime()
    except ImportError:
        pass
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return (bool(a) == bool(b) and type(a) is type(b)) or (a == b)
    if isinstance(a, int | float) and isinstance(b, int | float):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)
    if isinstance(a, datetime) and isinstance(b, datetime):
        return a == b
    if isinstance(a, date) and isinstance(b, date):
        # pandas has no date type: a DATE comes back as midnight of that day.
        for d in (a, b):
            if isinstance(d, datetime) and d.time() != datetime.min.time():
                return False
        return (a.date() if isinstance(a, datetime) else a) == (b.date() if isinstance(b, datetime) else b)
    if hasattr(a, "as_tuple") or hasattr(b, "as_tuple"):  # Decimal
        return math.isclose(float(a), float(b), rel_tol=1e-12)
    return bool(a == b)


def compare(
    names: list[str], rows: list[list[Any]], expected_names: list[str], expected: list[list[Any]]
) -> Outcome:
    if names != expected_names and (rows or names):
        return Outcome(False, f"Columns differ: generated {names}, grid {expected_names}.")
    if len(rows) != len(expected):
        return Outcome(False, f"Row counts differ: generated {len(rows):,}, grid {len(expected):,}.")
    for index, (got, want) in enumerate(zip(rows, expected, strict=True)):
        if len(got) != len(want) or not all(same(g, w) for g, w in zip(got, want, strict=False)):
            return Outcome(False, f"First difference at row {index + 1:,}: generated {got!r}, grid {want!r}.")
    return Outcome(True, None)


def verify(
    pasted: Pasted,
    source: pa.Table,
    expected_names: list[str],
    expected: list[list[Any]],
    memory_limit: str = "1GiB",
) -> Outcome:
    try:
        names, rows, note = run_pasted(pasted, source, memory_limit)
    except Exception as error:  # the code failed to run; report, don't crash the server
        return Outcome(False, f"The code failed: {type(error).__name__}: {error}")
    outcome = compare(names, rows, expected_names, expected)
    return Outcome(outcome.identical, outcome.message, note)
