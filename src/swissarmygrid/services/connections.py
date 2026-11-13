"""Connections: the databases and files the user reaches, apart from blades (domain/connections.py).

A connection is kept in the blade store, so this service lists, saves and tests through the blade
service; what it adds is the split (only connections here, and the blades lists without them) and
the Add connection form: each database and file starter as fields, passwords and the code they fill.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from swissarmygrid.domain import connections as rules
from swissarmygrid.domain.blades import BladeError, Template
from swissarmygrid.services.blades import BladeService


class ConnectionService:
    def __init__(self, blades: BladeService, templates: Sequence[Template]) -> None:
        self._blades = blades
        self._templates = templates

    def listing(self) -> dict[str, Any]:
        """Every connection: databases first, then files, each with what it is."""
        labels = {t.id: t.label for t in self._templates}
        found = []
        for b in self._blades.listing()["blades"]:
            kind = rules.connection_kind(str(b["kind"]), b.get("template"))
            if kind:
                found.append(b | {"connection": kind, "type": labels.get(str(b.get("template") or ""), "")})
        found.sort(
            key=lambda c: (c["connection"] != "database", not c.get("pinned"), str(c["name"]).casefold())
        )
        return {"connections": found}

    def types(self) -> dict[str, Any]:
        """The starters a connection is made from, each as the Add connection form."""
        out = []
        for t in self._templates:
            if rules.is_connection(t.kind, t.id):
                out.append(rules.form(t.to_json(), self._blades.inspect(t.code, t.id)))
        return {"types": out}

    def details(self, name: str) -> dict[str, Any]:
        """A saved connection as the form shows it to change: its own code's fields and passwords."""
        blade = self._blades.get(name)
        kind = rules.connection_kind(blade.kind, blade.template)
        if kind is None:
            raise BladeError(f"{blade.name} is a blade, not a connection: open it on the blades page.")
        inspected = self._blades.inspect(blade.code, blade.template, blade.name)
        template = {
            "id": blade.template or "",
            "label": next((t.label for t in self._templates if t.id == blade.template), blade.dialect),
            "group": "Databases" if kind == "database" else "Files",
            "kind": blade.kind,
            "dialect": blade.dialect,
            "variable": blade.variable or "",
            "catalog": {"language": blade.catalog_language, "command": blade.catalog_command}
            if blade.has_catalog
            else None,
            "code": blade.code,
        }
        return rules.form(template, inspected) | {
            "name": blade.name,
            "description": blade.description,
            "tags": blade.tags,
            "summary": self._blades.summary(blade),
        }

    def _check(self, body: Any) -> None:
        if not isinstance(body, dict):
            raise BladeError("Send the connection as a JSON object.")
        if not rules.is_connection(str(body.get("kind", "")), body.get("template")):
            raise BladeError(
                "A connection is made from a database or file starter. Other code is saved as a blade."
            )

    async def save(self, body: Any, previous: str | None) -> dict[str, Any]:
        self._check(body)
        return await self._blades.save(body, previous)

    async def test(self, body: Any) -> dict[str, Any]:
        """Run the form's code (not saved) with the passwords typed into it."""
        self._check(body)
        return await self._blades.test_draft(body)
