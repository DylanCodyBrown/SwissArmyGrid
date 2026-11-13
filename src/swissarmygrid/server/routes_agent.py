"""The agent door: what an MCP client may read and ask for (plan section 12).

Every request from the MCP launcher carries `X-Actor: agent`, so the audit log and the grid
page can tell agent actions from the user's. The use cases, and what the agent may and may not do,
are in services/agent.py; this file reads the requests and shapes the answers.

  GET  /agent/approvals                 the questions waiting for the user
  POST /agent/approvals/{id}            the user's answer (refused for the agent)
  GET  /agent/describe/{dataset}        schema, counts and the profile of every column
  GET  /agent/profile/{dataset}?column= one column's profile
  POST /agent/query                     {"dataset"|"grid", "sql"} one SELECT
  GET  /agent/view-state/{grid_id}      the grid's steps, settings, row count, selection
  GET  /agent/read/{grid_id}?offset=&limit=   a window of the view as the user sees it
  POST /agent/export-approval/{grid_id}?format=   ask the user; their click gives a one-time ticket
  GET  /agent/blades                    names, kinds, descriptions and tags (never code or secrets)
  POST /agent/blades                    propose a new blade: saved only on the user's Allow
  GET  /agent/blade-templates?id=       the starter blades
  POST /workspace/active                the page reports which grid tab is in front
  GET  /agent/context                   the current table: a short summary, no cell values

Cell values reach the agent only as data inside the MCP layer's delimited block
(swissarmygrid.mcp.server), never in a tool description or instruction text.
"""

from __future__ import annotations

import duckdb
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.server.http import actor, error_response, json_response, read_json, read_json_object
from swissarmygrid.services.agent import AgentService


def routes(agent: AgentService) -> list[BaseRoute]:
    ws = agent.ws

    # ===== the user's answers (refused for the agent) =====

    async def list_approvals(request: Request) -> Response:
        return json_response({"pending": agent.approvals.pending()})

    async def resolve_approval(request: Request) -> Response:
        if actor(request) == "agent":
            return Response("Only the user can approve.", status_code=403)
        approval = agent.approvals.get(request.path_params["approval_id"])
        if approval is None:
            return Response("No such pending request (it may have timed out).", status_code=404)
        try:
            body = await read_json(request)
        except ValueError as error:
            return error_response(error)
        return json_response(agent.resolve_approval(approval, body))

    # ===== agent reads =====

    async def describe(request: Request) -> Response:
        """Schema, counts and the profile of every column."""
        try:
            return json_response(await agent.describe(request.path_params["dataset"]))
        except Exception as error:
            return error_response(error)

    async def profile(request: Request) -> Response:
        try:
            return json_response(
                await agent.profile(request.path_params["dataset"], request.query_params.get("column"))
            )
        except Exception as error:
            return error_response(error)

    async def query(request: Request) -> Response:
        try:
            body = await read_json_object(request, '{"dataset": name, "sql": "SELECT ... FROM grid"}')
            try:
                answer = await agent.query(body)
            except duckdb.Error as error:
                return Response(f"DuckDB: {error}", status_code=400)
            # Up to 1,048,576 rows: the same byte limit as the SQL box's answer.
            return json_response(answer, max_bytes=ws.settings.max_sql_response_bytes)
        except Exception as error:
            return error_response(error)

    async def read_grid(request: Request) -> Response:
        """A window of the grid's view as the user sees it, always with the view's row count."""
        try:
            grid, info = ws.grid_and_info(request.path_params["grid_id"])
            offset = max(0, int(request.query_params.get("offset", "0")))
            limit = max(0, int(request.query_params.get("limit", "100")))
            try:
                answer = await agent.read_grid(grid, info, offset, limit)
            except duckdb.Error as error:
                return Response(f"DuckDB: {error}", status_code=400)
            if "capped" not in answer:  # nothing to read at that offset: the counts alone
                return json_response(answer)
            return json_response(answer, max_bytes=ws.settings.max_sql_response_bytes)
        except Exception as error:
            return error_response(error)

    async def view_state(request: Request) -> Response:
        try:
            grid, info = ws.grid_and_info(request.path_params["grid_id"])
            return json_response(await agent.view_state(grid, info))
        except Exception as error:
            return error_response(error)

    # ===== the current table =====

    async def set_active(request: Request) -> Response:
        if actor(request) == "agent":
            return Response("Only the page reports which grid is in front.", status_code=403)
        try:
            body = await read_json(request)
            return json_response(agent.set_active(body.get("grid") if isinstance(body, dict) else None))
        except Exception as error:
            return error_response(error)

    async def current(request: Request) -> Response:
        try:
            return json_response(await agent.current())
        except Exception as error:
            return error_response(error)

    # ===== blades: the agent lists them and proposes new ones; the user's Allow saves one =====

    async def list_blades(request: Request) -> Response:
        try:
            return json_response(agent.list_blades())
        except Exception as error:
            return error_response(error)

    async def blade_templates(request: Request) -> Response:
        """The starter blades (public examples, no user data): the list, or one in full by id."""
        try:
            return json_response(agent.blade_templates(request.query_params.get("id")))
        except Exception as error:
            return error_response(error)

    async def propose_blade(request: Request) -> Response:
        """A new blade from the agent, saved only when the user allows it."""
        try:
            body = await read_json_object(request, '{"name": "...", "kind": "sql", "code": "..."}')
            return json_response(await agent.propose_blade(body))
        except Exception as error:
            return error_response(error)

    async def approve_export(request: Request) -> Response:
        """An agent export: always waits for the user's click (plan section 10a)."""
        try:
            grid, info = ws.grid_and_info(request.path_params["grid_id"])
            return json_response(
                await agent.approve_export(grid, info, request.query_params.get("format", "csv"))
            )
        except Exception as error:
            return error_response(error)

    return [
        Route("/agent/approvals", list_approvals),
        Route("/agent/approvals/{approval_id}", resolve_approval, methods=["POST"]),
        Route("/agent/describe/{dataset}", describe),
        Route("/agent/profile/{dataset}", profile),
        Route("/agent/query", query, methods=["POST"]),
        Route("/agent/view-state/{grid_id}", view_state),
        Route("/agent/read/{grid_id}", read_grid),
        Route("/agent/export-approval/{grid_id}", approve_export, methods=["POST"]),
        Route("/agent/blades", list_blades),
        Route("/agent/blades", propose_blade, methods=["POST"]),
        Route("/agent/blade-templates", blade_templates),
        Route("/workspace/active", set_active, methods=["POST"]),
        Route("/agent/context", current),
    ]
