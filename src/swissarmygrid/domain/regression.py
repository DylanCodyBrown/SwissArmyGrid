"""Regression: how a target moves with its predictors (Plans/analyze-menu.plan.md, phase B).

**Linear** (a number target) by least squares; **logistic** (a yes/no target: an outcome, as in Odds) by
iteratively reweighted least squares, whose results are odds ratios. DuckDB computes the sums of products
in one pass over the rows (X'X and X'y, each predictor as a SQL expression); Python solves the small system
(Cholesky), and gives standard errors, t (or z) and F p-values, ranges, R-squared and the typical miss.

A number predictor is used centred on its mean, which keeps the sums well conditioned and makes the
intercept the prediction at the means. A category becomes an indicator per value (0 or 1); its most
common value is the reference the others are compared with; values past the 30 most common share one
"(other)" indicator. Rows with a blank in any used column are left out, and counted.

The fitted model is plain data (`model`): it predicts what-if values here and writes itself as a SQL
expression for the grid's Add predicted column and Add miss column steps. Standard library only; no I/O.
"""

from __future__ import annotations

import math
from typing import Any

from swissarmygrid.domain.analysis import AnalysisError, literal, number_literal, quote, short_number
from swissarmygrid.domain.stats import f_p, normal_quantile, t_p

MAX_LEVELS = 30  # a category's most common values get their own indicator; the rest share "(other)"
MAX_TERMS = 80
MAX_ITERATIONS = 30
OTHER = "(other)"
Z95 = 1.959963984540054


# ===== linear algebra (small, dense, symmetric positive definite) =====


def cholesky(a: list[list[float]]) -> list[list[float]]:
    """Lower-triangular L with L L' = a. Raises AnalysisError when a is not positive definite (a
    predictor is a copy or a sum of others, or has one value only)."""
    n = len(a)
    low = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            total = a[i][j] - sum(low[i][k] * low[j][k] for k in range(j))
            if i == j:
                if total <= 1e-10 * max(1.0, abs(a[i][i])):
                    raise AnalysisError(
                        "Two or more predictors say the same thing (one is a copy, a total or the opposite "
                        "of others), or one has a single value: leave one out."
                    )
                low[i][i] = math.sqrt(total)
            else:
                low[i][j] = total / low[j][j]
    return low


def solve(low: list[list[float]], b: list[float]) -> list[float]:
    """x with (L L') x = b."""
    n = len(b)
    y = [0.0] * n
    for i in range(n):
        y[i] = (b[i] - sum(low[i][k] * y[k] for k in range(i))) / low[i][i]
    x = [0.0] * n
    for i in reversed(range(n)):
        x[i] = (y[i] - sum(low[k][i] * x[k] for k in range(i + 1, n))) / low[i][i]
    return x


def inverse(low: list[list[float]]) -> list[list[float]]:
    """(L L')^-1, a column at a time."""
    n = len(low)
    columns = [solve(low, [1.0 if r == c else 0.0 for r in range(n)]) for c in range(n)]
    return [[columns[c][r] for c in range(n)] for r in range(n)]


def t_quantile(p: float, df: float) -> float:
    """The t with P(T <= t) = p, by bisection on t_p (accurate to 1e-10)."""
    if df > 1e6:
        return normal_quantile(p)
    target = 2 * (1 - p) if p > 0.5 else 2 * p
    low, high = 0.0, 1.0
    while t_p(high, df) > target:
        high *= 2
    for _ in range(200):
        mid = (low + high) / 2
        low, high = (mid, high) if t_p(mid, df) > target else (low, mid)
    return (low + high) / 2 if p > 0.5 else -(low + high) / 2


# ===== the terms: each predictor as SQL =====


def check_request(kind: str, target: str, predictors: list[str]) -> None:
    if kind not in ("linear", "logistic"):
        raise AnalysisError("A regression is linear (a number target) or logistic (a yes/no outcome).")
    if not predictors:
        raise AnalysisError("Pick at least one predictor.")
    if target in predictors:
        raise AnalysisError(f"{target} is the target: it cannot predict itself.")
    if len(set(predictors)) != len(predictors):
        raise AnalysisError("A predictor is listed twice.")


def levels_query(relation: str, column: str) -> str:
    """A category's values with their counts, most common first (one more than the indicators kept)."""
    c = quote(column)
    return (
        f"SELECT CAST({c} AS VARCHAR), count(*) FROM {relation} WHERE {c} IS NOT NULL "
        f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {MAX_LEVELS + 2}"
    )


def count_query(relation: str) -> str:
    return f"SELECT count(*) FROM {relation}"


def rate_query(relation: str, y: str, where: str) -> str:
    """The share of complete rows whose outcome is yes."""
    return f"SELECT avg({y}) FROM {relation} WHERE {where}"


