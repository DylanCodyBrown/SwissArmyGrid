"""Check an installed SwissArmyGrid end to end, the way a person would use it (the release workflow runs this).

Run it in a client-only environment (the plain wheel plus pandas and pyarrow), with the server installed as a uv
tool so `swissarmygrid` is on PATH:

    uv run --no-project --with <wheel> --with pandas --with pyarrow python tools/smoke_install.py

It starts the server with `swissarmygrid start`, pushes the made-up store (examples/sample_store.py), checks that
all nine tables arrived and that the page's script is served, then stops the server. State goes in a temporary
folder, so nothing of the machine's own SwissArmyGrid is touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from examples.sample_store import make_store


def get(info: dict[str, Any], route: str) -> Any:
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}{route}", headers={"Authorization": f"Bearer {info['token']}"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (fixed local URL)
        return json.load(response)


def main() -> None:
    command = shutil.which("swissarmygrid")
    if command is None:
        sys.exit("swissarmygrid is not on PATH: install the server first (uv tool install <wheel>[server]).")
    state = Path(tempfile.mkdtemp(prefix="sag-smoke-"))
    os.environ.update(
        SWISSARMYGRID_INFO=str(state / "server.json"),
        SWISSARMYGRID_STATE=str(state),
        SWISSARMYGRID_OPEN="0",
    )
    log = (state / "server.log").open("w")
    server = subprocess.Popen([command, "start"], stdout=log, stderr=subprocess.STDOUT)
    try:
        info_path = state / "server.json"
        for _ in range(120):
            if info_path.is_file():
                break
            time.sleep(0.5)
        else:
            sys.exit("The server did not start:\n" + (state / "server.log").read_text())
        info = json.loads(info_path.read_text())
        print(f"Server {info['version']} on port {info['port']}")

        from swissarmygrid.client import push

        tables = make_store()
        for name, frame in tables.items():
            push(frame, name=name)
        datasets = {d["name"] for d in get(info, "/datasets")["datasets"]}
        missing = set(tables) - datasets
        if missing:
            sys.exit(f"These tables did not arrive: {sorted(missing)}")
        print(f"All {len(tables)} tables arrived: {', '.join(sorted(datasets))}")

        with urllib.request.urlopen(
            f"http://127.0.0.1:{info['port']}/static/build/main.js", timeout=30
        ) as page:
            if page.status != 200 or len(page.read()) < 10_000:
                sys.exit("The page's script is missing from the install.")
        print("The page's script is served.")
    finally:
        subprocess.run([command, "stop"], check=False)
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
    print("Smoke test passed.")


if __name__ == "__main__":
    main()
