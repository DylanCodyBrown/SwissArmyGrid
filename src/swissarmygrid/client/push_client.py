"""Send tabular data to the local SwissArmyGrid server: `push(orders)`.

Accepts a pandas DataFrame, a Polars DataFrame, a PyArrow table, a DuckDB relation, anything
else that speaks the Arrow C stream interface, or a list of dicts ("records").

The dataset name, which is also the browser tab's title, comes from:
1. `name=` when given;
2. otherwise the variable you passed: `push(orders)` names the dataset "orders";
3. otherwise "data".

With pyarrow installed (the normal case) the data travels as an Arrow stream, which keeps
real column types (dates stay dates). Without it a pandas frame goes as orient="split" JSON.

SwissArmyGrid lives in VS Code. Its extension starts the server when VS Code opens and shows
the grids there; this client only connects to that server and never starts one or opens a
browser, so every script, notebook and agent reaches the one server the extension keeps current.
"""

from __future__ import annotations

import inspect
import io
import json
import linecache
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from types import FrameType
from typing import Any

_MIB: int = 1024 * 1024
_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TIMEOUT_SECONDS: float = 300.0
NOT_RUNNING: str = (
    "SwissArmyGrid isn't running. It runs inside VS Code: open VS Code and the SwissArmyGrid "
    "extension starts it (or click SwissArmyGrid in VS Code's status bar to start it again)."
)


class DatasetTooLargeError(ValueError):
    """The dataset is larger than the server's size limit."""


class ServerNotRunningError(RuntimeError):
    """No server answers at the address in the connection file."""


class TableNotOpenError(RuntimeError):
    """A table kept current by the watcher was closed in the grid, so the server refused the refresh."""


def info_file() -> Path:
    override = os.environ.get("SWISSARMYGRID_INFO")
    return Path(override) if override else Path.home() / ".swissarmygrid" / "server.json"


