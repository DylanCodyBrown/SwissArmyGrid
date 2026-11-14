"""The Workbench (services/workbench.py): notebooks and scripts in their own window, and the kernels that
run them. The golden rule (the owner, 2026-11-14): the agent may do all of it too, running code included.

  GET    /workbench                         the page (its own window, opened from the grid page's tab)
  GET    /workbench/home                    where Save as starts, and the recent files
  POST   /workbench/open      {"path"}      a notebook's cells or a script's text
  POST   /workbench/save      {"path", "cells" | "text", "modified", "force"}
  GET    /workbench/pythons                 the Pythons a kernel can run in (this server's own first)
  GET    /workbench/environments            what New environment offers: libraries, presets, versions
  POST   /workbench/environments  {"folder", "python", "packages", "document"}: make one; its progress
                                            as it goes, one JSON object a line, the last {"done": ...}
  POST   /workbench/environment   {"path", "python"}: remember the Python chosen for a document
  POST   /workbench/kernels   {"python", "path"}   start one, for a document
  POST   /workbench/kernels/{id}/execute    {"code", "kind", "connection"}: the outputs as they come,
                                            one JSON object a line, the last {"done": ...}
  POST   /workbench/kernels/{id}/input      {"value"}: the line typed for input(), getpass or pdb
  POST   /workbench/kernels/{id}/is-complete {"code"}: a whole statement yet? (the console's Enter)
  POST   /workbench/window               {"command", "args"}: run a command of the window's palette there
  POST   /workbench/window/{id}          the window's answer: {"result"} or {"error"} (only the window)
  POST   /workbench/statement            {"code", "line"}: the statement there (Ctrl+Enter), the next
  POST   /workbench/kernels/{id}/complete   {"code", "offset"}: the kernel's completions there
  POST   /workbench/kernels/{id}/inspect    {"code", "offset"}: the signature and help of the name there
  GET    /workbench/kernels/{id}/variables  the Variables overlay (values of secrets never included)
  POST   /workbench/kernels/{id}/grid       {"name"}: open a table variable as a grid
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
from swissarmygrid.services.workbench_window import WindowRefused, WindowUnanswered, WorkbenchWindow


def routes(workbench: Workbench, window: WorkbenchWindow) -> list[BaseRoute]:
    def safely(handler: Any) -> Any:
        async def guarded(request: Request) -> Response:
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

    async def environments(request: Request) -> Response:
        return json_response(workbench.environments())

    async def create_environment(request: Request) -> Response:
        events = workbench.create_environment(await read_json(request))

        async def lines() -> AsyncIterator[bytes]:
            try:
                async for event in events:
                    yield (json.dumps(event) + "\n").encode()
            except Exception as error:
                yield (json.dumps({"done": {"ok": False, "error": str(error)}}) + "\n").encode()

        return StreamingResponse(
            lines(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"}
        )

    async def choose_environment(request: Request) -> Response:
        return json_response(workbench.choose_environment(await read_json(request)))

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

    async def answer_input(request: Request) -> Response:
        return json_response(workbench.answer_input(request.path_params["kernel"], await read_json(request)))

    async def window_command(request: Request) -> Response:
        """Any command of the Workbench window's palette, run there (the agent's workbench_window)."""
        body = await read_json(request)
        if not isinstance(body, dict):
            raise ValueError('Send {"command": "...", "args": {...}}.')
        try:
            result = await window.ask(body.get("command"), body.get("args"), actor(request))
        except WindowRefused as error:
            return Response(str(error), status_code=409)
        except WindowUnanswered as error:
            return Response(str(error), status_code=504)
        return json_response({"result": result})

    async def window_answer(request: Request) -> Response:
        """The window's answer to a command. Only the window may send it."""
        if actor(request) == "agent":
            return Response("Only the Workbench window answers its commands.", status_code=403)
        waiting = window.waiting(request.path_params["request"])
        if waiting is None:
            return Response(
                "No command is waiting for that answer (it finished or timed out).", status_code=404
            )
        if not window.answer(waiting, await read_json(request)):
            return Response("That command is over (it timed out or was answered).", status_code=409)
        return json_response({"received": True})

    async def statement(request: Request) -> Response:
        return json_response(workbench.statement(await read_json(request)))

    async def is_complete(request: Request) -> Response:
        return json_response(
            await workbench.is_complete(request.path_params["kernel"], await read_json(request))
        )

    async def complete(request: Request) -> Response:
        return json_response(
            await workbench.complete(request.path_params["kernel"], await read_json(request))
        )

    async def inspect(request: Request) -> Response:
        return json_response(await workbench.inspect(request.path_params["kernel"], await read_json(request)))

    async def variables(request: Request) -> Response:
        return json_response(await workbench.variables(request.path_params["kernel"]))

    async def grid(request: Request) -> Response:
        return json_response(await workbench.grid(request.path_params["kernel"], await read_json(request)))

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
        Route("/workbench/home", safely(home)),
        Route("/workbench/open", safely(open_document), methods=["POST"]),
        Route("/workbench/save", safely(save), methods=["POST"]),
        Route("/workbench/pythons", safely(pythons)),
        Route("/workbench/environments", safely(environments)),
        Route("/workbench/environments", safely(create_environment), methods=["POST"]),
        Route("/workbench/environment", safely(choose_environment), methods=["POST"]),
        Route("/workbench/kernels", safely(start), methods=["POST"]),
        Route("/workbench/statement", safely(statement), methods=["POST"]),
        Route("/workbench/window", safely(window_command), methods=["POST"]),
        Route("/workbench/window/{request}", safely(window_answer), methods=["POST"]),
        Route("/workbench/kernels/{kernel}/execute", safely(execute), methods=["POST"]),
        Route("/workbench/kernels/{kernel}/input", safely(answer_input), methods=["POST"]),
        Route("/workbench/kernels/{kernel}/is-complete", safely(is_complete), methods=["POST"]),
        Route("/workbench/kernels/{kernel}/complete", safely(complete), methods=["POST"]),
        Route("/workbench/kernels/{kernel}/inspect", safely(inspect), methods=["POST"]),
        Route("/workbench/kernels/{kernel}/variables", safely(variables)),
        Route("/workbench/kernels/{kernel}/grid", safely(grid), methods=["POST"]),
        Route("/workbench/kernels/{kernel}/interrupt", safely(interrupt), methods=["POST"]),
        Route("/workbench/kernels/{kernel}/restart", safely(restart), methods=["POST"]),
        Route("/workbench/kernels/{kernel}", safely(stop), methods=["DELETE"]),
    ]
