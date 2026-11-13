"""Routes for Python sessions that offer tables (see processes.py).

  POST /processes/{id}/sync              the session's report: label, pid, tables; replies what it follows
  GET  /processes/{id}/requests?wait=N   the session's listener: long-poll for requests to load a table
  GET  /processes                        the page: every live session with its tables
  POST /processes/{id}/open              the page: ask a session to push one of its tables

The agent may read the list (names and sizes, the same as its other schema reads) but not open a
table: loading rows out of a running Python session is the user's action.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.bootstrap import Services as Context
from swissarmygrid.server.http import actor, error_response, json_response, read_json

MAX_WAIT_SECONDS: float = 30.0
_QUIET_PREFIX = "/processes/"


class QuietHeartbeats(logging.Filter):
    """Keeps the sessions' heartbeats and long polls out of uvicorn's access log: every open notebook
    sends one every few seconds, which would bury the lines worth reading. The page's own requests
    (GET /processes, POST .../open) are not under /processes/{id}/sync or /requests, so they stay."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        path = args[2] if isinstance(args, tuple) and len(args) >= 3 else ""
        if not isinstance(path, str) or not path.startswith(_QUIET_PREFIX):
            return True
        return not (path.split("?", 1)[0].endswith(("/sync", "/requests")))


def _clean_errors(raw: Any) -> list[tuple[str, str]]:
    """At most five (table name, message) pairs from a sync's `errors`, each message cut short."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("errors must be a list of {name, message}.")
    pairs: list[tuple[str, str]] = []
    for entry in raw[:5]:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and isinstance(entry.get("message"), str)
        ):
            pairs.append((entry["name"][:64], entry["message"][:300]))
    return pairs


def routes(ctx: Context) -> list[BaseRoute]:
    registry = ctx.processes

    def store_has(name: str) -> bool:
        return ctx.store.get(name) is not None

    async def sync(request: Request) -> Response:
        try:
            process_id = request.path_params["process"]
            body: Any = await read_json(request)
            if not isinstance(body, dict):
                raise ValueError("Send an object with label, pid and tables.")
            changed = registry.sync(process_id, body.get("label"), body.get("pid"), body.get("tables", []))
            errors = _clean_errors(body.get("errors"))
        except Exception as error:
            return error_response(error)
        if changed:
            await ctx.live.broadcast({"type": "processes_changed"})
        # Why a table the user clicked would not open (too large, say): the page shows it as a toast.
        label = next((s["label"] for s in registry.snapshot(store_has) if s["id"] == process_id), "")
        for name, message in errors:
            await ctx.live.broadcast(
                {"type": "process_error", "label": label, "name": name, "message": message}
            )
        return json_response({"following": registry.following(process_id, store_has)})

    async def requests(request: Request) -> Response:
        try:
            wait = float(request.query_params.get("wait", "0") or 0)
        except ValueError:
            return Response("wait must be a number of seconds.", status_code=400)
        process_id = request.path_params["process"]
        if not registry.knows(process_id):
            return Response("No such session; send a sync first.", status_code=404)
        taken = await registry.wait_requests(process_id, max(0.0, min(wait, MAX_WAIT_SECONDS)))
        return json_response({"requests": taken})

    async def listing(request: Request) -> Response:
        return json_response({"processes": registry.snapshot(store_has)})

    async def open_table(request: Request) -> Response:
        if actor(request) == "agent":
            return Response("Opening a table from a running Python session is for the user.", status_code=403)
        try:
            body: Any = await read_json(request)
            name = body.get("name") if isinstance(body, dict) else None
            if not isinstance(name, str):
                raise ValueError('Send {"name": "<table name>"}.')
        except Exception as error:
            return error_response(error)
        if not registry.request_load(request.path_params["process"], name):
            return Response("That Python session or table is no longer there.", status_code=404)
        return json_response({"queued": True}, status_code=202)

    return [
        Route("/processes", listing),
        Route("/processes/{process}/sync", sync, methods=["POST"]),
        Route("/processes/{process}/requests", requests),
        Route("/processes/{process}/open", open_table, methods=["POST"]),
    ]
