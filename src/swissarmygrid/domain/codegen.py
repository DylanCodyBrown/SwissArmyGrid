"""Generated code: the grid's steps as text the user copies into their own Python (plan section 9).

The default and primary target is "duckdb": the grid's steps as one DuckDB query over the user's
own table, by its variable name (the first section here). The response carries it twice: `sql`,
the query alone ("Copy as SQL"), and `code`, `import duckdb` plus the one statement that runs it
("Copy as Python").

The pandas and plain-Python generators stay as secondary targets for people who want code
without DuckDB. They are translations of the steps, longer and with their own imports and helper
functions, so they are never the default. They end by applying the transformation to the user's
variable, like the DuckDB copy, so all three paste and run the same way.

Every result is verified before it is offered: exactly the copied text runs in a fresh namespace
holding only the user's variable (adapters/codegen.py), and what it assigns is compared, row by row
and in order, with what the grid shows. Only verified code gets the copy buttons; otherwise the
response carries the first differing row.

Three sections: the DuckDB query (the primary copy), the pandas and plain-Python translations,
and how each copy is pasted and run. Everything here is text; running a copy to check it is an
adapter's job, behind the service layer (services/codegen.py).
"""

from __future__ import annotations

import keyword
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import sqlglot
from sqlglot import exp

from swissarmygrid.domain.datasets import Column, DatasetInfo
from swissarmygrid.domain.sql_guard import keeps_order, step_query_text, step_tables
from swissarmygrid.domain.steps import (
    TRIM_CHARACTERS,
    CompiledView,
    Stage,
    Step,
    StepError,
    ViewSettings,
    aggregate_name,
    bucket_edges,
    bucket_expression,
    bucket_labels,
    categorize_expression,
    column_list,
    describe_step,
    filter_condition,
    group_order,
    group_parts,
    has_wildcards,
    limit_count,
    quote,
    rename_list,
    sort_keys,
    transform_list,
    type_family,
)

# ===== The primary copy: a grid's steps as ONE DuckDB query over the user's own table =====
#
# What SAG is for: DuckDB SQL does the transforming, so the copy is not a translation. The query
# reads the table by the name the user pushed it under, which is their variable's name (DuckDB
# reads a pandas, Polars or Arrow variable, or a DuckDB relation, by name), so it runs in their
# Python as is:
#
#     import duckdb
#
#     orders_view = duckdb.sql("""
#     SELECT * FROM orders WHERE "amount" > 20 ORDER BY "amount" DESC NULLS LAST, row_number() OVER ()
#     """).df()
#
# "Copy as SQL" gives the query alone; "Copy as Python" gives these lines: `import duckdb` and one
# statement, ending in the output form that matches what was pushed (.df() for pandas, .pl() for
# Polars, .to_arrow_table() for Arrow, the relation itself for DuckDB; .df() when not knowable).
#
# Every expression comes from the engine's own step pieces (domain/steps.py), so the copy computes
# what the grid computes. What differs is how row order is kept: the engine numbers rows in a hidden
# column; the copy has no such column, and relies instead on what docs/verified.md checks:
# - DuckDB keeps a table's order through filters, projections, LIMIT and CTEs, and numbers an ordered
#   input in order with `row_number() OVER ()`;
# - ORDER BY is not stable, so a sort ends with `row_number() OVER ()`: ties keep their order;
# - remove duplicates keeps each row's first position with a numbered subquery, the one place a
#   helper column appears (inside the query; it is not in the result).
#
# Consecutive steps share one SELECT where SQL evaluates them in the same order (WHERE, then the
# select list, then ORDER BY, then LIMIT); any other step starts a new CTE named after its step
# number, so the query reads top to bottom like the steps panel, with each step as a comment.


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


def table_name_sql(name: str, keywords: frozenset[str]) -> str:
    """A table name as SQL: bare when that reads the same (lowercase, not one of DuckDB's `keywords`),
    else quoted."""
    return name if _SIMPLE_NAME.fullmatch(name) and name not in keywords else quote(name)


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
    def __init__(self, source: str, prefix: str, keywords: frozenset[str]) -> None:
        self.keywords = keywords
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


