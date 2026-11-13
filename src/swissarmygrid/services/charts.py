"""Charts and the side panel's cards (plan section 11): run a chart's one aggregate query.

The specification itself (what a chart may say, its query text, Vega-Lite) is the domain's
(domain/charts.py); here it is run on the grid's view through the `SqlSession` port.
"""

from __future__ import annotations

from typing import Any

from swissarmygrid.domain.charts import _grouped
from swissarmygrid.domain.datasets import quote
from swissarmygrid.services.ports import SqlSession


def aggregate(cursor: SqlSession, view_sql: str, spec: dict[str, Any], max_points: int) -> dict[str, Any]:
    """Run the chart's one aggregate query. Returns {"values": [...], "note": str | None}."""
    v = f"({view_sql}) AS v"
    mark = spec["mark"]
    x = spec["x"]
    xf = quote(x["field"])
    note: str | None = None

    if mark == "histogram":
        bins = x.get("bins", 20)
        # NaN and infinities have no bin (their width would be NaN, which no BIGINT holds).
        where = f"{xf} IS NOT NULL AND isfinite(CAST({xf} AS DOUBLE))"
        bounds: Any = cursor.execute(f"SELECT min({xf}), max({xf}) FROM {v} WHERE {where}").fetchone()
        low, high = bounds or (None, None)
        if low is None or high is None:
            return {"values": [], "note": "No values to chart."}
        low_f, high_f = float(low), float(high)
        # Divided before subtracting, so -1e308 to 1e308 does not overflow to infinity.
        width = high_f / bins - low_f / bins if high_f > low_f else 1.0
        position = f"(CAST({xf} AS DOUBLE) / {width!r} - {low_f / width!r})"
        rows = cursor.execute(
            f"SELECT greatest(0, least(CAST(floor({position}) AS BIGINT), {bins - 1})), count(*) "
            f"FROM {v} WHERE {where} GROUP BY 1 ORDER BY 1"
        ).fetchall()
        values = [
            {"bin_start": low_f + b * width, "bin_end": low_f + (b + 1) * width, "count": n} for b, n in rows
        ]
        return {"values": values, "note": None}

    y = spec.get("y", {})
    color = spec.get("color")

    if mark == "point" and y.get("aggregate") is None:
        yf = quote(y["field"])
        select = f"{xf} AS x, {yf} AS y" + (f", {quote(color['field'])} AS color" if color else "")
        total_row = cursor.execute(
            f"SELECT count(*) FROM {v} WHERE {xf} IS NOT NULL AND {yf} IS NOT NULL"
        ).fetchone()
        total = int(total_row[0]) if total_row else 0
        sample = ""
        if total > max_points:
            sample = f" USING SAMPLE reservoir({max_points} ROWS) REPEATABLE (42)"
            note = f"Showing a repeatable sample of {max_points:,} of {total:,} points."
        rows = cursor.execute(
            f"SELECT {select} FROM (SELECT * FROM {v} WHERE {xf} IS NOT NULL AND {yf} IS NOT NULL){sample}"
        ).fetchall()
        keys = ["x", "y", "color"] if color else ["x", "y"]
        return {"values": [dict(zip(keys, r, strict=True)) for r in rows], "note": note}

    projection, group, order = _grouped(spec)
    groups_row = cursor.execute(
        f"SELECT count(*) FROM (SELECT 1 FROM {v} GROUP BY {', '.join(group)})"
    ).fetchone()
    groups = int(groups_row[0]) if groups_row else 0
    if groups > max_points:
        note = f"Showing the {max_points:,} largest of {groups:,} groups."
        top = (
            f"SELECT {', '.join(projection)} FROM {v} GROUP BY {', '.join(group)} "
            f"ORDER BY y DESC NULLS LAST LIMIT {max_points}"
        )
        sql = f"SELECT * FROM ({top}) ORDER BY {order}"
    else:
        sql = f"SELECT {', '.join(projection)} FROM {v} GROUP BY {', '.join(group)} ORDER BY {order}"
    result = cursor.execute(sql)
    names = [d[0] for d in result.description or []]
    return {"values": [dict(zip(names, r, strict=True)) for r in result.fetchall()], "note": note}
