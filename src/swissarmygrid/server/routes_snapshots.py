"""Snapshots: a PNG of the page the user is looking at, for an agent to analyze.

The server draws nothing. The picture has to be what the user actually sees (their scroll, their
selection, a split, an open panel), and only their browser knows that, so the page draws itself and
this module is the go-between:

    agent  --POST /snapshots-->  server  --"snapshot_request" on /ws-->  page
    agent  <--image/png---------  server  <--POST /snapshots/{id} (the PNG)--  page

Think of it as a request/reply over a message bus: the agent's HTTP call parks on a future, the
page's upload completes it. Only the page may upload (a token holder that is not the agent), so an
agent cannot answer its own request with a picture of its choosing. The bytes are held in memory
for the length of one request and handed straight back: nothing is written to disk.

Every snapshot is written to the audit log.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.bootstrap import Services as Context
from swissarmygrid.server.http import (
    BodyTooLargeError,
    actor,
    error_response,
    json_response,
    read_body,
    read_json,
)

# The page areas an agent may ask for: what the page's own elements are called.
AREAS: tuple[str, ...] = ("app", "ribbon", "grid", "steps", "sandbox")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# The page's reason it could not draw: a sentence, cut to 300 characters for the agent.
MAX_REASON_BYTES: int = 64 * 1024

Answer = asyncio.Future[tuple[bytes, dict[str, str]]]


class SnapshotFailed(Exception):
    """The page could not draw itself; the message is its reason."""


def routes(ctx: Context) -> list[BaseRoute]:
    pending: dict[str, Answer] = {}
    settings = ctx.settings

    async def request_snapshot(request: Request) -> Response:
        try:
            body: Any = await read_json(request) if await request.body() else {}
            if not isinstance(body, dict):
                raise ValueError('Send {"area": ..., "scale": 1 or 2} (both optional).')
            area = body.get("area", "app")
            scale = body.get("scale", 1)
            if area not in AREAS:
                raise ValueError(f"area must be one of {', '.join(AREAS)}.")
            if scale not in (1, 2) or isinstance(scale, bool):
                raise ValueError("scale must be 1 or 2.")
            if not ctx.live.page_open():
                return Response(
                    "No SwissArmyGrid page is open to draw the snapshot. Ask the user to open the "
                    "SwissArmyGrid tab in VS Code.",
                    status_code=409,
                )
            snapshot_id = secrets.token_hex(6)
            waiting: Answer = asyncio.get_running_loop().create_future()
            pending[snapshot_id] = waiting
            try:
                await ctx.live.broadcast(
                    {
                        "type": "snapshot_request",
                        "id": snapshot_id,
                        "area": area,
                        "scale": scale,
                    }
                )
                data, meta = await asyncio.wait_for(waiting, settings.snapshot_timeout_seconds)
            except TimeoutError:
                return Response(
                    f"The page did not answer within {settings.snapshot_timeout_seconds:.0f} seconds. "
                    "It draws only while its tab is visible: ask the user to bring the SwissArmyGrid "
                    "tab to the front.",
                    status_code=504,
                )
            except SnapshotFailed as error:
                return Response(f"The page could not draw the snapshot: {error}", status_code=502)
            finally:
                pending.pop(snapshot_id, None)
            ctx.audit.write(actor(request), "snapshot", None, area=area, scale=scale, bytes=len(data))
            headers = {f"X-Snapshot-{k.title()}": v for k, v in meta.items()}
            return Response(data, media_type="image/png", headers=headers | {"X-Snapshot-Area": area})
        except Exception as error:
            return error_response(error)

    def waiting_for(request: Request) -> Answer | None:
        found = pending.get(request.path_params["snapshot_id"])
        return None if found is None or found.done() else found

    async def upload(request: Request) -> Response:
        """The page's answer: the PNG it drew. Only the page may send it."""
        if actor(request) == "agent":
            return Response("Only the page answers a snapshot request.", status_code=403)
        waiting = waiting_for(request)
        if waiting is None:
            return Response(
                "No snapshot is waiting for that answer (it finished or timed out).", status_code=404
            )
        try:  # refused past the limit while it arrives, not after it was read whole
            data = await read_body(request, settings.max_snapshot_bytes)
        except BodyTooLargeError:
            return Response(
                f"That picture is larger than the {settings.max_snapshot_bytes:,}-byte limit; "
                "ask for scale 1 or a smaller area.",
                status_code=413,
            )
        if not data.startswith(PNG_SIGNATURE):
            return Response("That is not a PNG.", status_code=400)
        meta = {k: v for k in ("width", "height") if (v := request.query_params.get(k, "")).isdigit()}
        if waiting.done():  # it timed out (or another answer won) while this one was arriving
            return Response("That snapshot request is over (it timed out or was answered).", status_code=409)
        waiting.set_result((data, meta))
        return json_response({"received": len(data)})

    async def fail(request: Request) -> Response:
        """The page could not draw (a blocked image, an empty area): its reason reaches the agent."""
        if actor(request) == "agent":
            return Response("Only the page answers a snapshot request.", status_code=403)
        waiting = waiting_for(request)
        if waiting is None:
            return Response("No snapshot is waiting for that answer.", status_code=404)
        try:
            raw = await read_body(request, MAX_REASON_BYTES)
        except BodyTooLargeError:
            raw = b"the page's reason was too long to pass on"
        reason = raw.decode("utf-8", "replace")[:300] or "no reason given"
        if waiting.done():
            return Response("That snapshot request is over (it timed out or was answered).", status_code=409)
        waiting.set_exception(SnapshotFailed(reason))
        return json_response({"failed": True})

    return [
        Route("/snapshots", request_snapshot, methods=["POST"]),
        Route("/snapshots/{snapshot_id}", upload, methods=["POST"]),
        Route("/snapshots/{snapshot_id}/error", fail, methods=["POST"]),
    ]
