"""Code handed to your own Python (services/handoff.py).

  POST /python/send               {"code", "imports", "label"}: queue code for sag.paste() (the page)
  POST /grids/{id}/python         the grid's steps as checked DuckDB code, queued
  POST /python/take               the newest code not yet pasted (sag.paste() calls this); 204 if none
  GET  /python/sent               what was sent lately, newest first

The agent never sends code to the user's Python: those routes refuse it.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.server.http import actor, error_response, json_response, read_json
from swissarmygrid.services.handoff import Handoff
from swissarmygrid.services.workspace import Refused


def _users_only(request: Request) -> None:
    if actor(request) == "agent":
        raise Refused("Code goes into the user's own Python only from the user's own click.")


def routes(handoff: Handoff) -> list[BaseRoute]:
    ws = handoff.ws

    async def send(request: Request) -> Response:
        try:
            _users_only(request)
            return json_response(handoff.send(await read_json(request), actor(request)))
        except Exception as error:
            return error_response(error)

    async def grid_code(request: Request) -> Response:
        try:
            _users_only(request)
            grid, info = ws.grid_and_info(request.path_params["grid_id"])
            return json_response(await handoff.grid_code(grid, info, actor(request)))
        except Exception as error:
            return error_response(error)

    async def take(request: Request) -> Response:
        try:
            _users_only(request)
            taken = handoff.take()
            return Response(status_code=204) if taken is None else json_response(taken)
        except Exception as error:
            return error_response(error)

    async def sent(request: Request) -> Response:
        try:
            _users_only(request)
            return json_response(handoff.recent())
        except Exception as error:
            return error_response(error)

    return [
        Route("/python/send", send, methods=["POST"]),
        Route("/grids/{grid_id}/python", grid_code, methods=["POST"]),
        Route("/python/take", take, methods=["POST"]),
        Route("/python/sent", sent),
    ]
