"""The Analyze menu's analyses: the fit test, outliers, odds and find predictors (domain/analysis.py has
the math and the query text).

Each analysis is a `run_*` function over any cursor, a relation and its columns, so the same code runs
on a grid's view here and on an analyst's own table through `swissarmygrid.stats` (which is what the
notebook code calls: the numbers match the window's because they come from the same function). The
`Analysis` service reads the rows the user is looking at, after the grid's steps, and changes nothing:
an outlier result carries the steps that would show or exclude its rows, for the page or the agent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from swissarmygrid.domain import analysis as A
from swissarmygrid.domain import stats
from swissarmygrid.domain.datasets import Column, DatasetInfo
from swissarmygrid.domain.grids import Grid
from swissarmygrid.domain.steps import type_family
from swissarmygrid.services.codegen import generate_code
from swissarmygrid.services.ports import AGENT, BACKGROUND, CodeVerifier, SqlSession
from swissarmygrid.services.workspace import Conflict, Workspace

MAX_STEP_VALUES = 1_000  # flagged values (or groups) written into a Show these rows step
FEW_VALUES = 12  # a number column with this many different values or fewer is categories as it is

Columns = tuple[Column, ...]


# ===== the analyses, over any cursor =====


def check_fit(body: dict[str, Any]) -> tuple[list[str], str, dict[str, float] | None]:
    """The columns, the expected kind and (for shares) the expected shares, checked."""
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
    if kind == "grid" and len(columns) != 1:
        raise A.AnalysisError("Test one column against another table's column.")
    expected: dict[str, float] | None = None
    if kind == "shares":
        shares = body.get("shares")
        if not isinstance(shares, dict) or not shares:
            raise A.AnalysisError('Give "shares": each value\'s expected share, such as {"North": 0.4}.')
        try:
            expected = {str(k): float(v) for k, v in shares.items()}
        except (TypeError, ValueError) as error:
            raise A.AnalysisError("Each share is a number.") from error
    return columns, kind, expected


def run_fit(
    cur: SqlSession,
    relation: str,
    view: Columns,
    body: dict[str, Any],
    reference: tuple[dict[str, Any], dict[str, float]] | None = None,
) -> dict[str, Any]:
    """The fit test. `reference`: (where it came from, its counts) for expected=grid."""
    columns, kind, expected = check_fit(body)
    if kind == "grid":
        if reference is None:
            raise A.AnalysisError("Testing against another table needs that table.")
        expected = reference[1]
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
    return {"expected": kind, "reference": reference[0] if reference else None, "results": results}


def reference_counts(cur: SqlSession, relation: str, view: Columns, column: str) -> dict[str, float]:
    """Another table's spread of a column: the expected shares for a fit test."""
    return {k: float(n) for k, n in _counts(cur, relation, _column(view, column).name)}


def check_outliers(body: dict[str, Any]) -> tuple[str, str, float, str | None]:
    look = body.get("look") or "amount"
    method = body.get("method") or "robust"
    return look, method, A.check_outlier_options(look, method, body.get("threshold")), body.get("by") or None


def run_outliers(cur: SqlSession, relation: str, view: Columns, body: dict[str, Any]) -> dict[str, Any]:
    """{"column", "look": amount | frequency, "method": robust | iqr | zscore, "threshold",
    "by": a column to judge each group of separately (amount)}."""
    look, method, threshold, by = check_outliers(body)
    column = _column(view, body.get("column"), number=look == "amount")
    group = _column(view, by).name if by and look == "amount" else None
    if group == column.name:
        raise A.AnalysisError("Split by another column than the one judged.")
    if look == "frequency":
        return _frequency(cur, relation, column.name, threshold)
    return _amount(cur, relation, column.name, group, method, threshold)


