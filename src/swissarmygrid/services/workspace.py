"""The workspace: the open datasets and grids, and what every use case needs to work on them.

A use case is a method on a service class (services/datasets.py, services/grids.py, ...). Each is
given the `Workspace` (its ports: the store, the views, the worker pool, the audit log, the pages) and
whatever else it alone needs. In the book's terms this is the unit of work the handlers share: the
repositories in one place, handed in by the composition root (bootstrap.py).

It also names the outcomes a use case can end in besides its answer. The doors (server/http.py)
turn them into their own language: an HTTP status, a line in the terminal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from swissarmygrid.domain.datasets import DatasetInfo, mib
from swissarmygrid.domain.grids import Grid, GridRegistry, StaleRequestError
from swissarmygrid.domain.sql_guard import SqlGrammar
from swissarmygrid.domain.steps import CompiledView
from swissarmygrid.services.ports import Audit, Datasets, Jobs, Notifier, SqlSession, Views
from swissarmygrid.settings import Settings

T = TypeVar("T")


class Refused(Exception):
    """Not for this caller: the agent asked for something that is the user's, or the user said no."""


class Conflict(Exception):
    """The request cannot be done as things stand now (a name taken, nothing open to answer)."""


class TimedOut(Exception):
    """The work was stopped: it ran past its time, or was cancelled."""


@dataclass(frozen=True)
class Addresses:
    """Where the pages live. The fragment carries the token to the page without ever sending it to
    the server. `with_token=False` for an agent: the token is the user's credential (a request with
    it and no X-Actor is the user's), so an agent gets the address only."""

    port: int
    token: str

    def grid(self, grid: Grid, with_token: bool = True) -> str:
        address = f"http://127.0.0.1:{self.port}/g/{grid.id}"
        return f"{address}#token={self.token}" if with_token else address

    def workspace(self, grid: Grid | None = None, with_token: bool = True) -> str:
        """The tabbed workspace (one window, a tab per grid), opened on `grid` when given."""
        address = f"http://127.0.0.1:{self.port}/workspace"
        if not with_token:
            return address + (f"#grid={grid.id}" if grid else "")
        focus = f"&grid={grid.id}" if grid else ""
        return f"{address}#token={self.token}{focus}"


@dataclass
class Workspace:
    settings: Settings
    addresses: Addresses
    store: Datasets
    views: Views
    grids: GridRegistry
    live: Notifier
    workers: Jobs
    audit: Audit
    # DuckDB's own reading of SQL text, which the guard checks every query against.
    grammar: SqlGrammar
    # The engine's own errors, named by the composition root so nothing here imports the engine:
    # a query that was interrupted, and any refusal at all.
    interrupted: type[BaseException]
    engine_error: type[Exception]

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
        fn: Callable[[Any], T],
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
        except self.interrupted as error:
            # Interrupted because a newer gen arrived: stale. Otherwise (a timeout) pass it on.
            if grid.is_stale(gen, client):
                raise StaleRequestError() from error
            raise
        finally:
            grid.release(ticket)

    def compiled(self, cursor: SqlSession, grid: Grid, info: DatasetInfo) -> CompiledView:
        return self.views.compile(cursor, info, grid.steps, grid.settings)

    def budget(self) -> dict[str, Any]:
        """How much of the memory budget and of the dataset and grid counts is in use."""
        used, total = self.store.used_bytes, self.store.max_total_bytes
        return {
            "used_bytes": used,
            "total_bytes": total,
            "used": mib(used),
            "total": mib(total),
            "datasets": len(self.store.all()),
            "max_datasets": self.store.max_datasets,
            "grids": len(self.grids.all()),
            "max_grids": self.settings.max_grids,
            "warn": used >= self.settings.warn_fraction * total,
        }
