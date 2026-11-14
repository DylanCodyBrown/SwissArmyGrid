"""Every table has a read-only cell (docs/ideas.md, 2026-11-14): the rules. Which names code uses, the
uses line, the Tables group's order and why a maker stays put, cell numbers, a script's regions, and a
table's cell as text. Made-up data only."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

import duckdb
import pytest

from swissarmygrid.domain import table_cells as tc
from swissarmygrid.domain.datasets import Column
from swissarmygrid.domain.table_cells import PlanCell, TableSpec

# ===== names =====


def test_only_real_names_count() -> None:
    code = (
        "# orders in a comment\n"
        'label = "orders in a string"\n'
        "orders_2026 = 1\n"
        "total = report.orders + f(orders=3)\n"
        "print(customers)\n"
    )
    names = tc.names_used(code)
    assert "orders" not in names  # a comment, a string, a longer name, an attribute, an argument's name
    assert names == ["label", "orders_2026", "total", "report", "f", "print", "customers"]
    assert tc.names_used("x = orders\ny = orders") == ["x", "orders", "y"]


def test_names_inside_an_f_string_and_code_half_typed() -> None:
    assert "orders" in tc.names_used('print(f"{len(orders)} rows")')
    # A line Python's tokenizer refuses does not hide the lines after it.
    assert "orders" in tc.names_used("frame?\nfirst = orders.head(")
    assert "orders" in tc.names_used("%matplotlib inline\norders.plot()")


def test_names_in_sql() -> None:
    sql = "SELECT o.region, 'orders' AS label -- from orders\n"
    sql += 'FROM "orders" o JOIN main.customers c USING (id)'
    names = tc.names_used(sql, sql=True)
    assert "orders" in names and "customers" not in names and "label" in names


def test_what_code_makes_and_needs() -> None:
    found = tc.facts(
        "import pandas as pd\n"
        "from pathlib import Path\n"
        "orders = pd.read_csv(Path(folder) / 'orders.csv')\n"
        "big = orders[orders.amount > limit]\n"
        "def clean(frame, by=default_by):\n"
        "    local = frame.dropna()\n"
        "    return local.sort_values(by)\n"
        "totals = [row.amount for row in rows]\n"
    )
    assert found.made == ("pd", "Path", "orders", "big", "clean", "totals")
    # Its own names, a function's arguments and variables, and a comprehension's are not needs.
    assert found.needs == ("folder", "limit", "default_by", "rows")
    assert not found.imports_only
    assert tc.facts('"""Imports."""\nimport duckdb\nfrom x import y as z\n').imports_only
    assert tc.facts("orders = orders.dropna()").needs == ("orders",)
    assert tc.facts("%%sql Shop\nSELECT 1") == tc.Facts()
    # Code that does not parse yet: read by its lines.
    half = tc.facts("orders = pd.read_csv(\nx = ")
    assert half.made == ("orders", "x") and "pd" in half.needs


# ===== the uses line =====

TABLES = {"orders": 3, "customers": 5}


def test_the_uses_line_names_each_table_once_in_the_order_used() -> None:
    assert tc.uses_line("x = customers.merge(orders)\ny = orders", TABLES) == (
        "# uses customers @cell5, orders @cell3"
    )
    assert tc.uses_line("x = 1", TABLES) is None
    assert tc.uses_line("SELECT * FROM orders", TABLES, sql=True) == "-- uses orders @cell3"
    assert tc.uses_line("orders.head()", {"orders": None}) == "# uses orders"


def test_the_uses_line_is_added_changed_and_taken_away() -> None:
    added = tc.with_uses("# my note\ntotal = orders.amount.sum()", TABLES)
    assert added == "# uses orders @cell3\n# my note\ntotal = orders.amount.sum()"
    changed = tc.with_uses(added + "\ncustomers.head()", TABLES)
    assert changed.split("\n")[0] == "# uses orders @cell3, customers @cell5"
    assert changed.split("\n")[1:] == ["# my note", "total = orders.amount.sum()", "customers.head()"]
    # Unchanged when right; gone when the cell names no table any more; nothing else is touched.
    assert tc.with_uses(changed, TABLES) == changed
    assert tc.with_uses("# uses orders @cell3\n# my note\ntotal = 1", TABLES) == "# my note\ntotal = 1"
    assert tc.with_uses("# uses orders @cell3", TABLES) == ""


def test_only_a_first_line_in_exactly_the_apps_format_is_the_apps() -> None:
    for own in ("# uses orders because it is big", "#uses orders", "# Uses orders @cell3", "# uses  orders"):
        source = f"{own}\nx = 1"
        assert tc.split_uses(source) == (None, source)
        assert tc.with_uses(source, TABLES) == source  # the user's comment stays
    # Not on a later line either.
    later = "x = 1\n# uses orders @cell3"
    assert tc.split_uses(later) == (None, later)
    assert tc.split_uses("-- uses orders @cell3\nSELECT 1", sql=True) == ("-- uses orders @cell3", "SELECT 1")


# ===== the Tables group =====


def code(number: int, source: str, **more: object) -> PlanCell:
    return PlanCell(number, "code", source, **more)  # type: ignore[arg-type]


def spec(name: str, steps: int = 1) -> TableSpec:
    return TableSpec(
        name, f"{name} = transformed({name})", f"{name} = load()\n\n{name} = transformed({name})", steps
    )


def shape(arrangement: tc.Arrangement) -> list[tuple[int | None, str, str | None, bool]]:
    return [(c.number, c.role, c.table, c.grouped) for c in arrangement.cells]


NOTEBOOK = [
    code(1, "import pandas as pd"),
    code(2, "print('hello')"),
    code(3, "customers = pd.read_csv('customers.csv')"),
    code(4, "orders = pd.read_csv('orders.csv')"),
    code(5, "joined = orders.merge(customers)"),
    code(6, "joined.plot()\nlen(orders)"),
]


def test_tables_go_to_the_top_each_after_the_tables_it_is_made_from() -> None:
    done = tc.arrange(NOTEBOOK, [spec("joined"), spec("orders"), spec("customers")], next_number=7)
    assert shape(done) == [
        (7, "tables", None, True),
        (1, "imports", None, True),  # the imports the tables need come first
        (8, "table", "orders", True),
        (4, "maker", "orders", True),
        (9, "transformations", "orders", True),
        (10, "table", "customers", True),
        (3, "maker", "customers", True),
        (11, "transformations", "customers", True),
        (12, "table", "joined", True),  # a join after its two sides
        (5, "maker", "joined", True),
        (13, "transformations", "joined", True),
        (2, "", None, False),
        (6, "", None, False),
    ]
    assert done.next_number == 14
    by_role = {(c.role, c.table): c for c in done.cells}
    assert by_role[("tables", None)].source == "# Tables" and by_role[("tables", None)].kind == "markdown"
    assert by_role[("table", "orders")].source == "## orders"
    # A maker cell made the table: the cell holds the transformations alone.
    assert by_role[("transformations", "orders")].source == "orders = transformed(orders)"
    assert [(t.name, t.cell, t.maker, t.moved, t.steps) for t in done.tables] == [
        ("orders", 9, 4, True, 1),
        ("customers", 11, 3, True, 1),
        ("joined", 13, 5, True, 1),
    ]
    # The user's own cell that names tables gets the one line; makers and the app's cells do not.
    uses = {c.number: c.uses for c in done.cells}
    assert uses[6] == "# uses joined @cell13, orders @cell9"
    assert all(line is None for number, line in uses.items() if number != 6)


def test_arranging_again_changes_nothing_and_numbers_never_change() -> None:
    specs = [spec("joined"), spec("orders"), spec("customers")]
    once = tc.arrange(NOTEBOOK, specs, next_number=7)
    twice = tc.arrange(once.cells, specs, once.next_number)
    assert shape(twice) == shape(once) and twice.next_number == once.next_number
    assert [c.source for c in twice.cells] == [c.source for c in once.cells]
    # A cell deleted: its number is not given to the next cell made.
    without = [c for c in twice.cells if c.number != 6]
    third = tc.arrange([*without, PlanCell(None, "code", "x = 1")], specs, twice.next_number)
    assert third.cells[-1].number == 14 and third.next_number == 15


def test_a_table_no_code_made_has_its_load_in_its_cell() -> None:
    done = tc.arrange([code(1, "x = 1")], [spec("pasted")], next_number=2)
    assert shape(done) == [
        (2, "tables", None, True),
        (3, "table", "pasted", True),
        (4, "transformations", "pasted", True),
        (1, "", None, False),
    ]
    assert done.cells[2].source == "pasted = load()\n\npasted = transformed(pasted)"
    assert done.tables[0].maker is None and done.tables[0].moved


def test_a_maker_that_needs_something_further_down_stays_put_and_is_listed() -> None:
    cells = [
        code(1, "import pandas as pd"),
        code(2, "def make_dates(frame):\n    return frame"),
        code(7, "orders = make_dates(pd.read_csv('orders.csv'))"),
        code(8, "summary = orders.describe()"),
    ]
    done = tc.arrange(cells, [spec("orders")], next_number=9)
    assert shape(done) == [
        (9, "tables", None, True),
        (10, "table", "orders", True),
        (1, "", None, False),
        (2, "", None, False),
        (7, "maker", "orders", False),
        (11, "transformations", "orders", False),  # its cells stay where they are, together
        (8, "", None, False),
    ]
    assert done.cells[1].source == "## orders\n\norders is made at @cell7 (it needs make_dates)"
    assert done.tables[0].note == "orders is made at @cell7 (it needs make_dates)"
    assert not done.tables[0].moved and done.tables[0].cell == 11
    assert done.cells[-1].uses == "# uses orders @cell11"
    # A table made from one that stays put stays put too.
    more = tc.arrange(
        [*cells, code(12, "big = orders[orders.amount > 5]")], [spec("orders"), spec("big")], 13
    )
    assert {t.name: t.note for t in more.tables} == {
        "orders": "orders is made at @cell7 (it needs make_dates)",
        "big": "big is made at @cell12 (it needs orders)",
    }


def test_a_cell_above_that_sets_the_same_name_keeps_the_maker_in_place() -> None:
    cells = [code(1, "frame = 1"), code(2, "frame = load()\norders = frame")]
    done = tc.arrange(cells, [spec("orders")], next_number=3)
    assert done.tables[0].note == "orders is made at @cell2 (frame is also set at @cell1)"


def test_two_tables_from_one_cell_share_it() -> None:
    cells = [code(1, "orders, customers = load_both()")]
    done = tc.arrange(cells, [spec("customers"), spec("orders")], next_number=2)
    assert [(c.role, c.table) for c in done.cells] == [
        ("tables", None),
        ("table", "customers"),
        ("maker", "customers"),
        ("transformations", "customers"),
        ("table", "orders"),
        ("transformations", "orders"),
    ]
    assert done.cells[4].source == "## orders\n\norders is made at @cell1"


def test_a_closed_table_keeps_its_cell_and_no_tables_means_no_group() -> None:
    specs = [spec("orders")]
    once = tc.arrange(NOTEBOOK, specs, next_number=7)
    closed = tc.arrange(once.cells, [], once.next_number)
    assert shape(closed) == shape(once)  # the file still holds the table's cell
    assert closed.tables[0].open is False and closed.tables[0].steps is None
    assert [c.source for c in closed.cells if c.role == "transformations"] == ["orders = transformed(orders)"]
    # The user removes the table's own cell: the group goes, the cells are the user's again.
    gone = tc.arrange([c for c in closed.cells if c.role != "transformations"], [], closed.next_number)
    assert [(c.number, c.role, c.grouped) for c in gone.cells] == [(n, "", False) for n in (1, 4, 2, 3, 5, 6)]
    assert tc.arrange(NOTEBOOK, [], 7).cells == [replace(c, uses=None) for c in NOTEBOOK]


def test_while_typing_nothing_moves_and_the_tables_cells_follow_the_grid() -> None:
    once = tc.arrange(NOTEBOOK, [spec("orders")], next_number=7)
    typed = [replace(c, source=c.source + "\ncustomers") if c.number == 2 else c for c in once.cells]
    new = TableSpec("orders", "orders = again(orders)", "", 2)
    quiet = tc.arrange(typed, [new, spec("customers")], once.next_number, move=False)
    assert shape(quiet) == shape(once)  # customers has no cell yet: none is added while typing
    assert [c.source for c in quiet.cells if c.role == "transformations"] == ["orders = again(orders)"]
    assert [t.name for t in quiet.tables] == ["orders"] and quiet.tables[0].steps == 2
    assert {c.number: c.uses for c in quiet.cells}[6] == "# uses orders @cell9"


def test_a_sql_cell_and_markdown() -> None:
    cells = [
        code(1, "orders = load()"),
        PlanCell(2, "sql", "SELECT * FROM orders"),
        PlanCell(3, "markdown", "About orders"),
    ]
    done = tc.arrange(cells, [spec("orders")], next_number=4)
    uses = {c.number: c.uses for c in done.cells}
    assert uses[2] == "-- uses orders @cell6" and uses[3] is None


# ===== scripts =====

SCRIPT = '''#!/usr/bin/env python
"""A made-up report."""
print("start")
# %% the imports
import pandas as pd
# %% Load the customers
customers = pd.read_csv("customers.csv")
# %%
orders = pd.read_csv("orders.csv")

# %% [9] the report
report = orders.merge(customers)
print(len(report))
'''


def script_spec(name: str) -> TableSpec:
    return TableSpec(
        name, f'import duckdb\n\n{name} = duckdb.sql("""\nSELECT * FROM {name}\n""").df()', "", 1
    )


def test_a_script_without_tables_is_left_exactly_as_it_is() -> None:
    blank_cell = "a\n# %%\n\n# %%\nb"
    for text in (SCRIPT, "", "x = 1", "x = 1\n\n\n", "# %%\nx = 1\n# %% [4] note\n\ny = 2\n\n", blank_cell):
        assert tc.arrange_script(text, [], move=False)[0] == text
    # A full arrangement only numbers the cells.
    numbered, _ = tc.arrange_script("a = 1\n# %%\nx = 1\n# %% [4] note\ny = 2\n", [])
    assert numbered == "a = 1\n# %% [5]\nx = 1\n# %% [4] note\ny = 2\n"


def test_a_scripts_tables_group_is_a_region_of_cells() -> None:
    text, arrangement = tc.arrange_script(SCRIPT, [script_spec("orders"), script_spec("customers")])
    assert (
        text
        == '''#!/usr/bin/env python
# region Tables
# %% [10] the imports
import pandas as pd
# %% [12] [table] orders
orders = pd.read_csv("orders.csv")

# %% [13] [transformations] orders
import duckdb

orders = duckdb.sql("""
SELECT * FROM orders
""").df()
# %% [11] [table] customers Load the customers
customers = pd.read_csv("customers.csv")
# %% [14] [transformations] customers
import duckdb

customers = duckdb.sql("""
SELECT * FROM customers
""").df()
# endregion
# %%
"""A made-up report."""
print("start")
# %% [9] the report
# uses orders @cell13, customers @cell14
report = orders.merge(customers)
print(len(report))
'''
    )
    # Every cell keeps the number it was given; the user's own words on a marker stay.
    assert [t.name for t in arrangement.tables] == ["orders", "customers"]


def test_a_script_is_stable_and_its_main_code_never_moves() -> None:
    specs = [script_spec("orders"), script_spec("customers")]
    once, _ = tc.arrange_script(SCRIPT, specs)
    assert tc.arrange_script(once, specs)[0] == once
    assert tc.arrange_script(once, specs, move=False)[0] == once
    # A table made in the main code (a script with no cells): listed, and its cell follows the code.
    plain = "import pandas as pd\norders = pd.read_csv('orders.csv')\nprint(len(orders))\n"
    text, arrangement = tc.arrange_script(plain, [script_spec("orders")])
    assert text == (
        "# region Tables\n"
        "# orders is made in the script's main code\n"
        "# endregion\n"
        "# %%\n"
        "import pandas as pd\norders = pd.read_csv('orders.csv')\nprint(len(orders))\n"
        "# %% [1] [transformations] orders\n"
        'import duckdb\n\norders = duckdb.sql("""\nSELECT * FROM orders\n""").df()\n'
    )
    assert not arrangement.tables[0].moved
    assert tc.arrange_script(text, [script_spec("orders")])[0] == text
    # The grid's steps change: only the table's cell changes.
    new = TableSpec("orders", "import duckdb\n\norders = duckdb.sql('SELECT 1').df()", "", 2)
    assert tc.arrange_script(text, [new], move=False)[0] == text.replace(
        'duckdb.sql("""\nSELECT * FROM orders\n""")', "duckdb.sql('SELECT 1')"
    )
    # The table closed and its cell deleted by hand: the script is the user's own again.
    by_hand = text[: text.index("# %% [1] [transformations]")]
    assert tc.arrange_script(by_hand, [])[0] == plain


def test_a_script_run_as_plain_python_makes_then_transforms_once(tmp_path: object) -> None:
    script = (
        "import duckdb\n"
        "# %%\n"
        'orders = duckdb.sql("SELECT * FROM (VALUES (1, 10), (2, 30)) AS t(id, amount)").to_arrow_table()\n'
    )
    code_text = 'import duckdb\n\norders = duckdb.sql("""\nSELECT * FROM orders WHERE amount > 20\n""")'
    code_text += ".to_arrow_table()"
    text, _ = tc.arrange_script(script, [TableSpec("orders", code_text, "", 1)])
    namespace: dict[str, object] = {}
    exec(compile(text, "report.py", "exec"), namespace)  # noqa: S102 (the made-up script above)
    assert namespace["orders"].num_rows == 1  # type: ignore[attr-defined]


def test_the_outline_says_where_each_cell_is() -> None:
    text, _ = tc.arrange_script(SCRIPT, [script_spec("orders")])
    found = tc.outline(text)
    lines = text.split("\n")
    assert found["region"] == [2, lines.index("# endregion") + 1]
    cells = found["cells"]
    assert [(c["number"], c["role"], c["table"], c["grouped"]) for c in cells] == [
        (10, "imports", None, True),
        (12, "maker", "orders", True),
        (13, "transformations", "orders", True),
        (11, "", None, False),
        (9, "", None, False),
    ]
    for cell in cells:
        assert lines[cell["line"] - 1].startswith(f"# %% [{cell['number']}]")
    assert cells[1]["first"] == 'orders = pd.read_csv("orders.csv")' and cells[2]["first"] == "import duckdb"
    assert cells[4]["first"] == "report = orders.merge(customers)"  # not the uses line
    assert cells[1]["end"] == cells[2]["line"] - 1
    assert tc.outline("x = 1\n") == {"region": None, "cells": []}


def test_a_cell_link_is_at_cell_and_a_number() -> None:
    assert [m.group(1) for m in tc.CELL_LINK.finditer("# see @cell7 and @cell12, not @cells or cell7")] == [
        "7",
        "12",
    ]


# ===== a table's cell as text =====


def test_the_transformations_are_one_statement_whose_result_is_the_tables_own_name() -> None:
    imports, statement = tc.transformations_code(
        "orders", "pandas", 'SELECT * FROM orders WHERE "amount" > 20'
    )
    assert imports == ["import duckdb"]
    assert statement == 'orders = duckdb.sql("""\nSELECT * FROM orders WHERE "amount" > 20\n""").df()'
    # The output is what the table was.
    assert tc.transformations_code("o", "polars", "SELECT 1")[1].endswith(".pl()")
    assert tc.transformations_code("o", "arrow", "SELECT 1")[1].endswith(".to_arrow_table()")
    assert tc.transformations_code("o", "duckdb", "SELECT 1")[1].endswith('""")')
    assert tc.transformations_code("o", "clipboard", "SELECT 1")[1].endswith(".df()")
    # No steps: nothing to run.
    assert tc.transformations_code("orders", "pandas", None) == (
        [],
        "# orders: no transformations yet. Steps made in the grid appear here.",
    )


