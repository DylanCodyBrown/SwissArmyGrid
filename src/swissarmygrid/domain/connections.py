"""Connections: the blades that reach a source of tables, seen and managed apart from blades.

A connection is a database blade (kind `sql`: its code makes a connection, its dialect says whose SQL)
or a file blade (made from a file starter: a CSV, a workbook, a Parquet or JSON file, a folder of
them; its code makes the path). They live in the blade store (one store: secrets, vaults, catalogs,
folders, packs and the portable file all work the same); what this module decides is which blades are
connections, what kind each is, and how a starter becomes the Add connection form. Pure rules: no I/O.
"""

from __future__ import annotations

from typing import Any

# The file starters (server/blade_templates.py FILES): a blade made from one is a file connection.
FILE_STARTERS: dict[str, str] = {
    "csv": "CSV file",
    "excel": "Workbook",
    "parquet": "Parquet file",
    "json": "JSON file",
    "folder": "Folder of files",
}
# A setting with one of these names holds a path the form offers a picker for.
_FILE_SETTINGS = frozenset({"path", "file", "dbq", "database_file"})
_FOLDER_SETTINGS = frozenset({"folder", "directory"})


def connection_kind(kind: str, template: str | None) -> str | None:
    """The connection a blade is: database, file, or None (a vault, a secret, an analysis, other code)."""
    if kind == "sql":
        return "database"
    if kind == "other" and template in FILE_STARTERS:
        return "file"
    return None


def is_connection(kind: str, template: str | None) -> bool:
    return connection_kind(kind, template) is not None


def picker(setting: dict[str, Any], template: str | None) -> str | None:
    """Which picker the form offers for this setting: file, folder, or None (typed)."""
    name, key = str(setting.get("name", "")).lower(), str(setting.get("key", "")).lower()
    if name in _FOLDER_SETTINGS or (template == "folder" and name in _FILE_SETTINGS):
        return "folder"
    if name in _FILE_SETTINGS or key in _FILE_SETTINGS:
        return "file"
    return None


def form(template: dict[str, Any], inspected: dict[str, Any]) -> dict[str, Any]:
    """A starter as the Add connection form: its fields (the code's settings, a picker where one is a
    path), its passwords (the code's secret names), and the code they are written into."""
    fields = [
        {**setting, "picker": picker(setting, template.get("id"))}
        for setting in inspected.get("settings", [])
    ]
    return {
        "id": template["id"],
        "label": template["label"],
        "group": template["group"],
        "connection": connection_kind(str(template["kind"]), str(template["id"])),
        "kind": template["kind"],
        "dialect": template.get("dialect", "other"),
        "variable": template["variable"],
        "catalog": template.get("catalog"),
        "note": template.get("note", ""),
        "code": template["code"],
        "fields": fields,
        "secrets": list(inspected.get("secrets", [])),
        "packages": inspected.get("packages", []),
        "install_here": inspected.get("install_here"),
    }
