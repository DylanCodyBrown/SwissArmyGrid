"""Two quick analyses on a grid's view (the owner's: "a chi-square goodness of fit I can quickly use on a
set of columns, and a quick outlier analysis to find the liars within a column, by frequency or amount").

**The fit test** (Pearson's chi-square goodness of fit): does a column's spread of values match an
expected one? Expected is even (every value as likely), Benford's law on the first digit or the first
two digits of amounts (made-up numbers rarely follow it), another table's spread of the same values,
or shares the caller gives. "Together" tests two columns for independence instead. With many rows any
difference is "significant", so each result also gives an effect size (Cohen's w, or Cramér's V) in
words, and a Benford test gives Nigrini's conformity (the mean absolute deviation of the shares).

**Outliers** by amount: values far from the rest, judged by the median and the median absolute
deviation (robust: a few wild values cannot hide themselves by dragging the mean), by Tukey's
quartile fences, or by the mean and standard deviation; optionally within each group of another
column. By frequency: values that appear far more often or far more rarely than values usually do,
judged robustly on the logarithm of the counts (a typo appears once among values seen hundreds of
times; a copied amount appears forty times among values seen once).

The math is here, and the query text (DuckDB computes the counts and the statistics). Standard
library only; no I/O.
"""

from __future__ import annotations

import math
from typing import Any

from swissarmygrid.domain.stats import chi_square_p, effect_words

__all__ = ["chi_square_p", "effect_words"]  # also here: the fit test's own words

EXPECTED_KINDS: tuple[str, ...] = ("even", "benford", "benford2", "grid", "shares", "together")
METHODS: tuple[str, ...] = ("robust", "iqr", "zscore")
LOOKS: tuple[str, ...] = ("amount", "frequency")
DEFAULT_THRESHOLD: dict[str, float] = {"robust": 3.5, "iqr": 1.5, "zscore": 3.0}
MAX_CATEGORIES = 200  # a fit test needs categories; more distinct values than this: bucket first
MAX_TOGETHER_CELLS = 2_500
MIN_GROUP = 5  # fewer values than this in a group: not judged
MIN_EXPECTED = 5.0
TOP = 50  # outliers listed (all are counted)
MAD_TO_SD = 0.6745  # the normal distribution's median absolute deviation, in standard deviations
MEAN_AD_TO_SD = 1.253314  # sqrt(pi / 2): the mean absolute deviation's, when the median one is 0

# Nigrini's conformity ranges for the mean absolute deviation of digit shares (Benford's Law, 2012).
NIGRINI: dict[int, tuple[tuple[float, str], ...]] = {
    1: ((0.006, "close conformity"), (0.012, "acceptable conformity"), (0.015, "marginal conformity")),
    2: ((0.0012, "close conformity"), (0.0018, "acceptable conformity"), (0.0022, "marginal conformity")),
}


class AnalysisError(ValueError):
    """An analysis that cannot be run as asked. The message says why, and what to do instead."""


# ===== Benford's law =====


def benford_shares(digits: int = 1) -> dict[str, float]:
    """Benford's law: the share of numbers whose first one (or two) significant digits are d is
    log10(1 + 1/d)."""
    low, high = (1, 9) if digits == 1 else (10, 99)
    return {str(d): math.log10(1.0 + 1.0 / d) for d in range(low, high + 1)}


def p_text(p: float) -> str:
    return "p < 0.0001" if p < 0.0001 else f"p = {p:.4f}"


def _verdict(p: float, effect: str, what: str) -> str:
    if p < 0.05:
        chance = "1 time in 1,000" if p < 0.001 else "1 time in 20"
        return (
            f"Doesn't {what}: a difference this big comes by chance less than {chance} ({p_text(p)}). "
            f"The difference is {effect}."
        )
    return f"{what.capitalize()}s: the differences are within what chance gives ({p_text(p)})."


# ===== the fit test =====


