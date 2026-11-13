"""Copy as code: generate, then verify what the user will paste (plan section 9).

The text is the domain's (domain/codegen.py). Every result is verified before it is offered:
exactly the copied text runs in a fresh namespace holding only the user's variable (the
`CodeVerifier` port; adapters/codegen.py), and what it assigns is compared, row by row and in order,
with what the grid shows. Only verified code gets the copy buttons; otherwise the response carries
the first differing row.
"""

from __future__ import annotations

from typing import Any

from swissarmygrid.domain.codegen import (
    TARGETS,
    default_target,
    generate_python,
    pasted_for,
    python_code,
    reads_text,
    view_query,
)
from swissarmygrid.domain.datasets import DatasetInfo
from swissarmygrid.domain.sql_guard import SqlGrammar
from swissarmygrid.domain.steps import Step, StepError, ViewSettings
from swissarmygrid.services.ports import CodeVerifier, SqlSession, Views


def generate_code(
    cursor: SqlSession,
    views: Views,
    info: DatasetInfo,
    steps: list[Step],
    settings: ViewSettings,
    target: str | None,
    *,
    verifier: CodeVerifier,
    grammar: SqlGrammar,
) -> dict[str, Any]:
    """The code for `target` (DuckDB by default), checked: `verifier` runs exactly the copied text
    and compares what it assigns with the grid's rows."""
    target = target or default_target(info.source_kind)
    if target not in TARGETS:
        raise ValueError(f"target must be one of {', '.join(TARGETS)}.")
    compiled = views.compile(cursor, info, steps, settings)
    if compiled.error is not None:
        raise StepError(compiled.error.index, f"Fix this step first: {compiled.error.message}")
    sql = view_query(info, compiled, steps, settings, grammar.keywords)
    if target == "duckdb":
        code = python_code(sql, info.name, info.source_kind)
    else:
        # The columns entering each step, so generators can expand "*text" and check names.
        stage_columns = [compiled.stages[i].columns for i in range(len(steps))]
        code = generate_python(
            target, info.name, info.columns, stage_columns, steps, settings, info.row_count
        )
    expected = views.window(cursor, info, steps, settings, 0, max(1, info.row_count))
    pasted = pasted_for(target, info.name, info.source_kind, code, sql)
    outcome = verifier.verify(pasted, cursor, info.table, [c.name for c in expected.columns], expected.rows)
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
