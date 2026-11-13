"""The Analyze menu's statistics on your own table, in any Python with duckdb and swissarmygrid:

    from swissarmygrid import stats

    stats.odds(orders, column="region", outcome={"column": "late", "values": ["late"]})
    stats.predictors(orders, target="amount")
    stats.fit_test(invoices, columns=["amount"], expected="benford")
    stats.outliers(invoices, column="unit_cost", by="vendor")

`data` is a pandas or Polars DataFrame, an Arrow table or a DuckDB relation. Each function is the one
the grid's window runs (services/analysis.py), on a private in-memory DuckDB, so the notebook code
SwissArmyGrid writes gives the window's numbers. The results are plain dictionaries.

The distributions are here too: chi_square_p, t_p, f_p, normal_quantile, correlation_p.
"""

from __future__ import annotations

from typing import Any

import duckdb

from swissarmygrid.domain.datasets import Column
from swissarmygrid.domain.stats import chi_square_p, correlation_p, f_p, normal_quantile, t_p
from swissarmygrid.services import analysis as _analysis

__all__ = [
    "chi_square_p",
    "correlation_p",
    "f_p",
    "fit_test",
    "normal_quantile",
    "odds",
    "outliers",
    "predictors",
    "t_p",
]


def _open(data: Any) -> tuple[duckdb.DuckDBPyConnection, str, tuple[Column, ...]]:
    """A private DuckDB holding `data` as the table `data`, and its columns."""
    if isinstance(data, duckdb.DuckDBPyRelation):
        data = data.to_arrow_table()
    con = duckdb.connect()
    con.register("data", data)
    columns = tuple(
        Column(str(name), str(kind)) for name, kind, *_ in con.execute("DESCRIBE data").fetchall()
    )
    return con, "data AS src", columns


def _body(**values: Any) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def fit_test(
    data: Any,
    columns: list[str] | str,
    expected: str = "even",
    shares: dict[str, float] | None = None,
    reference: Any = None,
    reference_column: str | None = None,
) -> dict[str, Any]:
    """Chi-square goodness of fit: even, benford, benford2, shares, together (two columns), or grid
    (against `reference`, another table, and its `reference_column`, by default the same column)."""
    body = _body(columns=columns, expected=expected, shares=shares)
    con, relation, view = _open(data)
    counts = None
    if expected == "grid":
        if reference is None:
            raise ValueError('expected="grid" needs reference=<another table>.')
        ref_con, ref_relation, ref_view = _open(reference)
        name = reference_column or (columns if isinstance(columns, str) else columns[0])
        counts = ({"column": name}, _analysis.reference_counts(ref_con, ref_relation, ref_view, name))
    return _analysis.run_fit(con, relation, view, body, counts)


def outliers(
    data: Any,
    column: str,
    look: str = "amount",
    method: str = "robust",
    threshold: float | None = None,
    by: str | None = None,
) -> dict[str, Any]:
    """Values far from the rest (look="amount": robust, iqr or zscore; `by` judges each group on its
    own) or seen too often or too rarely (look="frequency")."""
    con, relation, view = _open(data)
    body = _body(column=column, look=look, method=method, threshold=threshold, by=by)
    return _analysis.run_outliers(con, relation, view, body)


def odds(
    data: Any, column: str, buckets: int | None = None, outcome: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Each value's share, 1 in N and odds; with `outcome` ({"column", "values"} or {"column", "op",
    "value"}), each value's rate against the rest and its odds ratio with a 95% range."""
    con, relation, view = _open(data)
    return _analysis.run_odds(con, relation, view, _body(column=column, buckets=buckets, outcome=outcome))


def predictors(
    data: Any, target: str, candidates: list[str] | None = None, buckets: int | None = None
) -> dict[str, Any]:
    """Every other column (or `candidates`) tested together with `target`, ranked by Cramér's V."""
    con, relation, view = _open(data)
    return _analysis.run_predictors(
        con, relation, view, _body(target=target, candidates=candidates, buckets=buckets)
    )