def fit(
    observed: list[tuple[str, int]],
    expected: dict[str, float] | None = None,
    benford: int = 0,
) -> dict[str, Any]:
    """Pearson's chi-square goodness of fit. `observed`: (value, count). `expected`: each value's
    share (normalized here); None means even over the observed values. A value observed but not
    expected is left out and named; a value expected but not observed counts as 0."""
    counts = {label: int(n) for label, n in observed if n}
    if expected is None:
        expected = dict.fromkeys(counts, 1.0)
    shares = {k: float(v) for k, v in expected.items() if v and v > 0}
    total_share = sum(shares.values())
    if not shares or total_share <= 0:
        raise AnalysisError("No expected shares to test against.")
    unexpected = sorted(k for k in counts if k not in shares)
    n = sum(v for k, v in counts.items() if k in shares)
    if n == 0:
        raise AnalysisError("No values to test: the column is blank, or none of its values are expected.")
    if len(shares) < 2:
        raise AnalysisError("A fit test needs at least two different values.")
    rows: list[dict[str, Any]] = []
    statistic = 0.0
    small = 0
    for label, share in shares.items():
        p = share / total_share
        e = n * p
        o = counts.get(label, 0)
        contribution = (o - e) ** 2 / e
        statistic += contribution
        small += e < MIN_EXPECTED
        rows.append(
            {
                "value": label,
                "observed": o,
                "expected": round(e, 4),
                "observed_share": o / n,
                "expected_share": p,
                "residual": round((o - e) / math.sqrt(e), 4),
                "contribution": round(contribution, 4),
            }
        )
    df = len(shares) - 1
    p_value = chi_square_p(statistic, df)
    w = math.sqrt(statistic / n)
    effect = effect_words(w)
    result: dict[str, Any] = {
        "statistic": statistic,
        "df": df,
        "p": p_value,
        "n": n,
        "effect_size": w,
        "effect_name": "Cohen's w",
        "effect": effect,
        "verdict": _verdict(p_value, effect, "fit"),
        "categories": rows if benford else sorted(rows, key=lambda r: (-r["observed"], str(r["value"]))),
        "warnings": [],
        "left_out": unexpected[:20],
        "left_out_count": len(unexpected),
    }
    if small > 0.2 * len(shares) or any(r["expected"] < 1 for r in rows):
        result["warnings"].append(
            f"{small} of {len(shares)} expected counts are under {MIN_EXPECTED:g}: the p-value is rough. "
            "Combine or bucket values, or test more rows."
        )
    if unexpected:
        result["warnings"].append(
            f"{len(unexpected)} value(s) are not in the expected spread and were left out "
            f"({', '.join(map(str, unexpected[:5]))}{'...' if len(unexpected) > 5 else ''})."
        )
    if benford:
        mad = sum(abs(r["observed_share"] - r["expected_share"]) for r in rows) / len(rows)
        result["conformity_mad"] = mad
        result["conformity"] = next(
            (words for limit, words in NIGRINI[benford] if mad <= limit), "nonconformity"
        )
        if n < 100:
            result["warnings"].append("Benford's law needs many numbers; under 100 says little.")
    return result


