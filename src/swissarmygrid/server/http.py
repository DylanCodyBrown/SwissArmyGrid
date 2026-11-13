"""What every route module shares for speaking HTTP: reading a request (bodies with a cap, JSON, who
is asking) and shaping the answer (cell values as JSON, and each error as its status and plain words).

The use cases know nothing of this: they take plain values and raise plain errors, and this file is
where an error becomes a status code.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, TypeVar
from uuid import UUID

import duckdb
import sqlglot
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from swissarmygrid.domain.blades import BladeError
from swissarmygrid.domain.catalogs import CatalogError
from swissarmygrid.domain.datasets import (
    DatasetError,
    DatasetLimitError,
    DatasetTooLargeError,
    mib,
)
from swissarmygrid.domain.export import ExportError
from swissarmygrid.domain.grids import GridLimitError, StaleRequestError
from swissarmygrid.domain.sql_guard import SqlRefused
from swissarmygrid.domain.steps import StepError
from swissarmygrid.services.workspace import Conflict, NotFound, Refused, TimedOut

T = TypeVar("T")

# JavaScript numbers are exact only up to 2**53; larger integers travel as text.
_JS_SAFE_INTEGER: int = 2**53 - 1
# The largest JSON body a control request may send (steps, a query, a selection, a model).
MAX_JSON_BODY_BYTES: int = 64 * 1024 * 1024


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
    # What a use case ended in besides its answer (services/workspace.py).
    for outcome, code in ((Refused, 403), (NotFound, 404), (TimedOut, 408), (Conflict, 409)):
        if isinstance(error, outcome):
            return Response(str(error), status_code=code)
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
