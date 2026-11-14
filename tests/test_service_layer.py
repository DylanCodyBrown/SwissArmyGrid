"""The service layer on fakes: use cases run with no DuckDB, no files, no sockets and no HTTP.

Each service is given its ports (REFACTOR.md), so a test hands it small in-memory stand-ins and looks
at what the use case did: what it returned, what it told the pages, what it wrote to the audit log.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

import pytest

from swissarmygrid.domain.datasets import Column, DatasetInfo
from swissarmygrid.domain.grids import GridRegistry
from swissarmygrid.domain.relationships import Model, parse_model
from swissarmygrid.domain.sql_guard import SqlGrammar
from swissarmygrid.services.agent import AgentService, Approvals, ExportTickets
from swissarmygrid.services.grids import GridService
from swissarmygrid.services.processes import ProcessRegistry, Sessions
from swissarmygrid.services.relationships import Models
from swissarmygrid.services.snapshots import PNG_SIGNATURE, Snapshots, SnapshotUnanswered
from swissarmygrid.services.workspace import Addresses, Conflict, NotFound, Refused, Workspace
from swissarmygrid.settings import Settings

T = TypeVar("T")


class FakePages:
    """The `Notifier` port: remembers what the pages were told."""

    def __init__(self, open_page: bool = True) -> None:
        self.told: list[dict[str, Any]] = []
        self.open_page = open_page
        self.on_broadcast: Callable[[dict[str, Any]], None] = lambda message: None

    async def broadcast(self, message: dict[str, Any]) -> None:
        self.told.append(message)
        self.on_broadcast(message)

    async def dataset_updated(self, dataset: str, version: int) -> None:
        self.told.append({"type": "dataset_updated", "dataset": dataset, "version": version})

    def shown_or_opening(self, grid: str) -> bool:
        return True

    async def ask_opener(self, url: str) -> bool:
        return False

    def page_open(self) -> bool:
        return self.open_page

    def workbench_open(self) -> bool:
        return False

    async def tell_workbench(self, message: dict[str, Any]) -> int:
        return 0


class FakeAudit:
    """The `Audit` port: a list in memory."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, str | None, dict[str, Any]]] = []

    def write(
        self,
        actor: str,
        action: str,
        dataset: str | None = None,
        query: str | None = None,
        rows: int | None = None,
        **details: Any,
    ) -> None:
        self.entries.append((actor, action, dataset, details))

    def tail(self, limit: int = 200, actor: str | None = None) -> list[dict[str, Any]]:
        return []


class FakeStore:
    """The `Datasets` port: descriptions only, no rows."""

    used_bytes = 0
    max_total_bytes = 1000
    max_datasets = 5

    def __init__(self, *names: str) -> None:
        self.infos = {
            name: DatasetInfo(
                name=name,
                version=1,
                columns=(Column("id", "BIGINT"),),
                row_count=3,
                source_kind="test",
                nbytes=10,
            )
            for name in names
        }

    def replace(self, name: str, table: Any, source_kind: str, query: str | None = None) -> DatasetInfo:
        raise AssertionError("not used here")

    def close(self, name: str) -> bool:
        return self.infos.pop(name, None) is not None

    def get(self, name: str) -> DatasetInfo | None:
        return self.infos.get(name)

    def all(self) -> list[DatasetInfo]:
        return list(self.infos.values())

    def cursor(self) -> Any:
        return None


class InlineJobs:
    """The `Jobs` port: runs the work where it stands."""

    async def run(self, priority: int, fn: Callable[..., T], *args: Any) -> T:
        return fn(*args)


def workspace(tmp_path: Any, *datasets: str, pages: FakePages | None = None, **limits: Any) -> Workspace:
    views: Any = None  # no use case below reads a view
    return Workspace(
        settings=Settings(state_dir=tmp_path, export_dir=tmp_path / "exports", **limits),
        addresses=Addresses(4321, "the-token"),
        store=FakeStore(*datasets),
        views=views,
        grids=GridRegistry(10),
        live=pages or FakePages(),
        workers=InlineJobs(),
        audit=FakeAudit(),
        grammar=SqlGrammar(parse=lambda sql: {}, table_functions=frozenset(), keywords=frozenset()),
        interrupted=KeyboardInterrupt,
        engine_error=RuntimeError,
    )


