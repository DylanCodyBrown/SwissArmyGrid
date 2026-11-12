"""Compare routes: two grids side by side, aligned on key columns (engine/compare.py).

  POST   /compares                 {"left", "right", "keys": [[l, r], ...], "mode", "anti_side"}
  GET    /compares/{id}            the compare, with its counts (matched, left only, right only)
  PATCH  /compares/{id}            change keys, mode or anti_side
  DELETE /compares/{id}
  GET    /compares/{id}/rows?side=left|right&start=&end=
         that side's rows in compare order: placeholders where the other side has the key and
         this side doesn't, and the cells that differ from the aligned row on the other side
  GET    /compares/{id}/key-group?row=N
         the compare rows sharing row N's key (one block: the mapping is sorted by key)
  POST   /compares/{id}/focus      {"side": picked side, "position": its view row}   (cross compare)
  DELETE /compares/{id}/focus

The aligned mapping (compare row -> left position, right position) is materialized as one small
table per compare, rebuilt only when either grid's steps, settings or data change, so scrolling
reads windows from it like any grid view.
"""

# The SQL here is built from generated table names, the grids' own compiled view SQL and integers.
# ruff: noqa: S608

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any

import duckdb
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.engine.compare import (
    CompareSpec,
    counts,
    differences,
    focus_positions,
    mapping_sql,
    rows_at,
)
from swissarmygrid.engine.datasets import Column, DatasetInfo
from swissarmygrid.engine.workers import INTERACTIVE
from swissarmygrid.server.context import Context, actor, error_response, json_response, query_int, read_json
from swissarmygrid.server.grids import Grid
from swissarmygrid.server.level_zero import probe_refusal
from swissarmygrid.server.routes_agent import Policy, agent_may_see_raw_values, raw_values_refused


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


@dataclass(frozen=True)
class Side:
    grid: Grid
    info: DatasetInfo
    positioned: str
    columns: tuple[Column, ...]


