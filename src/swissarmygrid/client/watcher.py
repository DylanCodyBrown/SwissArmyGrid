"""The watcher: your running Python tells the grid which tables it holds, and loads one when asked.

You write `SOD = trino.execute("...")` and nothing else. A small daemon thread in your Python process
reads the top-level variables of `__main__` (a notebook kernel's variables, a script's globals and a
REPL's are all that one dict), finds the tables among them, and reports their names and sizes to the
grid server. The grid's Open list shows them. Click one and the server asks this thread to push it;
the table then follows its variable, so running the cell again refreshes the open tab in place.

Rows leave your process only when you open a table (and when an open one is refreshed). Reporting a
table sends the server a name, a kind and two counts.

In C# terms it is a background service inside your own process that registers with a local hub, answers
the hub's "send me that one" request, and re-sends it when the object behind the name changes.

What it sees: top-level variables, and the tables inside any DuckDB connection among them (and DuckDB's
default connection). What it cannot: a variable inside a function, a script that has already exited, a
process stopped at a debugger breakpoint (the debugger freezes this thread too). `push` still covers those.

Starts by itself when IPython is running (a notebook, the interactive window) or in a marimo notebook
(SwissArmyGrid's own notebook tab, whose variables marimo keeps outside `__main__`), because importing
swissarmygrid there is already the choice to use the grid. In a plain script call `sag.watch()`.
SWISSARMYGRID_WATCH=0 switches it off everywhere and =1 turns it on everywhere.

Standard library only. Pandas, Arrow, DuckDB and the like are recognised by their class names, so
nothing here imports them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any

from swissarmygrid.client import push_client

log = logging.getLogger("swissarmygrid.watcher")

_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Names IPython puts in the user namespace.
_SKIP_NAMES = frozenset({"In", "Out", "exit", "quit", "get_ipython"})
_CONNECTION_QUERY = (
    "SELECT table_name, estimated_size, column_count FROM duckdb_tables() "
    "WHERE NOT internal AND schema_name = 'main'"
)
_BACKOFF_SECONDS = 3.0
_MAX_ERRORS = 5


# ===== which variables are tables =====


@dataclass(frozen=True)
class Table:
    """One table the session offers: what the server hears, plus a fingerprint that changes when the
    object behind the name is replaced or resized (the watcher compares it to know when to refresh)."""

    name: str
    kind: str
    rows: int | None
    columns: int | None
    fingerprint: str

    def entry(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "rows": self.rows, "columns": self.columns}


def _root(cls: type) -> str:
    """The top-level package a class comes from. DuckDB 2.0 reports its connection class from the
    private module `_duckdb` (1.x said `duckdb`), so leading underscores are ignored."""
    return cls.__module__.lstrip("_").partition(".")[0]


# What _class_kind said about each class (CONNECTION for a DuckDB connection). The answer depends on
# the class alone and a namespace holds many values of few classes, so a scan is one dict lookup per
# variable. A plain dict, read without a lock (one dict.get is atomic): it holds its classes, so it
# is emptied when it grows past _KINDS_MAX (classes redefined cell after cell would pile up).
CONNECTION = "connection"
_KINDS: dict[type, str | None] = {}
_KINDS_MAX = 4096
_UNSEEN = object()


def _class_kind_cached(cls: type) -> str | None:
    try:
        known = _KINDS.get(cls, _UNSEEN)
    except TypeError:  # an unhashable class (a metaclass with __eq__ and no __hash__)
        return _class_kind(cls)
    if known is not _UNSEEN:
        return known  # type: ignore[return-value]
    kind = _class_kind(cls)
    if len(_KINDS) >= _KINDS_MAX:
        _KINDS.clear()
    _KINDS[cls] = kind
    return kind


def _kind(value: object) -> str | None:
    """What sort of table `value` is, judged by its class only. The class, never the instance:
    asking an instance can run its __getattr__ (mocks, lazy proxies), and nothing here should."""
    kind = _class_kind_cached(type(value))
    return None if kind == CONNECTION else kind


def _class_kind(cls: type) -> str | None:
    """`_kind` for a class. A class that errors when asked (a metaclass __getattr__ that raises, a
    __module__ that is not text) is not a table; the error never stops the scan of the others."""
    try:
        return _table_kind(cls)
    except Exception:
        return None


def _table_kind(cls: type) -> str | None:
    root = _root(cls)
    if root == "pandas" and cls.__name__ == "DataFrame":
        return "dataframe"
    if root == "polars" and cls.__name__ == "DataFrame":
        return "polars"
    if root == "pyarrow" and cls.__name__ == "Table":
        return "arrow"
    if root == "duckdb" and cls.__name__ == "DuckDBPyRelation":
        return "relation"
    if root == "duckdb" and cls.__name__ == "DuckDBPyConnection":
        return CONNECTION
    if hasattr(cls, "__arrow_c_stream__"):  # a query client's result, and anything else Arrow can read
        return "arrow"
    return None


def _shape(value: Any, kind: str) -> tuple[int | None, int | None]:
    """Row and column counts, from what the object already knows. Never runs a query."""
    try:
        if kind == "dataframe":
            return len(value), len(value.columns)
        if kind == "polars":
            return int(value.height), int(value.width)
        if kind == "relation":
            return None, len(value.columns)
        num_rows = getattr(value, "num_rows", None)
        if isinstance(num_rows, int):
            return num_rows, int(value.num_columns)
        rows, columns = getattr(value, "rows", None), getattr(value, "columns", None)
        if isinstance(rows, list) and isinstance(columns, list | tuple):
            return len(rows), len(columns)
    except Exception:  # a size we cannot read is just unknown
        log.debug("could not read the shape of a %s", kind, exc_info=True)
    return None, None


def _is_connection(value: object) -> bool:
    return _class_kind_cached(type(value)) == CONNECTION


def _default_connection() -> Any | None:
    """DuckDB's default connection (what duckdb.sql uses), only when duckdb is already imported."""
    duckdb = sys.modules.get("duckdb")
    getter = getattr(duckdb, "default_connection", None)
    try:
        return getter() if callable(getter) else getter
    except Exception:
        return None


