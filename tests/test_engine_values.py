"""Step semantics on awkward values: time zones, NaN, huge and tiny numbers. Made-up data only.

Counts are read in SQL: fetching a TIMESTAMP WITH TIME ZONE value into Python needs pytz.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pyarrow as pa
import pytest

from swissarmygrid.adapters.database import describer, duckdb_grammar, open_database
from swissarmygrid.adapters.datasets import DatasetStore
from swissarmygrid.domain.steps import Step, ViewSettings, compile_view


@pytest.fixture
def store() -> Iterator[DatasetStore]:
    connection = open_database("256MiB")
    store = DatasetStore(connection, 1 << 28, 1 << 28, 10)
    store.replace(
        "times",
        pa.table(
            {
                "zoned": pa.array([dt.datetime(2024, 1, 1, 10, tzinfo=dt.UTC)], pa.timestamp("us", tz="UTC")),
                "naive": pa.array([dt.datetime(2024, 1, 1, 10)]),
            }
        ),
        "arrow",
    )
    yield store
    connection.close()


def count(store: DatasetStore, step: Step) -> int | str:
    info = store.get("times")
    assert info is not None
    cursor = store.cursor()
    compiled = compile_view(
        describer(cursor), duckdb_grammar(), info.table, info.columns, [step], ViewSettings(), info.name
    )
    if compiled.error is not None:
        return compiled.error.message
    row = cursor.execute(f"SELECT count(*) FROM ({compiled.sql()})").fetchone()  # noqa: S608
    return int(row[0]) if row else 0


@pytest.mark.parametrize(
    ("op", "value", "expected"),
    [
        # 10:00 UTC is after 06:00 UTC (11:00 at +05:00); the offset used to be dropped, so
        # the filter compared against 11:00 UTC and kept nothing.
        ("after", "2024-01-01T11:00:00+05:00", 1),
        ("before", "2024-01-01T14:59:00+05:00", 0),
        ("after", "2024-01-01T09:59:00+00:00", 1),
        ("eq", "2024-01-01T05:00:00-05:00", 1),
    ],
)
def test_an_offset_in_the_value_is_kept_for_a_zoned_column(
    store: DatasetStore, op: str, value: str, expected: int
) -> None:
    assert count(store, Step("filter", {"column": "zoned", "op": op, "value": value})) == expected


def test_an_offset_on_a_column_without_time_zone_is_refused(store: DatasetStore) -> None:
    # Dropping the offset would shift the time silently (plan: never shift times).
    def after(value: str) -> int | str:
        return count(store, Step("filter", {"column": "naive", "op": "after", "value": value}))

    result = after("2024-01-01T09:00:00+05:00")
    assert isinstance(result, str) and "time zone" in result
    assert after("2024-01-01T09:00:00") == 1
