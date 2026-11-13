"""Ports: what the service layer needs from outside, as protocols the adapters satisfy.

A use case names the ports it needs in its constructor (or its arguments); bootstrap.py hands it the
concrete adapters. Nothing here imports an adapter, so a test can pass a fake.

These are the ports several features share. A port only one feature uses sits beside that feature's
use cases: `TableReader` (datasets.py), `ExportWriter` (export.py), `Catalogs` (catalogs.py), `Blades`
(blades.py), `BladeShelf` (agent.py), `Notebook` (notebook.py), `ModelLibrary` (relationships.py).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from typing import Any, Protocol, TypeVar

from swissarmygrid.domain.codegen import Outcome, Pasted
from swissarmygrid.domain.datasets import Column, DatasetInfo
from swissarmygrid.domain.grids import Window
from swissarmygrid.domain.steps import CompiledView, Step, ViewSettings

T = TypeVar("T")

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


class Views(Protocol):
    """A dataset run through a grid's steps: the compiled view, its windows and counts, and the
    relation other queries read it by (the view cache, adapters/views.py)."""

    def compile(
        self, cursor: SqlSession, info: DatasetInfo, steps: list[Step], settings: ViewSettings
    ) -> CompiledView: ...

    def window(
        self,
        cursor: SqlSession,
        info: DatasetInfo,
        steps: list[Step],
        settings: ViewSettings,
        start: int,
        end: int,
        upto: int | None = None,
    ) -> Window: ...

    def row_count(
        self,
        cursor: SqlSession,
        info: DatasetInfo,
        steps: list[Step],
        settings: ViewSettings,
        upto: int | None = None,
    ) -> int: ...

    def positioned_sql(
        self,
        cursor: SqlSession,
        info: DatasetInfo,
        steps: list[Step],
        settings: ViewSettings,
        upto: int | None = None,
    ) -> tuple[str, tuple[Column, ...]]: ...

    def reading(
        self,
        cursor: SqlSession,
        info: DatasetInfo,
        steps: list[Step],
        settings: ViewSettings,
        upto: int | None = None,
        ordered: bool = False,
    ) -> AbstractContextManager[str]: ...

    def rows_sql(
        self,
        cursor: SqlSession,
        info: DatasetInfo,
        steps: list[Step],
        settings: ViewSettings,
        upto: int | None = None,
    ) -> str: ...

    def ordered_sql(
        self,
        cursor: SqlSession,
        info: DatasetInfo,
        steps: list[Step],
        settings: ViewSettings,
        upto: int | None = None,
    ) -> str: ...

    def step_counts(
        self, cursor: SqlSession, info: DatasetInfo, steps: list[Step], settings: ViewSettings
    ) -> list[int]: ...

    def cell(
        self,
        cursor: SqlSession,
        info: DatasetInfo,
        steps: list[Step],
        settings: ViewSettings,
        row: int,
        column: str,
    ) -> Any: ...

    def forget_dataset(self, name: str) -> None: ...


class CodeVerifier(Protocol):
    """Run copied code the way the user will, over `table` as it was pushed, and compare what it
    assigns with the rows the grid shows."""

    def verify(
        self,
        pasted: Pasted,
        session: SqlSession,
        table: str,
        expected_names: list[str],
        expected: list[list[Any]],
    ) -> Outcome: ...


class Datasets(Protocol):
    """The open datasets: each one a table in the locked database (adapters/datasets.py)."""

    @property
    def used_bytes(self) -> int: ...

    @property
    def max_total_bytes(self) -> int: ...

    @property
    def max_datasets(self) -> int: ...

    def replace(self, name: str, table: Any, source_kind: str, query: str | None = None) -> DatasetInfo: ...

    def close(self, name: str) -> bool: ...

    def get(self, name: str) -> DatasetInfo | None: ...

    def all(self) -> list[DatasetInfo]: ...

    def cursor(self) -> Any:
        """A new session with the engine (a `SqlSession`; `Any` here because the engine's own cursor
        has a wider `execute` than the protocol spells out)."""
        ...


class Jobs(Protocol):
    """The worker pool: run `fn(*args)` off the event loop; a lower priority number runs first."""

    async def run(self, priority: int, fn: Callable[..., T], *args: Any) -> T: ...


class Audit(Protocol):
    """The audit log: who did what to which dataset, never a cell value."""

    def write(
        self,
        actor: str,
        action: str,
        dataset: str | None = None,
        query: str | None = None,
        rows: int | None = None,
        **details: Any,
    ) -> None: ...

    def tail(self, limit: int = 200, actor: str | None = None) -> list[dict[str, Any]]: ...


class Notifier(Protocol):
    """The open pages and VS Code windows: tell them, and ask what they show."""

    async def broadcast(self, message: dict[str, Any]) -> None: ...

    async def dataset_updated(self, dataset: str, version: int) -> None: ...

    def shown_or_opening(self, grid: str) -> bool: ...

    async def ask_opener(self, url: str) -> bool: ...

    def page_open(self) -> bool: ...


class Dashboards(Protocol):
    """Saved Sandboxes: each name's versions, oldest first, each the cards as they were drawn."""

    def names(self) -> list[dict[str, Any]]: ...

    def read(self, name: str) -> dict[str, Any] | None: ...

    def add_version(self, name: str, cards: list[dict[str, Any]], saved: str) -> int: ...

    def delete(self, name: str) -> bool: ...


class ThemeShelf(Protocol):
    """The user's imported Sandbox themes, one file each (as themes.read_theme reads them)."""

    def all(self) -> list[dict[str, Any]]: ...

    def write(self, theme_id: str, file: dict[str, Any]) -> None: ...

    def delete(self, theme_id: str) -> bool: ...


class Recipes(Protocol):
    """Saved recipes: a dataset name's steps and settings, with no rows."""

    def save(self, dataset: str, steps: list[Step], settings: ViewSettings) -> str: ...

    def load(self, dataset: str) -> tuple[list[Step], ViewSettings] | None: ...

    def delete(self, dataset: str) -> bool: ...
