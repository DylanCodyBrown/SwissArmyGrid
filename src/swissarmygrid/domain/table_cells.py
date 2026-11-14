"""Every table has a read-only cell: its transformations in DuckDB SQL (docs/ideas.md, 2026-11-14).

A table open in the grid has a cell of its own in the Workbench notebook or script it belongs to. The
cell holds only the grid's steps, as one DuckDB query whose result is the table's own name; it is
never typed in. The user's cell that made the table (its maker) stays theirs. All of it sits at the
top of the file in a Tables group. Pure rules, no I/O:

  names     which names code makes, needs and uses (Python's own parser and tokenizer)
  uses      the one comment line the app keeps at the top of a cell that names a table
  arrange   the Tables group: which cells move there, in which order, and why one stays put
  scripts   the same in a .py file: `# region Tables`, `# %% [7] [table] orders`, `# %% [8]`
  code      a table's cell as text: its load (a file, a paste, a connection) and its transformations

Every cell has a number that never changes (given when the cell is made, never reused); `@cell7` in a
comment links to cell 7.
"""

from __future__ import annotations

import ast
import builtins
import io
import keyword
import math
import re
import tokenize
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from swissarmygrid.domain.codegen import output_call, python_string, variable_name
from swissarmygrid.domain.datasets import Column, quote

# A link to a cell in a comment: `# see @cell7`.
CELL_LINK: re.Pattern[str] = re.compile(r"@cell(\d+)\b")
ROLES: tuple[str, ...] = ("", "tables", "table", "maker", "imports", "transformations")
TABLES_HEADING: str = "# Tables"
_BUILTINS: frozenset[str] = frozenset(dir(builtins))

# A pasted table is written into its cell when it is this small; a larger one gets a note.
PASTE_MAX_ROWS: int = 200
PASTE_MAX_COLUMNS: int = 50
PASTE_MAX_CHARS: int = 20_000


# ===== names: what code makes, needs and uses =====


def _tokens(code: str) -> list[tokenize.TokenInfo]:
    """Python's tokens for `code`. A line the tokenizer refuses (a `?` of IPython's help, something
    half typed) is skipped and the rest still read."""
    lines = code.split("\n")
    found: list[tokenize.TokenInfo] = []
    start = 0
    while start < len(lines):
        text = "\n".join(lines[start:])
        last = 0
        try:
            for token in tokenize.generate_tokens(io.StringIO(text).readline):
                found.append(token)
                last = token.end[0]
            break
        except (tokenize.TokenError, SyntaxError, IndentationError):
            start += max(last, 1)
    return found


_SKIP = (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT)


def _python_names(code: str) -> list[str]:
    """The real names in Python code, in the order they first appear: never text in a string or a
    comment, never part of a longer name, never an attribute (`x.orders`) or an argument's name
    (`f(orders=1)`)."""
    tokens = [t for t in _tokens(code) if t.type not in _SKIP]
    names: list[str] = []
    depth = 0
    for i, token in enumerate(tokens):
        if token.type == tokenize.OP:
            if token.string in "([{":
                depth += 1
            elif token.string in ")]}":
                depth = max(0, depth - 1)
            continue
        if token.type != tokenize.NAME or keyword.iskeyword(token.string):
            continue
        before = tokens[i - 1] if i else None
        after = tokens[i + 1] if i + 1 < len(tokens) else None
        if before is not None and before.type == tokenize.OP and before.string == ".":
            continue
        if depth and after is not None and after.type == tokenize.OP and after.string == "=":
            continue
        if token.string not in names:
            names.append(token.string)
    return names


_SQL_QUIET = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'", re.DOTALL)
_SQL_NAME = re.compile(r"(?<![\w.$])([A-Za-z_]\w*)(?![\w$])")


def _sql_names(code: str) -> list[str]:
    """The names in SQL, in order: not in a string or a comment, not after a dot."""
    text = _SQL_QUIET.sub(" ", code)
    text = re.sub(r'(?<![\w.])"([A-Za-z_]\w*)"', r"\1", text)
    names: list[str] = []
    for match in _SQL_NAME.finditer(text):
        if match.group(1) not in names:
            names.append(match.group(1))
    return names


def names_used(code: str, sql: bool = False) -> list[str]:
    """Every real name the code holds, in the order of first use."""
    return _sql_names(code) if sql else _python_names(code)


@dataclass(frozen=True)
class Facts:
    """What a cell's code makes and needs."""

    made: tuple[str, ...] = ()  # names it gives a value at the top level, in order
    needs: tuple[str, ...] = ()  # names it reads before it makes them itself
    imports_only: bool = False


