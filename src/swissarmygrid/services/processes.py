"""Python sessions that offer tables to the grid: the data behind the Open list.

A session is one running Python (a notebook kernel, a script, a REPL) whose client has the watcher
on (client/watcher.py). It reports the names and sizes of the tables among its variables, never their
rows. The page lists them; a click queues a request, the session's listener picks it up and pushes
that one table. Rows therefore leave a Python process only when someone chooses to open a table.

In C# terms this is a presence registry, like the connected-users list of a SignalR hub: sessions
announce themselves, vanish when they stop announcing, and can be sent one message (a load request)
that they collect with a long poll.

A session also remembers which datasets it loaded. Those are the ones it keeps current ("follows")
when the variable is assigned again, for as long as the dataset stays open on the server.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from swissarmygrid.domain.datasets import NAME_PATTERN
from swissarmygrid.services.ports import Datasets, Notifier

MAX_TABLES: int = 500
MAX_LABEL_CHARS: int = 120
_PROCESS_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_KIND = re.compile(r"[a-z_]{1,24}")


@dataclass
class _Session:
    id: str
    label: str
    pid: int
    tables: list[dict[str, Any]]
    last_seen: float
    loaded: set[str] = field(default_factory=set)
    requests: list[dict[str, str]] = field(default_factory=list)
    waiters: list[asyncio.Event] = field(default_factory=list)


def valid_process_id(value: str) -> bool:
    return bool(_PROCESS_ID.fullmatch(value))


def _count(value: Any, what: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"A table's {what} must be a whole number or null.")
    return value


def _clean_tables(tables: Any) -> list[dict[str, Any]]:
    if not isinstance(tables, list):
        raise ValueError("tables must be a list.")
    if len(tables) > MAX_TABLES:
        raise ValueError(f"A session may list at most {MAX_TABLES} tables; this one sent too many.")
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in tables:
        if not isinstance(entry, dict):
            raise ValueError("Each table is an object with a name, kind, rows and columns.")
        name, kind = entry.get("name"), entry.get("kind")
        if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
            raise ValueError(f"A table name must be 1 to 64 letters, digits, '_' or '-': {name!r} is not.")
        if not isinstance(kind, str) or not _KIND.fullmatch(kind):
            raise ValueError(f"The kind of table {name!r} is missing or not a plain word.")
        if name in seen:
            continue
        seen.add(name)
        cleaned.append(
            {
                "name": name,
                "kind": kind,
                "rows": _count(entry.get("rows"), "rows"),
                "columns": _count(entry.get("columns"), "columns"),
            }
        )
    return cleaned


class ProcessRegistry:
    """The live sessions. Used from the event loop only, so it needs no lock."""

    def __init__(self, ttl_seconds: float = 30.0, clock: Callable[[], float] = monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._sessions: dict[str, _Session] = {}

    # ===== presence =====

    def sync(self, process_id: str, label: Any, pid: Any, tables: Any) -> bool:
        """Record a session's report. Returns True when what the page shows changed: a new session, a
        new label, or a different table list. A heartbeat that repeats the last report returns False."""
        if not valid_process_id(process_id):
            raise ValueError("The process id must be 1 to 64 letters, digits, '_' or '-'.")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("A session needs a label (its notebook or script).")
        if isinstance(pid, bool) or not isinstance(pid, int):
            raise ValueError("pid must be a whole number.")
        cleaned = _clean_tables(tables)
        label = label.strip()[:MAX_LABEL_CHARS]
        self.prune()
        now = self._clock()
        session = self._sessions.get(process_id)
        if session is None:
            self._sessions[process_id] = _Session(process_id, label, pid, cleaned, now)
            return True
        session.last_seen = now
        changed = (session.label, session.pid, session.tables) != (label, pid, cleaned)
        session.label, session.pid, session.tables = label, pid, cleaned
        return changed

    def touch(self, process_id: str) -> bool:
        """A long poll is also proof of life. False when the session is unknown or has expired."""
        self.prune()
        session = self._sessions.get(process_id)
        if session is None:
            return False
        session.last_seen = self._clock()
        return True

    def prune(self) -> bool:
        """Drop sessions that stopped reporting. True when any went."""
        cutoff = self._clock() - self._ttl
        gone = [pid for pid, s in self._sessions.items() if s.last_seen < cutoff]
        for pid in gone:
            for event in self._sessions.pop(pid).waiters:
                event.set()  # wake its listener; the next poll finds no session
        return bool(gone)

    def snapshot(self, is_open: Callable[[str], bool]) -> list[dict[str, Any]]:
        """Every live session with its tables, each marked open when this session loaded it and the
        server still holds it."""
        self.prune()
        sessions = sorted(self._sessions.values(), key=lambda s: (s.label.lower(), s.id))
        return [
            {
                "id": s.id,
                "label": s.label,
                "pid": s.pid,
                "tables": [{**t, "open": t["name"] in s.loaded and is_open(t["name"])} for t in s.tables],
            }
            for s in sessions
        ]

    # ===== load requests =====

    def request_load(self, process_id: str, name: str) -> bool:
        """Queue a load of one of the session's tables. False when the session or table is unknown."""
        self.prune()
        session = self._sessions.get(process_id)
        if session is None or not any(t["name"] == name for t in session.tables):
            return False
        request = {"action": "load", "name": name}
        if request not in session.requests:
            session.requests.append(request)
        for event in session.waiters:
            event.set()
        return True

    async def wait_requests(self, process_id: str, wait: float) -> list[dict[str, str]]:
        """The session's pending requests, waiting up to `wait` seconds for one to arrive."""
        if not self.touch(process_id):
            return []
        session = self._sessions[process_id]
        if not session.requests and wait > 0:
            event = asyncio.Event()  # a fresh one per wait, so it never belongs to a finished loop
            session.waiters.append(event)
            try:
                await asyncio.wait_for(event.wait(), wait)
            except TimeoutError:
                pass
            finally:
                if event in session.waiters:
                    session.waiters.remove(event)
        taken, session.requests = session.requests, []
        return taken

    # ===== what a session keeps current =====

    def note_loaded(self, process_id: str, name: str) -> bool:
        """The session pushed `name` as a dataset. It now owns the name: another session that had
        loaded the same name stops following it. True when this changed which tables are open."""
        session = self._sessions.get(process_id)
        if session is None:
            return False
        changed = name not in session.loaded
        session.loaded.add(name)
        for other in self._sessions.values():
            if other is not session and name in other.loaded:
                other.loaded.discard(name)
                changed = True
        return changed

    def owns(self, process_id: str, name: str) -> bool:
        """True when this session loaded `name` and no other session has taken the name since."""
        session = self._sessions.get(process_id)
        return session is not None and name in session.loaded

    def following(self, process_id: str, exists: Callable[[str], bool]) -> list[str]:
        """The datasets this session loaded that the server still holds."""
        session = self._sessions.get(process_id)
        return sorted(n for n in session.loaded if exists(n)) if session else []

    def knows(self, process_id: str) -> bool:
        self.prune()
        return process_id in self._sessions


