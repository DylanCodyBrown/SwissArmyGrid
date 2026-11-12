"""Key Finder normalization across types (engine/keyfinder.normalized). Made-up data only."""

from __future__ import annotations

from decimal import Decimal

import pyarrow as pa

from swissarmygrid.engine.database import open_database
from swissarmygrid.engine.datasets import DatasetStore
from swissarmygrid.engine.keyfinder import KeyOptions, find_keys


def test_a_decimal_and_a_double_with_the_same_values_meet() -> None:
    store = DatasetStore(open_database("256MiB"), 1 << 28, 1 << 28, 10)
    left = store.replace(
        "prices",
        pa.table(
            {
                "p": pa.array(
                    [Decimal("1.50"), Decimal("2.25"), Decimal("3.00"), Decimal("-0.10")],
                    pa.decimal128(10, 2),
                )
            }
        ),
        "arrow",
    )
    right = store.replace("doubles", pa.table({"q": pa.array([1.5, 2.25, 3.0, -0.1, 7.0])}), "arrow")
    found = find_keys(
        store.cursor(),
        f"SELECT * FROM {left.table}",  # noqa: S608
        left.columns,
        f"SELECT * FROM {right.table}",  # noqa: S608
        right.columns,
        KeyOptions(),
    )
    # DECIMAL 1.50 read as '1.50' and DOUBLE 1.5 as '1.5': only the whole number 3 met.
    assert [(p["source_column"], p["target_column"], p["shared"]) for p in found["pairs"]] == [("p", "q", 4)]
