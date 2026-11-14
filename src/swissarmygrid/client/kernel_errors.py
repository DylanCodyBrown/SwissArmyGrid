"""The Workbench's error panel, read inside the kernel: the last error, taken apart for the page.

`detail()` is called by the Workbench right after a run fails, as a user expression on a silent run (the
next run could change what the error saw). It answers a JSON text: each exception of the chain (the cause
first), its frames (your cells and files told apart from libraries), each frame's line and the exact
columns of the failing expression, the values of that expression's parts at that moment (stack_data, the
library IPython itself uses, when this Python has it), a function's locals, and plain facts for the hints
(the missing name and the names close to it, a table's columns for a KeyError, a file's full path).

The words (what it means, what to do) are written by the server (domain/errors.py): this only gathers.
Values are never read whole: each is a short preview. Standard library only (stack_data is optional:
IPython 8 and later bring it).
"""

from __future__ import annotations

import builtins
import difflib
import json
import os
import reprlib
import sys
import sysconfig
import time
import traceback
import types
from typing import Any

_PREVIEW = reprlib.Repr()
_PREVIEW.maxstring = 160
_PREVIEW.maxother = 160
_PREVIEW.maxlist = _PREVIEW.maxtuple = _PREVIEW.maxset = _PREVIEW.maxdict = 10
_MAX_FRAMES = 60  # a RecursionError's frames: the first and last of them
_MAX_LOCALS = 30
_MAX_CHAIN = 5
_MAX_KEYS = 60
_BUDGET_SECONDS = 1.5  # previews after this are left out (a slow repr must not hold the kernel)

_HIDDEN_PARTS = (
    f"{os.sep}IPython{os.sep}",
    f"{os.sep}ipykernel{os.sep}",
    f"{os.sep}swissarmygrid{os.sep}server{os.sep}kernel_setup",
    f"{os.sep}swissarmygrid{os.sep}client{os.sep}kernel_",
)


def _ipython() -> Any:
    ipython = sys.modules.get("IPython")
    return ipython.get_ipython() if ipython is not None else None


def _preview(value: Any) -> str:
    columns = getattr(value, "columns", None)
    shape = getattr(value, "shape", None)
    try:
        if columns is not None and isinstance(shape, tuple) and len(shape) == 2:
            names = [str(c) for c in list(columns)[:8]]
            more = len(columns) - len(names)
            return (
                f"{type(value).__name__} {shape[0]:,} rows by {shape[1]:,} columns: "
                + ", ".join(names)
                + (f" and {more} more" if more > 0 else "")
            )
        text = _PREVIEW.repr(value)
    except Exception as error:  # a repr that fails says so
        return f"<no preview: {type(error).__name__}>"
    return text if len(text) <= 200 else text[:199] + "…"


def _type_name(value: Any) -> str:
    kind = type(value)
    module = kind.__module__.split(".")[0]
    return kind.__qualname__ if module == "builtins" else f"{module}.{kind.__qualname__}"


def _is_table(value: Any) -> bool:
    module = type(value).__module__.split(".")[0]
    try:
        return module in ("pandas", "polars", "pyarrow") and hasattr(value, "columns")
    except Exception:
        return False


def _utf16_column(line: str, byte_offset: int | None) -> int | None:
    """Python's column (a 0-based UTF-8 byte offset) as the editor counts it (1-based, UTF-16)."""
    if byte_offset is None or byte_offset < 0:
        return None
    chars = line.encode("utf-8")[:byte_offset].decode("utf-8", "replace")
    return sum(2 if ord(c) > 0xFFFF else 1 for c in chars) + 1


def _cells() -> dict[str, int]:
    """IPython's file name for each cell, to the cell's run number."""
    shell = _ipython()
    found = getattr(getattr(shell, "compile", None), "_filename_map", None)
    return dict(found) if isinstance(found, dict) else {}


_PATHS = sysconfig.get_paths()
_LIBRARIES = tuple(
    os.path.normcase(os.path.abspath(p)) for p in {_PATHS.get("purelib", ""), _PATHS.get("platlib", "")} if p
)
_STDLIB = tuple(
    os.path.normcase(os.path.abspath(p))
    for p in {_PATHS.get("stdlib", ""), _PATHS.get("platstdlib", "")}
    if p
)


