"""The table the user is looking at, for an agent that never asked (the "current table" context).

Claude's IDE integrations hand the model the open file and the selection with every prompt, for
free: no tool call, no tokens spent finding out. This is the same idea for a grid. The workspace
page reports which grid tab is in front (POST /workspace/active); an agent reads a short summary
of it (GET /agent/context) through the MCP `current_table` tool, or through the plugin's
UserPromptSubmit hook (`swissarmygrid-context`), which adds it to every prompt.

The summary is metadata: the dataset, the grid's row count, its columns and types, its steps and
the selection size, plus the other open tables. No cell values: it is a summary, and the agent
reads rows through its own tools when it needs them.
"""

from __future__ import annotations

from typing import Any

import duckdb
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.engine.workers import AGENT
from swissarmygrid.server.context import Context, actor, error_response, json_response, read_json


def routes(ctx: Context) -> list[BaseRoute]:
    active: dict[str, str | None] = {"grid": None}

    async def set_active(request: Request) -> Response:
        if actor(request) == "agent":
            return Response("Only the page reports which grid is in front.", status_code=403)
        try:
            body = await read_json(request)
            grid_id = body.get("grid") if isinstance(body, dict) else None
            active["grid"] = grid_id if isinstance(grid_id, str) and ctx.grids.get(grid_id) else None
            return json_response({"grid": active["grid"]})
        except Exception as error:
            return error_response(error)

    async def current(request: Request) -> Response:
        try:
            grid = ctx.grids.get(active["grid"]) if active["grid"] else None
            others = [
                {
                    "dataset": i.name,
                    "rows": i.row_count,
                    "grids": [g.id for g in ctx.grids.for_dataset(i.name)],
                }
                for i in sorted(ctx.store.all(), key=lambda i: i.name)
            ]
            out: dict[str, Any] = {"active": None, "open_tables": others}
            info = ctx.store.get(grid.dataset) if grid else None
            if grid is None or info is None:
                return json_response(out)

            def describe(cur: duckdb.DuckDBPyConnection) -> tuple[int | None, list[dict[str, str]]]:
                try:
                    n: int | None = ctx.views.row_count(cur, info, grid.steps, grid.settings, None)
                    columns = ctx.compiled(cur, grid, info).columns
                except duckdb.Error:
                    return None, [{"name": c.name, "type": c.type} for c in info.columns]
                return n, [{"name": c.name, "type": c.type} for c in columns]

            row_count, columns = await ctx.grid_query(grid, None, AGENT, describe)
            steps = [s.to_json() for s in grid.steps]
            out["active"] = {
                "grid": grid.id,
                "dataset": info.name,
                "dataset_rows": info.row_count,
                "view_rows": row_count,
                "columns": columns,
                "steps": steps,
                "selected_rows": grid.selected,
            }
            return json_response(out)
        except Exception as error:
            return error_response(error)

    return [
        Route("/workspace/active", set_active, methods=["POST"]),
        Route("/agent/context", current),
    ]
