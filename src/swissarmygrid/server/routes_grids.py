"""Grid routes: everything a grid page (or an agent, through MCP) does with one grid.

  GET    /grids                          every open grid
  POST   /grids                          {"dataset": name} open another grid on a dataset
  GET    /grids/{id}                     description: dataset, columns, steps, settings, errors
  DELETE /grids/{id}                     close the grid (the dataset stays)
  GET    /grids/{id}/rows?start=&end=&gen=&upto=    one window, in view order
  GET    /grids/{id}/count?gen=&upto=    rows in the view
  GET    /grids/{id}/cell?row=&column=&upto=        one full, untruncated value
  GET    /grids/{id}/step-counts         rows after each step ("12,000 -> 11,420")
  GET    /grids/{id}/profile?column=&upto=          column profile, or the whole-table profile
  GET    /grids/{id}/header-stats        distinct count and blank percent per column
  GET    /grids/{id}/categorize-stats?step=         unmatched and overlapping rows of a rule set
  POST   /grids/{id}/steps               {"steps": [...], "settings": {...}, "gen": n}
  POST   /grids/{id}/step-ops            {"op": add|edit|delete|toggle|move, "index"|"id", ...} one step
  POST   /grids/{id}/header-sort         {"keys": [["col", "asc"]], "gen": n}
  POST   /grids/{id}/sql                 {"sql": "..."} guarded; CREATE TABLE AS pins a new grid
  GET    /grids/{id}/export?format=&scope=&...      a download (or ?save=1 into the export folder)
  GET    /grids/{id}/code?target=duckdb|pandas|plain   generated, verified Python
  POST   /grids/{id}/recipe              save this grid's steps for its dataset name
  DELETE /grids/{id}/recipe              forget the saved recipe

Every query runs on the worker pool: grid windows at interactive priority, profiles, SQL and
exports behind them. Requests carrying a `gen` older than the grid's latest are dropped with
409 (server/grids.py), and the page ignores stale answers by gen too.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import duckdb
import pyarrow as pa
import sqlglot
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import BaseRoute, Route

from swissarmygrid.codegen.generate import generate_code
from swissarmygrid.engine import export as exporting
from swissarmygrid.engine.datasets import Column, DatasetInfo, quote
from swissarmygrid.engine.describe import describe_settings, describe_step, describe_steps
from swissarmygrid.engine.profile import header_stats, profile_column, profile_table
from swissarmygrid.engine.recipes import delete_recipe, save_recipe
from swissarmygrid.engine.sql_guard import guard
from swissarmygrid.engine.steps import (
    Step,
    StepError,
    ViewSettings,
    categorize_conditions,
    filter_condition,
    steps_from_json,
    with_header_sort,
)
from swissarmygrid.engine.views import POS, view_key
from swissarmygrid.engine.workers import AGENT, BACKGROUND, INTERACTIVE
from swissarmygrid.server.context import (
    Context,
    actor,
    client_of,
    error_response,
    json_response,
    query_int,
    read_json,
)
from swissarmygrid.server.grids import Grid
from swissarmygrid.server.level_zero import query_classes, step_refusal, value_use_refusal
from swissarmygrid.server.masking import Origins, column_rules
from swissarmygrid.server.routes_agent import (
    LEVEL_ROW_CAPS,
    Policy,
    agent_may_see_raw_values,
    export_refused,
    mask_table,
    pivot_refused,
    raw_values_refused,
    redact_profile_row,
)
from swissarmygrid.server.routes_datasets import budget, store_table
from swissarmygrid.server.security import ENGINE_ERROR_HIDDEN
from swissarmygrid.server.selection import combine_stats, selection_scope, stats_sql
from swissarmygrid.server.step_privacy import HIDDEN, mentions_hidden, redact_step, restore_hidden

_SAFE_FILE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def describe_grid(
    ctx: Context,
    grid: Grid,
    info: DatasetInfo,
    cursor: duckdb.DuckDBPyConnection,
    agent_level: int = 3,
    engine_errors_shown: bool = True,
) -> dict[str, Any]:
    """The grid in full. `agent_level`: the privacy level of an agent asking (3 for the user). At
    level 0 the steps and the "+" tab's query are shown without their values (step_privacy.py),
    and unless `engine_errors_shown` a step error in DuckDB's own words (which can quote a cell) is
    hidden."""
    compiled = ctx.compiled(cursor, grid, info)
    error = compiled.error
    step_error: dict[str, Any] | None = None
    if error is not None:
        message = error.message
        if agent_level == 0:
            message = (
                "This step cannot apply. Its message is hidden at privacy level 0, since it can quote values."
            )
        elif not engine_errors_shown and error.from_engine:
            message = ENGINE_ERROR_HIDDEN
        step_error = {"index": error.index, "message": message}
    if agent_level == 0:
        steps = [redact_step(s) for s in grid.steps]
    else:
        steps = [s.to_json() | {"description": describe_step(s)} for s in grid.steps]
    return {
        "grid": grid.id,
        "dataset": info.name,
        "title": info.name,
        "version": info.version,
        "row_count": info.row_count,
        "source_kind": info.source_kind,
        "query": HIDDEN if agent_level == 0 and info.query is not None else info.query,
        "source_columns": [{"name": c.name, "type": c.type} for c in info.columns],
        "columns": [{"name": c.name, "type": c.type} for c in compiled.columns],
        # Columns after each step (index 0 = the source), for previewing a step.
        "stage_columns": [[{"name": c.name, "type": c.type} for c in s.columns] for s in compiled.stages],
        "steps": steps,
        "step_error": step_error,
        "settings": grid.settings.to_json(),
        "gen": grid.gen,
        "budget": budget(ctx),
    }


def _truncate(rows: list[list[Any]], limit: int) -> list[list[int]]:
    """Cut long text in place; return the [row, column] positions that were cut."""
    cut: list[list[int]] = []
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            if isinstance(value, str) and len(value) > limit:
                row[c] = value[:limit]
                cut.append([r, c])
    return cut


def routes(ctx: Context, policy: Policy) -> list[BaseRoute]:
    """`policy`: the agent privacy policy (routes_agent), checked where an agent reads step values."""
    settings = ctx.settings
    profile_cache: OrderedDict[str, Any] = OrderedDict()
    profile_lock = threading.Lock()

    def cached(key: str, compute: Callable[[], Any]) -> Any:
        with profile_lock:
            if key in profile_cache:
                profile_cache.move_to_end(key)
                return profile_cache[key]
        value = compute()
        with profile_lock:
            profile_cache[key] = value
            while len(profile_cache) > 512:
                profile_cache.popitem(last=False)
        return value

    def priority(request: Request) -> int:
        return AGENT if actor(request) == "agent" else BACKGROUND

    def agent_level(request: Request) -> int:
        """The privacy level the caller reads at: the policy's for an agent, 3 for the user."""
        return policy.level if actor(request) == "agent" else 3

    def errors_shown(request: Request) -> bool:
        """Whether the caller may read DuckDB's own error text (it can quote a cell)."""
        return actor(request) != "agent" or policy.engine_errors_shown()

    # ===== grid lifecycle =====

    async def list_grids(request: Request) -> Response:
        return json_response(
            {
                "grids": [
                    {
                        "grid": g.id,
                        "dataset": g.dataset,
                        "steps": len(g.steps),
                        "url": ctx.grid_url(g, with_token=actor(request) != "agent"),
                    }
                    for g in ctx.grids.all()
                ],
                "budget": budget(ctx),
            }
        )

    async def create_grid(request: Request) -> Response:
        try:
            body = await read_json(request)
            name = body.get("dataset") if isinstance(body, dict) else None
            if not isinstance(name, str) or ctx.store.get(name) is None:
                return Response("Name an open dataset.", status_code=404)
            grid = ctx.grids.create(name)
            ctx.audit.write(actor(request), "open_grid", name, grid=grid.id)
            await ctx.live.broadcast({"type": "grid_shown", "grid": grid.id, "dataset": name})
            # Nothing shows it yet: VS Code opens the workspace, as for a push.
            shown = ctx.live.shown_or_opening(grid.id) or await ctx.live.ask_opener(ctx.workspace_url(grid))
            return json_response(
                {
                    "grid": grid.id,
                    "grid_url": ctx.grid_url(grid, with_token=actor(request) != "agent"),
                    "workspace_url": ctx.workspace_url(grid, with_token=actor(request) != "agent"),
                    "shown": shown,
                }
            )
        except Exception as error:
            return error_response(error)

    async def get_grid(request: Request) -> Response:
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            level = agent_level(request)
            return json_response(
                await ctx.grid_query(
                    grid,
                    None,
                    INTERACTIVE,
                    lambda cur: describe_grid(ctx, grid, info, cur, level, errors_shown(request)),
                )
            )
        except Exception as error:
            return error_response(error)

    async def close_grid(request: Request) -> Response:
        grid = ctx.grids.close(request.path_params["grid_id"])
        if grid is None:
            return Response("No such grid.", status_code=404)
        await ctx.live.broadcast({"type": "grid_closed", "grid": grid.id})
        return json_response({"closed": grid.id})

    # ===== reading rows =====

    async def rows(request: Request) -> Response:
        if not agent_may_see_raw_values(request, policy):
            return raw_values_refused()
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            start = query_int(request, "start", 0) or 0
            end = query_int(request, "end", start + 100) or 0
            gen = query_int(request, "gen")
            upto = query_int(request, "upto")
            if start < 0 or end < start:
                raise ValueError("Need 0 <= start <= end.")
            end = min(end, start + settings.max_window_rows)

            def read(cur: duckdb.DuckDBPyConnection) -> tuple[Any, list[list[Any]]]:
                window = ctx.views.window(cur, info, grid.steps, grid.settings, start, end, upto)
                marks: list[list[Any]] = []
                if grid.highlights and upto is None:
                    compiled = ctx.compiled(cur, grid, info)
                    positioned, _ = ctx.views.positioned_sql(cur, info, grid.steps, grid.settings)
                    for rule in grid.highlights:
                        condition = filter_condition(compiled.stages[-1], rule, grid.settings)
                        hits = cur.execute(
                            f"SELECT {POS} FROM ({positioned}) "
                            f"WHERE {POS} >= ? AND {POS} < ? AND ({condition})",
                            [start, end],
                        ).fetchall()
                        label = str(rule.get("label", "highlight"))
                        marks += [[int(h[0]) - start, label] for h in hits]
                return window, marks

            window, marks = await ctx.grid_query(grid, gen, INTERACTIVE, read, client_of(request))
            cut = _truncate(window.rows, settings.max_cell_chars)
            return json_response(
                {
                    "rows": window.rows,
                    "row_count": window.row_count,
                    "columns": [c.name for c in window.columns],
                    "truncated": cut,
                    "highlighted": marks,
                    "version": info.version,
                    "gen": gen,
                },
                max_bytes=settings.max_response_bytes,
            )
        except Exception as error:
            return error_response(error)

    async def count(request: Request) -> Response:
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            upto = query_int(request, "upto")
            n = await ctx.grid_query(
                grid,
                query_int(request, "gen"),
                INTERACTIVE,
                lambda cur: ctx.views.row_count(cur, info, grid.steps, grid.settings, upto),
                client_of(request),
            )
            return json_response({"row_count": n, "total": info.row_count})
        except Exception as error:
            return error_response(error)

    async def cell(request: Request) -> Response:
        if not agent_may_see_raw_values(request, policy):
            return raw_values_refused()
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            row = query_int(request, "row")
            column = request.query_params.get("column")
            if row is None or row < 0 or not column:
                raise ValueError("Give row and column.")
            value = await ctx.grid_query(
                grid,
                None,
                INTERACTIVE,
                lambda cur: ctx.views.cell(cur, info, grid.steps, grid.settings, row, column),
            )
            return json_response({"row": row, "column": column, "value": value})
        except (KeyError, IndexError):
            return Response("No such cell.", status_code=404)
        except Exception as error:
            return error_response(error)

    async def step_counts(request: Request) -> Response:
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            counts = await ctx.grid_query(
                grid,
                query_int(request, "gen"),
                BACKGROUND,
                lambda cur: ctx.views.step_counts(cur, info, grid.steps, grid.settings),
                client_of(request),
            )
            return json_response({"counts": counts})
        except Exception as error:
            return error_response(error)

    # ===== profiling (plan section 8) =====

    async def profile(request: Request) -> Response:
        if not agent_may_see_raw_values(request, policy):
            return raw_values_refused()
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            column_name = request.query_params.get("column")
            upto = query_int(request, "upto")
            key = view_key(info, grid.steps, grid.settings, upto) + f":{column_name}"

            def compute(cur: duckdb.DuckDBPyConnection) -> Any:
                compiled = ctx.compiled(cur, grid, info)
                columns = compiled.columns_at(upto)
                match = next((c for c in columns if c.name == column_name), None)
                if column_name is not None and match is None:
                    raise LookupError(f"No column {column_name!r} in this view.")

                def run() -> Any:
                    # The view cache, not every step again (engine/views.reading).
                    with ctx.views.reading(cur, info, grid.steps, grid.settings, upto) as rows:
                        if match is None:
                            return profile_table(cur, rows, columns)
                        return profile_column(cur, rows, match)

                return cached(key, run)

            result = await ctx.grid_query(grid, None, priority(request), compute)
            ctx.audit.write(actor(request), "profile", info.name, column=column_name)
            return json_response(result)
        except Exception as error:
            return error_response(error)

    async def stats(request: Request) -> Response:
        if not agent_may_see_raw_values(request, policy):
            return raw_values_refused()
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            key = view_key(info, grid.steps, grid.settings, None) + ":header"

            def compute(cur: duckdb.DuckDBPyConnection) -> Any:
                compiled = ctx.compiled(cur, grid, info)

                def run() -> Any:
                    with ctx.views.reading(cur, info, grid.steps, grid.settings) as rows:
                        return header_stats(cur, rows, compiled.columns)

                return cached(key, run)

            return json_response(
                await ctx.grid_query(grid, query_int(request, "gen"), BACKGROUND, compute, client_of(request))
            )
        except Exception as error:
            return error_response(error)

    async def categorize_stats(request: Request) -> Response:
        """Unmatched and multiply-matched rows for a categorize step, measured on its input."""
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            index = query_int(request, "step")
            if index is None or not 0 <= index < len(grid.steps):
                raise ValueError("step must be the index of a categorize step.")
            step = grid.steps[index]
            if step.kind != "categorize":
                raise ValueError("That step is not a categorize step.")

            def compute(cur: duckdb.DuckDBPyConnection) -> Any:
                compiled = ctx.compiled(cur, grid, info)
                stage = compiled.stage_at(index)
                column, conditions = categorize_conditions(stage, step.params, grid.settings)
                hits = " + ".join(f"CAST({c} AS INTEGER)" for c, _ in conditions)
                with_clause = ", ".join(f"{n} AS ({b})" for n, b in compiled.ctes)
                row = cur.execute(
                    f"WITH {with_clause} SELECT "
                    f"count(*) FILTER ({quote(column.name)} IS NOT NULL AND ({hits}) = 0), "
                    f"count(*) FILTER (({hits}) > 1) FROM {stage.name}"
                ).fetchone()
                return {"unmatched": row[0] if row else 0, "overlapping": row[1] if row else 0}

            return json_response(await ctx.grid_query(grid, None, BACKGROUND, compute))
        except Exception as error:
            return error_response(error)

    # ===== changing the view =====

    async def commit_steps(
        request: Request,
        grid: Grid,
        info: DatasetInfo,
        new_steps: list[Step],
        new_settings: ViewSettings,
        gen: Any = None,
        client: str = "",
        extra: dict[str, Any] | None = None,
    ) -> Response:
        """Make `new_steps` the grid's steps: the one path for the whole list (POST .../steps) and
        for one change (POST .../step-ops). Returns the grid described for the caller."""
        who = actor(request)
        level = agent_level(request)
        if who == "agent":
            # Values hidden from the agent at level 0 come back as they were (step_privacy.py).
            new_steps = restore_hidden(new_steps, grid.steps)
            if any(mentions_hidden(s.params) for s in new_steps):
                raise ValueError(
                    "A step still holds the hidden-value marker. Change one step with steps_edit and its "
                    "full params, or post the step exactly as tabs_get showed it to keep it unchanged."
                )
        sql_steps = [s.params.get("sql") for s in new_steps if s.kind == "sql"]
        if (refused := pivot_refused(request, policy, *sql_steps)) is not None:
            return refused
        before = {s.id: (s.kind, s.params) for s in grid.steps}
        changed = [i for i, s in enumerate(new_steps) if before.get(s.id) != (s.kind, s.params)]
        if who == "agent" and level == 0 and changed:
            why = await ctx.grid_query(
                grid,
                None,
                AGENT,
                lambda cur: step_refusal(ctx, cur, info, new_steps, new_settings, changed),
            )
            if why is not None:
                return Response(why, status_code=403)
        for i in changed:  # who wrote each step last: level_zero.probe_reason reads it
            if who == "agent":
                grid.agent_steps.add(new_steps[i].id)
            else:
                grid.agent_steps.discard(new_steps[i].id)
        if isinstance(gen, int):  # newer gen from this page: interrupt its older queries
            grid.release(grid.admit(gen, ctx.store.cursor(), client))
        grid.steps, grid.settings = new_steps, new_settings
        described = await ctx.grid_query(
            grid,
            None,
            INTERACTIVE,
            lambda cur: describe_grid(ctx, grid, info, cur, level, errors_shown(request)),
        )
        await ctx.live.broadcast({"type": "grid_updated", "grid": grid.id, "by": who, "gen": grid.gen})
        ctx.audit.write(who, "steps", info.name, steps=len(new_steps))
        return json_response(described | (extra or {}))

    async def put_steps(request: Request) -> Response:
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            body = await read_json(request)
            if not isinstance(body, dict):
                raise ValueError('Send {"steps": [...], "settings": {...}}.')
            new_steps = steps_from_json(body["steps"]) if "steps" in body else list(grid.steps)
            new_settings = ViewSettings.from_json(body["settings"]) if "settings" in body else grid.settings
            client = str(body.get("client", ""))[:40]
            return await commit_steps(request, grid, info, new_steps, new_settings, body.get("gen"), client)
        except StepError as error:
            return Response(str(error), status_code=400)
        except Exception as error:
            return error_response(error)

    async def step_op(request: Request) -> Response:
        """One change to one step, by its id or index, so an agent never needs the step values back:
        {"op": "add", "step": {...}, "index"?} | {"op": "edit", "params": {...}} |
        {"op": "delete"} | {"op": "toggle", "enabled"?} | {"op": "move", "to": n},
        each but add naming the step with "id" or "index"."""
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            body = await read_json(request)
            if not isinstance(body, dict):
                raise ValueError('Send {"op": "add" | "edit" | "delete" | "toggle" | "move", ...}.')
            steps = list(grid.steps)

            def whole(name: str) -> int | None:
                value = body.get(name)
                if value is None:
                    return None
                if type(value) is not int:
                    raise ValueError(f"{name} must be a whole number.")
                return value

            def position() -> int:
                step_id = body.get("id")
                if step_id is not None:
                    found = next((i for i, s in enumerate(steps) if s.id == step_id), None)
                    if found is None:
                        raise LookupError(
                            f"No step with id {str(step_id)[:40]!r} in this grid; tabs_get lists them."
                        )
                    return found
                index = whole("index")
                if index is None:
                    raise ValueError("Name the step with its index (from 0) or its id.")
                if not 0 <= index < len(steps):
                    raise ValueError(
                        f"index {index} is out of range: the grid has {len(steps)} step(s), numbered from 0."
                    )
                return index

            op = body.get("op")
            if op == "add":
                raw = body.get("step")
                if not isinstance(raw, dict):
                    raise ValueError('add needs "step": {"kind": ..., "params": {...}}.')
                new = Step.from_json({k: v for k, v in raw.items() if k != "id"}, len(steps))
                index = whole("index")
                if index is None:  # before a header-click sort, which stays the last word
                    at = next((i for i, s in enumerate(steps) if s.params.get("header")), len(steps))
                else:
                    at = max(0, min(index, len(steps)))
                steps.insert(at, new)
            elif op == "edit":
                at = position()
                params = body.get("params")
                if not isinstance(params, dict):
                    raise ValueError('edit needs "params": the step\'s full new params, an object.')
                steps[at] = replace(steps[at], params=params)
            elif op == "delete":
                at = position()
                del steps[at]
            elif op == "toggle":
                at = position()
                enabled = body.get("enabled")
                if enabled is not None and not isinstance(enabled, bool):
                    raise ValueError("enabled must be true or false.")
                steps[at] = replace(
                    steps[at], enabled=(not steps[at].enabled) if enabled is None else enabled
                )
            elif op == "move":
                start = position()
                to = whole("to")
                if to is None:
                    raise ValueError('move needs "to": the new index.')
                moved = steps.pop(start)
                at = max(0, min(to, len(steps)))
                steps.insert(at, moved)
            else:
                raise ValueError("op must be add, edit, delete, toggle or move.")
            return await commit_steps(request, grid, info, steps, grid.settings, extra={"index": at})
        except StepError as error:
            return Response(str(error), status_code=400)
        except Exception as error:
            return error_response(error)

    async def header_sort(request: Request) -> Response:
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            body = await read_json(request)
            keys = body.get("keys") if isinstance(body, dict) else None
            if not isinstance(keys, list):
                raise ValueError('Send {"keys": [["column", "asc"|"desc"], ...]}.')
            gen = body.get("gen")
            client = str(body.get("client", ""))[:40]
            if isinstance(gen, int):
                grid.release(grid.admit(gen, ctx.store.cursor(), client))
            grid.steps = with_header_sort(grid.steps, keys)
            level = agent_level(request)
            described = await ctx.grid_query(
                grid,
                None,
                INTERACTIVE,
                lambda cur: describe_grid(ctx, grid, info, cur, level, errors_shown(request)),
            )
            await ctx.live.broadcast(
                {"type": "grid_updated", "grid": grid.id, "by": actor(request), "gen": grid.gen}
            )
            return json_response(described)
        except Exception as error:
            return error_response(error)

    # ===== SQL box (plan sections 6 and 10) =====

    async def run_sql(request: Request) -> Response:
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            body = await read_json(request)
            text = body.get("sql") if isinstance(body, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise ValueError('Send {"sql": "SELECT ... FROM grid"}.')
            if (refused := pivot_refused(request, policy, text)) is not None:
                return refused
            who = actor(request)
            row_cap = settings.max_sql_rows
            if not agent_may_see_raw_values(request, policy):
                # A SELECT here returns cells, so it is refused below level 3; refused before it runs,
                # so whether it would have failed (an error() in a CASE) can't be read either.
                if _creates_table(text) is False:
                    return raw_values_refused()
                if policy.level == 0:
                    upto_step = body.get("upto") if isinstance(body.get("upto"), int) else None
                    tables = await ctx.grid_query(
                        grid, None, AGENT, lambda cur: query_classes(ctx, cur, grid, info, upto_step)
                    )
                    if (why := value_use_refusal(text, tables, returns_rows=False)) is not None:
                        return Response(why, status_code=403)

            def execute(cur: duckdb.DuckDBPyConnection) -> Any:
                # `upto`: while editing step N, `grid` is the table entering that step. A dataset
                # name, this grid's own included, reads its rows as loaded, as the variable of
                # that name does in the user's Python.
                upto = body.get("upto") if isinstance(body.get("upto"), int) else None
                # `grid` reads the view cache in view order: the rows of compiled.sql(upto), without
                # running every step again (engine/views.reading).
                names_known = {i.name for i in ctx.store.all()}
                with ctx.views.reading(cur, info, grid.steps, grid.settings, upto, ordered=True) as view:
                    guarded = guard(text, names_known, lambda: view)
                    timer = threading.Timer(settings.query_timeout_seconds, cur.interrupt)
                    started = time.perf_counter()
                    timer.start()
                    try:
                        if guarded.kind == "create":
                            made = cur.sql(guarded.sql).to_arrow_table()
                            # Masks follow each new column back to its source columns. The query
                            # ran over the view cache, which hides them, so trace the same query
                            # written over the steps' own SQL instead.
                            compiled = ctx.compiled(cur, grid, info)
                            traced = guard(text, names_known, lambda: compiled.sql(upto)).sql
                            return guarded, made, ctx.origins_of_result(traced, made.column_names), 0.0
                        result = cur.execute(guarded.sql)
                        names = [d[0] for d in result.description or []]
                        fetched = result.fetchmany(row_cap + 1)
                    finally:
                        timer.cancel()
                elapsed = (time.perf_counter() - started) * 1000
                return guarded, names, [list(r) for r in fetched], elapsed

            try:
                guarded, names, fetched, elapsed = await ctx.grid_query(
                    grid, None, priority(request), execute
                )
            except duckdb.InterruptException:
                return Response(
                    f"The query was stopped (cancelled, or it ran longer than "
                    f"{settings.query_timeout_seconds:.0f} seconds).",
                    status_code=408,
                )
            except duckdb.Error as error:
                return Response(f"DuckDB: {error}", status_code=400)
            if guarded.kind == "create" and guarded.new_table:
                result = await store_table(
                    ctx, guarded.new_table, names, "sql_result", True, who, origins=fetched
                )
                ctx.audit.write(who, "sql_pin", guarded.new_table, query=text, rows=names.num_rows)
                return json_response({"pinned": guarded.new_table} | result)
            # A CREATE TABLE pins a dataset and returns no cells, so it is open to the agent at every
            # level; a SELECT returns cells straight from the SQL box, so it is not.
            if not agent_may_see_raw_values(request, policy):
                return raw_values_refused()
            if not isinstance(fetched, list):
                raise ValueError("The query returned no rows.")
            capped = len(fetched) > row_cap
            fetched = fetched[:row_cap]
            ctx.audit.write(who, "sql", info.name, query=text, rows=len(fetched))
            cut = _truncate(fetched, settings.max_cell_chars)
            return json_response(
                {
                    "columns": names,
                    "rows": fetched,
                    "row_count": len(fetched),
                    "capped": capped,
                    "cap": row_cap,
                    "truncated": cut,
                    "milliseconds": round(elapsed, 1),
                },
                max_bytes=settings.max_sql_response_bytes,
            )
        except Exception as error:
            return error_response(error)

    async def selection_stats(request: Request) -> Response:
        """Excel's status bar for any selection, loaded rows or not: Count, Sum, Average."""
        if not agent_may_see_raw_values(request, policy):
            return raw_values_refused()
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            body = await read_json(request)
            raw = body.get("selection") if isinstance(body, dict) else None

            def compute(cur: duckdb.DuckDBPyConnection) -> dict[str, Any]:
                positioned, columns = ctx.views.positioned_sql(cur, info, grid.steps, grid.settings)
                where, chosen = selection_scope(raw, columns)
                sql, _ = stats_sql(positioned, where, chosen)
                row = cur.execute(sql).fetchone()
                return combine_stats(tuple(row) if row else (0,), chosen)

            stats = await ctx.grid_query(
                grid, query_int(request, "gen"), INTERACTIVE, compute, client_of(request)
            )
            grid.selected = int(stats["rows"])
            return json_response(stats)
        except Exception as error:
            return error_response(error)

    async def cancel(request: Request) -> Response:
        try:
            grid, _ = ctx.grid_and_info(request.path_params["grid_id"])
            return json_response({"interrupted": grid.interrupt_all()})
        except Exception as error:
            return error_response(error)

    # ===== export (plan section 10a) =====

    async def export(request: Request) -> Response:
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            params = dict(request.query_params)
            who = actor(request)
            # An agent's export writes to disk, where the agent can read it: only after the user's
            # click (POST /agent/export-approval), whose one-time ticket this spends, and only what
            # the agent may read at its level (masked, capped; no rows at level 0).
            if who == "agent" and not policy.redeem_export_ticket(params.pop("ticket", None), grid.id):
                return Response(
                    "An agent export needs the user's approval first; table_export asks for it.",
                    status_code=403,
                )
            level = policy.level if who == "agent" else 3
            options = exporting.ExportOptions.from_query(params)
            scope = params.get("scope", "view")
            if scope not in ("view", "rows", "columns", "selection", "source", "profile"):
                raise ValueError("scope must be view, rows, columns, selection, source or profile.")
            if (refused := export_refused(level, scope)) is not None:
                return refused
            # Rows written for an agent below level 3: its level's cap, and masks traced to the
            # source columns the way /agent/read traces them.
            row_cap = LEVEL_ROW_CAPS[level] if level < 3 and scope != "profile" else None
            trace_masks = level in (1, 2) and bool(policy.masks)
            selection = json.loads(params["selection"]) if scope == "selection" else None
            wanted_columns = [c for c in params.get("columns", "").split(",") if c]
            wanted_rows = [int(r) for r in params.get("rows", "").split(",") if r.strip().isdigit()]
            file_stem = params.get("file", info.name)
            if not _SAFE_FILE.fullmatch(file_stem):
                raise ValueError("File names may use letters, digits, '_' and '-'.")
            extension, media_type = exporting.FORMATS[options.format]
            file_name = file_stem + extension

            def view_origins(compiled: Any, names: list[str]) -> list[Origins] | None:
                """Where each exported view column came from, for the agent's masks."""
                if not trace_masks:
                    return None
                traced = ctx.trace(compiled.sql())
                by_name = dict(zip([c.name for c in compiled.columns], traced or [], strict=False))
                return [by_name.get(n) for n in names] if traced is not None else None

            def prepare(cur: duckdb.DuckDBPyConnection) -> Any:
                compiled = ctx.compiled(cur, grid, info)
                if scope == "profile":
                    with ctx.views.reading(cur, info, grid.steps, grid.settings) as rows:
                        profiled = profile_table(cur, rows, compiled.columns)
                    if level < 3:  # as agent_describe shows it
                        profiled_names = [r["column"] for r in profiled["columns"]]
                        decided = column_rules(
                            level, policy.masks, profiled_names, view_origins(compiled, profiled_names)
                        )
                        for row, how in zip(profiled["columns"], decided, strict=True):
                            redact_profile_row(level, how, row)
                    header = ["column", "type", "blanks", "blank_percent", "distinct", "min", "max"]
                    values = ", ".join(
                        "(" + ", ".join(_sql_value(row.get(n)) for n in header) + ")"
                        for row in profiled["columns"]
                    )
                    select = ", ".join(f"CAST(t.{quote(n)} AS VARCHAR) AS {quote(n)}" for n in header)
                    sql = (
                        f"SELECT {select} FROM (VALUES {values}) AS t({', '.join(quote(n) for n in header)})"
                    )
                    cols = tuple(Column(n, "VARCHAR") for n in header)
                    return sql, cols, len(profiled["columns"]), [], None
                if scope == "source":
                    names = ", ".join(quote(c.name) for c in info.columns)
                    sql = f"SELECT {names} FROM {info.table} ORDER BY rowid"
                    source_origins = [ctx.column_origins(info.name, c.name) for c in info.columns]
                    return sql, info.columns, info.row_count, [], source_origins if trace_masks else None
                positioned, columns = ctx.views.positioned_sql(cur, info, grid.steps, grid.settings)
                if scope == "columns":
                    unknown = set(wanted_columns) - {c.name for c in columns}
                    if not wanted_columns or unknown:
                        raise ValueError(f"Choose columns from the view; unknown: {sorted(unknown)}")
                    columns = tuple(c for c in columns if c.name in wanted_columns)
                where = ""
                if scope == "selection":
                    where, columns = selection_scope(selection, columns)
                names = ", ".join(quote(c.name) for c in columns)
                if scope == "rows":
                    if not wanted_rows:
                        raise ValueError("Select some rows first.")
                    where = f"WHERE {POS} IN ({', '.join(str(r) for r in sorted(set(wanted_rows)))})"
                sql = f"SELECT {names} FROM ({positioned}) {where} ORDER BY {POS}"
                total = cur.execute(f"SELECT count(*) FROM ({sql})").fetchone()
                origins = view_origins(compiled, [c.name for c in columns])
                return sql, columns, int(total[0]) if total else 0, describe_steps(grid.steps), origins

            sql, columns, total, step_text, origins = await ctx.grid_query(grid, None, BACKGROUND, prepare)
            capped_from: int | None = None
            if row_cap is not None:
                sql = f"{sql} LIMIT {row_cap}"
                if total > row_cap:
                    capped_from, total = total, row_cap
            provenance = (
                exporting.Provenance(
                    source=f"{info.name} (version {info.version}, {info.source_kind})",
                    steps=step_text if scope != "source" else [],
                    settings=describe_settings(grid.settings),
                    exported_at=datetime.now(UTC).isoformat(timespec="seconds"),
                    rows=total,
                )
                if options.provenance
                else None
            )

            def build() -> tuple[bytes, int]:
                cur = ctx.store.cursor()
                reader = exporting.batches(cur, sql)
                written = columns
                if row_cap is not None:  # at most a capped read's rows: mask them as /agent/read does
                    masked, written = mask_table(policy, reader.read_all(), columns, origins)
                    reader = pa.RecordBatchReader.from_batches(masked.schema, masked.to_batches())
                if options.format == "xlsx":
                    return exporting.excel(reader, written, options, provenance, total), len(written)
                if options.format == "parquet":
                    return exporting.parquet(reader, options, provenance), len(written)
                return b"".join(exporting.delimited(reader, written, options)), len(written)

            data, width = await ctx.workers.run(BACKGROUND, build)
            ctx.audit.write(
                who,
                "export",
                info.name,
                rows=total,
                format=options.format,
                file=file_name,
                scope=scope,
                columns=width,
            )
            message = f"Exported {total:,} rows x {width} columns to {file_name}"
            if capped_from is not None:
                message += f" (the first {total:,} of {capped_from:,}: privacy level {level}'s cap per read)"
            elif row_cap is not None and level < 3:
                message += f" (masked for privacy level {level})"
            if params.get("save") == "1" or who == "agent":
                folder = settings.export_dir
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / file_name
                if target.exists() and params.get("overwrite") != "1":
                    return Response(f"{target} already exists; choose another name.", status_code=409)
                target.write_bytes(data)
                return json_response({"saved": str(target), "message": message})

            async def stream() -> AsyncIterator[bytes]:
                for i in range(0, len(data), 1 << 20):
                    yield data[i : i + (1 << 20)]

            return StreamingResponse(
                stream(),
                media_type=media_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{file_name}"',
                    "X-Export-Message": message,
                },
            )
        except Exception as error:
            return error_response(error)

    # ===== code generation (plan section 9) =====

    async def code(request: Request) -> Response:
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            target = request.query_params.get("target")
            # The code spells out every step's values (text the user typed), which level 0 hides.
            if actor(request) == "agent" and policy.level == 0:
                return Response(
                    "Privacy level 0 hides step values, and the code spells them out. The user can "
                    "raise the level in the grid's Settings.",
                    status_code=403,
                )

            def make(cur: duckdb.DuckDBPyConnection) -> Any:
                return generate_code(
                    cur, ctx.views, info, grid.steps, grid.settings, target, settings.duckdb_memory_limit
                )

            result = await ctx.grid_query(grid, None, BACKGROUND, make)
            ctx.audit.write(
                actor(request),
                "code",
                info.name,
                target=result["target"],
                verified=result["verified"],
            )
            return json_response(result)
        except Exception as error:
            return error_response(error)

    # ===== recipes =====

    async def recipe(request: Request) -> Response:
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            folder = settings.state_dir / "recipes"
            if request.method == "DELETE":
                return json_response({"deleted": delete_recipe(folder, info.name)})
            path = save_recipe(folder, info.name, grid.steps, grid.settings)
            ctx.audit.write(actor(request), "save_recipe", info.name)
            return json_response({"saved": str(path)})
        except Exception as error:
            return error_response(error)

    return [
        Route("/grids", list_grids),
        Route("/grids", create_grid, methods=["POST"]),
        Route("/grids/{grid_id}", get_grid),
        Route("/grids/{grid_id}", close_grid, methods=["DELETE"]),
        Route("/grids/{grid_id}/rows", rows),
        Route("/grids/{grid_id}/count", count),
        Route("/grids/{grid_id}/cell", cell),
        Route("/grids/{grid_id}/step-counts", step_counts),
        Route("/grids/{grid_id}/profile", profile),
        Route("/grids/{grid_id}/header-stats", stats),
        Route("/grids/{grid_id}/categorize-stats", categorize_stats),
        Route("/grids/{grid_id}/steps", put_steps, methods=["POST"]),
        Route("/grids/{grid_id}/step-ops", step_op, methods=["POST"]),
        Route("/grids/{grid_id}/header-sort", header_sort, methods=["POST"]),
        Route("/grids/{grid_id}/sql", run_sql, methods=["POST"]),
        Route("/grids/{grid_id}/cancel", cancel, methods=["POST"]),
        Route("/grids/{grid_id}/selection-stats", selection_stats, methods=["POST"]),
        Route("/grids/{grid_id}/export", export),
        Route("/grids/{grid_id}/code", code),
        Route("/grids/{grid_id}/recipe", recipe, methods=["POST", "DELETE"]),
    ]


def _creates_table(text: str) -> bool | None:
    """Whether `text` is a CREATE TABLE (True) or something else (False); None when it can't be
    read, which the SQL guard then refuses with its own message before anything runs."""
    try:
        return isinstance(sqlglot.parse_one(text, read="duckdb"), sqlglot.exp.Create)
    except Exception:
        return None


def _sql_value(value: Any) -> str:
    from swissarmygrid.engine.steps import literal

    # The profile sheet is all text, so every value goes in as text.
    return "NULL" if value is None else literal(str(value))
