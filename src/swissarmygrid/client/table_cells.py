"""A table's read-only cell, run in the kernel (docs/ideas.md, 2026-11-14; the Workbench runs it as
`%%sag_table orders`).

The cell's result is the table's own name: `orders = duckdb.sql("... FROM orders ...").df()`. Run a
second time as it stands, it would transform a table that is already transformed. So this module keeps
a reference (never a copy) to the original the user's own code made, and the cell always starts from it:

  - `note_original` is told what was sent to the grid under a name (the watcher, client/watcher.py);
  - `run` puts the original back under the name, runs the cell, and remembers what the cell made;
  - `is_result` lets the watcher see that a changed variable is the cell's own result, which the grid
    already shows (its data with its steps): it is not sent back, or the steps would apply twice.

A script run as a file (`%sag_run`, the Workbench's Run file) runs its tables' cells the same way: each
`# %% [n] [transformations] name` cell goes through `run`, so the file's own run never sends a
transformed table to the grid as if it were new data. Outside the app the file is plain Python: the
maker makes the table, the cell transforms it once.

Both the original and the transformed table are in memory while working. Standard library only.
"""

from __future__ import annotations

import io
import re
import sys
from importlib import import_module
from typing import Any

_MISSING: Any = object()
# What the user's own code made under each name, as last sent to the grid.
_originals: dict[str, Any] = {}
# What a table's cell made from it.
_results: dict[str, Any] = {}
# The tables whose cell is running now: the watcher leaves them alone.
_busy: set[str] = set()
# The tables whose cell ran on an original the grid has not been sent yet.
_pending: set[str] = set()


def _namespace() -> dict[str, Any]:
    ipython = sys.modules.get("IPython")
    shell = ipython.get_ipython() if ipython is not None else None
    if shell is not None:
        return shell.user_ns  # type: ignore[no-any-return]
    main = sys.modules.get("__main__")
    return vars(main) if main is not None else {}


def note_original(name: str, value: Any) -> None:
    """`value` is what the user's code made under `name` (it was just sent to the grid)."""
    if _originals.get(name, _MISSING) is not value:
        _originals[name] = value
        _results.pop(name, None)
    _pending.discard(name)


def pending(name: str) -> bool:
    """The table's cell ran on data the grid has not seen (its maker ran again just before): the
    watcher sends that original, not the result."""
    return name in _pending


def is_result(name: str, value: Any) -> bool:
    """True when `value` is what the table's own cell made (not something the user's code assigned)."""
    return _results.get(name, _MISSING) is value


def is_busy(name: str) -> bool:
    return name in _busy


def original_for(name: str, value: Any) -> Any:
    """What to send to the grid for `name`: the original when `value` is the cell's result of it."""
    if is_result(name, value) and name in _originals:
        return _originals[name]
    return value


def forget(name: str | None = None) -> None:
    """Let go of a table's original and result (every table's with no name)."""
    for kept in (_originals, _results):
        if name is None:
            kept.clear()
        else:
            kept.pop(name, None)
    if name is None:
        _pending.clear()
    else:
        _pending.discard(name)


def _stop(message: str) -> Exception:
    """IPython's one-line error (no traceback), or a plain one outside it."""
    try:
        usage: type[Exception] = import_module("IPython.core.error").UsageError
    except ImportError:
        usage = RuntimeError
    return usage(message)


def run(name: str, code: str, namespace: dict[str, Any] | None = None) -> None:
    """Run a table's cell so that running it again gives the same table: it starts from the original
    the user's code made, whatever an earlier run left under the name."""
    ns = _namespace() if namespace is None else namespace
    _busy.add(name)
    try:
        current = ns.get(name, _MISSING)
        if name in _originals and (
            current is _MISSING or current is _originals[name] or is_result(name, current)
        ):
            ns[name] = _originals[name]
        elif current is not _MISSING and not is_result(name, current):
            # The user's code gave the name a new value: the new original (the grid is sent it next).
            _originals[name] = current
            _pending.add(name)
        had = name in ns
        try:
            exec(compile(code, f"<{name}: transformations>", "exec"), ns)  # noqa: S102 (the table's own cell)
        except Exception as error:
            missing = isinstance(error, NameError) or "does not exist" in str(error)
            if not had and missing:
                raise _stop(
                    f"{name} is not in this Python yet: run the cell that makes it, then this one."
                ) from None
            raise
    finally:
        if name in ns:
            _results[name] = ns[name]
        _busy.discard(name)


def open_grid(name: str, namespace: dict[str, Any] | None = None) -> str:
    """Open the table variable `name` as a grid, and keep it current from then on; the grid's address.
    When the table's cell already ran, the original is sent: the grid applies the steps itself."""
    from swissarmygrid.client import push_client, watcher

    ns = _namespace() if namespace is None else namespace
    value = ns[name]
    sent = original_for(name, value)
    watching = watcher.running()
    url = push_client.push(sent, name=name, _process=watching.id if watching else None)
    note_original(name, sent)
    if watching is not None:
        watching.mark_sent(name)
    return url


def _magic(line: str, cell: str) -> None:
    name = line.strip()
    if not name.isidentifier():
        raise _stop("%%sag_table takes the table's name: %%sag_table orders")
    run(name, cell)


_TABLE_CELL = re.compile(r"^# ?%%(?: \[\d+\])? \[transformations\] ([A-Za-z_]\w*)\b")
_CELL_END = re.compile(r"^# ?(?:%%|endregion\b)")


def safe_source(text: str) -> str:
    """A script's text with each table's cell (`# %% [n] [transformations] name`) run through `run`,
    on the same lines (so an error still names the right line). Everything else is as written."""
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        found = _TABLE_CELL.match(lines[index])
        out.append(lines[index])
        index += 1
        if found is None:
            continue
        body: list[str] = []
        while index < len(lines) and not _CELL_END.match(lines[index]):
            body.append(lines[index])
            index += 1
        if any(line.strip() for line in body):
            helper = "__import__('swissarmygrid.client.table_cells', fromlist=['run'])"
            out.append(f"{helper}.run({found.group(1)!r}, {chr(10).join(body)!r}, globals())")
            out += [""] * (len(body) - 1)
        else:
            out += body
    return "\n".join(out)


def _run_file(line: str) -> None:
    """`%sag_run script.py args`: IPython's own %run, with the script's table cells run through `run`.
    IPython reads the file itself; for the length of this run it is handed the safe text instead."""
    shell = import_module("IPython").get_ipython()
    core = import_module("IPython.core.interactiveshell")

    def opened(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if mode == "rb" and str(file).endswith(".py"):
            with open(file, "rb") as source:
                data = source.read()
            try:
                return io.BytesIO(safe_source(data.decode("utf-8")).encode("utf-8"))
            except UnicodeDecodeError:  # another encoding: run it as it is
                return io.BytesIO(data)
        return open(file, mode, *args, **kwargs)

    core.open = opened  # type: ignore[attr-defined]
    try:
        shell.run_line_magic("run", line)
    finally:
        del core.open


def install(shell: Any) -> None:
    """`%%sag_table <name>`: how the Workbench runs a table's cell. `%sag_run`: how it runs a script."""
    if shell is not None:
        shell.register_magic_function(_magic, magic_kind="cell", magic_name="sag_table")
        shell.register_magic_function(_run_file, magic_kind="line", magic_name="sag_run")
