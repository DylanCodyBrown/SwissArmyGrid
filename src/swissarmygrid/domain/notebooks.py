"""The Workbench's documents: notebooks (.ipynb, so Jupyter and VS Code open them too) and scripts (.py).
Pure rules: no I/O. Plans/workbench-and-connections.plan.md.

A notebook here is a list of cells, each code (Python), sql (run on a connection) or markdown, with
the outputs a run gave (kept as nbformat's own output records). In the file, a SQL cell is a code cell
that starts with its `%%sql <connection>` line (any Jupyter with SwissArmyGrid runs it as it is), marked
so the Workbench opens it as a SQL cell again.

Every cell has a number that never changes (docs/ideas.md, 2026-11-14): given when the cell is made,
never reused or shifted when cells move or go. It is kept in the cell's metadata, with the cell's part
in the Tables group (domain/table_cells.py): the group's heading, a table's heading, the user's cell
that makes a table, the imports the tables need, a table's read-only transformations.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

MAX_CELLS: int = 2_000
MAX_CELL_CHARS: int = 1_000_000
MAX_OUTPUTS_PER_CELL: int = 200
KINDS: tuple[str, ...] = ("code", "sql", "markdown")
ROLES: tuple[str, ...] = ("", "tables", "table", "maker", "imports", "transformations")
MAX_STEPS_CHARS: int = 200_000
_SQL_LINE = re.compile(r"^%%(?:sql|sag)\b(?!_)")


class NotebookError(ValueError):
    """A notebook or script that cannot be read or saved as sent (the message says why)."""


@dataclass
class Cell:
    kind: str  # code, sql or markdown
    source: str  # a SQL cell's query, without its %%sql line
    connection: str | None = None  # a SQL cell's connection (None: the %sql_connect one)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    execution_count: int | None = None
    number: int | None = None  # the cell's own number: never changes, never reused
    role: str = ""  # its part in the Tables group (ROLES); "" for a cell of the user's own
    table: str | None = None  # the table a heading, a maker or a transformations cell belongs to
    grouped: bool = False  # it sits in the Tables group
    # A transformations cell's steps as the grid holds them, so the grid gets them back when the
    # table is opened again.
    steps: list[Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "connection": self.connection,
            "outputs": self.outputs,
            "execution_count": self.execution_count,
            "number": self.number,
            "role": self.role,
            "table": self.table,
            "grouped": self.grouped,
            "steps": self.steps,
        }

    def marks(self) -> dict[str, Any]:
        """What the file keeps about the cell beside its text (the notebook's `swissarmygrid` metadata)."""
        kept: dict[str, Any] = {}
        if self.kind == "sql":
            kept |= {"kind": "sql", "connection": self.connection}
        if self.number is not None:
            kept["number"] = self.number
        if self.role:
            kept["role"] = self.role
        if self.table:
            kept["table"] = self.table
        if self.grouped:
            kept["grouped"] = True
        if self.steps is not None and self.role == "transformations":
            kept["steps"] = self.steps
        return kept


def sql_line(connection: str | None) -> str:
    """The first line of a SQL cell's code: `%%sql Warehouse`, quoted when the name has a space."""
    if not connection:
        return "%%sql"
    return f"%%sql {shlex.quote(connection) if re.search(r'\s', connection) else connection}"


def code_of(cell: Cell) -> str:
    """What the kernel runs for the cell (markdown runs nothing)."""
    if cell.kind == "sql":
        return f"{sql_line(cell.connection)}\n{cell.source}"
    return cell.source if cell.kind == "code" else ""


def _text(value: Any) -> str:
    """nbformat keeps text as one string or a list of lines."""
    if isinstance(value, list):
        return "".join(str(v) for v in value)
    return value if isinstance(value, str) else ""


def _lines(text: str) -> list[str]:
    """Text as nbformat writes it: a list of lines, each keeping its newline."""
    return text.splitlines(keepends=True)


def _marked(cell: Cell, raw: dict[str, Any]) -> Cell:
    """A cell with what the file keeps about it: its number and its part in the Tables group."""
    marks = (raw.get("metadata") or {}).get("swissarmygrid")
    if not isinstance(marks, dict):
        return cell
    number = marks.get("number")
    if isinstance(number, int) and not isinstance(number, bool) and number > 0:
        cell.number = number
    if marks.get("role") in ROLES:
        cell.role = str(marks["role"])
    if isinstance(marks.get("table"), str):
        cell.table = marks["table"]
    cell.grouped = marks.get("grouped") is True
    if isinstance(marks.get("steps"), list) and cell.role == "transformations":
        cell.steps = marks["steps"]
    return cell


def number_cells(cells: list[Cell], next_number: int = 1) -> int:
    """Give each cell without a number (or with an earlier cell's) the next one; the next after that.
    A number is never reused: `next_number` is the file's own count of the numbers given so far."""
    seen: set[int] = set()
    highest = max([next_number - 1, *(c.number for c in cells if c.number is not None)])
    for cell in cells:
        if cell.number is None or cell.number in seen:
            highest += 1
            cell.number = highest
        seen.add(cell.number)
    return highest + 1


def read_notebook(text: str) -> list[Cell]:
    """The cells of an .ipynb file's text (nbformat 4), each with its number."""
    return read_document(text)[0]


def read_document(text: str) -> tuple[list[Cell], int]:
    """A notebook's cells, and the number its next new cell gets."""
    try:
        data = json.loads(text)
    except ValueError as error:
        raise NotebookError(f"This is not a notebook file: {error}.") from None
    if not isinstance(data, dict) or not isinstance(data.get("cells"), list):
        raise NotebookError("This is not a notebook file: it has no cells.")
    if int(data.get("nbformat", 4)) < 4:
        raise NotebookError(
            "This notebook is in an old format (before 4): open it in Jupyter and save it again."
        )
    cells: list[Cell] = []
    for raw in data["cells"][:MAX_CELLS]:
        if not isinstance(raw, dict):
            continue
        source = _text(raw.get("source"))
        kind = raw.get("cell_type")
        if kind == "markdown":
            cells.append(_marked(Cell("markdown", source), raw))
        elif kind == "code":
            first, _, rest = source.partition("\n")
            marked = (raw.get("metadata") or {}).get("swissarmygrid", {}).get("kind") == "sql"
            if _SQL_LINE.match(first) or marked:
                words = first.split(None, 1)
                try:
                    named = " ".join(shlex.split(words[1])) if len(words) > 1 else None
                except ValueError:
                    named = words[1] if len(words) > 1 else None
                cell = Cell("sql", rest, named or None)
            else:
                cell = Cell("code", source)
            outputs = raw.get("outputs")
            cell.outputs = (
                [o for o in outputs if isinstance(o, dict)][:MAX_OUTPUTS_PER_CELL]
                if isinstance(outputs, list)
                else []
            )
            count = raw.get("execution_count")
            cell.execution_count = count if isinstance(count, int) else None
            cells.append(_marked(cell, raw))
        elif kind == "raw":
            cells.append(_marked(Cell("markdown", source), raw))
    kept = (data.get("metadata") or {}).get("swissarmygrid")
    counted = kept.get("next_cell") if isinstance(kept, dict) else None
    return cells, number_cells(cells, counted if isinstance(counted, int) and counted > 0 else 1)


def read_cells(sent: Any) -> list[Cell]:
    """The cells as the Workbench page sends them, checked."""
    if not isinstance(sent, list) or len(sent) > MAX_CELLS:
        raise NotebookError(f"Send the cells as a list (at most {MAX_CELLS:,}).")
    cells: list[Cell] = []
    for raw in sent:
        if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
            raise NotebookError('Each cell is {"kind": "code", "sql" or "markdown", "source": "..."}.')
        source = raw.get("source", "")
        if not isinstance(source, str) or len(source) > MAX_CELL_CHARS:
            raise NotebookError("A cell's source is text of at most a million characters.")
        connection = raw.get("connection")
        if connection is not None and (
            not isinstance(connection, str) or "\n" in connection or len(connection) > 64
        ):
            raise NotebookError("A SQL cell's connection is a connection's name.")
        outputs = raw.get("outputs") or []
        if not isinstance(outputs, list) or not all(isinstance(o, dict) for o in outputs):
            raise NotebookError("A cell's outputs are a list of output records.")
        count = raw.get("execution_count")
        number = raw.get("number")
        if number is not None and (isinstance(number, bool) or not isinstance(number, int) or number < 1):
            raise NotebookError("A cell's number is a whole number from 1.")
        role = raw.get("role") or ""
        table = raw.get("table")
        if role not in ROLES or (table is not None and (not isinstance(table, str) or len(table) > 64)):
            raise NotebookError("A cell's role is one of the Tables group's, and its table a table's name.")
        steps = raw.get("steps")
        if steps is not None and (not isinstance(steps, list) or len(json.dumps(steps)) > MAX_STEPS_CHARS):
            raise NotebookError("A table cell's steps are a list of the grid's steps.")
        cells.append(
            Cell(
                str(raw["kind"]),
                source,
                connection or None,
                outputs[:MAX_OUTPUTS_PER_CELL] if raw["kind"] != "markdown" else [],
                count if isinstance(count, int) else None,
                number,
                role,
                table or None,
                raw.get("grouped") is True,
                steps,
            )
        )
    return cells


def write_notebook(cells: list[Cell], python_version: str = "3", next_number: int | None = None) -> str:
    """The cells as an .ipynb file's text (nbformat 4.5). Each cell's number is in its metadata, and
    the notebook's keeps the next number to give, so a deleted cell's number is never given again."""
    following = number_cells(cells, next_number or 1)
    out: list[dict[str, Any]] = []
    for i, cell in enumerate(cells):
        marks = cell.marks()
        metadata = {"swissarmygrid": marks} if marks else {}
        base: dict[str, Any] = {"id": f"cell-{i + 1}", "metadata": metadata}
        if cell.kind == "markdown":
            out.append(base | {"cell_type": "markdown", "source": _lines(cell.source)})
            continue
        out.append(
            base
            | {
                "cell_type": "code",
                "source": _lines(code_of(cell)),
                "outputs": cell.outputs,
                "execution_count": cell.execution_count,
            }
        )
    notebook = {
        "cells": out,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": python_version},
            "swissarmygrid": {"next_cell": following},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    return json.dumps(notebook, indent=1, ensure_ascii=False) + "\n"


def kind_of(path: str) -> str:
    """ "notebook" or "script", by the file's ending."""
    lowered = path.lower()
    if lowered.endswith(".ipynb"):
        return "notebook"
    if lowered.endswith(".py"):
        return "script"
    raise NotebookError("The Workbench opens notebooks (.ipynb) and Python scripts (.py).")


def quote_path(path: str) -> str:
    """A path as IPython's %run reads it: quoted when it has spaces (Windows backslashes kept)."""
    if not re.search(r'[\s"\']', path):
        return path
    return '"' + path.replace('"', '\\"') + '"'


# Lines that carry on the statement above them, not start a new one.
_CARRY_ON = re.compile(r"^\s*(?:elif\b|else\b|except\b|finally\b|case\b|[)\]}])")


def _top_level(code: str) -> list[tuple[int, int]]:
    """Each top-level statement's first and last line (a decorator's line is its function's first)."""
    spans: list[tuple[int, int]] = []
    for node in ast.parse(code).body:
        decorators = getattr(node, "decorator_list", [])
        start = min([node.lineno, *(d.lineno for d in decorators)])
        spans.append((start, node.end_lineno or node.lineno))
    return spans


def statement_at(code: str, line: int) -> tuple[int, int, int | None] | None:
    """Ctrl+Enter in a script (RStudio's run line, VS Code's smart send): the whole top-level statement
    the 1-based `line` is in, as (first line, last line, the next statement's first line or None).
    None: the line is blank or a comment (nothing runs; the page moves on). While the file as a whole
    does not parse (something half typed further down), the statement is found by its indentation and
    the shortest run of lines that parses; failing that, the line alone, so Python's error says why."""
    lines = code.split("\n")
    if not 1 <= line <= len(lines):
        raise NotebookError(f"There is no line {line}: the code has {len(lines)}.")
    try:
        spans = _top_level(code)
    except SyntaxError:
        spans = []
        found = _by_indentation(lines, line)
        if found is None:
            return None
        start, end = found
        after = next((n for n in range(end + 1, len(lines) + 1) if _starts_statement(lines[n - 1])), None)
        return start, end, after
    for i, (start, end) in enumerate(spans):
        if start <= line <= end:
            return start, end, spans[i + 1][0] if i + 1 < len(spans) else None
    return None


def _starts_statement(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and not stripped.startswith("#") and not text[:1].isspace()


def _by_indentation(lines: list[str], line: int) -> tuple[int, int] | None:
    here = lines[line - 1].strip()
    if not here or here.startswith("#"):
        return None
    start = line
    while start > 1 and (
        lines[start - 1][:1].isspace() or not lines[start - 1].strip() or _CARRY_ON.match(lines[start - 1])
    ):
        start -= 1
    while start < line and not _starts_statement(lines[start - 1]):
        start += 1
    for end in range(line, min(len(lines), line + 500) + 1):
        following = lines[end] if end < len(lines) else ""
        if following.strip() and (following[:1].isspace() or _CARRY_ON.match(following)):
            continue  # the statement goes on below
        try:
            ast.parse("\n".join(lines[start - 1 : end]))
        except SyntaxError:
            continue
        return start, end
    return line, line
