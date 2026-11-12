"""Chart specifications: validate, aggregate on the server, render as Vega-Lite (plan section 11).

An agent (or the user) describes a chart declaratively; no code ever runs. The format is a
small, strict subset of the grammar of graphics; any key not listed below is refused:

    {"mark": "bar" | "line" | "area" | "point" | "histogram" | "heatmap",
     "x": {"field": "region", "time_unit": "month", "bins": 20, "sort": "-y"},
     "y": {"field": "qty", "aggregate": "sum"},
     "color": {"field": "product"},
     "title": "Quantity by region"}

The server turns it into one aggregate query over the grid's current view, so the browser
receives at most `max_chart_points` points however big the table is: groups beyond the cap
keep the largest; histograms are binned in DuckDB; scatter plots over the cap are a
repeatable reservoir sample (and say so). The result becomes a Vega-Lite spec with the data
inline; the page adds the theme's colors. Series are told apart by shape or dash as well as by
color (plan section 13: never color alone).
"""

from __future__ import annotations

from typing import Any

import duckdb

from swissarmygrid.engine.datasets import Column, quote
from swissarmygrid.engine.steps import type_family

MARKS: tuple[str, ...] = ("bar", "line", "area", "point", "histogram", "heatmap")
AGGREGATES: tuple[str, ...] = ("count", "sum", "mean", "min", "max")
TIME_UNITS: tuple[str, ...] = ("day", "week", "month", "quarter", "year")
_TOP_KEYS = {"mark", "x", "y", "color", "title"}
_X_KEYS = {"field", "time_unit", "bins", "sort"}
_Y_KEYS = {"field", "aggregate"}
_COLOR_KEYS = {"field"}
_SQL_AGG = {"count": "count", "sum": "sum", "mean": "avg", "min": "min", "max": "max"}
_TIME_FORMATS = {"day": "%d %b %Y", "week": "%d %b %Y", "month": "%b %Y", "quarter": "%b %Y", "year": "%Y"}


class ChartError(ValueError):
    """The chart specification is not valid. The message names the problem."""


