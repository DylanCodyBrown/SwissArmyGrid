"""Compare's diff mode (Plans/grid-tabs-split-compare.plan.md): two grids lined up as in outer mode,
showing only the rows that differ, a changed cell in a column both have, or a key on one side only,
with a count per column.

How two cells are compared is the user's (`DiffOptions`): numbers equal within a tolerance (an amount,
or a percentage of the left value); text with or without trimming and letter case; a blank equals a
blank (an empty text is a blank); columns can be left out. The same rule runs twice: as SQL, to pick
the rows and count them, and in Python, to mark the cells of the rows on screen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from swissarmygrid.domain.datasets import Column, quote
from swissarmygrid.domain.grids import POS
from swissarmygrid.domain.steps import type_family

MAX_TOLERANCE: float = 1e15


@dataclass(frozen=True)
class DiffOptions:
    tolerance: float = 0.0
    percent: bool = False  # the tolerance is a percentage of the left value
    trim: bool = True
    ignore_case: bool = False
    skip: tuple[str, ...] = ()  # left column names left out of the comparison
    column: str | None = None  # only the rows where this (left) column changed

    @staticmethod
    def from_json(raw: Any) -> DiffOptions:
        raw = raw if isinstance(raw, dict) else {}
        tolerance = raw.get("tolerance", 0)
        if (
            isinstance(tolerance, bool)
            or not isinstance(tolerance, int | float)
            or not 0 <= tolerance < MAX_TOLERANCE
        ):
            raise ValueError("The diff's tolerance is a number, 0 or more.")
        skip = raw.get("skip") or []
        if not isinstance(skip, list) or not all(isinstance(s, str) for s in skip):
            raise ValueError("The diff's skip is a list of column names.")
        column = raw.get("column")
        if column is not None and not isinstance(column, str):
            raise ValueError("The diff's column is one column's name.")
        return DiffOptions(
            float(tolerance),
            bool(raw.get("percent", False)),
            bool(raw.get("trim", True)),
            bool(raw.get("ignore_case", False)),
            tuple(dict.fromkeys(skip)),
            column or None,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "tolerance": self.tolerance,
            "percent": self.percent,
            "trim": self.trim,
            "ignore_case": self.ignore_case,
            "skip": list(self.skip),
            "column": self.column,
        }


@dataclass(frozen=True)
class Pair:
    """A column both grids have: its index and column on each side, and whether both are numbers."""

    left_index: int
    right_index: int
    left: Column
    right: Column
    numeric: bool


def pairs(
    left: tuple[Column, ...],
    right: tuple[Column, ...],
    keys: tuple[tuple[str, str], ...],
    options: DiffOptions,
) -> list[Pair]:
    """The columns compared: same name ignoring case on both sides, not a key, not left out."""
    keyed = {k[0].casefold() for k in keys} | {k[1].casefold() for k in keys}
    skipped = {s.casefold() for s in options.skip}
    right_at = {c.name.casefold(): (i, c) for i, c in enumerate(right)}
    found = []
    for i, column in enumerate(left):
        name = column.name.casefold()
        if name in right_at and name not in keyed and name not in skipped:
            j, other = right_at[name]
            numeric = type_family(column.type) == "number" and type_family(other.type) == "number"
            found.append(Pair(i, j, column, other, numeric))
    return found


def _text(sql: str, options: DiffOptions) -> str:
    text = f"CAST({sql} AS VARCHAR)"
    if options.trim:
        text = f"trim({text}, ' ' || chr(9) || chr(13) || chr(10))"
    if options.ignore_case:
        text = f"lower({text})"
    return f"NULLIF({text}, '')"


def differs_sql(pair: Pair, options: DiffOptions) -> str:
    """True when the pair's cells differ, on a matched row (L and R are the two sides' rows)."""
    left, right = f"L.{quote(pair.left.name)}", f"R.{quote(pair.right.name)}"
    if not pair.numeric:
        return f"({_text(left, options)} IS DISTINCT FROM {_text(right, options)})"
    a, b = f"CAST({left} AS DOUBLE)", f"CAST({right} AS DOUBLE)"
    allowed = f"{options.tolerance!r} * abs({a}) / 100" if options.percent else repr(options.tolerance)
    # Within the tolerance, or a hair's width (0.1 + 0.2 and 0.3); two NaNs are one value, as in DuckDB.
    close = f"abs({a} - {b}) <= {allowed} + 1e-9 * greatest(1, abs({a}))"
    return (
        f"(CASE WHEN {a} IS NULL OR {b} IS NULL THEN ({a} IS NULL) <> ({b} IS NULL) "
        f"WHEN isnan({a}) OR isnan({b}) THEN NOT (isnan({a}) AND isnan({b})) "
        f"WHEN isinf({a}) OR isinf({b}) THEN {a} <> {b} "
        f"ELSE NOT ({close}) END)"
    )


def diffed_sql(
    mapping: str, left_positioned: str, right_positioned: str, compared: list[Pair], options: DiffOptions
) -> str:
    """The outer mapping (lp, rp, s...) with d0, d1, ...: whether each compared column differs."""
    flags = [
        f"CASE WHEN m.lp IS NULL OR m.rp IS NULL THEN FALSE ELSE {differs_sql(p, options)} END AS d{i}"
        for i, p in enumerate(compared)
    ]
    return (
        f"SELECT m.*{''.join(', ' + f for f in flags)} FROM ({mapping}) AS m "
        f"LEFT JOIN ({left_positioned}) AS L ON L.{POS} = m.lp "
        f"LEFT JOIN ({right_positioned}) AS R ON R.{POS} = m.rp"
    )


def summary_sql(diffed: str, compared: list[Pair]) -> str:
    """matched, differ (matched rows with a change), left_only, right_only, then each column's changes."""
    changed = " OR ".join(f"d{i}" for i in range(len(compared))) or "FALSE"
    per_column = "".join(f", count(*) FILTER (WHERE d{i})" for i in range(len(compared)))
    return (
        "SELECT count(*) FILTER (WHERE lp IS NOT NULL AND rp IS NOT NULL), "
        f"count(*) FILTER (WHERE lp IS NOT NULL AND rp IS NOT NULL AND ({changed})), "
        "count(*) FILTER (WHERE rp IS NULL), count(*) FILTER (WHERE lp IS NULL)"
        f"{per_column} FROM ({diffed})"
    )


