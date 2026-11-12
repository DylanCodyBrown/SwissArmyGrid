"""Every golden case through the DuckDB, pandas and plain-Python generators (plan M4).

The generated code runs on the case's input and must return exactly the case's expected rows,
the same rows the engine returns (tests/test_golden_engine.py). A generator that is "almost
right" fails here, which is the point: no copy button for unverified code.
"""

from __future__ import annotations

from typing import Any

import pytest

from swissarmygrid.codegen.generate import generate_code
from swissarmygrid.codegen.verify import compare, run_generated
from swissarmygrid.engine.database import open_database
from swissarmygrid.engine.datasets import DatasetStore
from swissarmygrid.engine.steps import ViewSettings, steps_from_json
from swissarmygrid.engine.views import ViewEngine
from tests.golden.cases import CASES, Case
from tests.golden.tables import to_arrow

TARGETS = ["duckdb", "pandas", "plain"]


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_generator(case: Case, target: str) -> None:
    connection = open_database("1GiB")
    store = DatasetStore(connection, 10**9, 10**9, 50)
    source = to_arrow(case.table)
    info = store.replace("golden", source, "arrow")
    views = ViewEngine(connection, 10**8)
    steps = steps_from_json(case.steps)
    settings = ViewSettings.from_json(case.settings)
    result = generate_code(connection.cursor(), views, info, steps, settings, target)
    assert result["verified"], result["difference"] + "\n\n" + result["code"]

    # And against the hand-written expectation, not just the engine.
    names, rows = run_generated(target, result["code"], source)
    keep = [names.index(c) for c in case.expected_columns]
    trimmed: list[list[Any]] = [[row[i] for i in keep] for row in rows]
    outcome = compare(
        list(case.expected_columns),
        trimmed,
        list(case.expected_columns),
        [list(r) for r in case.expected],
    )
    assert outcome.identical, f"{outcome.message}\n\n{result['code']}"
