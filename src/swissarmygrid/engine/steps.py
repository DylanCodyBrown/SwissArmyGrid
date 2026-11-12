"""Steps: a grid's "applied steps" and how they compile to DuckDB SQL (plan section 7).

A step is plain data, {id, kind, params, enabled}, never code. A grid's view is its dataset run
through the enabled steps in order. Like Power Query, each step is one stage, compiled here to
one common table expression (CTE) over the previous stage:

    WITH s0 AS (SELECT *, rowid AS __sag_ord FROM "ds_orders"),
         s1 AS (SELECT * FROM s0 WHERE ...),        -- step 1: filter
         s2 AS (SELECT * REPLACE (...) FROM s1)     -- step 2: sort
    SELECT * EXCLUDE (__sag_ord) FROM s2 ORDER BY __sag_ord

"Preview at step N" is the same query ending at sN, so it costs nothing extra to build.

Row order is explicit. SQL relations have no order, so every stage carries a hidden __sag_ord
column and the final query sorts by it. Filters keep it; a sort step renumbers it; group-by
orders groups by their keys; distinct keeps each row's first position.

Reference semantics (the code generators in codegen/ must reproduce all of these):
- SQL blanks: a blank never equals anything, so `not equals`, `not in` and range filters
  drop blank rows too.
- Sorting puts blanks last in both directions; ties keep the previous order.
- Group by keeps the blank group; groups come out sorted by their keys, blanks last.
- With the case setting off, both sides of a text comparison are lowercased with DuckDB's
  lower(), which maps one character to one character (unlike Python's str.lower()).
- The only wildcard is `%` (any run of characters, as in SQL LIKE); `*`, `?` and `_` are
  literal. Translated to LIKE with `!` as the escape character for `_` and `!`.
  (DuckDB 2.0's LIKE cannot escape a backslash used as the escape character itself.)
- Weeks start on Monday (DuckDB date_trunc('week')).
- A SQL step is a SELECT over the stage before it, named `grid` or the dataset's own name (so
  the text runs unchanged in the user's Python), checked by the same guard as the
  SQL box (engine/sql_guard.guard_step). Its rows are numbered in the order the query returns
  them; a query with no defined order is sorted by every column first.

Every value from a step goes into SQL as a validated literal (`literal()`), and every name as
a quoted identifier (`quote()`), so step parameters can never inject SQL.
"""

from __future__ import annotations

import itertools
import math
import secrets
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, TypeGuard

import duckdb

from swissarmygrid.engine.datasets import Column
from swissarmygrid.engine.sql_guard import SqlRefused, guard_step

ORD: str = "__sag_ord"
RESERVED_PREFIX: str = "__sag_"
# Spaces, tabs and line breaks (plan section 7, "trim").
TRIM_CHARACTERS: str = " \t\r\n"
_TRIM_SQL: str = "(' ' || chr(9) || chr(13) || chr(10))"
LIKE_ESCAPE: str = "!"

KINDS: tuple[str, ...] = (
    "filter",
    "sort",
    "columns",
    "rename",
    "group_by",
    "distinct",
    "limit",
    "transform",
    "bucket",
    "categorize",
    "sql",
)
FILTER_OPS: tuple[str, ...] = (
    "eq",
    "ne",
    "in",
    "not_in",
    "blank",
    "not_blank",
    "contains",
    "starts_with",
    "ends_with",
    "lt",
    "gt",
    "between",
    "before",
    "after",
    "date_between",
    "true",
    "false",
)
AGGREGATES: tuple[str, ...] = (
    "count_rows",
    "count",
    "distinct_count",
    "sum",
    "min",
    "max",
    "mean",
)
TRANSFORMS: tuple[str, ...] = ("trim_left", "trim_right", "trim_both", "upper", "lower")
DATE_UNITS: tuple[str, ...] = ("day", "week", "month", "quarter", "year")
RULE_TYPES: tuple[str, ...] = ("wildcard", "in", "contains", "starts_with", "range")


