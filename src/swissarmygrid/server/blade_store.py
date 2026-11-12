"""Saved blades: one JSON file each under ~/.swissarmygrid/blades/, secrets in the operating system's vault.

Four kinds (plan decisions 14-15b):
- `sql`: code that makes a database connection, with its dialect, and usually a catalog command.
- `other`: any code that reaches outside Python (an API client, a path); a catalog command is optional.
- `vault`: code that makes `get(name) -> value`, plus which vault secret goes to which environment
  variable. Values are fetched live when the blade runs and never kept.
- `secret`: only environment variable names. Their values are typed in each session and never kept.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swissarmygrid.blade_core import code as blade_code
from swissarmygrid.engine.catalog import CatalogError, check_name, normalize_dialect

KINDS: tuple[str, ...] = ("sql", "other", "vault", "secret")
KEYRING_SERVICE: str = "SwissArmyGrid"
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_MAX_CODE_CHARS: int = 200_000


class BladeError(ValueError):
    """The blade cannot be saved or used as asked. The message says why in plain words."""


class VaultUnavailableError(RuntimeError):
    """This computer has no operating system vault that keyring can use."""


@dataclass
class Blade:
    name: str
    kind: str
    code: str = ""
    variable: str | None = None
    dialect: str = "other"
    catalog_language: str | None = None
    catalog_command: str | None = None
    vault_mapping: dict[str, str] = field(default_factory=dict)  # environment variable -> vault secret name
    env_names: list[str] = field(default_factory=list)  # a secret blade's variables
    template: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def secrets(self) -> list[str]:
        """The names its code asks for with secret("...")."""
        if not self.code:
            return []
        try:
            return blade_code.read(self.code, self.name).secrets
        except blade_code.BladeCodeError:
            return []

    @property
    def has_catalog(self) -> bool:
        return bool(self.catalog_language and self.catalog_command)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "code": self.code,
            "variable": self.variable,
            "dialect": self.dialect,
            "catalog": (
                {"language": self.catalog_language, "command": self.catalog_command}
                if self.has_catalog
                else None
            ),
            "vault_mapping": [{"env": e, "secret": s} for e, s in self.vault_mapping.items()],
            "env_names": list(self.env_names),
            "template": self.template,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _file_name(name: str) -> str:
    key = name.casefold()
    readable = re.sub(r"[^a-z0-9_-]+", "_", key).strip("_")[:40] or "blade"
    return f"{readable}-{hashlib.sha256(key.encode()).hexdigest()[:8]}.json"


def _text(data: dict[str, Any], key: str, limit: int = 1_000) -> str:
    value = data.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BladeError(f"{key} must be text.")
    if len(value) > limit:
        raise BladeError(f"{key} is too long ({len(value):,} characters; at most {limit:,}).")
    return value


def _env_name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _ENV_NAME.fullmatch(value):
        raise BladeError(
            f"{where}: {value!r} is not an environment variable name. Use letters, digits and '_', "
            "not starting with a digit (WAREHOUSE_PASSWORD)."
        )
    return value


def parse_blade(data: Any, previous: Blade | None = None) -> Blade:
    """A blade from the save screen's JSON, checked. Raises BladeError with a plain message."""
    if not isinstance(data, dict):
        raise BladeError("Send the blade as a JSON object.")
    try:
        name = check_name(_text(data, "name", 64))
    except CatalogError as error:
        raise BladeError(str(error).replace("Catalog name", "Blade name")) from None
    kind = _text(data, "kind", 10)
    if kind not in KINDS:
        raise BladeError(f"A blade's kind is one of {', '.join(KINDS)}, not {kind!r}.")
    now = time.time()
    blade = Blade(
        name=name,
        kind=kind,
        template=_text(data, "template", 64) or None,
        created_at=previous.created_at if previous else now,
        updated_at=now,
    )
    if kind == "secret":
        names = data.get("env_names")
        if not isinstance(names, list) or not names:
            raise BladeError("A secret blade lists at least one environment variable name.")
        blade.env_names = list(dict.fromkeys(_env_name(n, "A secret blade's variable") for n in names))
        if len(blade.env_names) > 100:
            raise BladeError("A secret blade holds at most 100 variables.")
        return blade

    blade.code = _text(data, "code", _MAX_CODE_CHARS)
    if not blade.code.strip():
        raise BladeError(
            "The blade has no code. Paste or write the Python that reaches outside to load something."
        )
    try:
        found = blade_code.read(blade.code, f"Blade {name!r}")
    except blade_code.BladeCodeError as error:
        raise BladeError(str(error)) from None
    variable = _text(data, "variable", 128).strip() or found.variable
    if not variable:
        raise BladeError(
            "The code makes no variable to hand back. End it with an assignment, such as conn = ..., "
            "or name the variable."
        )
    if variable not in found.assigned:
        raise BladeError(
            f"The code never makes {variable!r}. It makes: {', '.join(found.assigned) or 'nothing'}."
        )
    blade.variable = variable

    if kind == "sql":
        try:
            blade.dialect = normalize_dialect(_text(data, "dialect", 32) or None)
        except CatalogError as error:
            raise BladeError(str(error)) from None

    if kind == "vault":
        mapping = data.get("vault_mapping") or []
        if not isinstance(mapping, list) or not mapping:
            raise BladeError(
                "A vault blade maps at least one secret in the vault to an environment variable."
            )
        for row in mapping:
            if not isinstance(row, dict):
                raise BladeError(
                    'Each vault mapping is {"env": "WAREHOUSE_PASSWORD", "secret": "warehouse-pwd"}.'
                )
            env = _env_name(row.get("env"), "A vault mapping's variable")
            secret = row.get("secret")
            if not isinstance(secret, str) or not secret.strip() or len(secret) > 500:
                raise BladeError(f"Name the secret in the vault for {env}.")
            if env in blade.vault_mapping:
                raise BladeError(f"{env} is mapped twice.")
            blade.vault_mapping[env] = secret.strip()
        return blade

    catalog = data.get("catalog")
    if catalog:
        if not isinstance(catalog, dict):
            raise BladeError('The catalog is {"language": "sql" or "python", "command": "..."}.')
        language = _text(catalog, "language", 10)
        command = _text(catalog, "command", _MAX_CODE_CHARS)
        if language not in ("sql", "python"):
            raise BladeError("A catalog command is sql or python.")
        if command.strip():
            if language == "python":
                try:
                    blade_code.parse(command, "The catalog command")
                except blade_code.BladeCodeError as error:
                    raise BladeError(str(error)) from None
            blade.catalog_language, blade.catalog_command = language, command
    return blade


