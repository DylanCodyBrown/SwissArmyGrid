"""Column profiles: the built-in analysis (plan section 8).

Everything is computed in DuckDB on the grid's current view (after its steps), so a profile
always describes the rows the user is looking at. Callers cache results by (dataset version,
step list, settings, column) through ViewEngine-style keys.

Statistics follow the reference semantics: sample standard deviation and quartiles with
linear interpolation (DuckDB stddev_samp / quantile_cont, which match pandas' std() and
quantile() defaults; tested in tests/test_profile.py). "Distinct" counts non-blank values,
like pandas nunique().
"""

from __future__ import annotations

from typing import Any

import duckdb

from swissarmygrid.engine.datasets import Column, quote
from swissarmygrid.engine.steps import _TRIM_SQL, type_family

TOP_VALUES: int = 20
HISTOGRAM_BINS: int = 20


def _one(cursor: duckdb.DuckDBPyConnection, sql: str) -> tuple[Any, ...]:
    row = cursor.execute(sql).fetchone()
    return tuple(row) if row else ()


def profile_column(cursor: duckdb.DuckDBPyConnection, view_sql: str, column: Column) -> dict[str, Any]:
    """Everything section 8 lists for one column of the view `view_sql`."""
    c = quote(column.name)
    v = f"({view_sql}) AS v"
    family = type_family(column.type)
    rows, blanks, distinct, groups = _one(
        cursor,
        f"SELECT count(*), count(*) - count({c}), count(DISTINCT {c}), "
        f"(SELECT count(*) FROM (SELECT {c} FROM {v} GROUP BY {c})) FROM {v}",
    )
    top = cursor.execute(
        f"SELECT {c}, count(*) AS n FROM {v} GROUP BY {c} ORDER BY n DESC, {c} ASC NULLS LAST "
        f"LIMIT {TOP_VALUES}"
    ).fetchall()
    profile: dict[str, Any] = {
        "column": column.name,
        "type": column.type,
        "family": family,
        "rows": rows,
        "blanks": blanks,
        "blank_percent": round(100.0 * blanks / rows, 2) if rows else 0.0,
        "distinct": distinct,
        "all_unique": rows > 0 and groups == rows,
        "top_values": [{"value": value, "count": n} for value, n in top],
    }

    if family == "number":
        (
            low,
            high,
            mean,
            median,
            q1,
            q3,
            stddev,
            total,
            zeros,
            negatives,
        ) = _one(
            cursor,
            f"SELECT min({c}), max({c}), avg({c}), median({c}), quantile_cont({c}, 0.25), "
            f"quantile_cont({c}, 0.75), stddev_samp({c}), sum({c}), count(*) FILTER ({c} = 0), "
            f"count(*) FILTER ({c} < 0) FROM {v}",
        )
        profile.update(
            min=low,
            max=high,
            mean=mean,
            median=median,
            q1=q1,
            q3=q3,
            stddev=stddev,
            sum=total,
            zeros=zeros,
            negatives=negatives,
            histogram=_histogram(cursor, v, c, low, high),
        )
    elif family == "text":
        shortest, longest, empty, padded = _one(
            cursor,
            f"SELECT min(length({c})), max(length({c})), count(*) FILTER ({c} = ''), "
            f"count(*) FILTER ({c} <> trim({c}, {_TRIM_SQL})) FROM {v}",
        )
        case_groups = cursor.execute(
            f"SELECT lower({c}) AS k, list(DISTINCT {c} ORDER BY {c}) AS variants, "
            f"count(*) AS n FROM {v} WHERE {c} IS NOT NULL GROUP BY k "
            f"HAVING count(DISTINCT {c}) > 1 ORDER BY n DESC, k LIMIT {TOP_VALUES}"
        ).fetchall()
        profile.update(
            shortest=shortest,
            longest=longest,
            empty_strings=empty,
            padded=padded,
            case_groups=[{"variants": list(vs), "rows": n} for _, vs, n in case_groups],
        )
    elif family in ("date", "timestamp"):
        earliest, latest = _one(cursor, f"SELECT min({c}), max({c}) FROM {v}")
        months = cursor.execute(
            f"SELECT CAST(date_trunc('month', {c}) AS DATE) AS m, count(*) FROM {v} "
            f"WHERE {c} IS NOT NULL GROUP BY m ORDER BY m"
        ).fetchall()
        profile.update(
            earliest=earliest,
            latest=latest,
            by_month=[{"month": m, "count": n} for m, n in months],
        )
    return profile


def _histogram(
    cursor: duckdb.DuckDBPyConnection, v: str, c: str, low: Any, high: Any
) -> list[dict[str, Any]]:
    """Equal-width bins between min and max; the max lands in the last bin."""
    if low is None or high is None:
        return []
    low_f, high_f = float(low), float(high)
    if low_f == high_f:
        count = _one(cursor, f"SELECT count({c}) FROM {v}")[0]
        return [{"low": low_f, "high": high_f, "count": count}]
    width = (high_f - low_f) / HISTOGRAM_BINS
    rows = dict(
        cursor.execute(
            f"SELECT least(CAST(floor(({c} - {low_f!r}) / {width!r}) AS BIGINT), {HISTOGRAM_BINS - 1}) AS b, "
            f"count(*) FROM {v} WHERE {c} IS NOT NULL GROUP BY 1"
        ).fetchall()
    )
    return [
        {"low": low_f + i * width, "high": low_f + (i + 1) * width, "count": int(rows.get(i, 0))}
        for i in range(HISTOGRAM_BINS)
    ]


def profile_table(
    cursor: duckdb.DuckDBPyConnection, view_sql: str, columns: tuple[Column, ...]
) -> dict[str, Any]:
    """The whole-table Profile view: one summary row per column, plus duplicate rows."""
    v = f"({view_sql}) AS v"
    names = ", ".join(quote(c.name) for c in columns)
    rows, distinct_rows = _one(
        cursor,
        f"SELECT (SELECT count(*) FROM {v}), "
        f"(SELECT count(*) FROM (SELECT {names} FROM {v} GROUP BY {names}))",
    )
    summary: list[dict[str, Any]] = []
    for column in columns:
        c = quote(column.name)
        blanks, distinct, low, high = _one(
            cursor,
            f"SELECT count(*) - count({c}), count(DISTINCT {c}), min({c}), max({c}) FROM {v}",
        )
        summary.append(
            {
                "column": column.name,
                "type": column.type,
                "blanks": blanks,
                "blank_percent": round(100.0 * blanks / rows, 2) if rows else 0.0,
                "distinct": distinct,
                "min": low,
                "max": high,
            }
        )
    return {"rows": rows, "duplicate_rows": rows - distinct_rows, "columns": summary}


def header_stats(
    cursor: duckdb.DuckDBPyConnection, view_sql: str, columns: tuple[Column, ...]
) -> dict[str, dict[str, Any]]:
    """The small numbers shown in each column header, in one pass over the view."""
    if not columns:
        return {}
    v = f"({view_sql}) AS v"
    parts: list[str] = ["count(*)"]
    for column in columns:
        c = quote(column.name)
        parts += [f"count(DISTINCT {c})", f"count(*) - count({c})"]
    values = _one(cursor, f"SELECT {', '.join(parts)} FROM {v}")
    rows = int(values[0]) if values else 0
    out: dict[str, dict[str, Any]] = {}
    for i, column in enumerate(columns):
        distinct, blanks = values[1 + 2 * i], values[2 + 2 * i]
        out[column.name] = {
            "distinct": distinct,
            "blank_percent": round(100.0 * blanks / rows, 1) if rows else 0.0,
        }
    return out
