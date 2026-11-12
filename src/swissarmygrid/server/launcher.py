"""The `swissarmygrid` command, wherever it was installed from.

The one package installs this command twice over: with the server (the `[server]` extra, as a uv tool) and
with the plain client (in a project's environment, which has no uvicorn or DuckDB). A person who has the
project's environment active and types `swissarmygrid start` gets the project's copy first on PATH. That copy
cannot run a server, so instead of failing with an import error it hands the command to the server's own
install, found where uv puts its tools; with no server installed anywhere it says how to install one.

Standard library only: this must import in the client's environment (server/__init__.py imports nothing).
It is the one module outside the server proper that starts a process, and only ever the server itself.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

# What the server needs that the client does not have.
SERVER_MODULES: tuple[str, ...] = ("uvicorn", "starlette", "duckdb", "pyarrow")

INSTALL_HINT: str = (
    "The SwissArmyGrid server is not installed on this machine "
    "(this environment has only the Python client).\n"
    "Install it once, with its own Python, from a release:\n"
    "  Windows:        powershell -ExecutionPolicy Bypass -File install.ps1 <wheel>\n"
    "  macOS, Linux:   sh install.sh <wheel>\n"
    '  or directly:    uv tool install "<wheel>[server]"'
)


def has_server(find_spec: Callable[[str], object | None] = importlib.util.find_spec) -> bool:
    """Can this Python run the server?"""
    return all(find_spec(name) is not None for name in SERVER_MODULES)


def tool_pythons(env: Mapping[str, str], home: Path, platform: str, uv_tool_dir: str | None) -> list[Path]:
    """Where the server's own Python may be, in the order to try: SWISSARMYGRID_SERVER, then uv's tool
    folder (UV_TOOL_DIR, what `uv tool dir` says, then uv's defaults), as the VS Code extension looks."""
    if env.get("SWISSARMYGRID_SERVER"):
        return [Path(env["SWISSARMYGRID_SERVER"])]
    python = ("Scripts", "python.exe") if platform == "win32" else ("bin", "python")
    roots: list[Path] = []
    for given in (env.get("UV_TOOL_DIR"), uv_tool_dir):
        if given:
            roots.append(Path(given))
    if platform == "win32" and env.get("APPDATA"):
        roots.append(Path(env["APPDATA"]) / "uv" / "tools")
    roots.append(Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "uv" / "tools")
    seen: list[Path] = []
    for root in roots:
        candidate = root.joinpath("swissarmygrid", *python)
        if candidate not in seen:
            seen.append(candidate)
    return seen


def _uv_tool_dir() -> str | None:
    uv = shutil.which("uv")
    if uv is None:
        return None
    try:
        # uv itself, found on PATH, with fixed arguments.
        done = subprocess.run(  # noqa: S603
            [uv, "tool", "dir"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def find_server_python() -> Path | None:
    """The server's installed Python, when it is not this environment's own. Environments are compared, not
    interpreters: a uv environment's python links to a shared interpreter, so two environments' pythons can
    resolve to the same file."""
    here = Path(sys.prefix).resolve()
    for candidate in tool_pythons(os.environ, Path.home(), sys.platform, _uv_tool_dir()):
        if candidate.is_file() and candidate.parent.parent.resolve() != here:
            return candidate
    return None


def main() -> None:
    if has_server():
        from swissarmygrid.server.__main__ import main as server_main

        server_main()
        return
    python = find_server_python()
    if python is None:
        print(INSTALL_HINT, file=sys.stderr)
        sys.exit(1)
    # A child, not exec: Windows has no real exec, and the child's exit code is passed on as ours.
    # The server's own installed Python, running this package's server with the person's arguments.
    done = subprocess.run([str(python), "-m", "swissarmygrid.server", *sys.argv[1:]], check=False)  # noqa: S603
    sys.exit(done.returncode)


if __name__ == "__main__":
    main()
