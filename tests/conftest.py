"""Shared fixtures: a full app on a fake port, with its state in a temp folder."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from starlette.testclient import TestClient

from swissarmygrid.server.app import create_app
from swissarmygrid.settings import Settings

PORT = 8765
TOKEN = "test-token"
BASE = f"http://127.0.0.1:{PORT}"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
AGENT = AUTH | {"X-Actor": "agent"}
ARROW = "application/vnd.apache.arrow.stream"


def make_client(tmp_path: Path, **overrides: Any) -> TestClient:
    settings = Settings(
        state_dir=tmp_path / "state",
        export_dir=tmp_path / "exports",
        # Never this machine's own Claude Code plugins: a test lays out its own packs here.
        claude_dir=overrides.pop("claude_dir", tmp_path / "claude"),
        max_total_bytes=overrides.pop("max_total_bytes", 512 * 1024 * 1024),
        # Catalogs loading themselves start child processes: off unless a test is about them.
        auto_load_catalogs=overrides.pop("auto_load_catalogs", False),
        **overrides,
    )
    return TestClient(create_app(PORT, TOKEN, settings), base_url=BASE)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with make_client(tmp_path) as test_client:
        yield test_client


def arrow_bytes(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def push(
    client: TestClient,
    name: str,
    table: pa.Table,
    query: str = "",
    headers: dict[str, str] | None = None,
) -> Any:
    return client.put(
        f"/datasets/{name}{query}",
        content=arrow_bytes(table),
        headers=(headers or AUTH) | {"Content-Type": ARROW, "X-Source-Kind": "arrow"},
    )


def push_json(client: TestClient, name: str, payload: Any) -> Any:
    return client.put(
        f"/datasets/{name}",
        content=json.dumps(payload).encode(),
        headers=AUTH | {"Content-Type": "application/json"},
    )


def grid_id(response: Any) -> str:
    assert response.status_code == 200, response.text
    return str(response.json()["grid"])
