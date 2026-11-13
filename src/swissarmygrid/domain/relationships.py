"""Relationships: a data model's primary keys and joins; tangs (saved, shared sets of them); and the
finder that proposes them.

A saved set of relationships is a **tang** (the owner's name: the part of a blade that anchors it into
the handle, as a join anchors two tables), kept as a .tang file (below) that people share through the
same folders as blades. The original model JSON (no marker) still reads.

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

import json
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from swissarmygrid.domain.datasets import Column
from swissarmygrid.domain.diff import DiffOptions
from swissarmygrid.domain.grids import POS
from swissarmygrid.domain.steps import quote, type_family

KINDS: tuple[str, ...] = ("one-to-one", "one-to-many", "many-to-one", "many-to-many")
_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")  # a table: its dataset name
_TANG_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .+#&()\-]{0,63}$")  # a tang may have spaces
_TAG = re.compile(r"^[\w][\w .+#-]{0,39}$")
MAX_TAGS = 20
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
    # Of two joins between the same tables only one filters (Power BI keeps one path active).
    active: bool = True
    # Why it is trusted, from when it was found or last run: "99% of values found, 12 orphans".
    evidence: str = ""

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
            **({"active": False} if not self.active else {}),
            **({"evidence": self.evidence} if self.evidence else {}),
        }


@dataclass(frozen=True)
class Model:
    name: str = "current"
    description: str = ""
    keys: dict[str, tuple[str, ...]] = field(default_factory=dict)  # table -> primary key
    sources: dict[str, str] = field(default_factory=dict)  # table -> where it comes from
    relationships: tuple[Relationship, ...] = ()
    tags: tuple[str, ...] = ()
    # table -> its columns and types when saved: a run says when one has gone or changed type, and
    # an ERD draws from the tang alone.
    columns: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    saved_at: str = ""  # ISO time of the last save, given by the caller (the domain reads no clock)
    author: str = ""  # who saved it, as they wrote it; never read from the computer

    def to_json(self) -> dict[str, Any]:
        tables = sorted(set(self.keys) | set(self.sources) | set(self.columns))
        return {
            "name": self.name,
            "description": self.description,
            **({"tags": list(self.tags)} if self.tags else {}),
            **({"saved_at": self.saved_at} if self.saved_at else {}),
            **({"author": self.author} if self.author else {}),
            "tables": {
                t: {
                    **({"primary_key": list(self.keys[t])} if t in self.keys else {}),
                    **({"source": self.sources[t]} if t in self.sources else {}),
                    **({"columns": [list(c) for c in self.columns[t]]} if t in self.columns else {}),
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
    if not isinstance(name, str) or not _TANG_NAME.fullmatch(name):
        raise ValueError("name must be letters, digits, spaces, '_' or '-', up to 64 characters.")
    description = raw.get("description", "")
    if not isinstance(description, str) or len(description) > 2000:
        raise ValueError("description must be text, at most 2,000 characters.")
    tags = raw.get("tags", [])
    if (
        not isinstance(tags, list)
        or len(tags) > MAX_TAGS
        or not all(isinstance(t, str) and _TAG.fullmatch(t) for t in tags)
    ):
        raise ValueError(
            f"tags must be a list of words (letters, digits, spaces, . + # -), at most {MAX_TAGS}."
        )
    saved_at, author = raw.get("saved_at", ""), raw.get("author", "")
    if (
        not isinstance(saved_at, str)
        or len(saved_at) > 40
        or not isinstance(author, str)
        or len(author) > 100
    ):
        raise ValueError("saved_at and author must be short text.")
    columns: dict[str, tuple[tuple[str, str], ...]] = {}
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
        if "columns" in spec:
            listed = spec["columns"]
            if (
                not isinstance(listed, list)
                or len(listed) > 2000
                or not all(
                    isinstance(c, list)
                    and len(c) == 2
                    and all(isinstance(x, str) and 0 < len(x) <= 200 for x in c)
                    for c in listed
                )
            ):
                raise ValueError(f'tables.{table}.columns must be [["column", "TYPE"], ...].')
            columns[table] = tuple((c[0], c[1]) for c in listed)
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
            r.get("active", True),
            r.get("evidence", ""),
        )
        if not isinstance(rel.note, str) or len(rel.note) > 200:
            raise ValueError(f"relationships[{i}].note must be text, at most 200 characters.")
        if not isinstance(rel.active, bool):
            raise ValueError(f"relationships[{i}].active must be true or false.")
        if not isinstance(rel.evidence, str) or len(rel.evidence) > 300:
            raise ValueError(f"relationships[{i}].evidence must be text, at most 300 characters.")
        if len(rel.from_columns) != len(rel.to_columns):
            raise ValueError(f"relationships[{i}] needs as many from_columns as to_columns.")
        if rel.kind not in KINDS:
            raise ValueError(f"relationships[{i}].kind must be one of {', '.join(KINDS)}.")
        out.append(rel)
    return Model(
        name, description, keys, sources, tuple(out), tuple(dict.fromkeys(tags)), columns, saved_at, author
    )


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
        tags=tuple(dict.fromkeys((*base.tags, *extra.tags))),
        columns={**base.columns, **extra.columns},
    )


def conflicts(base: Model, extra: Model) -> list[str]:
    """Where `extra` changes what `base` says about the same join or key (applied later, it wins):
    plain sentences, so the window can say what changed hands."""
    out: list[str] = []
    for e in extra.relationships:
        for b in base.relationships:
            if b.same_join(e) and (b.kind, b.active) != (e.kind, e.active):
                out.append(
                    f"{e.from_table} -> {e.to_table}: {b.kind}{'' if b.active else ' (inactive)'} "
                    f"becomes {e.kind}{'' if e.active else ' (inactive)'}"
                )
    for table, key in extra.keys.items():
        if table in base.keys and base.keys[table] != key:
            out.append(f"{table}'s key: {', '.join(base.keys[table])} becomes {', '.join(key)}")
    return out


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


MODES: tuple[str, ...] = ("outer", "anti", "cross", "diff")


ANTI_SIDES: tuple[str, ...] = ("both", "left", "right")


MAX_KEYS: int = 8


@dataclass(frozen=True)
class CompareSpec:
    keys: tuple[tuple[str, str], ...]  # (left column, right column) per key part
    mode: str = "outer"
    anti_side: str = "both"
    diff: DiffOptions = field(default_factory=DiffOptions)  # how diff mode compares cells

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
        return CompareSpec(
            tuple((k[0], k[1]) for k in keys), mode, side, DiffOptions.from_json(raw.get("diff"))
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "keys": [list(k) for k in self.keys],
            "mode": self.mode,
            "anti_side": self.anti_side,
            "diff": self.diff.to_json(),
        }


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
    return (
        f"SELECT lp, rp, {', '.join(f's{i}' for i in range(n))} FROM ({_joined(left, right, n)}) {where} "
        f"ORDER BY {mapping_order(n)}"
    )


def mapping_order(keys: int) -> str:
    """Compare order: by key (numbers as numbers), then by each side's position."""
    order = ", ".join(f"TRY_CAST(s{i} AS DOUBLE) NULLS LAST, s{i} NULLS LAST" for i in range(keys))
    return f"{order}, lp NULLS LAST, rp NULLS LAST"


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


