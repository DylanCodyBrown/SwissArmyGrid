"""Portable blades: a blade as one plain Python file, `<name>.blade.py`, to share, keep in Git, or import.

The file is the blade's own code under a header comment block, written the way Python's inline script
metadata is (`# /// <type>` ... `# ///`, each line starting `# `), holding TOML:

    # /// swissarmygrid-blade
    # format = 1
    # name = "Warehouse"
    # kind = "sql"
    # description = "The sales warehouse, read only"
    # tags = ["sales", "orders-function-app"]
    # dialect = "sqlserver"
    # variable = "conn"
    # secrets = ["password"]
    #
    # [catalog]
    # language = "sql"
    # command = '''
    # SELECT ...
    # '''
    # ///
    import pyodbc
    conn = pyodbc.connect(...)

A vault blade adds `[vault]` (environment variable = secret name); a secret blade lists `env_names` and
has no code. Secret values are never written: `secrets` lists only the names the code asks for, which
the person importing types into their own vault. Several blades travel as a zip of these files.

Reading never runs anything: it parses the header (tomllib) and checks the code is Python (ast).
Standard library only, so the client can use it too.
"""

from __future__ import annotations

import ast
import io
import json
import re
import tomllib
import zipfile
from dataclasses import dataclass
from typing import Any

from swissarmygrid.blade_core.code import BladeCodeError, read

FORMAT: int = 1
HEADER_TYPE: str = "swissarmygrid-blade"
SUFFIX: str = ".blade.py"
KINDS: tuple[str, ...] = ("sql", "other", "vault", "secret")
_OPEN = re.compile(r"^# /// (?P<type>[a-zA-Z0-9-]+)$")
_CLOSE: str = "# ///"
_MAX_FILE_BYTES: int = 1_000_000
_MAX_ZIP_ENTRIES: int = 500
_MAX_ZIP_BYTES: int = 50_000_000


class PortableError(ValueError):
    """The file is not a blade, or not one this version reads. The message says why in plain words."""


@dataclass(frozen=True)
class Found:
    """One file offered for import: the blade it holds, or why it holds none."""

    file: str
    blade: dict[str, Any] | None
    error: str | None


# ===== writing =====


def _toml_string(value: str, multiline: bool = False) -> str:
    """A TOML string that reads back exactly. Multi-line text as a literal string ('''...''') when it can
    be (no ''' inside, no control characters but newlines and tabs, no line that would close the
    header); anything else as a basic string, escaped."""
    safe_literal = (
        "'''" not in value
        and not value.endswith("'")
        and all(c in "\n\t" or ord(c) >= 32 for c in value)
        and "\x7f" not in value
        and not any(line.strip() == "///" for line in value.split("\n"))
    )
    if multiline and safe_literal:
        return "'''\n" + value + "'''"
    return json.dumps(value, ensure_ascii=False)


def _header_lines(blade: dict[str, Any]) -> list[str]:
    kind = blade.get("kind", "other")
    lines = [
        f"format = {FORMAT}",
        f"name = {_toml_string(str(blade['name']))}",
        f"kind = {_toml_string(kind)}",
    ]
    if blade.get("description"):
        lines.append(f"description = {_toml_string(str(blade['description']))}")
    if blade.get("tags"):
        lines.append("tags = [" + ", ".join(_toml_string(str(t)) for t in blade["tags"]) + "]")
    if kind == "sql":
        lines.append(f"dialect = {_toml_string(str(blade.get('dialect') or 'other'))}")
    if kind != "secret":
        lines.append(f"variable = {_toml_string(str(blade.get('variable') or ''))}")
        try:
            secrets = read(str(blade.get("code", ""))).secrets
        except BladeCodeError:
            secrets = []
        if secrets:
            names = ", ".join(_toml_string(s) for s in secrets)
            lines.append(
                f"secrets = [{names}]  # names only: each person types the values into their own vault"
            )
    else:
        names = ", ".join(_toml_string(str(n)) for n in blade.get("env_names", []))
        lines.append(f"env_names = [{names}]  # typed each session, never kept")
    if blade.get("template"):
        lines.append(f"template = {_toml_string(str(blade['template']))}")
    catalog = blade.get("catalog")
    if kind in ("sql", "other") and isinstance(catalog, dict) and catalog.get("command"):
        lines += [
            "",
            "[catalog]",
            f"language = {_toml_string(str(catalog.get('language', 'sql')))}",
            f"command = {_toml_string(str(catalog['command']), multiline=True)}",
        ]
    if kind == "vault":
        lines += ["", "[vault]  # environment variable = the secret's name in the vault"]
        for row in blade.get("vault_mapping", []):
            lines.append(f"{row['env']} = {_toml_string(str(row['secret']))}")
    return lines


def write_blade(blade: dict[str, Any]) -> str:
    """The blade as its portable file. `blade` is a saved blade's JSON (its `to_json()`)."""
    if blade.get("secrets") and isinstance(blade["secrets"], dict):
        raise PortableError("A portable blade never holds secret values, only their names.")
    header = ["# /// " + HEADER_TYPE]
    for line in "\n".join(_header_lines(blade)).split("\n"):
        header.append("# " + line if line else "#")
    header.append(_CLOSE)
    code = "" if blade.get("kind") == "secret" else str(blade.get("code", ""))
    return "\n".join(header) + "\n" + code


def file_name(name: str) -> str:
    """A file name for the blade: its name, made safe for every file system."""
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_").lower() or "blade"
    return stem + SUFFIX


# ===== reading =====


