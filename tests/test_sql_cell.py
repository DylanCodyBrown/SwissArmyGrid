"""The SQL cell (%%sql <connection>) on each kind of connection, with made-up data: a SQLite database, a
CSV file, a workbook, a folder of files, and a blade that makes a DataFrame. The server is stood in for
(the connections list and what sag.blade() would hand back), and IPython by a small shell."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from swissarmygrid.client import blades_client, sql_cell

CONNECTIONS: list[dict[str, Any]] = [
    {"name": "Shop", "kind": "sql", "connection": "database", "dialect": "sqlite", "template": "sqlite"},
    {"name": "Orders file", "kind": "other", "connection": "file", "template": "csv"},
    {"name": "Budget", "kind": "other", "connection": "file", "template": "excel"},
    {"name": "Exports", "kind": "other", "connection": "file", "template": "folder"},
]
BLADES: list[dict[str, Any]] = [
    {"name": "Weather", "kind": "other", "variable": "readings", "template": None},
    {"name": "Office vault", "kind": "vault", "template": None},
]


@pytest.fixture
def made(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Each connection's object, as its code would make it; and a clean slate of open connections."""
    shop = tmp_path / "shop.db"
    with sqlite3.connect(shop) as db:
        db.execute("CREATE TABLE orders (order_id INTEGER, region TEXT, amount REAL)")
        db.executemany(
            "INSERT INTO orders VALUES (?, ?, ?)", [(1, "North", 10.5), (2, "South", 7.0), (3, "North", 2.5)]
        )
    csv = tmp_path / "data" / "orders.csv"
    csv.parent.mkdir()
    csv.write_text("order_id,amount\n1,10.5\n2,7\n", encoding="utf-8")
    book = tmp_path / "budget.xlsx"
    with pd.ExcelWriter(book) as writer:
        pd.DataFrame({"region": ["North", "South"], "amount": [100, 80]}).to_excel(
            writer, sheet_name="Forecast", index=False
        )
        pd.DataFrame({"region": ["North"], "amount": [90]}).to_excel(writer, sheet_name="Actual", index=False)
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "stores.csv").write_text("store_id,city\n1,Springfield\n2,Riverton\n", encoding="utf-8")
    pd.DataFrame({"day": [1, 2]}).to_parquet(exports / "days.parquet")
    objects: dict[str, Any] = {
        "Shop": sqlite3.connect(shop, check_same_thread=False),
        "Orders file": csv,
        "Budget": book,
        "Exports": exports,
        "Weather": pd.DataFrame({"city": ["Springfield", "Riverton"], "rain_mm": [3.5, 0.0]}),
        "Office vault": ["WAREHOUSE_PASSWORD"],
    }

    def call(method: str, path: str, *_: Any) -> Any:
        return {"connections": CONNECTIONS} if path == "/connections" else {"blades": CONNECTIONS + BLADES}

    monkeypatch.setattr(blades_client, "_call", call)
    monkeypatch.setattr(blades_client, "blade", lambda name: objects[name])
    monkeypatch.setattr(sql_cell, "_connections", {})
    monkeypatch.setattr(sql_cell, "_default", [])
    yield objects


def test_the_first_line_names_the_connection_a_result_name_and_the_grid() -> None:
    assert sql_cell.parse_line("Warehouse") == sql_cell.CellLine("Warehouse", None, False)
    assert sql_cell.parse_line('"Budget workbook" -> plan --grid') == sql_cell.CellLine(
        "Budget workbook", "plan", True
    )
    assert sql_cell.parse_line("My warehouse") == sql_cell.CellLine("My warehouse", None, False)
    assert sql_cell.parse_line("  -> late") == sql_cell.CellLine(None, "late", False)
    for bad in ("Shop -> 2x", "Shop -> a b", "Shop --limit 5", 'Shop "open'):
        with pytest.raises(sql_cell.SqlCellError):
            sql_cell.parse_line(bad)


def test_a_database_runs_the_cell_in_its_own_sql_and_commits_a_change(made: dict[str, Any]) -> None:
    result = sql_cell.sql(
        "shop", "SELECT region, sum(amount) AS total FROM orders GROUP BY region ORDER BY region;"
    )
    assert result.to_dict("records") == [
        {"region": "North", "total": 13.0},
        {"region": "South", "total": 7.0},
    ]
    assert sql_cell.sql("Shop", "UPDATE orders SET amount = 0 WHERE region = 'South'") == "1 row(s) changed."
    other = sqlite3.connect(made["Shop"].execute("PRAGMA database_list").fetchone()[2])
    assert other.execute("SELECT amount FROM orders WHERE region = 'South'").fetchone() == (0.0,)  # committed


def test_a_csv_file_is_a_table_named_after_it(made: dict[str, Any]) -> None:
    result = sql_cell.sql("Orders file", "SELECT sum(amount) AS total FROM orders")
    assert result["total"].tolist() == [17.5]
    # also as its catalog names it: the folder, then the file
    assert sql_cell.sql("Orders file", 'SELECT count(*) AS n FROM "data"."orders"')["n"].tolist() == [2]
    assert sql_cell.tables(sql_cell.connection("Orders file")) == ["orders", "data.orders"]


