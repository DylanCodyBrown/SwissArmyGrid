"""Key Finder: which columns of another table share values with this one? (a join-key search)

The job a data architect otherwise does by hand: take every column of a table, take its
distinct values, and find the columns of another table holding the same values. The column
pairs with the most overlap are the likely join keys.

How it works, in DuckDB:
1. Each side's columns are profiled in one pass: the longest value, the distinct count, the
   non-blank count. Only columns whose longest value is at most `max_length` characters (128
   by default) take part; longer text is notes and descriptions, never a key. True/false,
   list, struct and binary columns are skipped too.
2. Every value is compared as normalized text, so 42 (a number), 42.0 and '42' all meet:
   whole numbers lose their decimals, a timestamp at midnight reads as its date, and text is
   trimmed and/or lowercased when the switches say so.
3. Each side becomes one two-column list (column number, distinct value), and one hash join on
   the value counts the shared values for every column pair at once. Think of it as a
   GROUP BY over an inverted index of both tables.

For each pair it reports the shared distinct values, the share of each side's values found in
the other, whether each side is unique per row (so which one looks like the primary key), and
a score:

    overlap = shared / min(distinct on either side)          (the overlap coefficient)
    cover   = min(share of source found, share of target found)
    score   = (0.7 x overlap + 0.3 x cover) x min(1, log10(shared) / 3)

Overlap is 1.0 when one side's values all appear in the other (a foreign key into a primary
key). Cover breaks the classic tie: two unrelated running counters (1, 2, 3 ...) also overlap
completely, but a 6,000-row order_id covers a 2,000-row customer id only a third of the way,
while the real customer_id covers it 95%. The last factor (full weight from 1,000 shared
values) keeps a Y/N flag or a five-value status code, which overlap by accident, below a key.

It also answers the question asked of a table on its own: which columns together tell every
row apart (its primary key, often a composite one)? `most_distinct` reads the grid's first rows
and builds the combination greedily: the column with the most distinct values first, then each
round the column that separates the most rows still sharing a combination, until every row is
unique or no column helps. Greedy, not exhaustive: every combination of 50 columns is far too
many to count, while each round here is one scan, so the answer comes in seconds. A blank is a
value of its own there (as in GROUP BY), since two rows differing only by a blank are different
rows.
"""

# Every query here is built from quote()d column names and the grids' own compiled view SQL;
# no user text is spliced in, so the "string-built SQL" check is off for this file.
# ruff: noqa: S608

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import duckdb

from swissarmygrid.engine.datasets import Column
from swissarmygrid.engine.steps import quote, type_family

DEFAULT_MAX_LENGTH: int = 128
_SKIPPED_FAMILIES: frozenset[str] = frozenset({"boolean", "other"})
# The most distinct combination stops growing here; a key wider than this isn't a useful key.
MAX_KEY_COLUMNS: int = 8
MAX_ALTERNATIVES: int = 10


@dataclass(frozen=True)
class KeyOptions:
    trim: bool = True
    ignore_case: bool = False
    max_length: int = DEFAULT_MAX_LENGTH

    @staticmethod
    def from_json(raw: Any) -> KeyOptions:
        raw = raw if isinstance(raw, dict) else {}
        max_length = raw.get("max_length", DEFAULT_MAX_LENGTH)
        if not isinstance(max_length, int) or isinstance(max_length, bool) or not 1 <= max_length <= 4096:
            raise ValueError("max_length must be a whole number from 1 to 4,096.")
        return KeyOptions(bool(raw.get("trim", True)), bool(raw.get("ignore_case", False)), max_length)


@dataclass(frozen=True)
class ColumnStats:
    name: str
    type: str
    max_length: int | None
    distinct: int
    filled: int  # non-blank rows

    @property
    def unique(self) -> bool:
        return self.filled > 0 and self.distinct == self.filled


def normalized(column: Column, options: KeyOptions) -> str | None:
    """The SQL for one column's values as comparable text, or None if the column can't be a key."""
    family = type_family(column.type)
    if family in _SKIPPED_FAMILIES:
        return None
    c = quote(column.name)
    if family == "number":
        if column.type.upper().startswith(("FLOAT", "DOUBLE", "REAL", "DECIMAL")):
            # 42.0 and 42.00 read as 42, like an integer column's 42; a DECIMAL's 1.50 reads as
            # 1.5, as a DOUBLE's 1.5 does (its text keeps every decimal place).
            other = f"CAST({c} AS VARCHAR)"
            if column.type.upper().startswith("DECIMAL"):
                other = f"rtrim({other}, '0')"
            return (
                f"CASE WHEN {c} = trunc({c}) AND abs({c}) < 1e18 "
                f"THEN CAST(CAST({c} AS HUGEINT) AS VARCHAR) ELSE {other} END"
            )
        return f"CAST({c} AS VARCHAR)"
    if family == "timestamp":
        return (
            f"CASE WHEN {c} = date_trunc('day', {c}) THEN CAST(CAST({c} AS DATE) AS VARCHAR) "
            f"ELSE CAST({c} AS VARCHAR) END"
        )
    if family == "date":
        return f"CAST({c} AS VARCHAR)"
    text = c
    if options.trim:
        text = f"trim({text}, ' ' || chr(9) || chr(13) || chr(10))"
    if options.ignore_case:
        text = f"lower({text})"
    return f"NULLIF({text}, '')"


def profile_side(
    cursor: duckdb.DuckDBPyConnection, relation: str, columns: tuple[Column, ...], options: KeyOptions
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


def _value_list(relation: str, eligible: list[tuple[ColumnStats, str]]) -> str:
    return " UNION ALL ".join(
        f"SELECT DISTINCT {i} AS c, {sql} AS v FROM ({relation}) WHERE {sql} IS NOT NULL"
        for i, (_, sql) in enumerate(eligible)
    )


def score(shared: int, source_distinct: int, target_distinct: int) -> float:
    if not shared:
        return 0.0
    overlap = shared / max(1, min(source_distinct, target_distinct))
    cover = shared / max(1, source_distinct, target_distinct)
    confidence = min(1.0, math.log10(shared) / 3) if shared > 1 else 0.05
    return round((0.7 * overlap + 0.3 * cover) * confidence, 4)


def relationship(source: ColumnStats, target: ColumnStats) -> str:
    if source.unique and target.unique:
        return "one to one"
    if target.unique:
        return "many to one (the target looks like the key)"
    if source.unique:
        return "one to many (this column looks like the key)"
    return "many to many"


def find_keys(
    cursor: duckdb.DuckDBPyConnection,
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
    cursor: duckdb.DuckDBPyConnection,
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
    cursor: duckdb.DuckDBPyConnection,
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
