"""Hot reload: tell every open grid page when a dataset changes (plan section 5.2, WS /ws).

Each grid page holds one WebSocket. On a push the server sends
{"type": "dataset_updated", "dataset": "orders", "version": 4} to all of them, and each page
refetches only if the dataset is its own. Like a SignalR hub with one broadcast method.

SwissArmyGrid lives in VS Code: each VS Code window's extension holds one more socket, as an
opener (/ws?opener=1). When a push finds nothing on screen, the server asks the window the user
was last in to show the workspace, so no caller ever opens a browser of its own. The openers are
also how a server VS Code started knows VS Code has closed (`openers_gone_for`).
"""

from __future__ import annotations

from time import monotonic
from typing import Any

from starlette.websockets import WebSocket

# How long a window a push was told to open may take to connect before the next push opens
# another. A browser that isn't running yet takes a few seconds to start and load the page.
WINDOW_GRACE_SECONDS: float = 15.0


class LiveUpdates:
    """The set of connected grid pages, and the VS Code windows that show them."""

    def __init__(self) -> None:
        self._sockets: set[WebSocket] = set()
        # Which grid each page shows (it says so when it connects), so a push can tell whether
        # its grid is already on screen.
        self._grid_of: dict[WebSocket, str] = {}
        # Open workspace windows (the tab bar): a push adds its grid there as a tab.
        self._workspaces: set[WebSocket] = set()
        # When a caller was last told to open a window, until that window connects.
        self._window_promised: float | None = None
        # The VS Code windows, the one the user was last in at the end.
        self._openers: list[WebSocket] = []
        # When the last VS Code window left (from the start, until the first one arrives).
        self._openers_left_at: float = monotonic()

    def add(
        self, websocket: WebSocket, grid: str | None = None, workspace: bool = False, opener: bool = False
    ) -> None:
        self._sockets.add(websocket)
        if grid:
            self._grid_of[websocket] = grid
        if workspace:
            self._workspaces.add(websocket)
            self._window_promised = None  # the window it was waiting for has arrived
        if opener:
            self._openers.append(websocket)

    def discard(self, websocket: WebSocket) -> None:
        self._sockets.discard(websocket)
        self._grid_of.pop(websocket, None)
        self._workspaces.discard(websocket)
        if websocket in self._openers:
            self._openers.remove(websocket)
            if not self._openers:
                self._openers_left_at = monotonic()

    def focus(self, websocket: WebSocket) -> None:
        """A VS Code window came to the front: it shows the next grid that needs a window."""
        if websocket in self._openers:
            self._openers.remove(websocket)
            self._openers.append(websocket)

    def openers_gone_for(self) -> float:
        """Seconds since the last VS Code window left (0 while one is connected)."""
        return 0.0 if self._openers else monotonic() - self._openers_left_at

    def watching(self, grid: str) -> bool:
        """True when an open page shows this grid."""
        return any(g == grid and ws in self._sockets for ws, g in self._grid_of.items())

    def workspace_open(self) -> bool:
        """True when a workspace window is open (it shows every grid as a tab)."""
        return any(ws in self._sockets for ws in self._workspaces)

    def page_open(self) -> bool:
        """True when any grid page or workspace page is connected (a snapshot needs one to draw it)."""
        return self.workspace_open() or any(ws in self._sockets for ws in self._grid_of)

    def shown(self, grid: str) -> bool:
        """Will this grid be on screen without opening a window? (its page, or a workspace tab)"""
        return self.watching(grid) or self.workspace_open()

    def shown_or_opening(self, grid: str) -> bool:
        """`shown`, for a caller that has a window opened when the answer is False (a push, the
        MCP's open_grid). The first such caller with nothing open gets False, and a window is
        opened for it; the ones right behind it, while that window is still loading, get True and
        wait for it. Without this a script pushing three tables opened three windows: each push
        landed before the first window had connected, so each saw nothing on screen."""
        if self.shown(grid):
            return True
        now = monotonic()
        if self._window_promised is not None and now - self._window_promised < WINDOW_GRACE_SECONDS:
            return True
        self._window_promised = now
        return False

    async def ask_opener(self, url: str) -> bool:
        """Have the VS Code window the user was last in show `url` (the workspace). False when no
        window is connected: the grid then waits until the next push or open finds one."""
        for websocket in reversed(list(self._openers)):
            try:
                await websocket.send_json({"type": "open_workspace", "url": url})
                return True
            except Exception:
                self.discard(websocket)  # a window closed mid-send: try the one before it
        return False

    async def dataset_updated(self, dataset: str, version: int) -> None:
        await self.broadcast({"type": "dataset_updated", "dataset": dataset, "version": version})

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send one notice to every page. Kinds: dataset_updated, dataset_closed,
        grid_updated (steps changed elsewhere, e.g. by an agent), grid_closed,
        cards_updated, approval_requested, approval_resolved."""
        for websocket in list(self._sockets):
            try:
                await websocket.send_json(message)
            except Exception:
                # Best effort on purpose: a tab closed mid-send must never fail the push.
                # The socket's own endpoint loop removes it when it sees the disconnect.
                self.discard(websocket)
