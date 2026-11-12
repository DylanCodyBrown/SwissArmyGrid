"""The primary copy: a grid's steps as ONE DuckDB query over the user's own table.

What SAG is for: DuckDB SQL does the transforming, so the copy is not a translation. The query
reads the table by the name the user pushed it under, which is their variable's name (DuckDB
reads a pandas, Polars or Arrow variable, or a DuckDB relation, by name), so it runs in their
Python as is:

    import duckdb

    orders_view = duckdb.sql(\"\"\"
    SELECT * FROM orders WHERE "amount" > 20 ORDER BY "amount" DESC NULLS LAST, row_number() OVER ()
    \"\"\").df()

"Copy as SQL" gives the query alone; "Copy as Python" gives these lines: `import duckdb` and one
statement, ending in the output form that matches what was pushed (.df() for pandas, .pl() for
Polars, .to_arrow_table() for Arrow, the relation itself for DuckDB; .df() when not knowable).

Every expression comes from the engine's own step pieces (engine/steps.py), so the copy computes
what the grid computes. What differs is how row order is kept: the engine numbers rows in a hidden
column; the copy has no such column, and relies instead on what docs/verified.md checks:
- DuckDB keeps a table's order through filters, projections, LIMIT and CTEs, and numbers an ordered
  input in order with `row_number() OVER ()`;
- ORDER BY is not stable, so a sort ends with `row_number() OVER ()`: ties keep their order;
- remove duplicates keeps each row's first position with a numbered subquery, the one place a
  helper column appears (inside the query; it is not in the result).

Consecutive steps share one SELECT where SQL evaluates them in the same order (WHERE, then the
select list, then ORDER BY, then LIMIT); any other step starts a new CTE named after its step
number, so the query reads top to bottom like the steps panel, with each step as a comment.
"""

from __future__ import annotations

import functools
import keyword
import re
from dataclasses import dataclass, field

import duckdb
import sqlglot
from sqlglot import exp

from swissarmygrid.engine.datasets import DatasetInfo
from swissarmygrid.engine.describe import describe_step
from swissarmygrid.engine.sql_guard import keeps_order, step_tables
from swissarmygrid.engine.steps import (
    CompiledView,
    Stage,
    Step,
    ViewSettings,
    bucket_expression,
    categorize_expression,
    column_list,
    filter_condition,
    group_order,
    group_parts,
    limit_count,
    quote,
    rename_list,
    sort_keys,
    transform_list,
)

# What the copied Python calls on the query's result, by what was pushed. Anything else (a list
# of dicts, a file, the clipboard) gets a pandas DataFrame, the most common table in Python.
OUTPUT_CALLS: dict[str, str] = {
    "pandas": ".df()",
    "polars": ".pl()",
    "arrow": ".to_arrow_table()",
    "duckdb": "",
}
DEFAULT_OUTPUT_CALL: str = ".df()"
_TIEBREAK: str = "row_number() OVER ()"
_SIMPLE_NAME: re.Pattern[str] = re.compile(r"[a-z_][a-z0-9_]*")


def output_call(source_kind: str) -> str:
    return OUTPUT_CALLS.get(source_kind, DEFAULT_OUTPUT_CALL)


def variable_name(dataset: str) -> str:
    """The Python variable holding the user's table: the dataset name, which is the name it was
    pushed from. A name that cannot be a variable (`my-orders`, `2026`, `class`) is made into one."""
    if dataset.isidentifier() and not keyword.iskeyword(dataset):
        return dataset
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in dataset)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"data_{cleaned}"
    return f"{cleaned}_" if keyword.iskeyword(cleaned) else cleaned


@functools.cache
def _keywords() -> frozenset[str]:
    with duckdb.connect(":memory:") as con:
        return frozenset(
            str(r[0]).lower() for r in con.execute("SELECT keyword_name FROM duckdb_keywords()").fetchall()
        )


def table_name_sql(name: str) -> str:
    """A table name as SQL: bare when that reads the same (lowercase, not a keyword), else quoted."""
    return name if _SIMPLE_NAME.fullmatch(name) and name not in _keywords() else quote(name)


def python_string(text: str) -> str:
    """`text` as a Python string literal that reads back exactly: a triple-quoted block when it
    can be one, else a plain literal."""
    # The text sits between newlines, so it never touches the quotes. Backslashes are literal
    # in DuckDB strings, so a block holding one is a raw string; any other control character
    # (a carriage return would be read back as a newline) gets an escaped literal.
    plain = all(ch in "\n\t" or ch.isprintable() for ch in text)
    if plain and '"""' not in text and "\\" not in text:
        return f'"""\n{text}\n"""'
    if plain and '"""' not in text:
        return f'r"""\n{text}\n"""'
    if plain and "'''" not in text:
        return f"r'''\n{text}\n'''"
    return repr(text)


