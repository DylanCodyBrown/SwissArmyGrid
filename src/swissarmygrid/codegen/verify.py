"""Run generated code on the same data and compare it with the grid (plan section 9).

The runner is the plan's "restricted in-process runner with a fixed allowlist, for generated
code only": it only ever executes code this package generated from the step list (user values
appear in it as Python literals, never as code), with an import allowlist, inside a fresh,
locked-down DuckDB connection for the DuckDB target. It is not a sandbox for arbitrary code;
agent-written code never comes through here (that is milestone 9, not built).

Comparison: same column names, same number of rows, same values in the same order. Floats
compare with a relative tolerance of 1e-9 (a sum can differ in its last bit when DuckDB adds in
parallel), integers and floats with equal values count as equal (pandas turns an integer column
with blanks into floats), and pandas' NaN/NaT/NA count as blank.
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pyarrow as pa

from swissarmygrid.engine.database import open_database

_ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {"__future__", "datetime", "fnmatch", "typing", "pandas", "duckdb", "pyarrow"}
)


@dataclass(frozen=True)
class Outcome:
    identical: bool
    message: str | None


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


def run_generated(target: str, code: str, source: pa.Table) -> tuple[list[str], list[list[Any]]]:
    check_imports(code)
    namespace: dict[str, Any] = {"__name__": "swissarmygrid_generated"}
    exec(compile(code, "<generated>", "exec"), namespace)  # noqa: S102 (our own generated code)
    transform = namespace["transform"]
    if target == "duckdb":
        con = open_database("1GiB")
        relation = con.from_arrow(source)
        result = transform(con, relation).to_arrow_table()
        return list(result.column_names), [list(r.values()) for r in result.to_pylist()]
    if target == "pandas":
        frame = transform(source.to_pandas())
        names = [str(c) for c in frame.columns]
        return names, [[_from_pandas(v) for v in row] for row in frame.itertuples(index=False)]
    rows = transform(source.to_pylist())
    names = list(rows[0].keys()) if rows else []
    return names, [list(r.values()) for r in rows]


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
        return (a.date() if isinstance(a, datetime) else a) == (b.date() if isinstance(b, datetime) else b)
    if hasattr(a, "as_tuple") or hasattr(b, "as_tuple"):  # Decimal
        return math.isclose(float(a), float(b), rel_tol=1e-12)
    return bool(a == b)


def compare(
    names: list[str], rows: list[list[Any]], expected_names: list[str], expected: list[list[Any]]
) -> Outcome:
    if rows and names != expected_names:
        return Outcome(False, f"Columns differ: generated {names}, grid {expected_names}.")
    if len(rows) != len(expected):
        return Outcome(False, f"Row counts differ: generated {len(rows):,}, grid {len(expected):,}.")
    for index, (got, want) in enumerate(zip(rows, expected, strict=True)):
        if len(got) != len(want) or not all(same(g, w) for g, w in zip(got, want, strict=False)):
            return Outcome(False, f"First difference at row {index + 1:,}: generated {got!r}, grid {want!r}.")
    return Outcome(True, None)


def verify(
    target: str, code: str, source: pa.Table, expected_names: list[str], expected: list[list[Any]]
) -> Outcome:
    try:
        names, rows = run_generated(target, code, source)
    except Exception as error:  # the generated code failed to run; report, don't crash the server
        return Outcome(False, f"The generated code failed: {type(error).__name__}: {error}")
    return compare(names, rows, expected_names, expected)