def together(cells: list[tuple[str, str, int]]) -> dict[str, Any]:
    """Pearson's chi-square test of independence between two columns, from (a, b, count) cells."""
    table: dict[tuple[str, str], int] = {}
    rows: dict[str, int] = {}
    cols: dict[str, int] = {}
    for a, b, count in cells:
        if not count:
            continue
        table[(a, b)] = table.get((a, b), 0) + int(count)
        rows[a] = rows.get(a, 0) + int(count)
        cols[b] = cols.get(b, 0) + int(count)
    n = sum(rows.values())
    if len(rows) < 2 or len(cols) < 2:
        raise AnalysisError("Each column needs at least two different values to test them together.")
    if len(rows) * len(cols) > MAX_TOGETHER_CELLS:
        raise AnalysisError(
            f"{len(rows)} x {len(cols)} combinations is too many to judge; bucket one column first."
        )
    statistic = 0.0
    small = 0
    out: list[dict[str, Any]] = []
    for a, ra in rows.items():
        for b, cb in cols.items():
            e = ra * cb / n
            o = table.get((a, b), 0)
            contribution = (o - e) ** 2 / e
            statistic += contribution
            small += e < MIN_EXPECTED
            out.append(
                {
                    "a": a,
                    "b": b,
                    "observed": o,
                    "expected": round(e, 4),
                    "residual": round((o - e) / math.sqrt(e), 4),
                    "contribution": round(contribution, 4),
                }
            )
    df = (len(rows) - 1) * (len(cols) - 1)
    p_value = chi_square_p(statistic, df)
    v = math.sqrt(statistic / (n * (min(len(rows), len(cols)) - 1)))
    effect = effect_words(v)
    out.sort(key=lambda c: -c["contribution"])
    warnings = []
    if small > 0.2 * len(out):
        warnings.append(
            f"{small} of {len(out)} expected counts are under {MIN_EXPECTED:g}: the p-value is rough."
        )
    verdict = (
        f"Related: values of one go with values of the other more than chance allows ({p_text(p_value)}). "
        f"The link is {effect}."
        if p_value < 0.05
        else f"Independent as far as these rows show ({p_text(p_value)})."
    )
    return {
        "statistic": statistic,
        "df": df,
        "p": p_value,
        "n": n,
        "effect_size": v,
        "effect_name": "Cramér's V",
        "effect": effect,
        "verdict": verdict,
        "cells": out[:TOP],
        "first_values": len(rows),
        "second_values": len(cols),
        "warnings": warnings,
    }


# ===== query text (DuckDB) =====


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def counts_query(relation: str, column: str) -> str:
    """Each value's count (as text, so any type works), blanks left out, most common first."""
    c = quote(column)
    return (
        f"SELECT CAST({c} AS VARCHAR), count(*) FROM {relation} WHERE {c} IS NOT NULL "
        f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {MAX_CATEGORIES + 1}"
    )


def digits_query(relation: str, column: str, digits: int) -> str:
    """The first one or two significant digits of each non-zero number (printf's %e puts the first
    significant digit first, rounded correctly: 9.9999999999999999 is 1.0e+01)."""
    c = f"abs(CAST({quote(column)} AS DOUBLE))"
    text = f"replace(printf('%.14e', {c}), '.', '')"
    return (
        f"SELECT left({text}, {digits}), count(*) FROM {relation} "
        f"WHERE {quote(column)} IS NOT NULL AND {c} > 0 AND isfinite({c}) GROUP BY 1"
    )


def together_query(relation: str, first: str, second: str) -> str:
    a, b = quote(first), quote(second)
    return (
        f"SELECT CAST({a} AS VARCHAR), CAST({b} AS VARCHAR), count(*) FROM {relation} "
        f"WHERE {a} IS NOT NULL AND {b} IS NOT NULL GROUP BY 1, 2 LIMIT {MAX_TOGETHER_CELLS + 1}"
    )


def check_outlier_options(look: str, method: str, threshold: Any) -> float:
    if look not in LOOKS:
        raise AnalysisError(f"Look at {' or '.join(LOOKS)}, not {look!r}.")
    if method not in METHODS:
        raise AnalysisError(f"The method is one of {', '.join(METHODS)}, not {method!r}.")
    if threshold is None or threshold == "":
        return DEFAULT_THRESHOLD[method]
    try:
        value = float(threshold)
    except (TypeError, ValueError) as error:
        raise AnalysisError("The threshold is a number, such as 3.5.") from error
    if not math.isfinite(value) or value <= 0:
        raise AnalysisError("The threshold is a number above 0.")
    return value


