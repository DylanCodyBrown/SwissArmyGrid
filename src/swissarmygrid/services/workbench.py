"""The Workbench (Plans/workbench-and-connections.plan.md): notebooks and scripts, opened and saved,
and the IPython kernels that run them. The user and the agent alike (the owner's golden rule,
2026-11-14: whatever the user can do, the agent can do through the MCP, running code included); the
audit log names who did each."""

from __future__ import annotations

import secrets as tokens
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Protocol

from swissarmygrid.domain import environments as envs
from swissarmygrid.domain import errors
from swissarmygrid.domain import notebooks as rules
from swissarmygrid.services.workspace import Conflict, NotFound, Workspace

MAX_KERNELS: int = 12


class Documents(Protocol):
    default_folder: Any

    def read(self, path: str) -> tuple[str, str, float]: ...

    def modified(self, path: str) -> float | None: ...

    def write(self, path: str, text: str, force: bool = False) -> tuple[str, float]: ...

    def recent(self) -> list[dict[str, Any]]: ...

    def remember(self, path: str, when: float) -> None: ...

    def environment(self, path: str) -> str | None: ...

    def remember_environment(self, path: str, python: str) -> None: ...

    def scratch(self, text: str) -> str: ...


class Kernel(Protocol):
    python: str

    def execute(
        self, code: str, silent: bool = False, stdin: bool = True
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def complete(self, code: str, offset: int) -> dict[str, Any]: ...

    async def is_complete(self, code: str) -> dict[str, Any]: ...

    def answer_input(self, value: str) -> None: ...

    async def alive(self) -> bool: ...

    async def inspect(self, code: str, offset: int) -> str: ...

    async def variables(self, env_names: list[str]) -> dict[str, Any] | None: ...

    async def grid(self, name: str) -> str | None: ...

    async def interrupt(self) -> None: ...

    async def restart(self) -> None: ...

    async def shutdown(self) -> None: ...


class Kernels(Protocol):
    async def start(self, python: str, folder: str | None, environ: dict[str, str]) -> Kernel: ...

    def check_python(self, python: str) -> dict[str, Any]: ...

    def candidates(self, folders: list[str]) -> list[str]: ...

    def create_environment(
        self, folder: str, version: str | None, packages: list[str]
    ) -> AsyncIterator[dict[str, Any]]: ...

    def beside(self, folder: str) -> str | None: ...


class Workbench:
    def __init__(
        self,
        ws: Workspace,
        documents: Documents,
        kernels: Kernels,
        environ: Mapping[str, str],
        now: Callable[[], float],
        folder_of: Callable[[str], str],
        secret_names: Callable[[], list[str]] = list,
        vault_environment: Callable[[], Awaitable[tuple[dict[str, str], list[str]]]] | None = None,
    ) -> None:
        self.ws = ws
        self._documents = documents
        self._kernels = kernels
        self._environ = environ
        self._now = now
        self._folder_of = folder_of  # a file's folder (pathlib lives in the adapters)
        # The environment variables vaults, secret blades and connection passwords use (names only).
        self._secret_names = secret_names
        # Every vault's entries (variable -> value) and the vaults that failed, read as a kernel starts: the
        # values go into that kernel's environment and nowhere else (never in an answer, never saved).
        self._vault_environment = vault_environment
        # The variables the vaults set at the last kernel start (names only): a vault that loads every
        # secret names them only when it runs, and the Variables pane lists them.
        self._vault_names: set[str] = set()
        self._running: dict[str, Kernel] = {}

    # ===== files =====

    def home(self) -> dict[str, Any]:
        """Where Save as starts, and the recent files."""
        return {"folder": str(self._documents.default_folder), "recent": self._documents.recent()}

    def open(self, path: Any) -> dict[str, Any]:
        if not isinstance(path, str) or not path.strip():
            raise ValueError('Send {"path": "C:\\\\...\\\\analysis.ipynb"}.')
        kind = rules.kind_of(path)
        try:
            text, full, modified = self._documents.read(path)
        except FileNotFoundError as error:
            raise NotFound(str(error)) from None
        self._documents.remember(full, self._now())
        self.ws.audit.write("user", "open_document", full)
        # Its environment: the one chosen for it before, else a .venv beside it.
        python = self._documents.environment(full) or self._kernels.beside(self._folder_of(full))
        if kind == "notebook":
            cells = [c.to_json() for c in rules.read_notebook(text)]
            return {"path": full, "kind": kind, "modified": modified, "cells": cells, "python": python}
        return {"path": full, "kind": kind, "modified": modified, "text": text, "python": python}

    def save(self, body: Any) -> dict[str, Any]:
        """Save a notebook's cells or a script's text. A file changed on disk since it was opened is not
        overwritten unless the body says to (`"force": true`)."""
        if not isinstance(body, dict) or not isinstance(body.get("path"), str):
            raise ValueError('Send {"path": "...", "cells": [...]} or {"path": "...", "text": "..."}.')
        path = body["path"]
        kind = rules.kind_of(path)
        if kind == "notebook":
            text = rules.write_notebook(rules.read_cells(body.get("cells")))
        else:
            script = body.get("text")
            if not isinstance(script, str) or len(script) > rules.MAX_CELL_CHARS * 5:
                raise ValueError("Send the script's text (at most five million characters).")
            text = script
        opened = body.get("modified")
        on_disk = self._documents.modified(path)
        if (
            on_disk is not None
            and isinstance(opened, int | float)
            and on_disk > opened + 0.001
            and not body.get("force")
        ):
            raise Conflict(f"{path} changed on disk since it was opened here. Save anyway to replace it.")
        try:
            full, modified = self._documents.write(path, text, force=bool(body.get("force")))
        except FileNotFoundError as error:
            raise NotFound(str(error)) from None
        except PermissionError as error:
            raise Conflict(str(error)) from None
        self._documents.remember(full, self._now())
        self.ws.audit.write("user", "save_document", full)
        return {"path": full, "modified": modified}

    # ===== environments: a notebook's own Python =====

    def environments(self) -> dict[str, Any]:
        """What the New environment dialog offers: libraries by group, presets, Python versions."""
        return envs.catalog()

    def create_environment(self, body: Any) -> AsyncIterator[dict[str, Any]]:
        """Make a virtual environment with the kernel and the libraries chosen; its progress as it goes.
        When done, it is remembered for the document named in `body["document"]` (if any)."""
        folder, version, packages = envs.check_request(body)
        document = body.get("document") if isinstance(body.get("document"), str) else None
        self.ws.audit.write("user", "create_environment", f"{folder} ({', '.join(packages)})")

        async def run() -> AsyncIterator[dict[str, Any]]:
            async for event in self._kernels.create_environment(folder, version, packages):
                done = event.get("done")
                if done and done.get("ok") and document:
                    self._documents.remember_environment(document, str(done["python"]))
                yield event

        return run()

    def choose_environment(self, body: Any) -> dict[str, Any]:
        """Remember the Python chosen for a document (the Workbench's Python picker)."""
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("path"), str)
            or not isinstance(body.get("python"), str)
        ):
            raise ValueError('Send {"path": "the document", "python": "its Python"}.')
        checked = self._kernels.check_python(body["python"])
        self._documents.remember_environment(body["path"], str(checked["python"]))
        return checked

    # ===== kernels =====

    def pythons(self) -> dict[str, Any]:
        """The Pythons a kernel can run in: this server's own first, then projects' environments."""
        folders = [self._folder_of(r["path"]) for r in self._documents.recent()]
        found = []
        for python in self._kernels.candidates(folders):
            try:
                found.append(self._kernels.check_python(python))
            except ValueError:  # not a Python that runs here: left out
                continue
        return {"pythons": found}

    async def start(self, body: Any) -> dict[str, Any]:
        """A kernel for one document, in the Python chosen (this server's own when none is)."""
        if not isinstance(body, dict):
            body = {}
        await self._prune()
        if len(self._running) >= MAX_KERNELS:
            raise Conflict(f"{MAX_KERNELS} kernels are running: close a notebook or script first.")
        python = body.get("python") or self.pythons()["pythons"][0]["python"]
        path = body.get("path")
        folder = self._folder_of(path) if isinstance(path, str) and path else None
        from_vaults: dict[str, str] = {}
        problems: list[str] = []
        if self._vault_environment is not None:
            from_vaults, problems = await self._vault_environment()
        kernel = await self._kernels.start(str(python), folder, dict(self._environ) | from_vaults)
        kernel_id = tokens.token_hex(6)
        self._running[kernel_id] = kernel
        self.ws.audit.write("user", "start_kernel", str(python))
        self._vault_names |= set(from_vaults)
        if from_vaults:
            self.ws.audit.write("user", "vault_variables", ", ".join(sorted(from_vaults)))
        # Names only: which variables the vaults set, and why a vault could not.
        vaults = {"set": sorted(from_vaults), "problems": problems}
        return {"kernel": kernel_id, "python": kernel.python, "vaults": vaults}

    def _kernel(self, kernel_id: str) -> Kernel:
        kernel = self._running.get(kernel_id)
        if kernel is None:
            raise NotFound("That kernel is not running (the server restarted, or it was closed). Run again.")
        return kernel

    def execute(self, kernel_id: str, body: Any) -> AsyncIterator[dict[str, Any]]:
        """Run code (kind code), a SQL cell (kind sql, its connection) or a script file (kind file: `path`
        of a saved script, or `code` of an unsaved one, run with %run: its own __main__, __file__ and
        sys.argv, as `python script.py` would). A kernel that dies is dropped."""
        if not isinstance(body, dict):
            raise ValueError('Send {"code": "..."}.')
        kind = body.get("kind") or "code"
        if kind == "file":
            path = body.get("path")
            if not isinstance(path, str) or not path:
                if not isinstance(body.get("code"), str):
                    raise ValueError("A file run sends the script's path, or its code when it is not saved.")
                path = self._documents.scratch(body["code"])
            args = body.get("args") if isinstance(body.get("args"), str) else ""
            line = rules.quote_path(path) + (" " + args if args else "")
            code = f"get_ipython().run_line_magic('run', {line!r})"
        elif not isinstance(body.get("code"), str):
            raise ValueError('Send {"code": "..."}.')
        elif kind == "sql":
            connection = body.get("connection")
            named = connection if isinstance(connection, str) else None
            code = rules.code_of(rules.Cell("sql", body["code"], named))
        else:
            code = body["code"]
        kernel = self._kernel(kernel_id)

        stdin = body.get("stdin") is not False

        async def run() -> AsyncIterator[dict[str, Any]]:
            usage = ""  # a SQL cell's error is IPython's usage error, written to stderr
            async for item in kernel.execute(code, stdin=stdin):
                if "error_detail" in item:
                    item = {"error_detail": errors.explain_detail(item["error_detail"])}
                elif item.get("output_type") == "stream" and item.get("name") == "stderr":
                    usage = (usage + str(item.get("text", "")))[-20_000:]
                if "done" in item and kind == "sql" and "UsageError" in usage:
                    message = usage[usage.index("UsageError") :].split(":", 1)[-1].strip()
                    entry = sql_error_entry(message)
                    yield {"error_detail": errors.explain_detail({"chain": [entry]})}
                if item.get("done", {}).get("dead"):
                    await self.stop(kernel_id)  # gone: it no longer counts against the limit
                yield item

        return run()

    async def _prune(self) -> None:
        """Drop the kernels that died (out of memory, os._exit) without saying so."""
        for kernel_id, kernel in list(self._running.items()):
            if not await kernel.alive():
                await self.stop(kernel_id)

    def answer_input(self, kernel_id: str, body: Any) -> dict[str, Any]:
        """The line typed for input(), getpass or pdb."""
        value = body.get("value") if isinstance(body, dict) else None
        if not isinstance(value, str) or len(value) > 100_000:
            raise ValueError('Send {"value": "the line typed"}.')
        self._kernel(kernel_id).answer_input(value)
        return {"ok": True}

    async def is_complete(self, kernel_id: str, body: Any) -> dict[str, Any]:
        """Whether the console's code is a whole statement yet (Enter runs it, or goes to a new line)."""
        if not isinstance(body, dict) or not isinstance(body.get("code"), str):
            raise ValueError('Send {"code": "..."}.')
        return await self._kernel(kernel_id).is_complete(body["code"])

    @staticmethod
    def statement(body: Any) -> dict[str, Any]:
        """The whole statement at a script's line (Ctrl+Enter runs it, then the cursor moves to the next)."""
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("code"), str)
            or not isinstance(body.get("line"), int)
        ):
            raise ValueError('Send {"code": "...", "line": n} (the first line is 1).')
        if len(body["code"]) > rules.MAX_CELL_CHARS:
            raise ValueError("The code is over a million characters.")
        found = rules.statement_at(body["code"], body["line"])
        if found is None:
            return {"start": None, "end": None, "next": None}
        start, end, after = found
        return {"start": start, "end": end, "next": after}

    @staticmethod
    def _code_at(body: Any) -> tuple[str, int]:
        if not isinstance(body, dict) or not isinstance(body.get("code"), str):
            raise ValueError('Send {"code": "...", "offset": n}.')
        code: str = body["code"]
        offset = body.get("offset")
        if not isinstance(offset, int) or not 0 <= offset <= len(code) or len(code) > rules.MAX_CELL_CHARS:
            raise ValueError("The offset is a place in the code (0 to its length).")
        return code, offset

    async def complete(self, kernel_id: str, body: Any) -> dict[str, Any]:
        """The kernel's completions at a place in the code (Python; a %%sql cell's names too)."""
        code, offset = self._code_at(body)
        return await self._kernel(kernel_id).complete(code, offset)

    async def inspect(self, kernel_id: str, body: Any) -> dict[str, Any]:
        """The signature and help of the name at a place in the code."""
        code, offset = self._code_at(body)
        return {"help": await self._kernel(kernel_id).inspect(code, offset)}

    async def variables(self, kernel_id: str) -> dict[str, Any]:
        """The Variables overlay: every variable, the connections and blades, the environment's names
        (values hidden). `busy`: the kernel is running code; ask again when it is done."""
        names = sorted(set(self._secret_names()) | self._vault_names)
        found = await self._kernel(kernel_id).variables(names)
        return {"busy": True} if found is None else found | {"busy": False}

    async def grid(self, kernel_id: str, body: Any) -> dict[str, Any]:
        """Open a table variable as a grid."""
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError('Send {"name": "a variable\'s name"}.')
        url = await self._kernel(kernel_id).grid(name)
        if url is None:
            raise Conflict("The kernel is running code: open it as a grid when the run is done.")
        self.ws.audit.write("user", "variable_to_grid", name)
        return {"url": url}

    async def interrupt(self, kernel_id: str) -> None:
        await self._kernel(kernel_id).interrupt()

    async def restart(self, kernel_id: str) -> None:
        await self._kernel(kernel_id).restart()

    async def stop(self, kernel_id: str) -> None:
        kernel = self._running.pop(kernel_id, None)
        if kernel is not None:
            await kernel.shutdown()

    async def stop_all(self) -> None:
        for kernel_id in list(self._running):
            await self.stop(kernel_id)


def sql_error_entry(message: str) -> dict[str, Any]:
    """A SQL cell's error (IPython's usage error) as an error entry with every field the kernel's own
    entries carry (client/kernel_errors.py), so the page's error panel reads it as it reads a Python one."""
    return {
        "type": "UsageError",
        "module": "",
        "message": message,
        "hint": "",
        "relation": None,
        "notes": [],
        "facts": {},
        "syntax": None,
        "frames": [],
        "group": [],
        "your_frame": None,
    }
