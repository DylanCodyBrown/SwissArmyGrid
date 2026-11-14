"""Catalogs: a source database's names and types, browsed as a tree and searched (plan decision 17).

A catalog is loaded from any table that names, per row, one column of the source: its table and
column, and usually its schema and type. An `INFORMATION_SCHEMA.COLUMNS` result works as it is, as
do Oracle's ALL_TAB_COLUMNS and plain `schema, table, column, type` frames; the column names are
matched case-insensitively against ALIASES. The rows go into one DuckDB table per catalog, in the
locked database, under an engine name (`__sag_catalog_<n>`) that the SQL box and agent SQL refuse
like every `__sag_` name. Names and types only: nothing here is ever a value from the source.

The tree is Schema -> table or view -> column -> type. Search finds tables and columns: every word
must appear in `schema.table.column`; `a.b` means schema `a` and table `b`, or table `a` and
column `b`; exact names rank before names that start with the text, then names that contain it.
The text is only ever a parameter, never SQL.

This file is the rules: the dialects, a catalog's name, which source column is read as what (and the
SELECT that normalizes them), and a first SELECT in the source's own dialect. The loaded catalogs
themselves, in the locked DuckDB, are adapters/catalogs.py.
"""

from __future__ import annotations

import keyword
import re
from dataclasses import dataclass
from typing import Any

# The dialects SAG knows how to quote for, with the names people also use for them.
DIALECTS: tuple[str, ...] = (
    "sqlserver",
    "postgres",
    "mysql",
    "sqlite",
    "duckdb",
    "snowflake",
    "oracle",
    "trino",
    "other",
)
_DIALECT_ALIASES: dict[str, str] = {
    "tsql": "sqlserver",
    "mssql": "sqlserver",
    "sql server": "sqlserver",
    "azuresql": "sqlserver",
    "postgresql": "postgres",
    "redshift": "postgres",
    "mariadb": "mysql",
    "presto": "trino",
}

# Normalized column -> the source column names it is read from, in order of preference.
ALIASES: dict[str, tuple[str, ...]] = {
    "database": ("table_catalog", "database", "database_name", "catalog", "table_cat", "db"),
    "schema": ("table_schema", "schema", "schema_name", "owner", "table_schem", "table_owner"),
    "table": ("table_name", "table", "object_name", "view_name", "tablename"),
    "kind": ("table_type", "kind", "object_type", "type_of_table"),
    "column": ("column_name", "column", "columnname", "field", "attname"),
    "type": ("data_type", "type", "column_type", "type_name", "datatype", "data_type_name"),
    "nullable": ("is_nullable", "nullable", "null", "notnull_flag"),
    "position": ("ordinal_position", "position", "column_id", "ordinal", "column_position", "colid"),
    "description": ("description", "comment", "column_comment", "remarks", "comments"),
}
_REQUIRED: tuple[str, ...] = ("table", "column")

# A catalog's display name: what the user called it, shown in the explorer and the agent's tools.
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9 _.\-]{0,63}")


class CatalogError(ValueError):
    """The table cannot be a catalog, or a limit was reached. The message says why in plain words."""


@dataclass(frozen=True)
class CatalogInfo:
    name: str
    dialect: str
    source: str  # "load" (sag.load) or "blade" (a blade's Refresh)
    loaded_at: float
    table: str  # the engine table holding its rows
    databases: int
    schemas: int
    tables: int
    views: int
    columns: int

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "dialect": self.dialect,
            "source": self.source,
            "loaded_at": self.loaded_at,
            "databases": self.databases,
            "schemas": self.schemas,
            "tables": self.tables,
            "views": self.views,
            "columns": self.columns,
        }


def normalize_dialect(dialect: str | None) -> str:
    if dialect is None or not dialect.strip():
        return "other"
    key = dialect.strip().lower()
    key = _DIALECT_ALIASES.get(key, key)
    if key not in DIALECTS:
        raise CatalogError(f"Unknown dialect {dialect!r}. Use one of: {', '.join(DIALECTS)}.")
    return key


def check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name) or name != name.strip():
        raise CatalogError(
            f"Catalog name {name!r} is not allowed. Use 1 to 64 letters, digits, spaces, '_', '-' or '.', "
            "starting with a letter, digit or '_'."
        )
    return name


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def pick_columns(names: list[str]) -> dict[str, str]:
    """Normalized column -> the source column it is read from."""
    by_key: dict[str, str] = {}
    for name in names:
        by_key.setdefault(name.strip().lower(), name)
    picked: dict[str, str] = {}
    for target, aliases in ALIASES.items():
        for alias in aliases:
            if alias in by_key and by_key[alias] not in picked.values():
                picked[target] = by_key[alias]
                break
    missing = [t for t in _REQUIRED if t not in picked]
    if missing:
        wanted = "; ".join(f"{t}: {', '.join(ALIASES[t])}" for t in missing)
        raise CatalogError(
            f"A catalog needs a table name and a column name on every row. Not found ({wanted}). "
            f"The table has: {', '.join(names) or 'no columns'}. "
            "An INFORMATION_SCHEMA.COLUMNS result works as it is."
        )
    return picked