def _only(obj: Any, allowed: set[str], where: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ChartError(f"{where} must be an object.")
    unknown = set(obj) - allowed
    if unknown:
        raise ChartError(f"Unknown key(s) in {where}: {', '.join(sorted(unknown))}.")
    return obj


def validate(spec: Any, columns: tuple[Column, ...]) -> dict[str, Any]:
    """Return the spec if it is valid against `columns`, else raise ChartError."""
    spec = _only(spec, _TOP_KEYS, "the chart")
    mark = spec.get("mark")
    if mark not in MARKS:
        raise ChartError(f"mark must be one of {', '.join(MARKS)}.")
    by_name = {c.name: c for c in columns}

    def field(part: dict[str, Any], where: str) -> Column:
        name = part.get("field")
        if not isinstance(name, str) or name not in by_name:
            raise ChartError(f"{where}.field must be a column of the view; {name!r} is not.")
        return by_name[name]

    x = _only(spec.get("x"), _X_KEYS, "x")
    x_col = field(x, "x")
    if "time_unit" in x:
        if x["time_unit"] not in TIME_UNITS:
            raise ChartError(f"x.time_unit must be one of {', '.join(TIME_UNITS)}.")
        if type_family(x_col.type) not in ("date", "timestamp"):
            raise ChartError("x.time_unit needs a date column.")
    if "bins" in x and not (isinstance(x["bins"], int) and 2 <= x["bins"] <= 200):
        raise ChartError("x.bins must be a whole number from 2 to 200.")
    if "sort" in x and x["sort"] not in ("x", "-x", "y", "-y"):
        raise ChartError("x.sort must be x, -x, y or -y.")
    if mark == "histogram" and type_family(x_col.type) != "number":
        raise ChartError("A histogram needs a number column on x.")

    if mark != "histogram":
        y = _only(spec.get("y"), _Y_KEYS, "y")
        aggregate = y.get("aggregate")
        if mark == "point" and aggregate is None:
            y_col = field(y, "y")
            if type_family(y_col.type) != "number" or type_family(x_col.type) != "number":
                raise ChartError("A scatter plot without an aggregate needs number columns on x and y.")
        else:
            if aggregate not in AGGREGATES:
                raise ChartError(f"y.aggregate must be one of {', '.join(AGGREGATES)}.")
            if aggregate != "count":
                y_col = field(y, "y")
                if aggregate in ("sum", "mean") and type_family(y_col.type) != "number":
                    raise ChartError(f"{aggregate} needs a number column on y.")
    elif "y" in spec:
        raise ChartError("A histogram counts rows; leave y out.")
    if "color" in spec:
        field(_only(spec["color"], _COLOR_KEYS, "color"), "color")
    if mark == "heatmap" and "color" in spec:
        raise ChartError("A heatmap uses color for the value; put the second axis on y.field.")
    if mark == "heatmap":
        y = spec["y"]
        if y.get("aggregate") != "count":
            raise ChartError("A heatmap counts rows by x.field and y.field; use aggregate count.")
        field(y, "y")
    if "title" in spec and not isinstance(spec["title"], str):
        raise ChartError("title must be text.")
    return dict(spec)


def aggregate(
    cursor: duckdb.DuckDBPyConnection, view_sql: str, spec: dict[str, Any], max_points: int
) -> dict[str, Any]:
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
        low, high = cursor.execute(f"SELECT min({xf}), max({xf}) FROM {v} WHERE {where}").fetchone() or (
            None,
            None,
        )
        if low is None:
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
    x_expr = f"CAST(date_trunc('{x['time_unit']}', {xf}) AS DATE)" if "time_unit" in x else xf

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

    agg = y["aggregate"]
    y_expr = "count(*)" if agg == "count" else f"{_SQL_AGG[agg]}({quote(y['field'])})"
    group = [x_expr]
    projection = [f"{x_expr} AS x"]
    if mark == "heatmap":
        second = quote(y["field"]) if agg == "count" else None
        if second is None:
            raise ChartError("A heatmap counts rows by x.field and y.field; use aggregate count.")
        group.append(second)
        projection.append(f"{second} AS y2")
    elif color:
        group.append(quote(color["field"]))
        projection.append(f"{quote(color['field'])} AS color")
    projection.append(f"{y_expr} AS y")
    groups_row = cursor.execute(
        f"SELECT count(*) FROM (SELECT 1 FROM {v} GROUP BY {', '.join(group)})"
    ).fetchone()
    groups = int(groups_row[0]) if groups_row else 0
    order = {
        "x": "x ASC NULLS LAST",
        "-x": "x DESC NULLS LAST",
        "y": "y ASC NULLS LAST",
        "-y": "y DESC NULLS LAST",
    }
    sort = x.get("sort", "x" if mark in ("line", "area") or "time_unit" in x else "-y")
    limit = ""
    if groups > max_points:
        limit = f" LIMIT {max_points}"
        note = f"Showing the {max_points:,} largest of {groups:,} groups."
        top = (
            f"SELECT {', '.join(projection)} FROM {v} GROUP BY {', '.join(group)} "
            f"ORDER BY y DESC NULLS LAST{limit}"
        )
        sql = f"SELECT * FROM ({top}) ORDER BY {order[sort]}"
    else:
        sql = f"SELECT {', '.join(projection)} FROM {v} GROUP BY {', '.join(group)} ORDER BY {order[sort]}"
    result = cursor.execute(sql)
    names = [d[0] for d in result.description or []]
    return {"values": [dict(zip(names, r, strict=True)) for r in result.fetchall()], "note": note}


def _x_type(spec: dict[str, Any], columns: tuple[Column, ...]) -> str:
    x = spec["x"]
    if "time_unit" in x:
        return "temporal"
    family = type_family(next(c.type for c in columns if c.name == x["field"]))
    if spec["mark"] in ("line", "area", "point") and family in ("number",):
        return "quantitative"
    if family in ("date", "timestamp"):
        return "temporal"
    return "nominal"


def _headroom(values: list[dict[str, Any]]) -> dict[str, Any]:
    numbers = [v["y"] for v in values if isinstance(v.get("y"), int | float)]
    if not numbers:
        return {"nice": True}
    top, bottom = max(numbers), min(numbers)
    if top <= 0:
        return {"nice": True}
    return {"nice": True, "zero": bottom >= 0, "domainMax": top * 1.08}


def vega_lite(spec: dict[str, Any], data: dict[str, Any], columns: tuple[Column, ...]) -> dict[str, Any]:
    """A Vega-Lite v5 spec with the aggregated values inline. Colors come from the page."""
    mark = spec["mark"]
    x = spec["x"]
    title = spec.get("title")
    x_title = x["field"] + (f" ({x['time_unit']})" if "time_unit" in x else "")
    base: dict[str, Any] = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": data["values"]},
        "width": "container",
        "height": 220,
        "autosize": {"type": "fit", "contains": "padding"},
    }
    if title:
        base["title"] = title
    if mark == "histogram":
        return base | {
            "mark": {"type": "bar", "tooltip": True},
            "encoding": {
                "x": {
                    "field": "bin_start",
                    "bin": {"binned": True},
                    "type": "quantitative",
                    "title": x["field"],
                },
                "x2": {"field": "bin_end"},
                "y": {"field": "count", "type": "quantitative", "title": "rows"},
            },
        }
    y = spec.get("y", {})
    y_title = (
        "rows" if y.get("aggregate") == "count" else f"{y.get('aggregate', '')} {y.get('field', '')}".strip()
    )
    x_type = _x_type(spec, columns)
    if mark == "heatmap":
        return base | {
            "mark": {"type": "rect", "tooltip": True},
            "encoding": {
                "x": {"field": "x", "type": "nominal", "title": x_title},
                "y": {"field": "y2", "type": "nominal", "title": y.get("field")},
                "color": {"field": "y", "type": "quantitative", "title": "rows"},
            },
        }
    # Category labels are never dropped: horizontal for a few, angled for many.
    categories = len({str(v.get("x")) for v in data["values"]})
    x_axis: dict[str, Any] = {"labelLimit": 160}
    if x_type == "nominal":
        x_axis |= {"labelOverlap": False, "labelAngle": 0 if categories <= 8 else -40}
    else:
        x_axis |= {"labelOverlap": True, "tickCount": 6}
    if "time_unit" in x:
        x_axis["format"] = _TIME_FORMATS[x["time_unit"]]
    encoding: dict[str, Any] = {
        "x": {"field": "x", "type": x_type, "title": x_title, "sort": None, "axis": x_axis},
        "y": {
            "field": "y",
            "type": "quantitative",
            "title": y_title if y.get("aggregate") else y.get("field"),
            # Headroom above the highest value, so a line never sits on the plot's top edge;
            # zero stays the floor (a pixel padding would push the axis below zero).
            "scale": _headroom(data["values"]),
        },
    }
    vl_mark: dict[str, Any] = {"type": mark, "tooltip": True}
    if mark in ("line", "area"):
        vl_mark["point"] = True
    if "color" in spec:
        series = {"field": "color", "type": "nominal", "title": spec["color"]["field"]}
        encoding["color"] = series
        if mark == "line":
            encoding["strokeDash"] = series  # never color alone
        if mark == "point":
            encoding["shape"] = series
        if mark == "bar":
            encoding["xOffset"] = {"field": "color", "scale": {"paddingInner": 0.06}}
            encoding["x"]["scale"] = {"paddingInner": 0.14}
            # A dash pattern on each bar's outline (and in the legend), so series differ by
            # more than hue (plan section 13).
            encoding["strokeDash"] = series
    return base | {"mark": vl_mark, "encoding": encoding}


