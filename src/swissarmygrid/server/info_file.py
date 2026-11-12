"""The connection file clients read to find the running server (plan section 4).

On start the server writes ~/.swissarmygrid/server.json with its port, its token, the dataset
size limit and its process id; on clean shutdown it deletes it. The push client
(swissarmygrid.client.push_client) reads the same path, including the SWISSARMYGRID_INFO
override, so the two must stay in step. The process id lets anyone tell a live server from a
file left behind by a crash (server/lifecycle.py).

The token in this file is the only credential the server accepts, so the file is created
owner-only. On Windows the mode bits do little, but the user's profile folder is already
readable only by that user (plus administrators and SYSTEM).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def info_file_path() -> Path:
    """Where the connection file lives. SWISSARMYGRID_INFO overrides it (tests use this)."""
    override: str | None = os.environ.get("SWISSARMYGRID_INFO")
    return Path(override) if override else Path.home() / ".swissarmygrid" / "server.json"


def write_info_file(path: Path, info: dict[str, Any]) -> None:
    """Write the connection file atomically, so a client never reads a half-written one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp: Path = path.with_suffix(".tmp")
    descriptor: int = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(info))
    temp.replace(path)


def remove_info_file(path: Path, token: str | None = None) -> None:
    """Delete the connection file, but only our own: when `token` is given and the file now
    holds another server's token, leave it alone."""
    if token is not None:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = None
        if isinstance(current, dict) and current.get("token") != token:
            return
    path.unlink(missing_ok=True)
