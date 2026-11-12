"""Compare: two grids aligned row for row on key columns. All data is made up.

The rule under test: compare row N is the same key on both sides, or a blank placeholder on the
side that lacks it, so a person reads straight across.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
from starlette.testclient import TestClient

from tests.conftest import AUTH, grid_id, push

# Left: ids 1, 2, 2 (a duplicate), 3, 10 and a blank; amounts.
LEFT = pa.table(
    {
        "id": pa.array([10, 2, 1, 3, 2, None], pa.int64()),
        "amount": pa.array([100.0, 20.0, 10.0, 30.0, 21.0, 99.0]),
        "note": pa.array(["j", "b1", "a", "c", "b2", "blank key"]),
    }
)
# Right: ids as text (they still meet), 1 with a different amount, 2 once, 4 only here, 9.
RIGHT = pa.table(
    {
        "cust_id": pa.array(["2", "1", "4", "9", " 3 "]),
        "amount": pa.array([20.0, 11.0, 40.0, 90.0, 30.0]),
    }
)


def start(client: TestClient, **spec: Any) -> tuple[str, str, Any]:
    left = grid_id(push(client, "left_side", LEFT))
    right = grid_id(push(client, "right_side", RIGHT))
    body = {"left": left, "right": right, "keys": [["id", "cust_id"]]} | spec
    response = client.post("/compares", json=body, headers=AUTH)
    assert response.status_code == 200, response.text
    return left, right, response.json()


def side_rows(client: TestClient, compare: str, side: str) -> Any:
    response = client.get(
        f"/compares/{compare}/rows", params={"side": side, "start": 0, "end": 100}, headers=AUTH
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_outer_join_lines_every_key_up_row_for_row(client: TestClient) -> None:
    _, _, cmp = start(client)
    assert cmp["counts"] == {"matched": 4, "left_only": 2, "right_only": 2}
    left = side_rows(client, cmp["compare"], "left")
    right = side_rows(client, cmp["compare"], "right")
    assert left["row_count"] == right["row_count"] == 8
    keys_left = [None if i in left["placeholders"] else r[0] for i, r in enumerate(left["rows"])]
    keys_right = [None if i in right["placeholders"] else r[0] for i, r in enumerate(right["rows"])]
    # Number order (9 before 10); the duplicate 2 pairs with the one right 2 twice, as in SQL;
    # the blank key matches nothing and comes last.
    assert keys_left == [1, 2, 2, 3, None, None, 10, None]
    assert keys_right == ["1", "2", "2", " 3 ", "4", "9", None, None]
    # Placeholders stand where the other side has the key: rows 4-5 (right-only 4 and 9) on the
    # left, rows 6-7 (left-only 10 and the blank key) on the right.
    assert left["placeholders"] == [4, 5] and right["placeholders"] == [6, 7]
    assert left["rows"][7] == [None, 99.0, "blank key"]  # the blank-key row itself, unmatched
    # Row 0 (key 1): the amounts differ, and both sides mark that cell.
    assert [0, 1] in left["diffs"] and [0, 1] in right["diffs"]
    assert not any(d[0] == 1 for d in left["diffs"])  # key 2, 20.0 = 20.0


def test_anti_join_shows_only_unmatched_rows(client: TestClient) -> None:
    _, _, cmp = start(client, mode="anti")
    both = side_rows(client, cmp["compare"], "left")
    assert both["row_count"] == 4
    left_only = client.patch(f"/compares/{cmp['compare']}", json={"anti_side": "left"}, headers=AUTH).json()
    assert left_only["row_count"] == 2
    rows = side_rows(client, cmp["compare"], "left")
    assert [r[0] for r in rows["rows"]] == [10, None] and rows["placeholders"] == []
    right_only = client.patch(f"/compares/{cmp['compare']}", json={"anti_side": "right"}, headers=AUTH).json()
    assert right_only["row_count"] == 2
    assert [r[0] for r in side_rows(client, cmp["compare"], "right")["rows"]] == ["4", "9"]


def test_a_step_on_one_side_rebuilds_the_alignment(client: TestClient) -> None:
    left, _, cmp = start(client)
    client.post(
        f"/grids/{left}/steps",
        json={"steps": [{"kind": "filter", "params": {"column": "id", "op": "lt", "value": 3}}]},
        headers=AUTH,
    )
    again = client.get(f"/compares/{cmp['compare']}", headers=AUTH).json()
    assert again["counts"] == {"matched": 3, "left_only": 0, "right_only": 3}


def test_cross_compare_filters_the_other_side_to_the_picked_key(client: TestClient) -> None:
    _, _, cmp = start(client, mode="cross")
    left = side_rows(client, cmp["compare"], "left")
    assert left["row_count"] == 6 and left["placeholders"] == []  # its own view, in its own order
    # Pick the right side's row "2" (position 0): the left side shows its two rows with id 2.
    focus = client.post(
        f"/compares/{cmp['compare']}/focus", json={"side": "right", "position": 0}, headers=AUTH
    ).json()
    assert focus["focus"] == {"side": "left", "rows": 2, "key": ["2"]}
    filtered = side_rows(client, cmp["compare"], "left")
    assert filtered["row_count"] == 2 and [r[2] for r in filtered["rows"]] == ["b1", "b2"]
    client.delete(f"/compares/{cmp['compare']}/focus", headers=AUTH)
    assert side_rows(client, cmp["compare"], "left")["row_count"] == 6


def test_bad_keys_and_stopping(client: TestClient) -> None:
    left = grid_id(push(client, "left_side", LEFT))
    right = grid_id(push(client, "right_side", RIGHT))
    bad = client.post(
        "/compares", json={"left": left, "right": right, "keys": [["nope", "cust_id"]]}, headers=AUTH
    )
    assert bad.status_code == 400 and "nope" in bad.text
    same = client.post("/compares", json={"left": left, "right": left, "keys": [["id", "id"]]}, headers=AUTH)
    assert same.status_code == 400
    ok = client.post(
        "/compares", json={"left": left, "right": right, "keys": [["id", "cust_id"]]}, headers=AUTH
    ).json()
    assert client.delete(f"/compares/{ok['compare']}", headers=AUTH).status_code == 200
    assert client.get(f"/compares/{ok['compare']}", headers=AUTH).status_code == 404


def test_key_group_is_every_compare_row_sharing_the_key(client: TestClient) -> None:
    _, _, cmp = start(client)

    def group(row: int) -> Any:
        return client.get(f"/compares/{cmp['compare']}/key-group", params={"row": row}, headers=AUTH).json()

    assert (group(1)["first"], group(1)["last"]) == (1, 2)  # the duplicate 2 pairs twice: rows 1-2
    assert (group(2)["first"], group(2)["last"]) == (1, 2)
    assert (group(0)["first"], group(0)["last"]) == (0, 0)  # key 1, once on each side
    assert (group(7)["first"], group(7)["last"]) == (7, 7)  # a blank key matches nothing