def view_query(
    info: DatasetInfo,
    compiled: CompiledView,
    steps: list[Step],
    settings: ViewSettings,
    keywords: frozenset[str],
) -> str:
    """The grid's view as one DuckDB query over the user's table (every step must compile).
    `keywords` are DuckDB's (SqlGrammar.keywords): a table named like one is quoted."""
    var = variable_name(info.name)
    source = table_name_sql(var, keywords)
    prefix = "stage_" if var.startswith("step_") else "step_"
    b = _Builder(source, prefix, keywords)
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
    layer.body = _sql_step_body(str(p.get("sql", "")), dataset, layer.source, var, b.keywords)
    return layer


def _free_name(base: str, taken: set[str]) -> str:
    name = base
    while name in taken:
        name = "_" + name
    return name


def _sql_step_body(text: str, dataset: str, source: str, var: str, keywords: frozenset[str]) -> str:
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
        f"{table_name_sql(name, keywords)} AS (SELECT * FROM {source})"
        for name in step_tables(text, dataset)
        if not (source == table_name_sql(var, keywords) and name == var)
    ]
    return (f"WITH {', '.join(shims)}\n" if shims else "") + body


# ===== Plain-Python or pandas code that reproduces a grid's steps exactly =====
#
# The generators read the neutral step list, never the SQL text, and emit a typed function:
#
#     def transform(orders: pd.DataFrame) -> pd.DataFrame:      (pandas)
#     def transform(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:   (plain Python)
#
# Matching DuckDB is the whole job, so every place where Python and DuckDB naturally differ is
# handled explicitly in the emitted code:
# - blanks never match a comparison (pandas `!=` would keep NaN rows; we don't);
# - lowercase/uppercase go one character at a time (`str.lower()` turns İ into two characters,
#   `str.upper()` turns ß into SS; DuckDB does neither);
# - sorting puts blanks last per key and keeps ties in order (stable sorts, key by key);
# - group by keeps the blank group, sorts groups by key, and a sum of no values is blank
#   (pandas would say 0; hence `min_count=1`);
# - the wildcard `%` becomes fnmatchcase's `*` (fnmatch.fnmatch folds case on Windows), and
#   fnmatch's own `*`, `?` and `[` are escaped so they stay literal;
# - weeks start on Monday;
# - a SQL step runs its query, as the user wrote it, in DuckDB over the rows so far, registered
#   under the name the query reads (the dataset's own name, or `grid`) (the only
#   step that needs a library beyond the standard library or pandas).
# Text filters run as small per-value functions so their blank handling is visible; number
# filters in pandas use the vectorized methods, which already skip NaN.


def duck_lower(text: str) -> str:
    """Lowercase one character at a time, like DuckDB."""
    out = []
    for ch in text:
        low = ch.lower()
        out.append(low if len(low) == 1 else low[0])
    return "".join(out)


def duck_upper(text: str) -> str:
    """Uppercase one character at a time, like DuckDB (ß becomes ẞ, not SS)."""
    out = []
    for ch in text:
        up = ch.upper()
        out.append(up if len(up) == 1 else ("ẞ" if ch == "ß" else ch))
    return "".join(out)


def normalize(value: str | None, settings: ViewSettings) -> str | None:
    if value is None:
        return None
    if settings.trim_spaces:
        value = value.strip(TRIM_CHARACTERS)
    if settings.empty_is_blank and value == "":
        return None
    if not settings.case_sensitive:
        value = duck_lower(value)
    return value


def fnmatch_pattern(pattern: str) -> str:
    """Our wildcard `%` as fnmatch's `*`; fnmatch's own specials (`*`, `?`, `[`) bracketed so
    they match only themselves."""
    special = {"%": "*", "*": "[*]", "?": "[?]", "[": "[[]"}
    return "".join(special.get(ch, ch) for ch in pattern)


def py(value: Any) -> str:
    """A Python literal for a step value."""
    if isinstance(value, datetime):
        return f"datetime.fromisoformat({value.isoformat()!r})"
    if isinstance(value, date):
        return f"date({value.year}, {value.month}, {value.day})"
    if isinstance(value, float) and value.is_integer():
        return repr(value)
    return repr(value)


