"""A folder's contents on this computer, for the page's file chooser (the Connections form's file and
folder fields, the Workbench's Open and Save). Names, sizes and dates only: nothing is read or written."""

from __future__ import annotations

import os
import string
import sys
from pathlib import Path
from typing import Any

MAX_ENTRIES: int = 2_000


def roots() -> list[str]:
    """Where browsing can start: the home folder, and each drive on Windows (/ elsewhere)."""
    if sys.platform == "win32":
        drives = [f"{letter}:\\" for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()]
    else:
        drives = ["/"]
    return [str(Path.home()), *drives]


def listing(where: str | None, suffixes: set[str]) -> dict[str, Any]:
    """The folder's subfolders, then its files (only those ending in `suffixes`, when given). Hidden
    names are left out. A file's path gives its folder."""
    try:
        path = Path(where).expanduser() if where else Path.home()
        if path.is_file():
            path = path.parent
        path = path.resolve()
        found = path.is_dir()
    except OSError as error:  # a name too long, a device not ready: not a folder to open
        raise FileNotFoundError(
            f"{where} is not a folder this computer can open ({error.strerror})."
        ) from None
    if not found:
        raise FileNotFoundError(f"{path} is not a folder this computer can open.")
    folders: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name.casefold())
    except PermissionError:
        raise PermissionError(f"This computer does not let you open {path}.") from None
    for entry in entries:
        if entry.name.startswith(".") or len(folders) + len(files) >= MAX_ENTRIES:
            continue
        try:
            if entry.is_dir():
                folders.append({"name": entry.name, "path": entry.path})
            elif entry.is_file() and (not suffixes or Path(entry.name).suffix.lower() in suffixes):
                stat = entry.stat()
                files.append(
                    {"name": entry.name, "path": entry.path, "size": stat.st_size, "modified": stat.st_mtime}
                )
        except OSError:
            continue
    return {
        "path": str(path),
        "parent": str(path.parent) if path.parent != path else None,
        "folders": folders,
        "files": files,
        "roots": roots(),
        "home": str(Path.home()),
    }
