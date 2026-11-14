"""To Python: code the page hands to the user's own Python, and sag.paste() taking it (in IPython it
fills the cell; elsewhere it prints). A grid's steps go only as code checked against the grid. The agent
never sends code into the user's Python."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from swissarmygrid.client import paste_client
from tests.conftest import AGENT, AUTH, grid_id, make_client, push


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with make_client(tmp_path) as test_client:
        yield test_client


def test_code_waits_newest_first_and_is_taken_once(client: TestClient) -> None:
    for n in (1, 2):
        sent = client.post(
            "/python/send",
            json={"code": f"x = {n}", "imports": ["import duckdb"], "label": f"try {n}"},
            headers=AUTH,
        )
        assert sent.status_code == 200 and sent.json()["waiting"] == n
    first = client.post("/python/take", headers=AUTH).json()
    assert first["code"] == "import duckdb\n\nx = 2" and first["label"] == "try 2"
    assert client.post("/python/take", headers=AUTH).json()["code"].endswith("x = 1")
    assert client.post("/python/take", headers=AUTH).status_code == 204
    assert [s["label"] for s in client.get("/python/sent", headers=AUTH).json()["sent"]] == ["try 2", "try 1"]
    for bad in ({"code": ""}, {"code": "x", "imports": ["os.system('x')"]}):
        assert client.post("/python/send", json=bad, headers=AUTH).status_code == 400


def test_the_agent_may_queue_code_for_the_users_python(client: TestClient) -> None:
    gid = grid_id(push(client, "orders", pa.table({"n": [1, 2]})))
    for method, path in (
        ("POST", "/python/send"),
        ("POST", "/python/take"),
        ("GET", "/python/sent"),
        ("POST", f"/grids/{gid}/python"),
    ):
        answer = client.request(method, path, json={"code": "x"}, headers=AGENT)
        assert answer.status_code != 403, path  # the golden rule


def test_a_grids_steps_go_as_checked_duckdb_code(client: TestClient) -> None:
    gid = grid_id(push(client, "orders", pa.table({"region": ["North", "South"], "n": [1, 2]})))
    step = {"kind": "filter", "params": {"column": "region", "op": "eq", "value": "North"}}
    assert (
        client.post(f"/grids/{gid}/step-ops", json={"op": "add", "step": step}, headers=AUTH).status_code
        == 200
    )
    sent = client.post(f"/grids/{gid}/python", headers=AUTH).json()
    assert (
        sent["code"].startswith("import duckdb")
        and "North" in sent["code"]
        and sent["label"] == "orders: its steps"
    )


def test_sag_paste_fills_the_ipython_cell_or_prints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers: list[Any] = [{"code": "x = 1"}, {"code": "y = 2"}, None]
    monkeypatch.setattr(paste_client, "_call", lambda method, path: answers.pop(0))
    filled: list[tuple[str, bool]] = []

    class Shell:
        def set_next_input(self, text: str, replace: bool = False) -> None:
            filled.append((text, replace))

    fake = types.ModuleType("IPython")
    fake.get_ipython = lambda: Shell()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "IPython", fake)
    assert paste_client.paste() == "x = 1" and filled == [("x = 1", True)]
    monkeypatch.delitem(sys.modules, "IPython")
    assert paste_client.paste() == "y = 2" and "y = 2" in capsys.readouterr().out
    assert paste_client.paste() is None and "Nothing waiting" in capsys.readouterr().out