def python_code(query: str, dataset: str, source_kind: str, *, with_output: bool = True) -> str:
    """The copy as Python: `import duckdb` and one statement over the user's variable."""
    var = variable_name(dataset)
    module = "duckdb" if var != "duckdb" else "ddb"  # a table named duckdb keeps its name
    imported = "import duckdb" if module == "duckdb" else "import duckdb as ddb"
    call = output_call(source_kind) if with_output else ""
    return f"{imported}\n\n{var}_view = {module}.sql({python_string(query)}){call}\n"


# ===== Steps to one query =====


@dataclass
class _Layer:
    """One SELECT. `shape` says what its select list does, which decides what may join it."""

    source: str
    shape: str = "plain"  # plain | columns | added | renamed | replaced | grouped | distinct | sql
    select: list[str] = field(default_factory=lambda: ["*"])
    added: set[str] = field(default_factory=set)
    where: list[str] = field(default_factory=list)
    group_by: list[str] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    limit: int | None = None
    body: str | None = None  # a SQL step: the user's own query
    notes: list[str] = field(default_factory=list)
    last_step: int = 0

    def empty(self) -> bool:
        return not self.notes

    def render(self) -> str:
        if self.body is not None:
            return self.body
        select = ", ".join(self.select)
        if len(select) > 90:
            select = "\n" + ",\n".join("    " + item for item in self.select)
        else:
            select = " " + select
        lines = [f"SELECT{select}", f"FROM {self.source}"]
        if self.where:
            lines.append("WHERE " + "\n  AND ".join(self.where))
        if self.group_by:
            lines.append("GROUP BY " + ", ".join(self.group_by))
        if self.order:
            note = "  -- ties keep their order" if self.order[-1] == _TIEBREAK else ""
            lines.append("ORDER BY " + ", ".join(self.order) + note)
        if self.limit is not None:
            lines.append(f"LIMIT {self.limit}")
        return "\n".join(lines)


def _note(number: int, step: Step) -> str:
    text = " ".join(describe_step(step).split())
    text = "".join(ch for ch in text if ch.isprintable())
    return f"{number}. {text[:117] + '...' if len(text) > 120 else text}"


class _Builder:
    def __init__(self, source: str, prefix: str) -> None:
        self.prefix = prefix
        self.done: list[_Layer] = []
        self.layer = _Layer(source)

    def name(self, layer: _Layer) -> str:
        return f"{self.prefix}{layer.last_step}"

    def start(self, shape: str) -> _Layer:
        """A new SELECT over everything so far."""
        if self.layer.empty():
            source = self.layer.source
        else:
            self.done.append(self.layer)
            source = self.name(self.layer)
        self.layer = _Layer(source, shape)
        return self.layer

    def query(self) -> str:
        last = self.layer
        if self.done and last.render().startswith("WITH "):
            # A last SQL step with its own WITH cannot follow the outer one: make it a CTE too.
            self.done.append(last)
            last = _Layer(self.name(last))
        return self._render(last)

    def _render(self, last: _Layer) -> str:
        blocks = [
            "".join(f"-- {n}\n" for n in layer.notes)
            + f"{self.name(layer)} AS (\n{_indent(layer.render())}\n)"
            for layer in self.done
        ]
        head = "WITH\n" + ",\n".join(blocks) + "\n" if blocks else ""
        return head + "".join(f"-- {n}\n" for n in last.notes) + last.render()


def _indent(text: str) -> str:
    return "\n".join("    " + line if line else line for line in text.splitlines())


def view_query(info: DatasetInfo, compiled: CompiledView, steps: list[Step], settings: ViewSettings) -> str:
    """The grid's view as one DuckDB query over the user's table (every step must compile)."""
    var = variable_name(info.name)
    source = table_name_sql(var)
    prefix = "stage_" if var.startswith("step_") else "step_"
    b = _Builder(source, prefix)
    for index, step in enumerate(steps):
        if not step.enabled:
            continue
        stage = compiled.stages[index]
        layer = _add_step(b, step, stage, settings, info.name, var)
        layer.notes.append(_note(index + 1, step))
        layer.last_step = index + 1
    return b.query()


