"""Relationships: the current model and its library, the Key Finder, and comparing two grids
(services/relationships.py has the use cases).

  GET  /relationships                    the current model (keys + relationships), as model JSON
  POST /relationships                    {"model": {...}, "replace": false}: merge (or replace) a model
  GET  /relationships/library            the models found in the library folders
  POST /relationships/library/{name}     apply that library model (merge)
  GET  /relationships/erd?tables=a,b     the current model as Mermaid erDiagram text

  GET  /grids/{id}/key-finder            the grids a Key Finder can compare against
  POST /grids/{id}/key-finder            {"target": grid id | "*", "options": {...}}
       -> pairs (column, target grid, column, shared values, % found each way, score), best first,
          and unique_key: the column combination that tells this grid's first rows apart
  POST /grids/{id}/key-finder/sample     {"target", "source_column", "target_column", "options"}
       -> a few shared values and a few this column has that the target lacks (page only)

  POST   /compares                 {"left", "right", "keys": [[l, r], ...], "mode", "anti_side"}
  GET    /compares/{id}            the compare, with its counts (matched, left only, right only)
  PATCH  /compares/{id}            change keys, mode or anti_side
  DELETE /compares/{id}
  GET    /compares/{id}/rows?side=left|right&start=&end=
  GET    /compares/{id}/key-group?row=N
  POST   /compares/{id}/focus      {"side": picked side, "position": its view row}   (cross compare)
  DELETE /compares/{id}/focus

Key Finder pairs carry counts, never values. The value samples are for the page (the agent reads
values through its own query tool).
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.server.http import actor, error_response, json_response, query_int, read_json
from swissarmygrid.services.relationships import Compares, KeyFinder, Models


def routes(models: Models, keys: KeyFinder, compares: Compares) -> list[BaseRoute]:
    ws = keys.ws

    # ===== the model =====

    async def get_current(request: Request) -> Response:
        return json_response(models.current())

    async def set_current(request: Request) -> Response:
        try:
            return json_response(await models.set_current(await read_json(request), actor(request)))
        except Exception as error:
            return error_response(error)

    async def library(request: Request) -> Response:
        return json_response(models.library())

    async def apply_library(request: Request) -> Response:
        try:
            return json_response(await models.apply_library(request.path_params["name"], actor(request)))
        except Exception as error:
            return error_response(error)

    async def erd(request: Request) -> Response:
        try:
            wanted = {t for t in request.query_params.get("tables", "").split(",") if t}
            return json_response(models.erd(wanted))
        except Exception as error:
            return error_response(error)

    # ===== the Key Finder =====

    async def candidates(request: Request) -> Response:
        """The grids a Key Finder can compare against (for the page's picker)."""
        try:
            source, _ = ws.grid_and_info(request.path_params["grid_id"])
            return json_response(keys.candidates(source))
        except Exception as error:
            return error_response(error)

    async def find(request: Request) -> Response:
        try:
            source, _ = ws.grid_and_info(request.path_params["grid_id"])
            return json_response(await keys.run(source, await read_json(request), actor(request)))
        except Exception as error:
            return error_response(error)

    async def sample(request: Request) -> Response:
        try:
            if actor(request) == "agent":
                return Response("Value samples are for the grid page.", status_code=403)
            source, _ = ws.grid_and_info(request.path_params["grid_id"])
            return json_response(await keys.sample(source, await read_json(request)))
        except Exception as error:
            return error_response(error)

    # ===== compare =====

    async def create(request: Request) -> Response:
        try:
            return json_response(await compares.create(await read_json(request), actor(request)))
        except Exception as error:
            return error_response(error)

    async def read(request: Request) -> Response:
        try:
            return json_response(await compares.read(compares.get(request.path_params["compare_id"])))
        except Exception as error:
            return error_response(error)

    async def change(request: Request) -> Response:
        try:
            cmp = compares.get(request.path_params["compare_id"])
            return json_response(await compares.change(cmp, await read_json(request)))
        except Exception as error:
            return error_response(error)

    async def remove(request: Request) -> Response:
        try:
            return json_response(await compares.remove(request.path_params["compare_id"]))
        except Exception as error:
            return error_response(error)

    async def rows(request: Request) -> Response:
        try:
            cmp = compares.get(request.path_params["compare_id"])
            which = request.query_params.get("side")
            if which not in ("left", "right"):
                raise ValueError("side must be left or right.")
            start = max(0, query_int(request, "start", 0) or 0)
            end = max(
                start, min(query_int(request, "end", start + 100) or 0, start + ws.settings.max_window_rows)
            )
            return json_response(
                await compares.rows(cmp, which, start, end), max_bytes=ws.settings.max_response_bytes
            )
        except Exception as error:
            return error_response(error)

    async def key_group(request: Request) -> Response:
        try:
            cmp = compares.get(request.path_params["compare_id"])
            return json_response(await compares.key_group(cmp, query_int(request, "row")))
        except Exception as error:
            return error_response(error)

    async def set_focus(request: Request) -> Response:
        try:
            cmp = compares.get(request.path_params["compare_id"])
            return json_response(await compares.set_focus(cmp, await read_json(request)))
        except Exception as error:
            return error_response(error)

    async def clear_focus(request: Request) -> Response:
        try:
            return json_response(compares.clear_focus(compares.get(request.path_params["compare_id"])))
        except Exception as error:
            return error_response(error)

    return [
        Route("/relationships", get_current),
        Route("/relationships", set_current, methods=["POST"]),
        Route("/relationships/library", library),
        Route("/relationships/library/{name}", apply_library, methods=["POST"]),
        Route("/relationships/erd", erd),
        Route("/grids/{grid_id}/key-finder", candidates),
        Route("/grids/{grid_id}/key-finder", find, methods=["POST"]),
        Route("/grids/{grid_id}/key-finder/sample", sample, methods=["POST"]),
        Route("/compares", create, methods=["POST"]),
        Route("/compares/{compare_id}", read),
        Route("/compares/{compare_id}", change, methods=["PATCH"]),
        Route("/compares/{compare_id}", remove, methods=["DELETE"]),
        Route("/compares/{compare_id}/rows", rows),
        Route("/compares/{compare_id}/key-group", key_group),
        Route("/compares/{compare_id}/focus", set_focus, methods=["POST"]),
        Route("/compares/{compare_id}/focus", clear_focus, methods=["DELETE"]),
    ]