def altair_code(spec: dict[str, Any], columns: tuple[Column, ...], dataset: str) -> str:
    """The same chart as Altair code over the grid's output DataFrame (no data embedded)."""
    mark = spec["mark"]
    x = spec["x"]
    lines = [
        f"# Generated by SwissArmyGrid: the chart over dataset '{dataset}'.",
        "# Pass the grid's output (see Copy as Python for the transform).",
        "import altair as alt",
        "import pandas as pd",
        "",
        "",
        "def chart(df: pd.DataFrame) -> alt.Chart:",
    ]
    if mark == "histogram":
        enc = f'x=alt.X("{x["field"]}:Q", bin=alt.Bin(maxbins={x.get("bins", 20)})), y="count()"'
        lines.append(f"    return alt.Chart(df).mark_bar().encode({enc})")
        return "\n".join(lines) + "\n"
    y = spec.get("y", {})
    letter = {"nominal": "N", "quantitative": "Q", "temporal": "T"}[_x_type(spec, columns)]
    x_enc = f'"{x["time_unit"]}({x["field"]}):T"' if "time_unit" in x else f'"{x["field"]}:{letter}"'
    if mark == "heatmap":
        enc = f'x={x_enc}, y="{y["field"]}:N", color="count():Q"'
        lines.append(f"    return alt.Chart(df).mark_rect().encode({enc})")
        return "\n".join(lines) + "\n"
    agg = y.get("aggregate")
    y_enc = '"count()"' if agg == "count" else (f'"{agg}({y["field"]}):Q"' if agg else f'"{y["field"]}:Q"')
    enc = f"x={x_enc}, y={y_enc}"
    if "color" in spec:
        c = spec["color"]["field"]
        enc += f', color="{c}:N"'
        if mark == "line":
            enc += f', strokeDash="{c}:N"'
        if mark == "point":
            enc += f', shape="{c}:N"'
    lines.append(f"    return alt.Chart(df).mark_{mark}(tooltip=True).encode({enc})")
    return "\n".join(lines) + "\n"
