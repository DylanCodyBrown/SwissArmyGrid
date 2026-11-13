"""The composition root: the one place that builds the concrete adapters and hands them to the
service layer (the book's bootstrap script).

`bootstrap()` opens the locked database, starts the worker pool, and builds the stores (datasets,
views, catalogs, blades), the state files (audit log, recipes, the model library), the live updates
and the notebook. Each service gets these as its ports; no service or domain file imports an adapter.
The doors (server/app.py and its route modules) are given the services and nothing else.
"""

from __future__ import annotations

import os
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import duckdb

from swissarmygrid.adapters import blade_run
from swissarmygrid.adapters import datasets as table_reader
from swissarmygrid.adapters import export as export_writer
from swissarmygrid.adapters.blades import BladeStore
from swissarmygrid.adapters.catalogs import CatalogStore
from swissarmygrid.adapters.codegen import Verifier
from swissarmygrid.adapters.database import PriorityWorkers, duckdb_grammar, open_database
from swissarmygrid.adapters.datasets import DatasetStore
from swissarmygrid.adapters.live_updates import LiveUpdates
from swissarmygrid.adapters.packs import PackFolders
from swissarmygrid.adapters.state_files import AuditLog, ModelFiles, RecipeFiles, is_own_file
from swissarmygrid.adapters.views import ViewEngine
from swissarmygrid.domain.blades import Blade
from swissarmygrid.domain.grids import GridRegistry
from swissarmygrid.server import blade_runner
from swissarmygrid.server.blade_templates import TEMPLATES
from swissarmygrid.server.notebook import DEFAULT_NOTEBOOK, NotebookControl, NotebookServer
from swissarmygrid.services.agent import AgentService, Approvals, ExportTickets
from swissarmygrid.services.analysis import Analysis
from swissarmygrid.services.blades import BladeService
from swissarmygrid.services.catalogs import CatalogService
from swissarmygrid.services.charts import Cards
from swissarmygrid.services.codegen import CodeService
from swissarmygrid.services.datasets import DatasetService
from swissarmygrid.services.export import Exports
from swissarmygrid.services.grids import GridService
from swissarmygrid.services.notebook import NotebookCells
from swissarmygrid.services.packs import Packs
from swissarmygrid.services.processes import ProcessRegistry, Sessions
from swissarmygrid.services.profile import Profiles
from swissarmygrid.services.relationships import Compares, Finder, KeyFinder, Models
from swissarmygrid.services.snapshots import Snapshots
from swissarmygrid.services.sql import SqlBox
from swissarmygrid.services.workspace import Addresses, Workspace
from swissarmygrid.settings import Settings


@dataclass
class Services:
    """What the doors are given: the use cases, one service per feature, and the three adapters the
    entry point drives itself (the sockets of the live updates, the worker pool it closes and the
    notebook it starts, passes requests to and stops on the way out)."""

    settings: Settings
    ws: Workspace
    datasets: DatasetService
    grids: GridService
    profiles: Profiles
    analysis: Analysis
    sql: SqlBox
    exports: Exports
    codes: CodeService
    cards: Cards
    agent: AgentService
    sessions: Sessions
    catalogs: CatalogService
    blades: BladeService
    notebook_cells: NotebookCells
    models: Models
    finder: Finder
    packs: Packs
    keys: KeyFinder
    compares: Compares
    snapshots: Snapshots
    live: LiveUpdates
    workers: PriorityWorkers
    # The notebook (marimo), started on first use, and the cookie its frame is let in with.
    notebook: NotebookServer
    notebook_cookie: str


