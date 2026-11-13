"""Side-panel cards: charts, notes and small result tables (plan section 11).

  GET    /cards                            every card of every open grid: the one Analysis Sandbox
  GET    /grids/{id}/cards                 every card for the grid, charts computed on its view
  POST   /grids/{id}/cards                 {"kind": "chart"|"note"|"table"|"diagram", "title",
                                            "spec"|"body"|"sql"}
  PATCH  /cards/{card}                     change title, spec, body, sql or pinned
  DELETE /cards/{card}                     remove a card
  POST   /cards/{card}/select              {"value": ...} cross-filter: add a filter step for it
  GET    /cards/{card}/code                Altair code for a chart card

A card records its source (dataset, version, steps) and redraws whenever its grid's view
changes, because charts are computed on request from the grid's current steps. Agent-made
cards are tagged so the page can mark them.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import duckdb
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.domain.charts import ChartError, altair_code, validate, vega_lite
from swissarmygrid.domain.grids import Grid
from swissarmygrid.domain.sql_guard import guard
from swissarmygrid.domain.steps import Step, type_family
from swissarmygrid.server.context import Context, actor, error_response, json_response, read_json
from swissarmygrid.services.charts import aggregate
from swissarmygrid.services.ports import BACKGROUND

_TABLE_CARD_ROWS: int = 100
# A diagram is Mermaid text (an ERD from GET /relationships/erd, or any flowchart), drawn by the page.
_KINDS: tuple[str, ...] = ("chart", "note", "table", "diagram")


def routes(ctx: Context) -> list[BaseRoute]:
    cards: dict[str, dict[str, Any]] = {}

    def check_card(body: Any, grid: Grid) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ValueError("Send a card object.")
        kind = body.get("kind")
        if kind not in _KINDS:
            raise ValueError(f"kind must be one of {', '.join(_KINDS)}.")
        title = body.get("title", "")
        if not isinstance(title, str) or len(title) > 200:
            raise ValueError("title must be text, at most 200 characters.")
        out: dict[str, Any] = {"kind": kind, "title": title}
        if kind == "note":
            text = body.get("body", "")
            if not isinstance(text, str) or len(text) > 20_000:
                raise ValueError("A note's body is markdown text, at most 20,000 characters.")
            out["body"] = text
        elif kind == "diagram":
            text = body.get("body", "")
            if not isinstance(text, str) or not text.strip() or len(text) > 50_000:
                raise ValueError("A diagram's body is Mermaid text, at most 50,000 characters.")
            out["body"] = text
        elif kind == "table":
            sql = body.get("sql")
            if not isinstance(sql, str):
                raise ValueError("A table card needs sql.")
            out["sql"] = sql
        else:
            out["spec"] = body.get("spec")
        return out

    def render(card: dict[str, Any], cur: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        grid, info = ctx.grid_and_info(card["grid"])
        compiled = ctx.compiled(cur, grid, info)
        shown = dict(card) | {
            "source": {"dataset": info.name, "version": info.version, "steps": len(grid.steps)}
        }
        try:
            if card["kind"] == "chart":
                spec = validate(card["spec"], compiled.columns)
                data = aggregate(cur, compiled.sql(), spec, ctx.settings.max_chart_points)
                shown["vega_lite"] = vega_lite(spec, data, compiled.columns)
                shown["note"] = data["note"]
                shown["points"] = len(data["values"])
            elif card["kind"] == "table":
                guarded = guard(
                    card["sql"], {i.name for i in ctx.store.all()}, compiled.sql, grammar=ctx.grammar
                )
                if guarded.kind != "select":
                    raise ValueError("A table card runs a SELECT.")
                result = cur.execute(guarded.sql)
                shown["columns"] = [d[0] for d in result.description or []]
                fetched = result.fetchmany(_TABLE_CARD_ROWS + 1)
                shown["rows"] = [list(r) for r in fetched[:_TABLE_CARD_ROWS]]
                shown["capped"] = len(fetched) > _TABLE_CARD_ROWS
        except (ChartError, ValueError, duckdb.Error) as error:
            shown["error"] = str(error)
        return shown

    async def list_cards(request: Request) -> Response:
        try:
            grid, _ = ctx.grid_and_info(request.path_params["grid_id"])
            mine = [c for c in cards.values() if c["grid"] == grid.id]
            mine.sort(key=lambda c: (not c["pinned"], c["created"]))

            def compute(cur: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
                return [render(c, cur) for c in mine]

            return json_response({"cards": await ctx.grid_query(grid, None, BACKGROUND, compute)})
        except Exception as error:
            return error_response(error)

    async def all_cards(request: Request) -> Response:
        """The Analysis Sandbox: one list for every table. Each card is still drawn on its own
        grid's view; a card whose grid was closed drops out."""
        try:
            shown: list[dict[str, Any]] = []
            for grid_id in dict.fromkeys(c["grid"] for c in cards.values()):
                grid = ctx.grids.get(grid_id)
                if grid is None:
                    continue
                mine = [c for c in cards.values() if c["grid"] == grid_id]

                def compute(
                    cur: duckdb.DuckDBPyConnection, mine: list[dict[str, Any]] = mine
                ) -> list[dict[str, Any]]:
                    return [render(c, cur) for c in mine]

                shown += await ctx.grid_query(grid, None, BACKGROUND, compute)
            shown.sort(key=lambda c: (not c["pinned"], c["created"]))
            return json_response({"cards": shown})
        except Exception as error:
            return error_response(error)

    async def add_card(request: Request) -> Response:
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            card = check_card(await read_json(request), grid)
            if card["kind"] == "chart":

                def check(cur: duckdb.DuckDBPyConnection) -> None:
                    validate(card["spec"], ctx.compiled(cur, grid, info).columns)

                await ctx.grid_query(grid, None, BACKGROUND, check)
            card |= {
                "id": secrets.token_hex(4),
                "grid": grid.id,
                "dataset": info.name,
                "pinned": False,
                "created_by": actor(request),
                "created": time.time(),
            }
            cards[card["id"]] = card
            await ctx.live.broadcast({"type": "cards_updated", "grid": grid.id})
            ctx.audit.write(actor(request), "add_card", info.name, kind=card["kind"])
            return json_response({"card": card["id"]})
        except ChartError as error:
            return Response(str(error), status_code=400)
        except Exception as error:
            return error_response(error)

    async def update_card(request: Request) -> Response:
        try:
            card = cards.get(request.path_params["card_id"])
            if card is None:
                return Response("No such card.", status_code=404)
            body = await read_json(request)
            if not isinstance(body, dict):
                raise ValueError("Send the fields to change.")
            grid, info = ctx.grid_and_info(card["grid"])
            merged = check_card({**card, **{k: v for k, v in body.items() if k != "pinned"}}, grid)
            if merged["kind"] == "chart":

                def check(cur: duckdb.DuckDBPyConnection) -> None:
                    validate(merged["spec"], ctx.compiled(cur, grid, info).columns)

                await ctx.grid_query(grid, None, BACKGROUND, check)
            card.update(merged)
            if "pinned" in body:
                card["pinned"] = bool(body["pinned"])
            await ctx.live.broadcast({"type": "cards_updated", "grid": card["grid"]})
            ctx.audit.write(actor(request), "update_card", card["dataset"])
            return json_response({"card": card["id"]})
        except ChartError as error:
            return Response(str(error), status_code=400)
        except Exception as error:
            return error_response(error)

    async def delete_card(request: Request) -> Response:
        card = cards.pop(request.path_params["card_id"], None)
        if card is None:
            return Response("No such card.", status_code=404)
        await ctx.live.broadcast({"type": "cards_updated", "grid": card["grid"]})
        ctx.audit.write(actor(request), "remove_card", card["dataset"])
        return json_response({"removed": card["id"]})

    async def select(request: Request) -> Response:
        """Cross-filtering: clicking a bar adds `x = value` (or the time bucket's range); a heatmap
        cell adds its row's value too ({"value": x, "y": y}). The new steps must compile on the
        grid's view, or none is added (a value its column cannot hold used to break the grid)."""
        try:
            card = cards.get(request.path_params["card_id"])
            if card is None or card["kind"] != "chart":
                return Response("No such chart.", status_code=404)
            grid, info = ctx.grid_and_info(card["grid"])
            body = await read_json(request)
            if not isinstance(body, dict):
                raise ValueError('Send {"value": ...}.')
            spec = card["spec"]
            if spec["mark"] == "histogram":
                raise ValueError("Select a histogram range from the column profile instead.")

            def added(cur: duckdb.DuckDBPyConnection) -> list[Step]:
                columns = ctx.compiled(cur, grid, info).columns
                validate(spec, columns)  # its columns may have left the view since
                families = {c.name: type_family(c.type) for c in columns}
                x = spec["x"]
                clicked: list[tuple[str, Any, str | None]] = [
                    (x["field"], body.get("value"), x.get("time_unit"))
                ]
                if spec["mark"] == "heatmap" and "y" in body:
                    clicked.append((spec["y"]["field"], body["y"], None))
                picked = [_click_filter(f, families[f], v, unit) for f, v, unit in clicked]
                steps = [Step(kind="filter", params=p | {"from_chart": card["id"]}) for p in picked]
                trial = ctx.views.compile(cur, info, [*grid.steps, *steps], grid.settings)
                if trial.error is not None:
                    raise ValueError(str(trial.error))
                return steps

            grid.steps = [*grid.steps, *await ctx.grid_query(grid, None, BACKGROUND, added)]
            await ctx.live.broadcast(
                {"type": "grid_updated", "grid": grid.id, "by": actor(request), "gen": grid.gen}
            )
            return json_response({"steps": len(grid.steps)})
        except Exception as error:
            return error_response(error)

    async def code(request: Request) -> Response:
        try:
            card = cards.get(request.path_params["card_id"])
            if card is None or card["kind"] != "chart":
                return Response("No such chart.", status_code=404)
            grid, info = ctx.grid_and_info(card["grid"])

            def make(cur: duckdb.DuckDBPyConnection) -> str:
                columns = ctx.compiled(cur, grid, info).columns
                spec = validate(card["spec"], columns)
                return altair_code(spec, columns, info.name, ctx.settings.max_chart_points)

            return json_response({"code": await ctx.grid_query(grid, None, BACKGROUND, make)})
        except Exception as error:
            return error_response(error)

    return [
        Route("/cards", all_cards),
        Route("/grids/{grid_id}/cards", list_cards),
        Route("/grids/{grid_id}/cards", add_card, methods=["POST"]),
        Route("/cards/{card_id}", update_card, methods=["PATCH"]),
        Route("/cards/{card_id}", delete_card, methods=["DELETE"]),
        Route("/cards/{card_id}/select", select, methods=["POST"]),
        Route("/cards/{card_id}/code", code),
    ]


