"""Privacy level 0 for SQL an agent sends: counts and number statistics, never a value turned into one.

Level 0 promises an agent the schema, counts and profile numbers only. Checking the answer's type is
not enough: `max(ascii(substr(client, 3, 1)))`, `sum(length(client))`, `min(hash(client))` and
`count(*) FILTER (WHERE client = 'Acme')` all return a number, and each turns the text into a number
one piece at a time. So at level 0 every query an agent sends is checked before it runs (agent_query,
the SQL box's CREATE TABLE, the "+" tab, a SQL step, a table card), and so is every step it adds or
changes and every chart it asks for.

The rule. Each column the query reads is classed by where its data came from (masking.py traces a
column to its source columns): a *number* column holds numbers and was computed only from number
columns; every other column (text, dates, booleans, lists, structs, JSON, blobs, and anything that
cannot be traced) is a *value* column. A number column may be used in any way. A value column may only:

  * be counted: `count(col)`, `count(DISTINCT col)` (and `approx_count_distinct`);
  * be grouped by, as a bare name: `GROUP BY col` (a query that returns the key is refused by the
    output check, so only the group sizes come out, which a profile shows anyway);
  * be tested for blanks: `col IS NULL`, `col IS NOT NULL` (the profile's blank count);
  * pass through a subquery, CTE or new table unchanged (`SELECT col FROM ...`), where the same rule
    holds wherever it is read next.

Anything else on a value column is refused: a function or cast (`length`, `ascii`, `hash`, `year`,
`json_extract`, `len`, `regexp_matches`, `CAST`, `||`), a comparison or a sort (in WHERE, FILTER,
HAVING, CASE, JOIN ... ON, ORDER BY, a window's PARTITION BY or ORDER BY), an aggregate other than
count (`min`, `max`, `string_agg`, `list`), and a set operation (UNION, INTERSECT, EXCEPT) over it,
since each compares or mixes the values with ones the agent chose. A query whose answer is returned to
the agent must also return number columns only.

The user's own queries never come here. Like the other privacy rules this stops an agent that only
has the MCP tools (see routes_agent).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.optimizer import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.schema import ensure_schema

from swissarmygrid.engine.datasets import DatasetInfo
from swissarmygrid.engine.steps import CompiledView, Step, ViewSettings, type_family
from swissarmygrid.server.context import Context
from swissarmygrid.server.grids import Grid
from swissarmygrid.server.masking import Origins

# table name (lower case, as the agent writes it) -> column name (lower case) -> a number column?
Classes = Mapping[str, Mapping[str, bool]]

_COUNTS: tuple[type[exp.Expr], ...] = (exp.Count, exp.ApproxDistinct)
_CHILD_SCOPES: tuple[type[exp.Expr], ...] = (exp.Select, exp.SetOperation, exp.Subquery)
# Columns read by name, by position (#1), or the whole row (to_json(t)): the last two can't be traced.
_COLUMN_REFS: tuple[type[exp.Expr], ...] = tuple(
    kind
    for kind in (getattr(exp, name, None) for name in ("Column", "PositionalColumn", "TableColumn"))
    if isinstance(kind, type)
)

GUIDE_HINT = (
    "At privacy level 0 a text, date or other non-number column may only be counted (count, "
    "count(DISTINCT)), grouped by without returning it, tested with IS NULL, or passed through "
    "unchanged. The user can raise the level in Settings."
)


class ValueUse(Exception):
    """The query uses a value column in a way level 0 does not allow. The message says where."""


def is_number_type(duckdb_type: str) -> bool:
    return type_family(duckdb_type) == "number"


def origins_are_numbers(origins: Origins, base: Mapping[str, bool]) -> bool:
    """True when every source column `origins` names is a number column in every dataset that has
    one of that name (`base`). An untraced column (None) is not."""
    return origins is not None and all(base.get(name) is True for name in origins)


def value_use_refusal(sql: str, tables: Classes, returns_rows: bool) -> str | None:
    """None when `sql` keeps to level 0, else the plain reason it does not. `returns_rows`: the
    answer goes back to the agent, so each output column must be a number column too."""
    try:
        _check(sql, tables, returns_rows)
    except ValueUse as refused:
        return f"{refused} {GUIDE_HINT}"
    return None


def _check(sql: str, tables: Classes, returns_rows: bool) -> None:
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception as error:
        raise ValueUse(
            "The query could not be read, so it could not be checked at privacy level 0."
        ) from error
    if isinstance(tree, exp.Create):
        inner = tree.expression
        if not isinstance(inner, exp.Query):
            raise ValueUse("Only CREATE TABLE name AS SELECT ... can be checked at privacy level 0.")
        tree = inner
    if not isinstance(tree, exp.Query):
        raise ValueUse("Only a SELECT can be checked at privacy level 0.")
    if tree.find(exp.Pivot) is not None:
        # UNPIVOT puts several columns' values (one of them could be the agent's literal) into one.
        raise ValueUse("PIVOT and UNPIVOT mix the values of several columns, so at level 0 they are refused.")
    schema = ensure_schema(
        {
            name: {column: "DOUBLE" if number else "VARCHAR" for column, number in columns.items()}
            for name, columns in tables.items()
        },
        dialect="duckdb",
    )
    try:
        query = qualify.qualify(
            tree, schema=schema, dialect="duckdb", validate_qualify_columns=False, identify=False
        )
        scopes = list(traverse_scope(query))
    except Exception as error:
        raise ValueUse(
            "The query's columns could not be traced, so it could not be checked at level 0."
        ) from error
    known = {name.lower(): {c.lower(): n for c, n in columns.items()} for name, columns in tables.items()}
    outputs: dict[int, dict[str, bool]] = {}  # id(scope.expression) -> output column -> number column?
    root: Scope | None = None
    for scope in scopes:  # innermost first
        outputs[id(scope.expression)] = _check_scope(scope, known, outputs)
        root = scope
    if root is None:
        raise ValueUse("The query could not be checked at privacy level 0.")
    if returns_rows:
        values = [name for name, number in outputs[id(root.expression)].items() if not number]
        if values:
            raise ValueUse(f"The answer would return the values of {', '.join(values)}.")


def _check_scope(
    scope: Scope, known: Mapping[str, Mapping[str, bool]], outputs: Mapping[int, Mapping[str, bool]]
) -> dict[str, bool]:
    node = scope.expression
    if isinstance(node, exp.SetOperation):
        sides = [outputs.get(id(side.expression)) for side in scope.set_operation_scopes]
        if len(sides) != 2 or any(side is None for side in sides):
            raise ValueUse("A UNION, INTERSECT or EXCEPT could not be checked at privacy level 0.")
        left, right = (dict(side or {}) for side in sides)
        for side in (left, right):
            values = [name for name, number in side.items() if not number]
            if values:
                raise ValueUse(
                    f"A {node.key.upper()} over {', '.join(values)} compares or mixes its values with others."
                )
        merged = {name: True for name in left}
        _check_expressions(scope, node, known, outputs, merged)
        return merged
    if not isinstance(node, exp.Select):
        # A table function in FROM (unnest(col)): what it reads is checked; its columns are untraced.
        _check_expressions(scope, node, known, outputs, {})
        return {}
    # The output columns first, so a reference to an alias (ORDER BY n) resolves.
    own: dict[str, bool] = {}
    for projection in node.expressions:
        target = projection.this if isinstance(projection, exp.Alias) else projection
        while isinstance(target, exp.Paren):
            target = target.this
        number = True
        if _is_value_ref(target):
            number = _class_of(target, scope, known, outputs, {})
        own[projection.alias_or_name.lower()] = number
    for join in node.args.get("joins") or []:
        if str(join.args.get("method") or "").upper() == "NATURAL":
            raise ValueUse("A NATURAL JOIN compares every column the two tables share.")
        for using in join.args.get("using") or []:
            name = using.name.lower()
            for source in scope.sources.values():
                if _source_column(source, name, known, outputs) is False:
                    raise ValueUse(f"JOIN ... USING ({using.name}) compares the values of {using.name}.")
    _check_expressions(scope, node, known, outputs, own)
    return own


def _check_expressions(
    scope: Scope,
    node: exp.Expr,
    known: Mapping[str, Mapping[str, bool]],
    outputs: Mapping[int, Mapping[str, bool]],
    own: Mapping[str, bool],
) -> None:
    """Every column and subquery this scope reads (not those of the scopes inside it), each checked
    for where it stands."""
    for found in node.walk(prune=lambda n: n is not node and isinstance(n, _CHILD_SCOPES)):
        if found is node:
            continue
        if isinstance(found, exp.Star):
            if not isinstance(found.parent, _COUNTS) and not isinstance(found.parent, exp.Column):
                raise ValueUse("A * that could not be expanded to named columns can't be checked at level 0.")
            continue
        if isinstance(found, exp.Placeholder | exp.Parameter):
            raise ValueUse("Parameters can't be checked at privacy level 0.")
        if isinstance(found, _CHILD_SCOPES):
            if _is_source(found):
                continue
        elif not _is_value_ref(found):
            continue
        if not _class_of(found, scope, known, outputs, own) and not _allowed_place(found, node):
            raise ValueUse(
                f"{_name(found)} is used as {_place(found)}, which turns its values into numbers or "
                "comparisons."
            )


def _is_value_ref(node: exp.Expr) -> bool:
    return isinstance(node, _COLUMN_REFS) or (isinstance(node, _CHILD_SCOPES) and not _is_source(node))


def _is_source(node: exp.Expr) -> bool:
    """A subquery read as a table (FROM, JOIN, a CTE's body, a side of a UNION), not as a value."""
    parent = node.parent
    if isinstance(node, exp.Subquery | exp.Select | exp.SetOperation) and isinstance(
        parent, exp.From | exp.Join | exp.CTE | exp.SetOperation | exp.Lateral
    ):
        return True
    return (
        isinstance(node, exp.Select | exp.SetOperation)
        and isinstance(parent, exp.Subquery)
        and _is_source(parent)
    )


def _class_of(
    node: exp.Expr,
    scope: Scope,
    known: Mapping[str, Mapping[str, bool]],
    outputs: Mapping[int, Mapping[str, bool]],
    own: Mapping[str, bool],
) -> bool:
    """True when `node` (a column or a scalar subquery) holds numbers that came from numbers."""
    if isinstance(node, exp.Column):
        name = node.name.lower()
        if not node.table:
            return own.get(name, False)  # an alias of this scope's own output, or unknown
        current: Scope | None = scope
        while current is not None:  # the column's table, here or in an outer scope (correlated)
            source = current.sources.get(node.table)
            if source is not None:
                return _source_column(source, name, known, outputs) is True
            current = current.parent
        return False
    if isinstance(node, _CHILD_SCOPES):
        inner = node.this if isinstance(node, exp.Subquery) else node
        classes = outputs.get(id(inner))
        return classes is not None and bool(classes) and all(classes.values())
    return False  # a positional #1 or anything else that can't be traced


def _source_column(
    source: exp.Expr | Scope,
    name: str,
    known: Mapping[str, Mapping[str, bool]],
    outputs: Mapping[int, Mapping[str, bool]],
) -> bool | None:
    """Whether column `name` of one FROM source is a number column; None when it has no such column."""
    if isinstance(source, exp.Table):
        columns = known.get(source.name.lower())
        return False if columns is None else columns.get(name)
    if isinstance(source, Scope):
        columns = outputs.get(id(source.expression))
        return False if columns is None else columns.get(name)
    return False  # a table function, an unnest, VALUES: untraced


def _allowed_place(node: exp.Expr, select: exp.Expr) -> bool:
    """Where a value column may stand at level 0 (see the module docstring)."""
    child, parent = node, node.parent
    while isinstance(parent, exp.Paren):
        child, parent = parent, parent.parent
    if isinstance(parent, exp.Alias):
        return parent.parent is select and parent.arg_key == "expressions"
    if parent is select:
        return child.arg_key == "expressions"
    if isinstance(parent, _COUNTS):
        return True
    if isinstance(parent, exp.Distinct) and isinstance(parent.parent, _COUNTS):
        return True
    if isinstance(parent, exp.Group):
        return True
    if isinstance(parent, exp.Is) and isinstance(parent.expression, exp.Null) and child is parent.this:
        return True
    return isinstance(parent, exp.Exists)


def _name(node: exp.Expr) -> str:
    if isinstance(node, exp.Column):
        return node.name
    return "a subquery's answer" if isinstance(node, _CHILD_SCOPES) else node.sql(dialect="duckdb")


def _place(node: exp.Expr) -> str:
    parent = node.parent
    while isinstance(parent, exp.Paren):
        parent = parent.parent
    if parent is None:
        return "a value"
    if isinstance(parent, exp.Ordered | exp.Order):
        return "a sort key"
    if isinstance(parent, exp.Window):
        return "a window's PARTITION BY"
    if isinstance(parent, exp.Predicate | exp.Binary | exp.In | exp.Between):
        return f"part of {parent.key.upper()}"
    if isinstance(parent, exp.Anonymous):
        return f"an argument of {parent.name}()"
    if isinstance(parent, exp.Func):
        return f"an argument of {parent.sql_name()}()"
    return f"part of {parent.key.upper()}"


# ===== the classes of what a query may read, from the server's datasets and grids =====


def base_numbers(ctx: Context) -> dict[str, bool]:
    """Each source column name (lower case) -> whether it is a number column in every loaded
    dataset that has it. Tables made by a query are left out: their columns' origins name these."""
    found: dict[str, bool] = {}
    for info in ctx.store.all():
        if info.name in ctx.derived_columns:
            continue
        for column in info.columns:
            name = column.name.lower()
            found[name] = found.get(name, True) and is_number_type(column.type)
    return found


def dataset_classes(ctx: Context, info: DatasetInfo, base: Mapping[str, bool]) -> dict[str, bool]:
    """A dataset's columns as loaded: a number column, and for a table made by a query, one that
    was computed from number columns only."""
    derived = ctx.derived_columns.get(info.name)
    return {
        c.name.lower(): is_number_type(c.type)
        and (derived is None or origins_are_numbers(derived.get(c.name.lower()), base))
        for c in info.columns
    }


def view_classes(
    ctx: Context, compiled: CompiledView, info: DatasetInfo, upto: int | None, base: Mapping[str, bool]
) -> dict[str, bool]:
    """A grid view's columns (the table entering step `upto`, or after every step): traced through
    the steps, so a number the user's steps computed from text (`length(client)`) is not a number
    column. A view that can't be traced is all value columns, unless it has no steps applied."""
    columns = compiled.columns_at(upto)
    if compiled.applied(upto) == 0:
        return dataset_classes(ctx, info, base)
    traced = ctx.trace(compiled.sql(upto))
    if traced is None or len(traced) != len(columns):
        return {c.name.lower(): False for c in columns}
    return {
        c.name.lower(): is_number_type(c.type) and origins_are_numbers(origins, base)
        for c, origins in zip(columns, traced, strict=True)
    }


def query_classes(
    ctx: Context,
    cur: duckdb.DuckDBPyConnection,
    grid: Grid | None,
    info: DatasetInfo | None,
    upto: int | None = None,
    datasets_as_tabs: bool = False,
) -> dict[str, dict[str, bool]]:
    """What a query may read, classed: every dataset by name (as loaded, or with `datasets_as_tabs`
    as its first tab shows it, the "+" tab's reading), and `grid` (the grid's view entering `upto`)."""
    base = base_numbers(ctx)
    tables: dict[str, dict[str, bool]] = {}
    for each in ctx.store.all():
        tab = ctx.grids.first_for(each.name) if datasets_as_tabs else None
        tables[each.name.lower()] = (
            view_classes(ctx, ctx.compiled(cur, tab, each), each, None, base)
            if tab is not None
            else dataset_classes(ctx, each, base)
        )
    if info is not None:
        tables["grid"] = (
            view_classes(ctx, ctx.compiled(cur, grid, info), info, upto, base)
            if grid is not None
            else dataset_classes(ctx, info, base)
        )
    return tables


# ===== steps, charts, and the tables an agent made =====

# Steps whose new column can hold values the agent typed (a SQL step's literals, rule labels).
VALUE_MAKING_KINDS: frozenset[str] = frozenset({"sql", "categorize", "bucket"})
_BLANK_OPS: frozenset[str] = frozenset({"blank", "not_blank"})
_VALUE_AGGREGATES: frozenset[str] = frozenset({"sum", "mean", "min", "max"})


def step_refusal(
    ctx: Context,
    cur: duckdb.DuckDBPyConnection,
    info: DatasetInfo,
    steps: list[Step],
    settings: ViewSettings,
    changed: list[int],
) -> str | None:
    """None when the steps at `changed` (the ones an agent added or changed) keep to level 0. A
    step compiles to SQL over the table entering it, so it is held to the same rule as the agent's
    own SQL: a filter, bucket or categorize rule on a value column, or a sum, mean, min or max of
    one, would turn its values into a count (the view's row count) or a number."""
    if not changed:
        return None
    compiled = ctx.views.compile(cur, info, steps, settings)
    base = base_numbers(ctx)
    for index in changed:
        step = steps[index]
        if len(compiled.stages) <= index:
            return (
                f"Step {index + 1} comes after a step that cannot apply, so at privacy level 0 it can't be "
                "checked. Fix or remove the earlier step first."
            )
        classes = view_classes(ctx, compiled, info, index, base)

        def number(column: object, classes: Mapping[str, bool] = classes) -> bool:
            return isinstance(column, str) and classes.get(column.lower(), False)

        p = step.params
        column = p.get("column")
        if step.kind == "filter" and p.get("op") not in _BLANK_OPS and not number(column):
            return (
                f"At privacy level 0 a filter on {column} (not a number column) may only keep blank or "
                "non-blank rows: a filter on its values, with the row count, would tell its values. "
                + GUIDE_HINT
            )
        if step.kind in ("bucket", "categorize") and not number(column):
            return (
                f"At privacy level 0 a {step.kind} step needs a number column; {column} is not. {GUIDE_HINT}"
            )
        if step.kind == "group_by":
            for aggregate in p.get("aggregates") or []:
                if (
                    isinstance(aggregate, dict)
                    and aggregate.get("fn") in _VALUE_AGGREGATES
                    and not number(aggregate.get("column"))
                ):
                    return (
                        f"At privacy level 0 {aggregate.get('fn')} needs a number column; "
                        f"{aggregate.get('column')} is not (count it instead). {GUIDE_HINT}"
                    )
        if step.kind == "sql" and isinstance(p.get("sql"), str):
            refused = value_use_refusal(
                p["sql"], {"grid": classes, info.name.lower(): classes}, returns_rows=False
            )
            if refused is not None:
                return refused
    return None


def chart_refusal(spec: object, classes: Mapping[str, bool]) -> str | None:
    """None when a chart spec keeps to level 0: its groups are bare columns (the agent sees only how
    many points there are), and only a number column is summed, averaged or has a min or max."""
    if not isinstance(spec, dict):
        return None  # chart validation says what is wrong
    x_raw, y_raw = spec.get("x"), spec.get("y")
    x: dict[str, Any] = x_raw if isinstance(x_raw, dict) else {}
    y: dict[str, Any] = y_raw if isinstance(y_raw, dict) else {}
    if "time_unit" in x and not classes.get(str(x.get("field")).lower(), False):
        return (
            f"At privacy level 0 a chart can't bucket {x.get('field')} by {x.get('time_unit')}. {GUIDE_HINT}"
        )
    field = y.get("field")
    if y.get("aggregate") in _VALUE_AGGREGATES and not classes.get(str(field).lower(), False):
        return f"At privacy level 0 {y.get('aggregate')} needs a number column; {field} is not. {GUIDE_HINT}"
    return None


def probe_reason(ctx: Context, grid: Grid) -> str | None:
    """Why a grid can't be matched against the user's data at level 0, or None. A table the agent
    made, or a grid with an agent step that adds values (a SQL step, a rule label), can hold values
    the agent chose, and how many of them match the user's would tell the user's values."""
    if grid.dataset in ctx.agent_made:
        return f"{grid.dataset} is a table an agent made"
    for step in grid.steps:
        if step.enabled and step.id in grid.agent_steps and step.kind in VALUE_MAKING_KINDS:
            return f"grid {grid.id} has a {step.kind} step an agent added"
    return None


def probe_refusal(ctx: Context, *grids: Grid) -> str | None:
    for grid in grids:
        reason = probe_reason(ctx, grid)
        if reason is not None:
            return (
                f"At privacy level 0 the Key Finder and compare match only tables as the user built them: "
                f"{reason}, which can hold values it chose, and the match counts would tell whether the "
                "user's data has them. The user can raise the level in Settings."
            )
    return None
