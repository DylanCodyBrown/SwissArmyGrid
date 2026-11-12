"""The agent door: what an MCP client may read and ask for (plan section 12).

Every request from the MCP launcher carries `X-Actor: agent`, so the audit log and the grid
page can tell agent actions from the user's. The agent sees what the page shows: schemas,
profiles, step values and rows, up to 1,048,576 rows per read (one Excel sheet). Exports by an
agent always wait for the user's click (a one-time ticket) and write only into the export folder.

Cell values reach the agent only as data inside the MCP layer's delimited block
(swissarmygrid.mcp.server), never in a tool description or instruction text.

Limitation, stated plainly: the approval endpoint trusts the browser. Any process running as
the same Windows user can read the connection file and call it; the gate stops an agent that
only has these MCP tools, not code already running with the user's rights.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import duckdb
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.engine.datasets import DatasetInfo, quote
from swissarmygrid.engine.export import FORMATS
from swissarmygrid.engine.profile import profile_column, profile_table
from swissarmygrid.engine.sql_guard import guard
from swissarmygrid.engine.steps import filter_condition
from swissarmygrid.engine.workers import AGENT
from swissarmygrid.server.context import (
    Context,
    actor,
    error_response,
    json_response,
    read_json,
    read_json_object,
)
from swissarmygrid.server.grids import Grid
from swissarmygrid.settings import EXCEL_MAX_ROWS

# The most rows one agent read returns: as many as one Excel worksheet holds. The agent still
# picks its own limit.
AGENT_ROW_CAP: int = EXCEL_MAX_ROWS
APPROVAL_TIMEOUT_SECONDS: float = 180.0
# How long the user's click on an export stays good for the export it approved.
EXPORT_TICKET_SECONDS: float = 120.0


@dataclass
class ExportTickets:
    """Approved agent exports not yet written: ticket -> (grid id, when it lapses). One click, one
    file. The export route writes an agent's file only against a ticket, so an agent cannot skip
    the user's click by calling the export route directly."""

    held: dict[str, tuple[str, float]] = field(default_factory=dict)

    def issue(self, grid_id: str) -> str:
        now = time.monotonic()
        self.held = {k: v for k, v in self.held.items() if v[1] > now}
        ticket = secrets.token_urlsafe(16)
        self.held[ticket] = (grid_id, now + EXPORT_TICKET_SECONDS)
        return ticket

    def redeem(self, ticket: str | None, grid_id: str) -> bool:
        """True once for a ticket the user approved for this grid; it is used up either way."""
        held = self.held.pop(ticket, None) if ticket else None
        return held is not None and held[0] == grid_id and held[1] > time.monotonic()


@dataclass
class Approval:
    id: str
    dataset: str
    action: str
    detail: str
    future: asyncio.Future[bool]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "dataset": self.dataset,
            "action": self.action,
            "detail": self.detail,
        }


