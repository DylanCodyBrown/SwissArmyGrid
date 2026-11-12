"""The `swissarmygrid` command run from the client's environment hands over to the installed server."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from swissarmygrid.server.launcher import INSTALL_HINT, has_server, tool_pythons


def test_this_environment_has_the_server() -> None:
    assert has_server()


def test_a_missing_server_module_means_no_server() -> None:
    assert not has_server(lambda name: None if name == "uvicorn" else object())


def test_the_override_is_the_only_candidate() -> None:
    found = tool_pythons({"SWISSARMYGRID_SERVER": "/dev/python"}, Path("/home/me"), "linux", "/uv/tools")
    assert found == [Path("/dev/python")]


def test_uv_tool_folders_in_order_without_repeats() -> None:
    found = tool_pythons({"UV_TOOL_DIR": "/a"}, Path("/home/me"), "linux", "/a")
    assert found == [
        Path("/a/swissarmygrid/bin/python"),
        Path("/home/me/.local/share/uv/tools/swissarmygrid/bin/python"),
    ]


def test_windows_looks_in_appdata_and_scripts() -> None:
    found = tool_pythons({"APPDATA": "C:/Users/me/AppData/Roaming"}, Path("C:/Users/me"), "win32", None)
    assert found[0] == Path("C:/Users/me/AppData/Roaming/uv/tools/swissarmygrid/Scripts/python.exe")


def test_no_server_anywhere_says_how_to_install(tmp_path: Path) -> None:
    """Run the launcher in a Python that cannot see the server's modules, with uv's tool folder empty."""
    script = (
        "import sys, importlib.util\n"
        "real = importlib.util.find_spec\n"
        "importlib.util.find_spec = lambda n, *a: None if n == 'uvicorn' else real(n, *a)\n"
        "from swissarmygrid.server import launcher\n"
        "launcher._uv_tool_dir = lambda: None\n"
        "launcher.main()\n"
    )
    env = {"UV_TOOL_DIR": str(tmp_path), "XDG_DATA_HOME": str(tmp_path), "HOME": str(tmp_path), "PATH": ""}
    done = subprocess.run(  # noqa: S603 (this test's own interpreter and script)
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60, check=False
    )
    assert done.returncode == 1
    assert INSTALL_HINT.splitlines()[0] in done.stderr
    assert "Traceback" not in done.stderr
