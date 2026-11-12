"""Names DuckDB treats differently from Python: it matches names without case, and a column
named rowid hides the row numbers the views read. Plus step parameters of the wrong shape.

All data here is made up.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pytest

from swissarmygrid.engine.database import open_database
from swissarmygrid.engine.datasets import DatasetError, DatasetStore
from swissarmygrid.engine.steps import Step, ViewSettings
from swissarmygrid.engine.views import ViewEngine


@pytest.fixture
def engine() -> Iterator[tuple[DatasetStore, ViewEngine]]:
    connection = open_database("256MiB")
    store = DatasetStore(connection, 1 << 28, 1 << 28, 10)
    store.replace("t", pa.table({"x": [1, 2, 3], "y": ["a", "b", "c"]}), "arrow")
    yield store, ViewEngine(connection, 1 << 26)
    connection.close()


def window(engine: tuple[DatasetStore, ViewEngine], steps: list[Step], name: str = "t") -> Any:
    store, views = engine
    info = store.get(name)
    assert info is not None
    cursor = store.cursor()
    compiled = views.compile(cursor, info, steps, ViewSettings())
    if compiled.error is not None:
        return compiled.error.message
    result = views.window(cursor, info, steps, ViewSettings(), 0, 100)
    return [c.name for c in result.columns], result.rows


# ===== datasets =====


def test_dataset_names_differing_only_by_case_are_refused(engine: tuple[DatasetStore, ViewEngine]) -> None:
    store, _ = engine
    store.replace("Orders", pa.table({"x": [1, 2, 3]}), "arrow")
    # DuckDB's "ds_orders" is the same table as "ds_Orders": this push used to replace it.
    with pytest.raises(DatasetError, match="Orders"):
        store.replace("orders", pa.table({"y": ["made-up"]}), "arrow")
    assert window(engine, [], "Orders") == (["x"], [[1], [2], [3]])
    # Re-pushing the same name still works.
    store.replace("Orders", pa.table({"x": [4]}), "arrow")
    assert window(engine, [], "Orders") == (["x"], [[4]])


def test_column_names_differing_only_by_case_are_refused() -> None:
    store = DatasetStore(open_database("256MiB"), 1 << 28, 1 << 28, 10)
    # DuckDB used to keep these as "a" and "A_1", a name the user never gave.
    with pytest.raises(DatasetError, match="case"):
        store.replace("c", pa.table({"a": [1], "A": [2]}), "arrow")


@pytest.mark.parametrize("name", ["rowid", "ROWID", "RowId", "__SAG_ORD", "__Sag_pos", "__sag_x"])
def test_reserved_column_names_are_refused_in_any_case(name: str) -> None:
    store = DatasetStore(open_database("256MiB"), 1 << 28, 1 << 28, 10)
    # A column named rowid hid DuckDB's row numbers: every window came back empty.
    with pytest.raises(DatasetError, match=r"reserved|rowid"):
        store.replace("r", pa.table({name: [10, 20, 30], "v": ["a", "b", "c"]}), "arrow")


def test_a_table_with_no_columns_is_a_plain_error() -> None:
    store = DatasetStore(open_database("256MiB"), 1 << 28, 1 << 28, 10)
    with pytest.raises(DatasetError):
        store.replace("empty", pa.table({}), "arrow")


def test_arrow_types_duckdb_cannot_read_are_converted_or_refused_plainly() -> None:
    import decimal

    store = DatasetStore(open_database("256MiB"), 1 << 28, 1 << 28, 10)
    info = store.replace(
        "types",
        pa.table(
            {
                "half": pa.array([1.5, None], pa.float16()),
                "dec": pa.array([decimal.Decimal("1.25"), None], pa.decimal256(10, 2)),
                "wide": pa.array([decimal.Decimal("1" * 50), None], pa.decimal256(60, 0)),
            }
        ),
        "arrow",
    )
    assert [c.type for c in info.columns] == ["FLOAT", "DECIMAL(10,2)", "VARCHAR"]
    rows = store.cursor().execute(f"SELECT * FROM {info.table} ORDER BY rowid").fetchall()  # noqa: S608
    assert rows == [(1.5, decimal.Decimal("1.25"), "1" * 50), (None, None, None)]
    union = pa.UnionArray.from_dense(
        pa.array([0, 1], pa.int8()), pa.array([0, 0], pa.int32()), [pa.array([1]), pa.array(["a"])]
    )
    with pytest.raises(DatasetError):
        store.replace("union", pa.table({"u": union}), "arrow")


# ===== new names from steps =====


@pytest.mark.parametrize(
    "step",
    [
        Step("rename", {"mapping": {"x": "Y"}}),
        Step("group_by", {"keys": ["y"], "aggregates": [{"fn": "count_rows", "as": "Y"}]}),
        Step("bucket", {"column": "x", "as": "X", "mode": "edges", "edges": [0, 5]}),
        Step(
            "categorize",
            {"column": "y", "as": "Y", "rules": [{"type": "in", "values": ["a"], "label": "A"}]},
        ),
    ],
)
def test_new_names_must_differ_from_existing_ones_ignoring_case(
    engine: tuple[DatasetStore, ViewEngine], step: Step
) -> None:
    # DuckDB renamed the clash to "y_1" (or "X_1"), so the next step's "y" was gone.
    result = window(engine, [step])
    assert isinstance(result, str), result


@pytest.mark.parametrize(
    "step",
    [
        Step("rename", {"mapping": {"x": "__SAG_ORD"}}),
        Step("rename", {"mapping": {"x": "rowid"}}),
        Step("bucket", {"column": "x", "as": "rowid", "mode": "edges", "edges": [0, 5]}),
        Step("group_by", {"keys": ["y"], "aggregates": [{"fn": "count_rows", "as": "ROWID"}]}),
        Step("sql", {"sql": "SELECT x AS rowid, y FROM grid"}),
        Step("sql", {"sql": "SELECT x AS __SAG_POS FROM grid"}),
    ],
)
def test_new_names_may_not_be_reserved(engine: tuple[DatasetStore, ViewEngine], step: Step) -> None:
    result = window(engine, [step])
    assert isinstance(result, str), result


def test_renaming_only_the_case_of_a_column_works(engine: tuple[DatasetStore, ViewEngine]) -> None:
    assert window(engine, [Step("rename", {"mapping": {"x": "X"}})]) == (
        ["X", "y"],
        [[1, "a"], [2, "b"], [3, "c"]],
    )
    swapped = window(engine, [Step("rename", {"mapping": {"x": "y", "y": "x"}})])
    assert swapped == (["y", "x"], [[1, "a"], [2, "b"], [3, "c"]])


# ===== parameters of the wrong shape are step errors, not crashes =====


@pytest.mark.parametrize(
    "step",
    [
        Step("group_by", {"keys": [["x"]], "aggregates": [{"fn": "count_rows"}]}),
        Step("columns", {"keep": [["x"]]}),
        Step("columns", {"keep": [{"x": 1}]}),
        Step("rename", {"mapping": {"x": ["a"]}}),
        Step("bucket", {"column": "x", "as": "b", "mode": "width", "width": 1e-300, "low": -1e9, "high": 1}),
        Step("bucket", {"column": "x", "as": "b", "mode": "edges", "edges": [0, 10**400]}),
        Step("filter", {"column": "x", "op": "gt", "value": 10**400}),
        Step("filter", {"column": "x", "op": "in", "values": [1, 10**400]}),
        Step(
            "categorize",
            {"column": "x", "as": "c", "rules": [{"type": "range", "low": 0, "high": 10**400, "label": "L"}]},
        ),
        Step("limit", {"n": 10**30}),
        # Edges so close together that the width mode makes the same number twice.
        Step("bucket", {"column": "x", "as": "b", "mode": "width", "width": 1, "low": 1e16, "high": 2e16}),
    ],
)
def test_bad_parameters_are_step_errors(engine: tuple[DatasetStore, ViewEngine], step: Step) -> None:
    result = window(engine, [step])
    assert isinstance(result, str), result
