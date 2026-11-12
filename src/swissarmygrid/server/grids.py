"""Grids: views of a dataset, each with its own address /g/<id> (plan section 4, "Grid").

A grid owns its step list and toolbar settings; many grids can share one dataset. Each page
showing the grid has its own request generation (`gen`, plan section 5.2), keyed by a client id
the page picks at random: the page bumps gen whenever its view changes, and a request carrying
an older gen from the same page is stale, so the server drops it before it runs and interrupts
it if it is already running. Rapid typing in a filter box therefore never queues more than one
live query per page (plan section 14). Generations are per page, not per grid: two tabs on one
grid must never make each other's requests look stale.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from typing import Any

import duckdb

from swissarmygrid.engine.steps import Step, ViewSettings


class GridLimitError(ValueError):
    """Too many grids are open. Maps to HTTP 409."""


class StaleRequestError(Exception):
    """A newer request for the same grid replaced this one. Maps to HTTP 409."""


@dataclass
class Grid:
    id: str
    dataset: str
    steps: list[Step] = field(default_factory=list)
    settings: ViewSettings = field(default_factory=ViewSettings)
    gen: int = 0  # the highest gen seen from any page (informational)
    _gens: dict[str, int] = field(default_factory=dict)  # client id -> latest gen
    # Rows to mark (agent or user), as filter conditions: [{"column", "op", ..., "label"}].
    highlights: list[dict[str, Any]] = field(default_factory=list)
    # How many rows the user has selected (agents may see the count, never the values).
    selected: int = 0
    # Ids of the steps an agent added or last changed (server/level_zero.py: at level 0 a grid
    # whose agent steps can hold values the agent chose is not compared against the user's data).
    agent_steps: set[str] = field(default_factory=set)
    # Cursors running a query for this grid, with the gen they serve.
    _running: dict[int, tuple[str, int, duckdb.DuckDBPyConnection]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def admit(self, gen: int | None, cursor: duckdb.DuckDBPyConnection, client: str = "") -> int:
        """Register a query. A newer gen from a page interrupts that page's older running
        queries; an older gen from the same page is refused."""
        with self._lock:
            latest = self._gens.get(client, 0)
            if gen is not None and gen < latest:
                raise StaleRequestError()
            if gen is not None and gen > latest:
                self._gens[client] = gen
                self.gen = max(self.gen, gen)
                for old_client, old_gen, old_cursor in list(self._running.values()):
                    if old_client == client and old_gen < gen:
                        old_cursor.interrupt()
            ticket = id(cursor)
            self._running[ticket] = (client, gen if gen is not None else latest, cursor)
            return ticket

    def is_stale(self, gen: int | None, client: str = "") -> bool:
        return gen is not None and gen < self._gens.get(client, 0)

    def interrupt_all(self) -> int:
        """The SQL box's Cancel: stop every query running for this grid."""
        with self._lock:
            running = list(self._running.values())
        for _, _, cursor in running:
            cursor.interrupt()
        return len(running)

    def release(self, ticket: int) -> None:
        with self._lock:
            self._running.pop(ticket, None)


class GridRegistry:
    """In-memory map of grid id to grid."""

    def __init__(self, max_grids: int) -> None:
        self._grids: dict[str, Grid] = {}
        self._max_grids = max_grids

    def create(
        self, dataset: str, steps: list[Step] | None = None, settings: ViewSettings | None = None
    ) -> Grid:
        if len(self._grids) >= self._max_grids:
            raise GridLimitError(f"{len(self._grids)} grids are open, the most allowed. Close one first.")
        grid = Grid(
            id=secrets.token_hex(4),
            dataset=dataset,
            steps=list(steps or []),
            settings=settings or ViewSettings(),
        )
        self._grids[grid.id] = grid
        return grid

    def get(self, grid_id: str) -> Grid | None:
        return self._grids.get(grid_id)

    def first_for(self, dataset: str) -> Grid | None:
        """The oldest grid on `dataset`, which a plain re-push reuses."""
        return next((grid for grid in self._grids.values() if grid.dataset == dataset), None)

    def for_dataset(self, dataset: str) -> list[Grid]:
        return [g for g in self._grids.values() if g.dataset == dataset]

    def close(self, grid_id: str) -> Grid | None:
        return self._grids.pop(grid_id, None)

    def all(self) -> list[Grid]:
        return list(self._grids.values())
