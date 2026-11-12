"""`sag.load`, `sag.blade` and `sag.blades` against a real server on a free port. Made-up data only."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import uvicorn

import swissarmygrid as sag
from swissarmygrid.server.app import create_app
from swissarmygrid.settings import Settings
from tests.fake_keyring import memory_keyring

TOKEN = "blades-client-token"


@pytest.fixture
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    with memory_keyring():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])
        settings = Settings(state_dir=tmp_path / "state", export_dir=tmp_path / "exports")
        server = uvicorn.Server(
            uvicorn.Config(create_app(port, TOKEN, settings), port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        info = tmp_path / "server.json"
        info.write_text(json.dumps({"port": port, "token": TOKEN}))
        monkeypatch.setenv("SWISSARMYGRID_INFO", str(info))
        monkeypatch.setenv("SWISSARMYGRID_OPEN", "0")
        yield port
        server.should_exit = True
        thread.join(10)


def user(port: int, method: str, path: str, body: Any = None) -> Any:
    import urllib.request

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (loopback)
        return json.loads(response.read() or b"null")


@pytest.fixture
def bakery(tmp_path: Path) -> Path:
    path = tmp_path / "bakery.sqlite"
    with sqlite3.connect(path) as setup:
        setup.execute("CREATE TABLE loaves (id INTEGER, name TEXT)")
        setup.execute("INSERT INTO loaves VALUES (1, 'rye')")
    return path


def test_load_a_catalog_from_an_information_schema_frame(live: int) -> None:
    frame = pd.DataFrame(
        {
            "TABLE_SCHEMA": ["sales", "sales"],
            "TABLE_NAME": ["orders", "orders"],
            "COLUMN_NAME": ["order_id", "placed_at"],
            "DATA_TYPE": ["int", "datetime2"],
        }
    )
    url = sag.load(frame, name="Bakery warehouse", dialect="sqlserver")
    assert url.startswith(f"http://127.0.0.1:{live}/catalog#token=") and url.endswith(
        "catalog=Bakery%20warehouse"
    )
    assert [c["name"] for c in user(live, "GET", "/catalogs")["catalogs"]] == ["Bakery warehouse"]
    assert sag.load([{"table_name": "t", "column_name": "c"}], name="records")
    with pytest.raises(RuntimeError, match="column_name"):
        sag.load(pd.DataFrame({"x": [1]}), name="bad")


def test_blade_runs_saved_code_here_with_its_saved_secret(live: int, bakery: Path) -> None:
    blade = {
        "name": "Bakery",
        "kind": "sql",
        "code": "import sqlite3\nassert secret('password') == 'crusty'\n"
        f"conn = sqlite3.connect({str(bakery)!r})\n",
        "secrets": {"password": "crusty"},
    }
    user(live, "POST", "/blades", blade)
    assert sag.blades() == ["Bakery"]
    conn = sag.blade("bakery")
    assert conn.execute("SELECT name FROM loaves").fetchall() == [("rye",)]
    with pytest.raises(LookupError):
        sag.blade("nope")


def test_a_missing_driver_says_what_to_install(live: int) -> None:
    user(
        live,
        "POST",
        "/blades",
        {"name": "Snow", "kind": "sql", "code": "import snowflake.connector\nconn = 1\n"},
    )
    with pytest.raises(ImportError, match="uv pip install snowflake-connector-python"):
        sag.blade("Snow")


@pytest.fixture
def clean_env() -> Iterator[None]:
    """sag.blade sets variables in this process on purpose; take them away after the test."""
    names = ("PANTRY_KEY", "OVEN_PASSWORD")
    for name in names:
        os.environ.pop(name, None)
    yield
    for name in names:
        os.environ.pop(name, None)


@pytest.mark.usefixtures("clean_env")
def test_vault_and_secret_blades_set_variables_and_hand_back_names_only(
    live: int, bakery: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user(
        live,
        "POST",
        "/blades",
        {
            "name": "Pantry",
            "kind": "vault",
            "code": "shelf = {'pantry-key': 'cinnamon-7'}\ndef get(name):\n    return shelf[name]\n",
            "vault_mapping": [{"env": "PANTRY_KEY", "secret": "pantry-key"}],
        },
    )
    user(live, "POST", "/blades", {"name": "Oven login", "kind": "secret", "env_names": ["OVEN_PASSWORD"]})
    assert sag.blade("Pantry") == ["PANTRY_KEY"] and os.environ["PANTRY_KEY"] == "cinnamon-7"

    asked: list[str] = []

    def typed(prompt: str) -> str:
        asked.append(prompt)
        return "proofing-box"

    monkeypatch.setattr("getpass.getpass", typed)
    assert sag.blade("Oven login") == ["OVEN_PASSWORD"] and os.environ["OVEN_PASSWORD"] == "proofing-box"
    assert sag.blade("Oven login") == ["OVEN_PASSWORD"] and len(asked) == 1  # still set: not asked again
    del os.environ["OVEN_PASSWORD"], os.environ["PANTRY_KEY"]  # the kernel restarted

    # A blade that needs both: the vault runs first, the secret blade's value is asked for again.
    code = (
        f"import sqlite3\nassert secret('PANTRY_KEY') == 'cinnamon-7'\n"
        f"assert secret('OVEN_PASSWORD') == 'proofing-box'\nconn = sqlite3.connect({str(bakery)!r})\n"
    )
    user(live, "POST", "/blades", {"name": "Bakery", "kind": "sql", "code": code})
    assert sag.blade("Bakery").execute("SELECT count(*) FROM loaves").fetchall() == [(1,)]
    assert len(asked) == 2