def run_odds(cur: SqlSession, relation: str, view: Columns, body: dict[str, Any]) -> dict[str, Any]:
    """{"column", "buckets": n (a number column in n equal-sized ranges), "outcome": {"column",
    "values": [...]} or {"column", "op", "value"}}: each value's share and odds, and with an outcome,
    each value's odds ratio for it."""
    buckets = _buckets(body.get("buckets"))
    column = _column(view, body.get("column"))
    outcome_sql: str | None = None
    outcome_words: str | None = None
    if body.get("outcome"):
        outcome = body["outcome"]
        target = _column(view, outcome.get("column") if isinstance(outcome, dict) else None)
        outcome_sql, outcome_words = A.outcome_condition(outcome, _is_number(target))
    distinct = _sizes(cur, relation, [column])[column.name][1]
    value_sql, bucketed = _categories(cur, relation, column, buckets, distinct)
    found = cur.execute(A.odds_query(relation, value_sql, outcome_sql)).fetchall()
    if len(found) > A.MAX_CATEGORIES:
        raise A.AnalysisError(
            f"{column.name} has more than {A.MAX_CATEGORIES} different values: "
            "bucket it, or pick another column."
        )
    result = A.odds([(str(v), int(n), None if y is None else int(y)) for v, n, y in found], outcome_words)
    blanks = cur.execute(A.blanks_query(relation, value_sql)).fetchone()
    return {"column": column.name, "bucketed": bucketed, "blanks": int(blanks[0]) if blanks else 0, **result}


def run_predictors(cur: SqlSession, relation: str, view: Columns, body: dict[str, Any]) -> dict[str, Any]:
    """{"target", "candidates": [...] (default: every other column), "buckets": n (default 4)}: each
    candidate tested together with the target (both bucketed when they are numbers), ranked by Cramér's
    V, with the straight-line correlation for number pairs and the candidates that say much the same as
    a stronger one."""
    buckets = _buckets(body.get("buckets"))
    wanted = body.get("candidates")
    if wanted is not None and (not isinstance(wanted, list) or not all(isinstance(c, str) for c in wanted)):
        raise A.AnalysisError("Candidates are a list of column names.")
    target = _column(view, body.get("target"))
    pool = [_column(view, name) for name in wanted] if wanted else list(view)
    pool = [c for c in pool if c.name != target.name]
    if not pool:
        raise A.AnalysisError("No other columns to test.")
    sizes = _sizes(cur, relation, [target, *pool])
    target_sql, target_bucketed = _categories(cur, relation, target, buckets, sizes[target.name][1])
    if sizes[target.name][1] > A.MAX_CATEGORIES and not target_bucketed:
        raise A.AnalysisError(
            f"{target.name} has too many different values to predict; pick a number or a category."
        )
    ranked: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    exprs: dict[str, str] = {}
    for column in pool:
        reason = _skip_reason(column, *sizes[column.name])
        if reason:
            skipped.append({"column": column.name, "reason": reason})
            continue
        expr, bucketed = _categories(cur, relation, column, buckets, sizes[column.name][1])
        cells = cur.execute(A.together_cells_query(relation, target_sql, expr)).fetchall()
        try:
            tested = A.together([(str(a), str(b), int(n)) for a, b, n in cells])
        except A.AnalysisError as error:
            skipped.append({"column": column.name, "reason": str(error)})
            continue
        item: dict[str, Any] = {
            "column": column.name,
            "kind": "number" if _is_number(column) else "category",
            "bucketed": bucketed,
            "v": tested["effect_size"],
            "strength": A.strength(tested["effect_size"]),
            "p": tested["p"],
            "df": tested["df"],
            "rows": tested["n"],
            "correlation": None,
        }
        if _is_number(target) and _is_number(column):
            r, n = cur.execute(A.correlation_query(relation, target.name, column.name)).fetchone() or (
                None,
                0,
            )
            if r is not None:
                item.update(correlation=float(r), correlation_p=stats.correlation_p(float(r), int(n)))
        item["note"] = A.shape_note(item["v"], item["correlation"])
        exprs[column.name] = expr
        ranked.append(item)
    # Strongest first; equal strengths (to nine digits: sums in another order differ in the last one) keep
    # the table's own column order, so the window and the notebook rank alike.
    place = {c.name: i for i, c in enumerate(pool)}
    ranked.sort(key=lambda c: (-round(c["v"], 9), place[c["column"]]))
    return {
        "target": target.name,
        "target_bucketed": target_bucketed,
        "candidates": ranked,
        "skipped": skipped,
        "redundant": _redundant(cur, relation, ranked, exprs),
    }


