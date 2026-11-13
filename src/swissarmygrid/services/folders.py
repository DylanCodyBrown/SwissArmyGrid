"""The page's file chooser: a folder's contents (adapters/folders.py). The user's alone: the agent is
refused at the door, as it is for blades and connections."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_MAX_SUFFIXES: int = 20


class Folders:
    def __init__(self, listing: Callable[[str | None, set[str]], dict[str, Any]]) -> None:
        self._listing = listing

    def browse(self, where: str | None, suffixes: str | None) -> dict[str, Any]:
        """`suffixes` as the page sends them: ".csv,.xlsx" (empty: every file)."""
        wanted = {s.strip().lower() for s in (suffixes or "").split(",") if s.strip()}
        if len(wanted) > _MAX_SUFFIXES or any(not s.startswith(".") or len(s) > 12 for s in wanted):
            raise ValueError("Name the file kinds as .csv,.xlsx (at most 20).")
        return self._listing(where or None, wanted)
