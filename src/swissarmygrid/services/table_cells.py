"""Every table has a read-only cell (docs/ideas.md, 2026-11-14; rules in domain/table_cells.py).

Two use cases, for the Workbench window and the agent alike:

  listing   every open table as its cell's text: the grid's steps as one DuckDB query whose result is
            the table's own name, and (for a table no code made) the load that goes before it
  arrange   a document's cells with its Tables group as it should be: a notebook's cells, or a
            script's text with its region; each of the user's cells with its uses line

Where each table came from is remembered here (`Origins`): the Workbench kernel that pushed it, the
file it was read from, the connection and query. Memory only; nothing is written.
"""

from __future__ import annotations

from typing import Any

from swissarmygrid.domain import notebooks
from swissarmygrid.domain import table_cells as rules
from swissarmygrid.domain.codegen import variable_name, view_query
from swissarmygrid.domain.datasets import DatasetInfo, quote
from swissarmygrid.domain.grids import Grid
from swissarmygrid.domain.table_cells import Origin, PlanCell, TableSpec
from swissarmygrid.services.ports import BACKGROUND, SqlSession
from swissarmygrid.services.workspace import Workspace

MAX_SCRIPT_CHARS: int = notebooks.MAX_CELL_CHARS * 5


class Origins:
    """Where each open table came from. Used from the event loop only."""

    def __init__(self) -> None:
        self._known: dict[str, Origin] = {}

    def note(self, name: str, origin: Origin) -> None:
        self._known[name] = origin

    def get(self, name: str) -> Origin:
        return self._known.get(name) or Origin()

    def rename(self, old: str, new: str) -> None:
        if old in self._known:
            self._known[new] = self._known.pop(old)

    def forget(self, name: str) -> None:
        self._known.pop(name, None)


class TableCells:
    def __init__(self, ws: Workspace, origins: Origins) -> None:
        self.ws = ws
        self._origins = origins

    # ===== every open table as its cell's text =====

    def _table(self, cursor: SqlSession, info: DatasetInfo, grid: Grid | None) -> dict[str, Any]:
        ws = self.ws
        origin = self._origins.get(info.name)
        # A sort made by clicking a header is a way of looking, not a transformation: it stays out.
        steps = [s for s in (grid.steps if grid is not None else []) if not s.params.get("header")]
        on = [s for s in steps if s.enabled]
        problem: str | None = None
        query: str | None = None
        if grid is not None and on:
            compiled = ws.views.compile(cursor, info, steps, grid.settings)
            if compiled.error is not None:
                problem = f"Step {(compiled.error.index or 0) + 1}: {compiled.error.message}"
            else:
                query = view_query(info, compiled, steps, grid.settings, ws.grammar.keywords)
        if problem is not None:
            change: tuple[list[str], str] = (
                [],
                f"# {variable_name(info.name)}: a step in the grid cannot run ({problem}).\n"
                "# Fix it in the grid, and this cell follows.",
            )
        else:
            change = rules.transformations_code(info.name, info.source_kind, query)
        pasted: str | None = None
        if origin.kind == "paste" and info.row_count <= rules.PASTE_MAX_ROWS:
            names = ", ".join(quote(c.name) for c in info.columns)
            cursor.execute(f"SELECT {names} FROM {info.table}")  # noqa: S608 (quoted names of an open table)
            pasted = rules.pasted_query(info.columns, [list(row) for row in cursor.fetchall()])
        load = rules.load_code(info.name, origin, info.source_kind, pasted, info.row_count)
        return {
            "name": info.name,
            "variable": variable_name(info.name),
            "grid": grid.id if grid is not None else None,
            "source_kind": info.source_kind,
            "origin": origin.kind,
            "kernel": origin.kernel,  # the Workbench kernel that sent it (None: no kernel did)
            "steps": len(on),
            "step_list": [s.to_json() for s in steps],
            "problem": problem,
            "code": rules.cell_text([change]),
            "loaded": rules.cell_text([load, change]),
        }

    async def listing(self, names: list[str] | None = None) -> dict[str, Any]:
        """Every open table (or the ones named) as its cell's text."""
        ws = self.ws
        wanted = [i for i in ws.store.all() if names is None or i.name in names]
        pairs = [(info, ws.grids.first_for(info.name)) for info in wanted]

        def read() -> list[dict[str, Any]]:
            cursor = ws.store.cursor()
            return [self._table(cursor, info, grid) for info, grid in pairs]

        return {"tables": await ws.workers.run(BACKGROUND, read) if pairs else []}

    # ===== a document's Tables group =====

    async def arrange(self, body: Any) -> dict[str, Any]:
        """A notebook's cells or a script's text with its Tables group as it should be.

        {"kind": "notebook", "cells": [...], "tables": [names], "next_cell": n, "move": true}
        {"kind": "script", "text": "...", "tables": [names], "move": true}

        `tables` are the open tables that belong to the document. `move` false: nothing is added or
        moved (the user is typing); the tables' cells follow the grid and the uses lines are worked out.
        """
        if not isinstance(body, dict) or body.get("kind") not in ("notebook", "script"):
            raise ValueError(
                'Send {"kind": "notebook", "cells": [...]} or {"kind": "script", "text": "..."}.'
            )
        names = body.get("tables") or []
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names) or len(names) > 500:
            raise ValueError("tables is a list of table names.")
        move = body.get("move") is not False
        listed = (await self.listing(names))["tables"] if names else []
        by_name = {t["name"]: t for t in listed}
        specs = [
            TableSpec(t["name"], t["code"], t["loaded"], t["steps"], tuple(t["step_list"]))
            for t in (by_name[n] for n in names if n in by_name)
        ]
        if body["kind"] == "script":
            text = body.get("text")
            if not isinstance(text, str) or len(text) > MAX_SCRIPT_CHARS:
                raise ValueError("Send the script's text (at most five million characters).")
            new_text, arrangement = rules.arrange_script(text, specs, move)
            return {
                "text": new_text,
                "tables": [line.to_json() for line in arrangement.tables],
                "outline": rules.outline(new_text),
            }
        cells = notebooks.read_cells(body.get("cells"))
        following = body.get("next_cell")
        plan = [PlanCell(c.number, c.kind, c.source, c.role, c.table, c.grouped) for c in cells]
        arrangement = rules.arrange(
            plan, specs, following if isinstance(following, int) and following > 0 else 1, move
        )
        steps = {spec.name: list(spec.step_list) for spec in specs}
        return {
            "cells": [
                c.to_json()
                | ({"steps": steps[c.table]} if c.role == "transformations" and c.table in steps else {})
                for c in arrangement.cells
            ],
            "tables": [line.to_json() for line in arrangement.tables],
            "next_cell": arrangement.next_number,
        }
