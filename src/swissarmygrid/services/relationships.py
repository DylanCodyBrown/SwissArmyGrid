"""Relationships between tables: the Key Finder's and the comparison's queries.

The rules (what a key is, how values are normalized, the score, how two grids line up) are the
domain's (domain/relationships.py). Here they are run, through the `SqlSession` port, on the
relations the caller hands in (each grid's view).
"""

# Every query here is built from quote()d column names and the grids' own compiled view SQL;
# no user text is spliced in, so the "string-built SQL" check is off for this file.
# ruff: noqa: S608

from __future__ import annotations

from typing import Any

from swissarmygrid.domain.datasets import Column
from swissarmygrid.domain.grids import POS
from swissarmygrid.domain.relationships import (
    MAX_ALTERNATIVES,
    MAX_KEY_COLUMNS,
    ColumnStats,
    CompareSpec,
    KeyOptions,
    _joined,
    _value_list,
    keyed_sql,
    normalized,
    relationship,
    score,
)
from swissarmygrid.domain.steps import quote
from swissarmygrid.services.ports import SqlSession

# ===== Key Finder =====


def profile_side(
    cursor: SqlSession, relation: str, columns: tuple[Column, ...], options: KeyOptions
) -> tuple[list[tuple[ColumnStats, str]], list[dict[str, str]]]:
    """(the eligible columns with their normalized SQL, the skipped columns with the reason)."""
    expressions: list[tuple[Column, str]] = []
    skipped: list[dict[str, str]] = []
    for column in columns:
        sql = normalized(column, options)
        if sql is None:
            skipped.append({"column": column.name, "reason": f"{column.type} values are not keys"})
        else:
            expressions.append((column, sql))
    if not expressions:
        return [], skipped
    parts = ", ".join(f"max(length({sql})), count(DISTINCT {sql}), count({sql})" for _, sql in expressions)
    row = cursor.execute(f"SELECT {parts} FROM ({relation})").fetchone() or ()
    eligible: list[tuple[ColumnStats, str]] = []
    for i, (column, sql) in enumerate(expressions):
        longest, distinct, filled = row[3 * i], int(row[3 * i + 1]), int(row[3 * i + 2])
        stats = ColumnStats(
            column.name, column.type, None if longest is None else int(longest), distinct, filled
        )
        if not distinct:
            skipped.append({"column": column.name, "reason": "every value is blank"})
        elif stats.max_length is not None and stats.max_length > options.max_length:
            skipped.append(
                {"column": column.name, "reason": f"longest value is {stats.max_length:,} characters"}
            )
        else:
            eligible.append((stats, sql))
    return eligible, skipped


def find_keys(
    cursor: SqlSession,
    source: str,
    source_columns: tuple[Column, ...],
    target: str,
    target_columns: tuple[Column, ...],
    options: KeyOptions,
    same_table: bool = False,
) -> dict[str, Any]:
    """Every column pair of `source` x `target` (two SQL relations) with shared values, best
    first. `same_table` leaves out a column paired with itself."""
    left, left_skipped = profile_side(cursor, source, source_columns, options)
    right, right_skipped = profile_side(cursor, target, target_columns, options)
    pairs: list[dict[str, Any]] = []
    if left and right:
        sql = (
            f"WITH a AS ({_value_list(source, left)}), b AS ({_value_list(target, right)}) "
            "SELECT a.c, b.c, count(*) FROM a JOIN b USING (v) GROUP BY ALL"
        )
        for li, ri, shared in cursor.execute(sql).fetchall():
            s, t = left[int(li)][0], right[int(ri)][0]
            if same_table and s.name == t.name:
                continue
            pairs.append(
                {
                    "source_column": s.name,
                    "target_column": t.name,
                    "shared": int(shared),
                    "source_distinct": s.distinct,
                    "target_distinct": t.distinct,
                    "source_found": round(int(shared) / s.distinct, 4),
                    "target_found": round(int(shared) / t.distinct, 4),
                    "source_unique": s.unique,
                    "target_unique": t.unique,
                    "relationship": relationship(s, t),
                    "score": score(int(shared), s.distinct, t.distinct),
                }
            )
    pairs.sort(key=lambda p: (-p["score"], -p["shared"], p["source_column"], p["target_column"]))
    return {
        "pairs": pairs,
        "source_checked": [s.name for s, _ in left],
        "target_checked": [s.name for s, _ in right],
        "source_skipped": left_skipped,
        "target_skipped": right_skipped,
    }


