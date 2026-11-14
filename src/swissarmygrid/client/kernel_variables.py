"""The Workbench's Variables overlay, read inside the kernel: every variable the user's code made, with
its type, size and a short preview; the connections and blades by their names in code (open, or not yet);
and the environment variables that vaults, secret blades and connection passwords use, by name only.

`snapshot()` is called by the Workbench as a user expression (nothing is printed, nothing runs in the
cell's history); it answers a JSON text. A value is never read whole: a table gives its shape, a list its
length, and a preview stops at a few hundred characters. A secret's value never leaves the kernel.
Standard library only.
"""

from __future__ import annotations

import json
import os
import reprlib
import sys
import types
from typing import Any

_PREVIEW = reprlib.Repr()
_PREVIEW.maxstring = 120
_PREVIEW.maxother = 120
_PREVIEW.maxlist = _PREVIEW.maxtuple = _PREVIEW.maxset = _PREVIEW.maxdict = 8
_MAX_VARIABLES: int = 500
_MAX_PREVIEW: int = 200
# IPython's own names, and the names the Workbench's setup makes.
_HIDDEN = frozenset({"In", "Out", "exit", "quit", "get_ipython", "open", "sag"})


def _type_name(value: Any) -> str:
    kind = type(value)
    module = kind.__module__.split(".")[0]
    return kind.__qualname__ if module == "builtins" else f"{module}.{kind.__qualname__}"


def _count(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def _size(value: Any) -> str:
    """Rows by columns for a table, the shape of an array, the length of anything that has one."""
    shape = getattr(value, "shape", None)
    if isinstance(shape, tuple) and all(isinstance(n, int) for n in shape):
        if len(shape) == 2 and hasattr(value, "columns"):
            return f"{_count(shape[0], 'row')} by {_count(shape[1], 'column')}"
        return " by ".join(f"{n:,}" for n in shape) or "scalar"
    num_rows = getattr(value, "num_rows", None)  # a pyarrow Table
    if isinstance(num_rows, int) and hasattr(value, "num_columns"):
        return f"{_count(num_rows, 'row')} by {_count(value.num_columns, 'column')}"
    if isinstance(value, str | bytes | list | tuple | dict | set | frozenset):
        return f"{len(value):,}"
    return ""


def _preview(value: Any) -> str:
    """A short look at the value, never the whole of a big one."""
    columns = getattr(value, "columns", None)
    if columns is not None and getattr(value, "shape", None) is not None:
        names = [str(c) for c in list(columns)[:8]]
        more = len(columns) - len(names)
        return "columns: " + ", ".join(names) + (f" and {more} more" if more > 0 else "")
    try:
        text = _PREVIEW.repr(value)
    except Exception as error:  # a repr that fails says so instead of breaking the overlay
        text = f"<no preview: {type(error).__name__}>"
    return text if len(text) <= _MAX_PREVIEW else text[: _MAX_PREVIEW - 1] + "…"


def _is_table(value: Any) -> bool:
    """Something the grid can open (sag.push takes it): a DataFrame or an Arrow table."""
    module = type(value).__module__.split(".")[0]
    return module in ("pandas", "polars", "pyarrow") and hasattr(value, "columns")


def snapshot(env_names: list[str] | None = None, namespace: dict[str, Any] | None = None) -> str:
    """The overlay's rows, as JSON: connections and blades, the user's variables, the environment."""
    if namespace is None:
        ipython = sys.modules.get("IPython")
        shell = ipython.get_ipython() if ipython is not None else None
        namespace = shell.user_ns if shell is not None else {}
    try:
        from swissarmygrid.client.kernel_connections import code_names

        connections = code_names()
    except Exception:
        connections = {}
    rows: list[dict[str, Any]] = []
    for name, value in list(namespace.items()):
        if name.startswith("_") or name in _HIDDEN or name in connections:
            continue
        if isinstance(value, types.ModuleType | types.FunctionType | types.BuiltinFunctionType | type):
            continue
        rows.append(
            {
                "name": name,
                "type": _type_name(value),
                "size": _size(value),
                "value": _preview(value),
                "table": _is_table(value),
            }
        )
        if len(rows) >= _MAX_VARIABLES:
            break
    rows.sort(key=lambda r: r["name"].casefold())
    opened = [
        {
            "name": name,
            "connection": connection,
            "open": name in namespace,
            "type": _type_name(namespace[name]) if name in namespace else "",
            "value": _preview(namespace[name]) if name in namespace else "opened when a cell first uses it",
            "table": name in namespace and _is_table(namespace[name]),
        }
        for name, connection in sorted(connections.items())
    ]
    environment = [{"name": env, "set": bool(os.environ.get(env))} for env in sorted(set(env_names or []))]
    return json.dumps({"connections": opened, "variables": rows, "environment": environment}, default=str)
