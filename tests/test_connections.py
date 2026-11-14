"""Connections: database and file blades, seen apart from blades, and the Add connection window's forms.
Made-up data only: a sqlite file and a CSV written into the test's own folder."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from swissarmygrid.domain.connections import connection_kind, picker
from tests.conftest import AGENT, AUTH
from tests.fake_keyring import memory_keyring


@pytest.fixture(autouse=True)
def vault() -> Iterator[None]:
    with memory_keyring():
        yield


def csv_connection(path: Path, name: str = "Orders file") -> dict[str, Any]:
    return {
        "name": name,
        "kind": "other",
        "template": "csv",
        "code": f'from pathlib import Path\npath = Path(r"{path}")',
    }


def test_databases_and_file_starters_are_connections_everything_else_is_a_blade() -> None:
    assert connection_kind("sql", "postgres") == "database"
    assert connection_kind("sql", None) == "database"  # hand-written database code is a connection too
    assert connection_kind("other", "excel") == "file"
    assert connection_kind("other", "blank") is None
    assert connection_kind("vault", None) is None and connection_kind("analysis", "summary") is None
    assert picker({"name": "path", "key": "path"}, "csv") == "file"
    assert picker({"name": "folder", "key": "folder"}, "folder") == "folder"
    assert picker({"name": "server", "key": "host"}, "postgres") is None


def test_the_types_are_forms_fields_passwords_and_the_code_underneath(client: TestClient) -> None:
    types = {t["id"]: t for t in client.get("/connections/types", headers=AUTH).json()["types"]}
    assert {"postgres", "sqlserver", "sqlite", "csv", "excel", "folder"} <= set(types)
    assert not {"blank", "summary", "azure"} & set(types)  # other code, analyses, vaults: still blades
    postgres = types["postgres"]
    assert postgres["connection"] == "database" and postgres["group"] == "Databases"
    assert "password" in postgres["secrets"]
    assert {f["name"] for f in postgres["fields"]} >= {"server", "database"}
    csv = types["csv"]
    assert csv["connection"] == "file"
    assert [f["picker"] for f in csv["fields"] if f["name"] == "path"] == ["file"]


def test_saved_connections_are_listed_apart_and_marked_on_the_blades_list(
    client: TestClient, tmp_path: Path
) -> None:
    (tmp_path / "orders.csv").write_text("order_id,amount\n1,10.5\n2,7\n", encoding="utf-8")
    saved = client.post("/connections", json=csv_connection(tmp_path / "orders.csv"), headers=AUTH)
    assert saved.status_code == 200, saved.text
    blade = {"name": "Weather API", "kind": "other", "code": "import json\nsession = json"}
    assert client.post("/blades", json=blade, headers=AUTH).status_code == 200
    listed = client.get("/connections", headers=AUTH).json()["connections"]
    assert [(c["name"], c["connection"], c["type"]) for c in listed] == [
        ("Orders file", "file", "A CSV file")
    ]
    marks = {b["name"]: b["connection"] for b in client.get("/blades", headers=AUTH).json()["blades"]}
    assert marks == {"Orders file": "file", "Weather API": None}
    form = client.get("/connections/Orders%20file", headers=AUTH).json()
    assert form["name"] == "Orders file" and [f["value"] for f in form["fields"]] == [
        str(tmp_path / "orders.csv")
    ]
    # Other code is a blade, not a connection.
    refused = client.post("/connections", json=blade | {"name": "Other"}, headers=AUTH)
    assert refused.status_code == 400 and "saved as a blade" in refused.text
    assert client.get("/connections/Weather%20API", headers=AUTH).status_code == 400


def test_test_runs_the_unsaved_form_with_the_typed_password_and_checks_paths(
    client: TestClient, tmp_path: Path
) -> None:
    there = client.post("/connections/test", json=csv_connection(tmp_path), headers=AUTH)
    assert there.status_code == 200, there.text
    assert there.json()["found"].startswith("a folder of")
    missing = client.post("/connections/test", json=csv_connection(tmp_path / "gone.csv"), headers=AUTH)
    assert missing.status_code == 422 and "is not there" in missing.text
    database = tmp_path / "shop.db"
    sqlite3.connect(database).close()
    form = {
        "name": "Shop",
        "kind": "sql",
        "dialect": "sqlite",
        "template": "sqlite",
        "code": (
            f'import sqlite3\nassert secret("password") == "pa55"\nconn = sqlite3.connect(r"{database}")'
        ),
    }
    typed = client.post("/connections/test", json=form | {"secrets": {"password": "pa55"}}, headers=AUTH)
    assert typed.status_code == 200, typed.text
    assert typed.json()["ok"] is True
    wrong = client.post("/connections/test", json=form | {"secrets": {"password": "nope"}}, headers=AUTH)
    assert wrong.status_code == 422 and "pa55" not in wrong.text and "nope" not in wrong.text
    assert client.get("/connections", headers=AUTH).json()["connections"] == []  # nothing was saved


def test_the_agent_is_refused(client: TestClient) -> None:
    for method, path in (
        ("GET", "/connections"),
        ("GET", "/connections/types"),
        ("POST", "/connections/test"),
        ("POST", "/connections"),
        ("GET", "/connections/Anything"),
    ):
        assert client.request(method, path, headers=AGENT, json={}).status_code == 403, path


def test_the_file_chooser_lists_folders_then_chosen_kinds_of_file(client: TestClient, tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    (shared / "data").mkdir(parents=True)
    (shared / ".hidden").mkdir()
    for name in ("orders.csv", "budget.xlsx", "notes.txt"):
        (shared / name).write_text("x", encoding="utf-8")
    found = client.get("/folders", params={"path": str(shared), "suffixes": ".csv,.xlsx"}, headers=AUTH)
    assert found.status_code == 200, found.text
    body = found.json()
    assert [f["name"] for f in body["folders"]] == ["data"]
    assert [f["name"] for f in body["files"]] == ["budget.xlsx", "orders.csv"]
    assert body["parent"] == str(tmp_path) and body["roots"]
    assert client.get("/folders", params={"path": str(shared / "gone")}, headers=AUTH).status_code == 404
    assert client.get("/folders", params={"suffixes": "csv"}, headers=AUTH).status_code == 400
    assert client.get("/folders", headers=AGENT).status_code == 403


def test_a_password_comes_from_the_keyring_an_environment_variable_or_a_vault(client: TestClient) -> None:
    from swissarmygrid.domain.connections import env_name_for, rename_secret

    code = 'import psycopg\nconn = psycopg.connect(host="h", password=secret("password"))'
    assert rename_secret(code, "password", "SHOP_PW").endswith('password=secret("SHOP_PW"))')
    assert env_name_for("Sales DB", "password") == "SALES_DB_PASSWORD"
    # An environment variable: the code asks for it by name.
    env = client.post(
        "/connections/source",
        json={"code": code, "secret": "password", "from": "env", "env": "SHOP_PW"},
        headers=AUTH,
    )
    assert env.status_code == 200, env.text
    assert 'secret("SHOP_PW")' in env.json()["code"]
    bad = client.post(
        "/connections/source",
        json={"code": code, "secret": "password", "from": "env", "env": "9 bad"},
        headers=AUTH,
    )
    assert bad.status_code == 400
    # A vault: one of its entries, or a new one added to the vault blade.
    vault = {
        "name": "Team vault",
        "kind": "vault",
        "code": "def get(name):\n    return 'made-up'",
        "variable": "get",
        "vault_mapping": [{"env": "WAREHOUSE_PASSWORD", "secret": "warehouse-pw"}],
    }
    assert client.post("/blades", json=vault, headers=AUTH).status_code == 200
    listed = client.get("/connections/vaults", headers=AUTH).json()["vaults"]
    assert listed == [
        {
            "name": "Team vault",
            "entries": [{"env": "WAREHOUSE_PASSWORD", "secret": "warehouse-pw"}],
            "read_only": False,
        }
    ]
    picked = client.post(
        "/connections/source",
        json={
            "code": code,
            "secret": "password",
            "from": "vault",
            "vault": "Team vault",
            "env": "WAREHOUSE_PASSWORD",
        },
        headers=AUTH,
    ).json()
    assert 'secret("WAREHOUSE_PASSWORD")' in picked["code"]
    added = client.post(
        "/connections/source",
        json={
            "code": code,
            "secret": "password",
            "from": "vault",
            "vault": "Team vault",
            "vault_secret": "shop-pw",
            "connection": "Shop",
        },
        headers=AUTH,
    ).json()
    assert 'secret("SHOP_PASSWORD")' in added["code"]
    entries = client.get("/connections/vaults", headers=AUTH).json()["vaults"][0]["entries"]
    assert {"env": "SHOP_PASSWORD", "secret": "shop-pw"} in entries
    # A saved connection that asks for the vault's variable runs the vault first (no password saved).
    saved = {
        "name": "Shop",
        "kind": "sql",
        "dialect": "postgres",
        "template": "postgres",
        "code": added["code"],
    }
    assert client.post("/connections", json=saved, headers=AUTH).status_code == 200
    shop = next(
        c for c in client.get("/connections", headers=AUTH).json()["connections"] if c["name"] == "Shop"
    )
    assert shop["vaults"] == ["Team vault"]
    assert client.post("/connections/source", json={}, headers=AGENT).status_code == 403