def _targets(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Tuple | ast.List):
        return [name for item in node.elts for name in _targets(item)]
    if isinstance(node, ast.Starred):
        return _targets(node.value)
    return []


def _made(statement: ast.stmt) -> list[str]:
    """The names a statement gives a value in the module (not inside a function or class)."""
    if isinstance(statement, ast.Assign):
        return [name for target in statement.targets for name in _targets(target)]
    if isinstance(statement, ast.AnnAssign):
        return _targets(statement.target) if statement.value is not None else []
    if isinstance(statement, ast.AugAssign):
        return _targets(statement.target)
    if isinstance(statement, ast.Import):
        return [(a.asname or a.name.split(".")[0]) for a in statement.names]
    if isinstance(statement, ast.ImportFrom):
        return [(a.asname or a.name) for a in statement.names if a.name != "*"]
    if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return [statement.name]
    found: list[str] = []
    if isinstance(statement, ast.For | ast.AsyncFor):
        found += _targets(statement.target)
    if isinstance(statement, ast.With | ast.AsyncWith):
        found += [n for item in statement.items if item.optional_vars for n in _targets(item.optional_vars)]
    for part in ("body", "orelse", "finalbody"):
        for inner in getattr(statement, part, []) or []:
            if isinstance(inner, ast.stmt):
                found += _made(inner)
    for handler in getattr(statement, "handlers", []) or []:
        for inner in handler.body:
            found += _made(inner)
    for case in getattr(statement, "cases", []) or []:
        for inner in case.body:
            found += _made(inner)
    return found


def _stores(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}


def _reads(node: ast.AST) -> list[str]:
    """The names a piece of code reads from outside itself: a function's own arguments and variables,
    and a comprehension's, are its own."""
    if isinstance(node, ast.Name):
        return [node.id] if isinstance(node.ctx, ast.Load) else []
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        args = node.args
        own = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
        own |= {a.arg for a in (args.vararg, args.kwarg) if a is not None}
        outer = [n for d in [*args.defaults, *args.kw_defaults] if d is not None for n in _reads(d)]
        if isinstance(node, ast.Lambda):
            return outer + [n for n in _reads(node.body) if n not in own]
        outer += [n for d in node.decorator_list for n in _reads(d)]
        body = ast.Module(body=node.body, type_ignores=[])
        own |= _stores(body) | {node.name}
        return outer + [n for statement in node.body for n in _reads(statement) if n not in own]
    if isinstance(node, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp):
        own = {name for generator in node.generators for name in _stores(generator.target)}
        return [n for child in ast.iter_child_nodes(node) for n in _reads(child) if n not in own]
    return [n for child in ast.iter_child_nodes(node) for n in _reads(child)]


_MAGIC = re.compile(r"^\s*[%!]")
_ASSIGNED = re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=\n]+)?=(?!=)", re.MULTILINE)
_DEFINED = re.compile(r"^(?:async\s+def|def|class)\s+([A-Za-z_]\w*)", re.MULTILINE)


