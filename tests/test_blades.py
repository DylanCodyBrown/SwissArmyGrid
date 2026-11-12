"""Blades: reading their code, running it and its catalog command, the packages they need, and every
starter template. Made-up data only (a pretend bakery)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, NamedTuple

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from swissarmygrid.blades import code as blade_code
from swissarmygrid.blades.packages import install_command, missing_module_hint, needed
from swissarmygrid.blades.run import BladeRunError, run_catalog, run_code, run_vault, table_rows
from swissarmygrid.engine.catalog import CatalogStore
from swissarmygrid.engine.database import open_database
from swissarmygrid.server.blade_templates import BY_ID, TEMPLATES
from tests.fake_keyring import memory_keyring

# ===== reading the code =====


def test_the_variable_is_the_last_value_the_code_makes() -> None:
    found = blade_code.read(
        "import pyodbc\nimport os as o\nhost = o.environ['HOST']\n"
        "conn = pyodbc.connect(host, password=secret('pw'))\n"
    )
    assert found.variable == "conn"
    assert found.secrets == ["pw"]
    assert found.imports == ["pyodbc", "os"]


def test_variables_inside_with_if_and_try_count_but_functions_and_imports_do_not() -> None:
    code = (
        "from pathlib import Path\n"
        "try:\n    import polars as engine\nexcept ImportError:\n    import pandas as engine\n"
        "if True:\n    folder = Path('.')\nelse:\n    folder = Path('/')\n"
    )
    assert blade_code.read(code).variable == "folder"
    assert blade_code.read(code + "def get(name):\n    return name\n").variable == "get"
    assert blade_code.read("import duckdb\n").variable is None


def test_bad_python_is_refused_with_its_line() -> None:
    with pytest.raises(blade_code.BladeCodeError, match="line 2"):
        blade_code.read("conn = 1\nconn = (\n")


def test_imports_already_in_a_script_are_not_added_again() -> None:
    script = "import pandas as pd, numpy\nfrom pathlib import Path, PurePath\nimport swissarmygrid\n"
    wanted = [
        "import pandas as pd",
        "import numpy",
        "from pathlib import Path",
        "import swissarmygrid as sag",  # `import swissarmygrid` does not give `sag`
        "import pyodbc",
    ]
    assert blade_code.missing_imports(script, wanted) == ["import swissarmygrid as sag", "import pyodbc"]
    assert blade_code.missing_imports("x = (", ["import os"]) == ["import os"]
    assert blade_code.import_lines("import os\nx = 1\nfrom a import (\n    b,\n)\n") == [
        "import os",
        "from a import (\n    b,\n)",
    ]


# ===== running =====


def test_run_code_gives_the_variable_and_secrets_never_leak_into_errors() -> None:
    value, _ = run_code("x = secret('pw') + '!'", "x", {"pw": "hunter22"})
    assert value == "hunter22!"
    with pytest.raises(BladeRunError) as caught:
        run_code("raise ValueError('login failed for hunter22')", "x", {"pw": "hunter22"}, "Warehouse")
    assert (
        "hunter22" not in str(caught.value) and "****" in str(caught.value) and "line 1" in str(caught.value)
    )
    with pytest.raises(BladeRunError, match="neither saved"):
        run_code("x = secret('missing')", "x", {})
    with pytest.raises(BladeRunError, match="made no variable named 'conn'"):
        run_code("y = 1", "conn", {})


def test_secret_falls_back_to_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKERY_TOKEN", "from-env")
    assert run_code("x = secret('BAKERY_TOKEN')", "x", {})[0] == "from-env"
    assert run_code("x = secret('BAKERY_TOKEN')", "x", {"BAKERY_TOKEN": "saved"})[0] == "saved"


def test_a_missing_driver_names_the_package_to_install() -> None:
    with pytest.raises(BladeRunError) as caught:
        run_code("import snowflake.connector\nconn = 1", "conn", {})
    assert caught.value.missing_module == "snowflake"
    assert "snowflake-connector-python" in missing_module_hint("snowflake.connector")


def test_sql_and_python_catalog_commands(tmp_path: Path) -> None:
    db = tmp_path / "bakery.sqlite"
    with sqlite3.connect(db) as setup:
        setup.execute("CREATE TABLE loaves (id INTEGER NOT NULL, name TEXT)")
    value, names = run_code(f"import sqlite3\nconn = sqlite3.connect({str(db)!r})", "conn", {})
    columns, rows = run_catalog(value, names, "sql", BY_ID["sqlite"].catalog_command or "", {}, "Bakery")
    assert "column_name" in columns and len(rows) == 2
    columns, rows = run_catalog(value, names, "python", "[{'table_name': 't', 'column_name': 'c'}]", {}, "B")
    assert (columns, rows) == (["table_name", "column_name"], [["t", "c"]])
    columns, _ = run_catalog(
        value, names, "python", "catalog = [{'table_name': 't', 'column_name': 'c'}]", {}, "B"
    )
    assert columns == ["table_name", "column_name"]
    with pytest.raises(BladeRunError, match="no table"):
        run_catalog(value, names, "sql", "CREATE TABLE more (x INT)", {}, "B")
    with pytest.raises(BladeRunError, match="gave nothing"):
        run_catalog(value, {}, "python", "x = 1", {}, "B")
    with pytest.raises(BladeRunError, match="database connection"):
        run_catalog("a path", {}, "sql", "SELECT 1", {}, "B")


def test_table_rows_reads_frames_arrow_and_records() -> None:
    assert table_rows(pd.DataFrame({"a": [1, None]})) == (["a"], [[1.0], [None]])
    assert table_rows(pa.table({"a": [1, 2]})) == (["a"], [[1], [2]])
    assert table_rows([{"a": 1}, {"b": 2}]) == (["a", "b"], [[1, None], [None, 2]])
    with pytest.raises(BladeRunError, match="came back as a int"):
        table_rows(3)


def test_a_vault_sets_its_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BAKERY_PASSWORD", raising=False)
    code = "store = {'warehouse-password': 's3cret!'}\ndef get(name):\n    return store.get(name)\n"
    values = run_vault(code, "get", {"BAKERY_PASSWORD": "warehouse-password"}, {})
    assert values == {"BAKERY_PASSWORD": "s3cret!"} and os.environ["BAKERY_PASSWORD"] == "s3cret!"
    with pytest.raises(BladeRunError, match="no secret named 'nope'"):
        run_vault(code, "get", {"X": "nope"}, {})
    with pytest.raises(BladeRunError, match="must be a function"):
        run_vault("get = 1", "get", {}, {})


# ===== packages =====


def test_packages_map_imports_to_pip_names_and_skip_the_standard_library() -> None:
    found = {
        p.pip: p
        for p in needed("import os, json\nimport snowflake.connector\nimport yaml\nimport mystery_lib\n")
    }
    assert set(found) == {"snowflake-connector-python", "PyYAML", "mystery_lib"}
    assert found["mystery_lib"].guessed and not found["PyYAML"].guessed
    assert not found["mystery_lib"].installed
    excel = {p.pip for p in needed("import pandas as pd\nframe = pd.read_excel('x.xlsx')\n")}
    assert excel == {"pandas", "openpyxl"}
    assert (
        install_command(["psycopg[binary]"], "/opt/sag python")
        == "uv pip install --python '/opt/sag python' 'psycopg[binary]'"
    )


# ===== the starter templates =====


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.id)
def test_every_template_reads_as_python_and_names_its_variable(template: object) -> None:
    from swissarmygrid.server.blade_templates import Template

    assert isinstance(template, Template)
    found = blade_code.read(template.code, template.label)
    assert found.variable == template.variable, (found.variable, found.assigned)
    if template.catalog_language == "python":
        blade_code.parse(template.catalog_command or "", "catalog")
    if template.catalog_language == "sql":
        assert "SELECT" in (template.catalog_command or "").upper()
    assert json.dumps(template.to_json())
    assert template.kind in ("sql", "other", "vault")


def test_there_are_twenty_databases_files_and_vaults() -> None:
    groups = [t.group for t in TEMPLATES]
    assert groups.count("Databases") >= 20 and groups.count("Files") >= 4 and groups.count("Vaults") >= 7


class Shown(NamedTuple):
    tables: list[str]
    columns: list[dict[str, Any]]


def _catalog_from(template_id: str, path: Path, store: CatalogStore) -> Shown:
    """Run a starter template on a made-up file, load its catalog, return what the tree shows."""
    template = BY_ID[template_id]
    code = re.sub(r'r"[^"]*"', lambda _: repr(str(path)), template.code, count=1)
    value, names = run_code(code, template.variable, {}, template.label)
    columns, rows = run_catalog(
        value, names, template.catalog_language or "", template.catalog_command or "", {}, "t"
    )
    cur = store.connection.cursor()
    store.load(cur, template_id, pa.Table.from_pylist([dict(zip(columns, r, strict=True)) for r in rows]))
    schemas = store.schemas(cur, template_id)
    first = schemas[0]["schema"]
    tables = store.tables(cur, template_id, first)["tables"]
    return Shown([t["table"] for t in tables], store.columns(cur, template_id, first, tables[0]["table"]))


@pytest.fixture
def store() -> CatalogStore:
    return CatalogStore(open_database("256MiB"), max_catalogs=20, max_rows=100_000)


def test_the_sqlite_and_duckdb_templates_catalog_a_real_database(tmp_path: Path, store: CatalogStore) -> None:
    sqlite_path = tmp_path / "bakery.sqlite"
    with sqlite3.connect(sqlite_path) as setup:
        setup.execute("CREATE TABLE loaves (id INTEGER NOT NULL, name TEXT)")
        setup.execute("CREATE VIEW crusty AS SELECT name FROM loaves")
    shown = _catalog_from("sqlite", sqlite_path, store)
    assert shown.tables == ["crusty", "loaves"]
    duck_path = tmp_path / "bakery.duckdb"
    with duckdb.connect(str(duck_path)) as setup:
        setup.execute("CREATE SCHEMA ovens; CREATE TABLE ovens.bakes (oven INTEGER, started TIMESTAMP)")
    shown = _catalog_from("duckdb", duck_path, store)
    assert shown.tables == ["bakes"]
    assert [(c["column"], c["type"]) for c in shown.columns] == [
        ("oven", "INTEGER"),
        ("started", "TIMESTAMP"),
    ]


def test_the_file_templates_catalog_real_files(tmp_path: Path, store: CatalogStore) -> None:
    frame = pd.DataFrame(
        {"loaf": ["rye", "spelt"], "grams": [800, 650], "baked": pd.to_datetime(["2024-01-02"] * 2)}
    )
    frame.to_csv(tmp_path / "loaves.csv", index=False)
    with pd.ExcelWriter(tmp_path / "budget.xlsx") as writer:
        frame.to_excel(writer, sheet_name="Jan", index=False)
        frame[["grams"]].to_excel(writer, sheet_name="Feb", index=False)
    pq.write_table(pa.Table.from_pandas(frame), tmp_path / "events.parquet")
    frame.to_json(tmp_path / "records.json", orient="records")
    assert _catalog_from("csv", tmp_path / "loaves.csv", store).tables == ["loaves"]
    excel = _catalog_from("excel", tmp_path / "budget.xlsx", store)
    assert excel.tables == ["Feb", "Jan"]
    assert [c["column"] for c in _catalog_from("parquet", tmp_path / "events.parquet", store).columns] == [
        "loaf",
        "grams",
        "baked",
    ]
    assert _catalog_from("json", tmp_path / "records.json", store).tables == ["records"]
    (tmp_path / "notes.md").write_text("not a table")
    folder = _catalog_from("folder", tmp_path, store)
    assert folder.tables == ["budget.xlsx", "events.parquet", "loaves.csv"]


def test_the_os_vault_template_reads_the_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WAREHOUSE_PASSWORD", raising=False)
    with memory_keyring() as vault:
        vault.set_password("my-service", "warehouse-password", "rye-bread-42")
        template = BY_ID["os_vault"]
        mapping = {env: name for env, name in template.vault_mapping}
        assert run_vault(template.code, template.variable, mapping, {}) == {
            "WAREHOUSE_PASSWORD": "rye-bread-42"
        }
