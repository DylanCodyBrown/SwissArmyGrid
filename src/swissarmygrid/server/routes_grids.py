"""Grid routes: everything a grid page (or an agent, through MCP) does with one grid.

  GET    /grids                          every open grid
  POST   /grids                          {"dataset": name} open another grid on a dataset
  GET    /grids/{id}                     description: dataset, columns, steps, settings, errors
  DELETE /grids/{id}                     close the grid (the dataset stays)
  GET    /grids/{id}/rows?start=&end=&gen=&upto=    one window, in view order
  GET    /grids/{id}/count?gen=&upto=    rows in the view
  GET    /grids/{id}/cell?row=&column=&upto=        one full, untruncated value
  GET    /grids/{id}/step-counts         rows after each step ("12,000 -> 11,420")
  GET    /grids/{id}/profile?column=&upto=          column profile, or the whole-table profile
  GET    /grids/{id}/header-stats        distinct count and blank percent per column
  GET    /grids/{id}/categorize-stats?step=         unmatched and overlapping rows of a rule set
  POST   /grids/{id}/steps               {"steps": [...], "settings": {...}, "gen": n}
  POST   /grids/{id}/step-ops            {"op": add|edit|delete|toggle|move, "index"|"id", ...} one step
  POST   /grids/{id}/header-sort         {"keys": [["col", "asc"]], "gen": n}
  POST   /grids/{id}/sql                 {"sql": "..."} guarded; CREATE TABLE AS pins a new grid
  GET    /grids/{id}/export?format=&scope=&...      a download (or ?save=1 into the export folder)
  GET    /grids/{id}/code?target=duckdb|pandas|plain   generated, verified Python
  POST   /grids/{id}/recipe              save this grid's steps for its dataset name
  DELETE /grids/{id}/recipe              forget the saved recipe
  POST   /grids/{id}/cancel              stop every query running for this grid
  POST   /grids/{id}/selection-stats     {"selection": ...} Count, Sum and Average of a selection
  POST   /grids/{id}/highlights          {"highlights": [...]} mark the rows matching conditions
  POST   /grids/{id}/selection           {"count": n} the page reports how many rows are selected

Each route reads the request, finds the grid, and hands over to its use case (services/grids.py,
profile.py, sql.py, export.py, codegen.py).

Every query runs on the worker pool: grid windows at interactive priority, profiles, SQL and
exports behind them. Requests carrying a `gen` older than the grid's latest are dropped with
409 (domain/grids.py), and the page ignores stale answers by gen too.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import duckdb
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import BaseRoute, Route

from swissarmygrid.domain.steps import StepError
from swissarmygrid.server.http import actor, client_of, error_response, json_response, query_int, read_json
from swissarmygrid.services.codegen import CodeService
from swissarmygrid.services.export import Exports
from swissarmygrid.services.grids import GridService
from swissarmygrid.services.profile import Profiles
from swissarmygrid.services.sql import SqlBox


def routes(
    grids: GridService, profiles: Profiles, sql: SqlBox, exports: Exports, codes: CodeService
) -> list[BaseRoute]:
    settings = grids.ws.settings

    # ===== grid lifecycle =====

    async def list_grids(request: Request) -> Response:
        return json_response(grids.listing(actor(request)))

    async def create_grid(request: Request) -> Response:
        try:
            body = await read_json(request)
            name = body.get("dataset") if isinstance(body, dict) else None
            return json_response(await grids.create(name, actor(request)))
        except Exception as error:
            return error_response(error)

    async def get_grid(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            return json_response(await grids.describe(grid, info))
        except Exception as error:
            return error_response(error)

    async def close_grid(request: Request) -> Response:
        closed = await grids.close(request.path_params["grid_id"])
        if closed is None:
            return Response("No such grid.", status_code=404)
        return json_response(closed)

    # ===== reading rows =====

    async def rows(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            start = query_int(request, "start", 0) or 0
            end = query_int(request, "end", start + 100) or 0
            gen = query_int(request, "gen")
            upto = query_int(request, "upto")
            return json_response(
                await grids.rows(grid, info, start, end, gen, upto, client_of(request)),
                max_bytes=settings.max_response_bytes,
            )
        except Exception as error:
            return error_response(error)

    async def count(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            upto = query_int(request, "upto")
            return json_response(
                await grids.count(grid, info, query_int(request, "gen"), upto, client_of(request))
            )
        except Exception as error:
            return error_response(error)

    async def cell(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            row = query_int(request, "row")
            return json_response(await grids.cell(grid, info, row, request.query_params.get("column")))
        except (KeyError, IndexError):
            return Response("No such cell.", status_code=404)
        except Exception as error:
            return error_response(error)

    async def step_counts(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            return json_response(
                await grids.step_counts(grid, info, query_int(request, "gen"), client_of(request))
            )
        except Exception as error:
            return error_response(error)

    # ===== profiling (plan section 8) =====

    async def profile(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            column_name = request.query_params.get("column")
            upto = query_int(request, "upto")
            return json_response(await profiles.profile(grid, info, column_name, upto, actor(request)))
        except Exception as error:
            return error_response(error)

    async def stats(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            return json_response(
                await profiles.header_stats(grid, info, query_int(request, "gen"), client_of(request))
            )
        except Exception as error:
            return error_response(error)

    async def categorize_stats(request: Request) -> Response:
        """Unmatched and multiply-matched rows for a categorize step, measured on its input."""
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            return json_response(await grids.categorize_stats(grid, info, query_int(request, "step")))
        except Exception as error:
            return error_response(error)

    # ===== changing the view =====

    async def put_steps(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            body = await read_json(request)
            return json_response(await grids.put_steps(grid, info, body, actor(request)))
        except StepError as error:
            return Response(str(error), status_code=400)
        except Exception as error:
            return error_response(error)

    async def step_op(request: Request) -> Response:
        """One change to one step, by its id or index (services/grids.step_op has the shapes)."""
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            body = await read_json(request)
            return json_response(await grids.step_op(grid, info, body, actor(request)))
        except StepError as error:
            return Response(str(error), status_code=400)
        except Exception as error:
            return error_response(error)

    async def header_sort(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            body = await read_json(request)
            return json_response(await grids.header_sort(grid, info, body, actor(request)))
        except Exception as error:
            return error_response(error)

    # ===== SQL box (plan sections 6 and 10) =====

    async def run_sql(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            body = await read_json(request)
            try:
                answer = await sql.run(grid, info, body, actor(request))
            except duckdb.Error as error:
                return Response(f"DuckDB: {error}", status_code=400)
            if "pinned" in answer:
                return json_response(answer)
            return json_response(answer, max_bytes=settings.max_sql_response_bytes)
        except Exception as error:
            return error_response(error)

    async def selection_stats(request: Request) -> Response:
        """Excel's status bar for any selection, loaded rows or not: Count, Sum, Average."""
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            body = await read_json(request)
            raw = body.get("selection") if isinstance(body, dict) else None
            return json_response(
                await grids.selection_stats(grid, info, raw, query_int(request, "gen"), client_of(request))
            )
        except Exception as error:
            return error_response(error)

    async def cancel(request: Request) -> Response:
        try:
            grid, _ = grids.find(request.path_params["grid_id"])
            return json_response(grids.cancel(grid))
        except Exception as error:
            return error_response(error)

    async def set_highlights(request: Request) -> Response:
        """Mark rows matching conditions (same shape as filter params, plus a label)."""
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            body = await read_json(request)
            return json_response(await grids.set_highlights(grid, info, body, actor(request)))
        except Exception as error:
            return error_response(error)

    async def set_selection(request: Request) -> Response:
        try:
            grid, _ = grids.find(request.path_params["grid_id"])
            return json_response(grids.set_selection(grid, await read_json(request)))
        except Exception as error:
            return error_response(error)

    # ===== export (plan section 10a) =====

    async def export(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            made = await exports.export(grid, info, dict(request.query_params), actor(request))
            if made.saved is not None:
                return json_response({"saved": made.saved, "message": made.message})
            data = made.data

            async def stream() -> AsyncIterator[bytes]:
                for i in range(0, len(data), 1 << 20):
                    yield data[i : i + (1 << 20)]

            return StreamingResponse(
                stream(),
                media_type=made.media_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{made.file_name}"',
                    "X-Export-Message": made.message,
                },
            )
        except Exception as error:
            return error_response(error)

    # ===== code generation (plan section 9) =====

    async def code(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            return json_response(
                await codes.code(grid, info, request.query_params.get("target"), actor(request))
            )
        except Exception as error:
            return error_response(error)

    # ===== recipes =====

    async def recipe(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            if request.method == "DELETE":
                return json_response(grids.delete_recipe(info))
            return json_response(grids.save_recipe(grid, info, actor(request)))
        except Exception as error:
            return error_response(error)

    return [
        Route("/grids", list_grids),
        Route("/grids", create_grid, methods=["POST"]),
        Route("/grids/{grid_id}", get_grid),
        Route("/grids/{grid_id}", close_grid, methods=["DELETE"]),
        Route("/grids/{grid_id}/rows", rows),
        Route("/grids/{grid_id}/count", count),
        Route("/grids/{grid_id}/cell", cell),
        Route("/grids/{grid_id}/step-counts", step_counts),
        Route("/grids/{grid_id}/profile", profile),
        Route("/grids/{grid_id}/header-stats", stats),
        Route("/grids/{grid_id}/categorize-stats", categorize_stats),
        Route("/grids/{grid_id}/steps", put_steps, methods=["POST"]),
        Route("/grids/{grid_id}/step-ops", step_op, methods=["POST"]),
        Route("/grids/{grid_id}/header-sort", header_sort, methods=["POST"]),
        Route("/grids/{grid_id}/sql", run_sql, methods=["POST"]),
        Route("/grids/{grid_id}/cancel", cancel, methods=["POST"]),
        Route("/grids/{grid_id}/selection-stats", selection_stats, methods=["POST"]),
        Route("/grids/{grid_id}/export", export),
        Route("/grids/{grid_id}/code", code),
        Route("/grids/{grid_id}/recipe", recipe, methods=["POST", "DELETE"]),
        Route("/grids/{grid_id}/highlights", set_highlights, methods=["POST"]),
        Route("/grids/{grid_id}/selection", set_selection, methods=["POST"]),
    ]
