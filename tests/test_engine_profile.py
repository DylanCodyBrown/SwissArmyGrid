"""Profiles of awkward numbers and the cheaper view source they read. Made-up data only."""

from __future__ import annotations

import math
from collections.abc import Iterator

import pyarrow as pa
import pytest

from swissarmygrid.adapters.database import open_database
from swissarmygrid.adapters.datasets import DatasetStore
from swissarmygrid.adapters.views import ViewEngine
from swissarmygrid.domain.datasets import Column, DatasetInfo
from swissarmygrid.domain.steps import Step, ViewSettings
from swissarmygrid.services.profile import header_stats, profile_column, profile_table


@pytest.fixture
def store() -> Iterator[DatasetStore]:
    connection = open_database("256MiB")
    yield DatasetStore(connection, 1 << 28, 1 << 28, 10)
    connection.close()


def whole(info: DatasetInfo) -> str:
    return f"SELECT * FROM {info.table}"  # noqa: S608 (a generated table name)


@pytest.mark.parametrize(
    "values",
    [
        [1.0, float("nan"), 3.0],
        [1.0, float("inf"), 3.0],
        [float("-inf"), 0.0, None],
        [-1e308, 1e308],
        [float("nan"), float("nan")],
        [float("inf"), float("inf")],
    ],
)
def test_a_profile_of_nan_and_infinity_does_not_fail(store: DatasetStore, values: list[float | None]) -> None:
    # The histogram's bin width went into the SQL as `nan` or `inf`: a bare identifier
    # ("column nan not found") or a NaN cast to BIGINT. The whole profile failed.
    info = store.replace("f", pa.table({"f": pa.array(values, pa.float64())}), "arrow")
    profile = profile_column(store.cursor(), whole(info), info.columns[0])
    assert profile["rows"] == len(values)
    finite = [v for v in values if v is not None and math.isfinite(v)]
    assert sum(b["count"] for b in profile["histogram"]) == len(finite)


def test_profile_aliases_do_not_clash_with_column_names(store: DatasetStore) -> None:
    import datetime as dt

    # The profile's own aliases (k, n, m) were shadowed by columns of those names: DuckDB's
    # GROUP BY binds a column before an alias, so text and date profiles failed.
    info = store.replace(
        "a",
        pa.table(
            {
                "k": [1, 2, 3],
                "n": ["x", "X", "y"],
                "m": [dt.date(2024, 1, 5), dt.date(2024, 2, 5), None],
                "t": ["a", "A", "b"],
            }
        ),
        "arrow",
    )
    cursor = store.cursor()
    by_name = {c.name: profile_column(cursor, whole(info), c) for c in info.columns}
    assert by_name["t"]["case_groups"] == [{"variants": ["A", "a"], "rows": 2}]
    assert by_name["n"]["top_values"][0] == {"value": "X", "count": 1}
    assert [m["count"] for m in by_name["m"]["by_month"]] == [1, 1]


def test_all_unique_counts_a_blank_as_a_value(store: DatasetStore) -> None:
    info = store.replace("u", pa.table({"a": [1, 2, None], "b": [1, None, None], "c": [1, 1, 2]}), "arrow")
    cursor = store.cursor()
    unique = {c.name: profile_column(cursor, whole(info), c)["all_unique"] for c in info.columns}

    assert unique == {"a": True, "b": False, "c": False}


def test_profile_table_matches_one_query_per_column(store: DatasetStore) -> None:
    info = store.replace(
        "p",
        pa.table({"n": [3, None, 1, 2], "s": ["x", "y", None, "x"], "z": pa.array([None] * 4, pa.string())}),
        "arrow",
    )
    result = profile_table(store.cursor(), whole(info), info.columns)
    assert result["rows"] == 4 and result["duplicate_rows"] == 0
    keys = ("column", "type", "blanks", "blank_percent", "distinct", "min", "max")
    assert result["columns"] == [
        dict(zip(keys, ("n", "BIGINT", 1, 25.0, 3, 1, 3), strict=True)),
        dict(zip(keys, ("s", "VARCHAR", 1, 25.0, 2, "x", "y"), strict=True)),
        dict(zip(keys, ("z", "VARCHAR", 4, 100.0, 0, None, None), strict=True)),
    ]


def test_profiles_read_the_view_cache_and_agree_with_the_ordered_view(store: DatasetStore) -> None:
    info = store.replace("v", pa.table({"k": [5, 1, 4, 1, 3], "t": ["e", "a", "d", "a", None]}), "arrow")
    views = ViewEngine(store.cursor(), 1 << 26)
    cursor = store.cursor()
    steps = [Step("filter", {"column": "k", "op": "gt", "value": 1}), Step("sort", {"keys": [["k", "asc"]]})]
    compiled = views.compile(cursor, info, steps, ViewSettings())
    source = views.rows_sql(cursor, info, steps, ViewSettings())
    assert "ORDER BY" not in source and "vc_" in source
    for column in compiled.columns:
        assert profile_column(cursor, source, column) == profile_column(cursor, compiled.sql(), column)
    ordered = compiled.sql()
    assert profile_table(cursor, source, compiled.columns) == profile_table(cursor, ordered, compiled.columns)
    assert header_stats(cursor, source, compiled.columns) == header_stats(
        cursor, compiled.sql(), compiled.columns
    )
    # No steps: the dataset table itself, with no sort.
    plain = f"SELECT {_names(info.columns)} FROM {info.table}"  # noqa: S608
    assert views.rows_sql(cursor, info, [], ViewSettings()) == plain


def _names(columns: tuple[Column, ...]) -> str:
    return ", ".join(f'"{c.name}"' for c in columns)
