"""The client, imported from notebooks and scripts: `import swissarmygrid as sag`.

`watch()` (started by itself in a notebook) lists your tables in the grid's Open list. `push(orders)`
sends one by hand and names the dataset (and the tab) after the variable, "orders".
"""

from swissarmygrid.client.push_client import DatasetTooLargeError, ServerNotRunningError, push
from swissarmygrid.client.watcher import start as watch
from swissarmygrid.client.watcher import stop as unwatch

__all__ = ["DatasetTooLargeError", "ServerNotRunningError", "push", "unwatch", "watch"]
