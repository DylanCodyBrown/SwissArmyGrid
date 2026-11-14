"""Connections (Plans/workbench-and-connections.plan.md): the databases and files the user reaches, kept
in the blade store and seen apart from blades. The agent may do all of it too (the golden rule); no
answer holds a password's value:

  GET    /connections                 every connection: databases, then files
  GET    /connections/types           the starters, each as the Add connection form
  GET    /connections/{name}          a saved one as the form shows it to change
  POST   /connections                 save a new one        PUT /connections/{name}   change or rename it
  POST   /connections/test            run the form's code (not saved) with the passwords typed into it
  GET    /connections/vaults          the vault blades a password can come from, with their entries
  POST   /connections/source          point a password at the keyring, an environment variable or a vault

Removing, secrets, Refresh, favourites and export are the blade routes' (routes_blades.py).
The use cases are services/connections.py.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.domain.blades import VaultUnavailableError
from swissarmygrid.server.blade_runner import RefreshError
from swissarmygrid.server.http import error_response, json_response, read_json
from swissarmygrid.services.blades import BladeService, NeedsValues
from swissarmygrid.services.connections import ConnectionService


def routes(connections: ConnectionService, blades: BladeService) -> list[BaseRoute]:
    async def listing(request: Request) -> Response:
        try:
            return json_response(connections.listing())
        except Exception as error:
            return error_response(error)

    async def types(request: Request) -> Response:
        try:
            return json_response(connections.types())
        except Exception as error:
            return error_response(error)

    async def details(request: Request) -> Response:
        try:
            return json_response(connections.details(request.path_params["name"]))
        except Exception as error:
            return error_response(error)

    async def save(request: Request) -> Response:
        try:
            previous = request.path_params.get("name")
            return json_response(await connections.save(await read_json(request), previous))
        except VaultUnavailableError as error:
            return Response(str(error), status_code=409)
        except Exception as error:
            return error_response(error)

    async def vaults(request: Request) -> Response:
        try:
            return json_response(connections.vaults())
        except Exception as error:
            return error_response(error)

    async def source(request: Request) -> Response:
        try:
            return json_response(await connections.source(await read_json(request)))
        except Exception as error:
            return error_response(error)

    async def test(request: Request) -> Response:
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
        Route("/connections/vaults", vaults),
        Route("/connections/source", source, methods=["POST"]),
        Route("/connections/{name}", details),
        Route("/connections/{name}", save, methods=["PUT"]),
    ]
