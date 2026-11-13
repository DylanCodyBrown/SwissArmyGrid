"""Regression (domain/regression.py, services/regression.py) on made-up data with planted answers:
amount = 50 + 3 x units + 20 for West - 5 for East + noise, and late with log-odds -2 + 1.5 for West
+ 0.02 x units. The fits must recover them; least squares must match numpy's."""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from swissarmygrid.domain import regression as R
from swissarmygrid.domain.analysis import AnalysisError
from swissarmygrid.domain.sql_guard import keeps_order
from tests.conftest import AGENT, AUTH, grid_id, make_client, push

# ===== the domain =====


def test_cholesky_solves_and_inverts() -> None:
    a = [[4.0, 2.0, 0.6], [2.0, 5.0, 1.0], [0.6, 1.0, 3.0]]
    low = R.cholesky(a)
    x = R.solve(low, [1.0, 2.0, 3.0])
    assert np.allclose(np.array(a) @ np.array(x), [1.0, 2.0, 3.0])
    assert np.allclose(np.array(R.inverse(low)), np.linalg.inv(np.array(a)))
    with pytest.raises(AnalysisError, match="say the same thing"):
        R.cholesky([[1.0, 1.0], [1.0, 1.0]])


@pytest.mark.parametrize(
    ("p", "df", "t"), [(0.975, 10, 2.228138851986), (0.975, 30, 2.042272456301), (0.95, 5, 2.015048372669)]
)
def test_t_quantiles_match_the_table(p: float, df: int, t: float) -> None:
    assert R.t_quantile(p, df) == pytest.approx(t, abs=1e-8)
    assert R.t_quantile(1 - p, df) == pytest.approx(-t, abs=1e-8)


