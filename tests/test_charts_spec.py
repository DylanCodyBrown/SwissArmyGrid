"""Chart specs against awkward data and malformed specs (charts/spec.py). Made-up data only."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pytest

from swissarmygrid.charts.spec import ChartError, aggregate, validate
from swissarmygrid.engine.database import open_database
from swissarmygrid.engine.datasets import DatasetInfo, DatasetStore


@pytest.fixture
def table() -> Iterator[tuple[DatasetStore, DatasetInfo]]:
    connection = open_database("256MiB")
    store = DatasetStore(connection, 1 << 28, 1 << 28, 10)
    info = store.replace(
        "c",
        pa.table(
            {
                "region": ["N", "S", "N", "E"],
                "nan": [1.0, float("nan"), 3.0, None],
                "inf": [1.0, float("inf"), float("-inf"), 2.0],
                "qty": [1, 2, 3, 4],
            }
        ),
        "arrow",
    )
    yield store, info
    connection.close()


def chart(table: tuple[DatasetStore, DatasetInfo], spec: dict[str, Any]) -> dict[str, Any]:
    store, info = table
    view = f"SELECT * FROM {info.table}"  # noqa: S608 (a generated table name)
    return aggregate(store.cursor(), view, validate(spec, info.columns), 5000)


@pytest.mark.parametrize("field", ["nan", "inf"])
def test_a_histogram_bins_finite_values_and_survives_nan_and_infinity(
    table: tuple[DatasetStore, DatasetInfo], field: str
) -> None:
    # Was: Conversion Error, NaN can't be cast to BIGINT (the whole chart failed).
    values = chart(table, {"mark": "histogram", "x": {"field": field, "bins": 4}})["values"]
    assert sum(v["count"] for v in values) == 2


@pytest.mark.parametrize(
    "spec",
    [
        # A heatmap's second axis was never checked: KeyError, or a raw DuckDB binder error.
        {"mark": "heatmap", "x": {"field": "region"}, "y": {"aggregate": "count"}},
        {"mark": "heatmap", "x": {"field": "region"}, "y": {"aggregate": "count", "field": "nope"}},
        {"mark": "heatmap", "x": {"field": "region"}, "y": {"aggregate": "sum", "field": "qty"}},
        # A field that is not a string raised TypeError (unhashable).
        {"mark": "bar", "x": {"field": ["region"]}, "y": {"aggregate": "count"}},
        {"mark": "bar", "x": {"field": "region"}, "y": {"aggregate": "count"}, "color": {"field": {}}},
        {"mark": "bar", "x": {"field": "region"}, "y": {"aggregate": "sum", "field": ["qty"]}},
    ],
)
def test_malformed_specs_are_chart_errors(
    table: tuple[DatasetStore, DatasetInfo], spec: dict[str, Any]
) -> None:
    with pytest.raises(ChartError):
        validate(spec, table[1].columns)


def test_a_heatmap_with_both_axes_still_works(table: tuple[DatasetStore, DatasetInfo]) -> None:
    spec = {"mark": "heatmap", "x": {"field": "region"}, "y": {"aggregate": "count", "field": "qty"}}
    assert len(chart(table, spec)["values"]) == 4
