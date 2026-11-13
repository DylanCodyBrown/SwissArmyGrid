"""The Workbench (services/workbench.py): notebooks and scripts in their own window, and the kernels that
run them. The user's alone: every route refuses the agent (it never runs code in the user's Python).

  GET    /workbench                         the page (its own window, opened from the grid page's tab)
  GET    /workbench/home                    where Save as starts, and the recent files
  POST   /workbench/open      {"path"}      a notebook's cells or a script's text
  POST   /workbench/save      {"path", "cells" | "text", "modified", "force"}
  GET    /workbench/pythons                 the Pythons a kernel can run in (this server's own first)
  POST   /workbench/kernels   {"python", "path"}   start one, for a document
  POST   /workbench/kernels/{id}/execute    {"code", "kind", "connection"}: the outputs as they come,
                                            one JSON object a line, the last {"done": ...}
  POST   /workbench/kernels/{id}/interrupt, /restart       DELETE /workbench/kernels/{id}
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import BaseRoute, Route

from swissarmygrid.server.http import actor, error_response, json_response, read_json
from swissarmygrid.services.workbench import Workbench


def _agent_refused() -> Response:
    return Response(
        "The Workbench is the user's own: an agent cannot open, save or run their notebooks and scripts.",
        status_code=403,
    )


def routes(workbench: Workbench) -> list[BaseRoute]:
    def user_only(handler: Any) -> Any:
        async def guarded(request: Request) -> Response:
            if actor(request) == "agent":
                return _agent_refused()
            try:
                return await handler(request)  # type: ignore[no-any-return]
            except Exception as error:
                return error_response(error)

        return guarded

    async def home(request: Request) -> Response:
        return json_response(workbench.home())

    async def open_document(request: Request) -> Response:
        body = await read_json(request)
        return json_response(workbench.open(body.get("path") if isinstance(body, dict) else None))

    async def save(request: Request) -> Response:
        return json_response(workbench.save(await read_json(request)))

    async def pythons(request: Request) -> Response:
        return json_response(workbench.pythons())

    async def start(request: Request) -> Response:
        body = await read_json(request) if int(request.headers.get("content-length") or 0) else {}
        return json_response(await workbench.start(body))

    async def execute(request: Request) -> Response:
        outputs = workbench.execute(request.path_params["kernel"], await read_json(request))

        async def lines() -> AsyncIterator[bytes]:
            try:
                async for output in outputs:
                    yield (json.dumps(output) + "\n").encode()
            except Exception as error:  # the kernel went away mid-run
                yield (json.dumps({"done": {"ok": False, "error": str(error)}}) + "\n").encode()

        return StreamingResponse(
            lines(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"}
        )

    async def interrupt(request: Request) -> Response:
        await workbench.interrupt(request.path_params["kernel"])
        return json_response({"ok": True})

    async def restart(request: Request) -> Response:
        await workbench.restart(request.path_params["kernel"])
        return json_response({"ok": True})

    async def stop(request: Request) -> Response:
        await workbench.stop(request.path_params["kernel"])
        return json_response({"ok": True})

    return [
        Route("/workbench/home", user_only(home)),
        Route("/workbench/open", user_only(open_document), methods=["POST"]),
        Route("/workbench/save", user_only(save), methods=["POST"]),
        Route("/workbench/pythons", user_only(pythons)),
        Route("/workbench/kernels", user_only(start), methods=["POST"]),
        Route("/workbench/kernels/{kernel}/execute", user_only(execute), methods=["POST"]),
        Route("/workbench/kernels/{kernel}/interrupt", user_only(interrupt), methods=["POST"]),
        Route("/workbench/kernels/{kernel}/restart", user_only(restart), methods=["POST"]),
        Route("/workbench/kernels/{kernel}", user_only(stop), methods=["DELETE"]),
    ]
