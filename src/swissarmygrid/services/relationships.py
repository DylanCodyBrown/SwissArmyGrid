"""Relationships between tables: the Key Finder, the comparison of two grids, and the current
relationship model with its library.

The rules (what a key is, how values are normalized, the score, how two grids line up, what a model
is) are the domain's (domain/relationships.py). Here they are run, through the `SqlSession` port, on
the relations the caller hands in (each grid's view), and the use cases are at the end:

  KeyFinder   possible join columns between this grid and other grids, and this grid's own key
  Compares    two grids side by side, aligned row for row on key columns
  Models      the current model (keys and relationships); tangs: the library (the person's own and
              the shared folders), save, export, review and apply, and running a tang against the
              open tables; the relationship finder across the open tables; the ERD

Both sides of a Key Finder or a compare are grid views (each after its own steps), so a user can
filter two grids down first and then ask how they join. Pairs carry counts, never values.
"""

# Every query here is built from quote()d column names and the grids' own compiled view SQL;
# no user text is spliced in, so the "string-built SQL" check is off for this file.
# ruff: noqa: S608

from __future__ import annotations

import json
import secrets
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from swissarmygrid.domain.datasets import Column, DatasetInfo
from swissarmygrid.domain.grids import POS, Grid
from swissarmygrid.domain.relationships import (
    MAX_ALTERNATIVES,
    MAX_KEY_COLUMNS,
    Candidate,
    ColumnStats,
    CompareSpec,
    KeyOptions,
    Model,
    Relationship,
    _joined,
    _value_list,
    conflicts,
    differences,
    key_family,
    keyed_sql,
    mapping_sql,
    merge,
    mermaid_erd,
    name_likeness,
    normalized,
    parse_model,
    path_between,
    read_tang,
    relationship,
    score,
    suggest,
    tang_file_name,
    write_tang,
)
from swissarmygrid.domain.steps import quote
from swissarmygrid.services.grids import truncate
from swissarmygrid.services.ports import AGENT, BACKGROUND, INTERACTIVE, SqlSession
from swissarmygrid.services.workspace import NotFound, TimedOut, Workspace

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


# ===== the use cases: the Key Finder =====

MAX_PAIRS: int = 200


def _label(grid: Grid) -> str:
    steps = len([s for s in grid.steps if s.enabled])
    return f"{grid.dataset}" + (f" ({steps} step{'s' if steps != 1 else ''})" if steps else "")


