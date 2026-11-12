"""Every golden case through the DuckDB, pandas and plain-Python generators (plan M4).

The copied code runs exactly as copied, in a fresh namespace holding only the user's table under
its variable name (here `golden`), and what it assigns (`golden_view`) must be exactly the case's
expected rows, the same rows the engine returns (tests/test_golden_engine.py). A generator that is
"almost right" fails here, which is the point: no copy button for unverified code.

All data is made up.
"""

from __future__ import annotations

import ast
from typing import Any

import pytest

from swissarmygrid.codegen.generate import generate_code
from swissarmygrid.codegen.verify import compare, result_rows, user_value
from swissarmygrid.engine.database import open_database
from swissarmygrid.engine.datasets import DatasetStore
from swissarmygrid.engine.steps import ViewSettings, steps_from_json
from swissarmygrid.engine.views import ViewEngine
from tests.golden.cases import CASES, Case
from tests.golden.tables import to_arrow

TARGETS = ["duckdb", "pandas", "plain"]
# What each target's code reads: the DuckDB copy reads what was pushed (pandas here).
VALUE = {"duckdb": "pandas", "pandas": "pandas", "plain": "records"}


def run_as_pasted(code: str, variable: str, value: Any) -> tuple[list[str], list[list[Any]]]:
    """What the user does: paste into a Python that has only their table, run, read the result."""
    namespace: dict[str, Any] = {variable: value}
    exec(compile(code, "<pasted>", "exec"), namespace)  # noqa: S102 (generated code under test)
    return result_rows(namespace[f"{variable}_view"])


def generate(case: Case, target: str, source_kind: str = "pandas") -> dict[str, Any]:
    connection = open_database("1GiB")
    store = DatasetStore(connection, 10**9, 10**9, 50)
    info = store.replace("golden", to_arrow(case.table), source_kind)
    views = ViewEngine(connection, 10**8)
    steps = steps_from_json(case.steps)
    settings = ViewSettings.from_json(case.settings)
    return generate_code(connection.cursor(), views, info, steps, settings, target)


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_generator(case: Case, target: str) -> None:
    result = generate(case, target)
    assert result["verified"], f"{result['difference']}\n\n{result['code']}"
    assert result["note"] is None

    # And against the hand-written expectation, not just the engine.
    names, rows = run_as_pasted(result["code"], "golden", user_value(VALUE[target], to_arrow(case.table)))
    keep = [names.index(c) for c in case.expected_columns]
    trimmed: list[list[Any]] = [[row[i] for i in keep] for row in rows]
    outcome = compare(
        list(case.expected_columns),
        trimmed,
        list(case.expected_columns),
        [list(r) for r in case.expected],
    )
    assert outcome.identical, f"{outcome.message}\n\n{result['code']}"


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_duckdb_copy_is_one_import_and_one_statement(case: Case) -> None:
    """`import duckdb` and `golden_view = duckdb.sql(<the SQL copy>).df()`, nothing else: no
    helper, no server name, no internal column, no pyarrow."""
    result = generate(case, "duckdb")
    code, sql = result["code"], result["sql"]
    tree = ast.parse(code)
    assert len(tree.body) == 2, code
    first, second = tree.body
    assert isinstance(first, ast.Import) and [a.name for a in first.names] == ["duckdb"]
    assert first.names[0].asname is None
    assert isinstance(second, ast.Assign) and ast.unparse(second.targets[0]) == "golden_view"
    call = second.value
    assert isinstance(call, ast.Call) and not call.args
    assert isinstance(call.func, ast.Attribute) and call.func.attr == "df"
    inner = call.func.value
    assert isinstance(inner, ast.Call) and ast.unparse(inner.func) == "duckdb.sql"
    assert len(inner.args) == 1 and isinstance(inner.args[0], ast.Constant)
    text = inner.args[0].value
    assert isinstance(text, str) and text.strip() == sql.strip()  # Python runs exactly "Copy as SQL"
    for internal in ("__sag", "ds_golden", "source", "pyarrow", "def ", "register"):
        assert internal not in code, (internal, code)
    assert "FROM golden" in sql or not case.steps


@pytest.mark.parametrize(
    ("source_kind", "call"),
    [("arrow", ".to_arrow_table()"), ("duckdb", ""), ("pandas", ".df()"), ("records", ".df()")],
)
def test_duckdb_copy_ends_in_what_was_pushed(source_kind: str, call: str) -> None:
    case = next(c for c in CASES if len(c.steps) > 1)
    result = generate(case, "duckdb", source_kind)
    assert result["verified"], f"{result['difference']}\n\n{result['code']}"
    assert result["code"].rstrip().endswith('""")' + call), result["code"]