_HELPERS = '''
_TRIM = " \\t\\r\\n"  # spaces, tabs and line breaks


def _blank(value: Any) -> bool:
    """None, NaN and pandas' missing markers all count as blank."""
    return value is None or value != value or {pd_missing}


def _lower(text: str) -> str:
    """Lowercase one character at a time, like DuckDB (str.lower() turns some into two)."""
    out = []
    for ch in text:
        low = ch.lower()
        out.append(low if len(low) == 1 else low[0])
    return "".join(out)


def _upper(text: str) -> str:
    """Uppercase one character at a time, like DuckDB (\\u00df becomes \\u1e9e, not SS)."""
    out = []
    for ch in text:
        up = ch.upper()
        out.append(up if len(up) == 1 else ("\\u1e9e" if ch == "\\u00df" else ch))
    return "".join(out)


def _text(value: Any) -> str | None:
    """A text cell as the toolbar settings compare it ({settings})."""
    if _blank(value):
        return None
    text = str(value){trim}{empty}{case}
    return text


def _week(day: date) -> date:
    return day - timedelta(days=day.weekday())  # weeks start on Monday


def _truncate(value: Any, unit: str) -> date | None:
    if _blank(value):
        return None
    day = value.date() if isinstance(value, datetime) else value
    if unit == "day":
        return day
    if unit == "week":
        return _week(day)
    if unit == "month":
        return day.replace(day=1)
    if unit == "quarter":
        return day.replace(month=3 * ((day.month - 1) // 3) + 1, day=1)
    return day.replace(month=1, day=1)
'''


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.defs: list[str] = []
        self.counter = 0

    def name(self, prefix: str) -> str:
        self.counter += 1
        return f"_{prefix}_{self.counter}"


def generate_python(
    mode: str,
    dataset: str,
    source_columns: tuple[Column, ...],
    stage_columns: list[tuple[Column, ...]],
    steps: list[Step],
    settings: ViewSettings,
    row_count: int,
) -> str:
    """Code for `mode` "plain" or "pandas". `stage_columns[i]` are the columns before step i."""
    pandas = mode == "pandas"
    var_name = variable_name(dataset)
    w = _Writer()
    var = "df" if pandas else "rows"
    for index, step in enumerate(steps):
        if not step.enabled:
            continue
        columns = stage_columns[index]
        w.lines.append(f"    # {index + 1}. {describe_step(step)}")
        _emit_step(w, step, columns, settings, pandas, var, dataset)

    uses_sql = any(s.enabled and s.kind == "sql" for s in steps)
    header = _header(dataset, source_columns, steps, settings, row_count, pandas, uses_sql)
    helpers = _HELPERS.format(
        pd_missing="value is pd.NA or value is pd.NaT" if pandas else "False",
        settings=", ".join(
            name
            for name, on in (
                ("trim spaces", settings.trim_spaces),
                ("empty text is blank", settings.empty_is_blank),
                ("case insensitive", not settings.case_sensitive),
            )
            if on
        )
        or "exact match",
        trim=".strip(_TRIM)" if settings.trim_spaces else "",
        empty='\n    if text == "":\n        return None' if settings.empty_is_blank else "",
        case="\n    text = _lower(text)" if not settings.case_sensitive else "",
    )
    imports = [
        "from __future__ import annotations",
        "",
        "from datetime import date, datetime, timedelta",
        "from fnmatch import fnmatchcase",
        "from typing import Any",
        "",
    ]
    if uses_sql:
        imports.insert(5, "import duckdb")
        imports.insert(5, "")
    if pandas:
        imports.insert(5 if not uses_sql else 7, "import pandas as pd")
        if not uses_sql:
            imports.insert(5, "")
        signature = f"def transform({var_name}: pd.DataFrame) -> pd.DataFrame:"
        start = [f"    df = {var_name}.reset_index(drop=True)"]
        end = ["    return df.reset_index(drop=True)"]
    else:
        signature = f"def transform({var_name}: list[dict[str, Any]]) -> list[dict[str, Any]]:"
        start = [f"    rows = [dict(row) for row in {var_name}]"]
        end = ["    return rows"]
    body = [
        signature,
        '    """Reproduce the grid view. Generated by SwissArmyGrid."""',
        *start,
        *w.lines,
        *end,
    ]
    # Applied to the user's own variable, so the pasted code transforms it (as the DuckDB copy does).
    apply = ["", "", f"{var_name}_view = transform({var_name})"]
    return "\n".join([header, *imports, helpers.strip("\n"), "", "", *w.defs, *body, *apply]) + "\n"