# ===== Types =====


def type_family(duckdb_type: str) -> str:
    """Group a DuckDB type name into the families the operations care about."""
    t = duckdb_type.upper()
    if t.startswith(("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT")) or (
        t.startswith(("USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT", "FLOAT", "DOUBLE"))
        or t.startswith(("DECIMAL", "REAL"))
    ):
        return "number"
    if t == "DATE":
        return "date"
    if t.startswith("TIMESTAMP"):
        return "timestamp"
    if t == "BOOLEAN":
        return "boolean"
    if t == "VARCHAR":
        return "text"
    return "other"


@dataclass(frozen=True)
class ViewSettings:
    """The toolbar switches (plan section 7). Saved with the steps; read by the generators."""

    case_sensitive: bool = True
    wildcards: bool = False
    trim_spaces: bool = False
    empty_is_blank: bool = False

    @staticmethod
    def from_json(raw: Any) -> ViewSettings:
        if raw is None:
            return ViewSettings()
        if not isinstance(raw, dict) or not set(raw) <= {
            "case_sensitive",
            "wildcards",
            "trim_spaces",
            "empty_is_blank",
        }:
            raise StepError(None, "Settings must be an object of on/off switches.")
        if not all(isinstance(v, bool) for v in raw.values()):
            raise StepError(None, "Each setting must be true or false.")
        return ViewSettings(**raw)

    def to_json(self) -> dict[str, bool]:
        return {
            "case_sensitive": self.case_sensitive,
            "wildcards": self.wildcards,
            "trim_spaces": self.trim_spaces,
            "empty_is_blank": self.empty_is_blank,
        }


@dataclass(frozen=True)
class Step:
    kind: str
    params: dict[str, Any]
    enabled: bool = True
    id: str = field(default_factory=lambda: secrets.token_hex(4))

    @staticmethod
    def from_json(raw: Any, index: int) -> Step:
        if not isinstance(raw, dict):
            raise StepError(index, "Each step must be an object.")
        kind = raw.get("kind")
        if kind not in KINDS:
            raise StepError(index, f"Unknown step kind {kind!r}.")
        params = raw.get("params", {})
        if not isinstance(params, dict):
            raise StepError(index, "Step params must be an object.")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise StepError(index, "enabled must be true or false.")
        step_id = raw.get("id")
        if step_id is not None and not (isinstance(step_id, str) and 0 < len(step_id) <= 32):
            raise StepError(index, "id must be a short string.")
        return Step(kind=kind, params=params, enabled=enabled, id=step_id or secrets.token_hex(4))

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "params": self.params, "enabled": self.enabled}


def steps_from_json(raw: Any) -> list[Step]:
    if not isinstance(raw, list):
        raise StepError(None, "steps must be a list.")
    return [Step.from_json(item, index) for index, item in enumerate(raw)]


class StepError(ValueError):
    """A step that cannot apply. `index` is the step's position, or None for the whole list."""

    def __init__(self, index: int | None, message: str) -> None:
        super().__init__(message if index is None else f"Step {index + 1}: {message}")
        self.index = index
        self.message = message


