"""Code handed to your own Python: Jupyter, or Python in the terminal.

The page's "To Python" buttons (an analysis, a grid's steps) send code here; it waits in a short queue,
in memory only, until `sag.paste()` in Jupyter or IPython takes the newest and puts it in the cell (or
at the prompt). The page also copies it to the clipboard, so pasting by hand works anywhere.

A grid's steps go as DuckDB code over the user's own table, and only when the server ran it and it
matched the grid (the Copy buttons' rule). The agent never sends code here: what lands in the user's
Python is the user's.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

from swissarmygrid.domain.datasets import DatasetInfo
from swissarmygrid.domain.grids import Grid
from swissarmygrid.services.codegen import generate_code
from swissarmygrid.services.ports import BACKGROUND, CodeVerifier, SqlSession
from swissarmygrid.services.workspace import Conflict, Workspace

MAX_WAITING: int = 20
MAX_CODE_CHARS: int = 100_000
_IMPORT_LINE_MAX: int = 300


def check_imports(lines: Any) -> list[str]:
    """The import statements, checked and trimmed (none is fine)."""
    lines = lines or []
    if (
        not isinstance(lines, list)
        or len(lines) > 20
        or not all(isinstance(line, str) and 0 < len(line) <= _IMPORT_LINE_MAX for line in lines)
        or not all(line.lstrip().startswith(("import ", "from ")) for line in lines)
    ):
        raise ValueError("imports are up to 20 import statements, such as import duckdb.")
    return list(dict.fromkeys(line.strip() for line in lines))


@dataclass
class Handed:
    id: str
    label: str
    code: str
    imports: list[str]
    sent: float
    taken: bool = False

    def text(self) -> str:
        """The code as one cell: its imports first."""
        return "\n".join([*self.imports, "", self.code] if self.imports else [self.code])

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "code": self.text(),
            "sent": self.sent,
            "taken": self.taken,
        }


class Handoff:
    def __init__(self, ws: Workspace, verifier: CodeVerifier) -> None:
        self.ws = ws
        self._verifier = verifier
        self._waiting: list[Handed] = []

    def send(self, body: Any, who: str) -> dict[str, Any]:
        """{"code", "imports": [...], "label"}: queue code for `sag.paste()`."""
        if not isinstance(body, dict):
            raise ValueError('Send {"code": ..., "imports": [...], "label": ...}.')
        code, label = body.get("code"), body.get("label") or "code"
        if not isinstance(code, str) or not code.strip() or len(code) > MAX_CODE_CHARS:
            raise ValueError(f"code is 1 to {MAX_CODE_CHARS:,} characters.")
        if not isinstance(label, str) or len(label) > 120:
            raise ValueError("label is a short description, at most 120 characters.")
        handed = Handed(
            secrets.token_hex(4), label, code.strip("\n"), check_imports(body.get("imports")), time.time()
        )
        self._waiting = [*self._waiting, handed][-MAX_WAITING:]
        self.ws.audit.write(who, "to_python", label)
        return handed.to_json() | {"waiting": self.count()}

    def count(self) -> int:
        return sum(1 for h in self._waiting if not h.taken)

    def take(self) -> dict[str, Any] | None:
        """The newest code not yet pasted, now marked pasted; None when nothing waits."""
        for handed in reversed(self._waiting):
            if not handed.taken:
                handed.taken = True
                return handed.to_json()
        return None

    def recent(self) -> dict[str, Any]:
        """What was sent lately, newest first (to paste one again by hand)."""
        return {"sent": [h.to_json() for h in reversed(self._waiting)], "waiting": self.count()}

    async def grid_code(self, grid: Grid, info: DatasetInfo, who: str) -> dict[str, Any]:
        """The grid's steps as DuckDB code over the user's own table, checked against the grid, queued."""
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
                "These steps could not be checked as code, so they were not sent: "
                f"{result.get('difference') or result.get('note') or 'the copy did not match the grid'}"
            )
        sent = self.send(
            {"code": result["code"], "imports": ["import duckdb"], "label": f"{info.name}: its steps"}, who
        )
        return sent | {"variable": result.get("result_variable")}
