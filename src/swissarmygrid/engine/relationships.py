"""Relationship models: a data model's primary keys and joins, as a JSON file the team shares.

A model names tables by their dataset name in the grid (what `sag.push(df, "cs_paid_claims")`
called them), so pushing the same tables again makes the model apply as-is:

    {
      "name": "latam_cession_statement",
      "description": "The Latam Cession Statement model views",
      "tables": {
        "cs_paid_claims": {"primary_key": ["claim_id"], "source": "hive.finance_analytics.fin_vw_..."}
      },
      "relationships": [
        {"from": "cs_paid_claims", "from_columns": ["report_name"],
         "to": "l4_cs_report_v2", "to_columns": ["report_name"], "kind": "many-to-one",
         "note": "case-insensitive in the views"}
      ]
    }

The grid uses it for free: primary-key columns are marked in the headers, and splitting two
tables that have a relationship pre-ticks its columns as the compare keys. It can also draw the
model as an ERD card (Mermaid erDiagram) in the Analysis Sandbox.

Like a C# record set: Model is immutable; merge() returns a new one, the later model winning
for any table's key and for a relationship between the same tables on the same columns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

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
        label = r.note.replace('"', "'")
        left, ends, right = r.from_table, _CARDINALITY[r.kind], r.to_table
        hub = max((left, right), key=lambda t: (degree.get(t, 0), t))
        if left != right and degree.get(hub, 0) >= 3:
            spokes[hub] = spokes.get(hub, 0) + 1
            hub_first = spokes[hub] % 2 == 0  # odd spokes rank above the hub, even ones below
            if (left == hub) != hub_first:
                left, ends, right = right, _REVERSED[r.kind], left
        lines.append(f'    {_ident(left)} {ends} {_ident(right)} : "{label}"')
    return "\n".join(lines)
