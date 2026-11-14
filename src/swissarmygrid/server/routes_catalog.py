"""Catalogs (plan decision 17), readable by the agent (names and types, never values):

  GET    /catalogs                                  every loaded catalog
  PUT    /catalogs/{name}?dialect=                  load one (sag.load): Arrow or split JSON, like a push
  DELETE /catalogs/{name}
  GET    /catalogs/{name}/schemas
  GET    /catalogs/{name}/tables?schema=&database=&offset=&limit=
  GET    /catalogs/{name}/columns?schema=&table=&database=   with a first SELECT in the source's dialect
  GET    /catalogs/{name}/search?q=&limit=
  GET    /catalogs/{name}/script?schema=&table=&database=&as=select_star|select_columns|pandas&blade=
                                                    the right-click's Script as: SQL or Python for a table

The use cases are services/catalogs.py. Only the user loads or removes a catalog. (Blades, the saved
code that loads one, are routes_blades.py.)
"""

from __future__ import annotations

import json
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.server.http import actor, error_response, json_response, query_int, read_body
from swissarmygrid.services.catalogs import CatalogService

_ARROW_STREAM: str = "application/vnd.apache.arrow.stream"


def routes(catalogs: CatalogService) -> list[BaseRoute]:
    ws = catalogs.ws

    def catalog_url(name: str) -> str:
        where = ws.addresses
        return f"http://127.0.0.1:{where.port}/catalog#token={where.token}&catalog={quote(name)}"

    async def list_catalogs(request: Request) -> Response:
        return json_response(catalogs.listing())

    async def load_catalog(request: Request) -> Response:
        try:
            name = request.path_params["name"]
            body = await read_body(request, ws.settings.max_push_body_bytes)
            arrow = request.headers.get("content-type", "").startswith(_ARROW_STREAM)
            payload = None
            if not arrow:
                try:
                    payload = json.loads(body)
                except ValueError:
                    return Response("The request body is not valid JSON.", status_code=400)
            dialect = request.query_params.get("dialect") or None
            info = await catalogs.load(name, body if arrow else None, payload, dialect, actor(request))
            return json_response({"catalog": info.to_json(), "url": catalog_url(info.name)})
        except Exception as error:
            return error_response(error)

    async def remove_catalog(request: Request) -> Response:
        try:
            return json_response(await catalogs.remove(request.path_params["name"], actor(request)))
        except Exception as error:
            return error_response(error)

    def _database(request: Request) -> str | None:
        return request.query_params.get("database") if "database" in request.query_params else None

    async def schemas(request: Request) -> Response:
        try:
            return json_response(await catalogs.schemas(request.path_params["name"]))
        except Exception as error:
            return error_response(error)

    async def tables(request: Request) -> Response:
        try:
            name = request.path_params["name"]
            schema = request.query_params.get("schema", "")
            offset = query_int(request, "offset", 0) or 0
            limit = query_int(request, "limit", 1000) or 1000
            return json_response(await catalogs.tables(name, schema, _database(request), offset, limit))
        except Exception as error:
            return error_response(error)

    async def columns(request: Request) -> Response:
        try:
            name = request.path_params["name"]
            schema = request.query_params.get("schema", "")
            table = request.query_params.get("table")
            return json_response(await catalogs.columns(name, schema, table, _database(request)))
        except Exception as error:
            return error_response(error)

    async def script(request: Request) -> Response:
        try:
            q = request.query_params
            kind, blade = q.get("as", "select_star"), q.get("blade")
            found = await catalogs.script(
                request.path_params["name"],
                q.get("schema", ""),
                q.get("table"),
                _database(request),
                kind,
                blade,
            )
            return json_response(found)
        except Exception as error:
            return error_response(error)

    async def search(request: Request) -> Response:
        try:
            name = request.path_params["name"]
            text = request.query_params.get("q", "")[:200]
            limit = query_int(request, "limit", 50) or 50
            return json_response(await catalogs.search(name, text, limit))
        except Exception as error:
            return error_response(error)

    return [
        Route("/catalogs", list_catalogs),
        Route("/catalogs/{name}", load_catalog, methods=["PUT"]),
        Route("/catalogs/{name}", remove_catalog, methods=["DELETE"]),
        Route("/catalogs/{name}/schemas", schemas),
        Route("/catalogs/{name}/tables", tables),
        Route("/catalogs/{name}/columns", columns),
        Route("/catalogs/{name}/search", search),
        Route("/catalogs/{name}/script", script),
    ]
