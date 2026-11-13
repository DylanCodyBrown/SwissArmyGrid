"""Relationship models: a data model's primary keys and joins, as a JSON file people can share.

A model names tables by their dataset name in the grid (what `sag.push(df, "orders")` called
them), so pushing the same tables again makes the model apply as-is:

    {
      "name": "sample_store",
      "description": "The made-up store in examples/sample_store.py",
      "tables": {
        "customers": {"primary_key": ["cust_key"], "source": "examples/sample_store.py"},
        "orders": {"primary_key": ["order_id"]}
      },
      "relationships": [
        {"from": "orders", "from_columns": ["customer_id"],
         "to": "customers", "to_columns": ["cust_key"], "kind": "many-to-one",
         "note": "about 1% orphans"}
      ]
    }

The grid uses it for free: primary-key columns are marked in the headers, and splitting two
tables that have a relationship pre-ticks its columns as the compare keys. It can also draw the
model as an ERD card (Mermaid erDiagram) in the Analysis Sandbox.

Like a C# record set: Model is immutable; merge() returns a new one, the later model winning
for any table's key and for a relationship between the same tables on the same columns.
"""

# The key finder's and the comparison's SQL below is built from quote()d names, the grids' own
# compiled view SQL and integers; no user text is spliced in, so the "string-built SQL" check is
# off for this file.
# ruff: noqa: S608

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from swissarmygrid.domain.datasets import Column
from swissarmygrid.domain.grids import POS
from swissarmygrid.domain.steps import quote, type_family

KINDS: tuple[str, ...] = ("one-to-one", "one-to-many", "many-to-one", "many-to-many")
_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
MAX_TABLES = 200
MAX_RELATIONSHIPS = 500


@dataclass(frozen=True)
class Relationship:
    from_table: str
    from_columns: tuple[str, ...]
    to_table: str
    to_columns: tuple[str, ...]
    kind: str = "many-to-one"
    # A comment about the join (an outlier, a caveat, how the views compare it); the ERD draws it
    # as the line's label. Column names belong in the boxes, not here.
    note: str = ""

    def same_join(self, other: Relationship) -> bool:
        return (self.from_table, self.from_columns, self.to_table, self.to_columns) == (
            other.from_table,
            other.from_columns,
            other.to_table,
            other.to_columns,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "from": self.from_table,
            "from_columns": list(self.from_columns),
            "to": self.to_table,
            "to_columns": list(self.to_columns),
            "kind": self.kind,
            **({"note": self.note} if self.note else {}),
        }


@dataclass(frozen=True)
class Model:
    name: str = "current"
    description: str = ""
    keys: dict[str, tuple[str, ...]] = field(default_factory=dict)  # table -> primary key
    sources: dict[str, str] = field(default_factory=dict)  # table -> where it comes from
    relationships: tuple[Relationship, ...] = ()

    def to_json(self) -> dict[str, Any]:
        tables = sorted(set(self.keys) | set(self.sources))
        return {
            "name": self.name,
            "description": self.description,
            "tables": {
                t: {
                    **({"primary_key": list(self.keys[t])} if t in self.keys else {}),
                    **({"source": self.sources[t]} if t in self.sources else {}),
                }
                for t in tables
            },
            "relationships": [r.to_json() for r in self.relationships],
        }

    def tables(self) -> set[str]:
        return (
            set(self.keys)
            | set(self.sources)
            | {t for r in self.relationships for t in (r.from_table, r.to_table)}
        )

    def between(self, left: str, right: str) -> list[tuple[str, str]]:
        """The column pairs (left column, right column) of a relationship between two tables,
        whichever way round it was written; [] when there is none."""
        for r in self.relationships:
            if (r.from_table, r.to_table) == (left, right):
                return list(zip(r.from_columns, r.to_columns, strict=True))
            if (r.from_table, r.to_table) == (right, left):
                return list(zip(r.to_columns, r.from_columns, strict=True))
        if left == right and left in self.keys:
            return [(c, c) for c in self.keys[left]]
        return []


def _columns(raw: Any, what: str) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw or not all(isinstance(c, str) and c for c in raw):
        raise ValueError(f"{what} must be a column name or a list of column names.")
    return tuple(raw)


