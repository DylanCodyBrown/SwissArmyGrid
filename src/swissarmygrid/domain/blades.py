"""Blades: saved Python that reaches outside Python to load something (plan decisions 14-16).

A database connection, an API client, a CSV path, a workbook: any code that loads from outside is a
blade. It is written once, saved by the server, and run wherever it is needed: `sag.blade("name")` runs
it in the caller's Python and returns its variable; the server's Refresh runs it in a child process to
load the blade's catalog.

Everything here is about a blade as text and as a record; nothing is run, read from disk or looked up.
Standard library only, so the plain client can use it. In this order:

  the code       what a blade's code makes, asks for and imports (read with `ast`, never run)
  the packages   the pip packages its imports need
  the file       the portable `.blade.py` file: a `# /// swissarmygrid-blade` TOML header, and zips
  the settings   the values each person fills in: found with `ast`, placeholders, suggestions, written back
  the blade      the saved record (`Blade`) and the check every save goes through (`parse_blade`)

Running a blade is adapters/blade_run.py; keeping blades and their secrets is adapters/blades.py; the
starter templates are server/blade_templates.py.
"""

from __future__ import annotations

import ast
import io
import json
import re
import shlex
import sys
import tomllib
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from swissarmygrid.domain.catalogs import CatalogError, check_name, normalize_dialect

# ===== the code =====
# What a blade's code makes, asks for and imports, read with `ast` and never run.
#
# - The variable: the last name the code assigns at its top level (`conn = pyodbc.connect(...)`), so a
#   highlighted snippet needs no editing to become a blade. The save screen shows it and lets it change.
# - The secrets: every `secret("name")` call with a literal name. Their values live in the operating
#   system's vault, never in the code.
# - The imports: top-level module names, for the package check (below) and for adding imports to
#   a script the blade is dropped into.


class BladeCodeError(ValueError):
    """The code cannot be read as Python. The message gives the line."""


@dataclass(frozen=True)
class BladeCode:
    variable: str | None
    assigned: list[str] = field(default_factory=list)  # every top-level name, in order
    secrets: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)  # dotted module names as written
    calls: list[str] = field(default_factory=list)  # attribute and function names called (read_excel, ...)


def parse_code(code: str, label: str = "the blade") -> ast.Module:
    try:
        return ast.parse(code, filename=label)
    except SyntaxError as error:
        where = f" on line {error.lineno}" if error.lineno else ""
        raise BladeCodeError(f"{label} is not valid Python{where}: {error.msg}.") from error


def _targets(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Tuple | ast.List):
        return [name for element in node.elts for name in _targets(element)]
    if isinstance(node, ast.Starred):
        return _targets(node.value)
    return []


def _top_level_names(statements: list[ast.stmt]) -> list[str]:
    names: list[str] = []
    for statement in statements:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                names.extend(_targets(target))
        elif isinstance(statement, ast.AnnAssign | ast.AugAssign) and statement.value is not None:
            names.extend(_targets(statement.target))
        elif isinstance(statement, ast.With | ast.AsyncWith):
            for item in statement.items:
                if item.optional_vars is not None:
                    names.extend(_targets(item.optional_vars))
            names.extend(_top_level_names(statement.body))
        elif isinstance(statement, ast.If | ast.Try):
            # A blade may pick a driver or a path in an if or try; what it assigns there still counts.
            for block in (
                statement.body,
                getattr(statement, "orelse", []),
                getattr(statement, "finalbody", []),
            ):
                names.extend(_top_level_names(block))
            for handler in getattr(statement, "handlers", []):
                names.extend(_top_level_names(handler.body))
        elif isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(statement.name)
    return names


