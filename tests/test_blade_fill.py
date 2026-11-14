"""A blade's settings: found in its code, flagged when still a placeholder, suggested, written back.

Made-up servers and paths only.
"""

from __future__ import annotations

import pytest

from swissarmygrid.domain.blades import BladeCodeError, FillError, is_placeholder
from swissarmygrid.domain.blades import apply_settings as apply
from swissarmygrid.domain.blades import detect_settings as detect
from swissarmygrid.domain.blades import suggest_settings as suggest
from swissarmygrid.server.blade_templates import TEMPLATES

POSTGRES = """import psycopg

conn = psycopg.connect(
    host="my-server",
    port=5432,
    dbname="my_database",
    user="my_user",
    password=secret("password"),
)
"""
ODBC = """import pyodbc

conn = pyodbc.connect(
    "Driver={ODBC Driver 18 for SQL Server};"
    "Server=my-server;"
    "Database=my_database;"
    "TrustServerCertificate=yes;"
)
"""


def by_key(code: str) -> dict[str, object]:
    return {s.key: s.value for s in detect(code)}


def test_keyword_settings_are_found_and_placeholders_flagged() -> None:
    found = detect(POSTGRES)
    assert [(s.name, s.key, s.value, s.placeholder) for s in found] == [
        ("server", "host", "my-server", True),
        ("port", "port", 5432, False),
        ("database", "dbname", "my_database", True),
        ("user", "user", "my_user", True),
    ]
    assert found[0].line == 4 and found[0].where == "keyword"


def test_a_connection_string_gives_each_part_and_skips_lookalike_keys() -> None:
    found = detect(ODBC)
    assert [(s.name, s.key, s.value) for s in found] == [
        ("driver", "Driver", "{ODBC Driver 18 for SQL Server}"),
        ("server", "Server", "my-server"),
        ("database", "Database", "my_database"),
        ("trust server certificate", "TrustServerCertificate", "yes"),  # a checkbox, and not "Server"
    ]
    assert [s.line for s in found] == [4, 5, 6, 7] and found[1].where == "connection string"
    assert found[3].to_json()["type"] == "flag"
    assert by_key('conn = pyodbc.connect("DSN=sales_reporting")') == {"DSN": "sales_reporting"}


def test_passwords_tokens_and_keys_are_never_settings() -> None:
    code = (
        'conn = connect(host="db01", password="hunter2", api_key="k", access_token="t")\n'
        'other = connect("Server=db01;PWD=hunter2;Database=sales;")\n'
        'password = "hunter2"\n'
    )
    values = [s.value for s in detect(code)]
    assert "hunter2" not in values and "k" not in values and "t" not in values
    assert values.count("db01") == 2 and "sales" in values


def test_top_level_names_paths_and_path_arguments() -> None:
    code = (
        "from pathlib import Path\n"
        'project = "my-project"\n'
        'path = Path(r"C:\\data\\orders.csv")\n'
        'conn = duckdb.connect(r"D:\\shop\\shop.duckdb", read_only=True)\n'
        "limit = 500\n"
        'mode = "fast"\n'
    )
    found = {s.key: (s.name, s.value) for s in detect(code)}
    assert found["project"] == ("project", "my-project")
    assert found["path"] == ("path", "C:\\data\\orders.csv")
    assert found["connect"] == ("path", "D:\\shop\\shop.duckdb")
    assert found["limit"] == ("limit", 500) and found["mode"] == ("mode", "fast")


def test_what_is_not_a_single_value_is_left_alone() -> None:
    code = (
        'host_name = "db"\n'
        'conn = connect(host=f"{host_name}.example", database="a" "b", user=b"bytes")\n'
        'note = """two\nlines"""\n'
        "flag = True\n"
    )
    assert by_key(code) == {"host_name": "db"}


@pytest.mark.parametrize(
    "value", ["my-server", "<your host>", "vault.example.com", "MY_DATABASE", "changeme"]
)
def test_placeholders(value: str) -> None:
    assert is_placeholder(value)


