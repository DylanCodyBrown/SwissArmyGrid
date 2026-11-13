"""The tie-out report (Plans/grid-tabs-split-compare.plan.md): do two tables add up the same, group by
group? The left and right grids of a compare are each totalled by the same columns, and the totals
are set side by side: rows, and the sum of each measure, left, right, the difference, and whether the
group **ties** (within a tolerance) or is **off**.

Columns are matched by name, ignoring letter case, as the compare's own shared columns are. Group
values meet the way compare keys do (domain/relationships.normalized: 42, 42.0 and '42' are one
group; text trimmed), and a blank group meets a blank group. A group on one side only is off.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from swissarmygrid.domain.datasets import Column, quote
from swissarmygrid.domain.relationships import KeyOptions, normalized
from swissarmygrid.domain.steps import type_family

MAX_GROUPS: int = 10_000
MAX_GROUP_COLUMNS: int = 4
MAX_MEASURES: int = 12
SUGGESTED_GROUP_VALUES: int = 50  # a shared text column with at most this many values is suggested


class TieOutError(ValueError):
    """A tie-out that can't be run as asked (the message says why)."""


@dataclass(frozen=True)
class Pair:
    """One column as it is named on each side."""

    left: Column
    right: Column

    @property
    def name(self) -> str:
        return self.left.name


@dataclass(frozen=True)
class Request:
    groups: tuple[Pair, ...]
    measures: tuple[Pair, ...]
    tolerance: float
    percent: bool  # the tolerance is a percentage of the left total


def shared(left: tuple[Column, ...], right: tuple[Column, ...]) -> list[Pair]:
    """The columns both sides have, by name ignoring case, in the left's order."""
    by_name = {c.name.casefold(): c for c in right}
    return [Pair(c, by_name[c.name.casefold()]) for c in left if c.name.casefold() in by_name]


def numeric(pair: Pair) -> bool:
    return type_family(pair.left.type) == "number" and type_family(pair.right.type) == "number"


def groupable(pair: Pair) -> bool:
    options = KeyOptions()
    return normalized(pair.left, options) is not None and normalized(pair.right, options) is not None


def read_request(body: Any, pairs: list[Pair]) -> Request:
    """{"group": [names], "measures": [names], "tolerance": number, "tolerance_kind": absolute | percent}."""
    if not isinstance(body, dict):
        raise TieOutError('Send {"group": [columns], "measures": [number columns], "tolerance": 0}.')
    by_name = {p.name.casefold(): p for p in pairs}

    def pick(key: str, limit: int) -> tuple[Pair, ...]:
        names = body.get(key) or []
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise TieOutError(f"{key} is a list of column names.")
        if len(names) > limit:
            raise TieOutError(f"At most {limit} {key} columns.")
        found = []
        for name in dict.fromkeys(names):
            pair = by_name.get(name.casefold())
            if pair is None:
                raise TieOutError(f"{name!r} is not a column both tables have.")
            found.append(pair)
        return tuple(found)

    groups, measures = pick("group", MAX_GROUP_COLUMNS), pick("measures", MAX_MEASURES)
    for pair in groups:
        if not groupable(pair):
            raise TieOutError(f"{pair.name} can't be grouped by ({pair.left.type}).")
    for pair in measures:
        if not numeric(pair):
            raise TieOutError(f"{pair.name} is not a number on both sides, so it has no total.")
    tolerance = body.get("tolerance", 0)
    if isinstance(tolerance, bool) or not isinstance(tolerance, int | float) or not 0 <= tolerance < 1e15:
        raise TieOutError("tolerance is a number, 0 or more.")
    kind = body.get("tolerance_kind", "absolute")
    if kind not in ("absolute", "percent"):
        raise TieOutError("tolerance_kind is absolute or percent.")
    return Request(groups, measures, float(tolerance), kind == "percent")


def suggest(pairs: list[Pair], few_values: set[str]) -> dict[str, list[str]]:
    """What the window starts with: shared text columns with few values (`few_values`, measured by the
    service) to group by, and every shared number column to total."""
    text = [p.name for p in pairs if type_family(p.left.type) == "text" and p.name in few_values]
    return {"group": text[:1], "measures": [p.name for p in pairs if numeric(p)]}


def distinct_query(view: str, column: Column) -> str:
    """How many values a column has (a few-valued text column is suggested to group by)."""
    return f"SELECT count(DISTINCT {quote(column.name)}) FROM ({view})"


def _side(view: str, columns: list[Column], measures: list[Column]) -> str:
    """A side totalled per group: g (the group's comparable value), v (as it reads), n, s (each sum)."""
    options = KeyOptions()
    keyed = ", ".join(f"{normalized(c, options)} AS __sag_g{i}" for i, c in enumerate(columns))
    select = [
        *(f"__sag_g{i} AS g{i}" for i in range(len(columns))),
        *(f"min({quote(c.name)}) AS v{i}" for i, c in enumerate(columns)),
        "count(*) AS n",
        *(f"sum({quote(c.name)}) AS s{i}" for i, c in enumerate(measures)),
    ]
    source = f"(SELECT {keyed}, * FROM ({view}))" if columns else f"({view})"
    by = f" GROUP BY {', '.join(f'__sag_g{i}' for i in range(len(columns)))}" if columns else ""
    return f"SELECT {', '.join(select)} FROM {source}{by}"


