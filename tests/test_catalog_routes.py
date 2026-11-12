"""The catalog and blade routes over HTTP: what the user may do, what the agent may read, what it may not."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.conftest import AGENT, AUTH
from tests.fake_keyring import memory_keyring

CATALOG = {
    "columns": ["TABLE_SCHEMA", "TABLE_NAME", "TABLE_TYPE", "COLUMN_NAME", "DATA_TYPE", "ORDINAL_POSITION"],
    "data": [
        ["sales", "orders", "BASE TABLE", "order id", "int", 1],
        ["sales", "orders", "BASE TABLE", "placed_at", "datetime2", 2],
        ["sales", "daily", "VIEW", "day", "date", 1],
        ["stock", "flour", "BASE TABLE", "kilos", "decimal", 1],
    ],
}


@pytest.fixture(autouse=True)
def vault() -> Iterator[None]:
    with memory_keyring():
        yield


def load(client: TestClient, name: str = "Bakery", dialect: str = "sqlserver") -> Any:
    response = client.put(f"/catalogs/{name}?dialect={dialect}", json=CATALOG, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


# ===== catalogs =====


def test_load_browse_and_search_a_catalog(client: TestClient) -> None:
    loaded = load(client)
    assert loaded["catalog"]["columns"] == 4 and "#token=" in loaded["url"]
    assert [c["name"] for c in client.get("/catalogs", headers=AUTH).json()["catalogs"]] == ["Bakery"]
    schemas = client.get("/catalogs/Bakery/schemas", headers=AUTH).json()["schemas"]
    assert [(s["schema"], s["tables"], s["views"]) for s in schemas] == [("sales", 1, 1), ("stock", 1, 0)]
    tables = client.get("/catalogs/Bakery/tables", params={"schema": "sales"}, headers=AUTH).json()
    assert [(t["table"], t["kind"]) for t in tables["tables"]] == [("daily", "view"), ("orders", "table")]
    columns = client.get(
        "/catalogs/Bakery/columns", params={"schema": "sales", "table": "orders"}, headers=AUTH
    )
    assert columns.json()["select"] == "SELECT TOP 100 [order id], [placed_at]\nFROM [sales].[orders]"
    found = client.get("/catalogs/bakery/search", params={"q": "kilo"}, headers=AUTH).json()
    assert [c["column"] for c in found["columns"]] == ["kilos"]
    assert client.delete("/catalogs/Bakery", headers=AUTH).status_code == 200
    assert client.get("/catalogs/Bakery/schemas", headers=AUTH).status_code == 404


def test_catalog_mistakes_are_plain(client: TestClient) -> None:
    bad = client.put("/catalogs/Bakery", json={"columns": ["x"], "data": [[1]]}, headers=AUTH)
    assert bad.status_code == 400 and "column_name" in bad.text
    assert client.put("/catalogs/Bakery?dialect=cobol", json=CATALOG, headers=AUTH).status_code == 400
    load(client)
    missing = client.get("/catalogs/Bakery/columns", params={"schema": "sales"}, headers=AUTH)
    assert missing.status_code == 400 and "Name the table" in missing.text
    nowhere = client.get("/catalogs/Bakery/columns", params={"schema": "x", "table": "y"}, headers=AUTH)
    assert nowhere.status_code == 404


def test_the_agent_reads_catalogs_but_never_loads_one(client: TestClient) -> None:
    load(client)
    assert client.get("/catalogs", headers=AGENT).status_code == 200
    assert client.get("/catalogs/Bakery/schemas", headers=AGENT).status_code == 200
    found = client.get("/catalogs/Bakery/search", params={"q": "order id"}, headers=AGENT)
    assert found.status_code == 200 and found.json()["columns"][0]["column"] == "order id"
    assert client.put("/catalogs/Mine", json=CATALOG, headers=AGENT).status_code == 403
    assert client.delete("/catalogs/Bakery", headers=AGENT).status_code == 403


def test_catalogs_stay_out_of_the_sql_box(client: TestClient) -> None:
    load(client)
    pushed = client.put("/datasets/loaves", json={"columns": ["a"], "data": [[1]]}, headers=AUTH)
    grid = pushed.json()["grid"]
    for sql in ["SELECT * FROM __sag_catalog", "SELECT * FROM information_schema.tables"]:
        response = client.post(f"/grids/{grid}/sql", json={"sql": sql}, headers=AUTH)
        assert response.status_code == 400, response.text


# ===== blades =====


def sqlite_blade(path: Path, extra: str = "") -> dict[str, Any]:
    return {
        "name": "Bakery DB",
        "kind": "sql",
        "dialect": "sqlite",
        "code": f"import sqlite3\n{extra}conn = sqlite3.connect({str(path)!r})\n",
        "catalog": {
            "language": "sql",
            "command": "SELECT m.name AS table_name, p.name AS column_name, p.type AS data_type, "
            "p.cid + 1 AS ordinal_position FROM sqlite_master m JOIN pragma_table_info(m.name) p",
        },
    }


@pytest.fixture
def bakery(tmp_path: Path) -> Path:
    path = tmp_path / "bakery.sqlite"
    with sqlite3.connect(path) as setup:
        setup.execute("CREATE TABLE loaves (id INTEGER, name TEXT)")
    return path


def test_save_refresh_and_browse_a_blade(client: TestClient, bakery: Path) -> None:
    saved = client.post("/blades", json=sqlite_blade(bakery), headers=AUTH)
    assert saved.status_code == 200, saved.text
    assert saved.json()["has_catalog"] and not saved.json()["catalog_loaded"]
    refreshed = client.post("/blades/Bakery%20DB/refresh", headers=AUTH)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["catalog"] | {"loaded_at": 0} == {
        "name": "Bakery DB",
        "dialect": "sqlite",
        "source": "blade",
        "loaded_at": 0,
        "databases": 1,
        "schemas": 1,
        "tables": 1,
        "views": 0,
        "columns": 2,
    }
    columns = client.get(
        "/catalogs/Bakery%20DB/columns", params={"schema": "", "table": "loaves"}, headers=AUTH
    )
    assert columns.json()["select"] == 'SELECT "id", "name"\nFROM "loaves"\nLIMIT 100'
    listed = client.get("/blades", headers=AUTH).json()["blades"]
    assert listed[0]["catalog_loaded"] and "code" not in listed[0]
    detail = client.get("/blades/Bakery%20DB", headers=AUTH).json()
    assert detail["code"].startswith("import sqlite3") and detail["inspect"]["variable"] == "conn"
    test = client.post("/blades/Bakery%20DB/test", headers=AUTH)
    assert test.json() == {"ok": True, "type": "sqlite3.Connection"}


def test_secrets_are_set_but_never_read_back_except_by_sag_blade(client: TestClient, bakery: Path) -> None:
    blade = sqlite_blade(bakery, "password = secret('password')\n") | {"secrets": {"password": "seeded-rye"}}
    assert client.post("/blades", json=blade, headers=AUTH).status_code == 200
    detail = client.get("/blades/Bakery%20DB", headers=AUTH)
    assert detail.json()["secrets"] == {"password": True} and "seeded-rye" not in detail.text
    python = client.get("/blades/Bakery%20DB/python", headers=AUTH).json()
    assert python["secrets"] == {"password": "seeded-rye"} and python["variable"] == "conn"
    forgot = client.put("/blades/Bakery%20DB/secrets", json={"password": None}, headers=AUTH)
    assert forgot.json() == {"secrets": {"password": False}}


def test_the_agent_is_refused_every_blade_route(client: TestClient, bakery: Path) -> None:
    assert client.post("/blades", json=sqlite_blade(bakery), headers=AUTH).status_code == 200
    for method, path in [
        ("GET", "/blades"),
        ("GET", "/blades/templates"),
        ("GET", "/blades/Bakery%20DB"),
        ("GET", "/blades/Bakery%20DB/python"),
        ("POST", "/blades/Bakery%20DB/refresh"),
        ("POST", "/blades/Bakery%20DB/test"),
        ("PUT", "/blades/Bakery%20DB/secrets"),
        ("DELETE", "/blades/Bakery%20DB"),
        ("POST", "/blades"),
        ("POST", "/blades/inspect"),
        ("POST", "/blades/install"),
    ]:
        response = client.request(method, path, json={}, headers=AGENT)
        assert response.status_code == 403, (method, path, response.text)
        assert "sqlite" not in response.text


def test_a_secret_blade_value_is_asked_for_each_refresh(client: TestClient, bakery: Path) -> None:
    login = {"name": "Oven login", "kind": "secret", "env_names": ["OVEN_PASSWORD"]}
    assert client.post("/blades", json=login, headers=AUTH).status_code == 200
    blade = sqlite_blade(bakery, "assert secret('OVEN_PASSWORD') == 'typed-now'\n")
    assert client.post("/blades", json=blade, headers=AUTH).status_code == 200
    asked = client.post("/blades/Bakery%20DB/refresh", headers=AUTH)
    assert asked.status_code == 409 and asked.json()["needs"] == {"OVEN_PASSWORD": "Oven login"}
    typed = client.post(
        "/blades/Bakery%20DB/refresh", json={"env": {"OVEN_PASSWORD": "typed-now"}}, headers=AUTH
    )
    assert typed.status_code == 200, typed.text
    assert client.post("/blades/Bakery%20DB/refresh", headers=AUTH).status_code == 409  # never kept


def test_a_failing_refresh_says_why_and_what_to_install(client: TestClient) -> None:
    blade = {
        "name": "Snow",
        "kind": "sql",
        "code": "import snowflake.connector\nconn = 1\n",
        "catalog": {"language": "sql", "command": "SELECT 1"},
    }
    assert client.post("/blades", json=blade, headers=AUTH).status_code == 200
    failed = client.post("/blades/Snow/refresh", headers=AUTH)
    assert failed.status_code == 422
    assert failed.json()["missing"] == "snowflake-connector-python"
    assert "uv pip install --python" in failed.json()["install_here"]


def test_inspect_templates_and_install_checks(client: TestClient) -> None:
    seen = client.post(
        "/blades/inspect",
        json={"code": "import pandas as pd\nimport yaml\ndf = pd.read_excel('x')\n"},
        headers=AUTH,
    ).json()
    assert seen["variable"] == "df" and {p["pip"] for p in seen["packages"]} == {
        "pandas",
        "PyYAML",
        "openpyxl",
    }
    assert seen["install_yours"].startswith("uv pip install ")
    broken = client.post("/blades/inspect", json={"code": "x = ("}, headers=AUTH).json()
    assert "line 1" in broken["error"]
    templates = client.get("/blades/templates", headers=AUTH).json()["templates"]
    assert len(templates) >= 30 and {t["group"] for t in templates} == {
        "Databases",
        "Files",
        "Vaults",
        "Other",
    }
    for packages in [[], ["pyodbc; rm -rf /"], ["--index-url=http://evil"], ["a"] * 21, "pyodbc"]:
        refused = client.post("/blades/install", json={"packages": packages}, headers=AUTH)
        assert refused.status_code == 400, packages


def test_the_catalog_page_is_served_without_the_token(client: TestClient) -> None:
    page = client.get("/catalog")
    assert page.status_code == 200 and "<title>" in page.text
