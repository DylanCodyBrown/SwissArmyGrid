"""Live check: a notebook opens, and the grid offers the ERD models FI-Atlas has for it.

This is the one test that uses nothing made up. It needs a Python with BOTH swissarmygrid[server] and fi-clients (the
way a user's machine is after the setup skill) and an Azure sign-in (`az login`) that can write to FI-Atlas. It makes
one throwaway model called zz-live-<time>, associates it with a throwaway notebook, runs a real grid server and a real
notebook watcher, and checks the whole path:

    FI-Atlas  <-- fi_clients.sag_models  <-- the watcher (in the notebook's Python)  --> the server --> apply

then deletes everything it made. It is not part of `pytest` because it writes to the shared database.

    uv run --isolated --with-editable . --with-editable ../fi-clients python tools/live_atlas_check.py

Exit code 0 when every step passed.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import uvicorn

from swissarmygrid.client.watcher import Watcher
from swissarmygrid.server.app import create_app
from swissarmygrid.settings import Settings

STAMP = int(time.time())
NAME = f"zz-live-{STAMP}"
FOLDER = f"zz_live_{STAMP}"
NOTEBOOK_PATH = (
    rf"C:\somewhere\{FOLDER.upper()}\{FOLDER.upper()}.ipynb"  # any machine's path; the key drops the rest
)
KEY = f"{FOLDER}/{FOLDER}.ipynb"

failures: list[str] = []


def check(label: str, ok: bool, detail: Any = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"   ({detail})" if detail != "" and not ok else ""))
    if not ok:
        failures.append(label)


def until(condition: Any, seconds: float = 40.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if condition():
                return True
        except Exception:  # the server may not answer yet
            pass
        time.sleep(0.1)
    return False


def main() -> int:
    from fi_clients import sag_models

    model = {
        "name": NAME,
        "description": "live check: orders and lines",
        "tables": {"orders": {"primary_key": ["id"]}, "lines": {"primary_key": ["id"]}},
        "relationships": [
            {
                "from": "lines",
                "from_columns": ["order_id"],
                "to": "orders",
                "to_columns": ["id"],
                "kind": "many-to-one",
            }
        ],
    }
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    token = "live-check-token"  # noqa: S105 (a throwaway token for a server on 127.0.0.1)
    work = Path(tempfile.mkdtemp(prefix="sag-live-"))
    info = work / "server.json"
    info.write_text(json.dumps({"port": port, "token": token}))
    os.environ["SWISSARMYGRID_INFO"] = str(info)
    os.environ.pop("SWISSARMYGRID_MODELS", None)

    def call(method: str, path: str, body: Any = None) -> Any:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return json.loads(response.read() or b"null")

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(port, token, Settings(state_dir=work / "state", export_dir=work / "x")),
            port=port,
            log_level="error",
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    watcher = None
    try:
        until(lambda: server.started, 20)
        check("a grid server is running", server.started)

        saved = sag_models.save_model(model, via="live-check")
        check("the model is saved to FI-Atlas", saved == {"name": NAME, "created": True}, saved)
        linked = sag_models.associate(NOTEBOOK_PATH, [NAME], via="live-check")
        check("it is associated with the notebook", linked == [NAME], linked)

        # A real watcher, in this Python, for a "notebook" whose file is NOTEBOOK_PATH. Its lookup is the real one:
        # it imports fi_clients, signs in as you and reads FI-Atlas.
        watcher = Watcher(
            lambda: {"__vsc_ipynb_file__": NOTEBOOK_PATH},
            scan_seconds=0.1,
            heartbeat_seconds=1,
            poll_seconds=1,
        )
        watcher.start()
        got = until(lambda: call("GET", "/processes")["processes"][0]["models"] is not None, 90)
        check("the notebook's models reach the grid", got)
        offer = call("GET", "/processes")["processes"][0]
        shown = offer["models"] or {}
        check("it names the notebook by folder and file", shown.get("notebook") == KEY, shown.get("notebook"))
        check("no lookup error", shown.get("error") is None, shown.get("error"))
        check(
            "exactly the associated model is offered", [m["name"] for m in shown.get("models", [])] == [NAME]
        )
        check("and the page has not been answered yet", shown.get("handled") is False)

        applied = call("POST", f"/processes/{offer['id']}/models/apply", {"names": [NAME]})
        check("applying merges it", applied.get("applied") == [NAME], applied)
        current = call("GET", "/relationships")
        check(
            "the grid's relationship model now has its tables",
            set(current["tables"]) == {"orders", "lines"},
            current["tables"],
        )
        check(
            "the offer is marked answered",
            call("GET", "/processes")["processes"][0]["models"]["handled"] is True,
        )

        # The on-request path, from the same Python.
        import swissarmygrid as sag

        call("POST", "/relationships", {"model": {"name": "current"}, "replace": True})
        check("sag.import_erds() by name merges a model", sag.import_erds(NAME) == [NAME])
        check(
            "sag.import_erds() with no names uses the notebook's own",
            sag.import_erds(notebook=NOTEBOOK_PATH) == [NAME],
        )

        # The grid's FI-Atlas picker: the throwaway model, ranked for the notebook that is running.
        atlas = call("GET", "/relationships/atlas")
        check("the FI-Atlas picker is available", atlas.get("available") is True, atlas.get("error"))
        mine = [m for m in atlas.get("models", []) if m["name"] == NAME]
        check(
            "it lists the model, marked for the open notebook",
            bool(mine) and mine[0]["for_open"] == [offer["label"]],
            mine,
        )
        check(
            "and ranks it first", atlas["models"][0]["name"] == NAME, [m["name"] for m in atlas["models"][:3]]
        )
        call("POST", "/relationships", {"model": {"name": "current"}, "replace": True})
        applied_from_atlas = call("POST", f"/relationships/atlas/{NAME}")
        check(
            "applying it from the picker merges it", set(applied_from_atlas["tables"]) == {"orders", "lines"}
        )

        # The agent's tools (the models_ commands), against the same real tables.
        from swissarmygrid.cli.commands import execute

        listed = json.loads(execute("models_list", {"notebook": NOTEBOOK_PATH}))["models"]
        check("models_list(notebook) finds it", [m["name"] for m in listed] == [NAME], listed)
        got = json.loads(execute("models_get", {"name": NAME}))["model"]
        check("models_get returns the JSON", got["name"] == NAME and "orders" in got["tables"])
        got["description"] = "live check: edited by the agent tool"
        check(
            "models_save updates it", json.loads(execute("models_save", {"model": got}))["created"] is False
        )
        check("and the change is stored", sag_models.get_model(NAME)["description"].endswith("agent tool"))
        check(
            "models_dissociate unlinks it",
            json.loads(execute("models_dissociate", {"notebook": NOTEBOOK_PATH, "names": [NAME]}))[
                "notebook_models"
            ]
            == [],
        )
        check(
            "models_associate links it again",
            json.loads(execute("models_associate", {"notebook": NOTEBOOK_PATH, "names": [NAME]}))[
                "notebook_models"
            ]
            == [NAME],
        )
    finally:
        if watcher is not None:
            watcher.stop()
        server.should_exit = True
        deleted = sag_models.delete_model(NAME)
        left = sag_models.associations(KEY)
        check("cleanup: the throwaway model is deleted and its link with it", deleted and left == [], left)
    print(f"\n{'ALL PASSED' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
