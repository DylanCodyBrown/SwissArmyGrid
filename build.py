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
from pathlib import Path

HERE = Path(__file__).resolve().parent
NPM = "npm.cmd" if sys.platform == "win32" else "npm"


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

    run(["uv", "build", "--wheel", "--out-dir", "dist"], HERE)
    wheels = sorted((HERE / "dist").glob("swissarmygrid-*.whl"), key=lambda p: p.stat().st_mtime)
    wheel = wheels[-1]
    print(f"\nBuilt {wheel.relative_to(HERE)}")

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