def read_code(code: str, label: str = "the blade") -> BladeCode:
    """What the code makes, asks for and imports."""
    tree = parse_code(code, label)
    assigned = _top_level_names(tree.body)
    imported_names: set[str] = set()
    imports: list[str] = []
    secrets: list[str] = []
    calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
                imported_names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imports.append(node.module)
            imported_names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name:
                calls.append(name)
            if (
                isinstance(func, ast.Name)
                and func.id == "secret"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                secrets.append(node.args[0].value)
    # The variable is the last thing the code made, a value or a function (a vault's `get`), never a
    # module it imported.
    values = [n for n in assigned if n not in imported_names and not n.startswith("_")]
    return BladeCode(
        variable=values[-1] if values else None,
        assigned=list(dict.fromkeys(assigned)),
        secrets=list(dict.fromkeys(secrets)),
        imports=list(dict.fromkeys(imports)),
        calls=list(dict.fromkeys(calls)),
    )


def _function_params(code: str, name: str) -> int | None:
    """How many positional arguments the top-level function `name` takes; None when it is not one."""
    for statement in parse_code(code).body:
        if isinstance(statement, ast.FunctionDef) and statement.name == name:
            args = statement.args
            return len(args.posonlyargs) + len(args.args)
    return None


def import_lines(code: str) -> list[str]:
    """The code's top-level import statements, as written (for adding to a script's top)."""
    tree = parse_code(code)
    lines = code.splitlines()
    found: list[str] = []
    for statement in tree.body:
        if isinstance(statement, ast.Import | ast.ImportFrom):
            end = statement.end_lineno or statement.lineno
            found.append("\n".join(lines[statement.lineno - 1 : end]))
    return found


def missing_imports(script: str, wanted: list[str]) -> list[str]:
    """The import statements in `wanted` that `script` does not already make. An import counts as made
    when the script binds the same name to the same thing: `import pandas as pd` is there when the
    script has it anywhere (even among others on one line), and `from x import a` is there when the
    script has `from x import a, b`; but `import swissarmygrid` does not give the `sag` that
    `import swissarmygrid as sag` would. A script that does not parse yet gets them all."""
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return list(wanted)
    have: set[tuple[str, ...]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            have.update(("import", alias.name, alias.asname or alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            have.update(("from", node.module, alias.name, alias.asname or alias.name) for alias in node.names)
    needed: list[str] = []
    for line in wanted:
        try:
            statement = ast.parse(line).body[0]
        except (SyntaxError, IndexError):
            continue
        keys: list[tuple[str, ...]]
        if isinstance(statement, ast.Import):
            keys = [("import", alias.name, alias.asname or alias.name) for alias in statement.names]
        elif isinstance(statement, ast.ImportFrom) and statement.module:
            module = statement.module
            keys = [("from", module, alias.name, alias.asname or alias.name) for alias in statement.names]
        else:
            continue
        if not all(key in have for key in keys):
            needed.append(line)
    return needed


# ===== the packages =====
# The pip packages a blade needs.
#
# A blade's imports are read from its code (above). Each top-level module maps to the package that
# installs it, where the two names differ (`snowflake.connector` comes from
# `snowflake-connector-python`, `yaml` from `PyYAML`); a few calls need a package the code never
# imports (`pd.read_excel` needs openpyxl). Standard library modules need nothing. A module this table
# does not know is assumed to install under its own name, and is marked as a guess.

# Dotted module (the longest that matches wins) -> pip package.
PIP_NAMES: dict[str, str] = {
    "pyodbc": "pyodbc",
    "pymssql": "pymssql",
    "mssql_python": "mssql-python",
    "psycopg": "psycopg[binary]",
    "psycopg2": "psycopg2-binary",
    "pg8000": "pg8000",
    "pymysql": "PyMySQL",
    "MySQLdb": "mysqlclient",
    "mysql.connector": "mysql-connector-python",
    "mariadb": "mariadb",
    "oracledb": "oracledb",
    "cx_Oracle": "cx_Oracle",
    "snowflake.connector": "snowflake-connector-python",
    "google.cloud.bigquery": "google-cloud-bigquery",
    "google.cloud.storage": "google-cloud-storage",
    "redshift_connector": "redshift-connector",
    "databricks.sql": "databricks-sql-connector",
    "ibm_db": "ibm_db",
    "ibm_db_dbi": "ibm_db",
    "teradatasql": "teradatasql",
    "trino": "trino",
    "prestodb": "presto-python-client",
    "clickhouse_connect": "clickhouse-connect",
    "hdbcli": "hdbcli",
    "vertica_python": "vertica-python",
    "sqlalchemy": "SQLAlchemy",
    "pandas": "pandas",
    "polars": "polars",
    "pyarrow": "pyarrow",
    "numpy": "numpy",
    "openpyxl": "openpyxl",
    "xlrd": "xlrd",
    "pyxlsb": "pyxlsb",
    "duckdb": "duckdb",
    "requests": "requests",
    "httpx": "httpx",
    "boto3": "boto3",
    "s3fs": "s3fs",
    "azure.storage.blob": "azure-storage-blob",
    "azure.identity": "azure-identity",
    "azure.keyvault.secrets": "azure-keyvault-secrets",
    "google.cloud.secretmanager": "google-cloud-secret-manager",
    "hvac": "hvac",
    "onepassword": "onepassword-sdk",
    "keyring": "keyring",
    "yaml": "PyYAML",
    "dotenv": "python-dotenv",
    "bs4": "beautifulsoup4",
    "lxml": "lxml",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "pymongo": "pymongo",
    "redis": "redis",
    "elasticsearch": "elasticsearch",
    "simple_salesforce": "simple-salesforce",
    "gspread": "gspread",
}

# Calls that need a package the code may never import: `pd.read_excel(...)` reads .xlsx with openpyxl.
CALL_NEEDS: dict[str, str] = {
    "read_excel": "openpyxl",
    "to_excel": "openpyxl",
    "read_parquet": "pyarrow",
    "to_parquet": "pyarrow",
    "read_xml": "lxml",
    "read_html": "lxml",
}

# Never installed by pip: the standard library, and SwissArmyGrid itself (sag.blade gives it).
_PROVIDED: frozenset[str] = frozenset({*sys.stdlib_module_names, "swissarmygrid", "__future__"})


@dataclass(frozen=True)
class Package:
    module: str  # the top-level module Python imports
    pip: str  # what installs it
    guessed: bool  # not in PIP_NAMES: assumed to install under its own name
    installed: bool  # importable in this Python

    def to_json(self) -> dict[str, object]:
        return {"module": self.module, "pip": self.pip, "guessed": self.guessed, "installed": self.installed}


def pip_name(dotted: str) -> tuple[str, bool]:
    parts = dotted.split(".")
    for end in range(len(parts), 0, -1):
        known = PIP_NAMES.get(".".join(parts[:end]))
        if known:
            return known, False
    return parts[0], True


def needed(code: str, installed: Callable[[str], bool]) -> list[Package]:
    """Every package the blade needs, whether installed or not (`installed` answers for one module:
    adapters/blade_run.py asks this Python), in the order the code uses them."""
    found = read_code(code)
    packages: dict[str, Package] = {}
    for dotted in found.imports:
        if dotted.split(".")[0] in _PROVIDED:
            continue
        pip, guessed = pip_name(dotted)
        if pip not in packages:
            packages[pip] = Package(dotted, pip, guessed, installed(dotted))
    for call in found.calls:
        extra = CALL_NEEDS.get(call)
        if extra and extra not in packages:
            packages[extra] = Package(extra, extra, False, installed(extra))
    return list(packages.values())


def install_command(pips: list[str], python: str | None = None) -> str:
    """`uv pip install ...` for these packages; into `python`'s environment when given."""
    target = f"--python {shlex.quote(python)} " if python else ""
    return f"uv pip install {target}" + " ".join(shlex.quote(p) for p in pips)


def missing_module_hint(module: str) -> str:
    """The sentence for an ImportError: what to install, and the command that does it."""
    pip, _ = pip_name(module)
    return (
        f"This blade needs {pip}, which is not installed in this Python. "
        f"Install it with: {install_command([pip])}"
    )


# ===== the file =====
# Portable blades: a blade as one plain Python file, `<name>.blade.py`, to share, keep in Git, or import.
#
# The file is the blade's own code under a header comment block, written the way Python's inline script
# metadata is (`# /// <type>` ... `# ///`, each line starting `# `), holding TOML:
#
#     # /// swissarmygrid-blade
#     # format = 1
#     # name = "Warehouse"
#     # kind = "sql"
#     # description = "The sales warehouse, read only"
#     # tags = ["sales", "orders-function-app"]
#     # dialect = "sqlserver"
#     # variable = "conn"
#     # secrets = ["password"]
#     #
#     # [catalog]
#     # language = "sql"
#     # command = '''
#     # SELECT ...
#     # '''
#     # ///
#     import pyodbc
#     conn = pyodbc.connect(...)
#
# A vault blade adds `[vault]` (environment variable = secret name); a secret blade lists `env_names` and
# has no code. Secret values are never written: `secrets` lists only the names the code asks for, which
# the person importing types into their own vault. Several blades travel as a zip of these files.
#
# Reading never runs anything: it parses the header (tomllib) and checks the code is Python (ast).
# Standard library only, so the client can use it too.

FORMAT: int = 1
HEADER_TYPE: str = "swissarmygrid-blade"
SUFFIX: str = ".blade.py"
# An analysis blade is the user's own analysis: a function of the grid's rows (a pandas DataFrame, and
# optionally the selected columns) that returns markdown, a table, a figure, or a list or dict of them.
# It shows in the Analyze menu and runs in a child process, like Refresh.
KINDS: tuple[str, ...] = ("sql", "other", "vault", "secret", "analysis")
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
    if kind == "vault" and blade.get("vault_all"):
        lines.append("vault_all = true  # every secret in the vault, each under its own name")
    if kind != "secret":
        lines.append(f"variable = {_toml_string(str(blade.get('variable') or ''))}")
        try:
            secrets = read_code(str(blade.get("code", ""))).secrets
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
    if kind == "vault" and blade.get("vault_mapping"):
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
            f"This blade is in format {header.get('format')!r}; this version reads format {FORMAT}. "
            "Update the grid server to import it."
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
        vault = header.get("vault", {} if header.get("vault_all") is True else None)
        if kind == "vault":
            if not isinstance(vault, dict) or not all(isinstance(v, str) for v in vault.values()):
                raise PortableError('A vault blade\'s header has a [vault] table: VARIABLE = "secret name".')
            blade["vault_mapping"] = [{"env": env, "secret": secret} for env, secret in vault.items()]
            blade["vault_all"] = header.get("vault_all") is True
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


# ===== the settings =====
# The values a blade's code is written with that each person fills in for themselves: its settings.
#
# A blade from someone else says `host="their-server"` or `"Server=their-server;Database=sales;"`.
# These are read from the code with `ast` (never run), so the import review and the save screen can show
# one field per setting, flag the ones that are still placeholders (`my-server`, `<your host>`,
# `example`), suggest values (the user's other blades, a few standard environment variables), and write a
# new value back into exactly that place in the code.
#
# What counts as a setting:
#
# - a keyword argument whose name is a connection setting (`host=`, `port=`, `dbname=`, `user=`,
#   `account=`, `warehouse=`, `http_path=`, `vault_url=`, ...) given a literal: `host="my-server"`;
# - a top-level name given a literal or a path: `project = "my-project"`,
#   `path = Path(r"C:\data\orders.csv")`;
# - a path given as the first argument of a call: `duckdb.connect(r"C:\data\shop.duckdb")`;
# - a `Key=value;` part of a connection string: `"Server=my-server;Database=my_database;"`.
#
# Passwords, tokens and keys are never settings: they belong in `secret("...")`. Standard library only.

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
    # Encryption (SSL/TLS): the scheme, and the certificate authority's file (.pem) the client trusts.
    "http_scheme": "scheme",
    "verify": "certificate file",
    "sslrootcert": "certificate file",
    "ssl_ca": "certificate file",
    "ca": "certificate file",
    "sslmode": "encryption",
    "trust_server_certificate": "trust server certificate",
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
    "trustservercertificate": "trust server certificate",
    "encrypt": "encrypt",
}
# Settings that are on or off (a checkbox): True/False in code, yes/no in a connection string.
FLAGS = frozenset({"trust server certificate", "encrypt"})
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
            "type": "flag"
            if self.name in FLAGS
            else "number"
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


def _flag(node: ast.expr | None, name: str | None) -> ast.Constant | None:
    """A True/False literal for an on/off setting (FLAGS); other booleans are never settings."""
    if name in FLAGS and isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node
    return None


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def detect_settings(code: str) -> list[Setting]:
    """Every setting in the code, in the order it is written. Raises BladeCodeError for code that is
    not Python."""
    return [f.setting for f in _detect(code)]


def _detect(code: str) -> list[_Found]:
    tree = parse_code(code)
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
        if isinstance(target, ast.Name):
            literal = literal or _flag(value, KEYWORDS.get(target.id.casefold()))
        if isinstance(target, ast.Name) and literal is not None:
            # A known setting's own word first (verify = r"...pem" is a certificate file, not any path).
            known = KEYWORDS.get(target.id.casefold())
            name = (
                known
                if known
                else "path"
                if isinstance(literal.value, str) and _LOOKS_LIKE_PATH.search(literal.value)
                else target.id
            )
            add(literal, name, target.id, "name")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                word = KEYWORDS.get(keyword.arg.casefold()) if keyword.arg else None
                literal = _literal(keyword.value) or _flag(keyword.value, word)
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


def apply_settings(code: str, values: dict[str, Any]) -> str:
    """The code with each setting (by its id from `detect_settings` on this same code) set to a new value.
    Raises FillError when a value cannot go where its setting is."""
    by_id = {f.setting.id: f for f in _detect(code)}
    edits: list[tuple[int, int, str]] = []
    for setting_id, new in values.items():
        spot = by_id.get(setting_id)
        if spot is None:
            raise FillError("The code changed since its settings were read: read them again.")
        old = spot.setting.value
        if isinstance(old, bool):  # an on/off setting: True or False
            on = new is True or str(new).strip().casefold() in ("true", "yes", "1", "on")
            edits.append((spot.start, spot.end, "True" if on else "False"))
            continue
        if spot.inside_string and spot.setting.name in FLAGS:  # yes or no, as the driver reads it
            on = new is True or str(new).strip().casefold() in ("true", "yes", "1", "on")
            # true/false where the code said so; else yes/no (Encrypt=strict becomes yes or no)
            was_bool = str(old).strip().casefold() in ("true", "false")
            word = ("true" if on else "false") if was_bool else ("yes" if on else "no")
            edits.append((spot.start, spot.end, word))
            continue
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
        parse_code(code)
    except BladeCodeError as error:
        raise FillError(f"That value would break the code: {error}") from None
    return code


# ===== suggestions =====


def suggest_settings(
    settings: list[Setting],
    others: Iterable[tuple[str, str, str]],
    environ: Mapping[str, str] | None = None,
) -> list[Setting]:
    """Fill in each setting's suggestions: values the user's other blades use for the same setting
    (`others`: (blade name, dialect, code); the same dialect first, then by how often), then standard
    environment variables. Placeholders are never suggested, and neither is the value already there."""
    env: Mapping[str, str] = {} if environ is None else environ
    seen: dict[str, dict[str, list[tuple[str, str]]]] = {}  # name -> value -> [(blade, dialect)]
    for blade, dialect, code in others:
        try:
            found = detect_settings(code)
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


# ===== the blade =====

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


def env_name_for_secret(secret: str) -> str:
    """A secret's variable, named as in the vault: SqlPassword stays SqlPassword, Sql--Password becomes
    Sql__Password (Azure's convention for a section: `--` in the vault, `__` in the environment), any
    other character a variable cannot hold becomes _ (warehouse-password -> warehouse_password,
    op://Private/Warehouse/password -> Private_Warehouse_password). Case is kept."""
    bare = re.sub(r"^[a-z][a-z0-9+.-]*://", "", secret.strip(), flags=re.IGNORECASE)
    words = re.sub(r"[^A-Za-z0-9_]", "_", bare.replace("--", "__")).strip("_")[:128] or "SECRET"
    return words if not words[0].isdigit() else f"_{words}"[:128]


def every_secret(names: Iterable[str], renamed: Mapping[str, str]) -> dict[str, str]:
    """A vault's mapping when it loads every secret: each name its own variable, but a secret the user
    renamed (`renamed`: variable -> secret) keeps that name. Variable -> secret."""
    by_secret = {secret: env for env, secret in renamed.items()}
    found: dict[str, str] = {}
    for name in names:
        if name not in by_secret:
            found.setdefault(env_name_for_secret(name), name)
    return found | dict(renamed)


_MAX_CODE_CHARS: int = 200_000


class VaultUnavailableError(RuntimeError):
    """This computer has no operating system vault to keep a blade's secrets in."""


class BladeError(ValueError):
    """The blade cannot be saved or used as asked. The message says why in plain words."""


@dataclass
class Blade:
    name: str
    kind: str
    code: str = ""
    variable: str | None = None
    dialect: str = "other"
    catalog_language: str | None = None
    catalog_command: str | None = None
    vault_mapping: dict[str, str] = field(default_factory=dict)  # environment variable -> vault secret name
    # A vault: every secret it holds (its names(), read each time), each under its own name; the mapping
    # then only renames.
    vault_all: bool = False
    env_names: list[str] = field(default_factory=list)  # a secret blade's variables
    template: str | None = None
    description: str = ""
    tags: list[str] = field(default_factory=list)
    path: str | None = None  # a blade folder's file; None for a blade saved in the app
    pack: str | None = None  # an installed pack's label (sales@team 1a2b3c): read-only, from a marketplace
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def secrets(self) -> list[str]:
        """The names its code asks for with secret("...")."""
        if not self.code:
            return []
        try:
            return read_code(self.code, self.name).secrets
        except BladeCodeError:
            return []

    @property
    def has_catalog(self) -> bool:
        return bool(self.catalog_language and self.catalog_command)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "code": self.code,
            "variable": self.variable,
            "dialect": self.dialect,
            "catalog": (
                {"language": self.catalog_language, "command": self.catalog_command}
                if self.has_catalog
                else None
            ),
            "vault_mapping": [{"env": e, "secret": s} for e, s in self.vault_mapping.items()],
            "vault_all": self.vault_all,
            "env_names": list(self.env_names),
            "template": self.template,
            "description": self.description,
            "tags": list(self.tags),
            "path": self.path,
            "pack": self.pack,
            "source": "pack" if self.pack else "folder" if self.path else "saved",
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _text(data: dict[str, Any], key: str, limit: int = 1_000) -> str:
    value = data.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BladeError(f"{key} must be text.")
    if len(value) > limit:
        raise BladeError(f"{key} is too long ({len(value):,} characters; at most {limit:,}).")
    return value


def _env_name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _ENV_NAME.fullmatch(value):
        raise BladeError(
            f"{where}: {value!r} is not an environment variable name. Use letters, digits and '_', "
            "not starting with a digit (WAREHOUSE_PASSWORD)."
        )
    return value


_TAG = re.compile(r"[\w][\w .+#-]{0,39}")


def _tags(value: Any) -> list[str]:
    """Tags as typed (trimmed), each 1 to 40 letters, digits, spaces or . + # - _, at most 20, without
    repeats (ignoring case)."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [part for part in value.split(",")]
    if not isinstance(value, list):
        raise BladeError("Tags are a list of words, such as warehouse, finance.")
    tags: dict[str, str] = {}
    for raw in value:
        if not isinstance(raw, str):
            raise BladeError("Each tag is text.")
        tag = " ".join(raw.split())
        if not tag:
            continue
        if not _TAG.fullmatch(tag):
            raise BladeError(f"Tag {tag!r}: use 1 to 40 letters, digits, spaces or . + # - _.")
        tags.setdefault(tag.casefold(), tag)
    if len(tags) > 20:
        raise BladeError("A blade has at most 20 tags.")
    return list(tags.values())


def parse_blade(data: Any, previous: Blade | None = None, now: float = 0.0) -> Blade:
    """A blade from the save screen's JSON, checked. Raises BladeError with a plain message. `now` is
    the time it is saved at (the caller's clock; nothing here reads one)."""
    if not isinstance(data, dict):
        raise BladeError("Send the blade as a JSON object.")
    try:
        name = check_name(_text(data, "name", 64))
    except CatalogError as error:
        raise BladeError(str(error).replace("Catalog name", "Blade name")) from None
    kind = _text(data, "kind", 10)
    if kind not in KINDS:
        raise BladeError(f"A blade's kind is one of {', '.join(KINDS)}, not {kind!r}.")
    blade = Blade(
        name=name,
        kind=kind,
        template=_text(data, "template", 64) or None,
        description=_text(data, "description", 2_000).strip(),
        tags=_tags(data.get("tags")),
        path=previous.path if previous else None,
        created_at=previous.created_at if previous else now,
        updated_at=now,
    )
    if kind == "secret":
        names = data.get("env_names")
        if not isinstance(names, list) or not names:
            raise BladeError("A secret blade lists at least one environment variable name.")
        blade.env_names = list(dict.fromkeys(_env_name(n, "A secret blade's variable") for n in names))
        if len(blade.env_names) > 100:
            raise BladeError("A secret blade holds at most 100 variables.")
        return blade

    blade.code = _text(data, "code", _MAX_CODE_CHARS)
    if not blade.code.strip():
        raise BladeError(
            "The blade has no code. Paste or write the Python that reaches outside to load something."
        )
    try:
        found = read_code(blade.code, f"Blade {name!r}")
    except BladeCodeError as error:
        raise BladeError(str(error)) from None
    variable = _text(data, "variable", 128).strip() or found.variable
    if not variable:
        raise BladeError(
            "The code makes no variable to hand back. End it with an assignment, such as conn = ..., "
            "or name the variable."
        )
    if variable not in found.assigned:
        raise BladeError(
            f"The code never makes {variable!r}. It makes: {', '.join(found.assigned) or 'nothing'}."
        )
    blade.variable = variable

    if kind == "analysis":
        params = _function_params(blade.code, variable)
        if params is None:
            raise BladeError(
                f"An analysis blade's {variable!r} is a function of the rows: def {variable}(df): ... "
                "(and, if you want them, the selected columns: def analyze(df, columns): ...)."
            )
        if not 1 <= params <= 2:
            raise BladeError(
                f"{variable} takes the rows (df), and optionally the selected columns: 1 or 2 arguments."
            )
        return blade

    if kind == "sql":
        try:
            blade.dialect = normalize_dialect(_text(data, "dialect", 32) or None)
        except CatalogError as error:
            raise BladeError(str(error)) from None

    if kind == "vault":
        mapping = data.get("vault_mapping") or []
        blade.vault_all = data.get("vault_all") is True
        if not isinstance(mapping, list) or not (mapping or blade.vault_all):
            raise BladeError(
                "A vault blade maps at least one secret in the vault to an environment variable, "
                "or loads every secret (vault_all)."
            )
        for row in mapping:
            if not isinstance(row, dict):
                raise BladeError(
                    'Each vault mapping is {"env": "WAREHOUSE_PASSWORD", "secret": "warehouse-pwd"}.'
                )
            env = _env_name(row.get("env"), "A vault mapping's variable")
            secret = row.get("secret")
            if not isinstance(secret, str) or not secret.strip() or len(secret) > 500:
                raise BladeError(f"Name the secret in the vault for {env}.")
            if env in blade.vault_mapping:
                raise BladeError(f"{env} is mapped twice.")
            blade.vault_mapping[env] = secret.strip()
        return blade

    catalog = data.get("catalog")
    if catalog:
        if not isinstance(catalog, dict):
            raise BladeError('The catalog is {"language": "sql" or "python", "command": "..."}.')
        language = _text(catalog, "language", 10)
        command = _text(catalog, "command", _MAX_CODE_CHARS)
        if language not in ("sql", "python"):
            raise BladeError("A catalog command is sql or python.")
        if command.strip():
            if language == "python":
                try:
                    parse_code(command, "The catalog command")
                except BladeCodeError as error:
                    raise BladeError(str(error)) from None
            blade.catalog_language, blade.catalog_command = language, command
    return blade


# ===== a starter =====
# A starting point the user edits on the save screen (the starters themselves: server/blade_templates.py).


@dataclass(frozen=True)
class Template:
    id: str
    label: str
    group: str  # "Databases", "Files", "Vaults", "Other"
    kind: str  # "sql", "other", "vault" or "analysis"
    code: str
    variable: str
    dialect: str = "other"
    catalog_language: str | None = None  # "sql" or "python"
    catalog_command: str | None = None
    vault_mapping: tuple[tuple[str, str], ...] = ()  # (environment variable, secret name) examples
    note: str = ""
    # Ways to sign in, each its own code (the form's Sign in): (label, a text only that code holds, code).
    # The first is `code`.
    sign_ins: tuple[tuple[str, str, str], ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "group": self.group,
            "kind": self.kind,
            "dialect": self.dialect,
            "code": self.code,
            "variable": self.variable,
            "catalog": (
                {"language": self.catalog_language, "command": self.catalog_command}
                if self.catalog_language and self.catalog_command
                else None
            ),
            "vault_mapping": [{"env": e, "secret": s} for e, s in self.vault_mapping],
            "note": self.note,
            "sign_ins": [{"label": label, "mark": mark, "code": code} for label, mark, code in self.sign_ins],
        }