class KeyFinder:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    def _view(
        self, cur: SqlSession, grid: Grid, held: ExitStack, ordered: bool = False
    ) -> tuple[str, tuple[Column, ...], DatasetInfo]:
        """A grid's view as a SQL relation, with its columns and dataset info. It reads the view
        cache (not every step again), held until `held` closes; `ordered`: in view order."""
        ws = self.ws
        _, info = ws.grid_and_info(grid.id)
        compiled = ws.compiled(cur, grid, info)
        sql = held.enter_context(ws.views.reading(cur, info, grid.steps, grid.settings, ordered=ordered))
        return sql, compiled.columns, info

    def _targets(self, source: Grid, target: Any) -> list[Grid]:
        if target == "*":
            return [g for g in self.ws.grids.all() if g.id != source.id]
        if not isinstance(target, str):
            raise ValueError('Send {"target": "<grid id>"} or {"target": "*"} for every other grid.')
        found = self.ws.grids.get(target)
        if found is None:
            raise LookupError(f"No grid {target!r}; open one on the dataset first.")
        return [found]

    def _timed(self, cur: SqlSession, fn: Any) -> Any:
        timer = threading.Timer(self.ws.settings.key_finder_timeout_seconds, cur.interrupt)
        timer.start()
        try:
            return fn()
        finally:
            timer.cancel()

    def candidates(self, source: Grid) -> dict[str, Any]:
        """The grids a Key Finder can compare against (for the page's picker)."""
        return {
            "grids": [
                {"grid": g.id, "dataset": g.dataset, "label": _label(g), "self": g.id == source.id}
                for g in self.ws.grids.all()
            ]
        }

    async def run(self, source: Grid, body: Any, who: str) -> dict[str, Any]:
        """Pairs (column, target grid, column, shared values, % found each way, score), best first,
        and unique_key: the column combination that tells this grid's first rows apart."""
        ws = self.ws
        settings = ws.settings
        body = body if isinstance(body, dict) else {}
        options = KeyOptions.from_json(body.get("options"))
        chosen = self._targets(source, body.get("target", "*"))

        def compute(cur: SqlSession) -> dict[str, Any]:
            with ExitStack() as held:
                return search(cur, held)

        def search(cur: SqlSession, held: ExitStack) -> dict[str, Any]:
            src_sql, src_cols, src_info = self._view(cur, source, held)
            pairs: list[dict[str, Any]] = []
            skipped: dict[str, Any] = {}
            checked: list[str] = []
            for target in chosen:
                with ExitStack() as held_target:
                    tgt_sql, tgt_cols, tgt_info = self._view(cur, target, held_target)
                    found = self._timed(
                        cur,
                        lambda tgt_sql=tgt_sql, tgt_cols=tgt_cols, tgt_info=tgt_info: find_keys(
                            cur,
                            src_sql,
                            src_cols,
                            tgt_sql,
                            tgt_cols,
                            options,
                            same_table=tgt_info.name == src_info.name,
                        ),
                    )
                for pair in found["pairs"]:
                    pairs.append(
                        pair
                        | {
                            "target_grid": target.id,
                            "target": _label(target),
                            "target_dataset": target.dataset,
                        }
                    )
                skipped[target.id] = found["target_skipped"]
                checked = found["source_checked"]
                skipped["source"] = found["source_skipped"]
            pairs.sort(key=lambda p: (-p["score"], -p["shared"]))
            # This grid's own key: timed on its own, so a slow view still returns the pairs.
            limit = settings.key_finder_unique_rows
            # The grid's first rows, so this one reads the view in its order.
            first_sql, _, _ = self._view(cur, source, held, ordered=True)
            try:
                unique_key: dict[str, Any] = self._timed(
                    cur, lambda: most_distinct(cur, first_sql, src_cols, options, limit)
                )
            except ws.interrupted:
                unique_key = {"stopped": True, "row_limit": limit}
            return {
                "grid": source.id,
                "source": source.dataset,
                "pairs": pairs[:MAX_PAIRS],
                "pair_count": len(pairs),
                "source_checked": checked,
                "skipped": skipped,
                "targets": [{"grid": t.id, "label": _label(t), "dataset": t.dataset} for t in chosen],
                "unique_key": unique_key,
                "options": {
                    "trim": options.trim,
                    "ignore_case": options.ignore_case,
                    "max_length": options.max_length,
                },
            }

        try:
            result = await ws.grid_query(source, None, AGENT if who == "agent" else BACKGROUND, compute)
        except ws.interrupted:
            raise TimedOut(
                f"The Key Finder stopped after {settings.key_finder_timeout_seconds:.0f} seconds. "
                "Filter the grids first, or pick one target grid."
            ) from None
        ws.audit.write(who, "key_finder", source.dataset, targets=len(chosen), pairs=result["pair_count"])
        return result

    async def sample(self, source: Grid, body: Any) -> dict[str, Any]:
        """A few shared values and a few this column has that the target lacks (page only)."""
        body = body if isinstance(body, dict) else {}
        target = self._targets(source, body.get("target"))[0]
        options = KeyOptions.from_json(body.get("options"))

        def compute(cur: SqlSession) -> dict[str, Any]:
            with ExitStack() as held:
                src_sql, src_cols, _ = self._view(cur, source, held)
                tgt_sql, tgt_cols, _ = self._view(cur, target, held)
                a = next((c for c in src_cols if c.name == body.get("source_column")), None)
                b = next((c for c in tgt_cols if c.name == body.get("target_column")), None)
                if a is None or b is None:
                    raise ValueError("Name a column of each grid.")
                sampled: dict[str, Any] = self._timed(
                    cur, lambda: sample_values(cur, src_sql, a, tgt_sql, b, options)
                )
                return sampled

        return await self.ws.grid_query(source, None, BACKGROUND, compute)


# ===== the use cases: compare =====
# The aligned mapping (compare row -> left position, right position) is materialized as one small
# table per compare, rebuilt only when either grid's steps, settings or data change, so scrolling
# reads windows from it like any grid view.


@dataclass
class Compare:
    id: str
    left: str
    right: str
    spec: CompareSpec
    signature: str = ""
    row_count: int = 0
    # Cross compare: the side whose rows are filtered, its positions, and the key picked.
    focus: tuple[str, list[int], list[Any]] | None = None
    tallies: dict[str, int] = field(default_factory=dict)

    @property
    def table(self) -> str:
        return f'"__sag_cmp_{self.id}"'

    def to_json(self) -> dict[str, Any]:
        focus: dict[str, Any] | None = None
        if self.focus is not None:
            focus = {"side": self.focus[0], "rows": len(self.focus[1]), "key": self.focus[2]}
        return {
            "compare": self.id,
            "left": self.left,
            "right": self.right,
            **self.spec.to_json(),
            "row_count": self.row_count,
            "counts": self.tallies,
            "focus": focus,
        }


@dataclass(frozen=True)
class Side:
    grid: Grid
    info: DatasetInfo
    positioned: str
    columns: tuple[Column, ...]


