"""Charts and the side panel's cards (plan section 11): charts, notes, small result tables and
diagrams, kept per grid and drawn on its current view.

The specification itself (what a chart may say, its query text, Vega-Lite) is the domain's
(domain/charts.py); here a chart's one aggregate query is run on the grid's view through the
`SqlSession` port, and the cards are kept: added, changed, removed, clicked (cross-filtering).

A card records its source (dataset, version, steps) and redraws whenever its grid's view changes,
because charts are computed on request from the grid's current steps. Agent-made cards are tagged
so the page can mark them.

A card may also belong to no table (`add_free`): a note, a figure, a picture or a table of rows sent
from Python (`sag.sandbox`), the agent or the Sandbox's own Add note. It stays when grids close. A
`key` names a card, so sending the same key again replaces that card's content in its place.

Saved dashboards (the `Dashboards` port): Save freezes every card as drawn, a chart's points and a
query table's rows included; Open replaces the Sandbox with a version. A chart or query card relinks to
an open grid on its table (any time one is open again); until then it shows what it showed when saved.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from swissarmygrid.domain import themes
from swissarmygrid.domain.charts import _grouped, altair_code, fill_figure, validate, vega_lite
from swissarmygrid.domain.datasets import DatasetInfo, quote
from swissarmygrid.domain.grids import Grid
from swissarmygrid.domain.sql_guard import guard
from swissarmygrid.domain.steps import Step, type_family
from swissarmygrid.services.ports import BACKGROUND, Dashboards, SqlSession, ThemeShelf
from swissarmygrid.services.workspace import NotFound, Refused, Workspace

_TABLE_CARD_ROWS: int = 100
_TABLE_CARD_COLUMNS: int = 50
_CELL_CHARACTERS: int = 1_000
_ORIGINS: dict[str, str] = {"python": "Python", "agent": "Agent", "user": "Sandbox"}
# A diagram is Mermaid text (an ERD from GET /relationships/erd, or any flowchart), drawn by the page.
# Graphviz is DOT text; Plotly and ECharts are a figure as JSON text; an image is a data: URL. The page
# draws each (web/src/lazy/), so the server only checks that the text is there and not too big.
# A vegalite card is a whole Vega-Lite spec with its data inline (an Altair chart's to_dict()): the user's
# own Python's or the page's, never the agent's (Vega compiles its expressions; the agent has `chart`).
_KINDS: tuple[str, ...] = (
    "chart",
    "note",
    "table",
    "diagram",
    "graphviz",
    "plotly",
    "echarts",
    "vegalite",
    "image",
)
_BODY_LIMITS: dict[str, int] = {
    "note": 20_000,
    "diagram": 50_000,
    "graphviz": 100_000,
    "plotly": 2_000_000,
    "echarts": 2_000_000,
    "vegalite": 2_000_000,
    "image": 8_000_000,
}
_BODY_WORDS: dict[str, str] = {
    "note": "markdown text",
    "diagram": "Mermaid text",
    "graphviz": "Graphviz (DOT) text",
    "plotly": 'a Plotly figure as JSON text ({"data": [...], "layout": {...}})',
    "echarts": "an ECharts option as JSON text",
    "vegalite": "a Vega-Lite spec as JSON text, its data inline",
    "image": "a data: URL of a PNG, JPEG, GIF, WebP or SVG picture",
}
Card = dict[str, Any]


def aggregate(cursor: SqlSession, view_sql: str, spec: dict[str, Any], max_points: int) -> dict[str, Any]:
    """Run the chart's one aggregate query. Returns {"values": [...], "note": str | None}."""
    v = f"({view_sql}) AS v"
    mark = spec["mark"]
    x = spec["x"]
    xf = quote(x["field"])
    note: str | None = None

    if mark == "histogram":
        bins = x.get("bins", 20)
        # NaN and infinities have no bin (their width would be NaN, which no BIGINT holds).
        where = f"{xf} IS NOT NULL AND isfinite(CAST({xf} AS DOUBLE))"
        bounds: Any = cursor.execute(f"SELECT min({xf}), max({xf}) FROM {v} WHERE {where}").fetchone()
        low, high = bounds or (None, None)
        if low is None or high is None:
            return {"values": [], "note": "No values to chart."}
        low_f, high_f = float(low), float(high)
        # Divided before subtracting, so -1e308 to 1e308 does not overflow to infinity.
        width = high_f / bins - low_f / bins if high_f > low_f else 1.0
        position = f"(CAST({xf} AS DOUBLE) / {width!r} - {low_f / width!r})"
        rows = cursor.execute(
            f"SELECT greatest(0, least(CAST(floor({position}) AS BIGINT), {bins - 1})), count(*) "
            f"FROM {v} WHERE {where} GROUP BY 1 ORDER BY 1"
        ).fetchall()
        values = [
            {"bin_start": low_f + b * width, "bin_end": low_f + (b + 1) * width, "count": n} for b, n in rows
        ]
        return {"values": values, "note": None}

    y = spec.get("y", {})
    color = spec.get("color")

    if mark == "point" and y.get("aggregate") is None:
        yf = quote(y["field"])
        select = f"{xf} AS x, {yf} AS y" + (f", {quote(color['field'])} AS color" if color else "")
        total_row = cursor.execute(
            f"SELECT count(*) FROM {v} WHERE {xf} IS NOT NULL AND {yf} IS NOT NULL"
        ).fetchone()
        total = int(total_row[0]) if total_row else 0
        sample = ""
        if total > max_points:
            sample = f" USING SAMPLE reservoir({max_points} ROWS) REPEATABLE (42)"
            note = f"Showing a repeatable sample of {max_points:,} of {total:,} points."
        rows = cursor.execute(
            f"SELECT {select} FROM (SELECT * FROM {v} WHERE {xf} IS NOT NULL AND {yf} IS NOT NULL){sample}"
        ).fetchall()
        keys = ["x", "y", "color"] if color else ["x", "y"]
        return {"values": [dict(zip(keys, r, strict=True)) for r in rows], "note": note}

    projection, group, order = _grouped(spec)
    groups_row = cursor.execute(
        f"SELECT count(*) FROM (SELECT 1 FROM {v} GROUP BY {', '.join(group)})"
    ).fetchone()
    groups = int(groups_row[0]) if groups_row else 0
    if groups > max_points:
        note = f"Showing the {max_points:,} largest of {groups:,} groups."
        top = (
            f"SELECT {', '.join(projection)} FROM {v} GROUP BY {', '.join(group)} "
            f"ORDER BY y DESC NULLS LAST LIMIT {max_points}"
        )
        sql = f"SELECT * FROM ({top}) ORDER BY {order}"
    else:
        sql = f"SELECT {', '.join(projection)} FROM {v} GROUP BY {', '.join(group)} ORDER BY {order}"
    result = cursor.execute(sql)
    names = [d[0] for d in result.description or []]
    return {"values": [dict(zip(names, r, strict=True)) for r in result.fetchall()], "note": note}