def audit_of(ws: Workspace) -> list[tuple[str, str, str | None, dict[str, Any]]]:
    assert isinstance(ws.audit, FakeAudit)
    return ws.audit.entries


def told(ws: Workspace) -> list[dict[str, Any]]:
    assert isinstance(ws.live, FakePages)
    return ws.live.told


def test_opening_a_grid_tells_the_pages_and_keeps_the_token_from_the_agent(tmp_path: Any) -> None:
    ws = workspace(tmp_path, "orders")
    grids = GridService(ws, recipes=None)  # type: ignore[arg-type]

    made = asyncio.run(grids.create("orders", "agent"))

    assert made["shown"] is True
    assert made["grid_url"] == f"http://127.0.0.1:4321/g/{made['grid']}"  # an agent gets no token
    assert told(ws) == [{"type": "grid_shown", "grid": made["grid"], "dataset": "orders"}]
    assert audit_of(ws) == [("agent", "open_grid", "orders", {"grid": made["grid"]})]
    assert asyncio.run(grids.create("orders", "user"))["grid_url"].endswith("#token=the-token")
    with pytest.raises(NotFound, match="Name an open dataset"):
        asyncio.run(grids.create("nothing", "user"))
    assert asyncio.run(grids.close(made["grid"])) == {"closed": made["grid"]}
    assert asyncio.run(grids.close(made["grid"])) is None


def test_an_approval_waits_for_the_users_answer(tmp_path: Any) -> None:
    pages = FakePages()
    ws = workspace(tmp_path, "orders", pages=pages)
    approvals = Approvals(pages)
    tickets = ExportTickets()
    agent = AgentService(ws, approvals, tickets, blades=None, templates=[], packages=lambda code: [])  # type: ignore[arg-type]
    grids = GridService(ws, recipes=None)  # type: ignore[arg-type]

    async def scenario(answer: bool) -> Any:
        made = await grids.create("orders", "user")
        grid, info = ws.grid_and_info(made["grid"])

        def click(message: dict[str, Any]) -> None:  # the page shows the question; the user clicks
            if message["type"] == "approval_requested":
                assert approvals.pending() == [{k: message[k] for k in ("id", "dataset", "action", "detail")}]
                waiting = approvals.get(message["id"])
                assert waiting is not None
                agent.resolve_approval(waiting, {"approve": answer})

        pages.on_broadcast = click
        return grid, await agent.approve_export(grid, info, "csv")

    grid, approved = asyncio.run(scenario(True))
    assert approved["approved"] is True
    assert tickets.redeem(approved["ticket"], grid.id) is True
    assert tickets.redeem(approved["ticket"], grid.id) is False  # one click, one file
    assert ("user", "approval", "orders", {"approved": True, "request": "export"}) in audit_of(ws)
    assert approvals.pending() == []
    with pytest.raises(Refused, match="declined"):
        asyncio.run(scenario(False))
    with pytest.raises(ValueError, match="format must be one of"):
        asyncio.run(agent.approve_export(*ws.grid_and_info(grid.id), "exe"))


