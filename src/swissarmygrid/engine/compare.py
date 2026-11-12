"""Compare two grids row by row on key columns (plan: Plans/grid-tabs-split-compare.plan.md, T3).

The rule: the two grids are always aligned. Compare row N is one left-view row and one right-view
row with the same key, or one of them with a blank placeholder beside it, so a person reads straight
across and sees every match. Both panes read the same mapping, so row N lines up on both sides.

Each side is its grid's view (after its own steps), as a positioned relation (__sag_pos = the
row's place in that view). The mapping is one full outer join on the keys, compared the way the
Key Finder compares them (engine/keyfinder.normalized: 42, 42.0 and '42' meet; text trimmed), and
sorted by key: number-looking keys as numbers (9 before 10), then as text, blanks last.

    mapping row:  left position (or NULL)  |  right position (or NULL)

Modes:
  outer  every key from both sides; duplicate keys pair every row with every row, as SQL does
  anti   only the rows whose key has no match: on both sides, or the left or the right only
  cross  no mapping: each side shows its own view, and a row picked on one side filters the
         other side to the rows with that key

A blank key never matches anything (SQL), so its rows show as unmatched on their own side.
"""

# Every query here is built from quote()d names, the grids' own compiled view SQL and integers;
# no user text is spliced in, so the "string-built SQL" check is off for this file.
# ruff: noqa: S608

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import duckdb

from swissarmygrid.engine.datasets import Column
from swissarmygrid.engine.keyfinder import KeyOptions, normalized
from swissarmygrid.engine.views import POS

MODES: tuple[str, ...] = ("outer", "anti", "cross")
ANTI_SIDES: tuple[str, ...] = ("both", "left", "right")
MAX_KEYS: int = 8


@dataclass(frozen=True)
class CompareSpec:
    keys: tuple[tuple[str, str], ...]  # (left column, right column) per key part
    mode: str = "outer"
    anti_side: str = "both"

    @staticmethod
    def from_json(raw: Any) -> CompareSpec:
        if not isinstance(raw, dict):
            raise ValueError('A compare is {"keys": [[left, right], ...], "mode": "outer"}.')
        keys = raw.get("keys")
        if (
            not isinstance(keys, list)
            or not 1 <= len(keys) <= MAX_KEYS
            or not all(
                isinstance(k, list) and len(k) == 2 and all(isinstance(n, str) for n in k) for k in keys
            )
        ):
            raise ValueError(f"keys must be 1 to {MAX_KEYS} [left column, right column] pairs.")
        mode = raw.get("mode", "outer")
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}.")
        side = raw.get("anti_side", "both")
        if side not in ANTI_SIDES:
            raise ValueError(f"anti_side must be one of {', '.join(ANTI_SIDES)}.")
        return CompareSpec(tuple((k[0], k[1]) for k in keys), mode, side)

    def to_json(self) -> dict[str, Any]:
        return {"keys": [list(k) for k in self.keys], "mode": self.mode, "anti_side": self.anti_side}


def _key_expressions(columns: tuple[Column, ...], names: list[str], side: str) -> list[str]:
    by_name = {c.name: c for c in columns}
    out: list[str] = []
    for name in names:
        column = by_name.get(name)
        if column is None:
            raise ValueError(f"{name!r} is not a column of the {side} grid's view.")
        sql = normalized(column, KeyOptions())
        if sql is None:
            raise ValueError(f"{name!r} ({column.type}) can't be a key.")
        out.append(sql)
    return out


def keyed_sql(positioned: str, columns: tuple[Column, ...], names: list[str], side: str) -> str:
    """A side as (__sag_pos, k0, k1, ...): its position and its normalized key values."""
    keys = _key_expressions(columns, names, side)
    parts = ", ".join(f"{sql} AS k{i}" for i, sql in enumerate(keys))
    return f"SELECT {POS}, {parts} FROM ({positioned})"


def _joined(left: str, right: str, n: int) -> str:
    on = " AND ".join(f"l.k{i} = r.k{i}" for i in range(n))
    sort = ", ".join(f"coalesce(l.k{i}, r.k{i}) AS s{i}" for i in range(n))
    return (
        f"SELECT l.{POS} AS lp, r.{POS} AS rp, {sort} "
        f"FROM ({left}) AS l FULL OUTER JOIN ({right}) AS r ON {on}"
    )


def mapping_sql(
    left_positioned: str,
    left_columns: tuple[Column, ...],
    right_positioned: str,
    right_columns: tuple[Column, ...],
    spec: CompareSpec,
) -> str:
    """The aligned mapping (lp, rp, s0, s1, ...) in compare order, for outer and anti mode. The s
    columns are the row's normalized key, so the rows sharing a key can be found again."""
    left = keyed_sql(left_positioned, left_columns, [k[0] for k in spec.keys], "left")
    right = keyed_sql(right_positioned, right_columns, [k[1] for k in spec.keys], "right")
    n = len(spec.keys)
    where = ""
    if spec.mode == "anti":
        where = {
            "both": "WHERE lp IS NULL OR rp IS NULL",
            "left": "WHERE rp IS NULL",
            "right": "WHERE lp IS NULL",
        }[spec.anti_side]
    order = ", ".join(f"TRY_CAST(s{i} AS DOUBLE) NULLS LAST, s{i} NULLS LAST" for i in range(n))
    return (
        f"SELECT lp, rp, {', '.join(f's{i}' for i in range(n))} FROM ({_joined(left, right, n)}) {where} "
        f"ORDER BY {order}, lp NULLS LAST, rp NULLS LAST"
    )


