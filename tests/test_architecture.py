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
    # What the client borrows from the layers: blades as text, and running one in this Python.
    "swissarmygrid.domain.blades",
    "swissarmygrid.domain.catalogs",
    "swissarmygrid.adapters.blade_run",
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


DOORS = [*sorted((PACKAGE / "server").glob("routes_*.py")), PACKAGE / "server" / "http.py"]


@pytest.mark.parametrize("path", DOORS, ids=lambda p: p.name)
def test_the_http_door_reaches_the_adapters_only_through_the_composition_root(path: Path) -> None:
    """A route module is given its service and nothing else: it builds no adapter and imports none
    (the driven ones in `adapters/`), and it does not reach into the composition root. What it may
    import from its own folder is the HTTP helpers and the two things only `server/` can do (start
    the notebook, run a blade in a child process), whose errors it turns into statuses."""
    reached = sorted(m for m in _imports(path) if _own(m) in ("adapters", "bootstrap"))
    assert reached == []


def test_only_the_composition_root_and_the_server_build_adapters() -> None:
    """`bootstrap.py` is where adapters meet services. Besides it, only the server's own start-up
    (the connection file, the live updates it watches to end with VS Code, the blade runner's child)
    and the plain client (running a blade in the caller's Python) name an adapter."""
    allowed = {
        "bootstrap.py",
        "server/__main__.py",
        "server/blade_runner.py",
        "client/blades_client.py",
    }
    named: set[str] = set()
    for path in PACKAGE.rglob("*.py"):
        relative = path.relative_to(PACKAGE).as_posix()
        if relative.startswith("adapters/"):
            continue
        if any(_own(m) == "adapters" for m in _imports(path)):
            named.add(relative)
    assert named <= allowed


@pytest.mark.parametrize("folder", ["domain", "services", "adapters", "server"])
def test_no_layer_file_is_much_over_1500_lines(folder: str) -> None:
    long = {
        path.name: lines
        for path in _layer_files(folder)
        if (lines := len(path.read_text(encoding="utf-8").splitlines())) > 1500
    }
    assert long == {}


def test_every_feature_has_at_most_one_file_in_each_layer() -> None:
    """One file per feature: a name appears once in a layer, and a route module is named after the
    service it calls (or is one of the doors that serve several: the agent's, a grid's)."""
    services = {path.stem for path in _layer_files("services")} - {"__init__", "ports", "workspace"}
    doors = {path.stem.removeprefix("routes_") for path in (PACKAGE / "server").glob("routes_*.py")}
    # A door's name and the service file it stands on.
    stands_on = {"catalog": "catalogs", "cards": "charts", "agent": "agent"}
    assert {stands_on.get(door, door) for door in doors} <= services


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