def routes(ctx: Context, policy: Policy) -> list[BaseRoute]:
    compares: dict[str, Compare] = {}

    def side(cur: duckdb.DuckDBPyConnection, grid_id: str) -> Side:
        grid, info = ctx.grid_and_info(grid_id)
        positioned, columns = ctx.views.positioned_sql(cur, info, grid.steps, grid.settings)
        return Side(grid, info, positioned, columns)

    def signature(left: Side, right: Side, spec: CompareSpec) -> str:
        def one(s: Side) -> Any:
            return [
                s.info.name,
                s.info.version,
                [st.to_json() for st in s.grid.steps],
                s.grid.settings.to_json(),
            ]

        return json.dumps([one(left), one(right), spec.to_json()], default=str)

    def ensure(cur: duckdb.DuckDBPyConnection, cmp: Compare) -> tuple[Side, Side]:
        """Both sides, with the mapping table and the counts up to date."""
        left, right = side(cur, cmp.left), side(cur, cmp.right)
        sig = signature(left, right, cmp.spec)
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

    def describe(cmp: Compare, request: Request) -> dict[str, Any]:
        focus: dict[str, Any] | None = None
        if cmp.focus is not None:
            focus = {"side": cmp.focus[0], "rows": len(cmp.focus[1])}
            # The picked row's key is its cell values: for the page, and for an agent at level 3 only
            # (below it, picking row after row would read the key column one value at a time).
            if agent_may_see_raw_values(request, policy):
                focus["key"] = cmp.focus[2]
        return {
            "compare": cmp.id,
            "left": cmp.left,
            "right": cmp.right,
            **cmp.spec.to_json(),
            "row_count": cmp.row_count,
            "counts": cmp.tallies,
            "focus": focus,
        }

    def get(request: Request) -> Compare:
        cmp = compares.get(request.path_params["compare_id"])
        if cmp is None:
            raise LookupError("No such compare; it may have been stopped or the server restarted.")
        return cmp

    def probe_refused(request: Request, left: str, right: str) -> Response | None:
        """At level 0, an agent's compare of a table it made (values it chose) against the user's:
        the matched counts would tell whether the user's data has those values. Checked on every
        count, since a compare recounts after either grid changes."""
        if actor(request) != "agent" or policy.level != 0:
            return None
        grids = [g for g in (ctx.grids.get(left), ctx.grids.get(right)) if g is not None]
        why = probe_refusal(ctx, *grids)
        return None if why is None else Response(why, status_code=403)

    async def run(fn: Any) -> Any:
        return await ctx.workers.run(INTERACTIVE, lambda: fn(ctx.store.cursor()))

    async def create(request: Request) -> Response:
        try:
            body = await read_json(request)
            if not isinstance(body, dict) or not all(isinstance(body.get(k), str) for k in ("left", "right")):
                raise ValueError('Send {"left": grid id, "right": grid id, "keys": [[left col, right col]]}.')
            if body["left"] == body["right"]:
                raise ValueError("Compare two different grids.")
            if (refused := probe_refused(request, body["left"], body["right"])) is not None:
                return refused
            spec = CompareSpec.from_json(body)
            cmp = Compare(secrets.token_hex(4), body["left"], body["right"], spec)
            await run(lambda cur: ensure(cur, cmp))
            compares[cmp.id] = cmp
            ctx.audit.write(
                actor(request), "compare", f"{cmp.left}:{cmp.right}", keys=len(spec.keys), mode=spec.mode
            )
            return json_response(describe(cmp, request))
        except Exception as error:
            return error_response(error)

    async def read(request: Request) -> Response:
        try:
            cmp = get(request)
            if (refused := probe_refused(request, cmp.left, cmp.right)) is not None:
                return refused
            await run(lambda cur: ensure(cur, cmp))
            return json_response(describe(cmp, request))
        except Exception as error:
            return error_response(error)

    async def change(request: Request) -> Response:
        try:
            cmp = get(request)
            if (refused := probe_refused(request, cmp.left, cmp.right)) is not None:
                return refused
            body = await read_json(request)
            if not isinstance(body, dict):
                raise ValueError("Send the fields to change: keys, mode, anti_side.")
            merged = cmp.spec.to_json() | {
                k: v for k, v in body.items() if k in ("keys", "mode", "anti_side")
            }
            cmp.spec = CompareSpec.from_json(merged)
            cmp.focus = None
            cmp.signature = ""
            await run(lambda cur: ensure(cur, cmp))
            return json_response(describe(cmp, request))
        except Exception as error:
            return error_response(error)

    async def remove(request: Request) -> Response:
        try:
            cmp = compares.pop(request.path_params["compare_id"], None)
            if cmp is not None:
                await run(lambda cur: cur.execute(f"DROP TABLE IF EXISTS {cmp.table}"))
            return json_response({"stopped": request.path_params["compare_id"]})
        except Exception as error:
            return error_response(error)

    async def rows(request: Request) -> Response:
        from swissarmygrid.server.routes_grids import _truncate  # the grid page's own cell cut

        if not agent_may_see_raw_values(request, policy):
            return raw_values_refused()
        try:
            cmp = get(request)
            which = request.query_params.get("side")
            if which not in ("left", "right"):
                raise ValueError("side must be left or right.")
            start = max(0, query_int(request, "start", 0) or 0)
            end = max(
                start, min(query_int(request, "end", start + 100) or 0, start + ctx.settings.max_window_rows)
            )

            def compute(cur: duckdb.DuckDBPyConnection) -> dict[str, Any]:
                left, right = ensure(cur, cmp)
                mine = left if which == "left" else right
                if cmp.spec.mode == "cross":
                    if cmp.focus and cmp.focus[0] == which:
                        positions: list[int | None] = list(cmp.focus[1][start:end])
                        total = len(cmp.focus[1])
                        got = rows_at(cur, mine.positioned, mine.columns, positions)
                    else:
                        window = ctx.views.window(
                            cur, mine.info, mine.grid.steps, mine.grid.settings, start, end
                        )
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

            result = await run(compute)
            result["truncated"] = _truncate(result["rows"], ctx.settings.max_cell_chars)
            result["counts"] = cmp.tallies
            return json_response(result, max_bytes=ctx.settings.max_response_bytes)
        except Exception as error:
            return error_response(error)

    async def key_group(request: Request) -> Response:
        """Rows `row`'s key also covers: one block, since the mapping is sorted by key. A blank key
        matches nothing, so it is a block of one."""
        if not agent_may_see_raw_values(request, policy):
            return raw_values_refused()
        try:
            cmp = get(request)
            row = query_int(request, "row")
            if row is None or row < 0:
                raise ValueError("Give row.")
            if cmp.spec.mode == "cross":
                raise ValueError("Cross compare has no shared rows; pick a row to filter the other side.")

            def compute(cur: duckdb.DuckDBPyConnection) -> dict[str, Any]:
                ensure(cur, cmp)
                names = [f"s{i}" for i in range(len(cmp.spec.keys))]
                key = cur.execute(
                    f"SELECT {', '.join(names)} FROM {cmp.table} WHERE cpos = ?", [row]
                ).fetchone()
                if key is None:
                    raise LookupError("No such compare row.")
                if any(v is None for v in key):
                    return {"first": row, "last": row, "key": list(key)}
                where = " AND ".join(f"{n} = ?" for n in names)
                span = cur.execute(
                    f"SELECT min(cpos), max(cpos) FROM {cmp.table} WHERE {where}", list(key)
                ).fetchone() or (row, row)  # an aggregate always returns one row
                return {"first": int(span[0]), "last": int(span[1]), "key": list(key)}

            return json_response(await run(compute))
        except Exception as error:
            return error_response(error)

    async def set_focus(request: Request) -> Response:
        try:
            cmp = get(request)
            if (refused := probe_refused(request, cmp.left, cmp.right)) is not None:
                return refused
            body = await read_json(request)
            picked = body.get("side") if isinstance(body, dict) else None
            position = body.get("position") if isinstance(body, dict) else None
            if picked not in ("left", "right") or not isinstance(position, int):
                raise ValueError('Send {"side": "left" | "right", "position": the picked row}.')

            row: int = position

            def compute(cur: duckdb.DuckDBPyConnection) -> None:
                left, right = ensure(cur, cmp)
                at = row
                # A row picked on the side that is itself filtered: its place in that side's view.
                if cmp.focus and cmp.focus[0] == picked and 0 <= at < len(cmp.focus[1]):
                    at = cmp.focus[1][at]
                a, b = (left, right) if picked == "left" else (right, left)
                ka = [k[0] if picked == "left" else k[1] for k in cmp.spec.keys]
                kb = [k[1] if picked == "left" else k[0] for k in cmp.spec.keys]
                hits, key = focus_positions(cur, a.positioned, a.columns, ka, at, b.positioned, b.columns, kb)
                cmp.focus = ("right" if picked == "left" else "left", hits, key)

            await run(compute)
            return json_response(describe(cmp, request))
        except Exception as error:
            return error_response(error)

    async def clear_focus(request: Request) -> Response:
        try:
            cmp = get(request)
            cmp.focus = None
            return json_response(describe(cmp, request))
        except Exception as error:
            return error_response(error)

    return [
        Route("/compares", create, methods=["POST"]),
        Route("/compares/{compare_id}", read),
        Route("/compares/{compare_id}", change, methods=["PATCH"]),
        Route("/compares/{compare_id}", remove, methods=["DELETE"]),
        Route("/compares/{compare_id}/rows", rows),
        Route("/compares/{compare_id}/key-group", key_group),
        Route("/compares/{compare_id}/focus", set_focus, methods=["POST"]),
        Route("/compares/{compare_id}/focus", clear_focus, methods=["DELETE"]),
    ]
