"""The values a blade's code is written with that each person fills in for themselves: its settings.

A blade from someone else says `host="their-server"` or `"Server=their-server;Database=sales;"`.
These are read from the code with `ast` (never run), so the import review and the save screen can show
one field per setting, flag the ones that are still placeholders (`my-server`, `<your host>`,
`example`), suggest values (the user's other blades, a few standard environment variables), and write a
new value back into exactly that place in the code.

What counts as a setting:

- a keyword argument whose name is a connection setting (`host=`, `port=`, `dbname=`, `user=`,
  `account=`, `warehouse=`, `http_path=`, `vault_url=`, ...) given a literal: `host="my-server"`;
- a top-level name given a literal or a path: `project = "my-project"`,
  `path = Path(r"C:\\data\\orders.csv")`;
- a path given as the first argument of a call: `duckdb.connect(r"C:\\data\\shop.duckdb")`;
- a `Key=value;` part of a connection string: `"Server=my-server;Database=my_database;"`.

Passwords, tokens and keys are never settings: they belong in `secret("...")`. Standard library only.
"""

from __future__ import annotations

import ast
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from swissarmygrid.blade_core.code import BladeCodeError, parse

# Keyword arguments that hold a connection setting, and the plain name each is shown and matched as.
KEYWORDS: dict[str, str] = {
    "host": "server",
    "hostname": "server",
    "server": "server",
    "server_hostname": "server",
    "address": "server",
    "port": "port",
    "database": "database",
    "dbname": "database",
    "db": "database",
    "user": "user",
    "username": "user",
    "account": "account",
    "warehouse": "warehouse",
    "schema": "schema",
    "role": "role",
    "catalog": "catalog",
    "http_path": "http path",
    "dsn": "data source",
    "project": "project",
    "region_name": "region",
    "region": "region",
    "vault_url": "vault address",
    "url": "address",
    "base_url": "address",
    "service_name": "service",
    "sid": "service",
}
# The parts of a connection string (ODBC, DB2, OLE DB) that are settings, by their key ignoring case.
CONNECTION_KEYS: dict[str, str] = {
    "server": "server",
    "hostname": "server",
    "host": "server",
    "data source": "server",
    "address": "server",
    "port": "port",
    "database": "database",
    "initial catalog": "database",
    "uid": "user",
    "user id": "user",
    "dsn": "data source",
    "dbq": "path",
    "driver": "driver",
}
# Never a setting, wherever it appears: these belong in secret("...").
_SECRET_WORDS = re.compile(r"pass|pwd|token|secret|credential|api_?key|private", re.IGNORECASE)
# The first argument of these calls is a file or folder.
_PATH_CALLS = frozenset(
    {
        "Path",
        "connect",
        "open",
        "read_csv",
        "read_excel",
        "read_parquet",
        "read_json",
        "read_table",
        "read_feather",
        "dotenv_values",
        "load_workbook",
        "ExcelFile",
    }
)
_PLACEHOLDER = re.compile(
    r"(^|[^a-z])(my|your)([-_ .]|$)|example|<[^>]*>|\{\{|change[-_ ]?me|xxx|abc123|1234567890|placeholder",
    re.IGNORECASE,
)
_LOOKS_LIKE_PATH = re.compile(r"^([a-z]:[\\/]|[\\/]|~|\.{1,2}[\\/])|[\\/].*\.[a-z0-9]{1,8}$", re.IGNORECASE)
_PAIR = re.compile(r"(?:^|;)\s*([A-Za-z][A-Za-z ]*?)\s*=\s*([^;]*)")
# Environment variables that commonly hold a setting, per plain name. Never a password.
ENVIRONMENT: dict[str, tuple[str, ...]] = {
    "server": ("PGHOST", "MYSQL_HOST", "DB_HOST", "DATABASE_HOST", "SQLSERVER_HOST"),
    "port": ("PGPORT", "MYSQL_PORT", "DB_PORT"),
    "database": ("PGDATABASE", "MYSQL_DATABASE", "DB_NAME", "DATABASE_NAME"),
    "user": ("PGUSER", "MYSQL_USER", "DB_USER", "SNOWFLAKE_USER"),
    "account": ("SNOWFLAKE_ACCOUNT",),
    "warehouse": ("SNOWFLAKE_WAREHOUSE",),
    "role": ("SNOWFLAKE_ROLE",),
    "project": ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"),
    "region": ("AWS_REGION", "AWS_DEFAULT_REGION"),
    "vault address": ("AZURE_KEY_VAULT_URL", "VAULT_ADDR"),
    "address": ("VAULT_ADDR",),
}