def _where(filename: str, cells: dict[str, int]) -> str:
    if filename in cells:
        return "cell"
    if filename.startswith("<"):
        return "cell" if filename.startswith(("<ipython-input", "<string>")) else "library"
    # Compiled library code names its source relative to the package ("pandas/_libs/index.pyx"); your
    # own files (a %run script) always come with a full path.
    if not os.path.isabs(filename) or filename.endswith((".pyx", ".pxd")):
        return "library"
    full = os.path.normcase(os.path.abspath(filename))
    if any(full.startswith(p) for p in _LIBRARIES) or f"{os.sep}site-packages{os.sep}" in full:
        return "library"
    if any(full.startswith(p) for p in _STDLIB):
        return "stdlib"
    return "file"


def _hidden(filename: str, frame: types.FrameType | None) -> bool:
    if any(part in filename for part in _HIDDEN_PARTS):
        return True
    try:
        return bool(frame is not None and frame.f_locals.get("__tracebackhide__"))
    except Exception:
        return False


def _stack_values(tb: types.TracebackType) -> dict[int, list[dict[str, str]]]:
    """The failing expression's parts and their values, by frame (stack_data, when this Python has it):
    `r["count"]` = 0, as Visual Studio shows "s was null"."""
    try:
        import importlib

        stack_data: Any = importlib.import_module("stack_data")
    except Exception:
        return {}
    found: dict[int, list[dict[str, str]]] = {}
    started = time.monotonic()
    try:
        for info in stack_data.FrameInfo.stack_data(tb, collapse_repeated_frames=False):
            if not isinstance(info, stack_data.FrameInfo):
                continue
            if time.monotonic() - started > _BUDGET_SECONDS:
                break
            rows = []
            for variable in info.variables_in_executing_piece[:12]:
                rows.append(
                    {
                        "expr": str(variable.name),
                        "repr": _preview(variable.value),
                        "type": _type_name(variable.value),
                    }
                )
            found[id(info.frame)] = rows
    except Exception:  # stack_data cannot read this frame's source: the plain frames still go
        return found
    return found


def _executing_container(tb: types.TracebackType, user_frames: list[types.FrameType]) -> Any:
    """For a KeyError or IndexError: the object being indexed in your deepest frame (`df` in df["amont"])."""
    try:
        import ast

        import executing
    except Exception:
        return None
    for frame in reversed(user_frames):
        try:
            node: Any = executing.Source.executing(frame).node
            while node is not None and not isinstance(node, ast.Subscript):
                node = getattr(node, "parent", None)
            if node is None:
                continue
            import pure_eval

            evaluator = pure_eval.Evaluator.from_frame(frame)
            target = node.value
            # df.loc[...], df.iloc[...]: the table before the indexer (pure_eval reads no properties).
            if isinstance(target, ast.Attribute) and target.attr in ("loc", "iloc", "at", "iat"):
                target = target.value
            return evaluator[target]
        except Exception:  # noqa: S112 (a frame it cannot read: the next one)
            continue
    return None


def _close(name: str, options: list[str]) -> list[str]:
    return difflib.get_close_matches(name, [o for o in options if isinstance(o, str)], n=3, cutoff=0.7)