# ===== SQL building blocks =====


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def literal(value: Any) -> str:
    """A safe SQL literal for a JSON value. Strings double their quotes; backslashes are
    literal in DuckDB (checked), so nothing else needs escaping."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Numbers must be finite.")
        return f"{value!r}::DOUBLE"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise ValueError(f"Unsupported value {value!r}.")


def wildcard_to_like(pattern: str) -> str:
    """Our one wildcard, `%` (any run of characters, as in SQL), as a LIKE pattern. LIKE's other
    wildcard `_` and the escape character are escaped, so every other character is literal."""
    out: list[str] = []
    for ch in pattern:
        if ch in ("_", LIKE_ESCAPE):
            out.append(LIKE_ESCAPE + ch)
        else:
            out.append(ch)
    return "".join(out)


def has_wildcards(text: str) -> bool:
    return "%" in text


def format_number(value: float) -> str:
    """How bucket edges print in labels: 10 not 10.0; 2.5 stays 2.5."""
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def bucket_edges(params: dict[str, Any]) -> list[float]:
    """The edge list a numeric bucket step uses. Shared with the code generators, so all
    four implementations bucket on exactly the same numbers."""
    mode = params.get("mode")
    if mode == "edges":
        edges = params.get("edges")
        if (
            not isinstance(edges, list)
            or len(edges) < 2
            or not all(_is_number(e) for e in edges)
            or any(b <= a for a, b in itertools.pairwise(edges))
        ):
            raise ValueError("edges must be at least two increasing numbers.")
        return [float(e) for e in edges]
    if mode == "width":
        width, low, high = params.get("width"), params.get("low"), params.get("high")
        if not (_is_number(width) and _is_number(low) and _is_number(high)):
            raise ValueError("A width bucket needs numbers width, low and high.")
        if width <= 0 or high <= low:
            raise ValueError("Need width > 0 and high > low.")
        count = math.ceil((high - low) / width)
        if count > 1000:
            raise ValueError("That width makes more than 1,000 buckets.")
        edges = [low + i * width for i in range(count + 1)]
        return [float(e) for e in edges]
    raise ValueError("mode must be edges, width or date.")


def bucket_labels(edges: list[float], right_closed: bool) -> list[str]:
    left, right = ("(", "]") if right_closed else ("[", ")")
    return [f"{left}{format_number(a)}, {format_number(b)}{right}" for a, b in itertools.pairwise(edges)]


def _is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


# ===== Compilation =====


@dataclass(frozen=True)
class Stage:
    """The relation after a step: the CTE name holding it and its visible columns."""

    name: str
    columns: tuple[Column, ...]


@dataclass(frozen=True)
class CompiledView:
    ctes: tuple[tuple[str, str], ...]
    # stages[0] is the source; stages[i + 1] is the result after step i (a disabled step
    # repeats the previous stage).
    stages: tuple[Stage, ...]
    # The first broken step, if any; stages stop just before it.
    error: StepError | None

    @property
    def columns(self) -> tuple[Column, ...]:
        return self.stages[-1].columns

    def stage_at(self, upto: int | None) -> Stage:
        """The stage after `upto` steps (None = every step that compiles)."""
        return self.stages[-1 if upto is None else max(0, min(upto, len(self.stages) - 1))]

    def columns_at(self, upto: int | None) -> tuple[Column, ...]:
        return self.stage_at(upto).columns

    def applied(self, upto: int | None) -> int:
        """How many CTEs past the source the stage uses; 0 means the raw dataset."""
        return int(self.stage_at(upto).name[1:])

    def sql(self, upto: int | None = None, keep_order_column: bool = False) -> str:
        """The view as one query. `upto` = number of steps applied (None = all that compile)."""
        stage = self.stage_at(upto)
        with_clause = ",\n     ".join(f"{name} AS ({body})" for name, body in self.ctes)
        select = "*" if keep_order_column else f"* EXCLUDE ({ORD})"
        return f"WITH {with_clause}\nSELECT {select} FROM {stage.name} ORDER BY {ORD}"


def compile_view(
    cursor: duckdb.DuckDBPyConnection,
    table: str,
    source_columns: tuple[Column, ...],
    steps: list[Step],
    settings: ViewSettings,
    dataset: str,
) -> CompiledView:
    """Compile `steps` over the dataset table `table` (already quoted). `dataset` is its name,
    which a SQL step may read in place of `grid`."""
    ctes: list[tuple[str, str]] = [("s0", f"SELECT *, rowid AS {ORD} FROM {table}")]
    stages: list[Stage] = [Stage("s0", source_columns)]
    for index, step in enumerate(steps):
        previous = stages[-1]
        if not step.enabled:
            stages.append(previous)
            continue
        name = f"s{len(ctes)}"
        try:
            body = _compile_step(step, previous, settings, dataset)
            candidate = [*ctes, (name, body)]
            columns = _describe(cursor, candidate, name)
            if step.kind == "sql":
                _check_sql_columns(columns)
        except StepError as error:
            return CompiledView(tuple(ctes), tuple(stages), StepError(index, error.message))
        except (ValueError, duckdb.Error) as error:
            return CompiledView(tuple(ctes), tuple(stages), StepError(index, str(error)))
        ctes = candidate
        stages.append(Stage(name, columns))
    return CompiledView(tuple(ctes), tuple(stages), None)


def _describe(
    cursor: duckdb.DuckDBPyConnection, ctes: list[tuple[str, str]], name: str
) -> tuple[Column, ...]:
    with_clause = ", ".join(f"{n} AS ({b})" for n, b in ctes)
    rows = cursor.execute(f"DESCRIBE WITH {with_clause} SELECT * FROM {name}").fetchall()
    return tuple(Column(str(r[0]), str(r[1])) for r in rows if r[0] != ORD)


def _check_sql_columns(columns: tuple[Column, ...]) -> None:
    names = [c.name for c in columns]
    if not names:
        raise StepError(None, "The query returns no columns.")
    if len(set(names)) != len(names):
        raise StepError(None, "The query returns two columns with the same name; alias one with AS.")
    for name in names:
        if name.startswith(RESERVED_PREFIX):
            raise StepError(None, f"Column names may not start with {RESERVED_PREFIX!r}.")


def _column(stage: Stage, name: Any) -> Column:
    if not isinstance(name, str):
        raise StepError(None, "column must be a column name.")
    for column in stage.columns:
        if column.name == name:
            return column
    raise StepError(None, f"Column {name!r} is not in the table at this step.")


def _new_name(stage: Stage, name: Any, replacing: set[str] | None = None) -> str:
    if not isinstance(name, str) or not name.strip():
        raise StepError(None, "A new column needs a name.")
    if name.startswith(RESERVED_PREFIX):
        raise StepError(None, f"Column names may not start with {RESERVED_PREFIX!r}.")
    existing = {c.name for c in stage.columns} - (replacing or set())
    if name in existing:
        raise StepError(None, f"There is already a column named {name!r}.")
    return name


def text_expression(expression: str, settings: ViewSettings) -> str:
    """Apply the toolbar text settings to one side of a comparison."""
    if settings.trim_spaces:
        expression = f"trim({expression}, {_TRIM_SQL})"
    if settings.empty_is_blank:
        expression = f"NULLIF({expression}, '')"
    if not settings.case_sensitive:
        expression = f"lower({expression})"
    return expression


def _typed_literal(column: Column, value: Any) -> str:
    """A literal compared against `column`, checked against the column's family."""
    family = type_family(column.type)
    if family == "number":
        if not _is_number(value):
            raise StepError(None, f"{column.name} is numeric; the value must be a number.")
        return literal(value)
    if family in ("date", "timestamp"):
        if not isinstance(value, str):
            raise StepError(None, f"{column.name} holds dates; give the value as YYYY-MM-DD.")
        try:
            parsed = date.fromisoformat(value) if family == "date" else datetime.fromisoformat(value)
        except ValueError as error:
            raise StepError(None, f"{value!r} is not an ISO date.") from error
        cast = "DATE" if family == "date" else "TIMESTAMP"
        return f"CAST({literal(parsed.isoformat())} AS {cast})"
    if family == "boolean":
        if not isinstance(value, bool):
            raise StepError(None, f"{column.name} is true/false; the value must be a boolean.")
        return literal(value)
    if family == "text":
        if not isinstance(value, str):
            raise StepError(None, f"{column.name} is text; the value must be text.")
        return literal(value)
    raise StepError(None, f"Filtering a {column.type} column is not supported.")