# ===== the cards =====


def check_card(body: Any) -> Card:
    """A card as sent, checked: its kind, its title, and the one field its kind carries."""
    if not isinstance(body, dict):
        raise ValueError("Send a card object.")
    kind = body.get("kind")
    if kind not in _KINDS:
        raise ValueError(f"kind must be one of {', '.join(_KINDS)}.")
    title = body.get("title", "")
    if not isinstance(title, str) or len(title) > 200:
        raise ValueError("title must be text, at most 200 characters.")
    out: Card = {"kind": kind, "title": title}
    if kind in _BODY_LIMITS:
        text = body.get("body", "")
        limit = _BODY_LIMITS[kind]
        if not isinstance(text, str) or (kind != "note" and not text.strip()) or len(text) > limit:
            raise ValueError(f"A {kind} card's body is {_BODY_WORDS[kind]}, at most {limit:,} characters.")
        if kind in ("plotly", "echarts", "vegalite"):
            try:
                figure = json.loads(text)
            except ValueError as error:
                raise ValueError(f"A {kind} card's body is not JSON: {error}") from error
            if not isinstance(figure, dict):
                raise ValueError(f"A {kind} card's body is one JSON object.")
            if kind == "vegalite" and _fetches(figure):
                raise ValueError(
                    "A vegalite card draws only data inside it (data.values or datasets): no url or href."
                )
        if kind == "image" and not text.startswith("data:image/"):
            raise ValueError("An image card's body is a data:image/... URL.")
        out["body"] = text
        if kind in ("plotly", "echarts") and body.get("sql") is not None:
            if not isinstance(body["sql"], str) or not body["sql"].strip():
                raise ValueError("A figure's sql is a SELECT over its grid; the result fills the figure.")
            out["sql"] = body["sql"]
    elif kind == "table":
        sql = body.get("sql")
        if isinstance(sql, str):
            out["sql"] = sql
        elif "rows" in body:
            out |= _fixed_rows(body)
        else:
            raise ValueError("A table card needs sql (a SELECT over its grid), or columns and rows.")
    else:
        out["spec"] = body.get("spec")
    size = body.get("size")
    if size is not None:
        # The card's place on the 12-column board: w columns wide, h rows tall (the page's grid units).
        if (
            not isinstance(size, dict)
            or not isinstance(size.get("w"), int)
            or not isinstance(size.get("h"), int)
            or not 2 <= size["w"] <= 12
            or not 3 <= size["h"] <= 30
        ):
            raise ValueError('size is {"w": 2 to 12 columns, "h": 3 to 30 rows}.')
        out["size"] = {"w": size["w"], "h": size["h"]}
    return out