def facts(code: str) -> Facts:
    """What `code` makes and needs. Code that does not parse (something half typed) is read by its
    lines: what it assigns at the start of a line, and every name it holds as a need."""
    if code.lstrip().startswith("%%"):
        return Facts()  # a cell magic's body is not Python
    plain = "\n".join("" if _MAGIC.match(line) else line for line in code.split("\n"))
    try:
        tree = ast.parse(plain)
    except (SyntaxError, ValueError, RecursionError):
        by_line = list(dict.fromkeys(_ASSIGNED.findall(plain) + _DEFINED.findall(plain)))
        return Facts(tuple(by_line), tuple(n for n in _python_names(plain) if n not in _BUILTINS))
    made: list[str] = []
    needs: list[str] = []
    for statement in tree.body:
        for name in _reads(statement):
            if name not in made and name not in needs and name not in _BUILTINS:
                needs.append(name)
        for name in _made(statement):
            if name not in made:
                made.append(name)
    # A text on a line of its own (a docstring) is not a statement that does anything.
    statements = [s for s in tree.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    imports_only = bool(statements) and all(isinstance(s, ast.Import | ast.ImportFrom) for s in statements)
    return Facts(tuple(made), tuple(needs), imports_only)


# ===== the uses line: `# uses orders @cell3, customers @cell5` =====

_USE = r"[A-Za-z_]\w*(?: @cell\d+)?"
USES_PYTHON: re.Pattern[str] = re.compile(rf"^# uses {_USE}(?:, {_USE})*$")
USES_SQL: re.Pattern[str] = re.compile(rf"^-- uses {_USE}(?:, {_USE})*$")


def split_uses(source: str, sql: bool = False) -> tuple[str | None, str]:
    """A cell's source as (the app's own first line or None, the rest). Only a first line in exactly
    the app's format is the app's; any other comment is the user's."""
    first, newline, rest = source.partition("\n")
    if (USES_SQL if sql else USES_PYTHON).match(first):
        return first, rest if newline else ""
    return None, source


def uses_line(source: str, tables: Mapping[str, int | None], sql: bool = False) -> str | None:
    """The line a cell with this source should start with: the tables it names, in the order it first
    names them, each with a link to that table's cell. None: it names no table."""
    _, code = split_uses(source, sql)
    named = [name for name in names_used(code, sql) if name in tables]
    if not named:
        return None
    parts = [name if tables[name] is None else f"{name} @cell{tables[name]}" for name in named]
    return ("-- uses " if sql else "# uses ") + ", ".join(parts)


def with_uses(source: str, tables: Mapping[str, int | None], sql: bool = False) -> str:
    """`source` with its uses line as it should be: added, changed or taken away. Nothing else in the
    cell is touched."""
    _, code = split_uses(source, sql)
    line = uses_line(source, tables, sql)
    return code if line is None else f"{line}\n{code}"


# ===== the Tables group =====


@dataclass
class PlanCell:
    """A cell as the arrangement sees it. `number` never changes once given."""

    number: int | None
    kind: str  # code, sql or markdown
    source: str
    role: str = ""  # one of ROLES
    table: str | None = None
    grouped: bool = False  # it sits in the Tables group
    fixed: bool = False  # a script's main code (before its first cell): it never moves
    title: str = ""  # a script marker's own words (`# %% Load the data`)
    uses: str | None = None  # the uses line it should start with (None: none)

    def to_json(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "kind": self.kind,
            "source": self.source,
            "role": self.role,
            "table": self.table,
            "grouped": self.grouped,
            "uses": self.uses,
        }


@dataclass(frozen=True)
class TableSpec:
    """An open table: its cell's text, as the grid's steps stand now."""

    name: str
    code: str  # the transformations alone (a maker cell made the table)
    loaded: str  # its load first, then the transformations (no cell made it)
    steps: int = 0
    # The grid's steps as JSON, kept with a notebook's cell so the grid gets them back.
    step_list: tuple[Any, ...] = ()


@dataclass
class TableLine:
    """One table of the group, for its folded line."""

    name: str
    cell: int | None  # its transformations cell
    maker: int | None  # the user's cell that makes it (None: no cell does)
    moved: bool  # its cells sit in the group
    note: str | None  # why not: "orders is made at @cell7 (it needs make_dates)"
    steps: int | None  # how many steps the grid has (None: the table is not open)
    open: bool
    code: str

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "cell": self.cell,
            "maker": self.maker,
            "moved": self.moved,
            "note": self.note,
            "steps": self.steps,
            "open": self.open,
            "code": self.code,
        }


@dataclass
class Arrangement:
    cells: list[PlanCell]
    tables: list[TableLine] = field(default_factory=list)
    next_number: int = 1


def _names_list(names: Sequence[str]) -> str:
    shown = list(names[:3])
    if len(names) > 3:
        return ", ".join(shown) + f" and {len(names) - 3} more"
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]


def _numbered(cells: Sequence[PlanCell], next_number: int, give: bool) -> tuple[list[PlanCell], int]:
    """Each cell with its number: a cell without one (or with another cell's) gets the next."""
    seen: set[int] = set()
    out: list[PlanCell] = []
    highest = max([next_number - 1, *(c.number for c in cells if c.number is not None)])
    for cell in cells:
        number = cell.number
        if number is not None and (number in seen or number < 1):
            number = None
        if number is None and give and not cell.fixed:
            highest += 1
            number = highest
        if number is not None:
            seen.add(number)
        out.append(replace(cell, number=number))
    return out, highest + 1