# ===== the relationship finder: which joins hold between the open tables =====
#
# The hardest question in the work (the owner): how do these tables relate? The Key Finder already
# counts, for every column pair of two tables, the shared distinct values (compared as normalized
# text), each side's distinct count and whether each side is unique. The finder turns those counts
# into suggestions a person accepts or rejects:
#
#   - The "one" side must be unique (a key). Both unique: one-to-one; else many-to-one.
#   - Containment: at least MIN_CONTAINMENT of the "many" side's distinct values are found in the
#     key (real keys have orphans: deleted accounts).
#   - Name likeness: customer_id ~ customers, home_store ~ stores, order_date ~ date_key, cust_key ~
#     customers. A key with few values (under SMALL_DOMAIN: status codes, months, categories) needs a
#     name match, or every small number column would join it.
#   - Coverage: the share of the key's values the other side uses. Two counters that both start at 1
#     (order_id, cust_key) contain each other by accident but cover little; with no name match, a
#     join needs real coverage.
#   - Two-column keys: a table told apart only by a pair (order_lines: order_id, line_no) is joined
#     from a table holding a pair with the same names (checked by the service with a query).
#   - Implied joins are dropped: returns.order_id -> orders is already returns -> order_lines ->
#     orders.
#   - Two joins between the same tables (orders.order_date and ship_date to calendar): the first column
#     of the table is active, the others inactive, as Power BI keeps one path.
#
# Scored on examples/sample_store.py's answer key: every true relationship, no decoy.