def most_distinct(
    cursor: SqlSession,
    relation: str,
    columns: tuple[Column, ...],
    options: KeyOptions,
    row_limit: int,
) -> dict[str, Any]:
    """The combination of columns that tells the most of `relation`'s first `row_limit` rows
    apart, built one column at a time (see the module notes). `relation` is a grid view's SQL,
    which ends in its ORDER BY, so a LIMIT on it takes the rows the grid shows first.

    `steps` is the build, one entry per column added; its last entry is the answer.
    `alternatives` are other combinations as small as the answer that are also unique per row."""
    first = f"{relation} LIMIT {int(row_limit)}"
    # One row past the limit says whether the table goes on beyond it.
    counted = cursor.execute(f"SELECT count(*) FROM ({relation} LIMIT {int(row_limit) + 1})").fetchone()
    total = int(counted[0]) if counted else 0
    rows = min(total, row_limit)
    eligible, skipped = profile_side(cursor, first, columns, options)
    remaining = [stats.name for stats, _ in eligible]

    def entry(names: list[str], distinct: int) -> dict[str, Any]:
        return {
            "columns": names,
            "distinct": distinct,
            "share": round(distinct / rows, 4) if rows else 0.0,
            "repeated_rows": rows - distinct,
            "unique": distinct == rows,
        }

    steps: list[dict[str, Any]] = []
    alternatives: list[dict[str, Any]] = []
    chosen: list[str] = []
    best = 0
    while rows and remaining and len(chosen) < MAX_KEY_COLUMNS:
        parts = ", ".join(
            f"count(DISTINCT row({', '.join(quote(c) for c in [*chosen, name])}))" for name in remaining
        )
        counts = [int(c) for c in cursor.execute(f"SELECT {parts} FROM ({first})").fetchone() or ()]
        pick = max(range(len(remaining)), key=lambda i: (counts[i], -i))  # ties: table order
        if counts[pick] <= best:
            break  # no column separates any more rows (the table repeats whole rows)
        best = counts[pick]
        if best == rows:
            alternatives = [
                entry([*chosen, remaining[i]], counts[i])
                for i in range(len(remaining))
                if i != pick and counts[i] == rows
            ][:MAX_ALTERNATIVES]
        chosen = [*chosen, remaining.pop(pick)]
        steps.append(entry(chosen, best))
        if best == rows:
            break
    return {
        "rows_checked": rows,
        "row_limit": row_limit,
        "limited": total > row_limit,
        "steps": steps,
        "alternatives": alternatives,
        "checked": [stats.name for stats, _ in eligible],
        "skipped": skipped,
    }


def sample_values(
    cursor: SqlSession,
    source: str,
    source_column: Column,
    target: str,
    target_column: Column,
    options: KeyOptions,
    limit: int = 10,
) -> dict[str, Any]:
    """A few shared values, and a few of this column's values the target lacks (the rows a join
    would drop), with the count of each."""
    a, b = normalized(source_column, options), normalized(target_column, options)
    if a is None or b is None:
        raise ValueError("Those columns can't be compared as keys.")
    base = (
        f"WITH a AS (SELECT DISTINCT {a} AS v FROM ({source}) WHERE {a} IS NOT NULL), "
        f"b AS (SELECT DISTINCT {b} AS v FROM ({target}) WHERE {b} IS NOT NULL) "
    )
    matched = cursor.execute(
        base + f"SELECT v FROM a SEMI JOIN b USING (v) ORDER BY v LIMIT {int(limit)}"
    ).fetchall()
    missing = cursor.execute(
        base + f"SELECT v FROM a ANTI JOIN b USING (v) ORDER BY v LIMIT {int(limit)}"
    ).fetchall()
    counts = cursor.execute(
        base + "SELECT (SELECT count(*) FROM a SEMI JOIN b USING (v)), "
        "(SELECT count(*) FROM a ANTI JOIN b USING (v))"
    ).fetchone() or (0, 0)
    return {
        "matched": [r[0] for r in matched],
        "missing": [r[0] for r in missing],
        "matched_count": int(counts[0]),
        "missing_count": int(counts[1]),
    }


# ===== Compare =====


def counts(
    cursor: SqlSession,
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
    cursor: SqlSession,
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
    cursor: SqlSession,
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