def arrange(
    cells: Sequence[PlanCell],
    specs: Sequence[TableSpec],
    next_number: int = 1,
    move: bool = True,
    headings: bool = True,
) -> Arrangement:
    """The document's cells with its Tables group as it should be.

    `specs` are the open tables that belong to this document. A table whose cell the file already
    holds stays in the group when it is not open (its cell keeps its text). With `move` False nothing
    is added, taken away or moved (the user is typing): only the tables' cells follow the grid, and
    each of the user's cells learns its uses line. `headings` False: no heading cells (a script's group
    is a region of the file, not cells).
    """
    numbered, next_number = _numbered(cells, next_number, give=move)
    by_name = {spec.name: spec for spec in specs}
    transformations: dict[str, PlanCell] = {}
    for cell in numbered:
        if cell.role == "transformations" and cell.table and cell.table not in transformations:
            transformations[cell.table] = cell
    names = list(transformations)
    if move:
        names += [spec.name for spec in specs if spec.name not in transformations]
    old_headings = {c.table: c for c in numbered if c.role == "table" and c.table}
    top = next((c for c in numbered if c.role == "tables"), None)
    app = ("tables", "table", "transformations")
    users = [c for c in numbered if c.role not in app]
    if move:
        users = [replace(c, role="", table=None, grouped=False) for c in users]
    known = {id(c): facts(split_uses(c.source)[1]) if c.kind == "code" else Facts() for c in users}
    places = {id(c): i for i, c in enumerate(users)}

    def cell_facts(cell: PlanCell) -> Facts:
        return known[id(cell)]

    variables = {name: variable_name(name) for name in names}
    table_of = {variable: name for name, variable in variables.items()}

    # The maker: the first of the user's cells that gives the name a value without reading it.
    makers: dict[str, PlanCell | None] = {}
    for name in names:
        variable = variables[name]
        binders = [c for c in users if c.kind == "code" and variable in cell_facts(c).made]
        fresh = [c for c in binders if variable not in cell_facts(c).needs]
        found = fresh or binders
        makers[name] = found[0] if found else None

    blocked: dict[str, str] = {}
    needs_tables: dict[str, list[str]] = {name: [] for name in names}
    imports_for: dict[str, list[PlanCell]] = {name: [] for name in names}
    for name in names:
        maker = makers[name]
        if maker is None:
            continue
        if maker.fixed:
            blocked[name] = "it is in the script's main code"
            continue
        missing: list[str] = []
        mine = cell_facts(maker)
        for need in mine.needs:
            if need in table_of:
                other = table_of[need]
                if other != name and makers[other] is not maker:
                    needs_tables[name].append(other)
                continue
            providers = [c for c in users if c is not maker and need in cell_facts(c).made]
            if not providers:
                continue  # a built-in, a connection's name, something the kernel already holds
            above = [c for c in providers if places[id(c)] < places[id(maker)]]
            provider = above[-1] if above else None
            if provider is not None and cell_facts(provider).imports_only and not provider.fixed:
                imports_for[name].append(provider)
                continue
            made_there = [t for t in names if provider is not None and makers[t] is provider]
            if made_there:
                needs_tables[name].append(made_there[0])
            else:
                missing.append(need)
        if missing:
            blocked[name] = f"it needs {_names_list(missing)}"
            continue
        # Another cell above that sets the same name would then run after this one.
        for made in mine.made:
            if made in table_of and makers[table_of[made]] is maker:
                continue
            clash = next(
                (
                    c
                    for c in users
                    if c is not maker
                    and places[id(c)] < places[id(maker)]
                    and made in cell_facts(c).made
                    and not cell_facts(c).imports_only
                ),
                None,
            )
            if clash is not None:
                where = f"@cell{clash.number}" if clash.number is not None else "the script's main code"
                blocked[name] = f"{made} is also set at {where}"
                break
    # A table made from one that stays put stays put too.
    changed = True
    while changed:
        changed = False
        for name in names:
            if name in blocked:
                continue
            stuck = [other for other in needs_tables[name] if other in blocked]
            if stuck:
                blocked[name] = f"it needs {_names_list(stuck)}"
                changed = True

    # Tables that share a maker: the first holds the cell, the others come after it.
    owner: dict[str, str] = {}
    for name in names:
        maker = makers[name]
        first = next((n for n in names if maker is not None and makers[n] is maker), name)
        owner[name] = first
        if first != name and first not in needs_tables[name]:
            needs_tables[name].append(first)

    # Each table after the tables it is made from.
    order: list[str] = []
    waiting = list(names)
    while waiting:
        ready = [n for n in waiting if all(o in order or o not in waiting for o in needs_tables[n])]
        chosen = ready[0] if ready else waiting[0]
        order.append(chosen)
        waiting.remove(chosen)

    def code_for(name: str) -> str:
        spec = by_name.get(name)
        if spec is None:
            return transformations[name].source
        return spec.code if makers[name] is not None else spec.loaded

    def note_for(name: str) -> str | None:
        maker = makers[name]
        if maker is None:
            return None
        at = f"at @cell{maker.number}" if maker.number is not None else "in the script's main code"
        if name in blocked:
            reason = blocked[name]
            if reason.startswith("it is in"):
                return f"{name} is made in the script's main code"
            return f"{name} is made {at} ({reason})"
        return f"{name} is made {at}" if owner[name] != name else None

    out: list[PlanCell] = []

    def take_number() -> int:
        nonlocal next_number
        next_number += 1
        return next_number - 1

    def transformations_cell(name: str, grouped: bool) -> PlanCell:
        old = transformations.get(name)
        number = old.number if old is not None else take_number()
        return PlanCell(number, "code", code_for(name), "transformations", name, grouped)

    table_numbers: dict[str, int | None] = {}
    if not move:
        for cell in numbered:
            if cell.role == "transformations" and cell.table and cell.table in transformations:
                kept = cell
                if transformations[cell.table] is cell:
                    kept = replace(cell, source=code_for(cell.table))
                table_numbers[cell.table] = cell.number
                out.append(kept)
            else:
                out.append(cell)
    elif names:
        moving_makers = {id(makers[n]) for n in names if makers[n] is not None and n not in blocked}
        moving_imports: list[PlanCell] = []
        for name in order:
            if name not in blocked:
                for cell in imports_for[name]:
                    if all(cell is not c for c in moving_imports):
                        moving_imports.append(cell)
        moving_imports.sort(key=lambda c: places[id(c)])
        if headings:
            top_number = top.number if top else take_number()
            out.append(PlanCell(top_number, "markdown", TABLES_HEADING, "tables", None, True))
        out += [replace(c, role="imports", grouped=True) for c in moving_imports]
        for name in order:
            if headings:
                note = note_for(name)
                heading = old_headings.get(name)
                text = f"## {name}" + (f"\n\n{note}" if note else "")
                number = heading.number if heading else take_number()
                out.append(PlanCell(number, "markdown", text, "table", name, True))
            if name in blocked:
                continue
            maker = makers[name]
            if maker is not None and owner[name] == name:
                out.append(replace(maker, role="maker", table=name, grouped=True))
            cell = transformations_cell(name, True)
            table_numbers[name] = cell.number
            out.append(cell)
        moved = moving_makers | {id(c) for c in moving_imports}
        for cell in users:
            if id(cell) in moved:
                continue
            made_here = [n for n in order if makers[n] is cell]
            out.append(replace(cell, role="maker", table=made_here[0]) if made_here else cell)
            for name in made_here:
                if name in blocked:
                    placed = transformations_cell(name, False)
                    table_numbers[name] = placed.number
                    out.append(placed)
    else:
        out = list(users)

    # The uses line: on the user's own cells only (not a maker, not the app's cells, not Markdown).
    links = {variables[name]: table_numbers.get(name) for name in names}
    maker_ids = {id(m) for m in makers.values() if m is not None}
    final: list[PlanCell] = []
    for cell in out:
        own = cell.role == "" and id(cell) not in maker_ids and not cell.fixed
        if own and cell.kind in ("code", "sql") and links:
            cell = replace(cell, uses=uses_line(cell.source, links, cell.kind == "sql"))
        else:
            cell = replace(cell, uses=None)
        final.append(cell)

    lines = [
        TableLine(
            name=name,
            cell=table_numbers.get(name),
            maker=makers[name].number if makers[name] is not None else None,  # type: ignore[union-attr]
            moved=name not in blocked,
            note=note_for(name),
            steps=by_name[name].steps if name in by_name else None,
            open=name in by_name,
            code=code_for(name).rstrip("\n"),
        )
        for name in (order if move else names)
    ]
    return Arrangement(final, lines, next_number)


