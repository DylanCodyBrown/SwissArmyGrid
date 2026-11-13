"""The fit test and outliers on a grid's current view (domain/analysis.py has the math and the query
text). Both read the rows the user is looking at, after the grid's steps, and change nothing: an
outlier result carries the step that would show its rows, for the page (or the agent) to add.
"""

from __future__ import annotations

from typing import Any

from swissarmygrid.domain import analysis as A
from swissarmygrid.domain.datasets import Column, DatasetInfo
from swissarmygrid.domain.grids import Grid
from swissarmygrid.domain.steps import type_family
from swissarmygrid.services.ports import AGENT, BACKGROUND, SqlSession
from swissarmygrid.services.workspace import Workspace

MAX_STEP_VALUES = 1_000  # flagged values (or groups) written into a Show these rows step


def _column(columns: tuple[Column, ...], name: Any, number: bool = False) -> Column:
    match = next((c for c in columns if c.name == name), None)
    if match is None:
        raise A.AnalysisError(f"No column {name!r} in this view.")
    if number and type_family(match.type) != "number":
        raise A.AnalysisError(f"{match.name} is not a number column ({match.type}).")
    return match


def _counts(cur: SqlSession, relation: str, column: str) -> list[tuple[str, int]]:
    rows = [(str(v), int(n)) for v, n in cur.execute(A.counts_query(relation, column)).fetchall()]
    if len(rows) > A.MAX_CATEGORIES:
        raise A.AnalysisError(
            f"{column} has more than {A.MAX_CATEGORIES} different values: a fit test needs categories. "
            "Bucket it first (Column, Bucket), or test its first digits (Benford)."
        )
    return rows


