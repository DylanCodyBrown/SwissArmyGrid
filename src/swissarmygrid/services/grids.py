"""Grids: everything a grid page (or an agent, through MCP) does with one grid.

A grid is a dataset seen through a list of steps. The use cases here open and close grids, read rows
and counts in view order, change the steps (the whole list, or one change to one step), mark rows,
measure a selection, and save the steps as a recipe. Each takes the grid and its dataset, which the
door looks up first (`find`), so a missing grid is the first thing a caller hears about.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from swissarmygrid.domain.datasets import DatasetInfo, quote
from swissarmygrid.domain.grids import POS, Grid, combine_stats, selection_scope, stats_sql
from swissarmygrid.domain.steps import (
    Step,
    ViewSettings,
    categorize_conditions,
    describe_step,
    filter_condition,
    steps_from_json,
    with_header_sort,
)
from swissarmygrid.services.ports import AGENT, BACKGROUND, INTERACTIVE, Recipes, SqlSession
from swissarmygrid.services.workspace import NotFound, Workspace


def truncate(rows: list[list[Any]], limit: int) -> list[list[int]]:
    """Cut long text in place; return the [row, column] positions that were cut."""
    cut: list[list[int]] = []
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            if isinstance(value, str) and len(value) > limit:
                row[c] = value[:limit]
                cut.append([r, c])
    return cut


class GridService:
    def __init__(self, ws: Workspace, recipes: Recipes) -> None:
        self.ws = ws
        self._recipes = recipes

    def find(self, grid_id: str) -> tuple[Grid, DatasetInfo]:
        return self.ws.grid_and_info(grid_id)

    # ===== grid lifecycle =====

    def listing(self, who: str) -> dict[str, Any]:
        ws = self.ws
        return {
            "grids": [
                {
                    "grid": g.id,
                    "dataset": g.dataset,
                    "steps": len(g.steps),
                    "url": ws.addresses.grid(g, with_token=who != "agent"),
                }
                for g in ws.grids.all()
            ],
            "budget": ws.budget(),
        }

    async def create(self, dataset: Any, who: str) -> dict[str, Any]:
        """Open another grid on an open dataset."""
        ws = self.ws
        if not isinstance(dataset, str) or ws.store.get(dataset) is None:
            raise NotFound("Name an open dataset.")
        grid = ws.grids.create(dataset)
        ws.audit.write(who, "open_grid", dataset, grid=grid.id)
        await ws.live.broadcast({"type": "grid_shown", "grid": grid.id, "dataset": dataset})
        # Nothing shows it yet: VS Code opens the workspace, as for a push.
        shown = ws.live.shown_or_opening(grid.id) or await ws.live.ask_opener(ws.addresses.workspace(grid))
        return {
            "grid": grid.id,
            "grid_url": ws.addresses.grid(grid, with_token=who != "agent"),
            "workspace_url": ws.addresses.workspace(grid, with_token=who != "agent"),
            "shown": shown,
        }

    def described(self, grid: Grid, info: DatasetInfo, cursor: SqlSession) -> dict[str, Any]:
        """The grid in full: its dataset, columns, steps (each described in words) and settings."""
        compiled = self.ws.compiled(cursor, grid, info)
        error = compiled.error
        step_error: dict[str, Any] | None = None
        if error is not None:
            step_error = {"index": error.index, "message": error.message}
        steps = [s.to_json() | {"description": describe_step(s)} for s in grid.steps]
        return {
            "grid": grid.id,
            "dataset": info.name,
            "title": info.name,
            "version": info.version,
            "row_count": info.row_count,
            "source_kind": info.source_kind,
            "query": info.query,
            "source_columns": [{"name": c.name, "type": c.type} for c in info.columns],
            "columns": [{"name": c.name, "type": c.type} for c in compiled.columns],
            # Columns after each step (index 0 = the source), for previewing a step.
            "stage_columns": [[{"name": c.name, "type": c.type} for c in s.columns] for s in compiled.stages],
            "steps": steps,
            "step_error": step_error,
            "settings": grid.settings.to_json(),
            "gen": grid.gen,
            "budget": self.ws.budget(),
        }

    async def describe(self, grid: Grid, info: DatasetInfo) -> dict[str, Any]:
        return await self.ws.grid_query(grid, None, INTERACTIVE, lambda cur: self.described(grid, info, cur))

    async def close(self, grid_id: str) -> dict[str, Any] | None:
        """Close the grid (the dataset stays). None when there is no such grid."""
        grid = self.ws.grids.close(grid_id)
        if grid is None:
            return None
        await self.ws.live.broadcast({"type": "grid_closed", "grid": grid.id})
        return {"closed": grid.id}

    # ===== reading rows =====

    async def rows(
        self,
        grid: Grid,
        info: DatasetInfo,
        start: int,
        end: int,
        gen: int | None,
        upto: int | None,
        client: str,
    ) -> dict[str, Any]:
        """One window of the view, in view order, with the rows the highlights mark."""
        ws = self.ws
        if start < 0 or end < start:
            raise ValueError("Need 0 <= start <= end.")
        end = min(end, start + ws.settings.max_window_rows)

        def read(cur: SqlSession) -> tuple[Any, list[list[Any]]]:
            window = ws.views.window(cur, info, grid.steps, grid.settings, start, end, upto)
            marks: list[list[Any]] = []
            if grid.highlights and upto is None:
                compiled = ws.compiled(cur, grid, info)
                positioned, _ = ws.views.positioned_sql(cur, info, grid.steps, grid.settings)
                for rule in grid.highlights:
                    condition = filter_condition(compiled.stages[-1], rule, grid.settings)
                    hits = cur.execute(
                        f"SELECT {POS} FROM ({positioned}) WHERE {POS} >= ? AND {POS} < ? AND ({condition})",
                        [start, end],
                    ).fetchall()
                    label = str(rule.get("label", "highlight"))
                    marks += [[int(h[0]) - start, label] for h in hits]
            return window, marks

        window, marks = await ws.grid_query(grid, gen, INTERACTIVE, read, client)
        cut = truncate(window.rows, ws.settings.max_cell_chars)
        return {
            "rows": window.rows,
            "row_count": window.row_count,
            "columns": [c.name for c in window.columns],
            "truncated": cut,
            "highlighted": marks,
            "version": info.version,
            "gen": gen,
        }

    async def count(
        self, grid: Grid, info: DatasetInfo, gen: int | None, upto: int | None, client: str
    ) -> dict[str, Any]:
        ws = self.ws
        n = await ws.grid_query(
            grid,
            gen,
            INTERACTIVE,
            lambda cur: ws.views.row_count(cur, info, grid.steps, grid.settings, upto),
            client,
        )
        return {"row_count": n, "total": info.row_count}

    async def cell(
        self, grid: Grid, info: DatasetInfo, row: int | None, column: str | None
    ) -> dict[str, Any]:
        """One cell's full value (the grid cuts long text)."""
        ws = self.ws
        if row is None or row < 0 or not column:
            raise ValueError("Give row and column.")
        value = await ws.grid_query(
            grid,
            None,
            INTERACTIVE,
            lambda cur: ws.views.cell(cur, info, grid.steps, grid.settings, row, column),
        )
        return {"row": row, "column": column, "value": value}

    async def step_counts(
        self, grid: Grid, info: DatasetInfo, gen: int | None, client: str
    ) -> dict[str, Any]:
        ws = self.ws
        counts = await ws.grid_query(
            grid,
            gen,
            BACKGROUND,
            lambda cur: ws.views.step_counts(cur, info, grid.steps, grid.settings),
            client,
        )
        return {"counts": counts}

    async def categorize_stats(self, grid: Grid, info: DatasetInfo, index: int | None) -> dict[str, Any]:
        """Unmatched and multiply-matched rows for a categorize step, measured on its input."""
        ws = self.ws
        if index is None or not 0 <= index < len(grid.steps):
            raise ValueError("step must be the index of a categorize step.")
        step = grid.steps[index]
        if step.kind != "categorize":
            raise ValueError("That step is not a categorize step.")

        def compute(cur: SqlSession) -> dict[str, Any]:
            compiled = ws.compiled(cur, grid, info)
            stage = compiled.stage_at(index)
            column, conditions = categorize_conditions(stage, step.params, grid.settings)
            hits = " + ".join(f"CAST({c} AS INTEGER)" for c, _ in conditions)
            with_clause = ", ".join(f"{n} AS ({b})" for n, b in compiled.ctes)
            row = cur.execute(
                f"WITH {with_clause} SELECT "
                f"count(*) FILTER ({quote(column.name)} IS NOT NULL AND ({hits}) = 0), "
                f"count(*) FILTER (({hits}) > 1) FROM {stage.name}"
            ).fetchone()
            return {"unmatched": row[0] if row else 0, "overlapping": row[1] if row else 0}

        return await ws.grid_query(grid, None, BACKGROUND, compute)

    # ===== changing the view =====

    async def commit_steps(
        self,
        grid: Grid,
        info: DatasetInfo,
        new_steps: list[Step],
        new_settings: ViewSettings,
        who: str,
        gen: Any = None,
        client: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make `new_steps` the grid's steps: the one path for the whole list and for one change.
        Returns the grid described for the caller."""
        ws = self.ws
        if isinstance(gen, int):  # newer gen from this page: interrupt its older queries
            grid.release(grid.admit(gen, ws.store.cursor(), client))
        grid.steps, grid.settings = new_steps, new_settings
        described = await self.describe(grid, info)
        await ws.live.broadcast({"type": "grid_updated", "grid": grid.id, "by": who, "gen": grid.gen})
        ws.audit.write(who, "steps", info.name, steps=len(new_steps))
        return described | (extra or {})

    async def put_steps(self, grid: Grid, info: DatasetInfo, body: Any, who: str) -> dict[str, Any]:
        """The whole list: {"steps": [...], "settings": {...}}, either part optional."""
        if not isinstance(body, dict):
            raise ValueError('Send {"steps": [...], "settings": {...}}.')
        new_steps = steps_from_json(body["steps"]) if "steps" in body else list(grid.steps)
        new_settings = ViewSettings.from_json(body["settings"]) if "settings" in body else grid.settings
        client = str(body.get("client", ""))[:40]
        return await self.commit_steps(grid, info, new_steps, new_settings, who, body.get("gen"), client)

    async def step_op(self, grid: Grid, info: DatasetInfo, body: Any, who: str) -> dict[str, Any]:
        """One change to one step, by its id or index, so an agent never needs the step values back:
        {"op": "add", "step": {...}, "index"?} | {"op": "edit", "params": {...}} |
        {"op": "delete"} | {"op": "toggle", "enabled"?} | {"op": "move", "to": n},
        each but add naming the step with "id" or "index"."""
        if not isinstance(body, dict):
            raise ValueError('Send {"op": "add" | "edit" | "delete" | "toggle" | "move", ...}.')
        steps = list(grid.steps)

        def whole(name: str) -> int | None:
            value = body.get(name)
            if value is None:
                return None
            if type(value) is not int:
                raise ValueError(f"{name} must be a whole number.")
            return value

        def position() -> int:
            step_id = body.get("id")
            if step_id is not None:
                found = next((i for i, s in enumerate(steps) if s.id == step_id), None)
                if found is None:
                    raise LookupError(
                        f"No step with id {str(step_id)[:40]!r} in this grid; tabs_get lists them."
                    )
                return found
            index = whole("index")
            if index is None:
                raise ValueError("Name the step with its index (from 0) or its id.")
            if not 0 <= index < len(steps):
                raise ValueError(
                    f"index {index} is out of range: the grid has {len(steps)} step(s), numbered from 0."
                )
            return index

        op = body.get("op")
        if op == "add":
            raw = body.get("step")
            if not isinstance(raw, dict):
                raise ValueError('add needs "step": {"kind": ..., "params": {...}}.')
            new = Step.from_json({k: v for k, v in raw.items() if k != "id"}, len(steps))
            index = whole("index")
            if index is None:  # before a header-click sort, which stays the last word
                at = next((i for i, s in enumerate(steps) if s.params.get("header")), len(steps))
            else:
                at = max(0, min(index, len(steps)))
            steps.insert(at, new)
        elif op == "edit":
            at = position()
            params = body.get("params")
            if not isinstance(params, dict):
                raise ValueError('edit needs "params": the step\'s full new params, an object.')
            steps[at] = replace(steps[at], params=params)
        elif op == "delete":
            at = position()
            del steps[at]
        elif op == "toggle":
            at = position()
            enabled = body.get("enabled")
            if enabled is not None and not isinstance(enabled, bool):
                raise ValueError("enabled must be true or false.")
            steps[at] = replace(steps[at], enabled=(not steps[at].enabled) if enabled is None else enabled)
        elif op == "move":
            start = position()
            to = whole("to")
            if to is None:
                raise ValueError('move needs "to": the new index.')
            moved = steps.pop(start)
            at = max(0, min(to, len(steps)))
            steps.insert(at, moved)
        else:
            raise ValueError("op must be add, edit, delete, toggle or move.")
        return await self.commit_steps(grid, info, steps, grid.settings, who, extra={"index": at})

    async def header_sort(self, grid: Grid, info: DatasetInfo, body: Any, who: str) -> dict[str, Any]:
        """A click on a column header: {"keys": [["column", "asc"|"desc"], ...]}."""
        ws = self.ws
        keys = body.get("keys") if isinstance(body, dict) else None
        if not isinstance(keys, list):
            raise ValueError('Send {"keys": [["column", "asc"|"desc"], ...]}.')
        gen = body.get("gen")
        client = str(body.get("client", ""))[:40]
        if isinstance(gen, int):
            grid.release(grid.admit(gen, ws.store.cursor(), client))
        grid.steps = with_header_sort(grid.steps, keys)
        described = await self.describe(grid, info)
        await ws.live.broadcast({"type": "grid_updated", "grid": grid.id, "by": who, "gen": grid.gen})
        return described

    # ===== the selection, highlights =====

    async def selection_stats(
        self, grid: Grid, info: DatasetInfo, raw: Any, gen: int | None, client: str
    ) -> dict[str, Any]:
        """Excel's status bar for any selection, loaded rows or not: Count, Sum, Average."""
        ws = self.ws

        def compute(cur: SqlSession) -> dict[str, Any]:
            positioned, columns = ws.views.positioned_sql(cur, info, grid.steps, grid.settings)
            where, chosen = selection_scope(raw, columns)
            sql, _ = stats_sql(positioned, where, chosen)
            row = cur.execute(sql).fetchone()
            return combine_stats(tuple(row) if row else (0,), chosen)

        stats = await ws.grid_query(grid, gen, INTERACTIVE, compute, client)
        grid.selected = int(stats["rows"])
        return stats

    def cancel(self, grid: Grid) -> dict[str, Any]:
        """The SQL box's Cancel: stop every query running for this grid."""
        return {"interrupted": grid.interrupt_all()}

    async def set_highlights(self, grid: Grid, info: DatasetInfo, body: Any, who: str) -> dict[str, Any]:
        """Mark rows matching conditions (same shape as filter params, plus a label)."""
        ws = self.ws
        rules = body.get("highlights") if isinstance(body, dict) else None
        if not isinstance(rules, list):
            raise ValueError('Send {"highlights": [{"column": ..., "op": ..., "label": ...}]}.')

        def check(cur: SqlSession) -> None:
            compiled = ws.compiled(cur, grid, info)
            for rule in rules:
                if not isinstance(rule, dict):
                    raise ValueError("Each highlight is an object.")
                filter_condition(compiled.stages[-1], rule, grid.settings)

        await ws.workers.run(AGENT, lambda: check(ws.store.cursor()))
        grid.highlights = rules
        await ws.live.broadcast({"type": "grid_updated", "grid": grid.id, "by": who, "gen": grid.gen})
        ws.audit.write(who, "highlights", info.name, count=len(rules))
        return {"grid": grid.id, "highlights": len(rules)}

    def set_selection(self, grid: Grid, body: Any) -> dict[str, Any]:
        """The page reports how many rows are selected, for the agent's view of the grid."""
        grid.selected = int(body.get("count", 0)) if isinstance(body, dict) else 0
        return {"selected": grid.selected}

    # ===== recipes =====

    def save_recipe(self, grid: Grid, info: DatasetInfo, who: str) -> dict[str, Any]:
        path = self._recipes.save(info.name, grid.steps, grid.settings)
        self.ws.audit.write(who, "save_recipe", info.name)
        return {"saved": path}

    def delete_recipe(self, info: DatasetInfo) -> dict[str, Any]:
        return {"deleted": self._recipes.delete(info.name)}