def test_a_workbooks_sheets_are_tables_and_a_saved_change_is_read_again(made: dict[str, Any]) -> None:
    joined = sql_cell.sql(
        "Budget",
        'SELECT f.region, f.amount - a.amount AS gap FROM "Forecast" f JOIN "Actual" a USING (region)',
    )
    assert joined.to_dict("records") == [{"region": "North", "gap": 10}]
    assert sql_cell.sql("Budget", 'SELECT count(*) AS n FROM budget."Forecast"')["n"].tolist() == [2]
    with pd.ExcelWriter(made["Budget"]) as writer:  # the workbook saved again, with a new row
        pd.DataFrame({"region": ["North", "South", "East"], "amount": [1, 2, 3]}).to_excel(
            writer, sheet_name="Forecast", index=False
        )
        pd.DataFrame({"region": ["North"], "amount": [90]}).to_excel(writer, sheet_name="Actual", index=False)
    import os

    os.utime(made["Budget"], (1, 1))  # a different modified time, however fast the test runs
    assert sql_cell.sql("Budget", 'SELECT count(*) AS n FROM "Forecast"')["n"].tolist() == [3]


def test_a_folders_files_are_tables(made: dict[str, Any]) -> None:
    found = sql_cell.connection("Exports")
    assert set(sql_cell.tables(found)) >= {"stores", "days", "exports.stores.csv", "exports.days.parquet"}
    assert sql_cell.sql("Exports", "SELECT city FROM stores ORDER BY store_id")["city"].tolist() == [
        "Springfield",
        "Riverton",
    ]
    assert sql_cell.columns(found)["stores"] == ["store_id", "city"]


def test_a_blade_that_makes_a_table_is_queried_by_its_variable_name(made: dict[str, Any]) -> None:
    result = sql_cell.sql("Weather", "SELECT city FROM readings WHERE rain_mm > 0")
    assert result["city"].tolist() == ["Springfield"]


def test_what_cannot_run_sql_says_why(made: dict[str, Any], tmp_path: Path) -> None:
    with pytest.raises(sql_cell.SqlCellError, match="vault blade, not a connection"):
        sql_cell.connection("Office vault")
    with pytest.raises(sql_cell.SqlCellError, match="No connection is named"):
        sql_cell.connection("Nowhere")
    with pytest.raises(sql_cell.SqlCellError, match="choose one first"):
        sql_cell.connection(None)
    made["Orders file"] = tmp_path / "gone.csv"
    with pytest.raises(sql_cell.SqlCellError, match="is not there"):
        sql_cell.connection("Orders file")


class Shell:
    """IPython as far as the magics need it."""

    def __init__(self, taken: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.user_ns: dict[str, Any] = {}
        self.cells: dict[str, Any] = {name: object() for name in taken}
        self.lines: dict[str, Any] = {}

    def find_cell_magic(self, name: str) -> Any:
        return self.cells.get(name)

    def find_line_magic(self, name: str) -> Any:
        return self.lines.get(name)

    def register_magic_function(self, fn: Any, magic_kind: str, magic_name: str) -> None:
        (self.cells if magic_kind == "cell" else self.lines)[magic_name] = fn


def test_the_magics_run_a_cell_keep_the_result_and_choose_a_default(
    made: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    shell = Shell()
    assert sql_cell.register(shell) == [
        "%%sag",
        "%%sql",
        "%sag_connect",
        "%sag_connections",
        "%sql_connect",
        "%sql_connections",
    ]
    assert sql_cell.register(shell) == []  # once
    shown = shell.cells["sql"]("Shop -> by_region", "SELECT region FROM orders ORDER BY order_id")
    assert shown["region"].tolist() == ["North", "South", "North"] and shell.user_ns["by_region"] is shown
    shell.lines["sql_connect"]("Orders file")
    assert "now run on Orders file" in capsys.readouterr().out
    assert shell.cells["sag"]("", "SELECT count(*) AS n FROM orders")["n"].tolist() == [2]
    shell.lines["sql_connections"]("")
    listed = capsys.readouterr().out
    assert "Shop" in listed and "database (sqlite SQL)" in listed and "* Orders file" in listed
    # The database's refusal in one line (IPython shows it without a traceback and stops Run all).
    with pytest.raises(Exception, match="Shop: OperationalError: no such column: nope"):
        shell.cells["sql"]("Shop", "SELECT nope FROM orders")
    with pytest.raises(Exception, match="SQL cell: No connection is named 'Nowhere'"):
        shell.cells["sql"]("Nowhere", "SELECT 1")


def test_another_extensions_sql_magic_is_left_alone(made: dict[str, Any]) -> None:
    shell = Shell(taken={"sql"})
    assert "%%sql" not in sql_cell.register(shell) and "sag" in shell.cells


def test_tab_in_a_cell_offers_only_that_connections_names(
    made: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from swissarmygrid.client import completion

    searched: list[str] = []
    monkeypatch.setattr(completion, "_catalog_names", lambda: ["Shop", "Elsewhere"])

    def search(catalog: str, text: str) -> dict[str, Any]:
        searched.append(catalog)
        return {}

    monkeypatch.setattr(completion, "_search", search)
    completion.suggestions("ord", "%%sql Shop\nSELECT * FROM ")
    assert searched == ["Shop"]  # the cell's connection's catalog, not every catalog
    sql_cell.connection("Exports")  # a folder open in this Python: its own tables and columns
    assert completion.suggestions("sto", "%%sag Exports\nSELECT * FROM ") == ["stores", "store_id"]
    assert completion.suggestions("stores.ci", "%%sql Exports\nSELECT stores.ci") == ["stores.city"]
