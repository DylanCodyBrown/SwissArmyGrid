"""Code generation entry point: generate, then verify what the user will paste (plan section 9).

The default and primary target is "duckdb": the grid's steps as one DuckDB query over the user's
own table, by its variable name (codegen/duckdb_code.py). The response carries it twice: `sql`,
the query alone ("Copy as SQL"), and `code`, `import duckdb` plus the one statement that runs it
("Copy as Python").

The pandas and plain-Python generators stay as secondary targets for people who want code
without DuckDB. They are translations of the steps, longer and with their own imports and helper
functions, so they are never the default. They end by applying the transformation to the user's
variable, like the DuckDB copy, so all three paste and run the same way.

Every result is verified before it is offered: exactly the copied text runs in a fresh namespace
holding only the user's variable (codegen/verify.py), and what it assigns is compared, row by row
and in order, with what the grid shows. Only verified code gets the copy buttons; otherwise the
response carries the first differing row.
"""

from __future__ import annotations

from typing import Any

import duckdb

from swissarmygrid.codegen.duckdb_code import output_call, python_code, variable_name, view_query
from swissarmygrid.codegen.python_code import generate_python
from swissarmygrid.codegen.verify import VALUE_KINDS, Pasted, verify
from swissarmygrid.engine.datasets import DatasetInfo
from swissarmygrid.engine.steps import Step, StepError, ViewSettings
from swissarmygrid.engine.views import ViewEngine

TARGETS: tuple[str, ...] = ("duckdb", "pandas", "plain")
_READS: dict[str, str] = {
    "pandas": "a pandas DataFrame",
    "polars": "a Polars DataFrame",
    "arrow": "an Arrow table",
    "duckdb": "a DuckDB relation",
    "records": "a list of dicts",
}


def default_target(source_kind: str) -> str:
    """DuckDB SQL is the copy for every source (what SAG is for)."""
    return "duckdb"


def pasted_for(target: str, dataset: str, source_kind: str, code: str, sql: str) -> Pasted:
    """How the user runs `code`: in their Python, holding their table under its variable name."""
    var = variable_name(dataset)
    result = f"{var}_view"
    if target == "pandas":
        return Pasted(code, var, "pandas", result)
    if target == "plain":
        return Pasted(code, var, "records", result)
    # The DuckDB copy reads whatever was pushed; a source with no Python table of its own (a
    # file, the clipboard, a list of dicts DuckDB cannot read) is read as a pandas DataFrame.
    value = source_kind if source_kind in VALUE_KINDS and source_kind != "records" else "pandas"
    call = output_call(source_kind)
    fallback = None
    note = None
    if call in (".df()", ".pl()"):
        library = "pandas" if call == ".df()" else "Polars"
        fallback = Pasted(python_code(sql, dataset, source_kind, with_output=False), var, "arrow", result)
        note = (
            f"{library} is not installed beside SwissArmyGrid, so the same query was checked over an "
            f"Arrow table and the final {call} was not run."
        )
    return Pasted(code, var, value, result, fallback, note)


def reads_text(target: str, dataset: str, source_kind: str) -> str:
    var = variable_name(dataset)
    if target == "pandas":
        what = "a pandas DataFrame"
    elif target == "plain":
        what = "a list of dicts"
    elif source_kind in _READS and source_kind != "records":
        what = _READS[source_kind]
    else:  # DuckDB cannot read a list of dicts by name, nor a file the user has no variable for
        what = "a pandas DataFrame, Polars DataFrame or Arrow table"
    return f"Reads {what} named {var} and assigns the result to {var}_view."


def generate_code(
    cursor: duckdb.DuckDBPyConnection,
    views: ViewEngine,
    info: DatasetInfo,
    steps: list[Step],
    settings: ViewSettings,
    target: str | None,
    memory_limit: str = "1GiB",
) -> dict[str, Any]:
    target = target or default_target(info.source_kind)
    if target not in TARGETS:
        raise ValueError(f"target must be one of {', '.join(TARGETS)}.")
    compiled = views.compile(cursor, info, steps, settings)
    if compiled.error is not None:
        raise StepError(compiled.error.index, f"Fix this step first: {compiled.error.message}")
    sql = view_query(info, compiled, steps, settings)
    if target == "duckdb":
        code = python_code(sql, info.name, info.source_kind)
    else:
        # The columns entering each step, so generators can expand "*text" and check names.
        stage_columns = [compiled.stages[i].columns for i in range(len(steps))]
        code = generate_python(
            target, info.name, info.columns, stage_columns, steps, settings, info.row_count
        )
    expected = views.window(cursor, info, steps, settings, 0, max(1, info.row_count))
    source = cursor.sql(f"SELECT * FROM {info.table} ORDER BY rowid").to_arrow_table()  # noqa: S608
    pasted = pasted_for(target, info.name, info.source_kind, code, sql)
    outcome = verify(pasted, source, [c.name for c in expected.columns], expected.rows, memory_limit)
    return {
        "target": target,
        "code": code,
        # The query alone ("Copy as SQL"); the DuckDB code runs exactly this text.
        "sql": sql,
        "variable": pasted.variable,
        "result_variable": pasted.result,
        "reads": reads_text(target, info.name, info.source_kind),
        "verified": outcome.identical,
        "difference": outcome.message,
        "note": outcome.note,
        "rows_checked": len(expected.rows),
        "reason": None,
    }
