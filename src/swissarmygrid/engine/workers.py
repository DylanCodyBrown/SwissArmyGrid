"""A small priority worker pool for DuckDB work (plan section 4, process rules).

DuckDB queries block the calling thread, so they run here instead of on the web server's event
loop. Unlike a plain ThreadPoolExecutor the queue is ordered by priority: a grid scrolling to
a new window jumps ahead of a slow profile or an agent query that was queued first. Think of
it as a hand-rolled TaskScheduler with a priority queue behind it.

Callers await `run(priority, fn, *args)`; the result (or exception) comes back on the event
loop. Lower numbers run first.
"""

from __future__ import annotations

import asyncio
import itertools
import queue
import threading
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

# Lower runs first.
INTERACTIVE: int = 0  # grid windows and counts
BACKGROUND: int = 5  # profiles, SQL box, exports
AGENT: int = 8  # MCP queries

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
