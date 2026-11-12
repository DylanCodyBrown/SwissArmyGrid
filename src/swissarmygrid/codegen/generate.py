"""Code generation entry point: pick the target, generate, verify (plan section 9).

The target matches what was pushed: a DuckDB relation gets DuckDB code, a pandas frame gets
pandas code, records and files get plain Python. Every result is verified before it is offered:
the generated function runs on the same data (codegen/verify.py) and its output is compared,
row by row and in order, with what the grid shows. Only verified code gets the plain copy
button; otherwise the response carries the first differing row.
"""

from __future__ import annotations

from typing import Any

import duckdb

from swissarmygrid.codegen.duckdb_code import generate_duckdb
from swissarmygrid.codegen.python_code import generate_python
from swissarmygrid.codegen.verify import verify
from swissarmygrid.engine.datasets import DatasetInfo
from swissarmygrid.engine.steps import Step, StepError, ViewSettings
from swissarmygrid.engine.views import ViewEngine

TARGETS: tuple[str, ...] = ("duckdb", "pandas", "plain")


def default_target(source_kind: str) -> str:
    if source_kind == "pandas":
        return "pandas"
    if source_kind in ("records", "csv_file", "json_file", "clipboard"):
        return "plain"
    return "duckdb"


def generate_code(
    cursor: duckdb.DuckDBPyConnection,
    views: ViewEngine,
    info: DatasetInfo,
    steps: list[Step],
    settings: ViewSettings,
    target: str | None,
) -> dict[str, Any]:
    target = target or default_target(info.source_kind)
    if target not in TARGETS:
        raise ValueError(f"target must be one of {', '.join(TARGETS)}.")
    compiled = views.compile(cursor, info, steps, settings)
    if compiled.error is not None:
        raise StepError(compiled.error.index, f"Fix this step first: {compiled.error.message}")
    reason: str | None = None
    if target == "duckdb":
        code = generate_duckdb(info, compiled, steps, settings)
    else:
        # The columns entering each step, so generators can expand "*text" and check names.
        stage_columns = [compiled.stages[i].columns for i in range(len(steps))]
        code = generate_python(
            target, info.name, info.columns, stage_columns, steps, settings, info.row_count
        )
    expected = views.window(cursor, info, steps, settings, 0, max(1, info.row_count))
    source = cursor.sql(f"SELECT * FROM {info.table} ORDER BY rowid").to_arrow_table()  # noqa: S608
    outcome = verify(target, code, source, [c.name for c in expected.columns], expected.rows)
    return {
        "target": target,
        "code": code,
        "verified": outcome.identical,
        "difference": outcome.message,
        "rows_checked": len(expected.rows),
        "reason": reason,
    }