def _connection_tables(connection: Any) -> list[Table]:
    """The tables in a DuckDB connection, read through a cursor so the user's connection is untouched."""
    try:
        cursor = connection.cursor()
        try:
            found = cursor.execute(_CONNECTION_QUERY).fetchall()
        finally:
            cursor.close()
    except Exception:  # closed, or busy in a way a cursor cannot share: skip it this round
        return []
    tables: list[Table] = []
    for name, size, columns in found:
        name = str(name)
        if _NAME.fullmatch(name):
            rows = int(size) if size is not None else None
            tables.append(
                Table(name, "table", rows, int(columns), f"{id(connection)}:{name}:{rows}:{columns}")
            )
    return tables


def _connections(namespace: dict[str, Any], found: list[Any] | None = None) -> list[Any]:
    """The DuckDB connections among the variables (or `found`, already picked out), plus DuckDB's
    default one."""
    found = [v for v in list(namespace.values()) if _is_connection(v)] if found is None else list(found)
    default = _default_connection()
    if default is not None and all(default is not c for c in found):
        found.append(default)
    return found


def find_tables(namespace: dict[str, Any]) -> list[Table]:
    """The tables among a namespace's variables, then those inside its DuckDB connections. A variable
    wins over a connection table of the same name. Sorted by name so a repeated scan compares equal."""
    found: list[Table] = []
    connections: list[Any] = []
    kinds, unseen = _KINDS, _UNSEEN  # locals: this loop runs once per variable, twice a second
    for name, value in list(namespace.items()):
        cls = type(value)
        try:
            kind = kinds.get(cls, unseen)
        except TypeError:
            kind = unseen
        if kind is unseen:
            kind = _class_kind_cached(cls)
        if kind is None:  # nearly every variable: a number, a string, a function, a module...
            continue
        if kind == CONNECTION:
            connections.append(value)
            continue
        if (
            not isinstance(name, str)
            or name.startswith("_")
            or name in _SKIP_NAMES
            or not _NAME.fullmatch(name)
        ):
            continue
        rows, columns = _shape(value, kind)  # type: ignore[arg-type]
        found.append(Table(name, kind, rows, columns, f"{id(value)}:{rows}:{columns}"))  # type: ignore[arg-type]
    taken = {t.name for t in found}
    for connection in _connections(namespace, connections):
        for table in _connection_tables(connection):
            if table.name not in taken:
                taken.add(table.name)
                found.append(table)
    return sorted(found, key=lambda t: (t.name.lower(), t.name))