def query(left_view: str, right_view: str, request: Request) -> str:
    """One row per group: the group's values (as the left reads them, else the right), rows and sums
    on each side. A group on one side only has blanks on the other."""
    n, m = len(request.groups), len(request.measures)
    left = _side(left_view, [p.left for p in request.groups], [p.left for p in request.measures])
    right = _side(right_view, [p.right for p in request.groups], [p.right for p in request.measures])
    on = " AND ".join(f"l.g{i} IS NOT DISTINCT FROM r.g{i}" for i in range(n)) or "TRUE"
    values = [f"coalesce(CAST(l.v{i} AS VARCHAR), CAST(r.v{i} AS VARCHAR)) AS v{i}" for i in range(n)]
    totals = [f"l.s{i} AS ls{i}, r.s{i} AS rs{i}" for i in range(m)]
    select = ", ".join([*values, "l.n AS ln", "r.n AS rn", *totals])
    order = ", ".join(f"v{i} NULLS LAST" for i in range(n)) or "1"
    return (
        f"SELECT {select} FROM ({left}) AS l FULL OUTER JOIN ({right}) AS r ON {on} "
        f"ORDER BY {order} LIMIT {MAX_GROUPS + 1}"
    )


def _number(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _within(left: float | None, right: float | None, request: Request) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    allowed = request.tolerance * abs(left) / 100 if request.percent else request.tolerance
    return abs(right - left) <= allowed + 1e-9 * max(1.0, abs(left))


def report(rows: list[tuple[Any, ...]], request: Request) -> dict[str, Any]:
    """The report: groups that are off first (largest first), then those that tie, and a grand total."""
    n, m = len(request.groups), len(request.measures)
    capped = len(rows) > MAX_GROUPS
    groups: list[dict[str, Any]] = []
    total_left_rows = total_right_rows = 0
    sums = [[0.0, 0.0] for _ in range(m)]
    for row in rows[:MAX_GROUPS]:
        values = list(row[:n])
        left_rows, right_rows = row[n], row[n + 1]
        both = left_rows is not None and right_rows is not None
        measures = []
        for i in range(m):
            left, right = _number(row[n + 2 + 2 * i]), _number(row[n + 3 + 2 * i])
            sums[i][0] += left or 0.0
            sums[i][1] += right or 0.0
            # A group on one side only: its whole total is the difference (the other side has none).
            difference = None if left is None and right is None else (right or 0.0) - (left or 0.0)
            measures.append(
                {
                    "column": request.measures[i].name,
                    "left": left,
                    "right": right,
                    "difference": difference,
                    "difference_share": difference / abs(left) if difference is not None and left else None,
                    "ties": both and _within(left, right, request),
                }
            )
        total_left_rows += left_rows or 0
        total_right_rows += right_rows or 0
        ties = both and left_rows == right_rows and all(x["ties"] for x in measures)
        groups.append(
            {
                "group": values,
                "left_rows": left_rows,
                "right_rows": right_rows,
                "row_difference": (right_rows or 0) - (left_rows or 0),
                "measures": measures,
                "status": "ties" if ties else "off",
                "only": None if both else ("left" if right_rows is None else "right"),
            }
        )

    def size(g: dict[str, Any]) -> float:
        return float(max([abs(g["row_difference"]), *(abs(x["difference"] or 0) for x in g["measures"])]))

    groups.sort(key=lambda g: (g["status"] == "ties", -size(g)))
    total_measures = [
        {
            "column": request.measures[i].name,
            "left": sums[i][0],
            "right": sums[i][1],
            "difference": sums[i][1] - sums[i][0],
            "difference_share": (sums[i][1] - sums[i][0]) / abs(sums[i][0]) if sums[i][0] else None,
            "ties": _within(sums[i][0], sums[i][1], request),
        }
        for i in range(m)
    ]
    off = sum(1 for g in groups if g["status"] == "off")
    total_ties = total_left_rows == total_right_rows and all(x["ties"] for x in total_measures)
    return {
        "group_by": [p.name for p in request.groups],
        "measures": [p.name for p in request.measures],
        "tolerance": request.tolerance,
        "tolerance_kind": "percent" if request.percent else "absolute",
        "groups": groups,
        "total": {
            "left_rows": total_left_rows,
            "right_rows": total_right_rows,
            "row_difference": total_right_rows - total_left_rows,
            "measures": total_measures,
            "status": "ties" if total_ties else "off",
        },
        "off": off,
        "capped": capped,
        "words": words(groups, total_ties, off, capped),
    }


def words(groups: list[dict[str, Any]], total_ties: bool, off: int, capped: bool) -> str:
    count = len(groups)
    if not off:
        return f"Ties: all {count:,} group{'s' if count != 1 else ''} match." if count > 1 else "Ties."
    lead = f"{off:,} of {count:,} group{'s' if count != 1 else ''} {'is' if off == 1 else 'are'} off"
    end = "; the grand total ties (the differences cancel out)" if total_ties else ""
    return f"{lead}{end}." + (f" Showing the first {MAX_GROUPS:,} groups." if capped else "")
