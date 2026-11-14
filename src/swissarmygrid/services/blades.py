"""Blades (plan decisions 14-18): the user's saved code that loads from outside Python.

The user's alone: a blade's code names servers and accounts, and runs. The doors refuse the agent
before any of this is reached (its own narrow door is services/agent.py).

  save, change, remove         a blade, with its secrets into the operating system's vault
  inspect, fill                what code makes, asks for, imports and needs; its settings written back
  refresh, test                run it in a child process: load its catalog, or just see that it works
  for_python                   everything sag.blade() needs to run it in the caller's Python
  install                      packages into SwissArmyGrid's own Python, on the user's click
  favourites, folders          this person's own, kept on this computer
  export, import               portable .blade.py files; an import is reviewed before anything is saved
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol

from swissarmygrid.domain import blades as rules
from swissarmygrid.domain.analysis import whole_view_queries
from swissarmygrid.domain.blades import (
    Blade,
    BladeCodeError,
    BladeError,
    Package,
    Template,
    VaultUnavailableError,
    install_command,
    pip_name,
)
from swissarmygrid.domain.connections import code_names, connection_kind
from swissarmygrid.domain.datasets import DatasetInfo
from swissarmygrid.domain.grids import Grid
from swissarmygrid.services.catalogs import CatalogService
from swissarmygrid.services.ports import BACKGROUND, SqlSession
from swissarmygrid.services.workspace import Workspace

_PIP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,99}(\[[A-Za-z0-9,._\-]{1,100}\])?")


class Blades(Protocol):
    """The saved blades, the blade folders and the vault (adapters/blades.py)."""

    problems: list[dict[str, str]]

    def all(self) -> list[Blade]: ...

    def get(self, name: str) -> Blade: ...

    def save(self, data: Any, previous_name: str | None = None) -> Blade: ...

    def remove(self, name: str) -> None: ...

    def pinned(self) -> set[str]: ...

    def set_pinned(self, name: str, pinned: bool) -> bool: ...

    def folders(self) -> list[dict[str, Any]]: ...

    def set_folders(self, paths: Any) -> list[dict[str, Any]]: ...

    def secret_status(self, name: str | Blade) -> dict[str, bool]: ...

    def set_secrets(self, name: str, values: Any) -> dict[str, bool]: ...

    def saved_secrets(self, blade: Blade) -> dict[str, str]: ...

    def providers(
        self, blade: Blade, everything: list[Blade] | None = None
    ) -> tuple[list[Blade], dict[str, str]]: ...


class NeedsValues(Exception):
    """The run needs values the user types each time (a secret blade's): variable -> that blade."""

    def __init__(self, missing: dict[str, str]) -> None:
        super().__init__(f"Type {', '.join(missing)} for this run (never kept).")
        self.missing = missing


def _free_name(name: str, taken: set[str]) -> str:
    """`name 2`, `name 3`... the first not taken (names compared ignoring case), within 64 characters."""
    n = 2
    while True:
        candidate = f"{name[:58]} {n}"
        if candidate.casefold() not in taken:
            return candidate
        n += 1


class BladeService:
    def __init__(
        self,
        ws: Workspace,
        blades: Blades,
        catalogs: CatalogService,
        templates: Sequence[Template],
        packages: Callable[[str], list[Package]],
        run_blade: Callable[..., Awaitable[dict[str, Any]]],
        install_packages: Callable[[list[str]], Awaitable[tuple[int | None, str]]],
        python: str,
        environ: Mapping[str, str],
    ) -> None:
        self.ws = ws
        self._blades = blades
        self._catalogs = catalogs
        self._templates = templates
        # The packages a blade's code needs, each marked installed here or not (adapters/blade_run.py).
        self._packages = packages
        # Run a blade in a child process: "catalog", "test" or "analysis" (server/blade_runner.py).
        self._run_blade = run_blade
        self._install_packages = install_packages
        # SwissArmyGrid's own Python (where Refresh runs), and the environment suggestions come from.
        self._python = python
        self._environ = environ

    # ===== reading =====

    def get(self, name: str) -> Blade:
        return self._blades.get(name)

    def summary(
        self, blade: Blade, everything: list[Blade] | None = None, pinned: set[str] | None = None
    ) -> dict[str, Any]:
        vaults, typed = self._blades.providers(blade, everything)
        loaded = self._catalogs.loaded_names()
        favourites = self._blades.pinned() if pinned is None else pinned
        return {
            "name": blade.name,
            "kind": blade.kind,
            "dialect": blade.dialect,
            "variable": blade.variable,
            "has_catalog": blade.has_catalog,
            "catalog_loaded": blade.name.casefold() in loaded,
            "template": blade.template,
            # A database or file blade is a connection: listed apart (services/connections.py).
            "connection": connection_kind(blade.kind, blade.template),
            "secrets": self._blades.secret_status(blade),
            "vaults": [v.name for v in vaults],
            "typed": typed,
            "env_names": blade.env_names,
            "description": blade.description,
            "tags": blade.tags,
            "pinned": blade.name.casefold() in favourites,
            "source": "pack" if blade.pack else "folder" if blade.path else "saved",
            "pack": blade.pack,
            "path": blade.path,
            "updated_at": blade.updated_at,
        }

    def settings_of(
        self,
        code: str,
        template: str | None = None,
        name: str | None = None,
        everything: list[Blade] | None = None,
    ) -> list[dict[str, Any]]:
        """The values in the code each person fills in (domain/blades.py), with suggestions from the
        user's other blades. A path still as the starter wrote it (C:\\data\\orders.csv) is a placeholder."""
        try:
            found = rules.detect_settings(code)
        except BladeCodeError:
            return []
        starter = next((t for t in self._templates if t.id == template), None) if template else None
        if starter is not None:
            examples = {
                (s.key, str(s.value)) for s in rules.detect_settings(starter.code) if s.name == "path"
            }
            for setting in found:
                if (setting.key, str(setting.value)) in examples:
                    setting.placeholder = True
        mine = (name or "").casefold()
        others = [
            (b.name, b.dialect, b.code)
            for b in (everything or self._blades.all())
            if b.code and b.name.casefold() != mine
        ]
        return [s.to_json() for s in rules.suggest_settings(found, others, self._environ)]

    def inspect(self, code: str, template: str | None = None, name: str | None = None) -> dict[str, Any]:
        """What code makes, asks for, imports and needs (nothing saved or run)."""
        found = rules.read_code(code, "The code")
        packages = self._packages(code)
        missing = [p.pip for p in packages if not p.installed]
        return {
            "variable": found.variable,
            "assigned": found.assigned,
            "secrets": found.secrets,
            "imports": found.imports,
            "import_lines": rules.import_lines(code),
            "packages": [p.to_json() for p in packages],
            # Into SwissArmyGrid's own Python (where Refresh runs), and the same for the user's own.
            "install_here": install_command(missing, self._python) if missing else None,
            "install_yours": install_command([p.pip for p in packages]) if packages else None,
            "settings": self.settings_of(code, template, name),
        }

    def listing(self) -> dict[str, Any]:
        everything = self._blades.all()
        pinned = self._blades.pinned()
        found = [self.summary(b, everything, pinned) for b in everything]
        # A connection's variable in the Workbench (named in a fixed order, so a name never moves).
        named = code_names(sorted((str(b["name"]) for b in found if b["connection"]), key=str.casefold))
        return {"blades": [b | {"code_name": named.get(str(b["name"]))} for b in found]}

    def environment_names(self) -> list[str]:
        """The environment variables blades read or set: vaults' entries, secret blades' variables, and the
        names connections and blades ask secret() for. Names only (the Variables overlay says set or not)."""
        names: set[str] = set()
        for blade in self._blades.all():
            names.update(blade.vault_mapping)
            names.update(blade.env_names)
            names.update(blade.secrets)
        return sorted(names)

    def templates(self) -> dict[str, Any]:
        return {"templates": [t.to_json() for t in self._templates]}

    def details(self, name: str) -> dict[str, Any]:
        blade = self._blades.get(name)
        details = blade.to_json() | self.summary(blade)
        if blade.code:
            details["inspect"] = self.inspect(blade.code, blade.template, blade.name)
        return details

    def fill(self, code: str, values: dict[str, Any], template: Any, name: Any) -> dict[str, Any]:
        """Write new values into a blade's code at its settings (by the ids `settings` gave for this
        same code). Returns the new code and its settings, read again."""
        changed = rules.apply_settings(code, values)
        return {
            "code": changed,
            "settings": self.settings_of(
                changed,
                template if isinstance(template, str) else None,
                name if isinstance(name, str) else None,
            ),
        }

    # ===== saving =====

    async def save(self, body: Any, previous: str | None) -> dict[str, Any]:
        blade = self._blades.save(body, previous)
        if isinstance(body, dict) and isinstance(body.get("secrets"), dict) and body["secrets"]:
            self._blades.set_secrets(blade.name, body["secrets"])
        self.ws.audit.write("user", "save_blade", blade.name)
        await self.ws.live.broadcast({"type": "blades_changed"})
        return self.summary(blade)

    async def remove(self, name: str) -> dict[str, Any]:
        self._blades.remove(name)
        self.ws.audit.write("user", "remove_blade", name)
        await self.ws.live.broadcast({"type": "blades_changed"})
        return {"removed": name}

    def set_secrets(self, name: str, body: Any) -> dict[str, Any]:
        """Save or forget its secrets (in the operating system's vault)."""
        return {"secrets": self._blades.set_secrets(name, body)}

    # ===== running =====

    def runnable(self, name: str, mode: str) -> Blade:
        """The blade, when it can be run as asked: a Refresh needs a catalog command, a Test code."""
        blade = self._blades.get(name)
        if mode == "catalog" and not blade.has_catalog:
            raise BladeError(f"{blade.name} has no catalog command. Open it and add one under Catalog.")
        if mode == "test" and blade.kind == "secret":
            raise BladeError("A secret blade has no code to test; its values are typed when used.")
        return blade

    def _typed_values(self, blade: Blade, body: Any) -> dict[str, str]:
        """The values the user typed for secret blades; NeedsValues names what is still needed."""
        typed = body.get("env", {}) if isinstance(body, dict) else {}
        if not isinstance(typed, dict) or not all(isinstance(v, str) for v in typed.values()):
            raise ValueError('Send typed values as {"env": {"NAME": "value"}}.')
        _, wanted = self._blades.providers(blade)
        missing = {env: source for env, source in wanted.items() if env not in typed}
        if missing:
            raise NeedsValues(missing)
        return {k: v for k, v in typed.items() if k in wanted}

    async def refresh(self, blade: Blade, body: Any) -> dict[str, Any]:
        """Run it and its catalog command in a child process; load the catalog."""
        typed = self._typed_values(blade, body)
        answer = await self._run_blade(blade, "catalog", typed)
        info = await self._catalogs.load_from_blade(
            blade.name, answer["columns"], answer["rows"], blade.dialect
        )
        self.ws.audit.write("user", "refresh_blade", blade.name)
        await self._catalogs.blade_refreshed()
        return {"catalog": info.to_json()}

    async def test(self, blade: Blade, body: Any) -> dict[str, Any]:
        """Run it in a child process: does it work?"""
        return await self._run_blade(blade, "test", self._typed_values(blade, body))

    async def test_draft(self, data: Any) -> dict[str, Any]:
        """Run code not saved yet (the Add connection form's Test) with the passwords typed into the form
        (`data["secrets"]`), in a child process. Nothing is saved, the passwords included."""
        blade = rules.parse_blade(data)
        typed = data.get("secrets") if isinstance(data, dict) else None
        if typed is not None and (
            not isinstance(typed, dict) or not all(isinstance(v, str) for v in typed.values())
        ):
            raise ValueError('Send the typed passwords as {"secrets": {"name": "value"}}.')
        values = {k: v for k, v in (typed or {}).items() if v}
        return await self._run_blade(blade, "test", self._typed_values(blade, data), {"secrets": values})

    # ===== analysis blades: the user's own analyses in the Analyze menu =====

    def analyses(self) -> dict[str, Any]:
        """The analysis blades, for the Analyze menu: name, description, favourite."""
        found = [b for b in self._blades.all() if b.kind == "analysis"]
        return {"analyses": [{"name": b.name, "description": b.description, "tags": b.tags} for b in found]}

    async def analyze(
        self, name: str, grid: Grid, info: DatasetInfo, body: dict[str, Any], who: str
    ) -> dict[str, Any]:
        """Run the analysis blade `name` on the grid's rows as it shows them (its steps), in a child
        process: {"title", "cards"}, ready for the Sandbox."""
        blade = self._blades.get(name)
        if blade.kind != "analysis":
            raise BladeError(f"{blade.name} is not an analysis blade.")
        columns = body.get("columns") or []
        if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
            raise BladeError("columns is a list of the grid's column names.")
        ws = self.ws
        limit = ws.settings.max_analysis_rows

        def read(cur: SqlSession) -> Any:
            with ws.views.reading(cur, info, grid.steps, grid.settings) as rows:
                how_many, every = whole_view_queries(f"({rows}) AS src")
                counted = cur.execute(how_many).fetchone()
                if counted and int(counted[0]) > limit:
                    raise BladeError(
                        f"{counted[0]:,} rows is more than an analysis blade takes ({limit:,}): "
                        "filter the grid first."
                    )
                return cur.execute(every).to_arrow_table()

        table = await ws.grid_query(grid, None, BACKGROUND, read)
        typed = self._typed_values(blade, body)
        answer = await self._run_blade(blade, "analysis", typed, {"table": table, "columns": columns})
        ws.audit.write(who, "blade_analysis", info.name, blade=blade.name)
        return {
            "title": answer.get("title") or blade.name,
            "cards": answer.get("cards", []),
            "blade": blade.name,
        }

    def run_failure(self, message: str, missing_module: str | None) -> dict[str, Any]:
        """What the page shows for a failed run: the message, and the install when a module is missing."""
        content: dict[str, Any] = {"error": message}
        if missing_module:
            pip, _ = pip_name(missing_module)
            content |= {"missing": pip, "install_here": install_command([pip], self._python)}
        return content

    def for_python(self, name: str) -> dict[str, Any]:
        """What sag.blade() runs in the caller's Python. Secret values included: only the user's own
        Python asks (the token is the user's; the agent is refused)."""
        blade = self._blades.get(name)
        vaults, typed = self._blades.providers(blade)
        return {
            "name": blade.name,
            "kind": blade.kind,
            "code": blade.code,
            "variable": blade.variable,
            "secrets": self._blades.saved_secrets(blade),
            "vault_mapping": blade.vault_mapping,
            "env_names": blade.env_names,
            "typed": typed,
            "vaults": [
                {
                    "label": v.name,
                    "code": v.code,
                    "variable": v.variable,
                    "mapping": v.vault_mapping,
                    "secrets": self._blades.saved_secrets(v),
                }
                for v in vaults
            ],
        }

    async def install(self, packages: Any) -> tuple[bool, dict[str, Any]]:
        """Install packages into SwissArmyGrid's own Python, where Refresh runs, on the user's click.
        Returns whether it worked, and what to show."""
        if (
            not isinstance(packages, list)
            or not packages
            or len(packages) > 20
            or not all(isinstance(p, str) and _PIP_NAME.fullmatch(p) for p in packages)
        ):
            raise ValueError("Name 1 to 20 packages as pip knows them, such as pyodbc or psycopg[binary].")
        code, text = await self._install_packages(packages)
        if code is None:
            return False, {"ok": False, "output": "The install took over 10 minutes and was stopped."}
        self.ws.audit.write("user", "install_packages", ", ".join(packages))
        tail = "\n".join(text.strip().splitlines()[-15:])
        if code != 0:
            return False, {
                "ok": False,
                "output": tail,
                "command": install_command(packages, self._python),
            }
        return True, {"ok": True, "output": tail}

    # ===== favourites, blade folders, export and import (portable blades) =====

    async def pin(self, name: str, pinned: bool) -> dict[str, Any]:
        """A favourite, or not (kept on this computer)."""
        now = self._blades.set_pinned(name, pinned)
        await self.ws.live.broadcast({"type": "blades_changed"})
        return {"pinned": now}

    async def set_folders(self, folders: Any) -> None:
        self._blades.set_folders(folders)
        self.ws.audit.write("user", "blade_folders")
        await self.ws.live.broadcast({"type": "blades_changed"})

    def folders(self) -> dict[str, Any]:
        """The blade folders searched for blade files, and files that failed."""
        listed = self._blades.folders()
        everything = self._blades.all()  # also refreshes the list of files that could not be read
        for entry in listed:
            root = entry["path"]
            entry["blades"] = sum(1 for b in everything if b.path and b.path.startswith(root))
        return {"folders": listed, "problems": self._blades.problems}

    def export_one(self, name: str) -> tuple[str, str]:
        """The blade as a portable .blade.py file: its text and its file name."""
        blade = self._blades.get(name)
        return rules.write_blade(blade.to_json()), rules.file_name(blade.name)

    def export_zip(self, wanted: set[str]) -> bytes:
        """Several (or all) as a zip of portable files; `wanted` are names, casefolded."""
        chosen = [b.to_json() for b in self._blades.all() if not wanted or b.name.casefold() in wanted]
        if not chosen:
            raise LookupError("No blades to export.")
        return rules.write_zip(chosen)

    async def import_blades(self, body: dict[str, Any]) -> dict[str, Any]:
        """Import portable blades in two steps. Without "apply": what each file holds (name, kind,
        description, tags, its full code, the secrets it will ask for) and whether its name is taken, so
        the person reviews it; nothing is saved and nothing runs. With "apply": the choice for each
        (save, replace, copy or skip), saved in the app or into a blade folder."""
        blades = self._blades
        files = body.get("files")
        if not isinstance(files, list) or not 0 < len(files) <= 50:
            raise ValueError("Send 1 to 50 files.")
        decoded: list[tuple[str, bytes]] = []
        for entry in files:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise ValueError('Each file is {"name": "...", "data": "<base64>"}.')
            try:
                decoded.append(
                    (entry["name"][:200], base64.b64decode(str(entry.get("data", "")), validate=True))
                )
            except (binascii.Error, ValueError):
                raise ValueError(f"{entry['name']} did not arrive whole (not base64).") from None
        found = rules.read_blades(decoded)
        existing = {b.name.casefold(): b for b in blades.all()}
        if not body.get("apply"):
            preview = []
            for index, item in enumerate(found):
                row: dict[str, Any] = {"index": index, "file": item.file, "error": item.error}
                if item.blade is not None:
                    taken = existing.get(str(item.blade["name"]).casefold())
                    row |= {
                        "blade": item.blade,
                        "secrets": rules.read_code(item.blade.get("code") or "").secrets
                        if item.blade.get("code")
                        else [],
                        "conflict": {
                            "name": taken.name,
                            "source": "pack" if taken.pack else "folder" if taken.path else "saved",
                        }
                        if taken
                        else None,
                        "settings": self.settings_of(
                            str(item.blade.get("code") or ""),
                            item.blade.get("template"),
                            everything=list(existing.values()),
                        ),
                    }
                preview.append(row)
            return {"items": preview}
        choices = body.get("choices") or {}
        fills: dict[str, Any] = body["fill"] if isinstance(body.get("fill"), dict) else {}
        folder = body.get("folder") or None
        results = []
        for index, item in enumerate(found):
            choice = str(choices.get(str(index), "save")) if isinstance(choices, dict) else "save"
            if item.blade is None:
                results.append({"file": item.file, "status": "error", "message": item.error})
                continue
            data = dict(item.blade)
            taken = existing.get(str(data["name"]).casefold())
            entry = fills.get(str(index))
            filled: dict[str, Any] = entry if isinstance(entry, dict) else {}
            values: dict[str, Any] = filled["settings"] if isinstance(filled.get("settings"), dict) else {}
            typed: dict[str, Any] = filled["secrets"] if isinstance(filled.get("secrets"), dict) else {}
            try:
                if choice == "skip":
                    results.append({"file": item.file, "name": data["name"], "status": "skipped"})
                    continue
                if values and data.get("code"):  # the person's own values, written into the code
                    data["code"] = rules.apply_settings(str(data["code"]), values)
                if taken is not None and choice == "replace":
                    saved = blades.save(data, previous_name=taken.name)
                else:
                    if taken is not None:  # keep both: the copy gets a free name
                        data["name"] = _free_name(str(data["name"]), set(existing))
                    saved = blades.save(data | ({"folder": folder} if folder else {}))
                existing[saved.name.casefold()] = saved
                result: dict[str, Any] = {
                    "file": item.file,
                    "name": saved.name,
                    "status": "saved",
                    "path": saved.path,
                }
                secrets_typed = {str(k): str(v) for k, v in typed.items() if v and k in saved.secrets}
                if secrets_typed:
                    try:
                        blades.set_secrets(saved.name, secrets_typed)
                    except VaultUnavailableError as error:
                        result["message"] = f"Saved, but its secrets were not kept: {error}"
                results.append(result)
            except (BladeError, ValueError) as error:
                results.append(
                    {"file": item.file, "name": data["name"], "status": "error", "message": str(error)}
                )
        self.ws.audit.write("user", "import_blades", ", ".join(str(r.get("name", "")) for r in results))
        await self.ws.live.broadcast({"type": "blades_changed"})
        return {"results": results}
