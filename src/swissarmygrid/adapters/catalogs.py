"""The loaded catalogs (domain/catalogs.py has the rules): one DuckDB table per catalog, in the locked
database, under an engine name (`__sag_catalog_<n>`) that the SQL box and agent SQL refuse like every
`__sag_` name. Tree queries and search run here; the search text is only ever a parameter, never SQL.

Each method takes the cursor it runs on, so the server runs them on its worker pool.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import duckdb
import pyarrow as pa

from swissarmygrid.domain.catalogs import (
    CatalogError,
    CatalogInfo,
    check_name,
    normalize_dialect,
    normalizing_select,
    pick_columns,
)


class CatalogStore:
    """Every loaded catalog, keyed by name without regard to case (loading `warehouse` replaces
    `Warehouse`: the same source refreshed, not a second catalog)."""

    def __init__(self, connection: duckdb.DuckDBPyConnection, max_catalogs: int, max_rows: int) -> None:
        self.connection = connection
        self.max_catalogs = max_catalogs
        self.max_rows = max_rows
        self._catalogs: dict[str, CatalogInfo] = {}
        self._lock = threading.Lock()

    def cursor(self) -> duckdb.DuckDBPyConnection:
        """A new session with the locked database, for one piece of work (close it after)."""
        return self.connection.cursor()

    @staticmethod
    def text_table(columns: list[str], rows: list[list[Any]]) -> pa.Table:
        """A catalog's rows as text columns: a driver may give a position as a number on one row and
        as text on another, and the catalog reads every value as text anyway."""
        names = [str(c) for c in columns]
        values: list[list[str | None]] = [[] for _ in names]
        for row in rows:
            for i in range(len(names)):
                value = row[i] if i < len(row) else None
                values[i].append(None if value is None else str(value))
        return pa.table(
            {name: pa.array(column, pa.string()) for name, column in zip(names, values, strict=True)}
        )

    # ===== load and remove =====

    def load(
        self,
        cur: duckdb.DuckDBPyConnection,
        name: str,
        table: pa.Table,
        dialect: str | None = None,
        source: str = "load",
    ) -> CatalogInfo:
        check_name(name)
        dialect = normalize_dialect(dialect)
        picked = pick_columns(list(table.column_names))
        if table.num_rows > self.max_rows:
            raise CatalogError(
                f"A catalog holds at most {self.max_rows:,} rows (one per column of the source); "
                f"this one has {table.num_rows:,}. Narrow the catalog query, for example to the schemas "
                "you use."
            )
        key = name.casefold()
        with self._lock:
            if key not in self._catalogs and len(self._catalogs) >= self.max_catalogs:
                raise CatalogError(
                    f"{self.max_catalogs} catalogs are loaded, the most at once. Remove one first: "
                    f"{', '.join(i.name for i in self._catalogs.values())}."
                )
        staging = "__sag_incoming_" + uuid.uuid4().hex
        target = "__sag_catalog_" + uuid.uuid4().hex
        cur.register(staging, table)
        try:
            # One row per (database, schema, table, column), in the source's own order, numbered
            # within each table (position) so a catalog without ordinal positions still lists the
            # columns as they came. The lower-case path is what search reads.
            cur.execute(
                f"""CREATE TABLE {target} AS
                WITH rows AS ({normalizing_select(staging, picked)}),
                numbered AS (
                    SELECT *, row_number() OVER () AS arrival FROM rows WHERE "table" <> '' AND "column" <> ''
                )
                SELECT database, schema, "table", kind, "column", type, nullable, description,
                    coalesce(
                        source_position,
                        row_number() OVER (PARTITION BY database, schema, "table" ORDER BY arrival)
                    )::INTEGER AS position,
                    lower(schema) AS schema_l, lower("table") AS table_l, lower("column") AS column_l,
                    lower(schema || '.' || "table" || '.' || "column") AS path_l
                FROM numbered
                ORDER BY database, schema, "table", position, "column"
                """  # noqa: S608 (generated names)
            )
            counts = cur.execute(
                f"""SELECT count(DISTINCT database), count(DISTINCT (database, schema)),
                    count(DISTINCT (database, schema, "table")) FILTER (WHERE kind = 'table'),
                    count(DISTINCT (database, schema, "table")) FILTER (WHERE kind = 'view'),
                    count(*) FROM {target}"""  # noqa: S608
            ).fetchall()[0]
        except duckdb.Error as error:
            cur.execute(f"DROP TABLE IF EXISTS {target}")
            raise CatalogError(f"DuckDB could not read this catalog: {error}") from error
        finally:
            cur.unregister(staging)
        info = CatalogInfo(
            name=name,
            dialect=dialect,
            source=source,
            loaded_at=time.time(),
            table=target,
            databases=int(counts[0]),
            schemas=int(counts[1]),
            tables=int(counts[2]),
            views=int(counts[3]),
            columns=int(counts[4]),
        )
        with self._lock:
            old = self._catalogs.get(key)
            if old is None and len(self._catalogs) >= self.max_catalogs:
                cur.execute(f"DROP TABLE IF EXISTS {target}")
                raise CatalogError(
                    f"{self.max_catalogs} catalogs are loaded, the most at once. Remove one first."
                )
            # Keep the name's order in the list when a catalog is replaced.
            self._catalogs[key] = info
        if old is not None:
            cur.execute(f"DROP TABLE IF EXISTS {old.table}")
        return info

    def remove(self, cur: duckdb.DuckDBPyConnection, name: str) -> None:
        with self._lock:
            info = self._catalogs.pop(name.casefold(), None)
        if info is None:
            raise KeyError(name)
        cur.execute(f"DROP TABLE IF EXISTS {info.table}")

    # ===== read =====

    def all(self) -> list[CatalogInfo]:
        with self._lock:
            return sorted(self._catalogs.values(), key=lambda i: i.name.casefold())

    def get(self, name: str) -> CatalogInfo:
        with self._lock:
            info = self._catalogs.get(name.casefold())
        if info is None:
            raise KeyError(name)
        return info

    def schemas(self, cur: duckdb.DuckDBPyConnection, name: str) -> list[dict[str, Any]]:
        info = self.get(name)
        rows = cur.execute(
            f"""SELECT database, schema,
                count(DISTINCT "table") FILTER (WHERE kind = 'table'),
                count(DISTINCT "table") FILTER (WHERE kind = 'view')
            FROM {info.table} GROUP BY database, schema
            ORDER BY lower(database), lower(schema), database, schema"""  # noqa: S608
        ).fetchall()
        return [{"database": r[0], "schema": r[1], "tables": r[2], "views": r[3]} for r in rows]

    def tables(
        self,
        cur: duckdb.DuckDBPyConnection,
        name: str,
        schema: str,
        database: str | None = None,
        offset: int = 0,
        limit: int = 1000,
    ) -> dict[str, Any]:
        info = self.get(name)
        offset, limit = max(0, int(offset)), min(5000, max(1, int(limit)))
        where, params = self._place(schema, database)
        rows = cur.execute(
            f"""SELECT database, "table", any_value(kind), count(*), count(*) OVER ()
            FROM {info.table} WHERE {where}
            GROUP BY database, "table" ORDER BY lower("table"), "table", database
            LIMIT {limit} OFFSET {offset}""",  # noqa: S608 (numbers checked above)
            params,
        ).fetchall()
        total = rows[0][4] if rows else self._count_tables(cur, info, where, params)
        return {
            "total": total,
            "tables": [
                {"database": r[0], "schema": schema, "table": r[1], "kind": r[2], "columns": r[3]}
                for r in rows
            ],
        }

    def _count_tables(
        self, cur: duckdb.DuckDBPyConnection, info: CatalogInfo, where: str, params: list[Any]
    ) -> int:
        rows = cur.execute(
            f'SELECT count(DISTINCT (database, "table")) FROM {info.table} WHERE {where}',  # noqa: S608
            params,
        ).fetchall()
        return int(rows[0][0])

    def columns(
        self,
        cur: duckdb.DuckDBPyConnection,
        name: str,
        schema: str,
        table: str,
        database: str | None = None,
    ) -> list[dict[str, Any]]:
        info = self.get(name)
        where, params = self._place(schema, database)
        rows = cur.execute(
            f"""SELECT "column", type, nullable, position, description FROM {info.table}
            WHERE {where} AND "table" = ? ORDER BY position, "column" """,  # noqa: S608
            [*params, table],
        ).fetchall()
        return [
            {"column": r[0], "type": r[1], "nullable": r[2], "position": r[3], "description": r[4]}
            for r in rows
        ]

    @staticmethod
    def _place(schema: str, database: str | None) -> tuple[str, list[Any]]:
        if database is None:
            return "schema = ?", [schema]
        return "schema = ? AND database = ?", [schema, database]

    # ===== search =====

    def search(self, cur: duckdb.DuckDBPyConnection, name: str, text: str, limit: int = 50) -> dict[str, Any]:
        info = self.get(name)
        limit = min(500, max(1, int(limit)))
        words = [w for w in str(text).lower().split() if w]
        if not words:
            return {"tables": [], "columns": [], "total_tables": 0, "total_columns": 0}
        if len(words) == 1 and "." in words[0].strip("."):
            column_filter, table_filter, params_c, params_t, focus = self._dotted(words[0].strip("."))
        else:
            column_filter = " AND ".join("contains(path_l, ?)" for _ in words)
            table_filter = " AND ".join("contains(schema_l || '.' || table_l, ?)" for _ in words)
            params_c, params_t = list(words), list(words)
            focus = words[-1]
        columns = cur.execute(
            f"""SELECT database, schema, "table", kind, "column", type, count(*) OVER () FROM {info.table}
            WHERE {column_filter}
            ORDER BY CASE WHEN column_l = ? THEN 0 WHEN starts_with(column_l, ?) THEN 1
                WHEN contains(column_l, ?) THEN 2 ELSE 3 END,
                length("column"), lower(schema), lower("table"), position
            LIMIT {limit}""",  # noqa: S608 (the limit is a checked number)
            [*params_c, focus, focus, focus],
        ).fetchall()
        tables = cur.execute(
            f"""SELECT database, schema, "table", any_value(kind), count(*), count(*) OVER ()
            FROM {info.table} WHERE {table_filter}
            GROUP BY database, schema, "table", table_l
            ORDER BY CASE WHEN table_l = ? THEN 0 WHEN starts_with(table_l, ?) THEN 1
                WHEN contains(table_l, ?) THEN 2 ELSE 3 END,
                length("table"), lower(schema), lower("table")
            LIMIT {limit}""",  # noqa: S608
            [*params_t, focus, focus, focus],
        ).fetchall()
        return {
            "tables": [
                {"database": r[0], "schema": r[1], "table": r[2], "kind": r[3], "columns": r[4]}
                for r in tables
            ],
            "columns": [
                {"database": r[0], "schema": r[1], "table": r[2], "kind": r[3], "column": r[4], "type": r[5]}
                for r in columns
            ],
            "total_tables": tables[0][5] if tables else 0,
            "total_columns": columns[0][6] if columns else 0,
        }

    @staticmethod
    def _dotted(text: str) -> tuple[str, str, list[Any], list[Any], str]:
        """`a.b` is schema a + table b, or table a + column b; `a.b.c` is schema.table.column."""
        parts = [p for p in text.split(".") if p]
        if len(parts) >= 3:
            s, t, c = parts[0], parts[1], ".".join(parts[2:])
            column_filter = "contains(schema_l, ?) AND contains(table_l, ?) AND contains(column_l, ?)"
            return column_filter, "false", [s, t, c], [], c
        a, b = parts[0], parts[1]
        column_filter = "contains(table_l, ?) AND contains(column_l, ?)"
        table_filter = "contains(schema_l, ?) AND contains(table_l, ?)"
        return column_filter, table_filter, [a, b], [a, b], b
