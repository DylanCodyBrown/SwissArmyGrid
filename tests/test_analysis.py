"""The fit test (chi-square) and outliers, on made-up invoices: amounts that follow Benford's law
beside amounts someone typed in, planted outliers per vendor, and a misspelled region."""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from swissarmygrid.domain import analysis as A
from swissarmygrid.domain.sql_guard import keeps_order
from tests.conftest import AGENT, AUTH, grid_id, make_client, push

# ===== the domain: the math =====


@pytest.mark.parametrize(
    ("statistic", "df", "p"),
    [
        (3.841458820694124, 1, 0.05),  # the textbook critical values
        (6.634896601021213, 1, 0.01),
        (11.070497693516351, 5, 0.05),
        (15.50731305586545, 8, 0.05),
        (0.5, 3, 0.9188914116546758),
        (100.0, 10, 5.4497019829205215e-17),  # even df: exp(-50) * sum(50^k / k!, k < 5)
        (0.0, 4, 1.0),
    ],
)
def test_the_chi_square_p_value_matches_known_values(statistic: float, df: int, p: float) -> None:
    assert A.chi_square_p(statistic, df) == pytest.approx(p, rel=1e-9)


def test_benford_shares_add_up_and_start_with_thirty_percent_ones() -> None:
    one, two = A.benford_shares(1), A.benford_shares(2)
    assert sum(one.values()) == pytest.approx(1.0) and sum(two.values()) == pytest.approx(1.0)
    assert one["1"] == pytest.approx(0.30103, abs=1e-5) and len(two) == 90


def test_an_even_spread_fits_and_a_lopsided_one_does_not() -> None:
    even = A.fit([("North", 251), ("South", 249), ("East", 250), ("West", 250)])
    assert even["p"] > 0.99 and even["df"] == 3 and even["verdict"].startswith("Fits")
    lopsided = A.fit([("North", 3000), ("South", 2333), ("East", 2333), ("West", 2334)])
    assert lopsided["p"] < 0.0001 and lopsided["verdict"].startswith("Doesn't fit")
    assert lopsided["effect"] == "small"  # w = 0.115: certain, but not a big difference
    assert lopsided["categories"][0]["value"] == "North" and lopsided["categories"][0]["residual"] > 0


def test_expected_shares_leave_out_and_name_values_they_do_not_expect() -> None:
    result = A.fit([("A", 50), ("B", 50), ("Typo", 1)], {"A": 0.5, "B": 0.4, "C": 0.1})
    assert result["left_out"] == ["Typo"] and result["n"] == 100
    assert [c["value"] for c in result["categories"]] == ["A", "B", "C"]  # C expected, never seen: 0
    assert any("left out" in w for w in result["warnings"])
    small = A.fit([("A", 3), ("B", 1)])
    assert any("rough" in w for w in small["warnings"])
    with pytest.raises(A.AnalysisError, match="two different"):
        A.fit([("A", 10)])


def test_two_columns_together_are_related_or_independent() -> None:
    related = A.together(
        [("East", "late", 90), ("East", "on time", 10), ("West", "late", 10), ("West", "on time", 90)]
    )
    assert (
        related["p"] < 0.0001
        and related["effect_size"] == pytest.approx(0.8)
        and related["effect"] == "large"
    )
    apart = A.together(
        [("East", "late", 50), ("East", "on time", 50), ("West", "late", 50), ("West", "on time", 50)]
    )
    assert apart["p"] == pytest.approx(1.0) and apart["verdict"].startswith("Independent")


# ===== the domain: the queries, on DuckDB directly =====


def test_frequency_outliers_weigh_counting_noise_and_the_natural_spread() -> None:
    con = duckdb.connect()
    tables = {
        # Near-equal counts: only the value seen 3 times stands out, not 990 among 1,000s.
        "near": "SELECT v FROM (VALUES ('a',1000),('b',1001),('c',990),('d',1002),('e',998),('f',3)) "
        "x(v, n), range(n)",
        # Values usually seen once: 10 copies stand out, 3 do not.
        "ids": "SELECT 'id' || i AS v FROM range(500) t(i) UNION ALL SELECT 'id7' FROM range(9) "
        "UNION ALL SELECT 'id8' FROM range(2)",
        # Values seen 500 to 2,000 times: a typo seen twice and a flood both stand out.
        "spread": "SELECT v FROM (VALUES ('a',500),('b',800),('c',1200),('d',2000),('e',950),('g',1500),"
        "('h',700),('typo',2),('flood',20000)) x(v, n), range(n)",
        "flat": "SELECT 'v' || (i % 7) AS v FROM range(7000) t(i)",
    }
    found = {}
    for name, sql in tables.items():
        con.execute(f"CREATE TABLE {name} AS {sql}")
        _, top = A.frequency_queries(name, "v", 3.5)
        found[name] = [v for v, _, _ in con.execute(top).fetchall()]
    assert found == {"near": ["f"], "ids": ["id7"], "spread": ["typo", "flood"], "flat": []}


