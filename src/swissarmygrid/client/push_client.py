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

import http.client
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
    "SwissArmyGrid isn't running. Start it with the swissarmygrid command (installed with uv), or "
    "VS Code's Open SwissArmyGrid button."
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
    port, token = info.get("port"), info.get("token")
    # A half-written or hand-edited file: no server to talk to (and no InvalidURL for the user).
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 0 < port < 65536
        or not isinstance(token, str)
    ):
        return False
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310 (fixed http://127.0.0.1 URL)
            return bool(response.status == 200)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
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


SUPPORTED: str = (
    "push accepts a pandas DataFrame or Series, a Polars DataFrame, a PyArrow table or record batch, "
    "a DuckDB relation, anything that speaks the Arrow C stream interface, a dict of columns, "
    "or a list of dicts"
)


def _to_arrow(data: object) -> Any:
    """Any supported input as a pyarrow Table. Raises ImportError without pyarrow."""
    import pyarrow as pa

    kind = _source_kind(data)
    table: Any
    if kind == "pandas":
        table = _pandas_to_arrow(_prepared_frame(data))
    elif kind == "records":
        table = _records_to_arrow(data)  # type: ignore[arg-type]
    elif kind == "duckdb":
        table = data.to_arrow_table()  # type: ignore[attr-defined]
    elif isinstance(data, pa.Table):
        table = data
    elif isinstance(data, pa.RecordBatch):
        table = pa.Table.from_batches([data])
    elif hasattr(type(data), "__arrow_c_stream__"):
        # Read the stream directly. pa.table() would treat a tuple subclass that speaks Arrow (a
        # query client's NamedTuple result, say) as a list of arrays and fail before it looked for the stream.
        table = pa.RecordBatchReader.from_stream(data).read_all()
    else:
        # Polars, a dict of columns, and anything else pyarrow can turn into a table.
        try:
            table = pa.table(data)
        except (TypeError, ValueError, pa.ArrowException) as error:
            raise TypeError(f"{SUPPORTED}; this is a {type(data).__name__}.") from error
    if table.num_columns == 0:
        raise ValueError("The table has no columns, so there is nothing to show.")
    return table


_ARROW_REFUSED_NAMES = ("ArrowInvalid", "ArrowTypeError", "ArrowNotImplementedError")


def _unique_names(names: list[str], taken: set[str] | None = None) -> list[str]:
    """`names` with repeats suffixed .1, .2 (as pandas.read_csv does). Case is ignored, as DuckDB
    ignores it: "ID" and "id" are the same column there."""
    seen = set() if taken is None else taken
    unique: list[str] = []
    for name in names:
        candidate, n = name, 0
        while candidate.casefold() in seen:
            n += 1
            candidate = f"{name}.{n}"
        seen.add(candidate.casefold())
        unique.append(candidate)
    return unique


def _prepared_frame(data: Any) -> Any:
    """A pandas object as a DataFrame the grid can hold: a Series as one column; a meaningful index
    as columns; flattened MultiIndex columns; unique text names (the real columns keep theirs, an
    index that clashes takes the suffix); periods as text (Arrow would keep only their ordinals)."""
    import pandas as pd

    if isinstance(data, pd.Series):
        data = data.to_frame(name="value" if data.name is None else data.name)
    if not isinstance(data, pd.DataFrame):
        raise TypeError(f"{SUPPORTED}; this is a pandas {type(data).__name__}.")
    if isinstance(data.columns, pd.MultiIndex):
        names = ["_".join(str(part) for part in key if str(part) != "") for key in data.columns]
    else:
        names = [str(name) for name in data.columns]
    names = _unique_names(names)
    # A shallow copy: never the caller's frame (its names change below), and no data copied, on
    # pandas 2 as on 3 (set_axis copies the data on pandas 2 unless copy-on-write is on).
    frame: Any = data.copy(deep=False)
    frame.columns = names
    if not _has_default_index(frame):
        levels = list(frame.index.names)
        index_names = [
            str(level) if level is not None else ("index" if len(levels) == 1 else f"level_{i}")
            for i, level in enumerate(levels)
        ]
        index_names = _unique_names(index_names, {n.casefold() for n in names})
        frame = frame.reset_index(allow_duplicates=True)
        frame.columns = index_names + names
    for position, dtype in enumerate(frame.dtypes):
        if isinstance(dtype, pd.PeriodDtype):
            column = frame.iloc[:, position]
            frame.isetitem(position, column.astype(str).where(column.notna(), None))
    return frame


def _pandas_to_arrow(frame: Any) -> Any:
    """A prepared frame as Arrow. A column Arrow refuses (an object column mixing numbers and text,
    complex numbers) goes as text, so one odd column never stops the push of the others."""
    import pyarrow as pa

    try:
        return pa.Table.from_pandas(frame, preserve_index=False)
    except pa.ArrowException as error:
        if type(error).__name__ not in _ARROW_REFUSED_NAMES:
            raise
    arrays: dict[str, Any] = {}
    for position, name in enumerate(frame.columns):
        column = frame.iloc[:, position]
        try:
            arrays[name] = pa.Table.from_pandas(column.to_frame(name=name), preserve_index=False).column(0)
        except pa.ArrowException:
            arrays[name] = pa.array([None if _blank(v) else str(v) for v in column], type=pa.string())
    return pa.table(arrays)


def _records_to_arrow(rows: list[Any]) -> Any:
    """A list of dicts as Arrow, every key a column (pyarrow's from_pylist reads the first row's keys
    only, and dropped the rest). A key missing from a row is blank; a mixed column goes as text."""
    import pyarrow as pa

    if not all(isinstance(row, dict) for row in rows):
        raise TypeError("A list to push must be a list of dicts, one per row ({column: value}).")
    names = list(dict.fromkeys(str(key) for row in rows for key in row))
    unique = _unique_names(names)
    texts = [{str(k): v for k, v in row.items()} for row in rows]
    arrays: dict[str, Any] = {}
    for name, column in zip(unique, names, strict=True):
        values = [row.get(column) for row in texts]
        try:
            arrays[name] = pa.array(values)
        except pa.ArrowException:
            arrays[name] = pa.array([None if v is None else str(v) for v in values], type=pa.string())
    return pa.table(arrays)


def _blank(value: Any) -> bool:
    import pandas as pd

    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return missing is True or (type(missing).__name__ == "bool_" and bool(missing))


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
        frame: Any = _prepared_frame(data)
        if frame.shape[1] == 0:
            raise ValueError("The table has no columns, so there is nothing to show.")
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
    except TimeoutError as error:
        raise ServerNotRunningError(
            f"The SwissArmyGrid server at port {info['port']} did not answer within "
            f"{_TIMEOUT_SECONDS:.0f} seconds."
        ) from error
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        # Refused, reset, or closed without a reply (RemoteDisconnected): the server went away.
        reason = getattr(error, "reason", None) or error
        raise ServerNotRunningError(
            f"No SwissArmyGrid server at port {info['port']} ({reason}): it stopped while "
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
