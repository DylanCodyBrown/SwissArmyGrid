"""`sag.paste()`: the code the grid page sent ("To Python") into this Python.

    sag.paste()      # in Jupyter: this cell becomes the code (run it); in IPython: it waits at the prompt

In plain Python it prints the code and returns it. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys
from typing import Any

from swissarmygrid.client.blades_client import _call


def paste() -> str | None:
    """The newest code sent from the grid page, put where you type next. None when nothing waits."""
    taken: Any = _call("POST", "/python/take")
    if not taken:
        print("Nothing waiting: press To Python on an analysis or a grid's steps first.")
        return None
    text = str(taken["code"])
    shell = _ipython()
    if shell is not None:
        # In Jupyter this replaces the cell that ran sag.paste(); in the terminal it fills the next prompt.
        shell.set_next_input(text, replace=True)
    else:
        print(text)
    return text


def _ipython() -> Any:
    module = sys.modules.get("IPython")
    get = getattr(module, "get_ipython", None) if module is not None else None
    return get() if callable(get) else None
