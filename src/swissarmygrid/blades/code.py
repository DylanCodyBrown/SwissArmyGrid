"""What a blade's code makes, asks for and imports, read with `ast` and never run.

- The variable: the last name the code assigns at its top level (`conn = pyodbc.connect(...)`), so a
  highlighted snippet needs no editing to become a blade. The save screen shows it and lets it change.
- The secrets: every `secret("name")` call with a literal name. Their values live in the operating
  system's vault, never in the code.
- The imports: top-level module names, for the package check (packages.py) and for adding imports to
  a script the blade is dropped into.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field


class BladeCodeError(ValueError):
    """The code cannot be read as Python. The message gives the line."""


@dataclass(frozen=True)
class BladeCode:
    variable: str | None
    assigned: list[str] = field(default_factory=list)  # every top-level name, in order
    secrets: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)  # dotted module names as written
    calls: list[str] = field(default_factory=list)  # attribute and function names called (read_excel, ...)


def parse(code: str, label: str = "the blade") -> ast.Module:
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


def read(code: str, label: str = "the blade") -> BladeCode:
    """What the code makes, asks for and imports."""
    tree = parse(code, label)
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


def import_lines(code: str) -> list[str]:
    """The code's top-level import statements, as written (for adding to a script's top)."""
    tree = parse(code)
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
