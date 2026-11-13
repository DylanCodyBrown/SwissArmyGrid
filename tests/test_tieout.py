"""The tie-out report on made-up ledgers: the same sales booked twice, with planted differences (one
region's amount off, a region missing on one side, a row too many), and the rules for what ties."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from tests.conftest import AGENT, AUTH, grid_id, make_client, push


def ledgers() -> tuple[pa.Table, pa.Table]:
    regions = ["North", "North", "South", "South", "East", "West"]
    amounts = [100.0, 50.0, 70.0, 30.0, 40.0, 25.0]
    left = pa.table(
        {"order_id": list(range(6)), "region": regions, "amount": amounts, "units": [1, 2, 3, 4, 5, 6]}
    )
    # The right books South 0.004 short (within a tolerance of 0.01), East 15 short, has no West,
    # and one extra North row of 0.
    right = pa.table(
        {
            "ORDER_ID": [0, 1, 2, 3, 4, 9],
            "Region": [" North", "North", "South", "South", "East", "North"],
            "Amount": [100.0, 50.0, 70.0, 29.996, 25.0, 0.0],
            "units": [1, 2, 3, 4, 5, 0],
        }
    )
    return left, right


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with make_client(tmp_path) as test_client:
        yield test_client


@pytest.fixture
def compare(client: TestClient) -> str:
    left, right = ledgers()
    lg, rg = grid_id(push(client, "books_left", left)), grid_id(push(client, "books_right", right))
    made = client.post(
        "/compares", json={"left": lg, "right": rg, "keys": [["order_id", "ORDER_ID"]]}, headers=AUTH
    )
    assert made.status_code == 200, made.text
    return str(made.json()["compare"])


def tie_out(client: TestClient, compare: str, **body: Any) -> Any:
    answer = client.post(f"/compares/{compare}/tie-out", json=body, headers=AGENT)
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_the_choices_match_columns_ignoring_case_and_suggest_a_few_valued_text_column(
    client: TestClient, compare: str
) -> None:
    choices = client.get(f"/compares/{compare}/tie-out", headers=AUTH).json()
    assert [c["name"] for c in choices["columns"]] == ["order_id", "region", "amount", "units"]
    assert choices["suggested"] == {"group": ["region"], "measures": ["order_id", "amount", "units"]}


def test_groups_that_are_off_come_first_and_the_tolerance_decides_what_ties(
    client: TestClient, compare: str
) -> None:
    r = tie_out(client, compare, group=["region"], measures=["amount"], tolerance=0.01)
    by = {g["group"][0]: g for g in r["groups"]}
    assert set(by) == {"North", "South", "East", "West"}  # " North" trimmed meets "North"
    assert by["South"]["status"] == "ties" and by["South"]["measures"][0]["difference"] == pytest.approx(
        -0.004
    )
    assert by["East"]["status"] == "off" and by["East"]["measures"][0]["difference"] == -15
    assert by["West"]["only"] == "left" and by["West"]["right_rows"] is None and by["West"]["status"] == "off"
    assert by["West"]["measures"][0]["right"] is None and by["West"]["measures"][0]["difference"] == -25
    assert by["North"]["row_difference"] == 1 and by["North"]["status"] == "off"  # the extra row of 0
    assert [g["status"] for g in r["groups"]] == ["off", "off", "off", "ties"]
    assert r["groups"][0]["group"] == ["West"]  # the largest difference first: 25
    assert r["total"]["left_rows"] == 6 and r["total"]["right_rows"] == 6
    assert r["total"]["measures"][0]["difference"] == pytest.approx(-40.004) and r["total"]["status"] == "off"
    assert r["words"] == "3 of 4 groups are off."
    strict = tie_out(client, compare, group=["region"], measures=["amount"])
    assert {g["group"][0]: g["status"] for g in strict["groups"]}["South"] == "off"
    loose = tie_out(
        client, compare, group=["region"], measures=["amount"], tolerance=40, tolerance_kind="percent"
    )
    assert {g["group"][0]: g["status"] for g in loose["groups"]}["East"] == "ties"  # 15 of 40 is 37.5%


def test_no_group_is_one_grand_total_and_bad_requests_say_why(client: TestClient, compare: str) -> None:
    whole = tie_out(client, compare)
    assert len(whole["groups"]) == 1 and whole["groups"][0]["status"] == "ties"  # 6 rows on both sides
    assert whole["words"] == "Ties."
    units = tie_out(client, compare, measures=["units"])["total"]["measures"][0]
    assert (units["left"], units["right"], units["difference"], units["ties"]) == (21, 15, -6, False)
    for body, words in (
        ({"group": ["nowhere"]}, "not a column both tables have"),
        ({"measures": ["region"]}, "not a number on both sides"),
        ({"tolerance": -1}, "tolerance is a number"),
    ):
        answer = client.post(f"/compares/{compare}/tie-out", json=body, headers=AUTH)
        assert answer.status_code == 400 and words in answer.text, body


def test_the_tie_out_reads_each_grid_after_its_steps(client: TestClient, compare: str) -> None:
    read = client.get(f"/compares/{compare}", headers=AUTH).json()
    step = {"kind": "filter", "params": {"column": "region", "op": "ne", "value": "West"}}
    assert (
        client.post(
            f"/grids/{read['left']}/step-ops", json={"op": "add", "step": step}, headers=AUTH
        ).status_code
        == 200
    )
    r = tie_out(client, compare, group=["region"], measures=["amount"])
    assert "West" not in {g["group"][0] for g in r["groups"]}