def filter_condition(stage: Stage, params: dict[str, Any], settings: ViewSettings) -> str:
    column = _column(stage, params.get("column"))
    op = params.get("op")
    if op not in FILTER_OPS:
        raise StepError(None, f"Unknown filter {op!r}.")
    family = type_family(column.type)
    raw = quote(column.name)
    is_text = family == "text"
    left = text_expression(raw, settings) if is_text else raw

    def right(value: Any) -> str:
        typed = _typed_literal(column, value)
        return text_expression(typed, settings) if is_text else typed

    if op == "blank":
        return f"({left}) IS NULL"
    if op == "not_blank":
        return f"({left}) IS NOT NULL"
    if op in ("eq", "ne"):
        value = params.get("value")
        if is_text and settings.wildcards and isinstance(value, str) and has_wildcards(value):
            pattern = text_expression(literal(wildcard_to_like(value)), settings)
            like = f"{left} LIKE {pattern} ESCAPE '{LIKE_ESCAPE}'"
            return like if op == "eq" else f"NOT ({like})"
        return f"{left} {'=' if op == 'eq' else '<>'} {right(value)}"
    if op in ("in", "not_in"):
        values = params.get("values")
        if not isinstance(values, list):
            raise StepError(None, "values must be a list.")
        if not values:
            return "FALSE" if op == "in" else "TRUE"
        items = ", ".join(right(v) for v in values)
        return f"{left} {'IN' if op == 'in' else 'NOT IN'} ({items})"
    if op in ("contains", "starts_with", "ends_with"):
        if not is_text:
            raise StepError(None, f"{op} works on text columns.")
        return f"{op}({left}, {right(params.get('value'))})"
    if op in ("lt", "gt", "between"):
        if family != "number":
            raise StepError(None, f"{op} works on number columns.")
    if op in ("before", "after", "date_between") and family not in ("date", "timestamp"):
        raise StepError(None, f"{op} works on date columns.")
    if op in ("lt", "before"):
        return f"{raw} < {right(params.get('value'))}"
    if op in ("gt", "after"):
        return f"{raw} > {right(params.get('value'))}"
    if op in ("between", "date_between"):
        return f"{raw} BETWEEN {right(params.get('low'))} AND {right(params.get('high'))}"
    # true / false
    if family != "boolean":
        raise StepError(None, f"{op} works on true/false columns.")
    return f"{raw} = {'TRUE' if op == 'true' else 'FALSE'}"