# ===== scripts: the same in a .py file =====
#
#     # region Tables
#     # orders is made at @cell7 (it needs make_dates)        a table whose cells stay put
#     # %% [2]                                                 the imports the tables need
#     import pandas as pd
#     # %% [3] [table] customers                               the user's cell that makes it
#     customers = pd.read_csv("customers.csv")
#     # %% [4] [transformations] customers                     read-only: the grid's steps
#     import duckdb
#     customers = duckdb.sql("""...""").df()
#     # endregion
#     # %%                                                     the plain marker that closes the group
#     ...the script's main code...
#     # %% [7] Load the orders                                 the user's own cells, numbered
#
# VS Code, Spyder, PyCharm and Jupytext read `# %%` as a cell; VS Code folds `# region`. The file runs
# as plain Python from top to bottom.

REGION_START: str = "# region Tables"
REGION_END: str = "# endregion"
_MARKER = re.compile(
    r"^# ?%%(?: \[(?P<number>\d+)\])?(?: \[(?P<role>table|transformations)\] (?P<table>[A-Za-z_][\w-]*))?"
    r"(?P<title>(?: .*)?)$"
)
_REGION_START = re.compile(r"^# ?region Tables\s*$")
_REGION_END = re.compile(r"^# ?endregion\s*$")
_NOTE = re.compile(r"^# [A-Za-z_][\w-]* is made (?:at @cell\d+|in the script's main code)(?: \(.*\))?$")
_PREAMBLE = re.compile(r"^(#!|# -\*- coding|# coding[:=])")