def test_a_table_no_code_made_starts_with_its_load() -> None:
    def load(origin: tc.Origin, kind: str, pasted: str | None = None, rows: int = 3) -> str:
        change = tc.transformations_code("sales", kind, "SELECT * FROM sales LIMIT 2")
        return tc.cell_text([tc.load_code("sales", origin, kind, pasted, rows), change])

    csv = load(tc.Origin("file", path="/data/sales.csv"), "csv_file")
    assert csv == (
        "import duckdb\n\n"
        "sales = duckdb.read_csv('/data/sales.csv').df()\n\n"
        'sales = duckdb.sql("""\nSELECT * FROM sales LIMIT 2\n""").df()'
    )
    assert "duckdb.read_parquet('/d/s.parquet')" in load(
        tc.Origin("file", path="/d/s.parquet"), "parquet_file"
    )
    excel = load(tc.Origin("file", path="/d/s.xlsx", sheet="Jan"), "excel_file")
    assert excel.startswith(
        "import pandas as pd\nimport duckdb\n\nsales = pd.read_excel('/d/s.xlsx', sheet_name='Jan')"
    )
    shop = load(
        tc.Origin("connection", connection="Shop", sql="SELECT * FROM sales LIMIT 1000"), "connection"
    )
    assert 'sales = sag.sql(\'Shop\', """\nSELECT * FROM sales LIMIT 1000\n""")' in shop
    assert shop.startswith("import swissarmygrid as sag\nimport duckdb\n")
    query = load(tc.Origin("query", sql="SELECT region FROM orders"), "sql_result")
    assert 'sales = duckdb.sql("""\nSELECT region FROM orders\n""").df()\n\nsales = duckdb.sql(' in query
    # What the app cannot load says so, plainly, and still holds the transformations.
    dropped = load(tc.Origin("dropped", filename="sales.csv"), "csv_file")
    assert dropped.startswith("import duckdb\n\n# sales was dropped on the page (sales.csv): the browser")
    big = load(tc.Origin("paste"), "clipboard", None, 5000)
    assert "# sales was pasted into the grid (5,000 rows): too large to write here." in big
    outside = load(tc.Origin("python"), "pandas")
    assert "# sales was made outside this file" in outside and outside.endswith('""").df()')


