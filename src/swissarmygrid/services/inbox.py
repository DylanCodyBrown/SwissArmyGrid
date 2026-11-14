"""The agent's inbox: what the user sends their agent from the Workbench (right-click an error or
selected code, Send to the agent).

The owner (2026-11-14): "right click an error or highlight code and right click to send to an agent. It
will send via hook in the MCP." So the agent hears of it two ways: the plugin's UserPromptSubmit hook
(`swissarmygrid-context`) takes what is waiting into the user's next message, and the MCP tool
agent_inbox reads it whenever the agent asks. Kept in memory only (nothing on disk), the newest 20; each
send is written to the audit log (its size, not its text).
"""

from __future__ import annotations

import itertools
import time
from typing import Any

from swissarmygrid.services.workspace import Workspace

MAX_ITEMS: int = 20
MAX_TEXT: int = 20_000
MAX_NOTE: int = 4_000
KINDS: tuple[str, ...] = ("error", "code", "text")


class Inbox:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws
        self._items: list[dict[str, Any]] = []
        self._ids = itertools.count(1)

    async def send(self, body: Any, who: str) -> dict[str, Any]:
        """Keep one item for the agent: {"kind": error|code|text, "title", "text", "note"}."""
        if not isinstance(body, dict):
            raise ValueError('Send {"kind": "error" or "code", "title", "text", "note"}.')
        kind = body.get("kind") or "text"
        text = body.get("text")
        title = body.get("title") or ""
        note = body.get("note") or ""
        if kind not in KINDS:
            raise ValueError(f"kind is one of {', '.join(KINDS)}.")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Send the text to give the agent.")
        if not isinstance(title, str) or not isinstance(note, str):
            raise ValueError("title and note are text.")
        item = {
            "id": next(self._ids),
            "kind": kind,
            "title": title[:300],
            "text": text[:MAX_TEXT] + ("\n… (cut)" if len(text) > MAX_TEXT else ""),
            "note": note[:MAX_NOTE],
            "sent": time.strftime("%H:%M:%S"),
            "taken": False,
        }
        self._items.append(item)
        del self._items[:-MAX_ITEMS]
        self.ws.audit.write(who, "send_to_agent", None, kind=kind, chars=len(text))
        await self.ws.live.broadcast({"type": "inbox_changed", "waiting": self.waiting()})
        return {"id": item["id"], "waiting": self.waiting()}

    def waiting(self) -> int:
        return sum(1 for i in self._items if not i["taken"])

    def read(self, take: bool, everything: bool = False) -> dict[str, Any]:
        """What is waiting (or every item kept, `everything`), oldest first; `take` marks them read."""
        shown = [dict(i) for i in self._items if everything or not i["taken"]]
        if take:
            for item in self._items:
                item["taken"] = True
        return {"items": shown, "waiting": self.waiting()}
