"""Regression on one grid's view (services/regression.py; the math in domain/regression.py).

  POST /grids/{id}/regression   {"target", "predictors": [...], "outcome": {...} for a yes/no target}
                                -> each coefficient (with its range, p and a sentence), the fit (R-squared
                                and the typical miss, or accuracy and McFadden's R-squared), the verdict,
                                the model, and the steps that add its predictions or misses (not added)
  POST /regression/predict      {"model", "values": {column: value}} -> the what-if estimate and range

Both read; the agent may run them.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.server.http import actor, error_response, json_response, read_json
from swissarmygrid.services.grids import GridService
from swissarmygrid.services.regression import Regression, run_predict


def routes(grids: GridService, regression: Regression) -> list[BaseRoute]:
    async def fit(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            body = await read_json(request)
            if not isinstance(body, dict):
                raise ValueError('Send {"target": name, "predictors": [...]}.')
            return json_response(await regression.fit(grid, info, body, actor(request)))
        except Exception as error:
            return error_response(error)

    async def predict(request: Request) -> Response:
        try:
            body = await read_json(request)
            if not isinstance(body, dict):
                raise ValueError('Send {"model": ..., "values": {...}}.')
            return json_response(run_predict(body))
        except Exception as error:
            return error_response(error)

    return [
        Route("/grids/{grid_id}/regression", fit, methods=["POST"]),
        Route("/regression/predict", predict, methods=["POST"]),
    ]
