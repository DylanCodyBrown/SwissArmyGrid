"""Which aligned cells a compare marks as different (engine/compare.differences)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from swissarmygrid.domain.datasets import Column
from swissarmygrid.domain.relationships import differences

COLUMNS = (Column("id", "BIGINT"), Column("v", "DOUBLE"))


def differs(a: Any, b: Any) -> bool:
    left, _ = differences([[1, a]], COLUMNS, [[1, b]], COLUMNS, (("id", "id"),))
    return bool(left)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (float("inf"), float("inf")),  # inf - inf is NaN: equal infinities read as different
        (float("-inf"), float("-inf")),
        (float("nan"), float("nan")),  # one value, as DuckDB sees it (NaN = NaN)
        (0.1 + 0.2, 0.3),  # differ only in the last bits
        (0.0, -0.0),
        (Decimal("0.10"), 0.1),  # a DECIMAL beside a DOUBLE
        (2**60, 2**60),
        (None, None),
    ],
)
def test_the_same_value_is_not_a_difference(a: Any, b: Any) -> None:
    assert not differs(a, b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (2**60, 2**60 + 1),  # exact integers: the float comparison called these equal
        (1e-10, 2e-10),  # small numbers: an absolute 1e-9 tolerance called these equal
        (float("inf"), float("-inf")),
        (float("nan"), 1.0),
        (1.0, None),
        ("a", "A"),
    ],
)
def test_a_different_value_is_a_difference(a: Any, b: Any) -> None:
    assert differs(a, b)