def run_shape(cur: SqlSession, relation: str, view: Columns, body: dict[str, Any]) -> dict[str, Any]:
    """{"analysis", "columns": [...]}: plain notes on whether the grid suits the analysis (the Data
    shape panel's check): wide tables, few rows, blanks, identifiers, numbers to bucket, Benford's range."""
    analysis = body.get("analysis")
    selected = body.get("columns") or []
    if not isinstance(analysis, str) or not isinstance(selected, list):
        raise A.AnalysisError('Send {"analysis": "odds", "columns": [...]}.')
    flags = [(c.name, _is_number(c)) for c in view]
    row = cur.execute(A.shape_query(relation, flags)).fetchone() or ()
    rows = int(row[0]) if row else 0
    columns = {
        c.name: (
            type_family(c.type),
            int(row[1 + 5 * i]),
            int(row[2 + 5 * i]),
            row[3 + 5 * i],
            row[4 + 5 * i],
            row[5 + 5 * i],
        )
        for i, c in enumerate(view)
    }
    return {"notes": A.shape_notes(analysis, rows, columns, [str(s) for s in selected])}


RUNS: dict[str, Callable[[SqlSession, str, Columns, dict[str, Any]], dict[str, Any]]] = {
    "shape": run_shape,
    "outliers": run_outliers,
    "odds": run_odds,
    "predictors": run_predictors,
}


# ===== on a grid =====


class Analysis:
    def __init__(self, ws: Workspace, verifier: CodeVerifier) -> None:
        self.ws = ws
        self._verifier = verifier

    async def _on_view(
        self, grid: Grid, info: DatasetInfo, who: str, run: Callable[[SqlSession, str, Columns], Any]
    ) -> Any:
        ws = self.ws

        def compute(cur: SqlSession) -> Any:
            view = ws.compiled(cur, grid, info).columns
            with ws.views.reading(cur, info, grid.steps, grid.settings) as rows:
                return run(cur, f"({rows}) AS src", view)

        return await ws.grid_query(grid, None, AGENT if who == "agent" else BACKGROUND, compute)

    async def fit(self, grid: Grid, info: DatasetInfo, body: dict[str, Any], who: str) -> dict[str, Any]:
        """{"columns": [...], "expected": even | benford | benford2 | grid | shares | together,
        "reference": {"grid", "column"} (grid), "shares": {value: share} (shares)}."""
        columns, kind, _ = check_fit(body)
        reference = await self._reference(body.get("reference"), columns, who) if kind == "grid" else None
        result = await self._on_view(
            grid, info, who, lambda cur, relation, view: run_fit(cur, relation, view, body, reference)
        )
        self.ws.audit.write(who, "fit_test", info.name, columns=columns, expected=kind)
        return dict(result)

    async def _reference(
        self, ref: Any, columns: list[str], who: str
    ) -> tuple[dict[str, Any], dict[str, float]]:
        if not isinstance(ref, dict) or not ref.get("grid"):
            raise A.AnalysisError('Give "reference": {"grid": id, "column": name} to test against.')
        grid, info = self.ws.grid_and_info(str(ref["grid"]))
        name = ref.get("column") or columns[0]
        counts = await self._on_view(
            grid, info, who, lambda cur, relation, view: reference_counts(cur, relation, view, name)
        )
        return {"grid": grid.id, "dataset": info.name, "column": name}, counts

    async def run(
        self, analysis: str, grid: Grid, info: DatasetInfo, body: dict[str, Any], who: str
    ) -> dict[str, Any]:
        """Outliers, odds or find predictors on the grid's view."""
        if analysis == "outliers":
            check_outliers(body)  # refused before any query runs
        run = RUNS[analysis]
        result = await self._on_view(
            grid, info, who, lambda cur, relation, view: run(cur, relation, view, body)
        )
        self.ws.audit.write(who, analysis, info.name, column=body.get("column") or body.get("target"))
        return dict(result)

    async def code(
        self, analysis: str, grid: Grid, info: DatasetInfo, body: dict[str, Any]
    ) -> dict[str, Any]:
        """The analysis as Python for the notebook: the grid's steps as one DuckDB query (checked
        against the grid, like Copy as SQL), then the `swissarmygrid.stats` call the window made."""
        if analysis not in A.CODE_ARGUMENTS:
            raise A.AnalysisError(f"No analysis {analysis!r}.")
        view_sql: str | None = None
        if grid.steps:
            ws = self.ws

            def make(cur: SqlSession) -> dict[str, Any]:
                return generate_code(
                    cur,
                    ws.views,
                    info,
                    grid.steps,
                    grid.settings,
                    "duckdb",
                    verifier=self._verifier,
                    grammar=ws.grammar,
                )

            made = await self.ws.grid_query(grid, None, BACKGROUND, make)
            if not made["verified"]:
                raise Conflict(
                    "This grid's steps could not be checked as SQL, so the analysis was not written: "
                    f"{made.get('difference') or made.get('note') or 'the copy did not match the grid'}"
                )
            view_sql = str(made["sql"])
        reference = None
        if (
            analysis == "fit_test"
            and body.get("expected") == "grid"
            and isinstance(body.get("reference"), dict)
        ):
            _, ref_info = self.ws.grid_and_info(str(body["reference"].get("grid")))
            reference = ref_info.name
        code, imports = A.analysis_code(analysis, body, info.name, view_sql, reference)
        return {"code": code, "imports": imports}


