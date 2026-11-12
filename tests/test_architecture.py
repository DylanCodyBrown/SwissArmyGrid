"""The shape of the code (REFACTOR.md): three layers, imports pointing inwards, nothing deeper than
`swissarmygrid/<folder>/<file>.py`, and a client that needs nothing but the standard library.

  domain/     the model and its rules. The standard library and sqlglot (text parsing); no I/O.
  services/   the use cases. The domain and each other; every dependency arrives as an argument.
  adapters/, server/, cli/, mcp/, client/   the edges: DuckDB, files, HTTP, processes.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "swissarmygrid"

# What the plain wheel does not install: the server's packages (pyproject's [server] extra).
SERVER_PACKAGES = (
    "duckdb",
    "pyarrow",
    "pandas",
    "starlette",
    "uvicorn",
    "sqlglot",
    "keyring",
    "marimo",
    "mcp",
    "openpyxl",
    "xlsxwriter",
    "pytz",
    "websockets",
)
# The plain client: every module `import swissarmygrid`, `sag`, `sag.push`, `sag.blade` can reach.
CLIENT_MODULES = (
    "swissarmygrid",
    "swissarmygrid.client",
    "swissarmygrid.client.push_client",
    "swissarmygrid.client.blades_client",
    "swissarmygrid.client.watcher",
    "swissarmygrid.client.context_hook",
    "swissarmygrid.cli",
    "swissarmygrid.cli.api",
    "swissarmygrid.cli.commands",
    "swissarmygrid.cli.main",
)
# Standard library modules that do I/O, start something or read the clock: not for the domain.
DOMAIN_FORBIDDEN = {
    "asyncio",
    "getpass",
    "http",
    "importlib",
    "os",
    "pathlib",
    "shutil",
    "socket",
    "subprocess",
    "tempfile",
    "time",
    "urllib",
    "webbrowser",
}
# The service layer coordinates (asyncio, threading timers) but touches nothing itself.
SERVICES_FORBIDDEN = {
    "getpass",
    "http",
    "importlib",
    "os",
    "pathlib",
    "shutil",
    "socket",
    "subprocess",
    "tempfile",
    "urllib",
    "webbrowser",
}


def _imports(path: Path) -> set[str]:
    """Every module `path` imports, anywhere in the file (inside functions too), as dotted names."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _layer_files(folder: str) -> list[Path]:
    return sorted((PACKAGE / folder).glob("*.py"))


def _own(module: str) -> str | None:
    """The package folder (or root file) a `swissarmygrid.x...` import names; None for other packages."""
    parts = module.split(".")
    if parts[0] != "swissarmygrid":
        return None
    return parts[1] if len(parts) > 1 else ""


def test_nothing_is_deeper_than_a_folder_and_a_file() -> None:
    too_deep = [
        str(path.relative_to(PACKAGE))
        for path in PACKAGE.rglob("*.py")
        if len(path.relative_to(PACKAGE).parts) > 2
    ]
    assert too_deep == []


@pytest.mark.parametrize("path", _layer_files("domain"), ids=lambda p: p.name)
def test_the_domain_imports_only_itself_and_does_no_io(path: Path) -> None:
    problems: list[str] = []
    for module in _imports(path):
        top = module.split(".")[0]
        own = _own(module)
        if own is not None:
            # `from swissarmygrid.domain import steps` also lists "swissarmygrid.domain" itself.
            if own != "domain":
                problems.append(f"imports {module}")
        elif top not in sys.stdlib_module_names and top != "sqlglot":
            problems.append(f"imports {module}")
        elif top in DOMAIN_FORBIDDEN:
            problems.append(f"imports {module}")
    assert problems == []


@pytest.mark.parametrize("path", _layer_files("services"), ids=lambda p: p.name)
def test_the_service_layer_imports_only_the_domain_and_itself(path: Path) -> None:
    problems: list[str] = []
    for module in _imports(path):
        top = module.split(".")[0]
        own = _own(module)
        if own is not None:
            if own not in ("domain", "services", "settings"):
                problems.append(f"imports {module}")
        elif top not in sys.stdlib_module_names or top in SERVICES_FORBIDDEN:
            problems.append(f"imports {module}")
    assert problems == []


@pytest.mark.parametrize("path", _layer_files("adapters"), ids=lambda p: p.name)
def test_the_driven_adapters_do_not_know_the_doors(path: Path) -> None:
    """`adapters/` serves the service layer; it never reaches for HTTP routes, the `sag` command, the
    MCP server or the composition root."""
    doors = {"server", "cli", "mcp", "client", "bootstrap"}
    assert sorted(m for m in _imports(path) if _own(m) in doors) == []


def test_the_client_needs_only_the_standard_library() -> None:
    """The plain wheel installs no dependencies. With every server package made impossible to import,
    each client module still imports, and nothing outside the standard library ends up loaded."""
    probe = f"""
import importlib, importlib.abc, sys

BLOCKED = {SERVER_PACKAGES!r}

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(f"{{name}} is not installed with the plain client")
        return None

sys.meta_path.insert(0, Block())
for module in {CLIENT_MODULES!r}:
    importlib.import_module(module)
foreign = sorted(
    name for name in sys.modules
    if name.split(".")[0] not in sys.stdlib_module_names
    and name.split(".")[0] not in ("swissarmygrid", "__main__")
    and not name.startswith("_")
)
ours = sorted(name for name in sys.modules if name.startswith("swissarmygrid"))
print(foreign)
print(ours)
"""
    result = subprocess.run(  # noqa: S603 (this Python, a fixed probe)
        [sys.executable, "-S", "-c", probe],
        capture_output=True,
        text=True,
        cwd=PACKAGE.parent,
        env={"PYTHONPATH": str(PACKAGE.parent), "SWISSARMYGRID_WATCH": "0"},
    )
    assert result.returncode == 0, result.stderr
    foreign, ours = result.stdout.strip().splitlines()
    assert foreign == "[]"
    # None of the server's own modules came along either.
    loaded = ast.literal_eval(ours)
    assert [
        m for m in loaded if m.split(".")[1:2] in (["server"], ["services"], ["mcp"], ["bootstrap"])
    ] == []
