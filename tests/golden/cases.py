"""Golden cases: (input table, step list, settings, expected output) (plan section 9).

One data-driven table shared by the DuckDB engine and every code generator. A case passes only
when each implementation returns exactly `expected` (same columns, same rows, same order).
The inputs deliberately hold blanks, empty strings, case variants, padded spaces, integers next
to floats, week boundaries, wildcard characters, and Unicode edge cases (ß, ẞ, İ, final sigma).

All data is made up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class Table:
    columns: tuple[tuple[str, str], ...]  # (name, DuckDB type)
    rows: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True)
class Case:
    name: str
    table: Table
    steps: list[dict[str, Any]]
    expected_columns: tuple[str, ...]
    expected: tuple[tuple[Any, ...], ...]
    settings: dict[str, bool] = field(default_factory=dict)


PEOPLE = Table(
    columns=(("id", "BIGINT"), ("name", "VARCHAR"), ("city", "VARCHAR"), ("score", "DOUBLE")),
    rows=(
        (1, "Anna", "Oslo", 3.5),
        (2, "bob", "oslo", None),
        (3, None, " Rome ", 2.0),
        (4, "", "Rome", 3.5),
        (5, "Cleo", None, 1.0),
        (6, "Bob", "Paris", 4.0),
    ),
)

UNICODE = Table(
    columns=(("id", "BIGINT"), ("word", "VARCHAR")),
    rows=(
        (1, "straße"),  # straße
        (2, "STRAẞE"),  # STRAẞE
        (3, "İstanbul"),  # İstanbul
        (4, "istanbul"),
        (5, "ΣΟΣ"),  # ΣΟΣ
        (6, "a%b_c!d"),
        (7, "a*b?c"),
        (8, "ﬁle"),  # ﬁle
    ),
)

ORDERS = Table(
    columns=(
        ("id", "BIGINT"),
        ("region", "VARCHAR"),
        ("qty", "BIGINT"),
        ("price", "DOUBLE"),
        ("day", "DATE"),
        ("paid", "BOOLEAN"),
    ),
    rows=(
        (1, "North", 5, 10.0, date(2026, 10, 18), True),  # Sunday
        (2, "South", 3, 2.5, date(2026, 10, 19), False),  # Monday
        (3, "North", None, 7.25, date(2026, 10, 25), None),  # Sunday
        (4, None, 1, 100.0, date(2026, 12, 31), True),
        (5, "South", 3, 2.5, date(2026, 10, 19), False),  # duplicate of 2 apart from id
        (6, "East", 10, None, None, True),
    ),
)

CASES: list[Case] = [
    # ----- filters: equality and blanks -----
    Case(
        "eq is case sensitive by default",
        PEOPLE,
        [{"kind": "filter", "params": {"column": "city", "op": "eq", "value": "oslo"}}],
        ("id", "name", "city", "score"),
        ((2, "bob", "oslo", None),),
    ),
    Case(
        "eq with case off matches both",
        PEOPLE,
        [{"kind": "filter", "params": {"column": "city", "op": "eq", "value": "OSLO"}}],
        ("id", "name", "city", "score"),
        ((1, "Anna", "Oslo", 3.5), (2, "bob", "oslo", None)),
        {"case_sensitive": False},
    ),
    Case(
        "ne drops blanks (SQL semantics)",
        PEOPLE,
        [{"kind": "filter", "params": {"column": "city", "op": "ne", "value": "Rome"}}],
        ("id",),
        ((1,), (2,), (3,), (6,)),
    ),
    Case(
        "trim setting matches padded text",
        PEOPLE,
        [{"kind": "filter", "params": {"column": "city", "op": "eq", "value": "Rome"}}],
        ("id",),
        ((3,), (4,)),
        {"trim_spaces": True},
    ),
    Case(
        "blank vs empty string",
        PEOPLE,
        [{"kind": "filter", "params": {"column": "name", "op": "blank"}}],
        ("id",),
        ((3,),),
    ),
    Case(
        "empty counts as blank when the setting is on",
        PEOPLE,
        [{"kind": "filter", "params": {"column": "name", "op": "blank"}}],
        ("id",),
        ((3,), (4,)),
        {"empty_is_blank": True},
    ),
    Case(
        "not_blank on numbers",
        PEOPLE,
        [{"kind": "filter", "params": {"column": "score", "op": "not_blank"}}],
        ("id",),
        ((1,), (3,), (4,), (5,), (6,)),
    ),
    Case(
        "in list, not_in drops blanks",
        PEOPLE,
        [
            {
                "kind": "filter",
                "params": {"column": "city", "op": "not_in", "values": ["Oslo", "Paris"]},
            },
        ],
        ("id",),
        ((2,), (3,), (4,)),
    ),
    Case(
        "contains / starts / ends with case off",
        PEOPLE,
        [
            {"kind": "filter", "params": {"column": "name", "op": "starts_with", "value": "b"}},
        ],
        ("id",),
        ((2,), (6,)),
        {"case_sensitive": False},
    ),
    Case(
        "ends_with case sensitive",
        PEOPLE,
        [{"kind": "filter", "params": {"column": "name", "op": "ends_with", "value": "ob"}}],
        ("id",),
        ((2,), (6,)),
    ),
    Case(
        "number ranges are inclusive between, strict lt/gt",
        PEOPLE,
        [{"kind": "filter", "params": {"column": "score", "op": "between", "low": 2, "high": 3.5}}],
        ("id",),
        ((1,), (3,), (4,)),
    ),
    Case(
        "lt with an integer against doubles",
        PEOPLE,
        [{"kind": "filter", "params": {"column": "score", "op": "lt", "value": 3}}],
        ("id",),
        ((3,), (5,)),
    ),
    Case(
        "date before and between",
        ORDERS,
        [
            {
                "kind": "filter",
                "params": {
                    "column": "day",
                    "op": "date_between",
                    "low": "2026-10-19",
                    "high": "2026-10-25",
                },
            },
        ],
        ("id",),
        ((2,), (3,), (5,)),
    ),
    Case(
        "boolean false skips blanks",
        ORDERS,
        [{"kind": "filter", "params": {"column": "paid", "op": "false"}}],
        ("id",),
        ((2,), (5,)),
    ),
    # ----- wildcards and Unicode -----
    Case(
        "wildcards off: percent is literal",
        UNICODE,
        [{"kind": "filter", "params": {"column": "word", "op": "eq", "value": "a%b_c!d"}}],
        ("id",),
        ((6,),),
    ),
    Case(
        "wildcards on: percent matches any run of characters",
        UNICODE,
        [{"kind": "filter", "params": {"column": "word", "op": "eq", "value": "a%c%"}}],
        ("id",),
        ((6,), (7,)),
        {"wildcards": True},
    ),
    Case(
        "wildcards on: underscore and the escape character stay literal",
        UNICODE,
        [{"kind": "filter", "params": {"column": "word", "op": "eq", "value": "a%b_%!d"}}],
        ("id",),
        ((6,),),
        {"wildcards": True},
    ),
    Case(
        "wildcards on: star and question mark stay literal",
        UNICODE,
        [{"kind": "filter", "params": {"column": "word", "op": "eq", "value": "a*b?%"}}],
        ("id",),
        ((7,),),
        {"wildcards": True},
    ),
    Case(
        "wildcards on: not equals with percent",
        UNICODE,
        [{"kind": "filter", "params": {"column": "word", "op": "ne", "value": "%a%"}}],
        ("id",),
        ((2,), (5,), (8,)),  # case sensitive: STRAẞE has no lowercase a
        {"wildcards": True},
    ),
    Case(
        "lowercase is one character to one (sharp s, dotted I)",
        UNICODE,
        [
            {
                "kind": "filter",
                "params": {"column": "word", "op": "in", "values": ["straße", "istanbul"]},
            }
        ],
        ("id",),
        ((1,), (2,), (3,), (4,)),
        {"case_sensitive": False},
    ),
    Case(
        "upper transform keeps one character per character",
        UNICODE,
        [
            {"kind": "transform", "params": {"columns": ["word"], "fn": "upper"}},
            {"kind": "filter", "params": {"column": "id", "op": "in", "values": [1, 8]}},
        ],
        ("id", "word"),
        ((1, "STRAẞE"), (8, "ﬁLE")),
    ),
    Case(
        "lower transform: final sigma is not special",
        UNICODE,
        [
            {"kind": "transform", "params": {"columns": "*text", "fn": "lower"}},
            {"kind": "filter", "params": {"column": "id", "op": "in", "values": [3, 5]}},
        ],
        ("id", "word"),
        ((3, "istanbul"), (5, "σοσ")),
    ),
    # ----- sort -----
    Case(
        "sort desc puts blanks last, ties keep order",
        PEOPLE,
        [{"kind": "sort", "params": {"keys": [["score", "desc"]]}}],
        ("id", "score"),
        ((6, 4.0), (1, 3.5), (4, 3.5), (3, 2.0), (5, 1.0), (2, None)),
    ),
    Case(
        "sort text is by code point; blanks last ascending",
        PEOPLE,
        [
            {"kind": "sort", "params": {"keys": [["name", "asc"]]}},
            {"kind": "columns", "params": {"keep": ["id"]}},
        ],
        ("id",),
        ((4,), (1,), (6,), (5,), (2,), (3,)),
    ),
    Case(
        "two sort keys",
        ORDERS,
        [
            {"kind": "sort", "params": {"keys": [["region", "asc"], ["id", "desc"]]}},
            {"kind": "columns", "params": {"keep": ["id", "region"]}},
        ],
        ("id", "region"),
        ((6, "East"), (3, "North"), (1, "North"), (5, "South"), (2, "South"), (4, None)),
    ),
    # ----- columns, rename, limit, distinct -----
    Case(
        "select, reorder and rename",
        PEOPLE,
        [
            {"kind": "columns", "params": {"keep": ["score", "id"]}},
            {"kind": "rename", "params": {"mapping": {"score": "points"}}},
            {"kind": "limit", "params": {"n": 2}},
        ],
        ("points", "id"),
        ((3.5, 1), (None, 2)),
    ),
    Case(
        "distinct keeps first position and the blank row",
        ORDERS,
        [
            {"kind": "columns", "params": {"keep": ["region", "qty"]}},
            {"kind": "distinct", "params": {}},
        ],
        ("region", "qty"),
        (("North", 5), ("South", 3), ("North", None), (None, 1), ("East", 10)),
    ),
    # ----- group by -----
    Case(
        "group by keeps the blank group, sorted by key",
        ORDERS,
        [
            {
                "kind": "group_by",
                "params": {
                    "keys": ["region"],
                    "aggregates": [
                        {"fn": "count_rows"},
                        {"fn": "count", "column": "qty"},
                        {"fn": "sum", "column": "qty"},
                        {"fn": "mean", "column": "price"},
                        {"fn": "distinct_count", "column": "price"},
                        {"fn": "min", "column": "day"},
                    ],
                },
            }
        ],
        ("region", "rows", "count_qty", "sum_qty", "mean_price", "distinct_count_price", "min_day"),
        (
            ("East", 1, 1, 10, None, 0, None),
            ("North", 2, 1, 5, 8.625, 2, date(2026, 10, 18)),
            ("South", 2, 2, 6, 2.5, 1, date(2026, 10, 19)),
            (None, 1, 1, 1, 100.0, 1, date(2026, 12, 31)),
        ),
    ),
    Case(
        "group by with no keys is one row; sum of nothing is blank",
        ORDERS,
        [
            {"kind": "filter", "params": {"column": "id", "op": "gt", "value": 100}},
            {
                "kind": "group_by",
                "params": {
                    "keys": [],
                    "aggregates": [{"fn": "count_rows"}, {"fn": "sum", "column": "qty"}],
                },
            },
        ],
        ("rows", "sum_qty"),
        ((0, None),),
    ),
    # ----- transforms -----
    Case(
        "trim both removes spaces, tabs and line breaks only",
        Table((("t", "VARCHAR"),), (("\t a b \n",), (" x\r",), (None,), ("",))),
        [{"kind": "transform", "params": {"columns": ["t"], "fn": "trim_both"}}],
        ("t",),
        (("a b",), ("x",), (None,), ("",)),
    ),
    Case(
        "trim left and right",
        Table((("t", "VARCHAR"),), (("  a  ",),)),
        [
            {"kind": "transform", "params": {"columns": ["t"], "fn": "trim_left"}},
        ],
        ("t",),
        (("a  ",),),
    ),
    # ----- bucket -----
    Case(
        "number bucket by edges, left closed, other and blank",
        ORDERS,
        [
            {
                "kind": "bucket",
                "params": {"column": "price", "as": "band", "mode": "edges", "edges": [0, 5, 10]},
            },
            {"kind": "columns", "params": {"keep": ["id", "band"]}},
        ],
        ("id", "band"),
        ((1, "Other"), (2, "[0, 5)"), (3, "[5, 10)"), (4, "Other"), (5, "[0, 5)"), (6, None)),
    ),
    Case(
        "number bucket right closed",
        ORDERS,
        [
            {
                "kind": "bucket",
                "params": {
                    "column": "price",
                    "as": "band",
                    "mode": "edges",
                    "edges": [0, 5, 10],
                    "right_closed": True,
                },
            },
            {"kind": "columns", "params": {"keep": ["band"]}},
            {"kind": "limit", "params": {"n": 1}},
        ],
        ("band",),
        (("(5, 10]",),),
    ),
    Case(
        "width bucket",
        ORDERS,
        [
            {
                "kind": "bucket",
                "params": {
                    "column": "qty",
                    "as": "b",
                    "mode": "width",
                    "width": 2.5,
                    "low": 0,
                    "high": 10,
                },
            },
            {"kind": "columns", "params": {"keep": ["qty", "b"]}},
        ],
        ("qty", "b"),
        (
            (5, "[5, 7.5)"),
            (3, "[2.5, 5)"),
            (None, None),
            (1, "[0, 2.5)"),
            (3, "[2.5, 5)"),
            (10, "Other"),
        ),
    ),
    Case(
        "date bucket by week starts Monday",
        ORDERS,
        [
            {
                "kind": "bucket",
                "params": {"column": "day", "as": "week", "mode": "date", "unit": "week"},
            },
            {"kind": "columns", "params": {"keep": ["id", "week"]}},
        ],
        ("id", "week"),
        (
            (1, date(2026, 10, 12)),
            (2, date(2026, 10, 19)),
            (3, date(2026, 10, 19)),
            (4, date(2026, 12, 28)),
            (5, date(2026, 10, 19)),
            (6, None),
        ),
    ),
    Case(
        "date bucket by quarter",
        ORDERS,
        [
            {
                "kind": "bucket",
                "params": {"column": "day", "as": "q", "mode": "date", "unit": "quarter"},
            },
            {"kind": "columns", "params": {"keep": ["q"]}},
            {"kind": "distinct", "params": {}},
        ],
        ("q",),
        ((date(2026, 10, 1),), (None,)),
    ),
    # ----- categorize -----
    Case(
        "categorize: first match wins, blanks stay blank",
        PEOPLE,
        [
            {
                "kind": "categorize",
                "params": {
                    "column": "city",
                    "as": "zone",
                    "rules": [
                        {"type": "wildcard", "value": "O%", "label": "O-town"},
                        {"type": "in", "values": ["Rome", "Paris"], "label": "South"},
                        {"type": "contains", "value": "slo", "label": "never (rule 1 wins)"},
                    ],
                    "unmatched": "Elsewhere",
                },
            },
            {"kind": "columns", "params": {"keep": ["id", "zone"]}},
        ],
        ("id", "zone"),
        (
            (1, "O-town"),
            (2, "never (rule 1 wins)"),
            (3, "Elsewhere"),
            (4, "South"),
            (5, None),
            (6, "South"),
        ),
    ),
    Case(
        "categorize with case off and a number range",
        PEOPLE,
        [
            {
                "kind": "categorize",
                "params": {
                    "column": "score",
                    "as": "level",
                    "rules": [{"type": "range", "low": 3, "high": 5, "label": "high"}],
                    "unmatched": "low",
                },
            },
            {"kind": "columns", "params": {"keep": ["level"]}},
        ],
        ("level",),
        (("high",), (None,), ("low",), ("high",), ("low",), ("high",)),
    ),
    # ----- SQL steps -----
    Case(
        "sql step: a plain select keeps the grid's order",
        PEOPLE,
        [
            {
                "kind": "sql",
                "params": {"sql": "SELECT id, score * 2 AS doubled FROM grid WHERE score IS NOT NULL"},
            }
        ],
        ("id", "doubled"),
        ((1, 7.0), (3, 4.0), (4, 7.0), (5, 2.0), (6, 8.0)),
    ),
    Case(
        "sql step: group by with no ORDER BY is sorted by every column",
        ORDERS,
        [{"kind": "sql", "params": {"sql": "SELECT region, sum(qty) AS q FROM grid GROUP BY region"}}],
        ("region", "q"),
        (("East", 10), ("North", 5), ("South", 6), (None, 1)),
    ),
    Case(
        "sql step after a filter keeps its own ORDER BY",
        ORDERS,
        [
            {"kind": "filter", "params": {"column": "paid", "op": "true"}},
            {"kind": "sql", "params": {"sql": "SELECT id, price\nFROM grid\nORDER BY price DESC;"}},
        ],
        ("id", "price"),
        ((4, 100.0), (1, 10.0), (6, None)),
    ),
    Case(
        "steps after a sql step see its columns",
        PEOPLE,
        [
            {"kind": "sql", "params": {"sql": "SELECT name, 'it''s ' || city AS label FROM grid"}},
            {"kind": "filter", "params": {"column": "label", "op": "contains", "value": "Rome"}},
        ],
        ("name", "label"),
        ((None, "it's  Rome "), ("", "it's Rome")),
    ),
]
