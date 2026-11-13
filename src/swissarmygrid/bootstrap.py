"""The composition root: the one place that builds the concrete adapters and hands them to the
service layer (the book's bootstrap script).

`bootstrap()` opens the locked database, starts the worker pool, and builds the stores (datasets,
views, catalogs, blades), the state files (audit log, recipes), the live updates and the notebook.
Each use case gets these as its ports; no service or domain file imports an adapter. The doors
(server/app.py and its route modules) are given the result and nothing else.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import duckdb

from swissarmygrid.adapters.blades import BladeStore
from swissarmygrid.adapters.catalogs import CatalogStore
from swissarmygrid.adapters.codegen import Verifier
from swissarmygrid.adapters.database import PriorityWorkers, duckdb_grammar, open_database
from swissarmygrid.adapters.datasets import DatasetStore
from swissarmygrid.adapters.live_updates import LiveUpdates
from swissarmygrid.adapters.state_files import AuditLog
from swissarmygrid.adapters.views import ViewEngine
from swissarmygrid.domain.datasets import DatasetInfo
from swissarmygrid.domain.grids import Grid, GridRegistry
from swissarmygrid.domain.sql_guard import SqlGrammar
from swissarmygrid.domain.steps import CompiledView
from swissarmygrid.server.notebook import NotebookServer
from swissarmygrid.server.processes import ProcessRegistry
from swissarmygrid.services.ports import CodeVerifier
from swissarmygrid.services.workspace import Addresses, Workspace
from swissarmygrid.settings import Settings

T = TypeVar("T")


@dataclass
class Services:
    """What the doors are given: the workspace, and the adapters the entry point itself drives (the
    sockets of the live updates, the worker pool and the notebook it stops on the way out)."""

    ws: Workspace
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
    # Source databases' names and types (adapters/catalogs.py) and the saved blades (adapters/blades.py).
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
        return self.ws.addresses.grid(grid, with_token)

    def workspace_url(self, grid: Grid | None = None, with_token: bool = True) -> str:
        return self.ws.addresses.workspace(grid, with_token)

    def grid_and_info(self, grid_id: str) -> tuple[Grid, DatasetInfo]:
        return self.ws.grid_and_info(grid_id)

    async def grid_query(
        self, grid: Grid, gen: int | None, priority: int, fn: Callable[[Any], T], client: str = ""
    ) -> T:
        return await self.ws.grid_query(grid, gen, priority, fn, client)

    def compiled(self, cursor: Any, grid: Grid, info: DatasetInfo) -> CompiledView:
        return self.ws.compiled(cursor, grid, info)


def bootstrap(
    port: int,
    token: str,
    settings: Settings | None = None,
    on_shutdown: Callable[[], None] | None = None,
    audit_log: bool = True,
) -> Services:
    """Build everything for a server listening on 127.0.0.1:`port` that accepts `token`."""
    settings = settings or Settings()
    connection = open_database(settings.duckdb_memory_limit)
    workers = PriorityWorkers(settings.worker_threads)
    store = DatasetStore(
        connection, settings.max_dataset_bytes, settings.max_total_bytes, settings.max_datasets
    )
    # Derived view tables may use a quarter of the data budget; they are evicted freely.
    views = ViewEngine(connection, settings.max_total_bytes // 4)
    grids = GridRegistry(settings.max_grids)
    live = LiveUpdates()
    audit = AuditLog(settings.state_dir / "audit.log" if audit_log else None)
    grammar = duckdb_grammar()
    ws = Workspace(
        settings=settings,
        addresses=Addresses(port, token),
        store=store,
        views=views,
        grids=grids,
        live=live,
        workers=workers,
        audit=audit,
        grammar=grammar,
        interrupted=duckdb.InterruptException,
        engine_error=duckdb.Error,
    )
    return Services(
        ws=ws,
        port=port,
        token=token,
        settings=settings,
        store=store,
        views=views,
        grids=grids,
        live=live,
        workers=workers,
        audit=audit,
        on_shutdown=on_shutdown or (lambda: None),
        processes=ProcessRegistry(),
        catalogs=CatalogStore(connection, settings.max_catalogs, settings.max_catalog_rows),
        blades=BladeStore(
            settings.blades_dir,
            folders_file=settings.state_dir / "blade_folders.json",
            pins_file=settings.state_dir / "blade_pins.json",
        ),
        notebook=NotebookServer(settings.state_dir / "notebooks"),
        notebook_cookie=secrets.token_urlsafe(32),
        grammar=grammar,
        verifier=Verifier(settings.duckdb_memory_limit),
    )
