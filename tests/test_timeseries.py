"""Time series and forecast on made-up monthly sales with planted parts: a rising trend, December 34%
above a typical month and February 20% below, a spike in June 2021, and a step up in January 2023. The
analysis must find each, and nothing that was not planted."""

from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from swissarmygrid.domain import timeseries as T
from swissarmygrid.domain.sql_guard import keeps_order
from tests.conftest import AGENT, AUTH, grid_id, make_client, push


def monthly(shift: float = 300.0, seed: int = 4) -> tuple[list[date], list[float]]:
    rng = random.Random(seed)  # noqa: S311 (made-up test data)
    periods, values = [], []
    day = date(2019, 1, 1)
    for i in range(72):
        season = 1.34 if day.month == 12 else 0.8 if day.month == 2 else 1.0
        level = 1000 + 10 * i + (shift if day >= date(2023, 1, 1) else 0)
        value = level * season * rng.gauss(1, 0.02)
        if day == date(2021, 6, 1):
            value *= 1.6
        periods.append(day)
        values.append(value)
        day = T.step_period(day, "month")
    return periods, values


# ===== the domain =====


def test_periods_step_and_gaps_are_named() -> None:
    assert T.step_period(date(2024, 11, 1), "month", 3) == date(2025, 2, 1)
    assert T.step_period(date(2024, 1, 1), "quarter") == date(2024, 4, 1)
    periods, values = T.complete([(date(2024, 1, 1), 5.0), (date(2024, 3, 1), 7.0)], "month")
    assert periods == [date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1)] and values == [5.0, None, 7.0]


@pytest.mark.parametrize("seed", [4, 5, 6, 7])
def test_the_planted_season_spike_and_step_are_found_and_nothing_else(seed: int) -> None:
    periods, values = monthly(seed=seed)
    dec = T.decompose(periods, values, "month")
    assert dec["seasonal"] and dec["multiplicative"]
    assert 1.25 < dec["index"][11] < 1.40 and 0.75 < dec["index"][1] < 0.85  # December, February
    assert all(0.95 < v < 1.05 for k, v in dec["index"].items() if k not in (1, 11))
    adjusted = T._adjusted(values, dec["season"], dec["multiplicative"])
    shifts = T.level_shifts(periods, adjusted, T.big_spikes(periods, adjusted))
    # The step is placed within a month of where it was planted (December's peak sits right before it).
    assert len(shifts) == 1 and shifts[0]["period"] in ("2022-12-01", "2023-01-01", "2023-02-01")
    spikes = T.anomalies(periods, values, dec["season"], dec["multiplicative"], shifts)
    assert [(a["period"], a["kind"]) for a in spikes][:1] == [("2021-06-01", "spike")]
    assert len(spikes) <= 2  # at most one more, at the edge of a step placed a month off
    steady_periods, steady_values = monthly(shift=0, seed=seed)
    flat = T.decompose(steady_periods, steady_values, "month")
    flat_adjusted = T._adjusted(steady_values, flat["season"], flat["multiplicative"])
    assert T.level_shifts(steady_periods, flat_adjusted, T.big_spikes(steady_periods, flat_adjusted)) == []


def test_the_forecast_continues_trend_and_season_and_earns_its_miss() -> None:
    periods, values = monthly(shift=0)
    result = T.fit_forecast(periods, values, "month", 12, [])
    assert result["seasonal"] and result["trend_per_period"] == pytest.approx(10, abs=2.5)
    ahead = {f["period"]: f for f in result["forecast"]}
    january, december = ahead["2025-01-01"], ahead["2025-12-01"]
    assert january["estimate"] == pytest.approx(1720, rel=0.06)  # 1000 + 10 x 72
    assert december["estimate"] > january["estimate"] * 1.2  # the season carried forward
    assert january["low95"] < january["low80"] < january["estimate"] < january["high80"] < january["high95"]
    assert result["check"]["periods"] == 12 and result["check"]["typical_miss_share"] < 0.06


