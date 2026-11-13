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

This file is the specification: what a chart may say, the one query it becomes, and the Vega-Lite
and Altair it is drawn with. Running the query is the service layer's (services/charts.py).
"""

from __future__ import annotations

import math
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from swissarmygrid.domain.codegen import python_string, variable_name
from swissarmygrid.domain.datasets import Column, quote
from swissarmygrid.domain.steps import type_family

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


_ORDER = {
    "x": "x ASC NULLS LAST",
    "-x": "x DESC NULLS LAST",
    "y": "y ASC NULLS LAST",
    "-y": "y DESC NULLS LAST",
}


def _x_expression(x: dict[str, Any]) -> str:
    xf = quote(x["field"])
    return f"CAST(date_trunc('{x['time_unit']}', {xf}) AS DATE)" if "time_unit" in x else xf


def _grouped(spec: dict[str, Any]) -> tuple[list[str], list[str], str]:
    """An aggregated chart's select list, GROUP BY list and final ORDER BY."""
    x, y, color = spec["x"], spec["y"], spec.get("color")
    agg = y["aggregate"]
    x_expr = _x_expression(x)
    y_expr = "count(*)" if agg == "count" else f"{_SQL_AGG[agg]}({quote(y['field'])})"
    group = [x_expr]
    projection = [f"{x_expr} AS x"]
    if spec["mark"] == "heatmap":
        if agg != "count":
            raise ChartError("A heatmap counts rows by x.field and y.field; use aggregate count.")
        group.append(quote(y["field"]))
        projection.append(f"{quote(y['field'])} AS y2")
    elif color:
        group.append(quote(color["field"]))
        projection.append(f"{quote(color['field'])} AS color")
    projection.append(f"{y_expr} AS y")
    sort = x.get("sort", "x" if spec["mark"] in ("line", "area") or "time_unit" in x else "-y")
    return projection, group, _ORDER[sort]


def chart_query(spec: dict[str, Any], source: str, max_points: int) -> str:
    """The chart's points from `source` (a table name) as ONE DuckDB query, for the copied code.
    The same points aggregate() gives; it runs its own count first only to word its note."""
    x = spec["x"]
    xf = quote(x["field"])
    if spec["mark"] == "histogram":
        bins = x.get("bins", 20)
        return (
            f"WITH v AS (\n    SELECT CAST({xf} AS DOUBLE) AS value FROM {source}\n"
            f"    WHERE {xf} IS NOT NULL AND isfinite(CAST({xf} AS DOUBLE))\n),\n"
            "r AS (\n    SELECT min(value) AS low,\n"
            "    CASE WHEN max(value) > min(value)\n"
            f"    THEN max(value) / {bins} - min(value) / {bins} ELSE 1.0 END AS width\n    FROM v\n),\n"
            "b AS (\n    SELECT\n"
            f"    greatest(0, least(CAST(floor(value / width - low / width) AS BIGINT), {bins - 1}))\n"
            "    AS bin,\n"
            "    count(*) AS count\n    FROM v, r GROUP BY 1\n)\n"
            "SELECT low + bin * width AS bin_start, low + (bin + 1) * width AS bin_end, count\n"
            "FROM b, r\nORDER BY bin"
        )
    y = spec["y"]
    color = spec.get("color")
    if spec["mark"] == "point" and y.get("aggregate") is None:
        yf = quote(y["field"])
        select = f"{xf} AS x, {yf} AS y" + (f", {quote(color['field'])} AS color" if color else "")
        return (
            f"SELECT * FROM (\n    SELECT {select} FROM {source}\n"
            f"    WHERE {xf} IS NOT NULL AND {yf} IS NOT NULL\n)\n"
            f"USING SAMPLE reservoir({max_points} ROWS) REPEATABLE (42)"
        )
    projection, group, order = _grouped(spec)
    return (
        f"SELECT * FROM (\n    SELECT {', '.join(projection)}\n    FROM {source}\n"
        f"    GROUP BY {', '.join(group)}\n    ORDER BY y DESC NULLS LAST\n    LIMIT {max_points}\n)\n"
        f"ORDER BY {order}"
    )


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
            # zero stays the floor (a pixel padding would push the axis below zero). A blank x
            # has no place on a date or number axis, so its value does not stretch the axis.
            "scale": _headroom(
                data["values"]
                if x_type == "nominal"
                else [v for v in data["values"] if v.get("x") is not None]
            ),
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