def _click_filter(field: str, family: str, value: Any, time_unit: str | None) -> dict[str, Any]:
    """The filter for one clicked value: a blank, a whole time bucket, or the value itself."""
    if value is None:
        return {"column": field, "op": "blank"}
    if time_unit is None:
        return {"column": field, "op": "eq", "value": _as_family(value, family)}
    if not isinstance(value, str):
        raise ValueError(f"A {time_unit} of {field} is picked by its first day, as YYYY-MM-DD.")
    start = value[:10]
    end = _bucket_end(start, time_unit)
    if family == "timestamp":
        end += "T23:59:59.999999"  # the whole last day, not just its midnight
    return {"column": field, "op": "date_between", "low": start, "high": end}


def _as_family(value: Any, family: str) -> Any:
    """A value an agent gave as text (`sag sandbox_select --value 3`) as its column's type; any
    other value as it came (the filter step says what is wrong with it)."""
    if not isinstance(value, str):
        return value
    if family == "boolean" and value.lower() in ("true", "false"):
        return value.lower() == "true"
    if family == "number":
        try:
            number = float(value)
        except ValueError:
            return value
        return int(value) if value.strip().lstrip("+-").isdigit() else number
    return value


def _bucket_end(start: str, unit: str) -> str:
    """The last day of the time bucket starting at `start` (inclusive range for date_between)."""
    from datetime import date, timedelta

    day = date.fromisoformat(start)
    if unit == "day":
        end = day
    elif unit == "week":
        end = day + timedelta(days=6)
    else:
        months = {"month": 1, "quarter": 3, "year": 12}[unit]
        year, month = divmod(day.month - 1 + months, 12)
        end = date(day.year + year, month + 1, 1) - timedelta(days=1)
    return end.isoformat()
