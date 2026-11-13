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
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, TypeVar
from uuid import UUID

import duckdb
import sqlglot
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from swissarmygrid.adapters.database import PriorityWorkers
from swissarmygrid.adapters.datasets import DatasetStore
from swissarmygrid.adapters.views import ViewEngine
from swissarmygrid.domain.datasets import (
    DatasetError,
    DatasetInfo,
    DatasetLimitError,
    DatasetTooLargeError,
    mib,
)
from swissarmygrid.domain.export import ExportError
from swissarmygrid.domain.grids import Grid, GridLimitError, GridRegistry, StaleRequestError
from swissarmygrid.domain.sql_guard import SqlGrammar, SqlRefused
from swissarmygrid.domain.steps import CompiledView, StepError
from swissarmygrid.engine.audit import AuditLog
from swissarmygrid.engine.catalog import CatalogError, CatalogStore
from swissarmygrid.server.blade_store import BladeError, BladeStore
from swissarmygrid.server.live_updates import LiveUpdates
from swissarmygrid.server.notebook import NotebookServer
from swissarmygrid.server.processes import ProcessRegistry
from swissarmygrid.services.ports import CodeVerifier
from swissarmygrid.settings import Settings

T = TypeVar("T")

# JavaScript numbers are exact only up to 2**53; larger integers travel as text.
_JS_SAFE_INTEGER: int = 2**53 - 1
# The largest JSON body a control request may send (steps, a query, a selection, a model).
MAX_JSON_BODY_BYTES: int = 64 * 1024 * 1024


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
    # Source databases' names and types (engine/catalog.py) and the saved blades (blade_store.py).
    catalogs: CatalogStore
    blades: BladeStore
    # The notebook (marimo), started on first use, and the cookie its frame is let in with.
    notebook: NotebookServer
    notebook_cookie: str
    # DuckDB's own reading of SQL text, which the guard checks every query against.
    grammar: SqlGrammar
    # Runs copied code to check it against the grid (adapters/codegen.py).
    verifier: CodeVerifier

    def grid_url(self, grid: Grid, with_token: bool = True) -> str:
        """The grid's page. The fragment carries the token to the page without ever sending it to
        the server. `with_token=False` for an agent: the token is the user's credential (a request
        with it and no X-Actor is the user's), so an agent gets the address only."""
        address = f"http://127.0.0.1:{self.port}/g/{grid.id}"
        return f"{address}#token={self.token}" if with_token else address

    def workspace_url(self, grid: Grid | None = None, with_token: bool = True) -> str:
        """The tabbed workspace (one window, a tab per grid), opened on `grid` when given."""
        address = f"http://127.0.0.1:{self.port}/workspace"
        if not with_token:
            return address + (f"#grid={grid.id}" if grid else "")
        focus = f"&grid={grid.id}" if grid else ""
        return f"{address}#token={self.token}{focus}"

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
        return _jsonable_list(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    return str(value)


def _jsonable_list(values: list[Any] | tuple[Any, ...]) -> list[Any] | tuple[Any, ...]:
    """`jsonable` for a list (a window of rows is a list of lists): values that are already plain
    JSON are passed over inline, without a call each, and the list is copied only when one of its
    values had to change. json.dumps writes a tuple as an array, so a tuple can stay one. A
    1,000 x 50 window: 7.6 ms -> 2.3 ms."""
    changed: list[Any] | None = None
    isfinite = math.isfinite
    for i, value in enumerate(values):
        kind = type(value)
        if kind is str or value is None or kind is bool:
            continue
        if kind is int:
            if -_JS_SAFE_INTEGER <= value <= _JS_SAFE_INTEGER:
                continue
        elif kind is float and isfinite(value):
            continue
        shown = jsonable(value)
        if shown is not value:
            if changed is None:
                changed = list(values)
            changed[i] = shown
    return values if changed is None else changed


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
    if isinstance(error, DatasetTooLargeError | BodyTooLargeError):
        status = 413
    elif isinstance(error, DatasetLimitError | GridLimitError | StaleRequestError):
        status = 409
    elif isinstance(
        error, DatasetError | StepError | SqlRefused | ExportError | CatalogError | BladeError | ValueError
    ):
        status = 400
    elif isinstance(error, LookupError):
        status = 404
    elif isinstance(error, duckdb.Error):
        return PlainTextResponse(f"DuckDB: {error}", status_code=400)
    elif isinstance(error, sqlglot.errors.SqlglotError):  # SQL that could not be read: the sender's
        return PlainTextResponse(
            f"Could not read the SQL: {str(error).splitlines()[0][:300]}", status_code=400
        )
    message = "A newer request replaced this one." if isinstance(error, StaleRequestError) else str(error)
    return PlainTextResponse(message or type(error).__name__, status_code=status)


class BodyTooLargeError(ValueError):
    """A request body over its cap. Maps to HTTP 413."""


async def read_body(request: Request, cap: int) -> bytes:
    """The request body, refused past `cap` bytes: on Content-Length first, then while reading,
    so an oversized body is never held in memory whole."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        raise BodyTooLargeError(f"The request body is over the {mib(cap)} limit.")
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > cap:
            raise BodyTooLargeError(f"The request body is over the {mib(cap)} limit.")
        chunks.append(chunk)
    return b"".join(chunks)


async def read_json(request: Request) -> Any:
    """The JSON body of a control request (steps, a query, a card...), at most MAX_JSON_BODY_BYTES.
    Data arrives by push or paste, which have their own, larger cap."""
    body = await read_body(request, MAX_JSON_BODY_BYTES)
    try:
        return json.loads(body)
    except ValueError as error:
        raise ValueError("The request body is not valid JSON.") from error


async def read_json_object(request: Request, shape: str) -> dict[str, Any]:
    """`read_json`, refusing anything but an object with `shape` (the message names what to send)."""
    body = await read_json(request)
    if not isinstance(body, dict):
        raise ValueError(f"Send a JSON object: {shape}.")
    return body


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
