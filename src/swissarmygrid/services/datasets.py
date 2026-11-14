"""Datasets: the ways data arrives (a push, a file, pasted text, a query over the open tables), and
listing and closing what is open.

Every way in ends in `store_table`: store, pick a grid, tell the pages, write the audit log. A push
reuses the dataset's first grid (that grid hot-reloads in place) unless a new one is asked for. The
first grid on a dataset picks up a saved recipe for that name, if there is one.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from swissarmygrid.domain.datasets import DatasetError, DatasetInfo, Opened, mib, quote, validate_name
from swissarmygrid.domain.grids import Grid
from swissarmygrid.domain.sql_guard import SqlRefused, guard
from swissarmygrid.services.ports import BACKGROUND, INTERACTIVE, Recipes, SqlSession
from swissarmygrid.services.processes import ProcessRegistry
from swissarmygrid.services.workspace import Conflict, NotFound, Refused, TimedOut, Workspace


class TableReader(Protocol):
    """Bytes, files and text to a table (adapters/datasets.py: Arrow, CSV, Excel, Parquet, JSON)."""

    def arrow_stream_to_table(self, body: bytes, max_bytes: int) -> Any: ...

    def split_json_to_arrow(self, payload: Any) -> Any: ...

    def load_file(self, path_text: str, max_bytes: int, sheet: str | None = None) -> tuple[Any, str]: ...

    def excel_sheets(self, path_text: str) -> list[str]: ...

    def load_bytes(
        self, data: bytes, filename: str, max_bytes: int, sheet: str | None = None
    ) -> tuple[Any, str]: ...

    def excel_sheets_bytes(self, data: bytes, filename: str) -> list[str]: ...

    def parse_pasted(self, text: str, max_bytes: int) -> Any: ...

    def query_table(self, cursor: Any, sql: str) -> Any: ...

    def file_name(self, path_text: str) -> str: ...


class DatasetService:
    def __init__(
        self,
        ws: Workspace,
        recipes: Recipes,
        reader: TableReader,
        processes: ProcessRegistry,
        own_file: Callable[[str], bool],
        on_shutdown: Callable[[], None],
    ) -> None:
        self.ws = ws
        self._recipes = recipes
        self._reader = reader
        self._processes = processes
        # True for a path that is one of SwissArmyGrid's own files (adapters/state_files.py).
        self._own_file = own_file
        self._on_shutdown = on_shutdown
        # Who else names tables: each is told of a rename (the relationship model, bootstrap.py).
        self.renamed: list[Callable[[str, str], Awaitable[None]]] = []

    # ===== the server itself =====

    def stop(self, who: str) -> None:
        self.ws.audit.write(who, "shutdown")
        self._on_shutdown()

    def activity(self, limit: int, actor: str | None) -> list[dict[str, Any]]:
        """The audit log's last entries, for the page's activity list (never cell values)."""
        return self.ws.audit.tail(limit, actor)

    # ===== what is open =====

    def summary(self, info: DatasetInfo) -> dict[str, Any]:
        return {
            "name": info.name,
            "version": info.version,
            "rows": info.row_count,
            "columns": len(info.columns),
            "bytes": info.nbytes,
            "size": mib(info.nbytes),
            "source_kind": info.source_kind,
            "grids": [g.id for g in self.ws.grids.for_dataset(info.name)],
        }

    def listing(self) -> dict[str, Any]:
        return {"datasets": [self.summary(i) for i in self.ws.store.all()], "budget": self.ws.budget()}

    async def close(self, name: str, who: str) -> dict[str, Any] | None:
        """Close a dataset and its grids, freeing its memory. None when there is no such dataset."""
        ws = self.ws
        if not await ws.workers.run(INTERACTIVE, ws.store.close, name):
            return None
        ws.views.forget_dataset(name)
        for grid in ws.grids.for_dataset(name):
            ws.grids.close(grid.id)
        await ws.live.broadcast({"type": "dataset_closed", "dataset": name})
        ws.audit.write(who, "close", name)
        return {"closed": name, "budget": ws.budget()}

    async def rename(self, name: str, to: Any, who: str) -> dict[str, Any]:
        """Call a table by another name everywhere: its grids (steps and settings kept), the model's
        joins, every page's tabs. A Python session following the old name stops following it."""
        ws = self.ws
        if not isinstance(to, str) or not to.strip():
            raise ValueError('Send {"to": "new_name"}.')
        new = to.strip()
        if ws.store.get(name) is None:
            raise NotFound(f"No dataset named {name!r} is open.")
        validate_name(new)
        if new == name:
            return {"dataset": name, "renamed_from": name}
        clash = next((i.name for i in ws.store.all() if i.name != name and i.name.lower() == new.lower()), None)
        if clash is not None:
            raise Conflict(f"A table named {clash!r} is open. Pick another name, or close that one first.")
        ws.views.forget_dataset(name)  # its cached views name the old table
        info: DatasetInfo = await ws.workers.run(INTERACTIVE, ws.store.rename, name, new)
        grids = ws.grids.rename_dataset(name, new)
        for renamed in self.renamed:
            await renamed(name, new)
        await ws.live.broadcast(
            {"type": "dataset_renamed", "dataset": name, "to": new, "grids": [g.id for g in grids]}
        )
        ws.audit.write(who, "rename", name, to=new)
        return self.summary(info) | {"dataset": new, "renamed_from": name}

    # ===== the common end of every way in =====

    async def store_table(
        self,
        name: str,
        table: Any,
        source_kind: str,
        new_grid: bool,
        who: str,
        query: str | None = None,
        follow: bool = False,
        opened: Opened | None = None,
    ) -> dict[str, Any]:
        """The common end of every way data arrives: store, pick a grid, notify, audit.

        `follow` is a Python session keeping an open table current after its variable was assigned
        again. It refreshes the grid where it is: no jump to the front, and no new tab if the user
        closed the old one (the data stays fresh for when they open it again)."""
        ws = self.ws
        info: DatasetInfo = await ws.workers.run(
            BACKGROUND, ws.store.replace, name, table, source_kind, query, opened
        )
        ws.views.forget_dataset(name)
        existing = ws.grids.first_for(name)
        if follow and existing is None:
            await ws.live.dataset_updated(name, info.version)
            ws.audit.write(
                who,
                "push",
                name,
                rows=info.row_count,
                source_kind=info.source_kind,
                bytes=info.nbytes,
                follow=True,
            )
            return {
                "grid_url": None,
                "workspace_url": ws.addresses.workspace(with_token=who != "agent"),
                "grid": None,
                "dataset": name,
                "version": info.version,
                "watched": False,
            }
        grid: Grid
        if new_grid or existing is None:
            recipe = self._recipes.load(name) if existing is None else None
            grid = ws.grids.create(name, *(recipe or ([], None)))
        else:
            grid = existing
        await ws.live.dataset_updated(name, info.version)
        if follow:
            ws.audit.write(
                who,
                "push",
                name,
                rows=info.row_count,
                source_kind=info.source_kind,
                bytes=info.nbytes,
                follow=True,
            )
            watched = True  # the tab is wherever the user left it
        else:
            # An open workspace adds the grid as a tab (if new) and brings it to the front.
            await ws.live.broadcast({"type": "grid_shown", "grid": grid.id, "dataset": name})
            ws.audit.write(
                who, "push", name, rows=info.row_count, source_kind=info.source_kind, bytes=info.nbytes
            )
            # Nothing shows it yet: VS Code opens the workspace (its window the user was last in).
            watched = ws.live.shown_or_opening(grid.id) or await ws.live.ask_opener(
                ws.addresses.workspace(grid)
            )
        return {
            "grid_url": ws.addresses.grid(grid, with_token=who != "agent"),
            "workspace_url": ws.addresses.workspace(grid, with_token=who != "agent"),
            "grid": grid.id,
            "dataset": name,
            "version": info.version,
            # Whether the grid is on screen, about to be (a window an earlier push had opened), or
            # being opened by VS Code now. False only when no VS Code window is connected.
            "watched": watched,
        }

    # ===== a push =====

    def admit_push(self, name: str, follow: bool, process_id: str) -> None:
        """A follow keeps an open table current. If it was closed (or another session took the name)
        while this push was on its way, drop it: a push would quietly reopen the data."""
        if follow and not (self.ws.store.get(name) is not None and self._processes.owns(process_id, name)):
            raise Conflict("That table is no longer open, so it is not kept current.")

    async def push(
        self,
        name: str,
        arrow: bytes | None,
        payload: Any,
        source_kind: str,
        new_grid: bool,
        who: str,
        follow: bool,
        process_id: str,
    ) -> dict[str, Any]:
        """Store a pushed table: an Arrow stream (`arrow`), or pandas' orient="split" JSON (`payload`)."""
        ws = self.ws
        if arrow is not None:
            table = await ws.workers.run(
                INTERACTIVE, self._reader.arrow_stream_to_table, arrow, ws.settings.max_dataset_bytes
            )
        else:
            table = await ws.workers.run(INTERACTIVE, self._reader.split_json_to_arrow, payload)
        result = await self.store_table(name, table, source_kind, new_grid, who, follow=follow)
        # A Python session's watcher says which session pushed this (X-Process): that session
        # now follows the dataset, and the Open list shows the table as open.
        if process_id and self._processes.note_loaded(process_id, name):
            await ws.live.broadcast({"type": "processes_changed"})
        return result

    # ===== a file, pasted text =====

    def _refuse_own_file(self, path: str) -> None:
        """SwissArmyGrid's own files are not tables: the connection file (one line of JSON, so it
        loaded as a table, token and all), the audit log, recipes: the state folder, except the
        export folder inside it (an export loaded back is a table)."""
        if self._own_file(path):
            raise Refused("That is one of the grid server's own files, not a table.")

    async def load_file(
        self, name: str, path: str, sheet: str | None, new_grid: bool, who: str
    ) -> dict[str, Any]:
        ws = self.ws
        self._refuse_own_file(path)
        table, kind = await ws.workers.run(
            BACKGROUND, self._reader.load_file, path, ws.settings.max_dataset_bytes, sheet
        )
        result = await self.store_table(name, table, kind, new_grid, who)
        ws.audit.write(who, "load_file", name, file=self._reader.file_name(path), sheet=sheet)
        return result

    async def peek_file(self, path: str, sheet: str | None, rows: int) -> dict[str, Any]:
        """A file's first rows (the catalog page's preview): nothing is kept."""
        ws = self.ws
        self._refuse_own_file(path)
        table, _ = await ws.workers.run(
            BACKGROUND, self._reader.load_file, path, ws.settings.max_dataset_bytes, sheet
        )
        first = table.slice(0, rows)
        return {"columns": list(first.column_names), "rows": [list(r.values()) for r in first.to_pylist()]}

    async def sheets(self, path: str) -> dict[str, Any]:
        """The sheet names of a workbook, so VS Code can ask which to load."""
        self._refuse_own_file(path)
        return {"sheets": await self.ws.workers.run(BACKGROUND, self._reader.excel_sheets, path)}

    async def load_dropped(
        self, name: str, data: bytes, filename: str, sheet: str | None, who: str
    ) -> dict[str, Any]:
        """A file dropped on the page: its bytes (the browser never gives its path), read in memory
        as Open file reads a path; nothing is written to disk."""
        ws = self.ws
        table, kind = await ws.workers.run(
            BACKGROUND, self._reader.load_bytes, data, filename, ws.settings.max_dataset_bytes, sheet
        )
        result = await self.store_table(name, table, kind, True, who)
        ws.audit.write(who, "load_file", name, file=filename, sheet=sheet)
        return result

    async def dropped_sheets(self, data: bytes, filename: str) -> dict[str, Any]:
        """A dropped workbook's sheet names, so the page can ask which to open."""
        sheets = await self.ws.workers.run(BACKGROUND, self._reader.excel_sheets_bytes, data, filename)
        return {"sheets": sheets}

    async def paste(self, name: str, text: str, who: str) -> dict[str, Any]:
        ws = self.ws
        table = await ws.workers.run(
            INTERACTIVE, self._reader.parse_pasted, text, ws.settings.max_dataset_bytes
        )
        return await self.store_table(name, table, "clipboard", True, who)

    # ===== the "+" tab: a table from a query =====

    async def build_from_query(self, name: str, text: str, replace: bool, who: str) -> dict[str, Any]:
        """Dataset `name` from one SELECT over the open tables, each read as its tab shows it.
        `replace` is the tab's Run again; it rebuilds only a table a query made, so a pushed dataset
        (or another tab's table) is never overwritten by a name clash."""
        ws = self.ws
        settings = ws.settings
        existing = ws.store.get(name)
        if existing is not None and not (replace and existing.query is not None):
            raise DatasetError(f"A dataset named {name!r} already exists; pick a new name.")

        def tab_view(cursor: SqlSession, dataset: str) -> str:
            if dataset == name:
                raise SqlRefused(f"This query builds {name}, so it can't read {name}.")
            # Two tabs on one dataset: the name reads the first (oldest) one.
            grid, info = ws.grids.first_for(dataset), ws.store.get(dataset)
            if grid is None or info is None:  # no tab open: its rows as loaded
                return f"SELECT * FROM {quote('ds_' + dataset)}"  # noqa: S608 (a quoted dataset name)
            return ws.compiled(cursor, grid, info).sql()

        def execute(cursor: Any) -> Any:
            datasets = {i.name for i in ws.store.all()}
            guarded = guard(
                text, datasets, None, lambda dataset: tab_view(cursor, dataset), grammar=ws.grammar
            )
            if guarded.kind != "select":
                raise SqlRefused(f"Write one SELECT; its result becomes {name}.")
            timer = threading.Timer(settings.query_timeout_seconds, cursor.interrupt)
            timer.start()
            try:
                result = self._reader.query_table(cursor, guarded.sql)
            finally:
                timer.cancel()
            return result

        try:
            table = await ws.workers.run(BACKGROUND, lambda: execute(ws.store.cursor()))
        except ws.interrupted:
            raise TimedOut(
                f"The query was stopped: it ran past {settings.query_timeout_seconds:.0f} seconds."
            ) from None
        result = await self.store_table(name, table, "sql_result", False, who, query=text)
        ws.audit.write(who, "sql_query", name, query=text, rows=table.num_rows)
        return result | {"rows": table.num_rows}


class ConnectionTables:
    """A connection's table as a grid, for services/connections.py (its Tables port): stored as any
    table is, a file loaded as Open file loads it, a file's first rows for the catalog's preview."""

    def __init__(self, datasets: DatasetService) -> None:
        self._datasets = datasets

    async def store(self, name: str, table: Any, who: str, opened: Opened | None) -> dict[str, Any]:
        result = await self._datasets.store_table(name, table, "connection", True, who, opened=opened)
        self._datasets.ws.audit.write(who, "open_connection_table", name)
        return result

    async def load_file(self, name: str, path: str, sheet: str | None, who: str) -> dict[str, Any]:
        return await self._datasets.load_file(name, path, sheet, True, who)

    async def sheets(self, path: str) -> list[str]:
        return list((await self._datasets.sheets(path))["sheets"])

    async def peek(self, path: str, sheet: str | None, rows: int) -> dict[str, Any]:
        return await self._datasets.peek_file(path, sheet, rows)
