"""Regression on a grid's view (domain/regression.py has the math and the query text): linear for a
number target, logistic for a yes/no outcome; what-if predictions from the fitted model; and the steps
that add its predictions (or its misses) to the grid.

`run_regression` works over any cursor, like the Analyze menu's other analyses, so swissarmygrid.stats
runs the same code on an analyst's own table (what the notebook code calls).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from swissarmygrid.domain import analysis as A
from swissarmygrid.domain import regression as R
from swissarmygrid.domain.datasets import Column, DatasetInfo
from swissarmygrid.domain.grids import Grid
from swissarmygrid.domain.steps import type_family
from swissarmygrid.services.ports import AGENT, BACKGROUND, SqlSession
from swissarmygrid.services.workspace import Workspace

Columns = tuple[Column, ...]


def _column(view: Columns, name: Any) -> Column:
    match = next((c for c in view if c.name == name), None)
    if match is None:
        raise A.AnalysisError(f"No column {name!r} in this view.")
    return match


def run_regression(cur: SqlSession, relation: str, view: Columns, body: dict[str, Any]) -> dict[str, Any]:
    """{"target", "predictors": [...], "outcome": {"column", "values"} or {"column", "op", "value"}
    (a yes/no target: logistic)}. Without an outcome a number target is linear; a text target needs one."""
    target = _column(view, body.get("target"))
    names = body.get("predictors")
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise A.AnalysisError("Predictors are a list of column names.")
    predictors = [_column(view, n) for n in names]
    outcome = body.get("outcome")
    number_target = type_family(target.type) == "number"
    kind = "logistic" if outcome else "linear"
    if kind == "linear" and not number_target:
        raise A.AnalysisError(
            f"{target.name} is not a number: say which values count as yes (an outcome) "
            "for a logistic regression."
        )
    R.check_request(kind, target.name, [p.name for p in predictors])
    for p in predictors:
        if type_family(p.type) in ("date", "timestamp"):
            raise A.AnalysisError(
                f"{p.name} is a date: use Time series, or turn it into a number or a category first."
            )
    used = [target.name, *(p.name for p in predictors)]
    where = R.complete_rows(
        used, frozenset(c.name for c in (target, *predictors) if type_family(c.type) == "number")
    )
    numbers = [p.name for p in predictors if type_family(p.type) == "number"]
    means = cur.execute(R.means_query(relation, numbers, where)).fetchone() or (0,)
    complete = int(means[0])
    total_row = cur.execute(R.count_query(relation)).fetchone()
    total = int(total_row[0]) if total_row else complete
    if complete == 0:
        raise A.AnalysisError("No row has a value in every column used.")
    number_means: dict[str, float] = {}
    for i, name in enumerate(numbers):
        mean, sd = means[1 + 2 * i], means[2 + 2 * i]
        if not sd:
            raise A.AnalysisError(f"{name} has one value only in these rows: it cannot predict anything.")
        number_means[name] = float(mean)
    categories = {
        p.name: [str(v) for v, _ in cur.execute(R.levels_query(relation, p.name)).fetchall()]
        for p in predictors
        if p.name not in number_means
    }
    for name, levels in categories.items():
        if len(levels) < 2:
            raise A.AnalysisError(f"{name} has one value only: it cannot predict anything.")
    terms = R.build_terms(number_means, categories, [p.name for p in predictors])
    xs = ["1.0", *(R.term_sql(t) for t in terms)]
    if kind == "linear":
        y = f"CAST({A.quote(target.name)} AS DOUBLE)"
        row = cur.execute(R.sums_query(relation, xs, y, where)).fetchone() or ()
        result = R.linear(tuple(row), terms, target.name)
        outcome_words = None
    else:
        outcome_sql, outcome_words = A.outcome_condition(outcome, number_target)
        y = f"(CASE WHEN coalesce({outcome_sql}, FALSE) THEN 1.0 ELSE 0.0 END)"
        rate_row = cur.execute(R.rate_query(relation, y, where)).fetchone()
        rate = float(rate_row[0]) if rate_row and rate_row[0] is not None else 0.0
        if rate in (0.0, 1.0):
            raise A.AnalysisError(
                f"Every row is {'not ' if rate == 0 else ''}{outcome_words}: nothing to predict."
            )
        b = R.logistic_start(rate, len(xs))
        inv: list[list[float]] = []
        n = complete
        rounds = 0
        while rounds < R.MAX_ITERATIONS:
            rounds += 1
            weight, z = R.logistic_pass(terms, b, y)
            step = cur.execute(R.sums_query(relation, xs, z, where, weight=weight)).fetchone() or ()
            new, inv, n = R.logistic_step(tuple(step), len(xs))
            settled = max(abs(a - c) for a, c in zip(new, b, strict=True)) < 1e-9
            b = new
            if settled:
                break
        ll, avg_y, accuracy, rows = cur.execute(
            R.log_likelihood_query(relation, terms, b, y, where)
        ).fetchone() or (0, 0, 0, 0)
        fit = R.logistic_fit(float(ll), float(avg_y), float(accuracy), int(rows))
        result = R.logistic(b, inv, n, terms, target.name, str(outcome_words), fit, rounds)
    result["target"] = target.name
    result["outcome"] = outcome_words
    result["predictors"] = [p.name for p in predictors]
    result["left_out"] = total - complete
    result["references"] = {name: levels[0] for name, levels in categories.items()}
    result["verdict"] = _verdict(result)
    for row_ in result["coefficients"]:
        row_["words"] = R.effect_words(row_, result["kind"], target.name, outcome_words)
    predicted = f"predicted_{target.name}" if kind == "linear" else f"chance_{target.name}"
    result["steps"] = {
        "predicted": R.column_step(result["model"], predicted, miss=False),
        "miss": R.column_step(result["model"], f"miss_{target.name}", miss=True)
        if kind == "linear"
        else None,
    }
    return result


def _verdict(result: dict[str, Any]) -> str:
    fit = result["fit"]
    clear = [c["term"] for c in result["coefficients"][1:] if c["p"] < 0.05]
    named = ", ".join(clear[:4]) + ("..." if len(clear) > 4 else "") if clear else "none of the predictors"
    if result["kind"] == "linear":
        share = fit["r2"]
        return (
            f"The model explains {share:.0%} of the ups and downs of {result['target']}; its typical miss is "
            f"{A.short_number(fit['rmse'])}. Clearly part of it: {named}."
        )
    return (
        f"The model gets {fit['accuracy']:.0%} of rows right (always guessing the commoner answer gets "
        f"{fit['majority']:.0%}); it explains {fit['pseudo_r2']:.0%} of what chance leaves (McFadden). "
        f"Clearly part of it: {named}."
    )


def run_predict(body: dict[str, Any]) -> dict[str, Any]:
    """{"model": the fitted model, "values": {column: value}}: the what-if estimate and its range."""
    model = body.get("model")
    values = body.get("values")
    if not isinstance(model, dict) or not isinstance(values, dict):
        raise A.AnalysisError('Send {"model": ..., "values": {...}}.')
    for key in ("kind", "terms", "coefficients", "inverse"):
        if key not in model:
            raise A.AnalysisError("That is not a fitted model.")
    return R.predict(model, values)


class Regression:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    async def fit(self, grid: Grid, info: DatasetInfo, body: dict[str, Any], who: str) -> dict[str, Any]:
        ws = self.ws

        def compute(cur: SqlSession) -> dict[str, Any]:
            view = ws.compiled(cur, grid, info).columns
            with ws.views.reading(cur, info, grid.steps, grid.settings) as rows:
                return run_regression(cur, f"({rows}) AS src", view, body)

        run: Callable[[SqlSession], dict[str, Any]] = compute
        result = await ws.grid_query(grid, None, AGENT if who == "agent" else BACKGROUND, run)
        ws.audit.write(who, "regression", info.name, target=body.get("target"))
        return result