# ===== the use cases =====

MAX_WAIT_SECONDS: float = 30.0


def _clean_errors(raw: Any) -> list[tuple[str, str]]:
    """At most five (table name, message) pairs from a sync's `errors`, each message cut short."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("errors must be a list of {name, message}.")
    pairs: list[tuple[str, str]] = []
    for entry in raw[:5]:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and isinstance(entry.get("message"), str)
        ):
            pairs.append((entry["name"][:64], entry["message"][:300]))
    return pairs


class Sessions:
    """What the page and a session's watcher do with the registry: report, listen, list, ask to open."""

    def __init__(self, registry: ProcessRegistry, store: Datasets, live: Notifier) -> None:
        self.registry = registry
        self._store = store
        self._live = live

    def _open(self, name: str) -> bool:
        return self._store.get(name) is not None

    async def sync(self, process_id: str, body: Any) -> dict[str, Any]:
        """A session's report: label, pid, tables. Replies with the datasets it keeps current."""
        if not isinstance(body, dict):
            raise ValueError("Send an object with label, pid and tables.")
        changed = self.registry.sync(process_id, body.get("label"), body.get("pid"), body.get("tables", []))
        errors = _clean_errors(body.get("errors"))
        if changed:
            await self._live.broadcast({"type": "processes_changed"})
        # Why a table the user clicked would not open (too large, say): the page shows it as a toast.
        label = next((s["label"] for s in self.registry.snapshot(self._open) if s["id"] == process_id), "")
        for name, message in errors:
            await self._live.broadcast(
                {"type": "process_error", "label": label, "name": name, "message": message}
            )
        return {"following": self.registry.following(process_id, self._open)}

    async def requests(self, process_id: str, wait: float) -> list[dict[str, str]] | None:
        """The session's listener: its pending requests, waiting up to `wait` seconds (30 at most).
        None when the session is unknown."""
        if not self.registry.knows(process_id):
            return None
        return await self.registry.wait_requests(process_id, max(0.0, min(wait, MAX_WAIT_SECONDS)))

    def listing(self) -> list[dict[str, Any]]:
        return self.registry.snapshot(self._open)

    def open_table(self, process_id: str, name: str) -> bool:
        """Ask a session to push one of its tables. False when the session or table is gone."""
        return self.registry.request_load(process_id, name)

    async def drop_silent(self) -> None:
        """A notebook that closes or crashes just stops reporting; tell the pages when one went, so a
        dead session never lingers in the Open dialog."""
        if self.registry.prune():
            await self._live.broadcast({"type": "processes_changed"})