MIN_CONTAINMENT: float = 0.8
SMALL_DOMAIN: int = 50
NAME_MATCH: float = 0.5
MIN_COVERAGE: float = 0.5
_SUFFIX = re.compile(r"_?(id|key|code|no|num|number|nbr|ref)$")
_KEY_FAMILIES: frozenset[str] = frozenset({"integer", "text", "date", "timestamp", "other"})


def key_family(duckdb_type: str) -> str:
    """A column's type as the finder sees it: integer, float (never a key), text, date, timestamp,
    boolean (never a key) or other. A DECIMAL with no places counts as an integer."""
    t = duckdb_type.upper()
    if t.startswith(("FLOAT", "DOUBLE", "REAL")):
        return "float"
    if t.startswith("DECIMAL"):
        scale = re.search(r",\s*(\d+)\)", t)
        return "integer" if scale and scale.group(1) == "0" else "float"
    family = type_family(duckdb_type)
    return "integer" if family == "number" else family


def _stem(name: str) -> str:
    lowered = name.lower()
    return _SUFFIX.sub("", lowered) or lowered


def _singular(table: str) -> str:
    t = table.lower()
    if t.endswith("ies"):
        return t[:-3] + "y"
    if t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def name_likeness(source_column: str, target_table: str, target_column: str) -> float:
    """How much a column's name says it points at a table's key: 1 the same name, 0.9 the same stem
    or the table's name in it (customer_id -> customers), 0.6 a shortened stem (cust_key ->
    customers), 0 nothing."""
    a, b = source_column.lower(), target_column.lower()
    if a == b:
        return 1.0
    sa, sb, table = _stem(a), _stem(b), _singular(target_table)
    tokens = set(re.split(r"[_\W]+", a))
    if sa in (sb, table) or table in tokens or sb in tokens:
        return 0.9
    if len(sa) >= 3 and (table.startswith(sa) or sb.startswith(sa)):
        return 0.6
    return 0.0


@dataclass(frozen=True)
class Candidate:
    """One column (or column set) of a table found in another table's key, with the counts."""

    from_table: str
    from_columns: tuple[str, ...]
    to_table: str
    to_columns: tuple[str, ...]
    from_distinct: int
    to_distinct: int
    shared: int
    from_unique: bool
    to_unique: bool
    from_position: int = 0  # the first column's place in its table (the first of two paths is active)
    types: tuple[str, str] = ("", "")  # (from family, to family)


@dataclass(frozen=True)
class Suggestion:
    relationship: Relationship
    confidence: float
    containment: float
    coverage: float
    name: float
    orphans: int  # distinct values of the many side not found in the key
    reasons: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            **self.relationship.to_json(),
            "confidence": self.confidence,
            "containment": self.containment,
            "coverage": self.coverage,
            "name": self.name,
            "orphans": self.orphans,
            "reasons": list(self.reasons),
        }