def sort_keys(stage: Stage, keys: Any) -> list[str]:
    if not isinstance(keys, list) or not keys:
        raise StepError(None, "Sort needs at least one [column, asc|desc] key.")
    out: list[str] = []
    for key in keys:
        if not (isinstance(key, list) and len(key) == 2 and key[1] in ("asc", "desc")):
            raise StepError(None, "Each sort key is [column, 'asc' or 'desc'].")
        column = _column(stage, key[0])
        out.append(f"{quote(column.name)} {'DESC' if key[1] == 'desc' else 'ASC'} NULLS LAST")
    return out


def categorize_conditions(
    stage: Stage, params: dict[str, Any], settings: ViewSettings
) -> tuple[Column, list[tuple[str, str]]]:
    """(source column, [(condition, label), ...]) for a categorize step."""
    column = _column(stage, params.get("column"))
    rules = params.get("rules")
    if not isinstance(rules, list) or not rules:
        raise StepError(None, "Categorize needs at least one rule.")
    family = type_family(column.type)
    left = text_expression(quote(column.name), settings) if family == "text" else quote(column.name)
    out: list[tuple[str, str]] = []
    for position, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict) or rule.get("type") not in RULE_TYPES:
            raise StepError(None, f"Rule {position} needs a type: {', '.join(RULE_TYPES)}.")
        label = rule.get("label")
        if not isinstance(label, str):
            raise StepError(None, f"Rule {position} needs a text label.")
        kind = rule["type"]
        if kind == "range":
            if family != "number":
                raise StepError(None, "A range rule needs a number column.")
            low, high = rule.get("low"), rule.get("high")
            if not (_is_number(low) and _is_number(high)):
                raise StepError(None, f"Rule {position} needs numbers low and high.")
            condition = f"({quote(column.name)} >= {literal(low)} AND {quote(column.name)} < {literal(high)})"
        else:
            if family != "text":
                raise StepError(None, f"A {kind} rule needs a text column.")
            if kind == "in":
                values = rule.get("values")
                if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                    raise StepError(None, f"Rule {position} needs a list of text values.")
                items = ", ".join(text_expression(literal(v), settings) for v in values) or "NULL"
                condition = f"{left} IN ({items})"
            else:
                value = rule.get("value")
                if not isinstance(value, str):
                    raise StepError(None, f"Rule {position} needs a text value.")
                if kind == "wildcard":
                    pattern = text_expression(literal(wildcard_to_like(value)), settings)
                    condition = f"{left} LIKE {pattern} ESCAPE '{LIKE_ESCAPE}'"
                else:
                    condition = f"{kind}({left}, {text_expression(literal(value), settings)})"
        out.append((f"coalesce({condition}, FALSE)", label))
    return column, out


