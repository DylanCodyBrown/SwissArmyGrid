"""Ports: what the service layer needs from outside, as protocols the adapters satisfy.

A use case names the ports it needs in its constructor (or its arguments); bootstrap.py hands it the
concrete adapters. Nothing here imports an adapter, so a test can pass a fake.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

# Worker priorities: lower runs first.
INTERACTIVE: int = 0  # grid windows and counts
BACKGROUND: int = 5  # profiles, SQL box, exports
AGENT: int = 8  # MCP queries


class SqlSession(Protocol):
    """One conversation with the engine: run SQL, read rows. A DuckDB cursor is one."""

    @property
    def description(self) -> Sequence[Any] | None: ...

    def execute(self, query: Any, parameters: object = None, /) -> Any: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...

    def fetchall(self) -> list[Any]: ...

    def fetchmany(self, size: int = 1, /) -> list[Any]: ...

    def interrupt(self) -> None: ...
