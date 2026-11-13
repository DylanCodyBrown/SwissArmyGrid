"""The shared statistics (domain/stats.py) against published values."""

from __future__ import annotations

import math

import pytest

from swissarmygrid.domain import stats as S


@pytest.mark.parametrize(
    ("t", "df", "p"),
    [
        (2.228138851986, 10, 0.05),  # Student's t table, two-sided
        (2.009575237129, 49, 0.05),
        (3.169272672617, 10, 0.01),
        (0.0, 7, 1.0),
    ],
)
def test_t_p_values(t: float, df: int, p: float) -> None:
    assert S.t_p(t, df) == pytest.approx(p, abs=1e-9)


@pytest.mark.parametrize(
    ("f", "df1", "df2", "p", "within"),
    [
        (4.102821015130, 2, 10, 0.05, 1e-8),  # exact: F(2, 10) has a closed form
        (7.559432157, 2, 10, 0.01, 1e-8),
        (2.5336, 5, 30, 0.05, 1e-5),  # printed tables give four decimals
    ],
)
def test_f_p_values(f: float, df1: int, df2: int, p: float, within: float) -> None:
    assert S.f_p(f, df1, df2) == pytest.approx(p, abs=within)


@pytest.mark.parametrize(
    ("p", "z"),
    [(0.975, 1.959963984540054), (0.5, 0.0), (0.001, -3.090232306167813), (0.9999, 3.719016485455709)],
)
def test_normal_quantiles(p: float, z: float) -> None:
    assert S.normal_quantile(p) == pytest.approx(z, abs=1e-12)


def test_the_incomplete_beta_at_its_edges_and_a_known_value() -> None:
    assert S.incomplete_beta(2, 3, 0) == 0 and S.incomplete_beta(2, 3, 1) == 1
    # I_x(1, b) = 1 - (1 - x)^b
    assert S.incomplete_beta(1, 3, 0.2) == pytest.approx(1 - 0.8**3, rel=1e-12)


def test_a_correlations_p_value() -> None:
    t = 0.5 * math.sqrt(28 / 0.75)
    assert S.correlation_p(0.5, 30) == pytest.approx(S.t_p(t, 28)) and S.correlation_p(0.5, 30) < 0.01
    assert S.correlation_p(0.1, 2) == 1.0 and S.correlation_p(1.0, 10) == 0.0
