"""Every golden case through the DuckDB engine (the reference implementation)."""

from __future__ import annotations

from typing import Any

import pytest

from swissarmygrid.engine.database import open_database
from swissarmygrid.engine.datasets import DatasetStore
from swissarmygrid.engine.steps import ViewSettings, steps_from_json
from swissarmygrid.engine.views import ViewEngine
from tests.golden.cases import CASES, Case
from tests.golden.tables import to_arrow


def run_engine(case: Case) -> tuple[tuple[str, ...], list[tuple[Any, ...]]]:
    connection = open_database("1GiB")
    store = DatasetStore(connection, 10**9, 10**9, 50)
    info = store.replace("golden", to_arrow(case.table), "arrow")
    engine = ViewEngine(connection, 10**8)
    steps = steps_from_json(case.steps)
    settings = ViewSettings.from_json(case.settings)
    cursor = connection.cursor()
    compiled = engine.compile(cursor, info, steps, settings)
    assert compiled.error is None, str(compiled.error)
    window = engine.window(cursor, info, steps, settings, 0, 1000)
    return tuple(c.name for c in window.columns), [tuple(r) for r in window.rows]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_engine(case: Case) -> None:
    columns, rows = run_engine(case)
    # A case lists only the columns it checks, in output order, when that is a prefix.
    if columns != case.expected_columns:
        keep = [columns.index(c) for c in case.expected_columns]
        rows = [tuple(row[i] for i in keep) for row in rows]
    assert rows == list(case.expected)
