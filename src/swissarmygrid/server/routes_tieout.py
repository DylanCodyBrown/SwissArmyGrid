"""The tie-out report on a compare (services/tieout.py).

GET  /compares/{id}/tie-out       the columns both grids have, which can be grouped or totalled, and
                                  a suggestion (a text column with few values; every number column)
POST /compares/{id}/tie-out       {"group": [columns], "measures": [number columns], "tolerance": 0,
                                  "tolerance_kind": "absolute" | "percent"}: the report
POST /compares/{id}/tie-out/show  {"group": [columns], "keys": a group's keys}: filter both grids to
                                  that group (a SQL step on each)
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.server.http import actor, error_response, json_response, read_json
from swissarmygrid.services.tieout import TieOut


def routes(tie_out: TieOut) -> list[BaseRoute]:
    async def options(request: Request) -> Response:
        try:
            cmp = tie_out.compares.get(request.path_params["compare_id"])
            return json_response(await tie_out.options(cmp))
        except Exception as error:
            return error_response(error)

    async def run(request: Request) -> Response:
        try:
            cmp = tie_out.compares.get(request.path_params["compare_id"])
            return json_response(await tie_out.run(cmp, await read_json(request), actor(request)))
        except Exception as error:
            return error_response(error)

    async def show(request: Request) -> Response:
        try:
            cmp = tie_out.compares.get(request.path_params["compare_id"])
            return json_response(await tie_out.show(cmp, await read_json(request), actor(request)))
        except Exception as error:
            return error_response(error)

    return [
        Route("/compares/{compare_id}/tie-out/show", show, methods=["POST"]),
        Route("/compares/{compare_id}/tie-out", options),
        Route("/compares/{compare_id}/tie-out", run, methods=["POST"]),
    ]
