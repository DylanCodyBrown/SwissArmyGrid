"""Time series and forecast (Plans/analyze-menu.plan.md, phase C).

**The series:** a metric totalled per period (sum, average or a count of rows; day, week, month,
quarter or year) from a date column, with the periods that have no rows found and named.

**Its shape** (time series): a trend (the centred moving average over one season), a seasonal index per
position in the season ("December runs 34% above a typical month"; multiplicative when every value is
above zero, additive otherwise), and what is left; **anomalies** (periods whose leftover is beyond robust
fences); **growth** (period over period, the same period a season earlier, a rolling average); **level
shifts** (where the deseasonalised series stepped and stayed); **memory** (how much one period's leftover
predicts the next: the lag-one autocorrelation).

**The forecast:** a regression of the value on a straight trend, a dummy per season position and any
predictors (each predictor's future: its last value, its own trend, or a value given), with 80% and 95%
ranges for a new period. The last few periods are first held back and forecast from the rest, so the
typical miss shown is earned, not fitted.

DuckDB totals the periods; the rest is a few hundred numbers, done here. Standard library only; no I/O.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from swissarmygrid.domain.analysis import MAD_TO_SD, AnalysisError, literal, quote, short_number
from swissarmygrid.domain.regression import cholesky, inverse, solve, t_quantile

PERIODS: tuple[str, ...] = ("day", "week", "month", "quarter", "year")
TOTALS: tuple[str, ...] = ("sum", "avg", "count")
SEASON: dict[str, int] = {"day": 7, "week": 52, "month": 12, "quarter": 4, "year": 1}
POSITION_NAMES: dict[str, list[str]] = {
    "day": ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"],
    "month": [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ],
    "quarter": ["Q1", "Q2", "Q3", "Q4"],
}
MAX_PERIODS = 5_000
MAX_SERIES = 6  # with a split, the most common values each get a line
ANOMALY = 3.5
ANOMALY_TS = (
    5.0  # a period's spreads from its neighbours: 1 false flag in 100 made-up series, a real spike scores 15+
)
SHIFT = 6.0  # a level shift's t: a step this many standard errors from none, on top of the trend
FUTURES: tuple[str, ...] = ("last", "trend", "value")


def check_request(period: str, total: str, horizon: int) -> None:
    if period not in PERIODS:
        raise AnalysisError(f"The period is one of {', '.join(PERIODS)}.")
    if total not in TOTALS:
        raise AnalysisError("Total by sum, avg (the average) or count (rows).")
    if not 1 <= horizon <= 120:
        raise AnalysisError("Forecast 1 to 120 periods ahead.")


def bucket_sql(column: str, period: str) -> str:
    """The period a row falls in, as a DATE (weeks start on Monday)."""
    return f"CAST(date_trunc({literal(period)}, CAST({quote(column)} AS TIMESTAMP)) AS DATE)"


def series_query(
    relation: str, date_column: str, period: str, total: str, metric: str | None, split: str | None
) -> str:
    """(period, [split value,] total, rows) per period, in order."""
    value = "count(*)" if total == "count" else f"{total}(CAST({quote(metric or '')} AS DOUBLE))"
    keep = f"{quote(date_column)} IS NOT NULL" + (
        f" AND isfinite(CAST({quote(metric)} AS DOUBLE))" if metric and total != "count" else ""
    )
    if split:
        g = f"CAST({quote(split)} AS VARCHAR)"
        return (
            f"WITH top AS (SELECT {g} AS s FROM {relation} WHERE {g} IS NOT NULL GROUP BY 1 "
            f"ORDER BY count(*) DESC LIMIT {MAX_SERIES}) "
            f"SELECT {bucket_sql(date_column, period)}, {g}, {value}, count(*) FROM {relation} "
            f"WHERE {keep} AND {g} IN (SELECT s FROM top) GROUP BY 1, 2 ORDER BY 2, 1"
        )
    return (
        f"SELECT {bucket_sql(date_column, period)}, NULL, {value}, count(*) FROM {relation} "
        f"WHERE {keep} GROUP BY 1 ORDER BY 1 LIMIT {MAX_PERIODS + 1}"
    )


def step_period(day: date, period: str, steps: int = 1) -> date:
    """The start of the period `steps` after the one starting on `day`."""
    if period == "day":
        return day + timedelta(days=steps)
    if period == "week":
        return day + timedelta(weeks=steps)
    months = {"month": 1, "quarter": 3, "year": 12}[period] * steps
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def complete(points: list[tuple[date, float]], period: str) -> tuple[list[date], list[float | None]]:
    """Every period from the first to the last, None where no row fell."""
    if not points:
        return [], []
    found = dict(points)
    periods = [points[0][0]]
    while periods[-1] < points[-1][0] and len(periods) <= MAX_PERIODS:
        periods.append(step_period(periods[-1], period))
    return periods, [found.get(p) for p in periods]


def position(day: date, period: str) -> int:
    """Where in its season a period sits: weekday, week of the year, month, quarter."""
    if period == "day":
        return day.weekday()
    if period == "week":
        return min(51, (day.timetuple().tm_yday - 1) // 7)
    if period == "month":
        return day.month - 1
    if period == "quarter":
        return (day.month - 1) // 3
    return 0


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _robust(values: list[float]) -> tuple[float, float]:
    """(median, spread): the median absolute deviation scaled to a standard deviation (never 0)."""
    m = _median(values)
    mad = _median([abs(v - m) for v in values]) / MAD_TO_SD
    if mad <= 0:
        mad = (sum(abs(v - m) for v in values) / len(values)) * 1.253314 or 1e-12
    return m, mad


def moving_average(values: list[float], m: int) -> list[float | None]:
    """The centred moving average over m (2 x m for an even m), None at the ends."""
    n = len(values)
    out: list[float | None] = [None] * n
    if m <= 1:
        return list(values)
    half = m // 2
    for i in range(half, n - half):
        if m % 2:
            window = values[i - half : i + half + 1]
            out[i] = sum(window) / m
        else:
            window = values[i - half : i + half + 1]
            out[i] = (0.5 * window[0] + sum(window[1:-1]) + 0.5 * window[-1]) / m
    return out


def decompose(periods: list[date], values: list[float], period: str) -> dict[str, Any]:
    """Trend, seasonal index, leftover, from a second pass in which the big spikes of a first one are
    replaced by what the first pass expected there (one wild month must not bend the trend around it
    or the season of its month)."""
    first = _decompose(periods, values, period)
    adjusted = _adjusted(values, first["season"], first["multiplicative"])
    spikes = big_spikes(periods, adjusted)
    if not spikes:
        return first
    cleaned = [
        (t * s if first["multiplicative"] else t + s) if p.isoformat() in spikes else v
        for p, v, t, s in zip(periods, values, first["trend"], first["season"], strict=True)
    ]
    second = _decompose(periods, cleaned, period)
    multiplicative = second["multiplicative"]
    second["left"] = [
        (v / (t * s) if multiplicative and t * s else v - t - (0.0 if multiplicative else s))
        for v, t, s in zip(values, second["trend"], second["season"], strict=True)
    ]
    return second


def _decompose(periods: list[date], values: list[float], period: str) -> dict[str, Any]:
    """One pass: the centred moving average, the median seasonal ratio (or difference) per position,
    and what is left."""
    n = len(values)
    m = SEASON[period]
    seasonal = m > 1 and n >= 2 * m
    multiplicative = all(v > 0 for v in values)
    trend = moving_average(values, m if seasonal else min(3, n))
    # The ends have no centred average: carry the nearest one out.
    first = next((t for t in trend if t is not None), _median(values))
    last = next((t for t in reversed(trend) if t is not None), first)
    filled_trend: list[float] = []
    for i, t in enumerate(trend):
        filled_trend.append(t if t is not None else (first if i < n / 2 else last))
    index: dict[int, float] = {}
    if seasonal:
        by: dict[int, list[float]] = {}
        for p, v, t in zip(periods, values, trend, strict=True):
            if t is None or t == 0:
                continue
            by.setdefault(position(p, period), []).append(v / t if multiplicative else v - t)
        # The median of each position: one spike in one June must not make every June look high.
        raw = {k: _median(vs) for k, vs in by.items()}
        centre = (sum(raw.values()) / len(raw)) if raw else (1.0 if multiplicative else 0.0)
        index = {k: (v / centre if multiplicative else v - centre) for k, v in raw.items()}
    neutral = 1.0 if multiplicative else 0.0
    season = [index.get(position(p, period), neutral) for p in periods]
    left = [
        (v / (t * s) if multiplicative and t * s else v - t - (s if not multiplicative else 0.0))
        for v, t, s in zip(values, filled_trend, season, strict=True)
    ]
    return {
        "seasonal": seasonal,
        "multiplicative": multiplicative,
        "trend": filled_trend,
        "season": season,
        "index": index,
        "left": left,
    }


def season_words(index: dict[int, float], period: str, multiplicative: bool, unit: str) -> list[str]:
    """The seasonal index in sentences, the biggest departures first."""
    names = POSITION_NAMES.get(period)
    out = []
    for k, v in sorted(index.items(), key=lambda kv: -abs((kv[1] - 1) if multiplicative else kv[1]))[:4]:
        name = names[k] if names and k < len(names) else f"{period} {k + 1}"
        if multiplicative:
            share = (v - 1) * 100
            out.append(
                f"{name} {'run' if names else 'runs'} {abs(share):.0f}% "
                f"{'above' if share >= 0 else 'below'} a typical {period}"
            )
        else:
            out.append(
                f"{name}: {'+' if v >= 0 else '-'}{short_number(abs(v))} {unit} against a typical {period}"
            )
    return out


def _adjusted(values: list[float], season: list[float], multiplicative: bool) -> list[float]:
    """The series with its season taken out: on the log for a multiplicative series (so a 20% spike
    counts the same at any level), as a difference otherwise."""
    if multiplicative:
        return [math.log(v / s) if v > 0 and s > 0 else 0.0 for v, s in zip(values, season, strict=True)]
    return [v - s for v, s in zip(values, season, strict=True)]


def big_spikes(periods: list[date], adjusted: list[float]) -> set[str]:
    """Periods far (5 robust spreads) from the median of the three either side: set aside before
    looking for steps in the level, so one spike cannot look like a step."""
    n = len(adjusted)
    off = [adjusted[i] - _median(adjusted[max(0, i - 3) : i] + adjusted[i + 1 : i + 4]) for i in range(n)]
    m, spread = _robust(off)
    return {p.isoformat() for p, x in zip(periods, off, strict=True) if abs(x - m) / spread > 5}


def _local_line(near: list[int], values: list[float], at: int) -> float:
    """The straight line through the neighbours, read at `at` (their mean when they are too few): it
    follows a trend to the ends of the series, where a median of one-sided neighbours lags behind."""
    if len(near) < 3:
        return sum(values[j] for j in near) / len(near) if near else values[at]
    mx = sum(near) / len(near)
    my = sum(values[j] for j in near) / len(near)
    sxx = sum((j - mx) ** 2 for j in near)
    slope = sum((j - mx) * (values[j] - my) for j in near) / sxx if sxx else 0.0
    return my + slope * (at - mx)


def anomalies(
    periods: list[date],
    values: list[float],
    season: list[float],
    multiplicative: bool,
    shifts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Periods their neighbours do not explain: the season taken out, each period is compared with the
    median of its eight nearest neighbours within the same stretch between steps in the level (a
    median follows any trend shape, one spike cannot move it, and a step is not blurred), and flagged
    beyond robust fences (ANOMALY_TS spreads) over all the periods."""
    n = len(values)
    if n < 8:
        return []
    adjusted = _adjusted(values, season, multiplicative)
    cuts = sorted(s["period"] for s in shifts or [])
    stretch = [sum(1 for c in cuts if p.isoformat() >= c) for p in periods]
    spikes = big_spikes(periods, adjusted)
    off = []
    for i in range(n):
        same = [
            j
            for j in range(n)
            if j != i and stretch[j] == stretch[i] and periods[j].isoformat() not in spikes
        ]
        near = sorted(same, key=lambda j: (abs(j - i), j))[:8]
        off.append(adjusted[i] - _local_line(near, adjusted, i))
    m, spread = _robust(off)
    out = []
    for p, v, x in zip(periods, values, off, strict=True):
        z = (x - m) / spread
        if abs(z) > ANOMALY_TS:
            out.append(
                {
                    "period": p.isoformat(),
                    "value": v,
                    "score": z,
                    "kind": "spike" if z > 0 else "dip",
                    # Show the math: score = (off - typical_off) / spread (season out; log if multiplicative).
                    "off": x,
                    "typical_off": m,
                    "spread": spread,
                }
            )
    return out


