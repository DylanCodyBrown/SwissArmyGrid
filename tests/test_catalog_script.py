"""Script as (the catalog's right-click, like SSMS) and SQL completion of catalog names in Jupyter and
IPython, on a made-up bakery catalog."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from starlette.testclient import TestClient

from swissarmygrid.client import completion
from swissarmygrid.domain.catalogs import script
from tests.conftest import AGENT, AUTH
from tests.test_catalog_routes import load


def test_script_as_quotes_for_the_source_and_reads_with_the_blades_connection() -> None:
    star = script("select_star", "sqlserver", "", "sales", "orders", ["order id", "placed_at"])
    assert star["code"] == "SELECT *\nFROM [sales].[orders];" and star["language"] == "sql"
    listed = script("select_columns", "postgres", "", "sales", "orders", ["order id", "placed_at"])
    assert listed["code"] == 'SELECT\n    "order id",\n    "placed_at"\nFROM "sales"."orders";'
    python = script("pandas", "sqlserver", "", "sales", "order lines", ["qty"], blade="Bakery")
    assert python["imports"] == ["import pandas as pd", "import swissarmygrid as sag"]
    assert python["code"].startswith(
        'conn = sag.blade(\'Bakery\')\norder_lines = pd.read_sql(\n    """\nSELECT'
    )
    compile("\n".join([*python["imports"], python["code"]]), "<script>", "exec")  # it is Python
    no_blade = script("pandas", "sqlserver", "", "sales", "class", ["qty"])
    assert (
        no_blade["code"].startswith("# conn: a connection") and "t_class = pd.read_sql(" in no_blade["code"]
    )
    with pytest.raises(ValueError, match="Script as one of"):
        script("delete", "sqlserver", "", "s", "t", [])


def test_script_as_over_http_for_the_user_and_the_agent(client: TestClient) -> None:
    load(client)
    params = {"schema": "sales", "table": "orders", "as": "select_columns"}
    for headers in (AUTH, AGENT):
        made = client.get("/catalogs/Bakery/script", params=params, headers=headers)
        assert (
            made.status_code == 200
            and made.json()["code"] == "SELECT\n    [order id],\n    [placed_at]\nFROM [sales].[orders];"
        )
    missing = client.get("/catalogs/Bakery/script", params={**params, "table": "nowhere"}, headers=AUTH)
    assert missing.status_code == 404


@pytest.mark.parametrize(
    ("before", "inside"),
    [
        ('df = conn.sql("SELECT * FROM ord', True),
        ('rows = pd.read_sql("""\nSELECT kilos\nFROM sales.ord', True),
        ("%%sql\nSELECT * FROM ord", True),
        ('name = "hello wor', False),  # a string, but not SQL
        ('x = "SELECT 1"\ny = ord', False),  # the SQL string is closed
        ("SELECT = 3; ord", False),  # not in a string
    ],
)
def test_completion_only_inside_sql(before: str, inside: bool) -> None:
    assert (completion.sql_before(before) is not None) is inside


def test_completion_offers_tables_then_columns_of_the_named_table(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    load(client)
    monkeypatch.setattr(completion, "_get", lambda path: client.get(path, headers=AUTH).json())
    monkeypatch.setattr(completion, "_catalogs", (0.0, []))
    assert completion.suggestions("ord", "SELECT * FROM ") == ["orders", "order id"]
    assert completion.suggestions("orders.pl", "SELECT orders.pl") == ["orders.placed_at"]
    assert completion.suggestions("sales.da", "SELECT * FROM sales.da") == ["sales.daily"]
    assert completion.suggestions("k", "SELECT k FROM flour") == ["kilos"]


def test_completion_installs_into_ipythons_completer(monkeypatch: pytest.MonkeyPatch) -> None:
    class Completer:
        def __init__(self) -> None:
            self.custom_matchers: list[Any] = []
            self.text_until_cursor = 'conn.sql("SELECT * FROM ord'

    class Shell:
        def __init__(self) -> None:
            self.Completer = Completer()

    shell = Shell()
    fake = types.ModuleType("IPython")
    fake.get_ipython = lambda: shell  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "IPython", fake)
    monkeypatch.setattr(completion, "_installed", [])
    monkeypatch.setattr(completion, "suggestions", lambda token, sql: [f"{token}ers"])
    assert completion.complete() and completion.complete()  # once, however often it's called
    assert len(shell.Completer.custom_matchers) == 1
    assert shell.Completer.custom_matchers[0]("ord") == ["orders"]
    shell.Completer.text_until_cursor = "x = ord"
    assert shell.Completer.custom_matchers[0]("ord") == []