class FillError(ValueError):
    """A value that cannot be written into the code as it stands. The message says why."""


@dataclass
class Setting:
    """One value in the code a person may need to change."""

    id: str  # where it is in this code: "<start>:<end>"; changes when the code does
    name: str  # what it is: "server", "database", "path", or the code's own name
    key: str  # as the code writes it: host, Server, path, project...
    value: str | int | float | bool
    line: int
    where: str  # keyword, name, argument or connection string
    placeholder: bool  # still a stand-in such as my-server: it needs the person's own value
    suggestions: list[dict[str, str]] = field(default_factory=list)  # {"value", "from"}

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "key": self.key,
            "value": self.value,
            "type": "number"
            if isinstance(self.value, int | float) and not isinstance(self.value, bool)
            else "text",
            "line": self.line,
            "where": self.where,
            "placeholder": self.placeholder,
            "suggestions": self.suggestions,
        }


@dataclass(frozen=True)
class _Found:
    setting: Setting
    start: int  # character offsets of what a new value replaces, in the code
    end: int
    raw: bool  # a raw string literal (r"..."): paths keep their backslashes as written
    inside_string: bool  # a Key=value part: the value is text inside a larger string literal


def is_placeholder(value: object) -> bool:
    return isinstance(value, str) and bool(_PLACEHOLDER.search(value))


def _lines(code: str) -> list[str]:
    """The code's lines as ast counts them: split on newlines only (splitlines also splits on form
    feeds and other characters, which would shift every position after one)."""
    parts = code.split("\n")
    return [part + "\n" for part in parts[:-1]] + [parts[-1]]


def _offsets(code: str) -> list[int]:
    """The character offset where each line starts (ast gives byte columns; the code may hold é)."""
    starts, total = [0], 0
    for line in _lines(code):
        total += len(line)
        starts.append(total)
    return starts


def _char(code_lines: list[str], starts: list[int], line: int, byte_col: int) -> int:
    text = code_lines[line - 1] if line - 1 < len(code_lines) else ""
    return starts[line - 1] + len(text.encode("utf-8")[:byte_col].decode("utf-8", "replace"))


