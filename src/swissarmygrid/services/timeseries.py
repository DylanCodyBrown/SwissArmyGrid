"""Time series and forecast on a grid's view (domain/timeseries.py has the math and the query text).

`run_time_series` and `run_forecast` work over any cursor, like the Analyze menu's other analyses, so
swissarmygrid.stats runs the same code on an analyst's own table.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from swissarmygrid.domain import timeseries as T
from swissarmygrid.domain.analysis import AnalysisError, short_number
from swissarmygrid.domain.datasets import Column, DatasetInfo
from swissarmygrid.domain.grids import Grid
from swissarmygrid.domain.steps import type_family
from swissarmygrid.services.ports import AGENT, BACKGROUND, SqlSession
from swissarmygrid.services.workspace import Workspace

Columns = tuple[Column, ...]


def _column(view: Columns, name: Any, families: tuple[str, ...] = ()) -> Column:
    match = next((c for c in view if c.name == name), None)
    if match is None:
        raise AnalysisError(f"No column {name!r} in this view.")
    if families and type_family(match.type) not in families:
        raise AnalysisError(f"{match.name} is not a {' or '.join(families)} column ({match.type}).")
    return match


def _day(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _request(view: Columns, body: dict[str, Any]) -> tuple[str, str | None, str, str]:
    """(date column, metric or None for a count, total, period), checked."""
    when = _column(view, body.get("date"), ("date", "timestamp")).name
    total = body.get("total") or ("count" if not body.get("metric") else "sum")
    period = body.get("period") or "month"
    T.check_request(period, total, int(body.get("horizon") or 12))
    metric = None if total == "count" else _column(view, body.get("metric"), ("number",)).name
    return when, metric, total, period


def _series(
    cur: SqlSession, relation: str, when: str, metric: str | None, total: str, period: str
) -> tuple[list[date], list[float], list[str]]:
    """The periods from first to last, their totals, and the periods with no rows (counted as 0 for a
    sum or a count; carried from the period before for an average)."""
    rows = cur.execute(T.series_query(relation, when, period, total, metric, None)).fetchall()
    if len(rows) > T.MAX_PERIODS:
        raise AnalysisError(f"More than {T.MAX_PERIODS:,} periods: choose a longer period.")
    points = [(_day(p), float(v)) for p, _, v, _ in rows if p is not None and v is not None]
    if len(points) < 4:
        raise AnalysisError("Fewer than 4 periods with values: a longer history, or a shorter period.")
    periods, values = T.complete(points, period)
    gaps = [p.isoformat() for p, v in zip(periods, values, strict=True) if v is None]
    filled: list[float] = []
    for v in values:
        filled.append(
            v if v is not None else (0.0 if total in ("sum", "count") else (filled[-1] if filled else 0.0))
        )
    return periods, filled, gaps


def _what(metric: str | None, total: str) -> str:
    return "rows" if total == "count" else f"{'average ' if total == 'avg' else ''}{metric}"


def run_time_series(cur: SqlSession, relation: str, view: Columns, body: dict[str, Any]) -> dict[str, Any]:
    """{"date", "metric", "total": sum | avg | count, "period": day | week | month | quarter | year,
    "split": a category for one line per value}: the series' shape, growth, anomalies, shifts, memory."""
    when, metric, total, period = _request(view, body)
    periods, values, gaps = _series(cur, relation, when, metric, total, period)
    what = _what(metric, total)
    dec = T.decompose(periods, values, period)
    adjusted = T._adjusted(values, dec["season"], dec["multiplicative"])
    shifts = T.level_shifts(periods, adjusted, T.big_spikes(periods, adjusted))
    found = T.anomalies(periods, values, dec["season"], dec["multiplicative"], shifts)
    left = [x - 1 if dec["multiplicative"] else x for x in dec["left"]]
    memory = T.autocorrelation(left, 1)
    growth = T.growth(periods, values, period)
    n = len(values)
    first, last = dec["trend"][0], dec["trend"][-1]
    trend_words = (
        f"{what} went from about {short_number(first)} to {short_number(last)} per {period} "
        f"({'+' if last >= first else ''}{(last - first) / abs(first) * 100:.0f}% over {n} {period}s)"
        if first
        else f"{what} ends at about {short_number(last)} per {period}"
    )
    words = [trend_words]
    season = T.season_words(dec["index"], period, dec["multiplicative"], what) if dec["seasonal"] else []
    if season:
        words.append("Seasonal: " + "; ".join(season) + ".")
    elif T.SEASON[period] > 1:
        words.append(f"Too short to judge a season: it takes two full {T.SEASON[period]}-{period} cycles.")
    if found:
        words.append(
            f"{len(found)} period{'s' if len(found) != 1 else ''} stand{'s' if len(found) == 1 else ''} out: "
            + ", ".join(f"{a['kind']} {a['period']}" for a in found[:5])
            + "."
        )
    if dec["multiplicative"]:  # the steps were found on the log of the deseasonalised series
        for shift in shifts:
            shift["before"], shift["after"] = math.exp(shift["before"]), math.exp(shift["after"])
    for shift in shifts:
        change = (shift["after"] - shift["before"]) / abs(shift["before"]) * 100 if shift["before"] else 0.0
        words.append(
            f"The level stepped at {shift['period']}: from about {short_number(shift['before'])} to "
            f"{short_number(shift['after'])} a {period} "
            f"({'+' if change >= 0 else ''}{change:.0f}%), season aside."
        )
    words.append(f"Memory: {T.memory_words(memory)}.")
    lines = []
    if body.get("split"):
        split = _column(view, body["split"]).name
        rows = cur.execute(T.series_query(relation, when, period, total, metric, split)).fetchall()
        by: dict[str, list[dict[str, Any]]] = {}
        for p, s, v, _ in rows:
            by.setdefault(str(s), []).append({"period": _day(p).isoformat(), "value": float(v or 0)})
        lines = [{"name": k, "points": v} for k, v in by.items()]
    return {
        "date": when,
        "metric": metric,
        "total": total,
        "period": period,
        "what": what,
        "points": [
            {"period": p.isoformat(), "value": v, "trend": t, "season": s}
            for p, v, t, s in zip(periods, values, dec["trend"], dec["season"], strict=True)
        ],
        "gaps": gaps,
        "seasonal": dec["seasonal"],
        "multiplicative": dec["multiplicative"],
        "season_index": [
            {
                "position": (
                    T.POSITION_NAMES.get(period) or [f"{period} {k + 1}" for k in range(T.SEASON[period])]
                )[k]
                if k < T.SEASON[period]
                else str(k),
                "index": v,
            }
            for k, v in sorted(dec["index"].items())
        ],
        "season_words": season,
        "anomalies": found,
        "shifts": shifts,
        "memory": memory,
        "growth": growth[-36:],
        "words": words,
        "step": T.anomaly_step(when, period, [a["period"] for a in found]) if found else None,
        "lines": lines,
    }


