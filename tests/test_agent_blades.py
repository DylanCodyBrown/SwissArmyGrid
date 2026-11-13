"""The agent and blades: it lists them (never their code), reads the starters, and proposes new ones
that are saved only on the user's Allow (the owner, 2026-11-12). Made-up blades only."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.conftest import AGENT, AUTH
from tests.fake_keyring import memory_keyring

BAKERY = {
    "name": "Bakery DB",
    "kind": "sql",
    "dialect": "sqlite",
    "code": "import sqlite3\nconn = sqlite3.connect('file:bakery.db?mode=ro', uri=True)\n",
    "description": "The bakery's made-up database",
    "tags": ["bakery", "orders-function-app"],
    "catalog": {"language": "sql", "command": "SELECT name AS table_name FROM sqlite_master"},
}


@pytest.fixture(autouse=True)
def vault() -> Any:
    with memory_keyring():
        yield


def answer_when_asked(client: TestClient, approve: bool, seen: list[dict[str, Any]]) -> threading.Thread:
    """Stands in for the user at the page: waits for the request, keeps it, then answers it."""

    def run() -> None:
        for _ in range(200):
            pending = client.get("/agent/approvals", headers=AUTH).json()["pending"]
            if pending:
                seen.extend(pending)
                client.post(f"/agent/approvals/{pending[0]['id']}", json={"approve": approve}, headers=AUTH)
                return
            time.sleep(0.05)

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def audit(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (tmp_path / "state" / "audit.log").read_text().splitlines()]


def test_the_agent_lists_blades_without_their_code_or_secrets(client: TestClient) -> None:
    code = "conn = secret('password')\n"
    client.post(
        "/blades", json={"name": "Ovens", "kind": "other", "code": code, "tags": ["ovens"]}, headers=AUTH
    )
    [listed] = client.get("/agent/blades", headers=AGENT).json()["blades"]
    assert listed["name"] == "Ovens" and listed["tags"] == ["ovens"]
    assert listed["use"] == 'conn = sag.blade("Ovens")'
    assert "code" not in listed and "secrets" not in listed and "catalog" not in listed
    assert "password" not in json.dumps(listed)
    # Its own blade routes stay refused to the agent.
    assert client.get("/blades/Ovens", headers=AGENT).status_code == 403
    assert client.get("/blades", headers=AGENT).status_code == 403


def test_the_agent_reads_the_starter_blades(client: TestClient) -> None:
    starters = client.get("/agent/blade-templates", headers=AGENT).json()["templates"]
    assert len(starters) == 38 and all("code" not in t for t in starters)
    one = client.get("/agent/blade-templates", params={"id": starters[0]["id"]}, headers=AGENT).json()
    assert one["code"] and one["kind"] == starters[0]["kind"]
    assert client.get("/agent/blade-templates", params={"id": "nope"}, headers=AGENT).status_code == 404


def test_a_blade_the_agent_proposes_is_saved_only_after_the_user_reads_it_and_allows(
    client: TestClient, tmp_path: Path
) -> None:
    seen: list[dict[str, Any]] = []
    helper = answer_when_asked(client, True, seen)
    response = client.post("/agent/blades", json=BAKERY, headers=AGENT)
    helper.join()
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["saved"] == "Bakery DB" and answer["use"] == 'conn = sag.blade("Bakery DB")'
    # What the user read: the blade's whole file, its code included, and that it runs when used.
    [request] = seen
    assert request["action"] == "save the new blade" and request["dataset"] == "Bakery DB"
    assert "# /// swissarmygrid-blade" in request["detail"]
    assert "sqlite3.connect('file:bakery.db?mode=ro', uri=True)" in request["detail"]
    assert "runs on this computer whenever the blade is used" in request["detail"]
    saved = client.get("/blades/Bakery%20DB", headers=AUTH).json()
    assert (
        saved["code"] == BAKERY["code"] and saved["tags"] == BAKERY["tags"] and saved["dialect"] == "sqlite"
    )
    assert saved["catalog"] == BAKERY["catalog"] and saved["source"] == "saved"
    entries = audit(tmp_path)
    assert {"actor": "agent", "action": "create_blade"}.items() <= next(
        e for e in entries if e["action"] == "create_blade"
    ).items()
    decision = next(e for e in entries if e["action"] == "approval")
    assert decision["approved"] is True and decision["request"] == "save the new blade"


def test_a_declined_blade_is_not_saved(client: TestClient) -> None:
    helper = answer_when_asked(client, False, [])
    response = client.post("/agent/blades", json=BAKERY, headers=AGENT)
    helper.join()
    assert response.status_code == 403 and "declined" in response.text
    assert client.get("/agent/blades", headers=AGENT).json()["blades"] == []


def test_the_agent_never_sends_a_secret_never_picks_a_folder_and_never_replaces_a_blade(
    client: TestClient, tmp_path: Path
) -> None:
    with_value = BAKERY | {"code": "conn = secret('password')\n", "secrets": {"password": "hunter2"}}
    response = client.post("/agent/blades", json=with_value, headers=AGENT)
    assert response.status_code == 400 and "never sends a secret" in response.text
    client.post("/blades", json={"name": "Bakery DB", "kind": "other", "code": "conn = 1\n"}, headers=AUTH)
    response = client.post("/agent/blades", json=BAKERY | {"name": "bakery db"}, headers=AGENT)
    assert response.status_code == 409 and "cannot change the user's blades" in response.text
    assert client.get("/blades/Bakery%20DB", headers=AUTH).json()["code"] == "conn = 1\n"
    # A folder (or path) from the agent is ignored: the blade is kept by the app, nowhere else.
    folder = tmp_path / "team"
    folder.mkdir()
    client.put("/blades/folders", json={"folders": [str(folder)]}, headers=AUTH)
    helper = answer_when_asked(client, True, [])
    other = BAKERY | {"name": "Bakery copy", "folder": str(folder), "path": str(folder / "x.blade.py")}
    response = client.post("/agent/blades", json=other, headers=AGENT)
    helper.join()
    assert response.status_code == 200, response.text
    assert client.get("/blades/Bakery%20copy", headers=AUTH).json()["path"] is None
    assert list(folder.iterdir()) == []
    assert client.get("/agent/approvals", headers=AUTH).json()["pending"] == []


def test_a_broken_blade_is_refused_before_the_user_is_asked(client: TestClient) -> None:
    for broken, says in [
        (BAKERY | {"code": "conn = (\n"}, "Python"),
        (BAKERY | {"code": "import sqlite3\n"}, "no variable"),
        (BAKERY | {"kind": "spell"}, "kind"),
    ]:
        response = client.post("/agent/blades", json=broken, headers=AGENT)
        assert response.status_code == 400 and says in response.text, response.text
    assert client.get("/agent/approvals", headers=AUTH).json()["pending"] == []


def test_the_secrets_it_asks_for_are_named_for_the_user_to_type(client: TestClient) -> None:
    seen: list[dict[str, Any]] = []
    helper = answer_when_asked(client, True, seen)
    blade = {"name": "Warehouse", "kind": "other", "code": "conn = (secret('user'), secret('password'))\n"}
    answer = client.post("/agent/blades", json=blade, headers=AGENT).json()
    helper.join()
    assert answer["secrets_to_type"] == ["password", "user"]
    assert "It asks for: password, user" in seen[0]["detail"]
    assert client.get("/blades/Warehouse", headers=AUTH).json()["secrets"] == {
        "password": False,
        "user": False,
    }