def counts(
    cursor: duckdb.DuckDBPyConnection,
    left_positioned: str,
    left_columns: tuple[Column, ...],
    right_positioned: str,
    right_columns: tuple[Column, ...],
    spec: CompareSpec,
) -> dict[str, int]:
    """Matched pairs, rows only on the left, rows only on the right (whatever the mode)."""
    left = keyed_sql(left_positioned, left_columns, [k[0] for k in spec.keys], "left")
    right = keyed_sql(right_positioned, right_columns, [k[1] for k in spec.keys], "right")
    row = cursor.execute(
        "SELECT count(*) FILTER (WHERE lp IS NOT NULL AND rp IS NOT NULL), "
        "count(*) FILTER (WHERE rp IS NULL), count(*) FILTER (WHERE lp IS NULL) "
        f"FROM ({_joined(left, right, len(spec.keys))})"
    ).fetchone() or (0, 0, 0)
    return {"matched": int(row[0]), "left_only": int(row[1]), "right_only": int(row[2])}


def rows_at(
    cursor: duckdb.DuckDBPyConnection,
    positioned: str,
    columns: tuple[Column, ...],
    positions: list[int | None],
) -> list[list[Any] | None]:
    """The view rows at these positions, in this order; None where the position is None."""
    wanted = sorted({p for p in positions if p is not None})
    found: dict[int, list[Any]] = {}
    if wanted:
        names = ", ".join('"' + c.name.replace('"', '""') + '"' for c in columns)
        listed = ", ".join(str(int(p)) for p in wanted)
        for record in cursor.execute(
            f"SELECT {POS}, {names} FROM ({positioned}) WHERE {POS} IN ({listed})"
        ).fetchall():
            found[int(record[0])] = list(record[1:])
    return [found.get(p) if p is not None else None for p in positions]


def focus_positions(
    cursor: duckdb.DuckDBPyConnection,
    picked_positioned: str,
    picked_columns: tuple[Column, ...],
    picked_keys: list[str],
    picked_position: int,
    other_positioned: str,
    other_columns: tuple[Column, ...],
    other_keys: list[str],
) -> tuple[list[int], list[Any]]:
    """Cross compare: the other side's positions with the same key as the picked row, and that
    key (the picked row's values, for the banner)."""
    picked = keyed_sql(picked_positioned, picked_columns, picked_keys, "picked")
    other = keyed_sql(other_positioned, other_columns, other_keys, "other")
    n = len(picked_keys)
    on = " AND ".join(f"o.k{i} = p.k{i}" for i in range(n))
    picked_row = f"SELECT * FROM ({picked}) WHERE {POS} = {int(picked_position)}"
    hits = cursor.execute(
        f"SELECT o.{POS} FROM ({other}) AS o JOIN ({picked_row}) AS p ON {on} ORDER BY o.{POS}"
    ).fetchall()
    key = cursor.execute(
        f"SELECT {', '.join(f'k{i}' for i in range(n))} FROM ({picked}) WHERE {POS} = {int(picked_position)}"
    ).fetchone()
    return [int(h[0]) for h in hits], list(key) if key else []


def differences(
    left_rows: list[list[Any] | None],
    left_columns: tuple[Column, ...],
    right_rows: list[list[Any] | None],
    right_columns: tuple[Column, ...],
    keys: tuple[tuple[str, str], ...],
) -> tuple[list[list[int]], list[list[int]]]:
    """Cells that differ in same-named, non-key columns of an aligned pair: [[row offset, column
    index], ...] for each side. Numbers that differ only in their last bits count as equal."""
    key_names = {k[0] for k in keys} | {k[1] for k in keys}
    right_at = {c.name: i for i, c in enumerate(right_columns)}
    shared = [
        (i, right_at[c.name])
        for i, c in enumerate(left_columns)
        if c.name in right_at and c.name not in key_names
    ]
    left_diff: list[list[int]] = []
    right_diff: list[list[int]] = []
    for offset, (lrow, rrow) in enumerate(zip(left_rows, right_rows, strict=True)):
        if lrow is None or rrow is None:
            continue
        for li, ri in shared:
            if not _same(lrow[li], rrow[ri]):
                left_diff.append([offset, li])
                right_diff.append([offset, ri])
    return left_diff, right_diff


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if (
        isinstance(a, int | float)
        and isinstance(b, int | float)
        and not isinstance(a, bool)
        and not isinstance(b, bool)
    ):
        return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)), abs(float(b)))
    return bool(a == b) or str(a) == str(b)