def level_shifts(
    periods: list[date], adjusted: list[float], spikes: set[str] | None = None
) -> list[dict[str, Any]]:
    """Where the deseasonalised series stepped and stayed: for each cut, a straight trend plus a step
    at the cut is fitted, and the cut whose step is surest (the largest t of the step) is kept when t
    passes SHIFT and each side has three periods; each side is then tried once more (at most three
    shifts). A steady trend is not a step, and a lone spike is not one either (spikes are set to their
    neighbours first). The levels reported are the series' own on each side of the cut."""
    spikes = spikes or set()
    clean = list(adjusted)
    for i, p in enumerate(periods):
        if p.isoformat() in spikes:
            near = [adjusted[j] for j in (i - 1, i + 1) if 0 <= j < len(adjusted)]
            clean[i] = sum(near) / len(near) if near else clean[i]
    found: list[dict[str, Any]] = []
    stride = max(1, len(clean) // 400)  # a long daily series: every few periods is close enough

    def split(lo: int, hi: int, depth: int) -> None:
        if depth > 2 or hi - lo < 10:
            return
        best: tuple[float, int] | None = None
        for cut in range(lo + 3, hi - 2, stride):
            # A curved trend (to the square) plus the step: a series that bends is not a step.
            span = max(1, hi - lo)
            x = [
                [1.0, (i - lo) / span, ((i - lo) / span) ** 2, 1.0 if i >= cut else 0.0]
                for i in range(lo, hi)
            ]
            try:
                b, inv, sigma2, _ = least_squares(x, clean[lo:hi])
            except AnalysisError:
                continue
            se = math.sqrt(max(sigma2 * inv[3][3], 1e-24))
            score = abs(b[3]) / se
            if best is None or score > best[0]:
                best = (score, cut)
        if best and best[0] >= SHIFT:
            score, cut = best
            before = clean[max(lo, cut - 3) : cut]
            after = clean[cut : min(hi, cut + 3)]
            found.append(
                {
                    "period": periods[cut].isoformat(),
                    "before": sum(before) / len(before),
                    "after": sum(after) / len(after),
                    "t": score,
                }
            )
            split(lo, cut, depth + 1)
            split(cut, hi, depth + 1)

    split(0, len(clean), 0)
    return sorted(found, key=lambda s: s["period"])


def autocorrelation(values: list[float], lag: int) -> float | None:
    n = len(values)
    if n <= lag + 2:
        return None
    mean = sum(values) / n
    total = sum((v - mean) ** 2 for v in values)
    if total == 0:
        return None
    return sum((values[i] - mean) * (values[i - lag] - mean) for i in range(lag, n)) / total


def memory_words(r: float | None) -> str:
    if r is None:
        return "too few periods to say"
    if r > 0.5:
        return "strong: a high period is usually followed by another"
    if r > 0.2:
        return "some: one period carries a little into the next"
    if r < -0.2:
        return "it swings: a high period tends to be followed by a low one"
    return "little: each period is nearly a fresh start"


def growth(periods: list[date], values: list[float], period: str) -> list[dict[str, Any]]:
    m = SEASON[period]
    window = m if m > 1 else 3
    out = []
    for i, (p, v) in enumerate(zip(periods, values, strict=True)):
        prev = values[i - 1] if i >= 1 else None
        year = values[i - m] if m > 1 and i >= m else None
        roll = sum(values[i - window + 1 : i + 1]) / window if i >= window - 1 else None
        out.append(
            {
                "period": p.isoformat(),
                "value": v,
                "change": (v - prev) / abs(prev) if prev else None,
                "season_ago": (v - year) / abs(year) if year else None,
                "rolling": roll,
            }
        )
    return out


def anomaly_step(date_column: str, period: str, starts: list[str]) -> dict[str, Any]:
    """Show these rows: the rows in the anomalous periods (plain WHERE: keeps the grid's order)."""
    if not starts:
        raise AnalysisError("No anomalous periods to show.")
    days = ", ".join(f"DATE {literal(s)}" for s in starts)
    return {
        "kind": "sql",
        "params": {"sql": f"SELECT * FROM grid\nWHERE {bucket_sql(date_column, period)} IN ({days})"},
    }


# ===== the forecast =====


def design(
    periods: list[date],
    period: str,
    seasonal: bool,
    predictors: list[list[float]],
    steps: list[str] | None = None,
) -> list[list[float]]:
    """A row per period: 1, the trend (periods since the first), a column per step in the level (0
    before it, 1 from it on), a dummy per season position past the first, then the predictors."""
    m = SEASON[period]
    rows = []
    for i, p in enumerate(periods):
        row = [1.0, float(i), *(1.0 if p.isoformat() >= s else 0.0 for s in steps or [])]
        if seasonal:
            pos = position(p, period)
            row += [1.0 if pos == k else 0.0 for k in range(1, m)]
        row += [col[i] for col in predictors]
        rows.append(row)
    return rows


def least_squares(x: list[list[float]], y: list[float]) -> tuple[list[float], list[list[float]], float, int]:
    """(coefficients, (X'X)^-1, residual variance, degrees of freedom) for a small dense problem."""
    k = len(x[0])
    xtx = [[sum(r[i] * r[j] for r in x) for j in range(k)] for i in range(k)]
    xty = [sum(r[i] * v for r, v in zip(x, y, strict=True)) for i in range(k)]
    low = cholesky(xtx)
    b = solve(low, xty)
    inv = inverse(low)
    residual = [v - sum(bi * ri for bi, ri in zip(b, r, strict=True)) for r, v in zip(x, y, strict=True)]
    df = len(y) - k
    if df < 1:
        raise AnalysisError("Too few periods for this forecast: fewer predictors, or a longer history.")
    return b, inv, sum(e * e for e in residual) / df, df


def future_predictor(history: list[float], how: dict[str, Any], steps: int) -> list[float]:
    """A predictor's values in the forecast periods: its last value, its own straight trend continued,
    or a value given."""
    kind = how.get("future", "last")
    if kind not in FUTURES:
        raise AnalysisError("A predictor's future is last, trend or value.")
    if kind == "value":
        try:
            value = float(how.get("value"))  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise AnalysisError("Give the predictor's future value as a number.") from error
        return [value] * steps
    if kind == "last" or len(history) < 3:
        return [history[-1]] * steps
    n = len(history)
    mean_x = (n - 1) / 2
    mean_y = sum(history) / n
    slope = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(history)) / sum(
        (i - mean_x) ** 2 for i in range(n)
    )
    return [mean_y + slope * (n - 1 + s - mean_x) for s in range(1, steps + 1)]


