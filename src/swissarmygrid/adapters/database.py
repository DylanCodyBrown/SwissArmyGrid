"""DuckDB: the one in-memory database, locked down; the worker pool its queries run on; and what the
domain asks DuckDB about SQL text.

Open the one in-memory DuckDB database, locked down (plan section 6, rules 4 and 6).
Checked against DuckDB 2.0.0 on 2026-10-21 (see docs/verified.md):
- enable_external_access=false blocks every file and network path from SQL: read_csv, COPY,
  ATTACH, INSTALL, and scanning local Python variables by name. Tables we register
  explicitly from Python (con.register) still work, which is how pushes get in.
- lock_configuration=true makes later SET statements fail, so SQL cannot undo the above.
- temp_directory='' turns off spilling to disk. A query that needs more than memory_limit
  then fails with OutOfMemoryException and the connection keeps working. Without this,
  DuckDB spills into a .tmp folder in the working directory, which would write client data
  to disk.

A small priority worker pool for DuckDB work (plan section 4, process rules).
DuckDB queries block the calling thread, so they run here instead of on the web server's event
loop. Unlike a plain ThreadPoolExecutor the queue is ordered by priority: a grid scrolling to
a new window jumps ahead of a slow profile or an agent query that was queued first. Think of
it as a hand-rolled TaskScheduler with a priority queue behind it.
Callers await `run(priority, fn, *args)`; the result (or exception) comes back on the event
loop. Lower numbers run first (services/ports.py names the priorities).

The domain's two questions about SQL text (domain/steps.Describe, domain/sql_guard.SqlGrammar) are
answered here: `describer(cursor)` gives the columns a query returns, and `duckdb_grammar()` is
DuckDB's own parse of a statement, its keywords and its table functions.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import queue
import threading
from collections.abc import Callable
from typing import Any, TypeVar

import duckdb

from swissarmygrid.domain.sql_guard import SqlGrammar
from swissarmygrid.domain.steps import Describe, EngineError
from swissarmygrid.services.ports import SqlSession

T = TypeVar("T")


def open_database(memory_limit: str) -> duckdb.DuckDBPyConnection:
    """Return the shared connection. Each request should work on its own .cursor()."""
    return duckdb.connect(
        ":memory:",
        config={
            "enable_external_access": False,
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
            "temp_directory": "",
            "memory_limit": memory_limit,
            "lock_configuration": True,
        },
    )


# ===== what the domain asks about SQL text =====


def describer(cursor: SqlSession) -> Describe:
    """The columns a query returns, as (name, type) pairs, asked on `cursor`. DuckDB's refusal comes
    back as the domain's EngineError, carrying DuckDB's own text."""

    def describe(sql: str) -> list[tuple[str, str]]:
        try:
            rows = cursor.execute(f"DESCRIBE {sql}").fetchall()
        except duckdb.Error as error:
            raise EngineError(str(error)) from error
        return [(str(r[0]), str(r[1])) for r in rows]

    return describe


# A private connection that only parses: DuckDB's own reading of a query (json_serialize_sql),
# its list of table functions and its keywords. It holds no data and reaches nothing.
_parser_lock = threading.Lock()
_parser: duckdb.DuckDBPyConnection | None = None
_grammar: SqlGrammar | None = None


def _parser_connection() -> duckdb.DuckDBPyConnection:
    global _parser
    if _parser is None:
        _parser = duckdb.connect(
            ":memory:",
            config={
                "enable_external_access": False,
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
                "lock_configuration": True,
            },
        )
    return _parser


def _parse(sql: str) -> dict[str, Any]:
    """DuckDB's own parse of `sql` (json_serialize_sql), as the JSON it gives."""
    with _parser_lock:
        row = _parser_connection().execute("SELECT json_serialize_sql(?)", [sql]).fetchone()
    parsed: Any = json.loads(str(row[0])) if row else {"error": True}
    return parsed if isinstance(parsed, dict) else {"error": True}


def duckdb_grammar() -> SqlGrammar:
    """What this DuckDB says about SQL text, read once: its table functions that are not also scalar
    or aggregate functions (repeat and range are both), its keywords, and its parser."""
    global _grammar
    with _parser_lock:
        if _grammar is None:
            connection = _parser_connection()
            functions = connection.execute(
                "SELECT function_name FROM duckdb_functions() GROUP BY function_name "
                "HAVING bool_or(function_type IN ('table', 'table_macro', 'pragma')) "
                "AND NOT bool_or(function_type IN ('scalar', 'aggregate', 'macro'))"
            ).fetchall()
            keywords = connection.execute("SELECT keyword_name FROM duckdb_keywords()").fetchall()
            _grammar = SqlGrammar(
                parse=_parse,
                table_functions=frozenset(str(r[0]).lower() for r in functions),
                keywords=frozenset(str(r[0]).lower() for r in keywords),
            )
        return _grammar


# ===== the worker pool =====


_Job = tuple[int, int, Callable[[], Any], asyncio.AbstractEventLoop, "asyncio.Future[Any]"]


class PriorityWorkers:
    """N threads pulling jobs from one priority queue."""

    def __init__(self, threads: int) -> None:
        self._queue: queue.PriorityQueue[_Job | tuple[int, int, None, None, None]] = queue.PriorityQueue()
        # Tiebreak so equal priorities run in arrival order and jobs never compare callables.
        self._sequence = itertools.count()
        self._threads = [
            threading.Thread(target=self._loop, name=f"duckdb-worker-{i}", daemon=True)
            for i in range(threads)
        ]
        for thread in self._threads:
            thread.start()

    async def run(self, priority: int, fn: Callable[..., T], *args: Any) -> T:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        self._queue.put((priority, next(self._sequence), lambda: fn(*args), loop, future))
        return await future

    def close(self) -> None:
        for _ in self._threads:
            # Sentinels sort after every real job, so queued work still finishes.
            self._queue.put((1_000_000, next(self._sequence), None, None, None))

    def _loop(self) -> None:
        while True:
            _, _, job, loop, future = self._queue.get()
            if job is None or loop is None or future is None:
                return
            try:
                result: Any = job()
            except BaseException as error:  # handed back to the awaiting coroutine
                loop.call_soon_threadsafe(_set_exception, future, error)
            else:
                loop.call_soon_threadsafe(_set_result, future, result)


def _set_result(future: asyncio.Future[Any], result: Any) -> None:
    if not future.cancelled():
        future.set_result(result)


def _set_exception(future: asyncio.Future[Any], error: BaseException) -> None:
    if not future.cancelled():
        if isinstance(error, StopIteration):
            # A future refuses StopIteration (TypeError in this callback), and the request that
            # awaits it never answered (a stray next() on a missing column did that).
            error = RuntimeError(f"StopIteration{f': {error}' if str(error) else ''}")
        future.set_exception(error)