def judge(c: Candidate) -> Suggestion | None:
    """A candidate join as a suggestion, or None when it is too likely an accident."""
    if not c.to_unique or not c.from_distinct or not c.to_distinct:
        return None
    if any(family not in _KEY_FAMILIES for family in c.types if family):
        return None  # floats, booleans, lists: never a key
    containment = c.shared / c.from_distinct
    coverage = c.shared / c.to_distinct
    if containment < MIN_CONTAINMENT:
        return None
    if len(c.from_columns) == 1:
        name = name_likeness(c.from_columns[0], c.to_table, c.to_columns[0])
    else:
        name = min(name_likeness(f, c.to_table, t) for f, t in zip(c.from_columns, c.to_columns, strict=True))
    one_to_one = c.from_unique
    if c.to_distinct < SMALL_DOMAIN and name < NAME_MATCH:
        return None  # a small number column inside a small key: an accident without a name
    if name < NAME_MATCH and coverage < (0.9 if one_to_one else MIN_COVERAGE):
        return None  # two counters from 1 contain each other but cover little
    reasons = [f"{containment:.0%} of {c.from_table}.{', '.join(c.from_columns)} values are in {c.to_table}"]
    reasons.append(f"{c.to_table}.{', '.join(c.to_columns)} is unique: one row per value")
    if name >= NAME_MATCH:
        reasons.append("the names match" if name == 1.0 else "the names point at it")
    if c.types[0] and c.types[1] and c.types[0] != c.types[1]:
        reasons.append(f"{c.types[0]} on one side, {c.types[1]} on the other")
    orphans = c.from_distinct - c.shared
    if orphans:
        reasons.append(f"{orphans:,} values have no match (orphans)")
    confidence = round(containment * (0.5 + 0.5 * max(name, min(coverage, 1.0))), 3)
    kind = "one-to-one" if one_to_one else "many-to-one"
    note = (
        "text on one side, a number on the other" if {c.types[0], c.types[1]} == {"text", "integer"} else ""
    )
    rel = Relationship(c.from_table, c.from_columns, c.to_table, c.to_columns, kind, note)
    return Suggestion(
        rel, confidence, round(containment, 4), round(coverage, 4), name, orphans, tuple(reasons)
    )


def _implied(s: Suggestion, others: list[Suggestion]) -> bool:
    """A.x -> C.y is implied when A.(..x..) -> B.(..x'..) and B.x' -> C.y are suggested too."""
    r = s.relationship
    for via in others:
        v = via.relationship
        if via is s or v.from_table != r.from_table or v.to_table == r.to_table:
            continue
        ok = True
        for x, y in zip(r.from_columns, r.to_columns, strict=True):
            if x not in v.from_columns:
                ok = False
                break
            x_b = v.to_columns[v.from_columns.index(x)]
            if not any(
                o.relationship.from_table == v.to_table
                and o.relationship.from_columns == (x_b,)
                and o.relationship.to_table == r.to_table
                and o.relationship.to_columns == (y,)
                for o in others
            ):
                ok = False
                break
        if ok:
            return True
    return False


def suggest(candidates: list[Candidate]) -> list[Suggestion]:
    """The joins worth suggesting, most confident first: judged, implied ones dropped, and of two
    joins between the same pair of tables all but the first column's marked inactive."""
    judged = [s for s in (judge(c) for c in candidates) if s is not None]
    kept = [s for s in judged if not _implied(s, judged)]
    position = {(c.from_table, c.from_columns, c.to_table, c.to_columns): c.from_position for c in candidates}
    seen: set[tuple[str, str]] = set()
    marked: list[Suggestion] = []
    for s in sorted(kept, key=lambda s: position.get(_key(s.relationship), 0)):
        pair = (s.relationship.from_table, s.relationship.to_table)
        if pair in seen:
            r = s.relationship
            s = Suggestion(
                Relationship(
                    r.from_table, r.from_columns, r.to_table, r.to_columns, r.kind,
                    r.note or "a second path between these tables", active=False,
                ),
                s.confidence, s.containment, s.coverage, s.name, s.orphans,
                (*s.reasons, "a second path between these tables: inactive, as only one can filter"),
            )  # fmt: skip
        seen.add(pair)
        marked.append(s)
    return sorted(
        marked, key=lambda s: (-s.confidence, s.relationship.from_table, s.relationship.from_columns)
    )


