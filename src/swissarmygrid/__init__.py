"""SwissArmyGrid: a local server that shows tabular data in live grids inside VS Code.

From a notebook, nothing to write beyond the import:

    import swissarmygrid       # starts the watcher when IPython is running
    SOD = trino.execute("SELECT ...")

The grid's Open list shows SOD. Click it and it opens; run the cell again and the open tab refreshes.
In a plain script call `sag.watch()` once. `push` still works, for what the watcher cannot see
(a script about to exit, a table made inside a function):

    sag.push(orders)

The client needs only the standard library (pyarrow, when present, keeps column types), so it goes into
any project's environment. It only connects: the grid server (installed once per machine with the
[server] extra, a uv tool) is started by the `swissarmygrid` command and shows the grids.
The design is Plans/swissarmygrid-build-plan.md and Plans/autodetect-and-reload.plan.md.

The client is loaded on first use (a module-level `__getattr__`, PEP 562), so `import swissarmygrid`
costs nothing until `push` or `watch` is touched. That is what keeps the `sag` agent command, which
lives in this package and never pushes, fast to start.
"""

from __future__ import annotations

import os
import sys
from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from swissarmygrid.client.blades_client import blade, blades, load
    from swissarmygrid.client.blades_client import blade as connection
    from swissarmygrid.client.completion import complete
    from swissarmygrid.client.paste_client import paste
    from swissarmygrid.client.push_client import DatasetTooLargeError, ServerNotRunningError, push
    from swissarmygrid.client.sandbox_client import sandbox
    from swissarmygrid.client.sql_cell import sql
    from swissarmygrid.client.watcher import start as watch
    from swissarmygrid.client.watcher import stop as unwatch

__version__: str = "1.7.0b1"
__all__ = [
    "DatasetTooLargeError",
    "ServerNotRunningError",
    "__version__",
    "blade",
    "blades",
    "complete",
    "connection",
    "load",
    "paste",
    "push",
    "sandbox",
    "sql",
    "unwatch",
    "watch",
]

# Public name -> (module, attribute), imported when first asked for.
_LAZY: dict[str, tuple[str, str]] = {
    "push": ("swissarmygrid.client.push_client", "push"),
    "load": ("swissarmygrid.client.blades_client", "load"),
    "blade": ("swissarmygrid.client.blades_client", "blade"),
    "blades": ("swissarmygrid.client.blades_client", "blades"),
    # A connection (a database or file, added in the app's Connections) is run the way a blade is.
    "connection": ("swissarmygrid.client.blades_client", "blade"),
    "sql": ("swissarmygrid.client.sql_cell", "sql"),
    "sandbox": ("swissarmygrid.client.sandbox_client", "sandbox"),
    "paste": ("swissarmygrid.client.paste_client", "paste"),
    "complete": ("swissarmygrid.client.completion", "complete"),
    "DatasetTooLargeError": ("swissarmygrid.client.push_client", "DatasetTooLargeError"),
    "ServerNotRunningError": ("swissarmygrid.client.push_client", "ServerNotRunningError"),
    "watch": ("swissarmygrid.client.watcher", "start"),
    "unwatch": ("swissarmygrid.client.watcher", "stop"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'swissarmygrid' has no attribute {name!r}")
    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value  # later lookups skip this function
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})


# Start the watcher on import in a notebook or the interactive window, where importing the package is
# already the decision to use the grid. The watcher's own autostart makes the final call (and honors
# SWISSARMYGRID_WATCH=0); this only avoids loading it when it cannot start: IPython has to be
# loaded already for it to be running, so a plain script, the server and the MCP never pay for the import.
if os.environ.get("SWISSARMYGRID_WATCH", "") == "1" or "IPython" in sys.modules:
    from swissarmygrid.client.watcher import autostart

    autostart()
# Catalog completion inside SQL strings, and SQL cells on a connection: only in Jupyter and IPython.
if "IPython" in sys.modules:
    from swissarmygrid.client.completion import autostart as complete_on_tab
    from swissarmygrid.client.sql_cell import autostart as sql_cells

    complete_on_tab()
    # %%sql <connection> cells (and %%sag, %sql_connect, %sql_connections): SWISSARMYGRID_SQL_CELL=0 is off.
    sql_cells()