def bootstrap(
    port: int,
    token: str,
    settings: Settings | None = None,
    on_shutdown: Callable[[], None] | None = None,
    audit_log: bool = True,
) -> Services:
    """Build everything for a server listening on 127.0.0.1:`port` that accepts `token`."""
    settings = settings or Settings()
    state_dir, export_dir = settings.state_dir, settings.export_dir

    # ===== the adapters =====
    connection = open_database(settings.duckdb_memory_limit)
    workers = PriorityWorkers(settings.worker_threads)
    store = DatasetStore(
        connection, settings.max_dataset_bytes, settings.max_total_bytes, settings.max_datasets
    )
    live = LiveUpdates()
    # Packs: installed Claude Code plugins that carry blades and tangs, and editable clones in the
    # blade folders. Their blades and tangs join the blade store and the tang library read-only.
    pack_files = PackFolders(
        settings.claude_dir,
        shared=lambda: [f["path"] for f in blade_store.folders() if f["exists"]],
        environ=os.environ,
    )
    blade_store = BladeStore(
        settings.blades_dir,
        folders_file=state_dir / "blade_folders.json",
        pins_file=state_dir / "blade_pins.json",
        packs=pack_files.blade_dirs,
    )
    recipes = RecipeFiles(state_dir / "recipes")
    verifier = Verifier(settings.duckdb_memory_limit)
    notebook = NotebookServer(state_dir / "notebooks")

    async def run_blade(blade: Blade, mode: str, typed: dict[str, str]) -> dict[str, Any]:
        job = blade_runner.payload(blade_store, blade, mode, typed)
        return await blade_runner.run(job, settings.blade_timeout_seconds)

    # ===== the workspace: the ports every use case shares =====
    ws = Workspace(
        settings=settings,
        addresses=Addresses(port, token),
        store=store,
        # Derived view tables may use a quarter of the data budget; they are evicted freely.
        views=ViewEngine(connection, settings.max_total_bytes // 4),
        grids=GridRegistry(settings.max_grids),
        live=live,
        workers=workers,
        audit=AuditLog(state_dir / "audit.log" if audit_log else None),
        grammar=duckdb_grammar(),
        interrupted=duckdb.InterruptException,
        engine_error=duckdb.Error,
    )

    # ===== the services =====
    processes = ProcessRegistry()  # the Python sessions offering tables to the Open list
    tickets = ExportTickets()  # the agent exports the user approved, not yet written
    datasets = DatasetService(
        ws,
        recipes,
        table_reader,
        processes,
        own_file=lambda path: is_own_file(path, state_dir, export_dir),
        on_shutdown=on_shutdown or (lambda: None),
    )
    catalogs = CatalogService(
        ws, CatalogStore(connection, settings.max_catalogs, settings.max_catalog_rows), table_reader
    )
    # Tangs come from the person's own folder and the shared folders, which are the blade folders:
    # one team folder carries blades and tangs alike.
    models = Models(
        ws,
        ModelFiles(
            state_dir,
            shared=lambda: [f["path"] for f in blade_store.folders() if f["exists"]],
            packs=pack_files.tang_dirs,
        ),
    )
    return Services(
        settings=settings,
        ws=ws,
        datasets=datasets,
        grids=GridService(ws, recipes, processes),
        profiles=Profiles(ws),
        analysis=Analysis(ws),
        sql=SqlBox(ws, datasets, table_reader),
        exports=Exports(ws, export_writer, tickets, now=lambda: datetime.now(UTC)),
        codes=CodeService(ws, verifier),
        cards=Cards(ws),
        agent=AgentService(ws, Approvals(live), tickets, blade_store, TEMPLATES, blade_run.needed),
        sessions=Sessions(processes, store, live),
        catalogs=catalogs,
        blades=BladeService(
            ws,
            blade_store,
            catalogs,
            TEMPLATES,
            blade_run.needed,
            run_blade,
            blade_runner.install_packages,
            python=sys.executable,
            environ=os.environ,
        ),
        notebook_cells=NotebookCells(ws, NotebookControl(notebook), verifier, DEFAULT_NOTEBOOK),
        models=models,
        finder=Finder(ws, models),
        packs=Packs(ws, pack_files, blade_store, models.library_files),
        keys=KeyFinder(ws),
        compares=Compares(ws),
        snapshots=Snapshots(ws),
        live=live,
        workers=workers,
        notebook=notebook,
        notebook_cookie=secrets.token_urlsafe(32),
    )