def test_a_predictor_follows_the_future_it_is_given() -> None:
    history = [1.0, 2.0, 3.0, 4.0]
    assert T.future_predictor(history, {"future": "last"}, 2) == [4.0, 4.0]
    assert T.future_predictor(history, {"future": "trend"}, 2) == pytest.approx([5.0, 6.0])
    assert T.future_predictor(history, {"future": "value", "value": 9}, 2) == [9.0, 9.0]


# ===== on the server =====


def sales_rows() -> pa.Table:
    """The monthly totals spread over made-up daily sales rows, so the server does the totalling."""
    periods, values = monthly(shift=300)
    days, amounts, stores = [], [], []
    for p, v in zip(periods, values, strict=True):
        for k in range(4):
            days.append(date(p.year, p.month, 1 + 7 * k))
            amounts.append(v / 4)
            stores.append(["North", "South"][k % 2])
    return pa.table({"sold_on": days, "store": stores, "amount": amounts})


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with make_client(tmp_path) as test_client:
        yield test_client


@pytest.fixture
def grid(client: TestClient) -> str:
    return grid_id(push(client, "sales", sales_rows()))


def post(client: TestClient, grid: str, route: str, **body: Any) -> Any:
    response = client.post(f"/grids/{grid}/{route}", json=body, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_time_series_reads_the_planted_series_in_words(client: TestClient, grid: str) -> None:
    r = post(client, grid, "time-series", date="sold_on", metric="amount", total="sum", period="month")
    assert len(r["points"]) == 72 and r["gaps"] == [] and r["seasonal"]
    assert r["season_words"][0].startswith("December run") and "above a typical month" in r["season_words"][0]
    assert [a["period"] for a in r["anomalies"]] == ["2021-06-01"]
    assert [s["period"] for s in r["shifts"]] == ["2023-01-01"]
    assert any("The level stepped at 2023-01-01" in w and "(+20%)" in w for w in r["words"])
    split = post(client, grid, "time-series", date="sold_on", total="count", period="quarter", split="store")
    assert {line["name"] for line in split["lines"]} == {"North", "South"}
    sql = r["step"]["params"]["sql"]
    assert keeps_order(sql, "sales") and "DATE '2021-06-01'" in sql
    shown = client.post(f"/grids/{grid}/step-ops", json={"op": "add", "step": r["step"]}, headers=AUTH)
    assert shown.status_code == 200
    count = client.get(f"/grids/{grid}/count", headers=AUTH).json()["row_count"]
    assert count == 4  # the four sales of June 2021


def test_the_forecast_over_http_with_a_predictor(client: TestClient, grid: str) -> None:
    r = client.post(
        f"/grids/{grid}/forecast",
        json={"date": "sold_on", "metric": "amount", "period": "month", "horizon": 6},
        headers=AGENT,
    ).json()
    assert len(r["forecast"]) == 6 and r["forecast"][0]["period"] == "2025-01-01"
    assert r["check"] and any("Held back the last 12 months" in w for w in r["words"])
    assert r["steps"] == ["2023-01-01"] and r["trend_per_period"] == pytest.approx(10, abs=2.5)
    refused = client.post(
        f"/grids/{grid}/forecast", json={"date": "amount", "metric": "amount"}, headers=AUTH
    )
    assert refused.status_code == 400 and "not a date or timestamp column" in refused.text


def test_the_notebook_code_gives_the_windows_series_and_forecast(client: TestClient, grid: str) -> None:
    namespace: dict[str, Any] = {"sales": sales_rows().to_pandas()}
    for analysis, route, body in (
        ("time_series", "time-series", {"date": "sold_on", "metric": "amount", "period": "month"}),
        ("forecast", "forecast", {"date": "sold_on", "metric": "amount", "period": "quarter", "horizon": 4}),
    ):
        window = post(client, grid, route, **body)
        made = client.post(
            f"/grids/{grid}/analysis-code", json={"analysis": analysis, "body": body}, headers=AUTH
        ).json()
        exec("\n".join([*made["imports"], made["code"]]), namespace)  # noqa: S102 (our own generated code)
        mine = namespace[made["code"].splitlines()[-1]]
        assert mine["words"] == window["words"], analysis