def _fences(method: str, t: float) -> tuple[str, str, str]:
    """(low fence, high fence, score) as SQL over the per-group statistics s (n, m median,
    d median absolute deviation, ad mean absolute deviation, q1, q3, mu mean, sd) and the value
    v.x. A group of fewer than MIN_GROUP values, or with no spread at all, gets no fences."""
    if method == "iqr":
        spread = "(s.q3 - s.q1)"
        low, high = f"s.q1 - {t!r} * {spread}", f"s.q3 + {t!r} * {spread}"
        score = (
            f"CASE WHEN v.x > s.q3 THEN (v.x - s.q3) / nullif({spread}, 0) "
            f"WHEN v.x < s.q1 THEN (v.x - s.q1) / nullif({spread}, 0) ELSE 0 END"
        )
    else:
        center, scale = (
            ("s.mu", "nullif(s.sd, 0)")
            if method == "zscore"
            else (
                "s.m",
                f"CASE WHEN s.d > 0 THEN s.d / {MAD_TO_SD!r} WHEN s.ad > 0 THEN {MEAN_AD_TO_SD!r} * s.ad END",
            )
        )
        low, high = f"{center} - {t!r} * ({scale})", f"{center} + {t!r} * ({scale})"
        score = f"(v.x - {center}) / ({scale})"
    guard = f"CASE WHEN s.n >= {MIN_GROUP} THEN {{}} END"
    return guard.format(low), guard.format(high), score


def amount_queries(relation: str, column: str, by: str | None, method: str, t: float) -> tuple[str, str]:
    """(per-group summary, top outliers): the values of `column` outside their group's fences.
    Groups are compared as text, so any column can split them; no `by` is one group."""
    x = f"CAST({quote(column)} AS DOUBLE)"
    g = f"CAST({quote(by)} AS VARCHAR)" if by else "CAST(NULL AS VARCHAR)"
    low, high, score = _fences(method, t)
    base = (
        f"WITH v AS (SELECT {g} AS g, {x} AS x FROM {relation} "
        f"WHERE {quote(column)} IS NOT NULL AND isfinite({x})), "
        "s0 AS (SELECT g, count(*) AS n, median(x) AS m, mad(x) AS d, quantile_cont(x, 0.25) AS q1, "
        "quantile_cont(x, 0.75) AS q3, avg(x) AS mu, stddev_samp(x) AS sd FROM v GROUP BY g), "
        "s AS (SELECT s0.*, (SELECT avg(abs(i.x - s0.m)) FROM v AS i "
        "WHERE i.g IS NOT DISTINCT FROM s0.g) AS ad FROM s0), "
        f"f AS (SELECT s.*, {low} AS lo, {high} AS hi FROM s), "
        f"o AS (SELECT v.g, v.x, s.lo, s.hi, {score} AS score FROM v JOIN f AS s "
        "ON v.g IS NOT DISTINCT FROM s.g WHERE v.x < s.lo OR v.x > s.hi), "
        "k AS (SELECT g, count(*) FILTER (x < lo) AS low_n, count(*) FILTER (x > hi) AS high_n "
        "FROM o GROUP BY g) "
    )
    summary = (
        base + "SELECT f.g, f.n, f.lo, f.hi, f.m, f.mu, coalesce(k.low_n, 0) AS low_n, "
        "coalesce(k.high_n, 0) AS high_n FROM f LEFT JOIN k ON f.g IS NOT DISTINCT FROM k.g "
        "ORDER BY low_n + high_n DESC, f.g NULLS FIRST"
    )
    top = base + f"SELECT g, x, score FROM o ORDER BY abs(score) DESC, x LIMIT {TOP}"
    return summary, top


