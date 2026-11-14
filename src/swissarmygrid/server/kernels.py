"""IPython kernels for the Workbench (jupyter_client, approved 2026-11-13): one per open notebook or script,
started on the user's first Run, in the Python they chose. Each kernel runs as its own process on this
computer; jupyter_client tells it this server's process id, so it ends when the server does.

`execute` yields the run's outputs as they arrive, as nbformat's own output records (stream,
execute_result, display_data, error), then a last {"done": ...}. One run at a time per kernel: a second
waits for the first (as Jupyter queues cells).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from jupyter_client.kernelspec import KernelSpec
from jupyter_client.manager import AsyncKernelManager

_START_SECONDS: float = 60.0
# How long a completion or hover waits: the first on a big library (pandas) can take 4 s.
_ASK_SECONDS: float = 8.0
_MAX_MATCHES: int = 300
_MAX_HELP: int = 6_000
_IDLE_POLL_SECONDS: float = 0.5
# Run quietly once a kernel is ready: the package (sag.*, the %%sql cells, tables as grids) when that
# Python has it. A Python without it still runs code; the Workbench says how to add it.
_SETUP = (
    "try:\n"
    "    import swissarmygrid as sag\n"
    "    from swissarmygrid.client.kernel_connections import install as _sag_connections\n"
    "\n"
    "    _sag_connections()\n"
    "    del _sag_connections\n"
    "except ImportError:\n"
    "    pass\n"
)


class KernelError(ValueError):
    """A kernel that cannot start or run (the message says why and what to do)."""


def check_python(python: str) -> dict[str, Any]:
    """Whether a Python can run a kernel: its version, and whether it has ipykernel and swissarmygrid."""
    path = Path(python).expanduser()
    if not path.is_file():
        raise KernelError(f"{python} is not a Python on this computer.")
    probe = (
        "import sys, importlib.util as u;"
        "print(sys.version.split()[0]);"
        "print(bool(u.find_spec('ipykernel')));"
        "print(bool(u.find_spec('swissarmygrid')))"
    )
    try:
        done = subprocess.run(  # noqa: S603 (a Python the user chose, asked three fixed questions)
            [str(path), "-c", probe], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise KernelError(f"{python} did not answer: {error}") from None
    lines = done.stdout.split()
    if done.returncode != 0 or len(lines) != 3:
        raise KernelError(f"{python} is not a Python that runs: {done.stderr.strip()[-300:]}")
    return {
        "python": str(path),
        "version": lines[0],
        "ipykernel": lines[1] == "True",
        "swissarmygrid": lines[2] == "True",
    }


def candidates(folders: list[str]) -> list[str]:
    """Pythons worth offering: this server's own, then a project's .venv beside recent files."""
    found = [sys.executable]
    for folder in folders:
        for parent in [Path(folder), *Path(folder).parents][:4]:
            for rel in ("Scripts/python.exe", "bin/python"):
                candidate = parent / ".venv" / rel
                if candidate.is_file() and str(candidate) not in found:
                    found.append(str(candidate))
    return found[:12]


class Kernel:
    def __init__(self, manager: AsyncKernelManager, python: str, sockets: str | None = None) -> None:
        self.manager = manager
        self.client = manager.client()
        self.python = python
        self._sockets = sockets  # the private folder of its local sockets, removed when it ends
        self._lock = asyncio.Lock()
        self._shell = asyncio.Lock()  # one completion or hover at a time reads the shell channel's replies
        self._warm: asyncio.Task[Any] | None = None

    async def ready(self) -> None:
        self.client.start_channels()
        try:
            await self.client.wait_for_ready(timeout=_START_SECONDS)
        except RuntimeError as error:
            raise KernelError(f"The kernel did not start: {error}") from None
        async for _ in self.execute(_SETUP, silent=True):
            pass
        # The first completion loads Jedi (about 3 seconds): do it now, so the user's first Tab is quick.
        self._warm = asyncio.create_task(self.complete("im", 2))

    async def execute(self, code: str, silent: bool = False) -> AsyncIterator[dict[str, Any]]:
        async with self._lock:
            msg_id = self.client.execute(code, silent=silent, store_history=not silent, allow_stdin=False)
            count: int | None = None
            ok = True
            while True:
                try:
                    msg = await self.client.get_iopub_msg(timeout=_IDLE_POLL_SECONDS)
                except Exception:  # queue.Empty: nothing yet; is the kernel still alive?
                    if not await self.manager.is_alive():
                        yield {"done": {"ok": False, "dead": True, "execution_count": count}}
                        return
                    continue
                if msg.get("parent_header", {}).get("msg_id") != msg_id:
                    continue
                kind, content = msg["msg_type"], msg["content"]
                if kind == "status" and content.get("execution_state") == "idle":
                    break
                if kind == "execute_input":
                    count = content.get("execution_count")
                elif kind == "stream":
                    yield {"output_type": "stream", "name": content["name"], "text": content["text"]}
                elif kind in ("execute_result", "display_data"):
                    record = {
                        "output_type": kind,
                        "data": content.get("data", {}),
                        "metadata": content.get("metadata", {}),
                    }
                    if kind == "execute_result":
                        record["execution_count"] = content.get("execution_count")
                    yield record
                elif kind == "error":
                    ok = False
                    yield {
                        "output_type": "error",
                        "ename": content.get("ename", ""),
                        "evalue": content.get("evalue", ""),
                        "traceback": content.get("traceback", []),
                    }
                elif kind == "clear_output":
                    yield {"clear": True}
            yield {"done": {"ok": ok, "execution_count": count}}

    async def _ask(self, send: Any) -> dict[str, Any] | None:
        """Send one request (`send()` gives its id) and wait for the shell channel's reply to it (a run's
        own replies are passed over)."""
        async with self._shell:
            return await self._reply(send())

    async def _reply(self, msg_id: str) -> dict[str, Any] | None:
        deadline = asyncio.get_running_loop().time() + _ASK_SECONDS
        while (left := deadline - asyncio.get_running_loop().time()) > 0:
            try:
                msg = await self.client.get_shell_msg(timeout=left)
            except Exception:  # queue.Empty: no answer in time (the kernel is busy running code)
                return None
            if msg.get("parent_header", {}).get("msg_id") == msg_id:
                return dict(msg["content"])
        return None

    async def complete(self, code: str, offset: int) -> dict[str, Any]:
        """What the kernel offers at `offset` (IPython's completer, with Jedi): the matches, the span they
        replace, and each one's kind when the kernel says. Nothing while it is busy running."""
        content = await self._ask(lambda: self.client.complete(code, offset))
        if not content or content.get("status") != "ok":
            return {"matches": [], "start": offset, "end": offset, "types": {}}
        typed = content.get("metadata", {}).get("_jupyter_types_experimental", [])
        return {
            "matches": content.get("matches", [])[:_MAX_MATCHES],
            "start": content.get("cursor_start", offset),
            "end": content.get("cursor_end", offset),
            "types": {t.get("text"): t.get("type") for t in typed if isinstance(t, dict)},
        }

    async def inspect(self, code: str, offset: int) -> str:
        """The signature and help of the name at `offset`, as plain text (empty when there is none)."""
        content = await self._ask(lambda: self.client.inspect(code, offset, detail_level=0))
        if not content or not content.get("found"):
            return ""
        return str(content.get("data", {}).get("text/plain", ""))[:_MAX_HELP]

    async def interrupt(self) -> None:
        await self.manager.interrupt_kernel()

    async def restart(self) -> None:
        await self.manager.restart_kernel(now=True)
        self.client.stop_channels()
        self.client = self.manager.client()
        await self.ready()

    async def shutdown(self) -> None:
        self.client.stop_channels()
        try:
            await self.manager.shutdown_kernel(now=True)
        except Exception:  # noqa: S110 (already gone)
            pass
        if self._sockets:
            shutil.rmtree(self._sockets, ignore_errors=True)


async def start(python: str, folder: str | None, environ: dict[str, str]) -> Kernel:
    """A kernel in `python`, its working folder `folder` (the document's), ready to run."""
    found = check_python(python)
    if not found["ipykernel"]:
        raise KernelError(
            f"{python} has no ipykernel, which runs the Workbench's code. Install it there: "
            f"uv pip install --python {python} ipykernel"
        )
    manager = AsyncKernelManager()
    sockets: str | None = None
    if sys.platform != "win32":
        # Local files (Unix sockets), not network ports: only this user reaches the kernel. They are
        # named by a full path in a private folder (a relative name would be read from the kernel's
        # working folder, the document's, and the server's own: two places). Windows has no such
        # transport; there its ports listen on 127.0.0.1 and every message is signed.
        sockets = tempfile.mkdtemp(prefix="swissarmygrid-kernel-")
        manager.transport = "ipc"
        manager.ip = os.path.join(sockets, "kernel")
    manager._kernel_spec = KernelSpec(  # one kernel's command: this Python, no kernelspec file needed
        argv=[found["python"], "-m", "ipykernel_launcher", "-f", "{connection_file}"],
        display_name="Python",
        language="python",
        env={},
    )
    cwd = folder if folder and os.path.isdir(folder) else str(Path.home())
    if not found["swissarmygrid"]:
        # This Python lacks the package: lend it a copy (the plain client) on its import path.
        from swissarmygrid.server.environments import client_path

        environ = environ | {
            "PYTHONPATH": os.pathsep.join(p for p in (client_path(), environ.get("PYTHONPATH", "")) if p)
        }
    try:
        await manager.start_kernel(cwd=cwd, env=environ)
    except Exception as error:
        if sockets:
            shutil.rmtree(sockets, ignore_errors=True)
        raise KernelError(f"The kernel did not start: {error}") from None
    kernel = Kernel(manager, found["python"], sockets)
    try:
        await kernel.ready()
    except Exception:
        await kernel.shutdown()
        raise
    return kernel


class Launcher:
    """The Workbench's kernels port (services/workbench.py): kernels, and the environments they run in."""

    def create_environment(
        self, folder: str, version: str | None, packages: list[str]
    ) -> AsyncIterator[dict[str, Any]]:
        from swissarmygrid.server import environments

        return environments.create(folder, version, packages)

    def beside(self, folder: str) -> str | None:
        from swissarmygrid.server import environments

        return environments.beside(folder)

    async def start(self, python: str, folder: str | None, environ: dict[str, str]) -> Kernel:
        return await start(python, folder, environ)

    def check_python(self, python: str) -> dict[str, Any]:
        return check_python(python)

    def candidates(self, folders: list[str]) -> list[str]:
        return candidates(folders)
