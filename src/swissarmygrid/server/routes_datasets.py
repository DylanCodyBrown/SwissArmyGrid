"""Dataset routes: push, list, close, load a file, paste text; plus health and shutdown.

  GET    /health                        liveness (version and process id)
  POST   /shutdown                      `swissarmygrid stop`
  GET    /datasets                      every dataset with its size, and the memory budget
  PUT    /datasets/{name}?new_grid=0|1  push: Arrow stream (preferred) or orient="split" JSON
  DELETE /datasets/{name}               close a dataset and its grids, freeing its memory
  POST   /datasets/{name}/load          {"path": "C:\\...\\file.csv", "sheet": "..."} read by trusted Python
  POST   /files/sheets                  {"path": "C:\\...\\book.xlsx"} the workbook's sheet names
  POST   /datasets/{name}/drop?filename=&sheet=   a file dropped on the page: its bytes, read in memory
  POST   /files/dropped-sheets?filename=          a dropped workbook's sheet names (its bytes)
  POST   /datasets/{name}/paste         tab-separated text copied from Excel
  POST   /datasets/{name}/query         {"sql": "SELECT ...", "replace": bool} the "+" tab's table

A push reuses the dataset's first grid (that grid hot-reloads in place) unless new_grid=1.
The first grid on a dataset picks up a saved recipe for that name, if there is one.
"""

from __future__ import annotations

import json
import os
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from swissarmygrid import __version__
from swissarmygrid.domain.datasets import mib, validate_name
from swissarmygrid.server.http import actor, error_response, json_response, query_int, read_body, read_json
from swissarmygrid.services.datasets import DatasetService

ARROW_STREAM: str = "application/vnd.apache.arrow.stream"


def routes(datasets: DatasetService) -> list[BaseRoute]:
    settings = datasets.ws.settings

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok", "version": __version__, "pid": os.getpid()})

    async def shutdown(request: Request) -> Response:
        datasets.stop(actor(request))
        return JSONResponse({"status": "stopping"})

    async def audit(request: Request) -> Response:
        """The audit log's last entries, for the page's activity list (never cell values)."""
        try:
            limit = max(1, min(500, query_int(request, "limit", 100) or 100))
        except ValueError as error:
            return error_response(error)
        # ?actor=agent: the agent's own last entries, however many the user wrote since.
        return json_response({"entries": datasets.activity(limit, request.query_params.get("actor") or None)})

    async def list_datasets(request: Request) -> Response:
        return json_response(datasets.listing())

    async def put_dataset(request: Request) -> Response:
        try:
            name = validate_name(request.path_params["name"])
            follow = request.query_params.get("follow", "0") == "1"
            process_id = request.headers.get("x-process", "")
            datasets.admit_push(name, follow, process_id)
            content_type = request.headers.get("content-type", "").split(";")[0].strip()
            if content_type not in ("application/json", ARROW_STREAM):
                return Response(
                    f"Send {ARROW_STREAM} or application/json (pandas orient='split').",
                    status_code=415,
                )
            # Enforce the body cap early: refuse on Content-Length, then count while reading.
            cap = settings.max_push_body_bytes
            too_big = f"The request body is over the {mib(cap)} push limit."
            declared = request.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > cap:
                return Response(too_big, status_code=413)
            chunks: list[bytes] = []
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > cap:
                    return Response(too_big, status_code=413)
                chunks.append(chunk)
            body = b"".join(chunks)
            payload: Any = None
            if content_type != ARROW_STREAM:
                try:
                    payload = json.loads(body)
                except ValueError:
                    return Response("The request body is not valid JSON.", status_code=400)
            return json_response(
                await datasets.push(
                    name,
                    body if content_type == ARROW_STREAM else None,
                    payload,
                    request.headers.get("x-source-kind", "unknown"),
                    request.query_params.get("new_grid", "0") == "1",
                    actor(request),
                    follow,
                    process_id,
                )
            )
        except Exception as error:  # every failure becomes a plain-language status
            return error_response(error)

    async def delete_dataset(request: Request) -> Response:
        closed = await datasets.close(request.path_params["name"], actor(request))
        if closed is None:
            return Response("No such dataset.", status_code=404)
        return json_response(closed)

    async def load(request: Request) -> Response:
        try:
            name = validate_name(request.path_params["name"])
            body = await read_json(request)
            path = body.get("path") if isinstance(body, dict) else None
            if not isinstance(path, str):
                return Response('Send {"path": "<full path to the file>"}.', status_code=400)
            sheet = body.get("sheet") if isinstance(body.get("sheet"), str) else None
            return json_response(
                await datasets.load_file(name, path, sheet, bool(body.get("new_grid")), actor(request))
            )
        except Exception as error:
            return error_response(error)

    async def sheets(request: Request) -> Response:
        """The sheet names of a workbook, so VS Code can ask which to load."""
        try:
            body = await read_json(request)
            path = body.get("path") if isinstance(body, dict) else None
            if not isinstance(path, str):
                return Response('Send {"path": "<full path to the .xlsx file>"}.', status_code=400)
            return json_response(await datasets.sheets(path))
        except Exception as error:
            return error_response(error)

    async def drop(request: Request) -> Response:
        try:
            name = validate_name(request.path_params["name"])
            filename = request.query_params.get("filename") or ""
            sheet = request.query_params.get("sheet") or None
            data = await read_body(request, settings.max_push_body_bytes)
            return json_response(await datasets.load_dropped(name, data, filename, sheet, actor(request)))
        except Exception as error:
            return error_response(error)

    async def dropped_sheets(request: Request) -> Response:
        try:
            data = await read_body(request, settings.max_push_body_bytes)
            return json_response(
                await datasets.dropped_sheets(data, request.query_params.get("filename") or "")
            )
        except Exception as error:
            return error_response(error)

    async def paste(request: Request) -> Response:
        try:
            name = validate_name(request.path_params["name"])
            text = (await read_body(request, settings.max_push_body_bytes)).decode("utf-8", "replace")
            return json_response(await datasets.paste(name, text, actor(request)))
        except Exception as error:
            return error_response(error)

    async def build_from_query(request: Request) -> Response:
        """The "+" tab: dataset `name` from one SELECT over the open tables, each read as its tab
        shows it. "replace" is the tab's Run again."""
        try:
            name = validate_name(request.path_params["name"])
            body = await read_json(request)
            text = body.get("sql") if isinstance(body, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise ValueError('Send {"sql": "SELECT ... FROM <table>"}.')
            return json_response(
                await datasets.build_from_query(name, text, body.get("replace") is True, actor(request))
            )
        except Exception as error:
            return error_response(error)

    return [
        Route("/health", health),
        Route("/shutdown", shutdown, methods=["POST"]),
        Route("/datasets", list_datasets),
        Route("/audit", audit),
        Route("/datasets/{name}", put_dataset, methods=["PUT"]),
        Route("/datasets/{name}", delete_dataset, methods=["DELETE"]),
        Route("/datasets/{name}/load", load, methods=["POST"]),
        Route("/datasets/{name}/paste", paste, methods=["POST"]),
        Route("/datasets/{name}/query", build_from_query, methods=["POST"]),
        Route("/files/sheets", sheets, methods=["POST"]),
        Route("/datasets/{name}/drop", drop, methods=["POST"]),
        Route("/files/dropped-sheets", dropped_sheets, methods=["POST"]),
    ]
