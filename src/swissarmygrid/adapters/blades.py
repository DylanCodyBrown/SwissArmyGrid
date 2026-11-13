"""Saved blades: one JSON file each under ~/.swissarmygrid/blades/, secrets in the operating system's vault.

Four kinds (plan decisions 14-15b):
- `sql`: code that makes a database connection, with its dialect, and usually a catalog command.
- `other`: any code that reaches outside Python (an API client, a path); a catalog command is optional.
- `vault`: code that makes `get(name) -> value`, plus which vault secret goes to which environment
  variable. Values are fetched live when the blade runs and never kept.
- `secret`: only environment variable names. Their values are typed in each session and never kept.

Every blade has an optional description and tags, for finding and grouping blades.

Blades also live in **blade folders** (the owner's request): folders the user lists (Settings, or the
SWISSARMYGRID_BLADE_FOLDERS environment variable) are searched for portable blade files, Python files whose
frontmatter is a `# /// swissarmygrid-blade` header (domain/blades.py). Such a blade's file is its
source of truth: saving it writes the file, a change to the file (a `git pull`) shows on the next look, and
removing one is the user's to do in their folder. A saved blade with the same name comes first.

A blade's own secrets (`secret("password")` in its code) are kept in the vault through `keyring`,
under the service "SwissArmyGrid" and the user name `blade/<name>/<secret>`; the file never holds a
value. A blade's code is the user's: the routes never show it to the agent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from swissarmygrid.domain import blades as rules
from swissarmygrid.domain.blades import (
    Blade,
    BladeError,
    VaultUnavailableError,
    file_name,
    is_blade,
    read_blade,
    write_blade,
)

KEYRING_SERVICE: str = "SwissArmyGrid"


def parse_blade(data: Any, previous: Blade | None = None) -> Blade:
    """A blade from the save screen's JSON, checked (domain/blades.py) and stamped with the time now."""
    return rules.parse_blade(data, previous, time.time())


def _file_name(name: str) -> str:
    key = name.casefold()
    readable = re.sub(r"[^a-z0-9_-]+", "_", key).strip("_")[:40] or "blade"
    return f"{readable}-{hashlib.sha256(key.encode()).hexdigest()[:8]}.json"