def test_amount_fences_by_each_method_and_the_step_keeps_the_grids_order() -> None:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t AS SELECT i AS id, CASE WHEN i % 97 = 0 THEN 5000.0 ELSE 50 + (i % 13) END "
        "AS amount, ['A','B','C'][1 + i % 3] AS vendor FROM range(3000) t(i)"
    )
    for method in A.METHODS:
        summary, top = A.amount_queries("t", "amount", None, method, A.DEFAULT_THRESHOLD[method])
        [(_, n, lo, hi, *_rest, low, high)] = con.execute(summary).fetchall()
        assert n == 3000 and (low, high) == (0, 31) and lo < 50 and 62 < hi < 5000
        assert {x for _, x, _ in con.execute(top).fetchall()} == {5000.0}
    step = A.amount_step("amount", "vendor", [("A", 40.0, 70.0), (None, 1.0, 2.0)])
    sql = step["params"]["sql"]
    assert "CAST(\"vendor\" AS VARCHAR) = 'A'" in sql and "IS NULL" in sql and keeps_order(sql, "t")
    assert keeps_order(A.frequency_step("region", ["Nrth", "O'Brien"])["params"]["sql"], "t")
    with pytest.raises(A.AnalysisError, match="threshold"):
        A.check_outlier_options("amount", "robust", "high")


# ===== on the server =====


