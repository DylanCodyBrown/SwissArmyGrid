"""Charts and side-panel cards (plan M7): validation, aggregation caps, cross-filtering."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from tests.conftest import AGENT, AUTH, grid_id, push


def big_table(rows: int) -> pa.Table:
    return pa.table(
        {
            "id": pa.array(range(rows), pa.int64()),
            "region": pa.array([("North", "South", "East", "West")[i % 4] for i in range(rows)]),
            "qty": pa.array([i % 50 for i in range(rows)], pa.int64()),
            "price": pa.array([(i * 7919) % 1000 / 10 for i in range(rows)]),
            "day": pa.array([date(2026, 1, 1) + timedelta(days=i % 365) for i in range(rows)]),
        }
    )


def add(client: TestClient, grid: str, card: dict[str, Any], headers: dict[str, str] = AUTH) -> Any:
    return client.post(f"/grids/{grid}/cards", json=card, headers=headers)


def cards(client: TestClient, grid: str) -> list[dict[str, Any]]:
    return client.get(f"/grids/{grid}/cards", headers=AUTH).json()["cards"]  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    "spec",
    [
        {"mark": "pie", "x": {"field": "region"}},
        {"mark": "bar", "x": {"field": "region"}, "y": {"aggregate": "sum", "field": "region"}},
        {"mark": "bar", "x": {"field": "nope"}, "y": {"aggregate": "count"}},
        {
            "mark": "bar",
            "x": {"field": "region", "script": "alert(1)"},
            "y": {"aggregate": "count"},
        },
        {
            "mark": "bar",
            "x": {"field": "region"},
            "y": {"aggregate": "count"},
            "data": {"url": "x"},
        },
        {"mark": "histogram", "x": {"field": "region"}},
        {
            "mark": "line",
            "x": {"field": "region", "time_unit": "month"},
            "y": {"aggregate": "count"},
        },
    ],
)
def test_malformed_specs_are_rejected(client: TestClient, spec: dict[str, Any]) -> None:
    grid = grid_id(push(client, "o", big_table(10)))
    assert add(client, grid, {"kind": "chart", "spec": spec}).status_code == 400


def test_chart_over_a_million_rows_sends_few_points(client: TestClient) -> None:
    grid = grid_id(push(client, "o", big_table(1_000_000)))
    for spec in (
        {
            "mark": "bar",
            "x": {"field": "region"},
            "y": {"aggregate": "sum", "field": "qty"},
            "color": {"field": "qty"},
        },
        {"mark": "point", "x": {"field": "qty"}, "y": {"field": "price"}},
        {"mark": "histogram", "x": {"field": "price", "bins": 30}},
        {
            "mark": "line",
            "x": {"field": "day", "time_unit": "month"},
            "y": {"aggregate": "mean", "field": "price"},
        },
    ):
        assert add(client, grid, {"kind": "chart", "spec": spec}).status_code == 200
    rendered = cards(client, grid)
    assert all("error" not in c for c in rendered), [c.get("error") for c in rendered]
    assert all(c["points"] <= 5000 for c in rendered)
    by_mark = {c["spec"]["mark"]: c for c in rendered}
    assert by_mark["point"]["note"].startswith("Showing a repeatable sample of 5,000")
    assert by_mark["histogram"]["points"] == 30
    assert by_mark["line"]["points"] == 12
    assert by_mark["bar"]["points"] == 100  # region (i % 4) and qty (i % 50) repeat every 100 rows
    assert by_mark["bar"]["vega_lite"]["encoding"]["xOffset"]["field"] == "color"


def test_series_differ_by_more_than_color(client: TestClient) -> None:
    grid = grid_id(push(client, "o", big_table(100)))
    add(
        client,
        grid,
        {
            "kind": "chart",
            "spec": {
                "mark": "line",
                "x": {"field": "day", "time_unit": "month"},
                "y": {"aggregate": "count"},
                "color": {"field": "region"},
            },
        },
    )
    encoding = cards(client, grid)[0]["vega_lite"]["encoding"]
    assert encoding["strokeDash"]["field"] == "color"


def test_cards_redraw_on_view_change_and_cross_filter(client: TestClient) -> None:
    grid = grid_id(push(client, "o", big_table(100)))
    made = add(
        client,
        grid,
        {
            "kind": "chart",
            "title": "By region",
            "spec": {"mark": "bar", "x": {"field": "region"}, "y": {"aggregate": "count"}},
        },
        AGENT,
    )
    card = made.json()["card"]
    assert cards(client, grid)[0]["created_by"] == "agent"
    assert cards(client, grid)[0]["points"] == 4
    client.post(f"/cards/{card}/select", json={"value": "North"}, headers=AUTH)
    rows = client.get(f"/grids/{grid}/rows", headers=AUTH).json()
    assert rows["row_count"] == 25 and {r[1] for r in rows["rows"]} == {"North"}
    assert cards(client, grid)[0]["points"] == 1  # the chart follows the grid


def test_time_bucket_cross_filter_selects_the_whole_bucket(client: TestClient) -> None:
    grid = grid_id(push(client, "o", big_table(365)))
    card = add(
        client,
        grid,
        {
            "kind": "chart",
            "spec": {
                "mark": "bar",
                "x": {"field": "day", "time_unit": "month"},
                "y": {"aggregate": "count"},
            },
        },
    ).json()["card"]
    client.post(f"/cards/{card}/select", json={"value": "2026-02-01"}, headers=AUTH)
    assert client.get(f"/grids/{grid}/count", headers=AUTH).json()["row_count"] == 28


def test_notes_tables_updates_and_removal(client: TestClient) -> None:
    grid = grid_id(push(client, "o", big_table(10)))
    note = add(client, grid, {"kind": "note", "title": "Findings", "body": "**North** leads."}).json()["card"]
    table = add(
        client,
        grid,
        {
            "kind": "table",
            "sql": "SELECT region, count(*) AS n FROM grid GROUP BY region ORDER BY region",
        },
    ).json()["card"]
    rendered = {c["id"]: c for c in cards(client, grid)}
    assert rendered[note]["body"] == "**North** leads."
    assert rendered[table]["rows"][0] == ["East", 2]
    assert (
        client.patch(f"/cards/{note}", json={"pinned": True, "body": "updated"}, headers=AUTH).status_code
        == 200
    )
    first = cards(client, grid)[0]
    assert first["id"] == note and first["pinned"] and first["body"] == "updated"
    assert client.delete(f"/cards/{note}", headers=AUTH).status_code == 200
    assert len(cards(client, grid)) == 1
    bad = add(client, grid, {"kind": "table", "sql": "SELECT * FROM read_csv('x')"}).json()["card"]
    assert "error" in {c["id"]: c for c in cards(client, grid)}[bad]


def test_altair_code(client: TestClient) -> None:
    grid = grid_id(push(client, "o", big_table(10)))
    card = add(
        client,
        grid,
        {
            "kind": "chart",
            "spec": {
                "mark": "bar",
                "x": {"field": "region"},
                "y": {"aggregate": "sum", "field": "qty"},
            },
        },
    ).json()["card"]
    code = client.get(f"/cards/{card}/code", headers=AUTH).json()["code"]
    assert 'alt.Chart(df).mark_bar(tooltip=True).encode(x="region:N", y="sum(qty):Q")' in code
    compile(code, "<altair>", "exec")


def test_bar_series_differ_by_dash_too(client: TestClient) -> None:
    grid = grid_id(push(client, "o", big_table(100)))
    add(
        client,
        grid,
        {
            "kind": "chart",
            "spec": {
                "mark": "bar",
                "x": {"field": "region"},
                "y": {"aggregate": "count"},
                "color": {"field": "qty"},
            },
        },
    )
    encoding = cards(client, grid)[0]["vega_lite"]["encoding"]
    assert encoding["strokeDash"]["field"] == "color"
    assert encoding["x"]["axis"]["labelAngle"] == 0 and encoding["x"]["axis"]["labelOverlap"] is False


def test_one_sandbox_lists_every_tables_cards(client: TestClient) -> None:
    first = grid_id(push(client, "first_table", pa.table({"a": [1, 2]})))
    second = grid_id(push(client, "second_table", pa.table({"b": ["x", "y"]})))
    add(client, first, {"kind": "note", "title": "on first", "body": "one"})
    add(client, second, {"kind": "note", "title": "on second", "body": "two"})
    every = client.get("/cards", headers=AUTH).json()["cards"]
    assert {(c["title"], c["dataset"]) for c in every} == {
        ("on first", "first_table"),
        ("on second", "second_table"),
    }
    # A card whose grid is closed drops out of the sandbox.
    client.delete(f"/grids/{second}", headers=AUTH)
    assert [c["title"] for c in client.get("/cards", headers=AUTH).json()["cards"]] == ["on first"]


def chart(client: TestClient, grid: str, spec: dict[str, Any]) -> str:
    made = add(client, grid, {"kind": "chart", "spec": spec})
    assert made.status_code == 200, made.text
    return str(made.json()["card"])


def row_count(client: TestClient, grid: str) -> int:
    return int(client.get(f"/grids/{grid}/count", headers=AUTH).json()["row_count"])


def hours() -> pa.Table:
    start = datetime(2026, 1, 1)
    return pa.table(
        {
            "at": pa.array([start + timedelta(hours=i) for i in range(24 * 59)]),  # January, February
            "zoned": pa.array(
                [datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=i) for i in range(24 * 59)],
                pa.timestamp("us", tz="UTC"),
            ),
            "day": pa.array([date(2026, 1, 1) + timedelta(days=i // 24) for i in range(24 * 59)]),
            "kind": pa.array([("a", "b", "c")[i % 3] for i in range(24 * 59)]),
        }
    )


def test_a_month_of_a_timestamp_column_keeps_its_last_day(client: TestClient) -> None:
    # The bucket ended at 2026-01-31 00:00, so 23 hours of 31 January were left out (721 of 744).
    grid = grid_id(push(client, "o", hours()))
    month = {"mark": "bar", "x": {"field": "at", "time_unit": "month"}, "y": {"aggregate": "count"}}
    card = chart(client, grid, month)
    assert client.post(f"/cards/{card}/select", json={"value": "2026-01-01"}, headers=AUTH).status_code == 200
    assert row_count(client, grid) == 31 * 24


def test_a_raw_date_or_time_click_filters_on_that_value(client: TestClient) -> None:
    grid = grid_id(push(client, "o", hours()))
    by_day = chart(client, grid, {"mark": "bar", "x": {"field": "day"}, "y": {"aggregate": "count"}})
    picked = client.post(f"/cards/{by_day}/select", json={"value": "2026-01-05"}, headers=AUTH)
    assert picked.status_code == 200, picked.text
    assert row_count(client, grid) == 24
    client.post(f"/grids/{grid}/steps", json={"steps": []}, headers=AUTH)
    by_hour = chart(client, grid, {"mark": "point", "x": {"field": "zoned"}, "y": {"aggregate": "count"}})
    picked = client.post(
        f"/cards/{by_hour}/select", json={"value": "2026-01-01T05:00:00+00:00"}, headers=AUTH
    )
    assert picked.status_code == 200, picked.text
    assert row_count(client, grid) == 1


def test_a_click_value_the_column_cannot_hold_is_refused_and_adds_no_step(client: TestClient) -> None:
    # The page sent Vega's epoch milliseconds for a date; the step was saved and the grid broke.
    grid = grid_id(push(client, "o", hours()))
    by_day = chart(client, grid, {"mark": "bar", "x": {"field": "day"}, "y": {"aggregate": "count"}})
    month = {"mark": "bar", "x": {"field": "day", "time_unit": "month"}, "y": {"aggregate": "count"}}
    by_month = chart(client, grid, month)
    for card, value in ((by_day, 1767225600000), (by_day, "not a date"), (by_month, 1767225600000)):
        refused = client.post(f"/cards/{card}/select", json={"value": value}, headers=AUTH)
        assert refused.status_code == 400, (value, refused.text)
    assert client.get(f"/grids/{grid}", headers=AUTH).json()["steps"] == []


def test_a_click_on_a_chart_whose_column_left_the_view_says_so(client: TestClient) -> None:
    # It never answered: the column lookup's StopIteration cannot be set on an asyncio future.
    grid = grid_id(push(client, "o", hours()))
    card = chart(client, grid, {"mark": "bar", "x": {"field": "kind"}, "y": {"aggregate": "count"}})
    keep = {"steps": [{"kind": "columns", "params": {"keep": ["day"]}}]}
    client.post(f"/grids/{grid}/steps", json=keep, headers=AUTH)
    refused = client.post(f"/cards/{card}/select", json={"value": "a"}, headers=AUTH)
    assert refused.status_code == 400 and "kind" in refused.text


def test_a_heatmap_click_filters_on_both_axes(client: TestClient) -> None:
    grid = grid_id(push(client, "o", hours()))
    cells = {"mark": "heatmap", "x": {"field": "kind"}, "y": {"field": "day", "aggregate": "count"}}
    card = chart(client, grid, cells)
    picked = client.post(f"/cards/{card}/select", json={"value": "a", "y": "2026-01-02"}, headers=AUTH)
    assert picked.status_code == 200, picked.text
    assert row_count(client, grid) == 8
