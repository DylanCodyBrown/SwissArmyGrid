"""Check an installed SwissArmyGrid end to end, the way a person would use it (the release workflow runs this).

Run it in a client-only environment (the plain wheel plus pandas and pyarrow), with the server installed as a uv
tool so `swissarmygrid` is on PATH:

    uv run --no-project --with <wheel> --with pandas --with pyarrow python tools/smoke_install.py

It starts the server with `swissarmygrid start`, pushes the made-up store (examples/sample_store.py), checks that
all nine tables arrived, that the page's script is served and that blades and catalogs work, then stops the
server. State goes in a temporary
folder, so nothing of the machine's own SwissArmyGrid is touched.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
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


def post(info: dict[str, Any], route: str, body: Any) -> Any:
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}{route}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 (fixed local URL)
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
        print(f"Server {info['version']} on port {info['port']} from {info.get('executable')}")

        from swissarmygrid.client import push

        tables = make_store()
        for name, frame in tables.items():
            push(frame, name=name)
        # A time-zone-aware column: DuckDB needs pytz in the server's install to hand it back.
        import pandas as pd

        stamps = pd.DataFrame({"at": pd.to_datetime(["2024-01-01 09:30"]).tz_localize("UTC"), "n": [1]})
        grid = push(stamps, name="time_zones").split("/g/")[1].split("#")[0]
        try:
            rows = get(info, f"/grids/{grid}/rows?start=0&end=10")["rows"]
        except urllib.error.HTTPError as error:
            sys.exit(f"A time-zone column failed: {error.read().decode()[:300]}")
        if len(rows) != 1:
            sys.exit(f"A time-zone column did not come back: {rows}")
        print(f"A time-zone column comes back: {rows[0]}")
        tables["time_zones"] = stamps

        # Copy as code: the server runs exactly the copied Python (`.df()` included) before offering it.
        orders_grid = push(tables["orders"], name="orders").split("/g/")[1].split("#")[0]
        post(
            info,
            f"/grids/{orders_grid}/steps",
            {"steps": [{"kind": "filter", "params": {"column": "status_code", "op": "gt", "value": 2}}]},
        )
        code = get(info, f"/grids/{orders_grid}/code")
        if not code.get("verified") or code.get("note"):
            sys.exit(f"The copied code was not fully verified: {code.get('note') or code}")
        print("Copied code is verified as pasted.")
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

        # Blades: a SQLite blade from its starter, refreshed in the server's own Python (a child
        # process), run here with sag.blade, and a catalog loaded with sag.load.
        import sqlite3

        import swissarmygrid as sag

        bakery = state / "bakery.sqlite"
        with sqlite3.connect(bakery) as setup:
            setup.execute("CREATE TABLE loaves (loaf_id INTEGER, crust TEXT)")
        starter = next(t for t in get(info, "/blades/templates")["templates"] if t["id"] == "sqlite")
        code = re.sub(r'r"[^"]*"', lambda _: repr(str(bakery)), starter["code"], count=1)
        blade = {
            "name": "Smoke bakery",
            "kind": "sql",
            "dialect": "sqlite",
            "code": code,
            "catalog": starter["catalog"],
        }
        post(info, "/blades", blade)
        try:
            refreshed = post(info, "/blades/Smoke%20bakery/refresh", {"env": {}})
        except urllib.error.HTTPError as error:
            sys.exit(f"A blade's Refresh failed: {error.read().decode()[:500]}")
        if refreshed["catalog"]["columns"] != 2:
            sys.exit(f"The blade's catalog came back wrong: {refreshed}")
        conn = sag.blade("Smoke bakery")
        if conn.execute("SELECT count(*) FROM loaves").fetchall() != [(0,)]:
            sys.exit("sag.blade did not give a working connection.")
        sag.load([{"table_name": "loaves", "column_name": "crust", "data_type": "TEXT"}], name="Smoke load")
        found = get(info, "/catalogs/Smoke%20load/search?q=crust")["columns"]
        if [c["column"] for c in found] != ["crust"]:
            sys.exit(f"sag.load's catalog cannot be searched: {found}")
        with urllib.request.urlopen(
            f"http://127.0.0.1:{info['port']}/static/build/catalog.js", timeout=30
        ) as page:
            if page.status != 200 or len(page.read()) < 5_000:
                sys.exit("The blades page's script is missing from the install.")
        print("Blades refresh, run with sag.blade, and sag.load fills a catalog.")

        # Code handed to Python: queued by the page, taken by sag.paste().
        sent = urllib.request.Request(
            f"http://127.0.0.1:{info['port']}/python/send",
            data=json.dumps({"code": "print('smoke')", "label": "smoke"}).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(sent, timeout=30) as response:  # noqa: S310 (fixed local URL)
            if response.status != 200:
                sys.exit("To Python did not queue the code.")
        if sag.paste() != "print('smoke')":
            sys.exit("sag.paste() did not take the queued code.")
        print("To Python queues code and sag.paste() takes it.")
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