def invoices() -> pa.Table:
    """Made-up invoices. `amount` follows Benford's law (log-uniform over four decades); `typed` is
    what someone inventing numbers writes (even over 100-999); vendor V3 has three planted outliers;
    one region is misspelled once."""
    rng = np.random.default_rng(7)
    n = 6000
    amount = np.round(10 ** rng.uniform(1, 5, n), 2)
    vendor = np.array([f"V{i % 4}" for i in range(n)])
    regular = np.round(rng.uniform(190, 210, n), 2)  # no tails: only the planted three stand out
    regular[[30, 1030, 2030]] = [900.0, 905.0, 1.0]  # rows 30, 1030, 2030 are all V2
    region = rng.choice(["North", "South", "East", "West"], n, p=[0.4, 0.3, 0.2, 0.1]).astype(object)
    region[4321] = "Nrth"
    return pa.table(
        {
            "id": np.arange(n),
            "vendor": vendor,
            "region": region,
            "amount": amount,
            "typed": rng.integers(100, 1000, n).astype(float),
            "unit_cost": regular,
            "late": np.where((region == "West") & (rng.random(n) < 0.8), "late", "on time"),
        }
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with make_client(tmp_path) as test_client:
        yield test_client


@pytest.fixture
def grid(client: TestClient) -> str:
    return grid_id(push(client, "invoices", invoices()))


def fit(client: TestClient, grid: str, **body: Any) -> Any:
    response = client.post(f"/grids/{grid}/fit-test", json=body, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def test_benford_tells_natural_amounts_from_typed_ones(client: TestClient, grid: str) -> None:
    result = fit(client, grid, columns=["amount", "typed"], expected="benford")
    natural, typed = result["results"]
    assert natural["column"] == "amount" and natural["p"] > 0.01
    assert natural["conformity"] in ("close conformity", "acceptable conformity")
    assert typed["p"] < 0.0001 and typed["conformity"] == "nonconformity"
    assert [c["value"] for c in typed["categories"]] == [str(d) for d in range(1, 10)]
    two = fit(client, grid, columns=["amount"], expected="benford2")["results"][0]
    assert two["df"] == 89 and two["conformity"] != "nonconformity"


def test_even_shares_another_grid_and_together(client: TestClient, grid: str) -> None:
    even = fit(client, grid, columns=["region"])["results"][0]
    assert even["p"] < 0.0001 and even["categories"][0]["value"] == "North"
    same = invoices().slice(0, 3000)
    other = grid_id(push(client, "last_month", same, "?new_grid=1"))
    against = fit(client, grid, columns=["region"], expected="grid", reference={"grid": other})
    assert against["reference"]["dataset"] == "last_month"
    result = against["results"][0]
    assert result["p"] > 0.01 and result["left_out"] == ["Nrth"]  # row 4321: not in the first 3,000
    shares = fit(
        client,
        grid,
        columns=["region"],
        expected="shares",
        shares={"North": 4, "South": 3, "East": 2, "West": 1},
    )
    assert shares["results"][0]["p"] > 0.01 and shares["results"][0]["left_out"] == ["Nrth"]
    together = fit(client, grid, columns=["region", "late"], expected="together")
    assert together["p"] < 0.0001 and together["effect"] in ("medium", "large")


def test_a_fit_test_refuses_what_it_cannot_judge(client: TestClient, grid: str) -> None:
    many = client.post(f"/grids/{grid}/fit-test", json={"columns": ["amount"]}, headers=AUTH)
    assert many.status_code == 400 and "Bucket it first" in many.text
    text = client.post(
        f"/grids/{grid}/fit-test", json={"columns": ["region"], "expected": "benford"}, headers=AUTH
    )
    assert text.status_code == 400 and "not a number column" in text.text
    pair = client.post(
        f"/grids/{grid}/fit-test", json={"columns": ["region"], "expected": "together"}, headers=AUTH
    )
    assert pair.status_code == 400 and "exactly two" in pair.text


def test_outliers_by_amount_within_each_vendor_and_show_their_rows(client: TestClient, grid: str) -> None:
    response = client.post(
        f"/grids/{grid}/outliers", json={"column": "unit_cost", "by": "vendor"}, headers=AUTH
    )
    assert response.status_code == 200, response.text
    found = response.json()
    assert found["groups"] == 4 and found["groups_flagged"] == 1 and (found["low"], found["high"]) == (1, 2)
    assert found["fences"][0]["group"] == "V2"
    assert sorted(o["value"] for o in found["outliers"]) == [1.0, 900.0, 905.0]
    shown = client.post(f"/grids/{grid}/step-ops", json={"op": "add", "step": found["step"]}, headers=AUTH)
    assert shown.status_code == 200, shown.text
    rows = client.get(f"/grids/{grid}/rows", params={"start": 0, "end": 100}, headers=AUTH).json()["rows"]
    ids = [r["id"] if isinstance(r, dict) else r[0] for r in rows]
    assert ids == [30, 1030, 2030]  # the grid's own order


def test_outliers_by_frequency_find_the_misspelling(client: TestClient, grid: str) -> None:
    found = client.post(
        f"/grids/{grid}/outliers", json={"column": "region", "look": "frequency"}, headers=AGENT
    ).json()  # the agent may look too
    assert found["distinct"] == 5 and [o["value"] for o in found["outliers"]] == ["Nrth"]
    assert found["rarer"] == 1 and found["commoner"] == 0
    assert "IN ('Nrth')" in found["step"]["params"]["sql"]
    bad = client.post(f"/grids/{grid}/outliers", json={"column": "region"}, headers=AUTH)
    assert bad.status_code == 400 and "not a number column" in bad.text


# ===== odds and find predictors =====


def test_odds_by_hand_a_two_by_two_table() -> None:
    result = A.odds([("A", 100, 20), ("B", 100, 10)], "late")
    a = next(v for v in result["values"] if v["value"] == "A")
    # (20 x 90) / (80 x 10) = 2.25; Woolf: exp(ln 2.25 +- 1.96 sqrt(1/20 + 1/80 + 1/10 + 1/90))
    assert a["odds_ratio"] == pytest.approx(2.25) and a["rate"] == 0.2 and a["rest_rate"] == 0.1
    assert a["low"] == pytest.approx(0.9943, abs=1e-4) and a["high"] == pytest.approx(5.0916, abs=1e-4)
    assert not a["clear"]  # the range includes 1: not clearly different
    assert result["values"][0]["value"] == "A" and result["rate"] == 0.15
    plain = A.odds([("North", 300, None), ("West", 100, None)])
    west = plain["values"][1]
    assert west["share"] == 0.25 and west["one_in"] == 4 and west["odds_against"] == 3
    empty = A.odds([("A", 50, 0), ("B", 50, 10)], "late")["values"]
    assert all(math.isfinite(v["odds_ratio"]) for v in empty)  # half a count added to an empty cell


def test_buckets_read_as_people_write_ranges() -> None:
    expr = A.bucket_expression("amount", [25.0, 60.0, 60.0, 1250.5])
    assert "'1: up to 25'" in expr and "'2: 25 to 60'" in expr and "'3: 60 to 1,250'" in expr
    assert "'4: over 1,250'" in expr and expr.count("WHEN") == 4  # the repeated cut is used once
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT i::DOUBLE AS amount FROM range(1, 101) t(i)")
    [(edges,)] = con.execute(A.edges_query("t", "amount", 4)).fetchall()
    labels = con.execute(A.odds_query("t", A.bucket_expression("amount", edges), None)).fetchall()
    assert [n for _, n, _ in labels] == [25, 25, 25, 25]


def test_exclude_steps_keep_the_blank_rows() -> None:
    sql = A.amount_step("amount", None, [(None, 1.0, 9.0)], keep=False)["params"]["sql"]
    con = duckdb.connect()
    con.execute("CREATE TABLE grid AS SELECT * FROM (VALUES (5.0), (50.0), (NULL)) t(amount)")
    assert sorted(con.execute(sql).fetchall(), key=str) == [(5.0,), (None,)]
    assert keeps_order(sql, "t")


def orders() -> pa.Table:
    """Made-up orders: West is late far more often, large orders a little more; amount is price times
    units plus noise; office is region under another name (redundant on purpose)."""
    rng = np.random.default_rng(3)
    n = 8000
    region = rng.choice(["North", "South", "East", "West"], n, p=[0.4, 0.3, 0.2, 0.1])
    size = rng.choice(["small", "large"], n)
    units = rng.integers(1, 50, n)
    price = np.round(rng.uniform(5, 100, n), 2)
    late_p = np.where(region == "West", 0.4, 0.1) + np.where(size == "large", 0.1, 0)
    offices = {"North": "N1", "South": "S1", "East": "E1", "West": "W1"}
    return pa.table(
        {
            "order_id": np.arange(n),
            "region": region,
            "office": np.array([offices[r] for r in region]),
            "size": size,
            "units": units,
            "price": price,
            "late": np.where(rng.random(n) < late_p, "late", "on time"),
            "amount": np.round(price * units + rng.normal(0, 50, n), 2),
        }
    )


@pytest.fixture
def orders_grid(client: TestClient) -> str:
    return grid_id(push(client, "orders", orders()))


def test_the_odds_of_being_late_by_region_and_by_amount_range(client: TestClient, orders_grid: str) -> None:
    def odds(**body: Any) -> Any:
        response = client.post(f"/grids/{orders_grid}/odds", json=body, headers=AUTH)
        assert response.status_code == 200, response.text
        return response.json()

    plain = odds(column="region")
    assert [v["value"] for v in plain["values"]] == ["North", "South", "East", "West"]
    assert plain["values"][3]["one_in"] == pytest.approx(10, rel=0.05)
    late = odds(column="region", outcome={"column": "late", "values": ["late"]})
    west = late["values"][0]
    assert west["value"] == "West" and west["clear"] and 4 < west["odds_ratio"] < 6 and west["low"] > 3
    assert late["together"]["p"] < 0.0001
    ranges = odds(column="amount", buckets=4, outcome={"column": "amount", "op": "gt", "value": 2000})
    assert ranges["bucketed"] and [v["rows"] for v in ranges["values"]] == [2000] * 4
    assert ranges["outcome"] == "amount > 2,000"
    bad = client.post(
        f"/grids/{orders_grid}/odds",
        json={"column": "region", "outcome": {"column": "late", "op": "gt", "value": 1}},
        headers=AUTH,
    )
    assert bad.status_code == 400 and "number column" in bad.text


def test_find_predictors_ranks_flags_redundancy_and_skips_identifiers(
    client: TestClient, orders_grid: str
) -> None:
    amount = client.post(f"/grids/{orders_grid}/predictors", json={"target": "amount"}, headers=AUTH).json()
    assert amount["target_bucketed"]
    ranked = [c["column"] for c in amount["candidates"]]
    assert ranked[:2] == ["units", "price"] and amount["candidates"][0]["correlation"] > 0.6
    assert {c["strength"] for c in amount["candidates"][2:]} == {"none"}
    assert amount["skipped"] == [
        {"column": "order_id", "reason": "every row a different whole number: probably an identifier"}
    ]
    late = client.post(f"/grids/{orders_grid}/predictors", json={"target": "late"}, headers=AGENT).json()
    by = {c["column"]: c for c in late["candidates"]}
    assert by["region"]["strength"] == "moderate" and by["size"]["strength"] == "weak"
    assert late["redundant"] == [{"keep": "region", "drop": "office", "v": pytest.approx(1.0)}]
    assert by["office"]["same_as"] == "region"
