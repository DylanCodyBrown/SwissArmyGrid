"""Connections (Plans/workbench-and-connections.plan.md): the databases and files the user reaches, kept
in the blade store and seen apart from blades. The user's alone, as blades are (the agent is refused:
their code names servers and accounts, and runs):

  GET    /connections                 every connection: databases, then files
  GET    /connections/types           the starters, each as the Add connection form
  GET    /connections/{name}          a saved one as the form shows it to change
  POST   /connections                 save a new one        PUT /connections/{name}   change or rename it
  POST   /connections/test            run the form's code (not saved) with the passwords typed into it

Removing, secrets, Refresh, favourites and export are the blade routes' (routes_blades.py).
The use cases are services/connections.py.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.domain.blades import VaultUnavailableError
from swissarmygrid.server.blade_runner import RefreshError
from swissarmygrid.server.http import actor, error_response, json_response, read_json
from swissarmygrid.services.blades import BladeService, NeedsValues
from swissarmygrid.services.connections import ConnectionService


def _agent_refused() -> Response:
    return Response(
        "Connections are the user's own saved code and passwords: an agent cannot read, change or run "
        "them. Use the catalog_* tools to see a source's tables and columns.",
        status_code=403,
    )


def routes(connections: ConnectionService, blades: BladeService) -> list[BaseRoute]:
    async def listing(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            return json_response(connections.listing())
        except Exception as error:
            return error_response(error)

    async def types(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            return json_response(connections.types())
        except Exception as error:
            return error_response(error)

    async def details(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            return json_response(connections.details(request.path_params["name"]))
        except Exception as error:
            return error_response(error)

    async def save(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            previous = request.path_params.get("name")
            return json_response(await connections.save(await read_json(request), previous))
        except VaultUnavailableError as error:
            return Response(str(error), status_code=409)
        except Exception as error:
            return error_response(error)

    async def test(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            return json_response(await connections.test(await read_json(request)))
        except NeedsValues as error:
            return json_response({"needs": error.missing, "message": str(error)}, status_code=409)
        except RefreshError as error:
            return json_response(blades.run_failure(str(error), error.missing_module), status_code=422)
        except Exception as error:
            return error_response(error)

    return [
        Route("/connections", listing),
        Route("/connections", save, methods=["POST"]),
        Route("/connections/types", types),
        Route("/connections/test", test, methods=["POST"]),
        Route("/connections/{name}", details),
        Route("/connections/{name}", save, methods=["PUT"]),
    ]