def _header_start(lines: list[str]) -> int | None:
    """Where the blade header opens, when it is the file's frontmatter: only blank lines and comments
    (a shebang, an encoding line, a licence) may come before it. A header further down, after code, does
    not count, so an ordinary script that mentions the marker is never taken for a blade."""
    for i, line in enumerate(lines[:200]):
        match = _OPEN.match(line)
        if match and match["type"] == HEADER_TYPE:
            return i
        if line.strip() and not line.lstrip().startswith("#"):
            return None
    return None


def is_blade(text: str) -> bool:
    """True when the text opens with a blade header (the test a blade folder's files must pass)."""
    return _header_start(text.split("\n")) is not None


def read_blade(text: str) -> dict[str, Any]:
    """The blade a portable file holds, ready for the save screen (or BladeStore.save). Nothing runs."""
    lines = text.split("\n")
    start = _header_start(lines)
    if start is None:
        raise PortableError(
            f"This file has no {HEADER_TYPE} header: it is not a blade (export one to see the form)."
        )
    end = next((i for i in range(start + 1, len(lines)) if lines[i] == _CLOSE), None)
    if end is None:
        raise PortableError(
            f"The {HEADER_TYPE} header is never closed: it needs a line that reads '{_CLOSE}'."
        )
    body: list[str] = []
    for line in lines[start + 1 : end]:
        if line == "#":
            body.append("")
        elif line.startswith("# "):
            body.append(line[2:])
        else:
            raise PortableError(f"Every header line starts with '# '; this one does not: {line[:60]!r}")
    try:
        header = tomllib.loads("\n".join(body))
    except tomllib.TOMLDecodeError as error:
        raise PortableError(f"The header is not valid TOML: {error}.") from None
    if header.get("format") != FORMAT:
        raise PortableError(
            f"This blade is in format {header.get('format')!r}; this SwissArmyGrid reads format {FORMAT}. "
            "Update SwissArmyGrid to import it."
        )
    name, kind = header.get("name"), header.get("kind")
    if not isinstance(name, str) or not name.strip():
        raise PortableError('The header names no blade: it needs name = "...".')
    if kind not in KINDS:
        raise PortableError(f"The header's kind must be one of {', '.join(KINDS)}, not {kind!r}.")
    code = "\n".join(lines[end + 1 :])
    blade: dict[str, Any] = {"name": name, "kind": kind}
    if kind == "secret":
        names = header.get("env_names")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise PortableError('A secret blade\'s header lists env_names = ["..."].')
        blade |= {"env_names": names, "code": ""}
    else:
        try:
            ast.parse(code, filename=f"{name} (imported)")
        except SyntaxError as error:
            where = f" on line {error.lineno}" if error.lineno else ""
            raise PortableError(f"The blade's code is not valid Python{where}: {error.msg}.") from None
        blade |= {"code": code, "variable": str(header.get("variable") or "")}
        if kind == "sql":
            blade["dialect"] = str(header.get("dialect") or "other")
        catalog = header.get("catalog")
        if isinstance(catalog, dict) and isinstance(catalog.get("command"), str):
            blade["catalog"] = {
                "language": str(catalog.get("language", "sql")),
                "command": catalog["command"],
            }
        vault = header.get("vault")
        if kind == "vault":
            if not isinstance(vault, dict) or not all(isinstance(v, str) for v in vault.values()):
                raise PortableError('A vault blade\'s header has a [vault] table: VARIABLE = "secret name".')
            blade["vault_mapping"] = [{"env": env, "secret": secret} for env, secret in vault.items()]
    template = header.get("template")
    blade["template"] = template if isinstance(template, str) else None
    description = header.get("description")
    if isinstance(description, str) and description:
        blade["description"] = description
    tags = header.get("tags")
    if isinstance(tags, list) and tags:
        blade["tags"] = [str(t) for t in tags if isinstance(t, str)]
    return blade


def _found(file: str, data: bytes) -> Found:
    if len(data) > _MAX_FILE_BYTES:
        return Found(
            file, None, f"{file} is over {_MAX_FILE_BYTES // 1_000_000} MB; a blade is a short file."
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return Found(file, None, f"{file} is not text (UTF-8).")
    try:
        return Found(file, read_blade(text.replace("\r\n", "\n")), None)
    except PortableError as error:
        return Found(file, None, str(error))


def read_blades(files: list[tuple[str, bytes]]) -> list[Found]:
    """Every blade in the files offered: `.blade.py` (or `.py`) files, and zips of them. Zip entries are
    read in memory, never extracted, so a path inside one is only a label."""
    found: list[Found] = []
    for file, data in files:
        if file.lower().endswith(".zip") or data[:4] == b"PK\x03\x04":
            try:
                archive = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                found.append(Found(file, None, f"{file} is not a zip file."))
                continue
            entries = [e for e in archive.infolist() if not e.is_dir() and e.filename.lower().endswith(".py")]
            if len(entries) > _MAX_ZIP_ENTRIES or sum(e.file_size for e in entries) > _MAX_ZIP_BYTES:
                found.append(Found(file, None, f"{file} holds too much to be a set of blades."))
                continue
            for entry in entries:
                if entry.file_size > _MAX_FILE_BYTES:
                    found.append(Found(entry.filename, None, f"{entry.filename} is too large to be a blade."))
                    continue
                found.append(_found(entry.filename, archive.read(entry)))
        else:
            found.append(_found(file, data))
    return found


def write_zip(blades: list[dict[str, Any]]) -> bytes:
    """Several blades as one zip of portable files (names made unique)."""
    buffer = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for blade in blades:
            name = file_name(str(blade["name"]))
            stem, n = name[: -len(SUFFIX)], 2
            while name in used:
                name, n = f"{stem}_{n}{SUFFIX}", n + 1
            used.add(name)
            archive.writestr(name, write_blade(blade))
    return buffer.getvalue()
