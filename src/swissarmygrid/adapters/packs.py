"""Packs on disk: the Claude Code plugins carrying blades and tangs, and the clones a team edits.

Installed packs come from Claude Code's own folders, read and never written:

  <claude dir>/plugins (or CLAUDE_CODE_PLUGIN_CACHE_DIR); the claude dir is ~/.claude (Settings)
      installed_plugins.json        which plugin versions are installed, and where (read loosely)
      cache/<marketplace>/<plugin>/<version>/   the installed copy: its blades/ and tangs/ are the pack's
  <claude dir>/settings.json  enabledPlugins: a plugin set to false is left out

Editable packs are plugin folders (a `.claude-plugin/plugin.json`) inside the blade folders: a clone of
the team's marketplace repository. Their blades and tangs are already read as folder blades and shared
tangs; here they are listed so Add to pack can write into them. Scaffolding a new marketplace writes a
new folder.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from swissarmygrid.domain.packs import (
    BLADES_FOLDER,
    TANGS_FOLDER,
    Pack,
    PackError,
    installed_entries,
    is_enabled,
    split_id,
)

_DEPTH = 4
_SKIPPED = frozenset(
    {"__pycache__", "node_modules", "site-packages", ".venv", "venv", "env", "build", "dist"}
)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _count(folder: Path, suffix: str) -> int:
    if not folder.is_dir():
        return 0
    return sum(1 for p in folder.rglob(f"*{suffix}") if p.is_file())


class PackFolders:
    """The service layer's `PackFiles` port."""

    def __init__(self, claude_dir: Path, shared: Callable[[], list[str]], environ: Mapping[str, str]) -> None:
        self.config_dir = claude_dir
        self._shared = shared
        self.environ = environ

    @property
    def plugins_root(self) -> Path:
        moved = self.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR", "").strip()
        return Path(moved) if moved else self.config_dir / "plugins"

    def _install_paths(self) -> list[tuple[str, Path, str]]:
        """(plugin id, folder, version): from installed_plugins.json, else every cached version's
        newest folder per plugin (cache/<marketplace>/<plugin>/<version>/)."""
        recorded = installed_entries(_read_json(self.plugins_root / "installed_plugins.json"))
        if recorded:
            return [(pid, Path(path), version) for pid, path, version in recorded]
        found: list[tuple[str, Path, str]] = []
        cache = self.plugins_root / "cache"
        if not cache.is_dir():
            return found
        for marketplace in sorted(p for p in cache.iterdir() if p.is_dir()):
            for plugin in sorted(p for p in marketplace.iterdir() if p.is_dir()):
                versions = [v for v in plugin.iterdir() if v.is_dir()]
                if versions:
                    newest = max(versions, key=lambda v: v.stat().st_mtime)
                    found.append((f"{plugin.name}@{marketplace.name}", newest, newest.name))
        return found

    def installed(self) -> list[Pack]:
        """Every installed plugin that carries blades or tangs, enabled or not (the list says)."""
        settings = _read_json(self.config_dir / "settings.json")
        packs: list[Pack] = []
        seen: set[str] = set()
        for plugin_id, root, version in self._install_paths():
            if plugin_id in seen or not root.is_dir():
                continue
            blades_dir, tangs_dir = root / BLADES_FOLDER, root / TANGS_FOLDER
            if not blades_dir.is_dir() and not tangs_dir.is_dir():
                continue  # a plugin that is not a pack
            seen.add(plugin_id)
            name, marketplace = (
                split_id(plugin_id) if plugin_id else (root.parent.name, root.parent.parent.name)
            )
            packs.append(
                Pack(
                    name, marketplace, version[:12], str(root), _count(blades_dir, ".py"),
                    _count(tangs_dir, ".tang"), is_enabled(settings, plugin_id),
                )
            )  # fmt: skip
        return packs

    def blade_dirs(self) -> list[tuple[Path, str]]:
        """(blades folder, the pack's label) of each enabled installed pack."""
        return [(Path(p.root) / BLADES_FOLDER, p.label) for p in self.installed() if p.enabled and p.blades]

    def tang_dirs(self) -> list[tuple[Path, str]]:
        return [(Path(p.root) / TANGS_FOLDER, p.label) for p in self.installed() if p.enabled and p.tangs]

    def editable(self) -> list[Pack]:
        """Plugin folders inside the blade folders (clones of a marketplace repository)."""
        packs: list[Pack] = []
        for folder in self._shared():
            base = Path(folder)
            if not base.is_dir():
                continue
            for directory, subdirectories, _ in os.walk(base):
                here = Path(directory)
                depth = len(here.relative_to(base).parts)
                subdirectories[:] = sorted(
                    d
                    for d in subdirectories
                    if d not in _SKIPPED and (d == ".claude-plugin" or not d.startswith("."))
                )
                if depth >= _DEPTH:
                    subdirectories[:] = []
                manifest = _read_json(here / ".claude-plugin" / "plugin.json")
                if not isinstance(manifest, dict) or not isinstance(manifest.get("name"), str):
                    continue
                if (here / ".claude-plugin" / "marketplace.json").is_file() and not (
                    here / BLADES_FOLDER
                ).is_dir():
                    continue  # the marketplace's own root, not a pack
                marketplace = ""
                for up in [here, *here.parents]:
                    listed = _read_json(up / ".claude-plugin" / "marketplace.json")
                    if isinstance(listed, dict) and isinstance(listed.get("name"), str):
                        marketplace = listed["name"]
                        break
                    if up == base:
                        break
                packs.append(
                    Pack(
                        manifest["name"], marketplace, str(manifest.get("version") or "clone"), str(here),
                        _count(here / BLADES_FOLDER, ".py"), _count(here / TANGS_FOLDER, ".tang"),
                        editable=True,
                    )
                )  # fmt: skip
        return packs

    def create(self, folder: str, files: dict[str, str]) -> list[str]:
        """Write a new marketplace repository into `folder` (new, or empty). Returns the files written."""
        target = Path(folder).expanduser()
        if not target.is_absolute():
            raise PackError("Give the new folder's full path.")
        if target.exists() and any(target.iterdir()):
            raise PackError(f"{target} is not empty: pick a new folder for the marketplace.")
        written: list[str] = []
        for relative, text in files.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written.append(str(path))
        return written

    def write(self, root: str, folder: str, name: str, text: str, replace: bool) -> tuple[str, bool]:
        """Write one blade or tang into an editable pack (and nowhere else). (path, whether written)."""
        allowed = {os.path.normcase(str(Path(p.root).resolve())) for p in self.editable()}
        pack_root = Path(root)
        if os.path.normcase(str(pack_root.resolve())) not in allowed or folder not in (
            BLADES_FOLDER,
            TANGS_FOLDER,
        ):
            raise PackError("Add to a pack inside one of your blade folders (a clone of the repository).")
        path = pack_root / folder / name
        if path.exists() and not replace:
            return str(path), False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return str(path), True