def run_forecast(cur: SqlSession, relation: str, view: Columns, body: dict[str, Any]) -> dict[str, Any]:
    """{"date", "metric", "total", "period", "horizon": periods ahead, "predictors": [{"column",
    "future": last | trend | value, "value"}]}: the forecast with 80% and 95% ranges and the held-back
    check. A predictor enters as its average per period."""
    when, metric, total, period = _request(view, body)
    horizon = int(body.get("horizon") or 12)
    periods, values, gaps = _series(cur, relation, when, metric, total, period)
    asks = body.get("predictors") or []
    if not isinstance(asks, list) or not all(isinstance(a, dict) for a in asks):
        raise AnalysisError('Predictors are [{"column": name, "future": "last" | "trend" | "value"}].')
    predictors: list[tuple[str, list[float], dict[str, Any]]] = []
    for ask in asks:
        column = _column(view, ask.get("column"), ("number",)).name
        rows = cur.execute(T.series_query(relation, when, period, "avg", column, None)).fetchall()
        found = {_day(p): float(v) for p, _, v, _ in rows if p is not None and v is not None}
        series: list[float] = []
        for p in periods:
            series.append(found.get(p, series[-1] if series else next(iter(found.values()), 0.0)))
        predictors.append((column, series, ask))
    dec = T.decompose(periods, values, period)
    adjusted = T._adjusted(values, dec["season"], dec["multiplicative"])
    steps = [s["period"] for s in T.level_shifts(periods, adjusted, T.big_spikes(periods, adjusted))]
    result = T.fit_forecast(periods, values, period, horizon, predictors, steps)
    result["steps"] = steps
    what = _what(metric, total)
    nxt = result["forecast"][0]
    end = result["forecast"][-1]
    words = [
        f"Next {period}: about {short_number(nxt['estimate'])} {what} (80% of the time between "
        f"{short_number(nxt['low80'])} and {short_number(nxt['high80'])}).",
        f"In {horizon} {period}s: about {short_number(end['estimate'])} "
        f"(between {short_number(end['low80'])} and {short_number(end['high80'])}).",
        f"The trend adds about {short_number(result['trend_per_period'])} per {period}"
        + (", with the season's pattern on top." if result["seasonal"] else "."),
    ]
    check = result["check"]
    if check:
        words.append(
            f"Held back the last {check['periods']} {period}s and forecast them from the rest: off by about "
            f"{short_number(check['typical_miss'])} a {period} ({check['typical_miss_share'] * 100:.0f}%)."
        )
    if gaps:
        words.append(
            f"{len(gaps)} {period}s had no rows "
            f"(counted as {'0' if total != 'avg' else 'the period before'})."
        )
    words.append("A forecast is a range, not a line: it assumes the past pattern goes on.")
    return {
        "date": when,
        "metric": metric,
        "total": total,
        "period": period,
        "what": what,
        "horizon": horizon,
        "predictors": [
            {"column": c, "future": a.get("future", "last"), "value": a.get("value")}
            for c, _, a in predictors
        ],
        "gaps": gaps,
        "words": words,
        **result,
    }


class TimeSeries:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    async def run(
        self, analysis: str, grid: Grid, info: DatasetInfo, body: dict[str, Any], who: str
    ) -> dict[str, Any]:
        ws = self.ws
        fn: Callable[[SqlSession, str, Columns, dict[str, Any]], dict[str, Any]] = (
            run_forecast if analysis == "forecast" else run_time_series
        )

        def compute(cur: SqlSession) -> dict[str, Any]:
            view = ws.compiled(cur, grid, info).columns
            with ws.views.reading(cur, info, grid.steps, grid.settings) as rows:
                return fn(cur, f"({rows}) AS src", view, body)

        result = await ws.grid_query(grid, None, AGENT if who == "agent" else BACKGROUND, compute)
        ws.audit.write(who, analysis, info.name, date=body.get("date"), metric=body.get("metric"))
        return result
