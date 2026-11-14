"""The Workbench (Plans/workbench-and-connections.plan.md): notebooks and scripts, opened and saved on
the user's click, and the IPython kernels that run them. The user's alone: the agent is refused at the
door (milestone 9, an agent's code sandbox, stays unbuilt)."""

from __future__ import annotations

import secrets as tokens
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Protocol

from swissarmygrid.domain import environments as envs
from swissarmygrid.domain import notebooks as rules
from swissarmygrid.services.workspace import Conflict, NotFound, Workspace

MAX_KERNELS: int = 12


class Documents(Protocol):
    default_folder: Any

    def read(self, path: str) -> tuple[str, str, float]: ...

    def modified(self, path: str) -> float | None: ...

    def write(self, path: str, text: str) -> tuple[str, float]: ...

    def recent(self) -> list[dict[str, Any]]: ...

    def remember(self, path: str, when: float) -> None: ...

    def environment(self, path: str) -> str | None: ...

    def remember_environment(self, path: str, python: str) -> None: ...


class Kernel(Protocol):
    python: str

    def execute(self, code: str, silent: bool = False) -> AsyncIterator[dict[str, Any]]: ...

    async def complete(self, code: str, offset: int) -> dict[str, Any]: ...

    async def inspect(self, code: str, offset: int) -> str: ...

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
    ) -> None:
        self.ws = ws
        self._documents = documents
        self._kernels = kernels
        self._environ = environ
        self._now = now
        self._folder_of = folder_of  # a file's folder (pathlib lives in the adapters)
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
            full, modified = self._documents.write(path, text)
        except FileNotFoundError as error:
            raise NotFound(str(error)) from None
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
        if len(self._running) >= MAX_KERNELS:
            raise Conflict(f"{MAX_KERNELS} kernels are running: close a notebook or script first.")
        python = body.get("python") or self.pythons()["pythons"][0]["python"]
        path = body.get("path")
        folder = self._folder_of(path) if isinstance(path, str) and path else None
        kernel = await self._kernels.start(str(python), folder, dict(self._environ))
        kernel_id = tokens.token_hex(6)
        self._running[kernel_id] = kernel
        self.ws.audit.write("user", "start_kernel", str(python))
        return {"kernel": kernel_id, "python": kernel.python}

    def _kernel(self, kernel_id: str) -> Kernel:
        kernel = self._running.get(kernel_id)
        if kernel is None:
            raise NotFound("That kernel is not running (the server restarted, or it was closed). Run again.")
        return kernel

    def execute(self, kernel_id: str, body: Any) -> AsyncIterator[dict[str, Any]]:
        if not isinstance(body, dict) or not isinstance(body.get("code"), str):
            raise ValueError('Send {"code": "..."}.')
        if body.get("kind") == "sql":
            connection = body.get("connection")
            code = rules.code_of(
                rules.Cell("sql", body["code"], connection if isinstance(connection, str) else None)
            )
        else:
            code = body["code"]
        return self._kernel(kernel_id).execute(code)

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
