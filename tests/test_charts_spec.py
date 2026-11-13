"""Chart specs against awkward data and malformed specs (charts/spec.py). Made-up data only."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pytest

from swissarmygrid.adapters.database import open_database
from swissarmygrid.adapters.datasets import DatasetStore
from swissarmygrid.domain.charts import ChartError, validate
from swissarmygrid.domain.datasets import DatasetInfo
from swissarmygrid.services.charts import aggregate


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


def test_a_blank_date_does_not_stretch_the_y_axis() -> None:
    # 158 blank dates drew nowhere on the time axis but set its top at 170, over bars of 1.
    import datetime as dt

    from swissarmygrid.domain.charts import vega_lite
    from swissarmygrid.domain.datasets import Column

    columns = (Column("day", "DATE"), Column("kind", "VARCHAR"))
    values = [{"x": None, "y": 158}, {"x": dt.date(2026, 1, 1), "y": 2}, {"x": dt.date(2026, 1, 2), "y": 1}]
    by_day = {"mark": "bar", "x": {"field": "day"}, "y": {"aggregate": "count"}}
    assert vega_lite(by_day, {"values": values}, columns)["encoding"]["y"]["scale"]["domainMax"] == 2 * 1.08
    # On a category axis the blank is a bar of its own, and counts.
    by_kind = {"mark": "bar", "x": {"field": "kind"}, "y": {"aggregate": "count"}}
    scale = vega_lite(by_kind, {"values": values}, columns)["encoding"]["y"]["scale"]
    assert scale["domainMax"] == 158 * 1.08
