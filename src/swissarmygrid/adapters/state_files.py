"""Everything SwissArmyGrid keeps in its own state folder (~/.swissarmygrid), and nothing else is
written: the audit log, saved recipes, the connection file and the model library's files.

  the audit log        one JSON line per action, never a cell value
  recipes              a grid's steps and settings, saved on the user's click, with no rows
  the connection file  where the running server is, and its token (owner-only)
  the model library    relationship models: the shipped ones, the user's, and SWISSARMYGRID_MODELS

(Blades and their folders are adapters/blades.py; exports are adapters/export.py.)
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from swissarmygrid.domain.datasets import validate_name
from swissarmygrid.domain.relationships import Model, parse_model
from swissarmygrid.domain.steps import Step, ViewSettings, steps_from_json

# ===== the audit log =====
# The audit log: one JSON line per action, never a cell value (plan section 6, rule 9).
#
# Each entry records when, who (`user` or `agent`), what, on which dataset, the query text if
# any, and how many rows went out. Exports and agent reads are the ways client data leaves the
# system, so those are the entries that matter most. Like a structured ILogger sink writing to a
# local file; nothing is sent anywhere.


class AuditLog:
    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        actor: str,
        action: str,
        dataset: str | None = None,
        query: str | None = None,
        rows: int | None = None,
        **details: Any,
    ) -> None:
        entry: dict[str, Any] = {
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "actor": actor,
            "action": action,
            "dataset": dataset,
        }
        if query is not None:
            entry["query"] = query
        if rows is not None:
            entry["rows"] = rows
        entry.update(details)
        if self._path is None:
            return
        line = json.dumps(entry, default=str)
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def tail(self, limit: int = 200, actor: str | None = None) -> list[dict[str, Any]]:
        """The last `limit` entries, or the last `limit` of one actor's."""
        if self._path is None or not self._path.exists():
            return []
        newest_first: list[dict[str, Any]] = []
        for line in reversed(self._path.read_text(encoding="utf-8").splitlines()):
            if len(newest_first) >= limit:
                break
            if line.strip():
                entry = json.loads(line)
                if actor is None or entry.get("actor") == actor:
                    newest_first.append(entry)
        return newest_first[::-1]


# ===== recipes =====
# Recipes: a grid's steps and settings saved to a small file, with no rows (plan section 7).
#
# Saving is an explicit user action ("Save recipe"), because step parameters hold the values the
# user typed (a filter on 'Acme', say), and the default posture writes nothing to disk. When a
# dataset is pushed and gets its first grid, a saved recipe for that dataset name is applied
# automatically; if a column it needs is missing, compilation marks the first broken step and
# the grid stops there (shown in the steps panel).


def _path(folder: Path, dataset: str) -> Path:
    return folder / f"{validate_name(dataset)}.json"


def save_recipe(folder: Path, dataset: str, steps: list[Step], settings: ViewSettings) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = _path(folder, dataset)
    body: dict[str, Any] = {
        "dataset": dataset,
        "settings": settings.to_json(),
        "steps": [s.to_json() for s in steps if not s.params.get("header")],
    }
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return path


def load_recipe(folder: Path, dataset: str) -> tuple[list[Step], ViewSettings] | None:
    path = _path(folder, dataset)
    if not path.exists():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(body, dict):  # edited by hand into something else: ignore it
            return None
        return steps_from_json(body.get("steps", [])), ViewSettings.from_json(body.get("settings"))
    except (ValueError, OSError):
        return None


def delete_recipe(folder: Path, dataset: str) -> bool:
    path = _path(folder, dataset)
    if path.exists():
        path.unlink()
        return True
    return False


class RecipeFiles:
    """The recipes in one folder: what the service layer's `Recipes` port is here."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def save(self, dataset: str, steps: list[Step], settings: ViewSettings) -> str:
        return str(save_recipe(self.folder, dataset, steps, settings))

    def load(self, dataset: str) -> tuple[list[Step], ViewSettings] | None:
        return load_recipe(self.folder, dataset)

    def delete(self, dataset: str) -> bool:
        return delete_recipe(self.folder, dataset)


# ===== the connection file =====
# The connection file clients read to find the running server (plan section 4).
#
# On start the server writes ~/.swissarmygrid/server.json with its port, its token, the dataset
# size limit and its process id; on clean shutdown it deletes it. The push client
# (swissarmygrid.client.push_client) reads the same path, including the SWISSARMYGRID_INFO
# override, so the two must stay in step. The process id lets anyone tell a live server from a
# file left behind by a crash (server/lifecycle.py).
#
# The token in this file is the only credential the server accepts, so the file is created
# owner-only. On Windows the mode bits do little, but the user's profile folder is already
# readable only by that user (plus administrators and SYSTEM).


def info_file_path() -> Path:
    """Where the connection file lives. SWISSARMYGRID_INFO overrides it (tests use this)."""
    override: str | None = os.environ.get("SWISSARMYGRID_INFO")
    return Path(override) if override else Path.home() / ".swissarmygrid" / "server.json"


def write_info_file(path: Path, info: dict[str, Any]) -> None:
    """Write the connection file atomically, so a client never reads a half-written one.

    The temp file is always a new one (O_EXCL, never through a symlink): reusing one left behind
    kept that file's mode, and the rename gave it to the connection file, token and all."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp: Path = path.with_suffix(".tmp")
    temp.unlink(missing_ok=True)  # a leftover from a crash, or anything else in the way
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int = os.open(temp, flags, 0o600)
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


# ===== the model library =====
# A folder of model files (domain/relationships.py has the shape). Three places, read in this order,
# a later file with the same name winning:
#
#   1. the models shipped in the package (src/swissarmygrid/models/*.json), released with it
#   2. ~/.swissarmygrid/models/*.json: a person's own
#   3. every folder in SWISSARMYGRID_MODELS (separated by ; on Windows): for example a shared folder
#      of models, or a repository's models folder


class ModelFiles:
    """The library of relationship models: the service layer's `ModelLibrary` port."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir

    def folders(self) -> list[str]:
        return [str(folder) for folder in self._folders()]

    def _folders(self) -> list[Path]:
        folders = [Path(str(files("swissarmygrid").joinpath("models"))), self.state_dir / "models"]
        folders += [
            Path(p) for p in os.environ.get("SWISSARMYGRID_MODELS", "").split(os.pathsep) if p.strip()
        ]
        return folders

    def read(self) -> tuple[dict[str, tuple[Model, str]], list[str]]:
        """Every library model by name (with its file), and the files that could not be read."""
        found: dict[str, tuple[Model, str]] = {}
        problems: list[str] = []
        for folder in self._folders():
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.json")):
                try:
                    model = parse_model(json.loads(path.read_text(encoding="utf-8")))
                    found[model.name] = (model, str(path))
                except (OSError, ValueError) as error:
                    problems.append(f"{path.name}: {error}")
        return found, problems
