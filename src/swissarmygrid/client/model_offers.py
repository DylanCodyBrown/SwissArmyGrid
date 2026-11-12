"""The ERD models a notebook goes with: look them up in FI-Atlas through fi-clients, and import them on
request.

A relationship model (an ERD) can be associated with a notebook in FI-Atlas (see fi_clients.sag_models).
When the
notebook is opened and its watcher starts, the watcher calls `lookup` here once, in a background thread, and
hands the
result to the server, which asks the user which of those models to apply. `import_erds` is the same thing on
demand,
from a cell.

Why the notebook's own Python does this and not the grid server: the server is a separate install with its own
environment and never has fi-clients; the notebook's Python is the one the setup installs fi-clients into,
and it is
the one signed in as the user. So the lookup needs nothing new on the server, and a machine without
fi-clients simply
gets a clear message instead of a failure.

Standard library only until fi-clients is actually imported, and that import is guarded: nothing here raises
into the
notebook.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from swissarmygrid.client import push_client

NOT_INSTALLED = (
    "fi-clients is not installed in this Python, so this notebook's ERD models can't be looked up. "
    "Install it with /finance-toolbox:swissarmygrid-setup."
)
MAX_ERROR_CHARS = 300


class ModelsUnavailable(RuntimeError):
    """The models could not be fetched; the message says why (fi-clients missing, not signed in, no table
    yet)."""


def enabled() -> bool:
    """SWISSARMYGRID_MODELS=0 turns the lookup off for everyone on this machine (it costs one FI-Atlas query a
    session)."""
    return os.environ.get("SWISSARMYGRID_MODELS", "") != "0"


def notebook_path(namespace: dict[str, Any]) -> str | None:
    """The file this session runs: the notebook VS Code sets in a kernel, else the script. None for a REPL."""
    for key in ("__vsc_ipynb_file__", "__file__"):
        value = namespace.get(key)
        if isinstance(value, str) and value.strip() and not value.startswith("<"):
            return value
    return None


def notebook_key(path: str) -> str:
    """'folder/file.ipynb', lower case, forward slashes: the same on every machine. Mirrors
    fi_clients.sag_models.notebook_key; both tests check the same examples so they cannot drift apart."""
    parts = [p for p in path.replace("\\", "/").split("/") if p and p != "."]
    return "/".join(parts[-2:]).lower()[:400]


def _message(error: BaseException) -> str:
    text = getattr(error, "message", None) or str(error) or type(error).__name__
    return text[:MAX_ERROR_CHARS]


def _sag_models() -> Any:
    """fi-clients' module, or ModelsUnavailable saying what to do. Importing fi_clients reads the Key Vault
    once, so
    this can take a few seconds the first time; callers run it off the notebook's thread."""
    try:
        from fi_clients import sag_models
    except ImportError:
        raise ModelsUnavailable(NOT_INSTALLED) from None
    except (
        Exception
    ) as error:  # the import itself signs in; a failed sign-in is reported, not raised into the cell
        raise ModelsUnavailable(f"fi-clients could not start: {_message(error)}") from error
    return sag_models


def lookup(path: str) -> dict[str, Any]:
    """What the server is told about a notebook: {"notebook": key, "models": [...], "error": str | None}.
    Never
    raises: any failure becomes the `error`, and the notebook just has no models to offer."""
    out: dict[str, Any] = {"notebook": notebook_key(path), "models": [], "error": None}
    try:
        out["models"] = _sag_models().models_for_notebook(path)
    except ModelsUnavailable as error:
        out["error"] = str(error)[:MAX_ERROR_CHARS]
    except Exception as error:
        out["error"] = f"Could not read this notebook's models from FI-Atlas: {_message(error)}"[
            :MAX_ERROR_CHARS
        ]
    return out


def _post(path: str, body: Any) -> Any:
    info = push_client.ensure_server()
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (fixed http://127.0.0.1 URL)
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        raise ModelsUnavailable(
            f"The grid refused the model: {error.read().decode('utf-8', 'replace')[:200]}"
        ) from error


def import_erds(*names: str, notebook: str | None = None) -> list[str]:
    """Merge ERD models from FI-Atlas into the grid's current model, now.

        import swissarmygrid as sag
        sag.import_erds()                 # the models associated with this notebook
        sag.import_erds("CMM", "WMM")     # these, by name

    Returns the names applied. Raises ModelsUnavailable, with a plain message, if fi-clients is not
    installed, the
    sign-in fails, a name does not exist, or the grid is not running."""
    sag_models = _sag_models()
    try:
        if names:
            models = [sag_models.get_model(name) for name in names]
        else:
            path = notebook or notebook_path(
                vars(sys.modules.get("__main__")) if "__main__" in sys.modules else {}
            )
            if path is None:
                raise ModelsUnavailable(
                    "This session has no notebook file. Pass notebook='folder/file.ipynb' or model names."
                )
            models = sag_models.models_for_notebook(path)
    except ModelsUnavailable:
        raise
    except Exception as error:
        raise ModelsUnavailable(_message(error)) from error
    try:
        for model in models:
            _post("/relationships", {"model": model, "replace": False})
    except push_client.ServerNotRunningError as error:
        raise ModelsUnavailable(str(error)) from error
    return [str(model.get("name")) for model in models]