def _facts(
    error: BaseException, tb: types.TracebackType | None, user_frames: list[types.FrameType]
) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    shell = _ipython()
    namespace = shell.user_ns if shell is not None else {}
    if isinstance(error, NameError) and not isinstance(error, UnboundLocalError):
        name = getattr(error, "name", None)
        if isinstance(name, str):
            facts["name"] = name
            visible = [n for n in namespace if not n.startswith("_")] + dir(builtins)
            if user_frames:  # a script's own names (a %run file has a namespace of its own)
                frame = user_frames[-1]
                visible += [n for n in {**frame.f_globals, **frame.f_locals} if not n.startswith("_")]
            facts["candidates"] = _close(name, visible)
    elif isinstance(error, AttributeError):
        name = getattr(error, "name", None)
        obj = getattr(error, "obj", None)
        if isinstance(name, str):
            facts["name"] = name
            facts["object_type"] = _type_name(obj) if obj is not None else ""
            facts["is_none"] = obj is None and "NoneType" in str(error)
            try:
                facts["candidates"] = _close(name, [n for n in dir(obj) if not n.startswith("_")])
            except Exception:
                facts["candidates"] = []
            if _is_table(obj):
                facts["columns"] = [str(c) for c in list(getattr(obj, "columns", []))[:_MAX_KEYS]]
                facts["column_candidates"] = _close(name, facts["columns"])
    elif isinstance(error, ModuleNotFoundError):
        if isinstance(error.name, str):
            facts["module"] = error.name
            facts["python"] = sys.executable
    elif isinstance(error, KeyError | IndexError):
        key = error.args[0] if error.args else None
        facts["key"] = _preview(key)
        container = _executing_container(tb, user_frames) if tb is not None else None
        if container is not None:
            facts["container_type"] = _type_name(container)
            try:
                if _is_table(container):
                    keys = [str(c) for c in list(container.columns)]
                    facts["table"] = True
                elif isinstance(container, dict):
                    keys = [k if isinstance(k, str) else repr(k) for k in list(container.keys())]
                else:
                    keys = []
                    facts["length"] = len(container)
            except Exception:
                keys = []
            if keys:
                facts["keys"] = keys[:_MAX_KEYS]
                facts["more_keys"] = max(0, len(keys) - _MAX_KEYS)
                if isinstance(key, str):
                    facts["candidates"] = _close(key, keys)
    elif isinstance(error, FileNotFoundError | PermissionError | IsADirectoryError | NotADirectoryError):
        if error.filename is not None:
            facts["path"] = str(error.filename)
            facts["full_path"] = os.path.abspath(str(error.filename))
        facts["cwd"] = os.getcwd()
    elif isinstance(error, UnicodeDecodeError):
        facts["encoding"] = error.encoding
    elif isinstance(error, SyntaxError):
        facts["syntax_kind"] = type(error).__name__
    return facts


def _syntax(error: SyntaxError, cells: dict[str, int]) -> dict[str, Any]:
    text = error.text or ""
    line = text.rstrip("\n")
    start = (error.offset or 1) - 1  # a SyntaxError's offset counts characters, 1-based
    end = getattr(error, "end_offset", None)
    filename = error.filename or ""
    return {
        "where": _where(filename, cells) if filename else "cell",
        "cell": cells.get(filename),
        "file": filename if filename and not filename.startswith("<") and filename not in cells else None,
        "line": error.lineno,
        "end_line": getattr(error, "end_lineno", None) or error.lineno,
        "col": sum(2 if ord(c) > 0xFFFF else 1 for c in line[:start]) + 1,
        "end_col": sum(2 if ord(c) > 0xFFFF else 1 for c in line[: end - 1]) + 1
        if end and end > (error.offset or 0)
        else None,
        "text": line,
    }