def _compile_step(step: Step, stage: Stage, settings: ViewSettings, dataset: str) -> str:
    p = step.params
    src = stage.name
    if step.kind == "filter":
        return f"SELECT * FROM {src} WHERE {filter_condition(stage, p, settings)}"

    if step.kind == "sort":
        keys = sort_keys(stage, p.get("keys"))
        return f"SELECT * REPLACE (row_number() OVER (ORDER BY {', '.join(keys)}, {ORD}) AS {ORD}) FROM {src}"

    if step.kind == "columns":
        return f"SELECT {', '.join(column_list(stage, p))}, {ORD} FROM {src}"

    if step.kind == "rename":
        return f"SELECT {', '.join(rename_list(stage, p))}, {ORD} FROM {src}"

    if step.kind == "group_by":
        key_sql, agg_sql = group_parts(stage, p)
        select = ", ".join([*key_sql, *agg_sql])
        if not key_sql:
            return f"SELECT {select}, 1 AS {ORD} FROM {src}"
        order = ", ".join(group_order(key_sql))
        return (
            f"SELECT {select}, row_number() OVER (ORDER BY {order}) AS {ORD} "
            f"FROM {src} GROUP BY {', '.join(key_sql)}"
        )

    if step.kind == "distinct":
        names_sql = ", ".join(quote(c.name) for c in stage.columns)
        return f"SELECT {names_sql}, min({ORD}) AS {ORD} FROM {src} GROUP BY {names_sql}"

    if step.kind == "limit":
        return f"SELECT * FROM {src} ORDER BY {ORD} LIMIT {limit_count(p)}"

    if step.kind == "transform":
        replaced = transform_list(stage, p)
        if not replaced:
            return f"SELECT * FROM {src}"
        return f"SELECT * REPLACE ({', '.join(replaced)}) FROM {src}"

    if step.kind == "bucket":
        name, expression = bucket_expression(stage, p)
        return f"SELECT *, {expression} AS {quote(name)} FROM {src}"

    if step.kind == "sql":
        text = p.get("sql")
        if not isinstance(text, str) or not text.strip():
            raise StepError(None, 'A SQL step needs "sql": a SELECT over grid.')
        try:
            query = guard_step(text, f"SELECT * EXCLUDE ({ORD}) FROM {src} ORDER BY {ORD}", dataset)
        except SqlRefused as error:
            raise StepError(None, str(error)) from error
        # Numbered in the order the (ordered) query returns its rows; checked in verified.md.
        return f"SELECT *, row_number() OVER () AS {ORD} FROM ({query})"

    # categorize
    name, expression = categorize_expression(stage, p, settings)
    return f"SELECT *, {expression} AS {quote(name)} FROM {src}"


# ----- One step's SQL pieces. The engine wraps them in a CTE that carries the order column;
# codegen/duckdb_code.py puts the same pieces into the one query the user copies. -----


