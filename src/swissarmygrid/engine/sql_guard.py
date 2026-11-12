"""The guard in front of the SQL box and agent SQL (plan section 6, rule 5).

Two layers. DuckDB itself is locked (engine/database.py): no SQL can touch files, the network
or extensions whatever this module decides. On top of that, every query is parsed with sqlglot
and must be exactly one statement of an allowed shape:

  SELECT ... / WITH ... SELECT ... / UNION, INTERSECT, EXCEPT of those
  CREATE TABLE <new name> AS SELECT ...   ("pin as new grid"; Python stores the result)

Table names resolve only to:
  grid          the grid's current view (after its steps); with `grid_alias` (a SQL step), the
                grid's own dataset name reads the same thing, so the step's SQL matches the
                user's Python, where the table is a variable of that name
  <dataset>     any open dataset, by its name (so the SQL box can join); with `dataset_sql`
                (the "+" tab), the dataset as its tab shows it, after that tab's steps
  a CTE name    defined inside the query itself

Anything else (a schema-qualified name, a table function such as read_csv or query, an
internal table) is refused before DuckDB sees it. The source datasets are never written:
the only write shape, CREATE TABLE AS, is executed by Python as a read plus a new dataset.

Two details keep the name rules exact:
- Every CTE is renamed to a fresh internal name (__sag_cte_<n>), and only a reference that the
  CTE's scope covers is pointed at it. A name used outside that scope (or inside a CTE's own,
  non-recursive body, where DuckDB reads the real table) is checked like any other table name,
  so a CTE can never make an internal table's name pass.
- The rewritten query is then parsed again by DuckDB itself (json_serialize_sql), and every
  table and table function in DuckDB's own parse must be one the rewrite produced. A query
  sqlglot reads differently from DuckDB is refused rather than trusted.

Think of it as a whitelist validator in front of a stored-procedure-only database.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp

from swissarmygrid.engine.datasets import NAME_PATTERN

GRID_TABLE: str = "grid"
# Table functions that only generate values (no files, no catalogs, no SQL strings).
_ALLOWED_TABLE_FUNCTIONS: frozenset[str] = frozenset({"range", "generate_series", "unnest"})
_VIEW_CTE: str = "__sag_grid"
# A dataset read as its tab shows it: __sag_view_<dataset>.
_DATASET_VIEW_CTE: str = "__sag_view_"
# Each CTE of the user's query is renamed __sag_cte_<n>; no real table has such a name.
_FRESH_CTE: str = "__sag_cte_"
_RESERVED: str = "__sag_"
# Table references in DuckDB's parse that read nothing by themselves (their parts are checked).
_SAFE_TABLE_REFS: frozenset[str] = frozenset(
    {"SUBQUERY", "JOIN", "EXPRESSION_LIST", "EMPTY", "EMPTY_FROM", "PIVOT"}
)

# Functions refused anywhere in a query. DuckDB would block most of them anyway; this keeps
# the refusal message plain and covers functions that run SQL from a string (query, ...).
_DENIED_PREFIXES: tuple[str, ...] = (
    "read_",
    "glob",
    "sniff_",
    "parquet_",
    "duckdb_",
    "pragma_",
    "query",
    "getenv",
    "getvariable",
    "set_variable",
    "load",
    "install",
    "copy",
    "export",
    "import",
    "attach",
    "detach",
    "checkpoint",
    "iceberg_",
    "delta_",
    "sqlite_",
    "postgres_",
    "mysql_",
    "http",
    "s3",
    "azure",
    "which_secret",
    "current_setting",
)


class SqlRefused(ValueError):
    """The query is not allowed. The message says why, in plain words."""


# A private connection that only parses: DuckDB's own reading of a query (json_serialize_sql)
# and its list of table functions. It holds no data and reaches nothing.
_parser_lock = threading.Lock()
_parser: duckdb.DuckDBPyConnection | None = None
_table_functions: frozenset[str] | None = None


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


def table_only_functions() -> frozenset[str]:
    """DuckDB's table functions that are not also scalar or aggregate functions (repeat and
    range are both): a call to one of these anywhere in a query is refused."""
    global _table_functions
    with _parser_lock:
        if _table_functions is None:
            rows = (
                _parser_connection()
                .execute(
                    "SELECT function_name FROM duckdb_functions() GROUP BY function_name "
                    "HAVING bool_or(function_type IN ('table', 'table_macro', 'pragma')) "
                    "AND NOT bool_or(function_type IN ('scalar', 'aggregate', 'macro'))"
                )
                .fetchall()
            )
            _table_functions = frozenset(str(r[0]).lower() for r in rows) - _ALLOWED_TABLE_FUNCTIONS
        return _table_functions


def check_duckdb_parse(sql: str, tables: set[str]) -> None:
    """Refuse `sql` unless DuckDB's own parse of it reads only `tables` (plain names) and the
    allowed table functions: the second opinion behind the sqlglot rewrite."""
    with _parser_lock:
        row = _parser_connection().execute("SELECT json_serialize_sql(?)", [sql]).fetchone()
    parsed: Any = json.loads(str(row[0])) if row else {"error": True}
    if parsed.get("error"):
        raise SqlRefused(f"DuckDB could not read the query: {parsed.get('error_message', 'unknown error')}")
    allowed = {t.lower() for t in tables}
    stack: list[Any] = [parsed.get("statements")]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        stack.extend(node.values())
        kind = node.get("type")
        if "class" in node or "query_location" not in node or not isinstance(kind, str):
            continue  # an expression, a modifier or a literal, not a table reference
        if kind.endswith("_NODE") or kind in _SAFE_TABLE_REFS:
            continue
        if kind == "BASE_TABLE":
            name = str(node.get("table_name", ""))
            if node.get("schema_name") or node.get("catalog_name") or name.lower() not in allowed:
                raise SqlRefused(f"The query reads {name!r}, which is not an open table.")
        elif kind == "TABLE_FUNCTION":
            function = node.get("function") or {}
            name = str(function.get("function_name", ""))
            qualified = function.get("schema") or function.get("catalog")
            if qualified or name.lower() not in _ALLOWED_TABLE_FUNCTIONS:
                raise SqlRefused(f"The table function {name}() is not allowed here.")
        else:
            raise SqlRefused("Read from grid, a dataset or a CTE by name.")


@dataclass(frozen=True)
class GuardedQuery:
    sql: str  # rewritten, ready for DuckDB
    kind: str  # "select" or "create"
    new_table: str | None  # for "create": the name of the dataset to make


def guard(
    text: str,
    datasets: set[str],
    grid_sql: Callable[[], str] | None,
    dataset_sql: Callable[[str], str] | None = None,
    grid_alias: str | None = None,
) -> GuardedQuery:
    """Check `text` and rewrite its table names. `grid_sql()` gives the grid view's SQL.

    `dataset_sql(name)`, when given, is the SQL a dataset name reads in place of the raw rows,
    added to the query as a CTE: the "+" tab passes each tab's view, so its query reads the
    tables as they look on screen. It may raise SqlRefused to keep a name out.

    `grid_alias`, when given, is the grid's own dataset name, read exactly as `grid` (not as the
    raw dataset). Only a SQL step passes it: a step reads nothing but the table entering it. In
    the SQL box a dataset name, the grid's own included, reads its rows as loaded, as the user's
    Python variable of that name does.
    """
    try:
        return _guard(text, datasets, grid_sql, dataset_sql, grid_alias)
    except RecursionError as error:  # sqlglot parses and prints recursively
        raise SqlRefused("The query is nested too deeply.") from error


def _guard(
    text: str,
    datasets: set[str],
    grid_sql: Callable[[], str] | None,
    dataset_sql: Callable[[str], str] | None,
    grid_alias: str | None,
) -> GuardedQuery:
    try:
        statements = [s for s in sqlglot.parse(text, read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as error:
        raise SqlRefused(f"Could not parse the query: {_first_line(str(error))}") from error
    if len(statements) != 1:
        raise SqlRefused("Run one statement at a time.")
    root = statements[0]

    new_table: str | None = None
    if isinstance(root, exp.Create):
        if str(root.args.get("kind", "")).upper() != "TABLE" or not isinstance(root.expression, exp.Query):
            raise SqlRefused("Only CREATE TABLE <new name> AS SELECT ... is allowed.")
        target = root.this
        if isinstance(target, exp.Schema):
            raise SqlRefused("CREATE TABLE AS cannot declare columns; the SELECT defines them.")
        if not isinstance(target, exp.Table) or target.db or target.catalog:
            raise SqlRefused("Name the new table with a plain name.")
        new_table = target.name
        if not NAME_PATTERN.fullmatch(new_table) or new_table == GRID_TABLE:
            raise SqlRefused(f"{new_table!r} is not an allowed dataset name.")
        if new_table in datasets:
            raise SqlRefused(f"A dataset named {new_table!r} already exists; pick a new name.")
        query: Any = root.expression
    elif isinstance(root, exp.Query):
        query = root
    else:
        raise SqlRefused(
            "Only SELECT (or WITH ... SELECT) queries and CREATE TABLE ... AS SELECT are allowed."
        )

    _refuse_denied_functions(query)
    grid_names = {GRID_TABLE} if grid_alias is None else {GRID_TABLE, grid_alias}
    needs_grid, read, targets = _rewrite_tables(
        query, datasets, grid_names if grid_sql is not None else set(), as_views=dataset_sql is not None
    )
    # DuckDB's own parse of the user's part, before the server's CTEs are added.
    check_duckdb_parse(query.sql(dialect="duckdb"), targets)
    if dataset_sql is not None:
        for name in read:
            query = _prepend_cte(query, _DATASET_VIEW_CTE + name, dataset_sql(name))
    if needs_grid and grid_sql is not None:
        query = _prepend_cte(query, _VIEW_CTE, grid_sql())
    return GuardedQuery(query.sql(dialect="duckdb"), "create" if new_table else "select", new_table)


def guard_step(text: str, grid_sql: str, dataset: str) -> str:
    """A SQL step (plan section 7, "sql"): one SELECT that reads only the table entering the
    step, named either `grid` or the grid's own `dataset` name. Returns the query rewritten for
    DuckDB, with an order the grid can rely on.

    A step reads no other dataset, so its generated Python needs nothing but the grid's own
    data; to join datasets, run the query in the SQL box and pin the result as a new grid.

    Row order: a plain SELECT ... FROM grid [WHERE ...] keeps the grid's order (DuckDB preserves
    insertion order, checked in docs/verified.md); a query with ORDER BY keeps its own. Anything
    else (GROUP BY, DISTINCT, a join, a window, a set operation) has no defined order, so it is
    sorted by every column (ORDER BY ALL) and comes out the same way on every run.
    """
    try:
        guarded = guard(text, set(), lambda: grid_sql, grid_alias=dataset)
    except SqlRefused as error:
        message = str(error)
        if message.startswith("Unknown table"):
            message += (
                f" A SQL step reads only the table entering it ({dataset}, or grid);"
                " to join datasets, run the query and pin it."
            )
        raise SqlRefused(message) from error
    if guarded.kind != "select":
        raise SqlRefused(f"A SQL step is one SELECT over {dataset} (use Pin to make a new dataset).")
    ordered = f"SELECT * FROM ({guarded.sql}) ORDER BY ALL"  # noqa: S608 (guarded query)
    try:
        return guarded.sql if keeps_order(text, dataset) else ordered
    except RecursionError as error:
        raise SqlRefused("The query is nested too deeply.") from error


def step_query_text(text: str, dataset: str) -> str:
    """The step's query as the generated code runs it (against the step's input registered
    under the names it reads, see step_tables), with the same ORDER BY ALL rule as guard_step."""
    body = text.strip().rstrip(";")
    # The user's own query, written into their own code; the server never runs this text.
    return body if keeps_order(body, dataset) else f"SELECT * FROM (\n{body}\n) ORDER BY ALL"  # noqa: S608


def step_tables(text: str, dataset: str) -> list[str]:
    """The names a step's query reads its input by: `dataset`, `grid`, or both. The generated
    code registers the input under each, so the query runs there exactly as written."""
    root: Any = sqlglot.parse_one(text, read="duckdb")
    ctes = _cte_names(root)
    used = {t.name for t in root.find_all(exp.Table) if t.name not in ctes}
    return [name for name in dict.fromkeys((dataset, GRID_TABLE)) if name in used] or [dataset]


def keeps_order(text: str, dataset: str) -> bool:
    return _keeps_order(sqlglot.parse_one(text, read="duckdb"), {GRID_TABLE, dataset})


def _keeps_order(root: Any, grid_names: set[str]) -> bool:
    if root.args.get("order"):
        return True
    if not isinstance(root, exp.Select) or root.args.get("with_"):
        return False
    if root.args.get("group") or root.args.get("distinct") or root.args.get("joins"):
        return False
    source = root.args.get("from_") or root.args.get("from")
    table = source.this if source is not None else None
    if not isinstance(table, exp.Table) or table.name not in grid_names:
        return False
    return not any(root.find_all(exp.Window, exp.AggFunc))


def _first_line(message: str) -> str:
    return message.strip().splitlines()[0] if message.strip() else message


def _function_name(node: exp.Func) -> str:
    return (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()


def _refuse_denied_functions(query: exp.Expression) -> None:
    table_functions = table_only_functions()
    for node in query.find_all(exp.Func):
        name = _function_name(node)
        if name.startswith(_DENIED_PREFIXES) or name in table_functions:
            raise SqlRefused(f"The function {name}() is not allowed here.")
    for statement in query.find_all(exp.Command, exp.Pragma, exp.Set, exp.Use):
        raise SqlRefused(f"{statement.key.upper()} statements are not allowed.")


def _cte_names(query: exp.Expression) -> set[str]:
    return {cte.alias for cte in query.find_all(exp.CTE)}


def _rewrite_tables(
    query: exp.Expression, datasets: set[str], grid_names: set[str], as_views: bool
) -> tuple[bool, list[str], set[str]]:
    """Point each table name at its real table (`as_views`: at the CTE of its tab's view).
    `grid_names` read the grid view (empty: there is none). Returns whether the grid view is
    used, the datasets read, in order, and every table name the rewritten query reads."""
    ctes = list(query.find_all(exp.CTE))
    fresh: dict[int, str] = {}
    for number, cte in enumerate(ctes):
        if cte.alias.lower().startswith(_RESERVED):
            raise SqlRefused(f"Names starting with {_RESERVED!r} are reserved.")
        fresh[id(cte)] = f"{_FRESH_CTE}{number}"
    targets: set[str] = set(fresh.values())
    uses_grid = False
    read: list[str] = []
    for table in list(query.find_all(exp.Table)):
        if isinstance(table.this, exp.Func) and _function_name(table.this) in _ALLOWED_TABLE_FUNCTIONS:
            continue
        if not isinstance(table.this, exp.Identifier):
            raise SqlRefused("Read from grid, a dataset or a CTE by name; table functions are not allowed.")
        if table.db or table.catalog:
            raise SqlRefused(f"{table.sql()} is not allowed; use a plain dataset name.")
        name = table.name
        if name.lower().startswith(_RESERVED):
            raise SqlRefused(f"Names starting with {_RESERVED!r} are reserved.")
        named = _visible_cte(table)
        if named is not None:
            target = fresh[id(named)]
        elif name in grid_names:
            uses_grid = True
            target = _VIEW_CTE
        elif name == GRID_TABLE:
            raise SqlRefused("There is no grid view here; read a dataset by name.")
        elif name in datasets:
            target = _DATASET_VIEW_CTE + name if as_views else "ds_" + name
            if name not in read:
                read.append(name)
        else:
            known = ", ".join(sorted({*grid_names, *datasets}))
            raise SqlRefused(f"Unknown table {name!r}. Available: {known}.")
        targets.add(target)
        table.set("this", exp.to_identifier(target, quoted=True))
        # The table keeps its own name as its alias, so `name.column` still resolves; an alias
        # the user gave (with any column names) stays as written.
        if not table.args.get("alias"):
            table.set("alias", exp.TableAlias(this=exp.to_identifier(name, quoted=True)))
    for cte in ctes:
        alias: Any = cte.args.get("alias")
        alias.set("this", exp.to_identifier(fresh[id(cte)], quoted=True))
    return uses_grid, read, targets


def _visible_cte(table: exp.Table) -> exp.CTE | None:
    """The CTE `table` names, if one is in scope where it appears (DuckDB's rules, matched
    without case): an enclosing query's CTEs, and inside a CTE's body only the CTEs before it
    (and itself, in a WITH RECURSIVE)."""
    name = table.name.lower()
    child: Any = table
    node: Any = table.parent
    while node is not None:
        visible: list[Any] = []
        if isinstance(node, exp.With):
            ctes = list(node.expressions)
            index = next((i for i, cte in enumerate(ctes) if cte is child), None)
            if index is not None:
                visible = ctes[: index + 1] if node.args.get("recursive") else ctes[:index]
        else:
            with_ = node.args.get("with_")
            if isinstance(with_, exp.With) and with_ is not child:
                visible = list(with_.expressions)
        for cte in reversed(visible):
            if isinstance(cte, exp.CTE) and cte.alias.lower() == name:
                return cte
        child, node = node, node.parent
    return None


def _prepend_cte(query: exp.Expression, name: str, body: str) -> exp.Expression:
    cte_query = sqlglot.parse_one(body, read="duckdb")
    cte = exp.CTE(this=cte_query, alias=exp.TableAlias(this=exp.to_identifier(name, quoted=True)))
    existing = query.args.get("with_")
    if existing is not None:
        existing.set("expressions", [cte, *existing.expressions])
    else:
        query.set("with_", exp.With(expressions=[cte]))
    return query
