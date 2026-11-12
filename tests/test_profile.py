"""Profiles match pandas on the same data, blanks included (plan M2 acceptance)."""

from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pyarrow as pa
from starlette.testclient import TestClient

from tests.conftest import AUTH, grid_id, push

DATA = pa.table(
    {
        "amount": pa.array([10.0, None, 2.5, -1.0, 0.0, 2.5, None, 7.75]),
        "name": pa.array(["Ann", "ann", None, " Bo", "", "Cy", "Ann", "ANN"]),
        "day": pa.array(
            [
                date(2026, 1, 3),
                date(2026, 1, 9),
                None,
                date(2026, 2, 1),
                None,
                date(2026, 3, 1),
                date(2026, 3, 2),
                None,
            ]
        ),
    }
)


def profile(client: TestClient, grid: str, column: str) -> dict:  # type: ignore[type-arg]
    response = client.get(f"/grids/{grid}/profile", params={"column": column}, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def test_number_profile_matches_pandas(client: TestClient) -> None:
    grid = grid_id(push(client, "p", DATA))
    got = profile(client, grid, "amount")
    s = DATA.to_pandas()["amount"]
    assert got["rows"] == len(s)
    assert got["blanks"] == int(s.isna().sum())
    assert got["distinct"] == s.nunique()
    assert math.isclose(got["mean"], s.mean())
    assert math.isclose(got["stddev"], s.std())  # sample standard deviation
    assert math.isclose(got["median"], s.median())
    assert math.isclose(got["q1"], s.quantile(0.25))
    assert math.isclose(got["q3"], s.quantile(0.75))
    assert math.isclose(got["sum"], s.sum())
    assert got["zeros"] == 1 and got["negatives"] == 1
    assert sum(b["count"] for b in got["histogram"]) == int(s.notna().sum())
    assert got["all_unique"] is False


def test_text_profile(client: TestClient) -> None:
    grid = grid_id(push(client, "p", DATA))
    got = profile(client, grid, "name")
    s = DATA.to_pandas()["name"]
    assert got["blanks"] == int(s.isna().sum())
    assert got["distinct"] == s.nunique()
    assert got["empty_strings"] == 1 and got["padded"] == 1
    assert got["shortest"] == 0 and got["longest"] == 3
    assert got["top_values"][0] == {"value": "Ann", "count": 2}
    assert got["case_groups"][0]["variants"] == ["ANN", "Ann", "ann"]


def test_date_profile(client: TestClient) -> None:
    grid = grid_id(push(client, "p", DATA))
    got = profile(client, grid, "day")
    assert got["earliest"] == "2026-01-03" and got["latest"] == "2026-03-02"
    assert [m["count"] for m in got["by_month"]] == [2, 1, 2]


def test_profile_follows_the_steps(client: TestClient) -> None:
    grid = grid_id(push(client, "p", DATA))
    client.post(
        f"/grids/{grid}/steps",
        json={"steps": [{"kind": "filter", "params": {"column": "amount", "op": "gt", "value": 1}}]},
        headers=AUTH,
    )
    assert profile(client, grid, "amount")["rows"] == 4


def test_whole_table_profile_and_header_stats(client: TestClient) -> None:
    dup = pa.concat_tables([DATA, DATA.slice(0, 2)])
    grid = grid_id(push(client, "p", dup))
    table = client.get(f"/grids/{grid}/profile", headers=AUTH).json()
    assert table["duplicate_rows"] == 2
    frame: pd.DataFrame = dup.to_pandas()
    assert table["duplicate_rows"] == int(frame.duplicated().sum())
    stats = client.get(f"/grids/{grid}/header-stats", headers=AUTH).json()
    assert stats["amount"]["distinct"] == frame["amount"].nunique()
    assert stats["name"]["blank_percent"] == round(100 * frame["name"].isna().mean(), 1)


def test_categorize_stats(client: TestClient) -> None:
    grid = grid_id(push(client, "p", DATA))
    client.post(
        f"/grids/{grid}/steps",
        json={
            "steps": [
                {
                    "kind": "categorize",
                    "params": {
                        "column": "name",
                        "as": "who",
                        "rules": [
                            {"type": "wildcard", "value": "A%", "label": "A"},
                            {"type": "contains", "value": "n", "label": "has n"},
                        ],
                    },
                }
            ]
        },
        headers=AUTH,
    )
    stats = client.get(f"/grids/{grid}/categorize-stats", params={"step": 0}, headers=AUTH).json()
    # Non-blank names: Ann, ann, " Bo", "", Cy, Ann, ANN. Unmatched: " Bo", "", Cy.
    assert stats == {"unmatched": 3, "overlapping": 2}


def test_profile_after_a_header_sort_and_a_filter(client: TestClient) -> None:
    """Regression: a parameterized GROUP BY alias failed to bind in this sequence."""
    table = pa.table({"region": ["East", "West"] * 500, "price": [float(i % 97) for i in range(1000)]})
    grid = grid_id(push(client, "p", table))
    client.post(f"/grids/{grid}/header-sort", json={"keys": [["price", "desc"]]}, headers=AUTH)
    client.post(
        f"/grids/{grid}/steps",
        json={"steps": [{"kind": "filter", "params": {"column": "region", "op": "eq", "value": "East"}}]},
        headers=AUTH,
    )
    got = profile(client, grid, "price")
    assert got["rows"] == 500 and sum(b["count"] for b in got["histogram"]) == 500