def _sql_literal(query: str) -> str:
    """The query as adjacent Python string literals, one per line (safe for any quotes)."""
    lines = query.splitlines() or [""]
    parts = [repr(line + ("\n" if i < len(lines) - 1 else "")) for i, line in enumerate(lines)]
    return "(\n" + "\n".join("        " + part for part in parts) + "\n    )"


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _header(
    dataset: str,
    columns: tuple[Column, ...],
    steps: list[Step],
    settings: ViewSettings,
    row_count: int,
    pandas: bool,
    uses_sql: bool = False,
) -> str:
    needs = "pandas" if pandas else "the Python standard library only"
    if uses_sql:
        needs = ("pandas, duckdb" if pandas else "duckdb") + " (for the SQL step)"
    lines = [
        f"# Generated by SwissArmyGrid from dataset '{dataset}' ({row_count:,} rows when generated).",
        "# Source columns: " + ", ".join(f"{c.name} {c.type}" for c in columns),
        "# Settings: " + ", ".join(f"{k}={v}" for k, v in settings.to_json().items()),
        "# Steps:",
        *[
            f"#   {i}. {describe_step(s)}{'' if s.enabled else ' (off, skipped)'}"
            for i, s in enumerate(steps, 1)
        ],
        "# Needs: " + needs,
        "",
    ]
    return "\n".join(lines)


def _column(columns: tuple[Column, ...], name: Any) -> Column:
    for column in columns:
        if column.name == name:
            return column
    raise StepError(None, f"Column {name!r} is missing.")


def _typed(column: Column, value: Any) -> Any:
    family = type_family(column.type)
    if family == "date" and isinstance(value, str):
        return date.fromisoformat(value)
    if family == "timestamp" and isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def _emit_keep(w: _Writer, pandas: bool, var: str, column: str, fn_name: str) -> None:
    if pandas:
        w.lines.append(f"    df = df[df[{column!r}].map({fn_name}).astype(bool)]")
    else:
        w.lines.append(f"    rows = [row for row in rows if {fn_name}(row[{column!r}])]")


