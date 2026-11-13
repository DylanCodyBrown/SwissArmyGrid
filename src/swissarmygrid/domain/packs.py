"""Packs: blades and tangs a team shares through a Claude Code plugin marketplace (the owner's idea:
"use the Claude marketplace to source blades and tangs, so they could be stored in a GitHub repo and
collaborated with").

A marketplace is a repository with `.claude-plugin/marketplace.json` listing plugins; a **pack** is one
of those plugins carrying a `blades/` folder (`.blade.py` files) and a `tangs/` folder (`.tang` files).
Claude Code copies a plugin's folder as a unit when it installs it, so the blades and tangs arrive with
it (`/plugin marketplace add org/repo`, `/plugin install sales@repo`, `/plugin marketplace update`).
SwissArmyGrid lists an installed pack's blades and tangs read-only; changes go through the repository:
a clone added as a blade folder, a commit, a pull request.

Standard library only, and no I/O: the adapter reads and writes the files.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

PLUGIN_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")  # Claude Code plugin names are kebab-case
MARKETPLACE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RESERVED = frozenset({"claude-plugins-official", "claude-code-marketplace", "anthropic-marketplace"})
BLADES_FOLDER = "blades"
TANGS_FOLDER = "tangs"


class PackError(ValueError):
    """A pack or marketplace that cannot be made as asked. The message says why."""


@dataclass(frozen=True)
class Pack:
    """A plugin that carries blades and tangs: installed (read-only) or a clone's (editable)."""

    name: str
    marketplace: str
    version: str
    root: str  # the plugin's folder
    blades: int  # how many .blade.py files it carries
    tangs: int
    enabled: bool = True
    editable: bool = False  # in a blade folder (a clone of the repository): blades and tangs can be added

    @property
    def label(self) -> str:
        at = f"{self.name}@{self.marketplace}" if self.marketplace else self.name
        return f"{at} {self.version}".strip()

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "marketplace": self.marketplace,
            "version": self.version,
            "label": self.label,
            "root": self.root,
            "blades": self.blades,
            "tangs": self.tangs,
            "enabled": self.enabled,
            "editable": self.editable,
        }


def check_names(marketplace: str, pack: str) -> None:
    if not isinstance(marketplace, str) or not MARKETPLACE_NAME.fullmatch(marketplace) or ".." in marketplace:
        raise PackError(
            "The marketplace's name is letters, digits, '.', '_' or '-', starting with a letter or digit."
        )
    if marketplace.casefold() in RESERVED:
        raise PackError(f"{marketplace} is a name Claude Code keeps for itself; pick another.")
    if not isinstance(pack, str) or not PLUGIN_NAME.fullmatch(pack):
        raise PackError("The pack's name is lowercase letters, digits and '-' (sales-pack).")


def scaffold(marketplace: str, owner: str, pack: str, description: str = "") -> dict[str, str]:
    """A new marketplace repository with one pack, as {relative path: text}. No version in the pack, so
    every commit is an update for whoever has it installed (Claude Code uses the commit then)."""
    check_names(marketplace, pack)
    owner = (owner or "").strip() or "The team"
    about = (description or "").strip() or f"Blades and tangs, shared by {owner}."
    manifest = {
        "name": marketplace,
        "owner": {"name": owner},
        "description": about,
        "plugins": [
            {
                "name": pack,
                "source": f"./packs/{pack}",
                "description": about,
                "keywords": ["swissarmygrid", "blades", "tangs"],
            }
        ],
    }
    plugin = {"name": pack, "description": about, "keywords": ["swissarmygrid", "blades", "tangs"]}
    readme = f"""# {marketplace}

{about}

A Claude Code plugin marketplace of SwissArmyGrid **packs**: each pack is a plugin with a `blades/`
folder (`.blade.py` files: saved code that loads data) and a `tangs/` folder (`.tang` files: saved
relationships between tables).

## Use it

In Claude Code:

```
/plugin marketplace add <owner>/<this repository>
/plugin install {pack}@{marketplace}
```

SwissArmyGrid then lists the pack's blades on its blades page and its tangs in the Relationships
window, marked with the pack. To get the team's latest:

```
/plugin marketplace update {marketplace}
```

## Add to it

1. Clone this repository, and add the clone as a blade folder in SwissArmyGrid (Blade folders, on the
   blades page).
2. Save blades into `packs/{pack}/blades/` and tangs into `packs/{pack}/tangs/` (or use Add to pack in
   SwissArmyGrid's Packs window).
3. Commit, push a branch and open a pull request. Once it is merged, everyone updates.

Blade files hold code that runs on the computer of whoever uses the blade: review them like any code.
Secrets are never in them (only their names).
"""
    keep = "Files here travel with the pack: {what}. Remove this note once there is one.\n"
    return {
        ".claude-plugin/marketplace.json": json.dumps(manifest, indent=2) + "\n",
        f"packs/{pack}/.claude-plugin/plugin.json": json.dumps(plugin, indent=2) + "\n",
        f"packs/{pack}/{BLADES_FOLDER}/README.md": keep.format(what="blades, as .blade.py files"),
        f"packs/{pack}/{TANGS_FOLDER}/README.md": keep.format(what="tangs, as .tang files"),
        "README.md": readme,
    }


def installed_entries(raw: Any) -> list[tuple[str, str, str]]:
    """(plugin id "name@marketplace", install path, version) for every install recorded in Claude Code's
    installed_plugins.json, read loosely: its shape is not documented, so any object holding an
    "installPath" counts, named by the nearest key that looks like an id."""
    found: list[tuple[str, str, str]] = []

    def walk(node: Any, name: str) -> None:
        if isinstance(node, dict):
            path = node.get("installPath")
            if isinstance(path, str) and path:
                version = node.get("version") or node.get("gitCommitSha") or ""
                found.append((name, path, str(version)[:40]))
            for key, value in node.items():
                walk(value, key if isinstance(key, str) and "@" in key else name)
        elif isinstance(node, list):
            for item in node:
                walk(item, name)

    walk(raw, "")
    return found


def is_enabled(settings: Any, plugin_id: str) -> bool:
    """Off only when the user's settings say so (`enabledPlugins: {"sales@team": false}`)."""
    enabled = settings.get("enabledPlugins") if isinstance(settings, dict) else None
    return not (isinstance(enabled, dict) and enabled.get(plugin_id) is False)


def split_id(plugin_id: str) -> tuple[str, str]:
    name, _, marketplace = plugin_id.partition("@")
    return name, marketplace