class BladeStore:
    """The saved blades, and the blades in the user's blade folders. Every method is quick file work; call
    them off the event loop when it matters."""

    def __init__(
        self,
        folder: Path,
        folders_file: Path | None = None,
        pins_file: Path | None = None,
        packs: Callable[[], list[tuple[Path, str]]] | None = None,
    ) -> None:
        self.folder = folder
        self.folders_file = folders_file
        # Installed packs' blades folders, with each pack's label: read-only blades from a marketplace.
        self._packs = packs or (lambda: [])
        # Favourites: this person's own, so kept on this computer, never in a blade's file.
        self.pins_file = pins_file
        self._lock = threading.Lock()
        # A folder blade's file -> (its modification time and size, what it held): files are read again
        # only when they change.
        self._seen: dict[str, tuple[tuple[int, int], Blade | None, str | None]] = {}
        self.problems: list[dict[str, str]] = []  # files with a blade header that could not be read

    # ===== files =====

    def _path(self, name: str) -> Path:
        return self.folder / _file_name(name)

    def _read(self, path: Path) -> Blade | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("name"), str):
            return None
        catalog = data.get("catalog") or {}
        return Blade(
            name=data["name"],
            kind=data.get("kind", "other"),
            code=data.get("code", ""),
            variable=data.get("variable"),
            dialect=data.get("dialect", "other"),
            catalog_language=catalog.get("language"),
            catalog_command=catalog.get("command"),
            vault_mapping={
                r["env"]: r["secret"] for r in data.get("vault_mapping", []) if isinstance(r, dict)
            },
            env_names=list(data.get("env_names", [])),
            template=data.get("template"),
            description=str(data.get("description") or ""),
            tags=[str(t) for t in data.get("tags", []) if isinstance(t, str)],
            created_at=float(data.get("created_at", 0)),
            updated_at=float(data.get("updated_at", 0)),
        )

    def _write(self, blade: Blade) -> None:
        if blade.path:
            self._write_file(blade)
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self._path(blade.name)
        temporary = path.with_suffix(".tmp")
        # Only this user may read it: a blade's code can name servers and accounts.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(blade.to_json(), handle, indent=2)
        os.replace(temporary, path)

    @staticmethod
    def _write_file(blade: Blade) -> None:
        """A folder blade goes back into its own file, as a portable blade."""
        path = Path(blade.path or "")
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(write_blade(blade.to_json()), encoding="utf-8")
        os.replace(temporary, path)

    # ===== favourites =====

    def pinned(self) -> set[str]:
        """The favourite blades' names (ignoring case)."""
        if self.pins_file is None:
            return set()
        try:
            names = json.loads(self.pins_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return {n.casefold() for n in names if isinstance(n, str)}

    def set_pinned(self, name: str, pinned: bool) -> bool:
        """Pin a blade to the favourites, or take it off. Returns whether it is pinned now."""
        blade = self.get(name)
        if self.pins_file is None:
            raise BladeError("Favourites are not available here.")
        names = self.pinned()
        if pinned:
            names.add(blade.name.casefold())
        else:
            names.discard(blade.name.casefold())
        self.pins_file.parent.mkdir(parents=True, exist_ok=True)
        self.pins_file.write_text(json.dumps(sorted(names), indent=2), encoding="utf-8")
        return pinned

    # ===== blade folders =====

    def folders(self) -> list[dict[str, Any]]:
        """The blade folders: the user's list, then those in SWISSARMYGRID_BLADE_FOLDERS."""
        listed: list[dict[str, Any]] = []
        if self.folders_file is not None:
            try:
                saved = json.loads(self.folders_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = []
            listed += [{"path": str(p), "from_env": False} for p in saved if isinstance(p, str)]
        for part in os.environ.get("SWISSARMYGRID_BLADE_FOLDERS", "").split(os.pathsep):
            if part.strip():
                listed.append({"path": part.strip(), "from_env": True})
        seen: set[str] = set()
        unique = []
        for entry in listed:
            key = os.path.normcase(os.path.abspath(entry["path"]))
            if key not in seen:
                seen.add(key)
                unique.append(entry | {"exists": Path(entry["path"]).is_dir()})
        return unique

    def set_folders(self, paths: Any) -> list[dict[str, Any]]:
        """Replace the user's list (the environment's folders stay). Each must be an existing folder."""
        if self.folders_file is None:
            raise BladeError("Blade folders are not available here.")
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths) or len(paths) > 50:
            raise BladeError('Send the folders as {"folders": ["C:\\\\blades", ...]} (at most 50).')
        clean: list[str] = []
        for raw in paths:
            text = raw.strip()
            if not text:
                continue
            path = Path(text).expanduser()
            if not path.is_absolute():
                raise BladeError(f"{text}: give the folder's full path.")
            if not path.is_dir():
                raise BladeError(f"{text} is not a folder on this computer.")
            clean.append(str(path))
        self.folders_file.parent.mkdir(parents=True, exist_ok=True)
        self.folders_file.write_text(json.dumps(clean, indent=2), encoding="utf-8")
        return self.folders()

    def _scan(self) -> list[Blade]:
        """Every blade in the blade folders, in folder order (the first of a name wins). Hidden folders,
        virtual environments, site-packages and caches are skipped; at most 4 levels and 5,000 Python
        files per folder are looked at."""
        found: list[Blade] = []
        problems: list[dict[str, str]] = []
        skip = {"__pycache__", "node_modules", "site-packages", "venv", "env", "build", "dist"}
        for entry in self.folders():
            root = Path(entry["path"])
            if not root.is_dir():
                continue
            looked = 0
            for directory, subdirectories, files in os.walk(root):
                depth = len(Path(directory).relative_to(root).parts)
                subdirectories[:] = sorted(
                    d for d in subdirectories if not d.startswith(".") and d not in skip and depth < 4
                )
                for name in sorted(files):
                    if not name.endswith(".py") or looked >= 5_000:
                        continue
                    looked += 1
                    path = Path(directory) / name
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    stamp = (stat.st_mtime_ns, stat.st_size)
                    cached = self._seen.get(str(path))
                    if cached is None or cached[0] != stamp:
                        cached = (stamp, *self._load_file(path, stat.st_size, is_blade, read_blade))
                        self._seen[str(path)] = cached
                    blade, problem = cached[1], cached[2]
                    if problem:
                        problems.append({"path": str(path), "error": problem})
                    if blade is not None:
                        found.append(blade)
        for root, label in self._packs():
            for path in sorted(root.rglob("*.py")) if root.is_dir() else []:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                stamp = (stat.st_mtime_ns, stat.st_size)
                cached = self._seen.get(str(path))
                if cached is None or cached[0] != stamp:
                    cached = (stamp, *self._load_file(path, stat.st_size, is_blade, read_blade))
                    self._seen[str(path)] = cached
                blade, problem = cached[1], cached[2]
                if problem:
                    problems.append({"path": str(path), "error": problem})
                if blade is not None:
                    blade.pack = label
                    found.append(blade)
        self.problems = problems
        return found

    @staticmethod
    def _load_file(path: Path, size: int, is_blade: Any, read_blade: Any) -> tuple[Blade | None, str | None]:
        """The blade in one file (None for an ordinary Python file), or the reason it could not be read."""
        if size > 1_000_000:
            return None, None
        try:
            text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        except (OSError, UnicodeDecodeError):
            return None, None
        if not is_blade(text):
            return None, None  # an ordinary Python file: no blade header as its frontmatter
        try:
            blade = parse_blade(read_blade(text))
        except (ValueError, BladeError) as error:
            return None, str(error)
        blade.path = str(path)
        modified = path.stat().st_mtime
        blade.created_at = blade.updated_at = modified
        return blade, None

    # ===== the blades =====

    def _saved(self) -> list[Blade]:
        if not self.folder.is_dir():
            return []
        return [b for b in (self._read(p) for p in self.folder.glob("*.json")) if b is not None]

    def all(self) -> list[Blade]:
        """Saved blades, folder blades and pack blades; a saved blade comes first when two share a
        name, then the first folder's (a clone being edited), then an installed pack's."""
        by_name: dict[str, Blade] = {}
        for blade in [*self._saved(), *self._scan()]:
            by_name.setdefault(blade.name.casefold(), blade)
        return sorted(by_name.values(), key=lambda b: b.name.casefold())

    def get(self, name: str) -> Blade:
        blade = self._read(self._path(name))
        if blade is not None and blade.name.casefold() == name.casefold():
            return blade
        for found in self._scan():
            if found.name.casefold() == name.casefold():
                return found
        raise KeyError(name)

    def save(self, data: Any, previous_name: str | None = None) -> Blade:
        """Save a new blade, or replace `previous_name` (which may be a rename). A folder blade is saved
        back into its own file; a new blade with `"folder"` set goes into that blade folder as a file."""
        with self._lock:
            previous = None
            if previous_name is not None:
                try:
                    previous = self.get(previous_name)
                except KeyError:
                    raise BladeError(f"No blade named {previous_name!r} to change.") from None
            if previous is not None and previous.pack:
                raise BladeError(
                    f"{previous.name} comes from the pack {previous.pack}: change it in that pack's "
                    "repository (a clone added as a blade folder), or save a copy under another name."
                )
            blade = parse_blade(data, previous)
            renamed = previous is not None and previous.name.casefold() != blade.name.casefold()
            if previous is None or renamed:
                try:
                    taken = self.get(blade.name)
                except KeyError:
                    taken = None
                if taken is not None:
                    raise BladeError(f"A blade named {taken.name!r} already exists. Pick another name.")
            if previous is None and isinstance(data, dict) and data.get("folder"):
                blade.path = str(self._new_file(str(data["folder"]), blade.name))
            self._write(blade)
            if renamed and previous is not None:
                for secret in previous.secrets:
                    value = self._secret(previous.name, secret)
                    if value is not None and secret in blade.secrets:
                        self._set_secret(blade.name, secret, value)
                    self._delete_secret(previous.name, secret)
                if not previous.path:
                    self._path(previous.name).unlink(missing_ok=True)
            return blade

    def _new_file(self, folder: str, name: str) -> Path:
        listed = {os.path.normcase(os.path.abspath(f["path"])) for f in self.folders()}
        if os.path.normcase(os.path.abspath(folder)) not in listed:
            raise BladeError(f"{folder} is not one of your blade folders. Add it first.")
        path = Path(folder) / file_name(name)
        if path.exists():
            raise BladeError(f"{path} already exists. Pick another name, or edit that blade.")
        return path

    def remove(self, name: str) -> None:
        with self._lock:
            blade = self.get(name)
            if blade.pack:
                raise BladeError(
                    f"{blade.name} comes from the pack {blade.pack}. Uninstall or disable the pack in Claude "
                    "Code (/plugin) to remove its blades."
                )
            if blade.path:
                raise BladeError(
                    f"{blade.name} is a file in your blade folder ({blade.path}). Delete or move the file "
                    "there to remove it."
                )
            for secret in blade.secrets:
                self._delete_secret(blade.name, secret)
            self._path(blade.name).unlink(missing_ok=True)

    # ===== secrets, in the operating system's vault =====

    @staticmethod
    def _keyring() -> Any:
        import keyring
        from keyring.backends import fail

        backend = keyring.get_keyring()
        if isinstance(backend, fail.Keyring):
            raise VaultUnavailableError(
                "This computer has no vault SwissArmyGrid can keep secrets in (Windows Credential Manager, "
                "macOS Keychain or a Linux Secret Service). Use a secret blade (typed each session) or a "
                "vault blade instead."
            )
        return keyring

    @staticmethod
    def _user(name: str, secret: str) -> str:
        return f"blade/{name.casefold()}/{secret}"

    def _secret(self, name: str, secret: str) -> str | None:
        value: str | None = self._keyring().get_password(KEYRING_SERVICE, self._user(name, secret))
        return value

    def _set_secret(self, name: str, secret: str, value: str) -> None:
        self._keyring().set_password(KEYRING_SERVICE, self._user(name, secret), value)

    def _delete_secret(self, name: str, secret: str) -> None:
        try:
            self._keyring().delete_password(KEYRING_SERVICE, self._user(name, secret))
        except VaultUnavailableError:
            return
        except Exception:  # keyring raises PasswordDeleteError for one that was never set
            return

    def secret_status(self, name: str | Blade) -> dict[str, bool]:
        """Each secret the blade's code asks for, and whether a value is saved (never the value)."""
        blade = name if isinstance(name, Blade) else self.get(name)
        try:
            return {s: self._secret(blade.name, s) is not None for s in blade.secrets}
        except VaultUnavailableError:
            return {s: False for s in blade.secrets}

    def set_secrets(self, name: str, values: Any) -> dict[str, bool]:
        """Save (text) or forget (null) the blade's secrets. Only names its code asks for."""
        blade = self.get(name)
        if not isinstance(values, dict):
            raise BladeError('Send the secrets as {"password": "...", ...}; null forgets one.')
        for secret, value in values.items():
            if secret not in blade.secrets:
                raise BladeError(f"The blade's code never asks for secret({secret!r}).")
            if value is None:
                self._delete_secret(blade.name, secret)
            elif isinstance(value, str) and 0 < len(value) <= 10_000:
                self._set_secret(blade.name, secret, value)
            else:
                raise BladeError(f"{secret}: a secret is 1 to 10,000 characters of text.")
        return self.secret_status(name)

    def saved_secrets(self, blade: Blade) -> dict[str, str]:
        """The saved values, for running the blade (the client's sag.blade, the server's Refresh)."""
        values: dict[str, str] = {}
        try:
            for secret in blade.secrets:
                value = self._secret(blade.name, secret)
                if value is not None:
                    values[secret] = value
        except VaultUnavailableError:
            return {}
        return values

    # ===== what a blade needs from vault and secret blades =====

    def providers(
        self, blade: Blade, everything: list[Blade] | None = None
    ) -> tuple[list[Blade], dict[str, str]]:
        """For the secrets the blade asks for and has no saved value for: the vault blades that set
        them (to run first), and the variables a secret blade asks the user for (name -> that blade)."""
        missing = [s for s in blade.secrets if s not in self.saved_secrets(blade)]
        vaults: list[Blade] = []
        typed: dict[str, str] = {}
        for other in self.all() if everything is None else everything:
            if other.name == blade.name:
                continue
            if other.kind == "vault" and any(env in missing for env in other.vault_mapping):
                vaults.append(other)
            if other.kind == "secret":
                for env in other.env_names:
                    if env in missing:
                        typed.setdefault(env, other.name)
        return vaults, typed