class Compares:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws
        self._compares: dict[str, Compare] = {}

    def _side(self, cur: SqlSession, grid_id: str) -> Side:
        grid, info = self.ws.grid_and_info(grid_id)
        positioned, columns = self.ws.views.positioned_sql(cur, info, grid.steps, grid.settings)
        return Side(grid, info, positioned, columns)

    @staticmethod
    def _signature(left: Side, right: Side, spec: CompareSpec) -> str:
        def one(s: Side) -> Any:
            return [
                s.info.name,
                s.info.version,
                [st.to_json() for st in s.grid.steps],
                s.grid.settings.to_json(),
            ]

        return json.dumps([one(left), one(right), spec.to_json()], default=str)

    def _ensure(self, cur: SqlSession, cmp: Compare) -> tuple[Side, Side]:
        """Both sides, with the mapping table and the counts up to date."""
        left, right = self._side(cur, cmp.left), self._side(cur, cmp.right)
        sig = self._signature(left, right, cmp.spec)
        if sig != cmp.signature:
            cmp.tallies = counts(
                cur, left.positioned, left.columns, right.positioned, right.columns, cmp.spec
            )
            if cmp.spec.mode == "cross":
                cmp.row_count = 0
            else:
                # Numbered in the order the sorted mapping comes out (order checked in verified.md).
                sql = mapping_sql(left.positioned, left.columns, right.positioned, right.columns, cmp.spec)
                cur.execute(
                    f"CREATE OR REPLACE TABLE {cmp.table} AS "
                    f"SELECT row_number() OVER () - 1 AS cpos, * FROM ({sql})"
                )
                result = cur.execute(f"SELECT count(*) FROM {cmp.table}").fetchone()
                cmp.row_count = int(result[0]) if result else 0
            cmp.signature = sig
        return left, right

    async def _run(self, fn: Callable[[Any], Any]) -> Any:
        ws = self.ws
        return await ws.workers.run(INTERACTIVE, lambda: fn(ws.store.cursor()))

    def get(self, compare_id: str) -> Compare:
        cmp = self._compares.get(compare_id)
        if cmp is None:
            raise LookupError("No such compare; it may have been stopped or the server restarted.")
        return cmp

    async def create(self, body: Any, who: str) -> dict[str, Any]:
        if not isinstance(body, dict) or not all(isinstance(body.get(k), str) for k in ("left", "right")):
            raise ValueError('Send {"left": grid id, "right": grid id, "keys": [[left col, right col]]}.')
        if body["left"] == body["right"]:
            raise ValueError("Compare two different grids.")
        spec = CompareSpec.from_json(body)
        cmp = Compare(secrets.token_hex(4), body["left"], body["right"], spec)
        await self._run(lambda cur: self._ensure(cur, cmp))
        self._compares[cmp.id] = cmp
        self.ws.audit.write(who, "compare", f"{cmp.left}:{cmp.right}", keys=len(spec.keys), mode=spec.mode)
        return cmp.to_json()

    async def read(self, cmp: Compare) -> dict[str, Any]:
        """The compare, with its counts (matched, left only, right only)."""
        await self._run(lambda cur: self._ensure(cur, cmp))
        return cmp.to_json()

    async def change(self, cmp: Compare, body: Any) -> dict[str, Any]:
        """Change keys, mode or anti_side."""
        if not isinstance(body, dict):
            raise ValueError("Send the fields to change: keys, mode, anti_side.")
        merged = cmp.spec.to_json() | {k: v for k, v in body.items() if k in ("keys", "mode", "anti_side")}
        cmp.spec = CompareSpec.from_json(merged)
        cmp.focus = None
        cmp.signature = ""
        await self._run(lambda cur: self._ensure(cur, cmp))
        return cmp.to_json()

    async def remove(self, compare_id: str) -> dict[str, Any]:
        cmp = self._compares.pop(compare_id, None)
        if cmp is not None:
            await self._run(lambda cur: cur.execute(f"DROP TABLE IF EXISTS {cmp.table}"))
        return {"stopped": compare_id}

    async def rows(self, cmp: Compare, which: str | None, start: int, end: int) -> dict[str, Any]:
        """That side's rows in compare order: placeholders where the other side has the key and
        this side doesn't, and the cells that differ from the aligned row on the other side."""
        ws = self.ws
        if which not in ("left", "right"):
            raise ValueError("side must be left or right.")

        def compute(cur: SqlSession) -> dict[str, Any]:
            left, right = self._ensure(cur, cmp)
            mine = left if which == "left" else right
            if cmp.spec.mode == "cross":
                if cmp.focus and cmp.focus[0] == which:
                    positions: list[int | None] = list(cmp.focus[1][start:end])
                    total = len(cmp.focus[1])
                    got = rows_at(cur, mine.positioned, mine.columns, positions)
                else:
                    window = ws.views.window(cur, mine.info, mine.grid.steps, mine.grid.settings, start, end)
                    got, total = [list(r) for r in window.rows], window.row_count
                return {"rows": got, "row_count": total, "placeholders": [], "diffs": []}
            pairs = cur.execute(
                f"SELECT lp, rp FROM {cmp.table} WHERE cpos >= ? AND cpos < ? ORDER BY cpos", [start, end]
            ).fetchall()
            lrows = rows_at(cur, left.positioned, left.columns, [p[0] for p in pairs])
            rrows = rows_at(cur, right.positioned, right.columns, [p[1] for p in pairs])
            ldiff, rdiff = differences(lrows, left.columns, rrows, right.columns, cmp.spec.keys)
            got_rows = lrows if which == "left" else rrows
            width = len(mine.columns)
            return {
                # A placeholder is a row of blanks the page draws as an empty, shaded row.
                "rows": [r if r is not None else [None] * width for r in got_rows],
                "row_count": cmp.row_count,
                "placeholders": [i for i, r in enumerate(got_rows) if r is None],
                "diffs": ldiff if which == "left" else rdiff,
            }

        result: dict[str, Any] = await self._run(compute)
        result["truncated"] = truncate(result["rows"], ws.settings.max_cell_chars)
        result["counts"] = cmp.tallies
        return result

    async def key_group(self, cmp: Compare, row: int | None) -> dict[str, Any]:
        """Rows `row`'s key also covers: one block, since the mapping is sorted by key. A blank key
        matches nothing, so it is a block of one."""
        if row is None or row < 0:
            raise ValueError("Give row.")
        if cmp.spec.mode == "cross":
            raise ValueError("Cross compare has no shared rows; pick a row to filter the other side.")

        def compute(cur: SqlSession) -> dict[str, Any]:
            self._ensure(cur, cmp)
            names = [f"s{i}" for i in range(len(cmp.spec.keys))]
            key = cur.execute(f"SELECT {', '.join(names)} FROM {cmp.table} WHERE cpos = ?", [row]).fetchone()
            if key is None:
                raise LookupError("No such compare row.")
            if any(v is None for v in key):
                return {"first": row, "last": row, "key": list(key)}
            where = " AND ".join(f"{n} = ?" for n in names)
            span = cur.execute(
                f"SELECT min(cpos), max(cpos) FROM {cmp.table} WHERE {where}", list(key)
            ).fetchone() or (row, row)  # an aggregate always returns one row
            return {"first": int(span[0]), "last": int(span[1]), "key": list(key)}

        found: dict[str, Any] = await self._run(compute)
        return found

    async def set_focus(self, cmp: Compare, body: Any) -> dict[str, Any]:
        """Cross compare: a row picked on one side filters the other to the rows sharing its key."""
        picked = body.get("side") if isinstance(body, dict) else None
        position = body.get("position") if isinstance(body, dict) else None
        if picked not in ("left", "right") or not isinstance(position, int):
            raise ValueError('Send {"side": "left" | "right", "position": the picked row}.')

        row: int = position

        def compute(cur: SqlSession) -> None:
            left, right = self._ensure(cur, cmp)
            at = row
            # A row picked on the side that is itself filtered: its place in that side's view.
            if cmp.focus and cmp.focus[0] == picked and 0 <= at < len(cmp.focus[1]):
                at = cmp.focus[1][at]
            a, b = (left, right) if picked == "left" else (right, left)
            ka = [k[0] if picked == "left" else k[1] for k in cmp.spec.keys]
            kb = [k[1] if picked == "left" else k[0] for k in cmp.spec.keys]
            hits, key = focus_positions(cur, a.positioned, a.columns, ka, at, b.positioned, b.columns, kb)
            cmp.focus = ("right" if picked == "left" else "left", hits, key)

        await self._run(compute)
        return cmp.to_json()

    def clear_focus(self, cmp: Compare) -> dict[str, Any]:
        cmp.focus = None
        return cmp.to_json()