def test_a_snapshot_is_the_pages_answer_or_a_plain_refusal(tmp_path: Any) -> None:
    pages = FakePages()
    ws = workspace(tmp_path, pages=pages, snapshot_timeout_seconds=0.05)
    snapshots = Snapshots(ws)
    png = PNG_SIGNATURE + b"picture"

    def draw(message: dict[str, Any]) -> None:  # the page draws itself and uploads
        waiting = snapshots.waiting(message["id"])
        assert waiting is not None
        assert snapshots.answer(waiting, png, {"width": "800"}) is True
        assert snapshots.answer(waiting, png, {}) is False  # a second answer comes too late

    pages.on_broadcast = draw
    assert asyncio.run(snapshots.take("grid", 2, "agent")) == (png, {"width": "800"})
    assert audit_of(ws) == [("agent", "snapshot", None, {"area": "grid", "scale": 2, "bytes": len(png)})]

    pages.on_broadcast = lambda message: None  # a page that never answers
    with pytest.raises(SnapshotUnanswered, match="did not answer within 0 seconds"):
        asyncio.run(snapshots.take("app", 1, "agent"))
    with pytest.raises(ValueError, match="area must be one of"):
        asyncio.run(snapshots.take("desktop", 1, "agent"))
    pages.open_page = False
    with pytest.raises(Conflict, match="No SwissArmyGrid page is open"):
        asyncio.run(snapshots.take("app", 1, "agent"))


class FakeLibrary:
    """The `ModelLibrary` port: two models, no files."""

    def __init__(self, *models: Model) -> None:
        self.models = models

    def folders(self) -> list[str]:
        return ["shelf"]

    def read(self) -> tuple[dict[str, tuple[Model, str]], list[str]]:
        return {m.name: (m, f"shelf/{m.name}.json") for m in self.models}, ["broken.json: not a model"]

    def shared_folders(self) -> list[str]:
        return []

    def where(self, path: str) -> str:
        return "mine"

    def save(self, model: Model, folder: str | None) -> str:
        self.models = (*self.models, model)
        return f"shelf/{model.name}.tang"


def test_the_relationship_model_merges_and_announces_itself(tmp_path: Any) -> None:
    ws = workspace(tmp_path, "orders", "customers")
    join = {"from": "orders", "from_columns": ["customer_id"], "to": "customers", "to_columns": ["id"]}
    bakery = parse_model({"name": "bakery", "relationships": [join]})
    models = Models(ws, FakeLibrary(bakery))

    with pytest.raises(ValueError, match="No relationships yet"):
        models.erd(set())
    listed = models.library()
    assert listed["folders"] == ["shelf"] and listed["problems"] == ["broken.json: not a model"]
    assert listed["models"][0]["open_tables"] == ["customers", "orders"]
    with pytest.raises(NotFound, match="No tang named 'cellar'"):
        asyncio.run(models.apply_library("cellar", "user"))

    current = asyncio.run(models.apply_library("bakery", "agent"))
    assert len(current["relationships"]) == 1
    assert told(ws) == [{"type": "relationships_updated"}]
    assert audit_of(ws) == [("agent", "relationships", None, {"model": "bakery"})]
    assert "orders" in models.erd(set())["mermaid"]


def test_a_python_session_reports_listens_and_is_dropped_when_it_goes_quiet(tmp_path: Any) -> None:
    clock = [100.0]
    pages = FakePages()
    sessions = Sessions(ProcessRegistry(ttl_seconds=30, clock=lambda: clock[0]), FakeStore("orders"), pages)
    report = {
        "label": "analysis.ipynb",
        "pid": 7,
        "tables": [{"name": "orders", "kind": "pandas", "rows": 3, "columns": 1}],
    }

    async def scenario() -> None:
        assert await sessions.sync("kernel-1", report) == {"following": []}
        assert pages.told == [{"type": "processes_changed"}]
        assert await sessions.sync("kernel-1", report) == {"following": []}  # a heartbeat: nothing new
        assert len(pages.told) == 1
        assert await sessions.requests("someone-else", 0) is None
        assert sessions.open_table("kernel-1", "orders") is True
        assert sessions.open_table("kernel-1", "missing") is False
        assert await sessions.requests("kernel-1", 0) == [{"action": "load", "name": "orders"}]
        clock[0] += 31
        await sessions.drop_silent()
        assert pages.told[-1] == {"type": "processes_changed"} and len(pages.told) == 2
        assert sessions.listing() == []

    asyncio.run(scenario())
