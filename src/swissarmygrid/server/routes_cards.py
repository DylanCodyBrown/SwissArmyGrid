"""Side-panel cards: charts, notes and small result tables (plan section 11).

  GET    /cards                            every card of every open grid: the one Analysis Sandbox
  POST   /cards                            a card of no table: note, diagram, graphviz, plotly, echarts,
                                            image, or a table of its own {"columns", "rows"}; "key"
                                            replaces the card sent with that key before
  GET    /grids/{id}/cards                 every card for the grid, charts computed on its view
  POST   /grids/{id}/cards                 {"kind": "chart"|"note"|"table"|"diagram", "title",
                                            "spec"|"body"|"sql"}
  PATCH  /cards/{card}                     change title, spec, body, sql or pinned
  DELETE /cards/{card}                     remove a card
  POST   /cards/{card}/select              {"value": ...} cross-filter: add a filter step for it
  GET    /cards/{card}/code                Altair code for a chart card

A card records its source (dataset, version, steps) and redraws whenever its grid's view
changes, because charts are computed on request from the grid's current steps. Agent-made
cards are tagged so the page can mark them.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.domain.charts import ChartError
from swissarmygrid.server.http import actor, error_response, json_response, read_json
from swissarmygrid.services.charts import Cards


def routes(cards: Cards) -> list[BaseRoute]:
    ws = cards.ws

    async def list_cards(request: Request) -> Response:
        try:
            grid, _ = ws.grid_and_info(request.path_params["grid_id"])
            return json_response(await cards.for_grid(grid))
        except Exception as error:
            return error_response(error)

    async def all_cards(request: Request) -> Response:
        """The Analysis Sandbox: one list for every table."""
        try:
            return json_response(await cards.everything())
        except Exception as error:
            return error_response(error)

    async def add_card(request: Request) -> Response:
        try:
            grid, info = ws.grid_and_info(request.path_params["grid_id"])
            return json_response(await cards.add(grid, info, await read_json(request), actor(request)))
        except ChartError as error:
            return Response(str(error), status_code=400)
        except Exception as error:
            return error_response(error)

    async def add_free(request: Request) -> Response:
        try:
            return json_response(await cards.add_free(await read_json(request), actor(request)))
        except Exception as error:
            return error_response(error)

    async def update_card(request: Request) -> Response:
        try:
            card = cards.get(request.path_params["card_id"])
            return json_response(await cards.update(card, await read_json(request), actor(request)))
        except ChartError as error:
            return Response(str(error), status_code=400)
        except Exception as error:
            return error_response(error)

    async def delete_card(request: Request) -> Response:
        removed = await cards.remove(request.path_params["card_id"], actor(request))
        if removed is None:
            return Response("No such card.", status_code=404)
        return json_response(removed)

    async def select(request: Request) -> Response:
        """Cross-filtering: a click on a chart adds a filter step for what was clicked."""
        try:
            card, grid, info = cards.chart(request.path_params["card_id"])
            return json_response(
                await cards.select(card, grid, info, await read_json(request), actor(request))
            )
        except Exception as error:
            return error_response(error)

    async def code(request: Request) -> Response:
        try:
            card, grid, info = cards.chart(request.path_params["card_id"])
            return json_response(await cards.code(card, grid, info))
        except Exception as error:
            return error_response(error)

    return [
        Route("/cards", all_cards),
        Route("/cards", add_free, methods=["POST"]),
        Route("/grids/{grid_id}/cards", list_cards),
        Route("/grids/{grid_id}/cards", add_card, methods=["POST"]),
        Route("/cards/{card_id}", update_card, methods=["PATCH"]),
        Route("/cards/{card_id}", delete_card, methods=["DELETE"]),
        Route("/cards/{card_id}/select", select, methods=["POST"]),
        Route("/cards/{card_id}/code", code),
    ]
