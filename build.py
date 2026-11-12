"""Build SwissArmyGrid: one wheel that is both the client and the server.

    python build.py            # the page (release build), then the wheel in dist/
    python build.py --vsix     # also the optional VS Code extension, carrying that wheel

The wheel is one package with two installs:

    uv pip install swissarmygrid-<version>-py3-none-any.whl              # the client, any environment
    uv tool install "swissarmygrid-<version>-py3-none-any.whl[server]"   # the server, once per machine

The client is standard library only (`import swissarmygrid as sag`), so it never touches a project's own
packages; the server's `swissarmygrid` command runs the grid. The page ships inside the wheel
(pyproject.toml force-includes web/static), so it is built first, without source maps.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
NPM = "npm.cmd" if sys.platform == "win32" else "npm"


def project_version() -> str:
    """The version in pyproject.toml: the wheel, the extension and the installers all carry it."""
    with (HERE / "pyproject.toml").open("rb") as file:
        version: str = tomllib.load(file)["project"]["version"]
    return version


def run(command: list[str], cwd: Path) -> None:
    print(f"> {' '.join(command)}  (in {cwd.relative_to(HERE) or '.'})", flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--vsix", action="store_true", help="Also package the VS Code extension.")
    parser.add_argument("--skip-npm-install", action="store_true", help="Use web/node_modules as it is.")
    args = parser.parse_args()

    web = HERE / "web"
    if not args.skip_npm_install:
        run([NPM, "ci"], web)
    shutil.rmtree(web / "static" / "build", ignore_errors=True)
    run([NPM, "run", "build:release"], web)
    if not (web / "static" / "build" / "main.js").is_file():
        sys.exit("The page build wrote no web/static/build/main.js.")

    version = project_version()
    run(["uv", "build", "--wheel", "--out-dir", "dist"], HERE)
    wheel = HERE / "dist" / f"swissarmygrid-{version}-py3-none-any.whl"
    if not wheel.is_file():
        sys.exit(f"uv build wrote no {wheel.name}.")
    print(f"\nBuilt {wheel.relative_to(HERE)}")
    # The installers, with this version written in: they go in the release beside the wheel.
    for name in ("install.sh", "install.ps1"):
        text = (HERE / "installer" / name).read_text(encoding="utf-8").replace("__VERSION__", version)
        (HERE / "dist" / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"Wrote dist/{name}")

    if args.vsix:
        extension = HERE / "vscode-extension"
        if not args.skip_npm_install:
            run([NPM, "ci"], extension)
        run([NPM, "run", "package"], extension)

    print("\nInstall the server once per machine, and the client wherever you push from:")
    print(f'  uv tool install --force "{wheel}[server]"')
    print(f'  uv pip install "{wheel}"')


if __name__ == "__main__":
    main()