def column_list(stage: Stage, p: dict[str, Any]) -> list[str]:
    """A columns step: the kept columns, quoted, in the chosen order."""
    keep = p.get("keep")
    if not isinstance(keep, list) or not keep or len(set(keep)) != len(keep):
        raise StepError(None, "keep must be a non-empty list of distinct column names.")
    return [quote(_column(stage, name).name) for name in keep]


def rename_list(stage: Stage, p: dict[str, Any]) -> list[str]:
    """A rename step: every column in place, the renamed ones as `"old" AS "new"`."""
    mapping = p.get("mapping")
    if not isinstance(mapping, dict) or not mapping:
        raise StepError(None, "mapping must be an object of old name to new name.")
    for old in mapping:
        _column(stage, old)
    final = [mapping.get(c.name, c.name) for c in stage.columns]
    if len(set(final)) != len(final):
        raise StepError(None, "Renaming would leave two columns with the same name.")
    for new in mapping.values():
        _new_name(stage, new, replacing=set(mapping) | {new} & {c.name for c in stage.columns})
    return [
        f"{quote(c.name)} AS {quote(mapping[c.name])}" if c.name in mapping else quote(c.name)
        for c in stage.columns
    ]


def group_parts(stage: Stage, p: dict[str, Any]) -> tuple[list[str], list[str]]:
    """A group-by step: (the quoted key columns, the `aggregate AS "name"` items)."""
    keys = p.get("keys", [])
    aggregates = p.get("aggregates")
    if not isinstance(keys, list) or len(set(keys)) != len(keys):
        raise StepError(None, "keys must be a list of distinct column names.")
    key_sql = [quote(_column(stage, k).name) for k in keys]
    if not isinstance(aggregates, list) or not aggregates:
        raise StepError(None, "Group by needs at least one aggregate.")
    used_names: set[str] = set(keys)
    agg_sql = [_aggregate(stage, agg, used_names) for agg in aggregates]
    return key_sql, agg_sql


def group_order(key_sql: list[str]) -> list[str]:
    """Groups come out sorted by their keys, blanks last."""
    return [f"{k} ASC NULLS LAST" for k in key_sql]


def limit_count(p: dict[str, Any]) -> int:
    n = p.get("n")
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise StepError(None, "n must be a whole number, 0 or more.")
    return n


def transform_list(stage: Stage, p: dict[str, Any]) -> list[str]:
    """A transform step: `fn("col") AS "col"` for each target column (none: nothing changes)."""
    fn = p.get("fn")
    if fn not in TRANSFORMS:
        raise StepError(None, f"fn must be one of {', '.join(TRANSFORMS)}.")
    sql_fn = {
        "trim_left": f"ltrim({{}}, {_TRIM_SQL})",
        "trim_right": f"rtrim({{}}, {_TRIM_SQL})",
        "trim_both": f"trim({{}}, {_TRIM_SQL})",
        "upper": "upper({})",
        "lower": "lower({})",
    }[fn]
    return [f"{sql_fn.format(quote(c))} AS {quote(c)}" for c in transform_targets(stage, p.get("columns"))]


def bucket_expression(stage: Stage, p: dict[str, Any]) -> tuple[str, str]:
    """A bucket step: (the new column's name, its expression)."""
    column = _column(stage, p.get("column"))
    name = _new_name(stage, p.get("as"))
    source = quote(column.name)
    family = type_family(column.type)
    if p.get("mode") == "date":
        if family not in ("date", "timestamp"):
            raise StepError(None, "A date bucket needs a date column.")
        unit = p.get("unit")
        if unit not in DATE_UNITS:
            raise StepError(None, f"unit must be one of {', '.join(DATE_UNITS)}.")
        return name, f"CAST(date_trunc('{unit}', {source}) AS DATE)"
    if family != "number":
        raise StepError(None, "A number bucket needs a number column.")
    try:
        edges = bucket_edges(p)
    except ValueError as error:
        raise StepError(None, str(error)) from error
    right_closed = p.get("right_closed", False)
    if not isinstance(right_closed, bool):
        raise StepError(None, "right_closed must be true or false.")
    other = p.get("other_label", "Other")
    if not isinstance(other, str):
        raise StepError(None, "other_label must be text.")
    whens: list[str] = []
    for (a, b), label in zip(itertools.pairwise(edges), bucket_labels(edges, right_closed), strict=True):
        low_op, high_op = (">", "<=") if right_closed else (">=", "<")
        whens.append(
            f"WHEN {source} {low_op} {literal(a)} AND {source} {high_op} {literal(b)} THEN {literal(label)}"
        )
    return name, f"CASE WHEN {source} IS NULL THEN NULL {' '.join(whens)} ELSE {literal(other)} END"