def means_query(relation: str, columns: list[str], where: str) -> str:
    parts = ", ".join(
        f"avg(CAST({quote(c)} AS DOUBLE)), stddev_samp(CAST({quote(c)} AS DOUBLE))" for c in columns
    )
    return f"SELECT count(*){', ' + parts if parts else ''} FROM {relation} WHERE {where}"


def build_terms(
    numbers: dict[str, float], categories: dict[str, list[str]], order: list[str]
) -> list[dict[str, Any]]:
    """The model's terms in predictor order: a number centred on its mean; a category's values past its
    reference (the first, the most common) as indicators, "(other)" for the rest when there are more."""
    terms: list[dict[str, Any]] = []
    for column in order:
        if column in numbers:
            terms.append({"column": column, "kind": "number", "mean": numbers[column]})
        else:
            values = categories[column]
            kept = values[: MAX_LEVELS + 1]
            for level in kept[1:]:
                terms.append({"column": column, "kind": "level", "level": level})
            if len(values) > MAX_LEVELS + 1:
                terms.append({"column": column, "kind": "other", "kept": kept})
    if len(terms) > MAX_TERMS:
        raise AnalysisError(
            f"{len(terms)} terms is too many for one model: fewer predictors, or group a category first."
        )
    return terms


def term_sql(term: dict[str, Any]) -> str:
    """The term's value on a row (NULL when the row's value is blank)."""
    c = quote(term["column"])
    if term["kind"] == "number":
        return f"(CAST({c} AS DOUBLE) - {number_literal(term['mean'])})"
    text = f"CAST({c} AS VARCHAR)"
    if term["kind"] == "level":
        return (
            f"(CASE WHEN {c} IS NULL THEN NULL WHEN {text} = {literal(term['level'])} THEN 1.0 ELSE 0.0 END)"
        )
    kept = ", ".join(literal(v) for v in term["kept"])
    return f"(CASE WHEN {c} IS NULL THEN NULL WHEN {text} IN ({kept}) THEN 0.0 ELSE 1.0 END)"


def term_name(term: dict[str, Any]) -> str:
    if term["kind"] == "number":
        return str(term["column"])
    if term["kind"] == "level":
        return f"{term['column']} = {term['level']}"
    return f"{term['column']} = {OTHER}"


def complete_rows(columns: list[str], numbers: frozenset[str] = frozenset()) -> str:
    """Rows with a value in every used column. A number must also be finite: pandas writes a missing
    number as NaN, which SQL does not count as blank."""
    parts = [f"{quote(c)} IS NOT NULL" for c in columns]
    parts += [f"isfinite(CAST({quote(c)} AS DOUBLE))" for c in columns if c in numbers]
    return " AND ".join(parts) or "TRUE"


def sums_query(relation: str, xs: list[str], y: str, where: str, weight: str | None = None) -> str:
    """One pass: n, then the upper triangle of X'WX, then X'Wy, then y'Wy (W is 1 for linear). xs[0] is
    the intercept (1.0)."""
    w = f"({weight}) * " if weight else ""
    parts = ["count(*)"]
    for i, xi in enumerate(xs):
        for xj in xs[i:]:
            parts.append(f"sum({w}{xi} * {xj})")
    parts += [f"sum({w}{xi} * {y})" for xi in xs]
    parts.append(f"sum({w}{y} * {y})")
    return f"SELECT {', '.join(parts)} FROM {relation} WHERE {where}"


def unpack(row: tuple[Any, ...], p: int) -> tuple[int, list[list[float]], list[float], float]:
    n = int(row[0])
    at = 1
    xtx = [[0.0] * p for _ in range(p)]
    for i in range(p):
        for j in range(i, p):
            xtx[i][j] = xtx[j][i] = float(row[at] or 0.0)
            at += 1
    xty = [float(row[at + i] or 0.0) for i in range(p)]
    yty = float(row[at + p] or 0.0)
    return n, xtx, xty, yty


# ===== fitting =====


