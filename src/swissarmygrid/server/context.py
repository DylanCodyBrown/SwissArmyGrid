"""The services every route shares, and the helpers that shape responses.

`Context` is the composition root, the way a C# app registers its singletons in one place:
the dataset store, the view engine, the grid registry, live updates, the worker pool, the audit
log and the settings. Route modules (routes_*.py) take a Context and return their routes, so
each one reads like a controller over these services.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, TypeVar
from uuid import UUID

import duckdb
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from swissarmygrid.engine.audit import AuditLog
from swissarmygrid.engine.datasets import (
    DatasetError,
    DatasetInfo,
    DatasetLimitError,
    DatasetStore,
    DatasetTooLargeError,
)
from swissarmygrid.engine.export import ExportError
from swissarmygrid.engine.sql_guard import SqlRefused
from swissarmygrid.engine.steps import CompiledView, StepError
from swissarmygrid.engine.views import ViewEngine
from swissarmygrid.engine.workers import PriorityWorkers
from swissarmygrid.server.grids import Grid, GridLimitError, GridRegistry, StaleRequestError
from swissarmygrid.server.live_updates import LiveUpdates
from swissarmygrid.server.masking import Origins, trace
from swissarmygrid.server.processes import ProcessRegistry
from swissarmygrid.settings import Settings

T = TypeVar("T")

# JavaScript numbers are exact only up to 2**53; larger integers travel as text.
_JS_SAFE_INTEGER: int = 2**53 - 1


@dataclass
class Context:
    port: int
    token: str
    settings: Settings
    store: DatasetStore
    views: ViewEngine
    grids: GridRegistry
    live: LiveUpdates
    workers: PriorityWorkers
    audit: AuditLog
    on_shutdown: Callable[[], None]
    # The Python sessions offering tables to the Open list (processes.py).
    processes: ProcessRegistry
    # For a table made by a query: each column's origins, the dataset columns it was computed
    # from (masking.py), so a privacy mask on a source column follows the data into it.
    derived_columns: dict[str, dict[str, Origins]] = field(default_factory=dict)

    def grid_url(self, grid: Grid) -> str:
        # The fragment carries the token to the page without ever sending it to the server.
        return f"http://127.0.0.1:{self.port}/g/{grid.id}#token={self.token}"

    def workspace_url(self, grid: Grid | None = None) -> str:
        """The tabbed workspace (one window, a tab per grid), opened on `grid` when given."""
        focus = f"&grid={grid.id}" if grid else ""
        return f"http://127.0.0.1:{self.port}/workspace#token={self.token}{focus}"

    def grid_and_info(self, grid_id: str) -> tuple[Grid, DatasetInfo]:
        grid = self.grids.get(grid_id)
        info = self.store.get(grid.dataset) if grid else None
        if grid is None or info is None:
            raise LookupError("No such grid; it may have been closed or the server restarted.")
        return grid, info

    async def grid_query(
        self,
        grid: Grid,
        gen: int | None,
        priority: int,
        fn: Callable[[duckdb.DuckDBPyConnection], T],
        client: str = "",
    ) -> T:
        """Run `fn(cursor)` on a worker for `grid`, honouring request generations: an older gen
        is refused, and a newer one interrupts any older query still running."""
        cursor = self.store.cursor()
        ticket = grid.admit(gen, cursor, client)

        def job() -> T:
            if grid.is_stale(gen, client):
                raise StaleRequestError()
            return fn(cursor)

        try:
            return await self.workers.run(priority, job)
        except duckdb.InterruptException as error:
            # Interrupted because a newer gen arrived: stale. Otherwise (a timeout) pass it on.
            if grid.is_stale(gen, client):
                raise StaleRequestError() from error
            raise
        finally:
            grid.release(ticket)

    def compiled(self, cursor: duckdb.DuckDBPyConnection, grid: Grid, info: DatasetInfo) -> CompiledView:
        return self.views.compile(cursor, info, grid.steps, grid.settings)

    def lineage_tables(self) -> dict[str, list[str]]:
        """Every dataset's table (its plain name, ds_<name>) and columns, for masking.trace."""
        return {"ds_" + info.name: [c.name for c in info.columns] + ["rowid"] for info in self.store.all()}

    def lineage_derived(self) -> dict[str, dict[str, Origins]]:
        """The origins of the columns of tables made by queries, keyed by their table."""
        return {"ds_" + name: columns for name, columns in self.derived_columns.items()}

    def column_origins(self, dataset: str, column: str) -> Origins:
        """Where one column of a dataset came from: itself, or what its query read."""
        derived = self.derived_columns.get(dataset)
        if derived is None:
            return frozenset({column.lower()})
        return derived.get(column.lower())

    def trace(self, sql: str) -> list[Origins] | None:
        """The origins of each output column of `sql`, a query over the datasets' tables."""
        return trace(sql, self.lineage_tables(), self.lineage_derived())

    def origins_of_result(self, sql: str, names: list[str]) -> dict[str, Origins]:
        """For a table about to be made from `sql`: each of its columns' origins (None: untraced)."""
        traced = self.trace(sql)
        if traced is None or len(traced) != len(names):
            return {name.lower(): None for name in names}
        return {name.lower(): origin for name, origin in zip(names, traced, strict=True)}


def jsonable(value: Any) -> Any:
    """A cell value as JSON the browser can show without losing information."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value if abs(value) <= _JS_SAFE_INTEGER else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, Decimal | UUID | timedelta):
        return str(value)
    if isinstance(value, bytes | bytearray):
        return f"<{len(value)} bytes>"
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    return str(value)


def json_response(content: Any, status_code: int = 200, max_bytes: int | None = None) -> Response:
    body = json.dumps(jsonable(content), separators=(",", ":"))
    if max_bytes is not None and len(body) > max_bytes:
        return PlainTextResponse(
            f"The response would be {len(body) / 1048576:.1f} MiB; the limit is "
            f"{max_bytes / 1048576:.0f} MiB. Ask for fewer rows or columns.",
            status_code=413,
        )
    return Response(body, status_code=status_code, media_type="application/json")


def error_response(error: Exception) -> Response:
    """Map the engine's error types to HTTP status codes with plain-language messages."""
    status = 500
    if isinstance(error, DatasetTooLargeError):
        status = 413
    elif isinstance(error, DatasetLimitError | GridLimitError | StaleRequestError):
        status = 409
    elif isinstance(error, DatasetError | StepError | SqlRefused | ExportError | ValueError):
        status = 400
    elif isinstance(error, LookupError):
        status = 404
    elif isinstance(error, duckdb.Error):
        return PlainTextResponse(f"DuckDB: {error}", status_code=400)
    message = "A newer request replaced this one." if isinstance(error, StaleRequestError) else str(error)
    return PlainTextResponse(message or type(error).__name__, status_code=status)


async def read_json(request: Request) -> Any:
    try:
        return json.loads(await request.body())
    except ValueError as error:
        raise ValueError("The request body is not valid JSON.") from error


def query_int(request: Request, name: str, default: int | None = None) -> int | None:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a whole number.") from error


def client_of(request: Request) -> str:
    """The page's random client id (query `client`), which scopes request generations."""
    return request.query_params.get("client", "")[:40]


def actor(request: Request) -> str:
    """Who made the request, for the audit log: the MCP launcher marks itself as the agent."""
    return "agent" if request.headers.get("x-actor") == "agent" else "user"
