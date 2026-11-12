"""Which privacy mask applies to each column an agent reads, traced back to the columns it came from.

The user's masks name source columns ("client": hide). A rule that only looked at the name of each
column in a result was undone by any rename: a `rename` step, `SELECT client AS c`, `min(client)`,
`client || ''`, or a new table made by a query. So each column of what the agent reads is traced
through the SQL (sqlglot's column lineage) to the dataset columns it is computed from, and the
strictest rule among them applies. A table made by a query keeps the origins of its columns
(`Origins`), so a rule follows the data into it too.

Fail closed: when a column cannot be traced (a whole-row reference such as `to_json(t)`, a
positional `#1`, a correlated subquery, `COLUMNS(...)`, PIVOT, anything sqlglot cannot follow),
it gets the strictest rule the user has set. A column computed from no column at all (a literal,
`count(*)`) carries no data and is shown.

Text masking (level 1) works on the values themselves, not on the type's name: every string in a
result is masked, also inside lists, structs and JSON text, which a check for "VARCHAR" in the type
missed (`to_json(list(client))` is of type JSON).

Like the rest of the privacy rules this stops an agent that only has the MCP tools; see
routes_agent for the limitation stated plainly.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID

import sqlglot
from sqlglot import exp
from sqlglot.optimizer import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.schema import ensure_schema

# One column's origins: the lower-cased dataset column names it is computed from, or None when it
# could not be traced.
Origins = frozenset[str] | None

_STRICTNESS: dict[str, int] = {"hide": 2, "hash": 1}

# Nodes a column computed from no source column may contain and still carry no data.
_SOURCELESS_NODES: tuple[type[exp.Expr], ...] = (
    exp.Alias,
    exp.Literal,
    exp.Null,
    exp.Boolean,
    exp.Identifier,
    exp.Paren,
    exp.Neg,
    exp.Cast,
    exp.DataType,
    exp.DataTypeParam,
    exp.Count,
    exp.Star,
    # count(*) OVER (), row_number() OVER (): a window that names a column has a Column in it.
    exp.Window,
    exp.WindowSpec,
    exp.RowNumber,
    exp.Add,
    exp.Sub,
    exp.Mul,
    exp.Div,
)

# Nodes that read a row without naming a column: a subquery in the select list (it may be
# correlated), a positional #1 or a parameter, COLUMNS(...).
_ROW_REFERENCES: tuple[type[exp.Expr], ...] = tuple(
    kind
    for kind in (
        getattr(exp, name, None) for name in ("Subquery", "Select", "Placeholder", "Parameter", "Columns")
    )
    if isinstance(kind, type)
)

_CACHE_SIZE = 256
_cache: OrderedDict[str, list[Origins] | None] = OrderedDict()
_cache_lock = threading.Lock()


def _carries_no_data(expression: exp.Expr) -> bool:
    for node in expression.walk():
        if not isinstance(node, _SOURCELESS_NODES):
            return False
        if isinstance(node, exp.Star) and not isinstance(node.parent, exp.Count):
            return False
    return True


def trace(
    sql: str, tables: Mapping[str, Sequence[str]], derived: Mapping[str, Mapping[str, Origins]] | None = None
) -> list[Origins] | None:
    """The origins of each output column of `sql`, in order; None when the query as a whole can't be
    traced. `tables` maps every table the SQL may read (its plain name, e.g. ds_orders) to its
    columns; `derived` maps a table made by a query to the origins of its own columns."""
    derived = {k.lower(): v for k, v in (derived or {}).items()}
    key = repr((sql, sorted((t, tuple(c)) for t, c in tables.items()), sorted(derived.items(), key=repr)))
    digest = hashlib.sha256(key.encode()).hexdigest()
    with _cache_lock:
        if digest in _cache:
            _cache.move_to_end(digest)
            return _cache[digest]
    result = _trace(sql, tables, derived)
    with _cache_lock:
        _cache[digest] = result
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return result


def _trace(
    sql: str, tables: Mapping[str, Sequence[str]], derived: Mapping[str, Mapping[str, Origins]]
) -> list[Origins] | None:
    known = {name.lower() for name in tables}
    # Types do not matter for lineage; one that always parses keeps odd DuckDB types out of it.
    schema = ensure_schema(
        {name: dict.fromkeys(columns, "VARCHAR") for name, columns in tables.items()}, dialect="duckdb"
    )
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
        query = qualify.qualify(
            tree, schema=schema, dialect="duckdb", validate_qualify_columns=False, identify=False
        )
        if not isinstance(query, exp.Query):
            return None
        # One pass, innermost scopes first (CTEs, subqueries, each side of a UNION), so a scope's
        # columns are known before the scope reading them. (sqlglot's own per-column lineage walks
        # the whole CTE chain once per column: seconds for a wide grid with a few steps.)
        done: dict[int, list[tuple[str, Origins]] | None] = {}
        root: Scope | None = None
        for scope in traverse_scope(query):
            done[id(scope)] = _scope_origins(scope, known, derived, done)
            root = scope
        traced = done.get(id(root)) if root is not None else None
        if traced is None or len(traced) != len(query.selects):
            return None
        return [origins for _, origins in traced]
    except Exception:  # sqlglot cannot follow this query: every column counts as untraced
        return None


def _scope_origins(
    scope: Scope,
    known: set[str],
    derived: Mapping[str, Mapping[str, Origins]],
    done: Mapping[int, list[tuple[str, Origins]] | None],
) -> list[tuple[str, Origins]] | None:
    """Each output column of one scope, in order, with its origins."""
    if isinstance(scope.expression, exp.SetOperation):  # UNION, INTERSECT, EXCEPT: column by column
        if len(scope.set_operation_scopes) != 2:
            return None
        left, right = (done.get(id(side)) for side in scope.set_operation_scopes)
        if left is None or right is None or len(left) != len(right):
            return None
        return [
            (name, None if a is None or b is None else a | b)
            for (name, a), (_, b) in zip(left, right, strict=True)
        ]
    select = scope.expression
    if not isinstance(select, exp.Select):
        return None
    return [
        (projection.alias_or_name, _projection_origins(projection, scope, known, derived, done))
        for projection in select.selects
    ]


def _projection_origins(
    projection: exp.Expr,
    scope: Scope,
    known: set[str],
    derived: Mapping[str, Mapping[str, Origins]],
    done: Mapping[int, list[tuple[str, Origins]] | None],
) -> Origins:
    for node in projection.walk():
        if isinstance(node, _ROW_REFERENCES):
            return None  # reads the row some other way than by a column name
        if isinstance(node, exp.Star) and not isinstance(node.parent, exp.Count):
            return None  # qualify expands every star it can; one left over is not traceable
    columns = list(projection.find_all(exp.Column))
    if not columns:
        return frozenset() if _carries_no_data(projection) else None
    found: set[str] = set()
    for column in columns:
        source = scope.sources.get(column.table) if column.table else None
        name = column.name.lower()
        if isinstance(source, exp.Table):
            table = source.name.lower()
            if table not in known:
                return None
            if table in derived:
                inherited = derived[table].get(name)
                if inherited is None:
                    return None
                found |= inherited
            else:
                found.add(name)
        elif isinstance(source, Scope):
            inner = done.get(id(source))
            matches = [origins for n, origins in inner or [] if n.lower() == name]
            if not matches or any(m is None for m in matches):
                return None
            for match in matches:
                found |= match or frozenset()
        else:
            return None  # a whole-row reference (to_json(t)), a lambda variable, an unknown name
    return frozenset(found)


# ===== the rule for each column =====


def column_rules(
    level: int, masks: Mapping[str, str], names: Sequence[str], origins: Sequence[Origins] | None
) -> list[str]:
    """One of hide, hash, mask (text masked, level 1) or show for each output column.

    hide and hash: the strictest rule among the column's origins and its own name. mask: at level 1,
    unless every origin is marked show. A column that could not be traced gets the strictest rule
    the user set anywhere (and is masked at level 1)."""
    if level >= 3:
        return ["show"] * len(names)
    rules = {k.lower(): v for k, v in masks.items()}
    strictest = max(
        (v for v in rules.values() if v in _STRICTNESS), key=_STRICTNESS.__getitem__, default=None
    )
    decided: list[str] = []
    for i, name in enumerate(names):
        traced = origins[i] if origins is not None and i < len(origins) else None
        if origins is not None and len(origins) != len(names):
            traced = None  # the trace and the result disagree on the columns: trust neither
        if traced is None:
            if strictest is not None:
                decided.append(strictest)
            else:
                decided.append("mask" if level == 1 else "show")
            continue
        named = set(traced) | {name.lower()}
        hits = [rules[n] for n in named if rules.get(n) in _STRICTNESS]
        if hits:
            decided.append(max(hits, key=_STRICTNESS.__getitem__))
        elif level == 1 and not (traced or {name.lower()}) <= {n for n, v in rules.items() if v == "show"}:
            decided.append("mask")
        else:
            decided.append("show")
    return decided


# ===== masking values =====


def mask_text(text: str) -> str:
    return (text[:1] + "•" * min(len(text) - 1, 6)) if text else ""


def hash_value(value: Any) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()[:10]


def mask_strings(value: Any) -> Any:
    """`value` with every string in it masked, also inside lists and structs; numbers, dates and
    booleans stay. A UUID counts as text."""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, UUID):
        return mask_text(str(value))
    if isinstance(value, list | tuple):
        return [mask_strings(v) for v in value]
    if isinstance(value, dict):
        return {mask_text(str(k)) if isinstance(k, str) else k: mask_strings(v) for k, v in value.items()}
    return value


def has_strings(value: Any) -> bool:
    if isinstance(value, str | UUID):
        return True
    if isinstance(value, list | tuple):
        return any(has_strings(v) for v in value)
    if isinstance(value, dict):
        return any(isinstance(k, str) or has_strings(v) for k, v in value.items())
    return False


def is_plain_number(value: Any) -> bool:
    """A single number (or a blank): what a level 0 aggregate may return. A list of numbers is rows."""
    return value is None or isinstance(value, bool | int | float | Decimal)