def _literal(node: ast.expr | None) -> ast.Constant | None:
    """A str, int or float literal (not a bool or None), or Path(<str literal>)'s argument."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str | int | float):
        return None if isinstance(node.value, bool) else node
    if (
        isinstance(node, ast.Call)
        and _call_name(node) == "Path"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0]
    return None


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def detect(code: str) -> list[Setting]:
    """Every setting in the code, in the order it is written. Raises BladeCodeError for code that is
    not Python."""
    return [f.setting for f in _detect(code)]


def _detect(code: str) -> list[_Found]:
    tree = parse(code)
    lines = _lines(code)
    starts = _offsets(code)
    found: dict[tuple[int, int], _Found] = {}

    def add(node: ast.Constant, name: str, key: str, where: str) -> None:
        if _SECRET_WORDS.search(key) or node.end_lineno is None or node.end_col_offset is None:
            return
        start = _char(lines, starts, node.lineno, node.col_offset)
        end = _char(lines, starts, node.end_lineno, node.end_col_offset)
        source = code[start:end]
        if isinstance(node.value, str):
            if "\n" in node.value or len(node.value) > 500 or not _plain_string(source):
                return  # an f-string piece, a multi-line text or a joined string: not a single value
            if where != "connection string" and _pairs(node.value):
                _add_pairs(node, start, end)
                return
        value = node.value
        if (start, end) in found or not isinstance(value, str | int | float):
            return
        found[(start, end)] = _Found(
            Setting(f"{start}:{end}", name, key, value, node.lineno, where, is_placeholder(value)),
            start,
            end,
            source.lstrip("bBuU")[:1] in ("r", "R"),
            False,
        )

    def _add_pairs(node: ast.Constant, start: int, end: int) -> None:
        source = code[start:end]
        for match in _PAIR.finditer(str(node.value)):
            key, value = match.group(1).strip(), match.group(2).strip()
            name = CONNECTION_KEYS.get(key.casefold())
            if name is None or not value or _SECRET_WORDS.search(key):
                continue
            # The value as written in the source, after "Key=": as parsed in a raw string, with each
            # backslash doubled in a plain one (Server=db01\\SQLEXPRESS). Anything else is left alone.
            spot, written, raw = None, value, False
            for candidate in dict.fromkeys((value, value.replace("\\", "\\\\"))):
                spot = re.search(
                    rf"(?<![A-Za-z]){re.escape(key)}\s*=\s*{re.escape(candidate)}(?=\s*[;\"'])", source
                )
                if spot is None:
                    continue
                quote = max(source.rfind('"', 0, spot.start()), source.rfind("'", 0, spot.start()))
                raw = quote > 0 and source[quote - 1] in "rR"
                if "\\" not in value or raw == (candidate == value):
                    written = candidate
                    break
                spot = None
            if spot is None:
                continue
            value_start = start + spot.end() - len(written)
            pair_key = (value_start, value_start + len(written))
            if pair_key in found:
                continue
            found[pair_key] = _Found(
                Setting(
                    f"{pair_key[0]}:{pair_key[1]}",
                    name,
                    key,
                    value,
                    node.lineno + source[: spot.start()].count("\n"),
                    "connection string",
                    is_placeholder(value),
                ),
                pair_key[0],
                pair_key[1],
                raw,
                True,
            )

    for statement in tree.body:  # first: a top-level name labels its own value (path = Path(...))
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target, value = statement.target, statement.value
        else:
            continue
        literal = _literal(value)
        if isinstance(target, ast.Name) and literal is not None:
            name = (
                "path"
                if isinstance(literal.value, str) and _LOOKS_LIKE_PATH.search(literal.value)
                else target.id
            )
            add(literal, name, target.id, "name")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                literal = _literal(keyword.value)
                if keyword.arg and literal is not None and keyword.arg.casefold() in KEYWORDS:
                    add(literal, KEYWORDS[keyword.arg.casefold()], keyword.arg, "keyword")
            if node.args and _call_name(node) in _PATH_CALLS:
                literal = _literal(node.args[0])
                if literal is not None and isinstance(literal.value, str):
                    if _LOOKS_LIKE_PATH.search(literal.value):
                        add(literal, "path", _call_name(node), "argument")
                    elif _pairs(literal.value, least=1) and literal.end_lineno and literal.end_col_offset:
                        _add_pairs(
                            literal,
                            _char(lines, starts, literal.lineno, literal.col_offset),
                            _char(lines, starts, literal.end_lineno, literal.end_col_offset),
                        )
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _pairs(node.value):
            if node.end_lineno is not None and node.end_col_offset is not None:
                _add_pairs(
                    node,
                    _char(lines, starts, node.lineno, node.col_offset),
                    _char(lines, starts, node.end_lineno, node.end_col_offset),
                )
    return sorted(found.values(), key=lambda f: f.start)


def _pairs(text: str, least: int = 2) -> bool:
    """A connection string: `least` or more Key=value parts, at least one a known setting. One part is
    enough for the string a connect call is given ("DSN=sales")."""
    pairs = [m.group(1).strip().casefold() for m in _PAIR.finditer(text)]
    return len(pairs) >= least and any(p in CONNECTION_KEYS for p in pairs)


def _plain_string(source: str) -> bool:
    """One string literal (not implicit concatenation, not an f-string or bytes), or a number."""
    if not source or source[0].isdigit() or source[0] in "-.":
        return True
    prefix = re.match(r"[A-Za-z]*", source)
    letters = (prefix.group(0) if prefix else "").casefold()
    if set(letters) - {"r", "u"}:
        return False
    body = source[len(letters) :]
    quote = body[:3] if body[:3] in ('"""', "'''") else body[:1]
    return body.endswith(quote) and quote not in body[len(quote) : -len(quote)]


