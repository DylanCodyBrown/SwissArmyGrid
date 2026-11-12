"""The agent door: what an MCP client may see and do (plan section 12).

Every request from the MCP launcher carries `X-Actor: agent`, so the audit log and the grid
page can tell agent actions from the user's. One privacy level covers every table, set only by
the user in Settings (an agent cannot raise its own level), default 3:

  0  schema, counts and profile numbers only: no text values, no rows. A query may return
     only whole-table aggregates (SELECT count(*), sum(x) FROM grid), never group keys.
  1  also small masked samples: at most 20 rows, every text column masked unless the user
     marked it "show".
  2  rows, up to 200 per read, with the column masks.
  3  everything: up to 1,048,576 rows per read (one Excel sheet), no masks, no waiting for a click.

Below level 3, queries that return rows (anything but a whole-table aggregate) wait for the
user's click unless the user turned on "run automatically". Column masks (hide or hash, by
column name, so a rule follows that column into every table) apply at levels 1 and 2. Exports by
an agent always wait for a click and write only into the export folder.

Cell values reach the agent only as data inside the MCP layer's delimited block
(swissarmygrid.mcp.server), never in a tool description or instruction text.

Limitation, stated plainly: the approval endpoint trusts the browser. Any process running as
the same Windows user can read the connection file and call it; the gate stops an agent that
only has these MCP tools, not code already running with the user's rights.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.engine.datasets import DatasetInfo
from swissarmygrid.engine.profile import profile_column, profile_table
from swissarmygrid.engine.sql_guard import guard
from swissarmygrid.engine.steps import filter_condition, type_family
from swissarmygrid.engine.workers import AGENT
from swissarmygrid.server.context import Context, actor, error_response, json_response, read_json
from swissarmygrid.server.grids import Grid
from swissarmygrid.settings import EXCEL_MAX_ROWS

# Level 3 reads as much as one Excel worksheet holds; the agent still picks its own limit.
LEVEL_ROW_CAPS: dict[int, int] = {0: 0, 1: 20, 2: 200, 3: EXCEL_MAX_ROWS}
DEFAULT_LEVEL: int = 3
APPROVAL_TIMEOUT_SECONDS: float = 180.0


@dataclass
class Policy:
    level: int = DEFAULT_LEVEL
    auto_rows: bool = False
    masks: dict[str, str] = field(default_factory=dict)  # column -> hide | hash | show

    def to_json(self) -> dict[str, Any]:
        return {"level": self.level, "auto_rows": self.auto_rows, "masks": self.masks}


def agent_may_see_raw_values(request: Request, policy: Policy) -> bool:
    """False for an agent below level 3, on the page's own value routes (rows, cell, SQL box...).

    Those routes return cells with no masking and no approval click, which is right for the user
    and wrong for an agent: the agent's door for values is `/agent/read` and `/agent/query`, which
    mask, cap and ask. Think of it as an `[Authorize(Policy = "RawValues")]` attribute: the user
    always passes, the agent passes only at level 3, where it sees everything anyway."""
    return actor(request) != "agent" or policy.level == 3


def raw_values_refused() -> Response:
    return Response(
        "This returns cell values. At this privacy level use agent_read_grid or agent_query, which mask the "
        "values, cap the rows and ask the user; or ask the user to raise the level in Settings.",
        status_code=403,
    )


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


def is_whole_table_aggregate(sql: str) -> bool:
    """True for a single SELECT with no GROUP BY whose every output is an aggregate."""
    try:
        root = sqlglot.parse_one(sql, read="duckdb")
    except sqlglot.errors.ParseError:
        return False
    if not isinstance(root, exp.Select) or root.args.get("group"):
        return False
    for projection in root.expressions:
        target = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(target, exp.AggFunc) and not target.find(exp.AggFunc):
            return False
        # A bare column outside the aggregate (count(*) + id) would leak a value.
        for column in target.find_all(exp.Column):
            if not column.find_ancestor(exp.AggFunc):
                return False
    return True


def mask_value(value: Any, how: str) -> Any:
    if value is None:
        return None
    if how == "hash":
        return hashlib.sha256(str(value).encode()).hexdigest()[:10]
    text = str(value)
    return (text[:1] + "•" * min(len(text) - 1, 6)) if text else ""


def hidden_error(error: duckdb.Error, level: int) -> Response:
    """A DuckDB error for the agent. Its text can quote a cell value ("Could not convert string
    'Acme Corp' to INT32"), so below level 2 the agent gets only that something failed."""
    if level >= 2:
        return Response(f"DuckDB: {error}", status_code=400)
    return Response(
        f"DuckDB could not compute this. The message is hidden at privacy level {level} because "
        "it can quote cell values; check the query or the grid's steps.",
        status_code=400,
    )


def apply_masks(
    rules: Policy, names: list[str], types: list[str], rows: list[list[Any]]
) -> tuple[list[str], list[list[Any]], list[str]]:
    """(shown column names, masked rows, the masked column names) under the policy: "hide"
    drops a column, "hash" hashes it, and level 1 masks every text column not marked "show".
    Level 3 sees everything, so no mask applies there."""
    if rules.level >= 3:
        return names, rows, []
    keep: list[int] = []
    how: list[str] = []
    for i, (name, type_name) in enumerate(zip(names, types, strict=True)):
        rule = rules.masks.get(name)
        if rule == "hide":
            continue
        keep.append(i)
        if rule == "hash":
            how.append("hash")
        elif rules.level == 1 and rule != "show" and "VARCHAR" in type_name.upper():
            how.append("mask")
        else:
            how.append("show")
    shown = [
        [row[i] if h == "show" else mask_value(row[i], h) for i, h in zip(keep, how, strict=True)]
        for row in rows
    ]
    masked = [names[i] for i, h in zip(keep, how, strict=True) if h != "show"]
    return [names[i] for i in keep], shown, masked


def routes(ctx: Context, rules: Policy) -> list[BaseRoute]:
    """`rules`: the one policy for every table; the level the user picks in Settings applies
    to all of them. app.py makes it and shares it with routes_context."""
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

    # ===== user-side controls (refused for the agent) =====

    async def get_policy(request: Request) -> Response:
        return json_response(rules.to_json())

    async def set_policy(request: Request) -> Response:
        if actor(request) == "agent":
            return Response("Only the user can change what an agent may see.", status_code=403)
        try:
            body = await read_json(request)
            level = body.get("level", rules.level)
            if level not in LEVEL_ROW_CAPS:
                raise ValueError("level must be 0, 1, 2 or 3.")
            masks = body.get("masks", rules.masks)
            if not isinstance(masks, dict) or not all(v in ("hide", "hash", "show") for v in masks.values()):
                raise ValueError("masks maps column names to hide, hash or show.")
            rules.level = level
            rules.auto_rows = bool(body.get("auto_rows", rules.auto_rows))
            rules.masks = dict(masks)
            ctx.audit.write("user", "agent_policy", None, level=level)
            return json_response(rules.to_json())
        except Exception as error:
            return error_response(error)

    async def list_approvals(request: Request) -> Response:
        return json_response({"pending": [a.to_json() for a in approvals.values()]})

    async def resolve_approval(request: Request) -> Response:
        if actor(request) == "agent":
            return Response("Only the user can approve.", status_code=403)
        approval = approvals.get(request.path_params["approval_id"])
        if approval is None:
            return Response("No such pending request (it may have timed out).", status_code=404)
        body = await read_json(request)
        approved = bool(isinstance(body, dict) and body.get("approve"))
        if not approval.future.done():
            approval.future.set_result(approved)
        ctx.audit.write("user", "approval", approval.dataset, approved=approved, request=approval.action)
        return json_response({"id": approval.id, "approved": approved})

    # ===== agent reads =====

    async def describe(request: Request) -> Response:
        """Schema, counts and the profile, redacted to the dataset's level."""
        try:
            info = ctx.store.get(request.path_params["dataset"])
            if info is None:
                raise LookupError("No such dataset.")
            level = rules.level

            def compute(cur: duckdb.DuckDBPyConnection) -> Any:
                names = ", ".join(f'"{c.name}"' for c in info.columns)
                sql = f"SELECT {names} FROM {info.table}"
                return profile_table(cur, sql, info.columns)

            profiled = await ctx.workers.run(AGENT, lambda: compute(ctx.store.cursor()))
            for row in profiled["columns"]:
                if level == 0 and type_family(row["type"]) not in ("number",):
                    row.pop("min", None)
                    row.pop("max", None)
            ctx.audit.write("agent", "describe", info.name)
            return json_response(
                {
                    "dataset": info.name,
                    "version": info.version,
                    "rows": info.row_count,
                    "privacy_level": level,
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
            level = rules.level
            sql = f"SELECT * FROM {info.table}"
            result = await ctx.workers.run(AGENT, lambda: profile_column(ctx.store.cursor(), sql, column))
            if level == 0 and result["family"] != "number":
                for key in ("top_values", "case_groups", "by_month", "earliest", "latest"):
                    result.pop(key, None)
            elif level == 1 and rules.masks.get(column.name) != "show":
                result["top_values"] = [
                    {"value": mask_value(v["value"], "mask"), "count": v["count"]}
                    for v in result.get("top_values", [])
                ]
                result.pop("case_groups", None)
            ctx.audit.write("agent", "profile", info.name, column=column.name)
            return json_response(result | {"privacy_level": level})
        except Exception as error:
            return error_response(error)

    async def query(request: Request) -> Response:
        try:
            body = await read_json(request)
            info, grid = dataset_for(body)
            text = body.get("sql")
            if not isinstance(text, str):
                raise ValueError('Send {"dataset": name, "sql": "SELECT ... FROM grid"}.')
            aggregate = is_whole_table_aggregate(text)
            if rules.level == 0 and not aggregate:
                return Response(
                    f"Dataset {info.name!r} is at privacy level 0: only whole-table aggregates "
                    "(count, sum, avg, min, max with no GROUP BY) are allowed. The user can raise "
                    "the level in the grid's Settings.",
                    status_code=403,
                )
            if not aggregate and not rules.auto_rows and rules.level < 3:
                if not await ask_user(info.name, "query", text):
                    ctx.audit.write("agent", "query_declined", info.name, query=text)
                    return Response("The user declined (or did not answer) this query.", status_code=403)
            cap = 1 if aggregate and rules.level == 0 else LEVEL_ROW_CAPS[rules.level]

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
                result = cur.execute(guarded.sql)
                names = [d[0] for d in result.description or []]
                types = [str(d[1]) for d in result.description or []]
                return names, types, [list(r) for r in result.fetchmany(cap + 1)]

            try:
                names, types, rows = await ctx.workers.run(AGENT, lambda: execute(ctx.store.cursor()))
            except duckdb.Error as error:
                return hidden_error(error, rules.level)
            capped = len(rows) > cap
            shown_names, shown_rows, masked = apply_masks(rules, names, types, rows[:cap])
            ctx.audit.write("agent", "query", info.name, query=text, rows=len(shown_rows))
            await ctx.live.broadcast(
                {"type": "agent_query", "dataset": info.name, "sql": text, "rows": len(shown_rows)}
            )
            return json_response(
                {
                    "columns": shown_names,
                    "rows": shown_rows,
                    "capped": capped,
                    "row_cap": cap,
                    "masked": masked,
                    "privacy_level": rules.level,
                }
            )
        except Exception as error:
            return error_response(error)

    async def read_grid(request: Request) -> Response:
        """A window of the grid's view as the user sees it, always with the view's row count.

        The agent picks offset and limit; the privacy level caps the limit (0 rows at level 0,
        20 masked at 1, 200 at 2, 1,048,576 at 3) and below level 3 rows wait for the user's click
        like any row query."""
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            offset = max(0, int(request.query_params.get("offset", "0")))
            limit = max(0, int(request.query_params.get("limit", "100")))
            cap = LEVEL_ROW_CAPS[rules.level]
            take = min(limit, cap)

            def count(cur: duckdb.DuckDBPyConnection) -> tuple[int, list[str], list[str]]:
                n = ctx.views.row_count(cur, info, grid.steps, grid.settings, None)
                columns = ctx.compiled(cur, grid, info).columns
                return n, [c.name for c in columns], [c.type for c in columns]

            try:
                row_count, names, types = await ctx.grid_query(grid, None, AGENT, count)
            except duckdb.Error as error:
                return hidden_error(error, rules.level)
            out: dict[str, Any] = {
                "grid": grid.id,
                "dataset": info.name,
                "row_count": row_count,
                "offset": offset,
                "privacy_level": rules.level,
                "row_cap": cap,
                "columns": names,
                "rows": [],
            }
            if rules.level == 0 or take == 0 or offset >= row_count:
                if rules.level == 0:
                    out["note"] = (
                        f"Dataset {info.name!r} is at privacy level 0: the row count and column "
                        "names only. The user can raise the level in the grid's Settings."
                    )
                return json_response(out)
            end = min(row_count, offset + take)
            detail = f"Read rows {offset + 1:,}-{end:,} of {row_count:,} in the grid view"
            if not rules.auto_rows and rules.level < 3 and not await ask_user(info.name, "read", detail):
                ctx.audit.write("agent", "read_declined", info.name, grid=grid.id)
                return Response("The user declined (or did not answer) this read.", status_code=403)

            def read(cur: duckdb.DuckDBPyConnection) -> list[list[Any]]:
                window = ctx.views.window(cur, info, grid.steps, grid.settings, offset, end)
                return [list(r) for r in window.rows]

            try:
                rows = await ctx.grid_query(grid, None, AGENT, read)
            except duckdb.Error as error:
                return hidden_error(error, rules.level)
            shown_names, shown_rows, masked = apply_masks(rules, names, types, rows)
            ctx.audit.write("agent", "read", info.name, grid=grid.id, rows=len(shown_rows))
            return json_response(
                out
                | {
                    "columns": shown_names,
                    "rows": shown_rows,
                    "masked": masked,
                    "capped": limit > cap,
                }
            )
        except Exception as error:
            return error_response(error)

    # ===== agent actions on a grid =====

    async def view_state(request: Request) -> Response:
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            level = rules.level
            try:
                row_count: int | None = await ctx.grid_query(
                    grid,
                    None,
                    AGENT,
                    lambda cur: ctx.views.row_count(cur, info, grid.steps, grid.settings, None),
                )
            except duckdb.Error:
                row_count = None  # the view fails to compute; the steps show which one
            steps = [s.to_json() for s in grid.steps]
            if level == 0:
                # Step values are text the user typed, which can be client data: names only.
                steps = [{"kind": s["kind"], "column": s["params"].get("column")} for s in steps]
            return json_response(
                {
                    "grid": grid.id,
                    "dataset": info.name,
                    "steps": steps,
                    "settings": grid.settings.to_json(),
                    "row_count": row_count,
                    "selected_rows": grid.selected,
                    "highlights": len(grid.highlights),
                    "privacy_level": level,
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
        """An agent export: always waits for the user's click (plan section 10a)."""
        try:
            _grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            fmt = request.query_params.get("format", "csv")
            if not await ask_user(info.name, "export", f"{fmt} export of {info.name} to the export folder"):
                return Response("The user declined (or did not answer) this export.", status_code=403)
            return json_response({"approved": True})
        except Exception as error:
            return error_response(error)

    return [
        Route("/agent/policy", get_policy),
        Route("/agent/policy", set_policy, methods=["POST"]),
        # The old per-dataset address still works; it reads and sets the one policy.
        Route("/agent/policy/{dataset}", get_policy),
        Route("/agent/policy/{dataset}", set_policy, methods=["POST"]),
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