# ===== helpers =====


def _column(columns: Columns, name: Any, number: bool = False) -> Column:
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
        "exclude_step": A.amount_step(column, by, fences, keep=False) if fences else None,
    }
    if len(flagged) > MAX_STEP_VALUES:
        result["step_note"] = f"The step shows the first {MAX_STEP_VALUES} groups with outliers."
    return result


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
        "exclude_step": A.frequency_step(column, values, keep=False) if values else None,
    }


def _is_number(column: Column) -> bool:
    return type_family(column.type) == "number"


def _buckets(value: Any) -> int | None:
    if value in (None, "", 0):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError) as error:
        raise A.AnalysisError("Buckets is a whole number, such as 4.") from error
    if not 2 <= n <= A.MAX_BUCKETS:
        raise A.AnalysisError(f"Between 2 and {A.MAX_BUCKETS} buckets.")
    return n


def _sizes(cur: SqlSession, relation: str, columns: list[Column]) -> dict[str, tuple[int, int]]:
    """(rows with a value, different values) for each column, in one pass."""
    row = cur.execute(A.sizes_query(relation, [c.name for c in columns])).fetchone() or ()
    return {c.name: (int(row[2 * i]), int(row[2 * i + 1])) for i, c in enumerate(columns)}


def _categories(
    cur: SqlSession, relation: str, column: Column, buckets: int | None, distinct: int
) -> tuple[str, bool]:
    """The column as categories (SQL text, and whether it was bucketed): a number column is cut into
    equal-sized ranges when buckets are asked for or it has more than FEW_VALUES different values
    (4 ranges unless told); anything else is its values as text."""
    if _is_number(column) and (buckets or distinct > FEW_VALUES):
        edges = cur.execute(A.edges_query(relation, column.name, buckets or 4)).fetchone()
        return A.bucket_expression(column.name, list(edges[0] or []) if edges else []), True
    return A.as_text(column.name), False


def _skip_reason(column: Column, filled: int, distinct: int) -> str | None:
    family = type_family(column.type)
    if filled == 0:
        return "blank throughout"
    if distinct <= 1:
        return "one value only"
    if family in ("date", "timestamp"):
        return "a date: use Time series"
    if family not in ("number", "text", "boolean"):
        return f"a {column.type.lower()} column cannot be grouped"
    if family == "text" and distinct == filled and filled > 20:
        return "every row different: an identifier, not a predictor"
    if family == "number" and distinct == filled and filled > 20 and "INT" in column.type.upper():
        return "every row a different whole number: probably an identifier"
    if family == "text" and distinct > A.MAX_CATEGORIES:
        return f"more than {A.MAX_CATEGORIES} different values: group them first (Categorize)"
    return None


def _redundant(
    cur: SqlSession, relation: str, ranked: list[dict[str, Any]], exprs: dict[str, str]
) -> list[dict[str, Any]]:
    """Pairs among the strongest candidates that are themselves strongly related (Cramér's V of 0.7 or
    more): give the regression the stronger one."""
    top = [c for c in ranked if c["v"] >= 0.1][: A.MAX_REDUNDANCY_CHECKS]
    pairs: list[dict[str, Any]] = []
    for i, first in enumerate(top):
        for second in top[i + 1 :]:
            cells = cur.execute(
                A.together_cells_query(relation, exprs[first["column"]], exprs[second["column"]])
            ).fetchall()
            try:
                v = A.together([(str(a), str(b), int(n)) for a, b, n in cells])["effect_size"]
            except A.AnalysisError:
                continue
            if v >= A.STRONG_REDUNDANCY:
                pairs.append({"keep": first["column"], "drop": second["column"], "v": v})
                second.setdefault("same_as", first["column"])
    return pairs