@dataclass
class Script:
    preamble: list[str]  # a `#!` line, a coding line: they stay first
    cells: list[PlanCell]  # the main code is the one with `fixed`
    newline: bool = True  # the file ends with one


def _marker_role(found: re.Match[str], in_region: bool) -> tuple[str, str | None]:
    role = found.group("role")
    if role == "table":
        return "maker", found.group("table")
    if role == "transformations":
        return "transformations", found.group("table")
    return ("imports" if in_region else ""), None


def parse_script(text: str) -> Script:
    """A script's text as cells: the Tables group's (if it has one), the main code, the `# %%` cells.
    A cell's source is exactly the lines under its marker, each ending in its newline."""
    lines = text.split("\n")
    newline = text.endswith("\n")
    if newline:
        lines.pop()
    preamble: list[str] = []
    while lines and _PREAMBLE.match(lines[0]):
        preamble.append(lines.pop(0))
    grouped: list[PlanCell] = []
    index = 0
    end = next((i for i, line in enumerate(lines) if _REGION_END.match(line)), None)
    if lines and _REGION_START.match(lines[0]) and end is not None:
        loose: list[str] = []  # lines before the group's first cell that are not its notes
        current: PlanCell | None = None
        body: list[str] = []
        for line in lines[1:end]:
            found = _MARKER.match(line)
            if found is None:
                if current is not None:
                    body.append(line)
                elif line.strip() and not _NOTE.match(line):
                    loose.append(line)
                continue
            if current is not None:
                current.source = _joined(body)
            role, table = _marker_role(found, True)
            number = int(found.group("number")) if found.group("number") else None
            current = PlanCell(number, "code", "", role, table, True, title=found.group("title").strip())
            grouped.append(current)
            body = []
        if current is not None:
            current.source = _joined(body)
        if loose:
            grouped.insert(0, PlanCell(None, "code", _joined(loose), "imports", None, True))
        index = end + 1
        # The plain `# %%` that closes the group belongs to it.
        if index < len(lines) and lines[index].strip() in ("# %%", "#%%"):
            index += 1
    main = PlanCell(None, "code", "", fixed=True)
    rest: list[PlanCell] = []
    current = main
    body = []
    for line in lines[index:]:
        found = _MARKER.match(line)
        if found is None:
            body.append(line)
            continue
        current.source = _joined(body)
        role, table = _marker_role(found, False)
        number = int(found.group("number")) if found.group("number") else None
        # Outside the group only a table's own cell keeps a role; a maker there is found again.
        role = role if role == "transformations" else ""
        current = PlanCell(
            number, "code", "", role, table if role else None, False, title=found.group("title").strip()
        )
        rest.append(current)
        body = []
    current.source = _joined(body)
    return Script(preamble, [*grouped, main, *rest], newline)


def _joined(lines: Sequence[str]) -> str:
    return "".join(line + "\n" for line in lines)


def _body(source: str) -> list[str]:
    """A cell's lines as they go into the file (none for an empty cell)."""
    if source == "":
        return []
    return (source[:-1] if source.endswith("\n") else source).split("\n")


def _marker_line(cell: PlanCell) -> str:
    parts = ["# %%"]
    if cell.number is not None:
        parts.append(f"[{cell.number}]")
    if cell.role == "transformations" and cell.table:
        parts.append(f"[transformations] {cell.table}")
    elif cell.role == "maker" and cell.table and cell.grouped:
        parts.append(f"[table] {cell.table}")
    if cell.title:
        parts.append(cell.title)
    return " ".join(parts)