def linear(row: tuple[Any, ...], terms: list[dict[str, Any]], target: str) -> dict[str, Any]:
    """Least squares from the sums. Returns the result, with `model` for predicting."""
    p = len(terms) + 1
    n, xtx, xty, yty = unpack(row, p)
    if n <= p:
        raise AnalysisError(f"{n} complete rows for {p} coefficients: more rows, or fewer predictors.")
    low = cholesky(xtx)
    b = solve(low, xty)
    inv = inverse(low)
    rss = max(0.0, yty - sum(bi * yi for bi, yi in zip(b, xty, strict=True)))
    mean_y = xty[0] / n
    tss = max(0.0, yty - n * mean_y * mean_y)
    df = n - p
    sigma2 = rss / df
    t_crit = t_quantile(0.975, df)
    r2 = 1 - rss / tss if tss > 0 else 0.0
    adj = 1 - (1 - r2) * (n - 1) / df if df > 0 else r2
    f = ((tss - rss) / (p - 1)) / sigma2 if sigma2 > 0 and p > 1 else math.inf
    rows = []
    for i, term in enumerate([{"column": "(intercept)", "kind": "intercept"}, *terms]):
        se = math.sqrt(max(0.0, sigma2 * inv[i][i]))
        t = b[i] / se if se > 0 else math.inf
        rows.append(
            {
                "term": "(intercept)" if i == 0 else term_name(term),
                "column": term["column"],
                "kind": term["kind"],
                "coefficient": b[i],
                "se": se,
                "t": t,
                "p": t_p(t, df) if math.isfinite(t) else 0.0,
                "low": b[i] - t_crit * se,
                "high": b[i] + t_crit * se,
            }
        )
    model = {
        "kind": "linear",
        "target": target,
        "terms": terms,
        "coefficients": b,
        "inverse": inv,
        "sigma2": sigma2,
        "df": df,
    }
    return {
        "kind": "linear",
        "n": n,
        "coefficients": rows,
        "fit": {
            "r2": r2,
            "adj_r2": adj,
            "rmse": math.sqrt(rss / n),
            "f": f,
            "f_p": f_p(f, p - 1, df) if math.isfinite(f) else 0.0,
            "mean": mean_y,
        },
        "model": model,
    }


def eta_sql(terms: list[dict[str, Any]], b: list[float]) -> str:
    """b0 + b1 x1 + ...: the model's linear part on a row, as SQL."""
    parts = [number_literal(b[0])]
    parts += [f"{number_literal(bi)} * {term_sql(term)}" for bi, term in zip(b[1:], terms, strict=True)]
    return " + ".join(parts)


def logistic_start(rate: float, p: int) -> list[float]:
    """The intercept at the overall log-odds, every other coefficient 0."""
    rate = min(max(rate, 1e-6), 1 - 1e-6)
    return [math.log(rate / (1 - rate))] + [0.0] * (p - 1)


def logistic_pass(terms: list[dict[str, Any]], b: list[float], y: str) -> tuple[str, str]:
    """The weight and working response of one reweighting round, as SQL over the current b."""
    eta = f"least(greatest({eta_sql(terms, b)}, -30.0), 30.0)"
    prob = f"(1.0 / (1.0 + exp(-({eta}))))"
    weight = f"greatest({prob} * (1.0 - {prob}), 1e-10)"
    z = f"(({eta}) + (({y}) - {prob}) / {weight})"
    return weight, z


def logistic_step(row: tuple[Any, ...], p: int) -> tuple[list[float], list[list[float]], int]:
    n, xtwx, xtwz, _ = unpack(row, p)
    low = cholesky(xtwx)
    return solve(low, xtwz), inverse(low), n


def logistic(
    b: list[float],
    inv: list[list[float]],
    n: int,
    terms: list[dict[str, Any]],
    target: str,
    outcome: str,
    fit: dict[str, Any],
    iterations: int,
) -> dict[str, Any]:
    """The converged logistic regression: coefficients as odds ratios with Wald ranges."""
    rows = []
    for i, term in enumerate([{"column": "(intercept)", "kind": "intercept"}, *terms]):
        se = math.sqrt(max(0.0, inv[i][i]))
        z = b[i] / se if se > 0 else math.inf
        rows.append(
            {
                "term": "(intercept)" if i == 0 else term_name(term),
                "column": term["column"],
                "kind": term["kind"],
                "coefficient": b[i],
                "se": se,
                "z": z,
                "p": math.erfc(abs(z) / math.sqrt(2)) if math.isfinite(z) else 0.0,
                "odds_ratio": math.exp(b[i]) if b[i] < 700 else math.inf,
                "low": math.exp(b[i] - Z95 * se) if b[i] - Z95 * se < 700 else math.inf,
                "high": math.exp(b[i] + Z95 * se) if b[i] + Z95 * se < 700 else math.inf,
            }
        )
    warnings = []
    if any(abs(r["coefficient"]) > 15 for r in rows[1:]):
        warnings.append(
            "A predictor nearly decides the outcome on its own (perfect separation): "
            "its odds ratio is not trustworthy."
        )
    if iterations >= MAX_ITERATIONS:
        warnings.append("The fit did not settle within its rounds: treat the numbers as rough.")
    model = {
        "kind": "logistic",
        "target": target,
        "outcome": outcome,
        "terms": terms,
        "coefficients": b,
        "inverse": inv,
    }
    return {
        "kind": "logistic",
        "n": n,
        "coefficients": rows,
        "fit": fit,
        "warnings": warnings,
        "model": model,
        "iterations": iterations,
    }


