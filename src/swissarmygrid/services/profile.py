"""Column profiles: the built-in analysis (plan section 8).

Everything is computed in DuckDB on the grid's current view (after its steps), so a profile
always describes the rows the user is looking at. Callers cache results by (dataset version,
step list, settings, column) through ViewEngine-style keys.

Statistics follow the reference semantics: sample standard deviation and quartiles with
linear interpolation (DuckDB stddev_samp / quantile_cont, which match pandas' std() and
quantile() defaults; tested in tests/test_profile.py). "Distinct" counts non-blank values,
like pandas nunique().

Every query goes through the `SqlSession` port, on the relation the caller hands in (the grid's
view, held by the view cache); nothing here knows DuckDB's Python library.
"""

from __future__ import annotations

from typing import Any

from swissarmygrid.domain.datasets import Column, quote
from swissarmygrid.domain.steps import _TRIM_SQL, type_family
from swissarmygrid.services.ports import SqlSession

TOP_VALUES: int = 20


HISTOGRAM_BINS: int = 20


def _one(cursor: SqlSession, sql: str) -> tuple[Any, ...]:
    row = cursor.execute(sql).fetchone()
    return tuple(row) if row else ()


def profile_column(cursor: SqlSession, view_sql: str, column: Column) -> dict[str, Any]:
    """Everything section 8 lists for one column of the view `view_sql`. GROUP BY and ORDER
    BY name their columns by position: an alias would lose to a view column of the same name."""
    c = quote(column.name)
    v = f"({view_sql}) AS v"
    family = type_family(column.type)
    rows, blanks, distinct = _one(
        cursor, f"SELECT count(*), count(*) - count({c}), count(DISTINCT {c}) FROM {v}"
    )
    # GROUP BY's groups: the distinct values, plus one for the blanks if there are any.
    groups = distinct + (1 if blanks else 0)
    top = cursor.execute(
        f"SELECT {c}, count(*) FROM {v} GROUP BY 1 ORDER BY 2 DESC, 1 ASC NULLS LAST LIMIT {TOP_VALUES}"
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
            histogram=_histogram(cursor, v, c, column.type),
        )
    elif family == "text":
        shortest, longest, empty, padded = _one(
            cursor,
            f"SELECT min(length({c})), max(length({c})), count(*) FILTER ({c} = ''), "
            f"count(*) FILTER ({c} <> trim({c}, {_TRIM_SQL})) FROM {v}",
        )
        case_groups = cursor.execute(
            f"SELECT lower({c}), list(DISTINCT {c} ORDER BY {c}), count(*) FROM {v} "
            f"WHERE {c} IS NOT NULL GROUP BY 1 HAVING count(DISTINCT {c}) > 1 ORDER BY 3 DESC, 1 "
            f"LIMIT {TOP_VALUES}"
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
            f"SELECT CAST(date_trunc('month', {c}) AS DATE), count(*) FROM {v} "
            f"WHERE {c} IS NOT NULL GROUP BY 1 ORDER BY 1"
        ).fetchall()
        profile.update(
            earliest=earliest,
            latest=latest,
            by_month=[{"month": m, "count": n} for m, n in months],
        )
    return profile


def _histogram(cursor: SqlSession, v: str, c: str, duckdb_type: str) -> list[dict[str, Any]]:
    """Equal-width bins between the smallest and largest finite values; the largest lands in
    the last bin. NaN and infinities have no bin (min and max still report them)."""
    floating = duckdb_type.upper() in ("FLOAT", "DOUBLE", "REAL", "FLOAT4", "FLOAT8")
    where = f"{c} IS NOT NULL" + (f" AND isfinite({c})" if floating else "")
    low, high, count = _one(cursor, f"SELECT min({c}), max({c}), count(*) FROM {v} WHERE {where}")
    if low is None or high is None:
        return []
    low_f, high_f = float(low), float(high)
    if low_f == high_f:
        return [{"low": low_f, "high": high_f, "count": int(count)}]
    # Divided before subtracting, so -1e308 to 1e308 does not overflow to infinity.
    width = high_f / HISTOGRAM_BINS - low_f / HISTOGRAM_BINS
    position = f"(CAST({c} AS DOUBLE) / {width!r} - {low_f / width!r})"
    rows = dict(
        cursor.execute(
            f"SELECT greatest(0, least(CAST(floor({position}) AS BIGINT), {HISTOGRAM_BINS - 1})) AS b, "
            f"count(*) FROM {v} WHERE {where} GROUP BY 1"
        ).fetchall()
    )
    return [
        {"low": low_f + i * width, "high": low_f + (i + 1) * width, "count": int(rows.get(i, 0))}
        for i in range(HISTOGRAM_BINS)
    ]


def profile_table(cursor: SqlSession, view_sql: str, columns: tuple[Column, ...]) -> dict[str, Any]:
    """The whole-table Profile view: one summary row per column, plus duplicate rows."""
    v = f"({view_sql}) AS v"
    names = ", ".join(quote(c.name) for c in columns)
    rows, distinct_rows = _one(
        cursor,
        f"SELECT (SELECT count(*) FROM {v}), "
        f"(SELECT count(*) FROM (SELECT {names} FROM {v} GROUP BY {names}))",
    )
    summary: list[dict[str, Any]] = []
    # Every column's numbers in one pass over the view (one query per column read it 10 times).
    parts = [
        f"count(*) - count({c}), count(DISTINCT {c}), min({c}), max({c})"
        for c in (quote(column.name) for column in columns)
    ]
    values = _one(cursor, f"SELECT {', '.join(parts)} FROM {v}") if parts else ()
    for index, column in enumerate(columns):
        blanks, distinct, low, high = values[4 * index : 4 * index + 4]
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


def header_stats(cursor: SqlSession, view_sql: str, columns: tuple[Column, ...]) -> dict[str, dict[str, Any]]:
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