def _fetches(spec: Any) -> bool:
    """A Vega-Lite spec that would load or link anything: a url (data, an image mark) or an href."""
    if isinstance(spec, dict):
        return any(k in ("url", "href") or _fetches(v) for k, v in spec.items())
    if isinstance(spec, list):
        return any(_fetches(v) for v in spec)
    return False


def _not_the_agents(card: Card, who: str) -> None:
    if card["kind"] == "vegalite" and who == "agent":
        raise Refused("A vegalite card comes from your own Python or the page; the agent draws with chart.")


def _fixed_rows(body: dict[str, Any]) -> Card:
    """A table card's own rows (from Python or the agent): the first 100, cells as JSON values."""
    columns, rows = body.get("columns"), body.get("rows")
    if (
        not isinstance(columns, list)
        or not columns
        or len(columns) > _TABLE_CARD_COLUMNS
        or not all(isinstance(c, str) and len(c) <= 200 for c in columns)
    ):
        raise ValueError(f"A table card's columns are 1 to {_TABLE_CARD_COLUMNS} names.")
    if not isinstance(rows, list) or not all(isinstance(r, list) and len(r) == len(columns) for r in rows):
        raise ValueError("A table card's rows are lists, one value per column.")
    kept = rows[:_TABLE_CARD_ROWS]
    for row in kept:
        for value in row:
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise ValueError("A table card's cells are text, numbers, true/false or null.")
            if isinstance(value, str) and len(value) > _CELL_CHARACTERS:
                raise ValueError(f"A table card's cells are at most {_CELL_CHARACTERS:,} characters.")
    capped = len(rows) > _TABLE_CARD_ROWS or bool(body.get("capped"))
    return {"columns": list(columns), "rows": [list(r) for r in kept], "capped": capped}


def _key(body: Any) -> str | None:
    key = body.get("key") if isinstance(body, dict) else None
    if key is None:
        return None
    if not isinstance(key, str) or not key.strip() or len(key) > 100:
        raise ValueError(
            "key is text, at most 100 characters: it names the card, so sending it again replaces it."
        )
    return key


# What a saved dashboard keeps of each card (besides what it showed: "frozen").
_KEPT: tuple[str, ...] = (
    "kind",
    "title",
    "body",
    "spec",
    "sql",
    "columns",
    "rows",
    "capped",
    "size",
    "pinned",
    "new_page",
    "key",
    "origin",
    "dataset",
    "created_by",
)


def _on_a_grid(card: Card) -> bool:
    """A chart or a query table: drawn on a grid's view, so frozen when no grid on its table is open."""
    return card["kind"] == "chart" or "sql" in card


