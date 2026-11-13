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


def run_copied_chart(code: str, frame: Any) -> tuple[Any, list[tuple[str, tuple[Any, ...], dict[str, Any]]]]:
    """Run the copied Altair code as the user would, in a fresh namespace, with a stand-in for
    altair (not installed here) that records the chart it is asked to draw. Returns the frame
    handed to alt.Chart and every call made on altair."""
    import sys
    import types
    from unittest.mock import patch

    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    class Recorder:
        def __init__(self, name: str) -> None:
            self.name = name

        def __call__(self, *args: Any, **kwargs: Any) -> Recorder:
            calls.append((self.name, args, kwargs))
            return Recorder(f"{self.name}()")

        def __getattr__(self, attribute: str) -> Recorder:
            return Recorder(f"{self.name}.{attribute}")

    fake = types.ModuleType("altair")
    fake.__getattr__ = lambda attribute: Recorder(f"alt.{attribute}")  # type: ignore[method-assign]
    with patch.dict(sys.modules, {"altair": fake}):
        namespace: dict[str, Any] = {}
        exec(compile(code, "<copied>", "exec"), namespace)  # noqa: S102 (code this package generated)
        namespace["chart"](frame)
    data = next(args[0] for name, args, _ in calls if name == "alt.Chart")
    return data, calls


def _plain(value: Any) -> Any:
    """A point's value as the card's JSON has it, from what DuckDB's .df() gives."""
    import math

    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


@pytest.mark.parametrize(
    "spec",
    [
        {"mark": "bar", "x": {"field": "col:on"}, "y": {"aggregate": "sum", "field": 'q"uote'}},
        {"mark": "bar", "x": {"field": "a.b"}, "y": {"aggregate": "count"}, "color": {"field": "col:on"}},
        {
            "mark": "line",
            "x": {"field": "day", "time_unit": "month"},
            "y": {"aggregate": "mean", "field": "a.b"},
        },
        {"mark": "area", "x": {"field": "a.b"}, "y": {"aggregate": "max", "field": 'q"uote'}},
        {
            "mark": "point",
            "x": {"field": "col:on"},
            "y": {"aggregate": "min", "field": "a.b"},
            "title": 'It\'s "x"',
        },
        {"mark": "heatmap", "x": {"field": "col:on"}, "y": {"field": "a.b", "aggregate": "count"}},
        {"mark": "histogram", "x": {"field": 'q"uote', "bins": 7}},
        {"mark": "bar", "x": {"field": "id"}, "y": {"aggregate": "count"}},  # over the 5,000 cap
    ],
)
def test_the_copied_altair_code_draws_the_cards_points(client: TestClient, spec: dict[str, Any]) -> None:
    # The copy put column names into Altair's shorthand ("col:on:N" is a syntax error there, a
    # quote ended the string, a dot reads a nested field) and handed Altair the whole frame,
    # which it refuses over 5,000 rows. It now aggregates with the grid's own query first.
    rows = 6000
    table = pa.table(
        {
            "id": pa.array(range(rows), pa.int64()),
            "col:on": pa.array([("a", "b", None)[i % 3] for i in range(rows)]),
            "a.b": pa.array([i % 7 for i in range(rows)], pa.int64()),
            'q"uote': pa.array([(i * 37) % 101 / 4 for i in range(rows)]),
            "day": pa.array([date(2026, 1, 1) + timedelta(days=i % 90) for i in range(rows)]),
        }
    )
    grid = grid_id(push(client, "o", table))
    card = add(client, grid, {"kind": "chart", "spec": spec}).json()["card"]
    shown = cards(client, grid)[0]
    code = client.get(f"/cards/{card}/code", headers=AUTH).json()["code"]
    data, calls = run_copied_chart(code, table.to_pandas())
    assert len(data) == shown["points"] <= 5000
    expected = shown["vega_lite"]["data"]["values"]
    got = [{k: _plain(v) for k, v in row.items()} for row in data.to_dict("records")]
    if spec["x"]["field"] == "id":  # the 5,000 largest of 6,000 equal groups: any 5,000 of them
        assert len({r["x"] for r in got}) == 5000
    else:
        # Groups that tie on y may come in either order.
        want = [{k: _plain(v) for k, v in row.items()} for row in expected]
        assert sorted(map(repr, got)) == sorted(map(repr, want))
        if spec["mark"] in ("line", "area"):
            assert got == want  # in x order
    vl_mark = {"histogram": "bar", "heatmap": "rect"}.get(spec["mark"], spec["mark"])
    assert f"alt.Chart().mark_{vl_mark}" in {name for name, _, _ in calls}
    if "title" in spec:
        assert next(kwargs for name, _, kwargs in calls if name == "alt.Chart")["title"] == spec["title"]


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