def _table(raw: Any, what: str) -> str:
    if not isinstance(raw, str) or not _NAME.fullmatch(raw):
        raise ValueError(f"{what} must be a dataset name (letters, digits, '_' or '-').")
    return raw


def parse_model(raw: Any) -> Model:
    """Check a model's JSON and build it; any problem raises ValueError with its reason."""
    if not isinstance(raw, dict):
        raise ValueError('A model is {"name", "tables": {...}, "relationships": [...]}.')
    name = raw.get("name", "current")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("name must be letters, digits, '_' or '-', up to 64 characters.")
    description = raw.get("description", "")
    if not isinstance(description, str) or len(description) > 2000:
        raise ValueError("description must be text, at most 2,000 characters.")
    tables = raw.get("tables", {})
    if not isinstance(tables, dict) or len(tables) > MAX_TABLES:
        raise ValueError(f"tables maps dataset names to {{primary_key, source}}, at most {MAX_TABLES}.")
    keys: dict[str, tuple[str, ...]] = {}
    sources: dict[str, str] = {}
    for table, spec in tables.items():
        _table(table, "Each table")
        if not isinstance(spec, dict):
            raise ValueError(f"tables.{table} must be an object.")
        if "primary_key" in spec:
            keys[table] = _columns(spec["primary_key"], f"tables.{table}.primary_key")
        if "source" in spec:
            if not isinstance(spec["source"], str) or len(spec["source"]) > 500:
                raise ValueError(f"tables.{table}.source must be text.")
            sources[table] = spec["source"]
    rels = raw.get("relationships", [])
    if not isinstance(rels, list) or len(rels) > MAX_RELATIONSHIPS:
        raise ValueError(f"relationships must be a list, at most {MAX_RELATIONSHIPS}.")
    out: list[Relationship] = []
    for i, r in enumerate(rels):
        if not isinstance(r, dict):
            raise ValueError(f"relationships[{i}] must be an object.")
        rel = Relationship(
            _table(r.get("from"), f"relationships[{i}].from"),
            _columns(r.get("from_columns"), f"relationships[{i}].from_columns"),
            _table(r.get("to"), f"relationships[{i}].to"),
            _columns(r.get("to_columns"), f"relationships[{i}].to_columns"),
            r.get("kind", "many-to-one"),
            r.get("note", ""),
        )
        if not isinstance(rel.note, str) or len(rel.note) > 200:
            raise ValueError(f"relationships[{i}].note must be text, at most 200 characters.")
        if len(rel.from_columns) != len(rel.to_columns):
            raise ValueError(f"relationships[{i}] needs as many from_columns as to_columns.")
        if rel.kind not in KINDS:
            raise ValueError(f"relationships[{i}].kind must be one of {', '.join(KINDS)}.")
        out.append(rel)
    return Model(name, description, keys, sources, tuple(out))


def merge(base: Model, extra: Model) -> Model:
    """`extra` on top of `base`: its keys and sources win, its relationships replace the same join."""
    rels = [r for r in base.relationships if not any(r.same_join(e) for e in extra.relationships)]
    names = [n for n in (base.name, extra.name) if n and n != "current"]
    return Model(
        name=" + ".join(dict.fromkeys(names)) or "current",
        description=extra.description or base.description,
        keys={**base.keys, **extra.keys},
        sources={**base.sources, **extra.sources},
        relationships=(*rels, *extra.relationships),
    )


_MERMAID_SAFE = re.compile(r"[^A-Za-z0-9_]")
_CARDINALITY = {
    "many-to-one": "}o--||",
    "one-to-many": "||--o{",
    "one-to-one": "||--||",
    "many-to-many": "}o--o{",
}
# The same join written from the other table: the crow's feet stay on the "many" side.
_REVERSED = {
    "many-to-one": "||--o{",
    "one-to-many": "}o--||",
    "one-to-one": "||--||",
    "many-to-many": "}o--o{",
}


def _ident(name: str) -> str:
    return _MERMAID_SAFE.sub("_", name) or "_"