def normalizing_select(source: str, picked: dict[str, str]) -> str:
    def text(target: str, default: str = "''") -> str:
        column = picked.get(target)
        return f"coalesce(trim(CAST({quote(column)} AS VARCHAR)), {default})" if column else default

    kind = picked.get("kind")
    kind_sql = (
        f"CASE WHEN lower(CAST({quote(kind)} AS VARCHAR)) LIKE '%view%' THEN 'view' ELSE 'table' END"
        if kind
        else "'table'"
    )
    nullable = picked.get("nullable")
    flag = f"upper(trim(CAST({quote(nullable)} AS VARCHAR)))" if nullable else ""
    nullable_sql = (
        f"CASE WHEN {flag} IN ('YES', 'Y', 'TRUE', 'T', '1') THEN true "
        f"WHEN {flag} IN ('NO', 'N', 'FALSE', 'F', '0') THEN false END"
        if nullable
        else "CAST(NULL AS BOOLEAN)"
    )
    position = picked.get("position")
    position_sql = f"TRY_CAST({quote(position)} AS INTEGER)" if position else "CAST(NULL AS INTEGER)"
    description = picked.get("description")
    description_sql = (
        f"nullif(trim(CAST({quote(description)} AS VARCHAR)), '')" if description else "CAST(NULL AS VARCHAR)"
    )
    return (
        f'SELECT {text("database")} AS database, {text("schema")} AS schema, {text("table")} AS "table", '  # noqa: S608 (quoted names)
        f'{kind_sql} AS kind, {text("column")} AS "column", {text("type")} AS type, '
        f"{nullable_sql} AS nullable, {position_sql} AS source_position, {description_sql} AS description "
        f"FROM {source}"
    )


# ===== writing SQL against the source =====


def quote_identifier(name: str, dialect: str) -> str:
    if dialect == "sqlserver":
        return "[" + name.replace("]", "]]") + "]"
    if dialect == "mysql":
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'


def _source(dialect: str, database: str, schema: str, table: str) -> str:
    return ".".join(quote_identifier(p, dialect) for p in (database, schema, table) if p)


def select_sql(
    dialect: str, database: str, schema: str, table: str, columns: list[str], rows: int | None = 100
) -> str:
    """A first SELECT over one table, quoted for the source's dialect, limited to `rows` rows (None:
    every row). Name the database only when the catalog spans more than one (the connection already sits
    in it)."""
    dialect = normalize_dialect(dialect)
    source = _source(dialect, database, schema, table)
    picked = ", ".join(quote_identifier(c, dialect) for c in columns) or "*"
    if rows is None:
        return f"SELECT {picked}\nFROM {source}"
    if dialect == "sqlserver":
        return f"SELECT TOP {rows} {picked}\nFROM {source}"
    if dialect == "oracle":
        return f"SELECT {picked}\nFROM {source}\nFETCH FIRST {rows} ROWS ONLY"
    return f"SELECT {picked}\nFROM {source}\nLIMIT {rows}"


def count_sql(dialect: str, database: str, schema: str, table: str) -> str:
    """How many rows the table holds (the grid says how many it was not shown)."""
    return f"SELECT COUNT(*) FROM {_source(normalize_dialect(dialect), database, schema, table)}"  # noqa: S608


# ===== Script as (the catalog's right-click, like SSMS) =====

SCRIPTS: dict[str, str] = {
    "select_star": "SELECT *",
    "select_columns": "SELECT the columns",
    "pandas": "Python: pandas",
}


def _python_string(sql: str) -> str:
    """SQL as a Python string literal: triple quotes when they fit, raw when it holds a backslash."""
    if '"""' in sql or sql.endswith("\\"):
        return repr(sql)
    return f'{"r" if chr(92) in sql else ""}"""\n{sql}\n"""'


def script(
    kind: str,
    dialect: str,
    database: str,
    schema: str,
    table: str,
    columns: list[str],
    blade: str | None = None,
) -> dict[str, Any]:
    """One table scripted as `kind` (SCRIPTS): {"label", "language", "code", "imports"}. `blade` names the
    blade whose connection reaches this source (the catalog's own), for the Python script."""
    if kind not in SCRIPTS:
        raise ValueError(f"Script as one of {', '.join(SCRIPTS)}.")
    dialect = normalize_dialect(dialect)
    source = ".".join(quote_identifier(p, dialect) for p in (database, schema, table) if p)
    names = [quote_identifier(c, dialect) for c in columns]
    if kind == "select_star":
        return {"label": SCRIPTS[kind], "language": "sql", "code": f"SELECT *\nFROM {source};", "imports": []}
    listed = ",\n".join(f"    {n}" for n in names) or "    *"
    sql = f"SELECT\n{listed}\nFROM {source}"
    if kind == "select_columns":
        return {"label": SCRIPTS[kind], "language": "sql", "code": f"{sql};", "imports": []}
    variable = re.sub(r"[^a-z0-9_]+", "_", table.lower()).strip("_") or "rows"
    if variable[0].isdigit() or keyword.iskeyword(variable):
        variable = f"t_{variable}"
    lines = []
    imports = ["import pandas as pd"]
    if blade:
        imports.append("import swissarmygrid as sag")
        lines.append(f"conn = sag.blade({blade!r})")
    else:
        lines.append("# conn: a connection to this source (a blade's sag.blade(...) gives one)")
    lines.append(f"{variable} = pd.read_sql(\n    {_python_string(sql)},\n    conn,\n)")
    return {"label": SCRIPTS[kind], "language": "python", "code": "\n".join(lines), "imports": imports}
