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
from datetime import UTC, datetime
from typing import Any

import duckdb
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
from swissarmygrid.server.routes_agent import Policy, agent_may_see_raw_values, raw_values_refused
from swissarmygrid.server.routes_datasets import budget, store_table
from swissarmygrid.server.selection import combine_stats, selection_scope, stats_sql

_SAFE_FILE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def describe_grid(
    ctx: Context, grid: Grid, info: DatasetInfo, cursor: duckdb.DuckDBPyConnection
) -> dict[str, Any]:
    compiled = ctx.compiled(cursor, grid, info)
    error = compiled.error
    return {
        "grid": grid.id,
        "dataset": info.name,
        "title": info.name,
        "version": info.version,
        "row_count": info.row_count,
        "source_kind": info.source_kind,
        "query": info.query,
        "source_columns": [{"name": c.name, "type": c.type} for c in info.columns],
        "columns": [{"name": c.name, "type": c.type} for c in compiled.columns],
        # Columns after each step (index 0 = the source), for previewing a step.
        "stage_columns": [[{"name": c.name, "type": c.type} for c in s.columns] for s in compiled.stages],
        "steps": [s.to_json() | {"description": describe_step(s)} for s in grid.steps],
        "step_error": None if error is None else {"index": error.index, "message": error.message},
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

    # ===== grid lifecycle =====

    async def list_grids(request: Request) -> Response:
        return json_response(
            {
                "grids": [
                    {
                        "grid": g.id,
                        "dataset": g.dataset,
                        "steps": len(g.steps),
                        "url": ctx.grid_url(g),
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
                    "grid_url": ctx.grid_url(grid),
                    "workspace_url": ctx.workspace_url(grid),
                    "shown": shown,
                }
            )
        except Exception as error:
            return error_response(error)

    async def get_grid(request: Request) -> Response:
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            return json_response(
                await ctx.grid_query(grid, None, INTERACTIVE, lambda cur: describe_grid(ctx, grid, info, cur))
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
                sql = compiled.sql(upto)
                columns = compiled.columns_at(upto)
                if column_name is None:
                    return cached(key, lambda: profile_table(cur, sql, columns))
                match = next((c for c in columns if c.name == column_name), None)
                if match is None:
                    raise LookupError(f"No column {column_name!r} in this view.")
                return cached(key, lambda: profile_column(cur, sql, match))

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
                return cached(key, lambda: header_stats(cur, compiled.sql(), compiled.columns))

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

    async def put_steps(request: Request) -> Response:
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            body = await read_json(request)
            if not isinstance(body, dict):
                raise ValueError('Send {"steps": [...], "settings": {...}}.')
            new_steps = steps_from_json(body.get("steps", [s.to_json() for s in grid.steps]))
            new_settings = ViewSettings.from_json(body["settings"]) if "settings" in body else grid.settings
            gen = body.get("gen")
            client = str(body.get("client", ""))[:40]
            if isinstance(gen, int):  # newer gen from this page: interrupt its older queries
                grid.release(grid.admit(gen, ctx.store.cursor(), client))
            grid.steps, grid.settings = new_steps, new_settings
            described = await ctx.grid_query(
                grid, None, INTERACTIVE, lambda cur: describe_grid(ctx, grid, info, cur)
            )
            await ctx.live.broadcast(
                {"type": "grid_updated", "grid": grid.id, "by": actor(request), "gen": grid.gen}
            )
            ctx.audit.write(actor(request), "steps", info.name, steps=len(new_steps))
            return json_response(described)
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
            described = await ctx.grid_query(
                grid, None, INTERACTIVE, lambda cur: describe_grid(ctx, grid, info, cur)
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
            who = actor(request)
            row_cap = settings.max_sql_rows

            def execute(cur: duckdb.DuckDBPyConnection) -> Any:
                compiled = ctx.compiled(cur, grid, info)
                # `upto`: while editing step N, `grid` is the table entering that step. A dataset
                # name, this grid's own included, reads its rows as loaded, as the variable of
                # that name does in the user's Python.
                upto = body.get("upto") if isinstance(body.get("upto"), int) else None
                guarded = guard(text, {i.name for i in ctx.store.all()}, lambda: compiled.sql(upto))
                timer = threading.Timer(settings.query_timeout_seconds, cur.interrupt)
                started = time.perf_counter()
                timer.start()
                try:
                    if guarded.kind == "create":
                        return guarded, cur.sql(guarded.sql).to_arrow_table(), None, 0.0
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
                result = await store_table(ctx, guarded.new_table, names, "sql_result", True, who)
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
            options = exporting.ExportOptions.from_query(params)
            scope = params.get("scope", "view")
            if scope not in ("view", "rows", "columns", "selection", "source", "profile"):
                raise ValueError("scope must be view, rows, columns, selection, source or profile.")
            selection = json.loads(params["selection"]) if scope == "selection" else None
            wanted_columns = [c for c in params.get("columns", "").split(",") if c]
            wanted_rows = [int(r) for r in params.get("rows", "").split(",") if r.strip().isdigit()]
            file_stem = params.get("file", info.name)
            if not _SAFE_FILE.fullmatch(file_stem):
                raise ValueError("File names may use letters, digits, '_' and '-'.")
            extension, media_type = exporting.FORMATS[options.format]
            file_name = file_stem + extension
            who = actor(request)

            def prepare(cur: duckdb.DuckDBPyConnection) -> Any:
                compiled = ctx.compiled(cur, grid, info)
                if scope == "profile":
                    profiled = profile_table(cur, compiled.sql(), compiled.columns)
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
                    return sql, cols, len(profiled["columns"]), []
                if scope == "source":
                    names = ", ".join(quote(c.name) for c in info.columns)
                    sql = f"SELECT {names} FROM {info.table} ORDER BY rowid"
                    return sql, info.columns, info.row_count, []
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
                return sql, columns, int(total[0]) if total else 0, describe_steps(grid.steps)

            sql, columns, total, step_text = await ctx.grid_query(grid, None, BACKGROUND, prepare)
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

            def build() -> bytes:
                cur = ctx.store.cursor()
                reader = exporting.batches(cur, sql)
                if options.format == "xlsx":
                    return exporting.excel(reader, columns, options, provenance, total)
                if options.format == "parquet":
                    return exporting.parquet(reader, options, provenance)
                return b"".join(exporting.delimited(reader, columns, options))

            data: bytes = await ctx.workers.run(BACKGROUND, build)
            ctx.audit.write(
                who,
                "export",
                info.name,
                rows=total,
                format=options.format,
                file=file_name,
                scope=scope,
                columns=len(columns),
            )
            message = f"Exported {total:,} rows x {len(columns)} columns to {file_name}"
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
                return generate_code(cur, ctx.views, info, grid.steps, grid.settings, target)

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
        Route("/grids/{grid_id}/header-sort", header_sort, methods=["POST"]),
        Route("/grids/{grid_id}/sql", run_sql, methods=["POST"]),
        Route("/grids/{grid_id}/cancel", cancel, methods=["POST"]),
        Route("/grids/{grid_id}/selection-stats", selection_stats, methods=["POST"]),
        Route("/grids/{grid_id}/export", export),
        Route("/grids/{grid_id}/code", code),
        Route("/grids/{grid_id}/recipe", recipe, methods=["POST", "DELETE"]),
    ]


def _sql_value(value: Any) -> str:
    from swissarmygrid.engine.steps import literal

    # The profile sheet is all text, so every value goes in as text.
    return "NULL" if value is None else literal(str(value))