def resolve(namespace: dict[str, Any], name: str) -> Any | None:
    """What to push for a table name: the variable itself, or a connection table read as Arrow.
    None when the name no longer holds a table."""
    value = namespace.get(name)
    if value is not None and not name.startswith("_") and _kind(value) is not None:
        return value
    for connection in _connections(namespace):
        if any(t.name == name for t in _connection_tables(connection)):
            cursor = connection.cursor()
            try:
                return cursor.sql(f'SELECT * FROM "{name}"').to_arrow_table()  # noqa: S608 (name matched _NAME: no quote in it)
            finally:
                cursor.close()
    return None


def label_for(namespace: dict[str, Any]) -> str:
    """How the Open list names this session: the notebook file (VS Code sets __vsc_ipynb_file__ in a
    notebook's kernel), else the script's file, else the process."""
    for key in ("__vsc_ipynb_file__", "__file__"):
        value = namespace.get(key)
        if isinstance(value, str) and value:
            return os.path.basename(value.replace("\\", "/"))
    return f"Python {os.getpid()}"


# ===== the two threads =====


class _ServerDown(Exception):
    """No server answered; try again after a pause."""


class _Gone(Exception):
    """The server does not know this session (yet, or any more)."""


class Watcher:
    """Reports a session's tables and pushes the ones the grid asks for. Two daemon threads:

    - the scanner looks at the variables every `scan_seconds`, reports changes (and a heartbeat every
      `heartbeat_seconds`), and refreshes the open tables whose variable was assigned again;
    - the listener long-polls the server (up to `poll_seconds` per request) for "load that one".

    Neither raises into your session. With no server running they wait and try again.
    """

    def __init__(
        self,
        namespace: Callable[[], dict[str, Any]],
        label: str | None = None,
        *,
        scan_seconds: float = 0.5,
        heartbeat_seconds: float = 10.0,
        poll_seconds: float = 25.0,
    ) -> None:
        self.id = uuid.uuid4().hex[:16]
        self._namespace = namespace
        self._label = label
        self._scan_seconds = scan_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._resync = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()  # guards _pushed and _errors
        self._push_lock = threading.Lock()  # one push at a time
        self._pushed: dict[str, str] = {}  # table name -> fingerprint of what was last sent
        self._errors: list[dict[str, str]] = []

    # ----- lifecycle

    def start(self) -> None:
        if self._threads:
            return
        for name, target in (
            ("scan", self._scan_loop),
            ("listen", self._listen_loop),
        ):
            thread = threading.Thread(target=target, name=f"swissarmygrid-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()

    def alive(self) -> bool:
        return bool(self._threads) and not self._stop.is_set() and all(t.is_alive() for t in self._threads)

    # ----- loading

    def load(self, name: str, follow: bool) -> None:
        """Push one table to the server as the dataset `name`. `follow` is a refresh of a table that is
        already open. A failure the user should hear about (too large, say) is queued for the next sync."""
        with self._push_lock:
            namespace = self._namespace()
            table = next((t for t in find_tables(namespace) if t.name == name), None)
            value = resolve(namespace, name) if table is not None else None
            if table is None or value is None:
                self._fail(name, f"{name} is not a table in this Python session any more.")
                return
            try:
                push_client.push(value, name=name, _process=self.id, _follow=follow)
            except push_client.TableNotOpenError:
                with self._lock:
                    self._pushed.pop(name, None)
                self._resync.set()  # learn what is still open before trying again
                return
            except push_client.ServerNotRunningError as error:
                raise _ServerDown from error
            except Exception as error:  # too large, refused, a table Arrow could not convert
                self._fail(name, str(error))
            # Success or a reported failure: either way this is the object we are done with, so a
            # table that cannot be sent is not retried until its variable is assigned again.
            with self._lock:
                self._pushed[name] = table.fingerprint

    def _fail(self, name: str, message: str) -> None:
        with self._lock:
            if len(self._errors) < _MAX_ERRORS:
                self._errors.append({"name": name, "message": message})
        self._resync.set()

    def take_errors(self) -> list[dict[str, str]]:
        with self._lock:
            taken, self._errors = self._errors, []
        return taken

    # ----- talking to the server

    def _call(self, method: str, path: str, body: Any = None, timeout: float = 10.0) -> Any:
        info = push_client._read_info()  # re-read every time: the server may have restarted elsewhere
        if info is None:
            raise _ServerDown
        request = urllib.request.Request(
            f"http://127.0.0.1:{info['port']}{path}",
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed http://127.0.0.1 URL)
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise _Gone from error
            raise
        except (urllib.error.URLError, OSError) as error:
            raise _ServerDown from error

    def _sync(self, entries: list[dict[str, Any]]) -> list[str]:
        """Report the tables; the reply names the ones the server holds from this session."""
        errors = self.take_errors()
        body: dict[str, Any] = {
            "label": self._label or label_for(self._namespace()),
            "pid": os.getpid(),
            "tables": entries,
        }
        if errors:
            body["errors"] = errors
        try:
            reply = self._call("POST", f"/processes/{self.id}/sync", body)
        except BaseException:
            with self._lock:
                self._errors[:0] = errors  # tell the page next time
            raise
        return [str(name) for name in reply.get("following", [])]

    def _scan_loop(self) -> None:
        sent: list[dict[str, Any]] | None = None
        sent_at = 0.0
        following: list[str] = []
        while not self._stop.wait(self._scan_seconds):
            try:
                tables = find_tables(self._namespace())
                entries = [t.entry() for t in tables]
                now = monotonic()
                if entries != sent or now - sent_at >= self._heartbeat_seconds or self._resync.is_set():
                    self._resync.clear()
                    following = self._sync(entries)
                    sent, sent_at = entries, now
                current = {t.name: t.fingerprint for t in tables}
                for name in following:
                    with self._lock:
                        last = self._pushed.get(name)
                    if name in current and last != current[name]:
                        self.load(name, follow=True)
            except _ServerDown:
                sent, following = None, []
                self._stop.wait(_BACKOFF_SECONDS)
            except Exception:
                log.debug("the scan failed", exc_info=True)

    def _listen_loop(self) -> None:
        while not self._stop.is_set():
            try:
                reply = self._call(
                    "GET",
                    f"/processes/{self.id}/requests?wait={self._poll_seconds}",
                    timeout=self._poll_seconds + 15,
                )
                for request in reply.get("requests", []):
                    if request.get("action") == "load" and isinstance(request.get("name"), str):
                        self.load(request["name"], follow=False)
            except _Gone:
                self._stop.wait(1.0)  # not announced yet, or expired: the scanner's next sync fixes it
            except _ServerDown:
                self._stop.wait(_BACKOFF_SECONDS)
            except Exception:
                log.debug("the listener failed", exc_info=True)
                self._stop.wait(1.0)


# ===== the one watcher per process =====

_watcher: Watcher | None = None
_guard = threading.Lock()


def _main_namespace() -> dict[str, Any]:
    main = sys.modules.get("__main__")
    return vars(main) if main is not None else {}


def start(namespace: Callable[[], dict[str, Any]] | None = None) -> Watcher:
    """Start watching this process (once; calling it again returns the same watcher)."""
    global _watcher
    with _guard:
        if _watcher is None or not _watcher.alive():
            _watcher = Watcher(namespace or _main_namespace)
            _watcher.start()
        return _watcher


def stop() -> None:
    global _watcher
    with _guard:
        if _watcher is not None:
            _watcher.stop()
            _watcher = None


def _in_ipython() -> bool:
    getter = getattr(sys.modules.get("IPython"), "get_ipython", None)
    try:
        return callable(getter) and getter() is not None
    except Exception:
        return False


def _marimo_namespace() -> Callable[[], dict[str, Any]] | None:
    """In a marimo notebook's kernel, its variables. marimo keeps them in a dict of its own, not in
    `__main__`, and hands it out only on the kernel's thread (a thread-local context), so it is read
    here, at import, which a cell does on that thread, and kept for the watcher's threads. None
    anywhere else, and with a marimo that no longer has this (private) context."""
    if "marimo" not in sys.modules:
        return None
    try:
        from marimo._runtime.context.kernel_context import KernelRuntimeContext
        from marimo._runtime.context.types import get_context

        context = get_context()
        if not isinstance(context, KernelRuntimeContext):
            return None
        variables: dict[str, Any] = context.globals
    except Exception:
        return None
    return lambda: variables


def autostart() -> None:
    """Called when swissarmygrid is imported. Starts the watcher in a notebook (IPython or marimo) or the
    interactive window, where importing the package is already the decision to use the grid; never in the
    server, the MCP or a test run. SWISSARMYGRID_WATCH=0 or 1 overrides."""
    mode = os.environ.get("SWISSARMYGRID_WATCH", "")
    if mode == "0":
        return
    marimo = _marimo_namespace()
    if marimo is not None:
        start(marimo)  # the notebook tab's tables become grids (plan, phase 2)
    elif mode == "1" or _in_ipython():
        start()