def mermaid_erd(model: Model, columns: dict[str, list[tuple[str, str]]], only: set[str] | None = None) -> str:
    """The model as a Mermaid erDiagram: one box per table holding only its key columns (PK, and
    FK for columns a join uses), with crow's-foot lines for the joins. `columns` gives each open
    table's (name, type) list for the types; `only` limits the diagram to those tables."""
    tables = sorted(t for t in model.tables() if only is None or t in only)
    fk: dict[str, set[str]] = {}
    for r in model.relationships:
        fk.setdefault(r.from_table, set()).update(r.from_columns)
        fk.setdefault(r.to_table, set()).update(r.to_columns)
    lines = ["erDiagram"]
    for table in tables:
        pk = set(model.keys.get(table, ()))
        keys = pk | fk.get(table, set())
        types = dict(columns.get(table, []))
        # Keys only, in the table's own column order where it is open (else the key's order).
        order = [name for name, _ in columns.get(table, [])] or [
            *model.keys.get(table, ()),
            *sorted(fk.get(table, set())),
        ]
        cols = [(name, types.get(name, "key")) for name in dict.fromkeys(order) if name in keys]
        lines.append(f"    {_ident(table)} {{")
        for name, type_name in cols:
            marks = ",".join(m for m, on in (("PK", name in pk), ("FK", name in fk.get(table, set()))) if on)
            word = _ident(type_name.split("(")[0].lower()) or "value"
            lines.append(f"        {word} {_ident(name)}{' ' + marks if marks else ''}")
        lines.append("    }")
    drawn = [r for r in model.relationships if r.from_table in tables and r.to_table in tables]
    # The tables that join to the most other tables go in the center. Mermaid ranks boxes along
    # the way each line is written (the first table above the second), so spokes all pointing at
    # a hub would sink it to the bottom row. Each join belongs to the end that joins more tables
    # (a hub joins 3 or more), and each hub writes every other spoke from its own side (the same
    # join, its ends swapped): half its spokes rank above it and half below, the hub in between.
    neighbors: dict[str, set[str]] = {}
    for r in drawn:
        if r.from_table != r.to_table:
            neighbors.setdefault(r.from_table, set()).add(r.to_table)
            neighbors.setdefault(r.to_table, set()).add(r.from_table)
    degree = {t: len(n) for t, n in neighbors.items()}
    spokes: dict[str, int] = {}
    for r in drawn:
        # A line carries only the join's note (an outlier or caveat), never column names: the
        # boxes already show the keys. Mermaid needs a label, so a join without a note gets "".
        # One line: Mermaid ends a statement at a line break.
        label = " ".join(r.note.replace('"', "'").split())
        left, ends, right = r.from_table, _CARDINALITY[r.kind], r.to_table
        hub = max((left, right), key=lambda t: (degree.get(t, 0), t))
        if left != right and degree.get(hub, 0) >= 3:
            spokes[hub] = spokes.get(hub, 0) + 1
            hub_first = spokes[hub] % 2 == 0  # odd spokes rank above the hub, even ones below
            if (left == hub) != hub_first:
                left, ends, right = right, _REVERSED[r.kind], left
        lines.append(f'    {_ident(left)} {ends} {_ident(right)} : "{label}"')
    return "\n".join(lines)


