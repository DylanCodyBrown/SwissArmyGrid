"""The notebook tab: the changes SwissArmyGrid makes to the open marimo notebook (plan part 2).

  imports     add missing imports to the open notebook, in a new cell at the top
  drop        code dragged from the rail: into the cell it was dropped on, or after it
  grid cell   a grid's steps as one marimo SQL cell in the notebook (code-behind)

The notebook itself (marimo, started on first use, joined as a kiosk to add cells) is behind the
`Notebook` port (server/notebook.py). The agent never reaches the notebook: it runs code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from swissarmygrid.domain.datasets import DatasetInfo
from swissarmygrid.domain.grids import Grid
from swissarmygrid.services.codegen import generate_code
from swissarmygrid.services.ports import BACKGROUND, CodeVerifier, SqlSession
from swissarmygrid.services.workspace import Conflict, Workspace

_IMPORT_LINE_MAX: int = 300


class Notebook(Protocol):
    """The open notebook, changed from outside (server/notebook.NotebookControl)."""

    async def ensure_imports(self, file: str, lines: list[str]) -> list[str]: ...

    async def keep(self, file: str, key: str, code: str, imports: list[str]) -> dict[str, object]: ...

    async def drop(
        self, file: str, code: str, imports: list[str], target: str | None
    ) -> dict[str, object]: ...


def sql_cell(variable: str, sql: str) -> str:
    """A marimo SQL cell, `variable = mo.sql(\"\"\"...\"\"\")`, which marimo shows as one of its own SQL
    cells. The quotes are whichever triple quotes the query does not hold, raw when it holds a backslash
    (a LIKE escape) so Python leaves it alone; a query holding both kinds goes as one plain string."""
    quote = '"""' if '"""' not in sql else "'''" if "'''" not in sql else None
    if quote is None or (quote and "\\" in sql and sql.endswith("\\")):
        return f"{variable} = mo.sql({sql!r})"
    prefix = "r" if "\\" in sql else ""
    return f"{variable} = mo.sql(\n    {prefix}{quote}\n{sql}\n{quote}\n)"


def check_imports(lines: Any, allow_empty: bool = False) -> list[str]:
    """The import statements, checked and trimmed."""
    if (
        not isinstance(lines, list)
        or not (0 if allow_empty else 1) <= len(lines) <= 20
        or not all(isinstance(line, str) and 0 < len(line) <= _IMPORT_LINE_MAX for line in lines)
        or not all(line.lstrip().startswith(("import ", "from ")) for line in lines)
    ):
        raise ValueError("Send 1 to 20 import statements, such as import swissarmygrid as sag.")
    return [line.strip() for line in lines]


def check_file(file: Any) -> str:
    if not isinstance(file, str) or "/" in file or "\\" in file or not file.endswith(".py"):
        raise ValueError("Name a notebook in the notebooks folder, such as workspace.py.")
    return file


@dataclass(frozen=True)
class GridCell:
    """A grid's steps as a SQL cell, checked against the grid: the variable it makes and its code."""

    variable: str
    code: str


class NotebookCells:
    def __init__(self, ws: Workspace, notebook: Notebook, verifier: CodeVerifier, default_file: str) -> None:
        self.ws = ws
        self._notebook = notebook
        self._verifier = verifier
        self._default_file = default_file

    async def imports(self, body: dict[str, Any]) -> dict[str, Any]:
        lines = body.get("lines")
        file = body.get("file") or self._default_file
        checked = check_imports(lines)
        check_file(file)
        return {"added": await self._notebook.ensure_imports(file, checked)}

    async def drop(self, body: dict[str, Any]) -> dict[str, object]:
        code, lines, cell = body.get("code"), body.get("imports", []), body.get("cell")
        file = body.get("file") or self._default_file
        if not isinstance(code, str) or not code.strip() or len(code) > 100_000:
            raise ValueError("Send the dropped code (1 to 100,000 characters).")
        checked = check_imports(lines, allow_empty=True)
        if cell is not None and (not isinstance(cell, str) or len(cell) > 64):
            raise ValueError("cell is a cell id.")
        check_file(file)
        return await self._notebook.drop(file, code, checked, cell)

    async def grid_cell(self, grid: Grid, info: DatasetInfo) -> GridCell:
        """Code-behind (plan phase 2): this grid's steps, as one DuckDB query over its table's own
        variable. Only code the server ran and matched against the grid goes in (the same rule as the
        Copy buttons)."""
        ws = self.ws

        def make(cur: SqlSession) -> dict[str, Any]:
            return generate_code(
                cur,
                ws.views,
                info,
                grid.steps,
                grid.settings,
                "duckdb",
                verifier=self._verifier,
                grammar=ws.grammar,
            )

        result = await ws.grid_query(grid, None, BACKGROUND, make)
        if not result["verified"]:
            raise Conflict(
                "This grid's steps could not be checked as SQL, so they were not written to the "
                "notebook: "
                f"{result.get('difference') or result.get('note') or 'the copy did not match the grid'}"
            )
        variable = result.get("result_variable") or f"{info.name}_view"
        return GridCell(str(variable), sql_cell(str(variable), str(result["sql"])))

    async def keep_grid_cell(
        self, grid: Grid, info: DatasetInfo, cell: GridCell, file: Any
    ) -> dict[str, object]:
        """Write the grid's cell into the notebook (replacing the one it wrote before) and run it."""
        file = file or self._default_file
        check_file(file)
        kept = await self._notebook.keep(str(file), f"grid:{grid.id}", cell.code, ["import marimo as mo"])
        self.ws.audit.write("user", "notebook_cell", info.name)
        return kept | {"variable": cell.variable, "code": cell.code}