def _script_source(cell: PlanCell) -> str:
    """A cell's lines for the file: the user's cells with their uses line as it should be."""
    if cell.fixed or cell.role in ("transformations", "imports"):
        return cell.source
    code = split_uses(cell.source)[1]
    return code if cell.uses is None else f"{cell.uses}\n{code}"


def write_script(script: Script, arrangement: Arrangement) -> str:
    """The script's text with its cells as arranged (each user cell's uses line as it should be)."""
    cells = arrangement.cells
    grouped = [c for c in cells if c.grouped and c.role not in ("tables", "table")]
    main = next((c for c in cells if c.fixed), None)
    rest = [c for c in cells if not c.grouped and not c.fixed]
    out: list[str] = list(script.preamble)
    has_tables = bool(arrangement.tables)
    if has_tables:
        out.append(REGION_START)
        out += [f"# {line.note}" for line in arrangement.tables if line.note]
        for cell in grouped:
            out.append(_marker_line(cell))
            out += _body(_script_source(cell))
        out.append(REGION_END)
        out.append("# %%")
    if main is not None:
        out += _body(main.source)
    for cell in rest:
        out.append(_marker_line(cell))
        out += _body(_script_source(cell))
    text = "\n".join(out)
    return text + "\n" if out and script.newline else text


def arrange_script(text: str, specs: Sequence[TableSpec], move: bool = True) -> tuple[str, Arrangement]:
    """A script's text with its Tables group as it should be, and the arrangement behind it. The
    arrangement's cells are the new text's."""
    script = parse_script(text)
    plan = [c for c in script.cells if c.role not in ("notes",)]
    numbers = [c.number for c in plan if c.number is not None]
    arrangement = arrange(plan, specs, max(numbers, default=0) + 1, move, headings=False)
    return write_script(script, arrangement), arrangement


def outline(text: str) -> dict[str, Any]:
    """Where a script's cells are, for the editor: each cell's marker line and last line (1-based), its
    number, role and table, its first line of code; and the Tables group's first and last line."""
    lines = text.split("\n")
    first = sum(1 for _ in _leading(lines))
    end = next((i for i, line in enumerate(lines) if i > first and _REGION_END.match(line)), None)
    region: list[int] | None = None
    if first < len(lines) and _REGION_START.match(lines[first]) and end is not None:
        region = [first + 1, end + 1]
    cells: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def close(last: int) -> None:
        if current is not None:
            current["end"] = last
            code = [b for b in lines[current["line"] : last] if b.strip() and not USES_PYTHON.match(b)]
            current["first"] = (code[0] if code else "").strip()[:160]
            cells.append(current)

    for at, line in enumerate(lines, start=1):
        inside = region is not None and region[0] < at < region[1]
        if region is not None and at == region[1]:
            close(at - 1)
            current = None
            continue
        found = _MARKER.match(line)
        if found is None or (region is not None and at == region[0]):
            continue
        close(at - 1)
        current = None
        if region is not None and at == region[1] + 1 and line.strip() in ("# %%", "#%%"):
            continue  # the plain marker that closes the group: the main code follows, not a cell
        role, table = _marker_role(found, inside)
        current = {
            "number": int(found.group("number")) if found.group("number") else None,
            "line": at,
            "role": role if inside or role == "transformations" else "",
            "table": table,
            "grouped": inside,
        }
    close(len(lines))
    return {"region": region, "cells": cells}


def _leading(lines: Sequence[str]) -> Iterable[str]:
    for line in lines:
        if not _PREAMBLE.match(line):
            return
        yield line


# ===== a table's cell as text =====


@dataclass(frozen=True)
class Origin:
    """Where a table came from, for the load its cell starts with when no code made it."""

    kind: str = "unknown"  # python, file, dropped, paste, connection, query, unknown
    kernel: str | None = None  # the Workbench kernel that pushed it
    path: str | None = None
    sheet: str | None = None
    filename: str | None = None
    connection: str | None = None
    sql: str | None = None  # the query a connection's table was read with, or a "+" tab's query

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "kernel": self.kernel}


def _comment(text: str) -> str:
    return "\n".join(f"# {line}".rstrip() for line in text.split("\n"))


