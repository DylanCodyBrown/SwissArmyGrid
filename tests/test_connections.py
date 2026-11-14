"""Connections: database and file blades, seen apart from blades, and the Add connection window's forms.
Made-up data only: a sqlite file and a CSV written into the test's own folder."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from swissarmygrid.domain.blades import env_name_for_secret, every_secret
from swissarmygrid.domain.connections import connection_kind, opens_in_code, picker
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
    assert connection_kind("vault", "azure_key_vault") == "vault"  # a key vault is a connection too
    assert connection_kind("analysis", "summary") is None and connection_kind("secret", None) is None
    assert opens_in_code("file") and not opens_in_code("vault")  # no variable, no SQL cell: env vars
    assert picker({"name": "path", "key": "path"}, "csv") == "file"
    assert picker({"name": "folder", "key": "folder"}, "folder") == "folder"
    assert picker({"name": "server", "key": "host"}, "postgres") is None


def test_the_types_are_forms_fields_passwords_and_the_code_underneath(client: TestClient) -> None:
    types = {t["id"]: t for t in client.get("/connections/types", headers=AUTH).json()["types"]}
    assert {"postgres", "sqlserver", "sqlite", "csv", "excel", "folder"} <= set(types)
    assert not {"blank", "summary"} & set(types)  # other code and analyses: still blades
    azure = types["azure_key_vault"]  # a key vault is made here too: the passwords' source
    assert azure["connection"] == "vault" and azure["group"] == "Vaults" and azure["variable"] == "get"
    assert [f["name"] for f in azure["fields"]] == ["vault address"]
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


def test_the_agent_may_use_every_connection_route(client: TestClient) -> None:
    for method, path in (
        ("GET", "/connections"),
        ("GET", "/connections/types"),
        ("POST", "/connections/test"),
        ("POST", "/connections"),
        ("GET", "/connections/Anything"),
    ):
        assert client.request(method, path, headers=AGENT, json={}).status_code != 403, path


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
    assert client.get("/folders", headers=AGENT).status_code == 200  # the golden rule


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
            "type": "",
            "description": "",
            "entries": [{"env": "WAREHOUSE_PASSWORD", "secret": "warehouse-pw"}],
            "every": False,
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
    assert client.post("/connections/source", json={}, headers=AGENT).status_code == 400  # asked, not refused


def test_each_connection_has_a_name_in_code(client: TestClient, tmp_path: Path) -> None:
    from swissarmygrid.domain.connections import code_names

    assert code_names(["Sales DB", "orders", "Orders", "2024 Budget", "class", "Café"]) == {
        "Sales DB": "sales_db",
        "orders": "orders",
        "Orders": "orders_2",
        "2024 Budget": "c_2024_budget",
        "class": "class_",
        "Café": "cafe",
    }
    (tmp_path / "orders.csv").write_text("order_id\n1\n", encoding="utf-8")
    client.post("/connections", json=csv_connection(tmp_path / "orders.csv", "Orders file"), headers=AUTH)
    listed = client.get("/connections", headers=AUTH).json()["connections"]
    assert [(c["name"], c["code_name"]) for c in listed] == [("Orders file", "orders_file")]


def test_a_cell_that_names_a_connection_opens_it_first(monkeypatch: pytest.MonkeyPatch) -> None:
    from IPython.core.interactiveshell import InteractiveShell

    from swissarmygrid.client import blades_client, kernel_connections

    opened: list[str] = []

    def blade(name: str) -> Any:
        if name == "Broken":
            raise RuntimeError("the server said no")
        opened.append(name)
        return {"Sales DB": 41, "Orders file": "orders.csv"}[name]

    listed = [
        {"name": "Sales DB", "code_name": "sales_db"},
        {"name": "Orders file", "code_name": "orders_file"},
        {"name": "Broken", "code_name": "broken"},
    ]
    monkeypatch.setattr(blades_client, "_call", lambda method, path, *a: {"connections": listed})
    monkeypatch.setattr(blades_client, "blade", blade)
    monkeypatch.setattr(kernel_connections, "_names", [0.0, {}])
    shell: Any = InteractiveShell.instance()  # IPython's own methods carry no types
    try:
        assert kernel_connections.install(shell) and kernel_connections.install(shell)  # once
        assert shell.run_cell("answer = sales_db + 1").success
        assert shell.user_ns["answer"] == 42 and opened == ["Sales DB"]  # only what the cell used
        shell.user_ns["orders_file"] = "mine.csv"  # the user's own variable wins
        assert shell.run_cell("chosen = orders_file").success and shell.user_ns["chosen"] == "mine.csv"
        assert opened == ["Sales DB"]
        assert not shell.run_cell("broken").success  # NameError, after a line saying why
    finally:
        shell.ast_transformers.clear()
        InteractiveShell.clear_instance()


# A made-up vault in plain Python: the same shape as the Azure starter (get, names), no package needed.
MADE_UP_VAULT = """values = {"warehouse-password": "made-up-not-real", "api-key": "made-up", "3d-token": "x"}


