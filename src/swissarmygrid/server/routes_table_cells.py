"""Every table's read-only cell (services/table_cells.py), for the Workbench window and the agent.

GET    /workbench/tables              every open table as its cell's text (its transformations as one
                                      DuckDB query, its load when no code made it), its grid and steps
POST   /workbench/tables/arrange      {"kind": "notebook", "cells", "tables", "next_cell", "move"} or
                                      {"kind": "script", "text", "tables", "move"}: the document with
                                      its Tables group as it should be, and each cell's uses line
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.server.http import error_response, json_response, read_json
from swissarmygrid.services.table_cells import TableCells


def routes(tables: TableCells) -> list[BaseRoute]:
    async def listing(request: Request) -> Response:
        try:
            return json_response(await tables.listing())
        except Exception as error:
            return error_response(error)

    async def arrange(request: Request) -> Response:
        try:
            return json_response(await tables.arrange(await read_json(request)))
        except Exception as error:
            return error_response(error)

    return [
        Route("/workbench/tables", listing),
        Route("/workbench/tables/arrange", arrange, methods=["POST"]),
    ]