def _add_step(
    b: _Builder, step: Step, stage: Stage, settings: ViewSettings, dataset: str, var: str
) -> _Layer:
    p = step.params
    kind = step.kind
    layer = b.layer
    open_ = layer.limit is None and layer.body is None
    # WHERE and new columns read the SELECT's input, so they may join a SELECT that only picks
    # or adds columns, as long as they do not read a column added in that same SELECT.
    reads_input = (
        open_ and layer.shape in ("plain", "columns", "added") and p.get("column") not in layer.added
    )

    if kind == "filter":
        if not reads_input:
            layer = b.start("plain")
        layer.where.append(filter_condition(stage, p, settings))
        return layer

    if kind == "sort":
        keys = sort_keys(stage, p.get("keys"))
        mergeable = layer.shape in ("plain", "columns", "added", "distinct") or (
            layer.shape == "grouped" and layer.group_by
        )
        if not (open_ and mergeable):
            layer = b.start("plain")
        # A stable sort: the new keys first, then the order the rows already had.
        layer.order = [*keys, *(layer.order or [_TIEBREAK])]
        return layer

    if kind == "limit":
        n = limit_count(p)
        if layer.body is not None:
            layer = b.start("plain")
        layer.limit = n if layer.limit is None else min(layer.limit, n)
        return layer

    if kind == "columns":
        names = column_list(stage, p)
        if not (open_ and layer.shape == "plain"):
            layer = b.start("plain")
        layer.shape, layer.select = "columns", names
        return layer

    if kind == "rename":
        parts = rename_list(stage, p)
        if not (open_ and layer.shape == "plain" and not layer.order):
            layer = b.start("plain")
        layer.shape, layer.select = "renamed", parts
        return layer

    if kind == "transform":
        replaced = transform_list(stage, p)
        if not replaced:
            return layer  # no text columns: nothing changes
        if not (open_ and layer.shape == "plain" and not layer.order):
            layer = b.start("plain")
        layer.shape, layer.select = "replaced", [f"* REPLACE ({', '.join(replaced)})"]
        return layer

    if kind in ("bucket", "categorize"):
        if kind == "bucket":
            name, expression = bucket_expression(stage, p)
        else:
            name, expression = categorize_expression(stage, p, settings)
        if not reads_input:
            layer = b.start("plain")
        if layer.shape == "plain":
            layer.shape = "added"
        layer.select = [*layer.select, f"{expression} AS {quote(name)}"]
        layer.added.add(name)
        return layer

    if kind == "group_by":
        key_sql, agg_sql = group_parts(stage, p)
        if not (open_ and layer.shape == "plain"):
            layer = b.start("plain")
        # Grouping sets its own order (by the keys), so an earlier sort in this SELECT goes.
        layer.shape, layer.select = "grouped", [*key_sql, *agg_sql]
        layer.group_by, layer.order = key_sql, group_order(key_sql)
        return layer

    if kind == "distinct":
        layer = b.start("distinct")
        position = _free_name("_row", {c.name for c in stage.columns})
        layer.source = f"(SELECT *, {_TIEBREAK} AS {position} FROM {layer.source})"
        layer.select = [quote(c.name) for c in stage.columns]
        layer.group_by, layer.order = ["ALL"], [f"min({position})"]
        return layer

    # sql: the user's own query, as written, reading the rows so far by the name it uses.
    layer = b.start("sql")
    layer.body = _sql_step_body(str(p.get("sql", "")), dataset, layer.source, var)
    return layer


def _free_name(base: str, taken: set[str]) -> str:
    name = base
    while name in taken:
        name = "_" + name
    return name


def _sql_step_body(text: str, dataset: str, source: str, var: str) -> str:
    """A SQL step's query as written. It reads the table entering it as `grid` or by the dataset's
    name; a CTE of that name points at the rows so far (unless the name already is the user's
    variable). A query with no defined order is sorted by every column, as the grid does."""
    body = text.strip().rstrip(";").rstrip()
    root = sqlglot.parse_one(body, read="duckdb")
    if not keeps_order(body, dataset):
        body = f"SELECT * FROM (\n{_indent(body)}\n) ORDER BY ALL"
    elif isinstance(root, exp.Query) and root.args.get("with_"):
        body = f"SELECT * FROM (\n{_indent(body)}\n)"
    shims = [
        f"{table_name_sql(name)} AS (SELECT * FROM {source})"
        for name in step_tables(text, dataset)
        if not (source == table_name_sql(var) and name == var)
    ]
    return (f"WITH {', '.join(shims)}\n" if shims else "") + body
