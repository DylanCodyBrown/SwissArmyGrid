"""Catalogs that load themselves (the owner, 2026-11-14: "auto load the catalog in a separate thread so it
can background load and feed the autocomplete").

A connection's catalog (its tables and columns, by its catalog command) loads in the background:

  SAVED        when a connection is saved (the user is there: a sign-in window is expected)
  START-UP     every connection when the server starts, but not one that signs in with a browser window
               or a phone prompt (domain/connections.py signs_in_interactively): that one waits
  FIRST USE    a waiting one (or any not loaded) when a SQL cell, the side bar or the agent first asks

Each load is Refresh's own run (a child process of the server's Python, services/blades.py), a few at
once (settings.catalogs_at_once), never twice at the same time for one connection. A catalog that arrives
tells the pages (catalogs_changed), so the SQL cell's completion has it at once. A load that fails is
kept with its reason (shown on the connection); nothing is retried unasked. Catalogs stay in memory only,
as Refresh's do.
"""

from __future__ import annotations

import asyncio
from typing import Any

from swissarmygrid.domain import connections as rules
from swissarmygrid.domain.blades import BladeError
from swissarmygrid.services.blades import BladeService, NeedsValues
from swissarmygrid.services.catalogs import CatalogService
from swissarmygrid.services.workspace import Workspace


class CatalogLoading:
    def __init__(
        self, ws: Workspace, blades: BladeService, catalogs: CatalogService, enabled: bool, at_once: int
    ) -> None:
        self.ws = ws
        self._blades = blades
        self._catalogs = catalogs
        self._enabled = enabled
        self._gate = asyncio.Semaphore(max(1, at_once))
        self._running: dict[str, asyncio.Task[None]] = {}  # by name, case folded
        self._state: dict[str, dict[str, str]] = {}  # "waiting", or "failed" with its reason

    def state(self, name: str) -> dict[str, str] | None:
        """loading, waiting (first use), failed (and why), or None: loaded, or never asked."""
        key = name.casefold()
        if key in self._running:
            return {"state": "loading"}
        return self._state.get(key)

    def load(self, name: str, who: str = "app") -> dict[str, Any]:
        """Load a connection's catalog in the background (now, or after the ones already loading)."""
        blade = self._blades.get(name)
        if not rules.has_catalog(rules.connection_kind(blade.kind, blade.template)) or not blade.has_catalog:
            raise BladeError(f"{blade.name} has no catalog to load.")
        key = blade.name.casefold()
        if self._enabled and key not in self._running:
            self._state.pop(key, None)
            task = asyncio.get_running_loop().create_task(self._load(blade.name, who))
            self._running[key] = task
            task.add_done_callback(lambda _: self._running.pop(key, None))
        return {"name": blade.name, **(self.state(blade.name) or {"state": "loaded"})}

    def saved(self, name: str) -> None:
        """A connection was saved: its catalog follows (the user is there, so any sign-in is too)."""
        try:
            self.load(name, "user")
        except BladeError:
            pass  # a vault, or code with no catalog command

    def at_start(self) -> list[str]:
        """Every connection's catalog, in the background, but the ones that sign in with a window (they
        wait for first use) and the ones past the catalog limit. The names started."""
        if not self._enabled:
            return []
        started: list[str] = []
        room = self.ws.settings.max_catalogs - len(self._catalogs.loaded_names())
        for listed in self._blades.listing()["blades"]:
            if not rules.has_catalog(listed.get("connection")) or not listed.get("has_catalog"):
                continue
            blade = self._blades.get(str(listed["name"]))
            if rules.signs_in_interactively(blade.code) or len(started) >= room:
                self._state[blade.name.casefold()] = {"state": "waiting"}
                continue
            self.load(blade.name)
            started.append(blade.name)
        return started

    async def _load(self, name: str, who: str) -> None:
        async with self._gate:
            await self.ws.live.broadcast({"type": "catalog_loading", "name": name, "state": "loading"})
            try:
                await self._blades.refresh(self._blades.get(name), {}, who)
                state = "loaded"
            except NeedsValues as error:
                state = "failed"
                self._state[name.casefold()] = {"state": state, "error": f"needs {', '.join(error.missing)}"}
            except Exception as error:  # the source's own failure: kept to show, never raised here
                state = "failed"
                self._state[name.casefold()] = {"state": state, "error": str(error)[:500]}
            await self.ws.live.broadcast({"type": "catalog_loading", "name": name, "state": state})
