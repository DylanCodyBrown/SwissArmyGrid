"""`sag.load`, `sag.blade` and `sag.blades`: catalogs and blades from any Python (plan decisions 14-17).

    sag.load(columns_frame, name="Warehouse", dialect="sqlserver")   # a searchable catalog
    conn = sag.blade("Warehouse")                                     # a saved blade, run here
    sag.blade("Azure vault")        # a vault blade: sets its environment variables, gives their names
    sag.blade("Warehouse login")    # a secret blade: asks for any value this Python does not have yet

A blade's code runs in this Python, so its driver must be installed here; a missing one is named with
the command that installs it. Secret values never print: a vault or secret blade hands back the
names it set, not the values. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import getpass
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from swissarmygrid.blade_core.packages import missing_module_hint
from swissarmygrid.blade_core.run import BladeRunError, run_code, run_vault
from swissarmygrid.client.push_client import (
    _arrow_bytes,
    _prepared_frame,
    _records_json,
    _source_kind,
    _to_arrow,
    ensure_server,
)

_TIMEOUT_SECONDS: float = 300.0


def _call(method: str, path: str, body: bytes | None = None, content_type: str = "application/json") -> Any:
    info = ensure_server()
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}{path}",
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {info['token']}", "Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310 (fixed http://127.0.0.1 URL)
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        message = error.read().decode("utf-8", "replace")
        if error.code == 404:
            raise LookupError(message) from None
        raise RuntimeError(f"SwissArmyGrid refused ({error.code}): {message}") from None


def _quote(name: str) -> str:
    return urllib.parse.quote(name, safe="")


def load(catalog: object, name: str, *, dialect: str | None = None) -> str:
    """Load a source database's names and types as a searchable catalog. Returns the explorer's address.

    `catalog` is any table with, per row, one column of the source: its table and column, and usually
    its schema and type. An INFORMATION_SCHEMA.COLUMNS result works as it is, as do Oracle's
    ALL_TAB_COLUMNS and plain schema/table/column/type frames. `dialect` (sqlserver, postgres, mysql,
    sqlite, duckdb, snowflake, oracle, trino) lets the agent quote SQL for the source.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Name the catalog: sag.load(frame, name='Warehouse').")
    try:
        table = _to_arrow(catalog)
    except ImportError:
        table = None
    if table is not None:
        body, content_type = _arrow_bytes(table), "application/vnd.apache.arrow.stream"
    elif _source_kind(catalog) == "records":
        body, content_type = _records_json(catalog), "application/json"  # type: ignore[arg-type]
    elif _source_kind(catalog) == "pandas":
        frame: Any = _prepared_frame(catalog)
        body, content_type = frame.to_json(orient="split", default_handler=str).encode(), "application/json"
    else:
        raise ImportError(
            "Install pyarrow to load anything other than a pandas DataFrame or a list of dicts."
        )
    query = f"?dialect={_quote(dialect)}" if dialect else ""
    answer = _call("PUT", f"/catalogs/{_quote(name)}{query}", body, content_type)
    return str(answer.get("url") or "")


def blades(tag: str | None = None, search: str | None = None) -> list[str]:
    """The names of the blades (saved ones and those in your blade folders), favourites first.

    `tag` keeps those with that tag (ignoring case); `search` keeps those whose name, description or a
    tag contains the text: `sag.blades(search="orders-function-app")`.
    """
    found = _call("GET", "/blades")["blades"]
    if tag:
        found = [b for b in found if tag.casefold() in (t.casefold() for t in b.get("tags", []))]
    if search:
        text = search.casefold()
        found = [
            b
            for b in found
            if text in str(b["name"]).casefold()
            or text in str(b.get("description", "")).casefold()
            or any(text in t.casefold() for t in b.get("tags", []))
        ]
    found.sort(key=lambda b: (not b.get("pinned"), str(b["name"]).casefold()))
    return [str(b["name"]) for b in found]


def _ask(names: list[str], source: str) -> None:
    """Ask for each value this Python does not have yet, hidden as it is typed; set it here only."""
    for env in names:
        if os.environ.get(env):
            continue
        value = getpass.getpass(f"{env} ({source}, kept for this session only): ")
        if not value:
            raise ValueError(f"{env} was left empty.")
        os.environ[env] = value


def blade(name: str) -> Any:
    """Run a saved blade in this Python and return what it makes (its variable).

    A vault blade sets its environment variables and returns their names; a secret blade asks for any
    of its values this Python does not have yet (none on a second use) and returns their names. Other
    blades first run the vault blades and ask for the secret-blade values their code needs.
    """
    spec = _call("GET", f"/blades/{_quote(name)}/python")
    label = str(spec["name"])
    if spec["kind"] == "secret":
        _ask(list(spec["env_names"]), label)
        return list(spec["env_names"])
    try:
        for vault in spec.get("vaults", []):
            run_vault(vault["code"], vault["variable"], vault["mapping"], vault["secrets"], vault["label"])
        for env, source in spec.get("typed", {}).items():
            _ask([env], source)
        if spec["kind"] == "vault":
            return sorted(
                run_vault(spec["code"], spec["variable"], spec["vault_mapping"], spec["secrets"], label)
            )
        value, _ = run_code(spec["code"], spec["variable"], spec["secrets"], label)
    except BladeRunError as error:
        if error.missing_module:
            raise ImportError(missing_module_hint(error.missing_module)) from None
        raise
    return value
