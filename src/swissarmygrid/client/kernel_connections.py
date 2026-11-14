"""Every connection, always there, in the Workbench's kernels (the owner's choice, 2026-11-14): write
`warehouse.cursor()` or `pd.read_csv(orders_file)` with no sag.connection(...) line. Before each cell
runs, the names it uses are read; a connection's name (its code_name: Sales DB -> sales_db) that is not
a variable yet is opened then (sag.connection), so a connection a notebook never uses never connects.
It is the connection's own object (a DB-API connection, a file's path), not a stand-in. A variable of
the same name that the user made always wins. No password or connection code is in the document.

Installed by the Workbench's kernels when they start; `install()` does it in any IPython. Standard
library only.
"""

from __future__ import annotations

import ast
import sys
import time
from typing import Any

from swissarmygrid.client import blades_client

_REFRESH_SECONDS: float = 10.0
_names: list[Any] = [0.0, {}]  # when read, {code name: connection name}
_installed: list[Any] = []


def code_names() -> dict[str, str]:
    """{code name: connection name}, read again at most every ten seconds (a connection added in the
    app is there for the next cell)."""
    when, names = _names
    if time.monotonic() - when < _REFRESH_SECONDS:
        return dict(names)
    try:
        listed = blades_client._call("GET", "/connections")["connections"]
        names = {str(c["code_name"]): str(c["name"]) for c in listed if c.get("code_name")}
    except Exception:  # noqa: S110 (the server away: keep the names known, try again next time)
        pass
    _names[:] = [time.monotonic(), names]
    return dict(names)


def used_names(tree: ast.AST) -> set[str]:
    """The names a cell reads and never makes itself: a cell that assigns, imports, defines or loops over a
    name means its own variable, so that name opens no connection."""
    loaded: set[str] = set()
    made: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            (loaded if isinstance(n.ctx, ast.Load) else made).add(n.id)
        elif isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            made.add(n.name)
        elif isinstance(n, ast.Import | ast.ImportFrom):
            made.update((alias.asname or alias.name).split(".")[0] for alias in n.names)
        elif isinstance(n, ast.arg):
            made.add(n.arg)
    return loaded - made


def open_used(tree: ast.AST, namespace: dict[str, Any]) -> list[str]:
    """Open each connection the cell names and the namespace lacks; the names opened."""
    wanted = code_names()
    opened = []
    for name in sorted(used_names(tree) & wanted.keys()):
        if name in namespace:
            continue
        try:
            namespace[name] = blades_client.blade(wanted[name])
            opened.append(name)
        except Exception as error:  # the cell then says NameError; this says why
            print(f"{name}: the connection {wanted[name]} did not open: {error}", file=sys.stderr)
    return opened


class _OpenUsed(ast.NodeTransformer):
    """IPython runs this on each cell before it runs (it leaves the cell as it is)."""

    def __init__(self, shell: Any) -> None:
        self.shell = shell

    def visit_Module(self, node: ast.Module) -> ast.Module:
        try:
            open_used(node, self.shell.user_ns)
        except Exception:  # noqa: S110 (IPython drops a transformer that raises: never raise)
            pass
        return node


def install(shell: Any = None) -> bool:
    """Make every connection available by its name in this IPython. False outside IPython."""
    if shell is None:
        module = sys.modules.get("IPython")
        get = getattr(module, "get_ipython", None) if module is not None else None
        shell = get() if callable(get) else None
    if shell is None or not hasattr(shell, "ast_transformers"):
        return False
    if any(s is shell for s in _installed):
        return True
    shell.ast_transformers.append(_OpenUsed(shell))
    _installed.append(shell)
    return True
