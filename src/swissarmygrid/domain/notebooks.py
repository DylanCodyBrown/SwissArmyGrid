"""The Workbench's documents: notebooks (.ipynb, so Jupyter and VS Code open them too) and scripts (.py).
Pure rules: no I/O. Plans/workbench-and-connections.plan.md.

A notebook here is a list of cells, each code (Python), sql (run on a connection) or markdown, with
the outputs a run gave (kept as nbformat's own output records). In the file, a SQL cell is a code cell
that starts with its `%%sql <connection>` line (any Jupyter with SwissArmyGrid runs it as it is), marked
so the Workbench opens it as a SQL cell again.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

MAX_CELLS: int = 2_000
MAX_CELL_CHARS: int = 1_000_000
MAX_OUTPUTS_PER_CELL: int = 200
KINDS: tuple[str, ...] = ("code", "sql", "markdown")
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

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "connection": self.connection,
            "outputs": self.outputs,
            "execution_count": self.execution_count,
        }


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


def read_notebook(text: str) -> list[Cell]:
    """The cells of an .ipynb file's text (nbformat 4)."""
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
            cells.append(Cell("markdown", source))
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
            cells.append(cell)
        elif kind == "raw":
            cells.append(Cell("markdown", source))
    return cells


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
        cells.append(
            Cell(
                str(raw["kind"]),
                source,
                connection or None,
                outputs[:MAX_OUTPUTS_PER_CELL] if raw["kind"] != "markdown" else [],
                count if isinstance(count, int) else None,
            )
        )
    return cells


def write_notebook(cells: list[Cell], python_version: str = "3") -> str:
    """The cells as an .ipynb file's text (nbformat 4.5)."""
    out: list[dict[str, Any]] = []
    for i, cell in enumerate(cells):
        base: dict[str, Any] = {"id": f"cell-{i + 1}", "metadata": {}}
        if cell.kind == "markdown":
            out.append(base | {"cell_type": "markdown", "source": _lines(cell.source)})
            continue
        metadata = (
            {"swissarmygrid": {"kind": "sql", "connection": cell.connection}} if cell.kind == "sql" else {}
        )
        out.append(
            base
            | {
                "cell_type": "code",
                "metadata": metadata,
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