def _frozen(card: Card) -> Card:
    """What a chart or query card showed, to keep in a saved dashboard."""
    if card["kind"] == "chart" and "vega_lite" in card:
        return {"vega_lite": card["vega_lite"], "points": card.get("points"), "note": card.get("note")}
    if "sql" in card and card["kind"] in ("plotly", "echarts"):
        return {"body": card["body"]}  # as drawn: the query's result inside
    if "sql" in card and "columns" in card:
        return {"columns": card["columns"], "rows": card["rows"], "capped": card.get("capped", False)}
    return dict(card.get("frozen") or {})


def _place(card: Card) -> tuple[bool, float]:
    """Pinned cards first, then the order the analyst arranged (new cards go last)."""
    return (not card["pinned"], float(card.get("order") or card.get("created") or 0.0))


class Cards:
    def __init__(
        self, ws: Workspace, dashboards: Dashboards | None = None, theme_shelf: ThemeShelf | None = None
    ) -> None:
        self.ws = ws
        self.dashboards = dashboards
        self._cards: dict[str, Card] = {}
        # The Sandbox's theme: "app" (follow the app's), a built-in's id, or one of the user's own.
        self._shelf_themes = theme_shelf
        self._theme: str = themes.APP
        self._custom: dict[str, themes.Theme] = {}
        for file in theme_shelf.all() if theme_shelf else []:
            try:
                kept = themes.read_theme(file)
            except themes.ThemeError:
                continue  # edited by hand into something else
            self._custom[kept.id] = kept

    # ===== the Sandbox's theme =====

    def _theme_by_id(self, theme_id: str) -> themes.Theme | None:
        return themes.BY_ID.get(theme_id) or self._custom.get(theme_id)

    def themes(self) -> dict[str, Any]:
        """Every theme the Sandbox can take, and the one it has."""
        listed = [*themes.BUILT_IN, *self._custom.values()]
        current = self._theme_by_id(self._theme)
        return {
            "current": self._theme,
            "theme": current.to_json() if current else None,
            "themes": [t.to_json() for t in listed],
        }

    async def set_theme(self, theme_id: Any, who: str) -> dict[str, Any]:
        if theme_id != themes.APP and (not isinstance(theme_id, str) or self._theme_by_id(theme_id) is None):
            raise NotFound(f"No Sandbox theme {theme_id!r}: GET /sandbox/themes lists them.")
        self._theme = str(theme_id)
        await self.ws.live.broadcast({"type": "cards_updated", "grid": None})
        self.ws.audit.write(who, "sandbox_theme", self._theme)
        return {"current": self._theme}

    async def add_theme(self, raw: Any, who: str) -> dict[str, Any]:
        """A theme from its file. The user's are kept on disk (their import is the save); the agent's live
        in memory, marked as the agent's, until the user keeps one (adds it again themselves)."""
        theme = themes.read_theme(raw, by_agent=who == "agent")
        if theme.id not in self._custom and len(self._custom) >= themes.MAX_CUSTOM:
            raise ValueError(f"At most {themes.MAX_CUSTOM} themes of your own: remove one first.")
        self._custom[theme.id] = theme
        if who != "agent" and self._shelf_themes is not None:
            self._shelf_themes.write(theme.id, theme.file())
        await self.ws.live.broadcast({"type": "cards_updated", "grid": None})
        self.ws.audit.write(who, "add_theme", theme.label)
        return theme.to_json()

    async def remove_theme(self, theme_id: str, who: str) -> dict[str, Any]:
        if theme_id not in self._custom:
            raise NotFound(f"No theme of yours named {theme_id!r} (the built-in ones stay).")
        del self._custom[theme_id]
        if self._shelf_themes is not None:
            self._shelf_themes.delete(theme_id)
        if self._theme == theme_id:
            self._theme = themes.APP
        await self.ws.live.broadcast({"type": "cards_updated", "grid": None})
        self.ws.audit.write(who, "remove_theme", theme_id)
        return {"removed": theme_id}

    def get(self, card_id: str) -> Card:
        card = self._cards.get(card_id)
        if card is None:
            raise NotFound("No such card.")
        return card

    def chart(self, card_id: str) -> tuple[Card, Grid, DatasetInfo]:
        """A chart card with its grid and dataset."""
        card = self._cards.get(card_id)
        if card is None or card["kind"] != "chart":
            raise NotFound("No such chart.")
        if card["grid"] is None:
            raise NotFound(f"This chart is as it was saved: open {card['dataset']} to use it live.")
        grid, info = self.ws.grid_and_info(card["grid"])
        return card, grid, info

    def _render(self, card: Card, cur: SqlSession) -> Card:
        ws = self.ws
        grid, info = ws.grid_and_info(card["grid"])
        compiled = ws.compiled(cur, grid, info)
        shown = dict(card) | {
            "source": {"dataset": info.name, "version": info.version, "steps": len(grid.steps)}
        }
        try:
            if card["kind"] == "chart":
                spec = validate(card["spec"], compiled.columns)
                data = aggregate(cur, compiled.sql(), spec, ws.settings.max_chart_points)
                shown["vega_lite"] = vega_lite(spec, data, compiled.columns)
                shown["note"] = data["note"]
                shown["points"] = len(data["values"])
            elif card["kind"] in ("plotly", "echarts") and "sql" in card:
                # The grid's own table name reads the grid as it looks (its steps), like `grid` does.
                names = {i.name for i in ws.store.all()}
                guarded = guard(card["sql"], names, compiled.sql, grid_alias=info.name, grammar=ws.grammar)
                if guarded.kind != "select":
                    raise ValueError("A figure's sql is a SELECT.")
                result = cur.execute(guarded.sql)
                columns = [d[0] for d in result.description or []]
                limit = ws.settings.max_chart_points
                fetched = result.fetchmany(limit + 1)
                figure = fill_figure(card["kind"], json.loads(card["body"]), columns, fetched[:limit])
                shown["body"] = json.dumps(figure)
                shown["points"] = min(len(fetched), limit)
                if len(fetched) > limit:
                    shown["note"] = f"Showing the first {limit:,} rows of the query."
            elif card["kind"] == "table" and "sql" in card:
                guarded = guard(
                    card["sql"], {i.name for i in ws.store.all()}, compiled.sql, grammar=ws.grammar
                )
                if guarded.kind != "select":
                    raise ValueError("A table card runs a SELECT.")
                result = cur.execute(guarded.sql)
                shown["columns"] = [d[0] for d in result.description or []]
                fetched = result.fetchmany(_TABLE_CARD_ROWS + 1)
                shown["rows"] = [list(r) for r in fetched[:_TABLE_CARD_ROWS]]
                shown["capped"] = len(fetched) > _TABLE_CARD_ROWS
        except (ValueError, ws.engine_error) as error:  # ChartError is a ValueError
            shown["error"] = str(error)
        return shown

    async def for_grid(self, grid: Grid) -> dict[str, Any]:
        """Every card for the grid, charts computed on its view."""
        mine = [c for c in self._cards.values() if c["grid"] == grid.id]
        mine.sort(key=_place)

        def compute(cur: SqlSession) -> list[Card]:
            return [self._render(c, cur) for c in mine]

        return {"cards": await self.ws.grid_query(grid, None, BACKGROUND, compute)}

    async def everything(self) -> dict[str, Any]:
        """The Analysis Sandbox: one list for every table. Each card is still drawn on its own
        grid's view; a card whose grid was closed drops out."""
        for card in self._cards.values():
            self._relink(card)
        shown: list[Card] = [self._as_saved(c) for c in self._cards.values() if c["grid"] is None]
        for grid_id in dict.fromkeys(c["grid"] for c in self._cards.values() if c["grid"] is not None):
            grid = self.ws.grids.get(grid_id)
            if grid is None:
                continue
            mine = [c for c in self._cards.values() if c["grid"] == grid_id]

            def compute(cur: SqlSession, mine: list[Card] = mine) -> list[Card]:
                return [self._render(c, cur) for c in mine]

            shown += await self.ws.grid_query(grid, None, BACKGROUND, compute)
        shown.sort(key=_place)
        return {"cards": shown}

    def _relink(self, card: Card) -> None:
        """A chart or query card opened from a saved dashboard draws on an open grid on its table (now or
        once one opens). Any other card whose grid closes leaves the Sandbox, as it always has."""
        if "frozen" not in card or not _on_a_grid(card):
            return
        if card["grid"] is not None and self.ws.grids.get(card["grid"]):
            return
        grid = self.ws.grids.first_for(card["dataset"]) if card.get("dataset") else None
        if grid is not None and self.ws.store.get(grid.dataset) is not None:
            card["grid"] = grid.id
        elif card.get("frozen"):
            card["grid"] = None  # shows what it showed when saved

    def _as_saved(self, card: Card) -> Card:
        shown = dict(card)
        if _on_a_grid(card):
            shown |= card.get("frozen") or {}
            shown["note"] = " · ".join(
                filter(
                    None,
                    [
                        card.get("frozen", {}).get("note"),
                        f"As saved: open {card['dataset']} to redraw it live.",
                    ],
                )
            )
        return shown

    # ===== saved dashboards =====

    def _shelf(self) -> Dashboards:
        if self.dashboards is None:
            raise NotFound("Dashboards are not kept here.")
        return self.dashboards

    def saved(self) -> dict[str, Any]:
        return {"dashboards": self._shelf().names()}

    def versions(self, name: str) -> dict[str, Any]:
        """A dashboard's versions: when each was saved, its cards' kinds and titles (not their content)."""
        body = self._shelf().read(name)
        if body is None:
            raise NotFound(f"No dashboard named {name!r}.")
        return {
            "name": name,
            "versions": [
                {
                    "version": number,
                    "saved": v.get("saved"),
                    "cards": [
                        {"kind": c.get("kind"), "title": c.get("title", "")}
                        for c in v.get("cards", [])
                        if c.get("kind") != "theme"
                    ],
                }
                for number, v in enumerate(body["versions"], start=1)
            ],
        }

    async def save(self, name: Any, who: str) -> dict[str, Any]:
        """Save the Sandbox as `name`: a new version, every card as it is drawn now."""
        if not isinstance(name, str):
            raise ValueError('Send {"name": ...}: 1 to 64 letters, digits, _ or -.')
        shown = (await self.everything())["cards"]
        if not shown:
            raise ValueError("The Sandbox is empty: nothing to save.")
        kept = [{k: c[k] for k in _KEPT if k in c} | {"frozen": _frozen(c)} for c in shown]
        saved = datetime.now(UTC).isoformat(timespec="seconds")
        theme = self._theme_by_id(self._theme)
        if theme is not None:  # the dashboard keeps its look, a theme of the user's own included
            kept.append({"kind": "theme", "theme": theme.file() | {"id": theme.id}})
        version = self._shelf().add_version(name, kept, saved)
        self.ws.audit.write(who, "save_dashboard", name, cards=len(kept), version=version)
        return {"name": name, "version": version, "cards": len(kept), "saved": saved}

    async def open(self, name: str, version: Any, who: str) -> dict[str, Any]:
        """Make a saved version (the last one unless `version` names another) the Sandbox."""
        body = self._shelf().read(name)
        if body is None or not body["versions"]:
            raise NotFound(f"No dashboard named {name!r}.")
        versions = body["versions"]
        number = len(versions) if version is None else version
        if not isinstance(number, int) or not 1 <= number <= len(versions):
            raise ValueError(f"version is 1 to {len(versions)}.")
        self._cards = {}
        self._theme = themes.APP
        for order, saved in enumerate(versions[number - 1].get("cards", []), start=1):
            if saved.get("kind") == "theme":
                self._restore_theme(saved.get("theme"))
                continue
            try:
                card = check_card(saved) | {
                    k: saved[k] for k in _KEPT if k in saved and k not in ("columns", "rows")
                }
            except ValueError:
                continue  # edited by hand into something else
            if "sql" not in card and card["kind"] == "table":
                card |= {k: saved[k] for k in ("columns", "rows", "capped") if k in saved}
            card |= {
                "id": secrets.token_hex(4),
                "grid": None,
                "dataset": saved.get("dataset"),
                "frozen": saved.get("frozen") or {},
                "origin": saved.get("origin") or "Sandbox",
                "pinned": bool(saved.get("pinned")),
                "new_page": bool(saved.get("new_page")),
                "created_by": saved.get("created_by") or who,
                "created": time.time(),
                "order": float(order),
            }
            self._relink(card)
            self._cards[card["id"]] = card
        await self.ws.live.broadcast({"type": "cards_updated", "grid": None})
        self.ws.audit.write(who, "open_dashboard", name, version=number, cards=len(self._cards))
        live = sum(1 for c in self._cards.values() if _on_a_grid(c) and c["grid"] is not None)
        frozen = sum(1 for c in self._cards.values() if _on_a_grid(c) and c["grid"] is None)
        return {"name": name, "version": number, "cards": len(self._cards), "live": live, "as_saved": frozen}

    def _restore_theme(self, file: Any) -> None:
        if not isinstance(file, dict):
            return
        if file.get("id") in themes.BY_ID:
            self._theme = str(file["id"])
            return
        try:
            theme = themes.read_theme(file)
        except themes.ThemeError:
            return
        self._custom.setdefault(theme.id, theme)
        self._theme = theme.id

    def delete_saved(self, name: str, who: str) -> dict[str, Any]:
        if not self._shelf().delete(name):
            raise NotFound(f"No dashboard named {name!r}.")
        self.ws.audit.write(who, "delete_dashboard", name)
        return {"deleted": name}

    async def _check_chart(self, grid: Grid, info: DatasetInfo, spec: Any) -> None:
        def check(cur: SqlSession) -> None:
            validate(spec, self.ws.compiled(cur, grid, info).columns)

        await self.ws.grid_query(grid, None, BACKGROUND, check)

    async def add(self, grid: Grid, info: DatasetInfo, body: Any, who: str) -> dict[str, Any]:
        card = check_card(body)
        _not_the_agents(card, who)
        if card["kind"] == "chart":
            await self._check_chart(grid, info, card["spec"])
        return await self._keep(card, _key(body), grid.id, info.name, who, who)

    async def add_free(self, body: Any, who: str) -> dict[str, Any]:
        """A card that belongs to no table: any kind but a chart or a table's query, which need a
        grid's view. `"origin": "python"` marks one sent by sag.sandbox."""
        card = check_card(body)
        _not_the_agents(card, who)
        if card["kind"] == "chart" or "sql" in card:
            raise ValueError("A chart or a table's query is drawn on a grid: POST /grids/{id}/cards.")
        origin = "python" if isinstance(body, dict) and body.get("origin") == "python" else who
        return await self._keep(card, _key(body), None, None, who, origin)

    async def _keep(
        self, card: Card, key: str | None, grid: str | None, dataset: str | None, who: str, origin: str
    ) -> dict[str, Any]:
        ws = self.ws
        same = next((c for c in self._cards.values() if key is not None and c.get("key") == key), None)
        if same is not None:  # the same key again: new content in the old card's place and size
            for field in ("spec", "body", "sql", "columns", "rows", "capped"):
                same.pop(field, None)
            same.update(card | {"grid": grid, "dataset": dataset, "updated": time.time()})
            same.setdefault("size", card.get("size"))
            card = same
        else:
            card |= {
                "id": secrets.token_hex(4),
                "key": key,
                "grid": grid,
                "dataset": dataset,
                "origin": _ORIGINS.get(origin, "Sandbox"),
                "pinned": False,
                "new_page": False,
                "created_by": who,
                "created": time.time(),
                "order": max((c.get("order", 0.0) for c in self._cards.values()), default=0.0) + 1.0,
            }
            self._cards[card["id"]] = card
        await ws.live.broadcast({"type": "cards_updated", "grid": grid})
        ws.audit.write(
            who, "add_card", dataset or card["origin"], kind=card["kind"], replaced=same is not None
        )
        return {"card": card["id"], "replaced": same is not None}

    async def update(self, card: Card, body: Any, who: str) -> dict[str, Any]:
        """Change title, spec, body, sql, pinned, new_page (it starts a page in Page view), or its
        place: "before": another card's id puts it just before that card, null puts it last."""
        ws = self.ws
        if not isinstance(body, dict):
            raise ValueError("Send the fields to change.")
        placing = {"pinned", "new_page", "before"}
        if set(body) - placing:
            merged = check_card({**card, **{k: v for k, v in body.items() if k not in placing}})
            _not_the_agents(merged, who)
            if card["grid"] is None and _on_a_grid(card) and ({"spec", "sql"} & set(body)):
                raise ValueError(
                    f"This card is as it was saved: open {card['dataset']} to change what it draws."
                )
            if merged["kind"] == "chart" and card["grid"] is not None:
                grid, info = ws.grid_and_info(card["grid"])
                await self._check_chart(grid, info, merged["spec"])
            elif card["grid"] is None and "sql" in merged and not card.get("frozen"):
                raise ValueError("A card that belongs to no table has no query.")
            card.update(merged)
        if "pinned" in body:
            card["pinned"] = bool(body["pinned"])
        if "new_page" in body:
            card["new_page"] = bool(body["new_page"])
        if "before" in body:
            self._move(card, body["before"])
        await ws.live.broadcast({"type": "cards_updated", "grid": card["grid"]})
        ws.audit.write(who, "update_card", card["dataset"] or card.get("origin"))
        return {"card": card["id"]}

    def _move(self, card: Card, before: Any) -> None:
        """Put `card` just before the card `before` (or last), and number every card again."""
        if before is not None and (not isinstance(before, str) or before not in self._cards):
            raise ValueError('"before" is another card\'s id, or null for the end.')
        ordered = sorted(
            (c for c in self._cards.values() if c is not card), key=lambda c: c.get("order", 0.0)
        )
        at = next((i for i, c in enumerate(ordered) if c["id"] == before), len(ordered))
        ordered.insert(at, card)
        for number, each in enumerate(ordered, start=1):
            each["order"] = float(number)

    async def remove(self, card_id: str, who: str) -> dict[str, Any] | None:
        """Remove a card. None when there is no such card."""
        card = self._cards.pop(card_id, None)
        if card is None:
            return None
        await self.ws.live.broadcast({"type": "cards_updated", "grid": card["grid"]})
        self.ws.audit.write(who, "remove_card", card["dataset"] or card.get("origin"))
        return {"removed": card["id"]}

    async def select(self, card: Card, grid: Grid, info: DatasetInfo, body: Any, who: str) -> dict[str, Any]:
        """Cross-filtering: clicking a bar adds `x = value` (or the time bucket's range); a heatmap
        cell adds its row's value too ({"value": x, "y": y}). The new steps must compile on the
        grid's view, or none is added (a value its column cannot hold used to break the grid)."""
        ws = self.ws
        if not isinstance(body, dict):
            raise ValueError('Send {"value": ...}.')
        spec = card["spec"]
        if spec["mark"] == "histogram":
            raise ValueError("Select a histogram range from the column profile instead.")

        def added(cur: SqlSession) -> list[Step]:
            columns = ws.compiled(cur, grid, info).columns
            validate(spec, columns)  # its columns may have left the view since
            families = {c.name: type_family(c.type) for c in columns}
            x = spec["x"]
            clicked: list[tuple[str, Any, str | None]] = [(x["field"], body.get("value"), x.get("time_unit"))]
            if spec["mark"] == "heatmap" and "y" in body:
                clicked.append((spec["y"]["field"], body["y"], None))
            picked = [_click_filter(f, families[f], v, unit) for f, v, unit in clicked]
            steps = [Step(kind="filter", params=p | {"from_chart": card["id"]}) for p in picked]
            trial = ws.views.compile(cur, info, [*grid.steps, *steps], grid.settings)
            if trial.error is not None:
                raise ValueError(str(trial.error))
            return steps

        grid.steps = [*grid.steps, *await ws.grid_query(grid, None, BACKGROUND, added)]
        await ws.live.broadcast({"type": "grid_updated", "grid": grid.id, "by": who, "gen": grid.gen})
        return {"steps": len(grid.steps)}

    async def code(self, card: Card, grid: Grid, info: DatasetInfo) -> dict[str, Any]:
        """Altair code for a chart card."""
        ws = self.ws

        def make(cur: SqlSession) -> str:
            columns = ws.compiled(cur, grid, info).columns
            spec = validate(card["spec"], columns)
            return altair_code(spec, columns, info.name, ws.settings.max_chart_points)

        return {"code": await ws.grid_query(grid, None, BACKGROUND, make)}


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