def fit_forecast(
    periods: list[date],
    values: list[float],
    period: str,
    horizon: int,
    predictors: list[tuple[str, list[float], dict[str, Any]]],
    steps: list[str] | None = None,
) -> dict[str, Any]:
    """The forecast: history with the fitted line, `horizon` periods ahead with 80% and 95% ranges,
    and the held-back check (the last periods forecast from the rest). `steps`: periods where the
    level stepped (from the time series), carried into the future at their new level."""
    n = len(values)
    m = SEASON[period]
    seasonal = m > 1 and n >= 2 * m
    steps = steps or []
    k = 2 + len(steps) + (m - 1 if seasonal else 0) + len(predictors)
    if n < k + 3:
        raise AnalysisError(
            f"{n} periods is too short a history for this forecast ({k} terms): a coarser period, a longer "
            "history, or fewer predictors."
        )
    columns = [values_ for _, values_, _ in predictors]
    x = design(periods, period, seasonal, columns, steps)
    b, inv, sigma2, df = least_squares(x, values)
    fitted = [sum(bi * ri for bi, ri in zip(b, r, strict=True)) for r in x]
    future_periods = [step_period(periods[-1], period, s) for s in range(1, horizon + 1)]
    future_columns = [future_predictor(col, how, horizon) for _, col, how in predictors]
    future_x = design(
        [*periods, *future_periods],
        period,
        seasonal,
        [[*col, *fut] for col, fut in zip(columns, future_columns, strict=True)],
        steps,
    )[n:]
    t80, t95 = t_quantile(0.9, df), t_quantile(0.975, df)
    ahead = []
    for p, r in zip(future_periods, future_x, strict=True):
        estimate = sum(bi * ri for bi, ri in zip(b, r, strict=True))
        spread = sum(r[i] * inv[i][j] * r[j] for i in range(k) for j in range(k))
        se = math.sqrt(max(0.0, sigma2 * (1 + spread)))
        ahead.append(
            {
                "period": p.isoformat(),
                "estimate": estimate,
                "low80": estimate - t80 * se,
                "high80": estimate + t80 * se,
                "low95": estimate - t95 * se,
                "high95": estimate + t95 * se,
            }
        )
    check = held_back(periods, values, period, predictors, seasonal, steps)
    slope = b[1]
    return {
        "seasonal": seasonal,
        "trend_per_period": slope,
        "history": [
            {"period": p.isoformat(), "value": v, "fitted": f}
            for p, v, f in zip(periods, values, fitted, strict=True)
        ],
        "forecast": ahead,
        "check": check,
        "terms": k,
    }