@pytest.mark.parametrize("value", ["db01.corp.local", "sales", "us-east-1", "hive", "mysql-reader"])
def test_real_values_are_not_placeholders(value: str) -> None:
    assert not is_placeholder(value)


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.id)
def test_every_setting_of_every_starter_can_be_changed_alone(template: object) -> None:
    code = template.code  # type: ignore[attr-defined]
    found = detect(code)
    for index, setting in enumerate(found):
        new: object = (
            6543 if isinstance(setting.value, int) else ("D:\\x\\y.db" if setting.name == "path" else "db01")
        )
        if setting.to_json()["type"] == "flag":  # a checkbox: turned the other way
            new = (
                (not setting.value)
                if isinstance(setting.value, bool)
                else {"yes": "no"}.get(str(setting.value), "yes")
            )
        changed = apply(code, {setting.id: new})
        after = detect(changed)
        assert len(after) == len(found)
        assert after[index].value == new and after[index].key == setting.key
        others = [(s.key, s.value) for i, s in enumerate(after) if i != index]
        assert others == [(s.key, s.value) for i, s in enumerate(found) if i != index]


def test_several_values_at_once_and_text_around_them_kept() -> None:
    found = {s.key: s.id for s in detect(ODBC)}
    changed = apply(ODBC, {found["Server"]: "db01.corp.local", found["Database"]: "sales"})
    assert '"Server=db01.corp.local;"' in changed and '"Database=sales;"' in changed
    assert "TrustServerCertificate=yes;" in changed
    found = {s.key: s.id for s in detect(POSTGRES)}
    changed = apply(POSTGRES, {found["host"]: "db01", found["port"]: "6432", found["user"]: 'o"neil'})
    assert 'host="db01"' in changed and "port=6432" in changed and 'user="o\\"neil"' in changed
    assert 'password=secret("password")' in changed


def test_a_raw_path_stays_raw_and_positions_survive_accents() -> None:
    code = '# Café orders, made up\npath = Path(r"C:\\data\\orders.csv")\n'
    [setting] = detect(code)
    changed = apply(code, {setting.id: "D:\\café\\orders.csv"})
    assert changed == '# Café orders, made up\npath = Path(r"D:\\café\\orders.csv")\n'
    # A value a raw string cannot hold (it ends in a backslash) is written as an escaped string.
    assert 'Path("D:\\\\exports\\\\")' in apply(code, {setting.id: "D:\\exports\\"})


def test_values_that_cannot_go_there_are_refused() -> None:
    found = {s.key: s.id for s in detect(ODBC)}
    with pytest.raises(FillError, match="cannot hold"):
        apply(ODBC, {found["Server"]: "db01;Database=other"})
    found = {s.key: s.id for s in detect(POSTGRES)}
    with pytest.raises(FillError, match="number"):
        apply(POSTGRES, {found["port"]: "fifty"})
    with pytest.raises(FillError, match="one line"):
        apply(POSTGRES, {found["host"]: "a\nb"})
    with pytest.raises(FillError, match="read them again"):
        apply(POSTGRES, {"1:2": "x"})
    with pytest.raises(BladeCodeError):
        detect("conn = (\n")


def test_suggestions_come_from_other_blades_then_the_environment() -> None:
    settings = detect(POSTGRES)
    others = [
        ("Sales DB", "postgres", 'conn = connect(host="db01.corp.local", dbname="sales")'),
        ("Stock DB", "postgres", 'conn = connect(host="db01.corp.local", dbname="stock")'),
        ("Old DB", "mysql", 'conn = connect(host="db02.corp.local", user="my_user")'),
    ]
    suggest(settings, others, {"PGHOST": "db03.corp.local", "PGUSER": "reader", "PGPASSWORD": "hunter2"})
    host, port, database, user = settings
    assert host.suggestions == [
        {"value": "db01.corp.local", "from": "your blade Sales DB, Stock DB"},
        {"value": "db02.corp.local", "from": "your blade Old DB"},
        {"value": "db03.corp.local", "from": "environment variable PGHOST"},
    ]
    assert [s["value"] for s in database.suggestions] == ["sales", "stock"]
    # A placeholder is never suggested; no password from the environment ever is.
    assert user.suggestions == [{"value": "reader", "from": "environment variable PGUSER"}]
    assert port.suggestions == []
    assert "hunter2" not in str(settings)


def test_a_named_instance_backslash_is_written_escaped_and_found_again() -> None:
    found = {s.key: s.id for s in detect(ODBC)}
    changed = apply(ODBC, {found["Server"]: "db01\\SQLEXPRESS"})
    assert '"Server=db01\\\\SQLEXPRESS;"' in changed  # in Python source: a doubled backslash
    again = {s.key: s for s in detect(changed)}
    assert again["Server"].value == "db01\\SQLEXPRESS"
    back = apply(changed, {again["Server"].id: "db02"})
    assert '"Server=db02;"' in back
