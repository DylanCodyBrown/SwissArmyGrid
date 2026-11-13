"""Tab completion of catalog tables and columns inside SQL, in Jupyter and the IPython terminal.

When the cursor is inside a string that reads as SQL (a SELECT, FROM, JOIN...), or in a `%%sql` cell,
Tab offers the loaded catalogs' names: `ord<Tab>` gives tables (and columns) starting so, and
`orders.cu<Tab>` the columns of orders starting with cu (or `sales.ord<Tab>` the tables of sales).
Columns of the tables the query already names come first.

Installed by itself when swissarmygrid is imported in Jupyter or IPython (SWISSARMYGRID_COMPLETE=0
turns it off), or by hand with `sag.complete()`. It asks the running server and never starts one: with
no server, or a slow one, Tab simply offers nothing from here. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from swissarmygrid.client.push_client import _read_info

_TIMEOUT_SECONDS: float = 1.0
_CATALOGS_FOR_SECONDS: float = 30.0
_MAX_SUGGESTIONS: int = 100
_SQL_WORDS = re.compile(
    r"\b(select|from|join|where|group\s+by|order\s+by|having|insert\s+into|update|with)\b", re.I
)
_NAMED = re.compile(r"\b(?:from|join|update|into)\s+([A-Za-z_][\w.]*)", re.I)
_installed: list[Any] = []
_catalogs: tuple[float, list[str]] = (0.0, [])


def sql_before(text: str) -> str | None:
    """The SQL written so far when the cursor (the end of `text`) is inside SQL, else None: a `%%sql`
    cell, or an open string literal (triple-quoted or on this line) that holds a SQL word."""
    if text.lstrip().startswith(("%%sql", "%sql")):
        return text
    opened: str | None = None
    start = 0
    i = 0
    while i < len(text):
        for quote in ('"""', "'''", '"', "'"):
            if text.startswith(quote, i):
                break
        else:
            i += 1
            continue
        if opened is None:
            if len(quote) == 1 and "\n" in text[i:]:
                end = text.find(quote, i + 1, text.find("\n", i))
                if end != -1:  # a one-line string that closes before the line ends
                    i = end + 1
                    continue
            opened, start = quote, i + len(quote)
        elif quote == opened:
            opened = None
        i += len(quote)
    if opened is None:
        return None
    inside = text[start:]
    return inside if _SQL_WORDS.search(inside) else None


def _get(path: str) -> Any:
    info = _read_info()
    if not info:
        return None
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}{path}", headers={"Authorization": f"Bearer {info['token']}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310 (fixed http://127.0.0.1 URL)
            return json.loads(response.read() or b"null")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _catalog_names() -> list[str]:
    global _catalogs
    when, names = _catalogs
    if time.monotonic() - when < _CATALOGS_FOR_SECONDS:
        return names
    listed = _get("/catalogs") or {}
    names = [str(c["name"]) for c in listed.get("catalogs", []) if isinstance(c, dict) and "name" in c]
    _catalogs = (time.monotonic(), names)
    return names


def _search(catalog: str, text: str) -> dict[str, Any]:
    found = _get(
        f"/catalogs/{urllib.parse.quote(catalog, safe='')}/search?q={urllib.parse.quote(text)}&limit=200"
    )
    return found if isinstance(found, dict) else {}


def suggestions(token: str, sql: str) -> list[str]:
    """What `token` can become, from every loaded catalog."""
    if not token or len(token) > 200:
        return []
    named = {n.split(".")[-1].lower() for n in _NAMED.findall(sql)}
    first: list[str] = []
    rest: list[str] = []
    head, dot, tail = token.rpartition(".")
    for catalog in _catalog_names():
        found = _search(catalog, token)
        if dot:
            owner, start = head.split(".")[-1].lower(), tail.lower()
            for c in found.get("columns", []):
                if c["table"].lower() == owner and c["column"].lower().startswith(start):
                    first.append(f"{head}.{c['column']}")
            for t in found.get("tables", []):
                if (t["schema"] or "").lower() == owner and t["table"].lower().startswith(start):
                    rest.append(f"{head}.{t['table']}")
            continue
        start = token.lower()
        for t in found.get("tables", []):
            if t["table"].lower().startswith(start):
                rest.append(t["table"])
        for c in found.get("columns", []):
            if c["column"].lower().startswith(start):
                (first if c["table"].lower() in named else rest).append(c["column"])
    return list(dict.fromkeys([*first, *rest]))[:_MAX_SUGGESTIONS]


def _matcher(shell: Any) -> Any:
    def complete_catalog(text: str) -> list[str]:
        try:
            before = str(getattr(shell.Completer, "text_until_cursor", "") or "")
            sql = sql_before(before)
            return suggestions(text, sql) if sql is not None else []
        except Exception:  # completion must never break typing
            return []

    return complete_catalog


def complete() -> bool:
    """Turn on catalog completion in this Jupyter or IPython session. False outside IPython."""
    module = sys.modules.get("IPython")
    get = getattr(module, "get_ipython", None) if module is not None else None
    shell = get() if callable(get) else None
    if shell is None or not hasattr(shell, "Completer"):
        return False
    if any(m is shell for m in _installed):
        return True
    shell.Completer.custom_matchers.append(_matcher(shell))
    _installed.append(shell)
    return True


def autostart() -> None:
    """On import in Jupyter or IPython, unless SWISSARMYGRID_COMPLETE=0."""
    if os.environ.get("SWISSARMYGRID_COMPLETE", "") != "0":
        complete()
