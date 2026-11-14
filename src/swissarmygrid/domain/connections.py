"""Connections: the blades that reach a source of tables or passwords, seen and managed apart from blades.

A connection is a database blade (kind `sql`: its code makes a connection, its dialect says whose SQL),
a file blade (made from a file starter: a CSV, a workbook, a Parquet or JSON file, a folder of them;
its code makes the path) or a key vault (kind `vault`: its code reads a secret by name; each secret it
maps becomes an environment variable in every Workbench kernel, the owner's call, 2026-11-14). They
live in the blade store (one store: secrets, vaults, catalogs,
folders, packs and the portable file all work the same); what this module decides is which blades are
connections, what kind each is, and how a starter becomes the Add connection form. Pure rules: no I/O.
"""

from __future__ import annotations

import builtins
import re
import unicodedata
from typing import Any

from swissarmygrid.domain.blades import env_name_for_secret

__all__ = ["env_name_for_secret"]  # with the rules below: a vault's secret as a variable

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
    """The connection a blade is: database, file, vault, or None (a secret, an analysis, other code)."""
    if kind == "sql":
        return "database"
    if kind == "vault":
        return "vault"
    if kind == "other" and template in FILE_STARTERS:
        return "file"
    return None


def is_connection(kind: str, template: str | None) -> bool:
    return connection_kind(kind, template) is not None


def opens_in_code(connection: str | None) -> bool:
    """Whether the connection is a variable in the Workbench and a SQL cell's source: a database or a
    file. A vault is not: its secrets are already environment variables there."""
    return connection in ("database", "file")


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
        "vault_mapping": list(template.get("vault_mapping") or []),
        "vault_all": bool(template.get("vault_all")),
    }


# ===== where a password comes from =====

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def check_env_name(name: Any) -> str:
    """An environment variable's name, as a password's source (WAREHOUSE_PASSWORD)."""
    if not isinstance(name, str) or not _ENV_NAME.match(name.strip()):
        raise ValueError(
            "An environment variable's name is letters, digits and _, not starting with a digit."
        )
    return name.strip()


def env_name_for(connection: str, secret: str) -> str:
    """A variable's name for a connection's password: Sales DB, password -> SALES_DB_PASSWORD."""
    words = re.sub(r"[^A-Za-z0-9]+", "_", f"{connection}_{secret}").strip("_").upper()
    return words if words and not words[0].isdigit() else f"_{words}"


MAX_VAULT_NAMES: int = 5_000


def vault_names(found: Any) -> list[dict[str, str]]:
    """What a vault's names() gave, as rows to map: each secret's name and a variable for it. Sorted,
    each once, at most five thousand (a name is not a secret; no value is read)."""
    if not isinstance(found, list):
        return []
    names = sorted({str(n) for n in found if isinstance(n, str | int) and str(n).strip()}, key=str.casefold)
    return [{"secret": n, "env": env_name_for_secret(n)} for n in names[:MAX_VAULT_NAMES]]


def rename_secret(code: str, old: str, new: str) -> str:
    """The code with every secret("old") asking for secret("new") instead: the value then comes from the
    keyring saved under that name, or else the environment variable of that name (a vault sets it)."""
    pattern = re.compile(r"""(\bsecret\(\s*)(["'])""" + re.escape(old) + r"""\2(\s*\))""")
    changed, count = pattern.subn(lambda m: f"{m.group(1)}{m.group(2)}{new}{m.group(2)}{m.group(3)}", code)
    if not count:
        raise ValueError(f'The code does not ask for secret("{old}").')
    return changed


# ===== each connection's name in code (the Workbench: every connection, always there) =====


def code_names(names: list[str]) -> dict[str, str]:
    """Each connection's variable in a Workbench notebook or script: lower case, words joined by _, a
    Python name (Sales DB -> sales_db, 2024 Budget -> c_2024_budget, class -> class_), unique (orders,
    orders_2). In the order given; the first to claim a name keeps it."""
    taken: set[str] = set()
    out: dict[str, str] = {}
    for name in names:
        plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()  # Café -> Cafe
        base = re.sub(r"[^0-9a-zA-Z]+", "_", plain).strip("_").lower() or "connection"
        if base[0].isdigit():
            base = f"c_{base}"
        if base in _PYTHON_WORDS:
            base += "_"
        chosen, n = base, 2
        while chosen in taken:
            chosen, n = f"{base}_{n}", n + 1
        taken.add(chosen)
        out[name] = chosen
    return out


# Never a connection's name in code: Python's words, its built-ins (a connection named Print or List must
# not hide print or list in every kernel), and the names every notebook leans on.
_PYTHON_WORDS = (
    frozenset(
        "False None True and as assert async await break class continue def del elif else except finally for "
        "from global if import in is lambda nonlocal not or pass raise return try while with yield".split()
    )
    | frozenset(name.lower() for name in dir(builtins))
    | frozenset({"sag", "pd", "np", "plt", "df", "display", "get_ipython", "in", "out"})
)