def _frames(
    error: BaseException, cells: dict[str, int], started: float
) -> tuple[list[dict[str, Any]], list[types.FrameType]]:
    tb = error.__traceback__
    if tb is None:
        return [], []
    summaries = traceback.extract_tb(tb)
    raw: list[types.TracebackType] = []
    walk: types.TracebackType | None = tb
    while walk is not None:
        raw.append(walk)
        walk = walk.tb_next
    values = _stack_values(tb) if time.monotonic() - started < _BUDGET_SECONDS else {}
    frames: list[dict[str, Any]] = []
    user: list[types.FrameType] = []
    pairs = list(zip(summaries, raw, strict=False))
    if len(pairs) > _MAX_FRAMES:  # a RecursionError: its first and last frames
        half = _MAX_FRAMES // 2
        pairs = pairs[:half] + pairs[-half:]
    for summary, step in pairs:
        frame = step.tb_frame
        filename = summary.filename
        if _hidden(filename, frame):
            continue
        where = _where(filename, cells)
        code = summary.line or ""
        full_line = code
        try:
            import linecache

            full_line = linecache.getline(filename, summary.lineno or 0).rstrip("\n") or code
        except Exception:  # noqa: S110 (the line as the traceback has it)
            pass
        col = _utf16_column(full_line, getattr(summary, "colno", None))
        end_line = getattr(summary, "end_lineno", None) or summary.lineno
        end_text = full_line
        if end_line != summary.lineno:
            try:
                import linecache

                end_text = linecache.getline(filename, end_line or 0).rstrip("\n")
            except Exception:
                end_text = ""
        end_col = _utf16_column(end_text, getattr(summary, "end_colno", None))
        row: dict[str, Any] = {
            "where": where,
            "cell": cells.get(filename),
            "file": filename if where in ("file", "library", "stdlib") else None,
            "module": frame.f_globals.get("__name__", "") if where != "cell" else "",
            "function": summary.name,
            "line": summary.lineno,
            "end_line": end_line,
            "col": col,
            "end_col": end_col,
            "code": full_line,
            "values": values.get(id(frame), []) if where in ("cell", "file") else [],
            "locals": [],
        }
        if where in ("cell", "file"):
            user.append(frame)
            # A function's own variables (a cell's or a module's are the Variables pane's).
            if frame.f_code.co_name != "<module>" and time.monotonic() - started < _BUDGET_SECONDS:
                try:
                    for name, value in list(frame.f_locals.items())[:_MAX_LOCALS]:
                        if name.startswith("__") or isinstance(
                            value, types.ModuleType | types.FunctionType | type
                        ):
                            continue
                        row["locals"].append(
                            {
                                "name": name,
                                "repr": _preview(value),
                                "type": _type_name(value),
                                "table": _is_table(value),
                            }
                        )
                except Exception:  # noqa: S110 (a frame without readable locals)
                    pass
        frames.append(row)
    return frames, user


def _hint(error: BaseException) -> str:
    """Python's own "Did you mean ...?" (Jupyter drops it from the error's text)."""
    try:
        last = list(traceback.TracebackException.from_exception(error).format_exception_only())[-1]
    except Exception:
        return ""
    found = last.strip().rsplit(". ", 1)
    return found[-1] if len(found) > 1 and found[-1].startswith(("Did you mean", "Perhaps you")) else ""


def _entry(
    error: BaseException, relation: str | None, cells: dict[str, int], started: float
) -> dict[str, Any]:
    frames, user = _frames(error, cells, started)
    message = str(error)
    if isinstance(error, KeyError) and error.args:
        message = _preview(error.args[0])
    entry: dict[str, Any] = {
        "type": type(error).__name__,
        "module": type(error).__module__,
        "message": message,
        "hint": _hint(error),
        "relation": relation,
        "notes": [str(n) for n in getattr(error, "__notes__", [])][:5],
        "facts": _facts(error, error.__traceback__, user),
        "syntax": _syntax(error, cells) if isinstance(error, SyntaxError) else None,
        "frames": frames,
        "group": [f"{type(e).__name__}: {e}" for e in getattr(error, "exceptions", [])][:10],
    }
    yours = [i for i, f in enumerate(frames) if f["where"] in ("cell", "file")]
    entry["your_frame"] = yours[-1] if yours else None
    return entry


def detail() -> str:
    """The last error, as JSON (an empty object when there is none)."""
    error = getattr(sys, "last_exc", None) or getattr(sys, "last_value", None)
    if not isinstance(error, BaseException):
        return "{}"
    started = time.monotonic()
    cells = _cells()
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    current: BaseException | None = error
    relation: str | None = None
    while current is not None and id(current) not in seen and len(chain) < _MAX_CHAIN:
        seen.add(id(current))
        entry = _entry(current, relation, cells, started)
        # An earlier error that never went through your code (pandas' own lookup, re-raised) is noise.
        if not chain or entry["your_frame"] is not None or entry["syntax"]:
            chain.append(entry)
        if current.__cause__ is not None:
            relation, current = "cause", current.__cause__
        elif current.__context__ is not None and not current.__suppress_context__:
            relation, current = "context", current.__context__
        else:
            current = None
    chain.reverse()  # the first error first; the one that stopped the run last
    return json.dumps({"chain": chain, "python": sys.executable, "cwd": os.getcwd()}, default=str)