# ===== the use cases: the relationship model =====


class ModelLibrary(Protocol):
    """The library of tangs and model files (adapters/state_files.py)."""

    def folders(self) -> list[str]: ...

    def shared_folders(self) -> list[str]: ...

    def read(self) -> tuple[dict[str, tuple[Model, str]], list[str]]: ...

    def where(self, path: str) -> str: ...

    def save(self, model: Model, folder: str | None) -> str: ...


class Models:
    """The current model for this server, shared with the pages (and the agent): a page redraws on
    the relationships_updated notice. Agents may set relationships too: keys and joins are metadata,
    never cell values."""

    def __init__(self, ws: Workspace, library: ModelLibrary) -> None:
        self.ws = ws
        self._library = library
        self.model = Model()

    @property
    def library_files(self) -> ModelLibrary:
        """The tang library itself (packs copy tangs out of it)."""
        return self._library

    def current(self) -> dict[str, Any]:
        return self.model.to_json()

    async def _changed(self, who: str, name: str) -> dict[str, Any]:
        self.ws.audit.write(who, "relationships", None, model=name)
        await self.ws.live.broadcast({"type": "relationships_updated"})
        return self.model.to_json()

    async def set_current(self, body: Any, who: str) -> dict[str, Any]:
        """Merge (or replace with) a model: {"model": {...}, "replace": false}."""
        if not isinstance(body, dict) or "model" not in body:
            raise ValueError('Send {"model": {...}, "replace": false}.')
        model = parse_model(body["model"])
        self.model = model if body.get("replace") else merge(self.model, model)
        return await self._changed(who, model.name)

    def library(self) -> dict[str, Any]:
        """The models found in the library folders."""
        found, problems = self._library.read()
        open_names = {i.name for i in self.ws.store.all()}
        return {
            "models": [
                {
                    "name": name,
                    "description": model.description,
                    "tags": list(model.tags),
                    "saved_at": model.saved_at,
                    "author": model.author,
                    "file": path,
                    "where": self._library.where(path),
                    "tang": path.endswith(".tang"),
                    "tables": sorted(model.tables()),
                    "open_tables": sorted(model.tables() & open_names),
                    "relationships": len(model.relationships),
                }
                for name, (model, path) in sorted(found.items(), key=lambda kv: kv[0].casefold())
            ],
            "folders": self._library.folders(),
            "shared_folders": self._library.shared_folders(),
            "problems": problems,
        }

    def _named(self, name: str) -> Model:
        found, _ = self._library.read()
        entry = found.get(name) or next(
            (v for k, v in found.items() if k.casefold() == name.casefold()), None
        )
        if entry is None:
            raise NotFound(f"No tang named {name!r} in the library.")
        return entry[0]

    async def apply_library(self, name: str, who: str) -> dict[str, Any]:
        """Apply (merge) one library tang; the answer names what it changed in the current one."""
        tang = self._named(name)
        changed = conflicts(self.model, tang)
        self.model = merge(self.model, tang)
        return await self._changed(who, tang.name) | {"changed": changed}

    # ===== tangs: save, export, review and apply a .tang file =====

    def _with_columns(self, model: Model) -> dict[str, tuple[tuple[str, str], ...]]:
        """The open tables' columns and types, for every table the model names."""
        return {
            i.name: tuple((c.name, c.type) for c in i.columns)
            for i in self.ws.store.all()
            if i.name in model.tables()
        } | {t: c for t, c in model.columns.items() if self.ws.store.get(t) is None}

    def snapshot(self, body: Any) -> Model:
        """The current model as a tang to save: its name, description, tags and author from `body`,
        the open tables' columns, and the time."""
        body = body if isinstance(body, dict) else {}
        current = self.model.to_json()
        named = parse_model(
            current
            | {
                "name": body.get("name") or (self.model.name if self.model.name != "current" else ""),
                "description": body.get("description", self.model.description),
                "tags": body.get("tags", list(self.model.tags)),
                "author": body.get("author", self.model.author),
            }
        )
        if not self.model.tables():
            raise ValueError("Nothing to save yet: find or set some relationships first.")
        return Model(
            named.name,
            named.description,
            named.keys,
            named.sources,
            named.relationships,
            named.tags,
            self._with_columns(named),
            datetime.now(UTC).isoformat(timespec="seconds"),
            named.author,
        )

    async def save_tang(self, body: Any, who: str) -> dict[str, Any]:
        """Save the current relationships as a tang: in the person's own tangs, or a shared folder
        (`folder`), where coworkers' windows list it."""
        if not isinstance(body, dict) or not isinstance(body.get("name"), str) or not body["name"].strip():
            raise ValueError('Send {"name": "Sales store", "description", "tags", "author", "folder"}.')
        tang = self.snapshot(body)
        folder = body.get("folder") or None
        path = self._library.save(tang, folder if isinstance(folder, str) else None)
        self.model = Model(
            tang.name, tang.description, tang.keys, tang.sources, tang.relationships, tang.tags,
            tang.columns, tang.saved_at, tang.author,
        )  # fmt: skip
        self.ws.audit.write(who, "save_tang", None, tang=tang.name, shared=bool(folder))
        await self.ws.live.broadcast({"type": "relationships_updated"})
        return {"saved": tang.name, "file": path, "where": self._library.where(path)}

    def export_tang(self) -> tuple[str, str]:
        """The current relationships as .tang text, and a file name for it."""
        tang = self.snapshot({})
        name = tang.name if tang.name != "current" else "relationships"
        return write_tang(tang), tang_file_name(name)

    def review(self, text: Any) -> dict[str, Any]:
        """What a .tang file holds, before anything is applied: its tables (which are open), its
        relationships and keys, and what applying it would change in the current ones."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Send {"text": "<the .tang file\'s text>"}.')
        tang = read_tang(text)
        open_names = {i.name for i in self.ws.store.all()}
        return {
            "tang": tang.to_json(),
            "open_tables": sorted(tang.tables() & open_names),
            "missing_tables": sorted(tang.tables() - open_names),
            "changes": conflicts(self.model, tang),
        }

    async def import_tang(self, body: Any, who: str) -> dict[str, Any]:
        """Apply a .tang file's relationships (merged into the current ones), and with `save` keep it
        in the person's own tangs or a shared folder too."""
        body = body if isinstance(body, dict) else {}
        text = body.get("text")
        reviewed = self.review(text)
        tang = read_tang(str(text))
        self.model = merge(self.model, tang)
        answer = await self._changed(who, tang.name) | {"changed": reviewed["changes"]}
        if body.get("save"):
            folder = body.get("folder") or None
            path = self._library.save(tang, folder if isinstance(folder, str) else None)
            answer |= {"file": path, "where": self._library.where(path)}
        return answer

    # ===== run a tang: does each relationship still hold in the open tables? =====

    async def check(self, body: Any, who: str) -> dict[str, Any]:
        """Run the current relationships, or a library tang (`tang`), against the open tables. Each
        relationship: holds, holds with orphans, broken (with why), or not open."""
        body = body if isinstance(body, dict) else {}
        model = self._named(body["tang"]) if isinstance(body.get("tang"), str) else self.model
        if not model.relationships:
            raise ValueError("No relationships to run yet.")
        infos = {i.name: i for i in self.ws.store.all()}
        cursor = self.ws.store.cursor()

        def job() -> list[dict[str, Any]]:
            timer = threading.Timer(self.ws.settings.key_finder_timeout_seconds, cursor.interrupt)
            timer.start()
            try:
                return [check_relationship(cursor, r, infos, model.columns) for r in model.relationships]
            finally:
                timer.cancel()

        try:
            results = await self.ws.workers.run(AGENT if who == "agent" else BACKGROUND, job)
        except self.ws.interrupted:
            raise TimedOut("The run stopped at the time limit: run it with fewer tables open.") from None
        counts: dict[str, int] = {}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        self.ws.audit.write(who, "run_tang", None, tang=model.name, results=counts)
        return {"tang": model.name, "results": results, "counts": counts}

    def path(self, start: str, end: str) -> dict[str, Any]:
        """How two tables join through the current relationships: the shortest chain of joins."""
        chain = path_between(self.model.relationships, start, end)
        return {"from": start, "to": end, "joins": [r.to_json() for r in chain], "found": bool(chain)}

    def erd(self, wanted: set[str]) -> dict[str, Any]:
        """The current model as Mermaid erDiagram text."""
        columns: dict[str, list[tuple[str, str]]] = {
            i.name: [(c.name, c.type) for c in i.columns] for i in self.ws.store.all()
        }
        only = wanted or (self.model.tables() & set(columns)) or None
        if not self.model.tables():
            raise ValueError(
                "No relationships yet: apply a model from the library, import one, or set keys first."
            )
        text = mermaid_erd(self.model, columns, only)
        return {"mermaid": text, "tables": sorted(only or self.model.tables())}


