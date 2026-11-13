"""Key Finder routes: possible join columns between this grid and other grids (engine/keyfinder.py).

Both sides are grid views (each after its own steps), so a user can filter two grids down
first and then ask how they join. The target is one grid, or "*" for every other open grid.

  POST /grids/{id}/key-finder         {"target": grid id | "*", "options": {...}}
       -> pairs (column, target grid, column, shared values, % found each way, score), best first,
          and unique_key: the column combination that tells this grid's first rows apart
          (settings.key_finder_unique_rows), as the build steps plus equally small alternatives
  POST /grids/{id}/key-finder/sample  {"target", "source_column", "target_column", "options"}
       -> a few shared values and a few this column has that the target lacks (page only)

Pairs carry counts, never values. The value samples are for the page (the agent reads values
through its own query tool).
"""

from __future__ import annotations

import threading
from contextlib import ExitStack
from typing import Any

import duckdb
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.domain.grids import Grid
from swissarmygrid.engine.keyfinder import KeyOptions, find_keys, most_distinct, sample_values
from swissarmygrid.server.context import Context, actor, error_response, json_response, read_json
from swissarmygrid.services.ports import AGENT, BACKGROUND

MAX_PAIRS: int = 200


def routes(ctx: Context) -> list[BaseRoute]:
    settings = ctx.settings

    def view(
        cur: duckdb.DuckDBPyConnection, grid: Grid, held: ExitStack, ordered: bool = False
    ) -> tuple[str, Any, Any]:
        """A grid's view as a SQL relation, with its columns and dataset info. It reads the view
        cache (not every step again), held until `held` closes; `ordered`: in view order."""
        _, info = ctx.grid_and_info(grid.id)
        compiled = ctx.compiled(cur, grid, info)
        sql = held.enter_context(ctx.views.reading(cur, info, grid.steps, grid.settings, ordered=ordered))
        return sql, compiled.columns, info

    def label(grid: Grid) -> str:
        steps = len([s for s in grid.steps if s.enabled])
        return f"{grid.dataset}" + (f" ({steps} step{'s' if steps != 1 else ''})" if steps else "")

    def targets(source: Grid, target: Any) -> list[Grid]:
        if target == "*":
            return [g for g in ctx.grids.all() if g.id != source.id]
        if not isinstance(target, str):
            raise ValueError('Send {"target": "<grid id>"} or {"target": "*"} for every other grid.')
        found = ctx.grids.get(target)
        if found is None:
            raise LookupError(f"No grid {target!r}; open one on the dataset first.")
        return [found]

    def timed(cur: duckdb.DuckDBPyConnection, fn: Any) -> Any:
        timer = threading.Timer(settings.key_finder_timeout_seconds, cur.interrupt)
        timer.start()
        try:
            return fn()
        finally:
            timer.cancel()

    async def candidates(request: Request) -> Response:
        """The grids a Key Finder can compare against (for the page's picker)."""
        try:
            source, _ = ctx.grid_and_info(request.path_params["grid_id"])
            return json_response(
                {
                    "grids": [
                        {"grid": g.id, "dataset": g.dataset, "label": label(g), "self": g.id == source.id}
                        for g in ctx.grids.all()
                    ]
                }
            )
        except Exception as error:
            return error_response(error)

    async def run(request: Request) -> Response:
        try:
            source, _ = ctx.grid_and_info(request.path_params["grid_id"])
            body = await read_json(request)
            body = body if isinstance(body, dict) else {}
            options = KeyOptions.from_json(body.get("options"))
            chosen = targets(source, body.get("target", "*"))
            who = actor(request)

            def compute(cur: duckdb.DuckDBPyConnection) -> dict[str, Any]:
                with ExitStack() as held:
                    return search(cur, held)

            def search(cur: duckdb.DuckDBPyConnection, held: ExitStack) -> dict[str, Any]:
                src_sql, src_cols, src_info = view(cur, source, held)
                pairs: list[dict[str, Any]] = []
                skipped: dict[str, Any] = {}
                checked: list[str] = []
                for target in chosen:
                    with ExitStack() as held_target:
                        tgt_sql, tgt_cols, tgt_info = view(cur, target, held_target)
                        found = timed(
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
                                "target": label(target),
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
                first_sql, _, _ = view(cur, source, held, ordered=True)
                try:
                    unique_key: dict[str, Any] = timed(
                        cur, lambda: most_distinct(cur, first_sql, src_cols, options, limit)
                    )
                except duckdb.InterruptException:
                    unique_key = {"stopped": True, "row_limit": limit}
                return {
                    "grid": source.id,
                    "source": source.dataset,
                    "pairs": pairs[:MAX_PAIRS],
                    "pair_count": len(pairs),
                    "source_checked": checked,
                    "skipped": skipped,
                    "targets": [{"grid": t.id, "label": label(t), "dataset": t.dataset} for t in chosen],
                    "unique_key": unique_key,
                    "options": {
                        "trim": options.trim,
                        "ignore_case": options.ignore_case,
                        "max_length": options.max_length,
                    },
                }

            try:
                result = await ctx.grid_query(source, None, AGENT if who == "agent" else BACKGROUND, compute)
            except duckdb.InterruptException:
                return Response(
                    f"The Key Finder stopped after {settings.key_finder_timeout_seconds:.0f} seconds. "
                    "Filter the grids first, or pick one target grid.",
                    status_code=408,
                )
            ctx.audit.write(
                who, "key_finder", source.dataset, targets=len(chosen), pairs=result["pair_count"]
            )
            return json_response(result)
        except Exception as error:
            return error_response(error)

    async def sample(request: Request) -> Response:
        try:
            if actor(request) == "agent":
                return Response("Value samples are for the grid page.", status_code=403)
            source, _ = ctx.grid_and_info(request.path_params["grid_id"])
            body = await read_json(request)
            body = body if isinstance(body, dict) else {}
            target = targets(source, body.get("target"))[0]
            options = KeyOptions.from_json(body.get("options"))

            def compute(cur: duckdb.DuckDBPyConnection) -> dict[str, Any]:
                with ExitStack() as held:
                    src_sql, src_cols, _ = view(cur, source, held)
                    tgt_sql, tgt_cols, _ = view(cur, target, held)
                    a = next((c for c in src_cols if c.name == body.get("source_column")), None)
                    b = next((c for c in tgt_cols if c.name == body.get("target_column")), None)
                    if a is None or b is None:
                        raise ValueError("Name a column of each grid.")
                    sampled: dict[str, Any] = timed(
                        cur, lambda: sample_values(cur, src_sql, a, tgt_sql, b, options)
                    )
                    return sampled

            return json_response(await ctx.grid_query(source, None, BACKGROUND, compute))
        except Exception as error:
            return error_response(error)

    return [
        Route("/grids/{grid_id}/key-finder", candidates),
        Route("/grids/{grid_id}/key-finder", run, methods=["POST"]),
        Route("/grids/{grid_id}/key-finder/sample", sample, methods=["POST"]),
    ]