def frequency_queries(relation: str, column: str, t: float, limit: int = TOP) -> tuple[str, str]:
    """(summary, top): values whose count is far from the usual count. A value is flagged only when
    its count is unusual on two scales, and its score is the weaker of the two:

    - against the other values' counts, robustly on the logarithm (median and median absolute
      deviation): values naturally run from rare to popular, a few times apart;
    - against counting noise: Anscombe's transform 2 * sqrt(n + 3/8) makes chance one unit wide
      whether a value is usually seen once or a thousand times.

    So among values seen about 1,000 times, 990 is ordinary (noise) and 3 is not; among values seen
    500 to 2,000 times, 2 is not; among values seen once, 3 is ordinary and 10 is not. Summary:
    distinct values, the typical (median) count, flagged rarer, flagged commoner."""
    c = quote(column)
    base = (
        f"WITH c AS (SELECT CAST({c} AS VARCHAR) AS value, count(*) AS n FROM {relation} "
        f"WHERE {c} IS NOT NULL GROUP BY 1), "
        "l AS (SELECT value, n, ln(n) AS x FROM c), "
        "s0 AS (SELECT count(*) AS k, median(x) AS m, mad(x) AS d, median(n) AS typical FROM l), "
        "a AS (SELECT s0.*, (SELECT avg(abs(l.x - s0.m)) FROM l) AS ad FROM s0), "
        f"s AS (SELECT a.*, CASE WHEN a.d > 0 THEN a.d / {MAD_TO_SD!r} "
        f"WHEN a.ad > 0 THEN {MEAN_AD_TO_SD!r} * a.ad END AS scale FROM a), "
        "y AS (SELECT l.value, l.n, sign(l.x - s.m) AS direction, s.k, "
        "CASE WHEN s.scale IS NOT NULL THEN abs(l.x - s.m) / s.scale WHEN l.x = s.m THEN 0 "
        "ELSE 1e9 END AS spread_z, "
        "abs(2 * sqrt(l.n + 0.375) - 2 * sqrt(s.typical + 0.375)) AS noise_z FROM l, s), "
        f"z AS (SELECT value, n, CASE WHEN k >= {MIN_GROUP} THEN direction * least(spread_z, noise_z) "
        "END AS score FROM y) "
    )
    summary = (
        base + "SELECT s.k, s.typical, "
        f"(SELECT count(*) FROM z WHERE score < -{t!r}), "
        f"(SELECT count(*) FROM z WHERE score > {t!r}) FROM s"
    )
    top = base + (
        f"SELECT value, n, score FROM z WHERE abs(score) > {t!r} "
        f"ORDER BY abs(score) DESC, value LIMIT {limit}"
    )
    return summary, top


def number_literal(value: float) -> str:
    if not math.isfinite(value):
        raise AnalysisError("A fence is not a finite number.")
    return repr(float(value))


def _step(where: str, keep: bool) -> dict[str, Any]:
    """Keep the rows matching `where`, or (keep=False) every other row: blank values included, since
    `NOT` of an unknown is unknown and would drop them too."""
    sql = (
        f"SELECT * FROM grid\nWHERE {where}"
        if keep
        else f"SELECT * FROM grid\nWHERE NOT coalesce({where}, FALSE)"
    )
    return {"kind": "sql", "params": {"sql": sql}}


def frequency_step(column: str, values: list[str], keep: bool = True) -> dict[str, Any]:
    """Show (or exclude) these rows: the rows holding the flagged values (compared as text, as they
    were counted)."""
    if not values:
        raise AnalysisError("No outliers to show.")
    items = ", ".join(literal(v) for v in values)
    return _step(f"CAST({quote(column)} AS VARCHAR) IN ({items})", keep)


def amount_step(
    column: str, by: str | None, fences: list[tuple[str | None, float, float]], keep: bool = True
) -> dict[str, Any]:
    """Show these rows: a SQL step keeping the rows outside the fences found now, written as plain
    numbers so the step keeps the grid's order and says exactly what it keeps. One clause per group
    that has outliers."""
    c = quote(column)
    if not fences:
        raise AnalysisError("No outliers to show.")

    def outside(lo: float, hi: float) -> str:
        return f"({c} < {number_literal(lo)} OR {c} > {number_literal(hi)})"

    if not by:
        _, lo, hi = fences[0]
        where = outside(lo, hi)
    else:
        g = f"CAST({quote(by)} AS VARCHAR)"
        clauses = [
            f"({g} IS NULL AND {outside(lo, hi)})"
            if value is None
            else f"({g} = {literal(value)} AND {outside(lo, hi)})"
            for value, lo, hi in fences
        ]
        where = "(" + "\n   OR ".join(clauses) + ")"
    return _step(where, keep)


