"""The made-up store (examples/sample_store.py) is the test bed for workspaces and relationship
autodetection, so its answer key has to be true of its data: every real relationship holds, and
every decoy only looks like one."""

from __future__ import annotations

import pandas as pd
import pytest
from examples.sample_store import DECOYS, TRUE_RELATIONSHIPS, Relationship, make_store

TABLES: dict[str, pd.DataFrame] = make_store()


def _keys(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.Series:
    """Each row's key as one comparable text value (so text "17" and the number 17 meet)."""
    return frame[list(columns)].astype(str).agg("|".join, axis=1)


def test_same_seed_same_tables() -> None:
    again = make_store()
    for name, frame in TABLES.items():
        pd.testing.assert_frame_equal(frame, again[name])


def test_scale_multiplies_the_facts() -> None:
    assert len(make_store(scale=2)["orders"]) == 2 * len(TABLES["orders"])


@pytest.mark.parametrize("rel", TRUE_RELATIONSHIPS, ids=lambda r: f"{r.from_table}->{r.to_table}")
def test_every_true_relationship_holds(rel: Relationship) -> None:
    one = _keys(TABLES[rel.to_table], rel.to_columns)
    many = _keys(TABLES[rel.from_table], rel.from_columns)
    assert one.is_unique, "the one side must be unique per row"
    contained = many.isin(set(one)).mean()
    floor = 0.97 if "orphans" in rel.note else 1.0
    assert contained >= floor, f"only {contained:.1%} of the many side is found in the one side"


def test_orders_have_orphans_but_not_too_many() -> None:
    orders, customers = TABLES["orders"], TABLES["customers"]
    orphaned = (~orders["customer_id"].isin(customers["cust_key"])).mean()
    assert 0.005 < orphaned < 0.02


def test_two_paths_to_calendar_one_inactive() -> None:
    to_calendar = [r for r in TRUE_RELATIONSHIPS if r.from_table == "orders" and r.to_table == "calendar"]
    assert sorted(r.active for r in to_calendar) == [False, True]


@pytest.mark.parametrize("decoy", DECOYS, ids=lambda d: f"{d[0]}.{d[1]}~{d[2]}.{d[3]}")
def test_decoys_overlap_but_are_not_keys(decoy: tuple[str, str, str, str]) -> None:
    left, lcol, right, rcol = decoy
    a, b = TABLES[left][lcol], TABLES[right][rcol]
    # Some decoys are even fully contained in a unique column (quantity 1-12 in category_id
    # 1-12): value overlap alone cannot reject them, which is the point.
    assert set(a.astype(str)) & set(b.astype(str)), "a decoy has to share values, or it fools nobody"
    real = {(r.from_table, r.from_columns, r.to_table, r.to_columns) for r in TRUE_RELATIONSHIPS}
    assert (left, (lcol,), right, (rcol,)) not in real and (right, (rcol,), left, (lcol,)) not in real