def test_a_small_paste_is_written_out_and_reads_back_the_same() -> None:
    columns = [
        Column("item", "VARCHAR"),
        Column("price", "DOUBLE"),
        Column("day", "DATE"),
        Column("n", "BIGINT"),
    ]
    rows: list[list[Any]] = [
        ["it's", 1.5, date(2026, 1, 5), 3],
        [None, None, None, None],
        ["b", float("nan"), date(2026, 2, 1), -1],
    ]
    query = tc.pasted_query(columns, rows)
    assert query is not None
    result = duckdb.connect().execute(query)
    assert [d[0] for d in result.description] == ["item", "price", "day", "n"]
    back = result.fetchall()
    assert back[0] == ("it's", 1.5, date(2026, 1, 5), 3) and back[1] == (None, None, None, None)
    assert back[2][0] == "b" and back[2][1] != back[2][1] and back[2][3] == -1
    # Over the cap, or a value SQL text cannot hold: no literal (the cell gets a note instead).
    assert tc.pasted_query(columns, [rows[0]] * (tc.PASTE_MAX_ROWS + 1)) is None
    assert tc.pasted_query([Column("blob", "BLOB")], [[b"\x00"]]) is None
    assert tc.pasted_query([Column("t", "VARCHAR")], [["x" * 30_000]]) is None