def log_likelihood_query(
    relation: str, terms: list[dict[str, Any]], b: list[float], y: str, where: str
) -> str:
    """The fitted model's log-likelihood, the intercept-only one's, and how many rows it gets right."""
    eta = f"least(greatest({eta_sql(terms, b)}, -30.0), 30.0)"
    prob = f"(1.0 / (1.0 + exp(-({eta}))))"
    return (
        f"SELECT sum(({y}) * ln({prob}) + (1 - ({y})) * ln(1 - {prob})), avg({y}), "
        f"avg(CASE WHEN ({prob} >= 0.5) = (({y}) = 1) THEN 1.0 ELSE 0.0 END), count(*) "
        f"FROM {relation} WHERE {where}"
    )


def logistic_fit(ll: float, rate: float, accuracy: float, n: int) -> dict[str, Any]:
    rate_c = min(max(rate, 1e-12), 1 - 1e-12)
    ll0 = n * (rate_c * math.log(rate_c) + (1 - rate_c) * math.log(1 - rate_c))
    return {
        "pseudo_r2": 1 - ll / ll0 if ll0 else 0.0,
        "deviance": -2 * ll,
        "null_deviance": -2 * ll0,
        "accuracy": accuracy,
        "base_rate": rate,
        "majority": max(rate, 1 - rate),
    }


# ===== predicting, and the model as SQL =====


def predict(model: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """The model's estimate for one what-if row, with its 95% range: for linear, where a new row would
    fall (the prediction interval); for logistic, the probability and the range of that probability."""
    terms = model["terms"]
    x = [1.0]
    for term in terms:
        value = values.get(term["column"])
        if value is None or value == "":
            raise AnalysisError(f"Give a value for {term['column']}.")
        if term["kind"] == "number":
            try:
                x.append(float(value) - float(term["mean"]))
            except (TypeError, ValueError) as error:
                raise AnalysisError(f"{term['column']} is a number.") from error
        elif term["kind"] == "level":
            x.append(1.0 if str(value) == term["level"] else 0.0)
        else:
            x.append(0.0 if str(value) in term["kept"] else 1.0)
    b = model["coefficients"]
    inv = model["inverse"]
    estimate = sum(bi * xi for bi, xi in zip(b, x, strict=True))
    spread = sum(x[i] * inv[i][j] * x[j] for i in range(len(x)) for j in range(len(x)))
    if model["kind"] == "linear":
        se = math.sqrt(max(0.0, model["sigma2"] * (1 + spread)))
        t = t_quantile(0.975, model["df"])
        return {"estimate": estimate, "low": estimate - t * se, "high": estimate + t * se}
    se = math.sqrt(max(0.0, spread))

    def prob(v: float) -> float:
        return 1 / (1 + math.exp(-max(-30.0, min(30.0, v))))

    return {"estimate": prob(estimate), "low": prob(estimate - Z95 * se), "high": prob(estimate + Z95 * se)}


def model_sql(model: dict[str, Any]) -> str:
    """The prediction on a row as SQL: the linear part, through the logistic curve for a yes/no model."""
    eta = eta_sql(model["terms"], model["coefficients"])
    if model["kind"] == "logistic":
        return f"1.0 / (1.0 + exp(-least(greatest({eta}, -30.0), 30.0)))"
    return eta


def column_step(model: dict[str, Any], name: str, miss: bool) -> dict[str, Any]:
    """Add predicted column / Add miss column: a SQL step adding the model's prediction (or the target
    minus it) as a new column, written with the fitted numbers, keeping the grid's order."""
    expression = model_sql(model)
    if miss:
        if model["kind"] == "logistic":
            raise AnalysisError(
                "A miss column is for a number target; a yes/no model's misses are its wrong calls."
            )
        expression = f"CAST({quote(model['target'])} AS DOUBLE) - ({expression})"
    return {"kind": "sql", "params": {"sql": f"SELECT *, {expression} AS {quote(name)}\nFROM grid"}}


def effect_words(row: dict[str, Any], kind: str, target: str, outcome: str | None) -> str:
    """A coefficient in a sentence, for the note."""
    if row["kind"] == "intercept":
        return ""
    if kind == "logistic":
        ratio = row["odds_ratio"]
        if row["kind"] == "number":
            return f"each extra 1 of {row['column']}: {short_number(ratio)} times the odds of {outcome}"
        return f"{row['term']}: {short_number(ratio)} times the odds of {outcome}, against the reference"
    sign = "+" if row["coefficient"] >= 0 else "-"
    amount = short_number(abs(row["coefficient"]))
    if row["kind"] == "number":
        return f"each extra 1 of {row['column']}: {sign}{amount} {target}"
    return f"{row['term']}: {sign}{amount} {target}, against the reference"
