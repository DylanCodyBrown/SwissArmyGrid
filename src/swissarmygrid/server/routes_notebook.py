"""The notebook tab's routes: marimo served through SAG, and the changes SAG makes to it (plan part 2).

  GET  /notebook                 the notebook page: blades and catalog left of marimo (its own tab)
  POST /notebook/session         start marimo if needed; give this page the cookie the frame uses
  POST /notebook/imports         add missing imports to the open notebook, in a new cell at the top
  POST /notebook/drop            code dragged from the rail: into the cell it was dropped on, or after it
  POST /grids/{id}/notebook-cell a grid's steps as one marimo SQL cell in the notebook (code-behind)
  *    /marimo/...               marimo itself (HTTP and websockets), with marimo's token added here

A frame cannot put SAG's token in its requests' headers, so /marimo/ is reached with a cookie instead:
random for each server start, given only to a page that already holds the token (POST
/notebook/session), HttpOnly, SameSite=Strict and limited to /marimo. The guard still checks Host and
Origin on every request. The agent never reaches the notebook (decision 4: it runs code).
"""

from __future__ import annotations

import asyncio
import http.client
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from swissarmygrid.server.http import actor, error_response, json_response, read_json, read_json_object
from swissarmygrid.server.notebook import BASE_URL, DEFAULT_NOTEBOOK, NotebookError, NotebookServer
from swissarmygrid.services.notebook import NotebookCells

NOTEBOOK_COOKIE: str = "sag_notebook"
# Headers that belong to one hop, or carry SAG's own credentials, never passed on.
_HOP: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "cookie",
        "authorization",
    }
)


def _agent_refused() -> Response:
    return Response(
        "The notebook runs code, so it is the user's alone: an agent cannot reach it.", status_code=403
    )


def routes(cells: NotebookCells, notebook: NotebookServer, cookie: str, web_root: Path) -> list[BaseRoute]:
    """`cells`: the use cases. `notebook` and `cookie`: marimo itself, which this door starts and
    passes requests through to, and the cookie its frame is let in with."""
    ws = cells.ws

    async def page(request: Request) -> Response:
        return FileResponse(web_root / "notebook.html", headers={"Cache-Control": "no-store"})

    async def session(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            await notebook.ensure_started()
        except NotebookError as error:
            return Response(str(error), status_code=503)
        answer = json_response({"src": f"{BASE_URL}/?file={DEFAULT_NOTEBOOK}", "file": DEFAULT_NOTEBOOK})
        answer.set_cookie(NOTEBOOK_COOKIE, cookie, path=BASE_URL, httponly=True, samesite="strict")
        return answer

    async def imports(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            body = await read_json_object(request, '{"lines": ["import swissarmygrid as sag"]}')
            return json_response(await cells.imports(body))
        except NotebookError as error:
            return Response(str(error), status_code=409)
        except Exception as error:
            return error_response(error)

    async def drop(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            body = await read_json_object(request, '{"code": "...", "imports": [...], "cell": "<id>"}')
            return json_response(await cells.drop(body))
        except NotebookError as error:
            return Response(str(error), status_code=409)
        except Exception as error:
            return error_response(error)

    async def grid_cell(request: Request) -> Response:
        """Code-behind (plan phase 2): this grid's steps, as one DuckDB query over its table's own
        variable, written into the notebook as a marimo SQL cell and run."""
        if actor(request) == "agent":
            return _agent_refused()
        try:
            grid, info = ws.grid_and_info(request.path_params["grid_id"])
            cell = await cells.grid_cell(grid, info)
            body = await read_json(request) if int(request.headers.get("content-length") or 0) else {}
            file: Any = body.get("file") if isinstance(body, dict) and body.get("file") else None
            return json_response(await cells.keep_grid_cell(grid, info, cell, file))
        except NotebookError as error:
            return Response(str(error), status_code=409)
        except Exception as error:
            return error_response(error)

    # ===== marimo, through SAG =====

    def _open(
        method: str, target: str, body: bytes, headers: dict[str, str]
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        connection = http.client.HTTPConnection("127.0.0.1", notebook.port or 0, timeout=600)
        connection.request(method, target, body=body or None, headers=headers)
        return connection, connection.getresponse()

    async def proxy(request: Request) -> Response:
        try:
            await notebook.ensure_started()
        except NotebookError as error:
            return Response(str(error), status_code=503)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
        headers["Authorization"] = f"Bearer {notebook.token}"
        body = await request.body()
        try:
            connection, upstream = await asyncio.to_thread(_open, request.method, target, body, headers)
        except OSError as error:
            return Response(
                f"The notebook is not answering ({error}). Reload to start it again.", status_code=502
            )
        passed = {
            k: v for k, v in upstream.getheaders() if k.lower() not in _HOP and k.lower() != "set-cookie"
        }

        async def stream() -> AsyncIterator[bytes]:
            try:
                while True:
                    # read1: what has arrived, so a server-sent event stream flows as it is written.
                    chunk = await asyncio.to_thread(upstream.read1, 65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                connection.close()

        return StreamingResponse(stream(), status_code=upstream.status, headers=passed)

    async def websocket_proxy(websocket: WebSocket) -> None:
        import websockets

        try:
            await notebook.ensure_started()
        except NotebookError:
            await websocket.close(code=1011)
            return
        query = websocket.url.query
        url = f"ws://127.0.0.1:{notebook.port}{websocket.url.path}" + (f"?{query}" if query else "")
        offered: list[str] = websocket.scope.get("subprotocols", [])
        try:
            upstream = await websockets.connect(
                url,
                additional_headers={"Authorization": f"Bearer {notebook.token}"},
                subprotocols=[websockets.Subprotocol(p) for p in offered] or None,
                max_size=None,
                open_timeout=30,
            )
        except (OSError, TimeoutError, websockets.exceptions.WebSocketException):
            await websocket.close(code=1011)
            return
        await websocket.accept(subprotocol=upstream.subprotocol)

        async def to_marimo() -> None:
            while True:
                message: Any = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("text") is not None:
                    await upstream.send(message["text"])
                elif message.get("bytes") is not None:
                    await upstream.send(message["bytes"])

        async def to_page() -> None:
            async for message in upstream:
                if isinstance(message, str):
                    await websocket.send_text(message)
                else:
                    await websocket.send_bytes(message)

        tasks = [asyncio.create_task(to_marimo()), asyncio.create_task(to_page())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await upstream.close()
            try:
                await websocket.close()
            except RuntimeError:
                pass  # already closed by the page

    async def bare(request: Request) -> Response:
        return RedirectResponse(f"{BASE_URL}/" + (f"?{request.url.query}" if request.url.query else ""))

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    return [
        Route("/notebook", page),
        Route("/notebook/session", session, methods=["POST"]),
        Route("/notebook/imports", imports, methods=["POST"]),
        Route("/notebook/drop", drop, methods=["POST"]),
        Route("/grids/{grid_id}/notebook-cell", grid_cell, methods=["POST"]),
        Route(BASE_URL, bare, methods=methods),
        Route(BASE_URL + "/{path:path}", proxy, methods=methods),
        WebSocketRoute(BASE_URL + "/{path:path}", websocket_proxy),
    ]
