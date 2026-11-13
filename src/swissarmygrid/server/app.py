"""The web app: asks the composition root (bootstrap.py) for the services and mounts every route
behind the guard.

Route modules, one per area, like ASP.NET controllers. Each is given its service (services/*.py)
and does nothing but read the request, call the use case and shape the answer:
  routes_datasets.py       push, list, close, load a file, paste, the "+" tab; health and shutdown
  routes_grids.py          rows, steps, profile, SQL, export, code, recipes for one grid
  routes_analysis.py       the Analyze menu's analyses on one grid's view
  routes_regression.py     regression and what-if predictions
  routes_timeseries.py     time series and forecast
  routes_tieout.py         the tie-out report on a compare
  routes_agent.py          the MCP door: the agent's reads, approvals, the current table
  routes_cards.py          side-panel charts and notes (plan section 11)
  routes_catalog.py        catalogs (a source's names and types)
  routes_blades.py         blades (saved code that loads): the user's alone
  routes_notebook.py       the notebook tab: marimo served at /marimo, cells added to the open notebook
  routes_relationships.py  the relationship model, the Key Finder, comparing two grids
  routes_processes.py      Python sessions offering tables: the Open list
  routes_snapshots.py      a PNG of the page the user sees, drawn by the page, for an agent
plus here: the WebSocket for live updates, the grid page shell and its static files.

All DuckDB work runs on the priority worker pool (adapters/database.py), never on the event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from swissarmygrid.bootstrap import bootstrap
from swissarmygrid.server import (
    routes_agent,
    routes_analysis,
    routes_blades,
    routes_cards,
    routes_catalog,
    routes_datasets,
    routes_grids,
    routes_notebook,
    routes_packs,
    routes_processes,
    routes_regression,
    routes_relationships,
    routes_snapshots,
    routes_tieout,
    routes_timeseries,
)
from swissarmygrid.server.security import WEBSOCKET_SUBPROTOCOL, LocalOnlyGuard
from swissarmygrid.settings import Settings

# How often the server looks for Python sessions that stopped reporting.
PRUNE_EVERY_SECONDS: float = 10.0


def create_app(
    port: int,
    token: str,
    settings: Settings | None = None,
    on_shutdown: Callable[[], None] | None = None,
    audit_log: bool = True,
) -> Starlette:
    """Build the app for a server listening on 127.0.0.1:`port` that accepts `token`."""
    ctx = bootstrap(port, token, settings, on_shutdown, audit_log)
    settings, workers = ctx.settings, ctx.workers

    async def live(websocket: WebSocket) -> None:
        # Echo our subprotocol only when the client offered it (browsers do; others may not).
        offered: list[str] = websocket.scope.get("subprotocols", [])
        await websocket.accept(
            subprotocol=WEBSOCKET_SUBPROTOCOL if WEBSOCKET_SUBPROTOCOL in offered else None
        )
        ctx.live.add(
            websocket,
            websocket.query_params.get("grid"),
            workspace=websocket.query_params.get("workspace") == "1",
            # A VS Code window (the extension): it shows the workspace when a push needs it.
            opener=websocket.query_params.get("opener") == "1",
        )
        try:
            while True:
                # The only message a client sends: a VS Code window saying it came to the front.
                if await websocket.receive_text() == '{"type":"focus"}':
                    ctx.live.focus(websocket)
        except WebSocketDisconnect:
            pass
        finally:
            ctx.live.discard(websocket)

    async def grid_page(request: Request) -> Response:
        return FileResponse(settings.web_root / "index.html", headers={"Cache-Control": "no-store"})

    async def workspace_page(request: Request) -> Response:
        # The same page as /g/<id>: it shows every open grid as a tab, opened on the last one.
        return FileResponse(settings.web_root / "index.html", headers={"Cache-Control": "no-store"})

    async def catalog_page(request: Request) -> Response:
        # Blades and the catalog explorer, in a tab of its own (plan decision 18).
        return FileResponse(settings.web_root / "catalog.html", headers={"Cache-Control": "no-store"})

    async def drop_silent_sessions() -> None:
        # A notebook that closes or crashes just stops reporting; the list notices within a few
        # seconds and tells the pages, so a dead session never lingers in the Open dialog.
        while True:
            await asyncio.sleep(PRUNE_EVERY_SECONDS)
            await ctx.sessions.drop_silent()

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        pruner = asyncio.create_task(drop_silent_sessions())
        yield
        pruner.cancel()
        workers.close()
        ctx.notebook.stop()

    app = Starlette(
        routes=[
            *routes_datasets.routes(ctx.datasets),
            *routes_grids.routes(ctx.grids, ctx.profiles, ctx.sql, ctx.exports, ctx.codes),
            *routes_analysis.routes(ctx.grids, ctx.analysis),
            *routes_regression.routes(ctx.grids, ctx.regression),
            *routes_timeseries.routes(ctx.grids, ctx.timeseries),
            *routes_agent.routes(ctx.agent),
            *routes_cards.routes(ctx.cards),
            *routes_catalog.routes(ctx.catalogs),
            *routes_blades.routes(ctx.blades),
            *routes_notebook.routes(ctx.notebook_cells, ctx.notebook, ctx.notebook_cookie, settings.web_root),
            *routes_relationships.routes(ctx.models, ctx.finder, ctx.keys, ctx.compares),
            *routes_tieout.routes(ctx.tie_out),
            *routes_packs.routes(ctx.packs),
            *routes_processes.routes(ctx.sessions),
            *routes_snapshots.routes(ctx.snapshots),
            WebSocketRoute("/ws", live),
            Route("/g/{grid_id}", grid_page),
            Route("/workspace", workspace_page),
            Route("/catalog", catalog_page),
            Mount("/static", StaticFiles(directory=settings.web_root, check_dir=False)),
        ],
        middleware=[Middleware(LocalOnlyGuard, port=port, token=token, notebook_cookie=ctx.notebook_cookie)],
        lifespan=lifespan,
    )
    app.state.ctx = ctx
    return app
