"""Time series and forecast on one grid's view (services/timeseries.py; the math in domain/timeseries.py).

  POST /grids/{id}/time-series  {"date", "metric", "total": sum | avg | count, "period", "split"} -> the
                                series per period, its trend, seasonal index in words, anomalies (with
                                the step that shows their rows), level shifts, memory and growth
  POST /grids/{id}/forecast     {"date", "metric", "total", "period", "horizon", "predictors": [{"column",
                                "future": last | trend | value, "value"}]} -> history and fitted line,
                                the periods ahead with 80% and 95% ranges, the held-back check

Both read; the agent may run them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.server.http import actor, error_response, json_response, read_json
from swissarmygrid.services.grids import GridService
from swissarmygrid.services.timeseries import TimeSeries


def routes(grids: GridService, series: TimeSeries) -> list[BaseRoute]:
    def one(name: str) -> Callable[[Request], Awaitable[Response]]:
        async def handle(request: Request) -> Response:
            try:
                grid, info = grids.find(request.path_params["grid_id"])
                body = await read_json(request)
                if not isinstance(body, dict):
                    raise ValueError('Send {"date": name, "metric": name, "period": "month"}.')
                return json_response(await series.run(name, grid, info, body, actor(request)))
            except Exception as error:
                return error_response(error)

        return handle

    return [
        Route("/grids/{grid_id}/time-series", one("time_series"), methods=["POST"]),
        Route("/grids/{grid_id}/forecast", one("forecast"), methods=["POST"]),
    ]
