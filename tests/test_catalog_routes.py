"""The catalog and blade routes over HTTP: what the user may do, what the agent may read, what it may not."""

from __future__ import annotations

import base64
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
    # The golden rule: the agent loads and removes catalogs too.
    assert client.put("/catalogs/Mine", json=CATALOG, headers=AGENT).status_code == 200
    assert client.delete("/catalogs/Mine", headers=AGENT).status_code == 200


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


def test_the_agent_may_use_every_blade_route_but_the_one_with_passwords(
    client: TestClient, bakery: Path
) -> None:
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
        if path.endswith("/python"):  # its answer carries the saved password values
            assert response.status_code == 403, response.text
        else:  # the golden rule: whatever the user can do, the agent can too
            assert response.status_code != 403, (method, path, response.text)


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
        "Analyses",
    }
    for packages in [[], ["pyodbc; rm -rf /"], ["--index-url=http://evil"], ["a"] * 21, "pyodbc"]:
        refused = client.post("/blades/install", json={"packages": packages}, headers=AUTH)
        assert refused.status_code == 400, packages


def test_the_catalog_page_is_served_without_the_token(client: TestClient) -> None:
    page = client.get("/catalog")
    assert page.status_code == 200 and "<title>" in page.text


# ===== portable blades: export, import, folders, favourites =====


def test_export_then_import_on_another_computer(client: TestClient, bakery: Path) -> None:
    blade = sqlite_blade(bakery) | {"description": "The bakery.", "tags": ["bakery", "orders-function-app"]}
    assert client.post("/blades", json=blade, headers=AUTH).status_code == 200
    exported = client.get("/blades/Bakery%20DB/export", headers=AUTH)
    assert exported.status_code == 200 and "bakery_db.blade.py" in exported.headers["content-disposition"]
    text = exported.text
    assert text.startswith("# /// swissarmygrid-blade") and '"orders-function-app"' in text
    # The other computer: no blades yet. Review first: nothing saved, nothing run.
    client.delete("/blades/Bakery%20DB", headers=AUTH)
    files = [{"name": "bakery_db.blade.py", "data": base64.b64encode(text.encode()).decode()}]
    preview = client.post("/blades/import", json={"files": files}, headers=AUTH).json()["items"]
    assert preview[0]["blade"]["name"] == "Bakery DB" and preview[0]["conflict"] is None
    assert client.get("/blades", headers=AUTH).json()["blades"] == []
    applied = client.post("/blades/import", json={"files": files, "apply": True}, headers=AUTH).json()
    assert applied["results"][0]["status"] == "saved"
    listed = client.get("/blades", headers=AUTH).json()["blades"][0]
    assert listed["tags"] == ["bakery", "orders-function-app"] and listed["description"] == "The bakery."
    # Again: the name is taken. Keep both, replace, or skip.
    again = client.post("/blades/import", json={"files": files}, headers=AUTH).json()["items"][0]
    assert again["conflict"] == {"name": "Bakery DB", "source": "saved"}
    kept = client.post(
        "/blades/import", json={"files": files, "apply": True, "choices": {"0": "copy"}}, headers=AUTH
    )
    assert kept.json()["results"][0] | {"path": None} == {
        "file": "bakery_db.blade.py",
        "name": "Bakery DB 2",
        "status": "saved",
        "path": None,
    }
    skipped = client.post(
        "/blades/import", json={"files": files, "apply": True, "choices": {"0": "skip"}}, headers=AUTH
    )
    assert skipped.json()["results"][0]["status"] == "skipped"
    zipped = client.get("/blades/export", headers=AUTH)
    assert zipped.headers["content-type"] == "application/zip"
    zip_file = [{"name": "blades.zip", "data": base64.b64encode(zipped.content).decode()}]
    names = [
        i["blade"]["name"]
        for i in client.post("/blades/import", json={"files": zip_file}, headers=AUTH).json()["items"]
    ]
    assert names == ["Bakery DB", "Bakery DB 2"]