# ===== Key Finder: which columns of another table share values with this one? (a join-key search) =====
#
# The job a data architect otherwise does by hand: take every column of a table, take its
# distinct values, and find the columns of another table holding the same values. The column
# pairs with the most overlap are the likely join keys.
#
# How it works, in DuckDB:
# 1. Each side's columns are profiled in one pass: the longest value, the distinct count, the
#    non-blank count. Only columns whose longest value is at most `max_length` characters (128
#    by default) take part; longer text is notes and descriptions, never a key. True/false,
#    list, struct and binary columns are skipped too.
# 2. Every value is compared as normalized text, so 42 (a number), 42.0 and '42' all meet:
#    whole numbers lose their decimals, a timestamp at midnight reads as its date, and text is
#    trimmed and/or lowercased when the switches say so.
# 3. Each side becomes one two-column list (column number, distinct value), and one hash join on
#    the value counts the shared values for every column pair at once. Think of it as a
#    GROUP BY over an inverted index of both tables.
#
# For each pair it reports the shared distinct values, the share of each side's values found in
# the other, whether each side is unique per row (so which one looks like the primary key), and
# a score:
#
#     overlap = shared / min(distinct on either side)          (the overlap coefficient)
#     cover   = min(share of source found, share of target found)
#     score   = (0.7 x overlap + 0.3 x cover) x min(1, log10(shared) / 3)
#
# Overlap is 1.0 when one side's values all appear in the other (a foreign key into a primary
# key). Cover breaks the classic tie: two unrelated running counters (1, 2, 3 ...) also overlap
# completely, but a 6,000-row order_id covers a 2,000-row customer id only a third of the way,
# while the real customer_id covers it 95%. The last factor (full weight from 1,000 shared
# values) keeps a Y/N flag or a five-value status code, which overlap by accident, below a key.
#
# It also answers the question asked of a table on its own: which columns together tell every
# row apart (its primary key, often a composite one)? `most_distinct` reads the grid's first rows
# and builds the combination greedily: the column with the most distinct values first, then each
# round the column that separates the most rows still sharing a combination, until every row is
# unique or no column helps. Greedy, not exhaustive: every combination of 50 columns is far too
# many to count, while each round here is one scan, so the answer comes in seconds. A blank is a
# value of its own there (as in GROUP BY), since two rows differing only by a blank are different
# rows.


DEFAULT_MAX_LENGTH: int = 128


_SKIPPED_FAMILIES: frozenset[str] = frozenset({"boolean", "other"})


# The most distinct combination stops growing here; a key wider than this isn't a useful key.
MAX_KEY_COLUMNS: int = 8


MAX_ALTERNATIVES: int = 10


@dataclass(frozen=True)
class KeyOptions:
    trim: bool = True
    ignore_case: bool = False
    max_length: int = DEFAULT_MAX_LENGTH

    @staticmethod
    def from_json(raw: Any) -> KeyOptions:
        raw = raw if isinstance(raw, dict) else {}
        max_length = raw.get("max_length", DEFAULT_MAX_LENGTH)
        if not isinstance(max_length, int) or isinstance(max_length, bool) or not 1 <= max_length <= 4096:
            raise ValueError("max_length must be a whole number from 1 to 4,096.")
        return KeyOptions(bool(raw.get("trim", True)), bool(raw.get("ignore_case", False)), max_length)


@dataclass(frozen=True)
class ColumnStats:
    name: str
    type: str
    max_length: int | None
    distinct: int
    filled: int  # non-blank rows

    @property
    def unique(self) -> bool:
        return self.filled > 0 and self.distinct == self.filled


def normalized(column: Column, options: KeyOptions) -> str | None:
    """The SQL for one column's values as comparable text, or None if the column can't be a key."""
    family = type_family(column.type)
    if family in _SKIPPED_FAMILIES:
        return None
    c = quote(column.name)
    if family == "number":
        if column.type.upper().startswith(("FLOAT", "DOUBLE", "REAL", "DECIMAL")):
            # 42.0 and 42.00 read as 42, like an integer column's 42; a DECIMAL's 1.50 reads as
            # 1.5, as a DOUBLE's 1.5 does (its text keeps every decimal place).
            other = f"CAST({c} AS VARCHAR)"
            if column.type.upper().startswith("DECIMAL"):
                other = f"rtrim({other}, '0')"
            return (
                f"CASE WHEN {c} = trunc({c}) AND abs({c}) < 1e18 "
                f"THEN CAST(CAST({c} AS HUGEINT) AS VARCHAR) ELSE {other} END"
            )
        return f"CAST({c} AS VARCHAR)"
    if family == "timestamp":
        return (
            f"CASE WHEN {c} = date_trunc('day', {c}) THEN CAST(CAST({c} AS DATE) AS VARCHAR) "
            f"ELSE CAST({c} AS VARCHAR) END"
        )
    if family == "date":
        return f"CAST({c} AS VARCHAR)"
    text = c
    if options.trim:
        text = f"trim({text}, ' ' || chr(9) || chr(13) || chr(10))"
    if options.ignore_case:
        text = f"lower({text})"
    return f"NULLIF({text}, '')"