def altair_code(spec: dict[str, Any], columns: tuple[Column, ...], dataset: str, max_points: int) -> str:
    """The same chart as Altair code over the grid's output DataFrame (no data embedded).

    DuckDB turns the frame into the chart's points first, with the query the grid uses, so the
    chart holds at most `max_points` rows (Altair refuses a frame over 5,000 rows) and Altair
    only encodes plain columns named x, y, y2, color, bin_start and bin_end. Column names reach
    the code only inside that SQL, quoted, and every title is a Python string literal, so a
    name with a colon, a dot or a quote (which Altair's shorthand reads as syntax) still works.
    """
    mark = spec["mark"]
    x = spec["x"]
    y = spec.get("y", {})
    variable = variable_name(dataset)
    query = chart_query(spec, "df", max_points)
    title = spec.get("title")
    x_title = x["field"] + (f" ({x['time_unit']})" if "time_unit" in x else "")
    if mark == "histogram":
        vl_mark = "mark_bar(tooltip=True)"
        encodings = [
            f'x=alt.X("bin_start", type="quantitative", bin="binned", title={x["field"]!r})',
            'x2=alt.X2("bin_end")',
            'y=alt.Y("count", type="quantitative", title="rows")',
        ]
    elif mark == "heatmap":
        vl_mark = "mark_rect(tooltip=True)"
        encodings = [
            f'x=alt.X("x", type="nominal", title={x_title!r})',
            f'y=alt.Y("y2", type="nominal", title={y["field"]!r})',
            'color=alt.Color("y", type="quantitative", title="rows")',
        ]
    else:
        vl_mark = f"mark_{mark}(tooltip=True{', point=True' if mark in ('line', 'area') else ''})"
        y_title = (
            "rows"
            if y.get("aggregate") == "count"
            else f"{y.get('aggregate', '')} {y.get('field', '')}".strip()
        )
        axis = f", axis=alt.Axis(format={_TIME_FORMATS[x['time_unit']]!r})" if "time_unit" in x else ""
        encodings = [
            f'x=alt.X("x", type={_x_type(spec, columns)!r}, title={x_title!r}, sort=None{axis})',
            f'y=alt.Y("y", type="quantitative", title={y_title if y.get("aggregate") else y["field"]!r})',
        ]
        if "color" in spec:
            series = f'"color", type="nominal", title={spec["color"]["field"]!r}'
            encodings.append(f"color=alt.Color({series})")
            if mark == "line":
                encodings.append(f"strokeDash=alt.StrokeDash({series})")  # never color alone
            if mark == "point":
                encodings.append(f"shape=alt.Shape({series})")
            if mark == "bar":
                encodings.append('xOffset=alt.XOffset("color", type="nominal")')
    chart = f"alt.Chart(data{f', title={title!r}' if title else ''})"
    lines = [
        f"# Generated by SwissArmyGrid: the chart over dataset {dataset!r}.",
        f"# chart({variable}_view) draws it from the grid's output (Copy as Python gives {variable}_view).",
        "# DuckDB aggregates the rows first, as the grid does, so the chart holds at most "
        f"{max_points:,} points.",
        "import altair as alt",
        "import duckdb",
        "import pandas as pd",
        "",
        "",
        "def chart(df: pd.DataFrame) -> alt.Chart:",
        "    con = duckdb.connect()",
        '    con.register("df", df)',
        f"    data = con.sql({python_string(query)}).df()",
        f"    return {chart}.{vl_mark}.encode(",
        *(f"        {e}," for e in encodings),
        "    )",
    ]
    return "\n".join(lines) + "\n"


# ===== data-bound figures =====
# A Plotly or ECharts card on a grid may carry a SELECT over the grid's view. The server runs it and puts
# the result in the figure, so the data never passes through whoever wrote the figure (an agent, say):
# ECharts takes it as `dataset.source` (its header row first), and a Plotly trace field that names a
# column ("x": "region") gets that column's values.

PLOTLY_COLUMN_FIELDS: tuple[str, ...] = (
    "x",
    "y",
    "z",
    "text",
    "labels",
    "values",
    "ids",
    "parents",
    "customdata",
    "open",
    "high",
    "low",
    "close",
    "base",
    "lat",
    "lon",
    "r",
    "theta",
)


def _json_cell(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date | datetime | time):
        return value.isoformat()
    return str(value)


def fill_figure(
    kind: str, figure: dict[str, Any], columns: list[str], rows: list[tuple[Any, ...]]
) -> dict[str, Any]:
    """The figure with the query's result in it. Raises ChartError when a Plotly trace names no column."""
    cells = [[_json_cell(v) for v in row] for row in rows]
    if kind == "echarts":
        return {**figure, "dataset": {"source": [columns, *cells]}}
    by_name = {name: [row[i] for row in cells] for i, name in enumerate(columns)}
    traces = figure.get("data")
    if not isinstance(traces, list):
        raise ChartError('A Plotly figure is {"data": [traces], "layout": {...}}.')
    filled = []
    for trace in traces:
        if not isinstance(trace, dict):
            raise ChartError("Each Plotly trace is an object.")
        out = dict(trace)
        for field in PLOTLY_COLUMN_FIELDS:
            if isinstance(trace.get(field), str):
                if trace[field] not in by_name:
                    raise ChartError(
                        f"Trace field {field} names {trace[field]!r}, not a column of the query."
                    )
                out[field] = by_name[trace[field]]
        marker = trace.get("marker")
        if isinstance(marker, dict):
            out["marker"] = {
                k: by_name[v] if k in ("color", "size") and isinstance(v, str) and v in by_name else v
                for k, v in marker.items()
            }
        filled.append(out)
    return {**figure, "data": filled}
