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

from swissarmygrid.charts.spec import ChartError, aggregate, altair_code, validate, vega_lite
from swissarmygrid.engine.sql_guard import guard
from swissarmygrid.engine.steps import Step, type_family
from swissarmygrid.engine.workers import BACKGROUND
from swissarmygrid.server.context import Context, actor, error_response, json_response, read_json
from swissarmygrid.server.grids import Grid
from swissarmygrid.server.routes_agent import Policy, agent_may_see_raw_values
from swissarmygrid.server.security import ENGINE_ERROR_HIDDEN, ENGINE_ERRORS_SHOWN_FROM_LEVEL

_TABLE_CARD_ROWS: int = 100
# A diagram is Mermaid text (an ERD from GET /relationships/erd, or any flowchart), drawn by the page.
_KINDS: tuple[str, ...] = ("chart", "note", "table", "diagram")


def routes(ctx: Context, policy: Policy) -> list[BaseRoute]:
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
                guarded = guard(card["sql"], {i.name for i in ctx.store.all()}, compiled.sql)
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

    # Cards carry rendered cells (a table card's rows, a chart's data points). The agent below
    # level 3 gets only what describes the card, so listing cards cannot read the data around the
    # privacy gate; the card still draws in full for the user.
    _CARD_META = (
        "id",
        "grid",
        "dataset",
        "kind",
        "title",
        "pinned",
        "created_by",
        "created",
        "error",
        "note",
        "points",
    )

    def for_caller(request: Request, shown: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if agent_may_see_raw_values(request, policy):
            return shown
        described = [{k: v for k, v in c.items() if k in _CARD_META} for c in shown]
        if policy.level < ENGINE_ERRORS_SHOWN_FROM_LEVEL:
            # Why a card could not draw is often DuckDB's message, which can quote a cell value.
            for card in described:
                if card.get("error"):
                    card["error"] = ENGINE_ERROR_HIDDEN
        return described

    async def list_cards(request: Request) -> Response:
        try:
            grid, _ = ctx.grid_and_info(request.path_params["grid_id"])
            mine = [c for c in cards.values() if c["grid"] == grid.id]
            mine.sort(key=lambda c: (not c["pinned"], c["created"]))

            def compute(cur: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
                return [render(c, cur) for c in mine]

            return json_response(
                {"cards": for_caller(request, await ctx.grid_query(grid, None, BACKGROUND, compute))}
            )
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
            return json_response({"cards": for_caller(request, shown)})
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
        """Cross-filtering: clicking a bar adds `x = value` (or the time bucket's range)."""
        try:
            card = cards.get(request.path_params["card_id"])
            if card is None or card["kind"] != "chart":
                return Response("No such chart.", status_code=404)
            grid, info = ctx.grid_and_info(card["grid"])
            body = await read_json(request)
            value = body.get("value") if isinstance(body, dict) else None
            x = card["spec"]["x"]
            field = x["field"]

            def column_type(cur: duckdb.DuckDBPyConnection) -> str:
                columns = ctx.compiled(cur, grid, info).columns
                return next(c.type for c in columns if c.name == field)

            family = type_family(await ctx.grid_query(grid, None, BACKGROUND, column_type))
            if value is None:
                params: dict[str, Any] = {"column": field, "op": "blank"}
            elif "time_unit" in x:
                params = {
                    "column": field,
                    "op": "date_between",
                    "low": str(value)[:10],
                    "high": _bucket_end(str(value)[:10], x["time_unit"]),
                }
            elif family == "number" and card["spec"]["mark"] == "histogram":
                raise ValueError("Select a histogram range from the column profile instead.")
            else:
                params = {"column": field, "op": "eq", "value": value}
            grid.steps = [
                *grid.steps,
                Step(kind="filter", params=params | {"from_chart": card["id"]}),
            ]
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
                return altair_code(validate(card["spec"], columns), columns, info.name)

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