# ===== writing a value back =====


def _string_literal(value: str, raw: bool) -> str:
    """Python text for the value: a raw string for a path when it can be one, else a quoted string."""
    if raw and '"' not in value and "\n" not in value and not value.endswith("\\"):
        return f'r"{value}"'
    return json.dumps(value, ensure_ascii=False)


def apply(code: str, values: dict[str, Any]) -> str:
    """The code with each setting (by its id from `detect` on this same code) set to a new value.
    Raises FillError when a value cannot go where its setting is."""
    by_id = {f.setting.id: f for f in _detect(code)}
    edits: list[tuple[int, int, str]] = []
    for setting_id, new in values.items():
        spot = by_id.get(setting_id)
        if spot is None:
            raise FillError("The code changed since its settings were read: read them again.")
        old = spot.setting.value
        if spot.inside_string:
            text = str(new).strip()
            if not text or re.search(r"[;\"'\n]", text):
                raise FillError(f"{spot.setting.key} cannot hold ; quotes or a line break here.")
            edits.append((spot.start, spot.end, text if spot.raw else text.replace("\\", "\\\\")))
            continue
        if isinstance(old, int | float) and not isinstance(old, bool):
            try:
                number = float(new) if isinstance(old, float) else int(str(new).strip())
            except ValueError:
                raise FillError(f"{spot.setting.key} is a number.") from None
            edits.append((spot.start, spot.end, repr(number)))
            continue
        text = str(new)
        if "\n" in text:
            raise FillError(f"{spot.setting.key} is one line of text.")
        edits.append((spot.start, spot.end, _string_literal(text, spot.raw)))
    for start, end, text in sorted(edits, reverse=True):
        code = code[:start] + text + code[end:]
    try:
        parse(code)
    except BladeCodeError as error:
        raise FillError(f"That value would break the code: {error}") from None
    return code


# ===== suggestions =====


def suggest(
    settings: list[Setting],
    others: Iterable[tuple[str, str, str]],
    environ: dict[str, str] | None = None,
) -> list[Setting]:
    """Fill in each setting's suggestions: values the user's other blades use for the same setting
    (`others`: (blade name, dialect, code); the same dialect first, then by how often), then standard
    environment variables. Placeholders are never suggested, and neither is the value already there."""
    env = os.environ if environ is None else environ
    seen: dict[str, dict[str, list[tuple[str, str]]]] = {}  # name -> value -> [(blade, dialect)]
    for blade, dialect, code in others:
        try:
            found = detect(code)
        except BladeCodeError:
            continue
        for other in found:
            if other.placeholder or (other.name == other.key and other.where == "name"):
                continue  # a placeholder, or a top-level name only that blade's code knows
            seen.setdefault(other.name, {}).setdefault(str(other.value), []).append((blade, dialect))
    for setting in settings:
        offered: list[dict[str, str]] = []
        current = str(setting.value)
        values = seen.get(setting.name, {})
        for value, users in sorted(values.items(), key=lambda kv: -len(kv[1])):
            if value != current:
                names = sorted({u[0] for u in users})
                offered.append({"value": value, "from": "your blade " + ", ".join(names[:3])})
        for variable in ENVIRONMENT.get(setting.name, ()):
            value = env.get(variable, "").strip()
            if value and value != current and all(o["value"] != value for o in offered) and len(value) < 300:
                offered.append({"value": value, "from": f"environment variable {variable}"})
        setting.suggestions = offered[:8]
    return settings
