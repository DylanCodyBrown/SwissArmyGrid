"""Catalogs (plan decision 17): a source database's names and types, loaded by the user (sag.load or
a blade's Refresh), browsed as a tree and searched. Readable by the agent: names and types, never
values. Only the user loads or removes one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from swissarmygrid.domain.catalogs import CatalogInfo, script, select_sql
from swissarmygrid.services.datasets import TableReader
from swissarmygrid.services.ports import BACKGROUND, INTERACTIVE
from swissarmygrid.services.workspace import Workspace


class Catalogs(Protocol):
    """The loaded catalogs in the locked database (adapters/catalogs.py). Each query takes the
    cursor it runs on."""

    def cursor(self) -> Any: ...

    def text_table(self, columns: list[str], rows: list[list[Any]]) -> Any: ...

    def load(
        self, cur: Any, name: str, table: Any, dialect: str | None = None, source: str = "load"
    ) -> CatalogInfo: ...

    def remove(self, cur: Any, name: str) -> None: ...

    def all(self) -> list[CatalogInfo]: ...

    def get(self, name: str) -> CatalogInfo: ...

    def schemas(self, cur: Any, name: str) -> list[dict[str, Any]]: ...

    def tables(
        self,
        cur: Any,
        name: str,
        schema: str,
        database: str | None = None,
        offset: int = 0,
        limit: int = 1000,
    ) -> dict[str, Any]: ...

    def columns(
        self, cur: Any, name: str, schema: str, table: str, database: str | None = None
    ) -> list[dict[str, Any]]: ...

    def search(self, cur: Any, name: str, text: str, limit: int = 50) -> dict[str, Any]: ...


class CatalogService:
    def __init__(self, ws: Workspace, catalogs: Catalogs, reader: TableReader) -> None:
        self.ws = ws
        self._catalogs = catalogs
        self._reader = reader

    async def _on_worker(self, fn: Callable[[Any], Any]) -> Any:
        def job() -> Any:
            cursor = self._catalogs.cursor()
            try:
                return fn(cursor)
            finally:
                cursor.close()

        return await self.ws.workers.run(INTERACTIVE, job)

    async def _changed(self) -> None:
        await self.ws.live.broadcast({"type": "catalogs_changed"})

    def loaded_names(self) -> set[str]:
        return {i.name.casefold() for i in self._catalogs.all()}

    def listing(self) -> dict[str, Any]:
        return {"catalogs": [i.to_json() for i in self._catalogs.all()]}

    async def load(
        self, name: str, arrow: bytes | None, payload: Any, dialect: str | None, who: str
    ) -> CatalogInfo:
        """Load one (sag.load): an Arrow stream or split JSON, like a push."""
        ws = self.ws
        if arrow is not None:
            table = await ws.workers.run(
                BACKGROUND, self._reader.arrow_stream_to_table, arrow, ws.settings.max_dataset_bytes
            )
        else:
            table = await ws.workers.run(BACKGROUND, self._reader.split_json_to_arrow, payload)
        info: CatalogInfo = await self._on_worker(
            lambda cur: self._catalogs.load(cur, name, table, dialect, "load")
        )
        ws.audit.write(who, "load_catalog", info.name)
        await self._changed()
        return info

    async def load_from_blade(
        self, name: str, columns: list[str], rows: list[list[Any]], dialect: str
    ) -> CatalogInfo:
        """A blade's Refresh: the rows its catalog command gave, as that blade's catalog."""
        table = await self.ws.workers.run(BACKGROUND, self._catalogs.text_table, columns, rows)
        info: CatalogInfo = await self._on_worker(
            lambda cur: self._catalogs.load(cur, name, table, dialect, "blade")
        )
        return info

    async def blade_refreshed(self) -> None:
        await self._changed()

    async def remove(self, name: str, who: str) -> dict[str, Any]:
        await self._on_worker(lambda cur: self._catalogs.remove(cur, name))
        self.ws.audit.write(who, "remove_catalog", name)
        await self._changed()
        return {"removed": name}

    async def schemas(self, name: str) -> dict[str, Any]:
        info = self._catalogs.get(name)
        found = await self._on_worker(lambda cur: self._catalogs.schemas(cur, name))
        return {"catalog": info.to_json(), "schemas": found}

    async def tables(
        self, name: str, schema: str, database: str | None, offset: int, limit: int
    ) -> dict[str, Any]:
        found: dict[str, Any] = await self._on_worker(
            lambda cur: self._catalogs.tables(cur, name, schema, database, offset, limit)
        )
        return found

    async def columns(
        self, name: str, schema: str, table: str | None, database: str | None
    ) -> dict[str, Any]:
        """A table's columns, with a first SELECT in the source's dialect."""
        info = self._catalogs.get(name)
        if not table:
            raise ValueError("Name the table: ?schema=...&table=...")
        found = await self._on_worker(lambda cur: self._catalogs.columns(cur, name, schema, table, database))
        if not found:
            raise LookupError(f"No table {table!r} in schema {schema!r} of the catalog {info.name!r}.")
        named_database = database if database and info.databases > 1 else ""
        select = select_sql(info.dialect, named_database, schema, table, [c["column"] for c in found])
        return {
            "dialect": info.dialect,
            "schema": schema,
            "table": table,
            "columns": found,
            "select": select,
        }

    async def script(
        self, name: str, schema: str, table: str | None, database: str | None, kind: str, blade: str | None
    ) -> dict[str, Any]:
        """One table as SELECT *, SELECT its columns, or Python (pandas over the blade's connection)."""
        info = self._catalogs.get(name)
        if not table:
            raise ValueError("Name the table: ?schema=...&table=...")
        found = await self._on_worker(lambda cur: self._catalogs.columns(cur, name, schema, table, database))
        if not found:
            raise LookupError(f"No table {table!r} in schema {schema!r} of the catalog {info.name!r}.")
        named_database = database if database and info.databases > 1 else ""
        columns = [c["column"] for c in found]
        return script(kind, info.dialect, named_database, schema, table, columns, blade or None) | {
            "table": table,
            "dialect": info.dialect,
        }

    async def search(self, name: str, text: str, limit: int) -> dict[str, Any]:
        found: dict[str, Any] = await self._on_worker(
            lambda cur: self._catalogs.search(cur, name, text, limit)
        )
        return found