def test_a_click_value_given_as_text_is_read_as_the_columns_type(client: TestClient) -> None:
    # `sag sandbox_select <card> --value 3` sends "3": the filter step was refused for a number column.
    grid = grid_id(push(client, "o", big_table(100)))
    by_qty = chart(client, grid, {"mark": "bar", "x": {"field": "qty"}, "y": {"aggregate": "count"}})
    assert client.post(f"/cards/{by_qty}/select", json={"value": "3"}, headers=AUTH).status_code == 200
    assert row_count(client, grid) == 2
    client.post(f"/grids/{grid}/steps", json={"steps": []}, headers=AUTH)
    by_price = chart(client, grid, {"mark": "bar", "x": {"field": "price"}, "y": {"aggregate": "count"}})
    assert client.post(f"/cards/{by_price}/select", json={"value": "0.0"}, headers=AUTH).status_code == 200
    assert row_count(client, grid) == 1
    refused = client.post(f"/cards/{by_qty}/select", json={"value": "three"}, headers=AUTH)
    assert refused.status_code == 400


def test_a_heatmap_click_filters_on_both_axes(client: TestClient) -> None:
    grid = grid_id(push(client, "o", hours()))
    cells = {"mark": "heatmap", "x": {"field": "kind"}, "y": {"field": "day", "aggregate": "count"}}
    card = chart(client, grid, cells)
    picked = client.post(f"/cards/{card}/select", json={"value": "a", "y": "2026-01-02"}, headers=AUTH)
    assert picked.status_code == 200, picked.text
    assert row_count(client, grid) == 8


def test_figures_images_and_dot_keep_their_text_and_bad_ones_say_why(client: TestClient) -> None:
    grid = grid_id(push(client, "small", pa.table({"a": [1, 2]})))
    figure = '{"data": [{"type": "bar", "x": ["a"], "y": [1]}], "layout": {}}'
    for kind, body in (
        ("plotly", figure),
        ("echarts", '{"series": [{"type": "bar", "data": [1]}]}'),
        ("graphviz", "digraph { a -> b }"),
        ("image", "data:image/png;base64,iVBORw0KGgo="),
    ):
        assert add(client, grid, {"kind": kind, "title": kind, "body": body}).status_code == 200
    shown = {c["kind"]: c for c in cards(client, grid)}
    assert shown["plotly"]["body"] == figure and shown["graphviz"]["body"] == "digraph { a -> b }"
    bad = add(client, grid, {"kind": "plotly", "body": "not json"})
    assert bad.status_code == 400 and "not JSON" in bad.text
    assert add(client, grid, {"kind": "image", "body": "https://example.com/x.png"}).status_code == 400


def test_cards_move_before_another_and_can_start_a_page(client: TestClient) -> None:
    grid = grid_id(push(client, "small", pa.table({"a": [1, 2]})))
    ids = [
        add(client, grid, {"kind": "note", "title": t, "body": t}).json()["card"]
        for t in ("one", "two", "three")
    ]

    def titles() -> list[str]:
        return [c["title"] for c in cards(client, grid)]

    assert titles() == ["one", "two", "three"]
    assert client.patch(f"/cards/{ids[2]}", json={"before": ids[0]}, headers=AUTH).status_code == 200
    assert titles() == ["three", "one", "two"]
    client.patch(f"/cards/{ids[2]}", json={"before": None}, headers=AUTH)
    assert titles() == ["one", "two", "three"]
    client.patch(f"/cards/{ids[1]}", json={"new_page": True}, headers=AUTH)
    assert [c["new_page"] for c in cards(client, grid)] == [False, True, False]
    assert client.patch(f"/cards/{ids[1]}", json={"before": "nope"}, headers=AUTH).status_code == 400
    assert titles() == ["one", "two", "three"]  # a refused move changes nothing


def test_a_cards_size_is_kept_and_checked(client: TestClient) -> None:
    grid = grid_id(push(client, "small", pa.table({"a": [1, 2]})))
    card = add(client, grid, {"kind": "note", "body": "hi", "size": {"w": 5, "h": 8}}).json()["card"]
    assert cards(client, grid)[0]["size"] == {"w": 5, "h": 8}
    assert client.patch(f"/cards/{card}", json={"size": {"w": 7, "h": 4}}, headers=AUTH).status_code == 200
    assert cards(client, grid)[0]["size"] == {"w": 7, "h": 4}
    assert client.patch(f"/cards/{card}", json={"size": {"w": 13, "h": 4}}, headers=AUTH).status_code == 400
