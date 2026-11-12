"""Malformed requests get a plain 4xx, never a 500 with a traceback (found by fuzzing every route)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from tests.conftest import AGENT, AUTH, BASE, PORT, TOKEN, grid_id, make_client, push

TABLE = pa.table({"client": ["Acme", "Globex"], "balance": [1.0, 2.0]})


@pytest.fixture
def lenient(tmp_path: Path) -> Any:
    """A client that reports a server error as a 500 response instead of raising it."""
    from swissarmygrid.server.app import create_app
    from swissarmygrid.settings import Settings

    settings = Settings(state_dir=tmp_path / "state", export_dir=tmp_path / "exports")
    with TestClient(create_app(PORT, TOKEN, settings), base_url=BASE, raise_server_exceptions=False) as c:
        yield c


@pytest.mark.parametrize("limit", ["x", "1.5", "-5", "0", "99999999999999999999"])
def test_audit_limit_is_checked_and_bounded(lenient: TestClient, limit: str) -> None:
    for i in range(3):
        push(lenient, f"t{i}", TABLE)
    response = lenient.get(f"/audit?limit={limit}", headers=AUTH)
    assert response.status_code in (200, 400), response.text
    if response.status_code == 200:
        assert 1 <= len(response.json()["entries"]) <= 500


@pytest.mark.parametrize("body", [b"[]", b"null", b"1", b'"x"'])
@pytest.mark.parametrize(
    ("path", "headers"),
    [("/agent/policy", AUTH), ("/agent/policy/clients", AUTH), ("/agent/query", AGENT)],
)
def test_a_body_that_is_not_an_object_is_a_400(
    lenient: TestClient, path: str, headers: dict[str, str], body: bytes
) -> None:
    push(lenient, "clients", TABLE)
    response = lenient.post(path, content=body, headers=headers | {"Content-Type": "application/json"})
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("level", [True, 1.0, "1", None, -1, 4])
def test_the_privacy_level_must_be_a_whole_number_0_to_3(lenient: TestClient, level: Any) -> None:
    response = lenient.post("/agent/policy", json={"level": level}, headers=AUTH)
    assert response.status_code == 400
    assert lenient.get("/agent/policy", headers=AUTH).json()["level"] == 3


def test_auto_rows_must_be_a_boolean(lenient: TestClient) -> None:
    # bool("false") is True: a string must not switch on reads without a click.
    response = lenient.post("/agent/policy", json={"level": 2, "auto_rows": "false"}, headers=AUTH)
    assert response.status_code == 400
    assert lenient.get("/agent/policy", headers=AUTH).json()["auto_rows"] is False


def test_an_agent_cannot_stop_the_server(tmp_path: Path) -> None:
    stopped: list[bool] = []
    from swissarmygrid.server.app import create_app
    from swissarmygrid.settings import Settings

    settings = Settings(state_dir=tmp_path / "state", export_dir=tmp_path / "exports")
    app = create_app(PORT, TOKEN, settings, on_shutdown=lambda: stopped.append(True))
    with TestClient(app, base_url=BASE) as c:
        assert c.post("/shutdown", headers=AGENT).status_code == 403
        assert not stopped
        assert c.post("/shutdown", headers=AUTH).status_code == 200
        assert stopped


def test_json_bodies_are_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("swissarmygrid.server.context.MAX_JSON_BODY_BYTES", 1024)
    with make_client(tmp_path) as c:
        gid = grid_id(push(c, "clients", TABLE))
        big = b'{"steps": [], "pad": "' + b"x" * 4096 + b'"}'
        response = c.post(
            f"/grids/{gid}/steps", content=big, headers=AUTH | {"Content-Type": "application/json"}
        )
        assert response.status_code == 413
        small = c.post(f"/grids/{gid}/steps", json={"steps": []}, headers=AUTH)
        assert small.status_code == 200


def test_a_pasted_body_is_capped_like_a_push(tmp_path: Path) -> None:
    with make_client(tmp_path, max_push_body_bytes=1024) as c:
        response = c.post("/datasets/p/paste", content=b"a\tb\n" + b"1\t2\n" * 1000, headers=AUTH)
        assert response.status_code == 413