def check_relationship(
    cursor: SqlSession,
    rel: Relationship,
    infos: dict[str, DatasetInfo],
    saved: dict[str, tuple[tuple[str, str], ...]],
) -> dict[str, Any]:
    """One relationship run against the open tables (each dataset as pushed, before any grid's
    steps): its status and the counts behind it. Orphan samples are values from the user's data."""
    out = rel.to_json()
    many, one = (rel.from_table, rel.from_columns), (rel.to_table, rel.to_columns)
    if rel.kind == "one-to-many":
        many, one = one, many  # the key is on the "from" side
    notes: list[str] = []
    missing_tables = [t for t in (rel.from_table, rel.to_table) if t not in infos]
    if missing_tables:
        return out | {
            "status": "not open",
            "detail": f"Push {' and '.join(missing_tables)} to run this join.",
        }
    columns: dict[tuple[str, str], Column] = {
        (t, c.name): c for t in (rel.from_table, rel.to_table) for c in infos[t].columns
    }
    gone = [f"{t}.{c}" for t, cols in (many, one) for c in cols if (t, c) not in columns]
    if gone:
        return out | {"status": "broken", "detail": f"{', '.join(gone)} is no longer in the table."}
    for t, cols in (many, one):
        was = dict(saved.get(t, ()))
        for c in cols:
            if c in was and was[c] != columns[(t, c)].type:
                notes.append(f"{t}.{c} was {was[c]}, now {columns[(t, c)].type}")
    options = KeyOptions()
    exprs: dict[str, list[str | None]] = {}
    for which, (t, cols) in (("a", many), ("b", one)):
        sql = [normalized(columns[(t, c)], options) for c in cols]
        if any(x is None for x in sql):
            return out | {
                "status": "broken",
                "detail": f"{t}.{', '.join(cols)} holds values that are never keys.",
            }
        exprs[which] = sql
    keys = [f"k{i}" for i in range(len(many[1]))]
    a = _keyed(exprs["a"], keys, infos[many[0]].table)
    b = _keyed(exprs["b"], keys, infos[one[0]].table)
    using = ", ".join(keys)
    row = cursor.execute(
        f"WITH a AS {a}, b AS {b}, ad AS (SELECT DISTINCT * FROM a), bd AS (SELECT DISTINCT * FROM b) "
        f"SELECT (SELECT count(*) FROM a), (SELECT count(*) FROM ad), (SELECT count(*) FROM b), "
        f"(SELECT count(*) FROM bd), (SELECT count(*) FROM ad JOIN bd USING ({using}))"
    ).fetchone() or (0, 0, 0, 0, 0)
    a_rows, a_distinct, b_rows, b_distinct, found = (int(x) for x in row)
    orphan_sql = f"WITH a AS {a}, b AS {b} SELECT DISTINCT * FROM a ANTI JOIN b USING ({using})"
    samples = [
        list(r) if len(r) > 1 else r[0]
        for r in cursor.execute(f"{orphan_sql} ORDER BY ALL LIMIT 5").fetchall()
    ]
    a_unique, b_unique = a_rows == a_distinct, b_rows == b_distinct
    actual = (
        "one-to-one"
        if a_unique and b_unique
        else "many-to-one"
        if b_unique
        else "one-to-many"
        if a_unique
        else "many-to-many"
    )
    if rel.kind == "one-to-many" and actual in ("many-to-one", "one-to-many"):
        actual = "one-to-many" if actual == "many-to-one" else "many-to-one"
    orphans = a_distinct - found
    match = round(found / a_distinct, 4) if a_distinct else 0.0
    counts = {"match": match, "orphans": orphans, "orphan_samples": samples, "actual_kind": actual}
    key_side = f"{one[0]}.{', '.join(one[1])}"
    if not found:
        return (
            out
            | counts
            | {"status": "broken", "detail": "No value matches: the join finds nothing.", "notes": notes}
        )
    if rel.kind != "many-to-many" and not b_unique:
        repeats = b_rows - b_distinct
        detail = f"{key_side} is meant to be the key, but {repeats:,} of its values repeat."
        return out | counts | {"status": "broken", "detail": detail, "notes": notes}
    if actual != rel.kind:
        notes.append(f"Declared {rel.kind}; the data is {actual}.")
    if orphans:
        detail = f"{match:.1%} of values match; {orphans:,} have no row in {key_side}."
        return out | counts | {"status": "orphans", "detail": detail, "notes": notes}
    return out | counts | {"status": "holds", "detail": "Every value matches.", "notes": notes}