def _read_info() -> dict[str, Any] | None:
    try:
        info: Any = json.loads(info_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def _healthy(info: dict[str, Any]) -> bool:
    request = urllib.request.Request(
        f"http://127.0.0.1:{info.get('port')}/health",
        headers={"Authorization": f"Bearer {info.get('token')}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310 (fixed http://127.0.0.1 URL)
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError):
        return False


def ensure_server() -> dict[str, Any]:
    """The running server's connection info. The server is VS Code's to start (its extension
    does it when VS Code opens), so this only connects: with none running it raises, saying so.
    A connection file that names no live server (left by a crash) counts as not running."""
    info = _read_info()
    if info is not None and _healthy(info):
        return info
    raise ServerNotRunningError(NOT_RUNNING)


def _too_large_message(name: str, size: int, limit: int) -> str:
    return (
        f"Dataset '{name}' is about {size / _MIB:.1f} MiB; the limit is "
        f"{limit / _MIB:.0f} MiB. Filter, sample, or select fewer columns before "
        "pushing, or raise the limit (SWISSARMYGRID_MAX_DATASET_MB, then restart the server)."
    )


_CALL_ARGUMENT = re.compile(r"\bpush\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*[,)]")


def caller_variable_name(data: object, frame: FrameType | None) -> str | None:
    """The name the caller has for `data`.

    First the name written in the call itself (`sag.push(orders)` -> "orders"), read from the
    calling line of source, when that name does hold `data`: this is what you meant even when a
    loop variable (`for frame in ...`) points at the same table. Otherwise the one local or global
    that holds it; several names for one object are ambiguous and give None.
    """
    if frame is None:
        return None
    written = _name_in_call(frame)
    if written and _NAME.fullmatch(written):
        for scope in (frame.f_locals, frame.f_globals):
            if written in scope:
                if scope[written] is data:
                    return written
                break
    for scope in (frame.f_locals, frame.f_globals):
        names = [n for n, v in scope.items() if v is data and not n.startswith("_") and _NAME.fullmatch(n)]
        if len(names) == 1:
            return names[0]
        if names:  # several names for the same object: ambiguous, fall through
            return None
    return None


def _name_in_call(frame: FrameType) -> str | None:
    """The variable named first in the push(...) call on the caller's line, if the source can be
    read (scripts, notebooks and the VS Code debug console can; a bare `python -c` can't)."""
    try:
        line = linecache.getline(frame.f_code.co_filename, frame.f_lineno, frame.f_globals)
    except Exception:  # best effort: an unreadable source just falls back to the variable search
        return None
    found = _CALL_ARGUMENT.findall(line)
    return found[0] if len(found) == 1 else None


def _source_kind(data: object) -> str:
    module = type(data).__module__.split(".")[0]
    if isinstance(data, list):
        return "records"
    return {"pandas": "pandas", "polars": "polars", "duckdb": "duckdb", "pyarrow": "arrow"}.get(
        module, "unknown"
    )


def _to_arrow(data: object) -> Any:
    """Any supported input as a pyarrow Table. Raises ImportError without pyarrow."""
    import pyarrow as pa

    kind = _source_kind(data)
    if kind == "pandas":
        frame: Any = data
        if not _has_default_index(frame):
            frame = frame.reset_index()
        return pa.Table.from_pandas(frame, preserve_index=False)
    if kind == "records":
        return pa.Table.from_pylist(data)
    if kind == "duckdb":
        return data.to_arrow_table()  # type: ignore[attr-defined]
    if isinstance(data, pa.Table):
        return data
    if hasattr(type(data), "__arrow_c_stream__"):
        # Read the stream directly. pa.table() would treat a tuple subclass that speaks Arrow (a
        # query client's NamedTuple result, say) as a list of arrays and fail before it looked for the stream.
        return pa.RecordBatchReader.from_stream(data).read_all()
    # Polars and anything else pyarrow can turn into a table.
    return pa.table(data)


def _has_default_index(frame: Any) -> bool:
    import pandas as pd

    index = frame.index
    return isinstance(index, pd.RangeIndex) and index.start == 0 and index.step == 1


def push(
    data: object,
    name: str | None = None,
    new_grid: bool = False,
    *,
    start_server: bool = True,
    open: bool | None = None,
    _frame: FrameType | None = None,
    _process: str | None = None,
    _follow: bool = False,
) -> str:
    """Create or update a dataset from `data`. Returns the address of its grid.

    You rarely need this: the watcher (client/watch.py) lists the tables in your running Python and
    pushes the one you open, and keeps it current. Keep `push` for what the watcher cannot see: a
    script that is about to exit, a table made inside a function, code that already calls it.
    `_process` and `_follow` are the watcher's: which session pushes, and whether this is a refresh
    of an open table (no jump to the front, refused if the table was closed).

    The grid shows in VS Code: the server has the extension open the workspace when nothing
    shows it yet, so pushing again in a loop refreshes one tab instead of opening many.
    `start_server` and `open` no longer do anything (VS Code starts the server and shows the
    grids); they stay so code written for earlier versions still runs.
    """
    name = name or caller_variable_name(data, _frame or _caller()) or "data"
    if not _NAME.fullmatch(name):
        raise ValueError(f"Dataset name {name!r} is not allowed. Use 1 to 64 letters, digits, '_' or '-'.")
    info = ensure_server()
    limit: Any = info.get("max_dataset_bytes")
    kind = _source_kind(data)

    try:
        table = _to_arrow(data)
    except ImportError:
        table = None
    if table is not None:
        # The server measures the same Arrow size, so this check matches its decision.
        if isinstance(limit, int) and table.nbytes > limit:
            raise DatasetTooLargeError(_too_large_message(name, table.nbytes, limit))
        body, content_type = _arrow_bytes(table), "application/vnd.apache.arrow.stream"
    elif kind == "records":
        # No pyarrow (a light project environment): records go as split JSON. Dates arrive as
        # ISO text; install pyarrow to keep real column types.
        body, content_type = _records_json(data), "application/json"  # type: ignore[arg-type]
    else:
        if kind != "pandas":
            raise ImportError(
                "Install pyarrow to push anything other than a pandas DataFrame or a list of dicts."
            )
        frame: Any = data if _has_default_index(data) else data.reset_index()  # type: ignore[attr-defined]
        body = frame.to_json(orient="split", date_format="iso", default_handler=str).encode()
        content_type = "application/json"

    headers = {
        "Authorization": f"Bearer {info['token']}",
        "Content-Type": content_type,
        "X-Source-Kind": kind,
    }
    if _process:
        headers["X-Process"] = _process
    request = urllib.request.Request(
        url=f"http://127.0.0.1:{info['port']}/datasets/{name}?new_grid={int(new_grid)}&follow={int(_follow)}",
        data=body,
        method="PUT",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310 (fixed http://127.0.0.1 URL)
            result: dict[str, Any] = json.loads(response.read())
    except urllib.error.HTTPError as error:
        message = error.read().decode("utf-8", "replace")
        if error.code == 413:
            raise DatasetTooLargeError(message) from error
        if error.code == 409 and _follow:
            raise TableNotOpenError(message) from error
        raise RuntimeError(f"SwissArmyGrid refused the push ({error.code}): {message}") from error
    except urllib.error.URLError as error:
        raise ServerNotRunningError(
            f"No SwissArmyGrid server at port {info['port']} ({error.reason}): it stopped while "
            "this push was on its way. Click SwissArmyGrid in VS Code's status bar to start it again."
        ) from error
    return str(result.get("grid_url") or "")  # a refresh of a table whose tab was closed has none


def _records_json(rows: list[dict[str, Any]]) -> bytes:
    """A list of dicts as {"columns": [...], "data": [[...]]}; a key missing from a row is blank."""
    columns: dict[str, None] = {}
    for row in rows:
        for key in row:
            columns.setdefault(str(key), None)
    names = list(columns)
    data = [[row.get(name) for name in names] for row in rows]
    return json.dumps({"columns": names, "data": data}, default=_json_value).encode()


def _json_value(value: Any) -> Any:
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _caller() -> FrameType | None:
    frame = inspect.currentframe()
    # _caller -> push -> the user's code
    return frame.f_back.f_back if frame and frame.f_back else None


def _arrow_bytes(table: Any) -> bytes:
    import pyarrow.ipc as ipc

    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table, max_chunksize=64_000)
    return sink.getvalue()
