"""Routes for Python sessions that offer tables (see processes.py).

  POST /processes/{id}/sync              the session's report: label, pid, tables; replies what it follows
  GET  /processes/{id}/requests?wait=N   the session's listener: long-poll for requests to load a table
  GET  /processes                        the page: every live session with its tables
  POST /processes/{id}/open              the page: ask a session to push one of its tables
  POST /processes/{id}/models            the session: the relationship models its notebook is associated with
  POST /processes/{id}/models/apply      the page or an agent: merge some (or all) of them into the current
  model
  POST /processes/{id}/models/dismiss    the page or an agent: skip them (the page stops asking)

The agent may read the list (names and sizes, the same as its other schema reads) but not open a
table: loading rows out of a running Python session is the user's action.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.engine.relationships import merge
from swissarmygrid.server.context import Context, actor, error_response, json_response, read_json
from swissarmygrid.server.routes_models import Relationships

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


def routes(ctx: Context, current: Relationships) -> list[BaseRoute]:
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
        return json_response(
            {
                "following": registry.following(process_id, store_has),
                # True until the session has said which models its notebook goes with.
                "models_wanted": registry.wants_models(process_id),
            }
        )

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

    async def offer_models(request: Request) -> Response:
        """The session's answer to models_wanted: its notebook's key and the models found for it."""
        if actor(request) == "agent":
            return Response("Only a Python session reports its notebook's models.", status_code=403)
        try:
            body: Any = await read_json(request)
            if not isinstance(body, dict):
                raise ValueError("Send {notebook, models, error}.")
            offer = registry.set_offer(
                request.path_params["process"],
                body.get("notebook"),
                body.get("models", []),
                body.get("error"),
            )
        except Exception as error:
            return error_response(error)
        if offer is None:
            return Response("That Python session is no longer there.", status_code=404)
        await ctx.live.broadcast({"type": "models_offered", "process": request.path_params["process"]})
        return json_response({"stored": len(offer.models)}, status_code=202)

    async def apply_models(request: Request) -> Response:
        """Merge the chosen offered models (all, when no names are given) into the current model."""
        try:
            process_id = request.path_params["process"]
            offer = registry.offer(process_id)
            if offer is None:
                return Response("That session has no models on offer.", status_code=404)
            body: Any = await read_json(request) if await request.body() else {}
            names = body.get("names") if isinstance(body, dict) else None
            if names is not None and not (isinstance(names, list) and all(isinstance(n, str) for n in names)):
                raise ValueError('names is a list of model names, e.g. {"names": ["CMM"]}.')
            chosen = list(offer.models) if names is None else names
            missing = [n for n in chosen if n not in offer.models]
            if missing:
                return Response(
                    f"Not on offer: {', '.join(missing)}. On offer: {', '.join(offer.models)}.",
                    status_code=404,
                )
            for name in chosen:
                current.model = merge(current.model, offer.models[name])
            registry.mark_handled(process_id)
            ctx.audit.write(actor(request), "apply_models", None, notebook=offer.notebook, models=chosen)
            await ctx.live.broadcast({"type": "relationships_updated"})
            await ctx.live.broadcast({"type": "models_handled", "process": process_id})
            return json_response({"applied": chosen, "model": current.model.to_json()})
        except Exception as error:
            return error_response(error)

    async def dismiss_models(request: Request) -> Response:
        process_id = request.path_params["process"]
        if not registry.mark_handled(process_id):
            return Response("That session has no models on offer.", status_code=404)
        ctx.audit.write(actor(request), "dismiss_models", None)
        await ctx.live.broadcast({"type": "models_handled", "process": process_id})
        return json_response({"dismissed": True})

    return [
        Route("/processes", listing),
        Route("/processes/{process}/models", offer_models, methods=["POST"]),
        Route("/processes/{process}/models/apply", apply_models, methods=["POST"]),
        Route("/processes/{process}/models/dismiss", dismiss_models, methods=["POST"]),
        Route("/processes/{process}/sync", sync, methods=["POST"]),
        Route("/processes/{process}/requests", requests),
        Route("/processes/{process}/open", open_table, methods=["POST"]),
    ]