def kept_sql(diffed: str, keys: int, compared: list[Pair], options: DiffOptions, order: str) -> str:
    """The rows diff mode shows, in compare order: those that differ, or where options.column changed."""
    if options.column is not None:
        at = next(
            (i for i, p in enumerate(compared) if p.left.name.casefold() == options.column.casefold()), None
        )
        if at is None:
            raise ValueError(f"{options.column!r} is not a column both grids have (and not a key).")
        where = f"d{at}"
    else:
        changed = "".join(f" OR d{i}" for i in range(len(compared)))
        where = f"lp IS NULL OR rp IS NULL{changed}"
    sort = ", ".join(f"s{i}" for i in range(keys))
    return f"SELECT lp, rp, {sort} FROM ({diffed}) WHERE {where} ORDER BY {order}"


def summary(row: tuple[Any, ...], compared: list[Pair]) -> dict[str, Any]:
    matched, differ, left_only, right_only = (int(v or 0) for v in row[:4])
    columns = {p.left.name: int(row[4 + i] or 0) for i, p in enumerate(compared)}
    return {
        "matched": matched,
        "differ": differ,
        "left_only": left_only,
        "right_only": right_only,
        "columns": {k: v for k, v in sorted(columns.items(), key=lambda kv: -kv[1]) if v},
        "compared": [p.left.name for p in compared],
    }


def _number(value: Any) -> bool:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)


def _clean(value: Any, options: DiffOptions) -> str | None:
    if value is None:
        return None
    text = str(value)
    if options.trim:
        text = text.strip(" \t\r\n")
    if options.ignore_case:
        text = text.lower()
    return text or None


def same(a: Any, b: Any, numeric: bool, options: DiffOptions) -> bool:
    """The Python twin of differs_sql, for marking the cells on screen."""
    if numeric and _number(a) and _number(b):
        x, y = float(a), float(b)
        if math.isnan(x) or math.isnan(y):
            return math.isnan(x) and math.isnan(y)
        if math.isinf(x) or math.isinf(y):
            return x == y
        allowed = options.tolerance * abs(x) / 100 if options.percent else options.tolerance
        return abs(x - y) <= allowed + 1e-9 * max(1.0, abs(x))
    if numeric:
        return (a is None) == (b is None) if a is None or b is None else str(a) == str(b)
    return _clean(a, options) == _clean(b, options)


def cells(
    left_rows: list[list[Any] | None],
    right_rows: list[list[Any] | None],
    compared: list[Pair],
    options: DiffOptions,
) -> tuple[list[list[int]], list[list[int]]]:
    """The cells that differ in aligned rows: [[row offset, column index], ...] for each side."""
    left_diff: list[list[int]] = []
    right_diff: list[list[int]] = []
    for offset, (lrow, rrow) in enumerate(zip(left_rows, right_rows, strict=True)):
        if lrow is None or rrow is None:
            continue
        for p in compared:
            if not same(lrow[p.left_index], rrow[p.right_index], p.numeric, options):
                left_diff.append([offset, p.left_index])
                right_diff.append([offset, p.right_index])
    return left_diff, right_diff