def _emit_step(
    w: _Writer,
    step: Step,
    columns: tuple[Column, ...],
    settings: ViewSettings,
    pandas: bool,
    var: str,
    dataset: str,
) -> None:
    p = step.params
    kind = step.kind

    if kind == "sql":
        text = str(p.get("sql", ""))
        sql = _sql_literal(step_query_text(text, dataset))
        # The input goes in under the name(s) the query reads (the dataset's, or `grid`), so the
        # query below is the user's text as written.
        tables = step_tables(text, dataset)
        w.lines.append("    con = duckdb.connect()")
        if pandas:
            for table in tables:
                w.lines.append(f'    con.register("{table}", df)')  # a dataset name: no quotes in it
            w.lines.append(f"    df = con.sql({sql}).df()")
            return
        first = _quote(tables[0])
        names = [c.name for c in columns]
        ddl = ", ".join(f"{_quote(c.name)} {c.type}" for c in columns)
        insert = f"INSERT INTO {first} VALUES (" + ", ".join("?" for _ in columns) + ")"
        # The rows so far go into a typed DuckDB table, so the query sees the grid's own types.
        w.lines.append(f"    con.execute({(f'CREATE TABLE {first} (' + ddl + ')')!r})")
        for table in tables[1:]:
            view = f"CREATE VIEW {_quote(table)} AS SELECT * FROM {first}"
            w.lines.append(f"    con.execute({view!r})")
        w.lines.append(f"    names = {names!r}")
        w.lines.append("    if rows:")
        w.lines.append(f"        con.executemany({insert!r}, [tuple(row[n] for n in names) for row in rows])")
        w.lines.append(f"    result = con.execute({sql})")
        w.lines.append("    names = [d[0] for d in result.description]")
        w.lines.append("    rows = [dict(zip(names, values, strict=True)) for values in result.fetchall()]")
        return

    if kind == "filter":
        column = _column(columns, p.get("column"))
        family = type_family(column.type)
        op = p["op"]
        c = column.name
        fn = w.name("keep")
        if family == "text":

            def norm(v: Any) -> Any:
                return normalize(v, settings) if isinstance(v, str) else v

            body: str
            if op == "blank":
                body = "return _text(value) is None"
            elif op == "not_blank":
                body = "return _text(value) is not None"
            elif (
                op in ("eq", "ne")
                and settings.wildcards
                and isinstance(p.get("value"), str)
                and has_wildcards(p["value"])
            ):
                pattern = norm(p["value"])
                test = (
                    f"fnmatchcase(text, {py(fnmatch_pattern(pattern))})" if pattern is not None else "False"
                )
                if op == "ne":
                    test = f"not {test}"
                body = f"text = _text(value)\n    return text is not None and {test}"
            elif op in ("eq", "ne"):
                target = norm(p.get("value"))
                cmp = "==" if op == "eq" else "!="
                body = (
                    "return False"
                    if target is None
                    else f"text = _text(value)\n    return text is not None and text {cmp} {py(target)}"
                )
            elif op in ("in", "not_in"):
                values = [norm(v) for v in p.get("values", [])]
                if not values:
                    body = "return False" if op == "in" else "return True"
                else:
                    present = sorted({v for v in values if v is not None})
                    test = "in" if op == "in" else "not in"
                    body = f"text = _text(value)\n    return text is not None and text {test} {py(set(present)) if present else 'set()'}"
            elif op in ("contains", "starts_with", "ends_with"):
                needle = norm(p.get("value"))
                if needle is None:
                    body = "return False"
                else:
                    expr = {
                        "contains": f"{py(needle)} in text",
                        "starts_with": f"text.startswith({py(needle)})",
                        "ends_with": f"text.endswith({py(needle)})",
                    }[op]
                    body = f"text = _text(value)\n    return text is not None and {expr}"
            else:
                raise StepError(None, f"{op} does not apply to text.")
            w.defs.append(f"def {fn}(value: Any) -> bool:\n    {body}\n\n")
            _emit_keep(w, pandas, var, c, fn)
            return

        # numbers, dates, booleans
        if pandas and family in ("number", "boolean"):
            s = f"df[{c!r}]"
            mask = {
                "eq": lambda: f"{s}.eq({py(p['value'])})",
                "ne": lambda: f"{s}.ne({py(p['value'])}) & {s}.notna()",
                "in": lambda: f"{s}.isin({py(list(p.get('values', [])))})",
                "not_in": lambda: (
                    f"~{s}.isin({py(list(p.get('values', [])))}) & {s}.notna()"
                    if p.get("values")
                    else f"{s}.map(lambda v: True).astype(bool)"
                ),
                "blank": lambda: f"{s}.isna()",
                "not_blank": lambda: f"{s}.notna()",
                "lt": lambda: f"{s}.lt({py(p['value'])})",
                "gt": lambda: f"{s}.gt({py(p['value'])})",
                "between": lambda: f"{s}.between({py(p['low'])}, {py(p['high'])})",
                "true": lambda: f"{s}.eq(True)",
                "false": lambda: f"{s}.eq(False) & {s}.notna()",
            }.get(op)
            if mask is None:
                raise StepError(None, f"{op} does not apply to {column.type}.")
            w.lines.append(f"    df = df[({mask()}).astype(bool)]")
            return

        def value(key: str) -> str:
            return py(_typed(column, p.get(key)))

        tests_by_op: dict[str, Callable[[], str]] = {
            "eq": lambda: f"value == {value('value')}",
            "ne": lambda: f"value != {value('value')}",
            "in": lambda: f"value in {py([_typed(column, v) for v in p.get('values', [])])}",
            "not_in": lambda: f"value not in {py([_typed(column, v) for v in p.get('values', [])])}",
            "lt": lambda: f"value < {value('value')}",
            "gt": lambda: f"value > {value('value')}",
            "before": lambda: f"value < {value('value')}",
            "after": lambda: f"value > {value('value')}",
            "between": lambda: f"{value('low')} <= value <= {value('high')}",
            "date_between": lambda: f"{value('low')} <= value <= {value('high')}",
            "true": lambda: "bool(value)",
            "false": lambda: "not bool(value)",
        }
        if op == "blank":
            body = "return _blank(value)"
        elif op == "not_blank":
            body = "return not _blank(value)"
        elif op == "in" and not p.get("values"):
            body = "return False"
        elif op == "not_in" and not p.get("values"):
            body = "return True"
        elif op in tests_by_op:
            body = f"return not _blank(value) and {tests_by_op[op]()}"
        else:
            raise StepError(None, f"{op} does not apply to {column.type}.")
        w.defs.append(f"def {fn}(value: Any) -> bool:\n    {body}\n\n")
        _emit_keep(w, pandas, var, c, fn)
        return

    if kind == "sort":
        keys = p["keys"]
        if pandas:
            w.lines.append(
                f"    df = df.sort_values({py([k for k, _ in keys])}, ascending={py([d == 'asc' for _, d in keys])}, "
                'na_position="last", kind="stable")'
            )
        else:
            w.lines.append("    # Stable sorts from the last key to the first; blanks last for each key.")
            for column, direction in reversed(keys):
                w.lines.append(f"    present = [row for row in rows if not _blank(row[{column!r}])]")
                w.lines.append(f"    missing = [row for row in rows if _blank(row[{column!r}])]")
                w.lines.append(
                    f"    present.sort(key=lambda row: row[{column!r}], reverse={direction == 'desc'})"
                )
                w.lines.append("    rows = present + missing")
        return

    if kind == "columns":
        keep = p["keep"]
        if pandas:
            w.lines.append(f"    df = df[{py(keep)}]")
        else:
            w.lines.append(f"    rows = [{{name: row[name] for name in {py(keep)}}} for row in rows]")
        return

    if kind == "rename":
        mapping = p["mapping"]
        if pandas:
            w.lines.append(f"    df = df.rename(columns={py(mapping)})")
        else:
            w.lines.append(f"    rename = {py(mapping)}")
            w.lines.append("    rows = [{rename.get(k, k): v for k, v in row.items()} for row in rows]")
        return

    if kind == "group_by":
        keys = list(p.get("keys", []))
        aggregates = p["aggregates"]
        if pandas:
            if not keys:
                parts = []
                for agg in aggregates:
                    name, fn, col = aggregate_name(agg), agg["fn"], agg.get("column")
                    expr = {
                        "count_rows": "len(df)",
                        "count": f"df[{col!r}].count()",
                        "distinct_count": f"df[{col!r}].nunique()",
                        "sum": f"df[{col!r}].sum(min_count=1)",
                        "min": f"df[{col!r}].dropna().min() if df[{col!r}].notna().any() else None",
                        "max": f"df[{col!r}].dropna().max() if df[{col!r}].notna().any() else None",
                        "mean": f"df[{col!r}].mean()",
                    }[fn]
                    parts.append(f"{name!r}: [{expr}]")
                w.lines.append("    df = pd.DataFrame({" + ", ".join(parts) + "})")
                return
            w.lines.append(f"    grouped = df.groupby({py(keys)}, dropna=False, sort=True)")
            parts = []
            for agg in aggregates:
                name, fn, col = aggregate_name(agg), agg["fn"], agg.get("column")
                expr = {
                    "count_rows": "grouped.size()",
                    "count": f"grouped[{col!r}].count()",
                    "distinct_count": f"grouped[{col!r}].nunique()",
                    "sum": f"grouped[{col!r}].sum(min_count=1)",
                    "min": f"grouped[{col!r}].agg(lambda s: s.dropna().min() if s.notna().any() else None)",
                    "max": f"grouped[{col!r}].agg(lambda s: s.dropna().max() if s.notna().any() else None)",
                    "mean": f"grouped[{col!r}].mean()",
                }[fn]
                parts.append(f"        {name!r}: {expr},")
            w.lines.append("    df = pd.DataFrame({")
            w.lines += parts
            w.lines.append("    }).reset_index()")
            # Groups sorted by key, blanks last per key (pandas puts the NaN group last too,
            # but re-sorting stably makes it explicit for every key).
            w.lines.append(f'    df = df.sort_values({py(keys)}, na_position="last", kind="stable")')
            return
        fn = w.name("group")
        lines = [
            f"def {fn}(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:",
            f"    keys = {py(keys)}",
            "    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}",
            "    for row in rows:",
            "        groups.setdefault(tuple(None if _blank(row[k]) else row[k] for k in keys), []).append(row)",
            "    if not keys and not groups:",
            "        groups[()] = []",
            "    out = []",
            "    for key, members in groups.items():",
            "        result = dict(zip(keys, key))",
        ]
        for agg in aggregates:
            name, kind_fn, col = aggregate_name(agg), agg["fn"], agg.get("column")
            if kind_fn == "count_rows":
                lines.append(f"        result[{name!r}] = len(members)")
                continue
            lines.append(f"        values = [r[{col!r}] for r in members if not _blank(r[{col!r}])]")
            expr = {
                "count": "len(values)",
                "distinct_count": "len(set(values))",
                "sum": "sum(values) if values else None",
                "min": "min(values) if values else None",
                "max": "max(values) if values else None",
                "mean": "sum(values) / len(values) if values else None",
            }[kind_fn]
            lines.append(f"        result[{name!r}] = {expr}")
        lines += [
            "        out.append(result)",
            "    for k in reversed(keys):  # groups sorted by key, blanks last",
            "        present = [r for r in out if r[k] is not None]",
            "        missing = [r for r in out if r[k] is None]",
            "        present.sort(key=lambda r: r[k])",
            "        out = present + missing",
            "    return out",
            "",
            "",
        ]
        w.defs.append("\n".join(lines))
        w.lines.append(f"    rows = {fn}(rows)")
        return

    if kind == "distinct":
        if pandas:
            w.lines.append('    df = df.drop_duplicates(keep="first")')
        else:
            w.lines.append("    seen: set[tuple[Any, ...]] = set()")
            w.lines.append("    unique = []")
            w.lines.append("    for row in rows:")
            w.lines.append("        key = tuple(None if _blank(v) else v for v in row.values())")
            w.lines.append("        if key not in seen:")
            w.lines.append("            seen.add(key)")
            w.lines.append("            unique.append(row)")
            w.lines.append("    rows = unique")
        return

    if kind == "limit":
        n = p["n"]
        w.lines.append(f"    df = df.head({n})" if pandas else f"    rows = rows[:{n}]")
        return

    if kind == "transform":
        fn_name = p["fn"]
        targets = (
            [c.name for c in columns if type_family(c.type) == "text"]
            if p.get("columns") == "*text"
            else list(p["columns"])
        )
        expr = {
            "trim_left": "text.lstrip(_TRIM)",
            "trim_right": "text.rstrip(_TRIM)",
            "trim_both": "text.strip(_TRIM)",
            "upper": "_upper(text)",
            "lower": "_lower(text)",
        }[fn_name]
        fn = w.name(fn_name)
        w.defs.append(f"def {fn}(text: Any) -> Any:\n    return text if _blank(text) else {expr}\n\n")
        for c in targets:
            if pandas:
                w.lines.append(f"    df[{c!r}] = df[{c!r}].map({fn})")
            else:
                w.lines.append(f"    for row in rows:\n        row[{c!r}] = {fn}(row[{c!r}])")
        return

    if kind == "bucket":
        column = p["column"]
        new = p["as"]
        fn = w.name("bucket")
        if p.get("mode") == "date":
            body = f"return _truncate(value, {p['unit']!r})"
        else:
            edges = bucket_edges(p)
            right_closed = bool(p.get("right_closed", False))
            labels = bucket_labels(edges, right_closed)
            low_op, high_op = ("<", "<=") if right_closed else ("<=", "<")
            body = (
                "if _blank(value):\n        return None\n"
                f"    edges = {py(edges)}\n"
                f"    labels = {py(labels)}\n"
                "    for low, high, label in zip(edges, edges[1:], labels):\n"
                f"        if low {low_op} value {high_op} high:\n"
                "            return label\n"
                f"    return {py(p.get('other_label', 'Other'))}"
            )
        w.defs.append(f"def {fn}(value: Any) -> Any:\n    {body}\n\n")
        if pandas:
            w.lines.append(f"    df[{new!r}] = df[{column!r}].map({fn})")
        else:
            w.lines.append(f"    for row in rows:\n        row[{new!r}] = {fn}(row[{column!r}])")
        return

    # categorize
    column = _column(columns, p["column"])
    new = p["as"]
    fn = w.name("category")
    is_text = type_family(column.type) == "text"
    tests: list[str] = []
    for rule in p["rules"]:
        label = py(rule["label"])
        kind_rule = rule["type"]
        if kind_rule == "range":
            tests.append(f"    if {py(rule['low'])} <= value < {py(rule['high'])}:\n        return {label}")
            continue
        if kind_rule == "in":
            values = sorted({v for v in (normalize(x, settings) for x in rule["values"]) if v is not None})
            cond = f"text in {py(set(values)) if values else 'set()'}"
        else:
            needle = normalize(rule["value"], settings)
            if needle is None:
                continue
            cond = {
                "wildcard": f"fnmatchcase(text, {py(fnmatch_pattern(needle))})",
                "contains": f"{py(needle)} in text",
                "starts_with": f"text.startswith({py(needle)})",
            }[kind_rule]
        tests.append(f"    if text is not None and {cond}:\n        return {label}")
    prelude = "    if _blank(value):\n        return None\n"
    if is_text:
        prelude += "    text = _text(value)\n"
    body = prelude + "\n".join(tests) + f"\n    return {py(p.get('unmatched', 'Other'))}"
    w.defs.append(f"def {fn}(value: Any) -> Any:  # first matching rule wins\n{body}\n\n")
    if pandas:
        w.lines.append(f"    df[{new!r}] = df[{column.name!r}].map({fn})")
    else:
        w.lines.append(f"    for row in rows:\n        row[{new!r}] = {fn}(row[{column.name!r}])")