# ===== the relationship finder =====


def _keyed(exprs: list[str | None], names: list[str], table_sql: str) -> str:
    """A table's rows as its key columns, named k0, k1 ..., rows with a blank key left out."""
    picked = ", ".join(f"{e} AS {n}" for e, n in zip(exprs, names, strict=True))
    filled = " AND ".join(f"{n} IS NOT NULL" for n in names)
    return f"(SELECT * FROM (SELECT {picked} FROM {table_sql}) WHERE {filled})"


def find_candidates(
    cursor: SqlSession, infos: list[DatasetInfo], options: KeyOptions, row_limit: int
) -> tuple[list[Candidate], dict[str, tuple[str, ...]]]:
    """Every column of every table found in another table's unique column, with the counts the
    domain judges (each dataset as pushed); and the two-column keys of tables with no single one."""
    profiles: dict[str, list[tuple[ColumnStats, str]]] = {}
    for info in infos:
        profiles[info.name], _ = profile_side(cursor, f"SELECT * FROM {info.table}", info.columns, options)
    position = {(i.name, c.name): n for i in infos for n, c in enumerate(i.columns)}
    family = {(i.name, c.name): key_family(c.type) for i in infos for c in i.columns}
    candidates: list[Candidate] = []
    for a in infos:
        for b in infos:
            left, keys = profiles[a.name], [p for p in profiles[b.name] if p[0].unique]
            if a.name == b.name or not left or not keys:
                continue
            sql = (
                f"WITH a AS ({_value_list(f'SELECT * FROM {a.table}', left)}), "
                f"b AS ({_value_list(f'SELECT * FROM {b.table}', keys)}) "
                "SELECT a.c, b.c, count(*) FROM a JOIN b USING (v) GROUP BY ALL"
            )
            for li, ri, shared in cursor.execute(sql).fetchall():
                s, t = left[int(li)][0], keys[int(ri)][0]
                candidates.append(
                    Candidate(
                        a.name,
                        (s.name,),
                        b.name,
                        (t.name,),
                        s.distinct,
                        t.distinct,
                        int(shared),
                        s.unique,
                        True,
                        position[(a.name, s.name)],
                        (family[(a.name, s.name)], family[(b.name, t.name)]),
                    )
                )
    composite: dict[str, tuple[str, ...]] = {}
    for info in infos:
        if any(stats.unique for stats, _ in profiles[info.name]):
            continue
        # Only columns that can be keys: the greedy search starts with the most varied column, and a
        # price (a float) is the most varied column of an order line, never part of its key.
        keyish = tuple(c for c in info.columns if key_family(c.type) not in ("float", "boolean"))
        found = most_distinct(cursor, f"SELECT * FROM {info.table}", keyish, options, row_limit)
        last = found["steps"][-1] if found["steps"] else None
        if last and last["unique"] and 2 <= len(last["columns"]) <= 4 and not found["limited"]:
            composite[info.name] = tuple(last["columns"])
    by_name = {i.name: i for i in infos}
    for table, key in composite.items():
        target = by_name[table]
        target_cols = {c.name: c for c in target.columns}
        for src in infos:
            if src.name == table:
                continue
            mapped: list[str] = []
            for k in key:
                likeness, best = max(
                    ((name_likeness(c.name, table, k), c.name) for c in src.columns), default=(0.0, "")
                )
                if likeness < 0.9 or best in mapped:
                    break
                mapped.append(best)
            if len(mapped) != len(key):
                continue
            src_cols = {c.name: c for c in src.columns}
            left_sql = [normalized(src_cols[m], options) for m in mapped]
            right_sql = [normalized(target_cols[k], options) for k in key]
            if any(x is None for x in (*left_sql, *right_sql)):
                continue
            names = [f"k{i}" for i in range(len(key))]

            using = ", ".join(names)
            row = cursor.execute(
                f"WITH a AS {_keyed(left_sql, names, src.table)}, "
                f"b AS {_keyed(right_sql, names, target.table)}, "
                "ad AS (SELECT DISTINCT * FROM a), bd AS (SELECT DISTINCT * FROM b) "
                f"SELECT (SELECT count(*) FROM a), (SELECT count(*) FROM ad), (SELECT count(*) FROM bd), "
                f"(SELECT count(*) FROM ad JOIN bd USING ({using}))"
            ).fetchone() or (0, 0, 0, 0)
            a_rows, a_distinct, b_distinct, shared = (int(x) for x in row)
            if a_distinct:
                candidates.append(
                    Candidate(
                        src.name,
                        tuple(mapped),
                        table,
                        key,
                        a_distinct,
                        b_distinct,
                        shared,
                        a_rows == a_distinct,
                        True,
                        position[(src.name, mapped[0])],
                        (family[(src.name, mapped[0])], family[(table, key[0])]),
                    )
                )
    return candidates, composite