def routes(ctx: Context, tickets: ExportTickets) -> list[BaseRoute]:
    """`tickets`: the agent exports the user approved, shared with routes_grids, which spends them."""
    approvals: dict[str, Approval] = {}

    def dataset_for(body: dict[str, Any]) -> tuple[DatasetInfo, Grid | None]:
        grid_id = body.get("grid")
        if isinstance(grid_id, str):
            grid, info = ctx.grid_and_info(grid_id)
            return info, grid
        name = body.get("dataset")
        found = ctx.store.get(name) if isinstance(name, str) else None
        if found is None:
            raise LookupError("Name an open dataset (or grid).")
        return found, ctx.grids.first_for(found.name)

    async def ask_user(dataset: str, action: str, detail: str) -> bool:
        loop = asyncio.get_running_loop()
        approval = Approval(secrets.token_hex(4), dataset, action, detail, loop.create_future())
        approvals[approval.id] = approval
        await ctx.live.broadcast({"type": "approval_requested", **approval.to_json()})
        try:
            return await asyncio.wait_for(approval.future, APPROVAL_TIMEOUT_SECONDS)
        except TimeoutError:
            return False
        finally:
            approvals.pop(approval.id, None)
            await ctx.live.broadcast({"type": "approval_resolved", "id": approval.id})

    # ===== the user's answers (refused for the agent) =====

    async def list_approvals(request: Request) -> Response:
        return json_response({"pending": [a.to_json() for a in approvals.values()]})

    async def resolve_approval(request: Request) -> Response:
        if actor(request) == "agent":
            return Response("Only the user can approve.", status_code=403)
        approval = approvals.get(request.path_params["approval_id"])
        if approval is None:
            return Response("No such pending request (it may have timed out).", status_code=404)
        try:
            body = await read_json(request)
        except ValueError as error:
            return error_response(error)
        approved = bool(isinstance(body, dict) and body.get("approve"))
        if not approval.future.done():
            approval.future.set_result(approved)
        ctx.audit.write("user", "approval", approval.dataset, approved=approved, request=approval.action)
        return json_response({"id": approval.id, "approved": approved})

    # ===== agent reads =====

    async def describe(request: Request) -> Response:
        """Schema, counts and the profile of every column."""
        try:
            info = ctx.store.get(request.path_params["dataset"])
            if info is None:
                raise LookupError("No such dataset.")

            def compute(cur: duckdb.DuckDBPyConnection) -> Any:
                names = ", ".join(quote(c.name) for c in info.columns)
                sql = f"SELECT {names} FROM {info.table}"
                return profile_table(cur, sql, info.columns)

            profiled = await ctx.workers.run(AGENT, lambda: compute(ctx.store.cursor()))
            ctx.audit.write("agent", "describe", info.name)
            return json_response(
                {
                    "dataset": info.name,
                    "version": info.version,
                    "rows": info.row_count,
                    "columns": profiled["columns"],
                    "duplicate_rows": profiled["duplicate_rows"],
                    "grids": [g.id for g in ctx.grids.for_dataset(info.name)],
                }
            )
        except Exception as error:
            return error_response(error)

    async def profile(request: Request) -> Response:
        try:
            info = ctx.store.get(request.path_params["dataset"])
            column_name = request.query_params.get("column")
            if info is None:
                raise LookupError("No such dataset.")
            column = next((c for c in info.columns if c.name == column_name), None)
            if column is None:
                raise LookupError(f"No column {column_name!r}.")
            sql = f"SELECT * FROM {info.table}"
            result = await ctx.workers.run(AGENT, lambda: profile_column(ctx.store.cursor(), sql, column))
            ctx.audit.write("agent", "profile", info.name, column=column.name)
            return json_response(result)
        except Exception as error:
            return error_response(error)

    async def query(request: Request) -> Response:
        try:
            body = await read_json_object(request, '{"dataset": name, "sql": "SELECT ... FROM grid"}')
            info, grid = dataset_for(body)
            text = body.get("sql")
            if not isinstance(text, str):
                raise ValueError('Send {"dataset": name, "sql": "SELECT ... FROM grid"}.')
            cap = AGENT_ROW_CAP

            def execute(cur: duckdb.DuckDBPyConnection) -> Any:
                grid_sql: Callable[..., str]
                if grid is not None:
                    compiled = ctx.compiled(cur, grid, info)
                    grid_sql = compiled.sql
                else:
                    grid_sql = lambda: f"SELECT * FROM {info.table}"  # noqa: E731
                guarded = guard(text, {i.name for i in ctx.store.all()}, grid_sql)
                if guarded.kind != "select":
                    raise ValueError("Agents may only run SELECT queries; use steps_save_result to pin.")
                timer = threading.Timer(ctx.settings.query_timeout_seconds, cur.interrupt)
                timer.start()
                try:
                    result = cur.execute(guarded.sql)
                    names = [d[0] for d in result.description or []]
                    fetched = [list(r) for r in result.fetchmany(cap + 1)]
                finally:
                    timer.cancel()
                return names, fetched

            try:
                names, rows = await ctx.workers.run(AGENT, lambda: execute(ctx.store.cursor()))
            except duckdb.InterruptException:
                return Response(
                    f"The query was stopped: it ran past {ctx.settings.query_timeout_seconds:.0f} seconds.",
                    status_code=408,
                )
            except duckdb.Error as error:
                return Response(f"DuckDB: {error}", status_code=400)
            capped = len(rows) > cap
            rows = rows[:cap]
            ctx.audit.write("agent", "query", info.name, query=text, rows=len(rows))
            await ctx.live.broadcast(
                {"type": "agent_query", "dataset": info.name, "sql": text, "rows": len(rows)}
            )
            return json_response(
                {"columns": names, "rows": rows, "capped": capped, "row_cap": cap},
                # Up to 1,048,576 rows: the same byte limit as the SQL box's answer.
                max_bytes=ctx.settings.max_sql_response_bytes,
            )
        except Exception as error:
            return error_response(error)

    async def read_grid(request: Request) -> Response:
        """A window of the grid's view as the user sees it, always with the view's row count.

        The agent picks offset and limit, capped at 1,048,576 rows."""
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            offset = max(0, int(request.query_params.get("offset", "0")))
            limit = max(0, int(request.query_params.get("limit", "100")))
            cap = AGENT_ROW_CAP
            take = min(limit, cap)

            def count(cur: duckdb.DuckDBPyConnection) -> tuple[int, list[str]]:
                n = ctx.views.row_count(cur, info, grid.steps, grid.settings, None)
                return n, [c.name for c in ctx.compiled(cur, grid, info).columns]

            try:
                row_count, names = await ctx.grid_query(grid, None, AGENT, count)
            except duckdb.Error as error:
                return Response(f"DuckDB: {error}", status_code=400)
            out: dict[str, Any] = {
                "grid": grid.id,
                "dataset": info.name,
                "row_count": row_count,
                "offset": offset,
                "row_cap": cap,
                "columns": names,
                "rows": [],
            }
            if take == 0 or offset >= row_count:
                return json_response(out)
            end = min(row_count, offset + take)

            def read(cur: duckdb.DuckDBPyConnection) -> list[list[Any]]:
                window = ctx.views.window(cur, info, grid.steps, grid.settings, offset, end)
                return [list(r) for r in window.rows]

            try:
                rows = await ctx.grid_query(grid, None, AGENT, read)
            except duckdb.Error as error:
                return Response(f"DuckDB: {error}", status_code=400)
            ctx.audit.write("agent", "read", info.name, grid=grid.id, rows=len(rows))
            return json_response(
                out | {"rows": rows, "capped": limit > cap},
                max_bytes=ctx.settings.max_sql_response_bytes,
            )
        except Exception as error:
            return error_response(error)

    # ===== agent actions on a grid =====

    async def view_state(request: Request) -> Response:
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            try:
                row_count: int | None = await ctx.grid_query(
                    grid,
                    None,
                    AGENT,
                    lambda cur: ctx.views.row_count(cur, info, grid.steps, grid.settings, None),
                )
            except duckdb.Error:
                row_count = None  # the view fails to compute; the steps show which one
            return json_response(
                {
                    "grid": grid.id,
                    "dataset": info.name,
                    "steps": [s.to_json() for s in grid.steps],
                    "settings": grid.settings.to_json(),
                    "row_count": row_count,
                    "selected_rows": grid.selected,
                    "highlights": len(grid.highlights),
                }
            )
        except Exception as error:
            return error_response(error)

    async def set_highlights(request: Request) -> Response:
        """Mark rows matching conditions (same shape as filter params, plus a label)."""
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            body = await read_json(request)
            rules = body.get("highlights") if isinstance(body, dict) else None
            if not isinstance(rules, list):
                raise ValueError('Send {"highlights": [{"column": ..., "op": ..., "label": ...}]}.')

            def check(cur: duckdb.DuckDBPyConnection) -> None:
                compiled = ctx.compiled(cur, grid, info)
                for rule in rules:
                    if not isinstance(rule, dict):
                        raise ValueError("Each highlight is an object.")
                    filter_condition(compiled.stages[-1], rule, grid.settings)

            await ctx.workers.run(AGENT, lambda: check(ctx.store.cursor()))
            grid.highlights = rules
            await ctx.live.broadcast(
                {"type": "grid_updated", "grid": grid.id, "by": actor(request), "gen": grid.gen}
            )
            ctx.audit.write(actor(request), "highlights", info.name, count=len(rules))
            return json_response({"grid": grid.id, "highlights": len(rules)})
        except Exception as error:
            return error_response(error)

    async def set_selection(request: Request) -> Response:
        try:
            grid, _ = ctx.grid_and_info(request.path_params["grid_id"])
            body = await read_json(request)
            grid.selected = int(body.get("count", 0)) if isinstance(body, dict) else 0
            return json_response({"selected": grid.selected})
        except Exception as error:
            return error_response(error)

    async def approve_export(request: Request) -> Response:
        """An agent export: always waits for the user's click (plan section 10a). The click gives a
        one-time ticket, and the export route writes an agent's file only against one, so skipping
        this step is refused by the server, not just by the command that normally calls it."""
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            fmt = request.query_params.get("format", "csv")
            if fmt not in FORMATS:  # the text goes into the user's approval prompt
                raise ValueError(f"format must be one of {', '.join(FORMATS)}.")
            detail = f"{fmt} export of {info.name} to the export folder"
            if not await ask_user(info.name, "export", detail):
                return Response("The user declined (or did not answer) this export.", status_code=403)
            return json_response({"approved": True, "ticket": tickets.issue(grid.id)})
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
        Route("/grids/{grid_id}/highlights", set_highlights, methods=["POST"]),
        Route("/grids/{grid_id}/selection", set_selection, methods=["POST"]),
    ]
