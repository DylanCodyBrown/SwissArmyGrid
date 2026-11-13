"""Packs: blades and tangs from a Claude Code plugin marketplace (domain/packs.py has the idea).

  list     the installed packs (read-only: their blades and tangs show on the blades page and in the
           Relationships window with the pack's name), the editable ones (clones in the blade folders),
           and the commands to share
  create   a new marketplace repository with one pack, optionally added as a blade folder at once
  add      copy chosen blades and tangs into an editable pack, ready to commit

All of it is the user's: the routes refuse the agent (files on disk, and code others will run).
"""

from __future__ import annotations

from typing import Any, Protocol

from swissarmygrid.domain.blades import Blade, file_name, write_blade
from swissarmygrid.domain.packs import BLADES_FOLDER, TANGS_FOLDER, Pack, PackError, scaffold
from swissarmygrid.domain.relationships import Model, tang_file_name, write_tang
from swissarmygrid.services.workspace import NotFound, Workspace


class PackFiles(Protocol):
    """Packs on disk (adapters/packs.py)."""

    @property
    def plugins_root(self) -> Any: ...

    def installed(self) -> list[Pack]: ...

    def editable(self) -> list[Pack]: ...

    def create(self, folder: str, files: dict[str, str]) -> list[str]: ...

    def write(self, root: str, folder: str, name: str, text: str, replace: bool) -> tuple[str, bool]: ...


class PackBlades(Protocol):
    """What packs need of the blade store: a blade by name, and the blade folders."""

    def get(self, name: str) -> Blade: ...

    def folders(self) -> list[dict[str, Any]]: ...

    def set_folders(self, paths: Any) -> list[dict[str, Any]]: ...


class PackTangs(Protocol):
    """What packs need of the tang library: every tang by name."""

    def read(self) -> tuple[dict[str, tuple[Model, str]], list[str]]: ...


class Packs:
    def __init__(self, ws: Workspace, files: PackFiles, blades: PackBlades, tangs: PackTangs) -> None:
        self.ws = ws
        self.files = files
        self.blades = blades
        self.tangs = tangs

    def list(self) -> dict[str, Any]:
        installed = self.files.installed()
        return {
            "installed": [p.to_json() for p in installed],
            "editable": [p.to_json() for p in self.files.editable()],
            "plugins_root": str(self.files.plugins_root),
            "commands": {
                "add": "/plugin marketplace add <owner>/<repository>",
                "install": "/plugin install <pack>@<marketplace>",
                "update": "/plugin marketplace update <marketplace>",
            },
        }

    def create(self, body: Any, who: str) -> dict[str, Any]:
        """A new marketplace repository: {"folder", "marketplace", "owner", "pack", "description",
        "add_folder": true} (add_folder: add it as a blade folder, so saving into it works at once)."""
        if not isinstance(body, dict) or not isinstance(body.get("folder"), str):
            raise PackError('Send {"folder": "<new folder>", "marketplace", "owner", "pack", "description"}.')
        files = scaffold(
            str(body.get("marketplace") or ""),
            str(body.get("owner") or ""),
            str(body.get("pack") or ""),
            str(body.get("description") or ""),
        )
        written = self.files.create(body["folder"], files)
        added = False
        if body.get("add_folder", True):
            mine = [f["path"] for f in self.blades.folders() if not f.get("from_env")]
            if body["folder"] not in mine:
                self.blades.set_folders([*mine, body["folder"]])
                added = True
        self.ws.audit.write(
            who, "create_pack", None, marketplace=body.get("marketplace"), pack=body.get("pack")
        )
        return {"folder": body["folder"], "files": written, "added_as_blade_folder": added}

    def add(self, body: Any, who: str) -> dict[str, Any]:
        """Copy blades and tangs into an editable pack: {"pack": <its folder>, "blades": [names],
        "tangs": [names], "replace": false}. A file already there is kept unless replace."""
        if not isinstance(body, dict) or not isinstance(body.get("pack"), str):
            raise PackError('Send {"pack": "<the pack\'s folder>", "blades": [...], "tangs": [...]}.')
        root, replace = body["pack"], bool(body.get("replace"))
        written: list[str] = []
        kept: list[str] = []
        for name in body.get("blades") or []:
            try:
                blade = self.blades.get(str(name))
            except KeyError:
                raise NotFound(f"No blade named {name!r}.") from None
            path, done = self.files.write(
                root, BLADES_FOLDER, file_name(blade.name), write_blade(blade.to_json()), replace
            )
            (written if done else kept).append(path)
        found, _ = self.tangs.read()
        for name in body.get("tangs") or []:
            entry = found.get(str(name))
            if entry is None:
                raise NotFound(f"No tang named {name!r}.")
            tang = entry[0]
            path, done = self.files.write(
                root, TANGS_FOLDER, tang_file_name(tang.name), write_tang(tang), replace
            )
            (written if done else kept).append(path)
        self.ws.audit.write(who, "add_to_pack", None, pack=root, written=len(written))
        return {"written": written, "kept": kept}