# ===== buckets: a number column as categories =====

MAX_BUCKETS = 20


def edges_query(relation: str, column: str, buckets: int) -> str:
    """The cut points that split a number column into `buckets` groups of about equal size."""
    if not 2 <= buckets <= MAX_BUCKETS:
        raise AnalysisError(f"Between 2 and {MAX_BUCKETS} buckets.")
    cuts = ", ".join(repr(i / buckets) for i in range(1, buckets))
    x = f"CAST({quote(column)} AS DOUBLE)"
    return f"SELECT quantile_disc({x}, [{cuts}]) FROM {relation} WHERE {x} IS NOT NULL AND isfinite({x})"


def short_number(value: float) -> str:
    """A cut point as people write it: 1,250 or 0.35 or 12.5, never 1.2500000001."""
    if value == int(value) and abs(value) < 1e15:
        return f"{int(value):,}"
    return f"{value:,.4g}" if abs(value) < 1e5 else f"{value:,.0f}"


def bucket_expression(column: str, edges: list[float]) -> str:
    """CASE over the cut points: "1: up to 25", "2: 25 to 60", "3: over 60". The number first keeps
    the buckets in order wherever they are sorted as text. Blank stays blank."""
    cuts = sorted({float(e) for e in edges if e is not None and math.isfinite(float(e))})
    c = f"CAST({quote(column)} AS DOUBLE)"
    if not cuts:
        return f"CASE WHEN {c} IS NULL THEN NULL ELSE '1: all' END"
    parts = [f"WHEN {c} IS NULL THEN NULL"]
    low: float | None = None
    for i, cut in enumerate(cuts, start=1):
        label = (
            f"{i}: up to {short_number(cut)}"
            if low is None
            else f"{i}: {short_number(low)} to {short_number(cut)}"
        )
        parts.append(f"WHEN {c} <= {number_literal(cut)} THEN {literal(label)}")
        low = cut
    parts.append(f"ELSE {literal(f'{len(cuts) + 1}: over {short_number(cuts[-1])}')}")
    return "CASE " + " ".join(parts) + " END"


def as_text(column: str) -> str:
    return f"CAST({quote(column)} AS VARCHAR)"


def together_cells_query(relation: str, first: str, second: str) -> str:
    """(first, second, count) over two SQL expressions, blanks on either side left out."""
    return (
        f"SELECT {first}, {second}, count(*) FROM {relation} "
        f"WHERE ({first}) IS NOT NULL AND ({second}) IS NOT NULL GROUP BY 1, 2 LIMIT {MAX_TOGETHER_CELLS + 1}"
    )


def blanks_query(relation: str, value_sql: str) -> str:
    return f"SELECT count(*) FILTER (WHERE ({value_sql}) IS NULL) FROM {relation}"


def sizes_query(relation: str, columns: list[str]) -> str:
    """For each column: rows with a value, and different values. One pass."""
    parts = ", ".join(f"count({quote(c)}), count(DISTINCT {quote(c)})" for c in columns)
    return f"SELECT {parts} FROM {relation}"


def correlation_query(relation: str, first: str, second: str) -> str:
    """Pearson's correlation of two number columns, and how many rows have both."""
    a, b = f"CAST({quote(first)} AS DOUBLE)", f"CAST({quote(second)} AS DOUBLE)"
    return f"SELECT corr({a}, {b}), count(*) FROM {relation} WHERE {a} IS NOT NULL AND {b} IS NOT NULL"


# ===== outcomes: what counts as "yes" =====