def _key(r: Relationship) -> tuple[str, tuple[str, ...], str, tuple[str, ...]]:
    return (r.from_table, r.from_columns, r.to_table, r.to_columns)


def path_between(relationships: tuple[Relationship, ...], start: str, end: str) -> list[Relationship]:
    """The shortest chain of joins from one table to another (either direction), [] when none."""
    if start == end:
        return []
    links: dict[str, list[tuple[str, Relationship]]] = {}
    for r in relationships:
        links.setdefault(r.from_table, []).append((r.to_table, r))
        links.setdefault(r.to_table, []).append((r.from_table, r))
    came: dict[str, tuple[str, Relationship]] = {}
    queue = [start]
    seen = {start}
    while queue:
        table = queue.pop(0)
        for nxt, r in links.get(table, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            came[nxt] = (table, r)
            if nxt == end:
                chain: list[Relationship] = []
                at = end
                while at != start:
                    prev, rel = came[at]
                    chain.append(rel)
                    at = prev
                return chain[::-1]
            queue.append(nxt)
    return []


# ===== tangs: a saved set of relationships, as one .tang file =====
#
# The owner named them: a tang is the part of a blade that anchors it into the handle, as a join
# anchors two tables. A .tang file is JSON with a marker of its own first, so it is never taken for
# another JSON file, and its own extension:
#
#     {
#       "swissarmygrid": "tang", "format": 1,
#       "name": "Sample store", "description": "...", "tags": ["sales"],
#       "tables": {"orders": {"primary_key": ["order_id"], "columns": [["order_id", "BIGINT"], ...]}},
#       "relationships": [{"from": "orders", "from_columns": ["customer_id"], "to": "customers",
#                          "to_columns": ["cust_key"], "kind": "many-to-one", "active": true,
#                          "note": "about 1% orphans"}]
#     }
#
# The columns and their types are what the tables held when the tang was saved: running a tang
# against the open tables says when a column has gone or changed type, and an ERD can be drawn
# from the tang alone.

TANG_MARK = "tang"
TANG_FORMAT = 1
TANG_SUFFIX = ".tang"


class TangError(ValueError):
    """A file that is not a tang, or a tang that cannot be read. The message says why."""


def tang_file_name(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_").lower() or "tang"
    return stem + TANG_SUFFIX


def write_tang(model: Model) -> str:
    """The model as a .tang file's text."""
    body = model.to_json()
    return (
        json.dumps({"swissarmygrid": TANG_MARK, "format": TANG_FORMAT, **body}, indent=2, ensure_ascii=False)
        + "\n"
    )


def read_tang(text: str) -> Model:
    """A .tang file's text as a model. Raises TangError for anything else: plain JSON, another
    program's file, a newer format, or a tang that does not hold together."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise TangError(
            f"This is not a tang: it is not readable ({error.msg}, line {error.lineno})."
        ) from None
    if not isinstance(raw, dict) or raw.get("swissarmygrid") != TANG_MARK:
        raise TangError('This is not a tang: it does not start with "swissarmygrid": "tang".')
    if raw.get("format") != TANG_FORMAT:
        raise TangError(
            f"This tang is in format {raw.get('format')!r}; this SwissArmyGrid reads format {TANG_FORMAT}."
        )
    try:
        return parse_model({k: v for k, v in raw.items() if k not in ("swissarmygrid", "format")})
    except ValueError as error:
        raise TangError(f"This tang does not hold together: {error}") from None