def sql_value(value: Any) -> str | None:
    """A value as DuckDB SQL text (None: it cannot be written, bytes say)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return f"CAST('{value}' AS DOUBLE)"
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return f"'{value.isoformat(sep=' ')}'"
    if isinstance(value, date | time):
        return f"'{value.isoformat()}'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def pasted_query(columns: Sequence[Column], rows: Sequence[Sequence[Any]]) -> str | None:
    """A small pasted table as one query that makes it again, every column in its own type. None: too
    large to write into a cell (PASTE_MAX_ROWS, PASTE_MAX_COLUMNS, PASTE_MAX_CHARS), or a value SQL
    text cannot hold."""
    if not columns or len(rows) > PASTE_MAX_ROWS or len(columns) > PASTE_MAX_COLUMNS or not rows:
        return None
    written: list[str] = []
    size = 0
    for row in rows:
        values = [sql_value(v) for v in row]
        if any(v is None for v in values):
            return None
        line = "    (" + ", ".join(v for v in values if v is not None) + ")"
        size += len(line)
        if size > PASTE_MAX_CHARS:
            return None
        written.append(line)
    select = ",\n".join(
        f"    CAST(c{i} AS {column.type}) AS {quote(column.name)}" for i, column in enumerate(columns, 1)
    )
    names = ", ".join(f"c{i}" for i in range(1, len(columns) + 1))
    return f"SELECT\n{select}\nFROM (VALUES\n" + ",\n".join(written) + f"\n) AS pasted({names})"


_READERS: dict[str, str] = {
    "csv_file": "read_csv",
    "parquet_file": "read_parquet",
    "json_file": "read_json",
}


def load_code(
    dataset: str, origin: Origin, source_kind: str, pasted: str | None, rows: int
) -> tuple[list[str], str]:
    """The lines that make a table no code made: (the imports they need, the lines). When the app
    cannot write a load (a file dropped on the page, a paste too large), a note says so and what to do."""
    var = variable_name(dataset)
    module = "duckdb" if var != "duckdb" else "ddb"
    duck = "import duckdb" if module == "duckdb" else "import duckdb as ddb"
    if origin.kind == "file" and origin.path:
        if source_kind in _READERS:
            return [duck], f"{var} = {module}.{_READERS[source_kind]}({origin.path!r}).df()"
        if source_kind == "excel_file":
            sheet = f", sheet_name={origin.sheet!r}" if origin.sheet else ""
            return ["import pandas as pd"], f"{var} = pd.read_excel({origin.path!r}{sheet})"
        return [], _comment(
            f"{dataset} was opened from {origin.path}.\nPut the code that reads it in a cell above this one."
        )
    if origin.kind == "connection" and origin.connection and origin.sql:
        text = python_string(origin.sql)
        return ["import swissarmygrid as sag"], f"{var} = sag.sql({origin.connection!r}, {text})"
    if origin.kind == "query" and origin.sql:
        return [duck], f"{var} = {module}.sql({python_string(origin.sql.strip())}).df()"
    if origin.kind == "paste":
        if pasted is not None:
            return [duck], f"{var} = {module}.sql({python_string(pasted)}).df()"
        return [], _comment(
            f"{dataset} was pasted into the grid ({rows:,} rows): too large to write here.\n"
            "Export it to a file and open that file, and this cell will load it."
        )
    if origin.kind == "dropped":
        named = f" ({origin.filename})" if origin.filename else ""
        return [], _comment(
            f"{dataset} was dropped on the page{named}: the browser does not say where the file is.\n"
            "Open it from its folder (Open file), and this cell will load it."
        )
    return [], _comment(
        f"{dataset} was made outside this file (in the console, or sent with sag.push).\n"
        "Put the code that makes it in a cell above this one."
    )


def transformations_code(dataset: str, source_kind: str, query: str | None) -> tuple[list[str], str]:
    """A table's transformations as (imports, one statement): the grid's steps as one DuckDB query
    whose result is the table's own name, in the form the table had (.df(), .pl(), ...). With no
    steps: a comment, and nothing to run."""
    var = variable_name(dataset)
    if query is None:
        return [], f"# {var}: no transformations yet. Steps made in the grid appear here."
    module = "duckdb" if var != "duckdb" else "ddb"
    duck = "import duckdb" if module == "duckdb" else "import duckdb as ddb"
    return [duck], f"{var} = {module}.sql({python_string(query)}){output_call(source_kind)}"


def cell_text(parts: Iterable[tuple[list[str], str]]) -> str:
    """A cell's text from its parts: the imports once, first; then each part, a blank line between."""
    pieces = list(parts)
    imports = list(dict.fromkeys(line for needed, _ in pieces for line in needed))
    blocks = (["\n".join(imports)] if imports else []) + [body for _, body in pieces if body]
    return "\n\n".join(blocks)
