"""Show the math: each analysis result on made-up orders, proven step by step. The proof's own working
must land on the numbers the window showed."""

from __future__ import annotations

import math
import random
import re
from typing import Any

import pandas as pd
import pytest
from starlette.testclient import TestClient

from swissarmygrid import stats
from swissarmygrid.domain.proofs import PROOFS, ProofError, n, proof
from tests.conftest import AUTH


def orders() -> pd.DataFrame:
    rng = random.Random(11)  # noqa: S311 (made-up test data)
    regions = [rng.choice(["North", "South", "East", "West"]) for _ in range(400)]
    return pd.DataFrame(
        {
            "region": regions,
            "late": [
                "late" if rng.random() < (0.45 if r == "North" else 0.25) else "on time" for r in regions
            ],
            "amount": [
                round(rng.lognormvariate(4, 0.6) + (900 if i in (5, 77) else 0), 2)
                for i, r in enumerate(regions)
            ],
            "units": [rng.randint(1, 9) for _ in range(400)],
            "sold_on": pd.date_range("2020-01-05", periods=400, freq="W"),
        }
    )


LATE = {"column": "late", "values": ["late"]}


def bold_numbers(markdown: str) -> list[str]:
    return re.findall(r"\*\*([^*]+)\*\*", markdown)


def test_the_fit_tests_chi_square_adds_up_from_its_table() -> None:
    result = stats.fit_test(orders(), columns=["region"])
    md = proof("fit_test", result, "orders")["markdown"]
    r = result["results"][0]
    assert f"= **{n(r['statistic'])}**" in md and f"**{r['df']}**" in md
    assert n(math.sqrt(r["statistic"] / r["n"])) in md  # Cohen's w worked out
    assert "chisquare([" in md  # a line to redo it with scipy


def test_each_odds_ratio_is_worked_from_its_two_by_two_table() -> None:
    result = stats.odds(orders(), column="region", outcome=LATE)
    md = proof("odds", result)["markdown"]
    for v in result["values"]:
        assert f"= {n(v['odds_ratio'])} |" in md, v["value"]
        assert f"{n(v['low'])} to {n(v['high'])}" in md
    assert "Cramér's V" in md and n(result["together"]["effect_size"]) in md


def test_the_outlier_fences_come_from_the_spread_shown() -> None:
    data = orders()
    for method in ("robust", "iqr", "zscore"):
        result = stats.outliers(data, column="amount", method=method)
        f = result["fences"][0]
        md = proof("outliers", result)["markdown"]
        assert f"**{n(f['low_fence'])}**" in md and f"**{n(f['high_fence'])}**" in md, method
        if method == "robust":
            assert abs(f["median"] + result["threshold"] * f["mad"] / 0.6745 - f["high_fence"]) < 1e-6
        if method == "iqr":
            assert abs(f["q3"] + result["threshold"] * (f["q3"] - f["q1"]) - f["high_fence"]) < 1e-6
    freq = stats.outliers(
        pd.DataFrame({"code": ["a"] * 50 + ["b"] * 48 + ["c"] * 52 + ["d"] * 49 + ["e"] * 51 + ["f"] * 3}),
        column="code",
        look="frequency",
    )
    md = proof("outliers", freq)["markdown"]
    assert "| f | 3 |" in md and n(freq["outliers"][0]["score"]) in md


def test_regression_shows_t_ranges_and_r_squared_from_the_sums() -> None:
    result = stats.regression(orders(), target="amount", predictors=["units", "region"])
    md = proof("regression", result)["markdown"]
    for c in result["coefficients"]:
        assert f"= {n(c['t'])}" in md and f"{n(c['low'])} to {n(c['high'])}" in md
    assert f"**{n(result['fit']['r2'])}**" in md
    logistic = stats.regression(orders(), target="late", predictors=["region"], outcome=LATE)
    md = proof("regression", logistic)["markdown"]
    assert all(n(c["odds_ratio"]) in md for c in logistic["coefficients"]) and "McFadden" in md
    model = {k: result["model"][k] for k in ("kind", "terms", "coefficients", "inverse", "sigma2", "df")}
    what_if = stats.predict(model, {"units": 4, "region": "North"})
    md = proof("predict", what_if)["markdown"]
    assert abs(sum(t["contribution"] for t in what_if["terms"]) - what_if["estimate"]) < 1e-9
    assert f"**{n(what_if['estimate'])}**" in md


def test_predictors_time_series_forecast_and_tie_out_prove() -> None:
    data = orders()
    found = stats.predictors(data, target="late")
    md = proof("predictors", found)["markdown"]
    for c in found["candidates"]:
        assert n(c["v"]) in md, c["column"]
    series = stats.time_series(data, date="sold_on", metric="amount", period="month")
    md = proof("time_series", series)["markdown"]
    assert all(f"| {s['position']} | {n(s['index'])} |" in md for s in series["season_index"])
    for a in series["anomalies"]:
        assert abs((a["off"] - a["typical_off"]) / a["spread"] - a["score"]) < 1e-9 and n(a["score"]) in md
    ahead = stats.forecast(data, date="sold_on", metric="amount", period="month", horizon=3)
    md = proof("forecast", ahead)["markdown"]
    assert all(f"{n(f['low80'])} to {n(f['high80'])}" in md for f in ahead["forecast"])
    assert set(PROOFS) >= {
        "fit_test",
        "outliers",
        "odds",
        "predictors",
        "regression",
        "predict",
        "time_series",
        "forecast",
        "tie_out",
    }


def test_bad_requests_say_why_and_the_route_answers(client: TestClient) -> None:
    with pytest.raises(ProofError, match="Show the math covers"):
        proof("tarot", {})
    with pytest.raises(ProofError, match="missing what its proof needs"):
        proof("regression", {"coefficients": [{"term": "x"}]})
    result: Any = stats.odds(orders(), column="region")
    answer = client.post(
        "/analysis-proof", json={"analysis": "odds", "result": result, "dataset": "orders"}, headers=AUTH
    )
    assert answer.status_code == 200 and answer.json()["title"] == "The math: Odds on orders"
    assert (
        client.post("/analysis-proof", json={"analysis": "odds", "result": 3}, headers=AUTH).status_code
        == 400
    )
