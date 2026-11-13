"""Snapshots: a PNG of the page the user is looking at, for an agent to analyze.

The server draws nothing. The picture has to be what the user actually sees (their scroll, their
selection, a split, an open panel), and only their browser knows that, so the page draws itself and
this is the go-between:

    agent  --asks-->  server  --"snapshot_request" to the pages-->  page
    agent  <--PNG---  server  <--the page's upload (the PNG)------  page

A rendezvous: the agent's request parks on a future, the page's upload completes it. Only the page
may answer (the door refuses the agent), so an agent cannot answer its own request with a picture of
its choosing. The bytes are held in memory for the length of one request and handed straight back:
nothing is written to disk. Every snapshot is written to the audit log.
"""

from __future__ import annotations

import asyncio
import secrets

from swissarmygrid.services.workspace import Conflict, Workspace

# The page areas an agent may ask for: what the page's own elements are called.
AREAS: tuple[str, ...] = ("app", "ribbon", "grid", "steps", "sandbox")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

Answer = asyncio.Future[tuple[bytes, dict[str, str]]]


class SnapshotFailed(Exception):
    """The page could not draw itself; the message is its reason."""


class SnapshotUnanswered(Exception):
    """No page answered in time."""


class Snapshots:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws
        self._pending: dict[str, Answer] = {}

    async def take(self, area: object, scale: object, who: str) -> tuple[bytes, dict[str, str]]:
        """Ask the open page to draw `area` and wait for its PNG: the bytes, and its width and height."""
        ws = self.ws
        timeout = ws.settings.snapshot_timeout_seconds
        if area not in AREAS:
            raise ValueError(f"area must be one of {', '.join(AREAS)}.")
        if scale not in (1, 2) or isinstance(scale, bool):
            raise ValueError("scale must be 1 or 2.")
        if not ws.live.page_open():
            raise Conflict(
                "No SwissArmyGrid page is open to draw the snapshot. Ask the user to open the "
                "SwissArmyGrid tab in VS Code."
            )
        snapshot_id = secrets.token_hex(6)
        waiting: Answer = asyncio.get_running_loop().create_future()
        self._pending[snapshot_id] = waiting
        try:
            await ws.live.broadcast(
                {
                    "type": "snapshot_request",
                    "id": snapshot_id,
                    "area": area,
                    "scale": scale,
                }
            )
            data, meta = await asyncio.wait_for(waiting, timeout)
        except TimeoutError:
            raise SnapshotUnanswered(
                f"The page did not answer within {timeout:.0f} seconds. "
                "It draws only while its tab is visible: ask the user to bring the SwissArmyGrid "
                "tab to the front."
            ) from None
        finally:
            self._pending.pop(snapshot_id, None)
        ws.audit.write(who, "snapshot", None, area=area, scale=scale, bytes=len(data))
        return data, meta

    def waiting(self, snapshot_id: str) -> Answer | None:
        """The request still waiting for that answer, if there is one."""
        found = self._pending.get(snapshot_id)
        return None if found is None or found.done() else found

    @staticmethod
    def answer(waiting: Answer, data: bytes, meta: dict[str, str]) -> bool:
        """The page's PNG. False when the request ended (timed out, or another answer won) while
        this one was arriving."""
        if waiting.done():
            return False
        waiting.set_result((data, meta))
        return True

    @staticmethod
    def fail(waiting: Answer, reason: str) -> bool:
        """The page could not draw (a blocked image, an empty area): its reason reaches the agent."""
        if waiting.done():
            return False
        waiting.set_exception(SnapshotFailed(reason))
        return True