class Finder:
    """The relationship finder: which joins hold between the open tables (the domain judges).
    Suggestions the person rejects are not offered again while the server runs."""

    def __init__(self, ws: Workspace, models: Models) -> None:
        self.ws = ws
        self.models = models
        self.rejected: set[tuple[str, tuple[str, ...], str, tuple[str, ...]]] = set()

    async def find(self, body: Any, who: str) -> dict[str, Any]:
        body = body if isinstance(body, dict) else {}
        wanted = body.get("tables")
        infos = [i for i in self.ws.store.all() if not wanted or i.name in wanted]
        if len(infos) < 2:
            raise ValueError("Open (push) at least two tables to find how they relate.")
        if len(infos) > 40:
            raise ValueError('Pick at most 40 tables ("tables": [...]).')
        settings = self.ws.settings
        cursor = self.ws.store.cursor()
        started = time.monotonic()

        def job() -> tuple[list[Candidate], dict[str, tuple[str, ...]]]:
            timer = threading.Timer(settings.key_finder_timeout_seconds * 2, cursor.interrupt)
            timer.start()
            try:
                return find_candidates(cursor, infos, KeyOptions(), settings.key_finder_unique_rows)
            finally:
                timer.cancel()

        try:
            candidates, composite = await self.ws.workers.run(AGENT if who == "agent" else BACKGROUND, job)
        except self.ws.interrupted:
            limit = settings.key_finder_timeout_seconds * 2
            raise TimedOut(f"The finder stopped after {limit:.0f} seconds: pick fewer tables.") from None
        found = suggest(candidates)
        current = self.models.model.relationships
        day = datetime.now(UTC).date().isoformat()
        suggestions: list[dict[str, Any]] = []
        for s in found:
            r = s.relationship
            if (r.from_table, r.from_columns, r.to_table, r.to_columns) in self.rejected and not body.get(
                "include_rejected"
            ):
                continue
            evidence = f"Found {day}: {s.containment:.0%} of values in {r.to_table}" + (
                f", {s.orphans:,} orphans" if s.orphans else ""
            )
            known = any(r.same_join(c) for c in current)
            suggestions.append(s.to_json() | {"evidence": evidence, "known": known})
        keys = {s.relationship.to_table: list(s.relationship.to_columns) for s in found} | {
            t: list(k) for t, k in composite.items()
        }
        self.ws.audit.write(who, "find_relationships", None, tables=len(infos), found=len(suggestions))
        return {
            "tables": [i.name for i in infos],
            "suggestions": suggestions,
            "keys": keys,
            "rejected": len(self.rejected),
            "seconds": round(time.monotonic() - started, 2),
        }

    def reject(self, body: Any) -> dict[str, Any]:
        """Do not suggest this join again (while the server runs); `undo` brings it back."""
        if not isinstance(body, dict):
            raise ValueError('Send {"from", "from_columns", "to", "to_columns"}.')
        r = parse_model({"relationships": [body | {"kind": body.get("kind", "many-to-one")}]}).relationships[
            0
        ]
        key = (r.from_table, r.from_columns, r.to_table, r.to_columns)
        if body.get("undo"):
            self.rejected.discard(key)
        else:
            self.rejected.add(key)
        return {"rejected": len(self.rejected)}