# ===== How a copy is pasted and run, and what the check says =====


VALUE_KINDS: tuple[str, ...] = ("pandas", "polars", "arrow", "duckdb", "records")


@dataclass(frozen=True)
class Pasted:
    """Code as the user copies it, and how they run it."""

    code: str
    variable: str  # the user's table, by name: the only thing in the namespace
    value: str  # what that variable holds (VALUE_KINDS)
    result: str  # the variable the code assigns
    # Run instead when `value` (or the copy's output call) needs a library this Python lacks.
    fallback: Pasted | None = None
    fallback_note: str | None = None


@dataclass(frozen=True)
class Outcome:
    identical: bool
    message: str | None
    note: str | None = None


TARGETS: tuple[str, ...] = ("duckdb", "pandas", "plain")


_READS: dict[str, str] = {
    "pandas": "a pandas DataFrame",
    "polars": "a Polars DataFrame",
    "arrow": "an Arrow table",
    "duckdb": "a DuckDB relation",
    "records": "a list of dicts",
}


def default_target(source_kind: str) -> str:
    """DuckDB SQL is the copy for every source (what SAG is for)."""
    return "duckdb"


def pasted_for(target: str, dataset: str, source_kind: str, code: str, sql: str) -> Pasted:
    """How the user runs `code`: in their Python, holding their table under its variable name."""
    var = variable_name(dataset)
    result = f"{var}_view"
    if target == "pandas":
        return Pasted(code, var, "pandas", result)
    if target == "plain":
        return Pasted(code, var, "records", result)
    # The DuckDB copy reads whatever was pushed; a source with no Python table of its own (a
    # file, the clipboard, a list of dicts DuckDB cannot read) is read as a pandas DataFrame.
    value = source_kind if source_kind in VALUE_KINDS and source_kind != "records" else "pandas"
    call = output_call(source_kind)
    fallback = None
    note = None
    if call in (".df()", ".pl()"):
        library = "pandas" if call == ".df()" else "Polars"
        fallback = Pasted(python_code(sql, dataset, source_kind, with_output=False), var, "arrow", result)
        note = (
            f"{library} is not installed beside SwissArmyGrid, so the same query was checked over an "
            f"Arrow table and the final {call} was not run."
        )
    return Pasted(code, var, value, result, fallback, note)


def reads_text(target: str, dataset: str, source_kind: str) -> str:
    var = variable_name(dataset)
    if target == "pandas":
        what = "a pandas DataFrame"
    elif target == "plain":
        what = "a list of dicts"
    elif source_kind in _READS and source_kind != "records":
        what = _READS[source_kind]
    else:  # DuckDB cannot read a list of dicts by name, nor a file the user has no variable for
        what = "a pandas DataFrame, Polars DataFrame or Arrow table"
    return f"Reads {what} named {var} and assigns the result to {var}_view."