def held_back(
    periods: list[date],
    values: list[float],
    period: str,
    predictors: list[tuple[str, list[float], dict[str, Any]]],
    seasonal: bool,
    steps: list[str],
) -> dict[str, Any] | None:
    """Forecast the last periods (a fifth, at most a season, at least 2) from the rest, predictors at
    their real values: the typical miss a forecast earns. A step the rest has seen (three periods of
    it) is used; a later one is not known yet. None when the history is too short."""
    n = len(values)
    hold = max(2, min(SEASON[period] if SEASON[period] > 1 else 4, n // 5))
    train = n - hold
    known = [s for s in steps if sum(1 for p in periods[:train] if p.isoformat() >= s) >= 3]
    k = 2 + len(known) + (SEASON[period] - 1 if seasonal else 0) + len(predictors)
    if train < k + 3:
        return None
    columns = [col for _, col, _ in predictors]
    x = design(periods, period, seasonal, columns, known)
    try:
        b, _, _, _ = least_squares(x[:train], values[:train])
    except AnalysisError:
        return None
    misses = [values[i] - sum(bi * ri for bi, ri in zip(b, x[i], strict=True)) for i in range(train, n)]
    mae = sum(abs(e) for e in misses) / hold
    actual = [abs(values[i]) for i in range(train, n)]
    mape = sum(abs(e) / a for e, a in zip(misses, actual, strict=True) if a) / max(
        1, sum(1 for a in actual if a)
    )
    return {
        "periods": hold,
        "typical_miss": mae,
        "typical_miss_share": mape,
        "from": periods[train].isoformat(),
    }
