"""The notebook tab's routes: marimo served through SAG, and the changes SAG makes to it (plan part 2).

  GET  /notebook                 the notebook page: blades and catalog left of marimo (its own tab)
  POST /notebook/session         start marimo if needed; give this page the cookie the frame uses
  POST /notebook/imports         add missing imports to the open notebook, in a new cell at the top
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
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from swissarmygrid.server.context import Context, actor, error_response, json_response, read_json_object
from swissarmygrid.server.notebook import BASE_URL, DEFAULT_NOTEBOOK, NotebookControl, NotebookError

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
_IMPORT_LINE_MAX: int = 300


def _agent_refused() -> Response:
    return Response(
        "The notebook runs code, so it is the user's alone: an agent cannot reach it.", status_code=403
    )


def routes(ctx: Context) -> list[BaseRoute]:
    notebook = ctx.notebook
    control = NotebookControl(notebook)

    async def page(request: Request) -> Response:
        return FileResponse(ctx.settings.web_root / "notebook.html", headers={"Cache-Control": "no-store"})

    async def session(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            await notebook.ensure_started()
        except NotebookError as error:
            return Response(str(error), status_code=503)
        answer = json_response({"src": f"{BASE_URL}/?file={DEFAULT_NOTEBOOK}", "file": DEFAULT_NOTEBOOK})
        answer.set_cookie(
            NOTEBOOK_COOKIE, ctx.notebook_cookie, path=BASE_URL, httponly=True, samesite="strict"
        )
        return answer

    async def imports(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            body = await read_json_object(request, '{"lines": ["import swissarmygrid as sag"]}')
            lines = body.get("lines")
            file = body.get("file") or DEFAULT_NOTEBOOK
            if (
                not isinstance(lines, list)
                or not 0 < len(lines) <= 20
                or not all(isinstance(line, str) and 0 < len(line) <= _IMPORT_LINE_MAX for line in lines)
                or not all(line.lstrip().startswith(("import ", "from ")) for line in lines)
            ):
                raise ValueError("Send 1 to 20 import statements, such as import swissarmygrid as sag.")
            if not isinstance(file, str) or "/" in file or "\\" in file or not file.endswith(".py"):
                raise ValueError("Name a notebook in the notebooks folder, such as workspace.py.")
            added = await control.ensure_imports(file, [line.strip() for line in lines])
            return json_response({"added": added})
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
        Route(BASE_URL, bare, methods=methods),
        Route(BASE_URL + "/{path:path}", proxy, methods=methods),
        WebSocketRoute(BASE_URL + "/{path:path}", websocket_proxy),
    ]
