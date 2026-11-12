"""Generate plain-Python or pandas code that reproduces a grid's steps exactly (plan section 9).

The generators read the neutral step list, never the SQL text, and emit a typed function:

    def transform(orders: pd.DataFrame) -> pd.DataFrame:      (pandas)
    def transform(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:   (plain Python)

Matching DuckDB is the whole job, so every place where Python and DuckDB naturally differ is
handled explicitly in the emitted code:
- blanks never match a comparison (pandas `!=` would keep NaN rows; we don't);
- lowercase/uppercase go one character at a time (`str.lower()` turns İ into two characters,
  `str.upper()` turns ß into SS; DuckDB does neither);
- sorting puts blanks last per key and keeps ties in order (stable sorts, key by key);
- group by keeps the blank group, sorts groups by key, and a sum of no values is blank
  (pandas would say 0; hence `min_count=1`);
- the wildcard `%` becomes fnmatchcase's `*` (fnmatch.fnmatch folds case on Windows), and
  fnmatch's own `*`, `?` and `[` are escaped so they stay literal;
- weeks start on Monday;
- a SQL step runs its query, as the user wrote it, in DuckDB over the rows so far, registered
  under the name the query reads (the dataset's own name, or `grid`) (the only
  step that needs a library beyond the standard library or pandas).
Text filters run as small per-value functions so their blank handling is visible; number
filters in pandas use the vectorized methods, which already skip NaN.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from swissarmygrid.engine.datasets import Column
from swissarmygrid.engine.describe import describe_step
from swissarmygrid.engine.sql_guard import step_query_text, step_tables
from swissarmygrid.engine.steps import (
    TRIM_CHARACTERS,
    Step,
    StepError,
    ViewSettings,
    aggregate_name,
    bucket_edges,
    bucket_labels,
    has_wildcards,
    type_family,
)

# ----- helpers that run at generation time and are also emitted into the code -----


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
        signature = f"def transform({_ident(dataset)}: pd.DataFrame) -> pd.DataFrame:"
        start = [f"    df = {_ident(dataset)}.reset_index(drop=True)"]
        end = ["    return df.reset_index(drop=True)"]
    else:
        signature = f"def transform({_ident(dataset)}: list[dict[str, Any]]) -> list[dict[str, Any]]:"
        start = [f"    rows = [dict(row) for row in {_ident(dataset)}]"]
        end = ["    return rows"]
    body = [
        signature,
        '    """Reproduce the grid view. Generated by SwissArmyGrid."""',
        *start,
        *w.lines,
        *end,
    ]
    return "\n".join([header, *imports, helpers.strip("\n"), "", "", *w.defs, *body]) + "\n"


def _sql_literal(query: str) -> str:
    """The query as adjacent Python string literals, one per line (safe for any quotes)."""
    lines = query.splitlines() or [""]
    parts = [repr(line + ("\n" if i < len(lines) - 1 else "")) for i, line in enumerate(lines)]
    return "(\n" + "\n".join("        " + part for part in parts) + "\n    )"


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _ident(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
    return cleaned if cleaned and not cleaned[0].isdigit() else f"data_{cleaned}"


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
        insert = f"INSERT INTO {first} VALUES (" + ", ".join("?" for _ in columns) + ")"  # noqa: S608 (placeholders)
        # The rows so far go into a typed DuckDB table, so the query sees the grid's own types.
        w.lines.append(f"    con.execute({(f'CREATE TABLE {first} (' + ddl + ')')!r})")
        for table in tables[1:]:
            view = f"CREATE VIEW {_quote(table)} AS SELECT * FROM {first}"  # noqa: S608 (quoted names)
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