def categorize_expression(stage: Stage, p: dict[str, Any], settings: ViewSettings) -> tuple[str, str]:
    """A categorize step: (the new column's name, its CASE expression; the first matching rule wins)."""
    name = _new_name(stage, p.get("as"))
    column, conditions = categorize_conditions(stage, p, settings)
    unmatched = p.get("unmatched", "Other")
    if not isinstance(unmatched, str):
        raise StepError(None, "unmatched must be text.")
    rule_whens = " ".join(f"WHEN {c} THEN {literal(label)}" for c, label in conditions)
    return (
        name,
        f"CASE WHEN {quote(column.name)} IS NULL THEN NULL {rule_whens} ELSE {literal(unmatched)} END",
    )


def transform_targets(stage: Stage, columns: Any) -> list[str]:
    """The text columns a transform applies to. "*text" means every text column."""
    if columns == "*text":
        return [c.name for c in stage.columns if type_family(c.type) == "text"]
    if not isinstance(columns, list) or not columns:
        raise StepError(None, 'columns must be a list of names or "*text".')
    out: list[str] = []
    for name in columns:
        column = _column(stage, name)
        if type_family(column.type) != "text":
            raise StepError(None, f"{column.name} is not a text column.")
        out.append(column.name)
    return out


def aggregate_name(agg: dict[str, Any]) -> str:
    """The default output name of an aggregate, also used by the generators."""
    if isinstance(agg.get("as"), str) and agg["as"]:
        return str(agg["as"])
    if agg.get("fn") == "count_rows":
        return "rows"
    return f"{agg.get('fn')}_{agg.get('column')}"


def _aggregate(stage: Stage, agg: Any, names: set[str]) -> str:
    if not isinstance(agg, dict) or agg.get("fn") not in AGGREGATES:
        raise StepError(None, f"Each aggregate needs fn: {', '.join(AGGREGATES)}.")
    fn = agg["fn"]
    out_name = aggregate_name(agg)
    if out_name in names or out_name.startswith(RESERVED_PREFIX):
        raise StepError(None, f"Aggregate name {out_name!r} is already used.")
    names.add(out_name)
    if fn == "count_rows":
        return f"count(*) AS {quote(out_name)}"
    column = _column(stage, agg.get("column"))
    family = type_family(column.type)
    c = quote(column.name)
    if fn in ("sum", "mean") and family != "number":
        raise StepError(None, f"{fn} needs a number column.")
    sql = {
        "count": f"count({c})",
        "distinct_count": f"count(DISTINCT {c})",
        "sum": f"sum({c})",
        "min": f"min({c})",
        "max": f"max({c})",
        "mean": f"avg({c})",
    }[fn]
    return f"{sql} AS {quote(out_name)}"


def with_header_sort(steps: list[Step], keys: list[list[str]]) -> list[Step]:
    """Clicking a column header edits the trailing header-sort step (or adds/removes it)."""
    base = steps[:-1] if steps and steps[-1].params.get("header") else steps
    if not keys:
        return list(base)
    previous = steps[-1] if steps and steps[-1].params.get("header") else None
    step = Step(kind="sort", params={"keys": keys, "header": True})
    return [*base, replace(step, id=previous.id) if previous else step]