def _value_list(relation: str, eligible: list[tuple[ColumnStats, str]]) -> str:
    return " UNION ALL ".join(
        f"SELECT DISTINCT {i} AS c, {sql} AS v FROM ({relation}) WHERE {sql} IS NOT NULL"
        for i, (_, sql) in enumerate(eligible)
    )


def score(shared: int, source_distinct: int, target_distinct: int) -> float:
    if not shared:
        return 0.0
    overlap = shared / max(1, min(source_distinct, target_distinct))
    cover = shared / max(1, source_distinct, target_distinct)
    confidence = min(1.0, math.log10(shared) / 3) if shared > 1 else 0.05
    return round((0.7 * overlap + 0.3 * cover) * confidence, 4)


def relationship(source: ColumnStats, target: ColumnStats) -> str:
    if source.unique and target.unique:
        return "one to one"
    if target.unique:
        return "many to one (the target looks like the key)"
    if source.unique:
        return "one to many (this column looks like the key)"
    return "many to many"


# ===== Compare two grids row by row on key columns =====
#
# The rule: the two grids are always aligned. Compare row N is one left-view row and one right-view
# row with the same key, or one of them with a blank placeholder beside it, so a person reads straight
# across and sees every match. Both panes read the same mapping, so row N lines up on both sides.
#
# Each side is its grid's view (after its own steps), as a positioned relation (__sag_pos = the
# row's place in that view). The mapping is one full outer join on the keys, compared the way the
# Key Finder compares them (`normalized` above: 42, 42.0 and '42' meet; text trimmed), and
# sorted by key: number-looking keys as numbers (9 before 10), then as text, blanks last.
#
#     mapping row:  left position (or NULL)  |  right position (or NULL)
#
# Modes:
#   outer  every key from both sides; duplicate keys pair every row with every row, as SQL does
#   anti   only the rows whose key has no match: on both sides, or the left or the right only
#   cross  no mapping: each side shows its own view, and a row picked on one side filters the
#          other side to the rows with that key
#
# A blank key never matches anything (SQL), so its rows show as unmatched on their own side.


MODES: tuple[str, ...] = ("outer", "anti", "cross")


ANTI_SIDES: tuple[str, ...] = ("both", "left", "right")


MAX_KEYS: int = 8


@dataclass(frozen=True)
class CompareSpec:
    keys: tuple[tuple[str, str], ...]  # (left column, right column) per key part
    mode: str = "outer"
    anti_side: str = "both"

    @staticmethod
    def from_json(raw: Any) -> CompareSpec:
        if not isinstance(raw, dict):
            raise ValueError('A compare is {"keys": [[left, right], ...], "mode": "outer"}.')
        keys = raw.get("keys")
        if (
            not isinstance(keys, list)
            or not 1 <= len(keys) <= MAX_KEYS
            or not all(
                isinstance(k, list) and len(k) == 2 and all(isinstance(n, str) for n in k) for k in keys
            )
        ):
            raise ValueError(f"keys must be 1 to {MAX_KEYS} [left column, right column] pairs.")
        mode = raw.get("mode", "outer")
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}.")
        side = raw.get("anti_side", "both")
        if side not in ANTI_SIDES:
            raise ValueError(f"anti_side must be one of {', '.join(ANTI_SIDES)}.")
        return CompareSpec(tuple((k[0], k[1]) for k in keys), mode, side)

    def to_json(self) -> dict[str, Any]:
        return {"keys": [list(k) for k in self.keys], "mode": self.mode, "anti_side": self.anti_side}


def _key_expressions(columns: tuple[Column, ...], names: list[str], side: str) -> list[str]:
    by_name = {c.name: c for c in columns}
    out: list[str] = []
    for name in names:
        column = by_name.get(name)
        if column is None:
            raise ValueError(f"{name!r} is not a column of the {side} grid's view.")
        sql = normalized(column, KeyOptions())
        if sql is None:
            raise ValueError(f"{name!r} ({column.type}) can't be a key.")
        out.append(sql)
    return out