class Analysis:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    async def fit(self, grid: Grid, info: DatasetInfo, body: dict[str, Any], who: str) -> dict[str, Any]:
        """{"columns": [...], "expected": even | benford | benford2 | grid | shares | together,
        "reference": {"grid", "column"} (grid), "shares": {value: share} (shares)}."""
        ws = self.ws
        columns = body.get("columns")
        if isinstance(columns, str):
            columns = [columns]
        if not isinstance(columns, list) or not columns or not all(isinstance(c, str) for c in columns):
            raise A.AnalysisError("Give the columns to test.")
        kind = body.get("expected") or "even"
        if kind not in A.EXPECTED_KINDS:
            raise A.AnalysisError(f"Expected is one of {', '.join(A.EXPECTED_KINDS)}, not {kind!r}.")
        if kind == "together" and len(columns) != 2:
            raise A.AnalysisError("Together tests two columns: select exactly two.")
        expected: dict[str, float] | None = None
        reference: dict[str, Any] | None = None
        if kind == "shares":
            shares = body.get("shares")
            if not isinstance(shares, dict) or not shares:
                raise A.AnalysisError('Give "shares": each value\'s expected share, such as {"North": 0.4}.')
            try:
                expected = {str(k): float(v) for k, v in shares.items()}
            except (TypeError, ValueError) as error:
                raise A.AnalysisError("Each share is a number.") from error
        if kind == "grid":
            reference, expected = await self._reference(body.get("reference"), columns, who)

        def compute(cur: SqlSession) -> dict[str, Any]:
            view = ws.compiled(cur, grid, info).columns
            with ws.views.reading(cur, info, grid.steps, grid.settings) as rows:
                relation = f"({rows}) AS src"
                if kind == "together":
                    a, b = (_column(view, c).name for c in columns)
                    found = cur.execute(A.together_query(relation, a, b)).fetchall()
                    cells = [(str(x), str(y), int(n)) for x, y, n in found]
                    return {"expected": kind, "columns": [a, b], **A.together(cells)}
                results = []
                for name in columns:
                    if kind in ("benford", "benford2"):
                        digits = 1 if kind == "benford" else 2
                        column = _column(view, name, number=True)
                        query = A.digits_query(relation, column.name, digits)
                        observed = [(str(d), int(n)) for d, n in cur.execute(query).fetchall()]
                        result = A.fit(observed, A.benford_shares(digits), benford=digits)
                    else:
                        column = _column(view, name)
                        result = A.fit(_counts(cur, relation, column.name), expected)
                    results.append({"column": column.name, **result})
                return {"expected": kind, "reference": reference, "results": results}

        priority = AGENT if who == "agent" else BACKGROUND
        result = await ws.grid_query(grid, None, priority, compute)
        ws.audit.write(who, "fit_test", info.name, columns=columns, expected=kind)
        return result

    async def _reference(
        self, ref: Any, columns: list[str], who: str
    ) -> tuple[dict[str, Any], dict[str, float]]:
        """Another grid's spread of values: the expected shares for a fit test."""
        if not isinstance(ref, dict) or not ref.get("grid"):
            raise A.AnalysisError('Give "reference": {"grid": id, "column": name} to test against.')
        if len(columns) != 1:
            raise A.AnalysisError("Test one column against another grid's column.")
        ws = self.ws
        grid, info = ws.grid_and_info(str(ref["grid"]))
        name = ref.get("column") or columns[0]

        def compute(cur: SqlSession) -> list[tuple[str, int]]:
            column = _column(ws.compiled(cur, grid, info).columns, name)
            with ws.views.reading(cur, info, grid.steps, grid.settings) as rows:
                return _counts(cur, f"({rows}) AS src", column.name)

        counts = await ws.grid_query(grid, None, AGENT if who == "agent" else BACKGROUND, compute)
        return {"grid": grid.id, "dataset": info.name, "column": name}, {k: float(n) for k, n in counts}

    async def outliers(self, grid: Grid, info: DatasetInfo, body: dict[str, Any], who: str) -> dict[str, Any]:
        """{"column", "look": amount | frequency, "method": robust | iqr | zscore, "threshold",
        "by": a column to judge each group of separately (amount)}."""
        ws = self.ws
        look = body.get("look") or "amount"
        method = body.get("method") or "robust"
        threshold = A.check_outlier_options(look, method, body.get("threshold"))
        by = body.get("by") or None

        def compute(cur: SqlSession) -> dict[str, Any]:
            view = ws.compiled(cur, grid, info).columns
            column = _column(view, body.get("column"), number=look == "amount")
            group = _column(view, by).name if by and look == "amount" else None
            if group == column.name:
                raise A.AnalysisError("Split by another column than the one judged.")
            with ws.views.reading(cur, info, grid.steps, grid.settings) as rows:
                relation = f"({rows}) AS src"
                if look == "frequency":
                    return self._frequency(cur, relation, column.name, threshold)
                return self._amount(cur, relation, column.name, group, method, threshold)

        priority = AGENT if who == "agent" else BACKGROUND
        result = await ws.grid_query(grid, None, priority, compute)
        ws.audit.write(who, "outliers", info.name, column=body.get("column"), look=look, method=method)
        return result

    @staticmethod
    def _amount(
        cur: SqlSession, relation: str, column: str, by: str | None, method: str, t: float
    ) -> dict[str, Any]:
        summary_sql, top_sql = A.amount_queries(relation, column, by, method, t)
        groups = cur.execute(summary_sql).fetchall()
        flagged = [g for g in groups if g[6] or g[7]]
        top = cur.execute(top_sql).fetchall()
        fences = [(g[0], float(g[2]), float(g[3])) for g in flagged[:MAX_STEP_VALUES]]
        result: dict[str, Any] = {
            "look": "amount",
            "column": column,
            "by": by,
            "method": method,
            "threshold": t,
            "rows": sum(int(g[1]) for g in groups),
            "low": sum(int(g[6]) for g in groups),
            "high": sum(int(g[7]) for g in groups),
            "groups": len(groups),
            "groups_flagged": len(flagged),
            "groups_unjudged": sum(1 for g in groups if g[2] is None),
            "fences": [
                {
                    "group": g[0],
                    "rows": int(g[1]),
                    "low_fence": g[2],
                    "high_fence": g[3],
                    "median": g[4],
                    "mean": g[5],
                    "low": int(g[6]),
                    "high": int(g[7]),
                }
                for g in (flagged if by else groups)[: A.TOP]
            ],
            "outliers": [{"group": g, "value": x, "score": score} for g, x, score in top],
            "step": A.amount_step(column, by, fences) if fences else None,
        }
        if len(flagged) > MAX_STEP_VALUES:
            result["step_note"] = f"The step shows the first {MAX_STEP_VALUES} groups with outliers."
        return result

    @staticmethod
    def _frequency(cur: SqlSession, relation: str, column: str, t: float) -> dict[str, Any]:
        summary_sql, top_sql = A.frequency_queries(relation, column, t)
        distinct, typical, rarer, commoner = cur.execute(summary_sql).fetchone() or (0, None, 0, 0)
        top = cur.execute(top_sql).fetchall()
        _, values_sql = A.frequency_queries(relation, column, t, MAX_STEP_VALUES)
        values = [str(v) for v, _, _ in cur.execute(values_sql).fetchall()]
        return {
            "look": "frequency",
            "column": column,
            "method": "robust",
            "threshold": t,
            "distinct": int(distinct),
            "typical": typical,
            "rarer": int(rarer),
            "commoner": int(commoner),
            "judged": int(distinct) >= A.MIN_GROUP,
            "outliers": [{"value": v, "count": int(n), "score": score} for v, n, score in top],
            "step": A.frequency_step(column, values) if values else None,
        }