def names():
    return list(values)


def get(name):
    return values[name]
"""


@pytest.mark.parametrize(
    ("secret", "env"),
    [
        ("SqlPassword", "SqlPassword"),  # the vault's own name, case kept
        ("Sql--Password", "Sql__Password"),  # Azure's section convention: -- in the vault, __ here
        ("warehouse-password", "warehouse_password"),
        ("op://Private/Warehouse/password", "Private_Warehouse_password"),
        ("3d-token", "_3d_token"),
        ("---", "SECRET"),
    ],
)
def test_a_secret_in_a_vault_is_a_variable_under_its_own_name(secret: str, env: str) -> None:
    assert env_name_for_secret(secret) == env


def test_every_secret_takes_its_own_name_but_a_rename_wins() -> None:
    found = every_secret(["SqlPassword", "api-key", "Sql--Password"], {"CRM_KEY": "api-key"})
    assert found == {"SqlPassword": "SqlPassword", "Sql__Password": "Sql--Password", "CRM_KEY": "api-key"}


@pytest.mark.parametrize("headers", [AUTH, AGENT], ids=["user", "agent"])
def test_a_vault_is_added_its_secrets_listed_by_name_and_mapped(
    client: TestClient, headers: dict[str, str]
) -> None:
    draft = {"name": "Team vault", "kind": "vault", "template": "azure_key_vault", "code": MADE_UP_VAULT}
    draft |= {"variable": "get"}
    listed = client.post("/connections/vaults/secrets", json=draft, headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json() == {
        "names": [
            {"secret": "3d-token", "env": "_3d_token"},
            {"secret": "api-key", "env": "api_key"},
            {"secret": "warehouse-password", "env": "warehouse_password"},
        ]
    }
    assert "made-up" not in listed.text  # names only: never a value
    # A vault whose code cannot list: the user types the names.
    plain = draft | {"code": "def get(name):\n    return 'made-up-not-real'"}
    assert client.post("/connections/vaults/secrets", json=plain, headers=headers).json() == {"names": None}
    # Saved with the secrets ticked: a connection like any other, listed with its entries.
    mapping = [{"env": "WAREHOUSE_PASSWORD", "secret": "warehouse-password"}]
    saved = client.post("/connections", json=draft | {"vault_mapping": mapping}, headers=headers)
    assert saved.status_code == 200, saved.text
    [listed_vault] = client.get("/connections", headers=headers).json()["connections"]
    assert listed_vault["connection"] == "vault" and listed_vault["entries"] == mapping
    assert listed_vault["code_name"] is None  # no variable in the Workbench: its secrets are env vars
    blades = {b["name"]: b["connection"] for b in client.get("/blades", headers=headers).json()["blades"]}
    assert blades == {"Team vault": "vault"}  # marked a connection on the blades list, as files are
    [vault] = client.get("/connections/vaults", headers=headers).json()["vaults"]
    assert vault["name"] == "Team vault" and vault["type"] == "Azure Key Vault"
    assert vault["entries"] == mapping
    form = client.get("/connections/Team%20vault", headers=headers).json()
    assert form["connection"] == "vault" and form["vault_mapping"] == mapping
    # Test reads it: the variables' names come back, never their values.
    tested = client.post("/blades/Team%20vault/test", json={}, headers=headers)
    assert tested.status_code == 200, tested.text
    assert tested.json()["variables"] == ["WAREHOUSE_PASSWORD"] and "made-up" not in tested.text
    again = client.post("/connections/vaults/secrets", json={"name": "Team vault"}, headers=headers)
    assert len(again.json()["names"]) == 3
    # A vault with nothing mapped is refused, plainly.
    empty = client.post("/connections", json=draft | {"name": "Empty", "vault_mapping": []}, headers=headers)
    assert empty.status_code == 400 and "at least one secret" in empty.text


def test_a_vault_may_load_every_secret_under_its_own_name(client: TestClient) -> None:
    vault: dict[str, Any] = {"name": "Team vault", "kind": "vault", "template": "azure_key_vault"}
    vault |= {"variable": "get"}
    vault |= {"code": MADE_UP_VAULT, "vault_all": True, "vault_mapping": []}  # nothing to tick
    saved = client.post("/connections", json=vault, headers=AGENT)  # the agent may (the golden rule)
    assert saved.status_code == 200, saved.text
    [listed] = client.get("/connections", headers=AUTH).json()["connections"]
    assert listed["every"] is True and listed["entries"] == []
    tested = client.post("/blades/Team%20vault/test", json={}, headers=AUTH).json()
    assert tested["variables"] == ["_3d_token", "api_key", "warehouse_password"]
    # A rename is the only entry kept; the rest take their own names each time.
    renamed = vault | {"vault_mapping": [{"env": "WAREHOUSE_PASSWORD", "secret": "warehouse-password"}]}
    assert client.put("/connections/Team%20vault", json=renamed, headers=AUTH).status_code == 200
    tested = client.post("/blades/Team%20vault/test", json={}, headers=AUTH).json()
    assert tested["variables"] == ["WAREHOUSE_PASSWORD", "_3d_token", "api_key"]
    # A password pointed at it uses the secret's own name, and the vault is left as it was.
    code = 'import psycopg\nconn = psycopg.connect(host="h", password=secret("password"))'
    body = {"code": code, "secret": "password", "from": "vault", "vault": "Team vault"}
    pointed = client.post("/connections/source", json=body | {"vault_secret": "api-key"}, headers=AUTH)
    assert 'secret("api_key")' in pointed.json()["code"]
    assert client.get("/connections/Team%20vault", headers=AUTH).json()["vault_all"] is True
    # Code with no names() cannot load every secret: Test says so plainly.
    plain = vault | {"name": "Plain", "code": "def get(name):\n    return 'made-up'\n"}
    assert client.post("/connections", json=plain, headers=AUTH).status_code == 200
    failed = client.post("/blades/Plain/test", json={}, headers=AUTH)
    assert failed.status_code == 422 and "no names()" in failed.text


def test_trino_links_a_pem_over_https_with_a_password() -> None:
    from swissarmygrid.domain.blades import detect_settings, read_code
    from swissarmygrid.server.blade_templates import TEMPLATES

    code = next(t for t in TEMPLATES if t.id == "trino").code
    found = {s.name: s for s in detect_settings(code)}
    assert set(found) == {"user", "server", "port", "catalog", "scheme", "certificate file"}
    assert found["scheme"].value == "https" and found["certificate file"].placeholder
    assert picker(found["certificate file"].to_json(), "trino") == "file"  # Browse… offers .pem files
    assert read_code(code).secrets == ["password"]  # the Password box: keyring, variable or vault
    # The .pem wins even where REQUESTS_CA_BUNDLE names another bundle (requests prefers that otherwise).
    assert "session.trust_env = False" in code and "http_session=session" in code


@pytest.mark.parametrize(
    ("code", "key"),
    [
        (
            'conn = psycopg.connect(host="h", sslrootcert=r"C:\\certs\\ca.pem")',
            "sslrootcert",
        ),
        ('conn = pymysql.connect(host="h", ssl_ca="/etc/ssl/company-ca.pem")', "ssl_ca"),
        ('conn = trino.dbapi.connect(host="h", verify="/etc/ssl/company-ca.pem")', "verify"),
    ],
)
def test_a_certificate_file_is_a_field_with_a_file_picker(code: str, key: str) -> None:
    from swissarmygrid.domain.blades import detect_settings

    [cert] = [s for s in detect_settings(code) if s.key == key]
    assert cert.name == "certificate file" and picker(cert.to_json(), None) == "file"