OUTCOME_OPS: dict[str, str] = {"gt": ">", "ge": ">=", "lt": "<", "le": "<=", "eq": "=", "ne": "<>"}


def outcome_condition(outcome: Any, number: bool) -> tuple[str, str]:
    """(SQL condition, the outcome in words) from {"column", "values": [...]} or, for a number
    column, {"column", "op": gt | ge | lt | le | eq | ne, "value": n}."""
    if not isinstance(outcome, dict) or not isinstance(outcome.get("column"), str):
        raise AnalysisError(
            'An outcome is {"column": name, "values": [...]}, or a number column with "op" and "value".'
        )
    column = outcome["column"]
    if outcome.get("op") is not None:
        op = OUTCOME_OPS.get(str(outcome["op"]))
        if op is None or not number:
            raise AnalysisError(
                "A condition (op and value) needs a number column; otherwise list the values."
            )
        try:
            value = float(outcome.get("value"))  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise AnalysisError("The outcome's value is a number.") from error
        return (
            f"CAST({quote(column)} AS DOUBLE) {op} {number_literal(value)}",
            f"{column} {op} {short_number(value)}",
        )
    values = outcome.get("values")
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list) or not values:
        raise AnalysisError("Name the outcome's values, such as late.")
    items = ", ".join(literal(str(v)) for v in values)
    # Named by its values ("late", "refund or chargeback"): they read as the outcome in a sentence.
    return f"{as_text(column)} IN ({items})", " or ".join(map(str, values))


def odds_query(relation: str, value_sql: str, outcome_sql: str | None) -> str:
    """(value, rows, rows with the outcome) per value; blanks left out. Most rows first."""
    yes = f"count(*) FILTER (WHERE coalesce({outcome_sql}, FALSE))" if outcome_sql else "NULL"
    return (
        f"SELECT {value_sql} AS value, count(*), {yes} FROM {relation} WHERE ({value_sql}) IS NOT NULL "
        f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {MAX_CATEGORIES + 1}"
    )


# ===== odds =====

SMALL = 30  # fewer rows than this: the rate and the odds are rough
Z95 = 1.959963984540054


def odds(rows: list[tuple[str, int, int | None]], outcome: str | None = None) -> dict[str, Any]:
    """How often each value shows up (its share, 1 in N, the odds of it on any one row), and with an
    outcome, how often the outcome comes with each value against every other value: the odds ratio
    with Woolf's 95% range (half a count added to each cell when one is empty, Haldane's way)."""
    total = sum(n for _, n, _ in rows)
    if not total:
        raise AnalysisError("No values to count: the column is blank.")
    out: list[dict[str, Any]] = []
    yes_total = sum(int(y or 0) for _, _, y in rows) if outcome else 0
    for value, n, yes in rows:
        share = n / total
        item: dict[str, Any] = {
            "value": value,
            "rows": n,
            "share": share,
            "one_in": total / n,
            "odds_against": (total - n) / n,
        }
        if outcome:
            a = int(yes or 0)
            b, c = n - a, yes_total - a
            d = (total - n) - c
            cells = (a, b, c, d)
            if 0 in cells:
                a_, b_, c_, d_ = (x + 0.5 for x in cells)
            else:
                a_, b_, c_, d_ = (float(x) for x in cells)
            ratio = (a_ * d_) / (b_ * c_)
            spread = Z95 * math.sqrt(1 / a_ + 1 / b_ + 1 / c_ + 1 / d_)
            low, high = ratio * math.exp(-spread), ratio * math.exp(spread)
            rest = total - n
            item.update(
                outcome_rows=a,
                rate=a / n,
                rest_rate=(c / rest) if rest else None,
                odds_ratio=ratio,
                low=low,
                high=high,
                clear=low > 1 or high < 1,
                small=n < SMALL,
            )
        out.append(item)
    result: dict[str, Any] = {"rows": total, "values": out, "outcome": outcome}
    if outcome:
        out.sort(key=lambda v: (-v["odds_ratio"], -v["rows"]))
        result["outcome_rows"] = yes_total
        result["rate"] = yes_total / total
        table = [(v["value"], "yes", v["outcome_rows"]) for v in out] + [
            (v["value"], "no", v["rows"] - v["outcome_rows"]) for v in out
        ]
        try:
            result["together"] = together(table)
        except AnalysisError as error:
            result["together"] = {"verdict": str(error)}
    return result


