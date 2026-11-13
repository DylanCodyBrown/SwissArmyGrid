"""Relationships between tables: the Key Finder, the comparison of two grids, and the current
relationship model with its library.

The rules (what a key is, how values are normalized, the score, how two grids line up, what a model
is) are the domain's (domain/relationships.py). Here they are run, through the `SqlSession` port, on
the relations the caller hands in (each grid's view), and the use cases are at the end:

  KeyFinder   possible join columns between this grid and other grids, and this grid's own key
  Compares    two grids side by side, aligned row for row on key columns
  Models      the current model (keys and relationships), the shared library, the ERD

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
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any, Protocol

from swissarmygrid.domain.datasets import Column, DatasetInfo
from swissarmygrid.domain.grids import POS, Grid
from swissarmygrid.domain.relationships import (
    MAX_ALTERNATIVES,
    MAX_KEY_COLUMNS,
    ColumnStats,
    CompareSpec,
    KeyOptions,
    Model,
    _joined,
    _value_list,
    differences,
    keyed_sql,
    mapping_sql,
    merge,
    mermaid_erd,
    normalized,
    parse_model,
    relationship,
    score,
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
    """The library of model files (adapters/state_files.py)."""

    def folders(self) -> list[str]: ...

    def read(self) -> tuple[dict[str, tuple[Model, str]], list[str]]: ...


class Models:
    """The current model for this server, shared with the pages (and the agent): a page redraws on
    the relationships_updated notice. Agents may set relationships too: keys and joins are metadata,
    never cell values."""

    def __init__(self, ws: Workspace, library: ModelLibrary) -> None:
        self.ws = ws
        self._library = library
        self.model = Model()

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
                    "file": path,
                    "tables": sorted(model.tables()),
                    "open_tables": sorted(model.tables() & open_names),
                    "relationships": len(model.relationships),
                }
                for name, (model, path) in sorted(found.items())
            ],
            "folders": self._library.folders(),
            "problems": problems,
        }

    async def apply_library(self, name: str, who: str) -> dict[str, Any]:
        """Apply (merge) one library model."""
        found, _ = self._library.read()
        entry = found.get(name)
        if entry is None:
            raise NotFound(f"No model named {name!r} in the library.")
        self.model = merge(self.model, entry[0])
        return await self._changed(who, entry[0].name)

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
