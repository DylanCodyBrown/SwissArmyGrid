"""Dataset routes: push, list, close, load a file, paste text; plus health and shutdown.

  GET    /health                        liveness (version and process id)
  POST   /shutdown                      `swissarmygrid stop`
  GET    /datasets                      every dataset with its size, and the memory budget
  PUT    /datasets/{name}?new_grid=0|1  push: Arrow stream (preferred) or orient="split" JSON
  DELETE /datasets/{name}               close a dataset and its grids, freeing its memory
  POST   /datasets/{name}/load          {"path": "C:\\...\\file.csv", "sheet": "..."} read by trusted Python
  POST   /files/sheets                  {"path": "C:\\...\\book.xlsx"} the workbook's sheet names
  POST   /datasets/{name}/paste         tab-separated text copied from Excel
  POST   /datasets/{name}/query         {"sql": "SELECT ...", "replace": bool} the "+" tab's table

A push reuses the dataset's first grid (that grid hot-reloads in place) unless new_grid=1.
The first grid on a dataset picks up a saved recipe for that name, if there is one.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from swissarmygrid import __version__
from swissarmygrid.engine.datasets import (
    DatasetError,
    DatasetInfo,
    arrow_stream_to_table,
    mib,
    quote,
    split_json_to_arrow,
    validate_name,
)
from swissarmygrid.engine.loaders import excel_sheets, load_file, parse_pasted
from swissarmygrid.engine.recipes import load_recipe
from swissarmygrid.engine.sql_guard import SqlRefused, guard
from swissarmygrid.engine.workers import BACKGROUND, INTERACTIVE
from swissarmygrid.server.context import (
    Context,
    actor,
    error_response,
    json_response,
    query_int,
    read_body,
    read_json,
)
from swissarmygrid.server.grids import Grid
from swissarmygrid.server.info_file import info_file_path
from swissarmygrid.server.masking import Origins
from swissarmygrid.server.routes_agent import Policy, pivot_refused

ARROW_STREAM: str = "application/vnd.apache.arrow.stream"


def dataset_summary(ctx: Context, info: DatasetInfo) -> dict[str, Any]:
    return {
        "name": info.name,
        "version": info.version,
        "rows": info.row_count,
        "columns": len(info.columns),
        "bytes": info.nbytes,
        "size": mib(info.nbytes),
        "source_kind": info.source_kind,
        "grids": [g.id for g in ctx.grids.for_dataset(info.name)],
    }


def budget(ctx: Context) -> dict[str, Any]:
    used, total = ctx.store.used_bytes, ctx.store.max_total_bytes
    return {
        "used_bytes": used,
        "total_bytes": total,
        "used": mib(used),
        "total": mib(total),
        "datasets": len(ctx.store.all()),
        "max_datasets": ctx.store.max_datasets,
        "grids": len(ctx.grids.all()),
        "max_grids": ctx.settings.max_grids,
        "warn": used >= ctx.settings.warn_fraction * total,
    }


async def store_table(
    ctx: Context,
    name: str,
    table: pa.Table,
    source_kind: str,
    new_grid: bool,
    who: str,
    query: str | None = None,
    follow: bool = False,
    origins: dict[str, Origins] | None = None,
) -> dict[str, Any]:
    """The common end of every way data arrives: store, pick a grid, notify, audit.

    `follow` is a Python session keeping an open table current after its variable was assigned
    again. It refreshes the grid where it is: no jump to the front, and no new tab if the user
    closed the old one (the data stays fresh for when they open it again).

    `origins`: for a table made by a query, where each column came from (masking.py); any other
    arrival replaces the data, so whatever the name was derived from no longer applies."""
    info: DatasetInfo = await ctx.workers.run(BACKGROUND, ctx.store.replace, name, table, source_kind, query)
    if origins is None:
        ctx.derived_columns.pop(name, None)
    else:
        ctx.derived_columns[name] = origins
    ctx.views.forget_dataset(name)
    existing = ctx.grids.first_for(name)
    if follow and existing is None:
        await ctx.live.dataset_updated(name, info.version)
        ctx.audit.write(
            who,
            "push",
            name,
            rows=info.row_count,
            source_kind=info.source_kind,
            bytes=info.nbytes,
            follow=True,
        )
        return {
            "grid_url": None,
            "workspace_url": ctx.workspace_url(with_token=who != "agent"),
            "grid": None,
            "dataset": name,
            "version": info.version,
            "watched": False,
        }
    grid: Grid
    if new_grid or existing is None:
        recipe = load_recipe(ctx.settings.state_dir / "recipes", name) if existing is None else None
        grid = ctx.grids.create(name, *(recipe or ([], None)))
    else:
        grid = existing
    await ctx.live.dataset_updated(name, info.version)
    if follow:
        ctx.audit.write(
            who,
            "push",
            name,
            rows=info.row_count,
            source_kind=info.source_kind,
            bytes=info.nbytes,
            follow=True,
        )
        watched = True  # the tab is wherever the user left it
    else:
        # An open workspace adds the grid as a tab (if new) and brings it to the front.
        await ctx.live.broadcast({"type": "grid_shown", "grid": grid.id, "dataset": name})
        ctx.audit.write(
            who, "push", name, rows=info.row_count, source_kind=info.source_kind, bytes=info.nbytes
        )
        # Nothing shows it yet: VS Code opens the workspace (its window the user was last in).
        watched = ctx.live.shown_or_opening(grid.id) or await ctx.live.ask_opener(ctx.workspace_url(grid))
    return {
        "grid_url": ctx.grid_url(grid, with_token=who != "agent"),
        "workspace_url": ctx.workspace_url(grid, with_token=who != "agent"),
        "grid": grid.id,
        "dataset": name,
        "version": info.version,
        # Whether the grid is on screen, about to be (a window an earlier push had opened), or
        # being opened by VS Code now. False only when no VS Code window is connected.
        "watched": watched,
    }


def routes(ctx: Context, policy: Policy) -> list[BaseRoute]:
    """`policy`: the agent privacy policy (routes_agent), for the SQL an agent may send."""
    settings = ctx.settings

    def own_file_refused(path: str) -> Response | None:
        """A 403 for a file of SwissArmyGrid's own: the connection file (one line of JSON, so it
        loaded as a table, token and all), the audit log, recipes: the state folder, except the
        export folder inside it (an export loaded back is a table)."""
        try:
            target = Path(path).expanduser().resolve()
            connection = info_file_path().expanduser().resolve()
            state = settings.state_dir.expanduser().resolve()
            exports = settings.export_dir.expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return None  # an unusable path: the loader says why
        inside_state = target.is_relative_to(state) and not target.is_relative_to(exports)
        if target == connection or inside_state:
            return Response("That is one of SwissArmyGrid's own files, not a table.", status_code=403)
        return None

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok", "version": __version__, "pid": os.getpid()})

    async def shutdown(request: Request) -> Response:
        if actor(request) == "agent":
            return Response("Stopping the server is for the user.", status_code=403)
        ctx.audit.write(actor(request), "shutdown")
        ctx.on_shutdown()
        return JSONResponse({"status": "stopping"})

    async def audit(request: Request) -> Response:
        """The audit log's last entries, for the page's activity list (never cell values)."""
        if actor(request) == "agent":
            return Response("The audit log is for the user.", status_code=403)
        try:
            limit = max(1, min(500, query_int(request, "limit", 100) or 100))
        except ValueError as error:
            return error_response(error)
        return json_response({"entries": ctx.audit.tail(limit)})

    async def list_datasets(request: Request) -> Response:
        return json_response(
            {"datasets": [dataset_summary(ctx, i) for i in ctx.store.all()], "budget": budget(ctx)}
        )

    async def put_dataset(request: Request) -> Response:
        try:
            name = validate_name(request.path_params["name"])
            follow = request.query_params.get("follow", "0") == "1"
            process_id = request.headers.get("x-process", "")
            if follow and not (ctx.store.get(name) is not None and ctx.processes.owns(process_id, name)):
                # A follow keeps an open table current. If it was closed (or another session took the
                # name) while this push was on its way, drop it: a push would quietly reopen the data.
                return Response("That table is no longer open, so it is not kept current.", status_code=409)
            content_type = request.headers.get("content-type", "").split(";")[0].strip()
            if content_type not in ("application/json", ARROW_STREAM):
                return Response(
                    f"Send {ARROW_STREAM} or application/json (pandas orient='split').",
                    status_code=415,
                )
            # Enforce the body cap early: refuse on Content-Length, then count while reading.
            cap = settings.max_push_body_bytes
            too_big = f"The request body is over the {mib(cap)} push limit."
            declared = request.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > cap:
                return Response(too_big, status_code=413)
            chunks: list[bytes] = []
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > cap:
                    return Response(too_big, status_code=413)
                chunks.append(chunk)
            body = b"".join(chunks)
            if content_type == ARROW_STREAM:
                table = await ctx.workers.run(
                    INTERACTIVE, arrow_stream_to_table, body, settings.max_dataset_bytes
                )
            else:
                try:
                    payload = json.loads(body)
                except ValueError:
                    return Response("The request body is not valid JSON.", status_code=400)
                table = await ctx.workers.run(INTERACTIVE, split_json_to_arrow, payload)
            result = await store_table(
                ctx,
                name,
                table,
                request.headers.get("x-source-kind", "unknown"),
                request.query_params.get("new_grid", "0") == "1",
                actor(request),
                follow=follow,
            )
            # A Python session's watcher says which session pushed this (X-Process): that session
            # now follows the dataset, and the Open list shows the table as open.
            if process_id and ctx.processes.note_loaded(process_id, name):
                await ctx.live.broadcast({"type": "processes_changed"})
            return json_response(result)
        except Exception as error:  # every failure becomes a plain-language status
            return error_response(error)

    async def delete_dataset(request: Request) -> Response:
        name = request.path_params["name"]
        if not await ctx.workers.run(INTERACTIVE, ctx.store.close, name):
            return Response("No such dataset.", status_code=404)
        ctx.views.forget_dataset(name)
        ctx.derived_columns.pop(name, None)
        for grid in ctx.grids.for_dataset(name):
            ctx.grids.close(grid.id)
        await ctx.live.broadcast({"type": "dataset_closed", "dataset": name})
        ctx.audit.write(actor(request), "close", name)
        return json_response({"closed": name, "budget": budget(ctx)})

    async def load(request: Request) -> Response:
        try:
            name = validate_name(request.path_params["name"])
            body = await read_json(request)
            path = body.get("path") if isinstance(body, dict) else None
            if not isinstance(path, str):
                return Response('Send {"path": "<full path to the file>"}.', status_code=400)
            if (refused := own_file_refused(path)) is not None:
                return refused
            sheet = body.get("sheet") if isinstance(body.get("sheet"), str) else None
            table, kind = await ctx.workers.run(
                BACKGROUND, load_file, path, settings.max_dataset_bytes, sheet
            )
            result = await store_table(ctx, name, table, kind, bool(body.get("new_grid")), actor(request))
            ctx.audit.write(actor(request), "load_file", name, file=os.path.basename(path), sheet=sheet)
            return json_response(result)
        except Exception as error:
            return error_response(error)

    async def sheets(request: Request) -> Response:
        """The sheet names of a workbook, so VS Code can ask which to load."""
        try:
            body = await read_json(request)
            path = body.get("path") if isinstance(body, dict) else None
            if not isinstance(path, str):
                return Response('Send {"path": "<full path to the .xlsx file>"}.', status_code=400)
            if (refused := own_file_refused(path)) is not None:
                return refused
            return json_response({"sheets": await ctx.workers.run(BACKGROUND, excel_sheets, path)})
        except Exception as error:
            return error_response(error)

    async def paste(request: Request) -> Response:
        try:
            name = validate_name(request.path_params["name"])
            text = (await read_body(request, settings.max_push_body_bytes)).decode("utf-8", "replace")
            table = await ctx.workers.run(INTERACTIVE, parse_pasted, text, settings.max_dataset_bytes)
            return json_response(await store_table(ctx, name, table, "clipboard", True, actor(request)))
        except Exception as error:
            return error_response(error)

    async def build_from_query(request: Request) -> Response:
        """The "+" tab: dataset `name` from one SELECT over the open tables, each read as its tab
        shows it. "replace" is the tab's Run again; it rebuilds only a table a query made, so
        a pushed dataset (or another tab's table) is never overwritten by a name clash."""
        try:
            name = validate_name(request.path_params["name"])
            body = await read_json(request)
            text = body.get("sql") if isinstance(body, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise ValueError('Send {"sql": "SELECT ... FROM <table>"}.')
            if (refused := pivot_refused(request, policy, text)) is not None:
                return refused
            existing = ctx.store.get(name)
            if existing is not None and not (body.get("replace") is True and existing.query is not None):
                raise DatasetError(f"A dataset named {name!r} already exists; pick a new name.")

            def tab_view(cursor: duckdb.DuckDBPyConnection, dataset: str) -> str:
                if dataset == name:
                    raise SqlRefused(f"This query builds {name}, so it can't read {name}.")
                # Two tabs on one dataset: the name reads the first (oldest) one.
                grid, info = ctx.grids.first_for(dataset), ctx.store.get(dataset)
                if grid is None or info is None:  # no tab open: its rows as loaded
                    return f"SELECT * FROM {quote('ds_' + dataset)}"  # noqa: S608 (a quoted dataset name)
                return ctx.compiled(cursor, grid, info).sql()

            def execute(cursor: duckdb.DuckDBPyConnection) -> tuple[pa.Table, dict[str, Origins]]:
                datasets = {i.name for i in ctx.store.all()}
                guarded = guard(text, datasets, None, lambda dataset: tab_view(cursor, dataset))
                if guarded.kind != "select":
                    raise SqlRefused(f"Write one SELECT; its result becomes {name}.")
                timer = threading.Timer(settings.query_timeout_seconds, cursor.interrupt)
                timer.start()
                try:
                    result = cursor.sql(guarded.sql).to_arrow_table()
                finally:
                    timer.cancel()
                return result, ctx.origins_of_result(guarded.sql, result.column_names)

            try:
                table, origins = await ctx.workers.run(BACKGROUND, lambda: execute(ctx.store.cursor()))
            except duckdb.InterruptException:
                return Response(
                    f"The query was stopped: it ran past {settings.query_timeout_seconds:.0f} seconds.",
                    status_code=408,
                )
            who = actor(request)
            result = await store_table(
                ctx, name, table, "sql_result", False, who, query=text, origins=origins
            )
            ctx.audit.write(who, "sql_query", name, query=text, rows=table.num_rows)
            return json_response(result | {"rows": table.num_rows})
        except Exception as error:
            return error_response(error)

    return [
        Route("/health", health),
        Route("/shutdown", shutdown, methods=["POST"]),
        Route("/datasets", list_datasets),
        Route("/audit", audit),
        Route("/datasets/{name}", put_dataset, methods=["PUT"]),
        Route("/datasets/{name}", delete_dataset, methods=["DELETE"]),
        Route("/datasets/{name}/load", load, methods=["POST"]),
        Route("/datasets/{name}/paste", paste, methods=["POST"]),
        Route("/datasets/{name}/query", build_from_query, methods=["POST"]),
        Route("/files/sheets", sheets, methods=["POST"]),
    ]