@pytest.mark.parametrize("runs", [1, 3])
def test_the_cell_text_has_its_imports_once(runs: int) -> None:
    parts = [(["import duckdb"], "a = 1")] * runs
    assert tc.cell_text(parts).count("import duckdb") == 1


# ===== a notebook file keeps each cell's number =====


def test_a_notebook_keeps_cell_numbers_and_never_gives_one_twice() -> None:
    from swissarmygrid.domain import notebooks

    cells = [
        notebooks.Cell("markdown", "# Tables", number=7, role="tables", grouped=True),
        notebooks.Cell("code", "orders = 1", number=4, role="maker", table="orders", grouped=True),
        notebooks.Cell(
            "code",
            "orders = 2",
            number=9,
            role="transformations",
            table="orders",
            grouped=True,
            steps=[{"kind": "limit", "params": {"n": 2}}],
        ),
        notebooks.Cell("sql", "SELECT 1", connection="Shop"),  # made without a number: it gets the next
    ]
    text = notebooks.write_notebook(cells, next_number=12)
    read, following = notebooks.read_document(text)
    assert [(c.number, c.role, c.table, c.grouped) for c in read] == [
        (7, "tables", None, True),
        (4, "maker", "orders", True),
        (9, "transformations", "orders", True),
        (12, "", None, False),
    ]
    assert following == 13 and read[2].steps == [{"kind": "limit", "params": {"n": 2}}]
    assert read[3].kind == "sql" and read[3].connection == "Shop"
    # Cell 12 deleted and the file saved: the next cell is 13, not 12 again.
    again = notebooks.read_document(notebooks.write_notebook(read[:3], next_number=following))
    assert again[1] == 13
    # A notebook from Jupyter has no numbers: each cell gets one, in order; a copied cell's repeat
    # is renumbered.
    import json

    plain = {
        "nbformat": 4,
        "cells": [{"cell_type": "code", "source": "a"}, {"cell_type": "markdown", "source": "b"}],
    }
    assert [c.number for c in notebooks.read_notebook(json.dumps(plain))] == [1, 2]
    copied = json.loads(text)
    copied["cells"].append(copied["cells"][1])
    assert [c.number for c in notebooks.read_notebook(json.dumps(copied))] == [7, 4, 9, 12, 13]
    # In the file the headings are Markdown, so JupyterLab folds the group too.
    assert json.loads(text)["cells"][0]["cell_type"] == "markdown"
    assert json.loads(text)["cells"][0]["metadata"]["swissarmygrid"] == {
        "number": 7,
        "role": "tables",
        "grouped": True,
    }