class BladeStore:
    """The saved blades. Every method is quick file work; call them off the event loop when it matters."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._lock = threading.Lock()

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
            created_at=float(data.get("created_at", 0)),
            updated_at=float(data.get("updated_at", 0)),
        )

    def _write(self, blade: Blade) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self._path(blade.name)
        temporary = path.with_suffix(".tmp")
        # Only this user may read it: a blade's code can name servers and accounts.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(blade.to_json(), handle, indent=2)
        os.replace(temporary, path)

    # ===== the blades =====

    def all(self) -> list[Blade]:
        if not self.folder.is_dir():
            return []
        found = [b for b in (self._read(p) for p in self.folder.glob("*.json")) if b is not None]
        return sorted(found, key=lambda b: b.name.casefold())

    def get(self, name: str) -> Blade:
        blade = self._read(self._path(name))
        if blade is None or blade.name.casefold() != name.casefold():
            raise KeyError(name)
        return blade

    def save(self, data: Any, previous_name: str | None = None) -> Blade:
        """Save a new blade, or replace `previous_name` (which may be a rename)."""
        with self._lock:
            previous = None
            if previous_name is not None:
                try:
                    previous = self.get(previous_name)
                except KeyError:
                    raise BladeError(f"No blade named {previous_name!r} to change.") from None
            blade = parse_blade(data, previous)
            renamed = previous is not None and previous.name.casefold() != blade.name.casefold()
            if previous is None or renamed:
                try:
                    taken = self.get(blade.name)
                except KeyError:
                    taken = None
                if taken is not None:
                    raise BladeError(f"A blade named {taken.name!r} already exists. Pick another name.")
            self._write(blade)
            if renamed and previous is not None:
                for secret in previous.secrets:
                    value = self._secret(previous.name, secret)
                    if value is not None and secret in blade.secrets:
                        self._set_secret(blade.name, secret, value)
                    self._delete_secret(previous.name, secret)
                self._path(previous.name).unlink(missing_ok=True)
            return blade

    def remove(self, name: str) -> None:
        with self._lock:
            blade = self.get(name)
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

    def secret_status(self, name: str) -> dict[str, bool]:
        """Each secret the blade's code asks for, and whether a value is saved (never the value)."""
        blade = self.get(name)
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

    def providers(self, blade: Blade) -> tuple[list[Blade], dict[str, str]]:
        """For the secrets the blade asks for and has no saved value for: the vault blades that set
        them (to run first), and the variables a secret blade asks the user for (name -> that blade)."""
        missing = [s for s in blade.secrets if s not in self.saved_secrets(blade)]
        vaults: list[Blade] = []
        typed: dict[str, str] = {}
        for other in self.all():
            if other.name == blade.name:
                continue
            if other.kind == "vault" and any(env in missing for env in other.vault_mapping):
                vaults.append(other)
            if other.kind == "secret":
                for env in other.env_names:
                    if env in missing:
                        typed.setdefault(env, other.name)
        return vaults, typed
