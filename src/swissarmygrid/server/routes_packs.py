"""Packs: blades and tangs from a Claude Code plugin marketplace (services/packs.py).

  GET  /packs        the installed packs, the editable ones (clones in the blade folders), the commands
  POST /packs/new    {"folder", "marketplace", "owner", "pack", "description", "add_folder"}: a new
                     marketplace repository with one pack
  POST /packs/add    {"pack": <folder>, "blades": [names], "tangs": [names], "replace"}: copy them in

Files on disk, and code that others will run; the agent may do all of it too (the golden rule).
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.server.http import actor, error_response, json_response, read_json
from swissarmygrid.services.packs import Packs

_USERS_ONLY = "Packs are the user's: they write files that other people's computers run."


def routes(packs: Packs) -> list[BaseRoute]:
    async def listing(request: Request) -> Response:
        try:
            return json_response(packs.list())
        except Exception as error:
            return error_response(error)

    async def new(request: Request) -> Response:
        try:
            return json_response(packs.create(await read_json(request), actor(request)))
        except Exception as error:
            return error_response(error)

    async def add(request: Request) -> Response:
        try:
            return json_response(packs.add(await read_json(request), actor(request)))
        except Exception as error:
            return error_response(error)

    return [
        Route("/packs", listing),
        Route("/packs/new", new, methods=["POST"]),
        Route("/packs/add", add, methods=["POST"]),
    ]
