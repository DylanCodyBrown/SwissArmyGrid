"""Snapshots: a PNG of the page the user is looking at, for an agent to analyze
(services/snapshots.py: the page draws itself; the server is the go-between and keeps nothing).

  POST /snapshots                 {"area", "scale"} the agent asks; the answer is the PNG
  POST /snapshots/{id}            the page's answer: the PNG it drew (only the page may send it)
  POST /snapshots/{id}/error      the page could not draw: its reason, passed on to the agent
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.server.http import (
    BodyTooLargeError,
    actor,
    error_response,
    json_response,
    read_body,
    read_json,
)
from swissarmygrid.services.snapshots import (
    PNG_SIGNATURE,
    SnapshotFailed,
    Snapshots,
    SnapshotUnanswered,
)

# The page's reason it could not draw: a sentence, cut to 300 characters for the agent.
MAX_REASON_BYTES: int = 64 * 1024


def routes(snapshots: Snapshots) -> list[BaseRoute]:
    settings = snapshots.ws.settings

    async def request_snapshot(request: Request) -> Response:
        try:
            body: Any = await read_json(request) if await request.body() else {}
            if not isinstance(body, dict):
                raise ValueError('Send {"area": ..., "scale": 1 or 2} (both optional).')
            area = body.get("area", "app")
            try:
                data, meta = await snapshots.take(area, body.get("scale", 1), actor(request))
            except SnapshotUnanswered as error:
                return Response(str(error), status_code=504)
            except SnapshotFailed as error:
                return Response(f"The page could not draw the snapshot: {error}", status_code=502)
            headers = {f"X-Snapshot-{k.title()}": v for k, v in meta.items()}
            return Response(data, media_type="image/png", headers=headers | {"X-Snapshot-Area": area})
        except Exception as error:
            return error_response(error)

    async def upload(request: Request) -> Response:
        """The page's answer: the PNG it drew. Only the page may send it."""
        if actor(request) == "agent":
            return Response("Only the page answers a snapshot request.", status_code=403)
        waiting = snapshots.waiting(request.path_params["snapshot_id"])
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
        if not snapshots.answer(waiting, data, meta):
            return Response("That snapshot request is over (it timed out or was answered).", status_code=409)
        return json_response({"received": len(data)})

    async def fail(request: Request) -> Response:
        """The page could not draw (a blocked image, an empty area): its reason reaches the agent."""
        if actor(request) == "agent":
            return Response("Only the page answers a snapshot request.", status_code=403)
        waiting = snapshots.waiting(request.path_params["snapshot_id"])
        if waiting is None:
            return Response("No snapshot is waiting for that answer.", status_code=404)
        try:
            raw = await read_body(request, MAX_REASON_BYTES)
        except BodyTooLargeError:
            raw = b"the page's reason was too long to pass on"
        reason = raw.decode("utf-8", "replace")[:300] or "no reason given"
        if not snapshots.fail(waiting, reason):
            return Response("That snapshot request is over (it timed out or was answered).", status_code=409)
        return json_response({"failed": True})

    return [
        Route("/snapshots", request_snapshot, methods=["POST"]),
        Route("/snapshots/{snapshot_id}", upload, methods=["POST"]),
        Route("/snapshots/{snapshot_id}/error", fail, methods=["POST"]),
    ]