def orders(n: int = 20000) -> pa.Table:
    rng = np.random.default_rng(11)
    region = rng.choice(["North", "South", "East", "West"], n, p=[0.4, 0.3, 0.2, 0.1])
    units = rng.integers(1, 50, n).astype(float)
    amount = 50 + 3 * units + 20 * (region == "West") - 5 * (region == "East") + rng.normal(0, 10, n)
    logit = -2 + 1.5 * (region == "West") + 0.02 * units
    late = np.where(rng.random(n) < 1 / (1 + np.exp(-logit)), "late", "on time")
    amount[5] = np.nan  # a blank: left out and counted
    return pa.table(
        {"order_id": np.arange(n), "region": region, "units": units, "amount": amount, "late": late}
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with make_client(tmp_path) as test_client:
        yield test_client


@pytest.fixture
def grid(client: TestClient) -> str:
    return grid_id(push(client, "orders", orders()))


def fit(client: TestClient, grid: str, **body: Any) -> Any:
    response = client.post(f"/grids/{grid}/regression", json=body, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def test_linear_regression_recovers_the_planted_effects_and_matches_numpy(
    client: TestClient, grid: str
) -> None:
    r = fit(client, grid, target="amount", predictors=["units", "region"])
    by = {c["term"]: c for c in r["coefficients"]}
    assert by["units"]["coefficient"] == pytest.approx(3, abs=0.03)
    assert by["region = West"]["coefficient"] == pytest.approx(20, abs=0.7)  # against North, the commonest
    assert by["region = East"]["coefficient"] == pytest.approx(-5, abs=0.6)
    assert by["region = South"]["p"] > 0.01 and by["units"]["p"] < 1e-10
    assert r["references"] == {"region": "North"} and r["left_out"] == 1 and r["n"] == 19999
    assert r["fit"]["r2"] > 0.9 and r["fit"]["rmse"] == pytest.approx(10, abs=0.3)
    assert "each extra 1 of units: +3" in by["units"]["words"]
    table = orders().to_pandas().dropna()
    levels = ["South", "East", "West"]
    x = np.column_stack(
        [np.ones(len(table)), table.units - table.units.mean(), *(table.region == lv for lv in levels)]
    ).astype(float)
    numpy_b = np.linalg.lstsq(x, table.amount, rcond=None)[0]
    ours = [
        by["(intercept)"]["coefficient"],
        by["units"]["coefficient"],
        *(by[f"region = {lv}"]["coefficient"] for lv in levels),
    ]
    assert np.allclose(ours, numpy_b, rtol=1e-9)


def test_logistic_regression_recovers_the_planted_odds_ratios(client: TestClient, grid: str) -> None:
    r = fit(
        client,
        grid,
        target="late",
        predictors=["region", "units"],
        outcome={"column": "late", "values": ["late"]},
    )
    by = {c["term"]: c for c in r["coefficients"]}
    assert r["kind"] == "logistic" and r["iterations"] < 15
    west = by["region = West"]
    assert west["low"] < math.exp(1.5) < west["high"] and west["odds_ratio"] == pytest.approx(
        math.exp(1.5), rel=0.12
    )
    assert by["units"]["coefficient"] == pytest.approx(0.02, abs=0.004)
    assert r["fit"]["accuracy"] >= r["fit"]["majority"] - 0.01 and 0 < r["fit"]["pseudo_r2"] < 1
    assert "times the odds of late" in west["words"]


def test_what_if_predictions_have_ranges(client: TestClient, grid: str) -> None:
    linear = fit(client, grid, target="amount", predictors=["units", "region"])
    p = client.post(
        "/regression/predict",
        json={"model": linear["model"], "values": {"units": 10, "region": "West"}},
        headers=AUTH,
    ).json()
    assert p["estimate"] == pytest.approx(100, abs=1.5) and p["low"] < p["estimate"] < p["high"]
    assert p["high"] - p["low"] == pytest.approx(
        2 * 1.96 * 10, rel=0.05
    )  # a new row: about plus or minus 2 misses
    yes_no = fit(
        client,
        grid,
        target="late",
        predictors=["region", "units"],
        outcome={"column": "late", "values": ["late"]},
    )
    q = client.post(
        "/regression/predict",
        json={"model": yes_no["model"], "values": {"units": 25, "region": "West"}},
        headers=AGENT,
    ).json()
    assert q["estimate"] == pytest.approx(0.5, abs=0.05) and q["low"] < q["estimate"] < q["high"]
    bad = client.post(
        "/regression/predict", json={"model": linear["model"], "values": {"units": 10}}, headers=AUTH
    )
    assert bad.status_code == 400 and "region" in bad.text


def test_predicted_and_miss_columns_are_steps_that_keep_the_order(client: TestClient, grid: str) -> None:
    r = fit(client, grid, target="amount", predictors=["units", "region"])
    for name in ("predicted", "miss"):
        assert keeps_order(r["steps"][name]["params"]["sql"], "orders")
        added = client.post(
            f"/grids/{grid}/step-ops", json={"op": "add", "step": r["steps"][name]}, headers=AUTH
        )
        assert added.status_code == 200, added.text
    rows = client.get(f"/grids/{grid}/rows", params={"start": 0, "end": 3}, headers=AUTH).json()
    first = rows["rows"][0]
    values = dict(zip(rows["columns"], first, strict=False)) if isinstance(first, list) else first
    assert values["order_id"] == 0
    assert values["miss_amount"] == pytest.approx(values["amount"] - values["predicted_amount"])


def test_regression_refuses_what_it_cannot_fit(client: TestClient, grid: str) -> None:
    def refused(body: dict[str, Any], says: str) -> None:
        response = client.post(f"/grids/{grid}/regression", json=body, headers=AUTH)
        assert response.status_code == 400 and says in response.text, response.text

    refused({"target": "late", "predictors": ["units"]}, "not a number")
    refused({"target": "amount", "predictors": []}, "at least one predictor")
    refused({"target": "amount", "predictors": ["amount"]}, "cannot predict itself")
    refused(
        {"target": "late", "predictors": ["units"], "outcome": {"column": "late", "values": ["never"]}},
        "nothing to predict",
    )


def test_the_notebook_code_and_stats_give_the_windows_regression(client: TestClient, grid: str) -> None:
    from swissarmygrid import stats

    body = {"target": "amount", "predictors": ["units", "region"]}
    window = fit(client, grid, **body)
    made = client.post(
        f"/grids/{grid}/analysis-code", json={"analysis": "regression", "body": body}, headers=AUTH
    ).json()
    namespace: dict[str, Any] = {"orders": orders().to_pandas()}
    exec("\n".join([*made["imports"], made["code"]]), namespace)  # noqa: S102 (our own generated code)
    mine = namespace[made["code"].splitlines()[-1]]
    assert [c["coefficient"] for c in mine["coefficients"]] == pytest.approx(
        [c["coefficient"] for c in window["coefficients"]], rel=1e-9
    )
    direct = stats.regression(
        orders().to_pandas(),
        target="late",
        predictors=["region"],
        outcome={"column": "late", "values": ["late"]},
    )
    assert (
        direct["kind"] == "logistic" and stats.predict(direct["model"], {"region": "West"})["estimate"] > 0.3
    )
