"""The page's file chooser (services/folders.py), the user's alone:

GET    /folders?path=&suffixes=.csv,.xlsx   a folder's subfolders and files (names, sizes, dates)
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.server.http import actor, error_response, json_response
from swissarmygrid.services.folders import Folders


def routes(folders: Folders) -> list[BaseRoute]:
    async def browse(request: Request) -> Response:
        if actor(request) == "agent":
            return Response("The file chooser is the user's: an agent cannot list folders.", status_code=403)
        try:
            return json_response(
                folders.browse(request.query_params.get("path"), request.query_params.get("suffixes"))
            )
        except FileNotFoundError as error:
            return Response(str(error), status_code=404)
        except PermissionError as error:
            return Response(str(error), status_code=403)
        except Exception as error:
            return error_response(error)

    return [Route("/folders", browse)]
