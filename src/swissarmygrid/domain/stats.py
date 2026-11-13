"""The statistics the analyses share, standard library only: the chi-square, Student's t, F and normal
distributions, effect sizes in words, and (for the regressions) small linear algebra.

Each function is written out from its textbook definition, tested against published values in
tests/test_stats.py, and reached by generated code through `swissarmygrid.stats`.
"""

from __future__ import annotations

import math

# ===== distributions =====


def chi_square_p(statistic: float, df: int) -> float:
    """P(X >= statistic) for chi-square with `df` degrees of freedom: the regularized upper incomplete
    gamma function Q(df/2, statistic/2), by its series below a + 1 and its continued fraction above
    (Numerical Recipes 6.2). Accurate to about 1e-12, which is far more than a p-value needs."""
    if df <= 0:
        raise ValueError("Degrees of freedom must be at least 1.")
    if statistic <= 0:
        return 1.0
    a, x = df / 2.0, statistic / 2.0
    log_front = a * math.log(x) - x - math.lgamma(a)
    if x < a + 1.0:
        term = total = 1.0 / a
        n = a
        for _ in range(10_000):
            n += 1.0
            term *= x / n
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return max(0.0, 1.0 - total * math.exp(log_front))
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 10_000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        step = d * c
        h *= step
        if abs(step - 1.0) < 1e-15:
            break
    return min(1.0, math.exp(log_front) * h)


def _continued_fraction(a: float, b: float, x: float) -> float:
    """The continued fraction of the incomplete beta function (Numerical Recipes 6.4, betacf)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = tiny if abs(d) < tiny else d
    d = 1.0 / d
    h = d
    for m in range(1, 10_000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = tiny if abs(d) < tiny else d
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = tiny if abs(d) < tiny else d
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        step = d * c
        h *= step
        if abs(step - 1.0) < 1e-15:
            break
    return h


def incomplete_beta(a: float, b: float, x: float) -> float:
    """The regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _continued_fraction(a, b, x) / a
    return 1.0 - front * _continued_fraction(b, a, 1.0 - x) / b


def t_p(t: float, df: float) -> float:
    """Two-sided p-value of Student's t with `df` degrees of freedom."""
    if df <= 0:
        raise ValueError("Degrees of freedom must be above 0.")
    if not math.isfinite(t):
        return 0.0
    return incomplete_beta(df / 2.0, 0.5, df / (df + t * t))


def f_p(f: float, df1: float, df2: float) -> float:
    """P(F >= f) for the F distribution with (df1, df2) degrees of freedom."""
    if f <= 0:
        return 1.0
    return incomplete_beta(df2 / 2.0, df1 / 2.0, df2 / (df2 + df1 * f))


def normal_quantile(p: float) -> float:
    """The z with P(Z <= z) = p, for the standard normal (Acklam's rational approximation, then one
    Halley step on erfc: accurate to about 1e-15)."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be between 0 and 1.")
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00)
    low = 0.02425
    if p < low:
        q = math.sqrt(-2 * math.log(p))
        x = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    elif p <= 1 - low:
        q = p - 0.5
        r = q * q
        x = (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
        )
    else:
        q = math.sqrt(-2 * math.log(1 - p))
        x = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    e = 0.5 * math.erfc(-x / math.sqrt(2)) - p
    u = e * math.sqrt(2 * math.pi) * math.exp(x * x / 2)
    return x - u / (1 + x * u / 2)


def correlation_p(r: float, n: int) -> float:
    """Two-sided p-value of a Pearson correlation r on n pairs (t = r sqrt((n - 2) / (1 - r^2)))."""
    if n < 3:
        return 1.0
    if abs(r) >= 1.0:
        return 0.0
    return t_p(r * math.sqrt((n - 2) / (1 - r * r)), n - 2)


# ===== effect sizes =====


def effect_words(size: float) -> str:
    """Cohen's thresholds for w (and, read the same way, Cramér's V)."""
    if size < 0.1:
        return "negligible"
    if size < 0.3:
        return "small"
    if size < 0.5:
        return "medium"
    return "large"