def keyed_sql(positioned: str, columns: tuple[Column, ...], names: list[str], side: str) -> str:
    """A side as (__sag_pos, k0, k1, ...): its position and its normalized key values."""
    keys = _key_expressions(columns, names, side)
    parts = ", ".join(f"{sql} AS k{i}" for i, sql in enumerate(keys))
    return f"SELECT {POS}, {parts} FROM ({positioned})"


def _joined(left: str, right: str, n: int) -> str:
    on = " AND ".join(f"l.k{i} = r.k{i}" for i in range(n))
    sort = ", ".join(f"coalesce(l.k{i}, r.k{i}) AS s{i}" for i in range(n))
    return (
        f"SELECT l.{POS} AS lp, r.{POS} AS rp, {sort} "
        f"FROM ({left}) AS l FULL OUTER JOIN ({right}) AS r ON {on}"
    )


def mapping_sql(
    left_positioned: str,
    left_columns: tuple[Column, ...],
    right_positioned: str,
    right_columns: tuple[Column, ...],
    spec: CompareSpec,
) -> str:
    """The aligned mapping (lp, rp, s0, s1, ...) in compare order, for outer and anti mode. The s
    columns are the row's normalized key, so the rows sharing a key can be found again."""
    left = keyed_sql(left_positioned, left_columns, [k[0] for k in spec.keys], "left")
    right = keyed_sql(right_positioned, right_columns, [k[1] for k in spec.keys], "right")
    n = len(spec.keys)
    where = ""
    if spec.mode == "anti":
        where = {
            "both": "WHERE lp IS NULL OR rp IS NULL",
            "left": "WHERE rp IS NULL",
            "right": "WHERE lp IS NULL",
        }[spec.anti_side]
    order = ", ".join(f"TRY_CAST(s{i} AS DOUBLE) NULLS LAST, s{i} NULLS LAST" for i in range(n))
    return (
        f"SELECT lp, rp, {', '.join(f's{i}' for i in range(n))} FROM ({_joined(left, right, n)}) {where} "
        f"ORDER BY {order}, lp NULLS LAST, rp NULLS LAST"
    )


def differences(
    left_rows: list[list[Any] | None],
    left_columns: tuple[Column, ...],
    right_rows: list[list[Any] | None],
    right_columns: tuple[Column, ...],
    keys: tuple[tuple[str, str], ...],
) -> tuple[list[list[int]], list[list[int]]]:
    """Cells that differ in same-named, non-key columns of an aligned pair: [[row offset, column
    index], ...] for each side. Numbers that differ only in their last bits count as equal."""
    key_names = {k[0] for k in keys} | {k[1] for k in keys}
    right_at = {c.name: i for i, c in enumerate(right_columns)}
    shared = [
        (i, right_at[c.name])
        for i, c in enumerate(left_columns)
        if c.name in right_at and c.name not in key_names
    ]
    left_diff: list[list[int]] = []
    right_diff: list[list[int]] = []
    for offset, (lrow, rrow) in enumerate(zip(left_rows, right_rows, strict=True)):
        if lrow is None or rrow is None:
            continue
        for li, ri in shared:
            if not _same(lrow[li], rrow[ri]):
                left_diff.append([offset, li])
                right_diff.append([offset, ri])
    return left_diff, right_diff


def _same(a: Any, b: Any) -> bool:
    """Whether two cells hold the same value. Integers and decimals compare exactly; when either
    is a float, numbers within a relative 1e-9 are the same (0.1 + 0.2 and 0.3), as are two NaNs
    (one value to DuckDB) and two infinities of one sign."""
    if a is None or b is None:
        return a is None and b is None
    if _is_number(a) and _is_number(b):
        if not isinstance(a, float) and not isinstance(b, float):
            return bool(a == b)
        x, y = float(a), float(b)
        if math.isnan(x) or math.isnan(y):
            return math.isnan(x) and math.isnan(y)
        return x == y or math.isclose(x, y, rel_tol=1e-9, abs_tol=0.0)
    return bool(a == b) or str(a) == str(b)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)