def odds_words(item: dict[str, Any], outcome: str) -> str:
    """One value's odds in a sentence: "West: late 41% of the time, against 9% elsewhere: 7.0 times
    the odds (5.8 to 8.4)"."""
    rest = item.get("rest_rate")
    elsewhere = f", against {rest:.0%} elsewhere" if rest is not None else ""
    return (
        f"{item['value']}: {outcome} {item['rate']:.0%} of the time{elsewhere}: "
        f"{item['odds_ratio']:.2g} times the odds ({item['low']:.2g} to {item['high']:.2g})"
    )


# ===== find predictors =====

STRONG_REDUNDANCY = 0.7  # two candidates this related say much the same thing
MAX_REDUNDANCY_CHECKS = 8  # the strongest candidates are checked against each other


def strength(v: float) -> str:
    """Cramér's V in words, for a predictor: how much knowing it tells about the target."""
    if v < 0.1:
        return "none"
    if v < 0.2:
        return "weak"
    if v < 0.3:
        return "moderate"
    return "strong"


def shape_note(v: float, r: float | None) -> str | None:
    """When the bucketed test and the straight-line correlation disagree, say so."""
    if r is None:
        return None
    if v >= 0.2 and abs(r) < 0.1:
        return "related, but not in a straight line: bucket it for a regression"
    return None


# ===== the analysis as Python, for the notebook =====

CODE_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "fit_test": ("columns", "expected", "shares"),
    "outliers": ("column", "look", "method", "threshold", "by"),
    "odds": ("column", "buckets", "outcome"),
    "predictors": ("target", "candidates", "buckets"),
}


def _name_part(text: Any) -> str:
    part = "".join(ch if ch.isalnum() else "_" for ch in str(text).lower()).strip("_")
    return part or "result"


def analysis_code(
    analysis: str,
    body: dict[str, Any],
    table: str,
    view_sql: str | None,
    reference: str | None = None,
) -> tuple[str, list[str]]:
    """(code, imports): the analysis as one `swissarmygrid.stats` call over the analyst's own table
    (`table`, the name it was pushed with), through the grid's steps as a DuckDB query when it has
    any. The call is the function the window ran, so the numbers match."""
    known = CODE_ARGUMENTS.get(analysis)
    if known is None:
        raise AnalysisError(f"No analysis {analysis!r}.")
    args = {k: body[k] for k in known if body.get(k) not in (None, "", [], {})}
    parts = [f"{k}={v!r}" for k, v in args.items()]
    if analysis == "fit_test" and args.get("expected") == "grid":
        given = body.get("reference")
        ref: dict[str, Any] = given if isinstance(given, dict) else {}
        if not reference:
            raise AnalysisError("Testing against another grid needs that grid.")
        parts.append(f"reference={reference}")
        if ref.get("column"):
            parts.append(f"reference_column={ref['column']!r}")
    if view_sql:
        quote_ = '"""' if '"""' not in view_sql else "'''"
        data = f"duckdb.sql({quote_}\n{view_sql}\n{quote_})"
        imports = ["import duckdb", "from swissarmygrid import stats"]
    else:
        data = table
        imports = ["from swissarmygrid import stats"]
    subject = args.get("column") or args.get("target") or (args.get("columns") or ["result"])[0]
    variable = f"{_name_part(table)}_{analysis}_{_name_part(subject)}"
    call = f"{variable} = stats.{analysis}(\n    {data},\n    " + ",\n    ".join(parts) + ",\n)"
    return f"{call}\n{variable}", imports