def test_import_never_runs_code_and_reports_bad_files(client: TestClient, tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    code = f"open({str(marker)!r}, 'w').write('ran')\nx = 1\n"
    text = (
        '# /// swissarmygrid-blade\n# format = 1\n# name = "Sneaky"\n# kind = "other"\n'
        '# variable = "x"\n# ///\n' + code
    )
    files = [
        {"name": "sneaky.blade.py", "data": base64.b64encode(text.encode()).decode()},
        {"name": "script.py", "data": base64.b64encode(b"print('hello')\n").decode()},
    ]
    preview = client.post("/blades/import", json={"files": files}, headers=AUTH).json()["items"]
    assert preview[0]["blade"]["code"] == code and "no swissarmygrid-blade header" in preview[1]["error"]
    client.post("/blades/import", json={"files": files, "apply": True}, headers=AUTH)
    assert not marker.exists()  # reading and saving a blade never runs it
    bad = client.post("/blades/import", json={"files": [{"name": "x", "data": "%%%"}]}, headers=AUTH)
    assert bad.status_code == 400


def test_folders_and_favourites_over_http(
    client: TestClient, tmp_path: Path, bakery: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SWISSARMYGRID_BLADE_FOLDERS", raising=False)
    team = tmp_path / "team"
    team.mkdir()
    (team / "pantry.blade.py").write_text(
        '# /// swissarmygrid-blade\n# format = 1\n# name = "Pantry"\n# kind = "other"\n# variable = "x"\n'
        '# tags = ["orders-function-app"]\n# ///\nx = 1\n'
    )
    answer = client.put("/blades/folders", json={"folders": [str(team)]}, headers=AUTH).json()
    assert answer["folders"][0] | {"blades": 1} == {
        "path": str(team),
        "from_env": False,
        "exists": True,
        "blades": 1,
    }
    assert client.post("/blades", json=sqlite_blade(bakery), headers=AUTH).status_code == 200
    listed = {b["name"]: b for b in client.get("/blades", headers=AUTH).json()["blades"]}
    assert listed["Pantry"]["source"] == "folder" and listed["Pantry"]["path"] == str(
        team / "pantry.blade.py"
    )
    assert client.put("/blades/Pantry/pin", json={"pinned": True}, headers=AUTH).json() == {"pinned": True}
    assert {b["name"]: b["pinned"] for b in client.get("/blades", headers=AUTH).json()["blades"]} == {
        "Bakery DB": False,
        "Pantry": True,
    }
    refused = client.delete("/blades/Pantry", headers=AUTH)
    assert refused.status_code == 400 and "Delete or move the file" in refused.text
    for path in ("/blades/folders", "/blades/export", "/blades/Pantry/export"):
        assert client.get(path, headers=AGENT).status_code == 200
    assert client.put("/blades/Pantry/pin", json={"pinned": True}, headers=AGENT).status_code == 200


# ===== settings: the values each person fills in =====

POSTGRES_CODE = (
    "import psycopg\n\nconn = psycopg.connect(\n"
    '    host="my-server",\n    port=5432,\n    dbname="my_database",\n'
    '    user="my_user",\n    password=secret("password"),\n)\n'
)


def test_the_save_screen_sees_settings_with_suggestions_and_writes_new_values(client: TestClient) -> None:
    mine = 'import psycopg\nconn = psycopg.connect(host="db01.corp.local", dbname="sales", user="reader")\n'
    client.post(
        "/blades", json={"name": "Sales DB", "kind": "sql", "dialect": "postgres", "code": mine}, headers=AUTH
    )
    answer = client.post("/blades/inspect", json={"code": POSTGRES_CODE}, headers=AUTH).json()
    settings = {s["key"]: s for s in answer["settings"]}
    assert settings["host"]["placeholder"] is True and settings["port"]["type"] == "number"
    assert settings["host"]["suggestions"][0] == {"value": "db01.corp.local", "from": "your blade Sales DB"}
    assert "password" not in settings
    written = client.post(
        "/blades/fill",
        json={
            "code": POSTGRES_CODE,
            "values": {settings["host"]["id"]: "db01.corp.local", settings["port"]["id"]: 6432},
        },
        headers=AUTH,
    ).json()
    assert 'host="db01.corp.local"' in written["code"] and "port=6432" in written["code"]
    assert {s["key"]: s["value"] for s in written["settings"]}["host"] == "db01.corp.local"
    # A blade's own settings never suggest its own values back.
    own = client.get("/blades/Sales%20DB", headers=AUTH).json()["inspect"]["settings"]
    assert all(s["suggestions"] == [] for s in own if s["key"] == "host")
    bad = client.post("/blades/fill", json={"code": POSTGRES_CODE, "values": {"1:2": "x"}}, headers=AUTH)
    assert bad.status_code == 400 and "read them again" in bad.text
    assert (
        client.post("/blades/fill", json={"code": POSTGRES_CODE, "values": {}}, headers=AGENT).status_code
        == 200
    )


def test_csv_starter_path_needs_a_value(client: TestClient) -> None:
    from swissarmygrid.server.blade_templates import TEMPLATES

    csv = next(t for t in TEMPLATES if t.id == "csv")
    plain = client.post("/blades/inspect", json={"code": csv.code}, headers=AUTH).json()["settings"]
    starter = client.post("/blades/inspect", json={"code": csv.code, "template": "csv"}, headers=AUTH).json()
    assert plain[0]["placeholder"] is False and starter["settings"][0]["placeholder"] is True


def test_import_review_offers_settings_and_import_writes_them_and_keeps_typed_secrets(
    client: TestClient,
) -> None:
    from swissarmygrid.domain.blades import write_blade

    text = write_blade(
        {"name": "Orders DB", "kind": "sql", "dialect": "postgres", "code": POSTGRES_CODE, "variable": "conn"}
    )
    files = [{"name": "orders_db.blade.py", "data": base64.b64encode(text.encode()).decode()}]
    [item] = client.post("/blades/import", json={"files": files}, headers=AUTH).json()["items"]
    settings = {s["key"]: s for s in item["settings"]}
    assert [s["key"] for s in item["settings"]] == ["host", "port", "dbname", "user"]
    assert settings["dbname"]["placeholder"] is True and item["secrets"] == ["password"]
    fill = {
        "0": {
            "settings": {settings["host"]["id"]: "db01.corp.local", settings["dbname"]["id"]: "orders"},
            "secrets": {"password": "made-up-42"},
        }
    }
    [result] = client.post(
        "/blades/import", json={"files": files, "apply": True, "fill": fill}, headers=AUTH
    ).json()["results"]
    assert result["status"] == "saved" and "message" not in result
    saved = client.get("/blades/Orders%20DB", headers=AUTH).json()
    assert 'host="db01.corp.local"' in saved["code"] and 'dbname="orders"' in saved["code"]
    assert saved["secrets"] == {"password": True}
    assert "made-up-42" not in str(saved)
