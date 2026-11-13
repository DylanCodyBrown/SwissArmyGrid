"""The Analyze menu's analyses on one grid's view (services/analysis.py; the math in domain/analysis.py).

  POST /grids/{id}/fit-test   {"columns": [...], "expected": even | benford | benford2 | grid |
                              shares | together, "reference": {"grid", "column"}, "shares": {...}}
                              -> per column: chi-square, degrees of freedom, p, effect size, the verdict
                              in words, each value's observed and expected count
  POST /grids/{id}/outliers   {"column", "look": amount | frequency, "method": robust | iqr | zscore,
                              "threshold", "by"} -> how many, the fences, the most extreme, and the
                              steps that show or exclude their rows (not added: the page or the agent
                              adds one)
  POST /grids/{id}/odds       {"column", "buckets", "outcome": {"column", "values"} or {"column", "op",
                              "value"}} -> each value's share, 1 in N and odds; with an outcome, each
                              value's rate against the rest and its odds ratio with a 95% range
  POST /grids/{id}/predictors {"target", "candidates", "buckets"} -> each candidate tested together with
                              the target, ranked by Cramér's V; correlations; pairs that say the same

Both read; the agent may run them (cell values come back, so its tools mark them as data).
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.server.http import actor, error_response, json_response, read_json
from swissarmygrid.services.analysis import Analysis
from swissarmygrid.services.grids import GridService


def routes(grids: GridService, analysis: Analysis) -> list[BaseRoute]:
    async def fit_test(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            body = await read_json(request)
            if not isinstance(body, dict):
                raise ValueError('Send {"columns": [...], "expected": "even"}.')
            return json_response(await analysis.fit(grid, info, body, actor(request)))
        except Exception as error:
            return error_response(error)

    async def outliers(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            body = await read_json(request)
            if not isinstance(body, dict):
                raise ValueError('Send {"column": name, "look": "amount"}.')
            return json_response(await analysis.outliers(grid, info, body, actor(request)))
        except Exception as error:
            return error_response(error)

    async def odds(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            body = await read_json(request)
            if not isinstance(body, dict):
                raise ValueError('Send {"column": name}, with "outcome" for odds ratios.')
            return json_response(await analysis.odds(grid, info, body, actor(request)))
        except Exception as error:
            return error_response(error)

    async def predictors(request: Request) -> Response:
        try:
            grid, info = grids.find(request.path_params["grid_id"])
            body = await read_json(request)
            if not isinstance(body, dict):
                raise ValueError('Send {"target": name}.')
            return json_response(await analysis.predictors(grid, info, body, actor(request)))
        except Exception as error:
            return error_response(error)

    return [
        Route("/grids/{grid_id}/fit-test", fit_test, methods=["POST"]),
        Route("/grids/{grid_id}/outliers", outliers, methods=["POST"]),
        Route("/grids/{grid_id}/odds", odds, methods=["POST"]),
        Route("/grids/{grid_id}/predictors", predictors, methods=["POST"]),
    ]
