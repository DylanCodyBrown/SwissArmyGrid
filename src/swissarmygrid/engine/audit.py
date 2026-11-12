"""The audit log: one JSON line per action, never a cell value (plan section 6, rule 9).

Each entry records when, who (`user` or `agent`), what, on which dataset, the query text if
any, and how many rows went out. Exports and agent reads are the ways client data leaves the
system, so those are the entries that matter most. Like a structured ILogger sink writing to a
local file; nothing is sent anywhere.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        actor: str,
        action: str,
        dataset: str | None = None,
        query: str | None = None,
        rows: int | None = None,
        **details: Any,
    ) -> None:
        entry: dict[str, Any] = {
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "actor": actor,
            "action": action,
            "dataset": dataset,
        }
        if query is not None:
            entry["query"] = query
        if rows is not None:
            entry["rows"] = rows
        entry.update(details)
        if self._path is None:
            return
        line = json.dumps(entry, default=str)
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def tail(self, limit: int = 200, actor: str | None = None) -> list[dict[str, Any]]:
        """The last `limit` entries, or the last `limit` of one actor's."""
        if self._path is None or not self._path.exists():
            return []
        newest_first: list[dict[str, Any]] = []
        for line in reversed(self._path.read_text(encoding="utf-8").splitlines()):
            if len(newest_first) >= limit:
                break
            if line.strip():
                entry = json.loads(line)
                if actor is None or entry.get("actor") == actor:
                    newest_first.append(entry)
        return newest_first[::-1]
